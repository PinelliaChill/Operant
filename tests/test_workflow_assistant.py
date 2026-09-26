from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from operant.application.workflow_assistant import (
    WorkflowSuggestionError,
    WorkflowSuggestionRequest,
    suggest_workflow,
)
from operant.domain.messages import ModelResponse, ModelUsage, ProviderEvent
from operant.domain.models import ModelProfile, RolePreset
from operant.domain.team import TeamDefinition, TeamMember


class SuggestionProvider:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.snapshots: list[Any] = []
        self.prompts: list[str] = []

    async def stream(
        self, *, snapshot: Any, messages: Any, tools: Any
    ) -> AsyncIterator[ProviderEvent]:
        assert not tools
        assert "member_1" in messages[1].content
        self.prompts.append(messages[1].content)
        self.snapshots.append(snapshot)
        yield ProviderEvent(
            event_type="model.completed",
            response=ModelResponse(
                content=json.dumps(self.payload),
                usage=ModelUsage(prompt_tokens=10, completion_tokens=20, total_tokens=30),
            ),
        )


class SuggestionService:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.provider = SuggestionProvider(payload)
        self.profile = ModelProfile(
            id="profile_1",
            name="local test profile",
            model_id="discovered-test-model",
            base_url="https://provider.invalid/v1",
            secret_ref="OPERANT_TEST_SECRET",
        )
        self.role = RolePreset(
            id="role_1",
            name="Coordinator",
            system_prompt="Coordinate.",
            model_profile_id=self.profile.id,
        )

    def get_role(self, role_id: str, version: int | None = None) -> RolePreset:
        assert role_id == self.role.id
        assert version is None or version == self.role.version
        return self.role

    def get_model_profile(self, profile_id: str) -> ModelProfile:
        assert profile_id == self.profile.id
        return self.profile


TEAM = TeamDefinition(
    team_id="team_1",
    version=1,
    members=(
        TeamMember(
            member_id="member_1",
            agent_definition_id="role_1",
            role="Coordinator",
            can_coordinate=True,
        ),
    ),
    default_coordinator="member_1",
)


def _request() -> WorkflowSuggestionRequest:
    return WorkflowSuggestionRequest(
        instruction="让 Agent 总结任务",
        team_id=TEAM.team_id,
        team_version=TEAM.version,
    )


def test_suggestion_is_unpersisted_pinned_and_compiled() -> None:
    service = SuggestionService(
        {
            "name": "总结任务",
            "description": "交给一个 Agent",
            "nodes": [
                {"node_id": "member_1", "node_kind": "agent", "metadata": {"role_id": "invented"}}
            ],
            "edges": [],
        }
    )
    result = asyncio.run(
        suggest_workflow(request=_request(), service=service, team=TEAM, base=None, next_version=1)
    )
    assert result.definition.status.value == "draft"
    assert result.definition.default_policy == {
        "b24_executor": True,
        "team_id": TEAM.team_id,
        "team_version": TEAM.version,
    }
    assert result.definition.nodes[0].metadata["role_id"] == "role_1"
    assert result.definition.locked_role_versions == {"role_1": 1}
    assert result.model_id == "discovered-test-model"
    assert result.completion_tokens == 20
    assert service.provider.snapshots[0].budget.max_output_tokens == 4096


def test_suggestion_redacts_credential_shaped_instruction() -> None:
    service = SuggestionService(
        {
            "name": "Review",
            "description": "Review task",
            "nodes": [{"node_id": "member_1", "node_kind": "agent"}],
            "edges": [],
        }
    )
    request = _request().model_copy(
        update={"instruction": "Review with Authorization: Bearer very-private-test-token"}
    )
    result = asyncio.run(
        suggest_workflow(request=request, service=service, team=TEAM, base=None, next_version=1)
    )
    assert result.input_redacted is True
    assert "very-private-test-token" not in service.provider.prompts[0]


def test_suggestion_moves_common_model_fields_into_metadata() -> None:
    service = SuggestionService(
        {
            "name": "Review",
            "description": "Review task",
            "nodes": [
                {
                    "node_id": "member_1",
                    "node_kind": "agent",
                    "role_id": "model-invented",
                    "role_version": 99,
                }
            ],
            "edges": [],
        }
    )
    result = asyncio.run(
        suggest_workflow(request=_request(), service=service, team=TEAM, base=None, next_version=1)
    )
    assert result.definition.nodes[0].metadata["role_id"] == "role_1"
    assert result.definition.nodes[0].metadata["role_version"] == 1


def test_suggestion_reports_workflow_title_change() -> None:
    service = SuggestionService(
        {
            "name": "Original",
            "description": "Review task",
            "nodes": [{"node_id": "member_1", "node_kind": "agent"}],
            "edges": [],
        }
    )
    first = asyncio.run(
        suggest_workflow(request=_request(), service=service, team=TEAM, base=None, next_version=1)
    )
    service.provider.payload["name"] = "Renamed"
    second = asyncio.run(
        suggest_workflow(
            request=_request(), service=service, team=TEAM, base=first.definition, next_version=2
        )
    )
    assert second.definition.workflow_id == first.definition.workflow_id
    assert second.definition.version == 2
    assert "工作流名称：已修改" in second.changes


def test_suggestion_rejects_unbound_agent() -> None:
    service = SuggestionService(
        {
            "name": "bad",
            "description": "bad",
            "nodes": [{"node_id": "invented", "node_kind": "agent"}],
            "edges": [],
        }
    )
    with pytest.raises(WorkflowSuggestionError, match="outside the Team"):
        asyncio.run(
            suggest_workflow(
                request=_request(), service=service, team=TEAM, base=None, next_version=1
            )
        )
