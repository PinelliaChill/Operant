"""Root-owned formal Graph/Team API acceptance on an isolated synthetic project."""

import time

from fastapi.testclient import TestClient

from operant.api import create_app
from operant.domain.messages import ModelResponse, ModelUsage, ProviderEvent
from operant.domain.models import Budget, ModelProfile, RolePreset
from operant.providers.base import ModelProvider


class RecordingProvider(ModelProvider):
    def __init__(self):
        self.requests = []

    async def list_models(self, **kwargs):
        return ["test"]

    async def stream(self, **kwargs):
        self.requests.append(kwargs)
        yield ProviderEvent(
            event_type="model.completed",
            response=ModelResponse(
                content="done",
                finish_reason="stop",
                usage=ModelUsage(prompt_tokens=10, completion_tokens=2, total_tokens=12),
            ),
        )


def test_formal_directory_create_publish_run_and_team_terminal(tmp_path):
    app = create_app(tmp_path / "core.db")
    service = app.state.operant_service
    provider = RecordingProvider()
    service.provider = provider
    profile = service.add_model_profile(
        ModelProfile(
            name="test",
            base_url="https://invalid.test",
            model_id="test",
            secret_ref="TEST_KEY",
        )
    )
    roles = [
        service.create_role(
            RolePreset(
                name=f"role{i}",
                system_prompt="Read only.",
                model_profile_id=profile.id,
                budget=Budget(max_turns=2, max_output_tokens=100, timeout_seconds=5),
            )
        )
        for i in range(2)
    ]
    with TestClient(app) as client:
        directory = client.get("/v1/b2-4/collaboration").json()
        assert {r["id"] for r in directory["roles"]} == {r.id for r in roles}
        request = {
            "action": "graph_create_from_roles",
            "name": "API graph",
            "role_ids": [r.id for r in roles],
            "workspace_or_target": str(tmp_path),
            "task": "Reply done",
        }
        response = client.post(
            "/v1/b2-4/commands", json=request, headers={"Idempotency-Key": "create-api"}
        )
        assert response.status_code == 200, response.text
        workflow = response.json()["resource_id"]
        again = client.post(
            "/v1/b2-4/commands", json=request, headers={"Idempotency-Key": "create-api"}
        )
        assert again.json()["resource_id"] == workflow
        changed = client.post(
            "/v1/b2-4/commands",
            json={**request, "name": "changed"},
            headers={"Idempotency-Key": "create-api"},
        )
        assert changed.status_code == 409
        published = client.post(
            f"/v1/graph/workflows/{workflow}/publish", json={"draft_version": 1}
        )
        assert published.status_code == 202, published.text
        response = client.post(
            "/v1/graph/runs",
            json={
                "workflow_id": workflow,
                "definition_version": 2,
                "workspace_or_target": str(tmp_path),
                "input": {"task": "Reply done"},
            },
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["resource_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = client.get("/v1/graph/runs/" + run_id).json()
            if state["status"] in ("completed", "failed", "cancelled"):
                break
            time.sleep(0.01)
        assert state["status"] == "completed", state
        team = app.state.team_repository.get_team_run(state["team_run_id"])
        assert team.status.value == "completed"
        roster = app.state.team_repository.list_roster(team.team_run_id)
        assert len({service.store.get_agent(r.agent_instance_id).session_id for r in roster}) == 2
        assert len({r.agent_instance_id for r in roster}) == 2
        assert len(provider.requests) == 2
        board = client.get(f"/v1/teams/runs/{team.team_run_id}/tasks")
        assert board.status_code == 200, board.text
        tasks = board.json()["tasks"]
        assert len(tasks) == 2
        assert all(task["status"] == "completed" for task in tasks)
        assert {a for task in tasks for a in task["assignee_ids"]} == {
            r.agent_instance_id for r in roster
        }


def test_resume_rejects_running_attempt_without_resetting_it(tmp_path):
    from operant.domain.graph import (
        NodeKind,
        NodeSpec,
        WorkflowDefinition,
        WorkflowDefinitionStatus,
    )

    app = create_app(tmp_path / "resume.db")
    definition = WorkflowDefinition(
        name="guard",
        status=WorkflowDefinitionStatus.PUBLISHED,
        nodes=(NodeSpec(node_id="worker", node_kind=NodeKind.AGENT),),
    )
    with TestClient(app) as client:
        run = app.state.graph_runtime.create_run(definition, workspace_or_target=str(tmp_path))
        app.state.graph_runtime.start_run(run.id)
        node = app.state.graph_repository.list_node_runs(run.id)[0]
        attempt = app.state.graph_runtime.start_attempt(node.id)
        response = client.post(f"/v1/graph/runs/{run.id}/resume", json={})
        assert response.status_code == 409
        assert app.state.graph_repository.get_run(run.id).status.value == "running"
        assert app.state.graph_repository.get_attempt(attempt.id).result.value == "running"


def test_writer_role_creates_non_idempotent_node_contract(tmp_path):
    from operant.domain.models import ToolPolicy

    app = create_app(tmp_path / "writer.db")
    service = app.state.operant_service
    profile = service.add_model_profile(
        ModelProfile(
            name="test", base_url="https://invalid.test", model_id="test", secret_ref="TEST_KEY"
        )
    )
    role = service.create_role(
        RolePreset(
            name="writer",
            system_prompt="bounded writer",
            model_profile_id=profile.id,
            tool_policy=ToolPolicy(allowed_tools=("apply_patch",), workspace_write=True),
        )
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/b2-4/commands",
            json={
                "action": "graph_create_from_roles",
                "name": "writer",
                "role_ids": [role.id],
                "workspace_or_target": str(tmp_path),
            },
        )
        assert response.status_code == 200, response.text
        directory = response.json()["directory"]
        node = directory["workflows"][0]["nodes"][0]
        assert node["writes_workspace"] is True
        assert node["idempotency_class"] == "non_idempotent"


def test_resume_rejects_terminal_agent_before_mutating_graph(tmp_path):
    from operant.domain.models import AgentStatus

    app = create_app(tmp_path / "resume-terminal.db")
    service = app.state.operant_service
    service.provider = RecordingProvider()
    profile = service.add_model_profile(
        ModelProfile(
            name="test", base_url="https://invalid.test", model_id="test", secret_ref="TEST_KEY"
        )
    )
    role = service.create_role(
        RolePreset(name="reader", system_prompt="read only", model_profile_id=profile.id)
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/b2-4/commands",
            json={
                "action": "graph_create_from_roles",
                "name": "resume",
                "role_ids": [role.id],
                "workspace_or_target": str(tmp_path),
            },
        )
        workflow = response.json()["resource_id"]
        published = client.post(
            f"/v1/graph/workflows/{workflow}/publish", json={"draft_version": 1}
        )
        assert published.status_code == 202
        definition = app.state.graph_repository.get_definition(workflow, 2)
        run = app.state.graph_runtime.create_run(definition, workspace_or_target=str(tmp_path))
        team = app.state.b24_graph_executor.prepare(run.id)
        app.state.graph_runtime.start_run(run.id)
        node = app.state.graph_repository.list_node_runs(run.id)[0]
        app.state.graph_runtime.start_attempt(node.id)
        app.state.graph_runtime.interrupt_run(run.id)
        entry = app.state.team_repository.list_roster(team.team_run_id)[0]
        service.store.update_agent_status(entry.agent_instance_id, AgentStatus.CANCELLED)
        result = client.post(f"/v1/graph/runs/{run.id}/resume", json={})
        assert result.status_code == 409, result.text
        assert app.state.graph_repository.get_run(run.id).status.value == "interrupted"
        assert service.provider.requests == []


def _unbound_team_graph(app, workspace):
    from operant.domain.graph import (
        NodeKind,
        NodeSpec,
        WorkflowDefinition,
        WorkflowDefinitionStatus,
    )
    from operant.domain.team import TeamDefinition, TeamMember

    service = app.state.operant_service
    profile = service.add_model_profile(
        ModelProfile(
            name="test", model_id="test", base_url="https://invalid.test", secret_ref="TEST_KEY"
        )
    )
    role = service.create_role(
        RolePreset(
            name="reader",
            system_prompt="read only",
            model_profile_id=profile.id,
            budget=Budget(max_turns=6, max_output_tokens=100),
        )
    )
    team = TeamDefinition(
        team_id="admission-team",
        members=tuple(
            TeamMember(
                member_id=f"worker-{i}",
                agent_definition_id=role.id,
                role="reader",
                can_coordinate=i == 0,
            )
            for i in range(2)
        ),
        default_coordinator="worker-0",
    )
    app.state.team_repository.put_team_definition(team)
    definition = WorkflowDefinition(
        name="admission",
        status=WorkflowDefinitionStatus.PUBLISHED,
        nodes=tuple(
            NodeSpec(
                node_id=member.member_id,
                node_kind=NodeKind.AGENT,
                metadata={"role_id": role.id, "role_version": role.version},
            )
            for member in team.members
        ),
        default_policy={"team_id": team.team_id, "team_version": team.version},
        default_budget=Budget(max_turns=6, max_output_tokens=40),
    )
    graph = app.state.graph_runtime.create_run(definition, workspace_or_target=str(workspace))
    return role, team, graph


def test_team_start_uses_graph_admission_and_reserves_shared_budget(tmp_path):
    app = create_app(tmp_path / "admission.db")
    with TestClient(app) as client:
        _role, team, graph = _unbound_team_graph(app, tmp_path)
        body = {
            "action": "team_start_from_roles",
            "team_id": team.team_id,
            "team_version": team.version,
            "workflow_run_id": graph.id,
            "workspace_or_target": str(tmp_path),
        }
        response = client.post("/v1/b2-4/commands", json=body)
        assert response.status_code == 200, response.text
        team_run_id = response.json()["resource_id"]
        roster = app.state.team_repository.list_roster(team_run_id)
        assert len(roster) == 2
        for entry in roster:
            budget = app.state.operant_service.store.get_agent(
                entry.agent_instance_id
            ).role_snapshot.budget
            assert budget.max_turns == 3 and budget.max_output_tokens == 20
        repeat = client.post("/v1/b2-4/commands", json=body)
        assert repeat.status_code == 200 and repeat.json()["resource_id"] == team_run_id
        with app.state.operant_service.store._connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 2


def test_team_start_rejects_mismatched_team_without_creating_agents(tmp_path):
    app = create_app(tmp_path / "wrong-team.db")
    with TestClient(app) as client:
        _role, team, graph = _unbound_team_graph(app, tmp_path)
        response = client.post(
            "/v1/b2-4/commands",
            json={
                "action": "team_start_from_roles",
                "team_id": "different-team",
                "team_version": team.version,
                "workflow_run_id": graph.id,
            },
        )
        assert response.status_code == 409, response.text
        assert app.state.graph_repository.get_run(graph.id).team_run_id is None
        with app.state.operant_service.store._connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 0


def test_team_start_rejects_stale_role_and_terminal_graph_before_allocation(tmp_path):
    app = create_app(tmp_path / "stale-role.db")
    with TestClient(app) as client:
        role, team, graph = _unbound_team_graph(app, tmp_path)
        body = {
            "action": "team_start_from_roles",
            "team_id": team.team_id,
            "team_version": team.version,
            "workflow_run_id": graph.id,
        }
        app.state.operant_service.update_role(role.id, name="new role revision")
        stale = client.post("/v1/b2-4/commands", json=body)
        assert stale.status_code == 409, stale.text
        app.state.graph_runtime.cancel_run(graph.id)
        terminal = client.post("/v1/b2-4/commands", json=body)
        assert terminal.status_code == 409, terminal.text
        assert app.state.graph_repository.get_run(graph.id).team_run_id is None
        with app.state.operant_service.store._connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 0


def test_existing_team_cannot_bypass_graph_budget_reservation(tmp_path):
    app = create_app(tmp_path / "legacy-budget.db")
    with TestClient(app) as client:
        _role, team, graph = _unbound_team_graph(app, tmp_path)
        prepared = app.state.b24_graph_executor.prepare(graph.id)
        entry = app.state.team_repository.list_roster(prepared.team_run_id)[0]
        store = app.state.operant_service.store
        agent = store.get_agent(entry.agent_instance_id)
        # Reproduce a legacy binding created with the full Role budget.
        legacy = agent.model_copy(
            update={
                "role_snapshot": agent.role_snapshot.model_copy(
                    update={
                        "budget": agent.role_snapshot.budget.model_copy(
                            update={"max_output_tokens": 100}
                        )
                    }
                )
            }
        )
        with store._connect() as connection:
            connection.execute(
                "UPDATE agents SET body=? WHERE id=?", (legacy.model_dump_json(), legacy.id)
            )
        response = client.post(
            "/v1/b2-4/commands",
            json={
                "action": "team_start_from_roles",
                "team_id": team.team_id,
                "team_version": team.version,
                "workflow_run_id": graph.id,
            },
        )
        assert response.status_code == 409, response.text
        assert "budget reservation" in response.json()["detail"]
        with store._connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 2


def test_message_to_finished_roster_returns_conflict_without_partial_write(tmp_path):
    from operant.domain.team import RosterMemberStatus

    app = create_app(tmp_path / "finished-mailbox.db")
    with TestClient(app) as client:
        _role, _team, graph = _unbound_team_graph(app, tmp_path)
        run = app.state.b24_graph_executor.prepare(graph.id)
        roster = app.state.team_repository.list_roster(run.team_run_id)
        app.state.b24_graph_executor._finish_roster(roster[0], RosterMemberStatus.COMPLETED)
        response = client.post(
            f"/v1/teams/runs/{run.team_run_id}/messages",
            json={
                "sender_id": roster[1].agent_instance_id,
                "recipient_ids": [roster[0].agent_instance_id],
                "audience": "direct",
                "message_kind": "Finding",
                "payload": {"text": "not delivered"},
            },
        )
        assert response.status_code == 409, response.text
        assert "active Team members" in response.json()["detail"]
        with app.state.operant_service.store._connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM team_messages").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM mailbox_deliveries").fetchone()[0] == 0


def test_existing_team_with_terminal_agent_on_ready_node_is_rejected(tmp_path):
    from operant.domain.models import AgentStatus

    app = create_app(tmp_path / "legacy-terminal-agent.db")
    with TestClient(app) as client:
        _role, team, graph = _unbound_team_graph(app, tmp_path)
        prepared = app.state.b24_graph_executor.prepare(graph.id)
        entry = app.state.team_repository.list_roster(prepared.team_run_id)[0]
        app.state.operant_service.store.update_agent_status(
            entry.agent_instance_id, AgentStatus.FAILED
        )
        response = client.post(
            "/v1/b2-4/commands",
            json={
                "action": "team_start_from_roles",
                "team_id": team.team_id,
                "team_version": team.version,
                "workflow_run_id": graph.id,
            },
        )
        assert response.status_code == 409, response.text
        assert "CREATED status" in response.json()["detail"]
        assert all(
            node.status.value == "ready"
            for node in app.state.graph_repository.list_node_runs(graph.id)
        )


def test_context_inspector_reports_missing_session_after_core_switch(tmp_path):
    app = create_app(tmp_path / "empty-core.db")
    with TestClient(app) as client:
        response = client.get("/v1/b2-4/context/session_from_previous_core")
        assert response.status_code == 404
        assert response.json()["detail"] == "Session not found"
        assert response.json()["error"]["code"] == "http_404"
        assert response.json()["error"]["retryable"] is False


def test_command_large_directory_preserves_typed_result_and_safe_replay(tmp_path):
    import json

    from operant.contracts.b2_4 import B24Result

    app = create_app(tmp_path / "core.db")
    service = app.state.operant_service
    profile = service.add_model_profile(
        ModelProfile(
            name="test", base_url="https://invalid.test", model_id="test", secret_ref="TEST_KEY"
        )
    )
    roles = [
        service.create_role(
            RolePreset(
                name=f"role{i}",
                system_prompt="Read only.",
                model_profile_id=profile.id,
            )
        )
        for i in range(16)
    ]
    request = {
        "action": "graph_create_from_roles",
        "name": "large directory",
        "description": "Authorization: Bearer synthetic_b24_secret_123456789",
        "role_ids": [r.id for r in roles],
        "workspace_or_target": str(tmp_path),
    }
    with TestClient(app) as client:
        response = client.post(
            "/v1/b2-4/commands", json=request, headers={"Idempotency-Key": "large-directory"}
        )
        assert response.status_code == 200, response.text
        result = B24Result.model_validate(response.json())
        assert len(result.directory.roles) == 16
        assert len(result.directory.workflows[0].nodes) == 16
        assert "synthetic_b24_secret_123456789" not in response.text
        assert response.headers["Idempotency-Key"] == "large-directory"
        replay = client.post(
            "/v1/b2-4/commands", json=request, headers={"Idempotency-Key": "large-directory"}
        )
        assert replay.json() == response.json()
        assert replay.headers["Idempotency-Replayed"] == "true"
        with service.store._connect() as connection:
            stored = connection.execute(
                "SELECT result FROM b24_commands WHERE command_id='large-directory'"
            ).fetchone()["result"]
        assert "synthetic_b24_secret_123456789" not in stored
        assert json.loads(stored)["projection"] == response.json()
        changed = client.post(
            "/v1/b2-4/commands",
            json={**request, "name": "different"},
            headers={"Idempotency-Key": "large-directory"},
        )
        assert changed.status_code == 409
        legacy = response.json()
        legacy["directory"]["workflows"][0]["description"] = request["description"]
        with service.store._connect() as connection:
            connection.execute(
                "UPDATE b24_commands SET result=? WHERE command_id='large-directory'",
                (json.dumps(legacy),),
            )
        legacy_replay = client.post(
            "/v1/b2-4/commands", json=request, headers={"Idempotency-Key": "large-directory"}
        )
        assert legacy_replay.json() == response.json()
        empty_key = client.post("/v1/b2-4/commands", json=request, headers={"Idempotency-Key": ""})
        assert empty_key.status_code == 422
