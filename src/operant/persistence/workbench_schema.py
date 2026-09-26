"""Version 19 durable child-run and directed mailbox schema."""

from __future__ import annotations

import sqlite3
from typing import Any


def _execute_batch(connection: sqlite3.Connection, script: str) -> None:
    """Run DDL inside the caller's transaction; executescript commits it early."""

    statement = ""
    for line in script.splitlines():
        statement += f"{line}\n"
        if sqlite3.complete_statement(statement):
            connection.execute(statement)
            statement = ""
    if statement.strip():
        raise sqlite3.OperationalError("incomplete workbench migration statement")


def upgrade(connection: sqlite3.Connection) -> None:
    _execute_batch(
        connection,
        """
        CREATE TABLE workbench_children (
            thread_id TEXT PRIMARY KEY NOT NULL,
            parent_thread_id TEXT NOT NULL,
            session_id TEXT NOT NULL UNIQUE,
            task TEXT NOT NULL CHECK (length(task) BETWEEN 1 AND 12000),
            status TEXT NOT NULL CHECK (status IN (
                'queued', 'running', 'completed', 'failed', 'cancelled', 'interrupted'
            )),
            result TEXT,
            recovery TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (thread_id) REFERENCES threads(id),
            FOREIGN KEY (parent_thread_id) REFERENCES threads(id),
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );
        CREATE INDEX idx_workbench_children_parent ON workbench_children(parent_thread_id);
        CREATE TABLE workbench_messages (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            sender_thread_id TEXT NOT NULL,
            recipient_thread_id TEXT NOT NULL,
            body TEXT NOT NULL CHECK (length(body) BETWEEN 1 AND 12000),
            reply_to TEXT,
            idempotency_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            consumed_at TEXT,
            UNIQUE (sender_thread_id, idempotency_key),
            FOREIGN KEY (sender_thread_id) REFERENCES threads(id),
            FOREIGN KEY (recipient_thread_id) REFERENCES threads(id),
            FOREIGN KEY (reply_to) REFERENCES workbench_messages(id)
        );
        CREATE INDEX idx_workbench_messages_recipient
            ON workbench_messages(recipient_thread_id, sequence);
        CREATE TABLE workbench_wake_counts (
            thread_id TEXT PRIMARY KEY NOT NULL,
            wake_count INTEGER NOT NULL CHECK (wake_count >= 0),
            updated_at TEXT NOT NULL,
            FOREIGN KEY (thread_id) REFERENCES threads(id)
        );
        """,
    )


def downgrade(connection: sqlite3.Connection) -> None:
    # A rollback may remove only unused workbench structures. Keep the data and
    # the v19 ledger intact if a child/message/automatic wake exists.
    if any(
        connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None
        for table in ("workbench_children", "workbench_messages", "workbench_wake_counts")
    ):
        from operant.persistence.sqlite import MigrationError

        raise MigrationError("workbench v19 contains data; rollback is unsafe")
    _execute_batch(
        connection,
        """
        DROP INDEX idx_workbench_messages_recipient;
        DROP TABLE workbench_messages;
        DROP TABLE workbench_wake_counts;
        DROP INDEX idx_workbench_children_parent;
        DROP TABLE workbench_children;
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
    columns: dict[str, set[str]] = {
        "workbench_children": {
            "thread_id",
            "parent_thread_id",
            "session_id",
            "task",
            "status",
            "result",
            "recovery",
            "created_at",
            "updated_at",
        },
        "workbench_messages": {
            "sequence",
            "id",
            "sender_thread_id",
            "recipient_thread_id",
            "body",
            "reply_to",
            "idempotency_key",
            "created_at",
            "consumed_at",
        },
        "workbench_wake_counts": {"thread_id", "wake_count", "updated_at"},
    }
    physical: dict[str, dict[str, dict[str, Any]]] = {
        table: {
            column: {
                "type": "INTEGER" if column in {"sequence", "wake_count"} else "TEXT",
                "not_null": column
                not in (
                    {"sequence", "result", "recovery"}
                    if table == "workbench_children"
                    else {"sequence", "reply_to", "consumed_at"}
                ),
            }
            for column in names
        }
        for table, names in columns.items()
    }
    primary: dict[str, tuple[str, ...]] = {
        "workbench_children": ("thread_id",),
        "workbench_messages": ("sequence",),
        "workbench_wake_counts": ("thread_id",),
    }
    unique: dict[str, tuple[tuple[str, ...], ...]] = {
        "workbench_children": (("session_id",),),
        "workbench_messages": (("id",), ("sender_thread_id", "idempotency_key")),
        "workbench_wake_counts": (),
    }
    indexes: dict[str, tuple[str, ...]] = {
        "idx_workbench_children_parent": ("parent_thread_id",),
        "idx_workbench_messages_recipient": ("recipient_thread_id", "sequence"),
    }
    foreign: dict[str, tuple[tuple[str, str, str, str], ...]] = {
        "workbench_children": (
            ("thread_id", "threads", "id", "NO ACTION"),
            ("parent_thread_id", "threads", "id", "NO ACTION"),
            ("session_id", "sessions", "id", "NO ACTION"),
        ),
        "workbench_messages": (
            ("sender_thread_id", "threads", "id", "NO ACTION"),
            ("recipient_thread_id", "threads", "id", "NO ACTION"),
            ("reply_to", "workbench_messages", "id", "NO ACTION"),
        ),
        "workbench_wake_counts": (("thread_id", "threads", "id", "NO ACTION"),),
    }
    return columns, physical, primary, unique, indexes, foreign
