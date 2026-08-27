from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest

from operant.application.evaluation import EvaluationRunner, project_role_snapshot
from operant.application.service import ApplicationService
from operant.domain.evaluation import (
    EnvironmentSnapshot,
    EvaluationCase,
    EvaluationExperiment,
    EvaluationSuite,
    EvaluationSuiteStatus,
    EvaluationVariant,
    EvaluationVariantKind,
    ExecutionRunner,
    ExecutionSnapshot,
    MemorySnapshot,
    VerificationCommand,
    WorkflowSnapshot,
)
from operant.domain.messages import (
    Message,
    ModelResponse,
    ModelUsage,
    ProviderEvent,
    ToolDefinition,
)
from operant.domain.models import Budget, ModelProfile, RolePreset, RoleSnapshot
from operant.domain.workflow import WorkflowRunStatus
from operant.persistence.sqlite import SQLiteStore


class WorkflowProvider:
    def __init__(self) -> None:
        self.stream_calls = 0

    async def list_models(self, *, base_url: str, secret_ref: str) -> list[str]:
        del base_url, secret_ref
        return ["workflow-model"]

    async def stream(
        self,
        *,
        snapshot: RoleSnapshot,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        del messages, tools
        self.stream_calls += 1
        content = "VERDICT: APPROVED" if snapshot.role_name == "Reviewer" else "completed"
        yield ProviderEvent(
            event_type="model.completed",
            response=ModelResponse(
                content=content,
                finish_reason="stop",
                usage=ModelUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            ),
        )


def _workflow_service(
    tmp_path: Path,
    provider: WorkflowProvider | None = None,
) -> ApplicationService:
    service = ApplicationService(
        SQLiteStore(tmp_path / "workflow-evaluation.sqlite3"), provider or WorkflowProvider()
    )
    service.initialize()
    service.add_model_profile(
        ModelProfile(
            id="model_workflow",
            name="workflow model",
            model_id="workflow-model",
            base_url="https://relay.example.com/v1",
            secret_ref="OPERANT_TEST_KEY",
        )
    )
    for role_id, name in (
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
                model_profile_id="model_workflow",
                budget=Budget(timeout_seconds=10),
            )
        )
    return service


@pytest.mark.asyncio
async def test_runner_executes_explicit_workflow_slots_without_memory_writeback(
    tmp_path: Path,
) -> None:
    source = tmp_path / "fixture"
    source.mkdir()
    (source / "test_ok.py").write_text(
        "import unittest\n\n"
        "class TestFixture(unittest.TestCase):\n"
        "    def test_ok(self):\n"
        "        self.assertTrue(True)\n",
        encoding="utf-8",
    )
    service = _workflow_service(tmp_path)
    snapshots = {
        role_id: project_role_snapshot(service.create_session(role_id).role_snapshot)
        for role_id in ("role_planner", "role_explorer", "role_coder", "role_reviewer")
    }
    workflow = WorkflowSnapshot(
        workflow_name="sequential coding workflow",
        workflow_version=1,
        roles=tuple(snapshots.values()),
        planner_role_id="role_planner",
        explorer_role_ids=("role_explorer",),
        coder_role_id="role_coder",
        reviewer_role_id="role_reviewer",
        max_parallel_explorers=1,
        max_rework_rounds=0,
    )
    suite = EvaluationSuite(
        id="suite_workflow",
        name="workflow fixture",
        experiment=EvaluationExperiment.EXP_19,
        status=EvaluationSuiteStatus.READY,
        cases=(
            EvaluationCase(
                id="case_workflow",
                name="workflow case",
                task="Run the fixed coding workflow.",
                environment=EnvironmentSnapshot(fixture_ref=str(source.resolve())),
                verification_commands=(
                    VerificationCommand(argv=("python3", "-m", "unittest", "-q")),
                ),
            ),
        ),
        variants=(
            EvaluationVariant(
                id="variant_workflow",
                name="fixed workflow",
                kind=EvaluationVariantKind.WORKFLOW,
                workflow=workflow,
                memory=MemorySnapshot(enabled=False),
                execution=ExecutionSnapshot(runner=ExecutionRunner.HOST, docker_image=None),
            ),
        ),
    )
    service.create_evaluation_suite(suite)

    events = [
        event
        async for event in EvaluationRunner(service).run_suite(
            suite.id,
            artifact_root=tmp_path / "artifacts",
        )
    ]

    run_id = events[0].evaluation_run_id
    result = service.list_evaluation_results(run_id)[0]
    assert result.metrics.task_succeeded is True
    assert result.trace_workflow_run_id is not None
    workflow_run = service.get_workflow_run(result.trace_workflow_run_id)
    assert workflow_run.status is WorkflowRunStatus.COMPLETED
    assert workflow_run.explorer_role_ids == ("role_explorer",)
    assert result.snapshot is not None
    assert result.snapshot.workflow == workflow
    persisted_events = service.list_workflow_events(result.trace_workflow_run_id)
    assert not any(event.event_type == "workflow.memory_candidate" for event in persisted_events)


