from __future__ import annotations

import ipaddress
import json
import os
import re
import stat
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import uvicorn
from fastapi import HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from operant.api import create_app
from operant.auth import oauth_config_from_env
from operant.local_caller import DEVICE_AUTHENTICATED_ROUTES, LocalCallerAuthority
from operant.local_caller_middleware import LocalCallerProofMiddleware
from operant.remote_control.gateway import RemoteGatewayConfig
from operant.settings import load_local_env

_ENVIRONMENT_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_DEFAULT_WS_MAX_SIZE = 512 * 1024
_DESKTOP_MANAGEMENT_PREFIXES = (
    "/v1/setup",
    "/v1/remote-control",
    "/v1/local-control",
    "/v1/extensions",
    "/v1/writer-workspaces",
    "/v1/writer-conflicts",
    "/v1/merge-runs",
    "/internal/local-caller",
)
_DESKTOP_MANAGEMENT_PATH_PATTERNS = (
    r"/v1/graph/runs/[^/]+/(?:writer-workspaces|writer-artifacts|writer-conflicts|merge-runs)(?:/.*)?",
    r"/v1/workbench/(?:extensions/commands|threads/[^/]+/(?:extension-commands|skill-commands))(?:/.*)?",
)


class ServerConfigurationError(ValueError):
    pass


def read_desktop_caller_secret() -> bytes:
    """Read one bounded bootstrap message from the inherited private stdin pipe.

    The native parent must write 32 random bytes and close its end. No path,
    environment variable or model credential participates in this exchange.
    """
    try:
        stream = sys.stdin.buffer
        if not stat.S_ISFIFO(os.fstat(stream.fileno()).st_mode):
            raise ServerConfigurationError("desktop authentication requires a private stdin pipe")
        secret = stream.read(33)
        if len(secret) != 32:
            raise ServerConfigurationError("desktop bootstrap message has an invalid size")
        return secret
    except (AttributeError, OSError, ValueError) as exc:
        if isinstance(exc, ServerConfigurationError):
            raise
        raise ServerConfigurationError("desktop bootstrap pipe is unavailable") from exc


