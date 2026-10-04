"""Human decision boundaries operate through formal HTTP commands and persistence."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from test_b24_graph_execution import RecordingProvider

from operant.api import create_app
from operant.domain.graph import (
    EdgeSpec,
    NodeKind,
    NodeSpec,
    PortSpec,
    TimeoutPolicy,
    WorkflowDefinition,
    WorkflowDefinitionStatus,
)
from operant.domain.models import ModelProfile, RolePreset
from operant.domain.team import TeamDefinition, TeamMember


def _setup(app: Any, workspace: Path) -> RecordingProvider:
    provider = RecordingProvider()
    service = app.state.operant_service
    service.provider = provider
    profile = service.add_model_profile(
        ModelProfile(
            name="boundary model",
            model_id="graph-model",
            base_url="https://provider.invalid/v1",
            secret_ref="GRAPH_BOUNDARY_TEST_KEY",
        )
    )
    role = service.create_role(
        RolePreset(
            name="Worker",
            system_prompt="Answer briefly.",
            model_profile_id=profile.id,
        )
    )
    team = TeamDefinition(
        team_id="boundary-team",
        members=(
            TeamMember(
                member_id="agent",
                agent_definition_id=role.id,
                role="Worker",
                can_coordinate=True,
            ),
        ),
        default_coordinator="agent",
    )
    app.state.team_repository.put_team_definition(team)
    node_defs = (
        NodeSpec(
            node_id="human",
            node_kind=NodeKind.HUMAN_INPUT,
            output_ports=(
                PortSpec(name="answer", value_type="string"),
                PortSpec(name="timeout", value_type="bool", required=False),
            ),
            timeout_policy=TimeoutPolicy(timeout_seconds=30, on_timeout_node_id="fallback"),
            metadata={"prompt": "Provide the task answer."},
        ),
        NodeSpec(
            node_id="approve",
            node_kind=NodeKind.APPROVAL,
            input_ports=(PortSpec(name="answer", value_type="string"),),
            output_ports=(
                PortSpec(name="answer", value_type="string"),
                PortSpec(name="timeout", value_type="bool", required=False),
            ),
            timeout_policy=TimeoutPolicy(timeout_seconds=30, on_timeout_node_id="fallback"),
            metadata={"detail": "Continue the selected task?", "category": "workflow"},
        ),
        NodeSpec(
            node_id="wait",
            node_kind=NodeKind.WAIT,
            input_ports=(PortSpec(name="answer", value_type="string"),),
            output_ports=(PortSpec(name="answer", value_type="string"),),
            metadata={"delay_seconds": 0.05},
        ),
        NodeSpec(
            node_id="agent",
            node_kind=NodeKind.AGENT,
            input_ports=(PortSpec(name="input", value_type="string"),),
            output_ports=(PortSpec(name="result", value_type="string"),),
            metadata={"role_id": role.id, "role_version": role.version, "task": "Respond."},
        ),
        NodeSpec(
            node_id="fallback",
            node_kind=NodeKind.JOIN,
            input_ports=(PortSpec(name="timeout", value_type="bool", required=False),),
        ),
    )
    edges = (
        EdgeSpec(
            edge_id="human-approve",
            source_node="human",
            source_port="answer",
            target_node="approve",
            target_port="answer",
            delivery_mode="value",
        ),
        EdgeSpec(
            edge_id="approve-wait",
            source_node="approve",
            source_port="answer",
            target_node="wait",
            target_port="answer",
            delivery_mode="value",
        ),
        EdgeSpec(
            edge_id="wait-agent",
            source_node="wait",
            source_port="answer",
            target_node="agent",
            target_port="input",
            delivery_mode="value",
        ),
        EdgeSpec(
            edge_id="human-timeout",
            source_node="human",
            source_port="timeout",
            target_node="fallback",
            target_port="timeout",
            join_mode="any",
            delivery_mode="value",
        ),
        EdgeSpec(
            edge_id="approve-timeout",
            source_node="approve",
            source_port="timeout",
            target_node="fallback",
            target_port="timeout",
            join_mode="any",
            delivery_mode="value",
        ),
    )
    definition = WorkflowDefinition(
        workflow_id="boundary-workflow",
        name="Human decision combination",
        version=1,
        nodes=node_defs,
        edges=edges,
        default_policy={
            "team_id": team.team_id,
            "team_version": team.version,
            "b24_executor": True,
        },
        locked_role_versions={role.id: role.version},
        status=WorkflowDefinitionStatus.PUBLISHED,
    )
    app.state.graph_repository.put_definition(definition)
    return provider


def _wait(client: TestClient, run_id: str, node_id: str, status: str) -> dict[str, Any]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        response = client.get(f"/v1/graph/runs/{run_id}/nodes")
        assert response.status_code == 200, response.text
        values = response.json()
        node = next(item for item in values if item["node_id"] == node_id)
        if node["status"] == status:
            return node
        time.sleep(0.03)
    raise AssertionError(f"{node_id} did not reach {status}: {node}")


def _start(client: TestClient, workspace: Path) -> str:
    response = client.post(
        "/v1/graph/runs",
        json={
            "workflow_id": "boundary-workflow",
            "definition_version": 1,
            "workspace_or_target": str(workspace),
        },
    )
    assert response.status_code == 202, response.text
    return response.json()["resource_id"]


def test_human_approval_wait_combo_uses_exact_tokens_and_real_agent_entry(tmp_path: Path) -> None:
    app = create_app(tmp_path / "core.sqlite3")
    provider = _setup(app, tmp_path)
    with TestClient(app) as client:
        run_id = _start(client, tmp_path)
        human = _wait(client, run_id, "human", "waiting_input")
        stale = client.post(
            f"/v1/graph/runs/{run_id}/nodes/human/input",
            json={
                "value": "ANSWER_BOUNDARY",
                "wait_token": "old-wait-token",
            },
        )
        assert stale.status_code == 409
        assert provider.calls == []
        provided = client.post(
            f"/v1/graph/runs/{run_id}/nodes/human/input",
            json={
                "value": "ANSWER_BOUNDARY",
                "wait_token": human["wait_token"],
            },
        )
        assert provided.status_code == 202, provided.text
        _wait(client, run_id, "approve", "waiting_approval")
        approval = client.get(f"/v1/graph/runs/{run_id}/nodes/approve/approval").json()
        assert approval["status"] == "pending"
        assert provider.calls == []
        decided = client.post(
            f"/v1/graph/runs/{run_id}/nodes/approve/approval/decision",
            json={
                "approval_id": approval["approval_id"],
                "wait_token": approval["wait_token"],
                "approved": True,
            },
            headers={"Idempotency-Key": "boundary-approve"},
        )
        assert decided.status_code == 202, decided.text
        agent = _wait(client, run_id, "agent", "succeeded")
        final = client.get(f"/v1/graph/runs/{run_id}/nodes/approve/approval")
        assert final.status_code == 200, final.text
        assert final.json()["status"] == "approved"
        assert agent["input_refs"] == {"input": "ANSWER_BOUNDARY"}
        assert len(provider.calls) == 1
        assert "ANSWER_BOUNDARY" in provider.calls[0][1]
        retry = client.post(
            f"/v1/graph/runs/{run_id}/nodes/approve/approval/decision",
            json={
                "approval_id": approval["approval_id"],
                "wait_token": approval["wait_token"],
                "approved": True,
            },
            headers={"Idempotency-Key": "boundary-approve"},
        )
        assert retry.status_code == 202
        assert len(provider.calls) == 1


def test_flow_decision_survives_core_restart_and_denial_stops_downstream(tmp_path: Path) -> None:
    database = tmp_path / "core.sqlite3"
    app = create_app(database)
    provider = _setup(app, tmp_path)
    with TestClient(app) as client:
        run_id = _start(client, tmp_path)
        human = _wait(client, run_id, "human", "waiting_input")
        response = client.post(
            f"/v1/graph/runs/{run_id}/nodes/human/input",
            json={
                "value": "PERSISTED_INPUT",
                "wait_token": human["wait_token"],
            },
        )
        assert response.status_code == 202, response.text
        _wait(client, run_id, "approve", "waiting_approval")
        approval = client.get(f"/v1/graph/runs/{run_id}/nodes/approve/approval").json()
    restarted = create_app(database)
    restarted.state.operant_service.provider = provider
    with TestClient(restarted) as client:
        persisted = client.get(f"/v1/graph/runs/{run_id}/nodes/approve/approval").json()
        assert persisted["approval_id"] == approval["approval_id"]
        assert persisted["wait_token"] == approval["wait_token"]
        resumed = client.post(
            f"/v1/graph/runs/{run_id}/resume", json={"allow_unknown_side_effect_replay": False}
        )
        assert resumed.status_code == 202, resumed.text
        denied = client.post(
            f"/v1/graph/runs/{run_id}/nodes/approve/approval/decision",
            json={
                "approval_id": approval["approval_id"],
                "wait_token": approval["wait_token"],
                "approved": False,
            },
        )
        assert denied.status_code == 202, denied.text
        _wait(client, run_id, "approve", "failed")
        final = client.get(f"/v1/graph/runs/{run_id}/nodes/approve/approval")
        assert final.status_code == 200, final.text
        assert final.json()["status"] == "denied"
        assert provider.calls == []
        assert client.get(f"/v1/graph/runs/{run_id}").json()["status"] == "failed"
