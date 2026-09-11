"""Focused B2-2 API contract tests.

These tests exercise the public B2 projection over durable Core facts.  Test
fixtures may seed the SQLite store directly, but all assertions go through
the HTTP API so a projection cannot pass by returning an in-memory fixture.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from operant.api import create_app
from operant.application.graph import GraphCompilationError, GraphCompiler
from operant.domain.graph import NodeKind, NodeSpec, WorkflowDefinition
from operant.domain.threads import (
    ConversationThread,
    Item,
    LegacySourceType,
    ThreadLegacyRef,
    ThreadStatus,
    Turn,
    UserMessagePayload,
)
from operant.domain.workflow import WorkflowRun, WorkflowRunEvent, WorkflowRunStatus


@pytest.fixture()
def client(tmp_path: Path) -> Any:
    with TestClient(create_app(tmp_path / "b2-api.sqlite3")) as test_client:
        yield test_client


def _bootstrap(client: TestClient) -> tuple[str, str]:
    model_response = client.post(
        "/v1/models",
        json={
            "name": "B2 test provider",
            "model_id": "b2-test-model-v1",
            "base_url": "https://provider.example.test/v1",
            "secret_ref": "OPERANT_B2_TEST_KEY",
        },
        headers={"Idempotency-Key": "b2-model-create"},
    )
    assert model_response.status_code == 201, model_response.text
    model_id = model_response.json()["id"]

    role_response = client.post(
        "/v1/roles",
        json={
            "name": "B2 test role",
            "system_prompt": "Keep the original role snapshot.",
            "model_profile_id": model_id,
        },
        headers={"Idempotency-Key": "b2-role-create"},
    )
    assert role_response.status_code == 201, role_response.text
    return model_id, role_response.json()["id"]


def _service(client: TestClient) -> Any:
    return client.app.state.operant_service


def _action(task: dict[str, Any], name: str) -> dict[str, Any]:
    return next(action for action in task["actions"] if action["action"] == name)


def test_model_role_idempotency_and_versioned_snapshots(client: TestClient) -> None:
    model_id, role_id = _bootstrap(client)

    model_payload = {
        "name": "B2 test provider",
        "model_id": "b2-test-model-v1",
        "base_url": "https://provider.example.test/v1",
        "secret_ref": "OPERANT_B2_TEST_KEY",
    }
    model_replay = client.post(
        "/v1/models",
        json=model_payload,
        headers={"Idempotency-Key": "b2-model-create"},
    )
    assert model_replay.status_code == 201
    assert model_replay.headers["Idempotency-Replayed"] == "true"
    assert model_replay.json()["id"] == model_id
    assert len(client.get("/v1/models").json()) == 1

    role_payload = {
        "name": "B2 test role",
        "system_prompt": "Keep the original role snapshot.",
        "model_profile_id": model_id,
    }
    role_replay = client.post(
        "/v1/roles",
        json=role_payload,
        headers={"Idempotency-Key": "b2-role-create"},
    )
    assert role_replay.status_code == 201
    assert role_replay.headers["Idempotency-Replayed"] == "true"
    assert role_replay.json()["id"] == role_id
    assert len(client.get("/v1/roles").json()) == 1

    session_response = client.post(
        "/v1/sessions",
        json={"role_id": role_id},
    )
    assert session_response.status_code == 201, session_response.text
    session_id = session_response.json()["id"]
    old_snapshot = session_response.json()["role_snapshot"]

    updated_model = client.patch(
        f"/v1/models/{model_id}",
        json={"name": "B2 test provider v2", "model_id": "b2-test-model-v2"},
        headers={"Idempotency-Key": "b2-model-update"},
    )
    assert updated_model.status_code == 200, updated_model.text

    updated_role = client.patch(
        f"/v1/roles/{role_id}",
        json={"system_prompt": "Use the new role prompt."},
        headers={"Idempotency-Key": "b2-role-update"},
    )
    assert updated_role.status_code == 200, updated_role.text
    assert updated_role.json()["version"] == 2

    old_session = client.get(f"/v1/sessions/{session_id}")
    assert old_session.status_code == 200
    assert old_session.json()["role_snapshot"] == old_snapshot
    assert old_snapshot["role_version"] == 1
    assert old_snapshot["system_prompt"] == "Keep the original role snapshot."
    assert old_snapshot["model_profile_name"] == "B2 test provider"
    assert old_snapshot["model_id"] == "b2-test-model-v1"

    new_session = client.post("/v1/sessions", json={"role_id": role_id})
    assert new_session.status_code == 201, new_session.text
    new_snapshot = new_session.json()["role_snapshot"]
    assert new_snapshot["role_version"] == 2
    assert new_snapshot["system_prompt"] == "Use the new role prompt."
    assert new_snapshot["model_profile_name"] == "B2 test provider v2"
    assert new_snapshot["model_id"] == "b2-test-model-v2"

    conflict = client.post(
        "/v1/models",
        json={**model_payload, "name": "different body"},
        headers={"Idempotency-Key": "b2-model-create"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_key_conflict"


def test_session_thread_workspace_binding_and_typed_failures(
    client: TestClient, tmp_path: Path
) -> None:
    _model_id, role_id = _bootstrap(client)
    service = _service(client)
    initialization, created = service.initialize_workspace(tmp_path)
    assert created is True

    thread_response = client.post(
        "/v1/threads",
        json={"workspace_ref": str(tmp_path)},
    )
    assert thread_response.status_code == 201, thread_response.text
    thread_id = thread_response.json()["id"]

    session_response = client.post(
        "/v1/sessions",
        json={"role_id": role_id, "thread_id": thread_id},
    )
    assert session_response.status_code == 201, session_response.text
    session_id = session_response.json()["id"]

    tasks = client.get("/v1/b2/tasks")
    assert tasks.status_code == 200, tasks.text
    session_task = next(
        item
        for item in tasks.json()["items"]
        if item["source"] == {"source_type": "session", "source_id": session_id}
    )
    assert session_task["thread_id"] == thread_id
    assert session_task["project_id"] == initialization.id
    assert session_task["workspace_id"] == initialization.id

    missing_thread = client.post(
        "/v1/sessions",
        json={"role_id": role_id, "thread_id": "thread_missing_b2"},
    )
    assert missing_thread.status_code == 404
    assert missing_thread.json()["error"]["code"] == "thread_not_found"

    inactive_thread = service.create_thread(ConversationThread(workspace_ref=str(tmp_path)))
    service.set_thread_status(inactive_thread.id, ThreadStatus.COMPLETED)
    inactive = client.post(
        "/v1/sessions",
        json={"role_id": role_id, "thread_id": inactive_thread.id},
    )
    assert inactive.status_code == 409
    assert inactive.json()["error"]["code"] == "thread_not_active"

    already_bound = client.post(
        "/v1/sessions",
        json={"role_id": role_id, "thread_id": thread_id},
    )
    assert already_bound.status_code == 409
    assert already_bound.json()["error"]["code"] == "thread_already_bound"


def test_graph_with_plugin_dependency_is_rejected_without_affecting_plain_graphs() -> None:
    plain = WorkflowDefinition(
        name="plain B2 graph",
        nodes=(NodeSpec(node_id="entry", node_kind=NodeKind.AGENT),),
    )
    assert GraphCompiler().compile(plain).definition.workflow_id == plain.workflow_id

    plugin_dependent = plain.model_copy(update={"required_plugins": ("memory",)})
    with pytest.raises(GraphCompilationError) as error:
        GraphCompiler().compile(plugin_dependent)
    assert [issue.code for issue in error.value.issues] == ["plugins_out_of_scope"]


def test_task_projection_keeps_session_and_workflow_sources_and_cancel_admission(
    client: TestClient,
    tmp_path: Path,
) -> None:
    _model_id, role_id = _bootstrap(client)
    service = _service(client)
    initialization, _created = service.initialize_workspace(tmp_path)

    session_thread = service.create_thread(ConversationThread(workspace_ref=str(tmp_path)))
    session_response = client.post(
        "/v1/sessions",
        json={"role_id": role_id, "thread_id": session_thread.id},
    )
    assert session_response.status_code == 201, session_response.text
    session_id = session_response.json()["id"]

    workflow = service.create_workflow_run(
        WorkflowRun(
            task="Workflow source task",
            workspace=str(tmp_path),
            planner_role_id=role_id,
            coder_role_id=role_id,
            reviewer_role_id=role_id,
            status=WorkflowRunStatus.RUNNING,
        )
    )
    workflow_thread = service.create_thread(
        ConversationThread(
            workspace_ref=str(tmp_path),
            legacy_refs=(
                ThreadLegacyRef(
                    source_type=LegacySourceType.WORKFLOW_RUN,
                    source_id=workflow.id,
                ),
            ),
        )
    )
    workflow_event = service.append_workflow_event(
        WorkflowRunEvent(
            workflow_run_id=workflow.id,
            role="workflow",
            event_type="workflow.started",
        )
    )

    projection = client.get("/v1/b2/tasks", params={"limit": 1})
    assert projection.status_code == 200, projection.text
    assert projection.json()["next_offset"] == 1
    first_page = projection.json()["items"]
    assert len(first_page) == 1
    second_page = client.get("/v1/b2/tasks", params={"offset": 1, "limit": 1})
    assert second_page.status_code == 200, second_page.text
    all_tasks = first_page + second_page.json()["items"]
    assert {task["source"]["source_type"] for task in all_tasks} == {"session", "workflow_run"}
    assert all(task["source"]["source_type"] != "team_task" for task in all_tasks)

    session_task = next(task for task in all_tasks if task["source"]["source_id"] == session_id)
    workflow_task = next(task for task in all_tasks if task["source"]["source_id"] == workflow.id)
    assert session_task["thread_id"] == session_thread.id
    assert session_task["project_id"] == initialization.id
    assert session_task["source_status"] == "created"
    assert _action(session_task, "cancel") == {
        "action": "cancel",
        "availability": "blocked",
        "reason_code": "no_active_run",
        "projection_revision": 0,
    }
    assert workflow_task["thread_id"] == workflow_thread.id
    assert workflow_task["source_status"] == "running"
    assert workflow_task["revision"] == workflow_event.cursor
    assert _action(workflow_task, "cancel")["availability"] == "available"

    no_lease = client.post(
        f"/v1/sessions/{session_id}/cancel",
        headers={"Idempotency-Key": "b2-cancel-no-lease"},
    )
    assert no_lease.status_code == 200
    assert no_lease.json() == {"accepted": False}

    assert service.admit_session_run(session_id) is True
    lease = service.admitted_session_run_lease(session_id)
    assert lease is not None
    try:
        admitted = client.post(
            f"/v1/sessions/{session_id}/cancel",
            headers={"Idempotency-Key": "b2-cancel-active"},
        )
        assert admitted.status_code == 200
        assert admitted.json() == {"accepted": True}
        persisted_lease = service.store.get_session_run_lease(session_id)
        assert persisted_lease.cancel_requested is True

        # A cancellation request fences execution but does not invent a
        # terminal source status or a completed history receipt.
        after_request = client.get("/v1/b2/tasks").json()["items"]
        session_after_request = next(
            task for task in after_request if task["source"]["source_id"] == session_id
        )
        assert session_after_request["source_status"] == "created"
    finally:
        service.release_session_run(session_id, lease)

    missing_cancel = client.post("/v1/sessions/session_missing_b2/cancel")
    assert missing_cancel.status_code == 404
    assert missing_cancel.json()["error"]["code"] == "http_404"


def test_session_history_is_canonical_paginated_redacted_and_refreshable(
    client: TestClient,
    tmp_path: Path,
) -> None:
    _model_id, role_id = _bootstrap(client)
    service = _service(client)
    service.initialize_workspace(tmp_path)
    thread = service.create_thread(ConversationThread(workspace_ref=str(tmp_path)))
    session_response = client.post(
        "/v1/sessions",
        json={"role_id": role_id, "thread_id": thread.id},
    )
    assert session_response.status_code == 201, session_response.text
    session_id = session_response.json()["id"]
    agent = service.factory.create_agent(session_id)

    first_turn = service.create_turn(Turn(thread_id=thread.id))
    first_item = service.append_item(
        Item(
            thread_id=thread.id,
            turn_id=first_turn.id,
            payload=UserMessagePayload(text="first user message"),
        )
    )
    second_turn = service.create_turn(Turn(thread_id=thread.id))
    second_item = service.append_item(
        Item(
            thread_id=thread.id,
            turn_id=second_turn.id,
            payload=UserMessagePayload(
                text='please do not expose password="super-secret-value" in history',
            ),
        )
    )

    page_one = client.get(
        f"/v1/b2/sessions/{session_id}/history",
        params={"limit": 1},
    )
    assert page_one.status_code == 200, page_one.text
    page_one_body = page_one.json()
    assert page_one_body["thread_id"] == thread.id
    assert [item["id"] for item in page_one_body["items"]] == [first_item.id]
    assert page_one_body["agents"][0]["id"] == agent.id
    assert page_one_body["next_cursor"] == first_item.cursor
    assert "super-secret-value" not in page_one.text

    page_two = client.get(
        f"/v1/b2/sessions/{session_id}/history",
        params={"after_cursor": page_one_body["next_cursor"], "limit": 1},
    )
    assert page_two.status_code == 200, page_two.text
    page_two_body = page_two.json()
    assert [item["id"] for item in page_two_body["items"]] == [second_item.id]
    assert page_two_body["next_cursor"] is None
    redacted_text = page_two_body["items"][0]["payload"]["text"]
    assert 'password="[REDACTED]"' in redacted_text
    assert "super-secret-value" not in redacted_text

    third_turn = service.create_turn(Turn(thread_id=thread.id))
    third_item = service.append_item(
        Item(
            thread_id=thread.id,
            turn_id=third_turn.id,
            payload=UserMessagePayload(text="new item after refresh"),
        )
    )
    refreshed = client.get(
        f"/v1/b2/sessions/{session_id}/history",
        params={"after_cursor": second_item.cursor, "limit": 1},
    )
    assert refreshed.status_code == 200, refreshed.text
    assert [item["id"] for item in refreshed.json()["items"]] == [third_item.id]
    assert refreshed.json()["next_cursor"] is None

    missing_history = client.get("/v1/b2/sessions/session_missing_b2/history")
    assert missing_history.status_code == 404
    assert missing_history.json()["error"]["code"] == "http_404"


def test_core_owns_explicit_host_lifetime_but_never_installs_implicitly(tmp_path: Path) -> None:
    from operant.plugins import PluginError, PluginHost, PluginRegistry

    with TestClient(create_app(tmp_path / "plain.sqlite3")) as plain:
        assert plain.app.state.plugin_host is None
        assert plain.get("/healthz").status_code == 200
        assert not (tmp_path / "plugin-host").exists()

    root = tmp_path / "explicit-host"
    registry = PluginRegistry(root)
    host = PluginHost(registry)
    with TestClient(create_app(tmp_path / "host.sqlite3", plugin_host=host)) as connected:
        assert connected.app.state.plugin_host is host
        assert connected.get("/v1/b2/tasks").json()["items"] == []
        with pytest.raises(PluginError, match="another PluginHost"):
            PluginRegistry(root)
    # Shutdown releases the actual writer lock without deleting the registry.
    reopened = PluginRegistry(root)
    try:
        assert reopened.state.installations == ()
    finally:
        reopened.close()


def test_task_lookup_is_exact_not_limited_to_first_list_page(client: TestClient) -> None:
    _, role_id = _bootstrap(client)
    first = client.post("/v1/sessions", json={"role_id": role_id}).json()["id"]
    second = client.post("/v1/sessions", json={"role_id": role_id}).json()["id"]
    assert (
        client.get("/v1/b2/tasks", params={"limit": 1}).json()["items"][0]["source"]["source_id"]
        == second
    )
    found = client.get(f"/v1/b2/tasks/{first}", params={"source_type": "session"})
    assert found.status_code == 200
    assert found.json()["source"] == {"source_type": "session", "source_id": first}
    assert (
        client.get(f"/v1/b2/tasks/{first}", params={"source_type": "workflow_run"}).status_code
        == 404
    )
    assert client.get("/v1/b2/tasks/missing").status_code == 404
    _service(client).create_workflow_run(
        WorkflowRun(
            id=first,
            task="Colliding legacy identity",
            workspace=str(_service(client).store.path.parent),
            planner_role_id=role_id,
            coder_role_id=role_id,
            reviewer_role_id=role_id,
        )
    )
    assert client.get(f"/v1/b2/tasks/{first}").status_code == 409
    exact = client.get(f"/v1/b2/tasks/{first}", params={"source_type": "workflow_run"})
    assert exact.status_code == 200
    assert exact.json()["source"]["source_type"] == "workflow_run"


def test_empty_registered_workspace_can_create_first_thread_and_session(
    client: TestClient, tmp_path: Path
) -> None:
    _, role_id = _bootstrap(client)
    workspace, _ = _service(client).initialize_workspace(tmp_path)
    body = {"workspace_id": workspace.id}
    headers = {"Idempotency-Key": "first-thread"}
    response = client.post("/v1/b2/threads", json=body, headers=headers)
    assert response.status_code == 201, response.text
    thread = response.json()
    assert thread["workspace_ref"] == str(tmp_path)
    replay = client.post("/v1/b2/threads", json=body, headers=headers)
    assert replay.json()["id"] == thread["id"]
    assert replay.headers["Idempotency-Replayed"] == "true"
    session = client.post("/v1/sessions", json={"role_id": role_id, "thread_id": thread["id"]})
    assert session.status_code == 201
    history = client.get(f"/v1/b2/sessions/{session.json()['id']}/history")
    assert history.json()["thread_id"] == thread["id"]
    assert client.post("/v1/b2/threads", json={"workspace_id": "missing"}).status_code == 404
    assert client.post("/v1/b2/threads", json={**body, "workspace_ref": "/"}).status_code == 422


def test_gui_run_persists_canonical_history_once_and_survives_reopen(
    client: TestClient, tmp_path: Path
) -> None:
    from operant.domain.messages import ModelResponse, ProviderEvent

    class FixedProvider:
        calls = 0

        async def stream(self, **kwargs):
            self.calls += 1
            yield ProviderEvent(
                event_type="model.completed",
                response=ModelResponse(content="17 + 25 = 42", finish_reason="stop"),
            )

    _, role_id = _bootstrap(client)
    service = _service(client)
    provider = FixedProvider()
    service.provider = provider
    thread = service.create_thread(ConversationThread(workspace_ref=str(tmp_path)))
    session = client.post("/v1/sessions", json={"role_id": role_id, "thread_id": thread.id}).json()
    url = f"/v1/sessions/{session['id']}/runs"
    request = {"message": "Compute 17 + 25", "workspace": str(tmp_path), "thread_id": thread.id}
    headers = {"Idempotency-Key": "b22-canonical-run"}
    response = client.post(url, json=request, headers=headers)
    assert response.status_code == 200, response.text
    assert "agent.completed" in response.text, response.text
    history_url = f"/v1/b2/sessions/{session['id']}/history"
    first = client.get(history_url).json()
    texts = [
        item["payload"].get("text")
        for item in first["items"]
        if item["payload"]["type"] in {"user_message", "agent_message"}
    ]
    assert texts == ["Compute 17 + 25", "17 + 25 = 42"]
    client.post(url, json=request, headers=headers)
    assert provider.calls == 1
    assert client.get(history_url).json()["items"] == first["items"]
    page = client.get(history_url, params={"limit": 1}).json()
    rest = client.get(history_url, params={"after_cursor": page["next_cursor"]}).json()
    assert page["items"] + rest["items"] == first["items"]
    # Multiple agents plus a populated page must not share a log truncation budget.
    client.post(url, json=request, headers={"Idempotency-Key": "b22-second-round"})
    multi = client.get(history_url)
    assert multi.status_code == 200, multi.text
    assert len(multi.json()["items"]) == 2 * len(first["items"])
    assert len(multi.json()["agents"]) == 2
    first = multi.json()
    from operant.domain.models import AgentStatus

    # Reproduce a client disconnect interrupting cleanup after the terminal event.
    for agent in first["agents"]:
        service.store.update_agent_status(agent["id"], AgentStatus.RUNNING)
    agents = client.get("/v1/b2/agents", params={"limit": 1}).json()
    assert len(agents["items"]) == 1
    assert agents["items"][0]["status"] == "completed"
    next_agents = client.get("/v1/b2/agents", params={"offset": agents["next_offset"]}).json()
    assert len(next_agents["items"]) == 1
    assert next_agents["items"][0]["id"] != agents["items"][0]["id"]
    # A fresh application over the same SQLite file reads the committed result.
    with TestClient(create_app(tmp_path / "b2-api.sqlite3")) as reopened:
        assert reopened.get(history_url).json()["items"] == first["items"]
