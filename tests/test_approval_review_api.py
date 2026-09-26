from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from operant.api import create_app
from operant.domain.messages import Message, ModelResponse, ProviderEvent, ToolDefinition
from operant.domain.models import ModelProfile, RoleSnapshot


def test_phase45_ask_automatically_reviews_with_configured_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPERANT_H09_MCP_ENDPOINT", "https://mcp.example/sse")
    monkeypatch.setenv("OPERANT_H09_MCP_SECRET", "test-only-secret")
    app = create_app(tmp_path / "auto-review.sqlite3")
    service = app.state.operant_service
    service.add_model_profile(
        ModelProfile(
            id="independent-reviewer",
            name="Approval reviewer",
            model_id="discovered-model",
            base_url="https://provider.example/v1",
            secret_ref="REVIEWER_TEST_KEY",
        )
    )
    seen: list[tuple[RoleSnapshot, Sequence[Message]]] = []

    async def list_models(*, base_url: str, secret_ref: str) -> list[str]:
        return ["discovered-model"]

    async def stream(
        *, snapshot: RoleSnapshot, messages: Sequence[Message], tools: Sequence[ToolDefinition]
    ) -> AsyncIterator[ProviderEvent]:
        seen.append((snapshot, messages))
        assert tools == ()
        yield ProviderEvent(
            event_type="model.completed",
            response=ModelResponse(
                content=json.dumps(
                    {"decision": "allow", "reason_code": "bounded_send", "summary": "Approved."}
                )
            ),
        )

    monkeypatch.setattr(service.provider, "list_models", list_models)
    monkeypatch.setattr(service.provider, "stream", stream)

    with TestClient(app) as client:
        configured = client.put(
            "/v1/config/scopes/global/default",
            json={
                "expected_revision": 0,
                "patch": {
                    "approval_reviewer": {
                        "mode": "auto",
                        "profile_id": "independent-reviewer",
                        "strictness": "permissive",
                        "custom_rules": ["Allow bounded test sends."],
                    }
                },
            },
        )
        assert configured.status_code == 200, configured.text
        created = client.post(
            "/v1/mcp/servers",
            json={
                "server_id": "auto-review-test",
                "transport": "legacy_sse",
                "endpoint_ref": "OPERANT_H09_MCP_ENDPOINT",
                "secret_ref": "OPERANT_H09_MCP_SECRET",
            },
        )
        assert created.status_code == 201, created.text
        asked = client.post("/v1/mcp/servers/auto-review-test/start")
        assert asked.status_code == 409, asked.text
        approval_id = asked.json()["detail"]["approval_id"]
        approval = {}
        for _ in range(40):
            approval = client.get(f"/v1/security/approvals/{approval_id}").json()
            if approval["status"] != "pending":
                break
            time.sleep(0.025)
        assert approval["status"] == "approved", approval
        assert approval["decided_by"] == "reviewer"
        audit = client.get(f"/v1/security/actions/{approval['action_hash']}/audit").json()["items"]
        decided = [event for event in audit if event["event_type"] == "approval.decided"]
        assert len(decided) == 1
        assert decided[0]["detail"]["model_profile_id"] == "independent-reviewer"
    assert len(seen) == 1
    assert seen[0][0].model_profile_id == "independent-reviewer"
    assert "test-only-secret" not in " ".join(message.content for message in seen[0][1])
