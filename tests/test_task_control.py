from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from operant.application.task_control import TaskControlService
from operant.domain.task_control import (
    ChecklistStatus,
    ExecutionChecklistItem,
    Goal,
    GoalStatus,
    PlanArtifact,
    PlanStatus,
)
from operant.domain.threads import ConversationThread
from operant.persistence.sqlite import ConflictError, MigrationError, NotFoundError, SQLiteStore


def _service(tmp_path: Path) -> tuple[TaskControlService, ConversationThread]:
    store = SQLiteStore(tmp_path / "task-control.sqlite")
    store.initialize()
    thread = store.create_thread(ConversationThread(workspace_ref="/tmp/operant-task-control"))
    return TaskControlService(store), thread


def test_goal_plan_and_checklist_persist_with_explicit_state_commands(tmp_path: Path) -> None:
    service, thread = _service(tmp_path)
    linked = service.store.create_thread(
        ConversationThread(workspace_ref=thread.workspace_ref, parent_thread_id=thread.id)
    )
    goal = service.create_goal(
        Goal(
            owner_thread_id=thread.id,
            objective="Deliver task control",
            token_budget=1200,
            cost_budget=2.5,
            time_budget_seconds=3600,
            completion_criteria=("User reviews result",),
            linked_thread_ids=(linked.id,),
        )
    )
    assert service.list_goals(thread.id) == [goal]
    blocked = service.update_goal(
        goal.id,
        expected_revision=1,
        status=GoalStatus.BLOCKED,
        blocked_reason="Waiting for approval",
    )
    assert blocked.revision == 2
    with pytest.raises(ConflictError, match="revision"):
        service.update_goal(goal.id, expected_revision=1, objective="Stale edit")
    resumed = service.update_goal(
        goal.id, expected_revision=2, status=GoalStatus.ACTIVE, blocked_reason=None
    )

    plan = service.create_plan(
        PlanArtifact(
            goal_id=goal.id,
            scope="Implement and verify task control",
            proposed_changes=("Persist Goal and Plan",),
            verification_plan=("Run focused tests",),
        )
    )
    assert plan.source_mode == "read_only"
    revised = service.update_plan(
        plan.id,
        expected_revision=1,
        status=PlanStatus.IN_REVIEW,
        proposed_changes=("Persist Goal, Plan, and checklist",),
    )
    assert revised.revision == 2
    first = service.add_checklist_item(
        ExecutionChecklistItem(plan_id=plan.id, description="Implement schema")
    )
    second = service.add_checklist_item(
        ExecutionChecklistItem(
            plan_id=plan.id, description="Verify schema", dependencies=(first.id,)
        )
    )
    with pytest.raises(ConflictError, match="dependencies"):
        service.command_checklist_status(
            second.id,
            expected_revision=1,
            status=ChecklistStatus.DONE,
            evidence_refs=("test:task-control",),
        )
    done_first = service.command_checklist_status(
        first.id,
        expected_revision=1,
        status=ChecklistStatus.DONE,
        evidence_refs=("test:task-control",),
    )
    assert done_first.revision == 2
    done_second = service.command_checklist_status(
        second.id,
        expected_revision=1,
        status=ChecklistStatus.DONE,
        evidence_refs=("test:task-control",),
    )
    assert done_second.status is ChecklistStatus.DONE
    with pytest.raises(ConflictError, match="revision"):
        service.command_checklist_status(first.id, expected_revision=1, status=ChecklistStatus.TODO)

    completed = service.update_goal(
        goal.id,
        expected_revision=resumed.revision,
        status=GoalStatus.COMPLETED,
        result_ref="artifact:walkthrough",
    )
    restarted = TaskControlService(SQLiteStore(service.store.path))
    assert restarted.get_goal(goal.id) == completed
    assert restarted.get_plan(plan.id) == revised
    assert restarted.list_checklist_items(plan.id) == [done_first, done_second]
    with pytest.raises(ConflictError, match="terminal"):
        restarted.update_goal(goal.id, expected_revision=completed.revision, objective="Change")


def test_task_control_validates_scope_provenance_and_evidence(tmp_path: Path) -> None:
    service, thread = _service(tmp_path)
    other = service.store.create_thread(ConversationThread(workspace_ref="/tmp/other"))
    with pytest.raises(ConflictError, match="another workspace"):
        service.create_goal(
            Goal(owner_thread_id=thread.id, objective="Cross scope", linked_thread_ids=(other.id,))
        )
    with pytest.raises(NotFoundError, match="workflow run"):
        service.create_goal(
            Goal(
                owner_thread_id=thread.id,
                objective="Missing run",
                linked_workflow_run_ids=("missing",),
            )
        )
    with pytest.raises(ValidationError, match="read_only"):
        PlanArtifact(goal_id="goal-1", source_mode="write")  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="evidence"):
        ExecutionChecklistItem(
            plan_id="plan-1", description="Done without evidence", status=ChecklistStatus.DONE
        )
    with pytest.raises(ValidationError, match="reason"):
        Goal(owner_thread_id=thread.id, objective="Blocked", status=GoalStatus.BLOCKED)


def test_v20_schema_contract_and_nonempty_rollback_guard(tmp_path: Path) -> None:
    database = tmp_path / "migration.sqlite"
    store = SQLiteStore(database)
    store.migrate(19)
    assert store.schema_version() == 19
    store.initialize()
    assert store.schema_version() == 20
    with store._connect() as connection:
        store._validate_schema_contract(connection, version=20)
    assert (
        store.list_applied_migrations()[-1]["checksum"]
        == (SQLiteStore._FROZEN_MIGRATION_CHECKSUMS[20])
    )
    store.rollback(19, isolated=True)
    assert store.schema_version() == 19
    store.initialize()
    with store._connect() as connection:
        connection.execute(
            "INSERT INTO scope_configs(scope_type, scope_id, body_json, revision, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("global", "global", "{}", 1, "2026-09-26T00:00:00+00:00"),
        )
    with pytest.raises(MigrationError, match="contains data"):
        store.rollback(19, isolated=True)
    assert store.schema_version() == 20
