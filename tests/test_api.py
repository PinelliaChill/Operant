from pathlib import Path

from fastapi.testclient import TestClient

from operant.api import create_app
from operant.domain.workflow import WorkflowRun, WorkflowRunEvent, WorkflowRunStatus
from operant.persistence.sqlite import SQLiteStore


def test_health_and_empty_registries(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "api.sqlite3")) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/v1/models").json() == []
        assert client.get("/v1/roles").json() == []


def test_registry_crud_default_roles_and_snapshot_override(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "api.sqlite3")) as client:
        model_payload = {
            "name": "relay",
            "model_id": "planner-id",
            "base_url": "https://relay.example.com/v1",
            "secret_ref": "OPERANT_TEST_KEY",
        }
        model = client.post("/v1/models", json=model_payload)
        assert model.status_code == 201
        model_id = model.json()["id"]

        patched = client.patch(
            f"/v1/models/{model_id}",
            json={"name": "relay-updated"},
        )
        assert patched.status_code == 200
        assert patched.json()["name"] == "relay-updated"

        seeded = client.post(
            "/v1/roles/seed-defaults",
            json={
                "planner_model_profile_id": model_id,
                "coder_model_profile_id": model_id,
                "reviewer_model_profile_id": model_id,
            },
        )
        assert seeded.status_code == 200
        assert len(seeded.json()) == 5

        session = client.post(
            "/v1/sessions",
            json={
                "role_id": "role_planner",
                "effort": "high",
                "budget_overrides": {"timeout_seconds": 17},
            },
        )
        assert session.status_code == 201
        snapshot = session.json()["role_snapshot"]
        assert snapshot["role_id"] == "role_planner"
        assert snapshot["overrides"]["effort_overridden"]
        assert snapshot["budget"]["timeout_seconds"] == 17

        updated_role = client.patch(
            "/v1/roles/role_planner",
            json={"system_prompt": "A new prompt."},
        )
        assert updated_role.status_code == 200
        assert updated_role.json()["version"] == 2

        old_session = client.get(f"/v1/sessions/{session.json()['id']}").json()
        assert old_session["role_snapshot"]["role_version"] == 1
        assert old_session["role_snapshot"]["system_prompt"] != "A new prompt."

        coder_session = client.post("/v1/sessions", json={"role_id": "role_coder"}).json()
        memory = client.post(
            "/v1/memories",
            json={
                "session_id": coder_session["id"],
                "kind": "project",
                "content": "Use pytest for calculator verification.",
                "project_scope": str(tmp_path),
                "source_task": "Task A pytest verification",
                "confidence": 0.95,
                "confirmed": True,
            },
        )
        assert memory.status_code == 201
        reused = client.get(
            "/v1/memories/search",
            params={
                "session_id": session.json()["id"],
                "query": "pytest calculator",
                "project_scope": str(tmp_path),
            },
        )
        assert [item["id"] for item in reused.json()] == [memory.json()["id"]]

        writable_explorer = client.post(
            "/v1/roles",
            json={
                "name": "Writable Explorer",
                "system_prompt": "Inspect and edit files.",
                "model_profile_id": model_id,
                "tool_policy": {
                    "allowed_tools": ["apply_patch"],
                    "workspace_write": True,
                },
            },
        )
        assert writable_explorer.status_code == 201

        invalid_workflow = client.post(
            "/v1/workflows/coding/runs",
            json={
                "task": "Inspect the project",
                "workspace": str(tmp_path),
                "explorer_role_ids": [writable_explorer.json()["id"]],
            },
        )
        assert invalid_workflow.status_code == 400
        assert "explorer[1] role must be read-only" in invalid_workflow.json()["detail"]


def test_task_queries_trace_export_and_cancel(tmp_path: Path) -> None:
    database = tmp_path / "tasks.sqlite3"
    app = create_app(database)
    store = SQLiteStore(database)
    run = store.create_workflow_run(
        WorkflowRun(
            task="Inspect the project",
            workspace=str(tmp_path),
            planner_role_id="role_planner",
            coder_role_id="role_coder",
            reviewer_role_id="role_reviewer",
            status=WorkflowRunStatus.INTERRUPTED,
        )
    )
    store.append_workflow_event(
        WorkflowRunEvent(
            workflow_run_id=run.id,
            role="workflow",
            event_type="workflow.started",
        )
    )

    with TestClient(app) as client:
        listed = client.get("/v1/tasks")
        assert [item["id"] for item in listed.json()] == [run.id]
        assert client.get(f"/v1/tasks/{run.id}").json()["status"] == "interrupted"
        assert client.get(f"/v1/tasks/{run.id}/events").json()[0]["sequence"] == 1
        trace = client.get(f"/v1/tasks/{run.id}/trace")
        assert trace.json()["workflow_run_id"] == run.id
        exported = client.get(f"/v1/tasks/{run.id}/trace.jsonl")
        assert exported.headers["content-type"].startswith("application/x-ndjson")
        assert '"record_type":"trace.workflow"' in exported.text
        assert client.post(f"/v1/tasks/{run.id}/cancel").json() == {"accepted": True}
        assert client.get(f"/v1/tasks/{run.id}").json()["status"] == "cancelled"
