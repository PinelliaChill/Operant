"""Persistent suggestion history and local watcher observations (schema v22)."""

from __future__ import annotations

import sqlite3
from typing import Any


def upgrade(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE workflow_suggestion_conversations ("
        "conversation_id TEXT PRIMARY KEY NOT NULL, team_id TEXT NOT NULL, "
        "team_version INTEGER NOT NULL, base_workflow_id TEXT, base_version INTEGER, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE workflow_suggestion_turns ("
        "turn_id TEXT PRIMARY KEY NOT NULL, conversation_id TEXT NOT NULL, "
        "ordinal INTEGER NOT NULL, instruction TEXT NOT NULL, "
        "suggestion_json TEXT NOT NULL CHECK (json_valid(suggestion_json)), "
        "created_at TEXT NOT NULL, "
        "UNIQUE(conversation_id, ordinal), "
        "FOREIGN KEY (conversation_id) REFERENCES "
        "workflow_suggestion_conversations(conversation_id))"
    )
    connection.execute(
        "CREATE INDEX idx_workflow_suggestion_turns_conversation "
        "ON workflow_suggestion_turns(conversation_id, ordinal)"
    )
    connection.execute(
        "CREATE TABLE scheduler_watch_baselines ("
        "schedule_id TEXT NOT NULL, schedule_version INTEGER NOT NULL, "
        "fingerprint TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 0, "
        "observed_at TEXT NOT NULL, error_code TEXT, "
        "PRIMARY KEY(schedule_id, schedule_version), "
        "FOREIGN KEY (schedule_id, schedule_version) "
        "REFERENCES schedule_definitions(schedule_id, version))"
    )


def downgrade(connection: sqlite3.Connection) -> None:
    if any(
        connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None
        for table in (
            "workflow_suggestion_conversations",
            "workflow_suggestion_turns",
            "scheduler_watch_baselines",
        )
    ):
        from operant.persistence.sqlite import MigrationError

        raise MigrationError("automation v22 contains data; rollback is unsafe")
    connection.execute("DROP TABLE scheduler_watch_baselines")
    connection.execute("DROP INDEX idx_workflow_suggestion_turns_conversation")
    connection.execute("DROP TABLE workflow_suggestion_turns")
    connection.execute("DROP TABLE workflow_suggestion_conversations")


def schema_contracts() -> tuple[
    dict[str, set[str]],
    dict[str, dict[str, dict[str, Any]]],
    dict[str, tuple[str, ...]],
    dict[str, tuple[tuple[str, ...], ...]],
    dict[str, tuple[str, ...]],
    dict[str, tuple[tuple[str, str, str, str], ...]],
]:
    columns = {
        "workflow_suggestion_conversations": {
            "conversation_id",
            "team_id",
            "team_version",
            "base_workflow_id",
            "base_version",
            "created_at",
            "updated_at",
        },
        "workflow_suggestion_turns": {
            "turn_id",
            "conversation_id",
            "ordinal",
            "instruction",
            "suggestion_json",
            "created_at",
        },
        "scheduler_watch_baselines": {
            "schedule_id",
            "schedule_version",
            "fingerprint",
            "generation",
            "observed_at",
            "error_code",
        },
    }
    nullable = {"base_workflow_id", "base_version", "error_code"}
    integer = {"team_version", "base_version", "ordinal", "schedule_version", "generation"}
    physical = {
        table: {
            column: {
                "type": "INTEGER" if column in integer else "TEXT",
                "not_null": column not in nullable,
            }
            for column in names
        }
        for table, names in columns.items()
    }
    primary = {
        "workflow_suggestion_conversations": ("conversation_id",),
        "workflow_suggestion_turns": ("turn_id",),
        "scheduler_watch_baselines": ("schedule_id", "schedule_version"),
    }
    unique: dict[str, tuple[tuple[str, ...], ...]] = {
        "workflow_suggestion_conversations": (),
        "workflow_suggestion_turns": (("conversation_id", "ordinal"),),
        "scheduler_watch_baselines": (),
    }
    indexes: dict[str, tuple[str, ...]] = {
        "idx_workflow_suggestion_turns_conversation": ("conversation_id", "ordinal")
    }
    foreign = {
        "workflow_suggestion_conversations": (),
        "workflow_suggestion_turns": (
            (
                "conversation_id",
                "workflow_suggestion_conversations",
                "conversation_id",
                "NO ACTION",
            ),
        ),
        "scheduler_watch_baselines": (
            ("schedule_id", "schedule_definitions", "schedule_id", "NO ACTION"),
            ("schedule_version", "schedule_definitions", "version", "NO ACTION"),
        ),
    }
    return columns, physical, primary, unique, indexes, foreign
