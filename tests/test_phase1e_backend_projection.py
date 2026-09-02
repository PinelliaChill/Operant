from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import operant.application.client_projection as client_projection
from operant.api import create_app
from operant.application.client_projection import WorkspaceProjectionError
from operant.application.protocol_metadata import (
    ProtocolSchemaUnavailable,
    phase1e_protocol_metadata,
)
from operant.application.service import ApplicationService
from operant.domain.commands import WorkspaceInitialization
from operant.domain.threads import ConversationThread
from operant.domain.workflow import WorkflowRun
from operant.persistence.sqlite import SQLiteStore
from operant.providers.openai_compatible import OpenAICompatibleProvider


def _workspace_initialization(
    service: ApplicationService, workspace: Path
) -> WorkspaceInitialization:
    initialization, created = service.initialize_workspace(workspace)
    assert created is True
    return initialization


def _workflow(workspace: Path, *, run_id: str) -> WorkflowRun:
    return WorkflowRun(
        id=run_id,
        task="inspect the registered workspace",
        workspace=str(workspace),
        planner_role_id="role-planner",
        coder_role_id="role-coder",
        reviewer_role_id="role-reviewer",
    )


def test_protocol_metadata_reads_only_an_injected_generated_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest_path = tmp_path / "generated.sha256"
    digest = hashlib.sha256(b"public schema").hexdigest()
    digest_path.write_text(f"{digest}  operant-phase1e.openapi.json\n", encoding="utf-8")
    monkeypatch.setenv("OPERANT_PHASE1E_SCHEMA_DIGEST_PATH", str(digest_path))

    metadata = phase1e_protocol_metadata()

    assert metadata["protocol_version"] == "phase1e.v1"
    assert metadata["schema_digest"] == digest
    assert metadata["min_client_version"] == "phase1e.v1"
    assert "workspace_file_metadata" in metadata["capabilities"]

    digest_path.write_text("not-a-digest\n", encoding="utf-8")
    with pytest.raises(ProtocolSchemaUnavailable):
        phase1e_protocol_metadata()


def test_protocol_endpoint_fails_closed_without_digest_and_accepts_generated_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The integration checkout contains the generated digest by design.  Point
    # the Core at a definitely missing absolute path so this branch exercises
    # the fail-closed deployment boundary rather than the source-checkout
    # fallback.
    missing_digest = tmp_path / "missing" / "operant-phase1e.openapi.sha256"
    monkeypatch.setenv("OPERANT_PHASE1E_SCHEMA_DIGEST_PATH", str(missing_digest))
    app = create_app(tmp_path / "protocol.sqlite3", artifact_root=tmp_path / "artifacts")
    with TestClient(app) as client:
        unavailable = client.get("/v1/protocol")
    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["code"] == "protocol_schema_unavailable"

    digest_path = tmp_path / "generated.sha256"
    digest_path.write_text("a" * 64, encoding="utf-8")
    monkeypatch.setenv("OPERANT_PHASE1E_SCHEMA_DIGEST_PATH", str(digest_path))
    app_with_digest = create_app(
        tmp_path / "protocol-with-digest.sqlite3", artifact_root=tmp_path / "artifacts-2"
    )
    with TestClient(app_with_digest) as client:
        response = client.get("/v1/protocol")
    assert response.status_code == 200
    assert response.json()["schema_digest"] == "a" * 64


