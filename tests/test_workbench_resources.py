from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from operant.api import create_app
from operant.api_workbench_context import WorkbenchReferenceRequest, create_reference
from operant.application.resource_governance import ResourceGovernanceService
from operant.application.service import ApplicationService
from operant.domain.models import ModelProfile, RolePreset, ToolPolicy, utc_now
from operant.domain.threads import (
    ArtifactAccessLevel,
    ArtifactSensitivity,
    ArtifactSourceRef,
    ArtifactSourceType,
    ConversationThread,
    Item,
    RetentionLifecycle,
    Turn,
    UserMessagePayload,
)
from operant.persistence.sqlite import MigrationError, SQLiteStore
from operant.providers.openai_compatible import OpenAICompatibleProvider


def make_scope(tmp_path: Path):
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
    session = service.create_session(role.id, thread_id=thread.id)
    return service, thread, session, workspace


def snapshot(service: ApplicationService, thread_id: str, workspace: Path, text: str) -> str:
    (workspace / "brief.txt").write_text(text)
    view = create_reference(
        service, thread_id, WorkbenchReferenceRequest(kind="file", target="brief.txt")
    )
    return f"artifact:{view.reference.target_id}"


def make_due(governance: ResourceGovernanceService, thread_id: str) -> None:
    policy = governance.repository.get(thread_id)
    governance.repository.save(
        thread_id,
        policy.model_copy(update={"completed_at": utc_now() - timedelta(hours=2)}),
    )


def advance_cleanup_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "operant.application.resource_governance.utc_now",
        lambda: utc_now() + timedelta(hours=2),
    )


def test_v21_migration_preserves_history_and_only_empty_governance_can_roll_back(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "migration.sqlite3")
    assert store.migrate(20) == 20
    thread = store.create_thread(ConversationThread())
    turn = store.create_turn(Turn(thread_id=thread.id))
    item = store.append_item(
        Item(thread_id=thread.id, turn_id=turn.id, payload=UserMessagePayload(text="保留历史"))
    )
    before = store.list_applied_migrations()
    assert store.migrate(21) == 21
    assert store.list_applied_migrations()[:20] == before
    assert store.list_items(thread.id) == [item]
    assert store.rollback(20, isolated=True) == 20
    assert store.list_items(thread.id) == [item]
    store.migrate(21)
    from operant.persistence.resource_governance import ResourcePolicyRepository

    policies = ResourcePolicyRepository(store)
    policies.save(thread.id, policies.get(thread.id))
    with pytest.raises(MigrationError, match="rollback is unsafe"):
        store.rollback(20, isolated=True)
    assert store.schema_version() == 21
    assert store.list_items(thread.id) == [item]


def test_two_real_snapshots_pin_preview_and_physical_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, thread, _session, workspace = make_scope(tmp_path)
    first = snapshot(service, thread.id, workspace, "first")
    second = snapshot(service, thread.id, workspace, "second")
    assert first != second
    governance = ResourceGovernanceService(service)
    found = {item.id: item for item in governance.inventory(thread.id).resources}
    assert found[first].kind == "context_reference_snapshot"
    assert found[second].size_bytes > 0
    make_due(governance, thread.id)
    assert (
        not governance.preview_cleanup(thread.id, resource_ids=[first], mode="automatic")
        .items[0]
        .eligible
    )
    advance_cleanup_clock(monkeypatch)
    assert (
        governance.preview_cleanup(thread.id, resource_ids=[first], mode="automatic")
        .items[0]
        .eligible
    )
    governance.pin(thread.id, first, pinned=True)
    assert (
        not governance.preview_cleanup(thread.id, resource_ids=[first], mode="automatic")
        .items[0]
        .eligible
    )
    assert governance.cleanup(thread.id, resource_ids=[first], mode="automatic").removed_ids == []
    governance.pin(thread.id, first, pinned=False)
    assert governance.cleanup(
        thread.id, resource_ids=[first, second], mode="automatic"
    ).removed_ids == [first, second]
    assert service.get_artifact_retention_state(first.removeprefix("artifact:")).lifecycle is (
        RetentionLifecycle.DELETED
    )


