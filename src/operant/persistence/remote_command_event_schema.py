"""Append-only Remote Command status cursor (schema v23)."""

from __future__ import annotations

import sqlite3
from typing import Any


def upgrade(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE remote_command_events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            command_id TEXT NOT NULL,
            host_id TEXT NOT NULL,
            device_id TEXT NOT NULL,
            remote_session_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('received','accepted','rejected','completed','outcome_unknown')
            ),
            host_acknowledged_at TEXT,
            result_ref TEXT,
            error_code TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (command_id) REFERENCES remote_command_receipts(command_id)
        )"""
    )
    connection.execute(
        "CREATE INDEX idx_remote_command_events_host_sequence "
        "ON remote_command_events(host_id, sequence)"
    )
    connection.execute(
        "CREATE INDEX idx_remote_command_events_session_sequence "
        "ON remote_command_events(remote_session_id, sequence)"
    )
    # v22 cursors were receipt rowids. Start snapshots strictly after their
    # maximum, so a connected v22 device cannot silently miss current status.
    connection.execute(
        """INSERT INTO remote_command_events(
            sequence, command_id, host_id, device_id, remote_session_id,
            status, host_acknowledged_at, result_ref, error_code, updated_at
        ) SELECT
            (SELECT COALESCE(MAX(rowid), 0) FROM remote_command_receipts)
                + ROW_NUMBER() OVER (ORDER BY r.rowid),
            r.command_id, r.host_id, r.device_id, r.remote_session_id,
            r.status, r.host_acknowledged_at, r.result_ref, r.error_code, r.updated_at
        FROM remote_command_receipts AS r ORDER BY r.rowid"""
    )
    connection.execute(
        """CREATE TRIGGER remote_command_event_insert
        AFTER INSERT ON remote_command_receipts
        BEGIN
            INSERT INTO remote_command_events(
                command_id, host_id, device_id, remote_session_id,
                status, host_acknowledged_at, result_ref, error_code, updated_at
            ) VALUES (
                NEW.command_id, NEW.host_id, NEW.device_id, NEW.remote_session_id,
                NEW.status, NEW.host_acknowledged_at, NEW.result_ref,
                NEW.error_code, NEW.updated_at
            );
        END"""
    )
    connection.execute(
        """CREATE TRIGGER remote_command_event_status
        AFTER UPDATE OF status ON remote_command_receipts
        WHEN NEW.status != OLD.status
        BEGIN
            INSERT INTO remote_command_events(
                command_id, host_id, device_id, remote_session_id,
                status, host_acknowledged_at, result_ref, error_code, updated_at
            ) VALUES (
                NEW.command_id, NEW.host_id, NEW.device_id, NEW.remote_session_id,
                NEW.status, NEW.host_acknowledged_at, NEW.result_ref,
                NEW.error_code, NEW.updated_at
            );
        END"""
    )


def downgrade(connection: sqlite3.Connection) -> None:
    if connection.execute("SELECT 1 FROM remote_command_events LIMIT 1").fetchone():
        from operant.persistence.sqlite import MigrationError

        raise MigrationError("Remote Command events contain data; rollback is unsafe")
    connection.execute("DROP TRIGGER remote_command_event_status")
    connection.execute("DROP TRIGGER remote_command_event_insert")
    connection.execute("DROP INDEX idx_remote_command_events_session_sequence")
    connection.execute("DROP INDEX idx_remote_command_events_host_sequence")
    connection.execute("DROP TABLE remote_command_events")


def schema_contracts() -> tuple[
    dict[str, set[str]],
    dict[str, dict[str, dict[str, Any]]],
    dict[str, tuple[str, ...]],
    dict[str, tuple[tuple[str, ...], ...]],
    dict[str, tuple[str, ...]],
    dict[str, tuple[tuple[str, str, str, str], ...]],
]:
    columns = {
        "remote_command_events": {
            "sequence",
            "command_id",
            "host_id",
            "device_id",
            "remote_session_id",
            "status",
            "host_acknowledged_at",
            "result_ref",
            "error_code",
            "updated_at",
        }
    }
    nullable = {"sequence", "host_acknowledged_at", "result_ref", "error_code"}
    physical = {
        "remote_command_events": {
            name: {
                "type": "INTEGER" if name == "sequence" else "TEXT",
                "not_null": name not in nullable,
            }
            for name in columns["remote_command_events"]
        }
    }
    return (
        columns,
        physical,
        {"remote_command_events": ("sequence",)},
        {},
        {
            "idx_remote_command_events_host_sequence": ("host_id", "sequence"),
            "idx_remote_command_events_session_sequence": ("remote_session_id", "sequence"),
        },
        {
            "remote_command_events": (
                ("command_id", "remote_command_receipts", "command_id", "NO ACTION"),
            )
        },
    )
