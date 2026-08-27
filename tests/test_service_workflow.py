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
        content = (
            "VERDICT: APPROVED"
            if snapshot.role_name == "Reviewer"
            else f"{snapshot.role_name} completed"
        )
        yield ProviderEvent(
            event_type="model.completed",
            response=ModelResponse(
                content=content,
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


class FailingProvider(ImmediateProvider):
    async def stream(
        self,
        *,
        snapshot: RoleSnapshot,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        del snapshot, messages, tools
        if False:
            yield ProviderEvent(event_type="unreachable")
        raise RuntimeError("raw provider detail must not enter persisted events")


class ConcurrentExplorerProvider(ImmediateProvider):
    def __init__(self) -> None:
        self.active_explorers = 0
        self.max_active_explorers = 0
        self.user_messages: dict[str, str] = {}

    async def stream(
        self,
        *,
        snapshot: RoleSnapshot,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        self.user_messages[snapshot.role_name] = messages[-1].content or ""
        if snapshot.role_name.startswith("Explorer"):
            self.active_explorers += 1
            self.max_active_explorers = max(
                self.max_active_explorers,
                self.active_explorers,
            )
            await asyncio.sleep(0.05)
            self.active_explorers -= 1
        async for event in super().stream(
            snapshot=snapshot,
            messages=messages,
            tools=tools,
        ):
            yield event


class SlowExplorerProvider(ConcurrentExplorerProvider):
    async def stream(
        self,
        *,
        snapshot: RoleSnapshot,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        self.user_messages[snapshot.role_name] = messages[-1].content or ""
        if snapshot.role_name == "Explorer":
            await asyncio.sleep(10)
        async for event in ImmediateProvider.stream(
            self,
            snapshot=snapshot,
            messages=messages,
            tools=tools,
        ):
            yield event


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


class ReworkProvider(ImmediateProvider):
    async def stream(
        self,
        *,
        snapshot: RoleSnapshot,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        del messages, tools
        content = (
            "需要补充测试。\nVERDICT: REWORK"
            if snapshot.role_name == "Reviewer"
            else f"{snapshot.role_name} completed"
        )
        yield ProviderEvent(
            event_type="model.completed",
            response=ModelResponse(content=content, finish_reason="stop"),
        )


class MissingVerdictProvider(ImmediateProvider):
    async def stream(
        self,
        *,
        snapshot: RoleSnapshot,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        del messages, tools
        content = "请补充测试。" if snapshot.role_name == "Reviewer" else "completed"
        yield ProviderEvent(
            event_type="model.completed",
            response=ModelResponse(content=content, finish_reason="stop"),
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
        ("role_main", "Main"),
        ("role_planner", "Planner"),
        ("role_explorer", "Explorer"),
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
async def test_readonly_explorers_run_in_parallel_and_handoff_results(tmp_path: Path) -> None:
    provider = ConcurrentExplorerProvider()
    service = service_with_roles(tmp_path, provider)
    service.create_role(
        RolePreset(
            id="role_api_explorer",
            name="Explorer API",
            system_prompt="Inspect API boundaries without changing files.",
            model_profile_id="model_test",
        )
    )

    events = [
        event
        async for event in SequentialCodingWorkflow(service).run(
            task="Fix calculator",
            workspace=tmp_path,
            main_role_id="role_main",
            planner_role_id="role_planner",
            explorer_role_ids=("role_explorer", "role_api_explorer"),
            coder_role_id="role_coder",
            reviewer_role_id="role_reviewer",
            max_parallel_explorers=2,
        )
    ]

    completed = [event for event in events if event.event_type == "agent.completed"]
    assert completed[0].role == "planner"
    assert [event.role for event in completed[1:3]] == ["explorer", "explorer"]
    assert [event.role for event in completed[3:]] == ["coder", "reviewer", "main"]
    assert len({event.session_id for event in completed}) == 6
    assert provider.max_active_explorers == 2
    assert "role_explorer" in provider.user_messages["Coder"]
    assert "role_api_explorer" in provider.user_messages["Coder"]
    assert '"role":"reviewer"' in provider.user_messages["Main"].replace(" ", "")
    assert events[-1].event_type == "workflow.completed"
    assert events[-1].payload["verdict"] == "APPROVED"


@pytest.mark.asyncio
async def test_explorer_timeout_is_structured_and_nonfatal(tmp_path: Path) -> None:
    provider = SlowExplorerProvider()
    service = service_with_roles(tmp_path, provider)
    service.update_role(
        "role_explorer",
        budget=Budget(timeout_seconds=1),
    )

    events = [
        event
        async for event in SequentialCodingWorkflow(service).run(
            task="Fix calculator",
            workspace=tmp_path,
            main_role_id="role_main",
            planner_role_id="role_planner",
            explorer_role_ids=("role_explorer",),
            coder_role_id="role_coder",
            reviewer_role_id="role_reviewer",
        )
    ]

    explorer_result = next(
        event.payload["result"]
        for event in events
        if event.role == "explorer" and event.event_type == "workflow.subtask_result"
    )
    assert explorer_result["status"] == "timed_out"
    assert explorer_result["failure_reason"] == "timeout after 1 seconds"
    assert explorer_result["completed_steps"] == ["agent.started", "agent.timed_out"]
    assert '"status":"timed_out"' in provider.user_messages["Coder"].replace(" ", "")
    assert events[-1].event_type == "workflow.completed"


def test_parallel_slots_reject_writable_explorer(tmp_path: Path) -> None:
    service = service_with_roles(tmp_path, ImmediateProvider())
    service.create_role(
        RolePreset(
            id="role_writable_explorer",
            name="Writable Explorer",
            system_prompt="Explore and edit.",
            model_profile_id="model_test",
            tool_policy=ToolPolicy(
                allowed_tools=("apply_patch",),
                workspace_write=True,
            ),
        )
    )

    with pytest.raises(ValueError, match="explorer\\[1\\] role must be read-only"):
        SequentialCodingWorkflow(service).validate_configuration(
            planner_role_id="role_planner",
            explorer_role_ids=("role_writable_explorer",),
            coder_role_id="role_coder",
            reviewer_role_id="role_reviewer",
            max_parallel_explorers=2,
        )


@pytest.mark.asyncio
async def test_rework_is_explicit_and_bounded(tmp_path: Path) -> None:
    service = service_with_roles(tmp_path, ReworkProvider())
    events = [
        event
        async for event in SequentialCodingWorkflow(service).run(
            task="Fix calculator",
            workspace=tmp_path,
            planner_role_id="role_planner",
            coder_role_id="role_coder",
            reviewer_role_id="role_reviewer",
            max_rework_rounds=1,
        )
    ]

    completed = [event for event in events if event.event_type == "agent.completed"]
    assert [event.role for event in completed] == [
        "planner",
        "coder",
        "reviewer",
        "coder",
        "reviewer",
    ]
    assert [event.event_type for event in events].count("workflow.rework_started") == 1
    assert [event.event_type for event in events].count("workflow.rework_limit_reached") == 1


@pytest.mark.asyncio
async def test_missing_reviewer_verdict_is_reported_when_rework_is_disabled(
    tmp_path: Path,
) -> None:
    service = service_with_roles(tmp_path, MissingVerdictProvider())
    events = [
        event
        async for event in SequentialCodingWorkflow(service).run(
            task="Fix calculator",
            workspace=tmp_path,
            planner_role_id="role_planner",
            coder_role_id="role_coder",
            reviewer_role_id="role_reviewer",
            max_rework_rounds=0,
        )
    ]

    assert [event.event_type for event in events].count("workflow.review_verdict_missing") == 1
    assert all(event.event_type != "workflow.rework_started" for event in events)


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
async def test_provider_exception_becomes_sanitized_persisted_failure(tmp_path: Path) -> None:
    service = service_with_roles(tmp_path, FailingProvider())
    session = service.create_session("role_planner")

    events = [
        event
        async for event in service.run_session(
            session.id,
            user_message="Fail safely",
            workspace=tmp_path,
        )
    ]

    assert events[-1].event_type == "agent.failed"
    assert events[-1].payload == {"error_type": "RuntimeError"}
    persisted = service.list_events(session.id)[-1]
    assert persisted.event_type == "agent.failed"
    assert persisted.payload["error_type"] == "RuntimeError"
    assert "raw provider detail" not in persisted.model_dump_json()


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
