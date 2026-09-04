from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest

import operant.mcp.adapter as mcp_adapter_module
from operant.mcp import (
    GatewayDecision,
    LegacySseTransport,
    McpAdapter,
    McpAuditFact,
    McpCapabilityLease,
    McpError,
    McpGatewayResult,
    McpLimits,
    McpServerConfig,
    McpStdioConfig,
    StdioTransport,
)
from operant.mcp.adapter import _copy_workspace_snapshot_safely


class FakeTransport:
    def __init__(self, *, tools: list[dict[str, Any]] | None = None) -> None:
        self.started = False
        self.closed = False
        self.calls: list[tuple[str, Mapping[str, Any]]] = []
        self.tools = tools or [
            {
                "name": "lookup",
                "description": "Bounded lookup",
                "inputSchema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
            }
        ]

    async def start(self) -> None:
        self.started = True

    async def request(self, method: str, params: Mapping[str, Any]) -> Any:
        self.calls.append((method, params))
        if method == "initialize":
            return {"protocolVersion": "2024-11-05", "capabilities": {}}
        if method == "tools/list":
            return {"tools": self.tools}
        if method == "tools/call":
            return {"content": [{"type": "text", "text": "token=secret-value"}]}
        raise AssertionError(method)

    async def close(self) -> None:
        self.closed = True


class FakeGateway:
    def __init__(self, decision: GatewayDecision = GatewayDecision.ALLOW) -> None:
        self.decision = decision
        self.audit: list[McpAuditFact] = []
        self.verify_error: McpError | None = None
        self.expired_lease = False
        self.receipts: dict[str, tuple[str, Any | None]] = {}

    async def authorize(
        self,
        *,
        server_id: str,
        tool_name: str,
        action_hash: str,
        target_ref: str,
        schema_sha256: str,
        arguments: Mapping[str, Any],
    ) -> McpGatewayResult:
        del target_ref, schema_sha256, arguments
        lease = None
        if self.decision == GatewayDecision.ALLOW:
            lease = McpCapabilityLease(
                lease_id="lease-1",
                action_hash=action_hash,
                server_id=server_id,
                tool_name=tool_name,
                expires_at_monotonic=time.monotonic() + (-1 if self.expired_lease else 30),
                policy_version="policy-v1",
            )
        return McpGatewayResult(decision=self.decision, reason_code="test", lease=lease)

    async def verify_lease(
        self,
        lease: McpCapabilityLease,
        *,
        action_hash: str,
        server_id: str,
        tool_name: str,
    ) -> None:
        assert lease.action_hash == action_hash
        assert lease.server_id == server_id
        assert lease.tool_name == tool_name
        if self.verify_error is not None:
            raise self.verify_error

    async def record_audit(self, fact: McpAuditFact) -> None:
        self.audit.append(fact)

    async def reserve_outcome(
        self,
        *,
        action_hash: str,
        server_id: str,
        tool_name: str,
        schema_sha256: str,
        arguments: Mapping[str, Any],
    ) -> tuple[str, Any | None]:
        del server_id, tool_name, schema_sha256, arguments
        return self.receipts.setdefault(action_hash, ("reserved", None))

    async def mark_sent(self, action_hash: str) -> None:
        self.receipts[action_hash] = ("sent", None)

    async def complete_outcome(self, action_hash: str, result: Any) -> None:
        self.receipts[action_hash] = ("completed", result)

    async def mark_outcome_unknown(self, action_hash: str, error_code: str) -> None:
        del error_code
        self.receipts[action_hash] = ("outcome_unknown", None)


@pytest.mark.asyncio
async def test_tool_snapshot_and_gateway_fence() -> None:
    transport = FakeTransport()
    gateway = FakeGateway()
    adapter = McpAdapter(
        server_id="server-1",
        target_ref="target-ref",
        transport=transport,
        action_gateway=gateway,
    )

    snapshot = await adapter.start()
    result = await adapter.call_tool("lookup", {"query": "safe"})

    assert snapshot[0].schema_sha256
    assert result == {"content": [{"type": "text", "text": "token=[REDACTED]"}]}
    assert transport.calls[-1][0] == "tools/call"
    assert gateway.audit[-1].event_type == "mcp.tool_completed"
    assert gateway.audit[-1].result_sha256


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", [GatewayDecision.DENY, GatewayDecision.ASK])
async def test_deny_or_ask_never_reaches_server(decision: GatewayDecision) -> None:
    transport = FakeTransport()
    gateway = FakeGateway(decision)
    adapter = McpAdapter(
        server_id="server-1",
        target_ref="target-ref",
        transport=transport,
        action_gateway=gateway,
    )
    await adapter.start()

    with pytest.raises(McpError) as error:
        await adapter.call_tool("lookup", {"query": "safe"})

    assert error.value.code in {"mcp.policy_denied", "mcp.approval_required"}
    assert [method for method, _ in transport.calls].count("tools/call") == 0
    assert gateway.audit[-1].event_type == "mcp.tool_denied"