def test_projects_only_join_exact_workspace_facts(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    service = ApplicationService(
        SQLiteStore(tmp_path / "projects.sqlite3"),
        OpenAICompatibleProvider(),
        artifact_root=tmp_path / "artifacts",
    )
    service.initialize()
    initialization = _workspace_initialization(service, workspace)
    replay, created = service.initialize_workspace(workspace)
    assert replay == initialization
    assert created is False

    exact_thread = service.create_thread(ConversationThread(workspace_ref=str(workspace.resolve())))
    service.create_thread(ConversationThread(workspace_ref=str(other.resolve())))
    service.create_thread(ConversationThread(workspace_ref=str(workspace / "alias")))
    exact_run = service.create_workflow_run(_workflow(workspace, run_id="workflow-exact"))
    service.create_workflow_run(_workflow(other, run_id="workflow-other"))

    projects = service.list_project_projections()

    assert len(projects) == 1
    project = projects[0]
    assert project.project_id == initialization.id
    assert project.workspace_ref == str(workspace.resolve())
    assert [thread.id for thread in project.threads] == [exact_thread.id]
    assert [run.id for run in project.workflow_runs] == [exact_run.id]


def test_projects_endpoint_serializes_domain_projection(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = create_app(tmp_path / "projects-api.sqlite3", artifact_root=tmp_path / "artifacts")
    service: ApplicationService = app.state.operant_service
    initialization = _workspace_initialization(service, workspace)
    thread = service.create_thread(ConversationThread(workspace_ref=str(workspace.resolve())))

    with TestClient(app) as client:
        response = client.get("/v1/projects", params={"limit": 1})

    assert response.status_code == 200
    assert response.json() == [
        {
            "project_id": initialization.id,
            "workspace_ref": str(workspace.resolve()),
            "readable": True,
            "writable": True,
            "created_at": initialization.created_at.isoformat().replace("+00:00", "Z"),
            "threads": [
                {
                    "id": thread.id,
                    "status": "active",
                    "created_at": thread.created_at.isoformat().replace("+00:00", "Z"),
                    "updated_at": thread.updated_at.isoformat().replace("+00:00", "Z"),
                }
            ],
            "workflow_runs": [],
        }
    ]


def test_projects_endpoint_bounds_redacted_workflow_summary_to_model_limit(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = create_app(tmp_path / "long-task.sqlite3", artifact_root=tmp_path / "artifacts")
    service: ApplicationService = app.state.operant_service
    _workspace_initialization(service, workspace)
    service.create_workflow_run(
        WorkflowRun(
            id="workflow-long-task",
            task="x" * 500,
            workspace=str(workspace),
            planner_role_id="role-planner",
            coder_role_id="role-coder",
            reviewer_role_id="role-reviewer",
        )
    )

    with TestClient(app) as client:
        response = client.get("/v1/projects")

    assert response.status_code == 200
    summary = response.json()[0]["workflow_runs"][0]["summary"]
    assert len(summary) == 500
    assert summary.endswith("...[truncated]")


def test_projects_endpoint_does_not_misreport_persisted_projection_error_as_cursor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = create_app(tmp_path / "projection-error.sqlite3", artifact_root=tmp_path / "artifacts")
    service: ApplicationService = app.state.operant_service
    _workspace_initialization(service, workspace)

    def raise_persisted_error(**_: object) -> list[object]:
        raise ValueError("persisted workflow row is invalid")

    monkeypatch.setattr(service.store, "list_workflow_runs", raise_persisted_error)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/v1/projects")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "project_projection_unavailable"


def test_workspace_file_projection_is_metadata_only_safe_and_paged(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "alpha.txt").write_text("public", encoding="utf-8")
    (workspace / "beta.txt").write_text("public", encoding="utf-8")
    (workspace / "gamma.txt").write_text("public", encoding="utf-8")
    (workspace / "api-key.json").write_text('{"api_key":"private"}', encoding="utf-8")
    (workspace / ".env.production").write_text("PRIVATE", encoding="utf-8")
    (workspace / "credentials-backup.json").write_text("PRIVATE", encoding="utf-8")
    nested = workspace / "nested"
    nested.mkdir()
    (nested / "inside.txt").write_text("inside", encoding="utf-8")
    (workspace / "link-to-secret").symlink_to(workspace / "api-key.json")
    (workspace / "link-to-nested").symlink_to(nested, target_is_directory=True)

    app = create_app(tmp_path / "files.sqlite3", artifact_root=tmp_path / "artifacts")
    service: ApplicationService = app.state.operant_service
    initialization = _workspace_initialization(service, workspace)
    with TestClient(app) as client:
        first = client.get(
            f"/v1/workspaces/{initialization.id}/files",
            params={"limit": 2},
        )
        assert first.status_code == 200
        first_payload = first.json()
        assert [entry["name"] for entry in first_payload["entries"]] == [
            "alpha.txt",
            "beta.txt",
        ]
        assert first_payload["next_page_token"]
        assert "public" not in first.text
        assert str(workspace) not in first.text
        assert "inode" not in first.text
        assert "api-key.json" not in first.text
        assert ".env.production" not in first.text
        assert "credentials-backup.json" not in first.text
        assert "link-to-secret" not in first.text
        assert "link-to-nested" not in first.text

        second = client.get(
            f"/v1/workspaces/{initialization.id}/files",
            params={"page_token": first_payload["next_page_token"]},
        )
        assert second.status_code == 200
        assert [entry["name"] for entry in second.json()["entries"]] == ["gamma.txt", "nested"]

        nested_listing = client.get(
            f"/v1/workspaces/{initialization.id}/files",
            params={"path": "nested"},
        )
        assert nested_listing.status_code == 200
        assert nested_listing.json()["entries"][0]["path"] == "nested/inside.txt"
        assert nested_listing.json()["entries"][0]["type"] == "file"

        for unsafe_path in ("../", "/tmp", "C:/tmp", "C:tmp", "nested\\inside.txt", "nested/../"):
            rejected = client.get(
                f"/v1/workspaces/{initialization.id}/files",
                params={"path": unsafe_path},
            )
            assert rejected.status_code == 400
            assert rejected.json()["error"]["code"] in {
                "workspace_path_invalid",
                "workspace_path_absolute",
            }

        alias = client.get(
            f"/v1/workspaces/{initialization.id}/files",
            params={"path": "NESTED"},
        )
        assert alias.status_code == 400
        assert alias.json()["error"]["code"] == "workspace_path_case_mismatch"

        symlink = client.get(
            f"/v1/workspaces/{initialization.id}/files",
            params={"path": "link-to-nested"},
        )
        assert symlink.status_code == 403
        assert symlink.json()["error"]["code"] == "workspace_symlink_forbidden"

        changed = workspace / "changed.txt"
        changed.write_text("new", encoding="utf-8")
        stale = client.get(
            f"/v1/workspaces/{initialization.id}/files",
            params={"page_token": first_payload["next_page_token"]},
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "workspace_directory_changed"


def test_workspace_file_projection_fails_closed_when_registered_root_is_replaced(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "before.txt").write_text("before", encoding="utf-8")
    app = create_app(tmp_path / "root-replace.sqlite3", artifact_root=tmp_path / "artifacts")
    service: ApplicationService = app.state.operant_service
    initialization = _workspace_initialization(service, workspace)

    with TestClient(app) as client:
        assert client.get(f"/v1/workspaces/{initialization.id}/files").status_code == 200
        moved = tmp_path / "old-workspace"
        shutil.move(workspace, moved)
        workspace.mkdir()
        (workspace / "after.txt").write_text("after", encoding="utf-8")
        replaced = client.get(f"/v1/workspaces/{initialization.id}/files")

    assert replaced.status_code == 409
    assert replaced.json()["error"]["code"] == "workspace_directory_changed"


def test_workspace_file_projection_rejects_replaced_registered_parent_symlink(
    tmp_path: Path,
) -> None:
    registered_parent = tmp_path / "registered-parent"
    registered_parent.mkdir()
    workspace = registered_parent / "workspace"
    workspace.mkdir()
    (workspace / "before.txt").write_text("before", encoding="utf-8")

    attacker_parent = tmp_path / "attacker-parent"
    attacker_parent.mkdir()
    attacker_workspace = attacker_parent / "workspace"
    attacker_workspace.mkdir()
    (attacker_workspace / "after.txt").write_text("after", encoding="utf-8")

    app = create_app(tmp_path / "parent-symlink.sqlite3", artifact_root=tmp_path / "artifacts")
    service: ApplicationService = app.state.operant_service
    initialization = _workspace_initialization(service, workspace)

    shutil.move(registered_parent, tmp_path / "old-parent")
    registered_parent.symlink_to(attacker_parent, target_is_directory=True)

    with TestClient(app) as client:
        replaced = client.get(f"/v1/workspaces/{initialization.id}/files")

    assert replaced.status_code == 409
    assert replaced.json()["error"]["code"] == "workspace_directory_changed"
    assert "after.txt" not in replaced.text


def test_workspace_file_projection_catches_root_replacement_during_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "before.txt").write_text("before", encoding="utf-8")
    service = ApplicationService(
        SQLiteStore(tmp_path / "toctou.sqlite3"),
        OpenAICompatibleProvider(),
        artifact_root=tmp_path / "artifacts",
    )
    service.initialize()
    _workspace_initialization(service, workspace)
    original_scan = client_projection._scan_directory
    scan_calls = 0

    def replace_after_first_scan(fd: int, canonical_parts: tuple[str, ...]):
        nonlocal scan_calls
        result = original_scan(fd, canonical_parts)
        scan_calls += 1
        if scan_calls == 1:
            shutil.move(workspace, tmp_path / "old-workspace")
            workspace.mkdir()
            (workspace / "after.txt").write_text("after", encoding="utf-8")
        return result

    monkeypatch.setattr(client_projection, "_scan_directory", replace_after_first_scan)
    with pytest.raises(WorkspaceProjectionError) as raised:
        service.list_workspace_files(next(iter(service.store.list_workspace_initializations())).id)

    assert raised.value.code == "workspace_directory_changed"
    assert scan_calls == 2


def test_workspace_file_projection_rejects_manifest_overflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "first.txt").write_text("first", encoding="utf-8")
    (workspace / "second.txt").write_text("second", encoding="utf-8")
    app = create_app(tmp_path / "manifest-limit.sqlite3", artifact_root=tmp_path / "artifacts")
    service: ApplicationService = app.state.operant_service
    initialization = _workspace_initialization(service, workspace)
    monkeypatch.setattr(client_projection, "MAX_WORKSPACE_DIRECTORY_ENTRIES", 1)

    with TestClient(app) as client:
        response = client.get(f"/v1/workspaces/{initialization.id}/files")

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "workspace_directory_too_large"
    assert response.json()["error"]["recovery"] == "none"
    assert response.json()["error"]["retryable"] is False


def test_projects_fail_closed_instead_of_silently_omitting_too_many_threads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = create_app(tmp_path / "thread-limit.sqlite3", artifact_root=tmp_path / "artifacts")
    service: ApplicationService = app.state.operant_service
    _workspace_initialization(service, workspace)
    service.create_thread(ConversationThread(workspace_ref=str(workspace.resolve())))
    service.create_thread(ConversationThread(workspace_ref=str(workspace.resolve())))
    monkeypatch.setattr(client_projection, "MAX_PROJECT_THREADS", 1)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/v1/projects")

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "project_projection_too_large"
    assert response.json()["error"]["recovery"] == "none"
    assert response.json()["error"]["retryable"] is False


def test_projects_fail_closed_instead_of_silently_omitting_too_many_workflow_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = create_app(tmp_path / "workflow-limit.sqlite3", artifact_root=tmp_path / "artifacts")
    service: ApplicationService = app.state.operant_service
    _workspace_initialization(service, workspace)
    for run_id in ("workflow-one", "workflow-two"):
        service.create_workflow_run(_workflow(workspace, run_id=run_id))
    monkeypatch.setattr(client_projection, "MAX_PROJECT_WORKFLOW_RUNS", 1)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/v1/projects")

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "project_projection_too_large"
    assert response.json()["error"]["recovery"] == "none"
    assert response.json()["error"]["retryable"] is False


def test_workspace_file_projection_rejects_malformed_or_conflicting_page_state(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("content", encoding="utf-8")
    app = create_app(tmp_path / "page-state.sqlite3", artifact_root=tmp_path / "artifacts")
    service: ApplicationService = app.state.operant_service
    initialization = _workspace_initialization(service, workspace)

    with TestClient(app) as client:
        malformed = client.get(
            f"/v1/workspaces/{initialization.id}/files",
            params={"page_token": "%%%"},
        )
        assert malformed.status_code == 400
        assert malformed.json()["error"]["code"] == "workspace_page_token_invalid"

        conflicting = client.get(
            f"/v1/workspaces/{initialization.id}/files",
            params={"after_name": "a", "after": "b"},
        )
        assert conflicting.status_code == 400
        assert conflicting.json()["error"]["code"] == "workspace_page_token_invalid"

        unknown_workspace = client.get("/v1/workspaces/missing/files")
        assert unknown_workspace.status_code == 404
        assert unknown_workspace.json()["error"]["code"] == "workspace_not_registered"
