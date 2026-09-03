import asyncio
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest

from operant.application.service import ApplicationService
from operant.application.workflow import SequentialCodingWorkflow
from operant.domain.graph import (
    AttemptResult,
    AttemptSideEffectState,
    GraphRunStatus,
    NodeRunStatus,
)
from operant.domain.messages import Message, ModelResponse, ProviderEvent, ToolDefinition
from operant.domain.models import Budget, ModelProfile, RolePreset, RoleSnapshot
from operant.domain.workflow import WorkflowRunStatus, WorkflowStage
from operant.persistence.graph_team import SQLiteGraphRepository
from operant.persistence.sqlite import SQLiteStore


class CountingProvider:
    def __init__(self, *, block_coder: bool = False) -> None:
        self.calls: list[str] = []
        self.block_coder = block_coder

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
        self.calls.append(snapshot.role_name)
        if self.block_coder and snapshot.role_name == "Coder":
            await asyncio.sleep(10)
        content = (
            "VERDICT: APPROVED"
            if snapshot.role_name == "Reviewer"
            else f"{snapshot.role_name} completed"
        )
        yield ProviderEvent(
            event_type="model.completed",
            response=ModelResponse(content=content, finish_reason="stop"),
        )


def recovery_service(tmp_path: Path, provider: CountingProvider) -> ApplicationService:
    service = ApplicationService(SQLiteStore(tmp_path / "recovery.sqlite3"), provider)
    service.initialize()
    service.add_model_profile(
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
                model_profile_id="model_test",
                budget=Budget(timeout_seconds=20),
            )
        )
    return service


@pytest.mark.asyncio
async def test_resume_skips_committed_write_stage(tmp_path: Path) -> None:
    provider = CountingProvider()
    service = recovery_service(tmp_path, provider)
    workflow = SequentialCodingWorkflow(service)
    stream = workflow.run(
        task="Fix calculator",
        workspace=tmp_path,
        main_role_id="role_main",
        planner_role_id="role_planner",
        explorer_role_ids=("role_explorer",),
        coder_role_id="role_coder",
        reviewer_role_id="role_reviewer",
    )

    workflow_run_id = ""
    async for event in stream:
        workflow_run_id = event.workflow_run_id
        if event.role == "coder" and event.event_type == "workflow.subtask_result":
            break
    await stream.aclose()
    interrupted = service.get_workflow_run(workflow_run_id)
    graph_repository = SQLiteGraphRepository(service.store)
    interrupted_graph = graph_repository.get_run_by_legacy_workflow_run_id(workflow_run_id)
    assert interrupted.status is WorkflowRunStatus.INTERRUPTED
    assert interrupted_graph.status is GraphRunStatus.INTERRUPTED
    coder_node = next(
        node
        for node in graph_repository.list_node_runs(interrupted_graph.id)
        if node.node_id == "coder"
    )
    coder_attempt = graph_repository.list_attempts(coder_node.id)[-1]
    assert coder_node.status is NodeRunStatus.SUCCEEDED
    assert coder_attempt.side_effect_state is AttemptSideEffectState.COMMITTED
    assert interrupted.current_stage is WorkflowStage.CODER
    assert provider.calls == ["Planner", "Explorer", "Coder"]

    resumed_events = [event async for event in workflow.resume(workflow_run_id)]
    resumed = service.get_workflow_run(resumed_events[-1].workflow_run_id)

    assert resumed.status is WorkflowRunStatus.COMPLETED
    resumed_graph = graph_repository.get_run_by_legacy_workflow_run_id(resumed.id)
    assert resumed_graph.status is GraphRunStatus.COMPLETED
    assert resumed_graph.input["resumed_from_id"] == workflow_run_id
    assert resumed.resumed_from_id == workflow_run_id
    assert provider.calls == ["Planner", "Explorer", "Coder", "Reviewer", "Main"]
    assert resumed_events[-1].event_type == "workflow.completed"


@pytest.mark.asyncio
async def test_unknown_coder_write_is_not_replayed_automatically(tmp_path: Path) -> None:
    provider = CountingProvider(block_coder=True)
    service = recovery_service(tmp_path, provider)
    workflow = SequentialCodingWorkflow(service)
    stream = workflow.run(
        task="Fix calculator",
        workspace=tmp_path,
        planner_role_id="role_planner",
        coder_role_id="role_coder",
        reviewer_role_id="role_reviewer",
    )

    workflow_run_id = ""
    async for event in stream:
        workflow_run_id = event.workflow_run_id
        if event.role == "coder" and event.event_type == "agent.started":
            break
    await stream.aclose()
    coder_calls_before_resume = provider.calls.count("Coder")

    graph_repository = SQLiteGraphRepository(service.store)
    graph_run = graph_repository.get_run_by_legacy_workflow_run_id(workflow_run_id)
    coder_node = next(
        node for node in graph_repository.list_node_runs(graph_run.id) if node.node_id == "coder"
    )
    coder_attempt = graph_repository.list_attempts(coder_node.id)[-1]
    assert graph_run.status is GraphRunStatus.MANUAL_RECONCILE_REQUIRED
    assert coder_node.status is NodeRunStatus.MANUAL_RECONCILE_REQUIRED
    assert coder_attempt.result is AttemptResult.INTERRUPTED
    assert coder_attempt.side_effect_state is AttemptSideEffectState.UNKNOWN

    with pytest.raises(ValueError, match="coder outcome is unknown"):
        async for _ in workflow.resume(workflow_run_id):
            pass
    blocked = service.get_workflow_run(workflow_run_id)
    assert blocked.status is WorkflowRunStatus.MANUAL_RECONCILE_REQUIRED
    assert provider.calls.count("Coder") == coder_calls_before_resume
