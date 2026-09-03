from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager
from enum import Enum
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from operant.protocol import canonical_action_hash, redact_public_data, redact_public_text


class McpError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class McpLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    lifecycle_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    max_frame_bytes: int = Field(default=1_000_000, ge=1_024, le=8_000_000)
    max_schema_bytes: int = Field(default=128_000, ge=256, le=1_000_000)
    max_tools: int = Field(default=256, ge=1, le=2_048)
    max_json_depth: int = Field(default=20, ge=1, le=64)
    max_json_items: int = Field(default=10_000, ge=1, le=100_000)
    max_string_chars: int = Field(default=100_000, ge=1, le=1_000_000)
    max_stderr_chars: int = Field(default=20_000, ge=0, le=200_000)


class McpStdioConfig(BaseModel):
    """Preconfigured executable argv. Shell parsing is deliberately absent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    argv: tuple[str, ...] = Field(min_length=1, max_length=64)
    cwd: str | None = None
    environment_refs: dict[str, str] = Field(default_factory=dict)

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not part or "\x00" in part or len(part) > 8_192 for part in value):
            raise ValueError("stdio argv contains an invalid argument")
        return value

    @field_validator("environment_refs")
    @classmethod
    def validate_environment_refs(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 64 or any(not key or not ref for key, ref in value.items()):
            raise ValueError("environment references are invalid or too numerous")
        return value


class McpServerConfig(BaseModel):
    """Remote configuration stores references, never an endpoint secret value."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    server_id: str = Field(min_length=1, max_length=200)
    endpoint_ref: str = Field(min_length=1, max_length=300)
    secret_ref: str | None = Field(default=None, min_length=1, max_length=300)
    allow_loopback_http: bool = False


class McpTool(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=10_000)
    input_schema: dict[str, Any]
    schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class GatewayDecision(str, Enum):
    DENY = "deny"
    ASK = "ask"
    ALLOW = "allow"


