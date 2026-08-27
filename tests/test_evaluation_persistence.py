from datetime import datetime, timezone
from pathlib import Path

import pytest

from operant.application.service import ApplicationService
from operant.domain.evaluation import (
    ArtifactWorkspace,
    EnvironmentFact,
    EnvironmentSnapshot,
    EvaluationMetrics,
    EvaluationResult,
    EvaluationResultSnapshot,
    EvaluationResultStatus,
    EvaluationRoleSnapshot,
    EvaluationRun,
    EvaluationRunStatus,
    EvaluationSuite,
    EvaluationSuiteStatus,
    EvaluationVariant,
    ExecutionSnapshot,
    MemoryReference,
    MemorySnapshot,
    ModelSnapshot,
    PromptSnapshot,
    VerificationCommand,
    VerificationOutcome,
    interrupted_evaluation_failure_analysis,
)
from operant.persistence.sqlite import ConflictError, NotFoundError, SQLiteStore
from operant.providers.openai_compatible import OpenAICompatibleProvider


def make_environment() -> EnvironmentSnapshot:
    return EnvironmentSnapshot(
        fixture_ref="examples/buggy_calculator",
        source_revision="e8d6485",
        facts=(EnvironmentFact(name="python_version", value="3.13.3"),),
    )


def make_role() -> EvaluationRoleSnapshot:
    return EvaluationRoleSnapshot(
        role_id="role_coder",
        role_version=1,
        role_name="Coder",
        prompt=PromptSnapshot(id="prompt_coder", content="Repair the requested fixture."),
        model=ModelSnapshot(
            model_profile_id="model_coder",
            provider="openai-compatible",
            model_id="coder-v1",
            effort="medium",
        ),
        tool_policy_fingerprint="a" * 64,
    )


def make_execution() -> ExecutionSnapshot:
    return ExecutionSnapshot(runner="docker", docker_image="python:3.13-slim")


def make_suite() -> EvaluationSuite:
    environment = make_environment()
    role = make_role()
    variant = EvaluationVariant(
        id="variant_coder",
        name="single coder",
        kind="session",
        session_role=role,
        # A suite may turn memory on without pinning references.  The runner
        # then captures the exact references it actually used in each result.
        memory=MemorySnapshot(enabled=True),
        execution=make_execution(),
    )
    return EvaluationSuite(
        id="suite_eval",
        name="calculator evaluation",
        status=EvaluationSuiteStatus.READY,
        cases=(
            {
                "id": "case_calculator",
                "name": "calculator repair",
                "task": "Repair floating-point division.",
                "environment": environment,
                "verification_commands": (
                    VerificationCommand(argv=("python", "-m", "unittest", "-v")),
                    VerificationCommand(argv=("git", "diff", "--check")),
                ),
                "allowed_changed_paths": ("calculator.py",),
                "expected_changed_paths": ("calculator.py",),
            },
        ),
        variants=(variant,),
        repetitions=2,
    )


def make_passing_result(evaluation_run: EvaluationRun, suite: EvaluationSuite) -> EvaluationResult:
    case = suite.cases[0]
    variant = suite.variants[0]
    assert variant.session_role is not None
    return EvaluationResult(
        id="result_first",
        run_id=evaluation_run.id,
        case_id=case.id,
        variant_id=variant.id,
        repetition=1,
        status=EvaluationResultStatus.PASSED,
        metrics=EvaluationMetrics(
            task_succeeded=True,
            tests_passed=True,
            verification_passed=True,
            patch_accuracy=1.0,
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            cost_usd=0.001,
            latency_ms=20,
            tool_failures=0,
            approval_requests=0,
            approvals_approved=0,
            approvals_denied=0,
        ),
        changed_paths=("calculator.py",),
        snapshot=EvaluationResultSnapshot(
            roles=(variant.session_role,),
            memory=MemorySnapshot(
                enabled=True,
                references=(
                    MemoryReference(
                        memory_id="memory_actual",
                        version=3,
                        content_sha256="d" * 64,
                    ),
                ),
            ),
            environment=case.environment.model_copy(
                update={
                    "fixture_ref": "examples/buggy_calculator/",
                    "fixture_sha256": "e" * 64,
                    "facts": (
                        *case.environment.facts,
                        EnvironmentFact(name="os", value="linux"),
                    ),
                }
            ),
            execution=variant.execution,
        ),
        artifact_workspace=ArtifactWorkspace(artifact_ref="artifact_result_first"),
        verification=(
            VerificationOutcome(
                argv=("python", "-m", "unittest", "-v"),
                exit_code=0,
                timed_out=False,
                duration_ms=10,
                output_sha256="b" * 64,
                output_chars=64,
                output_truncated=True,
            ),
            VerificationOutcome(
                argv=("git", "diff", "--check"),
                exit_code=0,
                timed_out=False,
                duration_ms=2,
                output_sha256="c" * 64,
                output_chars=0,
                output_truncated=False,
            ),
        ),
        execution_facts=(EnvironmentFact(name="runner", value="docker"),),
    )


