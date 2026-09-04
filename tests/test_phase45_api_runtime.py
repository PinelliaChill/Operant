from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from operant.api import create_app
from operant.application.security import ApprovalReviewerAdapter, PolicyEngine
from operant.domain.graph import NodeKind, NodeSpec, WorkflowDefinition, WorkflowDefinitionStatus
from operant.domain.security import ActionRequest, PolicyBundle, PolicyDecision
from operant.mcp import GatewayDecision, LegacySseTransport, StdioTransport
from operant.persistence.graph_team import SQLiteGraphRepository
from operant.protocol import canonical_action_hash


def _approved_phase45_policy() -> PolicyEngine:
    return PolicyEngine(
        PolicyBundle(
            bundle_id="phase45-test-approved",
            version="phase45-test.v1",
            default_decision=PolicyDecision.ALLOW,
            rules=(),
        )
    )


def _mock_remote_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_start(_transport: LegacySseTransport) -> None:
        return None

    async def fake_request(
        _transport: LegacySseTransport, method: str, params: dict[str, Any]
    ) -> Any:
        calls.append((method, params))
        if method == "initialize":
            return {"protocolVersion": "2024-11-05"}
        if method == "tools/list":
            return {
                "tools": [
                    {
                        "name": "lookup",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"],
                            "additionalProperties": False,
                        },
                    }
                ]
            }
        return {"content": [{"type": "text", "text": "safe"}]}

    async def fake_close(_transport: LegacySseTransport) -> None:
        return None

    monkeypatch.setattr(LegacySseTransport, "start", fake_start)
    monkeypatch.setattr(LegacySseTransport, "request", fake_request)
    monkeypatch.setattr(LegacySseTransport, "close", fake_close)
    return calls


def _remote_server_body(server_id: str) -> dict[str, Any]:
    return {
        "server_id": server_id,
        "transport": "legacy_sse",
        "endpoint_ref": "OPERANT_TEST_MCP_ENDPOINT",
        "secret_ref": "OPERANT_TEST_MCP_SECRET",
    }


def test_skill_discovery_persists_only_untrusted_safe_projection(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    skill = root / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: demo\ndescription: bounded demo\n---\nDo bounded work.\n",
        encoding="utf-8",
    )
    with TestClient(
        create_app(tmp_path / "operant.sqlite3", phase45_skill_roots={"test-root": root})
    ) as client:
        response = client.post("/v1/skills/discover", json={})
        assert response.status_code == 200, response.text
        candidate = response.json()["candidates"][0]
        assert candidate["trust_status"] == "untrusted_candidate"
        assert candidate["root_ref"] == "test-root"
        assert str(root) not in response.text

        listed = client.get("/v1/skills").json()["items"]
        assert listed == [candidate]
        rejected = client.post("/v1/skills/discover", json={"roots": [str(tmp_path)]})
        assert rejected.status_code == 422


def test_stdio_mcp_unconfigured_workspace_does_not_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawned = False

    async def forbidden_start(_transport: StdioTransport) -> None:
        nonlocal spawned
        spawned = True

    monkeypatch.setattr(StdioTransport, "start", forbidden_start)
    app = create_app(tmp_path / "operant.sqlite3")
    with TestClient(app) as client:
        created = client.post(
            "/v1/mcp/servers",
            json={
                "server_id": "unapproved-local",
                "transport": "stdio",
                "stdio_argv": ["python", "server.py"],
                "workspace_root_ref": "workspace",
                "docker_image": "sha256:" + "a" * 64,
            },
        )
        assert created.status_code == 404
        assert spawned is False
        assert client.get("/v1/mcp/servers").json()["items"] == []