def _setup_origins_from_env(values: Mapping[str, str]) -> tuple[str, ...]:
    raw = values.get("OPERANT_SETUP_ALLOWED_ORIGINS_JSON", "")
    if not raw:
        return ()
    try:
        origins = json.loads(raw)
        if not isinstance(origins, list) or not 1 <= len(origins) <= 16:
            raise ValueError("expected a bounded list")
        for origin in origins:
            if not isinstance(origin, str):
                raise ValueError("invalid origin")
            parsed = urlsplit(origin)
            if (
                parsed.scheme != "http"
                or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
                or parsed.port is None
                or not 1 <= parsed.port <= 65535
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("expected a loopback HTTP origin")
        return tuple(origins)
    except (ValueError, TypeError) as exc:
        raise ServerConfigurationError(
            "setup origins must be an array of 1-16 explicit loopback HTTP origins"
        ) from exc


def _gateway_config_from_env(
    environment: Mapping[str, str] | None = None,
) -> RemoteGatewayConfig | None:
    values = os.environ if environment is None else environment
    token_ref = values.get("OPERANT_REMOTE_GATEWAY_TOKEN_REF", "")
    origins_json = values.get("OPERANT_REMOTE_GATEWAY_ALLOWED_ORIGINS_JSON", "")
    if not token_ref and not origins_json:
        return None
    if not token_ref or not origins_json:
        raise ServerConfigurationError(
            "remote gateway token reference and origins must be set together"
        )
    if _ENVIRONMENT_NAME.fullmatch(token_ref) is None:
        raise ServerConfigurationError("remote gateway token reference is invalid")
    token = values.get(token_ref)
    if token is None:
        raise ServerConfigurationError("remote gateway token reference is unavailable")
    try:
        origins = json.loads(origins_json)
    except json.JSONDecodeError as exc:
        raise ServerConfigurationError("remote gateway origins must be a JSON array") from exc
    if (
        not isinstance(origins, list)
        or not 1 <= len(origins) <= 16
        or not all(isinstance(origin, str) for origin in origins)
    ):
        raise ServerConfigurationError("remote gateway origins must contain 1-16 strings")
    try:
        max_frame_bytes = int(values.get("OPERANT_REMOTE_GATEWAY_MAX_FRAME_BYTES", "524288"))
        max_pending_frames = int(values.get("OPERANT_REMOTE_GATEWAY_MAX_PENDING_FRAMES", "16"))
    except ValueError as exc:
        raise ServerConfigurationError("remote gateway limits must be integers") from exc
    return RemoteGatewayConfig(
        token=token,
        allowed_origins=frozenset(origins),
        max_frame_bytes=max_frame_bytes,
        max_pending_frames=max_pending_frames,
    )


def _bind_address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    if host == "localhost":
        return ipaddress.ip_address("127.0.0.1")
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ServerConfigurationError(
            "server host must be localhost or an explicit IP address"
        ) from exc
    if address.is_unspecified or address.is_multicast or address.is_global:
        raise ServerConfigurationError(
            "public, wildcard, and multicast server hosts are not allowed"
        )
    if not (address.is_loopback or address.is_private):
        raise ServerConfigurationError("server host must be loopback or private")
    return address


def build_server_config(
    *,
    host: str,
    port: int,
    ssl_certfile: Path | None,
    ssl_keyfile: Path | None,
    desktop: bool = False,
    environment: Mapping[str, str] | None = None,
    local_caller_secret: bytes | None = None,
) -> uvicorn.Config:
    if not 1 <= port <= 65535:
        raise ServerConfigurationError("server port is out of range")
    address = _bind_address(host)
    if (ssl_certfile is None) != (ssl_keyfile is None):
        raise ServerConfigurationError("TLS certificate and key must be configured together")
    for path, label in ((ssl_certfile, "TLS certificate"), (ssl_keyfile, "TLS key")):
        if path is not None and (not path.is_absolute() or not path.is_file()):
            raise ServerConfigurationError(f"{label} must be an existing absolute file")

    values = os.environ if environment is None else environment
    gateway_config = _gateway_config_from_env(values)
    oauth_config = oauth_config_from_env(values)
    if desktop and not address.is_loopback:
        raise ServerConfigurationError("desktop bridge is restricted to loopback")
    if local_caller_secret is not None and not desktop:
        raise ServerConfigurationError("local caller authentication requires desktop mode")
    if not address.is_loopback and (ssl_certfile is None or oauth_config is None):
        raise ServerConfigurationError("private-network listening requires TLS and OAuth")
    if gateway_config is not None and ssl_certfile is None:
        raise ServerConfigurationError("remote gateway requires TLS/WSS")

    authority = None
    if local_caller_secret is not None:
        try:
            authority = LocalCallerAuthority(local_caller_secret)
        except ValueError as exc:
            raise ServerConfigurationError("desktop bootstrap message has an invalid size") from exc
    application = create_app(
        db_path=values.get("OPERANT_DB_PATH"),
        phase56_gateway_config=gateway_config,
        oauth_config=oauth_config,
        setup_allowed_origins=_setup_origins_from_env(values),
        phase56_local_authorizer=None if authority is None else authority.is_trusted,
    )
    if authority is not None:
        caller_authority = authority

        @application.get("/internal/local-caller/identity", include_in_schema=False)
        def local_core_identity(request: Request, response: Response) -> dict[str, str]:
            proof = caller_authority.core_identity_proof(request)
            if proof is None:
                raise HTTPException(status_code=403, detail="local caller authentication required")
            response.headers["Cache-Control"] = "no-store"
            return {"protocol": "local-caller.core.v1", "proof": proof}

        # Proof and body replay happen before the command middleware,
        # path lookup, receipts and Gateway effects. Device messages keep their
        # separate pairing-code/signature/session authentication.
        application.add_middleware(
            LocalCallerProofMiddleware,
            authority=authority,
            protected_prefixes=_DESKTOP_MANAGEMENT_PREFIXES,
            protected_path_patterns=_DESKTOP_MANAGEMENT_PATH_PATTERNS,
            public_exact_routes=DEVICE_AUTHENTICATED_ROUTES,
        )
    if desktop:
        # CORS handles only fixed-origin preflights before proof verification;
        # every business request still traverses the proof guard. Caller keys
        # and signing headers remain in the native HTTP bridge, not browser JS.
        application.add_middleware(
            CORSMiddleware,
            allow_origins=[
                "tauri://localhost",
                "http://tauri.localhost",
                "https://tauri.localhost",
            ],
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=[
                "Accept",
                "Content-Type",
                "Idempotency-Key",
                "Last-Event-ID",
                "X-Operant-Client-Version",
            ],
            allow_credentials=False,
            max_age=600,
        )
    return uvicorn.Config(
        application,
        host=host,
        port=port,
        ssl_certfile=None if ssl_certfile is None else str(ssl_certfile),
        ssl_keyfile=None if ssl_keyfile is None else str(ssl_keyfile),
        ws_max_size=(
            gateway_config.uvicorn_ws_max_size
            if gateway_config is not None
            else _DEFAULT_WS_MAX_SIZE
        ),
        proxy_headers=False,
        # OAuth callback query parameters contain one-use authorization codes.
        # Structured Core audits remain available without raw HTTP access logs.
        access_log=False,
        server_header=False,
        workers=1,
    )


def run_server(
    *,
    host: str,
    port: int,
    ssl_certfile: Path | None = None,
    ssl_keyfile: Path | None = None,
    desktop: bool = False,
    desktop_auth_stdio: bool = False,
) -> None:
    if desktop_auth_stdio and not desktop:
        raise ServerConfigurationError("private desktop bootstrap requires --desktop")
    local_caller_secret = read_desktop_caller_secret() if desktop_auth_stdio else None
    load_local_env()
    uvicorn.Server(
        build_server_config(
            host=host,
            port=port,
            ssl_certfile=ssl_certfile,
            ssl_keyfile=ssl_keyfile,
            desktop=desktop,
            local_caller_secret=local_caller_secret,
        )
    ).run()


def server_config_snapshot(config: uvicorn.Config) -> dict[str, Any]:
    """Return only non-secret fields for diagnostics and tests."""

    return {
        "host": config.host,
        "port": config.port,
        "ssl": config.is_ssl,
        "ws_max_size": config.ws_max_size,
        "proxy_headers": config.proxy_headers,
        "workers": config.workers,
    }
