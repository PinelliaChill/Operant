from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest

from operant.application.service import ApplicationService
from operant.application.workflow import SequentialCodingWorkflow
from operant.domain.memory import MemoryKind, MemoryStatus
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


@pytest.mark.asyncio
async def test_workflow_retrieves_prior_memory_and_generates_candidates(tmp_path: Path) -> None:
    (tmp_path / "test_sample.py").write_text(
        "import unittest\n\n"
        "class SampleTest(unittest.TestCase):\n"
        "    def test_ok(self):\n"
        "        self.assertTrue(True)\n",
        encoding="utf-8",
    )
    provider = MemoryWorkflowProvider()
    service = memory_workflow_service(tmp_path, provider)
    author = service.create_session("role_coder")
    prior = service.save_memory(
        snapshot=author.role_snapshot,
        session_id=author.id,
        kind=MemoryKind.PROJECT,
        content="Fix calculator using the existing unittest convention.",
        project_scope=str(tmp_path),
        source_session_id=author.id,
        source_task="Task A",
        confidence=0.95,
        confirmed=True,
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

    assert prior.content in provider.messages["Planner"][0]
    memory_events = [event for event in events if event.event_type == "workflow.memory_candidate"]
    assert {event.payload["status"] for event in memory_events} == {
        MemoryStatus.ACTIVE.value,
        MemoryStatus.CANDIDATE.value,
    }
    reader = service.create_session("role_planner")
    reused = service.query_memories(
        "unittest calculator",
        snapshot=reader.role_snapshot,
        session_id=reader.id,
        project_scope=str(tmp_path),
    )
    generated = next(memory for memory in reused if memory.id != prior.id)
    assert generated.content == "验证命令：python3 -m unittest -v"
    assert generated.source_session_id is not None
    project_candidates = service.query_memories(
        "Explorer completed",
        snapshot=reader.role_snapshot,
        session_id=reader.id,
        project_scope=str(tmp_path),
        kinds=(MemoryKind.PROJECT,),
        include_candidates=True,
    )
    structure_candidate = next(
        memory
        for memory in project_candidates
        if memory.content.startswith("项目结构与编码约定候选")
    )
    assert structure_candidate.status is MemoryStatus.CANDIDATE
    assert structure_candidate.source_session_id == next(
        event.session_id
        for event in events
        if event.role == "explorer" and event.event_type == "workflow.subtask_result"
    )
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
    author = service.create_session("role_coder")
    prior = service.save_memory(
        snapshot=author.role_snapshot,
        session_id=author.id,
        kind=MemoryKind.PROJECT,
        content="This prior memory must not be read by a disabled evaluation.",
        project_scope=str(tmp_path),
        source_session_id=author.id,
        source_task="seed",
        confidence=0.95,
        confirmed=True,
    )

    def fail_if_read(*_args: object, **_kwargs: object) -> list[object]:
        raise AssertionError("memory_enabled=False must not query project Memory")

    monkeypatch.setattr(service, "query_memories", fail_if_read)
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
            memory_enabled=False,
            memory_project_scope=tmp_path,
            persist_memory_candidates=False,
        )
    ]

    assert events[-1].event_type == "workflow.completed"
    assert all(
        prior.content not in message
        for messages in provider.messages.values()
        for message in messages
    )
    assert not any(event.event_type == "workflow.memory_candidate" for event in events)
