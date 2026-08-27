import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from operant.domain.evaluation import (
    ArtifactWorkspace,
    EnvironmentSnapshot,
    EvaluationAggregate,
    EvaluationCase,
    EvaluationMetrics,
    EvaluationResult,
    EvaluationResultSnapshot,
    EvaluationResultStatus,
    EvaluationRoleSnapshot,
    EvaluationSuite,
    EvaluationVariant,
    ExecutionSnapshot,
    FailureAnalysis,
    FailureCategory,
    FailureEvidence,
    MemorySnapshot,
    ModelPricing,
    ModelSnapshot,
    PromptSnapshot,
    VerificationCommand,
    VerificationOutcome,
    WorkflowSnapshot,
    assert_evaluation_result_contract,
    interrupted_evaluation_failure_analysis,
    utc_now,
)


def model_snapshot(*, effort: str = "medium") -> ModelSnapshot:
    return ModelSnapshot(
        model_profile_id="model_coder",
        provider="openai-compatible",
        model_id="coder-v1",
        effort=effort,
        pricing=ModelPricing(
            model_profile_id="model_coder",
            model_id="coder-v1",
            input_usd_per_million_tokens=1.0,
            output_usd_per_million_tokens=2.0,
        ),
    )


def role_snapshot(*, role_id: str = "role_coder") -> EvaluationRoleSnapshot:
    return EvaluationRoleSnapshot(
        role_id=role_id,
        role_version=2,
        role_name="Coder",
        prompt=PromptSnapshot(id=f"prompt_{role_id}", content="Modify only the requested files."),
        model=model_snapshot(),
        tool_policy_fingerprint="a" * 64,
    )


def execution() -> ExecutionSnapshot:
    return ExecutionSnapshot(runner="docker", docker_image="python:3.13-slim")


def workflow_snapshot() -> WorkflowSnapshot:
    return WorkflowSnapshot(
        workflow_name="coding-review",
        roles=(
            role_snapshot(role_id="role_planner"),
            role_snapshot(role_id="role_coder"),
            role_snapshot(role_id="role_reviewer"),
        ),
        planner_role_id="role_planner",
        coder_role_id="role_coder",
        reviewer_role_id="role_reviewer",
    )


def actual_snapshot(*, role: EvaluationRoleSnapshot | None = None) -> EvaluationResultSnapshot:
    return EvaluationResultSnapshot(
        roles=(role or role_snapshot(),),
        memory=MemorySnapshot(enabled=False),
        environment=EnvironmentSnapshot(
            fixture_ref="examples/buggy_calculator",
            source_revision="e8d6485",
        ),
        execution=execution(),
    )


def passed_verification(case_definition: EvaluationCase) -> tuple[VerificationOutcome, ...]:
    return tuple(
        VerificationOutcome(
            argv=command.argv,
            exit_code=0,
            timed_out=False,
            duration_ms=1,
        )
        for command in case_definition.verification_commands
    )


def case(*, case_id: str = "case_one") -> EvaluationCase:
    return EvaluationCase(
        id=case_id,
        name="calculator repair",
        task="Fix division and preserve the public API.",
        environment=EnvironmentSnapshot(
            fixture_ref="examples/buggy_calculator",
            source_revision="e8d6485",
        ),
        verification_commands=(
            VerificationCommand(argv=("python", "-m", "unittest", "-v")),
            VerificationCommand(argv=("ruff", "check", ".")),
        ),
        allowed_changed_paths=("calculator.py", "tests/*.py"),
        expected_changed_paths=("calculator.py",),
    )


def test_suite_supports_week4_ablation_dimensions_and_is_bounded() -> None:
    assert ExecutionSnapshot().docker_image == "python:3.13-slim"
    session_variant = EvaluationVariant(
        id="variant_low_memory_off",
        name="single coder low effort no memory",
        kind="session",
        session_role=role_snapshot(),
        memory={"enabled": False},
        execution=execution(),
    )
    workflow_variant = EvaluationVariant(
        id="variant_workflow",
        name="planner coder reviewer",
        kind="workflow",
        workflow=workflow_snapshot(),
        execution=execution(),
    )
    suite = EvaluationSuite(
        id="suite_exp_19",
        name="single versus multi model",
        experiment="exp_19_single_vs_multi_model",
        cases=(case(),),
        variants=(session_variant, workflow_variant),
        repetitions=3,
    )

    assert suite.expanded_result_count == 6
    assert suite.variants[0].memory.enabled is False
    with pytest.raises(ValidationError):
        suite.name = "mutated"  # type: ignore[misc]
    with pytest.raises(ValidationError, match="forbid workflow"):
        EvaluationVariant(
            id="invalid_variant",
            name="invalid",
            kind="session",
            session_role=role_snapshot(),
            workflow=workflow_variant.workflow,
            execution=execution(),
        )
    too_many_cases = tuple(case(case_id=f"case_{index}") for index in range(51))
    with pytest.raises(ValidationError, match="maximum number"):
        EvaluationSuite(
            id="suite_too_large",
            name="too large",
            cases=too_many_cases,
            variants=(session_variant,),
            repetitions=20,
        )


