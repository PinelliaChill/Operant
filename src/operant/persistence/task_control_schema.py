"""Version 20 schema for task control and scoped configuration."""

from __future__ import annotations

import sqlite3
from typing import Any


def _execute_batch(connection: sqlite3.Connection, script: str) -> None:
    statement = ""
    for line in script.splitlines():
        statement += f"{line}\n"
        if sqlite3.complete_statement(statement):
            connection.execute(statement)
            statement = ""
    if statement.strip():
        raise sqlite3.OperationalError("incomplete task-control migration statement")


def upgrade(connection: sqlite3.Connection) -> None:
    _execute_batch(
        connection,
        """
        CREATE TABLE goals (
            id TEXT PRIMARY KEY NOT NULL,
            owner_thread_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('active', 'blocked', 'completed', 'cancelled')),
            body_json TEXT NOT NULL CHECK (json_valid(body_json)),
            revision INTEGER NOT NULL CHECK (revision >= 1),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (owner_thread_id) REFERENCES threads(id)
        );
        CREATE INDEX idx_goals_owner_updated ON goals(owner_thread_id, updated_at);
        CREATE TABLE plan_artifacts (
            id TEXT PRIMARY KEY NOT NULL,
            goal_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN (
                'draft', 'in_review', 'approved', 'in_progress', 'completed', 'superseded'
            )),
            source_mode TEXT NOT NULL CHECK (source_mode = 'read_only'),
            body_json TEXT NOT NULL CHECK (json_valid(body_json)),
            revision INTEGER NOT NULL CHECK (revision >= 1),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (goal_id) REFERENCES goals(id)
        );
        CREATE INDEX idx_plan_artifacts_goal_updated ON plan_artifacts(goal_id, updated_at);
        CREATE TABLE execution_checklist_items (
            id TEXT PRIMARY KEY NOT NULL,
            plan_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN (
                'todo', 'in_progress', 'blocked', 'done', 'skipped'
            )),
            body_json TEXT NOT NULL CHECK (json_valid(body_json)),
            revision INTEGER NOT NULL CHECK (revision >= 1),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (plan_id) REFERENCES plan_artifacts(id)
        );
        CREATE INDEX idx_execution_checklist_plan_created
            ON execution_checklist_items(plan_id, created_at);
        CREATE TABLE scope_configs (
            scope_type TEXT NOT NULL CHECK (scope_type IN (
                'global', 'project', 'workspace', 'role'
            )),
            scope_id TEXT NOT NULL CHECK (length(scope_id) > 0),
            body_json TEXT NOT NULL CHECK (json_valid(body_json)),
            revision INTEGER NOT NULL CHECK (revision >= 1),
            updated_at TEXT NOT NULL,
            PRIMARY KEY (scope_type, scope_id)
        );
        """,
    )


def downgrade(connection: sqlite3.Connection) -> None:
    if any(
        connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None
        for table in ("goals", "plan_artifacts", "execution_checklist_items", "scope_configs")
    ):
        from operant.persistence.sqlite import MigrationError

        raise MigrationError("task control v20 contains data; rollback is unsafe")
    _execute_batch(
        connection,
        """
        DROP TABLE scope_configs;
        DROP INDEX idx_execution_checklist_plan_created;
        DROP TABLE execution_checklist_items;
        DROP INDEX idx_plan_artifacts_goal_updated;
        DROP TABLE plan_artifacts;
        DROP INDEX idx_goals_owner_updated;
        DROP TABLE goals;
        """,
    )


def schema_contracts() -> tuple[
    dict[str, set[str]],
    dict[str, dict[str, dict[str, Any]]],
    dict[str, tuple[str, ...]],
    dict[str, tuple[tuple[str, ...], ...]],
    dict[str, tuple[str, ...]],
    dict[str, tuple[tuple[str, str, str, str], ...]],
]:
    columns = {
        "goals": {
            "id",
            "owner_thread_id",
            "status",
            "body_json",
            "revision",
            "created_at",
            "updated_at",
        },
        "plan_artifacts": {
            "id",
            "goal_id",
            "status",
            "source_mode",
            "body_json",
            "revision",
            "created_at",
            "updated_at",
        },
        "execution_checklist_items": {
            "id",
            "plan_id",
            "status",
            "body_json",
            "revision",
            "created_at",
            "updated_at",
        },
        "scope_configs": {"scope_type", "scope_id", "body_json", "revision", "updated_at"},
    }
    physical: dict[str, dict[str, dict[str, Any]]] = {
        table: {
            column: {
                "type": "INTEGER" if column == "revision" else "TEXT",
                "not_null": True,
            }
            for column in names
        }
        for table, names in columns.items()
    }
    primary = {
        "goals": ("id",),
        "plan_artifacts": ("id",),
        "execution_checklist_items": ("id",),
        "scope_configs": ("scope_type", "scope_id"),
    }
    unique: dict[str, tuple[tuple[str, ...], ...]] = {table: () for table in columns}
    indexes: dict[str, tuple[str, ...]] = {
        "idx_goals_owner_updated": ("owner_thread_id", "updated_at"),
        "idx_plan_artifacts_goal_updated": ("goal_id", "updated_at"),
        "idx_execution_checklist_plan_created": ("plan_id", "created_at"),
    }
    foreign = {
        "goals": (("owner_thread_id", "threads", "id", "NO ACTION"),),
        "plan_artifacts": (("goal_id", "goals", "id", "NO ACTION"),),
        "execution_checklist_items": (("plan_id", "plan_artifacts", "id", "NO ACTION"),),
        "scope_configs": (),
    }
    return columns, physical, primary, unique, indexes, foreign
