from __future__ import annotations

import asyncio
import contextlib
import hmac
import ipaddress
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from operant.domain.remote_control import EncryptedRemoteCommand


class RemoteGatewayService(Protocol):
    def validate_gateway_binding(
        self,
        host_id: str,
        device_id: str,
        session_id: str,
        protocol_version: str,
    ) -> None: ...

    def submit_command(self, command: EncryptedRemoteCommand) -> Any: ...

    def list_events(
        self,
        host_id: str,
        *,
        session_id: str | None,
        after_cursor: int,
        limit: int,
        device_id: str | None = None,
    ) -> Sequence[Mapping[str, Any]]: ...


class RemoteGatewayConnectionRegistry(Protocol):
    def open(
        self,
        *,
        remote_session_id: str,
        device_id: str,
        event_cursor: int,
    ) -> str: ...

    def heartbeat(self, connection_id: str, *, event_cursor: int | None = None) -> None: ...

    def close(self, connection_id: str, *, error_code: str | None = None) -> None: ...


@dataclass(frozen=True)
class RemoteGatewayConfig:
    """Fail-closed transport policy for the direct Remote Control gateway."""

    token: str
    allowed_origins: frozenset[str]
    allow_insecure_loopback: bool = False
    trusted_proxy_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()
    max_frame_bytes: int = 512 * 1024
    max_pending_frames: int = 16
    event_page_limit: int = 200

    @property
    def uvicorn_ws_max_size(self) -> int:
        """Transport limit that must be passed to Uvicorn's ``ws_max_size``."""

        return self.max_frame_bytes

    def __post_init__(self) -> None:
        if not 32 <= len(self.token) <= 512 or any(character.isspace() for character in self.token):
            raise ValueError("remote gateway token must be a bounded non-whitespace secret")
        if not self.allowed_origins:
            raise ValueError("remote gateway requires an explicit Origin allowlist")
        for origin in self.allowed_origins:
            parsed = urlsplit(origin)
            if (
                parsed.scheme != "https"
                or parsed.hostname is None
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or parsed.path
            ):
                raise ValueError("remote gateway Origins must be exact HTTPS origins")
        if not 1024 <= self.max_frame_bytes <= 4 * 1024 * 1024:
            raise ValueError("remote gateway frame limit is out of bounds")
        if not 1 <= self.max_pending_frames <= 128:
            raise ValueError("remote gateway backpressure limit is out of bounds")
        if not 1 <= self.event_page_limit <= 500:
            raise ValueError("remote gateway event page limit is out of bounds")