@pytest.mark.asyncio
async def test_workflow_role_drift_stops_before_coordinator_and_records_actual_roles(
    tmp_path: Path,
) -> None:
    source = tmp_path / "fixture"
    source.mkdir()
    (source / "test_ok.py").write_text(
        "import unittest\n\n"
        "class TestFixture(unittest.TestCase):\n"
        "    def test_ok(self):\n"
        "        self.assertTrue(True)\n",
        encoding="utf-8",
    )
    provider = WorkflowProvider()
    service = _workflow_service(tmp_path, provider)
    snapshots = {
        role_id: project_role_snapshot(service.create_session(role_id).role_snapshot)
        for role_id in ("role_planner", "role_explorer", "role_coder", "role_reviewer")
    }
    workflow = WorkflowSnapshot(
        workflow_name="sequential coding workflow",
        workflow_version=1,
        roles=tuple(snapshots.values()),
        planner_role_id="role_planner",
        explorer_role_ids=("role_explorer",),
        coder_role_id="role_coder",
        reviewer_role_id="role_reviewer",
        max_parallel_explorers=1,
        max_rework_rounds=0,
    )
    suite = EvaluationSuite(
        id="suite_workflow_role_drift",
        name="workflow role drift",
        experiment=EvaluationExperiment.EXP_19,
        status=EvaluationSuiteStatus.READY,
        cases=(
            EvaluationCase(
                id="case_workflow_role_drift",
                name="workflow role drift case",
                task="Do not run this workflow after role drift.",
                environment=EnvironmentSnapshot(fixture_ref=str(source.resolve())),
                verification_commands=(
                    VerificationCommand(argv=("python3", "-m", "unittest", "-q")),
                ),
            ),
        ),
        variants=(
            EvaluationVariant(
                id="variant_workflow_role_drift",
                name="workflow role drift",
                kind=EvaluationVariantKind.WORKFLOW,
                workflow=workflow,
                memory=MemorySnapshot(enabled=False),
                execution=ExecutionSnapshot(runner=ExecutionRunner.HOST, docker_image=None),
            ),
        ),
    )
    service.create_evaluation_suite(suite)
    service.update_role("role_planner", system_prompt="The planner changed after suite creation.")

    events = [
        event
        async for event in EvaluationRunner(service).run_suite(
            suite.id,
            artifact_root=tmp_path / "artifacts",
        )
    ]

    result = service.list_evaluation_results(events[0].evaluation_run_id)[0]
    assert result.status.value == "error"
    assert result.trace_workflow_run_id is None
    assert result.snapshot is not None
    actual_by_id = {role.role_id: role for role in result.snapshot.roles}
    assert set(actual_by_id) == set(snapshots)
    assert actual_by_id["role_planner"].role_version > snapshots["role_planner"].role_version
    assert (
        actual_by_id["role_planner"].prompt.content_sha256
        != snapshots["role_planner"].prompt.content_sha256
    )
    assert result.failure_analysis is not None
    assert result.failure_analysis.reason_code == "orchestration.role_snapshot_drift"
    assert provider.stream_calls == 0