def test_active_run_blocks_snapshot_even_after_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, thread, session, workspace = make_scope(tmp_path)
    resource_id = snapshot(service, thread.id, workspace, "data")
    governance = ResourceGovernanceService(service)
    make_due(governance, thread.id)
    advance_cleanup_clock(monkeypatch)
    assert (
        governance.preview_cleanup(thread.id, resource_ids=[resource_id], mode="automatic")
        .items[0]
        .eligible
    )
    now = utc_now()
    with service.store._connect() as connection:
        connection.execute(
            """INSERT INTO session_run_leases
            (session_id,lease_token,owner_id,generation,workflow_run_id,agent_id,
             cancel_requested,acquired_at,renewed_at,expires_at,released_at)
            VALUES (?,?,?,1,NULL,NULL,0,?,?,?,NULL)""",
            (
                session.id,
                "lease-test",
                "owner",
                now.isoformat(),
                now.isoformat(),
                (now + timedelta(hours=1)).isoformat(),
            ),
        )
    preview = governance.preview_cleanup(
        thread.id, resource_ids=[resource_id], mode="automatic"
    ).items[0]
    assert not preview.eligible
    assert "active_or_recoverable_run" in preview.reason
    assert (
        governance.cleanup(thread.id, resource_ids=[resource_id], mode="automatic").removed_ids
        == []
    )


def test_missing_blob_after_trash_is_marked_unknown_and_not_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, thread, _session, workspace = make_scope(tmp_path)
    resource_id = snapshot(service, thread.id, workspace, "data")
    governance = ResourceGovernanceService(service)
    make_due(governance, thread.id)
    advance_cleanup_clock(monkeypatch)
    artifact_id = resource_id.removeprefix("artifact:")
    for operation, action in (
        ("retention_schedule", service.schedule_artifact_deletion),
        ("retention_trash", service.trash_artifact),
    ):
        capability = service.issue_artifact_capability(
            artifact_id, operation=operation, access_level=ArtifactAccessLevel.NORMAL
        )
        action(artifact_id, capability=capability)
    artifact = service.get_artifact(artifact_id)
    blob_store = service._artifact_blob_store()
    with blob_store.mutation_guard():
        blob_store.delete_verified(artifact.content_hash, artifact.size_bytes)
    result = governance.cleanup(thread.id, resource_ids=[resource_id], mode="automatic")
    assert result.removed_ids == []
    assert service.get_artifact_retention_state(artifact_id).lifecycle is RetentionLifecycle.TRASHED
    assert (
        governance.preview_cleanup(thread.id, resource_ids=[resource_id], mode="automatic")
        .items[0]
        .reason
        == "physical_outcome_unknown"
    )


def test_concurrent_policy_and_completion_updates_do_not_erase_each_other(tmp_path: Path) -> None:
    service, thread, _session, _workspace = make_scope(tmp_path)
    governance = ResourceGovernanceService(service)
    for _ in range(10):
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(
                governance.update_policy,
                thread.id,
                completed_ttl_seconds=7200,
                unanswered_ttl_seconds=345600,
            )
            second = pool.submit(governance.confirm, thread.id, completed=True)
            first.result()
            second.result()
        policy = governance.repository.get(thread.id)
        assert policy.completed_ttl_seconds == 7200
        assert policy.unanswered_ttl_seconds == 345600
        assert policy.completed_at is not None