@pytest.mark.parametrize(
    "argv",
    [
        ("sh", "-c", "pytest"),
        ("curl", "https://example.invalid"),
        ("pytest", "--token=not-a-secret"),
        ("python", "script.py"),
        ("pytest", "../outside"),
    ],
)
def test_verification_argv_rejects_shell_network_secrets_and_arbitrary_executables(
    argv: tuple[str, ...],
) -> None:
    with pytest.raises(ValidationError):
        VerificationCommand(argv=argv)


def test_metrics_aggregation_keeps_unknowns_as_none_and_classifies_trace_failure() -> None:
    known_failure = EvaluationResult(
        id="result_one",
        run_id="run_one",
        case_id="case_one",
        variant_id="variant_one",
        repetition=1,
        status=EvaluationResultStatus.FAILED,
        metrics=EvaluationMetrics(
            task_succeeded=False,
            tests_passed=False,
            repair_turns=2,
            rework_rounds=1,
            prompt_tokens=100,
            completion_tokens=20,
            total_tokens=120,
            cost_usd=0.01,
            latency_ms=50,
            tool_failures=0,
            approval_requests=0,
            approvals_approved=0,
            approvals_denied=0,
        ),
        snapshot=actual_snapshot(),
        artifact_workspace=ArtifactWorkspace(artifact_ref="artifact_result_one"),
        failure_analysis=FailureAnalysis(
            category=FailureCategory.ORCHESTRATION,
            reason_code="orchestration.repeated_failure",
            root_cause="Repeated identical test-failure signature stopped the run.",
            evidence_event_types=("agent.no_progress",),
            evidence=(
                FailureEvidence(
                    event_id="event_one",
                    event_type="agent.no_progress",
                    reason_code="agent.no_progress",
                ),
            ),
            inflection_event_id="event_one",
            inflection_event_type="agent.no_progress",
            confidence=0.9,
        ),
    )
    unknown = EvaluationResult(
        id="result_two",
        run_id="run_one",
        case_id="case_one",
        variant_id="variant_two",
        repetition=1,
        status=EvaluationResultStatus.ERROR,
        snapshot=actual_snapshot(),
        artifact_workspace=ArtifactWorkspace(artifact_ref="artifact_result_two"),
    )

    aggregate = EvaluationAggregate.from_results((known_failure, unknown), run_id="run_one")
    assert aggregate.task_success_rate == 0.0
    assert aggregate.task_success_observed == 1
    assert aggregate.test_pass_rate == 0.0
    assert aggregate.total_tokens is None
    assert aggregate.total_cost_usd is None
    assert aggregate.total_tool_failures is None
    assert known_failure.failure_analysis.category is FailureCategory.ORCHESTRATION


def test_interrupted_results_keep_the_planned_matrix_and_metrics_unknown() -> None:
    interrupted_created_at = utc_now()
    interrupted = EvaluationResult(
        id="result_interrupted",
        run_id="run_one",
        case_id="case_one",
        variant_id="variant_one",
        repetition=1,
        status=EvaluationResultStatus.INTERRUPTED,
        artifact_workspace=ArtifactWorkspace(artifact_ref="evaluation/run_one/case_one/one/1"),
        failure_analysis=interrupted_evaluation_failure_analysis(),
        created_at=interrupted_created_at,
        finished_at=utc_now(),
    )
    pending = EvaluationResult(
        id="result_pending",
        run_id="run_one",
        case_id="case_one",
        variant_id="variant_two",
        repetition=1,
        artifact_workspace=ArtifactWorkspace(artifact_ref="evaluation/run_one/case_one/two/1"),
    )
    known_error = EvaluationResult(
        id="result_known_error",
        run_id="run_one",
        case_id="case_one",
        variant_id="variant_three",
        repetition=1,
        status=EvaluationResultStatus.ERROR,
        snapshot=actual_snapshot(),
        artifact_workspace=ArtifactWorkspace(artifact_ref="evaluation/run_one/case_one/three/1"),
    )

    aggregate = EvaluationAggregate.from_results(
        (interrupted, pending, known_error),
        run_id="run_one",
        expected_result_count=6,
    )

    assert aggregate.expected_result_count == 6
    assert aggregate.result_count == 3
    assert aggregate.completed_result_count == 1
    assert aggregate.finished_result_count == 1
    assert aggregate.interrupted_result_count == 1
    assert aggregate.pending_result_count == 1
    assert aggregate.unknown_result_count == 3
    assert aggregate.task_success_rate is None
    assert aggregate.test_pass_rate is None
    assert aggregate.total_tokens is None


