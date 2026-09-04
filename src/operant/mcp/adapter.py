from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import shutil
import stat
import tempfile
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager, suppress
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from operant.domain.models import CommandExecutionPolicy, CommandRunnerType
from operant.protocol import canonical_action_hash, redact_public_data, redact_public_text
from operant.tools.execution import (
    SNAPSHOT_EXCLUDED_NAMES,
    DockerCommandRunner,
    is_protected_workspace_name,
)


class McpError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.detail = {} if detail is None else dict(detail)


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
    max_snapshot_entries: int = Field(default=20_000, ge=1, le=100_000)
    max_snapshot_bytes: int = Field(default=512_000_000, ge=1_024, le=2_000_000_000)


class McpStdioConfig(BaseModel):
    """Preconfigured executable argv. Shell parsing is deliberately absent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    argv: tuple[str, ...] = Field(min_length=1, max_length=64)
    workspace: str = Field(min_length=1, max_length=4096)
    cwd: str = Field(default=".", min_length=1, max_length=4096)
    docker_image: str = Field(min_length=1, max_length=300)
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
        if value:
            raise ValueError("stdio environment secrets are unsupported for long-lived servers")
        return value

    @field_validator("docker_image")
    @classmethod
    def validate_docker_image(cls, value: str) -> str:
        if re.fullmatch(r"(?:[A-Za-z0-9][A-Za-z0-9._:/-]*@)?sha256:[0-9a-f]{64}", value) is None:
            raise ValueError("stdio Docker image must be pinned by sha256 digest")
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
    approval_id: str | None = Field(default=None, max_length=300)

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

    async def reserve_outcome(
        self,
        *,
        action_hash: str,
        server_id: str,
        tool_name: str,
        schema_sha256: str,
        arguments: Mapping[str, Any],
    ) -> tuple[str, Any | None]: ...

    async def mark_sent(self, action_hash: str) -> None: ...

    async def complete_outcome(self, action_hash: str, result: Any) -> None: ...

    async def mark_outcome_unknown(self, action_hash: str, error_code: str) -> None: ...


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
        self._temporary_root: Path | None = None
        self._cidfile: Path | None = None

    @property
    def safe_stderr(self) -> str:
        return redact_public_text(self._stderr, max_chars=self.limits.max_stderr_chars)

    async def start(self) -> None:
        if self._process is not None:
            return
        workspace = Path(self.config.workspace).resolve(strict=True)
        if (
            not workspace.is_dir()
            or workspace == Path(workspace.anchor)
            or workspace == Path.home().resolve()
            or "," in str(workspace)
        ):
            raise McpError("mcp.stdio_workspace_invalid", "MCP stdio workspace is unsafe")
        cwd = (workspace / self.config.cwd).resolve(strict=True)
        if not cwd.is_dir() or (cwd != workspace and workspace not in cwd.parents):
            raise McpError("mcp.stdio_cwd_invalid", "MCP stdio cwd escapes its workspace")
        temporary_root = Path(tempfile.mkdtemp(prefix="operant-mcp-sandbox-"))
        snapshot = temporary_root / "workspace"
        cidfile = temporary_root / "container-id"
        self._temporary_root = temporary_root
        self._cidfile = cidfile
        try:
            await asyncio.to_thread(
                _copy_workspace_snapshot_safely,
                workspace,
                snapshot,
                self.limits,
            )
            policy = CommandExecutionPolicy(
                runner=CommandRunnerType.DOCKER,
                docker_image=self.config.docker_image,
                cpu_limit=1.0,
                memory_limit_mb=512,
                pids_limit=128,
            )
            docker_argv = DockerCommandRunner.build_argv(
                argv=self.config.argv,
                snapshot=snapshot,
                relative_cwd=cwd.relative_to(workspace),
                cidfile=cidfile,
                workspace_write=False,
                policy=policy,
            )
            # The daemon must not pull mutable or unapproved code while handling
            # a PROCESS_EXEC_NO_NETWORK action. The image is digest-pinned above.
            docker_argv[2:2] = ["--pull", "never", "--interactive"]
            self._process = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *docker_argv,
                    cwd=temporary_root,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=self.limits.max_frame_bytes + 1,
                ),
                timeout=self.limits.lifecycle_timeout_seconds,
            )
        except asyncio.CancelledError:
            await self._cleanup_sandbox()
            raise
        except McpError:
            await self._cleanup_sandbox()
            raise
        except Exception as exc:
            await self._cleanup_sandbox()
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
            await self._cleanup_sandbox()
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
        await self._cleanup_sandbox()

    async def _cleanup_sandbox(self) -> None:
        if self._cidfile is not None:
            await DockerCommandRunner._remove_container(self._cidfile)
        self._cidfile = None
        if self._temporary_root is not None:
            await asyncio.to_thread(shutil.rmtree, self._temporary_root, ignore_errors=True)
        self._temporary_root = None

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


def _copy_workspace_snapshot_safely(
    workspace: Path,
    destination: Path,
    limits: McpLimits,
) -> None:
    """Copy a bounded stdio snapshot without following source links.

    The source is untrusted and may change while it is copied. Directory file
    descriptors keep traversal anchored to the opened tree; O_NOFOLLOW plus
    before/after identity checks turn replacement races into a closed failure.
    """

    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise McpError(
            "mcp.stdio_snapshot_unsupported",
            "MCP stdio safe snapshotting is unavailable on this platform",
        )
    try:
        expected_root = os.stat(workspace, follow_symlinks=False)
        root_fd = os.open(
            workspace,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
    except OSError as exc:
        raise McpError(
            "mcp.stdio_snapshot_unreadable",
            "MCP workspace snapshot changed before it could be opened",
        ) from exc
    try:
        opened_root = os.fstat(root_fd)
        if not _same_snapshot_identity(expected_root, opened_root) or not stat.S_ISDIR(
            opened_root.st_mode
        ):
            raise McpError(
                "mcp.stdio_snapshot_changed",
                "MCP workspace snapshot changed before it could be copied",
            )
        destination.mkdir(mode=0o700)
        budget = {"entries": 0, "bytes": 0}
        _copy_snapshot_directory(root_fd, destination, limits, budget)
        final_root = os.fstat(root_fd)
        if not _same_snapshot_version(opened_root, final_root):
            raise McpError(
                "mcp.stdio_snapshot_changed",
                "MCP workspace snapshot changed while it was copied",
            )
    finally:
        os.close(root_fd)


def _copy_snapshot_directory(
    source_fd: int,
    destination: Path,
    limits: McpLimits,
    budget: dict[str, int],
) -> None:
    try:
        with os.scandir(source_fd) as iterator:
            names = sorted(entry.name for entry in iterator)
    except OSError as exc:
        raise McpError(
            "mcp.stdio_snapshot_unreadable",
            "MCP workspace snapshot could not be enumerated",
        ) from exc
    for name in names:
        if name.lower() in SNAPSHOT_EXCLUDED_NAMES or is_protected_workspace_name(name):
            continue
        budget["entries"] += 1
        if budget["entries"] > limits.max_snapshot_entries:
            raise McpError("mcp.stdio_snapshot_limit", "MCP workspace snapshot is too large")
        try:
            before = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        except OSError as exc:
            raise McpError(
                "mcp.stdio_snapshot_unreadable",
                "MCP workspace snapshot changed while it was inspected",
            ) from exc
        target = destination / name
        if stat.S_ISLNK(before.st_mode):
            raise McpError(
                "mcp.stdio_snapshot_unsafe",
                "MCP workspace snapshot contains a symbolic link",
            )
        if stat.S_ISDIR(before.st_mode):
            _copy_snapshot_subdirectory(source_fd, name, before, target, limits, budget)
            continue
        if stat.S_ISREG(before.st_mode):
            _copy_snapshot_file(source_fd, name, before, target, limits, budget)
            continue
        raise McpError(
            "mcp.stdio_snapshot_unsafe",
            "MCP workspace snapshot contains a non-regular file",
        )


def _copy_snapshot_subdirectory(
    parent_fd: int,
    name: str,
    expected: os.stat_result,
    destination: Path,
    limits: McpLimits,
    budget: dict[str, int],
) -> None:
    try:
        child_fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise McpError(
            "mcp.stdio_snapshot_unreadable",
            "MCP workspace directory changed while it was opened",
        ) from exc
    try:
        opened = os.fstat(child_fd)
        if not _same_snapshot_identity(expected, opened) or not stat.S_ISDIR(opened.st_mode):
            raise McpError(
                "mcp.stdio_snapshot_changed",
                "MCP workspace directory changed while it was opened",
            )
        destination.mkdir(mode=(stat.S_IMODE(opened.st_mode) & 0o777) or 0o700)
        _copy_snapshot_directory(child_fd, destination, limits, budget)
        if not _same_snapshot_version(opened, os.fstat(child_fd)):
            raise McpError(
                "mcp.stdio_snapshot_changed",
                "MCP workspace directory changed while it was copied",
            )
    finally:
        os.close(child_fd)


def _copy_snapshot_file(
    parent_fd: int,
    name: str,
    expected: os.stat_result,
    destination: Path,
    limits: McpLimits,
    budget: dict[str, int],
) -> None:
    try:
        source_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as exc:
        raise McpError(
            "mcp.stdio_snapshot_unreadable",
            "MCP workspace file changed while it was opened",
        ) from exc
    destination_fd: int | None = None
    try:
        opened = os.fstat(source_fd)
        if not _same_snapshot_identity(expected, opened) or not stat.S_ISREG(opened.st_mode):
            raise McpError(
                "mcp.stdio_snapshot_changed",
                "MCP workspace file changed while it was opened",
            )
        if budget["bytes"] + opened.st_size > limits.max_snapshot_bytes:
            raise McpError("mcp.stdio_snapshot_limit", "MCP workspace snapshot is too large")
        destination_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            (stat.S_IMODE(opened.st_mode) & 0o777) or 0o600,
        )
        copied = 0
        while True:
            chunk = os.read(source_fd, 64 * 1024)
            if not chunk:
                break
            copied += len(chunk)
            if budget["bytes"] + copied > limits.max_snapshot_bytes:
                raise McpError("mcp.stdio_snapshot_limit", "MCP workspace snapshot is too large")
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                if written <= 0:
                    raise McpError(
                        "mcp.stdio_snapshot_unreadable",
                        "MCP workspace snapshot could not be written",
                    )
                view = view[written:]
        final = os.fstat(source_fd)
        if copied != opened.st_size or not _same_snapshot_version(opened, final):
            raise McpError(
                "mcp.stdio_snapshot_changed",
                "MCP workspace file changed while it was copied",
            )
        budget["bytes"] += copied
        os.fchmod(destination_fd, stat.S_IMODE(opened.st_mode) & 0o777)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    finally:
        if destination_fd is not None:
            os.close(destination_fd)
        os.close(source_fd)


def _same_snapshot_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _same_snapshot_version(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        _same_snapshot_identity(left, right)
        and left.st_mode == right.st_mode
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
    )


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
        clear = getattr(self._resolver, "clear", None)
        if clear is not None:
            clear()

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
        redact_values: tuple[str, ...] = (),
    ) -> None:
        self.server_id = server_id
        self.target_ref = target_ref
        self.transport = transport
        self.action_gateway = action_gateway
        self.limits = limits or McpLimits()
        self._redact_values = tuple(value for value in redact_values if value)
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
        receipt_status, replay = await self.action_gateway.reserve_outcome(
            action_hash=action_hash,
            server_id=self.server_id,
            tool_name=name,
            schema_sha256=tool.schema_sha256,
            arguments=dict(arguments),
        )
        if receipt_status == "completed":
            return replay
        if receipt_status in {"sent", "outcome_unknown"}:
            raise McpError(
                "mcp.outcome_unknown",
                "MCP action may already have executed and requires manual reconciliation",
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
            raise McpError(
                code,
                "MCP tool call is not authorized",
                detail={
                    "approval_id": result.approval_id,
                    "action_hash": action_hash,
                    "reason_code": result.reason_code,
                },
            )
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
        try:
            await self.action_gateway.mark_sent(action_hash)
        except BaseException as exc:
            raise McpError(
                "mcp.outcome_unknown",
                "another execution owns this MCP action receipt",
            ) from exc
        started = time.monotonic()
        try:
            value = await asyncio.wait_for(
                self.transport.request("tools/call", {"name": name, "arguments": dict(arguments)}),
                timeout=self.limits.request_timeout_seconds,
            )
            _validate_bounded_json(value, self.limits)
        except BaseException as exc:
            with suppress(Exception):
                await self.action_gateway.mark_outcome_unknown(
                    action_hash,
                    exc.code if isinstance(exc, McpError) else type(exc).__name__,
                )
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
        safe_value = redact_public_data(_redact_exact_values(value, self._redact_values))
        try:
            await self.action_gateway.complete_outcome(action_hash, safe_value)
        except BaseException as exc:
            with suppress(Exception):
                await self.action_gateway.mark_outcome_unknown(
                    action_hash,
                    "mcp.receipt_persist_failed",
                )
            await self.action_gateway.record_audit(
                McpAuditFact(
                    event_type="mcp.tool_failed",
                    server_id=self.server_id,
                    tool_name=name,
                    action_hash=action_hash,
                    policy_version=lease.policy_version,
                    reason_code="mcp.receipt_persist_failed",
                    duration_ms=int((time.monotonic() - started) * 1_000),
                )
            )
            raise McpError(
                "mcp.outcome_unknown",
                "MCP completed but its durable outcome could not be recorded",
            ) from exc
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
            _validate_input_schema(schema, root=True)
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


_SCHEMA_TYPES = frozenset({"array", "boolean", "integer", "null", "number", "object", "string"})
_SCHEMA_ANNOTATIONS = frozenset(
    {
        "$id",
        "$schema",
        "default",
        "deprecated",
        "description",
        "examples",
        "readOnly",
        "title",
        "writeOnly",
    }
)
_SCHEMA_ASSERTIONS = frozenset(
    {
        "additionalProperties",
        "allOf",
        "anyOf",
        "const",
        "enum",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "items",
        "maxItems",
        "maxLength",
        "maxProperties",
        "maximum",
        "minItems",
        "minLength",
        "minProperties",
        "minimum",
        "multipleOf",
        "not",
        "oneOf",
        "properties",
        "required",
        "type",
        "uniqueItems",
    }
)


def _validate_input_schema(schema: Any, *, root: bool = False) -> None:
    """Accept a bounded JSON Schema subset and reject every unknown assertion."""

    if isinstance(schema, bool):
        if root:
            raise McpError("mcp.schema_unsupported", "MCP input schema root must be an object")
        return
    if not isinstance(schema, dict):
        raise McpError("mcp.schema_invalid", "MCP input schema entry is invalid")
    unsupported = set(schema).difference(_SCHEMA_ANNOTATIONS | _SCHEMA_ASSERTIONS)
    if unsupported:
        raise McpError(
            "mcp.schema_unsupported",
            "MCP input schema contains an unsupported keyword",
        )

    expected = schema.get("type")
    declared_types: tuple[str, ...] = ()
    if "type" in schema:
        if isinstance(expected, str):
            declared_types = (expected,)
        elif (
            isinstance(expected, list)
            and expected
            and all(isinstance(item, str) for item in expected)
            and len(set(expected)) == len(expected)
        ):
            declared_types = tuple(expected)
        else:
            raise McpError("mcp.schema_invalid", "MCP input schema type is invalid")
        if any(item not in _SCHEMA_TYPES for item in declared_types):
            raise McpError("mcp.schema_unsupported", "MCP input schema type is unsupported")
    if root and declared_types and "object" not in declared_types:
        raise McpError("mcp.schema_unsupported", "MCP input schema root must accept an object")

    properties = schema.get("properties")
    if "properties" in schema:
        if not isinstance(properties, dict):
            raise McpError("mcp.schema_invalid", "MCP input schema properties are invalid")
        for key, child in properties.items():
            if not isinstance(key, str):
                raise McpError("mcp.schema_invalid", "MCP input schema property name is invalid")
            _validate_input_schema(child)

    required = schema.get("required")
    if "required" in schema and (
        not isinstance(required, list)
        or any(not isinstance(item, str) for item in required)
        or len(set(required)) != len(required)
    ):
        raise McpError("mcp.schema_invalid", "MCP input schema required list is invalid")

    additional = schema.get("additionalProperties")
    if "additionalProperties" in schema:
        if not isinstance(additional, bool) and not isinstance(additional, dict):
            raise McpError("mcp.schema_invalid", "MCP input schema additionalProperties is invalid")
        _validate_input_schema(additional)

    items = schema.get("items")
    if "items" in schema:
        _validate_input_schema(items)

    for keyword in ("allOf", "anyOf", "oneOf"):
        if keyword not in schema:
            continue
        branches = schema[keyword]
        if not isinstance(branches, list) or not branches:
            raise McpError("mcp.schema_invalid", "MCP input schema branches are invalid")
        for branch in branches:
            _validate_input_schema(branch)
    if "not" in schema:
        _validate_input_schema(schema["not"])

    enum = schema.get("enum")
    if "enum" in schema and (
        not isinstance(enum, list)
        or not enum
        or len({_json_equivalence_key(item) for item in enum}) != len(enum)
    ):
        raise McpError("mcp.schema_invalid", "MCP input schema enum is invalid")

    for minimum, maximum in (
        ("minLength", "maxLength"),
        ("minItems", "maxItems"),
        ("minProperties", "maxProperties"),
    ):
        lower = schema.get(minimum)
        upper = schema.get(maximum)
        if minimum in schema and (
            isinstance(lower, bool) or not isinstance(lower, int) or lower < 0
        ):
            raise McpError("mcp.schema_invalid", "MCP input schema size bound is invalid")
        if maximum in schema and (
            isinstance(upper, bool) or not isinstance(upper, int) or upper < 0
        ):
            raise McpError("mcp.schema_invalid", "MCP input schema size bound is invalid")
        if minimum in schema and maximum in schema:
            assert isinstance(lower, int) and not isinstance(lower, bool)
            assert isinstance(upper, int) and not isinstance(upper, bool)
            if lower > upper:
                raise McpError("mcp.schema_invalid", "MCP input schema size bounds conflict")

    for keyword in (
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
    ):
        if keyword not in schema:
            continue
        bound = schema[keyword]
        if (
            isinstance(bound, bool)
            or not isinstance(bound, (int, float))
            or not _is_finite_number(bound)
            or (keyword == "multipleOf" and bound <= 0)
        ):
            raise McpError("mcp.schema_invalid", "MCP input schema numeric bound is invalid")

    for keyword in ("uniqueItems", "deprecated", "readOnly", "writeOnly"):
        if keyword in schema and not isinstance(schema[keyword], bool):
            raise McpError("mcp.schema_invalid", "MCP input schema boolean option is invalid")
    for keyword in ("$id", "$schema", "description", "title"):
        if keyword in schema and not isinstance(schema[keyword], str):
            raise McpError("mcp.schema_invalid", "MCP input schema annotation is invalid")
    if "examples" in schema and not isinstance(schema["examples"], list):
        raise McpError("mcp.schema_invalid", "MCP input schema examples are invalid")


def _validate_arguments(schema: Mapping[str, Any], arguments: Mapping[str, Any]) -> None:
    _validate_value_against_schema(schema, dict(arguments))


def _validate_value_against_schema(schema: Any, value: Any) -> None:
    if schema is True:
        return
    if schema is False:
        _invalid_arguments()
    assert isinstance(schema, dict)

    expected = schema.get("type")
    if expected is not None:
        expected_types = (expected,) if isinstance(expected, str) else tuple(expected)
        if not any(_matches_json_type(value, item) for item in expected_types):
            _invalid_arguments()
    if "const" in schema and not _json_values_equal(value, schema["const"]):
        _invalid_arguments()
    if "enum" in schema and not any(_json_values_equal(value, item) for item in schema["enum"]):
        _invalid_arguments()

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if set(required).difference(value):
            _invalid_arguments()
        if len(value) < schema.get("minProperties", 0):
            _invalid_arguments()
        maximum = schema.get("maxProperties")
        if maximum is not None and len(value) > maximum:
            _invalid_arguments()
        additional = schema.get("additionalProperties", True)
        for key, child in value.items():
            property_schema = properties.get(key)
            if property_schema is not None:
                _validate_value_against_schema(property_schema, child)
            elif additional is False:
                _invalid_arguments()
            elif isinstance(additional, (dict, bool)):
                _validate_value_against_schema(additional, child)

    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            _invalid_arguments()
        maximum = schema.get("maxItems")
        if maximum is not None and len(value) > maximum:
            _invalid_arguments()
        if schema.get("uniqueItems") and len(
            {_json_equivalence_key(item) for item in value}
        ) != len(value):
            _invalid_arguments()
        item_schema = schema.get("items")
        if item_schema is not None:
            for item in value:
                _validate_value_against_schema(item_schema, item)

    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            _invalid_arguments()
        maximum = schema.get("maxLength")
        if maximum is not None and len(value) > maximum:
            _invalid_arguments()

    if _is_json_number(value):
        if "minimum" in schema and value < schema["minimum"]:
            _invalid_arguments()
        if "maximum" in schema and value > schema["maximum"]:
            _invalid_arguments()
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            _invalid_arguments()
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            _invalid_arguments()
        if "multipleOf" in schema:
            try:
                if Decimal(str(value)) % Decimal(str(schema["multipleOf"])) != 0:
                    _invalid_arguments()
            except InvalidOperation:
                _invalid_arguments()

    for branch in schema.get("allOf", []):
        _validate_value_against_schema(branch, value)
    if "anyOf" in schema and not any(_schema_matches(branch, value) for branch in schema["anyOf"]):
        _invalid_arguments()
    if "oneOf" in schema and sum(_schema_matches(branch, value) for branch in schema["oneOf"]) != 1:
        _invalid_arguments()
    if "not" in schema and _schema_matches(schema["not"], value):
        _invalid_arguments()


def _schema_matches(schema: Any, value: Any) -> bool:
    try:
        _validate_value_against_schema(schema, value)
    except McpError as exc:
        if exc.code == "mcp.arguments_invalid":
            return False
        raise
    return True


def _matches_json_type(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return _is_json_number(value)
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return False


def _is_json_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_finite_number(value: int | float) -> bool:
    return (
        not isinstance(value, float)
        or value not in (float("inf"), float("-inf"))
        and value == value
    )


def _json_values_equal(left: Any, right: Any) -> bool:
    return bool(_json_equivalence_key(left) == _json_equivalence_key(right))


def _json_equivalence_key(value: Any) -> Any:
    if value is None:
        return ("null",)
    if isinstance(value, bool):
        return ("boolean", value)
    if _is_json_number(value):
        return ("number", Decimal(str(value)).normalize())
    if isinstance(value, str):
        return ("string", value)
    if isinstance(value, list):
        return ("array", tuple(_json_equivalence_key(item) for item in value))
    if isinstance(value, dict):
        return (
            "object",
            tuple(sorted((key, _json_equivalence_key(item)) for key, item in value.items())),
        )
    raise McpError("mcp.schema_invalid", "MCP input schema contains a non-JSON value")


def _invalid_arguments() -> None:
    raise McpError("mcp.arguments_invalid", "MCP tool arguments do not match the input schema")


def _redact_exact_values(value: Any, secrets: tuple[str, ...]) -> Any:
    if not secrets:
        return value
    if isinstance(value, str):
        redacted = value
        for secret in secrets:
            redacted = redacted.replace(secret, "[REDACTED]")
        return redacted
    if isinstance(value, list):
        return [_redact_exact_values(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: _redact_exact_values(item, secrets) for key, item in value.items()}
    return value


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
