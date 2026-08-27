import json
from pathlib import Path

from operant.application.service import ApplicationService
from operant.application.trace import (
    session_trace_jsonl,
    summarize_session_trace,
    summarize_workflow_trace,
    workflow_trace_jsonl,
)
from operant.domain.models import Event, ModelProfile, RolePreset
from operant.domain.workflow import (
    WorkflowRun,
    WorkflowRunEvent,
    WorkflowRunStatus,
    WorkflowStage,
)
from operant.persistence.sqlite import SQLiteStore
from operant.providers.openai_compatible import OpenAICompatibleProvider


def trace_fixture(tmp_path: Path) -> tuple[ApplicationService, str]:
    service = ApplicationService(
        SQLiteStore(tmp_path / "trace.sqlite3"),
        OpenAICompatibleProvider(),
    )
    service.initialize()
    profile = service.add_model_profile(
        ModelProfile(
            id="model_trace",
            name="trace",
            model_id="trace-model",
            base_url="https://relay.example.com/v1",
            secret_ref="OPERANT_TRACE_KEY",
        )
    )
    role = service.create_role(
        RolePreset(
            id="role_trace",
            name="Trace Role",
            system_prompt="Keep this prompt out of exported traces: private-marker.",
            model_profile_id=profile.id,
        )
    )
    session = service.create_session(role.id)
    for event in (
        Event(
            session_id=session.id,
            event_type="agent.started",
            payload={"turn": 0, "model_id": "trace-model"},
        ),
        Event(
            session_id=session.id,
            event_type="model.completed",
            payload={
                "turn": 1,
                "content": "secret-like raw model output",
                "finish_reason": "tool_calls",
                "tool_calls": [{"id": "call_1"}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 4,
                    "total_tokens": 14,
                },
                "duration_ms": 9,
            },
        ),
        Event(
            session_id=session.id,
            event_type="tool.completed",
            payload={
                "turn": 1,
                "tool_call_id": "call_1",
                "name": "read_file",
                "result": "sensitive file content",
                "is_error": False,
                "duration_ms": 3,
            },
        ),
        Event(
            session_id=session.id,
            event_type="agent.completed",
            payload={"turn": 2, "content": "raw final answer", "duration_ms": 21},
        ),
    ):
        service.store.append_event(event)
    return service, session.id


def test_trace_summary_includes_model_tools_tokens_and_duration(tmp_path: Path) -> None:
    service, session_id = trace_fixture(tmp_path)
    session = service.get_session(session_id)
    events = service.list_events(session_id)

    summary = summarize_session_trace(session, events)

    assert summary.status == "completed"
    assert summary.model_calls == 1
    assert summary.tool_calls == 0
    assert summary.total_tokens == 14
    assert summary.duration_ms == 21


def test_jsonl_trace_omits_prompts_raw_content_and_secret_reference(tmp_path: Path) -> None:
    service, session_id = trace_fixture(tmp_path)
    lines = list(
        session_trace_jsonl(
            service.get_session(session_id),
            service.list_events(session_id),
        )
    )
    records = [json.loads(line) for line in lines]
    exported = "\n".join(lines)

    assert records[0]["record_type"] == "trace.session"
    assert records[-1]["record_type"] == "trace.summary"
    assert "private-marker" not in exported
    assert "OPERANT_TRACE_KEY" not in exported
    assert "secret-like raw model output" not in exported
    assert "sensitive file content" not in exported
    assert "raw final answer" not in exported
    model_event = next(
        record for record in records if record.get("event_type") == "model.completed"
    )
    assert model_event["payload"]["content_chars"] == 28
    assert model_event["payload"]["tool_call_count"] == 1


def test_workflow_trace_aggregates_sessions_and_redacts_handoffs(tmp_path: Path) -> None:
    service, session_id = trace_fixture(tmp_path)
    run = service.create_workflow_run(
        WorkflowRun(
            task="private workflow task marker",
            workspace=str(tmp_path),
            planner_role_id="role_trace",
            coder_role_id="role_trace",
            reviewer_role_id="role_trace",
        )
    )
    service.append_workflow_event(
        WorkflowRunEvent(
            workflow_run_id=run.id,
            role="planner",
            session_id=session_id,
            event_type="workflow.subtask_result",
            payload={
                "result": {
                    "role": "planner",
                    "role_id": "role_trace",
                    "session_id": session_id,
                    "status": "completed",
                    "summary": "private handoff body",
                    "completed_steps": ["agent.completed"],
                    "failure_reason": None,
                }
            },
        )
    )
    completed = service.update_workflow_run(
        run.id,
        status=WorkflowRunStatus.COMPLETED,
        current_stage=WorkflowStage.COMPLETED,
        final_verdict="APPROVED",
    )
    workflow_events = service.list_workflow_events(run.id)
    session = service.get_session(session_id)
    session_events = service.list_events(session_id)
    summary = summarize_workflow_trace(
        completed,
        workflow_events,
        [summarize_session_trace(session, session_events)],
    )
    lines = list(
        workflow_trace_jsonl(
            completed,
            workflow_events,
            [(session, session_events)],
        )
    )
    exported = "\n".join(lines)

    assert summary.status == "completed"
    assert summary.session_count == 1
    assert summary.model_ids == ("trace-model",)
    assert summary.total_tokens == 14
    assert json.loads(lines[-1])["record_type"] == "trace.workflow_summary"
    assert "private workflow task marker" not in exported
    assert "private handoff body" not in exported
    assert "OPERANT_TRACE_KEY" not in exported
