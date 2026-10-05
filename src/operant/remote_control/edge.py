"""A separate, bounded Remote Device listener that cannot proxy Core management."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import ssl
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import Response
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException
from websockets.typing import Origin, Subprotocol

_PAIR_PATH = "/v1/remote-control/devices/pair"
_QUERY_PATH = "/v1/remote-control/session-query"
_GATEWAY_PATH = "/v1/remote-control/gateway"
_SUBPROTOCOL = "operant.remote.v1"


@dataclass(frozen=True)
class RemoteEdgeConfig:
    core_origin: str
    core_ca_file: Path
    allowed_origins: frozenset[str]
    max_bytes: int = 512 * 1024

    def __post_init__(self) -> None:
        parts = urlsplit(self.core_origin)
        try:
            loopback = ipaddress.ip_address(parts.hostname or "").is_loopback
            port = parts.port
        except ValueError as exc:
            raise ValueError("Core origin must be an explicit HTTPS loopback origin") from exc
        if (
            parts.scheme != "https"
            or not loopback
            or port is None
            or not 1 <= port <= 65535
            or parts.path not in {"", "/"}
            or parts.query
            or parts.fragment
            or parts.username is not None
            or parts.password is not None
        ):
            raise ValueError("Core origin must be an explicit HTTPS loopback origin")
        if not self.core_ca_file.is_absolute() or not self.core_ca_file.is_file():
            raise ValueError("Core CA must be an existing absolute file")
        if not 1 <= len(self.allowed_origins) <= 16:
            raise ValueError("Remote Edge requires 1-16 explicit HTTPS device origins")
        for origin in self.allowed_origins:
            parsed = urlsplit(origin)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.path
                or parsed.query
                or parsed.fragment
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError("Device Origin must be an exact HTTPS origin")
        if not 16 * 1024 <= self.max_bytes <= 512 * 1024:
            raise ValueError("Remote Edge byte limit is out of range")

    def ssl_context(self) -> ssl.SSLContext:
        return ssl.create_default_context(cafile=str(self.core_ca_file))


def create_remote_edge_app(
    config: RemoteEdgeConfig, *, transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    """Expose device pairing/results/WSS and a worker's read-only authorization check.

    The transport argument is solely for deterministic HTTP boundary tests. In
    normal operation every upstream connection verifies the configured Core CA.
    """
    context = config.ssl_context()
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None, redirect_slashes=False)
    origin = config.core_origin.rstrip("/")

    async def forward_request(request: Request, upstream_path: str) -> Response:
        if request.url.query:
            return Response(status_code=400)
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
            return Response(status_code=415)
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > config.max_bytes:
                return Response(status_code=413)
            body.extend(chunk)
        # Never forward cookies, local auth headers, or user-controlled routing headers.
        headers = {"Content-Type": "application/json"}
        key = request.headers.get("idempotency-key")
        if key is not None:
            if not 1 <= len(key) <= 200:
                return Response(status_code=400)
            headers["Idempotency-Key"] = key
        try:
            async with (
                httpx.AsyncClient(
                    verify=context,
                    trust_env=False,
                    follow_redirects=False,
                    timeout=30,
                    transport=transport,
                ) as client,
                client.stream(
                    "POST", f"{origin}{upstream_path}", headers=headers, content=bytes(body)
                ) as upstream,
            ):
                if 300 <= upstream.status_code < 400:
                    return Response(status_code=502)
                output = bytearray()
                async for chunk in upstream.aiter_bytes():
                    if len(output) + len(chunk) > config.max_bytes:
                        return Response(status_code=502)
                    output.extend(chunk)
                return Response(
                    bytes(output),
                    status_code=upstream.status_code,
                    media_type="application/json",
                    headers={"Cache-Control": "no-store"},
                )
        except (httpx.HTTPError, OSError):
            return Response(status_code=502)

    @app.post(_PAIR_PATH)
    async def pair(request: Request) -> Response:
        return await forward_request(request, _PAIR_PATH)

    @app.post(_QUERY_PATH)
    async def query(request: Request) -> Response:
        return await forward_request(request, _QUERY_PATH)

    @app.post("/v1/remote-targets/{target_id}/leases/verify")
    async def verify_target_job(target_id: str, request: Request) -> Response:
        if re.fullmatch(r"[A-Za-z0-9_-]{1,200}", target_id) is None:
            return Response(status_code=400)
        return await forward_request(request, f"/v1/remote-targets/{target_id}/leases/verify")

    @app.websocket(_GATEWAY_PATH)
    async def gateway(websocket: WebSocket) -> None:
        device_origin = websocket.headers.get("origin", "")
        bearer = websocket.headers.get("authorization", "")
        protocols = websocket.scope.get("subprotocols", [])
        if (
            websocket.scope.get("scheme") != "wss"
            or websocket.url.query
            or device_origin not in config.allowed_origins
            or _SUBPROTOCOL not in protocols
            or not bearer.startswith("Bearer ")
            or not 32 <= len(bearer.removeprefix("Bearer ")) <= 512
        ):
            await websocket.close(code=1008)
            return
        try:
            async with connect(
                f"{origin.replace('https://', 'wss://', 1)}{_GATEWAY_PATH}",
                ssl=context,
                origin=Origin(device_origin),
                additional_headers={"Authorization": bearer},
                subprotocols=[Subprotocol(_SUBPROTOCOL)],
                proxy=None,
                max_size=config.max_bytes,
                max_queue=16,
                open_timeout=10,
                close_timeout=5,
            ) as upstream:
                if upstream.subprotocol != _SUBPROTOCOL:
                    await websocket.close(code=1008)
                    return
                await websocket.accept(subprotocol=_SUBPROTOCOL)

                async def to_core() -> None:
                    while True:
                        message = await websocket.receive()
                        if message["type"] == "websocket.disconnect":
                            return
                        text = message.get("text")
                        if not isinstance(text, str):
                            await websocket.close(code=1003)
                            return
                        if len(text.encode("utf-8")) > config.max_bytes:
                            await websocket.close(code=1009)
                            return
                        await upstream.send(text)

                async def to_device() -> None:
                    async for frame in upstream:
                        if not isinstance(frame, str):
                            await websocket.close(code=1003)
                            return
                        await websocket.send_text(frame)

                tasks = {asyncio.create_task(to_core()), asyncio.create_task(to_device())}
                try:
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                finally:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except (WebSocketDisconnect, WebSocketException, httpx.HTTPError, OSError):
            pass
        finally:
            if websocket.application_state.name != "DISCONNECTED":
                await websocket.close(code=1001)

    return app
