from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from operant.application.phase45_gateway import Phase45ActionGateway
from operant.application.security import ActionNormalizer, PolicyEngine, SecretBroker
from operant.domain.security import Capability, PolicyBundle, PolicyDecision
from operant.mcp import McpAdapter, McpError
from operant.persistence.phase45 import SQLitePhase45Repository
from operant.persistence.security import SQLiteSecurityRepository
from operant.persistence.sqlite import ConflictError, SQLiteStore


class CountingTransport:
    def __init__(self, *, fail_call: bool = False, delay: float = 0.0) -> None:
        self.fail_call = fail_call
        self.delay = delay
        self.tool_calls = 0

    async def start(self) -> None:
        return None

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        if method == "initialize":
            return {"protocolVersion": "2024-11-05", "capabilities": {}}
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
        assert method == "tools/call"
        self.tool_calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail_call:
            raise McpError("mcp.transport_failed", "ambiguous transport failure")
        return {"content": [{"type": "text", "text": str(params["arguments"]["value"])}]}

    async def close(self) -> None:
        return None


def _allow_engine() -> PolicyEngine:
    return PolicyEngine(
        PolicyBundle(
            bundle_id="phase45-mcp-test",
            version="phase45-mcp-test.v1",
            default_decision=PolicyDecision.ALLOW,
            rules=(),
        )
    )


def _gateway(database: Path) -> Phase45ActionGateway:
    store = SQLiteStore(database)
    store.initialize()
    phase_repository = SQLitePhase45Repository(store)
    try:
        phase_repository.get_mcp_server("durable-server")
    except KeyError:
        phase_repository.put_mcp_server(_stdio_server_config("durable-server"), create_only=True)
    return Phase45ActionGateway(
        SQLiteSecurityRepository(store),
        phase_repository,
        _allow_engine(),
    )


def _adapter(database: Path, transport: CountingTransport) -> McpAdapter:
    return McpAdapter(
        server_id="durable-server",
        target_ref="stdio:durable-server",
        transport=transport,
        action_gateway=_gateway(database),
    )


@pytest.mark.asyncio
async def test_completed_receipt_replays_after_gateway_restart_without_server_call(
    tmp_path: Path,
) -> None:
    database = tmp_path / "receipt-replay.sqlite3"
    first_transport = CountingTransport()
    first = _adapter(database, first_transport)
    await first.start()
    expected = await first.call_tool("echo", {"value": "same"})

    restarted_transport = CountingTransport()
    restarted = _adapter(database, restarted_transport)
    await restarted.start()
    replayed = await restarted.call_tool("echo", {"value": "same"})

    assert replayed == expected
    assert first_transport.tool_calls == 1
    assert restarted_transport.tool_calls == 0


@pytest.mark.asyncio
async def test_unknown_receipt_is_not_replayed_after_gateway_restart(tmp_path: Path) -> None:
    database = tmp_path / "receipt-unknown.sqlite3"
    failing_transport = CountingTransport(fail_call=True)
    first = _adapter(database, failing_transport)
    await first.start()
    with pytest.raises(McpError, match="ambiguous transport failure"):
        await first.call_tool("echo", {"value": "same"})

    restarted_transport = CountingTransport()
    restarted = _adapter(database, restarted_transport)
    await restarted.start()
    with pytest.raises(McpError) as error:
        await restarted.call_tool("echo", {"value": "same"})

    assert error.value.code == "mcp.outcome_unknown"
    assert failing_transport.tool_calls == 1
    assert restarted_transport.tool_calls == 0


@pytest.mark.asyncio
async def test_concurrent_identical_calls_have_one_receipt_owner(tmp_path: Path) -> None:
    database = tmp_path / "receipt-concurrent.sqlite3"
    transports = (CountingTransport(delay=0.05), CountingTransport(delay=0.05))
    adapters = tuple(_adapter(database, transport) for transport in transports)
    await asyncio.gather(*(adapter.start() for adapter in adapters))

    results = await asyncio.gather(
        *(adapter.call_tool("echo", {"value": "same"}) for adapter in adapters),
        return_exceptions=True,
    )

    assert sum(transport.tool_calls for transport in transports) == 1
    assert sum(not isinstance(result, BaseException) for result in results) == 1
    failure = next(result for result in results if isinstance(result, McpError))
    assert failure.code == "mcp.outcome_unknown"


def _stdio_server_config(server_id: str = "fenced-server") -> dict[str, Any]:
    return {
        "server_id": server_id,
        "transport": "stdio",
        "endpoint_ref": None,
        "secret_ref": None,
        "stdio_argv": ["python", "server.py"],
        "cwd_ref": None,
        "environment_refs": {},
        "workspace_root_ref": "workspace",
        "docker_image": "sha256:" + "c" * 64,
        "allow_loopback_http": False,
    }