class McpCapabilityLease(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    lease_id: str = Field(min_length=1, max_length=300)
    lease_ids: tuple[str, ...] = ()
    action_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    security_action_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    server_id: str = Field(min_length=1, max_length=200)
    tool_name: str = Field(min_length=1, max_length=200)
    expires_at_monotonic: float = Field(gt=0, allow_inf_nan=False)
    max_uses: int = Field(default=1, ge=1, le=100)
    policy_version: str = Field(min_length=1, max_length=200)


class McpGatewayResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: GatewayDecision
    reason_code: str = Field(min_length=1, max_length=200)
    lease: McpCapabilityLease | None = None

    @model_validator(mode="after")
    def validate_decision(self) -> McpGatewayResult:
        if self.decision == GatewayDecision.ALLOW and self.lease is None:
            raise ValueError("ALLOW requires a capability lease")
        if self.decision in (GatewayDecision.DENY, GatewayDecision.ASK) and self.lease is not None:
            raise ValueError("DENY and ASK cannot issue a capability lease")
        return self


class McpAuditFact(BaseModel):
    """Secret-safe fact. Arguments and raw results are deliberately excluded."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_type: str = Field(min_length=1, max_length=100)
    server_id: str = Field(min_length=1, max_length=200)
    tool_name: str = Field(min_length=1, max_length=200)
    action_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_version: str | None = Field(default=None, max_length=200)
    reason_code: str | None = Field(default=None, max_length=200)
    result_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    duration_ms: int | None = Field(default=None, ge=0)


class McpActionGateway(Protocol):
    async def authorize(
        self,
        *,
        server_id: str,
        tool_name: str,
        action_hash: str,
        target_ref: str,
        schema_sha256: str,
        arguments: Mapping[str, Any],
    ) -> McpGatewayResult: ...

    async def verify_lease(
        self,
        lease: McpCapabilityLease,
        *,
        action_hash: str,
        server_id: str,
        tool_name: str,
    ) -> None: ...

    async def record_audit(self, fact: McpAuditFact) -> None: ...


class JsonRpcTransport(Protocol):
    async def start(self) -> None: ...

    async def request(self, method: str, params: Mapping[str, Any]) -> Any: ...

    async def close(self) -> None: ...


class ReferenceResolver(Protocol):
    def resolve(self, reference: str) -> str: ...


class StdioTransport:
    def __init__(
        self,
        config: McpStdioConfig,
        *,
        reference_resolver: ReferenceResolver | None = None,
        limits: McpLimits | None = None,
    ) -> None:
        self.config = config
        self.limits = limits or McpLimits()
        self._resolver = reference_resolver
        self._process: asyncio.subprocess.Process | None = None
        self._request_id = 0
        self._lock = asyncio.Lock()
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr = ""

    @property
    def safe_stderr(self) -> str:
        return redact_public_text(self._stderr, max_chars=self.limits.max_stderr_chars)

    async def start(self) -> None:
        if self._process is not None:
            return
        # MCP subprocesses receive only explicitly mapped references. They do not
        # inherit the Core process environment or its unrelated credentials.
        environment: dict[str, str] = {}
        if self.config.environment_refs:
            if self._resolver is None:
                raise McpError("mcp.reference_resolver_required", "reference resolver is required")
            environment = {
                key: self._resolver.resolve(reference)
                for key, reference in self.config.environment_refs.items()
            }
        try:
            self._process = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *self.config.argv,
                    cwd=self.config.cwd,
                    env=environment,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=self.limits.max_frame_bytes + 1,
                ),
                timeout=self.limits.lifecycle_timeout_seconds,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise McpError("mcp.stdio_start_failed", "MCP stdio process could not start") from exc
        assert self._process.stderr is not None
        self._stderr_task = asyncio.create_task(self._drain_stderr(self._process.stderr))

    async def request(self, method: str, params: Mapping[str, Any]) -> Any:
        async with self._lock:
            process = self._require_process()
            if process.returncode is not None:
                raise McpError("mcp.process_exited", "MCP stdio process exited")
            self._request_id += 1
            request_id = self._request_id
            payload = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": dict(params),
            }
            encoded = _encode_frame(payload, self.limits)
            assert process.stdin is not None
            process.stdin.write(encoded + b"\n")
            try:
                await asyncio.wait_for(
                    process.stdin.drain(), timeout=self.limits.request_timeout_seconds
                )
                response = await asyncio.wait_for(
                    self._read_response(process, request_id),
                    timeout=self.limits.request_timeout_seconds,
                )
            except asyncio.TimeoutError as exc:
                raise McpError("mcp.request_timeout", "MCP request timed out") from exc
            return _response_result(response, request_id)

    async def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), self.limits.lifecycle_timeout_seconds)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        if self._stderr_task is not None:
            try:
                await asyncio.wait_for(self._stderr_task, self.limits.lifecycle_timeout_seconds)
            except asyncio.TimeoutError:
                self._stderr_task.cancel()
            self._stderr_task = None

    def _require_process(self) -> asyncio.subprocess.Process:
        if self._process is None:
            raise McpError("mcp.not_started", "MCP stdio process is not started")
        return self._process

    async def _read_response(
        self, process: asyncio.subprocess.Process, request_id: int
    ) -> dict[str, Any]:
        assert process.stdout is not None
        while True:
            try:
                line = await process.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError) as exc:
                raise McpError("mcp.frame_too_large", "MCP response frame is too large") from exc
            if not line:
                raise McpError("mcp.unexpected_eof", "MCP stdio response ended unexpectedly")
            if len(line) > self.limits.max_frame_bytes:
                raise McpError("mcp.frame_too_large", "MCP response frame is too large")
            value = _decode_object(line, self.limits)
            if "id" not in value:
                continue
            if value.get("id") != request_id:
                raise McpError("mcp.response_id_mismatch", "MCP response ID does not match")
            return value

    async def _drain_stderr(self, stream: asyncio.StreamReader) -> None:
        captured = bytearray()
        byte_limit = self.limits.max_stderr_chars * 4
        while True:
            chunk = await stream.read(4_096)
            if not chunk:
                break
            if len(captured) < byte_limit:
                captured.extend(chunk[: byte_limit - len(captured)])
        self._stderr = captured.decode("utf-8", errors="replace")


class LegacySseTransport:
    """Legacy MCP transport: long-lived SSE receive stream plus POST endpoint."""

    def __init__(
        self,
        config: McpServerConfig,
        *,
        reference_resolver: ReferenceResolver,
        limits: McpLimits | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self.limits = limits or McpLimits()
        self._resolver = reference_resolver
        self._client = client
        self._owns_client = client is None
        self._request_id = 0
        self._lock = asyncio.Lock()
        self._endpoint: str | None = None
        self._message_endpoint: str | None = None
        self._stream_context: AbstractAsyncContextManager[httpx.Response] | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._endpoint_future: asyncio.Future[str] | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._headers: dict[str, str] = {}

    async def start(self) -> None:
        if self._endpoint is not None:
            return
        endpoint = self._resolver.resolve(self.config.endpoint_ref)
        _validate_remote_endpoint(endpoint, self.config.allow_loopback_http)
        self._endpoint = endpoint
        if self._client is None:
            self._client = httpx.AsyncClient(
                follow_redirects=False,
                timeout=self.limits.request_timeout_seconds,
            )
        self._headers = {"Accept": "text/event-stream"}
        if self.config.secret_ref is not None:
            secret = self._resolver.resolve(self.config.secret_ref)
            self._headers["Authorization"] = f"Bearer {secret}"
        self._endpoint_future = asyncio.get_running_loop().create_future()
        try:
            self._stream_context = self._client.stream("GET", endpoint, headers=self._headers)
            response = await asyncio.wait_for(
                self._stream_context.__aenter__(),
                timeout=self.limits.lifecycle_timeout_seconds,
            )
            if response.is_redirect or response.history:
                raise McpError("mcp.redirect_rejected", "MCP endpoint redirect is rejected")
            response.raise_for_status()
            _require_sse_content_type(response)
            self._reader_task = asyncio.create_task(self._receive_events(response))
            self._message_endpoint = await asyncio.wait_for(
                asyncio.shield(self._endpoint_future),
                timeout=self.limits.lifecycle_timeout_seconds,
            )
        except McpError:
            await self.close()
            raise
        except (httpx.HTTPError, asyncio.TimeoutError, UnicodeError) as exc:
            await self.close()
            raise McpError("mcp.remote_start_failed", "remote MCP SSE startup failed") from exc

    async def request(self, method: str, params: Mapping[str, Any]) -> Any:
        async with self._lock:
            if self._endpoint is None or self._message_endpoint is None or self._client is None:
                raise McpError("mcp.not_started", "MCP SSE transport is not started")
            self._request_id += 1
            request_id = self._request_id
            payload = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": dict(params),
            }
            encoded = _encode_frame(payload, self.limits)
            response_future: asyncio.Future[dict[str, Any]] = (
                asyncio.get_running_loop().create_future()
            )
            self._pending[request_id] = response_future
            headers = {
                **self._headers,
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
            try:
                response = await self._client.post(
                    self._message_endpoint,
                    content=encoded,
                    headers=headers,
                )
                if response.is_redirect or response.history:
                    raise McpError("mcp.redirect_rejected", "MCP endpoint redirect is rejected")
                response.raise_for_status()
                json_response = await asyncio.wait_for(
                    asyncio.shield(response_future),
                    timeout=self.limits.request_timeout_seconds,
                )
            except McpError:
                raise
            except (httpx.HTTPError, asyncio.TimeoutError) as exc:
                raise McpError("mcp.remote_request_failed", "remote MCP request failed") from exc
            finally:
                self._pending.pop(request_id, None)
            return _response_result(json_response, request_id)

    async def close(self) -> None:
        self._endpoint = None
        self._message_endpoint = None
        error = McpError("mcp.transport_closed", "MCP SSE transport was closed")
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()
        if self._reader_task is not None:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
            self._reader_task = None
        if self._stream_context is not None:
            await self._stream_context.__aexit__(None, None, None)
            self._stream_context = None
        if self._endpoint_future is not None and not self._endpoint_future.done():
            self._endpoint_future.cancel()
        self._endpoint_future = None
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
        self._headers = {}

    async def _receive_events(self, response: httpx.Response) -> None:
        try:
            async for event_type, data in _iter_sse_events(response.aiter_bytes(), self.limits):
                if event_type == "endpoint":
                    if self._message_endpoint is not None:
                        raise McpError(
                            "mcp.endpoint_changed", "MCP server changed its message endpoint"
                        )
                    assert self._endpoint is not None
                    message_endpoint = urljoin(
                        self._endpoint, data.decode("utf-8", errors="strict")
                    )
                    _validate_derived_message_endpoint(
                        self._endpoint,
                        message_endpoint,
                        self.config.allow_loopback_http,
                    )
                    self._message_endpoint = message_endpoint
                    if self._endpoint_future is not None and not self._endpoint_future.done():
                        self._endpoint_future.set_result(message_endpoint)
                    continue
                if event_type not in ("message", ""):
                    continue
                value = _decode_object(data, self.limits)
                response_id = value.get("id")
                if not isinstance(response_id, int) or isinstance(response_id, bool):
                    continue
                future = self._pending.get(response_id)
                if future is None:
                    raise McpError(
                        "mcp.response_id_mismatch", "MCP response ID has no pending request"
                    )
                if not future.done():
                    future.set_result(value)
            raise McpError("mcp.unexpected_eof", "MCP SSE stream ended unexpectedly")
        except BaseException as exc:
            safe_error = (
                exc
                if isinstance(exc, McpError)
                else McpError("mcp.remote_stream_failed", "remote MCP SSE stream failed")
            )
            if self._endpoint_future is not None and not self._endpoint_future.done():
                self._endpoint_future.set_exception(safe_error)
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(safe_error)


class McpAdapter:
    """MCP lifecycle and tool calls fenced by an injected Action Gateway."""

    def __init__(
        self,
        *,
        server_id: str,
        target_ref: str,
        transport: JsonRpcTransport,
        action_gateway: McpActionGateway,
        limits: McpLimits | None = None,
    ) -> None:
        self.server_id = server_id
        self.target_ref = target_ref
        self.transport = transport
        self.action_gateway = action_gateway
        self.limits = limits or McpLimits()
        self._tools: dict[str, McpTool] = {}
        self._started = False

    @property
    def tool_snapshot(self) -> tuple[McpTool, ...]:
        return tuple(self._tools[name] for name in sorted(self._tools))

    async def start(self) -> tuple[McpTool, ...]:
        if self._started:
            return self.tool_snapshot
        await self.transport.start()
        try:
            initialize_result = await asyncio.wait_for(
                self.transport.request(
                    "initialize",
                    {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "operant", "version": "2.0"},
                    },
                ),
                timeout=self.limits.lifecycle_timeout_seconds,
            )
            _validate_bounded_json(initialize_result, self.limits)
            if not isinstance(initialize_result, dict):
                raise McpError("mcp.initialize_invalid", "MCP initialize result is invalid")
            raw_tools = await asyncio.wait_for(
                self.transport.request("tools/list", {}),
                timeout=self.limits.lifecycle_timeout_seconds,
            )
            self._tools = self._parse_tools(raw_tools)
            self._started = True
            return self.tool_snapshot
        except asyncio.TimeoutError as exc:
            await self.transport.close()
            raise McpError("mcp.lifecycle_timeout", "MCP lifecycle request timed out") from exc
        except BaseException:
            await self.transport.close()
            raise

    async def close(self) -> None:
        self._started = False
        self._tools = {}
        await self.transport.close()

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Any:
        if not self._started:
            raise McpError("mcp.not_started", "MCP adapter is not started")
        tool = self._tools.get(name)
        if tool is None:
            raise McpError("mcp.tool_not_in_snapshot", "MCP tool is not in the approved snapshot")
        _validate_bounded_json(dict(arguments), self.limits)
        _validate_arguments(tool.input_schema, arguments)
        action_hash = canonical_action_hash(
            {
                "kind": "mcp_tool_call",
                "server_id": self.server_id,
                "target_ref": self.target_ref,
                "tool_name": name,
                "schema_sha256": tool.schema_sha256,
                "arguments": dict(arguments),
            }
        )
        result = await self.action_gateway.authorize(
            server_id=self.server_id,
            tool_name=name,
            action_hash=action_hash,
            target_ref=self.target_ref,
            schema_sha256=tool.schema_sha256,
            arguments=dict(arguments),
        )
        if result.decision != GatewayDecision.ALLOW or result.lease is None:
            await self.action_gateway.record_audit(
                McpAuditFact(
                    event_type="mcp.tool_denied",
                    server_id=self.server_id,
                    tool_name=name,
                    action_hash=action_hash,
                    reason_code=result.reason_code,
                )
            )
            code = (
                "mcp.policy_denied"
                if result.decision == GatewayDecision.DENY
                else "mcp.approval_required"
            )
            raise McpError(code, "MCP tool call is not authorized")
        lease = result.lease
        if (
            lease.action_hash != action_hash
            or lease.server_id != self.server_id
            or lease.tool_name != name
            or lease.expires_at_monotonic <= time.monotonic()
        ):
            await self.action_gateway.record_audit(
                McpAuditFact(
                    event_type="mcp.tool_denied",
                    server_id=self.server_id,
                    tool_name=name,
                    action_hash=action_hash,
                    policy_version=lease.policy_version,
                    reason_code="capability_lease_invalid",
                )
            )
            raise McpError("mcp.capability_lease_invalid", "MCP capability lease is invalid")
        try:
            await self.action_gateway.verify_lease(
                lease,
                action_hash=action_hash,
                server_id=self.server_id,
                tool_name=name,
            )
        except BaseException as exc:
            await self.action_gateway.record_audit(
                McpAuditFact(
                    event_type="mcp.tool_denied",
                    server_id=self.server_id,
                    tool_name=name,
                    action_hash=action_hash,
                    policy_version=lease.policy_version,
                    reason_code=(
                        exc.code if isinstance(exc, McpError) else "lease_verification_failed"
                    ),
                )
            )
            raise
        started = time.monotonic()
        try:
            value = await asyncio.wait_for(
                self.transport.request("tools/call", {"name": name, "arguments": dict(arguments)}),
                timeout=self.limits.request_timeout_seconds,
            )
            _validate_bounded_json(value, self.limits)
        except BaseException as exc:
            await self.action_gateway.record_audit(
                McpAuditFact(
                    event_type="mcp.tool_failed",
                    server_id=self.server_id,
                    tool_name=name,
                    action_hash=action_hash,
                    policy_version=lease.policy_version,
                    reason_code=(exc.code if isinstance(exc, McpError) else type(exc).__name__),
                    duration_ms=int((time.monotonic() - started) * 1_000),
                )
            )
            raise
        safe_value = redact_public_data(value)
        result_digest = hashlib.sha256(
            json.dumps(
                safe_value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        await self.action_gateway.record_audit(
            McpAuditFact(
                event_type="mcp.tool_completed",
                server_id=self.server_id,
                tool_name=name,
                action_hash=action_hash,
                policy_version=lease.policy_version,
                result_sha256=result_digest,
                duration_ms=int((time.monotonic() - started) * 1_000),
            )
        )
        return safe_value

    def _parse_tools(self, value: Any) -> dict[str, McpTool]:
        _validate_bounded_json(value, self.limits)
        if not isinstance(value, dict) or not isinstance(value.get("tools"), list):
            raise McpError("mcp.tools_invalid", "MCP tools/list result is invalid")
        raw_tools = value["tools"]
        if len(raw_tools) > self.limits.max_tools:
            raise McpError("mcp.tool_limit_exceeded", "MCP tool snapshot is too large")
        tools: dict[str, McpTool] = {}
        for item in raw_tools:
            if not isinstance(item, dict):
                raise McpError("mcp.tools_invalid", "MCP tool entry is invalid")
            name = item.get("name")
            description = item.get("description", "")
            schema = item.get("inputSchema")
            if (
                not isinstance(name, str)
                or not name
                or len(name) > 200
                or not isinstance(description, str)
                or len(description) > 10_000
                or not isinstance(schema, dict)
            ):
                raise McpError("mcp.tools_invalid", "MCP tool entry is invalid")
            if name in tools:
                raise McpError("mcp.tools_invalid", "MCP tool names must be unique")
            schema_bytes = json.dumps(
                schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
            if len(schema_bytes) > self.limits.max_schema_bytes:
                raise McpError("mcp.schema_too_large", "MCP tool schema is too large")
            tools[name] = McpTool(
                name=name,
                description=description,
                input_schema=schema,
                schema_sha256=hashlib.sha256(schema_bytes).hexdigest(),
            )
        return tools


def _encode_frame(value: Any, limits: McpLimits) -> bytes:
    _validate_bounded_json(value, limits)
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise McpError("mcp.json_invalid", "MCP payload is not valid JSON") from exc
    if len(encoded) > limits.max_frame_bytes:
        raise McpError("mcp.frame_too_large", "MCP frame is too large")
    return encoded


def _decode_object(data: bytes, limits: McpLimits) -> dict[str, Any]:
    if len(data) > limits.max_frame_bytes:
        raise McpError("mcp.frame_too_large", "MCP frame is too large")
    try:
        value = json.loads(data)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise McpError("mcp.json_invalid", "MCP response is not valid JSON") from exc
    _validate_bounded_json(value, limits)
    if not isinstance(value, dict):
        raise McpError("mcp.jsonrpc_invalid", "MCP response must be an object")
    return value


def _response_result(response: Mapping[str, Any], request_id: int) -> Any:
    if response.get("jsonrpc") != "2.0" or response.get("id") != request_id:
        raise McpError("mcp.jsonrpc_invalid", "MCP JSON-RPC envelope is invalid")
    has_result = "result" in response
    has_error = "error" in response
    if has_result == has_error:
        raise McpError("mcp.jsonrpc_invalid", "MCP response must contain result or error")
    if has_error:
        error = response["error"]
        code = error.get("code") if isinstance(error, dict) else None
        raise McpError("mcp.remote_error", f"MCP server returned error {code!s}"[:200])
    return response["result"]


def _validate_bounded_json(value: Any, limits: McpLimits) -> None:
    remaining = [limits.max_json_items]

    def visit(item: Any, depth: int) -> None:
        if depth > limits.max_json_depth:
            raise McpError("mcp.json_too_deep", "MCP JSON nesting is too deep")
        remaining[0] -= 1
        if remaining[0] < 0:
            raise McpError("mcp.json_too_large", "MCP JSON contains too many values")
        if item is None or isinstance(item, (bool, int)):
            return
        if isinstance(item, float):
            if item != item or item in (float("inf"), float("-inf")):
                raise McpError("mcp.json_invalid", "MCP JSON contains a non-finite number")
            return
        if isinstance(item, str):
            if len(item) > limits.max_string_chars:
                raise McpError("mcp.string_too_large", "MCP JSON string is too large")
            return
        if isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
            return
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or len(key) > 1_000:
                    raise McpError("mcp.json_invalid", "MCP JSON object key is invalid")
                visit(child, depth + 1)
            return
        raise McpError("mcp.json_invalid", "MCP payload contains a non-JSON value")

    visit(value, 0)


def _validate_arguments(schema: Mapping[str, Any], arguments: Mapping[str, Any]) -> None:
    if schema.get("type") not in (None, "object"):
        raise McpError("mcp.schema_unsupported", "MCP input schema root must be an object")
    required = schema.get("required", [])
    properties = schema.get("properties", {})
    if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
        raise McpError("mcp.schema_invalid", "MCP input schema required list is invalid")
    if not isinstance(properties, dict):
        raise McpError("mcp.schema_invalid", "MCP input schema properties are invalid")
    missing = set(required).difference(arguments)
    if missing:
        raise McpError("mcp.arguments_invalid", "MCP tool arguments omit required fields")
    if schema.get("additionalProperties") is False and set(arguments).difference(properties):
        raise McpError("mcp.arguments_invalid", "MCP tool arguments contain unknown fields")
    python_types: dict[str, tuple[type[Any], ...]] = {
        "string": (str,),
        "integer": (int,),
        "number": (int, float),
        "boolean": (bool,),
        "object": (dict,),
        "array": (list,),
        "null": (type(None),),
    }
    for key, value in arguments.items():
        property_schema = properties.get(key)
        if not isinstance(property_schema, dict):
            continue
        expected = property_schema.get("type")
        if isinstance(expected, str) and expected in python_types:
            if expected in ("integer", "number") and isinstance(value, bool):
                raise McpError("mcp.arguments_invalid", "MCP tool argument type is invalid")
            if not isinstance(value, python_types[expected]):
                raise McpError("mcp.arguments_invalid", "MCP tool argument type is invalid")


def _validate_remote_endpoint(endpoint: str, allow_loopback_http: bool) -> None:
    parsed = urlsplit(endpoint)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise McpError("mcp.endpoint_invalid", "MCP endpoint contains forbidden components")
    if not parsed.hostname or parsed.scheme not in ("https", "http"):
        raise McpError("mcp.endpoint_invalid", "MCP endpoint must be HTTP(S)")
    if parsed.scheme == "https":
        return
    if not allow_loopback_http:
        raise McpError("mcp.endpoint_insecure", "plain HTTP MCP endpoints are rejected")
    hostname = parsed.hostname.lower()
    if hostname == "localhost":
        return
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError as exc:
        raise McpError("mcp.endpoint_insecure", "plain HTTP is limited to loopback") from exc
    if not address.is_loopback:
        raise McpError("mcp.endpoint_insecure", "plain HTTP is limited to loopback")


def _validate_derived_message_endpoint(
    stream_endpoint: str,
    message_endpoint: str,
    allow_loopback_http: bool,
) -> None:
    """Keep the server-provided POST endpoint on the configured origin.

    Legacy MCP commonly puts an opaque session identifier in the derived
    endpoint query, so the query is permitted here but never on the configured
    endpoint reference itself.
    """

    _validate_remote_endpoint_without_query(message_endpoint, allow_loopback_http)
    stream = urlsplit(stream_endpoint)
    message = urlsplit(message_endpoint)
    if (
        stream.scheme.lower(),
        stream.hostname.lower() if stream.hostname else None,
        stream.port,
    ) != (
        message.scheme.lower(),
        message.hostname.lower() if message.hostname else None,
        message.port,
    ):
        raise McpError(
            "mcp.endpoint_origin_mismatch",
            "MCP message endpoint must use the configured stream origin",
        )


def _validate_remote_endpoint_without_query(endpoint: str, allow_loopback_http: bool) -> None:
    parsed = urlsplit(endpoint)
    if parsed.username or parsed.password or parsed.fragment:
        raise McpError("mcp.endpoint_invalid", "MCP endpoint contains forbidden components")
    queryless = parsed._replace(query="").geturl()
    _validate_remote_endpoint(queryless, allow_loopback_http)


def _require_sse_content_type(response: httpx.Response) -> None:
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "text/event-stream":
        raise McpError("mcp.content_type_invalid", "MCP SSE response has invalid content type")


async def _iter_sse_events(
    chunks: AsyncIterator[bytes], limits: McpLimits
) -> AsyncIterator[tuple[str, bytes]]:
    """Parse SSE incrementally while bounding one line and one event frame."""

    buffer = bytearray()
    event_type = ""
    data_lines: list[bytes] = []
    data_size = 0

    async for chunk in chunks:
        if not isinstance(chunk, bytes):
            raise McpError("mcp.sse_invalid", "MCP SSE stream yielded a non-byte chunk")
        buffer.extend(chunk)
        if len(buffer) > limits.max_frame_bytes and b"\n" not in buffer:
            raise McpError("mcp.frame_too_large", "MCP SSE line is too large")
        while b"\n" in buffer:
            raw_line, _, remainder = buffer.partition(b"\n")
            buffer = bytearray(remainder)
            if len(raw_line) > limits.max_frame_bytes:
                raise McpError("mcp.frame_too_large", "MCP SSE line is too large")
            line = raw_line.rstrip(b"\r")
            if not line:
                if data_lines:
                    yield event_type, b"\n".join(data_lines)
                event_type = ""
                data_lines = []
                data_size = 0
                continue
            if line.startswith(b":"):
                continue
            field, separator, raw_value = line.partition(b":")
            value = raw_value[1:] if separator and raw_value.startswith(b" ") else raw_value
            if field == b"event":
                try:
                    event_type = value.decode("utf-8", errors="strict")
                except UnicodeError as exc:
                    raise McpError("mcp.sse_invalid", "MCP SSE event name is invalid") from exc
                if len(event_type) > 100:
                    raise McpError("mcp.frame_too_large", "MCP SSE event name is too large")
            elif field == b"data":
                data_size += len(value) + (1 if data_lines else 0)
                if data_size > limits.max_frame_bytes:
                    raise McpError("mcp.frame_too_large", "MCP SSE event is too large")
                data_lines.append(bytes(value))

    if buffer or data_lines:
        raise McpError("mcp.sse_incomplete", "MCP SSE stream ended with an incomplete event")