@pytest.mark.asyncio
async def test_revoked_lease_never_reaches_server_and_is_audited() -> None:
    transport = FakeTransport()
    gateway = FakeGateway()
    gateway.verify_error = McpError("mcp.lease_revoked", "revoked")
    adapter = McpAdapter(
        server_id="server-1",
        target_ref="target-ref",
        transport=transport,
        action_gateway=gateway,
    )
    await adapter.start()

    with pytest.raises(McpError, match="revoked"):
        await adapter.call_tool("lookup", {"query": "safe"})

    assert [method for method, _ in transport.calls].count("tools/call") == 0
    assert gateway.audit[-1].reason_code == "mcp.lease_revoked"


@pytest.mark.asyncio
async def test_expired_lease_is_rejected_before_gateway_consumption() -> None:
    transport = FakeTransport()
    gateway = FakeGateway()
    gateway.expired_lease = True
    adapter = McpAdapter(
        server_id="server-1",
        target_ref="target-ref",
        transport=transport,
        action_gateway=gateway,
    )
    await adapter.start()

    with pytest.raises(McpError) as error:
        await adapter.call_tool("lookup", {"query": "safe"})

    assert error.value.code == "mcp.capability_lease_invalid"
    assert [method for method, _ in transport.calls].count("tools/call") == 0
    assert gateway.audit[-1].reason_code == "capability_lease_invalid"


@pytest.mark.asyncio
async def test_schema_snapshot_rejects_unknown_and_invalid_arguments() -> None:
    transport = FakeTransport()
    gateway = FakeGateway()
    adapter = McpAdapter(
        server_id="server-1",
        target_ref="target-ref",
        transport=transport,
        action_gateway=gateway,
    )
    await adapter.start()

    with pytest.raises(McpError) as unknown:
        await adapter.call_tool("later-added", {})
    with pytest.raises(McpError) as missing:
        await adapter.call_tool("lookup", {})
    with pytest.raises(McpError) as extra:
        await adapter.call_tool("lookup", {"query": "safe", "extra": True})

    assert unknown.value.code == "mcp.tool_not_in_snapshot"
    assert missing.value.code == "mcp.arguments_invalid"
    assert extra.value.code == "mcp.arguments_invalid"
    assert [method for method, _ in transport.calls].count("tools/call") == 0


@pytest.mark.asyncio
async def test_schema_validation_is_recursive_and_fail_closed() -> None:
    transport = FakeTransport(
        tools=[
            {
                "name": "controlled",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "mode": {"type": "string", "enum": ["read"]},
                        "options": {
                            "type": "object",
                            "properties": {
                                "safe": {"const": True},
                                "level": {"type": "integer", "minimum": 1, "maximum": 3},
                            },
                            "required": ["safe"],
                            "additionalProperties": False,
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string", "enum": ["a", "b"]},
                            "minItems": 1,
                            "uniqueItems": True,
                        },
                        "quota": {
                            "type": "number",
                            "minimum": 0,
                            "exclusiveMaximum": 10,
                            "multipleOf": 0.5,
                        },
                    },
                    "required": ["mode", "options"],
                    "additionalProperties": False,
                },
            }
        ]
    )
    gateway = FakeGateway()
    adapter = McpAdapter(
        server_id="server-1",
        target_ref="target-ref",
        transport=transport,
        action_gateway=gateway,
    )
    await adapter.start()

    invalid_arguments = (
        {"mode": "delete", "options": {"safe": True}},
        {"mode": "read", "options": {"safe": False}},
        {"mode": "read", "options": {}},
        {"mode": "read", "options": {"safe": True, "extra": True}},
        {"mode": "read", "options": {"safe": True}, "tags": ["a", "a"]},
        {"mode": "read", "options": {"safe": True}, "quota": 1.25},
    )
    for arguments in invalid_arguments:
        with pytest.raises(McpError) as error:
            await adapter.call_tool("controlled", arguments)
        assert error.value.code == "mcp.arguments_invalid"

    assert [method for method, _ in transport.calls].count("tools/call") == 0
    assert gateway.receipts == {}
    await adapter.call_tool(
        "controlled",
        {"mode": "read", "options": {"safe": True, "level": 2}, "tags": ["a"], "quota": 1.5},
    )
    assert [method for method, _ in transport.calls].count("tools/call") == 1