def test_concurrent_start_claim_is_fenced_and_stale_owner_cannot_finish(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "start-fence.sqlite3")
    store.initialize()
    repository = SQLitePhase45Repository(store)
    config = repository.put_mcp_server(_stdio_server_config(), create_only=True)

    def claim(owner: str) -> tuple[str, int] | ConflictError:
        try:
            return repository.claim_mcp_start(
                "fenced-server", owner, expected_updated_at=config["updated_at"]
            )
        except ConflictError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = tuple(pool.map(claim, ("owner-a", "owner-b")))

    winner = next(item for item in claims if isinstance(item, tuple))
    assert sum(isinstance(item, ConflictError) for item in claims) == 1
    repository.finish_mcp_start(
        "fenced-server", winner[0], winner[1], status="stopped", event_type="mcp.stopped"
    )
    refreshed = repository.get_mcp_server("fenced-server")
    next_token, next_fencing = repository.claim_mcp_start(
        "fenced-server", "owner-next", expected_updated_at=refreshed["updated_at"]
    )
    assert next_fencing == winner[1] + 1
    with pytest.raises(ConflictError, match="stale MCP lifecycle fence"):
        repository.finish_mcp_start(
            "fenced-server", winner[0], winner[1], status="running", event_type="mcp.started"
        )
    repository.finish_mcp_start(
        "fenced-server",
        next_token,
        next_fencing,
        status="running",
        event_type="mcp.started",
    )


def test_receipt_and_approval_are_bound_to_exact_action_inputs(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "bindings.sqlite3")
    store.initialize()
    repository = SQLitePhase45Repository(store)
    repository.put_mcp_server(_stdio_server_config("server-a"), create_only=True)
    repository.put_mcp_server(_stdio_server_config("server-b"), create_only=True)
    action = SQLiteSecurityRepository(store).record_security_action(
        ActionNormalizer().normalize(
            principal="core:test",
            tool="mcp",
            operation="echo",
            arguments={"target_ref": "stdio:server-a"},
            requested_capabilities=(Capability.WORKSPACE_READ,),
            idempotency_key="binding-test",
            policy_version="policy-v1",
            workspace_id="server-a",
        )
    )
    action_hash = action.action_hash
    repository.reserve_mcp_action(
        action_hash=action_hash,
        server_id="server-a",
        tool_name="echo",
        schema_sha256="e" * 64,
        arguments_sha256="f" * 64,
    )
    with pytest.raises(ConflictError, match="bound to different call inputs"):
        repository.reserve_mcp_action(
            action_hash=action_hash,
            server_id="server-b",
            tool_name="echo",
            schema_sha256="e" * 64,
            arguments_sha256="f" * 64,
        )

    repository.ensure_phase45_approval(
        approval_id="approval-bound",
        action_hash=action_hash,
        target={"target_id": "server-a"},
        policy_version="policy-v1",
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
    )
    with pytest.raises(ConflictError, match="approval action binding changed"):
        repository.ensure_phase45_approval(
            approval_id="approval-ignored",
            action_hash=action_hash,
            target={"target_id": "server-b"},
            policy_version="policy-v1",
            expires_at=(datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
        )


def test_secret_material_is_redacted_and_expired_lease_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from operant.api_phase45 import LeasedReferenceResolver

    action = ActionNormalizer().normalize(
        principal="agent:test",
        tool="remote_api",
        operation="send",
        arguments={"url": "https://example.invalid/path"},
        requested_capabilities=(Capability.SECRET_USE,),
        idempotency_key="secret-exact",
        policy_version="phase45-test.v1",
        workspace=tmp_path,
        secret_refs=("OPERANT_TEST_SECRET",),
    )
    evaluation = _allow_engine().evaluate(action)
    material = SecretBroker({"OPERANT_TEST_SECRET": "exact-secret-value"}, max_ttl_seconds=5).issue(
        action, evaluation, secret_ref="OPERANT_TEST_SECRET", ttl_seconds=1
    )
    resolver = LeasedReferenceResolver((material,))

    assert resolver.resolve("OPERANT_TEST_SECRET") == "exact-secret-value"
    assert "exact-secret-value" not in SecretBroker.redact_output(
        "bearer exact-secret-value", material
    )
    monkeypatch.setattr(
        resolver,
        "expires_at",
        datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    with pytest.raises(McpError) as error:
        resolver.resolve("OPERANT_TEST_SECRET")
    assert error.value.code == "mcp.secret_lease_expired"
    assert resolver.secret_values == ()
