"""Local browser transport stays separate from native management permissions."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from operant.server import build_server_config


def test_loopback_preflight_does_not_authenticate_or_issue_a_ticket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = build_server_config(
        host="127.0.0.1",
        port=18778,
        ssl_certfile=None,
        ssl_keyfile=None,
        desktop=True,
        environment={"OPERANT_DB_PATH": str(tmp_path / "cors.sqlite3")},
        local_caller_secret=b"test-caller-secret".ljust(32, b"x"),
    )
    with TestClient(config.app, base_url="http://127.0.0.1:18778") as client:
        for origin in ("tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"):
            for path in ("/v1/protocol/onboarding", "/v1/protocol/caller-pairing"):
                response = client.get(path, headers={"Origin": origin})
                assert response.status_code == 200
                assert response.headers.get_list("access-control-allow-origin") == [origin]
        for path, method in (
            ("/v1/protocol/caller-pairing", "GET"),
            ("/v1/protocol/onboarding", "GET"),
            ("/v1/local-callers/pair", "POST"),
            ("/v1/local-callers/commands", "POST"),
            ("/v1/local-callers/requests/readback", "POST"),
        ):
            response = client.options(
                path,
                headers={
                    "Origin": "http://127.0.0.1:3031",
                    "Access-Control-Request-Method": method,
                    "Access-Control-Request-Headers": (
                        "Content-Type, Idempotency-Key, X-Operant-Client-Version"
                    ),
                },
            )
            assert response.status_code == 200
            assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:3031"
            assert "access-control-allow-credentials" not in response.headers
        for path in ("/v1/local-callers/challenges", "/v1/setup/skill-sources"):
            response = client.options(
                path,
                headers={
                    "Origin": "http://127.0.0.1:3031",
                    "Access-Control-Request-Method": "POST",
                },
            )
            assert response.status_code >= 400
        for origin in ("https://other.example", "null", "http://localhost:3031"):
            response = client.options(
                "/v1/local-callers/pair",
                headers={"Origin": origin, "Access-Control-Request-Method": "POST"},
            )
            assert response.status_code == 403
            assert "access-control-allow-origin" not in response.headers
        for method, header in (
            ("DELETE", "content-type"),
            ("POST", "x-operant-native-confirmation"),
        ):
            response = client.options(
                "/v1/local-callers/pair",
                headers={
                    "Origin": "http://127.0.0.1:3031",
                    "Access-Control-Request-Method": method,
                    "Access-Control-Request-Headers": header,
                },
            )
            assert response.status_code == 403
        response = client.post(
            "/v1/local-callers/pair", json={}, headers={"Origin": "http://127.0.0.1:3031"}
        )
        assert response.status_code == 422
        assert response.headers.get_list("access-control-allow-origin") == ["http://127.0.0.1:3031"]
        with config.app.state.caller_pairing_runtime.repo.store._connect() as db:
            assert db.execute("SELECT count(*) FROM caller_pairing_challenges").fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM caller_pairing_devices").fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM command_executions").fetchone()[0] == 0
