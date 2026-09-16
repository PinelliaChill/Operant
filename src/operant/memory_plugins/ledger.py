"""A small, generic SQLite ledger for dataset-owned memory plugins.

This module is deliberately independent from ``persistence.sqlite``.  It
stores the frozen B2-1 contract objects as JSON and exposes the transaction
boundaries needed by the Core service:

* versions are append-only and immutable;
* each ``(dataset_id, record_id)`` has one mutable publication head;
* proposals never move the head until an explicit CAS publish;
* records are tombstoned instead of physically removed; and
* legacy imports are quarantined as ``legacy_unverified`` candidates.

The Core still owns authentication, grants, binding epochs and permission
epochs.  Optional epoch arguments below let that service bind its checks to a
write without making the ledger guess the caller's identity.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from pydantic import TypeAdapter

from operant.contracts.b2_1 import (
    DatasetOwner,
    MemoryConditions,
    MemoryHead,
    MemoryProposal,
    MemoryVersion,
    MemoryVersionRef,
    PersonalScope,
    RunScope,
    Scope,
    SessionScope,
    SourceRef,
    Tombstone,
    WorkspaceScope,
)

SCHEMA_SQL = """
-- B2-3 memory plugin tables.  These are deliberately namespaced: the Core
-- migration owner may include this SQL in its v15 migration without giving a
-- plugin a second copy of the Core schema.
CREATE TABLE IF NOT EXISTS memory_ledger_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT OR IGNORE INTO memory_ledger_meta(key, value)
VALUES ('publication_cursor', '0');

CREATE TABLE IF NOT EXISTS memory_ledger_versions (
    dataset_id TEXT NOT NULL,
    record_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    content_digest TEXT NOT NULL,
    body TEXT NOT NULL,
    body_hash TEXT NOT NULL,
    owner_namespace TEXT NOT NULL,
    kind TEXT NOT NULL,
    scope_kind TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (dataset_id, record_id, version),
    UNIQUE (dataset_id, record_id, version, content_digest)
);
CREATE INDEX IF NOT EXISTS idx_memory_ledger_versions_dataset
    ON memory_ledger_versions(dataset_id, record_id, version);
CREATE INDEX IF NOT EXISTS idx_memory_ledger_versions_source_shape
    ON memory_ledger_versions(dataset_id, kind, scope_kind);

CREATE TABLE IF NOT EXISTS memory_ledger_heads (
    dataset_id TEXT NOT NULL,
    record_id TEXT NOT NULL,
    published_version INTEGER,
    published_digest TEXT,
    revision INTEGER NOT NULL CHECK (revision >= 0),
    publication_cursor TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('unpublished', 'published', 'inactive', 'revoked', 'deleted')
    ),
    permission_epoch INTEGER NOT NULL CHECK (permission_epoch >= 0),
    body TEXT NOT NULL,
    PRIMARY KEY (dataset_id, record_id)
);
CREATE INDEX IF NOT EXISTS idx_memory_ledger_heads_dataset_state
    ON memory_ledger_heads(dataset_id, state, revision);

