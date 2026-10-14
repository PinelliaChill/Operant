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
from operant.persistence.workflow_suggestions import (
    SQLiteWorkflowSuggestionRepository,
    SuggestionConflictError,
)


class JsonProvider:
    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.calls = 0
        self.prompts: list[str] = []
        self.payload = payload or {
            "name": "Review task",
            "description": "One Agent reviews the task.",
            "nodes": [{"node_id": "reviewer", "node_kind": "agent"}],
            "edges": [],
        }

    async def list_models(self, *, base_url: str, secret_ref: str) -> list[str]:
        del base_url, secret_ref
        return ["suggestion-test-model"]

    async def stream(self, **kwargs: Any) -> AsyncIterator[ProviderEvent]:
        assert kwargs["tools"] == ()
        self.calls += 1
        self.prompts.append(kwargs["messages"][1].content)
        yield ProviderEvent(
            event_type="model.completed",
            response=ModelResponse(
                content=json.dumps(self.payload),
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


def test_suggestion_conversation_survives_restart_and_stays_unpublished(tmp_path: Any) -> None:
    database = tmp_path / "conversation.db"
    app = create_app(database)
    service = app.state.operant_service
    provider = JsonProvider()
    service.provider = provider
    profile = service.add_model_profile(
        ModelProfile(
            name="history model",
            model_id="suggestion-test-model",
            base_url="https://provider.invalid/v1",
            secret_ref="OPERANT_TEST_SECRET",
        )
    )
    role = service.create_role(
        RolePreset(name="History reviewer", system_prompt="Review.", model_profile_id=profile.id)
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
    first_input = {
        "instruction": "Review with Authorization: Bearer very-private-test-token",
        "team_id": team.team_id,
        "team_version": team.version,
    }
    with TestClient(app) as client:
        first = client.post("/v1/graph/workflows/suggest", json=first_input)
        assert first.status_code == 200, first.text
        conversation_id = first.json()["conversation_id"]
        assert first.json()["turn_id"]
        assert "very-private-test-token" not in str(first.json())
        with app.state.operant_service.store._connect() as connection:
            persisted = connection.execute(
                "SELECT instruction, suggestion_json FROM workflow_suggestion_turns"
            ).fetchone()
        assert "very-private-test-token" not in str(tuple(persisted))
        assert (
            client.get(
                f"/v1/graph/workflows/{first.json()['definition']['workflow_id']}/definitions/1"
            ).status_code
            == 404
        )

    restarted = create_app(database)
    second_provider = JsonProvider()
    restarted.state.operant_service.provider = second_provider
    with TestClient(restarted) as client:
        history = client.get(f"/v1/graph/workflows/suggestion-conversations/{conversation_id}")
        assert history.status_code == 200, history.text
        assert history.json()["turn_count"] == 1
        assert "very-private-test-token" not in history.text
        continued = client.post(
            "/v1/graph/workflows/suggest",
            json={
                **first_input,
                "instruction": "改成新的任务名称",
                "conversation_id": conversation_id,
            },
        )
        assert continued.status_code == 200, continued.text
        assert continued.json()["conversation_id"] == conversation_id
        assert continued.json()["turn_id"] != first.json()["turn_id"]
        assert "conversation_history" in second_provider.prompts[0]
        assert "previous_candidate" in second_provider.prompts[0]
        assert "very-private-test-token" not in second_provider.prompts[0]
        readback = client.get(
            f"/v1/graph/workflows/suggestion-conversations/{conversation_id}"
        ).json()
        assert readback["turn_count"] == 2
        assert readback["last_instruction"] == "改成新的任务名称"
        assert len(readback["turns"]) == 2
        listed = client.get("/v1/graph/workflows/suggestion-conversations").json()
        assert listed["items"][0]["conversation_id"] == conversation_id
        assert listed["items"][0]["turn_count"] == 2
        repository = SQLiteWorkflowSuggestionRepository(database)
        try:
            repository.append(
                conversation_id=conversation_id,
                expected_turn_count=1,
                team_id=team.team_id,
                team_version=team.version,
                base_workflow_id=None,
                base_version=None,
                instruction="stale concurrent edit",
                suggestion=continued.json(),
            )
        except SuggestionConflictError:
            pass
        else:
            raise AssertionError("stale concurrent suggestion was appended")
        assert repository.get(conversation_id)["turn_count"] == 2

        class BrokenProvider(JsonProvider):
            async def stream(self, **kwargs: Any) -> AsyncIterator[ProviderEvent]:
                del kwargs
                yield ProviderEvent(
                    event_type="model.completed",
                    response=ModelResponse(content="not JSON"),
                )

        restarted.state.operant_service.provider = BrokenProvider()
        failed = client.post(
            "/v1/graph/workflows/suggest",
            json={
                **first_input,
                "instruction": "invalid third turn",
                "conversation_id": conversation_id,
            },
        )
        assert failed.status_code == 422
        assert repository.get(conversation_id)["turn_count"] == 2


def test_suggestion_normalizes_artifact_publication_contract(tmp_path: Any) -> None:
    app = create_app(tmp_path / "artifact-suggestion.db")
    service = app.state.operant_service
    service.provider = JsonProvider(
        {
            "name": "Publish review",
            "description": "Review and publish a summary.",
            "nodes": [
                {
                    "node_id": "reviewer",
                    "node_kind": "agent",
                    "output_ports": [{"name": "out"}],
                },
                {
                    "node_id": "publish",
                    "node_kind": "artifact",
                    "input_ports": [{"name": "in"}],
                    "title": "Review summary",
                    "content": "Reviewed",
                    "media_type": "text/plain",
                },
            ],
            "edges": [
                {
                    "edge_id": "review-publish",
                    "source_node": "reviewer",
                    "source_port": "out",
                    "target_node": "publish",
                    "target_port": "in",
                }
            ],
        }
    )
    profile = service.add_model_profile(
        ModelProfile(
            name="artifact model",
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
            json={"instruction": "发布审核摘要", "team_id": team.team_id, "team_version": 1},
        )
        assert response.status_code == 200, response.text
        artifact = next(
            node for node in response.json()["definition"]["nodes"] if node["node_id"] == "publish"
        )
        assert artifact["idempotency_class"] == "idempotent"
        assert artifact["writes_workspace"] is False
        assert artifact["metadata"] == {
            "title": "Review summary",
            "content": "Reviewed",
            "media_type": "text/plain",
        }
