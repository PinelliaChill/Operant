from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from operant.api import create_app
from operant.application.security import PolicyEngine
from operant.domain.graph import NodeKind, NodeSpec, WorkflowDefinition, WorkflowDefinitionStatus
from operant.domain.security import PolicyBundle, PolicyDecision
from operant.mcp import GatewayDecision
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


def test_stdio_mcp_default_ask_does_not_spawn(tmp_path: Path) -> None:
    marker = tmp_path / "spawned"
    server = tmp_path / "must_not_start.py"
    server.write_text(
        "from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('spawned')\n",
        encoding="utf-8",
    )
    app = create_app(tmp_path / "operant.sqlite3")
    with TestClient(app) as client:
        created = client.post(
            "/v1/mcp/servers",
            json={
                "server_id": "unapproved-local",
                "transport": "stdio",
                "stdio_argv": [sys.executable, str(server)],
            },
        )
        assert created.status_code == 201
        blocked = client.post("/v1/mcp/servers/unapproved-local/start")
        assert blocked.status_code == 409
        assert not marker.exists()
        assert client.get("/v1/mcp/servers").json()["items"][0]["lifecycle_status"] == "stopped"


def test_stdio_mcp_tool_call_requires_host_process_approval(tmp_path: Path) -> None:
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

    assert result.decision is GatewayDecision.ASK
    assert result.lease is None


def test_stdio_mcp_lifecycle_snapshot_call_and_gateway_audit(tmp_path: Path) -> None:
    server = tmp_path / "mcp_server.py"
    server.write_text(
        "import json, sys\n"
        "for line in sys.stdin:\n"
        " request=json.loads(line)\n"
        " method=request['method']\n"
        " if method == 'initialize': result={'protocolVersion':'2024-11-05'}\n"
        " elif method == 'tools/list': result={'tools':[{'name':'echo','inputSchema':"
        "{'type':'object','properties':{'value':{'type':'string'}},'required':['value'],"
        "'additionalProperties':False}}]}\n"
        " else: result={'content':[{'type':'text','text':'token=fixture-secret'}]}\n"
        " print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}), flush=True)\n",
        encoding="utf-8",
    )
    body = {
        "server_id": "local-test",
        "transport": "stdio",
        "stdio_argv": [sys.executable, str(server)],
    }
    app = create_app(tmp_path / "operant.sqlite3", phase45_policy_engine=_approved_phase45_policy())
    with TestClient(app) as client:
        assert client.post("/v1/mcp/servers", json=body).status_code == 201
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

    with app.state.operant_service.store._connect() as connection:
        event_types = {
            row[0]
            for row in connection.execute(
                "SELECT event_type FROM security_audit_events WHERE event_type LIKE 'mcp.%'"
            ).fetchall()
        }
    assert "mcp.tool_completed" in event_types


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