@pytest.mark.asyncio
async def test_schema_snapshot_rejects_unsupported_keywords() -> None:
    transport = FakeTransport(
        tools=[
            {
                "name": "unsafe-schema",
                "inputSchema": {
                    "type": "object",
                    "properties": {"value": {"$ref": "#/$defs/value"}},
                },
            }
        ]
    )
    adapter = McpAdapter(
        server_id="server-1",
        target_ref="target-ref",
        transport=transport,
        action_gateway=FakeGateway(),
    )

    with pytest.raises(McpError) as error:
        await adapter.start()

    assert error.value.code == "mcp.schema_unsupported"
    assert transport.closed is True


@pytest.mark.asyncio
async def test_rejects_oversized_tool_schema_and_closes_transport() -> None:
    transport = FakeTransport(
        tools=[
            {
                "name": "large",
                "inputSchema": {"type": "object", "description": "x" * 2_000},
            }
        ]
    )
    adapter = McpAdapter(
        server_id="server-1",
        target_ref="target-ref",
        transport=transport,
        action_gateway=FakeGateway(),
        limits=McpLimits(max_schema_bytes=256),
    )

    with pytest.raises(McpError) as error:
        await adapter.start()

    assert error.value.code == "mcp.schema_too_large"
    assert transport.closed is True


class Resolver:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def resolve(self, reference: str) -> str:
        return self.values[reference]


class QueueSseStream(httpx.AsyncByteStream):
    def __init__(self, endpoint: str) -> None:
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._queue.put_nowait(f"event: endpoint\ndata: {endpoint}\n\n".encode())

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        while True:
            chunk = await self._queue.get()
            if chunk is None:
                return
            yield chunk

    async def aclose(self) -> None:
        self._queue.put_nowait(None)

    def respond(self, request_id: int) -> None:
        self._queue.put_nowait(
            (
                "event: message\n"
                f'data: {{"jsonrpc":"2.0","id":{request_id},"result":{{"ok":true}}}}\n\n'
            ).encode()
        )


@pytest.mark.asyncio
async def test_legacy_sse_uses_endpoint_and_secret_references() -> None:
    observed: dict[str, str] = {}
    stream = QueueSseStream("/messages/?session_id=opaque")

    def handler(request: httpx.Request) -> httpx.Response:
        observed["authorization"] = request.headers["Authorization"]
        if request.method == "GET":
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream; charset=utf-8"},
                stream=stream,
            )
        assert str(request.url) == "https://mcp.example/messages/?session_id=opaque"
        stream.respond(json.loads(request.content)["id"])
        return httpx.Response(202)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = LegacySseTransport(
        McpServerConfig(
            server_id="server",
            endpoint_ref="MCP_ENDPOINT",
            secret_ref="MCP_SECRET",
        ),
        reference_resolver=Resolver(
            {"MCP_ENDPOINT": "https://mcp.example/rpc", "MCP_SECRET": "secret-value"}
        ),
        client=client,
    )

    await transport.start()
    result = await transport.request("ping", {})
    await transport.close()
    await client.aclose()

    assert result == {"ok": True}
    assert observed["authorization"] == "Bearer secret-value"
    assert transport.config.secret_ref == "MCP_SECRET"


@pytest.mark.asyncio
async def test_legacy_sse_rejects_insecure_endpoint_and_mismatched_id() -> None:
    insecure = LegacySseTransport(
        McpServerConfig(server_id="server", endpoint_ref="endpoint"),
        reference_resolver=Resolver({"endpoint": "http://example.com/rpc"}),
    )
    with pytest.raises(McpError) as endpoint_error:
        await insecure.start()
    assert endpoint_error.value.code == "mcp.endpoint_insecure"

    stream = QueueSseStream("/messages")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)
        stream.respond(99)
        return httpx.Response(202)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = LegacySseTransport(
        McpServerConfig(server_id="server", endpoint_ref="endpoint"),
        reference_resolver=Resolver({"endpoint": "https://example.com/rpc"}),
        client=client,
    )
    await transport.start()
    with pytest.raises(McpError) as id_error:
        await transport.request("ping", {})
    await transport.close()
    await client.aclose()
    assert id_error.value.code == "mcp.response_id_mismatch"


@pytest.mark.asyncio
async def test_legacy_sse_rejects_cross_origin_message_endpoint() -> None:
    stream = QueueSseStream("https://attacker.example/messages")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = LegacySseTransport(
        McpServerConfig(server_id="server", endpoint_ref="endpoint"),
        reference_resolver=Resolver({"endpoint": "https://mcp.example/sse"}),
        client=client,
    )

    with pytest.raises(McpError) as error:
        await transport.start()
    await client.aclose()

    assert error.value.code == "mcp.endpoint_origin_mismatch"


