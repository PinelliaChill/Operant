from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest

from operant.application.service import ApplicationService
from operant.application.workflow import SequentialCodingWorkflow, WorkflowEvent
from operant.domain.messages import (
    Message,
    ModelResponse,
    ProviderEvent,
    ToolCall,
    ToolDefinition,
)
from operant.domain.models import (
    CommandExecutionPolicy,
    CommandRunnerType,
    ModelProfile,
    RolePreset,
    RoleSnapshot,
    ToolPolicy,
)
from operant.persistence.sqlite import SQLiteStore


class MemoryWorkflowProvider:
    def __init__(self) -> None:
        self.coder_turn = 0
        self.messages: dict[str, list[str]] = {}

    async def list_models(self, *, base_url: str, secret_ref: str) -> list[str]:
        del base_url, secret_ref
        return ["memory-model"]

    async def stream(
        self,
        *,
        snapshot: RoleSnapshot,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        del tools
        self.messages.setdefault(snapshot.role_name, []).append(messages[-1].content or "")
        if snapshot.role_name == "Coder":
            self.coder_turn += 1
            if self.coder_turn == 1:
                yield ProviderEvent(
                    event_type="model.completed",
                    response=ModelResponse(
                        tool_calls=(
                            ToolCall(
                                id="verify_unittest",
                                name="run_command",
                                arguments_json=('{"argv":["python3","-m","unittest","-v"]}'),
                            ),
                        ),
                        finish_reason="tool_calls",
                    ),
                )
                return
        content = (
            "VERDICT: APPROVED"
            if snapshot.role_name == "Reviewer"
            else f"{snapshot.role_name} completed"
        )
        yield ProviderEvent(
            event_type="model.completed",
            response=ModelResponse(content=content, finish_reason="stop"),
        )


def memory_workflow_service(
    tmp_path: Path,
    provider: MemoryWorkflowProvider,
) -> ApplicationService:
    service = ApplicationService(SQLiteStore(tmp_path / "workflow-memory.sqlite3"), provider)
    service.initialize()
    service.add_model_profile(
        ModelProfile(
            id="model_memory_workflow",
            name="memory-model",
            model_id="memory-model",
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
        writable = name == "Coder"
        service.create_role(
            RolePreset(
                id=role_id,
                name=name,
                system_prompt=f"You are {name}.",
                model_profile_id="model_memory_workflow",
                memory_scope=(
                    "read: [project, episodic]; write: [project, episodic]"
                    if writable
                    else "read: [project, episodic]; write: []"
                ),
                tool_policy=(
                    ToolPolicy(
                        allowed_tools=("run_command",),
                        workspace_write=True,
                        command_execution=True,
                        command_execution_policy=CommandExecutionPolicy(
                            runner=CommandRunnerType.HOST
                        ),
                    )
                    if writable
                    else ToolPolicy()
                ),
            )
        )
    return service


async def consume_with_expected_unittest_approval(
    service: ApplicationService,
    stream: AsyncIterator[WorkflowEvent],
) -> list[WorkflowEvent]:
    events: list[WorkflowEvent] = []
    approved_session_id: str | None = None
    async for event in stream:
        events.append(event)
        if event.event_type != "tool.approval_required":
            continue
        assert approved_session_id is None
        assert event.role == "coder"
        assert event.payload["tool_call_id"] == "verify_unittest"
        assert event.payload["name"] == "run_command"
        assert event.payload["category"] == "security_policy"
        assert event.payload["detail"] == (
            "run_command category=security_policy; executable=python3; argument_count=4"
        )
        assert service.submit_approval(
            event.session_id,
            "verify_unittest",
            approved=True,
        )
        approved_session_id = event.session_id

    assert approved_session_id is not None
    approval = service.store.list_approval_requests(approved_session_id, status=None)[0]
    assert [
        audit.event_type for audit in service.store.list_approval_audit_events(approval.id)
    ] == ["approval.requested", "approval.decided"]
    assert [event.event_type for event in events].count("tool.approval_required") == 1
    decided = [event for event in events if event.event_type == "tool.approval_decided"]
    assert len(decided) == 1
    assert decided[0].payload["approved"] is True
    return events


@pytest.mark.asyncio
async def test_workflow_does_not_use_legacy_memory_recall_or_writeback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "test_sample.py").write_text(
        "import unittest\n\n"
        "class SampleTest(unittest.TestCase):\n"
        "    def test_ok(self):\n"
        "        self.assertTrue(True)\n",
        encoding="utf-8",
    )
    provider = MemoryWorkflowProvider()
    service = memory_workflow_service(tmp_path, provider)

    def fail_if_read(*_args: object, **_kwargs: object) -> list[object]:
        raise AssertionError("workflow must not use the legacy memory query")

    def fail_if_write(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("workflow must not use the legacy memory writer")

    monkeypatch.setattr(service, "query_memories", fail_if_read)
    monkeypatch.setattr(service, "save_memory", fail_if_write)

    events = await consume_with_expected_unittest_approval(
        service,
        SequentialCodingWorkflow(service).run(
            task="Fix calculator",
            workspace=tmp_path,
            main_role_id="role_main",
            planner_role_id="role_planner",
            explorer_role_ids=("role_explorer",),
            coder_role_id="role_coder",
            reviewer_role_id="role_reviewer",
        ),
    )

    assert not any(event.event_type == "workflow.memory_candidate" for event in events)
    assert events[-1].event_type == "workflow.completed"


@pytest.mark.asyncio
async def test_workflow_evaluation_memory_controls_skip_reads_and_writeback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "test_sample.py").write_text(
        "import unittest\n\n"
        "class SampleTest(unittest.TestCase):\n"
        "    def test_ok(self):\n"
        "        self.assertTrue(True)\n",
        encoding="utf-8",
    )
    provider = MemoryWorkflowProvider()
    service = memory_workflow_service(tmp_path, provider)

    def fail_if_read(*_args: object, **_kwargs: object) -> list[object]:
        raise AssertionError("memory_enabled=False must not query memory")

    monkeypatch.setattr(service, "query_memories", fail_if_read)
    monkeypatch.setattr(
        service,
        "save_memory",
        lambda *_args, **_kwargs: pytest.fail("memory_enabled=False must not write memory"),
    )
    events = await consume_with_expected_unittest_approval(
        service,
        SequentialCodingWorkflow(service).run(
            task="Fix calculator",
            workspace=tmp_path,
            main_role_id="role_main",
            planner_role_id="role_planner",
            explorer_role_ids=("role_explorer",),
            coder_role_id="role_coder",
            reviewer_role_id="role_reviewer",
            memory_enabled=False,
            memory_project_scope=tmp_path,
            persist_memory_candidates=False,
        ),
    )

    assert events[-1].event_type == "workflow.completed"
    assert not any(event.event_type == "workflow.memory_candidate" for event in events)


@pytest.mark.asyncio
async def test_legacy_workflow_write_switch_requires_plugin_command(tmp_path: Path) -> None:
    service = memory_workflow_service(tmp_path, MemoryWorkflowProvider())
    stream = SequentialCodingWorkflow(service).run(
        task="legacy write request",
        workspace=tmp_path,
        main_role_id="role_main",
        planner_role_id="role_planner",
        coder_role_id="role_coder",
        reviewer_role_id="role_reviewer",
        persist_memory_candidates=True,
    )

    with pytest.raises(ValueError, match="schema_upgrade_required"):
        await anext(stream)