def test_evaluation_store_crud_results_and_snapshot_contract(tmp_path: Path) -> None:
    database = tmp_path / "evaluation.sqlite3"
    store = SQLiteStore(database)
    store.initialize()
    store.initialize()  # Schema creation and restart reconciliation are idempotent.
    service = ApplicationService(store, OpenAICompatibleProvider())
    suite = make_suite()

    assert service.create_evaluation_suite(suite) == suite
    assert service.get_evaluation_suite(suite.id) == suite
    assert service.list_evaluation_suites(status="ready") == [suite]
    with pytest.raises(ConflictError):
        service.create_evaluation_suite(suite)

    evaluation_run = EvaluationRun(id="run_eval", suite_id=suite.id)
    assert service.create_evaluation_run(evaluation_run) == evaluation_run
    assert service.get_evaluation_run(evaluation_run.id) == evaluation_run
    running = service.update_evaluation_run(
        evaluation_run.id,
        status="running",
        started_at=datetime.now(timezone.utc),
    )
    assert running.status is EvaluationRunStatus.RUNNING
    assert service.list_evaluation_runs(suite_id=suite.id, status="running") == [running]

    result = make_passing_result(running, suite)
    assert service.append_evaluation_result(result) == result
    assert service.list_evaluation_results(running.id) == [result]

    beyond_repetitions = result.model_copy(
        update={"id": "result_beyond_repetitions", "repetition": suite.repetitions + 1}
    )
    with pytest.raises(ValueError, match="repetition exceeds"):
        service.append_evaluation_result(beyond_repetitions)

    forged_pass = result.model_copy(
        update={
            "id": "result_forged_pass",
            "repetition": 2,
            "metrics": result.metrics.model_copy(update={"verification_passed": None}),
        }
    )
    with pytest.raises(ValueError, match="successful task and verification metrics"):
        service.append_evaluation_result(forged_pass)

    wrong_verification = result.model_copy(
        update={
            "id": "result_wrong_verification",
            "repetition": 2,
            "verification": result.verification[:1],
        }
    )
    with pytest.raises(ValueError, match="verification outcomes must match"):
        service.append_evaluation_result(wrong_verification)

    duplicate = result.model_copy(update={"id": "result_duplicate"})
    with pytest.raises(ConflictError):
        service.append_evaluation_result(duplicate)
    drifted = result.model_copy(
        update={
            "id": "result_drifted",
            "repetition": 2,
            "snapshot": result.snapshot.model_copy(
                update={
                    "roles": (
                        result.snapshot.roles[0].model_copy(
                            update={"role_version": result.snapshot.roles[0].role_version + 1}
                        ),
                    )
                }
            ),
        }
    )
    with pytest.raises(ValueError, match="role snapshot drift"):
        service.append_evaluation_result(drifted)
    with pytest.raises(NotFoundError):
        service.append_evaluation_result(result.model_copy(update={"run_id": "missing_run"}))

    pending = EvaluationResult(
        id="result_pending",
        run_id=running.id,
        case_id=suite.cases[0].id,
        variant_id=suite.variants[0].id,
        repetition=2,
        artifact_workspace=ArtifactWorkspace(artifact_ref="artifact_result_pending"),
    )
    assert service.append_evaluation_result(pending) == pending
    assert service.get_evaluation_result(pending.id) == pending
    with pytest.raises(ValueError, match="verification outcomes must match"):
        service.update_evaluation_result(
            pending.id,
            status=EvaluationResultStatus.PASSED,
            metrics=result.metrics,
            changed_paths=result.changed_paths,
            snapshot=result.snapshot,
            artifact_workspace=ArtifactWorkspace(artifact_ref="artifact_result_pending"),
            verification=result.verification[:1],
            execution_facts=result.execution_facts,
            finished_at=datetime.now(timezone.utc),
        )
    assert service.get_evaluation_result(pending.id).status is EvaluationResultStatus.PENDING
    with pytest.raises(ValueError, match="must transition from pending to a terminal status"):
        service.update_evaluation_result(pending.id, status=EvaluationResultStatus.PENDING)
    completed_pending = service.update_evaluation_result(
        pending.id,
        status=EvaluationResultStatus.PASSED,
        metrics=result.metrics,
        changed_paths=result.changed_paths,
        snapshot=result.snapshot,
        artifact_workspace=ArtifactWorkspace(artifact_ref="artifact_result_pending"),
        verification=result.verification,
        execution_facts=result.execution_facts,
        finished_at=datetime.now(timezone.utc),
    )
    assert completed_pending.status is EvaluationResultStatus.PASSED
    assert service.get_evaluation_result(pending.id) == completed_pending
    with pytest.raises(ValueError, match="only be updated once from pending"):
        service.update_evaluation_result(
            pending.id,
            status=EvaluationResultStatus.PASSED,
            metrics=completed_pending.metrics.model_copy(update={"total_tokens": 999}),
            finished_at=datetime.now(timezone.utc),
        )
    with pytest.raises(ValueError, match="only be updated once from pending"):
        service.update_evaluation_result(pending.id, repetition=1)