@pytest.mark.asyncio
async def test_stdio_is_docker_argv_only_bounded_and_stderr_is_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeStdin:
        def __init__(self) -> None:
            self.frames: list[bytes] = []

        def write(self, frame: bytes) -> None:
            self.frames.append(frame)

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

    class FakeProcess:
        def __init__(self) -> None:
            self.stdin = FakeStdin()
            self.stdout = asyncio.StreamReader()
            self.stdout.feed_data(b'{"jsonrpc":"2.0","id":1,"result":{"env":null}}\n')
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_data(b"token=server-secret\n")
            self.stderr.feed_eof()
            self.returncode: int | None = None

        def terminate(self) -> None:
            self.returncode = 0

        def kill(self) -> None:
            self.returncode = -9

        async def wait(self) -> int:
            return 0 if self.returncode is None else self.returncode

    observed: dict[str, Any] = {}

    async def fake_create_subprocess_exec(*argv: str, **kwargs: Any) -> FakeProcess:
        observed["argv"] = argv
        observed["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    transport = StdioTransport(
        McpStdioConfig(
            argv=("python", "server.py"),
            workspace=str(tmp_path),
            docker_image="sha256:" + "a" * 64,
        ),
        limits=McpLimits(request_timeout_seconds=2, lifecycle_timeout_seconds=2),
    )
    await transport.start()
    result = await transport.request("ping", {})
    await transport.close()

    assert result == {"env": None}
    assert observed["argv"][:5] == ("docker", "run", "--pull", "never", "--interactive")
    assert "--network" in observed["argv"]
    assert "none" in observed["argv"]
    assert observed["kwargs"]["cwd"] != tmp_path
    assert "server-secret" not in transport.safe_stderr
    assert "[REDACTED]" in transport.safe_stderr


@pytest.mark.asyncio
async def test_stdio_timeout_is_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeStdin:
        def write(self, _frame: bytes) -> None:
            return None

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

    class FakeProcess:
        def __init__(self) -> None:
            self.stdin = FakeStdin()
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_eof()
            self.returncode: int | None = None

        def terminate(self) -> None:
            self.returncode = 0
            self.stdout.feed_eof()

        def kill(self) -> None:
            self.returncode = -9

        async def wait(self) -> int:
            return 0 if self.returncode is None else self.returncode

    async def fake_create_subprocess_exec(*_argv: str, **_kwargs: Any) -> FakeProcess:
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    transport = StdioTransport(
        McpStdioConfig(
            argv=("python", "slow.py"),
            workspace=str(tmp_path),
            docker_image="sha256:" + "b" * 64,
        ),
        limits=McpLimits(request_timeout_seconds=0.05, lifecycle_timeout_seconds=1),
    )
    await transport.start()
    with pytest.raises(McpError) as error:
        await transport.request("ping", {})
    await transport.close()
    assert error.value.code == "mcp.request_timeout"


@pytest.mark.asyncio
async def test_stdio_rejects_oversized_snapshot_before_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "large.bin").write_bytes(b"x" * 2_048)
    spawned = False

    async def forbidden_spawn(*_argv: str, **_kwargs: Any) -> None:
        nonlocal spawned
        spawned = True

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_spawn)
    transport = StdioTransport(
        McpStdioConfig(
            argv=("server",),
            workspace=str(tmp_path),
            docker_image="sha256:" + "c" * 64,
        ),
        limits=McpLimits(max_snapshot_bytes=1_024),
    )

    with pytest.raises(McpError) as error:
        await transport.start()

    assert error.value.code == "mcp.stdio_snapshot_limit"
    assert spawned is False


def test_stdio_snapshot_rejects_symbolic_links(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("must-not-copy", encoding="utf-8")
    (workspace / "outside-link").symlink_to(outside)

    with pytest.raises(McpError) as error:
        _copy_workspace_snapshot_safely(
            workspace,
            tmp_path / "snapshot",
            McpLimits(),
        )

    assert error.value.code == "mcp.stdio_snapshot_unsafe"
    assert not (tmp_path / "snapshot" / "outside-link").exists()


def test_stdio_snapshot_replacement_race_fails_without_copying_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    visible = workspace / "visible.txt"
    visible.write_text("safe", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("must-not-copy", encoding="utf-8")
    real_open = os.open
    swapped = False

    def racing_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == "visible.txt" and dir_fd is not None and not swapped:
            swapped = True
            visible.unlink()
            visible.symlink_to(outside)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(mcp_adapter_module.os, "open", racing_open)

    with pytest.raises(McpError) as error:
        _copy_workspace_snapshot_safely(
            workspace,
            tmp_path / "snapshot",
            McpLimits(),
        )

    assert swapped is True
    assert error.value.code == "mcp.stdio_snapshot_unreadable"
    copied = tmp_path / "snapshot" / "visible.txt"
    assert not copied.exists()
