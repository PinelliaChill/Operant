"""The suggestion API returns a reviewable draft without publishing or running it."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi.testclient import TestClient

from operant.api import create_app
from operant.domain.messages import ModelResponse, ModelUsage, ProviderEvent
from operant.domain.models import ModelProfile, RolePreset
from operant.domain.team import TeamDefinition, TeamMember


class JsonProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def list_models(self, *, base_url: str, secret_ref: str) -> list[str]:
        del base_url, secret_ref
        return ["suggestion-test-model"]

    async def stream(self, **kwargs: Any) -> AsyncIterator[ProviderEvent]:
        assert kwargs["tools"] == ()
        self.calls += 1
        yield ProviderEvent(
            event_type="model.completed",
            response=ModelResponse(
                content=json.dumps(
                    {
                        "name": "Review task",
                        "description": "One Agent reviews the task.",
                        "nodes": [{"node_id": "reviewer", "node_kind": "agent"}],
                        "edges": [],
                    }
                ),
                usage=ModelUsage(prompt_tokens=12, completion_tokens=17, total_tokens=29),
            ),
        )


def test_suggest_requires_explicit_save_and_publish(tmp_path: Any) -> None:
    app = create_app(tmp_path / "assistant.db")
    service = app.state.operant_service
    provider = JsonProvider()
    service.provider = provider
    profile = service.add_model_profile(
        ModelProfile(
            name="test",
            model_id="suggestion-test-model",
            base_url="https://provider.invalid/v1",
            secret_ref="OPERANT_TEST_SECRET",
        )
    )
    role = service.create_role(
        RolePreset(name="Reviewer", system_prompt="Review.", model_profile_id=profile.id)
    )
    team = TeamDefinition(
        members=(
            TeamMember(
                member_id="reviewer",
                agent_definition_id=role.id,
                role="Reviewer",
                can_coordinate=True,
            ),
        ),
        default_coordinator="reviewer",
    )
    app.state.team_repository.put_team_definition(team)
    with TestClient(app) as client:
        response = client.post(
            "/v1/graph/workflows/suggest",
            json={
                "instruction": "请让 Reviewer 审核任务",
                "team_id": team.team_id,
                "team_version": team.version,
            },
        )
        assert response.status_code == 200, response.text
        suggestion = response.json()
        definition = suggestion["definition"]
        assert suggestion["model_id"] == "suggestion-test-model"
        assert definition["status"] == "draft"
        assert definition["nodes"][0]["metadata"]["role_id"] == role.id
        assert definition["nodes"][0]["agent_or_action_ref"] is None
        assert definition["nodes"][0]["retry_policy"] == {
            "max_attempts": 1,
            "delay_seconds": 0.0,
        }
        assert provider.calls == 1
        missing = client.get(
            f"/v1/graph/workflows/{definition['workflow_id']}/definitions/{definition['version']}"
        )
        assert missing.status_code == 404

        saved = client.post("/v1/graph/workflows/drafts", json=definition)
        assert saved.status_code == 202, saved.text
        readback = client.get(
            f"/v1/graph/workflows/{definition['workflow_id']}/definitions/{definition['version']}"
        )
        assert readback.status_code == 200
        assert readback.json()["nodes"] == definition["nodes"]
        compiled = client.post(
            f"/v1/graph/workflows/{definition['workflow_id']}/compile",
            json={"definition_version": definition["version"]},
        )
        assert compiled.status_code == 200, compiled.text
        assert compiled.json()["valid"] is True
