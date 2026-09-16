"""Additive SQLite objects used by the B2-5 memory governance service.

The publication ledger remains the authority for version bodies and heads.  The
tables in this module only hold governance metadata: source availability,
dependencies, relationships and review receipts.  In particular, no mailbox
body is copied here and no table in this module is a second publication head.

The Core migration owner includes this SQL in its v17 migration.  The service
also applies it when used with an isolated temporary database so that the
domain service can be tested without a Core migration fixture.
"""

from __future__ import annotations

import sqlite3
from functools import lru_cache

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS b25_governance_sources (
    dataset_id TEXT NOT NULL,
    source_key TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_revision INTEGER NOT NULL CHECK (source_revision >= 1),
    content_digest TEXT NOT NULL,
    scope_json TEXT NOT NULL CHECK (json_valid(scope_json)),
    permission_epoch INTEGER NOT NULL CHECK (permission_epoch >= 0),
    state TEXT NOT NULL CHECK (
        state IN ('available', 'deleted', 'revoked', 'unavailable', 'blocked')
    ),
    reason TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (dataset_id, source_key),
    UNIQUE (dataset_id, source_type, source_id, source_revision, content_digest)
);
CREATE INDEX IF NOT EXISTS idx_b25_governance_sources_lookup
    ON b25_governance_sources(dataset_id, source_type, source_id, source_revision);
CREATE INDEX IF NOT EXISTS idx_b25_governance_sources_state
    ON b25_governance_sources(dataset_id, state, updated_at);

CREATE TABLE IF NOT EXISTS b25_governance_proposals (
    proposal_id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    record_id TEXT NOT NULL,
    proposal_revision INTEGER NOT NULL CHECK (proposal_revision >= 0),
    proposed_version INTEGER NOT NULL CHECK (proposed_version >= 1),
    proposed_digest TEXT NOT NULL,
    base_head_revision INTEGER NOT NULL CHECK (base_head_revision >= 0),
    review_due_at TEXT,
    source_watermark TEXT,
    source_keys_json TEXT NOT NULL CHECK (json_valid(source_keys_json)),
    model_ids_json TEXT NOT NULL CHECK (json_valid(model_ids_json)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_b25_governance_proposals_dataset_state
    ON b25_governance_proposals(dataset_id, record_id, proposal_revision);
CREATE INDEX IF NOT EXISTS idx_b25_governance_proposals_review_due
    ON b25_governance_proposals(dataset_id, review_due_at);

CREATE TABLE IF NOT EXISTS b25_governance_relations (
    dataset_id TEXT NOT NULL,
    relation_id TEXT NOT NULL,
    relation TEXT NOT NULL CHECK (relation IN ('conflicts_with', 'supersedes', 'derived_from')),
    subject_record_id TEXT NOT NULL,
    subject_version INTEGER NOT NULL CHECK (subject_version >= 1),
    subject_digest TEXT NOT NULL,
    target_record_id TEXT,
    target_version INTEGER CHECK (target_version IS NULL OR target_version >= 1),
    target_digest TEXT,
    target_source_key TEXT,
    reason TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (dataset_id, relation_id),
    CHECK (
        (relation = 'derived_from' AND target_source_key IS NOT NULL
            AND target_record_id IS NULL AND target_version IS NULL)
        OR (relation IN ('conflicts_with', 'supersedes')
            AND target_record_id IS NOT NULL AND target_version IS NOT NULL
            AND target_digest IS NOT NULL AND target_source_key IS NULL)
    ),
    UNIQUE (
        dataset_id, relation, subject_record_id, subject_version,
        target_record_id, target_version, target_source_key
    )
);
CREATE INDEX IF NOT EXISTS idx_b25_governance_relations_subject
    ON b25_governance_relations(dataset_id, subject_record_id, subject_version, relation);
CREATE INDEX IF NOT EXISTS idx_b25_governance_relations_target
    ON b25_governance_relations(dataset_id, target_record_id, target_version, relation);
CREATE INDEX IF NOT EXISTS idx_b25_governance_relations_source
    ON b25_governance_relations(dataset_id, target_source_key, relation);

CREATE TABLE IF NOT EXISTS b25_governance_dependencies (
    dataset_id TEXT NOT NULL,
    derived_record_id TEXT NOT NULL,
    derived_version INTEGER NOT NULL CHECK (derived_version >= 1),
    source_key TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('active', 'blocked', 'revoked')),
    reason TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (dataset_id, derived_record_id, derived_version, source_key)
);
CREATE INDEX IF NOT EXISTS idx_b25_governance_dependencies_source
    ON b25_governance_dependencies(dataset_id, source_key, state);
CREATE INDEX IF NOT EXISTS idx_b25_governance_dependencies_derived
    ON b25_governance_dependencies(dataset_id, derived_record_id, derived_version, state);

CREATE TABLE IF NOT EXISTS b25_governance_reviews (
    review_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    dataset_id TEXT NOT NULL,
    proposal_revision INTEGER NOT NULL CHECK (proposal_revision >= 0),
    decision TEXT NOT NULL CHECK (decision IN ('accept', 'reject')),
    reviewer_id TEXT NOT NULL,
    reviewer_kind TEXT NOT NULL CHECK (reviewer_kind IN ('user', 'system')),
    reason TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_b25_governance_reviews_proposal
    ON b25_governance_reviews(dataset_id, proposal_id, proposal_revision);
"""


@lru_cache(maxsize=1)
def schema_contracts() -> tuple[
    dict[str, set[str]],
    dict[str, dict[str, dict[str, object]]],
    dict[str, tuple[str, ...]],
    dict[str, tuple[tuple[str, ...], ...]],
    dict[str, tuple[str, ...]],
]:
    """Return the migration inspector shape used by ``SQLiteStore``."""

    from operant.memory_plugins.ledger import SCHEMA_SQL as ledger_sql

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(ledger_sql + SCHEMA_SQL)
        table_rows = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name LIKE 'b25_%' ORDER BY name"
        ).fetchall()
        required: dict[str, set[str]] = {}
        columns: dict[str, dict[str, dict[str, object]]] = {}
        keys: dict[str, tuple[str, ...]] = {}
        unique: dict[str, tuple[tuple[str, ...], ...]] = {}
        indexes: dict[str, tuple[str, ...]] = {}
        for table_row in table_rows:
            table = str(table_row["name"])
            rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            required[table] = {str(row["name"]) for row in rows}
            columns[table] = {
                str(row["name"]): {
                    "type": row["type"],
                    "not_null": bool(row["notnull"]),
                }
                for row in rows
            }
            keys[table] = tuple(
                str(row["name"]) for row in sorted(rows, key=lambda row: row["pk"]) if row["pk"]
            )
            index_rows = connection.execute(f'PRAGMA index_list("{table}")').fetchall()
            for index in index_rows:
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