def test_new_snapshot_resets_old_completion_and_session_artifact_can_pin(tmp_path: Path) -> None:
    service, thread, session, workspace = make_scope(tmp_path)
    governance = ResourceGovernanceService(service)
    governance.confirm(thread.id, completed=True)
    resource_id = snapshot(service, thread.id, workspace, "新的内容")
    assert governance.repository.get(thread.id).completed_at is None
    assert (
        not governance.preview_cleanup(thread.id, resource_ids=[resource_id], mode="automatic")
        .items[0]
        .eligible
    )
    ordinary, _ = service.create_artifact(
        content="保留成果".encode(),
        media_type="text/plain",
        source_refs=(
            ArtifactSourceRef(source_type=ArtifactSourceType.SESSION, source_id=session.id),
        ),
    )
    ordinary_id = f"artifact:{ordinary.id}"
    assert ordinary_id in {item.id for item in governance.inventory(thread.id).resources}
    assert governance.pin(thread.id, ordinary_id, pinned=True).pinned
    assert (
        governance.preview_cleanup(thread.id, resource_ids=[ordinary_id], mode="manual")
        .items[0]
        .eligible
        is False
    )
    sensitive, _ = service.create_artifact(
        content=b"sensitive",
        media_type="text/plain",
        sensitivity=ArtifactSensitivity.SENSITIVE,
        source_refs=(
            ArtifactSourceRef(source_type=ArtifactSourceType.SESSION, source_id=session.id),
        ),
    )
    with pytest.raises(PermissionError):
        governance.pin(thread.id, f"artifact:{sensitive.id}", pinned=True)


def test_restart_worker_reclaims_due_snapshot_with_default_physical_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, thread, _session, workspace = make_scope(tmp_path)
    resource_id = snapshot(service, thread.id, workspace, "data")
    governance = ResourceGovernanceService(service)
    make_due(governance, thread.id)
    service.close()
    advance_cleanup_clock(monkeypatch)
    app = create_app(tmp_path / "core.sqlite3")
    with TestClient(app):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if (
                app.state.operant_service.get_artifact_retention_state(
                    resource_id.removeprefix("artifact:")
                ).lifecycle
                is RetentionLifecycle.DELETED
            ):
                break
            time.sleep(0.05)
        else:
            pytest.fail("startup resource worker did not reclaim the due snapshot")


def test_inventory_reports_utf8_payload_bytes(tmp_path: Path) -> None:
    service, thread, _session, _workspace = make_scope(tmp_path)
    turn = service.create_turn(Turn(thread_id=thread.id))
    service.append_item(
        Item(thread_id=thread.id, turn_id=turn.id, payload=UserMessagePayload(text="中文资源"))
    )
    with service.store._connect() as connection:
        size = connection.execute(
            "SELECT SUM(length(CAST(body AS BLOB))) FROM items WHERE thread_id=?",
            (thread.id,),
        ).fetchone()[0]
    history = next(
        item
        for item in ResourceGovernanceService(service).inventory(thread.id).resources
        if item.kind == "thread_history"
    )
    assert history.size_bytes == size


def test_scan_rotates_threads_and_resumes_trashed_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, first_thread, session, workspace = make_scope(tmp_path)
    governance = ResourceGovernanceService(service)
    thread_ids = [first_thread.id]
    resource_ids = [snapshot(service, first_thread.id, workspace, "first")]
    for index in (2, 3):
        thread = service.create_thread(ConversationThread(workspace_ref=str(workspace)))
        service.create_session(session.role_snapshot.role_id, thread_id=thread.id)
        thread_ids.append(thread.id)
        resource_ids.append(snapshot(service, thread.id, workspace, f"value-{index}"))
    for thread_id in thread_ids:
        make_due(governance, thread_id)
    advance_cleanup_clock(monkeypatch)
    first_artifact = resource_ids[0].removeprefix("artifact:")
    for operation, action in (
        ("retention_schedule", service.schedule_artifact_deletion),
        ("retention_trash", service.trash_artifact),
    ):
        capability = service.issue_artifact_capability(
            first_artifact, operation=operation, access_level=ArtifactAccessLevel.NORMAL
        )
        action(first_artifact, capability=capability)
    for _ in range(4):
        governance.scan_due(thread_limit=1, resource_limit=1)
    assert all(
        service.get_artifact_retention_state(resource_id.removeprefix("artifact:")).lifecycle
        is RetentionLifecycle.DELETED
        for resource_id in resource_ids
    )