def test_stdio_mcp_tool_call_uses_no_network_docker_capabilities(tmp_path: Path) -> None:
    app = create_app(tmp_path / "operant.sqlite3")
    target_ref = "stdio:unapproved-local"
    schema_sha256 = "0" * 64
    arguments = {"value": "safe"}
    action_hash = canonical_action_hash(
        {
            "kind": "mcp_tool_call",
            "server_id": "unapproved-local",
            "target_ref": target_ref,
            "tool_name": "echo",
            "schema_sha256": schema_sha256,
            "arguments": arguments,
        }
    )

    result = asyncio.run(
        app.state.phase45_action_gateway.authorize(
            server_id="unapproved-local",
            tool_name="echo",
            action_hash=action_hash,
            target_ref=target_ref,
            schema_sha256=schema_sha256,
            arguments=arguments,
        )
    )

    assert result.decision is GatewayDecision.ALLOW
    assert result.lease is not None
    assert result.lease.action_hash == action_hash
    assert len(result.lease.lease_ids) == 2


def test_stdio_mcp_lifecycle_snapshot_call_and_gateway_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_start(_transport: StdioTransport) -> None:
        return None

    async def fake_request(_transport: StdioTransport, method: str, params: dict[str, Any]) -> Any:
        calls.append((method, params))
        if method == "initialize":
            return {"protocolVersion": "2024-11-05"}
        if method == "tools/list":
            return {
                "tools": [
                    {
                        "name": "echo",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                            "required": ["value"],
                            "additionalProperties": False,
                        },
                    }
                ]
            }
        return {"content": [{"type": "text", "text": "token=fixture-secret"}]}

    async def fake_close(_transport: StdioTransport) -> None:
        return None

    monkeypatch.setattr(StdioTransport, "start", fake_start)
    monkeypatch.setattr(StdioTransport, "request", fake_request)
    monkeypatch.setattr(StdioTransport, "close", fake_close)
    body = {
        "server_id": "local-test",
        "transport": "stdio",
        "stdio_argv": ["python", "mcp_server.py"],
        "workspace_root_ref": "workspace",
        "docker_image": "sha256:" + "b" * 64,
    }
    app = create_app(
        tmp_path / "operant.sqlite3",
        phase45_policy_engine=_approved_phase45_policy(),
        phase45_mcp_workspace_roots={"workspace": tmp_path},
    )
    with TestClient(app) as client:
        created = client.post("/v1/mcp/servers", json=body)
        assert created.status_code == 201
        roots = client.get("/v1/mcp/workspace-roots")
        assert roots.json() == {"items": [{"root_ref": "workspace"}]}
        assert str(tmp_path) not in roots.text
        started = client.post("/v1/mcp/servers/local-test/start")
        assert started.status_code == 200, started.text
        tools = client.get("/v1/mcp/servers/local-test/tools").json()["items"]
        assert [tool["name"] for tool in tools] == ["echo"]

        called = client.post(
            "/v1/mcp/servers/local-test/tools/echo/call",
            json={"arguments": {"value": "ok"}},
        )
        assert called.status_code == 200, called.text
        assert "fixture-secret" not in called.text
        assert "[REDACTED]" in called.text
        assert client.post("/v1/mcp/servers/local-test/stop").status_code == 200

    assert [method for method, _params in calls] == ["initialize", "tools/list", "tools/call"]

    persisted = created.json()
    binding = {
        key: persisted.get(key)
        for key in (
            "server_id",
            "transport",
            "endpoint_ref",
            "secret_ref",
            "stdio_argv",
            "cwd_ref",
            "workspace_root_ref",
            "docker_image",
            "allow_loopback_http",
        )
    }
    target_ref = f"stdio:local-test:{ActionRequest.calculate_hash(binding)}"
    schema_sha256 = app.state.phase45_repository.list_mcp_tools("local-test")[0]["schema_sha256"]
    expected_action_hash = canonical_action_hash(
        {
            "kind": "mcp_tool_call",
            "server_id": "local-test",
            "target_ref": target_ref,
            "tool_name": "echo",
            "schema_sha256": schema_sha256,
            "arguments": {"value": "ok"},
        }
    )
    with app.state.operant_service.store._connect() as connection:
        event_types = {
            row[0]
            for row in connection.execute(
                "SELECT event_type FROM security_audit_events WHERE event_type LIKE 'mcp.%'"
            ).fetchall()
        }
        receipt = connection.execute("SELECT action_hash FROM mcp_action_receipts").fetchone()
    assert "mcp.tool_completed" in event_types
    assert receipt["action_hash"] == expected_action_hash