@pytest.mark.parametrize(
    "status",
    (
        EvaluationResultStatus.PASSED,
        EvaluationResultStatus.FAILED,
        EvaluationResultStatus.ERROR,
        EvaluationResultStatus.SKIPPED,
        EvaluationResultStatus.INTERRUPTED,
    ),
)
def test_evaluation_result_update_allows_each_pending_to_terminal_status(
    tmp_path: Path, status: EvaluationResultStatus
) -> None:
    store = SQLiteStore(tmp_path / f"evaluation-{status.value}.sqlite3")
    store.initialize()
    service = ApplicationService(store, OpenAICompatibleProvider())
    suite = service.create_evaluation_suite(make_suite())
    evaluation_run = service.create_evaluation_run(
        EvaluationRun(id=f"run_{status.value}", suite_id=suite.id)
    )
    pending = service.append_evaluation_result(
        EvaluationResult(
            id=f"result_{status.value}",
            run_id=evaluation_run.id,
            case_id=suite.cases[0].id,
            variant_id=suite.variants[0].id,
            repetition=1,
            artifact_workspace=ArtifactWorkspace(artifact_ref=f"artifact_{status.value}"),
        )
    )
    finished_at = datetime.now(timezone.utc)
    passing = make_passing_result(evaluation_run, suite)
    if status is EvaluationResultStatus.INTERRUPTED:
        changes: dict[str, object] = {
            "status": status,
            "failure_analysis": interrupted_evaluation_failure_analysis(),
            "finished_at": finished_at,
        }
    elif status is EvaluationResultStatus.SKIPPED:
        changes = {"status": status, "finished_at": finished_at}
    else:
        metrics = passing.metrics
        if status is EvaluationResultStatus.FAILED:
            metrics = metrics.model_copy(
                update={
                    "task_succeeded": False,
                    "tests_passed": False,
                    "verification_passed": False,
                }
            )
        elif status is EvaluationResultStatus.ERROR:
            metrics = EvaluationMetrics()
        changes = {
            "status": status,
            "metrics": metrics,
            "changed_paths": passing.changed_paths,
            "snapshot": passing.snapshot,
            "verification": passing.verification,
            "execution_facts": passing.execution_facts,
            "finished_at": finished_at,
        }

    updated = service.update_evaluation_result(pending.id, **changes)

    assert updated.id == pending.id
    assert updated.status is status
    assert service.get_evaluation_result(pending.id) == updated


def test_evaluation_running_runs_become_interrupted_after_reopen(tmp_path: Path) -> None:
    database = tmp_path / "evaluation-recovery.sqlite3"
    store = SQLiteStore(database)
    store.initialize()
    suite = store.create_evaluation_suite(make_suite())
    running = store.create_evaluation_run(
        EvaluationRun(
            id="run_running",
            suite_id=suite.id,
            status=EvaluationRunStatus.RUNNING,
            started_at=datetime.now(timezone.utc),
        )
    )
    completed = store.create_evaluation_run(
        EvaluationRun(
            id="run_completed",
            suite_id=suite.id,
            status=EvaluationRunStatus.COMPLETED,
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
        )
    )
    pending = store.append_evaluation_result(
        EvaluationResult(
            id="result_recovery_pending",
            run_id=running.id,
            case_id=suite.cases[0].id,
            variant_id=suite.variants[0].id,
            repetition=1,
            artifact_workspace=ArtifactWorkspace(
                artifact_ref="evaluation/run_running/case_calculator/variant_coder/1"
            ),
        )
    )

    reopened = SQLiteStore(database)
    reopened.initialize()
    recovered_run = reopened.get_evaluation_run(running.id)
    assert recovered_run.status is EvaluationRunStatus.INTERRUPTED
    assert recovered_run.finished_at is not None
    assert recovered_run.aggregate is not None
    assert recovered_run.aggregate.expected_result_count == suite.expanded_result_count
    assert recovered_run.aggregate.result_count == 1
    assert recovered_run.aggregate.interrupted_result_count == 1
    assert recovered_run.aggregate.unknown_result_count == suite.expanded_result_count - 1
    assert reopened.get_evaluation_run(completed.id).status is EvaluationRunStatus.COMPLETED
    recovered_results = reopened.list_evaluation_results(running.id)
    assert len(recovered_results) == 1
    recovered = recovered_results[0]
    assert recovered.id == pending.id
    assert recovered.status is EvaluationResultStatus.INTERRUPTED
    assert recovered.snapshot is None
    assert recovered.metrics.task_succeeded is None
    assert recovered.metrics.total_tokens is None
    assert recovered.artifact_workspace == pending.artifact_workspace
    assert recovered.finished_at is not None
    assert recovered.failure_analysis is not None
    assert recovered.failure_analysis.reason_code == "orchestration.evaluation_interrupted"
    with pytest.raises(ValueError, match="only be updated once from pending"):
        reopened.update_evaluation_result(recovered.id, status=EvaluationResultStatus.PENDING)

    before_second_reopen = reopened.get_evaluation_result(recovered.id)
    reopened.initialize()
    assert reopened.get_evaluation_result(recovered.id) == before_second_reopen
