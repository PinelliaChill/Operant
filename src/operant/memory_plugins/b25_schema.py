"""SQLite objects owned by the bounded memory maintenance runner.

The Scheduler remains the queue and execution lease authority.  These tables
only keep the immutable input snapshot, the result of an idempotent
maintenance attempt, and the canonical-history watermark.  In particular,
there is no second queue or publication head here.

Core's migration owner should include :data:`SCHEMA_SQL` in its B2-5
migration.  ``schema_contracts`` mirrors the small schema inspectors used by
the other memory-plugin modules and makes isolated tests use the exact same
DDL.
"""

from __future__ import annotations

import sqlite3
from functools import lru_cache

from operant.memory_plugins.governance_schema import SCHEMA_SQL as GOVERNANCE_SQL
from operant.memory_plugins.maintenance_schema import SCHEMA_SQL as MAINTENANCE_SQL

SCHEMA_SQL = (
    GOVERNANCE_SQL
    + MAINTENANCE_SQL
    + """
CREATE TABLE b25_events (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,
 dataset_id TEXT NOT NULL, project_id TEXT NOT NULL,
 action TEXT NOT NULL, affected_ids TEXT NOT NULL, occurred_at TEXT NOT NULL
);
CREATE INDEX idx_b25_events_project ON b25_events(project_id,sequence);
CREATE TABLE b25_commands (
 command_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
 request_digest TEXT NOT NULL, state TEXT NOT NULL,
 result TEXT
);
"""
)


@lru_cache(maxsize=1)
def schema_contracts() -> tuple[
    dict[str, set[str]],
    dict[str, dict[str, dict[str, object]]],
    dict[str, tuple[str, ...]],
    dict[str, tuple[tuple[str, ...], ...]],
    dict[str, tuple[str, ...]],
]:
    """Return the migration inspector shape for the maintenance tables."""

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(SCHEMA_SQL)
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'b25_%' ORDER BY name"
        ).fetchall()
        required: dict[str, set[str]] = {}
        columns: dict[str, dict[str, dict[str, object]]] = {}
        keys: dict[str, tuple[str, ...]] = {}
        unique: dict[str, tuple[tuple[str, ...], ...]] = {}
        indexes: dict[str, tuple[str, ...]] = {}
        for table_row in rows:
            table = str(table_row["name"])
            table_info = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            required[table] = {str(row["name"]) for row in table_info}
            columns[table] = {
                str(row["name"]): {
                    "type": row["type"],
                    "not_null": bool(row["notnull"]),
                }
                for row in table_info
            }
            keys[table] = tuple(
                str(row["name"])
                for row in sorted(table_info, key=lambda row: row["pk"])
                if row["pk"]
            )
            for index in connection.execute(f'PRAGMA index_list("{table}")').fetchall():
                index_name = str(index["name"])
                fields = tuple(
                    str(field["name"])
                    for field in connection.execute(f'PRAGMA index_info("{index_name}")')
                )
                if index["origin"] != "pk" and index["unique"]:
                    unique[table] = (*unique.get(table, ()), fields)
                if not index_name.startswith("sqlite_autoindex") and not index["unique"]:
                    indexes[index_name] = fields
        return required, columns, keys, unique, indexes
    finally:
        connection.close()


__all__ = ["SCHEMA_SQL", "schema_contracts"]
