from __future__ import annotations

import hashlib
import hmac
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from operant.local_caller import canonical_proof
from operant.server import (
    ServerConfigurationError,
    _setup_origins_from_env,
    build_server_config,
    server_config_snapshot,
)


def test_loopback_server_is_bounded_without_remote_gateway(tmp_path: Path) -> None:
    config = build_server_config(
        host="127.0.0.1",
        port=8765,
        ssl_certfile=None,
        ssl_keyfile=None,
        environment={"OPERANT_DB_PATH": str(tmp_path / "operant.sqlite3")},
    )
    assert server_config_snapshot(config) == {
        "host": "127.0.0.1",
        "port": 8765,
        "ssl": False,
        "ws_max_size": 512 * 1024,
        "proxy_headers": False,
        "workers": 1,
    }
    assert config.access_log is False


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "8.8.8.8", "core.example"])
def test_server_rejects_wildcard_public_and_ambiguous_hosts(host: str) -> None:
    with pytest.raises(ServerConfigurationError):
        build_server_config(
            host=host,
            port=8000,
            ssl_certfile=None,
            ssl_keyfile=None,
            environment={},
        )


def test_gateway_limit_is_wired_to_uvicorn_and_requires_tls(tmp_path: Path) -> None:
    environment = {
        "OPERANT_DB_PATH": str(tmp_path / "operant.sqlite3"),
        "OPERANT_REMOTE_GATEWAY_TOKEN_REF": "OPERANT_GATEWAY_TOKEN",
        "OPERANT_GATEWAY_TOKEN": "x" * 32,
        "OPERANT_REMOTE_GATEWAY_ALLOWED_ORIGINS_JSON": '["https://control.example"]',
        "OPERANT_REMOTE_GATEWAY_MAX_FRAME_BYTES": "4096",
    }
    with pytest.raises(ServerConfigurationError, match="TLS/WSS"):
        build_server_config(
            host="127.0.0.1",
            port=8000,
            ssl_certfile=None,
            ssl_keyfile=None,
            environment=environment,
        )


def test_private_network_requires_tls_and_oauth() -> None:
    with pytest.raises(ServerConfigurationError, match="TLS and OAuth"):
        build_server_config(
            host="192.168.1.20",
            port=8000,
            ssl_certfile=None,
            ssl_keyfile=None,
            environment={},
        )


def test_setup_origin_allowlist_is_explicit_and_loopback_only(tmp_path: Path) -> None:
    assert _setup_origins_from_env({}) == ()
    config = build_server_config(
        host="127.0.0.1",
        port=8000,
        ssl_certfile=None,
        ssl_keyfile=None,
        environment={
            "OPERANT_DB_PATH": str(tmp_path / "setup.sqlite3"),
            "OPERANT_SETUP_ALLOWED_ORIGINS_JSON": '["http://127.0.0.1:3000"]',
        },
    )
    assert config.app.state.setup_allowed_origins == frozenset({"http://127.0.0.1:3000"})
    for raw in (
        "[]",
        '["https://example.test"]',
        '["http://127.0.0.1"]',
        '["http://127.0.0.1:bad"]',
        '["http://user@localhost:3000"]',
        '["http://localhost:3000/path"]',
        '["http://localhost:3000?query"]',
        "invalid-json",
    ):
        with pytest.raises(ServerConfigurationError):
            _setup_origins_from_env({"OPERANT_SETUP_ALLOWED_ORIGINS_JSON": raw})


def test_desktop_bridge_has_exact_origin_and_stays_on_loopback(tmp_path: Path) -> None:
    config = build_server_config(
        host="127.0.0.1",
        port=8000,
        ssl_certfile=None,
        ssl_keyfile=None,
        desktop=True,
        environment={"OPERANT_DB_PATH": str(tmp_path / "desktop.sqlite3")},
    )
    middleware = [
        item for item in config.app.user_middleware if item.cls.__name__ == "CORSMiddleware"
    ]
    assert len(middleware) == 1
    assert middleware[0].kwargs["allow_origins"] == [
        "tauri://localhost",
        "http://tauri.localhost",
        "https://tauri.localhost",
    ]
    assert middleware[0].kwargs["allow_headers"] == [
        "Accept",
        "Content-Type",
        "Idempotency-Key",
        "Last-Event-ID",
        "X-Operant-Client-Version",
    ]
    with pytest.raises(ServerConfigurationError, match="desktop bridge"):
        build_server_config(
            host="192.168.1.20",
            port=8000,
            ssl_certfile=None,
            ssl_keyfile=None,
            desktop=True,
            environment={},
        )


@pytest.mark.parametrize(
    ("method", "suffix", "body", "signed_status"),
    [
        ("GET", "", b"", 404),
        ("POST", "", b'{"approved":true,"reason_code":"user-approved"}', 404),
        ("POST", "/review", b"", 409),
    ],
)
def test_desktop_approval_routes_require_native_proof_before_processing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    suffix: str,
    body: bytes,
    signed_status: int,
) -> None:
    monkeypatch.chdir(tmp_path)
    secret = b"approval-test".ljust(32, b"-")
    config = build_server_config(
        host="127.0.0.1",
        port=8765,
        ssl_certfile=None,
        ssl_keyfile=None,
        desktop=True,
        local_caller_secret=secret,
        environment={"OPERANT_DB_PATH": str(tmp_path / "approval.sqlite3")},
    )
    path = "/v1/security/approvals/approval_absent" + suffix
    timestamp = str(int(time.time()))
    nonce = "a" * 32
    proof = canonical_proof(
        method=method.encode(),
        target=path.encode(),
        timestamp=timestamp.encode(),
        nonce=nonce.encode(),
        idempotency_key=b"approval-test",
        body_digest=hashlib.sha256(body).hexdigest().encode(),
    )
    headers = {
        "X-Operant-Caller-Protocol": "local-caller.v1",
        "X-Operant-Caller-Timestamp": timestamp,
        "X-Operant-Caller-Nonce": nonce,
        "X-Operant-Caller-Signature": hmac.new(secret, proof, hashlib.sha256).hexdigest(),
        "Idempotency-Key": "approval-test",
        "Content-Type": "application/json",
    }
    with TestClient(
        config.app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 12000)
    ) as client:
        unsigned = client.request(method, path, content=body)
        assert unsigned.status_code == 403
        assert unsigned.json()["error"]["code"] == "local_caller_required"
        forged = client.request(
            method, path, content=body, headers={**headers, "X-Operant-Caller-Signature": "0" * 64}
        )
        assert forged.status_code == 403
        signed = client.request(method, path, content=body, headers=headers)
        assert signed.status_code == signed_status
        assert client.request(method, path, content=body, headers=headers).status_code == 403