def parse_proxy_networks(
    values: Sequence[str],
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    return tuple(ipaddress.ip_network(value, strict=True) for value in values)


def _is_loopback(host: str | None) -> bool:
    if host in {"localhost", "testclient"}:
        return True
    try:
        return host is not None and ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _trusted_proxy(host: str | None, config: RemoteGatewayConfig) -> bool:
    try:
        address = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    return any(address in network for network in config.trusted_proxy_networks)


def _secure_transport(websocket: WebSocket, config: RemoteGatewayConfig) -> bool:
    if websocket.url.scheme == "wss":
        return True
    client_host = None if websocket.client is None else websocket.client.host
    if config.allow_insecure_loopback and _is_loopback(client_host):
        return True
    if not _trusted_proxy(client_host, config):
        return False
    forwarded = websocket.headers.get("x-forwarded-proto", "")
    # A trusted proxy must supply exactly one canonical value. Chained or
    # ambiguous headers fail closed instead of guessing which hop is trusted.
    return forwarded.lower() == "https"


def _authorized(websocket: WebSocket, config: RemoteGatewayConfig) -> bool:
    origin = websocket.headers.get("origin")
    if origin not in config.allowed_origins:
        return False
    supplied = websocket.headers.get("authorization", "")
    if not supplied.startswith("Bearer "):
        return False
    return hmac.compare_digest(supplied.removeprefix("Bearer "), config.token)


def _supports_protocol(websocket: WebSocket) -> bool:
    offered = websocket.headers.get("sec-websocket-protocol", "")
    return "operant.remote.v1" in {item.strip() for item in offered.split(",") if item.strip()}


async def _send_error(websocket: WebSocket, code: str) -> None:
    await websocket.send_json({"type": "error", "code": code})


@dataclass(frozen=True)
class _FrameViolation:
    code: str
    close_code: int


async def _receive_frames(
    websocket: WebSocket,
    queue: asyncio.Queue[str | _FrameViolation | None],
    config: RemoteGatewayConfig,
) -> None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            await queue.put(None)
            return
        if message.get("bytes") is not None:
            await queue.put(_FrameViolation("gateway.binary_frames_not_supported", 1003))
            return
        raw = message.get("text")
        if raw is None:
            continue
        if len(raw.encode("utf-8")) > config.max_frame_bytes:
            await queue.put(_FrameViolation("gateway.frame_too_large", 1009))
            return
        if queue.qsize() >= config.max_pending_frames:
            await queue.put(_FrameViolation("gateway.backpressure", 1013))
            return
        queue.put_nowait(raw)


def install_remote_gateway(
    app: FastAPI,
    service: RemoteGatewayService,
    *,
    config: RemoteGatewayConfig,
    on_connect: Callable[[str], Awaitable[None]] | None = None,
    connection_registry: RemoteGatewayConnectionRegistry | None = None,
) -> None:
    """Install the direct WSS endpoint without changing Relay semantics.

    The endpoint accepts encrypted, signed command envelopes only. The
    returned ``host_ack`` is the durable Core receipt produced by
    ``RemoteControlService``; no transport-level delivery acknowledgement is
    represented as a Host/Core acknowledgement.
    """

    # Uvicorn enforces this before an application frame exists. Launchers must
    # pass this value as ``ws_max_size``; exposing it on app.state prevents a
    # second, drifting transport constant.
    app.state.remote_gateway_ws_max_size = config.uvicorn_ws_max_size

    @app.websocket("/v1/remote-control/gateway", name="connectRemoteGateway")
    async def remote_gateway(websocket: WebSocket) -> None:
        if not _secure_transport(websocket, config):
            await websocket.close(code=4403, reason="secure transport required")
            return
        if not _authorized(websocket, config):
            await websocket.close(code=4401, reason="gateway authentication failed")
            return
        if not _supports_protocol(websocket):
            await websocket.close(code=4406, reason="gateway subprotocol required")
            return
        await websocket.accept(subprotocol="operant.remote.v1")
        queue: asyncio.Queue[str | _FrameViolation | None] = asyncio.Queue(
            maxsize=config.max_pending_frames + 1
        )
        receiver = asyncio.create_task(_receive_frames(websocket, queue, config))
        binding: tuple[str, str, str, str] | None = None
        connection_id: str | None = None
        try:
            while True:
                incoming = await queue.get()
                if incoming is None:
                    return
                if isinstance(incoming, _FrameViolation):
                    await _send_error(websocket, incoming.code)
                    await websocket.close(code=incoming.close_code)
                    return
                raw = incoming
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    await _send_error(websocket, "gateway.invalid_json")
                    continue
                if not isinstance(frame, dict):
                    await _send_error(websocket, "gateway.invalid_frame")
                    continue
                frame_type = frame.get("type")
                if binding is None:
                    if frame_type != "hello":
                        await _send_error(websocket, "gateway.hello_required")
                        continue
                    opened = await _handle_hello(
                        websocket,
                        service,
                        frame,
                        connection_registry=connection_registry,
                    )
                    if opened is not None:
                        binding, connection_id = opened
                        if on_connect is not None:
                            await on_connect(str(websocket.client))
                    continue
                if frame_type == "hello":
                    await _send_error(websocket, "gateway.already_bound")
                    continue
                if frame_type == "ping":
                    if connection_registry is not None and connection_id is not None:
                        await asyncio.to_thread(connection_registry.heartbeat, connection_id)
                    await websocket.send_json({"type": "pong"})
                    continue
                if frame_type == "cursor_sync":
                    await _handle_cursor_sync(
                        websocket,
                        service,
                        frame,
                        config,
                        binding,
                        connection_id=connection_id,
                        connection_registry=connection_registry,
                    )
                    continue
                if frame_type != "command":
                    await _send_error(websocket, "gateway.unknown_frame_type")
                    continue
                try:
                    command = EncryptedRemoteCommand.model_validate(frame.get("command"))
                    if (
                        command.host_id,
                        command.device_id,
                        command.remote_session_id,
                        command.protocol_version,
                    ) != binding:
                        await _send_error(websocket, "gateway.command_binding_invalid")
                        continue
                    receipt = await asyncio.to_thread(service.submit_command, command)
                except ValidationError:
                    await _send_error(websocket, "gateway.invalid_command")
                    continue
                except Exception:
                    # RemoteControlService has already persisted any
                    # accepted/unknown outcome. Do not retry the command.
                    await _send_error(websocket, "gateway.command_rejected")
                    continue
                await websocket.send_json(
                    {"type": "host_ack", "receipt": receipt.model_dump(mode="json")}
                )
                if connection_registry is not None and connection_id is not None:
                    await asyncio.to_thread(connection_registry.heartbeat, connection_id)
        except WebSocketDisconnect:
            return
        finally:
            if connection_registry is not None and connection_id is not None:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(connection_registry.close, connection_id)
            receiver.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await receiver


async def _handle_hello(
    websocket: WebSocket,
    service: RemoteGatewayService,
    frame: Mapping[str, Any],
    *,
    connection_registry: RemoteGatewayConnectionRegistry | None,
) -> tuple[tuple[str, str, str, str], str | None] | None:
    host_id = frame.get("host_id")
    device_id = frame.get("device_id")
    session_id = frame.get("remote_session_id")
    protocol_version = frame.get("protocol_version")
    if not all(
        isinstance(value, str) and value and len(value) <= 200
        for value in (host_id, device_id, session_id, protocol_version)
    ):
        await _send_error(websocket, "gateway.invalid_hello")
        return None
    assert isinstance(host_id, str)
    assert isinstance(device_id, str)
    assert isinstance(session_id, str)
    assert isinstance(protocol_version, str)
    try:
        await asyncio.to_thread(
            service.validate_gateway_binding,
            host_id,
            device_id,
            session_id,
            protocol_version,
        )
    except Exception:
        await _send_error(websocket, "gateway.hello_rejected")
        return None
    connection_id = None
    if connection_registry is not None:
        try:
            connection_id = await asyncio.to_thread(
                connection_registry.open,
                remote_session_id=session_id,
                device_id=device_id,
                event_cursor=0,
            )
        except Exception:
            await _send_error(websocket, "gateway.connection_conflict")
            return None
    await websocket.send_json(
        {
            "type": "hello_ack",
            "host_id": host_id,
            "device_id": device_id,
            "remote_session_id": session_id,
            "protocol_version": protocol_version,
            "connection_id": connection_id,
        }
    )
    return (host_id, device_id, session_id, protocol_version), connection_id


async def _handle_cursor_sync(
    websocket: WebSocket,
    service: RemoteGatewayService,
    frame: Mapping[str, Any],
    config: RemoteGatewayConfig,
    binding: tuple[str, str, str, str],
    *,
    connection_id: str | None,
    connection_registry: RemoteGatewayConnectionRegistry | None,
) -> None:
    host_id, device_id, session_id, protocol_version = binding
    after_cursor = frame.get("after_cursor", 0)
    if (
        set(frame).difference({"type", "after_cursor"})
        or not isinstance(after_cursor, int)
        or isinstance(after_cursor, bool)
        or not 0 <= after_cursor <= 2**63 - 1
    ):
        await _send_error(websocket, "gateway.invalid_cursor")
        return
    try:
        await asyncio.to_thread(
            service.validate_gateway_binding,
            host_id,
            device_id,
            session_id,
            protocol_version,
        )
        events = await asyncio.to_thread(
            service.list_events,
            host_id,
            session_id=session_id,
            after_cursor=after_cursor,
            limit=config.event_page_limit,
            device_id=device_id,
        )
    except Exception:
        await _send_error(websocket, "gateway.cursor_sync_failed")
        return
    items = [dict(item) for item in events]
    next_cursor = items[-1].get("cursor", after_cursor) if items else after_cursor
    if connection_registry is not None and connection_id is not None:
        await asyncio.to_thread(
            connection_registry.heartbeat,
            connection_id,
            event_cursor=next_cursor,
        )
    await websocket.send_json({"type": "events", "items": items, "next_cursor": next_cursor})
