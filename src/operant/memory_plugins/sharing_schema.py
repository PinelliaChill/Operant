"""SQLite objects for B2-6 sharing and writer-promotion state.

The publication ledger remains the authority for MemoryVersion and
MemoryHead.  These tables only hold explicit associations, grants, dataset
consumers, transfer state and independently checked Writer evidence.  No
table here is a second memory publication head or a public command journal.
"""

from __future__ import annotations

import sqlite3
from functools import lru_cache

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS b26_worktree_registrations (
    registration_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    worktree_id TEXT NOT NULL,
    workspace_ref TEXT NOT NULL,
    branch_ref TEXT NOT NULL,
    commit_ref TEXT,
    tree_digest TEXT,
    principal_id TEXT NOT NULL,
    association_revision INTEGER NOT NULL CHECK (association_revision >= 0),
    permission_epoch INTEGER NOT NULL CHECK (permission_epoch >= 0),
    state TEXT NOT NULL CHECK (state IN ('active', 'revoked')),
    revoked_at TEXT,
    revoke_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (project_id, worktree_id),
    UNIQUE (workspace_id, worktree_id),
    CHECK ((state = 'revoked') = (revoked_at IS NOT NULL AND revoke_reason IS NOT NULL)),
    CHECK (state != 'active' OR (revoked_at IS NULL AND revoke_reason IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_b26_worktrees_project_state
    ON b26_worktree_registrations(project_id, state, updated_at);
CREATE INDEX IF NOT EXISTS idx_b26_worktrees_workspace_state
    ON b26_worktree_registrations(workspace_id, state, updated_at);

CREATE TABLE IF NOT EXISTS b26_sharing_grants (
    grant_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    source_dataset_id TEXT NOT NULL,
    source_scope_json TEXT NOT NULL CHECK (json_valid(source_scope_json)),
    target_scope_json TEXT NOT NULL CHECK (json_valid(target_scope_json)),
    subject_id TEXT NOT NULL,
    grantor_id TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (
        purpose IN ('recall', 'source_read', 'publish', 'share', 'transfer', 'delete')
    ),
    memory_refs_json TEXT NOT NULL CHECK (
        json_valid(memory_refs_json) AND json_type(memory_refs_json) = 'array'
    ),
    policy_revision INTEGER NOT NULL CHECK (policy_revision >= 0),
    permission_epoch INTEGER NOT NULL CHECK (permission_epoch >= 0),
    expires_at TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 0),
    state TEXT NOT NULL CHECK (state IN ('active', 'revoked', 'expired')),
    revoked_at TEXT,
    revoke_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK ((state = 'revoked') = (revoked_at IS NOT NULL AND revoke_reason IS NOT NULL)),
    CHECK (state != 'active' OR (revoked_at IS NULL AND revoke_reason IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_b26_grants_dataset_state
    ON b26_sharing_grants(source_dataset_id, state, expires_at);
CREATE INDEX IF NOT EXISTS idx_b26_grants_subject_scope
    ON b26_sharing_grants(subject_id, project_id, state);

CREATE TABLE IF NOT EXISTS b26_dataset_consumers (
    consumer_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    dataset_id TEXT NOT NULL,
    installation_id TEXT NOT NULL,
    grant_id TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (
        purpose IN ('primary_engine', 'shared_read', 'export', 'migration')
    ),
    state TEXT NOT NULL CHECK (state IN ('active', 'revoked', 'transferred')),
    revision INTEGER NOT NULL CHECK (revision >= 0),
    revoked_at TEXT,
    reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (dataset_id, installation_id, purpose),
    CHECK (state = 'active' OR revoked_at IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_b26_consumers_dataset_state
    ON b26_dataset_consumers(dataset_id, state, updated_at);
CREATE INDEX IF NOT EXISTS idx_b26_consumers_installation_state
    ON b26_dataset_consumers(installation_id, state, updated_at);

CREATE TABLE IF NOT EXISTS b26_dataset_transfers (
    transfer_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    request_json TEXT NOT NULL CHECK (json_valid(request_json)),
    source_installation_id TEXT,
    source_consumer_id TEXT,
    destination_consumer_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('pending', 'validated', 'committed', 'revoked', 'blocked')
    ),
    revision INTEGER NOT NULL CHECK (revision >= 0),
    evidence_refs_json TEXT NOT NULL CHECK (
        json_valid(evidence_refs_json) AND json_type(evidence_refs_json) = 'array'
    ),
    reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (state != 'validated' OR json_array_length(evidence_refs_json) > 0),
    CHECK (state != 'blocked' OR reason IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_b26_transfers_dataset_state
    ON b26_dataset_transfers(state, updated_at);
CREATE INDEX IF NOT EXISTS idx_b26_transfers_project_state
    ON b26_dataset_transfers(project_id, state, updated_at);

CREATE TABLE IF NOT EXISTS b26_writer_memory_evidence (
    evidence_id TEXT PRIMARY KEY,
    memory_ref_json TEXT NOT NULL CHECK (json_valid(memory_ref_json)),
    dataset_id TEXT NOT NULL,
    candidate_head_revision INTEGER NOT NULL CHECK (candidate_head_revision >= 0),
    project_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    worktree_id TEXT NOT NULL,
    writer_workspace_id TEXT NOT NULL,
    graph_run_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    branch_ref TEXT NOT NULL,
    base_revision TEXT NOT NULL,
    base_tree_digest TEXT,
    merge_run_id TEXT,
    target_isolation_ref TEXT,
    target_commit_ref TEXT,
    target_tree_digest TEXT,
    verification_artifact_refs_json TEXT NOT NULL CHECK (
        json_valid(verification_artifact_refs_json)
        AND json_type(verification_artifact_refs_json) = 'array'
    ),
    verification_digest TEXT,
    verification_status TEXT NOT NULL CHECK (
        verification_status IN ('pending', 'passed', 'failed', 'unknown')
    ),
    state TEXT NOT NULL CHECK (
        state IN ('candidate', 'eligible', 'published', 'blocked', 'revoked')
    ),
    published_memory_ref_json TEXT CHECK (
        published_memory_ref_json IS NULL OR json_valid(published_memory_ref_json)
    ),
    permission_epoch INTEGER NOT NULL CHECK (permission_epoch >= 0),
    revision INTEGER NOT NULL CHECK (revision >= 0),
    reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        verification_status != 'passed'
        OR (
            merge_run_id IS NOT NULL
            AND target_commit_ref IS NOT NULL
            AND target_tree_digest IS NOT NULL
            AND verification_digest IS NOT NULL
            AND json_array_length(verification_artifact_refs_json) > 0
        )
    ),
    CHECK (state != 'eligible' OR verification_status = 'passed'),
    CHECK (state != 'published' OR published_memory_ref_json IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_b26_writer_evidence_project_state
    ON b26_writer_memory_evidence(project_id, state, updated_at);
CREATE INDEX IF NOT EXISTS idx_b26_writer_evidence_run
    ON b26_writer_memory_evidence(run_id, state, updated_at);
CREATE INDEX IF NOT EXISTS idx_b26_writer_evidence_memory
    ON b26_writer_memory_evidence(dataset_id, memory_ref_json);

"""


@lru_cache(maxsize=1)
def schema_contracts() -> tuple[
    dict[str, set[str]],
    dict[str, dict[str, dict[str, object]]],
    dict[str, tuple[str, ...]],
    dict[str, tuple[tuple[str, ...], ...]],
    dict[str, tuple[str, ...]],
]:
    """Return table/column/key/index contracts from the exact DDL above."""

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
