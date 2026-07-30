import asyncio
import subprocess
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest

from operant.application.service import ApplicationService
from operant.application.workflow import SequentialCodingWorkflow
from operant.domain.messages import (
    Message,
    ModelResponse,
    ProviderEvent,
    ToolCall,
    ToolDefinition,
)
from operant.domain.models import Budget, ModelProfile, RolePreset, RoleSnapshot, ToolPolicy
from operant.persistence.sqlite import SQLiteStore


class ImmediateProvider:
    async def list_models(self, *, base_url: str, secret_ref: str) -> list[str]:
        del base_url, secret_ref
        return ["test-model"]

    async def stream(
        self,
        *,
        snapshot: RoleSnapshot,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        del messages, tools
        yield ProviderEvent(
            event_type="model.completed",
            response=ModelResponse(
                content=f"{snapshot.role_name} completed",
                finish_reason="stop",
            ),
        )


class SlowProvider(ImmediateProvider):
    async def stream(
        self,
        *,
        snapshot: RoleSnapshot,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        del snapshot, messages, tools
        await asyncio.sleep(10)
        yield ProviderEvent(
            event_type="model.completed",
            response=ModelResponse(content="too late", finish_reason="stop"),
        )


class ApprovalProvider(ImmediateProvider):
    def __init__(self) -> None:
        self.turn = 0

    async def stream(
        self,
        *,
        snapshot: RoleSnapshot,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        del snapshot, messages, tools
        self.turn += 1
        if self.turn == 1:
            yield ProviderEvent(
                event_type="model.completed",
                response=ModelResponse(
                    tool_calls=(
                        ToolCall(
                            id="service_git_add",
                            name="run_command",
                            arguments_json='{"argv":["git","add","change.txt"]}',
                        ),
                    ),
                    finish_reason="tool_calls",
                ),
            )
        else:
            yield ProviderEvent(
                event_type="model.completed",
                response=ModelResponse(content="approved", finish_reason="stop"),
            )


def service_with_roles(
    tmp_path: Path,
    provider: ImmediateProvider,
    *,
    timeout_seconds: int = 10,
) -> ApplicationService:
    store = SQLiteStore(tmp_path / "service.sqlite3")
    service = ApplicationService(store, provider)
    service.initialize()
    profile = service.add_model_profile(
        ModelProfile(
            id="model_test",
            name="test",
            model_id="test-model",
            base_url="https://relay.example.com/v1",
            secret_ref="OPERANT_TEST_KEY",
        )
    )
    for role_id, name in (
        ("role_planner", "Planner"),
        ("role_coder", "Coder"),
        ("role_reviewer", "Reviewer"),
    ):
        service.create_role(
            RolePreset(
                id=role_id,
                name=name,
                system_prompt=f"You are {name}.",
                model_profile_id=profile.id,
                budget=Budget(timeout_seconds=timeout_seconds),
            )
        )
    return service


@pytest.mark.asyncio
async def test_sequential_workflow_streams_three_isolated_sessions(tmp_path: Path) -> None:
    service = service_with_roles(tmp_path, ImmediateProvider())
    events = [
        event
        async for event in SequentialCodingWorkflow(service).run(
            task="Fix calculator",
            workspace=tmp_path,
            planner_role_id="role_planner",
            coder_role_id="role_coder",
            reviewer_role_id="role_reviewer",
        )
    ]

    completed = [event for event in events if event.event_type == "agent.completed"]
    assert [event.role for event in completed] == ["planner", "coder", "reviewer"]
    assert len({event.session_id for event in completed}) == 3


@pytest.mark.asyncio
async def test_total_timeout_is_persisted(tmp_path: Path) -> None:
    service = service_with_roles(tmp_path, SlowProvider(), timeout_seconds=1)
    session = service.create_session("role_planner")

    events = [
        event
        async for event in service.run_session(
            session.id,
            user_message="Wait",
            workspace=tmp_path,
        )
    ]

    assert events[-1].event_type == "agent.timed_out"
    assert service.list_events(session.id)[-1].event_type == "agent.timed_out"


@pytest.mark.asyncio
async def test_running_session_can_be_cancelled(tmp_path: Path) -> None:
    service = service_with_roles(tmp_path, SlowProvider())
    session = service.create_session("role_planner")

    async def consume() -> list[str]:
        return [
            event.event_type
            async for event in service.run_session(
                session.id,
                user_message="Wait",
                workspace=tmp_path,
            )
        ]

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    assert service.cancel_session(session.id)
    assert (await task)[-1] == "agent.cancelled"


@pytest.mark.asyncio
async def test_service_approval_endpoint_resumes_pending_run(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "change.txt").write_text("change\n", encoding="utf-8")
    provider = ApprovalProvider()
    service = service_with_roles(tmp_path, provider)
    service.update_role(
        "role_coder",
        tool_policy=ToolPolicy(
            allowed_tools=("run_command",),
            command_execution=True,
        ),
    )
    session = service.create_session("role_coder")

    async def consume() -> list[str]:
        return [
            event.event_type
            async for event in service.run_session(
                session.id,
                user_message="Stage it",
                workspace=tmp_path,
            )
        ]

    task = asyncio.create_task(consume())
    for _ in range(100):
        pending = service.list_pending_approvals(session.id)
        if pending:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("approval did not become pending")

    assert service.submit_approval(
        session.id,
        pending[0]["tool_call_id"],
        approved=True,
    )
    events = await task

    assert "tool.approval_required" in events
    assert "tool.approval_decided" in events
    assert events[-1] == "agent.completed"
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert staged.stdout.strip() == "change.txt"
