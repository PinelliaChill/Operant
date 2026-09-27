from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path

import pytest

from operant.application.plan_generation import generate_plan_draft
from operant.application.service import ApplicationService
from operant.application.task_control import TaskControlService
from operant.domain.messages import (
    Message,
    ModelResponse,
    ModelUsage,
    ProviderEvent,
    ToolDefinition,
)
from operant.domain.models import Budget, ModelProfile, RolePreset, RoleSnapshot, ToolPolicy
from operant.domain.task_control import Goal, PlanStatus
from operant.domain.threads import ConversationThread
from operant.persistence.sqlite import ConflictError, SQLiteStore


class PlanProvider:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls = 0
        self.tools: list[tuple[ToolDefinition, ...]] = []
        self.on_call: Callable[[], None] | None = None

    async def list_models(self, *, base_url: str, secret_ref: str) -> list[str]:
        del base_url, secret_ref
        return ["plan-model"]

    async def stream(
        self,
        *,
        snapshot: RoleSnapshot,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        del snapshot, messages
        self.calls += 1
        self.tools.append(tuple(tools))
        if self.on_call is not None:
            self.on_call()
        yield ProviderEvent(
            event_type="model.completed",
            response=ModelResponse(
                content=self.response,
                usage=ModelUsage(prompt_tokens=10, completion_tokens=20, total_tokens=30),
            ),
        )


def _scope(tmp_path: Path, response: str) -> tuple[ApplicationService, PlanProvider, str, str, str]:
    provider = PlanProvider(response)
    service = ApplicationService(SQLiteStore(tmp_path / "plan.sqlite"), provider)
    service.initialize()
    profile = service.add_model_profile(
        ModelProfile(
            name="planner-test",
            model_id="plan-model",
            base_url="https://example.invalid/v1",
            secret_ref="OPERANT_PLAN_TEST_KEY",
            context_window=16000,
        )
    )
    planner = service.create_role(
        RolePreset(
            id="role_planner",
            name="Planner",
            system_prompt="Plan read-only.",
            model_profile_id=profile.id,
            tool_policy=ToolPolicy(allowed_tools=("read_file", "search_files", "git_diff")),
            budget=Budget(max_turns=2, max_output_tokens=500, timeout_seconds=60),
        )
    )
    source_role = service.create_role(
        RolePreset(
            name="Source",
            system_prompt="Source session.",
            model_profile_id=profile.id,
        )
    )
    thread = service.create_thread(ConversationThread(workspace_ref=str(tmp_path.resolve())))
    source = service.create_session(source_role.id, thread_id=thread.id)
    goal = TaskControlService(service.store).create_goal(
        Goal(
            owner_thread_id=thread.id,
            objective="Ship an editable plan",
            completion_criteria=("Plan reviewed",),
            token_budget=300,
        )
    )
    return service, provider, goal.id, source.id, planner.id


@pytest.mark.asyncio
async def test_generate_plan_draft_uses_isolated_readonly_sidecar(tmp_path: Path) -> None:
    content = {
        "scope": "Implement only task control",
        "assumptions": [],
        "constraints": ["No execution"],
        "open_questions": [],
        "proposed_changes": ["Persist Plan"],
        "risk_items": [],
        "approval_requirements": ["Review before execution"],
        "verification_plan": ["Run focused tests"],
    }
    service, provider, goal_id, source_id, planner_id = _scope(tmp_path, json.dumps(content))
    goal = TaskControlService(service.store).get_goal(goal_id)
    result = await generate_plan_draft(
        service,
        goal_id=goal_id,
        source_session_id=source_id,
        thread_id=goal.owner_thread_id,
        workspace=tmp_path,
        planner_role_id=planner_id,
    )
    assert result.plan.status is PlanStatus.DRAFT
    assert result.plan.source_mode == "read_only"
    assert result.plan.scope == content["scope"]
    assert result.plan.created_from_context_revision is not None
    assert provider.calls == 1
    assert provider.tools == [()]
    assert TaskControlService(service.store).list_plans(goal_id) == [result.plan]
    assert service.store.list_items(goal.owner_thread_id) == []
    planner_session = service.get_session(result.planner_session_id)
    assert planner_session.role_snapshot.budget.max_turns == 1
    assert planner_session.role_snapshot.budget.max_tool_calls == 0
    assert planner_session.role_snapshot.budget.max_output_tokens == 300
    assert service.get_btw_sidecar_run(result.sidecar_run_id).response is not None


@pytest.mark.asyncio
async def test_generate_plan_draft_rejects_invalid_response_without_plan(tmp_path: Path) -> None:
    service, provider, goal_id, source_id, planner_id = _scope(tmp_path, "not JSON")
    goal = TaskControlService(service.store).get_goal(goal_id)
    with pytest.raises(ValueError, match="valid Plan draft"):
        await generate_plan_draft(
            service,
            goal_id=goal_id,
            source_session_id=source_id,
            thread_id=goal.owner_thread_id,
            workspace=tmp_path,
            planner_role_id=planner_id,
        )
    assert provider.calls == 1
    assert TaskControlService(service.store).list_plans(goal_id) == []


@pytest.mark.asyncio
async def test_generate_plan_draft_rejects_writable_planner_before_model(tmp_path: Path) -> None:
    service, provider, goal_id, source_id, _planner_id = _scope(tmp_path, "{}")
    writable = service.create_role(
        RolePreset(
            name="Writable",
            system_prompt="Write.",
            model_profile_id=service.list_model_profiles()[0].id,
            tool_policy=ToolPolicy(allowed_tools=("apply_patch",), workspace_write=True),
        )
    )
    goal = TaskControlService(service.store).get_goal(goal_id)
    with pytest.raises(PermissionError, match="read-only"):
        await generate_plan_draft(
            service,
            goal_id=goal_id,
            source_session_id=source_id,
            thread_id=goal.owner_thread_id,
            workspace=tmp_path,
            planner_role_id=writable.id,
        )
    assert provider.calls == 0
    assert TaskControlService(service.store).list_plans(goal_id) == []


@pytest.mark.asyncio
async def test_generate_plan_draft_rejects_goal_changed_during_model(tmp_path: Path) -> None:
    content = {
        "scope": "Draft",
        "assumptions": [],
        "constraints": [],
        "open_questions": [],
        "proposed_changes": [],
        "risk_items": [],
        "approval_requirements": [],
        "verification_plan": [],
    }
    service, provider, goal_id, source_id, planner_id = _scope(tmp_path, json.dumps(content))
    control = TaskControlService(service.store)
    goal = control.get_goal(goal_id)
    provider.on_call = lambda: control.update_goal(
        goal_id, expected_revision=goal.revision, objective="Changed objective"
    )
    with pytest.raises(ConflictError, match="Goal changed"):
        await generate_plan_draft(
            service,
            goal_id=goal_id,
            source_session_id=source_id,
            thread_id=goal.owner_thread_id,
            workspace=tmp_path,
            planner_role_id=planner_id,
        )
    assert control.list_plans(goal_id) == []
