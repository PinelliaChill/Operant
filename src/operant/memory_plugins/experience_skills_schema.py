"""SQLite objects for experience-derived Skill versions.

These tables are an additive v18 migration fragment.  The Core migration
owner should combine this SQL with the B2-6 command/event tables.  Skill
versions, validations, dependencies, and lifecycle events are separate from
the command receipt journal; no table here grants a tool or stores a local
filesystem path.
"""

from __future__ import annotations

import sqlite3
from functools import lru_cache

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS b26_skill_versions (
    skill_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 128),
    description TEXT NOT NULL CHECK (length(description) BETWEEN 1 AND 4000),
    procedure_ref_json TEXT NOT NULL CHECK (json_valid(procedure_ref_json)),
    source_refs_json TEXT NOT NULL CHECK (json_valid(source_refs_json)),
    source_digest TEXT NOT NULL CHECK (
        length(source_digest) = 64 AND source_digest NOT GLOB '*[^0-9a-f]*'
    ),
    scope_json TEXT NOT NULL CHECK (json_valid(scope_json)),
    role_ids_json TEXT NOT NULL CHECK (json_valid(role_ids_json)),
    agent_ids_json TEXT NOT NULL CHECK (json_valid(agent_ids_json)),
    sensitivity TEXT NOT NULL CHECK (sensitivity IN ('public', 'internal', 'sensitive')),
    artifact_id TEXT NOT NULL,
    artifact_content_hash TEXT NOT NULL CHECK (
        length(artifact_content_hash) = 64 AND artifact_content_hash NOT GLOB '*[^0-9a-f]*'
    ),
    artifact_size_bytes INTEGER NOT NULL CHECK (artifact_size_bytes >= 1),
    artifact_json TEXT NOT NULL CHECK (json_valid(artifact_json)),
    trust_status TEXT NOT NULL CHECK (
        trust_status IN ('untrusted_draft', 'validated', 'core_published', 'revoked')
    ),
    created_at TEXT NOT NULL,
    PRIMARY KEY (skill_id, version)
);
CREATE INDEX IF NOT EXISTS idx_b26_skill_versions_procedure
    ON b26_skill_versions(
        json_extract(procedure_ref_json, '$.dataset_id'),
        json_extract(procedure_ref_json, '$.record_id'),
        json_extract(procedure_ref_json, '$.version')
    );

CREATE TABLE IF NOT EXISTS b26_skill_validations (
    validation_id TEXT PRIMARY KEY,
    skill_id TEXT NOT NULL,
    skill_version INTEGER NOT NULL CHECK (skill_version >= 1),
    status TEXT NOT NULL CHECK (status IN ('pending', 'passed', 'failed', 'blocked')),
    validator_version TEXT NOT NULL,
    artifact_hash TEXT NOT NULL CHECK (
        length(artifact_hash) = 64 AND artifact_hash NOT GLOB '*[^0-9a-f]*'
    ),
    source_digest TEXT NOT NULL CHECK (
        length(source_digest) = 64 AND source_digest NOT GLOB '*[^0-9a-f]*'
    ),
    issues_json TEXT NOT NULL CHECK (json_valid(issues_json)),
    checked_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_b26_skill_validations_target
    ON b26_skill_validations(skill_id, skill_version, checked_at);

CREATE TABLE IF NOT EXISTS b26_skill_heads (
    skill_id TEXT PRIMARY KEY,
    head_revision INTEGER NOT NULL CHECK (head_revision >= 0),
    published_version INTEGER CHECK (published_version IS NULL OR published_version >= 1),
    state TEXT NOT NULL CHECK (state IN ('draft', 'published', 'disabled', 'revoked', 'blocked')),
    permission_epoch INTEGER NOT NULL CHECK (permission_epoch >= 0),
    updated_at TEXT NOT NULL,
    CHECK (
        state NOT IN ('published', 'disabled', 'revoked') OR published_version IS NOT NULL
    )
);

CREATE TABLE IF NOT EXISTS b26_skill_dependencies (
    skill_id TEXT NOT NULL,
    skill_version INTEGER NOT NULL CHECK (skill_version >= 1),
    source_key TEXT NOT NULL,
    source_json TEXT NOT NULL CHECK (json_valid(source_json)),
    state TEXT NOT NULL CHECK (state IN ('active', 'blocked', 'revoked')),
    reason TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (skill_id, skill_version, source_key)
);
CREATE INDEX IF NOT EXISTS idx_b26_skill_dependencies_source
    ON b26_skill_dependencies(source_key, state, skill_id, skill_version);

CREATE TABLE IF NOT EXISTS b26_skill_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    skill_id TEXT NOT NULL,
    skill_version INTEGER CHECK (skill_version IS NULL OR skill_version >= 1),
    action TEXT NOT NULL,
    state TEXT NOT NULL,
    head_revision INTEGER NOT NULL CHECK (head_revision >= 0),
    source_digest TEXT,
    artifact_hash TEXT,
    detail_json TEXT NOT NULL CHECK (json_valid(detail_json)),
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_b26_skill_events_skill
    ON b26_skill_events(skill_id, sequence);
CREATE INDEX IF NOT EXISTS idx_b26_skill_events_source
    ON b26_skill_events(source_digest, sequence);
"""


@lru_cache(maxsize=1)
def schema_contracts() -> tuple[
    dict[str, set[str]],
    dict[str, dict[str, dict[str, object]]],
    dict[str, tuple[str, ...]],
    dict[str, tuple[tuple[str, ...], ...]],
    dict[str, tuple[str, ...]],
]:
    """Return the strict table/column/index shape for migration inspection."""

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(SCHEMA_SQL)
        rows = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name LIKE 'b26_skill_%' ORDER BY name"
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