def test_legacy_sse_start_ask_does_not_resolve_endpoint_or_touch_server(tmp_path: Path) -> None:
    app = create_app(tmp_path / "operant.sqlite3")
    with TestClient(app) as client:
        created = client.post(
            "/v1/mcp/servers",
            json={
                "server_id": "remote-test",
                "transport": "legacy_sse",
                "endpoint_ref": "UNSET_MCP_ENDPOINT",
            },
        )
        assert created.status_code == 201, created.text
        blocked = client.post("/v1/mcp/servers/remote-test/start")
        assert blocked.status_code == 409
        servers = client.get("/v1/mcp/servers").json()["items"]
        assert servers[0]["lifecycle_status"] == "stopped"


def test_remote_mcp_ask_can_be_approved_by_human_then_consumed_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _mock_remote_transport(monkeypatch)
    monkeypatch.setenv("OPERANT_TEST_MCP_ENDPOINT", "https://mcp.example/sse")
    monkeypatch.setenv("OPERANT_TEST_MCP_SECRET", "human-secret")
    app = create_app(tmp_path / "human-approval.sqlite3")
    with TestClient(app) as client:
        assert (
            client.post("/v1/mcp/servers", json=_remote_server_body("human-approved")).status_code
            == 201
        )
        asked = client.post("/v1/mcp/servers/human-approved/start")
        assert asked.status_code == 409
        detail = asked.json()["detail"]
        assert detail["code"] == "approval_required"

        approved = client.post(
            f"/v1/security/approvals/{detail['approval_id']}",
            json={"approved": True, "reason_code": "user-confirmed"},
        )
        assert approved.status_code == 200
        assert approved.json()["decided_by"] == "user"
        started = client.post("/v1/mcp/servers/human-approved/start")
        assert started.status_code == 200, started.text
        consumed = client.get(f"/v1/security/approvals/{detail['approval_id']}").json()
        assert consumed["status"] == "consumed"
        assert client.post("/v1/mcp/servers/human-approved/stop").status_code == 200

    assert [method for method, _params in calls] == ["initialize", "tools/list"]


def test_remote_mcp_ask_can_be_approved_by_reviewer_then_consumed_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _mock_remote_transport(monkeypatch)
    monkeypatch.setenv("OPERANT_TEST_MCP_ENDPOINT", "https://mcp.example/sse")
    monkeypatch.setenv("OPERANT_TEST_MCP_SECRET", "reviewer-secret")
    seen: list[dict[str, Any]] = []

    async def reviewer(payload: dict[str, Any]) -> dict[str, str]:
        seen.append(payload)
        return {
            "decision": "allow",
            "reason_code": "reviewer-approved",
            "summary": "bounded remote MCP start is approved",
        }

    app = create_app(
        tmp_path / "reviewer-approval.sqlite3",
        phase45_approval_reviewer=ApprovalReviewerAdapter(reviewer),
    )
    with TestClient(app) as client:
        assert (
            client.post(
                "/v1/mcp/servers", json=_remote_server_body("reviewer-approved")
            ).status_code
            == 201
        )
        asked = client.post("/v1/mcp/servers/reviewer-approved/start")
        detail = asked.json()["detail"]
        reviewed = client.post(f"/v1/security/approvals/{detail['approval_id']}/review")
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()["decided_by"] == "reviewer"
        assert reviewed.json()["review_summary"] == "bounded remote MCP start is approved"
        started = client.post("/v1/mcp/servers/reviewer-approved/start")
        assert started.status_code == 200, started.text
        assert client.post("/v1/mcp/servers/reviewer-approved/stop").status_code == 200

    assert len(seen) == 1
    assert seen[0]["secret_ref_count"] == 2
    assert "normalized_arguments" not in seen[0]
    assert [method for method, _params in calls] == ["initialize", "tools/list"]


