from __future__ import annotations

import hashlib
import os
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest

from operant.application.evaluation import (
    EvaluationRunner,
    execute_verification,
    project_role_snapshot,
)
from operant.application.service import ApplicationService
from operant.domain.evaluation import (
    EnvironmentSnapshot,
    EvaluationCase,
    EvaluationExperiment,
    EvaluationResultStatus,
    EvaluationSuite,
    EvaluationSuiteStatus,
    EvaluationVariant,
    EvaluationVariantKind,
    ExecutionRunner,
    ExecutionSnapshot,
    MemoryReference,
    MemorySnapshot,
    ModelPricing,
    VerificationCommand,
)
from operant.domain.memory import MemoryKind
from operant.domain.messages import (
    Message,
    ModelResponse,
    ModelUsage,
    ProviderEvent,
    ToolDefinition,
)
from operant.domain.models import Budget, ModelProfile, RolePreset, RoleSnapshot
from operant.persistence.sqlite import SQLiteStore


class CapturingProvider:
    """A deterministic, no-network provider that keeps only test-local prompts."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def list_models(self, *, base_url: str, secret_ref: str) -> list[str]:
        del base_url, secret_ref
        return ["eval-model"]

    async def stream(
        self,
        *,
        snapshot: RoleSnapshot,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        del snapshot, tools
        self.messages.append(messages[-1].content or "")
        yield ProviderEvent(
            event_type="model.completed",
            response=ModelResponse(
                content="deterministic evaluation completion",
                finish_reason="stop",
                usage=ModelUsage(prompt_tokens=3, completion_tokens=5, total_tokens=8),
            ),
        )


def _service(tmp_path: Path, provider: CapturingProvider) -> ApplicationService:
    service = ApplicationService(SQLiteStore(tmp_path / "evaluation.sqlite3"), provider)
    service.initialize()
    service.add_model_profile(
        ModelProfile(
            id="model_eval",
            name="evaluation model",
            model_id="eval-model",
            base_url="https://relay.example.com/v1",
            secret_ref="OPERANT_TEST_KEY",
        )
    )
    service.create_role(
        RolePreset(
            id="role_eval",
            name="Evaluator",
            system_prompt="You are a deterministic evaluator.",
            model_profile_id="model_eval",
            memory_scope="read: [project]; write: [project]",
            budget=Budget(timeout_seconds=10),
        )
    )
    return service


def _fixture(source: Path) -> None:
    source.mkdir()
    (source / "test_ok.py").write_text(
        "import unittest\n\n"
        "class TestFixture(unittest.TestCase):\n"
        "    def test_ok(self):\n"
        "        self.assertTrue(True)\n",
        encoding="utf-8",
    )


def _case(source: Path) -> EvaluationCase:
    return EvaluationCase(
        id="case_eval",
        name="isolated fixture",
        task="Verify the isolated fixture.",
        environment=EnvironmentSnapshot(fixture_ref=str(source.resolve())),
        verification_commands=(VerificationCommand(argv=("python3", "-m", "unittest", "-q")),),
        validation_timeout_seconds=20,
    )


def _role_snapshot(service: ApplicationService, pricing: ModelPricing):
    return project_role_snapshot(
        service.create_session("role_eval").role_snapshot,
        prices=(pricing,),
    )


@pytest.mark.asyncio
async def test_runner_isolates_secrets_and_captures_memory_metrics_and_cost(tmp_path: Path) -> None:
    source = tmp_path / "fixture"
    _fixture(source)
    (source / ".env").write_text("OPERANT_KEY=do-not-copy", encoding="utf-8")
    (source / ".ENV.PRODUCTION").write_text("OPERANT_KEY=uppercase", encoding="utf-8")
    (source / "secrets.json").write_text('{"token":"do-not-copy"}', encoding="utf-8")
    lowercase = source / "lowercase"
    (lowercase / ".venv").mkdir(parents=True)
    uppercase_runtime_names = (
        ".OPERANT",
        ".VENV",
        "NODE_MODULES",
        ".PYTEST_CACHE",
    )
    for name in uppercase_runtime_names:
        directory = source / name
        directory.mkdir()
        (directory / "must-not-copy.txt").write_text("local data", encoding="utf-8")
    external = tmp_path / "outside.txt"
    external.write_text("outside", encoding="utf-8")
    os.symlink(external, source / "outside_link")

    provider = CapturingProvider()
    service = _service(tmp_path, provider)
    author = service.create_session("role_eval")
    memory = service.save_memory(
        snapshot=author.role_snapshot,
        session_id=author.id,
        kind=MemoryKind.PROJECT,
        content="Use the source fixture's unittest convention.",
        project_scope=str(source.resolve()),
        source_session_id=author.id,
        source_task="seed memory",
        confidence=0.95,
        confirmed=True,
    )
    pricing = ModelPricing(
        model_profile_id="model_eval",
        model_id="eval-model",
        input_usd_per_million_tokens=2.0,
        output_usd_per_million_tokens=4.0,
    )
    expected_role = _role_snapshot(service, pricing)
    reference = MemoryReference(
        memory_id=memory.id,
        version=memory.version,
        content_sha256=hashlib.sha256(memory.content.encode("utf-8")).hexdigest(),
    )
    suite = EvaluationSuite(
        id="suite_session_memory",
        name="session memory comparison",
        experiment=EvaluationExperiment.EXP_23,
        status=EvaluationSuiteStatus.READY,
        cases=(_case(source),),
        variants=(
            EvaluationVariant(
                id="variant_memory_on",
                name="memory on",
                kind=EvaluationVariantKind.SESSION,
                session_role=expected_role,
                memory=MemorySnapshot(enabled=True, references=(reference,)),
                execution=ExecutionSnapshot(runner=ExecutionRunner.HOST, docker_image=None),
            ),
            EvaluationVariant(
                id="variant_memory_off",
                name="memory off",
                kind=EvaluationVariantKind.SESSION,
                session_role=expected_role,
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

    results = {
        result.variant_id: result
        for result in service.list_evaluation_results(events[0].evaluation_run_id)
    }
    on = results["variant_memory_on"]
    off = results["variant_memory_off"]
    assert on.metrics.task_succeeded is True
    assert on.metrics.tests_passed is True
    assert on.metrics.first_attempt_succeeded is True
    assert on.metrics.prompt_tokens == 3
    assert on.metrics.completion_tokens == 5
    assert on.metrics.total_tokens == 8
    assert on.metrics.cost_usd == pytest.approx(0.000026)
    assert on.snapshot is not None
    assert on.snapshot.memory.references == (reference,)
    assert off.snapshot is not None
    assert off.snapshot.memory.enabled is False
    assert any(memory.content in message for message in provider.messages)
    assert any(
        "Use the source fixture's unittest convention." not in message
        for message in provider.messages
    )

    assert on.artifact_workspace is not None
    artifact = Path(on.artifact_workspace.local_workspace_path or "")
    assert artifact.is_dir()
    assert not (artifact / ".env").exists()
    assert not (artifact / ".ENV.PRODUCTION").exists()
    assert not (artifact / "secrets.json").exists()
    assert not (artifact / "lowercase" / ".venv").exists()
    for name in uppercase_runtime_names:
        assert not (artifact / name).exists()
    assert not (artifact / "outside_link").exists()
    assert (source / ".env").read_text(encoding="utf-8") == "OPERANT_KEY=do-not-copy"
    assert any(fact.name == "skipped_external_symlink_count" for fact in on.execution_facts)
    assert on.verification[0].exit_code == 0
    assert on.verification[0].output_sha256 is not None
    assert [event.event_type for event in events][-1] == "evaluation.run_finished"


@pytest.mark.asyncio
async def test_runner_cost_is_unknown_when_pricing_is_missing(tmp_path: Path) -> None:
    source = tmp_path / "fixture"
    _fixture(source)
    provider = CapturingProvider()
    service = _service(tmp_path, provider)
    expected_role = _role_snapshot(
        service,
        ModelPricing(model_profile_id="model_eval", model_id="eval-model"),
    )
    suite = EvaluationSuite(
        id="suite_unknown_cost",
        name="unknown cost",
        experiment=EvaluationExperiment.EXP_20,
        status=EvaluationSuiteStatus.READY,
        cases=(_case(source),),
        variants=(
            EvaluationVariant(
                id="variant_unknown_cost",
                name="unknown cost",
                kind=EvaluationVariantKind.SESSION,
                session_role=expected_role,
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

    result = service.list_evaluation_results(events[0].evaluation_run_id)[0]
    assert result.metrics.cost_usd is None
    run = service.get_evaluation_run(events[0].evaluation_run_id)
    assert run.aggregate is not None
    assert run.aggregate.total_cost_usd is None


@pytest.mark.asyncio
async def test_runner_persists_pending_before_execution_and_updates_the_same_row(
    tmp_path: Path,
) -> None:
    source = tmp_path / "fixture"
    _fixture(source)
    provider = CapturingProvider()
    service = _service(tmp_path, provider)
    expected_role = _role_snapshot(
        service,
        ModelPricing(model_profile_id="model_eval", model_id="eval-model"),
    )
    suite = EvaluationSuite(
        id="suite_pending_lifecycle",
        name="pending lifecycle",
        experiment=EvaluationExperiment.EXP_19,
        status=EvaluationSuiteStatus.READY,
        cases=(_case(source),),
        variants=(
            EvaluationVariant(
                id="variant_pending_lifecycle",
                name="pending lifecycle",
                kind=EvaluationVariantKind.SESSION,
                session_role=expected_role,
                memory=MemorySnapshot(enabled=False),
                execution=ExecutionSnapshot(runner=ExecutionRunner.HOST, docker_image=None),
            ),
        ),
    )
    service.create_evaluation_suite(suite)

    stream = EvaluationRunner(service).run_suite(
        suite.id,
        artifact_root=tmp_path / "artifacts",
    )
    run_started = await anext(stream)
    result_started = await anext(stream)
    pending_rows = service.list_evaluation_results(run_started.evaluation_run_id)

    assert result_started.event_type == "evaluation.result_started"
    assert result_started.result_id is not None
    assert len(pending_rows) == 1
    pending = pending_rows[0]
    assert pending.id == result_started.result_id
    assert pending.status is EvaluationResultStatus.PENDING
    assert pending.snapshot is None
    assert pending.artifact_workspace is not None
    assert pending.artifact_workspace.artifact_ref == (
        f"evaluation/{run_started.evaluation_run_id}/case_eval/variant_pending_lifecycle/1"
    )

    finished_events = [event async for event in stream]
    results = service.list_evaluation_results(run_started.evaluation_run_id)
    assert len(results) == 1
    assert results[0].id == pending.id
    assert results[0].status is EvaluationResultStatus.PASSED
    assert results[0].artifact_workspace is not None
    assert results[0].artifact_workspace.artifact_ref == pending.artifact_workspace.artifact_ref
    assert [event.event_type for event in finished_events] == [
        "evaluation.result_finished",
        "evaluation.run_finished",
    ]


@pytest.mark.asyncio
async def test_closing_a_started_run_updates_its_pending_row_to_interrupted(
    tmp_path: Path,
) -> None:
    source = tmp_path / "fixture"
    _fixture(source)
    provider = CapturingProvider()
    service = _service(tmp_path, provider)
    expected_role = _role_snapshot(
        service,
        ModelPricing(model_profile_id="model_eval", model_id="eval-model"),
    )
    suite = EvaluationSuite(
        id="suite_cancel_pending",
        name="cancel pending",
        experiment=EvaluationExperiment.EXP_19,
        status=EvaluationSuiteStatus.READY,
        cases=(_case(source),),
        variants=(
            EvaluationVariant(
                id="variant_cancel_pending",
                name="cancel pending",
                kind=EvaluationVariantKind.SESSION,
                session_role=expected_role,
                memory=MemorySnapshot(enabled=False),
                execution=ExecutionSnapshot(runner=ExecutionRunner.HOST, docker_image=None),
            ),
        ),
    )
    service.create_evaluation_suite(suite)

    stream = EvaluationRunner(service).run_suite(
        suite.id,
        artifact_root=tmp_path / "artifacts",
    )
    run_started = await anext(stream)
    result_started = await anext(stream)
    await stream.aclose()

    run = service.get_evaluation_run(run_started.evaluation_run_id)
    results = service.list_evaluation_results(run.id)
    assert run.status.value == "interrupted"
    assert run.aggregate is not None
    assert run.aggregate.interrupted_result_count == 1
    assert run.aggregate.unknown_result_count == 0
    assert len(results) == 1
    assert results[0].id == result_started.result_id
    assert results[0].status is EvaluationResultStatus.INTERRUPTED
    assert results[0].snapshot is None
    assert provider.messages == []


@pytest.mark.asyncio
async def test_reopen_marks_scheduled_pending_result_interrupted_after_session_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "fixture"
    _fixture(source)
    provider = CapturingProvider()
    service = _service(tmp_path, provider)
    expected_role = _role_snapshot(
        service,
        ModelPricing(model_profile_id="model_eval", model_id="eval-model"),
    )
    suite = EvaluationSuite(
        id="suite_hard_interrupt",
        name="hard interruption",
        experiment=EvaluationExperiment.EXP_19,
        status=EvaluationSuiteStatus.READY,
        cases=(_case(source),),
        variants=(
            EvaluationVariant(
                id="variant_hard_interrupt",
                name="hard interruption",
                kind=EvaluationVariantKind.SESSION,
                session_role=expected_role,
                memory=MemorySnapshot(enabled=False),
                execution=ExecutionSnapshot(runner=ExecutionRunner.HOST, docker_image=None),
            ),
        ),
    )
    service.create_evaluation_suite(suite)

    class HardInterruption(BaseException):
        pass

    def stop_terminal_update(*_args: object, **_changes: object) -> None:
        raise HardInterruption

    monkeypatch.setattr(service, "update_evaluation_result", stop_terminal_update)
    run_id = ""
    with pytest.raises(HardInterruption):
        async for event in EvaluationRunner(service).run_suite(
            suite.id,
            artifact_root=tmp_path / "artifacts",
        ):
            run_id = event.evaluation_run_id

    assert run_id
    assert provider.messages
    pending = service.list_evaluation_results(run_id)
    assert len(pending) == 1
    assert pending[0].status is EvaluationResultStatus.PENDING
    assert service.get_evaluation_run(run_id).status.value == "running"

    reopened = SQLiteStore(service.store.path)
    reopened.initialize()
    recovered_run = reopened.get_evaluation_run(run_id)
    recovered = reopened.list_evaluation_results(run_id)

    assert recovered_run.status.value == "interrupted"
    assert recovered_run.aggregate is not None
    assert recovered_run.aggregate.expected_result_count == suite.expanded_result_count
    assert recovered_run.aggregate.interrupted_result_count == 1
    assert recovered_run.aggregate.unknown_result_count == 0
    assert len(recovered) == 1
    assert recovered[0].id == pending[0].id
    assert recovered[0].status is EvaluationResultStatus.INTERRUPTED
    assert recovered[0].snapshot is None
    assert recovered[0].metrics.total_tokens is None
    assert recovered[0].metrics.task_succeeded is None
    assert recovered[0].artifact_workspace == pending[0].artifact_workspace
    assert recovered[0].failure_analysis is not None
    assert recovered[0].failure_analysis.reason_code == "orchestration.evaluation_interrupted"


@pytest.mark.asyncio
async def test_verification_timeout_uses_argv_execution_and_records_no_raw_output(
    tmp_path: Path,
) -> None:
    (tmp_path / "test_hang.py").write_text(
        "import time\nimport unittest\n\n"
        "class TestHang(unittest.TestCase):\n"
        "    def test_hang(self):\n"
        "        time.sleep(10)\n",
        encoding="utf-8",
    )
    observation = await execute_verification(
        VerificationCommand(
            argv=("python3", "-m", "unittest", "test_hang"),
            timeout_seconds=1,
        ),
        workspace=tmp_path,
        default_timeout_seconds=20,
    )

    assert observation.timed_out is True
    assert observation.exit_code is None
    assert observation.output_sha256 is None


@pytest.mark.asyncio
async def test_role_snapshot_drift_becomes_complete_error_result(tmp_path: Path) -> None:
    source = tmp_path / "fixture"
    _fixture(source)
    provider = CapturingProvider()
    service = _service(tmp_path, provider)
    expected_role = _role_snapshot(
        service,
        ModelPricing(model_profile_id="model_eval", model_id="eval-model"),
    )
    suite = EvaluationSuite(
        id="suite_role_drift",
        name="role drift",
        experiment=EvaluationExperiment.EXP_21,
        status=EvaluationSuiteStatus.READY,
        cases=(_case(source),),
        variants=(
            EvaluationVariant(
                id="variant_role_drift",
                name="role drift",
                kind=EvaluationVariantKind.SESSION,
                session_role=expected_role,
                memory=MemorySnapshot(enabled=False),
                execution=ExecutionSnapshot(runner=ExecutionRunner.HOST, docker_image=None),
            ),
        ),
    )
    service.create_evaluation_suite(suite)
    service.update_role("role_eval", system_prompt="This changed after suite creation.")

    events = [
        event
        async for event in EvaluationRunner(service).run_suite(
            suite.id,
            artifact_root=tmp_path / "artifacts",
        )
    ]

    result = service.list_evaluation_results(events[0].evaluation_run_id)[0]
    assert result.status is EvaluationResultStatus.ERROR
    assert result.snapshot is not None
    actual_role = result.snapshot.roles[0]
    assert actual_role.role_id == expected_role.role_id
    assert actual_role.role_version > expected_role.role_version
    assert actual_role.prompt.content_sha256 != expected_role.prompt.content_sha256
    assert result.artifact_workspace is not None
    assert result.verification[0].argv == ("python3", "-m", "unittest", "-q")
    assert result.verification[0].exit_code is None
    assert result.failure_analysis is not None
    assert result.failure_analysis.reason_code == "orchestration.role_snapshot_drift"
    assert provider.messages == []
