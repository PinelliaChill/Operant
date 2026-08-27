from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, ConfigDict

from operant.domain.models import Event, Session
from operant.domain.workflow import WorkflowRun, WorkflowRunEvent


class TraceSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    role_id: str
    role_name: str
    model_profile_id: str
    model_id: str
    effort: str
    status: str
    event_count: int
    model_calls: int
    tool_calls: int
    tool_failures: int
    test_feedback_events: int
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    duration_ms: int | None
    error_types: tuple[str, ...]


class WorkflowTraceSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_run_id: str
    status: str
    current_stage: str
    resumed_from_id: str | None
    final_verdict: str | None
    workflow_event_count: int
    subtask_count: int
    session_count: int
    role_ids: tuple[str, ...]
    model_profile_ids: tuple[str, ...]
    model_ids: tuple[str, ...]
    model_calls: int
    tool_calls: int
    tool_failures: int
    correction_rounds: int
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    duration_ms: int
    error_types: tuple[str, ...]


def summarize_session_trace(session: Session, events: list[Event]) -> TraceSummary:
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0
    has_usage = False
    error_types: list[str] = []
    duration_ms: int | None = None

    for event in events:
        usage = event.payload.get("usage")
        if event.event_type == "model.completed" and isinstance(usage, dict):
            prompt_tokens += _safe_int(usage.get("prompt_tokens"))
            completion_tokens += _safe_int(usage.get("completion_tokens"))
            total_tokens += _safe_int(usage.get("total_tokens"))
            has_usage = True
        candidate_duration = event.payload.get("duration_ms")
        if event.event_type.startswith("agent.") and isinstance(candidate_duration, int):
            duration_ms = max(duration_ms or 0, candidate_duration)
        if event.event_type == "agent.failed":
            error_type = event.payload.get("error_type")
            if isinstance(error_type, str) and error_type not in error_types:
                error_types.append(error_type)
        if event.event_type == "tool.failed" and "ToolError" not in error_types:
            error_types.append("ToolError")

    status = _terminal_status(events)
    return TraceSummary(
        session_id=session.id,
        role_id=session.role_snapshot.role_id,
        role_name=session.role_snapshot.role_name,
        model_profile_id=session.role_snapshot.model_profile_id,
        model_id=session.role_snapshot.model_id,
        effort=session.role_snapshot.effort.value,
        status=status,
        event_count=len(events),
        model_calls=sum(event.event_type == "model.completed" for event in events),
        tool_calls=sum(event.event_type == "tool.started" for event in events),
        tool_failures=sum(event.event_type == "tool.failed" for event in events),
        test_feedback_events=sum(event.event_type == "test.failure_feedback" for event in events),
        prompt_tokens=prompt_tokens if has_usage else None,
        completion_tokens=completion_tokens if has_usage else None,
        total_tokens=total_tokens if has_usage else None,
        duration_ms=duration_ms,
        error_types=tuple(error_types),
    )