def test_schedule_manual_trigger_is_durable_and_idempotent(tmp_path: Path) -> None:
    timer_at = datetime.now(timezone.utc) + timedelta(days=1)
    schedule = {
        "id": "schedule-api",
        "version": 1,
        "name": "API timer",
        "trigger_kind": "timer",
        "timer_at": timer_at.isoformat(),
        "timezone_name": "UTC",
        "workflow_id": "workflow-api",
        "workflow_version": 1,
        "workflow_input": {"task": "bounded"},
    }
    app = create_app(tmp_path / "operant.sqlite3")
    graph_repository = SQLiteGraphRepository(app.state.operant_service.store)
    graph_repository.put_definition(
        WorkflowDefinition(
            workflow_id="workflow-api",
            version=1,
            name="API workflow",
            nodes=(NodeSpec(node_id="artifact", node_kind=NodeKind.ARTIFACT),),
            status=WorkflowDefinitionStatus.PUBLISHED,
        )
    )
    with TestClient(app) as client:
        created = client.post(
            "/v1/schedules",
            headers={"Idempotency-Key": "schedule-create-stable"},
            json=schedule,
        )
        assert created.status_code == 201, created.text
        replayed_create = client.post(
            "/v1/schedules",
            headers={"Idempotency-Key": "schedule-create-stable"},
            json=schedule,
        )
        assert replayed_create.status_code == 201
        assert replayed_create.json() == created.json()
        assert replayed_create.headers["Idempotency-Replayed"] == "true"
        first = client.post(
            "/v1/schedules/schedule-api/trigger",
            headers={"Idempotency-Key": "schedule-trigger-stable"},
            json={"idempotency_key": "schedule-trigger-stable"},
        )
        second = client.post(
            "/v1/schedules/schedule-api/trigger",
            headers={"Idempotency-Key": "schedule-trigger-stable"},
            json={"idempotency_key": "schedule-trigger-stable"},
        )
        assert first.status_code == second.status_code == 200
        assert first.json()["id"] == second.json()["id"]
        assert second.headers["Idempotency-Replayed"] == "true"
        assert client.get("/v1/schedules/schedule-api").status_code == 200
        queue = client.get("/v1/scheduler/queue").json()["items"]
        assert [item["id"] for item in queue] == [first.json()["id"]]


def test_schedule_creation_requires_existing_published_workflow(tmp_path: Path) -> None:
    app = create_app(tmp_path / "operant.sqlite3")
    graph_repository = SQLiteGraphRepository(app.state.operant_service.store)
    schedule = {
        "id": "schedule-contract",
        "version": 1,
        "name": "Pinned workflow",
        "trigger_kind": "cron",
        "cron_expression": "0 3 * * *",
        "timezone_name": "UTC",
        "workflow_id": "workflow-contract",
        "workflow_version": 1,
    }
    with TestClient(app) as client:
        missing = client.post(
            "/v1/schedules",
            headers={"Idempotency-Key": "schedule-missing"},
            json=schedule,
        )
        assert missing.status_code == 422
        assert "does not exist" in missing.json()["detail"]

        graph_repository.put_definition(
            WorkflowDefinition(
                workflow_id="workflow-contract",
                version=1,
                name="draft",
                nodes=(NodeSpec(node_id="artifact", node_kind=NodeKind.ARTIFACT),),
                status=WorkflowDefinitionStatus.DRAFT,
            )
        )
        draft = client.post(
            "/v1/schedules",
            headers={"Idempotency-Key": "schedule-draft"},
            json=schedule,
        )
        assert draft.status_code == 422
        assert "must be published" in draft.json()["detail"]