def test_interrupted_result_contract_keeps_identity_and_execution_facts_unknown() -> None:
    case_definition = case()
    variant = EvaluationVariant(
        id="variant_interrupted_contract",
        name="interrupted session",
        kind="session",
        session_role=role_snapshot(),
        memory=MemorySnapshot(enabled=False),
        execution=execution(),
    )
    created_at = utc_now()
    interrupted = EvaluationResult(
        id="result_interrupted_contract",
        run_id="run_contract",
        case_id=case_definition.id,
        variant_id=variant.id,
        repetition=1,
        status=EvaluationResultStatus.INTERRUPTED,
        artifact_workspace=ArtifactWorkspace(
            artifact_ref="evaluation/run_contract/case_one/variant_interrupted_contract/1"
        ),
        failure_analysis=interrupted_evaluation_failure_analysis(),
        created_at=created_at,
        finished_at=utc_now(),
    )

    assert_evaluation_result_contract(case_definition, variant, interrupted)

    with pytest.raises(ValueError, match="case identity"):
        assert_evaluation_result_contract(
            case_definition,
            variant,
            interrupted.model_copy(update={"case_id": "case_wrong"}),
        )
    with pytest.raises(ValueError, match="metrics unknown"):
        assert_evaluation_result_contract(
            case_definition,
            variant,
            interrupted.model_copy(update={"metrics": EvaluationMetrics(task_succeeded=True)}),
        )


def test_case_changed_paths_and_secret_safe_prompt_are_checked() -> None:
    invalid_case = case().model_dump()
    invalid_case["expected_changed_paths"] = ("README.md",)
    with pytest.raises(ValidationError, match="expected changed paths"):
        EvaluationCase.model_validate(invalid_case)
    with pytest.raises(ValidationError, match="credential material"):
        PromptSnapshot(id="prompt_secret", content="Bearer abcdefghijklmnop")
    assert Path(case().environment.fixture_ref).is_relative_to(Path("examples"))


def test_failed_result_retains_an_out_of_contract_changed_path_for_analysis() -> None:
    case_definition = case()
    failed = EvaluationResult(
        id="result_bad_patch",
        run_id="run_one",
        case_id=case_definition.id,
        variant_id="variant_one",
        repetition=1,
        status=EvaluationResultStatus.FAILED,
        changed_paths=("outside_contract.py",),
        snapshot=actual_snapshot(),
        artifact_workspace=ArtifactWorkspace(artifact_ref="artifact_bad_patch"),
        verification=passed_verification(case_definition),
    )
    variant = EvaluationVariant(
        id="variant_one",
        name="single coder",
        kind="session",
        session_role=role_snapshot(),
        memory=MemorySnapshot(enabled=False),
        execution=execution(),
    )

    assert_evaluation_result_contract(case_definition, variant, failed)


def test_prompt_hash_workflow_slots_and_actual_snapshot_contract_are_strict() -> None:
    prompt = PromptSnapshot(id="prompt_hash", content="Follow the requested contract.")
    assert prompt.content_sha256 is not None
    spaced_content = "  Preserve execution-relevant prompt whitespace.\n"
    spaced_prompt = PromptSnapshot(id="prompt_spaced", content=spaced_content)
    assert spaced_prompt.content == spaced_content
    assert (
        spaced_prompt.content_sha256 == hashlib.sha256(spaced_content.encode("utf-8")).hexdigest()
    )
    with pytest.raises(ValidationError, match="must match"):
        PromptSnapshot(
            id="prompt_bad_hash", content="Follow the requested contract.", content_sha256="a" * 64
        )
    with pytest.raises(ValidationError, match="missing role snapshots"):
        WorkflowSnapshot(
            workflow_name="coding-review",
            roles=(role_snapshot(role_id="role_planner"),),
            planner_role_id="role_planner",
            coder_role_id="role_coder",
            reviewer_role_id="role_reviewer",
        )

    declared_role = role_snapshot()
    variant = EvaluationVariant(
        id="variant_contract",
        name="single coder",
        kind="session",
        session_role=declared_role,
        memory=MemorySnapshot(enabled=False),
        execution=execution(),
    )
    case_definition = case()
    result = EvaluationResult(
        id="result_contract",
        run_id="run_contract",
        case_id=case_definition.id,
        variant_id=variant.id,
        repetition=1,
        status="passed",
        metrics=EvaluationMetrics(
            task_succeeded=True,
            tests_passed=True,
            verification_passed=True,
        ),
        changed_paths=("calculator.py",),
        snapshot=actual_snapshot(role=declared_role),
        artifact_workspace=ArtifactWorkspace(artifact_ref="artifact_contract"),
        verification=passed_verification(case_definition),
    )
    assert_evaluation_result_contract(case_definition, variant, result)
    drifted = result.model_copy(
        update={
            "snapshot": actual_snapshot(
                role=declared_role.model_copy(
                    update={"role_version": declared_role.role_version + 1}
                )
            )
        }
    )
    with pytest.raises(ValueError, match="role snapshot drift"):
        assert_evaluation_result_contract(case_definition, variant, drifted)


