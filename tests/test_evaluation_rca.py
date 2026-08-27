from __future__ import annotations

from operant.application.evaluation import VerificationObservation, classify_failure
from operant.domain.evaluation import FailureCategory
from operant.domain.models import Event
from operant.domain.workflow import WorkflowRunEvent


def _event(event_id: str, event_type: str, payload: dict[str, object]) -> Event:
    return Event(
        id=event_id,
        session_id="session_eval",
        event_type=event_type,
        payload=payload,
    )


def _workflow_event(event_id: str, event_type: str) -> WorkflowRunEvent:
    return WorkflowRunEvent(
        id=event_id,
        workflow_run_id="workflow_eval",
        role="workflow",
        event_type=event_type,
    )


def test_rca_classifies_exp24_tool_hallucination_and_apply_patch_ambiguity() -> None:
    hallucination = classify_failure(
        session_events=(
            _event(
                "event_unknown_tool",
                "tool.failed",
                {"name": "invented_api", "result": "unknown tool"},
            ),
        )
    )
    assert hallucination is not None
    assert hallucination.category is FailureCategory.MODEL
    assert hallucination.reason_code == "model.hallucinated_tool_or_api"
    assert hallucination.inflection is not None
    assert hallucination.inflection.event_id == "event_unknown_tool"

    patch = classify_failure(
        session_events=(
            _event(
                "event_patch_ambiguous",
                "tool.failed",
                {"name": "apply_patch", "result": "target must occur exactly once; found 2"},
            ),
        )
    )
    assert patch is not None
    assert patch.category is FailureCategory.TOOL_CONTEXT
    assert patch.reason_code == "tool_context.apply_patch_ambiguous"

    declared_tool = classify_failure(
        session_events=(
            _event(
                "event_declared_tool_failure",
                "tool.failed",
                {"name": "read_file", "result": "ToolError: workspace tool failed"},
            ),
        )
    )
    assert declared_tool is not None
    assert declared_tool.category is FailureCategory.TOOL_CONTEXT
    assert declared_tool.reason_code == "tool_context.declared_tool_execution_failed"


def test_rca_classifies_no_progress_dependency_and_workflow_protocol_edges() -> None:
    no_progress = classify_failure(
        session_events=(
            _event("event_no_progress", "agent.no_progress", {"reason": "repeated_test_failure"}),
        )
    )
    assert no_progress is not None
    assert no_progress.category is FailureCategory.ORCHESTRATION
    assert no_progress.reason_code == "orchestration.repeated_test_no_progress"

    dependency = classify_failure(
        session_events=(
            _event(
                "event_missing_module",
                "tool.failed",
                {"name": "run_command", "result": "ModuleNotFoundError: No module named pkg"},
            ),
        )
    )
    assert dependency is not None
    assert dependency.category is FailureCategory.ENVIRONMENT
    assert dependency.reason_code == "environment.dependency_missing"

    missing_verdict = classify_failure(
        workflow_events=(
            _workflow_event("workflow_missing_verdict", "workflow.review_verdict_missing"),
        )
    )
    assert missing_verdict is not None
    assert missing_verdict.category is FailureCategory.PROMPT_PROTOCOL
    assert missing_verdict.reason_code == "prompt_protocol.review_verdict_missing"

    rework_limit = classify_failure(
        workflow_events=(_workflow_event("workflow_rework_limit", "workflow.rework_limit_reached"),)
    )
    assert rework_limit is not None
    assert rework_limit.category is FailureCategory.ORCHESTRATION
    assert rework_limit.reason_code == "orchestration.rework_limit_reached"


def test_rca_maps_external_verification_to_environment_without_raw_output() -> None:
    classification = classify_failure(
        verification=(
            VerificationObservation(
                argv=("python3", "-m", "unittest", "-q"),
                exit_code=None,
                timed_out=True,
                duration_ms=1000,
                output_sha256=None,
                output_chars=None,
                output_truncated=None,
            ),
        ),
        runtime_succeeded=True,
    )
    assert classification is not None
    assert classification.category is FailureCategory.ENVIRONMENT
    assert classification.reason_code == "verification.command_timed_out"
    assert classification.evidence == ()
