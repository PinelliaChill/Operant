from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from operant.api import create_app
from operant.api_workbench_context import (
    WorkbenchReferenceRequest,
    create_reference,
    install_workbench_context_routes,
    read_context_reference,
)
from operant.application.service import ApplicationService
from operant.domain.models import ModelProfile, RolePreset, ToolPolicy
from operant.domain.threads import ConversationThread, Item, Turn, UserMessagePayload
from operant.persistence.sqlite import SQLiteStore
from operant.providers.openai_compatible import OpenAICompatibleProvider


def scope(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = ApplicationService(
        SQLiteStore(tmp_path / "core.sqlite3"),
        OpenAICompatibleProvider(),
        artifact_root=tmp_path / "artifacts",
    )
    service.initialize()
    profile = service.add_model_profile(
        ModelProfile(
            name="test",
            model_id="test",
            base_url="https://example.invalid/v1",
            secret_ref="TEST_WORKBENCH_KEY",
        )
    )
    role = service.create_role(
        RolePreset(
            name="reader",
            system_prompt="read",
            model_profile_id=profile.id,
            tool_policy=ToolPolicy(allowed_tools=("read_file",)),
        )
    )
    thread = service.create_thread(ConversationThread(workspace_ref=str(workspace)))
    service.create_session(role.id, thread_id=thread.id)
    return service, thread, workspace, role


def test_file_snapshot_is_metadata_first_scoped_and_immutable(tmp_path):
    service, thread, workspace, role = scope(tmp_path)
    (workspace / "brief.txt").write_text("original source")
    view = create_reference(
        service, thread.id, WorkbenchReferenceRequest(kind="file", target="brief.txt")
    )
    assert view.reference.include_mode.value == "metadata"
    assert "original source" not in view.summary
    (workspace / "brief.txt").write_text("changed later")
    result = read_context_reference(service, thread.id, (view.reference,), view.reference.target_id)
    assert result["content"] == "original source"
    with pytest.raises(PermissionError):
        read_context_reference(service, thread.id, (), view.reference.target_id)
    other = service.create_thread(ConversationThread(workspace_ref=str(workspace)))
    service.create_session(role.id, thread_id=other.id)
    with pytest.raises(PermissionError):
        read_context_reference(service, other.id, (view.reference,), view.reference.target_id)


@pytest.mark.parametrize("target", ["../outside", ".env", "link.txt", "folder/link.txt"])
def test_reference_rejects_escape_protected_and_symlinks(tmp_path, target):
    service, thread, workspace, _ = scope(tmp_path)
    (tmp_path / "outside").write_text("private")
    (workspace / ".env").write_text("private")
    (workspace / "link.txt").symlink_to(tmp_path / "outside")
    (workspace / "folder").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises((PermissionError, OSError)):
        create_reference(service, thread.id, WorkbenchReferenceRequest(kind="file", target=target))


def test_thread_reference_copies_only_public_history_and_rejects_cross_workspace(tmp_path):
    service, thread, workspace, role = scope(tmp_path)
    source = service.create_thread(ConversationThread(workspace_ref=str(workspace)))
    service.create_session(role.id, thread_id=source.id)
    turn = service.create_turn(Turn(thread_id=source.id))
    service.append_item(
        Item(
            thread_id=source.id,
            turn_id=turn.id,
            payload=UserMessagePayload(text="explicit shared fact"),
        )
    )
    view = create_reference(
        service, thread.id, WorkbenchReferenceRequest(kind="thread", target=source.id)
    )
    result = read_context_reference(service, thread.id, (view.reference,), view.reference.target_id)
    assert result["content"] == "user_message: explicit shared fact"
    unrelated = service.create_thread(ConversationThread(workspace_ref=str(tmp_path)))
    with pytest.raises(PermissionError):
        create_reference(
            service, thread.id, WorkbenchReferenceRequest(kind="thread", target=unrelated.id)
        )


def test_command_parameter_error_and_clear_keep_history(tmp_path):
    service, thread, _, _ = scope(tmp_path)
    turn = service.create_turn(Turn(thread_id=thread.id))
    service.append_item(
        Item(
            thread_id=thread.id,
            turn_id=turn.id,
            payload=UserMessagePayload(text="preserved history"),
        )
    )
    app = create_app(service.store.path)
    # Install explicitly while runtime integration is being built; use same production middleware.
    if not any(getattr(r, "path", "") == "/v1/workbench/commands" for r in app.routes):
        install_workbench_context_routes(app, service)
    with TestClient(app) as client:
        path = f"/v1/workbench/threads/{thread.id}/commands"
        invalid = client.post(path, json={"text": "/clear-context extra"})
        assert invalid.status_code == 400
        cleared = client.post(
            path, json={"text": "/clear-context"}, headers={"Idempotency-Key": "clear-once"}
        )
        assert cleared.status_code == 200, cleared.text
        replay = client.post(
            path, json={"text": "/clear-context"}, headers={"Idempotency-Key": "clear-once"}
        )
        assert replay.status_code == 200
        assert replay.json()["resource_id"] == cleared.json()["resource_id"]
        assert service.list_items(thread.id)[0].payload.text == "preserved history"
