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

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS b25_maintenance_jobs (
    job_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_digest TEXT NOT NULL,
    project_id TEXT NOT NULL,
    dataset_id TEXT NOT NULL,
    installation_id TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    source_stream TEXT NOT NULL,
    source_cursor INTEGER NOT NULL CHECK (source_cursor >= 0),
    processed_cursor INTEGER NOT NULL DEFAULT 0 CHECK (processed_cursor >= 0),
    source_digest TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    workflow_version INTEGER NOT NULL CHECK (workflow_version >= 1),
    graph_run_id TEXT,
    run_request_id TEXT,
    plugin_id TEXT NOT NULL,
    plugin_version TEXT NOT NULL,
    package_digest TEXT NOT NULL,
    config_id TEXT NOT NULL,
    config_revision INTEGER NOT NULL CHECK (config_revision >= 0),
    config_digest TEXT NOT NULL,
    model_profile_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    model_digest TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    budget_tokens INTEGER NOT NULL CHECK (budget_tokens >= 0),
    max_sources INTEGER NOT NULL CHECK (max_sources BETWEEN 1 AND 100),
    max_attempts INTEGER NOT NULL CHECK (max_attempts BETWEEN 1 AND 20),
    state TEXT NOT NULL CHECK (
        state IN (
            'queued', 'running', 'succeeded', 'no_change', 'cancelled',
            'retry_wait', 'dead_letter', 'manual_reconcile_required', 'blocked'
        )
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    error_code TEXT,
    proposal_ids_json TEXT NOT NULL CHECK (json_valid(proposal_ids_json)),
    snapshot_json TEXT NOT NULL CHECK (json_valid(snapshot_json)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_b25_maintenance_jobs_project_state
    ON b25_maintenance_jobs(project_id, state, updated_at);
CREATE INDEX IF NOT EXISTS idx_b25_maintenance_jobs_dataset_cursor
    ON b25_maintenance_jobs(dataset_id, source_stream, source_cursor);

CREATE TABLE IF NOT EXISTS b25_maintenance_watermarks (
    dataset_id TEXT NOT NULL,
    source_stream TEXT NOT NULL,
    processed_cursor INTEGER NOT NULL CHECK (processed_cursor >= 0),
    last_job_id TEXT NOT NULL,
    snapshot_digest TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (dataset_id, source_stream)
);

CREATE TABLE IF NOT EXISTS b25_maintenance_idempotency (
    dataset_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    state TEXT NOT NULL,
    result_json TEXT NOT NULL CHECK (json_valid(result_json)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (dataset_id, idempotency_key)
);
"""


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
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'b25_maintenance_%' "
            "ORDER BY name"
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
