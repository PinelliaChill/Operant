from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from fastapi import FastAPI
from fastapi.testclient import TestClient

from operant.api_model_connections import install_model_connection_routes
from operant.persistence.onboarding import UXRepository
from operant.persistence.sqlite import SQLiteStore


def test_completed_login_reports_actual_model_availability_and_safe_reason(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "operant.db")
    store.initialize()
    repo = UXRepository(store)

    class Gateway:
        def guard(self, **kwargs: Any) -> tuple[Any, Any, None]:
            return (
                SimpleNamespace(action_hash="hash"),
                SimpleNamespace(decision=SimpleNamespace(value="allow"), lease=object()),
                None,
            )

        def consume(self, lease: Any, action: Any) -> None:
            pass

    app = FastAPI()
    provider = install_model_connection_routes(
        app, cast(Any, SimpleNamespace(store=store)), repo, action_gateway=cast(Any, Gateway())
    )
    headers = {"Idempotency-Key": "account-status"}
    body = {"provider": "chatgpt"}
    with TestClient(app, base_url="http://127.0.0.1:4343") as client:
        started = client.post("/v1/setup/oauth/start", json=body, headers=headers)
        assert started.status_code == 200
        attempt_id = started.json()["attempt_id"]
        attempt = provider.oauth.attempts[attempt_id]
        attempt.status = "completed"
        record = {
            "connection_id": attempt.connection_id,
            "name": "ChatGPT",
            "provider": "chatgpt",
            "auth_method": "oauth",
            "base_url": "https://api.openai.com/v1",
        }
        repo.save_connection(
            attempt.connection_id,
            {
                **record,
                "account_label": "signed-in account",
                "status": "error",
                "error": "chatgpt_plan_usage_disabled",
            },
        )
        poll = client.get(f"/v1/setup/oauth/{attempt_id}")
        assert poll.status_code == 200
        assert poll.json()["status"] == "error"
        assert poll.json()["message"] == "chatgpt_plan_usage_disabled"
        assert poll.json()["authorization_url"] is None
        assert poll.headers["cache-control"] == "no-store"
        replay = client.post("/v1/setup/oauth/start", json=body, headers=headers)
        assert replay.json() == poll.json()
        assert (
            client.get("/v1/setup/connections").json()["items"][0]["account_label"]
            == "signed-in account"
        )

        repo.save_connection(
            attempt.connection_id,
            {
                **record,
                "status": "error",
                "error": "do-not-publish-untrusted-detail",
            },
        )
        safe = client.get(f"/v1/setup/oauth/{attempt_id}")
        assert safe.json()["message"] == "connection_unavailable"
        assert "do-not-publish" not in safe.text

        repo.save_connection(attempt.connection_id, {**record, "status": "ready", "error": None})
        assert client.get(f"/v1/setup/oauth/{attempt_id}").json()["status"] == "ready"