def test_role_drift_error_retains_actual_selected_role_only() -> None:
    declared_role = role_snapshot()
    variant = EvaluationVariant(
        id="variant_role_drift",
        name="single coder",
        kind="session",
        session_role=declared_role,
        memory=MemorySnapshot(enabled=False),
        execution=execution(),
    )
    case_definition = case()
    actual_role = declared_role.model_copy(
        update={
            "role_version": declared_role.role_version + 1,
            "prompt": PromptSnapshot(
                id="prompt_role_coder",
                version=declared_role.role_version + 1,
                content="The current role prompt changed after suite creation.",
            ),
        }
    )
    error = EvaluationResult(
        id="result_role_drift_error",
        run_id="run_contract",
        case_id=case_definition.id,
        variant_id=variant.id,
        repetition=1,
        status=EvaluationResultStatus.ERROR,
        snapshot=actual_snapshot(role=actual_role),
        artifact_workspace=ArtifactWorkspace(artifact_ref="artifact_role_drift_error"),
        verification=passed_verification(case_definition),
        failure_analysis=FailureAnalysis(
            category=FailureCategory.ORCHESTRATION,
            reason_code="orchestration.role_snapshot_drift",
            root_cause="The live role selection drifted from the declared evaluation snapshot.",
        ),
    )

    assert_evaluation_result_contract(case_definition, variant, error)

    unrelated_error = error.model_copy(
        update={
            "failure_analysis": error.failure_analysis.model_copy(
                update={"reason_code": "unknown.evaluation_runner_error"}
            )
        }
    )
    with pytest.raises(ValueError, match="role snapshot drift"):
        assert_evaluation_result_contract(case_definition, variant, unrelated_error)

    unrelated_role = actual_role.model_copy(update={"role_id": "role_unrelated"})
    extra_role_error = error.model_copy(
        update={
            "snapshot": error.snapshot.model_copy(update={"roles": (actual_role, unrelated_role)})
        }
    )
    with pytest.raises(ValueError, match="selected role ids"):
        assert_evaluation_result_contract(case_definition, variant, extra_role_error)


def test_actual_memory_and_environment_can_add_unpinned_execution_facts() -> None:
    declared_role = role_snapshot()
    variant = EvaluationVariant(
        id="variant_unpinned_memory",
        name="single coder with memory",
        kind="session",
        session_role=declared_role,
        memory={"enabled": True},
        execution=execution(),
    )
    case_definition = case()
    captured_environment = case_definition.environment.model_copy(
        update={
            "fixture_ref": "examples/buggy_calculator/",
            "fixture_sha256": "b" * 64,
            "facts": (),
        }
    )
    result = EvaluationResult(
        id="result_enriched_snapshot",
        run_id="run_contract",
        case_id=case_definition.id,
        variant_id=variant.id,
        repetition=1,
        status="passed",
        metrics=EvaluationMetrics(
            task_succeeded=True,
            tests_passed=True,
            verification_passed=True,
        ),
        changed_paths=("calculator.py",),
        snapshot=EvaluationResultSnapshot(
            roles=(declared_role,),
            memory={
                "enabled": True,
                "references": (
                    {
                        "memory_id": "memory_actual",
                        "version": 2,
                        "content_sha256": "c" * 64,
                    },
                ),
            },
            environment=captured_environment,
            execution=execution(),
        ),
        artifact_workspace=ArtifactWorkspace(artifact_ref="artifact_enriched_snapshot"),
        verification=passed_verification(case_definition),
    )

    assert_evaluation_result_contract(case_definition, variant, result)

    pinned_variant = variant.model_copy(
        update={
            "memory": MemorySnapshot(
                enabled=True,
                references=(
                    {
                        "memory_id": "memory_expected",
                        "version": 1,
                        "content_sha256": "d" * 64,
                    },
                ),
            )
        }
    )
    with pytest.raises(ValueError, match="memory reference snapshot drift"):
        assert_evaluation_result_contract(case_definition, pinned_variant, result)
