"""SQLite objects for the bounded Remote Memory package lifecycle.

The tables are Core-owned metadata.  A package body may contain memory text,
so it is kept in the local Core database only; Relay storage continues to hold
opaque encrypted envelopes.  Target uploads are retained as pending review
facts and never become a publication head by themselves.
"""

from __future__ import annotations

import sqlite3
from functools import lru_cache

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS b26_memory_packs (
    package_id TEXT PRIMARY KEY,
    package_digest TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL,
    dataset_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    purpose TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    binding_epoch INTEGER NOT NULL CHECK (binding_epoch >= 0),
    permission_epoch INTEGER NOT NULL CHECK (permission_epoch >= 0),
    revocation_epoch INTEGER NOT NULL CHECK (revocation_epoch >= 0),
    status TEXT NOT NULL CHECK (status IN ('active', 'revoked', 'expired')),
    entry_count INTEGER NOT NULL CHECK (entry_count >= 0),
    body TEXT NOT NULL CHECK (json_valid(body))
);
CREATE INDEX IF NOT EXISTS idx_b26_memory_packs_project
    ON b26_memory_packs(project_id, status, expires_at);
CREATE INDEX IF NOT EXISTS idx_b26_memory_packs_target
    ON b26_memory_packs(target_id, status, expires_at);

CREATE TABLE IF NOT EXISTS b26_memory_uploads (
    upload_id TEXT PRIMARY KEY,
    package_id TEXT NOT NULL,
    package_digest TEXT NOT NULL,
    project_id TEXT NOT NULL,
    dataset_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    purpose TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending_review', 'rejected', 'accepted')),
    rejection_code TEXT,
    body TEXT NOT NULL CHECK (json_valid(body)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_b26_memory_uploads_project
    ON b26_memory_uploads(project_id, state, created_at);
CREATE INDEX IF NOT EXISTS idx_b26_memory_uploads_package
    ON b26_memory_uploads(package_id, package_digest);
"""


@lru_cache(maxsize=1)
def schema_contracts() -> tuple[
    dict[str, set[str]],
    dict[str, dict[str, dict[str, object]]],
    dict[str, tuple[str, ...]],
    dict[str, tuple[tuple[str, ...], ...]],
    dict[str, tuple[str, ...]],
]:
    """Return the migration inspector shape for the remote package tables."""

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(SCHEMA_SQL)
        rows = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name LIKE 'b26_memory_%' ORDER BY name"
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
