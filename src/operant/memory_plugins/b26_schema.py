"""Additive v18 schema for experience lifecycle, sharing and remote memory."""

from __future__ import annotations

import sqlite3
from functools import lru_cache

from operant.memory_plugins.experience_skills_schema import SCHEMA_SQL as SKILL_SQL
from operant.memory_plugins.remote_memory_schema import SCHEMA_SQL as REMOTE_SQL
from operant.memory_plugins.sharing_schema import SCHEMA_SQL as SHARING_SQL

SCHEMA_SQL = (
    SKILL_SQL
    + SHARING_SQL
    + REMOTE_SQL
    + """
CREATE TABLE b26_run_context (
 session_id TEXT NOT NULL, agent_id TEXT NOT NULL, project_id TEXT NOT NULL,
 body TEXT NOT NULL CHECK (json_valid(body)), PRIMARY KEY(session_id,agent_id)
);
CREATE TABLE b26_commands (
 command_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
 request_digest TEXT NOT NULL, state TEXT NOT NULL, result TEXT
);
CREATE TABLE b26_events (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id TEXT NOT NULL, action TEXT NOT NULL,
 affected_ids TEXT NOT NULL, occurred_at TEXT NOT NULL
);
CREATE INDEX idx_b26_events_project ON b26_events(project_id,sequence);
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
    """Return the migration inspector shape for the B2-6 tables."""

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(SCHEMA_SQL)
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'b26_%' ORDER BY name"
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
