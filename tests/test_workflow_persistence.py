from pathlib import Path

import pytest

from operant.application.service import ApplicationService
from operant.domain.workflow import (
    WorkflowRun,
    WorkflowRunEvent,
    WorkflowRunStatus,
    WorkflowStage,
)
from operant.persistence.sqlite import ConflictError, NotFoundError, SQLiteStore
from operant.providers.openai_compatible import OpenAICompatibleProvider


def make_workflow(
    tmp_path: Path, *, status: WorkflowRunStatus = WorkflowRunStatus.CREATED
) -> WorkflowRun:
    return WorkflowRun(
        id=f"workflow-{status.value}",
        task="implement the requested change",
        workspace=str(tmp_path),
        planner_role_id="role-planner",
        explorer_role_ids=("role-explorer",),
        coder_role_id="role-coder",
        reviewer_role_id="role-reviewer",
        status=status,
    )


def test_workflow_run_crud_and_events_survive_reopen(tmp_path: Path) -> None:
    database = tmp_path / "workflow.sqlite3"
    store = SQLiteStore(database)
    service = ApplicationService(store, OpenAICompatibleProvider())
    service.initialize()
    workflow = make_workflow(tmp_path)

    assert service.create_workflow_run(workflow) == workflow
    assert service.get_workflow_run(workflow.id) == workflow
    assert service.list_workflow_runs() == [workflow]

    running = service.update_workflow_run(
        workflow.id,
        status=WorkflowRunStatus.RUNNING,
        current_stage=WorkflowStage.PLANNER,
    )
    assert running.status is WorkflowRunStatus.RUNNING
    assert running.current_stage is WorkflowStage.PLANNER
    assert running.updated_at >= workflow.updated_at

    first = service.append_workflow_event(
        WorkflowRunEvent(
            workflow_run_id=workflow.id,
            role="planner",
            session_id="session-planner",
            event_type="stage.completed",
            payload={"summary": "plan ready", "count": 1},
        )
    )
    second = service.append_workflow_event(
        WorkflowRunEvent(
            workflow_run_id=workflow.id,
            role="main",
            event_type="stage.started",
            payload={"message": "开始编码"},
        )
    )
    assert first.sequence == 1
    assert second.sequence == 2
    assert [event.event_type for event in service.list_workflow_events(workflow.id)] == [
        "stage.completed",
        "stage.started",
    ]

    completed = service.update_workflow_run(
        workflow.id,
        status=WorkflowRunStatus.COMPLETED,
        current_stage=WorkflowStage.COMPLETED,
        final_verdict="APPROVED",
    )
    reopened = SQLiteStore(database)
    reopened.initialize()
    assert reopened.get_workflow_run(workflow.id) == completed
    persisted_events = reopened.list_workflow_events(workflow.id)
    assert [event.sequence for event in persisted_events] == [1, 2]
    assert persisted_events[0].payload == {"summary": "plan ready", "count": 1}
    assert persisted_events[1].payload == {"message": "开始编码"}


def test_initialize_interrupts_only_legacy_running_workflows(tmp_path: Path) -> None:
    database = tmp_path / "workflow-recovery.sqlite3"
    store = SQLiteStore(database)
    store.initialize()
    running = make_workflow(tmp_path, status=WorkflowRunStatus.RUNNING)
    completed = WorkflowRun(
        **{
            **make_workflow(tmp_path).model_dump(),
            "id": "workflow-completed",
            "status": WorkflowRunStatus.COMPLETED,
        }
    )
    store.create_workflow_run(running)
    store.create_workflow_run(completed)

    reopened = SQLiteStore(database)
    reopened.initialize()

    recovered = reopened.get_workflow_run(running.id)
    assert recovered.status is WorkflowRunStatus.INTERRUPTED
    assert recovered.current_stage is running.current_stage
    assert recovered.task == running.task
    assert reopened.get_workflow_run(completed.id).status is WorkflowRunStatus.COMPLETED
    assert reopened.list_workflow_runs(status=WorkflowRunStatus.INTERRUPTED) == [recovered]


def test_workflow_persistence_rejects_missing_runs_and_duplicate_ids(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "workflow-errors.sqlite3")
    store.initialize()
    workflow = make_workflow(tmp_path)
    store.create_workflow_run(workflow)

    with pytest.raises(ConflictError):
        store.create_workflow_run(workflow)
    with pytest.raises(NotFoundError):
        store.get_workflow_run("missing-workflow")
    with pytest.raises(NotFoundError):
        store.list_workflow_events("missing-workflow")
    with pytest.raises(NotFoundError):
        store.append_workflow_event(
            WorkflowRunEvent(
                workflow_run_id="missing-workflow",
                role="main",
                event_type="run.started",
            )
        )
