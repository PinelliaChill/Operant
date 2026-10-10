from __future__ import annotations

import hashlib
import hmac
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from pydantic import BaseModel

from operant.local_caller import canonical_proof
from operant.server import (
    ServerConfigurationError,
    build_server_config,
    read_desktop_caller_secret,
)

FIXTURE_SECRET = bytes(range(32))


class Echo(BaseModel):
    text: str


def proof(
    method: str, path: str, body: bytes = b"", *, key: str = "", nonce: int
) -> dict[str, str]:
    timestamp = str(int(time.time()))
    nonce_text = f"{nonce:032x}"
    signed = canonical_proof(
        method=method.encode(),
        target=path.encode(),
        timestamp=timestamp.encode(),
        nonce=nonce_text.encode(),
        idempotency_key=key.encode(),
        body_digest=hashlib.sha256(body).hexdigest().encode(),
    )
    headers = {
        "X-Operant-Caller-Protocol": "local-caller.v1",
        "X-Operant-Caller-Timestamp": timestamp,
        "X-Operant-Caller-Nonce": nonce_text,
        "X-Operant-Caller-Signature": hmac.new(FIXTURE_SECRET, signed, hashlib.sha256).hexdigest(),
    }
    if key:
        headers["Idempotency-Key"] = key
    return headers


@pytest.mark.parametrize("size", [0, 31, 32, 33, 64])
def test_bootstrap_accepts_only_one_exact_size_pipe_message(
    monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, "rb") as stream:
        os.write(write_fd, b"f" * size)
        os.close(write_fd)
        monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=stream))
        if size == 32:
            assert read_desktop_caller_secret() == b"f" * 32
        else:
            with pytest.raises(ServerConfigurationError, match="invalid size"):
                read_desktop_caller_secret()


def test_bootstrap_never_reads_a_regular_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "not-a-private-pipe"
    path.write_bytes(FIXTURE_SECRET)
    with path.open("rb") as stream:
        monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=stream))
        with pytest.raises(ServerConfigurationError, match="private stdin pipe"):
            read_desktop_caller_secret()
        assert stream.tell() == 0


def test_caller_key_requires_desktop_mode_and_exact_size(tmp_path: Path) -> None:
    options: dict[str, Any] = {
        "host": "127.0.0.1",
        "port": 8765,
        "ssl_certfile": None,
        "ssl_keyfile": None,
        "environment": {"OPERANT_DB_PATH": str(tmp_path / "core.sqlite3")},
    }
    with pytest.raises(ServerConfigurationError, match="desktop mode"):
        build_server_config(**options, local_caller_secret=FIXTURE_SECRET)
    with pytest.raises(ServerConfigurationError, match="invalid size"):
        build_server_config(**options, desktop=True, local_caller_secret=b"too-short")


def test_server_proof_precedes_source_effects_and_pairing(tmp_path: Path) -> None:
    config = build_server_config(
        host="127.0.0.1",
        port=8765,
        ssl_certfile=None,
        ssl_keyfile=None,
        desktop=True,
        local_caller_secret=FIXTURE_SECRET,
        environment={"OPERANT_DB_PATH": str(tmp_path / "core.sqlite3")},
    )
    app = config.app
    resolved: list[str] = []
    effects = app.state.skill_source_effects
    original = effects.source_path

    def record_resolution(path: str | Path) -> Path:
        resolved.append(str(path))
        return original(path)

    effects.source_path = record_resolution

    @app.post("/v1/setup/proof-echo")
    def echo(body: Echo, request: Request) -> dict[str, str]:
        assert request.state.local_caller_trusted is True
        return {"text": body.text}

    with TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 12000)) as client:
        for method, path, body in (
            ("GET", "/v1/setup/skill-sources", None),
            ("POST", "/v1/setup/skill-sources", {"path": str(tmp_path)}),
            ("DELETE", "/v1/setup/skill-sources/user-fixture", None),
            ("POST", "/v1/remote-control/pairing-challenges", {"host_id": "untrusted"}),
            ("POST", "/v1/graph/runs/untrusted/writer-workspaces", {}),
            ("POST", "/v1/writer-workspaces/untrusted/lease", {}),
            ("POST", "/v1/merge-runs", {}),
            ("POST", "/v1/workbench/threads/untrusted/skill-commands", {}),
            ("GET", "/v1/workbench/extensions/commands", None),
            ("GET", "/v1/workbench/threads/untrusted/skill%2Dcommands", None),
        ):
            response = client.request(
                method, path, json=body, headers={"Idempotency-Key": "untrusted-source"}
            )
            assert response.status_code == 403
            assert response.json()["error"]["code"] == "local_caller_required"
        assert resolved == []
        preflight = client.options(
            "/v1/setup/connections",
            headers={
                "Origin": "tauri://localhost",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Content-Type,Idempotency-Key",
            },
        )
        assert preflight.status_code == 200
        assert preflight.headers["Access-Control-Allow-Origin"] == "tauri://localhost"
        assert (
            client.post(
                "/v1/setup/connections",
                json={"provider": "gemini"},
                headers={"Origin": "tauri://localhost", "Idempotency-Key": "untrusted-source"},
            ).status_code
            == 403
        )
        assert (
            client.options(
                "/v1/setup/connections",
                headers={
                    "Origin": "https://untrusted.example",
                    "Access-Control-Request-Method": "POST",
                },
            ).status_code
            == 400
        )
        for path in (
            "/v1/remote-control/devices/pair",
            "/v1/remote-control/commands",
            "/v1/remote-control/session-query",
        ):
            assert (
                client.post(
                    path, json={}, headers={"Idempotency-Key": "untrusted-source"}
                ).status_code
                == 422
            )
        with effects.store._connect() as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM command_executions WHERE idempotency_key=?",
                    ("untrusted-source",),
                ).fetchone()[0]
                == 0
            )
        assert client.get("/healthz").status_code == 200
        identity_path = "/internal/local-caller/identity"
        assert client.get(identity_path).status_code == 403
        identity = client.get(identity_path, headers=proof("GET", identity_path, nonce=3))
        assert identity.status_code == 200
        assert identity.headers["Cache-Control"] == "no-store"
        assert identity.json() == {
            "protocol": "local-caller.core.v1",
            "proof": hmac.new(
                FIXTURE_SECRET, b"local-caller.core.v1\n" + f"{3:032x}".encode(), hashlib.sha256
            ).hexdigest(),
        }
        # Callback keeps its own state/PKCE validation and does not require a
        # native proof; missing callback parameters are still explicitly rejected.
        assert client.get("/internal/model-auth/gemini/callback").status_code != 403
        assert (
            client.get(
                "/v1/setup/skill-sources",
                headers=proof("GET", "/v1/setup/skill-sources", nonce=1),
            ).status_code
            == 200
        )
        raw = '{"text":"测试请求"}'.encode()
        signed = proof("POST", "/v1/setup/proof-echo", raw, key="echo", nonce=2)
        response = client.post(
            "/v1/setup/proof-echo",
            content=raw,
            headers={**signed, "Content-Type": "application/json"},
        )
        assert response.status_code == 200
        assert response.json() == {"text": "测试请求"}
        assert (
            client.post(
                "/v1/setup/proof-echo",
                content=raw,
                headers={**signed, "Content-Type": "application/json"},
            ).status_code
            == 403
        )
