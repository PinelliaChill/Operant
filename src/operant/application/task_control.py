"""Explicit commands for durable Goal, Plan, and checklist records."""

from __future__ import annotations

import sqlite3
from typing import Any, TypeVar

from pydantic import BaseModel

from operant.domain.models import utc_now
from operant.domain.task_control import (
    ChecklistStatus,
    ExecutionChecklistItem,
    Goal,
    GoalStatus,
    PlanArtifact,
    PlanStatus,
)
from operant.persistence.sqlite import ConflictError, NotFoundError, SQLiteStore

Record = TypeVar("Record", bound=BaseModel)


class TaskControlService:
    """Use the Core database; natural-language progress never updates checklist state."""

    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    @staticmethod
    def _load(
        connection: sqlite3.Connection, table: str, record_id: str, model: type[Record]
    ) -> Record:
        row = connection.execute(
            f"SELECT body_json FROM {table} WHERE id = ?", (record_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"{table} record not found: {record_id}")
        return model.model_validate_json(row["body_json"])

    @staticmethod
    def _insert(
        connection: sqlite3.Connection,
        table: str,
        record: Goal | PlanArtifact | ExecutionChecklistItem,
        parent_column: str,
        parent_id: str,
        *,
        source_mode: str | None = None,
    ) -> None:
        extra_columns = f", {parent_column}, status, body_json, revision, created_at, updated_at"
        placeholders = "?, ?, ?, ?, ?, ?, ?"
        values: tuple[Any, ...] = (
            record.id,
            parent_id,
            record.status.value,
            record.model_dump_json(),
            record.revision,
            record.created_at.isoformat(),
            record.updated_at.isoformat(),
        )
        if source_mode is not None:
            extra_columns += ", source_mode"
            placeholders += ", ?"
            values += (source_mode,)
        try:
            connection.execute(
                f"INSERT INTO {table}(id{extra_columns}) VALUES ({placeholders})", values
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"{table} identity or parent is invalid") from exc

    @staticmethod
    def _update(
        connection: sqlite3.Connection,
        table: str,
        current: Goal | PlanArtifact | ExecutionChecklistItem,
        updated: Goal | PlanArtifact | ExecutionChecklistItem,
    ) -> None:
        result = connection.execute(
            f"""UPDATE {table}
                SET status = ?, body_json = ?, revision = ?, updated_at = ?
                WHERE id = ? AND revision = ?""",
            (
                updated.status.value,
                updated.model_dump_json(),
                updated.revision,
                updated.updated_at.isoformat(),
                current.id,
                current.revision,
            ),
        )
        if result.rowcount != 1:
            raise ConflictError(f"{table} revision changed")

    @staticmethod
    def _changed(model: type[Record], current: Record, changes: dict[str, Any]) -> Record:
        data = current.model_dump()
        data.update(changes)
        data["revision"] = int(data["revision"]) + 1
        data["updated_at"] = utc_now()
        return model.model_validate(data)

    @staticmethod
    def _require_revision(
        current: Goal | PlanArtifact | ExecutionChecklistItem, expected_revision: int
    ) -> None:
        if current.revision != expected_revision:
            raise ConflictError("task-control revision changed")

    @staticmethod
    def _require_record(connection: sqlite3.Connection, table: str, record_id: str) -> None:
        if (
            connection.execute(f"SELECT 1 FROM {table} WHERE id = ?", (record_id,)).fetchone()
            is None
        ):
            raise NotFoundError(f"{table} record not found: {record_id}")

    @staticmethod
    def _check_goal_links(connection: sqlite3.Connection, goal: Goal) -> None:
        owner = connection.execute(
            "SELECT workspace_ref FROM threads WHERE id = ?", (goal.owner_thread_id,)
        ).fetchone()
        if owner is None:
            raise NotFoundError(f"owner thread not found: {goal.owner_thread_id}")
        for thread_id in goal.linked_thread_ids:
            linked = connection.execute(
                "SELECT workspace_ref FROM threads WHERE id = ?", (thread_id,)
            ).fetchone()
            if linked is None:
                raise NotFoundError(f"linked thread not found: {thread_id}")
            if linked["workspace_ref"] != owner["workspace_ref"]:
                raise ConflictError("linked thread belongs to another workspace")
        for run_id in goal.linked_workflow_run_ids:
            legacy = connection.execute(
                "SELECT 1 FROM workflow_runs WHERE id = ?", (run_id,)
            ).fetchone()
            graph = connection.execute(
                "SELECT 1 FROM graph_workflow_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if legacy is None and graph is None:
                raise NotFoundError(f"linked workflow run not found: {run_id}")

    def create_goal(self, goal: Goal) -> Goal:
        if goal.revision != 1 or goal.status is not GoalStatus.ACTIVE:
            raise ValueError("new goal must be active at revision 1")
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._check_goal_links(connection, goal)
            self._insert(connection, "goals", goal, "owner_thread_id", goal.owner_thread_id)
        return goal

    def get_goal(self, goal_id: str) -> Goal:
        with self.store._connect() as connection:
            return self._load(connection, "goals", goal_id, Goal)

    def list_goals(self, owner_thread_id: str) -> list[Goal]:
        with self.store._connect() as connection:
            self._require_record(connection, "threads", owner_thread_id)
            rows = connection.execute(
                "SELECT body_json FROM goals WHERE owner_thread_id = ? ORDER BY created_at, id",
                (owner_thread_id,),
            ).fetchall()
        return [Goal.model_validate_json(row["body_json"]) for row in rows]

    def update_goal(self, goal_id: str, *, expected_revision: int, **changes: Any) -> Goal:
        if {"id", "owner_thread_id", "revision", "created_at", "updated_at"} & changes.keys():
            raise ValueError("goal identity and revision are immutable")
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._load(connection, "goals", goal_id, Goal)
            self._require_revision(current, expected_revision)
            if current.status in {GoalStatus.COMPLETED, GoalStatus.CANCELLED}:
                raise ConflictError("terminal goal cannot be changed")
            updated = self._changed(Goal, current, changes)
            self._check_goal_links(connection, updated)
            self._update(connection, "goals", current, updated)
        return updated

    def create_plan(
        self, plan: PlanArtifact, *, expected_goal_revision: int | None = None
    ) -> PlanArtifact:
        if plan.revision != 1 or plan.status is not PlanStatus.DRAFT:
            raise ValueError("new plan must be a draft at revision 1")
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            goal = self._load(connection, "goals", plan.goal_id, Goal)
            if expected_goal_revision is not None and goal.revision != expected_goal_revision:
                raise ConflictError("Goal changed while Plan was being created")
            if goal.status in {GoalStatus.COMPLETED, GoalStatus.CANCELLED}:
                raise ConflictError("terminal Goal cannot receive a Plan")
            if plan.created_from_context_revision is not None:
                self._require_record(
                    connection, "context_revisions", plan.created_from_context_revision
                )
            self._insert(
                connection,
                "plan_artifacts",
                plan,
                "goal_id",
                plan.goal_id,
                source_mode=plan.source_mode,
            )
        return plan

    def get_plan(self, plan_id: str) -> PlanArtifact:
        with self.store._connect() as connection:
            return self._load(connection, "plan_artifacts", plan_id, PlanArtifact)

    def list_plans(self, goal_id: str) -> list[PlanArtifact]:
        with self.store._connect() as connection:
            self._require_record(connection, "goals", goal_id)
            rows = connection.execute(
                "SELECT body_json FROM plan_artifacts WHERE goal_id = ? ORDER BY created_at, id",
                (goal_id,),
            ).fetchall()
        return [PlanArtifact.model_validate_json(row["body_json"]) for row in rows]

    def update_plan(self, plan_id: str, *, expected_revision: int, **changes: Any) -> PlanArtifact:
        if {
            "id",
            "goal_id",
            "source_mode",
            "created_from_context_revision",
            "revision",
            "created_at",
            "updated_at",
        } & changes.keys():
            raise ValueError("plan provenance and revision are immutable")
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._load(connection, "plan_artifacts", plan_id, PlanArtifact)
            self._require_revision(current, expected_revision)
            if current.status in {PlanStatus.COMPLETED, PlanStatus.SUPERSEDED}:
                raise ConflictError("terminal plan cannot be changed")
            updated = self._changed(PlanArtifact, current, changes)
            self._update(connection, "plan_artifacts", current, updated)
        return updated

    @staticmethod
    def _check_dependencies(connection: sqlite3.Connection, item: ExecutionChecklistItem) -> None:
        for dependency_id in item.dependencies:
            row = connection.execute(
                "SELECT plan_id FROM execution_checklist_items WHERE id = ?", (dependency_id,)
            ).fetchone()
            if row is None or row["plan_id"] != item.plan_id:
                raise ConflictError("checklist dependency must belong to the same plan")

    def add_checklist_item(self, item: ExecutionChecklistItem) -> ExecutionChecklistItem:
        if item.revision != 1 or item.status is not ChecklistStatus.TODO:
            raise ValueError("new checklist item must be todo at revision 1")
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_record(connection, "plan_artifacts", item.plan_id)
            self._check_dependencies(connection, item)
            self._insert(connection, "execution_checklist_items", item, "plan_id", item.plan_id)
        return item

    def get_checklist_item(self, item_id: str) -> ExecutionChecklistItem:
        with self.store._connect() as connection:
            return self._load(
                connection, "execution_checklist_items", item_id, ExecutionChecklistItem
            )

    def list_checklist_items(self, plan_id: str) -> list[ExecutionChecklistItem]:
        with self.store._connect() as connection:
            self._require_record(connection, "plan_artifacts", plan_id)
            rows = connection.execute(
                "SELECT body_json FROM execution_checklist_items "
                "WHERE plan_id = ? ORDER BY created_at, id",
                (plan_id,),
            ).fetchall()
        return [ExecutionChecklistItem.model_validate_json(row["body_json"]) for row in rows]

    def command_checklist_status(
        self,
        item_id: str,
        *,
        expected_revision: int,
        status: ChecklistStatus,
        evidence_refs: tuple[str, ...] = (),
        blocker: str | None = None,
        node_run_id: str | None = None,
    ) -> ExecutionChecklistItem:
        """Persist a real status command; DONE requires evidence and finished dependencies."""

        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._load(
                connection, "execution_checklist_items", item_id, ExecutionChecklistItem
            )
            self._require_revision(current, expected_revision)
            if status is ChecklistStatus.DONE:
                for dependency_id in current.dependencies:
                    dependency = self._load(
                        connection,
                        "execution_checklist_items",
                        dependency_id,
                        ExecutionChecklistItem,
                    )
                    if dependency.status is not ChecklistStatus.DONE:
                        raise ConflictError("checklist dependencies are not done")
            updated = self._changed(
                ExecutionChecklistItem,
                current,
                {
                    "status": status,
                    "evidence_refs": evidence_refs,
                    "blocker": blocker,
                    "node_run_id": node_run_id,
                },
            )
            self._update(connection, "execution_checklist_items", current, updated)
        return updated