CREATE TABLE IF NOT EXISTS memory_ledger_proposals (
    proposal_id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    record_id TEXT NOT NULL,
    proposal_revision INTEGER NOT NULL CHECK (proposal_revision >= 0),
    base_head_revision INTEGER NOT NULL CHECK (base_head_revision >= 0),
    proposed_version INTEGER NOT NULL CHECK (proposed_version >= 1),
    state TEXT NOT NULL CHECK (
        state IN ('pending', 'accepted', 'rejected', 'conflict', 'cancelled')
    ),
    body TEXT NOT NULL,
    idempotency_key TEXT,
    request_digest TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_ledger_proposals_record
    ON memory_ledger_proposals(dataset_id, record_id, state, proposal_revision);
CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_ledger_proposals_idempotency
    ON memory_ledger_proposals(dataset_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS memory_ledger_tombstones (
    dataset_id TEXT NOT NULL,
    tombstone_key TEXT NOT NULL,
    record_id TEXT,
    state TEXT NOT NULL CHECK (state IN ('deleted', 'revoked', 'source_deleted')),
    revision INTEGER NOT NULL CHECK (revision >= 0),
    cursor TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (dataset_id, tombstone_key)
);
CREATE INDEX IF NOT EXISTS idx_memory_ledger_tombstones_dataset
    ON memory_ledger_tombstones(dataset_id, state, revision);

CREATE TABLE IF NOT EXISTS memory_ledger_idempotency (
    operation TEXT NOT NULL,
    dataset_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    result_type TEXT NOT NULL,
    result_body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (operation, dataset_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS memory_ledger_legacy_imports (
    dataset_id TEXT NOT NULL,
    source_key TEXT NOT NULL,
    record_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    legacy_body_hash TEXT NOT NULL,
    legacy_body TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    PRIMARY KEY (dataset_id, source_key),
    UNIQUE (dataset_id, record_id, version)
);

-- No operation in this module updates or deletes a version.  The triggers
-- make that invariant survive accidental direct SQL writes as well.
CREATE TRIGGER IF NOT EXISTS memory_ledger_versions_immutable_update
BEFORE UPDATE ON memory_ledger_versions
BEGIN
    SELECT RAISE(ABORT, 'memory ledger versions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS memory_ledger_versions_immutable_delete
BEFORE DELETE ON memory_ledger_versions
WHEN memory_ledger_gc_allowed() = 0
BEGIN
    SELECT RAISE(ABORT, 'memory ledger versions are immutable');
END;
"""

_SCOPE_ADAPTER: TypeAdapter[Any] = TypeAdapter(Scope)

_ProposalOperation = Literal["create", "modify", "merge", "supersede", "revoke"]
_MemoryKind = Literal["working", "episodic", "project"]


class LedgerError(RuntimeError):
    """Base class for errors raised by the standalone ledger."""


class LedgerNotFoundError(LookupError, LedgerError):
    """A dataset, record, version, proposal, or tombstone is absent."""


class LedgerConflictError(ValueError, LedgerError):
    """A CAS, immutable identity, state, ownership, or scope check failed."""


class IdempotencyConflictError(LedgerConflictError):
    """An idempotency key was reused with a different request digest."""


class LedgerValidationError(ValueError, LedgerError):
    """Input could not be represented by the frozen B2-1 contracts."""


@dataclass(frozen=True)
class LegacyMigrationResult:
    """Readback of one explicit legacy import.

    The result is iterable over imported versions for callers that only need
    the historical rows, while the counts and proposals make migration impact
    visible to a management UI.
    """

    dataset_id: str
    imported_versions: tuple[MemoryVersion, ...]
    proposals: tuple[MemoryProposal, ...]
    skipped_versions: int = 0

    @property
    def imported_count(self) -> int:
        return len(self.imported_versions)

    @property
    def record_count(self) -> int:
        return len({version.ref.record_id for version in self.imported_versions})

    def __iter__(self) -> Iterator[MemoryVersion]:
        return iter(self.imported_versions)

    def __len__(self) -> int:
        return len(self.imported_versions)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _jsonable(value: Any) -> Any:
    """Convert nested contract values to deterministic JSON-compatible data."""

    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _canonical_json(value: Any) -> str:
    """Encode contract data deterministically for hashes and idempotency."""

    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _request_fingerprint(
    operation: str,
    request: Mapping[str, Any],
    supplied_digest: str | None,
) -> str:
    """Hash the complete normalized request used by an idempotency key.

    ``request_digest`` is included as an input rather than replacing the
    locally derived fingerprint.  This prevents a caller-supplied digest that
    omits a CAS or Proposal field from making two different requests look
    identical to the ledger.
    """

    return _digest(
        {
            "operation": operation,
            "request": dict(request),
            "supplied_request_digest": supplied_digest,
        }
    )


def _content_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _as_owner(value: DatasetOwner | Mapping[str, Any]) -> DatasetOwner:
    try:
        return value if isinstance(value, DatasetOwner) else DatasetOwner.model_validate(value)
    except Exception as exc:  # pydantic's ValidationError is intentionally hidden at this boundary
        raise LedgerValidationError(f"invalid dataset owner: {exc}") from exc


def _as_scope(value: Scope | Mapping[str, Any]) -> Scope:
    try:
        return (
            value
            if isinstance(value, (WorkspaceScope, SessionScope, RunScope, PersonalScope))
            else _SCOPE_ADAPTER.validate_python(value)
        )
    except Exception as exc:
        raise LedgerValidationError(f"invalid memory scope: {exc}") from exc


def _as_version(value: MemoryVersion | Mapping[str, Any]) -> MemoryVersion:
    try:
        return value if isinstance(value, MemoryVersion) else MemoryVersion.model_validate(value)
    except Exception as exc:
        raise LedgerValidationError(f"invalid memory version: {exc}") from exc


def _as_proposal(value: MemoryProposal | Mapping[str, Any]) -> MemoryProposal:
    try:
        return value if isinstance(value, MemoryProposal) else MemoryProposal.model_validate(value)
    except Exception as exc:
        raise LedgerValidationError(f"invalid memory proposal: {exc}") from exc


def _as_ref(value: MemoryVersionRef | Mapping[str, Any]) -> MemoryVersionRef:
    try:
        return (
            value if isinstance(value, MemoryVersionRef) else MemoryVersionRef.model_validate(value)
        )
    except Exception as exc:
        raise LedgerValidationError(f"invalid memory version reference: {exc}") from exc


def _as_source(value: SourceRef | Mapping[str, Any]) -> SourceRef:
    try:
        return value if isinstance(value, SourceRef) else SourceRef.model_validate(value)
    except Exception as exc:
        raise LedgerValidationError(f"invalid source reference: {exc}") from exc


def _as_tombstone(value: Tombstone | Mapping[str, Any]) -> Tombstone:
    try:
        return value if isinstance(value, Tombstone) else Tombstone.model_validate(value)
    except Exception as exc:
        raise LedgerValidationError(f"invalid tombstone: {exc}") from exc


def _model_json(value: Any) -> str:
    return _canonical_json(value)


def _scope_json(scope: Scope) -> str:
    return _model_json(scope)


def _scope_matches(actual: Scope, requested: Scope | None) -> bool:
    """Return whether a request scope can see the stored scope.

    This is an intentionally small containment rule.  Fine-grained grants and
    role/agent checks remain Core responsibilities.  A session or run never
    becomes a workspace-wide scope merely because its project matches.
    """

    if requested is None:
        return True
    if type(actual) is not type(requested):
        return False
    if isinstance(actual, WorkspaceScope) and isinstance(requested, WorkspaceScope):
        return (actual.project_id, actual.workspace_id) == (
            requested.project_id,
            requested.workspace_id,
        )
    if isinstance(actual, SessionScope) and isinstance(requested, SessionScope):
        return (actual.project_id, actual.workspace_id, actual.session_id) == (
            requested.project_id,
            requested.workspace_id,
            requested.session_id,
        )
    if isinstance(actual, RunScope) and isinstance(requested, RunScope):
        return (actual.project_id, actual.workspace_id, actual.run_id) == (
            requested.project_id,
            requested.workspace_id,
            requested.run_id,
        )
    if isinstance(actual, PersonalScope) and isinstance(requested, PersonalScope):
        return (actual.principal_id, actual.opt_in_grant_id) == (
            requested.principal_id,
            requested.opt_in_grant_id,
        )
    return False


class MemoryLedger:
    """SQLite-backed immutable memory ledger.

    ``path`` is the only required constructor argument.  The constructor
    creates the plugin-owned tables by default so a fresh temporary database is
    immediately usable; Core migrations can call :meth:`schema_sql` and apply
    the same SQL as part of their own v15 transaction.
    """

    def __init__(self, path: str | Path, *, initialize: bool = True) -> None:
        self.path = str(path)
        self._memory_connection: sqlite3.Connection | None = None
        if initialize:
            self.initialize()

    @classmethod
    def schema_sql(cls) -> str:
        """Return the standalone plugin schema for the Core v15 migration."""

        return SCHEMA_SQL

    def initialize(self) -> None:
        if self.path != ":memory:" and not self.path.startswith("file:"):
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA_SQL)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        is_memory = self.path == ":memory:"
        if is_memory:
            if self._memory_connection is None:
                self._memory_connection = sqlite3.connect(":memory:", timeout=30.0)
            connection = self._memory_connection
            close = False
        else:
            connection = sqlite3.connect(self.path, timeout=30.0)
            close = True
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        # Version deletion is guarded by a trigger.  Only a lifecycle purge
        # transaction explicitly flips this connection-local function to 1.
        connection.create_function("memory_ledger_gc_allowed", 0, lambda: 0, deterministic=True)
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            if close:
                connection.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            yield connection

    @staticmethod
    def _enable_purge(connection: sqlite3.Connection) -> None:
        connection.create_function("memory_ledger_gc_allowed", 0, lambda: 1, deterministic=True)

    @staticmethod
    def _disable_purge(connection: sqlite3.Connection) -> None:
        connection.create_function("memory_ledger_gc_allowed", 0, lambda: 0, deterministic=True)

    @staticmethod
    def _row_json(row: sqlite3.Row, field: str) -> str:
        value = row[field]
        if not isinstance(value, str):
            raise LedgerValidationError(f"stored {field} is not JSON text")
        return value

    @staticmethod
    def _version_from_row(row: sqlite3.Row) -> MemoryVersion:
        try:
            return MemoryVersion.model_validate_json(MemoryLedger._row_json(row, "body"))
        except Exception as exc:
            raise LedgerValidationError("stored memory version failed contract validation") from exc

    @staticmethod
    def _head_from_row(row: sqlite3.Row) -> MemoryHead:
        try:
            return MemoryHead.model_validate_json(MemoryLedger._row_json(row, "body"))
        except Exception as exc:
            raise LedgerValidationError("stored memory head failed contract validation") from exc

    @staticmethod
    def _proposal_from_row(row: sqlite3.Row) -> MemoryProposal:
        try:
            return MemoryProposal.model_validate_json(MemoryLedger._row_json(row, "body"))
        except Exception as exc:
            raise LedgerValidationError(
                "stored memory proposal failed contract validation"
            ) from exc

    @staticmethod
    def _tombstone_from_row(row: sqlite3.Row) -> Tombstone:
        try:
            return Tombstone.model_validate_json(MemoryLedger._row_json(row, "body"))
        except Exception as exc:
            raise LedgerValidationError("stored tombstone failed contract validation") from exc

    @staticmethod
    def _next_cursor(connection: sqlite3.Connection) -> str:
        row = connection.execute(
            "SELECT value FROM memory_ledger_meta WHERE key = 'publication_cursor'"
        ).fetchone()
        current = int(row["value"]) if row is not None else 0
        if current >= 2**63 - 1:
            raise LedgerConflictError("publication cursor exhausted")
        value = current + 1
        connection.execute(
            "INSERT INTO memory_ledger_meta(key, value) VALUES ('publication_cursor', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(value),),
        )
        return str(value)

    @staticmethod
    def _check_expected(value: int | None, actual: int, *, label: str = "head revision") -> None:
        if value is not None and value != actual:
            raise LedgerConflictError(f"{label} conflict: expected {value}, got {actual}")

    @staticmethod
    def _check_owner(version: MemoryVersion, dataset_id: str | None = None) -> None:
        if dataset_id is not None and version.owner.dataset_id != dataset_id:
            raise LedgerConflictError("unknown_owner: version is outside the requested dataset")
        if version.owner.owner_namespace != f"dataset:{version.owner.dataset_id}":
            raise LedgerValidationError("unknown_owner: namespace does not match dataset")

    @staticmethod
    def _head_for_create(
        dataset_id: str,
        record_id: str,
        *,
        permission_epoch: int = 0,
    ) -> MemoryHead:
        return MemoryHead(
            dataset_id=dataset_id,
            record_id=record_id,
            published_version=None,
            revision=0,
            publication_cursor="0",
            state="unpublished",
            permission_epoch=permission_epoch,
        )

    @staticmethod
    def _insert_head(connection: sqlite3.Connection, head: MemoryHead) -> None:
        connection.execute(
            """
            INSERT INTO memory_ledger_heads(
                dataset_id, record_id, published_version, published_digest,
                revision, publication_cursor, state, permission_epoch, body
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                head.dataset_id,
                head.record_id,
                head.published_version.version if head.published_version else None,
                head.published_version.content_digest if head.published_version else None,
                head.revision,
                head.publication_cursor,
                head.state,
                head.permission_epoch,
                _model_json(head),
            ),
        )

    @staticmethod
    def _insert_version(connection: sqlite3.Connection, version: MemoryVersion) -> None:
        actual_digest = hashlib.sha256(version.content.encode("utf-8")).hexdigest()
        if actual_digest != version.ref.content_digest:
            raise LedgerValidationError(
                "memory version content digest does not match its immutable reference"
            )
        body = _model_json(version)
        try:
            connection.execute(
                """
                INSERT INTO memory_ledger_versions(
                    dataset_id, record_id, version, content_digest, body, body_hash,
                    owner_namespace, kind, scope_kind, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version.ref.dataset_id,
                    version.ref.record_id,
                    version.ref.version,
                    version.ref.content_digest,
                    body,
                    _digest(body),
                    version.owner.owner_namespace,
                    version.kind,
                    version.scope.kind,
                    version.recorded_at.isoformat(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise LedgerConflictError(
                f"memory version already exists or violates its immutable identity: "
                f"{version.ref.dataset_id}/{version.ref.record_id}@{version.ref.version}"
            ) from exc

    @staticmethod
    def _stored_version(
        connection: sqlite3.Connection,
        dataset_id: str,
        record_id: str,
        version: int,
    ) -> MemoryVersion | None:
        row = connection.execute(
            """
            SELECT body FROM memory_ledger_versions
            WHERE dataset_id = ? AND record_id = ? AND version = ?
            """,
            (dataset_id, record_id, version),
        ).fetchone()
        return None if row is None else MemoryVersion.model_validate_json(row["body"])

    @staticmethod
    def _stored_head(
        connection: sqlite3.Connection,
        dataset_id: str,
        record_id: str,
    ) -> MemoryHead | None:
        row = connection.execute(
            """
            SELECT body FROM memory_ledger_heads
            WHERE dataset_id = ? AND record_id = ?
            """,
            (dataset_id, record_id),
        ).fetchone()
        return None if row is None else MemoryHead.model_validate_json(row["body"])

    @staticmethod
    def _stored_proposal(
        connection: sqlite3.Connection,
        proposal_id: str,
    ) -> MemoryProposal | None:
        row = connection.execute(
            "SELECT body FROM memory_ledger_proposals WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
        return None if row is None else MemoryProposal.model_validate_json(row["body"])

    @staticmethod
    def _stored_tombstone(
        connection: sqlite3.Connection,
        dataset_id: str,
        record_id: str | None,
    ) -> Tombstone | None:
        key = record_id if record_id is not None else "__dataset__"
        row = connection.execute(
            """
            SELECT body FROM memory_ledger_tombstones
            WHERE dataset_id = ? AND tombstone_key = ?
            """,
            (dataset_id, key),
        ).fetchone()
        return None if row is None else Tombstone.model_validate_json(row["body"])

    @staticmethod
    def _idempotency_result(
        connection: sqlite3.Connection,
        operation: str,
        dataset_id: str,
        idempotency_key: str | None,
        request_digest: str,
    ) -> tuple[str, str] | None:
        if idempotency_key is None:
            return None
        row = connection.execute(
            """
            SELECT request_digest, result_type, result_body
            FROM memory_ledger_idempotency
            WHERE operation = ? AND dataset_id = ? AND idempotency_key = ?
            """,
            (operation, dataset_id, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        if row["request_digest"] != request_digest:
            raise IdempotencyConflictError(
                f"idempotency key reused with a different request: {operation}/{idempotency_key}"
            )
        return str(row["result_type"]), str(row["result_body"])

    @staticmethod
    def _store_idempotency(
        connection: sqlite3.Connection,
        operation: str,
        dataset_id: str,
        idempotency_key: str | None,
        request_digest: str,
        result_type: str,
        result: Any,
    ) -> None:
        if idempotency_key is None:
            return
        try:
            connection.execute(
                """
                INSERT INTO memory_ledger_idempotency(
                    operation, dataset_id, idempotency_key, request_digest,
                    result_type, result_body, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operation,
                    dataset_id,
                    idempotency_key,
                    request_digest,
                    result_type,
                    _model_json(result),
                    _now_iso(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise IdempotencyConflictError(
                f"idempotency key was concurrently claimed: {operation}/{idempotency_key}"
            ) from exc

    @staticmethod
    def _decode_idempotency(result_type: str, body: str) -> Any:
        if result_type == "version":
            return MemoryVersion.model_validate_json(body)
        if result_type == "proposal":
            return MemoryProposal.model_validate_json(body)
        if result_type == "head":
            return MemoryHead.model_validate_json(body)
        if result_type == "tombstone":
            return Tombstone.model_validate_json(body)
        raise LedgerValidationError(f"unknown stored idempotency result type: {result_type}")

    def save_version(
        self,
        version: MemoryVersion | Mapping[str, Any],
        *,
        dataset_id: str | None = None,
        expected_head_revision: int | None = None,
        permission_epoch: int = 0,
        idempotency_key: str | None = None,
        request_digest: str | None = None,
        _allow_version_gap: bool = False,
    ) -> MemoryVersion:
        """Append one immutable candidate version without publishing it."""

        normalized = _as_version(version)
        self._check_owner(normalized, dataset_id)
        target_dataset = normalized.ref.dataset_id
        digest = _request_fingerprint(
            "save_version",
            {
                "dataset_id": dataset_id,
                "version": normalized,
                "expected_head_revision": expected_head_revision,
                "permission_epoch": permission_epoch,
                "allow_version_gap": _allow_version_gap,
            },
            request_digest,
        )
        with self._write() as connection:
            cached = self._idempotency_result(
                connection, "save_version", target_dataset, idempotency_key, digest
            )
            if cached is not None:
                result = self._decode_idempotency(*cached)
                if not isinstance(result, MemoryVersion):
                    raise LedgerValidationError("idempotency result type mismatch for save_version")
                return result
            if (
                self._stored_tombstone(connection, target_dataset, normalized.ref.record_id)
                is not None
                or self._stored_tombstone(connection, target_dataset, None) is not None
            ):
                raise LedgerConflictError("record is tombstoned and cannot receive a late version")

            existing = self._stored_version(
                connection,
                normalized.ref.dataset_id,
                normalized.ref.record_id,
                normalized.ref.version,
            )
            if existing is not None:
                if _model_json(existing) != _model_json(normalized):
                    raise LedgerConflictError(
                        "immutable memory version identity has different content"
                    )
                head = self._stored_head(connection, target_dataset, normalized.ref.record_id)
                if head is None:
                    self._check_expected(expected_head_revision, 0)
                    self._insert_head(
                        connection,
                        self._head_for_create(
                            target_dataset,
                            normalized.ref.record_id,
                            permission_epoch=permission_epoch,
                        ),
                    )
                self._store_idempotency(
                    connection,
                    "save_version",
                    target_dataset,
                    idempotency_key,
                    digest,
                    "version",
                    existing,
                )
                return existing

            head = self._stored_head(connection, target_dataset, normalized.ref.record_id)
            if head is None:
                self._check_expected(expected_head_revision, 0)
                if (
                    self._stored_tombstone(connection, target_dataset, normalized.ref.record_id)
                    is not None
                ):
                    raise LedgerConflictError(
                        "record is tombstoned and cannot receive a late version"
                    )
                if normalized.ref.version != 1 and not _allow_version_gap:
                    raise LedgerConflictError("a new memory record must start at version 1")
                self._insert_head(
                    connection,
                    self._head_for_create(
                        target_dataset,
                        normalized.ref.record_id,
                        permission_epoch=permission_epoch,
                    ),
                )
            else:
                if head.state in {"deleted", "revoked"}:
                    raise LedgerConflictError(
                        "record is tombstoned and cannot receive a late version"
                    )
                self._check_expected(expected_head_revision, head.revision)
                max_row = connection.execute(
                    """
                    SELECT MAX(version) AS max_version FROM memory_ledger_versions
                    WHERE dataset_id = ? AND record_id = ?
                    """,
                    (target_dataset, normalized.ref.record_id),
                ).fetchone()
                max_version = int(max_row["max_version"] or 0)
                if not _allow_version_gap and normalized.ref.version != max_version + 1:
                    raise LedgerConflictError(
                        f"memory version must advance from {max_version} to {max_version + 1}"
                    )
            self._insert_version(connection, normalized)
            self._store_idempotency(
                connection,
                "save_version",
                target_dataset,
                idempotency_key,
                digest,
                "version",
                normalized,
            )
        return normalized

    # Short aliases are intentionally boring so plugin code does not need to
    # know whether the underlying implementation calls this an append or save.
    append_version = save_version
    save = save_version

    def get_head(self, dataset_id: str, record_id: str) -> MemoryHead:
        with self._connect() as connection:
            head = self._stored_head(connection, dataset_id, record_id)
        if head is None:
            raise LedgerNotFoundError(f"memory head not found: {dataset_id}/{record_id}")
        return head

    def list_heads(
        self,
        dataset_id: str,
        *,
        scope: Scope | Mapping[str, Any] | None = None,
        states: Iterable[str] | None = None,
        include_deleted: bool = False,
    ) -> list[MemoryHead]:
        """Read every publication head owned by one dataset.

        A head has no scope field of its own.  When a scope filter is given,
        the currently published version supplies the scope; unpublished heads
        are excluded because there is no authorized version to inspect.
        """

        requested_scope = _as_scope(scope) if scope is not None else None
        allowed_states = set(states) if states is not None else None
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT body FROM memory_ledger_heads WHERE dataset_id = ? ORDER BY record_id",
                (dataset_id,),
            ).fetchall()
            heads = [MemoryHead.model_validate_json(row["body"]) for row in rows]
            versions: dict[tuple[str, int], MemoryVersion] = {}
            if requested_scope is not None:
                for head in heads:
                    ref = head.published_version
                    if ref is None:
                        continue
                    row = connection.execute(
                        """
                        SELECT body FROM memory_ledger_versions
                        WHERE dataset_id = ? AND record_id = ? AND version = ?
                        """,
                        (dataset_id, head.record_id, ref.version),
                    ).fetchone()
                    if row is not None:
                        versions[(head.record_id, ref.version)] = MemoryVersion.model_validate_json(
                            row["body"]
                        )
        result: list[MemoryHead] = []
        for head in heads:
            if not include_deleted and head.state == "deleted":
                continue
            if allowed_states is not None and head.state not in allowed_states:
                continue
            if requested_scope is not None:
                ref = head.published_version
                version = None if ref is None else versions.get((head.record_id, ref.version))
                if version is None or not _scope_matches(version.scope, requested_scope):
                    continue
            result.append(head)
        return result

    # ``heads`` is the compact spelling used by the manager integration.
    heads = list_heads
    get_heads = list_heads

    def authorize_ref(
        self,
        ref: MemoryVersionRef | Mapping[str, Any],
        *,
        dataset_id: str | None = None,
        scope: Scope | Mapping[str, Any] | None = None,
        include_inactive: bool = False,
        include_legacy: bool = False,
        allow_candidate: bool = False,
    ) -> MemoryVersion:
        """Resolve one exact ref after dataset/head/scope checks.

        Core performs caller and grant checks before invoking this method.  The
        ledger still refuses cross-dataset refs, stale/non-head refs, deleted
        heads, and legacy evidence unless explicitly requested.
        """

        normalized = _as_ref(ref)
        if dataset_id is not None and dataset_id != normalized.dataset_id:
            raise LedgerConflictError("unknown_owner: reference is outside the requested dataset")
        requested_scope = _as_scope(scope) if scope is not None else None
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT body FROM memory_ledger_versions
                WHERE dataset_id = ? AND record_id = ? AND version = ?
                """,
                (normalized.dataset_id, normalized.record_id, normalized.version),
            ).fetchone()
            if row is None:
                raise LedgerNotFoundError(
                    "memory version not found: "
                    f"{normalized.dataset_id}/{normalized.record_id}@{normalized.version}"
                )
            version = MemoryVersion.model_validate_json(row["body"])
            head = self._stored_head(connection, normalized.dataset_id, normalized.record_id)
        if version.ref != normalized:
            raise LedgerConflictError(
                "memory version reference digest does not match stored content"
            )
        if head is None:
            raise LedgerNotFoundError("memory head is missing for the referenced version")
        published = head.published_version == normalized
        candidate = (
            not published
            and allow_candidate
            and normalized
            in {
                item.proposed_version
                for item in self.list_proposals(
                    normalized.dataset_id,
                    normalized.record_id,
                    states=("pending",),
                )
            }
        )
        if not published and not candidate:
            raise LedgerConflictError("memory reference is not the current published head")
        if head.state == "deleted" or (
            head.state in {"inactive", "revoked"} and not include_inactive
        ):
            raise LedgerConflictError("memory reference is inactive or deleted")
        if version.evidence == "legacy_unverified" and not include_legacy:
            raise LedgerConflictError("legacy memory evidence requires explicit review")
        if requested_scope is not None and not _scope_matches(version.scope, requested_scope):
            raise LedgerConflictError("memory reference is outside the requested scope")
        return version

    authorize = authorize_ref

    def get_version(
        self,
        dataset_id: str,
        record_id: str,
        version: int | None = None,
    ) -> MemoryVersion:
        with self._connect() as connection:
            if version is None:
                head = self._stored_head(connection, dataset_id, record_id)
                if head is None or head.published_version is None:
                    raise LedgerNotFoundError(
                        f"published memory version not found: {dataset_id}/{record_id}"
                    )
                version = head.published_version.version
            result = self._stored_version(connection, dataset_id, record_id, version)
        if result is None:
            raise LedgerNotFoundError(
                f"memory version not found: {dataset_id}/{record_id}@{version}"
            )
        return result

    def list_versions(self, dataset_id: str, record_id: str) -> list[MemoryVersion]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT body FROM memory_ledger_versions
                WHERE dataset_id = ? AND record_id = ? ORDER BY version
                """,
                (dataset_id, record_id),
            ).fetchall()
        if not rows:
            raise LedgerNotFoundError(f"memory record not found: {dataset_id}/{record_id}")
        return [MemoryVersion.model_validate_json(row["body"]) for row in rows]

    list_memory_versions = list_versions

    def propose(
        self,
        version: MemoryVersion | Mapping[str, Any] | None = None,
        *,
        proposal: MemoryProposal | Mapping[str, Any] | None = None,
        operation: str | None = None,
        source_refs: Sequence[SourceRef | Mapping[str, Any]] | None = None,
        extractor_version: str = "user",
        reason: str = "explicit user save",
        expected_head_revision: int | None = None,
        proposal_id: str | None = None,
        idempotency_key: str | None = None,
        request_digest: str | None = None,
        permission_epoch: int = 0,
        transaction_guard: Callable[[sqlite3.Connection, MemoryProposal], None] | None = None,
        commit_callback: Callable[[sqlite3.Connection, MemoryProposal], None] | None = None,
    ) -> MemoryProposal:
        """Persist a pending Proposal and leave the publication head alone."""

        if transaction_guard is not None and commit_callback is not None:
            raise LedgerValidationError("propose accepts only one transaction metadata callback")
        guard = transaction_guard or commit_callback

        supplied_proposal = _as_proposal(proposal) if proposal is not None else None
        normalized_version = _as_version(version) if version is not None else None
        if supplied_proposal is None and normalized_version is None:
            raise LedgerValidationError("propose requires a memory version or a complete proposal")
        if (
            supplied_proposal is not None
            and normalized_version is not None
            and supplied_proposal.proposed_version != normalized_version.ref
        ):
            raise LedgerConflictError("proposal target does not match supplied version")
        if normalized_version is not None:
            self._check_owner(normalized_version)
            dataset_id = normalized_version.ref.dataset_id
            record_id = normalized_version.ref.record_id
        else:
            assert supplied_proposal is not None
            dataset_id = supplied_proposal.owner.dataset_id
            record_id = supplied_proposal.base_head.record_id

        normalized_source_refs = (
            None if source_refs is None else tuple(_as_source(item) for item in source_refs)
        )
        if normalized_version is not None:
            if (
                normalized_source_refs is not None
                and normalized_source_refs != normalized_version.sources
            ):
                raise LedgerConflictError(
                    "proposal source references must match target memory version"
                )
            if supplied_proposal is not None and (
                supplied_proposal.owner != normalized_version.owner
                or supplied_proposal.source_refs != normalized_version.sources
            ):
                raise LedgerConflictError(
                    "proposal owner or source references do not match target memory version"
                )
        proposal_digest = _request_fingerprint(
            "propose",
            {
                "dataset_id": dataset_id,
                "record_id": record_id,
                "version": normalized_version,
                "proposal": supplied_proposal,
                "expected_head_revision": expected_head_revision,
                "permission_epoch": permission_epoch,
                "operation": operation,
                "source_refs": normalized_source_refs,
                "reason": reason,
                "extractor_version": extractor_version,
                "proposal_id": proposal_id,
            },
            request_digest,
        )
        with self._write() as connection:
            cached = self._idempotency_result(
                connection, "propose", dataset_id, idempotency_key, proposal_digest
            )
            if cached is not None:
                result = self._decode_idempotency(*cached)
                if not isinstance(result, MemoryProposal):
                    raise LedgerValidationError("idempotency result type mismatch for propose")
                if guard is not None:
                    guard(connection, result)
                return result

            head = self._stored_head(connection, dataset_id, record_id)
            if head is None:
                if (
                    self._stored_tombstone(connection, dataset_id, record_id) is not None
                    or self._stored_tombstone(connection, dataset_id, None) is not None
                ):
                    raise LedgerConflictError("record is tombstoned and cannot receive a proposal")
                if normalized_version is None:
                    assert supplied_proposal is not None
                    head = supplied_proposal.base_head
                    if head.dataset_id != dataset_id or head.record_id != record_id:
                        raise LedgerConflictError(
                            "proposal base head is outside its dataset record"
                        )
                else:
                    head = self._head_for_create(
                        dataset_id,
                        record_id,
                        permission_epoch=permission_epoch,
                    )
                self._insert_head(connection, head)
            if head.state in {"deleted", "revoked"}:
                raise LedgerConflictError("record is tombstoned and cannot receive a proposal")
            self._check_expected(expected_head_revision, head.revision)

            if normalized_version is not None:
                stored = self._stored_version(
                    connection,
                    dataset_id,
                    record_id,
                    normalized_version.ref.version,
                )
                if stored is None:
                    max_row = connection.execute(
                        """
                        SELECT MAX(version) AS max_version FROM memory_ledger_versions
                        WHERE dataset_id = ? AND record_id = ?
                        """,
                        (dataset_id, record_id),
                    ).fetchone()
                    max_version = int(max_row["max_version"] or 0)
                    if normalized_version.ref.version != max_version + 1:
                        raise LedgerConflictError(
                            f"proposal version must advance from {max_version} to {max_version + 1}"
                        )
                    self._insert_version(connection, normalized_version)
                elif _model_json(stored) != _model_json(normalized_version):
                    raise LedgerConflictError(
                        "proposal version identity has different immutable content"
                    )
                target_ref = normalized_version.ref
                owner = normalized_version.owner
                refs = (
                    normalized_source_refs
                    if normalized_source_refs is not None
                    else normalized_version.sources
                )
                operation_value = operation or ("create" if head.revision == 0 else "modify")
                if operation_value not in {"create", "modify", "merge", "supersede", "revoke"}:
                    raise LedgerValidationError(
                        f"unsupported proposal operation: {operation_value}"
                    )
                built = MemoryProposal(
                    proposal_id=proposal_id or f"proposal_{uuid4().hex}",
                    proposal_revision=0,
                    owner=owner,
                    operation=cast(_ProposalOperation, operation_value),
                    base_head=head,
                    proposed_version=target_ref,
                    source_refs=refs,
                    extractor_version=extractor_version,
                    reason=reason,
                    state="pending",
                )
            else:
                assert supplied_proposal is not None
                built = supplied_proposal
                if built.base_head != head:
                    raise LedgerConflictError("proposal base head is stale")
                target = self._stored_version(
                    connection,
                    dataset_id,
                    record_id,
                    built.proposed_version.version,
                )
                if target is None or target.ref != built.proposed_version:
                    raise LedgerNotFoundError("proposal target version is not stored")
                if built.owner != target.owner:
                    raise LedgerConflictError("proposal owner does not match target memory version")
                if built.source_refs != target.sources:
                    raise LedgerConflictError(
                        "proposal source references must match target memory version"
                    )
                if built.state != "pending":
                    raise LedgerConflictError("only pending proposals can be submitted")

            try:
                connection.execute(
                    """
                    INSERT INTO memory_ledger_proposals(
                        proposal_id, dataset_id, record_id, proposal_revision,
                        base_head_revision, proposed_version, state, body,
                        idempotency_key, request_digest, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        built.proposal_id,
                        dataset_id,
                        record_id,
                        built.proposal_revision,
                        built.base_head.revision,
                        built.proposed_version.version,
                        built.state,
                        _model_json(built),
                        idempotency_key,
                        proposal_digest,
                        _now_iso(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                existing = self._stored_proposal(connection, built.proposal_id)
                if existing is not None and _model_json(existing) == _model_json(built):
                    if guard is not None:
                        guard(connection, existing)
                    return existing
                raise LedgerConflictError(
                    "proposal identity or idempotency key already exists"
                ) from exc
            self._store_idempotency(
                connection,
                "propose",
                dataset_id,
                idempotency_key,
                proposal_digest,
                "proposal",
                built,
            )
            if guard is not None:
                guard(connection, built)
        return built

    create_proposal = propose

    def get_proposal(self, proposal_id: str) -> MemoryProposal:
        with self._connect() as connection:
            result = self._stored_proposal(connection, proposal_id)
        if result is None:
            raise LedgerNotFoundError(f"memory proposal not found: {proposal_id}")
        return result

    def list_proposals(
        self,
        dataset_id: str,
        record_id: str | None = None,
        *,
        states: Iterable[str] | None = None,
    ) -> list[MemoryProposal]:
        clauses = ["dataset_id = ?"]
        values: list[Any] = [dataset_id]
        if record_id is not None:
            clauses.append("record_id = ?")
            values.append(record_id)
        if states is not None:
            normalized_states = tuple(states)
            if not normalized_states:
                return []
            clauses.append("state IN (" + ",".join("?" for _ in normalized_states) + ")")
            values.extend(normalized_states)
        query = (
            "SELECT body FROM memory_ledger_proposals WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at, proposal_id"
        )
        with self._connect() as connection:
            rows = connection.execute(query, values).fetchall()
        return [MemoryProposal.model_validate_json(row["body"]) for row in rows]

    def _mark_proposal_state(
        self,
        connection: sqlite3.Connection,
        proposal: MemoryProposal,
        state: str,
    ) -> MemoryProposal:
        updated = proposal.model_copy(
            update={"proposal_revision": proposal.proposal_revision + 1, "state": state}
        )
        connection.execute(
            """
            UPDATE memory_ledger_proposals
            SET proposal_revision = ?, state = ?, body = ?
            WHERE proposal_id = ? AND proposal_revision = ?
            """,
            (
                updated.proposal_revision,
                updated.state,
                _model_json(updated),
                proposal.proposal_id,
                proposal.proposal_revision,
            ),
        )
        return updated

    def confirm_proposal(
        self,
        proposal: str | MemoryProposal,
        *,
        dataset_id: str | None = None,
        expected_head_revision: int | None = None,
        permission_epoch: int | None = None,
        idempotency_key: str | None = None,
        request_digest: str | None = None,
    ) -> MemoryHead:
        """Explicitly publish a pending Proposal using one atomic head CAS."""

        proposal_id = proposal.proposal_id if isinstance(proposal, MemoryProposal) else proposal
        with self._write() as connection:
            stored = self._stored_proposal(connection, proposal_id)
            if stored is None:
                raise LedgerNotFoundError(f"memory proposal not found: {proposal_id}")
            target_dataset = stored.owner.dataset_id
            if dataset_id is not None and dataset_id != target_dataset:
                raise LedgerConflictError(
                    "unknown_owner: proposal is outside the requested dataset"
                )
            digest = request_digest or _digest(
                {
                    "proposal_id": proposal_id,
                    "expected_head_revision": expected_head_revision,
                    "permission_epoch": permission_epoch,
                }
            )
            cached = self._idempotency_result(
                connection, "confirm_proposal", target_dataset, idempotency_key, digest
            )
            if cached is not None:
                result = self._decode_idempotency(*cached)
                if not isinstance(result, MemoryHead):
                    raise LedgerValidationError(
                        "idempotency result type mismatch for confirm_proposal"
                    )
                return result
            head = self._stored_head(connection, target_dataset, stored.base_head.record_id)
            if head is None:
                raise LedgerNotFoundError("proposal record head is missing")
            if stored.state == "accepted":
                if head.published_version == stored.proposed_version and head.state == "published":
                    self._store_idempotency(
                        connection,
                        "confirm_proposal",
                        target_dataset,
                        idempotency_key,
                        digest,
                        "head",
                        head,
                    )
                    return head
                raise LedgerConflictError(
                    "accepted proposal no longer matches its publication head"
                )
            if stored.state != "pending":
                raise LedgerConflictError(f"proposal is {stored.state}, not pending")
            expected = (
                stored.base_head.revision
                if expected_head_revision is None
                else expected_head_revision
            )
            if head.revision != expected or head.revision != stored.base_head.revision:
                self._mark_proposal_state(connection, stored, "conflict")
                connection.commit()
                raise LedgerConflictError(
                    "head revision conflict: "
                    f"proposal expected {stored.base_head.revision}, got {head.revision}"
                )
            if head.state in {"deleted", "revoked"}:
                self._mark_proposal_state(connection, stored, "conflict")
                connection.commit()
                raise LedgerConflictError("record is tombstoned and cannot be published")
            target = self._stored_version(
                connection,
                target_dataset,
                stored.proposed_version.record_id,
                stored.proposed_version.version,
            )
            if target is None or target.ref != stored.proposed_version:
                raise LedgerNotFoundError("proposal target version is missing or changed")
            if permission_epoch is not None and head.permission_epoch != permission_epoch:
                self._mark_proposal_state(connection, stored, "conflict")
                connection.commit()
                raise LedgerConflictError("permission epoch conflict")

            cursor = self._next_cursor(connection)
            next_head = head.model_copy(
                update={
                    "published_version": stored.proposed_version,
                    "revision": head.revision + 1,
                    "publication_cursor": cursor,
                    "state": "published",
                    "permission_epoch": head.permission_epoch
                    if permission_epoch is None
                    else permission_epoch,
                }
            )
            updated = connection.execute(
                """
                UPDATE memory_ledger_heads
                SET published_version = ?, published_digest = ?, revision = ?,
                    publication_cursor = ?, state = ?, permission_epoch = ?, body = ?
                WHERE dataset_id = ? AND record_id = ? AND revision = ?
                """,
                (
                    next_head.published_version.version if next_head.published_version else None,
                    next_head.published_version.content_digest
                    if next_head.published_version
                    else None,
                    next_head.revision,
                    next_head.publication_cursor,
                    next_head.state,
                    next_head.permission_epoch,
                    _model_json(next_head),
                    target_dataset,
                    head.record_id,
                    expected,
                ),
            )
            if updated.rowcount != 1:
                self._mark_proposal_state(connection, stored, "conflict")
                connection.commit()
                raise LedgerConflictError("head changed during publication")
            accepted = self._mark_proposal_state(connection, stored, "accepted")
            del accepted  # State is persisted for readback; the returned object is the head.
            self._store_idempotency(
                connection,
                "confirm_proposal",
                target_dataset,
                idempotency_key,
                digest,
                "head",
                next_head,
            )
            return next_head

    publish = confirm_proposal
    accept_proposal = confirm_proposal
    confirm = confirm_proposal

    @staticmethod
    def _batch_review_field(entry: Any, name: str, *, default: Any = None) -> Any:
        """Read an internal exact-review DTO without importing public contracts.

        B2-5's public ``ExactProposal`` is owned by Core.  The ledger accepts
        that DTO, a plain mapping, or a ``MemoryProposal`` so the publication
        primitive remains independent from the protocol package.
        """

        if isinstance(entry, Mapping):
            return entry.get(name, default)
        return getattr(entry, name, default)

    @classmethod
    def _normalize_batch_reviews(cls, entries: Sequence[Any]) -> list[dict[str, Any]]:
        if not entries:
            raise LedgerValidationError("proposal review batch cannot be empty")
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for entry in entries:
            proposal_id = cls._batch_review_field(entry, "proposal_id")
            proposal_revision = cls._batch_review_field(entry, "proposal_revision")
            proposed_version = cls._batch_review_field(entry, "proposed_version")
            base_head_revision = cls._batch_review_field(entry, "base_head_revision")
            if base_head_revision is None:
                base_head = cls._batch_review_field(entry, "base_head")
                base_head_revision = cls._batch_review_field(base_head, "revision")
            if not isinstance(proposal_id, str) or not proposal_id:
                raise LedgerValidationError("proposal review requires proposal_id")
            if proposal_id in seen:
                raise LedgerConflictError("proposal review batch contains a duplicate proposal")
            if (
                not isinstance(proposal_revision, int)
                or isinstance(proposal_revision, bool)
                or proposal_revision < 0
            ):
                raise LedgerValidationError("proposal review requires an exact proposal_revision")
            if (
                not isinstance(base_head_revision, int)
                or isinstance(base_head_revision, bool)
                or base_head_revision < 0
            ):
                raise LedgerValidationError("proposal review requires an exact base_head_revision")
            normalized_ref = _as_ref(proposed_version)
            seen.add(proposal_id)
            normalized.append(
                {
                    "proposal_id": proposal_id,
                    "proposal_revision": proposal_revision,
                    "proposed_version": normalized_ref,
                    "base_head_revision": base_head_revision,
                }
            )
        return normalized

    @staticmethod
    def _decode_batch(result_type: str, body: str) -> tuple[Any, ...]:
        raw = json.loads(body)
        if not isinstance(raw, list):
            raise LedgerValidationError("stored proposal review result is not a list")
        if result_type == "batch_heads":
            return tuple(MemoryHead.model_validate(item) for item in raw)
        if result_type == "batch_proposals":
            return tuple(MemoryProposal.model_validate(item) for item in raw)
        raise LedgerValidationError(f"unknown stored proposal review result type: {result_type}")

    def review_proposals_batch(
        self,
        entries: Sequence[Any],
        *,
        decision: Literal["accept", "reject"] = "accept",
        dataset_id: str | None = None,
        permission_epoch: int | None = None,
        idempotency_key: str | None = None,
        request_digest: str | None = None,
        transaction_guard: Callable[[sqlite3.Connection, tuple[MemoryProposal, ...]], None]
        | None = None,
    ) -> tuple[MemoryHead | MemoryProposal, ...]:
        """Review several proposals after one complete validation pass.

        Every entry must carry the exact proposal id, proposal revision,
        immutable proposed version reference (including its digest), and base
        head revision.  The method keeps a single ``BEGIN IMMEDIATE``
        transaction open while validating all entries and only then mutates
        any head or proposal.  A stale entry therefore rolls back the entire
        batch instead of leaving an earlier entry accepted.
        """

        if decision not in {"accept", "reject"}:
            raise LedgerValidationError("proposal review decision must be accept or reject")
        normalized = self._normalize_batch_reviews(entries)
        request = {
            "entries": normalized,
            "decision": decision,
            "dataset_id": dataset_id,
            "permission_epoch": permission_epoch,
        }
        # Resolve the dataset outside the transaction only for the idempotency
        # table key.  The authoritative owner checks happen again below while
        # the write lock is held.
        requested_dataset = dataset_id
        if requested_dataset is None:
            requested_dataset = normalized[0]["proposed_version"].dataset_id
        digest = _request_fingerprint("review_proposals_batch", request, request_digest)
        result_type = "batch_heads" if decision == "accept" else "batch_proposals"
        with self._write() as connection:
            cached = self._idempotency_result(
                connection,
                "review_proposals_batch",
                requested_dataset,
                idempotency_key,
                digest,
            )
            if cached is not None:
                return self._decode_batch(*cached)

            stored_entries: list[tuple[dict[str, Any], MemoryProposal, MemoryHead]] = []
            record_ids: set[tuple[str, str]] = set()
            actual_dataset: str | None = None
            for item in normalized:
                stored = self._stored_proposal(connection, item["proposal_id"])
                if stored is None:
                    raise LedgerNotFoundError(f"memory proposal not found: {item['proposal_id']}")
                if actual_dataset is None:
                    actual_dataset = stored.owner.dataset_id
                if stored.owner.dataset_id != actual_dataset:
                    raise LedgerConflictError("proposal review batch must use one dataset")
                if dataset_id is not None and stored.owner.dataset_id != dataset_id:
                    raise LedgerConflictError(
                        "unknown_owner: proposal is outside the requested dataset"
                    )
                if stored.proposal_revision != item["proposal_revision"]:
                    raise LedgerConflictError(
                        f"proposal revision conflict: expected {item['proposal_revision']}, "
                        f"got {stored.proposal_revision}"
                    )
                if stored.proposed_version != item["proposed_version"]:
                    raise LedgerConflictError("proposed version reference conflict")
                if stored.base_head.revision != item["base_head_revision"]:
                    raise LedgerConflictError("base head revision conflict")
                if stored.state != "pending":
                    raise LedgerConflictError(f"proposal is {stored.state}, not pending")
                key = (stored.owner.dataset_id, stored.base_head.record_id)
                if key in record_ids:
                    raise LedgerConflictError(
                        "proposal review batch contains multiple proposals for one record"
                    )
                record_ids.add(key)
                head = self._stored_head(connection, *key)
                if head is None:
                    raise LedgerNotFoundError("proposal record head is missing")
                if decision == "accept" and head.revision != item["base_head_revision"]:
                    raise LedgerConflictError(
                        f"head revision conflict: expected {item['base_head_revision']}, "
                        f"got {head.revision}"
                    )
                if decision == "accept" and head.state in {"deleted", "revoked"}:
                    raise LedgerConflictError("record is tombstoned and cannot be reviewed")
                if permission_epoch is not None and head.permission_epoch != permission_epoch:
                    raise LedgerConflictError("permission epoch conflict")
                target = self._stored_version(
                    connection,
                    stored.proposed_version.dataset_id,
                    stored.proposed_version.record_id,
                    stored.proposed_version.version,
                )
                if target is None or target.ref != stored.proposed_version:
                    raise LedgerNotFoundError("proposal target version is missing or changed")
                stored_entries.append((item, stored, head))

            if actual_dataset is None:
                raise LedgerValidationError("proposal review batch has no dataset")
            if requested_dataset != actual_dataset:
                raise LedgerConflictError("proposal review dataset does not match its target")

            if transaction_guard is not None:
                transaction_guard(
                    connection, tuple(stored for _item, stored, _head in stored_entries)
                )

            if decision == "reject":
                results: tuple[MemoryHead | MemoryProposal, ...] = tuple(
                    self._mark_proposal_state(connection, stored, "rejected")
                    for _item, stored, _head in stored_entries
                )
            else:
                accepted: list[MemoryHead] = []
                for _item, stored, head in stored_entries:
                    cursor = self._next_cursor(connection)
                    next_head = head.model_copy(
                        update={
                            "published_version": stored.proposed_version,
                            "revision": head.revision + 1,
                            "publication_cursor": cursor,
                            "state": "published",
                            "permission_epoch": head.permission_epoch
                            if permission_epoch is None
                            else permission_epoch,
                        }
                    )
                    updated = connection.execute(
                        """
                        UPDATE memory_ledger_heads
                        SET published_version = ?, published_digest = ?, revision = ?,
                            publication_cursor = ?, state = ?, permission_epoch = ?, body = ?
                        WHERE dataset_id = ? AND record_id = ? AND revision = ?
                        """,
                        (
                            next_head.published_version.version
                            if next_head.published_version
                            else None,
                            next_head.published_version.content_digest
                            if next_head.published_version
                            else None,
                            next_head.revision,
                            next_head.publication_cursor,
                            next_head.state,
                            next_head.permission_epoch,
                            _model_json(next_head),
                            head.dataset_id,
                            head.record_id,
                            head.revision,
                        ),
                    )
                    if updated.rowcount != 1:
                        raise LedgerConflictError("head changed during proposal review")
                    self._mark_proposal_state(connection, stored, "accepted")
                    accepted.append(next_head)
                results = tuple(accepted)

            self._store_idempotency(
                connection,
                "review_proposals_batch",
                actual_dataset,
                idempotency_key,
                digest,
                result_type,
                results,
            )
            return results

    def confirm_proposals(
        self,
        entries: Sequence[Any],
        **kwargs: Any,
    ) -> tuple[MemoryHead, ...]:
        """Convenience alias for an atomic acceptance batch."""

        result = self.review_proposals_batch(entries, decision="accept", **kwargs)
        return tuple(item for item in result if isinstance(item, MemoryHead))

    def reject_proposals(
        self,
        entries: Sequence[Any],
        **kwargs: Any,
    ) -> tuple[MemoryProposal, ...]:
        """Convenience alias for an atomic rejection batch."""

        result = self.review_proposals_batch(entries, decision="reject", **kwargs)
        return tuple(item for item in result if isinstance(item, MemoryProposal))

    batch_confirm_proposals = confirm_proposals
    batch_reject_proposals = reject_proposals

    def reject_proposal(self, proposal_id: str, *, dataset_id: str | None = None) -> MemoryProposal:
        with self._write() as connection:
            stored = self._stored_proposal(connection, proposal_id)
            if stored is None:
                raise LedgerNotFoundError(f"memory proposal not found: {proposal_id}")
            if dataset_id is not None and stored.owner.dataset_id != dataset_id:
                raise LedgerConflictError(
                    "unknown_owner: proposal is outside the requested dataset"
                )
            if stored.state == "rejected":
                return stored
            if stored.state != "pending":
                raise LedgerConflictError(f"proposal is {stored.state}, not pending")
            return self._mark_proposal_state(connection, stored, "rejected")

    def correct(
        self,
        dataset_id: str,
        record_id: str,
        *,
        content: str | None = None,
        replacement: MemoryVersion | Mapping[str, Any] | None = None,
        sources: Sequence[SourceRef | Mapping[str, Any]] | None = None,
        reason: str = "explicit user correction",
        expected_head_revision: int | None = None,
        idempotency_key: str | None = None,
        request_digest: str | None = None,
    ) -> MemoryProposal:
        """Create a new correction Proposal; confirmation remains a separate CAS."""

        if content is None and replacement is None:
            raise LedgerValidationError("correct requires content or replacement")
        head = self.get_head(dataset_id, record_id)
        if head.published_version is None:
            raise LedgerConflictError("cannot correct a record without a published version")
        current = self.get_version(dataset_id, record_id, head.published_version.version)
        if expected_head_revision is not None and expected_head_revision != head.revision:
            raise LedgerConflictError("head revision conflict before correction")
        if replacement is not None:
            next_version = _as_version(replacement)
            if (
                next_version.owner.dataset_id != dataset_id
                or next_version.ref.record_id != record_id
            ):
                raise LedgerConflictError("replacement is outside the requested dataset record")
            if next_version.ref.version != current.ref.version + 1:
                raise LedgerConflictError(
                    "replacement version must immediately follow the published version"
                )
        else:
            assert content is not None
            if not content.strip():
                raise LedgerValidationError("correction content is blank")
            data = current.model_dump(mode="python")
            data["content"] = content
            data["evidence"] = "user_asserted"
            data["recorded_at"] = _now()
            data["ref"] = {
                "dataset_id": dataset_id,
                "record_id": record_id,
                "version": current.ref.version + 1,
                "content_digest": _content_digest(content),
            }
            if sources is not None:
                data["sources"] = tuple(_as_source(item) for item in sources)
            next_version = _as_version(data)
        return self.propose(
            next_version,
            operation="modify",
            source_refs=sources,
            reason=reason,
            expected_head_revision=head.revision,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            permission_epoch=head.permission_epoch,
        )

    propose_correction = correct

    def deactivate(
        self,
        dataset_id: str,
        record_id: str,
        *,
        expected_head_revision: int | None = None,
        permission_epoch: int | None = None,
        idempotency_key: str | None = None,
        request_digest: str | None = None,
        state: str = "inactive",
    ) -> MemoryHead:
        """Stop a record from normal recall while retaining its history."""

        if state not in {"inactive", "revoked"}:
            raise LedgerValidationError("deactivation state must be inactive or revoked")
        digest = request_digest or _digest(
            {
                "dataset_id": dataset_id,
                "record_id": record_id,
                "expected_head_revision": expected_head_revision,
                "permission_epoch": permission_epoch,
                "state": state,
            }
        )
        with self._write() as connection:
            cached = self._idempotency_result(
                connection, "deactivate", dataset_id, idempotency_key, digest
            )
            if cached is not None:
                result = self._decode_idempotency(*cached)
                if not isinstance(result, MemoryHead):
                    raise LedgerValidationError("idempotency result type mismatch for deactivate")
                return result
            head = self._stored_head(connection, dataset_id, record_id)
            if head is None:
                raise LedgerNotFoundError(f"memory head not found: {dataset_id}/{record_id}")
            if head.state == state:
                self._store_idempotency(
                    connection, "deactivate", dataset_id, idempotency_key, digest, "head", head
                )
                return head
            if head.state == "deleted":
                raise LedgerConflictError("deleted record cannot be reactivated or revoked")
            self._check_expected(expected_head_revision, head.revision)
            if permission_epoch is not None and permission_epoch != head.permission_epoch:
                raise LedgerConflictError("permission epoch conflict")
            cursor = self._next_cursor(connection)
            next_head = head.model_copy(
                update={
                    "revision": head.revision + 1,
                    "publication_cursor": cursor,
                    "state": state,
                }
            )
            result = connection.execute(
                """
                UPDATE memory_ledger_heads
                SET revision = ?, publication_cursor = ?, state = ?, body = ?
                WHERE dataset_id = ? AND record_id = ? AND revision = ?
                """,
                (
                    next_head.revision,
                    next_head.publication_cursor,
                    next_head.state,
                    _model_json(next_head),
                    dataset_id,
                    record_id,
                    head.revision,
                ),
            )
            if result.rowcount != 1:
                raise LedgerConflictError("head changed during deactivation")
            if state == "revoked":
                tombstone = Tombstone(
                    dataset_id=dataset_id,
                    record_id=record_id,
                    state="revoked",
                    revision=next_head.revision,
                    cursor=cursor,
                )
                self._upsert_tombstone(connection, tombstone)
            self._store_idempotency(
                connection, "deactivate", dataset_id, idempotency_key, digest, "head", next_head
            )
            return next_head

    deactivate_record = deactivate

    def _upsert_tombstone(self, connection: sqlite3.Connection, tombstone: Tombstone) -> None:
        key = tombstone.record_id if tombstone.record_id is not None else "__dataset__"
        connection.execute(
            """
            INSERT INTO memory_ledger_tombstones(
                dataset_id, tombstone_key, record_id, state, revision, cursor, body, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(dataset_id, tombstone_key) DO UPDATE SET
                state = excluded.state,
                revision = excluded.revision,
                cursor = excluded.cursor,
                body = excluded.body,
                created_at = excluded.created_at
            """,
            (
                tombstone.dataset_id,
                key,
                tombstone.record_id,
                tombstone.state,
                tombstone.revision,
                tombstone.cursor,
                _model_json(tombstone),
                _now_iso(),
            ),
        )

    def delete_record(
        self,
        dataset_id: str,
        record_id: str,
        *,
        expected_head_revision: int | None = None,
        permission_epoch: int | None = None,
        idempotency_key: str | None = None,
        request_digest: str | None = None,
    ) -> Tombstone:
        """Purge one plugin-owned record and retain a body-free tombstone."""

        digest = request_digest or _digest(
            {
                "dataset_id": dataset_id,
                "record_id": record_id,
                "expected_head_revision": expected_head_revision,
                "permission_epoch": permission_epoch,
            }
        )
        with self._write() as connection:
            cached = self._idempotency_result(
                connection, "delete_record", dataset_id, idempotency_key, digest
            )
            if cached is not None:
                result = self._decode_idempotency(*cached)
                if not isinstance(result, Tombstone):
                    raise LedgerValidationError(
                        "idempotency result type mismatch for delete_record"
                    )
                return result
            head = self._stored_head(connection, dataset_id, record_id)
            if head is None:
                existing = self._stored_tombstone(connection, dataset_id, record_id)
                if existing is None:
                    raise LedgerNotFoundError(f"memory head not found: {dataset_id}/{record_id}")
                self._store_idempotency(
                    connection,
                    "delete_record",
                    dataset_id,
                    idempotency_key,
                    digest,
                    "tombstone",
                    existing,
                )
                return existing
            self._check_expected(expected_head_revision, head.revision)
            if permission_epoch is not None and permission_epoch != head.permission_epoch:
                raise LedgerConflictError("permission epoch conflict")
            cursor = self._next_cursor(connection)
            next_head = head.model_copy(
                update={
                    "revision": head.revision + 1,
                    "publication_cursor": cursor,
                    "state": "deleted",
                }
            )
            updated = connection.execute(
                """
                UPDATE memory_ledger_heads
                SET revision = ?, publication_cursor = ?, state = ?, body = ?
                WHERE dataset_id = ? AND record_id = ? AND revision = ?
                """,
                (
                    next_head.revision,
                    next_head.publication_cursor,
                    next_head.state,
                    _model_json(next_head),
                    dataset_id,
                    record_id,
                    head.revision,
                ),
            )
            if updated.rowcount != 1:
                raise LedgerConflictError("head changed during deletion")
            pending = connection.execute(
                """
                SELECT body FROM memory_ledger_proposals
                WHERE dataset_id = ? AND record_id = ? AND state = 'pending'
                """,
                (dataset_id, record_id),
            ).fetchall()
            for row in pending:
                self._mark_proposal_state(
                    connection,
                    MemoryProposal.model_validate_json(row["body"]),
                    "cancelled",
                )
            tombstone = Tombstone(
                dataset_id=dataset_id,
                record_id=record_id,
                state="deleted",
                revision=next_head.revision,
                cursor=cursor,
            )
            self._upsert_tombstone(connection, tombstone)
            self._store_idempotency(
                connection,
                "delete_record",
                dataset_id,
                idempotency_key,
                digest,
                "tombstone",
                tombstone,
            )
            return tombstone

    delete = delete_record

    def delete_dataset(
        self,
        dataset_id: str,
        *,
        idempotency_key: str | None = None,
        request_digest: str | None = None,
    ) -> list[Tombstone]:
        """Tombstone all records owned by a dataset, retaining their history."""

        digest = request_digest or _digest({"dataset_id": dataset_id})
        with self._write() as connection:
            if idempotency_key is not None:
                cached = self._idempotency_result(
                    connection, "delete_dataset", dataset_id, idempotency_key, digest
                )
                if cached is not None:
                    result_type, body = cached
                    if result_type != "tombstones":
                        raise LedgerValidationError(
                            "idempotency result type mismatch for delete_dataset"
                        )
                    raw = json.loads(body)
                    return [Tombstone.model_validate(item) for item in raw]
            rows = connection.execute(
                """
                SELECT body FROM memory_ledger_heads
                WHERE dataset_id = ? ORDER BY record_id
                """,
                (dataset_id,),
            ).fetchall()
            tombstones: list[Tombstone] = []
            for row in rows:
                head = MemoryHead.model_validate_json(row["body"])
                if head.state == "deleted":
                    existing = connection.execute(
                        """
                        SELECT body FROM memory_ledger_tombstones
                        WHERE dataset_id = ? AND tombstone_key = ?
                        """,
                        (dataset_id, head.record_id),
                    ).fetchone()
                    if existing is not None:
                        tombstones.append(Tombstone.model_validate_json(existing["body"]))
                    continue
                cursor = self._next_cursor(connection)
                next_head = head.model_copy(
                    update={
                        "revision": head.revision + 1,
                        "publication_cursor": cursor,
                        "state": "deleted",
                    }
                )
                connection.execute(
                    """
                    UPDATE memory_ledger_heads
                    SET revision = ?, publication_cursor = ?, state = ?, body = ?
                    WHERE dataset_id = ? AND record_id = ? AND revision = ?
                    """,
                    (
                        next_head.revision,
                        next_head.publication_cursor,
                        next_head.state,
                        _model_json(next_head),
                        dataset_id,
                        head.record_id,
                        head.revision,
                    ),
                )
                pending = connection.execute(
                    """
                    SELECT body FROM memory_ledger_proposals
                    WHERE dataset_id = ? AND record_id = ? AND state = 'pending'
                    """,
                    (dataset_id, head.record_id),
                ).fetchall()
                for proposal_row in pending:
                    self._mark_proposal_state(
                        connection,
                        MemoryProposal.model_validate_json(proposal_row["body"]),
                        "cancelled",
                    )
                tombstone = Tombstone(
                    dataset_id=dataset_id,
                    record_id=head.record_id,
                    state="deleted",
                    revision=next_head.revision,
                    cursor=cursor,
                )
                self._upsert_tombstone(connection, tombstone)
                tombstones.append(tombstone)
            if self._stored_tombstone(connection, dataset_id, None) is None:
                marker_revision = max((item.revision for item in tombstones), default=0) + 1
                marker = Tombstone(
                    dataset_id=dataset_id,
                    record_id=None,
                    state="deleted",
                    revision=marker_revision,
                    cursor=self._next_cursor(connection),
                )
                self._upsert_tombstone(connection, marker)
            if idempotency_key is not None:
                self._store_idempotency(
                    connection,
                    "delete_dataset",
                    dataset_id,
                    idempotency_key,
                    digest,
                    "tombstones",
                    tombstones,
                )
            return tombstones

    def get_tombstone(self, dataset_id: str, record_id: str | None = None) -> Tombstone:
        key = record_id if record_id is not None else "__dataset__"
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT body FROM memory_ledger_tombstones
                WHERE dataset_id = ? AND tombstone_key = ?
                """,
                (dataset_id, key),
            ).fetchone()
        if row is None:
            raise LedgerNotFoundError(
                f"tombstone not found: {dataset_id}/{record_id or '<dataset>'}"
            )
        return Tombstone.model_validate_json(row["body"])

    def list_tombstones(self, dataset_id: str) -> list[Tombstone]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT body FROM memory_ledger_tombstones
                WHERE dataset_id = ? ORDER BY revision, tombstone_key
                """,
                (dataset_id,),
            ).fetchall()
        return [Tombstone.model_validate_json(row["body"]) for row in rows]

    def query(
        self,
        dataset_id: str,
        text: str | None = None,
        *,
        record_id: str | None = None,
        scope: Scope | Mapping[str, Any] | None = None,
        include_candidates: bool = False,
        include_inactive: bool = False,
        include_legacy: bool = False,
        limit: int = 100,
    ) -> list[MemoryVersion]:
        """Read dataset-scoped versions with a small exact-scope filter.

        This is deliberately a correctness-oriented scan.  MP-3 indexing and
        ranking are outside this ledger's scope.
        """

        if not 1 <= limit <= 10_000:
            raise LedgerValidationError("query limit must be between 1 and 10000")
        requested_scope = _as_scope(scope) if scope is not None else None
        with self._connect() as connection:
            head_rows = connection.execute(
                "SELECT record_id, body FROM memory_ledger_heads WHERE dataset_id = ?",
                (dataset_id,),
            ).fetchall()
            heads = {
                str(row["record_id"]): MemoryHead.model_validate_json(row["body"])
                for row in head_rows
            }
            proposal_rows = connection.execute(
                """
                SELECT body FROM memory_ledger_proposals
                WHERE dataset_id = ? AND state = 'pending'
                """,
                (dataset_id,),
            ).fetchall()
            candidate_refs = {
                MemoryProposal.model_validate_json(row["body"]).proposed_version
                for row in proposal_rows
            }
            clauses = ["dataset_id = ?"]
            values: list[Any] = [dataset_id]
            if record_id is not None:
                clauses.append("record_id = ?")
                values.append(record_id)
            rows = connection.execute(
                "SELECT body FROM memory_ledger_versions WHERE "
                + " AND ".join(clauses)
                + " ORDER BY record_id, version",
                values,
            ).fetchall()
        results: list[MemoryVersion] = []
        query_text = text.casefold().strip() if text else None
        for row in rows:
            version = MemoryVersion.model_validate_json(row["body"])
            head = heads.get(version.ref.record_id)
            if head is None:
                continue
            published = (
                head.state in {"published", "inactive", "revoked"}
                and head.published_version == version.ref
            )
            candidate = version.ref in candidate_refs or head.state == "unpublished"
            if not published and not (include_candidates and candidate):
                continue
            if not include_inactive and head.state in {"inactive", "revoked", "deleted"}:
                continue
            if not include_legacy and version.evidence == "legacy_unverified":
                continue
            if requested_scope is not None and not _scope_matches(version.scope, requested_scope):
                continue
            if query_text and query_text not in version.content.casefold():
                continue
            results.append(version)
            if len(results) >= limit:
                break
        return results

    search = query
    list_published = query

    def count(
        self,
        dataset_id: str,
        *,
        scope: Scope | Mapping[str, Any] | None = None,
        include_candidates: bool = False,
        include_inactive: bool = False,
        include_legacy: bool = False,
        text: str | None = None,
    ) -> int:
        """Count the same authorized projection returned by :meth:`query`."""

        return len(
            self.query(
                dataset_id,
                text,
                scope=scope,
                include_candidates=include_candidates,
                include_inactive=include_inactive,
                include_legacy=include_legacy,
                limit=10_000,
            )
        )

    count_dataset = count
    count_memories = count

    def get_sources(
        self,
        dataset_id: str,
        record_id: str,
        version: int | None = None,
    ) -> tuple[SourceRef, ...]:
        return self.get_version(dataset_id, record_id, version).sources

    sources = get_sources
    trace_sources = get_sources

    def list_by_source(
        self,
        dataset_id: str,
        source_type: str,
        source_id: str,
        *,
        include_legacy: bool = True,
    ) -> list[MemoryVersion]:
        versions: list[MemoryVersion] = []
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT body FROM memory_ledger_versions "
                "WHERE dataset_id = ? ORDER BY record_id, version",
                (dataset_id,),
            ).fetchall()
        for row in rows:
            version = MemoryVersion.model_validate_json(row["body"])
            if not include_legacy and version.evidence == "legacy_unverified":
                continue
            if any(
                source.source_type == source_type and source.source_id == source_id
                for source in version.sources
            ):
                versions.append(version)
        return versions

    def export_dataset(
        self,
        dataset_id: str,
        *,
        include_tombstones: bool = True,
        include_proposals: bool = True,
    ) -> dict[str, Any]:
        """Return a JSON-serializable complete dataset export."""

        with self._connect() as connection:
            version_rows = connection.execute(
                "SELECT body FROM memory_ledger_versions "
                "WHERE dataset_id = ? ORDER BY record_id, version",
                (dataset_id,),
            ).fetchall()
            head_rows = connection.execute(
                "SELECT body FROM memory_ledger_heads WHERE dataset_id = ? ORDER BY record_id",
                (dataset_id,),
            ).fetchall()
            proposal_rows = connection.execute(
                "SELECT body FROM memory_ledger_proposals "
                "WHERE dataset_id = ? ORDER BY created_at, proposal_id",
                (dataset_id,),
            ).fetchall()
            tombstone_rows = connection.execute(
                "SELECT body FROM memory_ledger_tombstones "
                "WHERE dataset_id = ? ORDER BY revision, tombstone_key",
                (dataset_id,),
            ).fetchall()
        return {
            "schema": "operant.memory-ledger.v1",
            "dataset_id": dataset_id,
            "versions": [json.loads(row["body"]) for row in version_rows],
            "heads": [json.loads(row["body"]) for row in head_rows],
            "proposals": [json.loads(row["body"]) for row in proposal_rows]
            if include_proposals
            else [],
            "tombstones": [json.loads(row["body"]) for row in tombstone_rows]
            if include_tombstones
            else [],
        }

    export = export_dataset

    def export_json(self, dataset_id: str, **kwargs: Any) -> str:
        return _canonical_json(self.export_dataset(dataset_id, **kwargs))

    @staticmethod
    def _legacy_rows_from_sqlite(path: str | Path) -> list[dict[str, Any]]:
        source_path = str(path)
        if source_path == ":memory:":
            raise LedgerValidationError("legacy sqlite source must be a readable file")
        uri = "file:" + source_path.replace("\\", "/") + "?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True)
        except sqlite3.Error as exc:
            raise LedgerValidationError(
                f"cannot open legacy sqlite source read-only: {exc}"
            ) from exc
        try:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            rows: list[dict[str, Any]] = []
            if "memory_versions" in tables:
                raw_rows = connection.execute(
                    "SELECT * FROM memory_versions ORDER BY memory_id, version"
                ).fetchall()
                columns = [
                    str(item[1])
                    for item in connection.execute("PRAGMA table_info(memory_versions)").fetchall()
                ]
                rows.extend(dict(zip(columns, row, strict=False)) for row in raw_rows)
            elif "memories" in tables:
                raw_rows = connection.execute("SELECT * FROM memories ORDER BY id").fetchall()
                columns = [
                    str(item[1])
                    for item in connection.execute("PRAGMA table_info(memories)").fetchall()
                ]
                rows.extend(dict(zip(columns, row, strict=False)) for row in raw_rows)
            return rows
        finally:
            connection.close()

    @staticmethod
    def _legacy_payload(raw: Any) -> tuple[dict[str, Any], str]:
        if isinstance(raw, MemoryVersion):
            payload = raw.model_dump(mode="json")
            return payload, _model_json(payload)
        if hasattr(raw, "model_dump"):
            payload = raw.model_dump(mode="json")
            return payload, _model_json(payload)
        if not isinstance(raw, Mapping):
            raise LedgerValidationError("legacy record must be a mapping or pydantic model")
        outer = dict(raw)
        raw_body = outer.get("body")
        if isinstance(raw_body, str):
            try:
                parsed = json.loads(raw_body)
            except json.JSONDecodeError:
                parsed = None
            payload = dict(parsed) if isinstance(parsed, Mapping) else outer
        elif isinstance(raw_body, Mapping):
            payload = dict(raw_body)
        else:
            payload = outer
        for key, value in outer.items():
            if key not in payload and key != "body":
                payload[key] = value
        legacy_body = raw_body if isinstance(raw_body, str) else _model_json(payload)
        return payload, legacy_body

    @staticmethod
    def _legacy_scope(
        payload: Mapping[str, Any],
        scope: Scope | Mapping[str, Any] | None,
        scope_factory: Any,
        *,
        index: int,
    ) -> Scope:
        # The Core-provided registered scope is authoritative.  A callback is
        # also a Core mapping decision; only when neither is supplied may an
        # old typed scope be considered, and that path still requires an
        # explicit migration scope below.
        explicit = scope
        if explicit is None and scope_factory is not None:
            explicit = scope_factory(payload, index)
        if explicit is None:
            raise LedgerValidationError(
                "unknown_scope: legacy import requires an explicit registered scope"
            )

        explicit_scope = _as_scope(explicit)
        legacy_scope_value: Any = None
        for key in ("scope", "scope_json"):
            if key in payload and payload[key] is not None:
                legacy_scope_value = payload[key]
                break
        if legacy_scope_value is not None:
            if isinstance(legacy_scope_value, str):
                try:
                    legacy_scope_value = json.loads(legacy_scope_value)
                except json.JSONDecodeError as exc:
                    raise LedgerValidationError(
                        "unknown_scope: legacy scope is not typed JSON"
                    ) from exc
            legacy_scope = _as_scope(legacy_scope_value)
            if legacy_scope != explicit_scope:
                raise LedgerConflictError(
                    "legacy scope conflicts with the explicit registered scope"
                )
        return explicit_scope

    def migrate_legacy(
        self,
        records: Iterable[Any] | str | Path,
        *,
        owner: DatasetOwner | Mapping[str, Any],
        scope: Scope | Mapping[str, Any] | None = None,
        scope_factory: Any = None,
        source_namespace: str = "legacy",
        permission_epoch: int = 0,
    ) -> LegacyMigrationResult:
        """Import old records into a quarantined, explicitly owned dataset.

        ``records`` may be an iterable of old rows/models or a path to an old
        SQLite file.  The source file is opened read-only.  Every imported
        version receives a new canonical content digest and
        ``legacy_unverified`` evidence; even an old ``active`` status never
        moves the new publication head.
        """

        normalized_owner = _as_owner(owner)
        if isinstance(records, (str, Path)):
            source_records: list[Any] = self._legacy_rows_from_sqlite(records)
        else:
            source_records = list(records)
        imported: list[MemoryVersion] = []
        proposal_by_record: dict[str, MemoryProposal] = {}
        skipped = 0
        with self._write() as connection:
            if self._stored_tombstone(connection, normalized_owner.dataset_id, None) is not None:
                raise LedgerConflictError("dataset is tombstoned and cannot receive legacy data")
            for index, raw in enumerate(source_records):
                payload, legacy_body = self._legacy_payload(raw)
                old_id = payload.get("id") or payload.get("memory_id")
                if not isinstance(old_id, str) or not old_id.strip():
                    raise LedgerValidationError("legacy record has no stable id")
                record_id = old_id.strip()
                version_number = payload.get("version", 1)
                try:
                    version_number = int(version_number)
                except (TypeError, ValueError) as exc:
                    raise LedgerValidationError("legacy memory version is not an integer") from exc
                if version_number < 1:
                    raise LedgerValidationError("legacy memory version must be positive")
                source_key = str(
                    payload.get("legacy_source_key")
                    or f"{source_namespace}:{record_id}:{version_number}"
                )
                legacy_hash = hashlib.sha256(legacy_body.encode("utf-8")).hexdigest()
                existing_import = connection.execute(
                    """
                    SELECT record_id, version, legacy_body_hash
                    FROM memory_ledger_legacy_imports
                    WHERE dataset_id = ? AND source_key = ?
                    """,
                    (normalized_owner.dataset_id, source_key),
                ).fetchone()
                if existing_import is not None:
                    if existing_import["legacy_body_hash"] != legacy_hash:
                        raise LedgerConflictError(
                            "legacy source key was reused for different content"
                        )
                    stored = self._stored_version(
                        connection,
                        normalized_owner.dataset_id,
                        str(existing_import["record_id"]),
                        int(existing_import["version"]),
                    )
                    if stored is not None:
                        imported.append(stored)
                    skipped += 1
                    continue
                memory_kind = str(payload.get("kind") or payload.get("memory_type") or "episodic")
                if memory_kind not in {"working", "episodic", "project"}:
                    raise LedgerValidationError(f"unknown_scope: legacy kind {memory_kind!r}")
                typed_scope = self._legacy_scope(payload, scope, scope_factory, index=index)
                content = payload.get("content")
                if not isinstance(content, str) or not content.strip():
                    raise LedgerValidationError("legacy memory content is blank")
                role_ids_value = payload.get("role_ids", payload.get("role_scope", ()))
                if isinstance(role_ids_value, str):
                    role_ids = tuple(
                        item.strip() for item in role_ids_value.split(",") if item.strip()
                    )
                else:
                    role_ids = tuple(str(item) for item in (role_ids_value or ()))
                recorded_at_value = payload.get("recorded_at") or payload.get("created_at")
                if isinstance(recorded_at_value, str):
                    try:
                        recorded_at = datetime.fromisoformat(
                            recorded_at_value.replace("Z", "+00:00")
                        )
                    except ValueError:
                        recorded_at = _now()
                else:
                    recorded_at = _now()
                if recorded_at.tzinfo is None:
                    recorded_at = recorded_at.replace(tzinfo=timezone.utc)
                content_type: Literal["fact", "episode"] = (
                    "episode" if memory_kind == "episodic" else "fact"
                )
                conditions = MemoryConditions(
                    commit_ref=None,
                    tree_digest=None,
                    file_fingerprints={},
                    environment_digest=None,
                    tool_versions={},
                    verified_at=None,
                    valid_from=recorded_at,
                    valid_until=None,
                )
                version = MemoryVersion(
                    ref=MemoryVersionRef(
                        dataset_id=normalized_owner.dataset_id,
                        record_id=record_id,
                        version=version_number,
                        content_digest=_content_digest(content),
                    ),
                    owner=normalized_owner,
                    kind=cast(_MemoryKind, memory_kind),
                    content_type=content_type,
                    scope=typed_scope,
                    role_ids=role_ids,
                    agent_ids=(),
                    content=content,
                    sources=(),
                    evidence="legacy_unverified",
                    sensitivity="internal",
                    retention_policy_id="legacy",
                    conditions=conditions,
                    recorded_at=recorded_at,
                )
                head = self._stored_head(connection, normalized_owner.dataset_id, record_id)
                if head is None:
                    self._insert_head(
                        connection,
                        self._head_for_create(
                            normalized_owner.dataset_id,
                            record_id,
                            permission_epoch=permission_epoch,
                        ),
                    )
                existing_version = self._stored_version(
                    connection,
                    normalized_owner.dataset_id,
                    record_id,
                    version_number,
                )
                if existing_version is None:
                    self._insert_version(connection, version)
                    imported.append(version)
                elif _model_json(existing_version) == _model_json(version):
                    imported.append(existing_version)
                    skipped += 1
                else:
                    raise LedgerConflictError(
                        "legacy record/version has different immutable content"
                    )
                connection.execute(
                    """
                    INSERT INTO memory_ledger_legacy_imports(
                        dataset_id, source_key, record_id, version, legacy_body_hash,
                        legacy_body, imported_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_owner.dataset_id,
                        source_key,
                        record_id,
                        version_number,
                        legacy_hash,
                        legacy_body,
                        _now_iso(),
                    ),
                )

            # One pending proposal for each imported record's latest version.
            record_ids = {version.ref.record_id for version in imported}
            for record_id in sorted(record_ids):
                head = self._stored_head(connection, normalized_owner.dataset_id, record_id)
                if head is None:
                    continue
                latest_row = connection.execute(
                    """
                    SELECT body FROM memory_ledger_versions
                    WHERE dataset_id = ? AND record_id = ? ORDER BY version DESC LIMIT 1
                    """,
                    (normalized_owner.dataset_id, record_id),
                ).fetchone()
                if latest_row is None:
                    continue
                latest = MemoryVersion.model_validate_json(latest_row["body"])
                pending_row = connection.execute(
                    """
                    SELECT body FROM memory_ledger_proposals
                    WHERE dataset_id = ? AND record_id = ? AND state = 'pending'
                    ORDER BY proposal_revision, proposal_id LIMIT 1
                    """,
                    (normalized_owner.dataset_id, record_id),
                ).fetchone()
                if pending_row is not None:
                    proposal_by_record[record_id] = MemoryProposal.model_validate_json(
                        pending_row["body"]
                    )
                    continue
                proposal = MemoryProposal(
                    proposal_id=f"legacy_proposal_{uuid4().hex}",
                    proposal_revision=0,
                    owner=normalized_owner,
                    operation="create",
                    base_head=head,
                    proposed_version=latest.ref,
                    source_refs=(),
                    extractor_version="legacy",
                    reason="legacy migration requires explicit user review",
                    state="pending",
                )
                connection.execute(
                    """
                    INSERT INTO memory_ledger_proposals(
                        proposal_id, dataset_id, record_id, proposal_revision,
                        base_head_revision, proposed_version, state, body,
                        idempotency_key, request_digest, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        proposal.proposal_id,
                        normalized_owner.dataset_id,
                        record_id,
                        proposal.proposal_revision,
                        proposal.base_head.revision,
                        proposal.proposed_version.version,
                        proposal.state,
                        _model_json(proposal),
                        None,
                        _digest(proposal),
                        _now_iso(),
                    ),
                )
                proposal_by_record[record_id] = proposal
        return LegacyMigrationResult(
            dataset_id=normalized_owner.dataset_id,
            imported_versions=tuple(imported),
            proposals=tuple(proposal_by_record[key] for key in sorted(proposal_by_record)),
            skipped_versions=skipped,
        )

    import_legacy = migrate_legacy
    migrate_legacy_sqlite = migrate_legacy


__all__ = [
    "SCHEMA_SQL",
    "IdempotencyConflictError",
    "LedgerConflictError",
    "LedgerError",
    "LedgerNotFoundError",
    "LedgerValidationError",
    "LegacyMigrationResult",
    "MemoryLedger",
]