def session_trace_jsonl(session: Session, events: list[Event]) -> Iterable[str]:
    snapshot = session.role_snapshot
    yield json.dumps(
        {
            "record_type": "trace.session",
            "session_id": session.id,
            "created_at": session.created_at.isoformat(),
            "role": {
                "id": snapshot.role_id,
                "version": snapshot.role_version,
                "name": snapshot.role_name,
            },
            "model": {
                "profile_id": snapshot.model_profile_id,
                "model_id": snapshot.model_id,
                "provider": snapshot.provider,
                "effort": snapshot.effort.value,
            },
            "policy": {
                "allowed_tools": list(snapshot.tool_policy.allowed_tools),
                "workspace_write": snapshot.tool_policy.workspace_write,
                "command_execution": snapshot.tool_policy.command_execution,
                "max_turns": snapshot.budget.max_turns,
                "timeout_seconds": snapshot.budget.timeout_seconds,
                "memory_scope": snapshot.memory_scope,
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    for event in events:
        yield json.dumps(
            {
                "record_type": "trace.event",
                "id": event.id,
                "session_id": event.session_id,
                "agent_id": event.agent_id,
                "event_type": event.event_type,
                "created_at": event.created_at.isoformat(),
                "payload": _sanitized_payload(event),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    yield json.dumps(
        {
            "record_type": "trace.summary",
            **summarize_session_trace(session, events).model_dump(mode="json"),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def summarize_workflow_trace(
    workflow_run: WorkflowRun,
    workflow_events: list[WorkflowRunEvent],
    session_traces: list[TraceSummary],
) -> WorkflowTraceSummary:
    def unique(values: Iterable[str]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(value for value in values if value))

    token_fields = ("prompt_tokens", "completion_tokens", "total_tokens")
    token_totals: dict[str, int | None] = {}
    for field in token_fields:
        values = [getattr(trace, field) for trace in session_traces]
        token_totals[field] = (
            sum(value for value in values if value is not None)
            if any(value is not None for value in values)
            else None
        )
    error_types = list(unique(error for trace in session_traces for error in trace.error_types))
    if workflow_run.last_error_type and workflow_run.last_error_type not in error_types:
        error_types.append(workflow_run.last_error_type)
    return WorkflowTraceSummary(
        workflow_run_id=workflow_run.id,
        status=workflow_run.status.value,
        current_stage=workflow_run.current_stage.value,
        resumed_from_id=workflow_run.resumed_from_id,
        final_verdict=workflow_run.final_verdict,
        workflow_event_count=len(workflow_events),
        subtask_count=sum(
            event.event_type == "workflow.subtask_result" for event in workflow_events
        ),
        session_count=len(session_traces),
        role_ids=unique(trace.role_id for trace in session_traces),
        model_profile_ids=unique(trace.model_profile_id for trace in session_traces),
        model_ids=unique(trace.model_id for trace in session_traces),
        model_calls=sum(trace.model_calls for trace in session_traces),
        tool_calls=sum(trace.tool_calls for trace in session_traces),
        tool_failures=sum(trace.tool_failures for trace in session_traces),
        correction_rounds=sum(
            event.event_type == "workflow.rework_started" for event in workflow_events
        ),
        prompt_tokens=token_totals["prompt_tokens"],
        completion_tokens=token_totals["completion_tokens"],
        total_tokens=token_totals["total_tokens"],
        duration_ms=sum(trace.duration_ms or 0 for trace in session_traces),
        error_types=tuple(error_types),
    )


def workflow_trace_jsonl(
    workflow_run: WorkflowRun,
    workflow_events: list[WorkflowRunEvent],
    sessions: list[tuple[Session, list[Event]]],
) -> Iterable[str]:
    yield json.dumps(
        {
            "record_type": "trace.workflow",
            "workflow_run_id": workflow_run.id,
            "status": workflow_run.status.value,
            "current_stage": workflow_run.current_stage.value,
            "resumed_from_id": workflow_run.resumed_from_id,
            "created_at": workflow_run.created_at.isoformat(),
            "updated_at": workflow_run.updated_at.isoformat(),
            "task_chars": len(workflow_run.task),
            "workspace_name": workflow_run.workspace.rsplit("/", 1)[-1],
            "roles": {
                "main": workflow_run.main_role_id,
                "planner": workflow_run.planner_role_id,
                "explorers": list(workflow_run.explorer_role_ids),
                "coder": workflow_run.coder_role_id,
                "reviewer": workflow_run.reviewer_role_id,
            },
            "limits": {
                "max_parallel_explorers": workflow_run.max_parallel_explorers,
                "max_rework_rounds": workflow_run.max_rework_rounds,
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    for event in workflow_events:
        yield json.dumps(
            {
                "record_type": "trace.workflow_event",
                "sequence": event.sequence,
                "id": event.id,
                "workflow_run_id": event.workflow_run_id,
                "role": event.role,
                "session_id": event.session_id,
                "event_type": event.event_type,
                "created_at": event.created_at.isoformat(),
                "payload": _sanitized_workflow_payload(event),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    session_summaries: list[TraceSummary] = []
    for session, events in sessions:
        session_summaries.append(summarize_session_trace(session, events))
        yield from session_trace_jsonl(session, events)
    yield json.dumps(
        {
            "record_type": "trace.workflow_summary",
            **summarize_workflow_trace(
                workflow_run,
                workflow_events,
                session_summaries,
            ).model_dump(mode="json"),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _sanitized_payload(event: Event) -> dict[str, Any]:
    payload = event.payload
    safe_keys = {
        "turn",
        "role_id",
        "role_version",
        "model_profile_id",
        "model_id",
        "provider",
        "effort",
        "finish_reason",
        "usage",
        "duration_ms",
        "tool_call_id",
        "name",
        "is_error",
        "category",
        "approved",
        "timeout_seconds",
        "reason",
        "signature",
        "consecutive_failures",
        "max_consecutive_test_failures",
        "max_turns",
        "error_type",
    }
    sanitized = {key: payload[key] for key in safe_keys if key in payload}
    if event.event_type == "model.completed":
        tool_calls = payload.get("tool_calls")
        sanitized["tool_call_count"] = len(tool_calls) if isinstance(tool_calls, list) else 0
        sanitized["content_chars"] = len(str(payload.get("content", "")))
    elif event.event_type.startswith("tool."):
        sanitized["result_chars"] = len(str(payload.get("result", "")))
    elif event.event_type == "agent.completed":
        sanitized["content_chars"] = len(str(payload.get("content", "")))
    return sanitized


def _sanitized_workflow_payload(event: WorkflowRunEvent) -> dict[str, Any]:
    payload = event.payload
    if event.event_type == "workflow.subtask_result":
        raw = payload.get("result")
        if not isinstance(raw, dict):
            return {}
        return {
            "result": {
                "role": raw.get("role"),
                "role_id": raw.get("role_id"),
                "session_id": raw.get("session_id"),
                "status": raw.get("status"),
                "summary_chars": len(str(raw.get("summary", ""))),
                "completed_steps": raw.get("completed_steps", []),
                "failure_reason": raw.get("failure_reason"),
            }
        }
    if event.event_type == "workflow.completed":
        subtasks = payload.get("subtasks")
        return {
            "verdict": payload.get("verdict"),
            "subtask_count": len(subtasks) if isinstance(subtasks, list) else 0,
        }
    safe_keys = {
        "round",
        "max_rework_rounds",
        "expected",
        "memory_id",
        "kind",
        "status",
        "source_session_id",
        "resumed_from_id",
        "max_parallel_explorers",
    }
    return {key: payload[key] for key in safe_keys if key in payload}


def _terminal_status(events: list[Event]) -> str:
    terminal = {
        "agent.completed": "completed",
        "agent.cancelled": "cancelled",
        "agent.timed_out": "timed_out",
        "agent.failed": "failed",
        "agent.no_progress": "failed",
        "agent.max_turns": "failed",
    }
    for event in reversed(events):
        status = terminal.get(event.event_type)
        if status is not None:
            return status
    return "unknown"


def _safe_int(value: Any) -> int:
    return value if isinstance(value, int) and value >= 0 else 0
