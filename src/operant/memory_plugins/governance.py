"""B2-5 memory history and governance domain service.

The service deliberately keeps two authorities separate:

* canonical ``threads``/``items`` remain the source of historical facts; and
* :class:`~operant.memory_plugins.ledger.MemoryLedger` remains the source of
  immutable memory versions and publication heads.

The additive governance tables only index source availability, dependencies,
relationships and review receipts.  They never copy Mailbox content and they
never create a second publication pointer.  Core callers should pass their
current ``RpcContext`` when one exists so binding and permission epochs are
checked at every mutating boundary.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4

from operant.contracts.b2_1 import (
    DatasetOwner,
    MemoryConditions,
    MemoryHead,
    MemoryProposal,
    MemoryVersion,
    MemoryVersionRef,
    RpcContext,
    SourceRef,
    WorkspaceScope,
)
from operant.contracts.b2_5 import (
    ExactProposal,
    GovernanceEntry,
    GovernanceRecord,
    GovernanceState,
    HistoryDetail,
    HistoryEntry,
    HistoryPage,
    MemoryRelationship,
)
from operant.domain.threads import Item, UserMessagePayload

from .governance_schema import SCHEMA_SQL
from .ledger import (
    LedgerConflictError,
    LedgerNotFoundError,
    LedgerValidationError,
    MemoryLedger,
)

if TYPE_CHECKING:
    from operant.memory_plugins.manager import MemoryManager


_REVIEWER_KINDS = {"user", "system"}
_SOURCE_STATES = {"available", "deleted", "revoked", "unavailable", "blocked"}
MAINTENANCE_REVIEW_DAYS = 30


class GovernanceError(RuntimeError):
    """Base error for the B2-5 governance service."""


class GovernanceSchemaError(GovernanceError):
    """The additive v17 governance tables are unavailable."""


class GovernancePermissionError(PermissionError, GovernanceError):
    """A closed binding, stale epoch or unauthorized source blocked a request."""


@dataclass(frozen=True)
class PreparedCandidate:
    """Untrusted model output after Core bounds it to source indexes.

    The model cannot provide an owner, dataset, record id, version, head, or
    publication state.  Those values are derived during the commit transaction
    from the current project binding and the exact source list.
    """

    content: str
    source_indexes: tuple[int, ...]


@dataclass(frozen=True)
class PreparedExtraction:
    """Pure-memory maintenance output passed to the atomic commit boundary."""

    source_cursor: int
    sources: tuple[SourceRef, ...]
    candidates: tuple[PreparedCandidate, ...]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _json(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _source_identity(source: SourceRef) -> dict[str, Any]:
    """Return identity fields that describe the underlying evidence.

    Availability, permission epoch and display scope are intentionally absent:
    changing a grant or current status must not make one canonical source look
    like independent evidence.  This is the same source after a recheck.
    """

    return {
        "source_type": source.source_type,
        "source_id": source.source_id,
        "source_revision": source.revision,
        "content_digest": source.content_digest,
    }


def source_key(source: SourceRef) -> str:
    """Return a stable non-secret key for one immutable source reference."""

    return "source_" + _digest(_source_identity(source))


def _as_source(value: SourceRef | Mapping[str, Any]) -> SourceRef:
    return value if isinstance(value, SourceRef) else SourceRef.model_validate(value)


def _as_ref(value: MemoryVersionRef | Mapping[str, Any]) -> MemoryVersionRef:
    return value if isinstance(value, MemoryVersionRef) else MemoryVersionRef.model_validate(value)


def _as_exact(value: ExactProposal | Mapping[str, Any]) -> ExactProposal:
    return value if isinstance(value, ExactProposal) else ExactProposal.model_validate(value)


def _unique_sources(sources: Sequence[SourceRef]) -> tuple[SourceRef, ...]:
    result: list[SourceRef] = []
    seen: set[str] = set()
    for source in sources:
        key = source_key(source)
        if key in seen:
            continue
        seen.add(key)
        result.append(source)
    return tuple(result)


def _payload_excerpt(item: Item) -> str:
    payload = item.payload
    if isinstance(payload, (UserMessagePayload,)):
        return payload.text[:500]
    raw = payload.model_dump(mode="json")
    for key in ("text", "summary", "detail_summary", "label", "event_type"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            return value[:500]
    return payload.type


def _payload_text(item: Item) -> str:
    payload = item.payload
    raw = payload.model_dump(mode="json")
    for key in ("text", "summary", "detail_summary", "label"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            return value
    return _json(raw)


class GovernanceService:
    """Core-facing MP-4.1/4.2 governance operations.

    ``initialize_schema`` is intentionally opt-in.  Production Core must
    install the objects through its v17 migration; tests using an isolated
    temporary store may pass ``initialize_schema=True``.
    """

    def __init__(self, manager: MemoryManager, *, initialize_schema: bool = False) -> None:
        self.manager = manager
        self.store = manager.store
        self.ledger: MemoryLedger = manager.ledger
        if initialize_schema:
            self.ensure_schema()

    def ensure_schema(self) -> None:
        """Apply the additive schema for an isolated test database."""

        with self.store._connect() as connection:
            connection.executescript(SCHEMA_SQL)

    def _require_schema(self, connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='b25_governance_sources'"
        ).fetchone()
        if row is None:
            raise GovernanceSchemaError("B2-5 governance schema is not installed; apply v17")

    def _project(self, project_id: str) -> dict[str, Any]:
        project = self.manager._project(project_id)
        if project.get("archived"):
            raise GovernancePermissionError("project is archived")
        return project

    def _scope(self, project: Mapping[str, Any]) -> WorkspaceScope:
        return self.manager._scope(dict(project))

    def _workspace_ref(self, project: Mapping[str, Any]) -> str:
        initialization = self.store.get_workspace_initialization_by_id(project["workspace_id"])
        return initialization.workspace_ref

    def _installation(self, project: Mapping[str, Any], *, enabled: bool) -> Any | None:
        installation_id = project.get("installation_id")
        if not installation_id:
            if enabled:
                raise GovernancePermissionError("memory governance has no selected installation")
            return None
        try:
            installation = self.manager.registry.get_installation(installation_id)
        except Exception as exc:
            raise GovernancePermissionError("selected memory installation is unavailable") from exc
        if enabled:
            state = getattr(self.manager, "_state", {})
            if not state.get("global_enabled", True) or not project.get("memory_enabled", True):
                raise GovernancePermissionError("memory governance is disabled")
            binding_id = installation.binding_id
            if not binding_id:
                raise GovernancePermissionError("memory governance binding is unavailable")
            binding = self.manager.registry.get_binding(binding_id)
            if installation.state != "enabled" or not binding.enabled or not binding.global_enabled:
                raise GovernancePermissionError("memory governance binding is closed")
        return installation

    def _runtime_check(
        self,
        project: Mapping[str, Any],
        *,
        context: RpcContext | None,
        enabled: bool,
    ) -> tuple[Any | None, Any | None]:
        installation = self._installation(project, enabled=enabled)
        if installation is None:
            if context is not None:
                raise GovernancePermissionError("context requires an active memory binding")
            return None, None
        binding = self.manager.registry.get_binding(installation.binding_id)
        if context is not None:
            expected_scope = self._scope(project)
            if (
                context.dataset_id != installation.dataset_id
                or context.installation_id != installation.installation_id
                or context.scope != expected_scope
                or context.binding_epoch != binding.binding_epoch
                or context.permission_epoch != binding.permission_epoch
            ):
                raise GovernancePermissionError("stale memory governance epoch or scope")
        return installation, binding

    def _dataset(self, project: Mapping[str, Any], *, enabled: bool) -> str | None:
        installation = self._installation(project, enabled=enabled)
        return None if installation is None else installation.dataset_id

    def _source_digest(self, item: Item) -> str:
        payload = item.payload
        if isinstance(payload, UserMessagePayload):
            return hashlib.sha256(payload.text.encode("utf-8")).hexdigest()
        return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()

    def _canonical_source_valid(
        self,
        project: Mapping[str, Any],
        source: SourceRef,
        *,
        context: RpcContext | None,
    ) -> bool:
        if source.source_type == "mailbox_message":
            # Mailbox is a collaboration projection, never a public memory
            # source.  Governance does not inspect or copy its body.
            return False
        if source.source_type == "memory_version":
            try:
                version = self.ledger.get_version(
                    self._dataset(project, enabled=False) or "",
                    source.source_id,
                    source.revision,
                )
                head = self.ledger.get_head(version.ref.dataset_id, version.ref.record_id)
            except Exception:
                return False
            if (
                source.availability != "available"
                or source.content_digest != version.ref.content_digest
                or source.scope != version.scope
                or head.state != "published"
                or head.published_version != version.ref
            ):
                return False
            return context is None or source.permission_epoch == context.permission_epoch
        if source.source_type != "item":
            return False
        try:
            item = self.store.get_item(source.source_id)
            thread = self.store.get_thread(item.thread_id)
        except Exception:
            return False
        workspace_ref = self._workspace_ref(project)
        if thread.workspace_ref != workspace_ref:
            return False
        # B2-3 stored a source-local revision of ``1`` while the canonical Item
        # later acquired a global sequence cursor.  Only an exact, same-dataset
        # b23_sources registration can authorize that compatibility shape; a
        # random revision=1 source never receives this fallback.
        legacy_registered = False
        dataset = self._dataset(project, enabled=False)
        if source.revision == 1 and dataset is not None:
            with self.store._connect() as connection:
                row = connection.execute(
                    "SELECT dataset_id, ref FROM b23_sources WHERE source_id=?",
                    (source.source_id,),
                ).fetchone()
            if row is not None and row["dataset_id"] == dataset:
                try:
                    legacy_registered = source == SourceRef.model_validate_json(row["ref"])
                except Exception:
                    legacy_registered = False
        if item.cursor != source.revision and not legacy_registered:
            return False
        if source.scope != self._scope(project) or source.content_digest != self._source_digest(
            item
        ):
            return False
        return context is None or source.permission_epoch == context.permission_epoch

    def _source_authorized(
        self,
        project: Mapping[str, Any],
        source: SourceRef,
        *,
        context: RpcContext | None,
    ) -> bool:
        if source.availability != "available":
            return False
        # The metadata table is a revocation barrier.  A source marked revoked
        # here remains revoked even if a stale plugin payload says available.
        dataset = self._dataset(project, enabled=False)
        if dataset is None:
            return False
        key = source_key(source)
        with self.store._connect() as connection:
            self._require_schema(connection)
            row = connection.execute(
                "SELECT state FROM b25_governance_sources WHERE dataset_id=? AND source_key=?",
                (dataset, key),
            ).fetchone()
        if row is not None and row["state"] in {"deleted", "revoked", "unavailable", "blocked"}:
            return False

        # Core's source authorizer is authoritative when it knows the source.
        # Canonical history items that predate b23 source registration still
        # receive the same scope/thread/digest checks below; no other source
        # type gets this fallback.
        if context is not None:
            try:
                legacy_authorizer = getattr(self.manager, "_authorize_source_legacy", None)
                if legacy_authorizer is None:
                    legacy_authorizer = getattr(self.manager, "authorize_source", None)
                if legacy_authorizer is not None and bool(legacy_authorizer(context, source)):
                    return True
            except Exception:
                pass
            if source.source_type != "item":
                return False
        return self._canonical_source_valid(project, source, context=context)

    def _register_source(
        self,
        connection: sqlite3.Connection,
        dataset: str,
        source: SourceRef,
        *,
        state: str | None = None,
        reason: str | None = None,
    ) -> str:
        if state is None:
            state = "available" if source.availability == "available" else source.availability
        if state not in _SOURCE_STATES:
            raise LedgerValidationError(f"unsupported governance source state: {state}")
        key = source_key(source)
        connection.execute(
            """
            INSERT INTO b25_governance_sources(
                dataset_id, source_key, source_type, source_id, source_revision,
                content_digest, scope_json, permission_epoch, state, reason, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(dataset_id, source_key) DO UPDATE SET
                scope_json=excluded.scope_json,
                permission_epoch=excluded.permission_epoch,
                state=CASE
                    WHEN b25_governance_sources.state='revoked' THEN 'revoked'
                    ELSE excluded.state
                END,
                reason=excluded.reason,
                updated_at=excluded.updated_at
            """,
            (
                dataset,
                key,
                source.source_type,
                source.source_id,
                source.revision,
                source.content_digest,
                source.scope.model_dump_json(),
                source.permission_epoch,
                state,
                reason,
                _now_iso(),
            ),
        )
        return key

    def _proposal_metadata(
        self,
        proposal: MemoryProposal,
        *,
        review_due_at: datetime | None,
        source_watermark: str | None,
        model_ids: Sequence[str],
    ) -> None:
        with self.store._connect() as connection:
            self._proposal_metadata_in_connection(
                connection,
                proposal,
                review_due_at=review_due_at,
                source_watermark=source_watermark,
                model_ids=model_ids,
            )

    def _proposal_metadata_in_connection(
        self,
        connection: sqlite3.Connection,
        proposal: MemoryProposal,
        *,
        review_due_at: datetime | None,
        source_watermark: str | None,
        model_ids: Sequence[str],
    ) -> None:
        self._require_schema(connection)
        source_keys = [
            self._register_source(connection, proposal.owner.dataset_id, source)
            for source in _unique_sources(proposal.source_refs)
        ]
        connection.execute(
            """
            INSERT INTO b25_governance_proposals(
                proposal_id, dataset_id, record_id, proposal_revision,
                proposed_version, proposed_digest, base_head_revision,
                review_due_at, source_watermark, source_keys_json, model_ids_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(proposal_id) DO UPDATE SET
                proposal_revision=excluded.proposal_revision,
                proposed_version=excluded.proposed_version,
                proposed_digest=excluded.proposed_digest,
                base_head_revision=excluded.base_head_revision,
                review_due_at=excluded.review_due_at,
                source_watermark=excluded.source_watermark,
                source_keys_json=excluded.source_keys_json,
                model_ids_json=excluded.model_ids_json,
                updated_at=excluded.updated_at
            """,
            (
                proposal.proposal_id,
                proposal.owner.dataset_id,
                proposal.proposed_version.record_id,
                proposal.proposal_revision,
                proposal.proposed_version.version,
                proposal.proposed_version.content_digest,
                proposal.base_head.revision,
                review_due_at.isoformat() if review_due_at else None,
                source_watermark,
                _json(source_keys),
                _json(sorted(set(model_ids))),
                _now_iso(),
                _now_iso(),
            ),
        )

    def _relation_rows(
        self,
        dataset: str,
        proposal: MemoryProposal,
        relationships: Sequence[MemoryRelationship],
    ) -> None:
        if not relationships:
            return
        with self.store._connect() as connection:
            self._relation_rows_in_connection(connection, dataset, proposal, relationships)

    def _relation_rows_in_connection(
        self,
        connection: sqlite3.Connection,
        dataset: str,
        proposal: MemoryProposal,
        relationships: Sequence[MemoryRelationship],
    ) -> None:
        if not relationships:
            return
        self._require_schema(connection)
        for relationship in relationships:
            target = relationship.target
            if target.dataset_id != dataset:
                raise GovernancePermissionError("relationship target is outside the dataset")
            connection.execute(
                """
                INSERT OR IGNORE INTO b25_governance_relations(
                    dataset_id, relation_id, relation, subject_record_id, subject_version,
                    subject_digest, target_record_id, target_version, target_digest,
                    target_source_key, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
                """,
                (
                    dataset,
                    "relation_" + uuid4().hex,
                    relationship.relation,
                    proposal.proposed_version.record_id,
                    proposal.proposed_version.version,
                    proposal.proposed_version.content_digest,
                    target.record_id,
                    target.version,
                    target.content_digest,
                    _now_iso(),
                ),
            )

    def _dependency_rows(self, proposal: MemoryProposal) -> None:
        sources = _unique_sources(proposal.source_refs)
        if not sources:
            return
        with self.store._connect() as connection:
            self._dependency_rows_in_connection(connection, proposal)

    def _dependency_rows_in_connection(
        self, connection: sqlite3.Connection, proposal: MemoryProposal
    ) -> None:
        sources = _unique_sources(proposal.source_refs)
        if not sources:
            return
        self._require_schema(connection)
        for source in sources:
            key = self._register_source(connection, proposal.owner.dataset_id, source)
            connection.execute(
                """
                INSERT INTO b25_governance_dependencies(
                    dataset_id, derived_record_id, derived_version, source_key,
                    state, reason, updated_at
                ) VALUES (?, ?, ?, ?, 'active', NULL, ?)
                ON CONFLICT(dataset_id, derived_record_id, derived_version, source_key)
                DO UPDATE SET
                    state=CASE
                        WHEN b25_governance_dependencies.state IN ('blocked', 'revoked')
                            THEN b25_governance_dependencies.state
                        ELSE 'active'
                    END,
                    reason=CASE
                        WHEN b25_governance_dependencies.state IN ('blocked', 'revoked')
                            THEN b25_governance_dependencies.reason
                        ELSE NULL
                    END,
                    updated_at=excluded.updated_at
                """,
                (
                    proposal.owner.dataset_id,
                    proposal.proposed_version.record_id,
                    proposal.proposed_version.version,
                    key,
                    _now_iso(),
                ),
            )

    @staticmethod
    def _snapshot_field(snapshot: Any, name: str, default: Any = None) -> Any:
        if isinstance(snapshot, Mapping):
            return snapshot.get(name, default)
        return getattr(snapshot, name, default)

    def prepare_extraction(
        self,
        *,
        source_cursor: int,
        sources: Sequence[SourceRef | Mapping[str, Any]],
        candidates: Sequence[PreparedCandidate | Mapping[str, Any] | Any],
    ) -> PreparedExtraction:
        """Normalize bounded model output without reading or writing state."""

        if (
            not isinstance(source_cursor, int)
            or isinstance(source_cursor, bool)
            or source_cursor < 0
        ):
            raise GovernanceError("maintenance source cursor is invalid")
        normalized_sources = tuple(_as_source(item) for item in sources)
        normalized: list[PreparedCandidate] = []
        seen: set[tuple[str, tuple[int, ...]]] = set()
        for raw in candidates:
            if isinstance(raw, PreparedCandidate):
                candidate = raw
            else:
                if isinstance(raw, Mapping):
                    content = raw.get("content")
                    source_indexes = raw.get("source_indexes")
                else:
                    content = getattr(raw, "content", None)
                    source_indexes = getattr(raw, "source_indexes", None)
                if not isinstance(content, str) or not isinstance(source_indexes, Sequence):
                    raise GovernanceError(
                        "maintenance candidate requires content and source_indexes"
                    )
                candidate = PreparedCandidate(
                    content=content,
                    source_indexes=tuple(source_indexes),
                )
            if not candidate.content.strip() or len(candidate.content) > 100_000:
                raise GovernanceError("maintenance candidate content is blank or too large")
            if not candidate.source_indexes:
                raise GovernanceError("maintenance candidate requires at least one source")
            indexes = candidate.source_indexes
            if any(
                not isinstance(index, int) or isinstance(index, bool) or index < 0
                for index in indexes
            ) or len(set(indexes)) != len(indexes):
                raise GovernanceError("maintenance candidate source indexes are invalid")
            if any(index >= len(normalized_sources) for index in indexes):
                raise GovernanceError("maintenance candidate source index is outside the snapshot")
            identity = (candidate.content, tuple(indexes))
            if identity in seen:
                continue
            seen.add(identity)
            normalized.append(candidate)
        return PreparedExtraction(
            source_cursor=source_cursor,
            sources=normalized_sources,
            candidates=tuple(normalized),
        )

    def _maintenance_source_available(
        self,
        connection: sqlite3.Connection,
        project: Mapping[str, Any],
        source: SourceRef,
        *,
        binding: Any,
        workspace_ref: str,
    ) -> bool:
        """Authorize a history Item using the commit connection itself."""

        if (
            source.source_type != "item"
            or source.availability != "available"
            or source.scope != self._scope(project)
            or source.permission_epoch != binding.permission_epoch
        ):
            return False
        state = connection.execute(
            "SELECT state FROM b25_governance_sources WHERE dataset_id=? AND source_key=?",
            (binding.dataset_id, source_key(source)),
        ).fetchone()
        if state is not None and state["state"] != "available":
            return False
        row = connection.execute(
            """
            SELECT i.body, i.sequence, t.workspace_ref
            FROM items i JOIN threads t ON t.id=i.thread_id
            WHERE i.id=?
            """,
            (source.source_id,),
        ).fetchone()
        if row is None or row["workspace_ref"] != workspace_ref:
            return False
        if int(row["sequence"]) != source.revision:
            return False
        try:
            item = Item.model_validate({**json.loads(row["body"]), "cursor": int(row["sequence"])})
        except Exception:
            return False
        return source.content_digest == self._source_digest(item)

    def commit_prepared_extraction(
        self,
        connection: sqlite3.Connection,
        snapshot: Any,
        extraction: PreparedExtraction,
    ) -> tuple[str, ...]:
        """Insert inferred versions/proposals and metadata into an open transaction.

        The method intentionally never calls ``commit``.  The maintenance
        committer owns the surrounding transaction and advances its source
        watermark beside this method.  Any validation error therefore rolls
        back both the candidate rows and the watermark.
        """

        if not isinstance(extraction, PreparedExtraction):
            raise GovernanceError("maintenance commit requires PreparedExtraction")
        extraction = self.prepare_extraction(
            source_cursor=extraction.source_cursor,
            sources=extraction.sources,
            candidates=extraction.candidates,
        )
        project_id = self._snapshot_field(snapshot, "project_id")
        dataset_id = self._snapshot_field(snapshot, "dataset_id")
        installation_id = self._snapshot_field(snapshot, "installation_id")
        binding_id = self._snapshot_field(snapshot, "binding_id")
        source_cursor = self._snapshot_field(snapshot, "source_cursor")
        expected_cursor = self._snapshot_field(snapshot, "expected_processed_cursor", 0)
        snapshot_scope = self._snapshot_field(snapshot, "scope")
        snapshot_digest = self._snapshot_field(snapshot, "source_digest")
        if not isinstance(project_id, str) or not isinstance(dataset_id, str):
            raise GovernanceError("maintenance snapshot identity is missing")
        if extraction.source_cursor != source_cursor or source_cursor < expected_cursor:
            raise GovernanceError("maintenance source watermark is stale")
        project = self._project(project_id)
        installation, binding = self._runtime_check(project, context=None, enabled=True)
        assert installation is not None and binding is not None
        if (
            installation.dataset_id != dataset_id
            or installation.installation_id != installation_id
            or installation.binding_id != binding_id
            or snapshot_scope != self._scope(project)
        ):
            raise GovernancePermissionError("maintenance binding or scope is stale")
        if snapshot_digest != _digest(
            [source.model_dump(mode="json") for source in extraction.sources]
        ):
            raise GovernanceError("maintenance source snapshot digest mismatch")
        budget = self._snapshot_field(snapshot, "budget")
        max_sources = self._snapshot_field(budget, "max_sources", 100)
        if len(extraction.sources) > max_sources or len(extraction.candidates) > max_sources:
            raise GovernanceError("maintenance source or candidate limit exceeded")
        workspace_row = connection.execute(
            "SELECT workspace_ref FROM workspace_initializations WHERE id=?",
            (project["workspace_id"],),
        ).fetchone()
        if workspace_row is None:
            raise GovernancePermissionError("maintenance workspace is unavailable")
        workspace_ref = str(workspace_row["workspace_ref"])
        self._require_schema(connection)
        for source in extraction.sources:
            if not self._maintenance_source_available(
                connection,
                project,
                source,
                binding=binding,
                workspace_ref=workspace_ref,
            ):
                raise GovernancePermissionError("maintenance source is unavailable or unauthorized")

        job_id = self._snapshot_field(snapshot, "job_id", "maintenance")
        prompt_version = self._snapshot_field(snapshot, "prompt_version", "maintenance")
        model_id = self._snapshot_field(snapshot, "model_id", "maintenance")
        proposal_ids: list[str] = []
        for candidate_index, candidate in enumerate(extraction.candidates):
            selected_sources = tuple(
                extraction.sources[index] for index in candidate.source_indexes
            )
            content_digest = hashlib.sha256(candidate.content.encode("utf-8")).hexdigest()
            fingerprint = _digest(
                {
                    "job_id": job_id,
                    "candidate_index": candidate_index,
                    "content_digest": content_digest,
                    "source_keys": [source_key(source) for source in selected_sources],
                    "source_cursor": source_cursor,
                }
            )
            record_id = "record_maint_" + fingerprint
            proposal_id = "proposal_maint_" + fingerprint
            existing = self.ledger._stored_proposal(connection, proposal_id)
            if existing is not None:
                if (
                    existing.owner.dataset_id != dataset_id
                    or existing.state not in {"pending", "accepted", "rejected"}
                    or existing.proposed_version.record_id != record_id
                    or existing.proposed_version.version != 1
                    or existing.proposed_version.content_digest != content_digest
                    or existing.source_refs != selected_sources
                ):
                    raise LedgerConflictError("maintenance proposal identity conflict")
                proposal_ids.append(proposal_id)
                continue
            now = _now()
            review_due_at = now + timedelta(days=MAINTENANCE_REVIEW_DAYS)
            conditions = MemoryConditions(
                commit_ref=None,
                tree_digest=None,
                file_fingerprints={},
                environment_digest=None,
                tool_versions={},
                verified_at=None,
                valid_from=now,
                valid_until=review_due_at,
            )
            version = MemoryVersion(
                ref=MemoryVersionRef(
                    dataset_id=dataset_id,
                    record_id=record_id,
                    version=1,
                    content_digest=content_digest,
                ),
                owner=cast(DatasetOwner, installation.owner),
                kind="project",
                content_type="fact",
                scope=self._scope(project),
                role_ids=(),
                agent_ids=(),
                content=candidate.content,
                sources=selected_sources,
                evidence="inferred",
                sensitivity="internal",
                retention_policy_id="retain-default",
                conditions=conditions,
                recorded_at=now,
            )
            head = self.ledger._stored_head(connection, dataset_id, record_id)
            if head is None:
                head = self.ledger._head_for_create(
                    dataset_id,
                    record_id,
                    permission_epoch=binding.permission_epoch,
                )
                self.ledger._insert_head(connection, head)
            elif head.state in {"deleted", "revoked"}:
                raise LedgerConflictError("maintenance target record is tombstoned")
            if head.revision != 0:
                raise LedgerConflictError("maintenance target head changed")
            self.ledger._insert_version(connection, version)
            proposal = MemoryProposal(
                proposal_id=proposal_id,
                proposal_revision=0,
                owner=cast(DatasetOwner, installation.owner),
                operation="create",
                base_head=head,
                proposed_version=version.ref,
                source_refs=selected_sources,
                extractor_version=str(prompt_version),
                reason="bounded maintenance extraction",
                state="pending",
            )
            proposal_digest = _digest(
                {
                    "job_id": job_id,
                    "proposal": proposal.model_dump(mode="json"),
                    "source_cursor": source_cursor,
                }
            )
            connection.execute(
                """
                INSERT INTO memory_ledger_proposals(
                    proposal_id, dataset_id, record_id, proposal_revision,
                    base_head_revision, proposed_version, state, body,
                    idempotency_key, request_digest, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    proposal.proposal_id,
                    dataset_id,
                    record_id,
                    proposal.proposal_revision,
                    proposal.base_head.revision,
                    proposal.proposed_version.version,
                    proposal.state,
                    _json(proposal),
                    proposal_digest,
                    now.isoformat(),
                ),
            )
            source_keys = [
                self._register_source(connection, dataset_id, source) for source in selected_sources
            ]
            connection.execute(
                """
                INSERT INTO b25_governance_proposals(
                    proposal_id, dataset_id, record_id, proposal_revision,
                    proposed_version, proposed_digest, base_head_revision,
                    review_due_at, source_watermark, source_keys_json, model_ids_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal_id,
                    dataset_id,
                    record_id,
                    proposal.proposal_revision,
                    proposal.proposed_version.version,
                    proposal.proposed_version.content_digest,
                    proposal.base_head.revision,
                    review_due_at.isoformat(),
                    str(source_cursor),
                    _json(source_keys),
                    _json([str(model_id)]),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            for source_key_value in source_keys:
                connection.execute(
                    """
                    INSERT INTO b25_governance_dependencies(
                        dataset_id, derived_record_id, derived_version, source_key,
                        state, reason, updated_at
                    ) VALUES (?, ?, 1, ?, 'active', NULL, ?)
                    """,
                    (dataset_id, record_id, source_key_value, now.isoformat()),
                )
            proposal_ids.append(proposal_id)
        return tuple(proposal_ids)

    propose_in_transaction = commit_prepared_extraction
    commit_maintenance_in_transaction = commit_prepared_extraction

    def _relationships_for(
        self, dataset: str, record_id: str, version: int
    ) -> list[MemoryRelationship]:
        with self.store._connect() as connection:
            self._require_schema(connection)
            rows = connection.execute(
                """
                SELECT relation, target_record_id, target_version, target_digest
                FROM b25_governance_relations
                WHERE dataset_id=? AND subject_record_id=? AND subject_version=?
                    AND relation IN ('conflicts_with', 'supersedes')
                ORDER BY relation_id
                """,
                (dataset, record_id, version),
            ).fetchall()
        return [
            MemoryRelationship(
                relation=row["relation"],
                target=MemoryVersionRef(
                    dataset_id=dataset,
                    record_id=row["target_record_id"],
                    version=row["target_version"],
                    content_digest=row["target_digest"],
                ),
            )
            for row in rows
        ]

    def _proposal_source_keys(self, proposal: MemoryProposal) -> tuple[str, ...]:
        return tuple(source_key(item) for item in _unique_sources(proposal.source_refs))

    def _independent_source_keys(
        self,
        dataset: str,
        sources: Sequence[SourceRef],
        *,
        seen_versions: set[tuple[str, int]] | None = None,
    ) -> tuple[str, ...]:
        """Flatten derived memory refs to their canonical evidence sources."""

        visited = seen_versions if seen_versions is not None else set()
        result: list[str] = []
        result_seen: set[str] = set()

        def add(value: str) -> None:
            if value not in result_seen:
                result_seen.add(value)
                result.append(value)

        for source in _unique_sources(sources):
            if source.source_type != "memory_version":
                add(source_key(source))
                continue
            marker = (source.source_id, source.revision)
            if marker in visited:
                continue
            visited.add(marker)
            try:
                try:
                    version = self.ledger.get_version(dataset, source.source_id, source.revision)
                except Exception:
                    add(source_key(source))
                    continue
                nested = self._independent_source_keys(
                    dataset, version.sources, seen_versions=visited
                )
                if nested:
                    for key in nested:
                        add(key)
                else:
                    add(source_key(source))
            finally:
                visited.remove(marker)
        return tuple(result)

    def _source_block_reason(self, dataset: str, proposal: MemoryProposal) -> str | None:
        with self.store._connect() as connection:
            self._require_schema(connection)
            keys = self._proposal_source_keys(proposal)
            if not keys:
                return "missing_source"
            placeholders = ",".join("?" for _ in keys)
            rows = connection.execute(
                "SELECT state FROM b25_governance_sources "
                f"WHERE dataset_id=? AND source_key IN ({placeholders})",
                (dataset, *keys),
            ).fetchall()
            dependency_rows = connection.execute(
                """
                SELECT state FROM b25_governance_dependencies
                WHERE dataset_id=? AND derived_record_id=? AND derived_version=?
                """,
                (dataset, proposal.proposed_version.record_id, proposal.proposed_version.version),
            ).fetchall()
        states = {str(row["state"]) for row in rows}
        states.update(str(row["state"]) for row in dependency_rows)
        if states.intersection({"revoked", "deleted", "unavailable", "blocked"}):
            return "source_unavailable"
        return None

    def _dependency_blocked(self, dataset: str, record_id: str, version: int) -> bool:
        with self.store._connect() as connection:
            self._require_schema(connection)
            row = connection.execute(
                """
                SELECT 1 FROM b25_governance_dependencies
                WHERE dataset_id=? AND derived_record_id=? AND derived_version=?
                    AND state != 'active' LIMIT 1
                """,
                (dataset, record_id, version),
            ).fetchone()
        return row is not None

    def _conditions_reason(
        self, version: MemoryVersion, *, now: datetime | None = None
    ) -> str | None:
        moment = now or _now()
        if moment < version.conditions.valid_from:
            return "conditions_not_yet_valid"
        if version.conditions.valid_until is not None and moment >= version.conditions.valid_until:
            return "conditions_expired"
        return None

    def _entry(
        self,
        project: Mapping[str, Any],
        proposal: MemoryProposal,
        *,
        context: RpcContext | None,
        review_due_at: datetime | None = None,
    ) -> GovernanceEntry:
        head = self.ledger.get_head(proposal.owner.dataset_id, proposal.proposed_version.record_id)
        version = self.ledger.get_version(
            proposal.proposed_version.dataset_id,
            proposal.proposed_version.record_id,
            proposal.proposed_version.version,
        )
        due = review_due_at
        if due is None:
            with self.store._connect() as connection:
                self._require_schema(connection)
                row = connection.execute(
                    "SELECT review_due_at FROM b25_governance_proposals WHERE proposal_id=?",
                    (proposal.proposal_id,),
                ).fetchone()
            if row is not None and row["review_due_at"]:
                due = datetime.fromisoformat(str(row["review_due_at"]))
        blocked = None
        if head.revision != proposal.base_head.revision:
            blocked = "revision_conflict"
        elif self._source_block_reason(proposal.owner.dataset_id, proposal):
            blocked = self._source_block_reason(proposal.owner.dataset_id, proposal)
        elif self._conditions_reason(version) is not None:
            blocked = self._conditions_reason(version)
        elif context is not None and not all(
            self._source_authorized(project, source, context=context)
            for source in proposal.source_refs
        ):
            blocked = "source_unauthorized"
        independent = len(
            self._independent_source_keys(
                proposal.owner.dataset_id,
                proposal.source_refs,
            )
        )
        return GovernanceEntry(
            proposal=proposal,
            version=version,
            current_head=head,
            review_due_at=due,
            review_expired=due is not None and _now() >= due,
            independent_evidence_count=independent,
            relationships=self._relationships_for(
                proposal.owner.dataset_id,
                proposal.proposed_version.record_id,
                proposal.proposed_version.version,
            ),
            blocked_reason=blocked,
        )

    def _build_version(
        self,
        project: Mapping[str, Any],
        *,
        installation: Any,
        record_id: str | None,
        content: str,
        sources: Sequence[SourceRef],
        valid_from: datetime | None,
        valid_until: datetime | None,
    ) -> MemoryVersion:
        dataset = installation.dataset_id
        actual_record = record_id or "record_" + uuid4().hex
        try:
            previous = self.ledger.list_versions(dataset, actual_record)
            next_version = max(item.ref.version for item in previous) + 1
        except LedgerNotFoundError:
            next_version = 1
        now = _now()
        normalized_sources = _unique_sources(sources)
        conditions = MemoryConditions(
            commit_ref=None,
            tree_digest=None,
            file_fingerprints={},
            environment_digest=None,
            tool_versions={},
            verified_at=None,
            valid_from=valid_from or now,
            valid_until=valid_until,
        )
        content_digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return MemoryVersion(
            ref=MemoryVersionRef(
                dataset_id=dataset,
                record_id=actual_record,
                version=next_version,
                content_digest=content_digest,
            ),
            owner=cast(DatasetOwner, installation.owner),
            kind="project",
            content_type="fact",
            scope=self._scope(project),
            role_ids=(),
            agent_ids=(),
            content=content,
            sources=normalized_sources,
            evidence="inferred",
            sensitivity="internal",
            retention_policy_id="retain-default",
            conditions=conditions,
            recorded_at=now,
        )

    def propose(
        self,
        project_id: str,
        version: MemoryVersion | Mapping[str, Any] | None = None,
        *,
        record_id: str | None = None,
        content: str | None = None,
        sources: Sequence[SourceRef | Mapping[str, Any]] = (),
        relationships: Sequence[MemoryRelationship | Mapping[str, Any]] = (),
        valid_from: datetime | None = None,
        valid_until: datetime | None = None,
        review_due_at: datetime | None = None,
        expected_head_revision: int | None = None,
        context: RpcContext | None = None,
        extractor_version: str = "user",
        model_ids: Sequence[str] = (),
        reason: str = "memory governance proposal",
        source_watermark: str | None = None,
        idempotency_key: str | None = None,
    ) -> GovernanceEntry:
        """Create a pending proposal after source and epoch checks.

        This method never confirms a proposal.  Even a caller identifying a
        model or maintenance process receives a pending candidate only.
        """

        project = self._project(project_id)
        installation, binding = self._runtime_check(project, context=context, enabled=True)
        assert installation is not None
        assert binding is not None
        if version is None:
            if content is None or not content.strip():
                raise ValueError("proposal content is required")
            normalized_sources = tuple(_as_source(item) for item in sources)
            built = self._build_version(
                project,
                installation=installation,
                record_id=record_id,
                content=content,
                sources=normalized_sources,
                valid_from=valid_from,
                valid_until=valid_until,
            )
        else:
            built = (
                version
                if isinstance(version, MemoryVersion)
                else MemoryVersion.model_validate(version)
            )
            if built.owner.dataset_id != installation.dataset_id or built.scope != self._scope(
                project
            ):
                raise GovernancePermissionError("proposal version is outside the project dataset")
            if valid_from is not None or valid_until is not None:
                conditions = built.conditions.model_copy(
                    update={
                        "valid_from": valid_from or built.conditions.valid_from,
                        "valid_until": (
                            valid_until if valid_until is not None else built.conditions.valid_until
                        ),
                    }
                )
                built = built.model_copy(update={"conditions": conditions})
            normalized_sources = (
                tuple(_as_source(item) for item in sources) if sources else built.sources
            )
            if _unique_sources(normalized_sources) != _unique_sources(built.sources):
                raise LedgerConflictError("proposal sources do not match the target version")
            if normalized_sources != built.sources:
                built = built.model_copy(update={"sources": _unique_sources(normalized_sources)})
        if not built.sources:
            raise GovernancePermissionError("semantic proposals require an authorized source")
        if any(source.source_type == "mailbox_message" for source in built.sources):
            raise GovernancePermissionError("private Mailbox cannot become a memory source")
        for source in built.sources:
            if source.scope != self._scope(project):
                raise GovernancePermissionError("proposal source is outside the project scope")

        due = review_due_at or built.conditions.valid_until
        normalized_relationships = tuple(
            item
            if isinstance(item, MemoryRelationship)
            else MemoryRelationship.model_validate(item)
            for item in relationships
        )
        governance_request_digest = hashlib.sha256(
            _json(
                {
                    "review_due_at": due.isoformat() if due else None,
                    "source_watermark": source_watermark,
                    "model_ids": sorted(set(model_ids)),
                    "relationships": [
                        relationship.model_dump(mode="json")
                        for relationship in normalized_relationships
                    ],
                }
            ).encode("utf-8")
        ).hexdigest()

        def commit_governance_metadata(
            connection: sqlite3.Connection, proposal: MemoryProposal
        ) -> None:
            self._proposal_metadata_in_connection(
                connection,
                proposal,
                review_due_at=due,
                source_watermark=source_watermark,
                model_ids=model_ids,
            )
            self._relation_rows_in_connection(
                connection,
                proposal.owner.dataset_id,
                proposal,
                normalized_relationships,
            )
            self._dependency_rows_in_connection(connection, proposal)

        proposal = self.ledger.propose(
            built,
            operation="modify" if built.ref.version > 1 else "create",
            source_refs=built.sources,
            extractor_version=extractor_version,
            reason=reason,
            expected_head_revision=expected_head_revision,
            idempotency_key=idempotency_key,
            request_digest=governance_request_digest,
            permission_epoch=(
                context.permission_epoch if context is not None else binding.permission_epoch
            ),
            transaction_guard=commit_governance_metadata,
        )
        return self._entry(project, proposal, context=context, review_due_at=due)

    def inbox(
        self,
        project_id: str,
        *,
        context: RpcContext | None = None,
        include_expired: bool = True,
    ) -> list[GovernanceEntry]:
        """Return visible pending candidates and their current blockers."""

        project = self._project(project_id)
        installation, _binding = self._runtime_check(project, context=context, enabled=False)
        if installation is None:
            return []
        proposals = self.ledger.list_proposals(installation.dataset_id, states=("pending",))
        entries: list[GovernanceEntry] = []
        for proposal in proposals:
            try:
                version = self.ledger.get_version(
                    proposal.proposed_version.dataset_id,
                    proposal.proposed_version.record_id,
                    proposal.proposed_version.version,
                )
            except LedgerNotFoundError:
                continue
            if version.scope != self._scope(project):
                continue
            entry = self._entry(project, proposal, context=context)
            if not include_expired and entry.review_expired:
                continue
            entries.append(entry)
        return entries

    list_inbox = inbox

    def review(
        self,
        project_id: str,
        selections: Sequence[ExactProposal | Mapping[str, Any]] | ExactProposal | Mapping[str, Any],
        *,
        decision: Literal["accept", "reject"],
        context: RpcContext | None = None,
        reviewer_id: str = "user",
        reviewer_kind: Literal["user", "system"] = "user",
        reason: str | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[GovernanceEntry, ...]:
        """Atomically accept or reject exact proposal selections."""

        if reviewer_kind not in _REVIEWER_KINDS:
            raise GovernancePermissionError("unknown reviewer kind")
        if reviewer_kind == "system" and decision == "accept":
            raise GovernancePermissionError("background maintenance cannot self-confirm proposals")
        project = self._project(project_id)
        installation, binding = self._runtime_check(project, context=context, enabled=True)
        assert installation is not None
        assert binding is not None
        if isinstance(selections, (Mapping, ExactProposal)):
            values: Sequence[ExactProposal | Mapping[str, Any]] = (selections,)
        else:
            values = selections
        exact = tuple(_as_exact(item) for item in values)
        if not exact:
            raise ValueError("proposal review requires at least one exact selection")
        proposals: list[MemoryProposal] = []
        for selection in exact:
            proposal = self.ledger.get_proposal(selection.proposal_id)
            if proposal.owner.dataset_id != installation.dataset_id:
                raise GovernancePermissionError("proposal is outside the project dataset")
            if (
                proposal.proposal_revision != selection.proposal_revision
                or proposal.proposed_version != selection.proposed_version
                or proposal.base_head.revision != selection.base_head_revision
            ):
                raise LedgerConflictError("exact proposal selection is stale")
            if decision == "accept":
                if self._conditions_reason(
                    self.ledger.get_version(
                        proposal.proposed_version.dataset_id,
                        proposal.proposed_version.record_id,
                        proposal.proposed_version.version,
                    )
                ):
                    raise GovernancePermissionError("proposal conditions are not currently valid")
                if not all(
                    self._source_authorized(project, source, context=context)
                    for source in proposal.source_refs
                ):
                    raise GovernancePermissionError(
                        "proposal source is unavailable or unauthorized"
                    )
            proposals.append(proposal)
        permission_epoch = (
            context.permission_epoch if context is not None else binding.permission_epoch
        )

        def commit_governance(
            connection: sqlite3.Connection, stored: tuple[MemoryProposal, ...]
        ) -> None:
            self._runtime_check(project, context=context, enabled=True)
            now = _now_iso()
            retire: dict[str, MemoryHead] = {}
            selected_records = {p.proposed_version.record_id for p in stored}
            for proposal in stored:
                if decision == "accept":
                    metadata = connection.execute(
                        "SELECT review_due_at FROM b25_governance_proposals WHERE proposal_id=?",
                        (proposal.proposal_id,),
                    ).fetchone()
                    if (
                        metadata is not None
                        and metadata["review_due_at"] is not None
                        and datetime.fromisoformat(metadata["review_due_at"]) <= _now()
                    ):
                        raise LedgerConflictError("proposal review deadline expired")
                    if not self.version_dependencies_valid(
                        proposal.proposed_version, project_id=project_id, context=context
                    ):
                        raise GovernancePermissionError(
                            "proposal source or validity is no longer current"
                        )
                    relations = connection.execute(
                        "SELECT * FROM b25_governance_relations WHERE dataset_id=? "
                        "AND subject_record_id=? AND subject_version=? "
                        "AND relation IN ('conflicts_with','supersedes')",
                        (
                            installation.dataset_id,
                            proposal.proposed_version.record_id,
                            proposal.proposed_version.version,
                        ),
                    ).fetchall()
                    for relation in relations:
                        target = MemoryVersionRef(
                            dataset_id=installation.dataset_id,
                            record_id=relation["target_record_id"],
                            version=relation["target_version"],
                            content_digest=relation["target_digest"],
                        )
                        head = self.ledger._stored_head(
                            connection, installation.dataset_id, target.record_id
                        )
                        if (
                            head is None
                            or head.published_version != target
                            or head.state != "published"
                        ):
                            raise LedgerConflictError("relationship target changed")
                        if relation["relation"] == "conflicts_with":
                            raise GovernanceError("conflict_resolution_required")
                        if target.record_id in selected_records or target.record_id in retire:
                            raise LedgerConflictError("ambiguous replacement in batch")
                        retire[target.record_id] = head
                connection.execute(
                    "INSERT INTO b25_governance_reviews VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        "review_" + uuid4().hex,
                        proposal.proposal_id,
                        installation.dataset_id,
                        proposal.proposal_revision,
                        decision,
                        reviewer_id,
                        reviewer_kind,
                        reason,
                        now,
                    ),
                )
            for head in retire.values():
                retired = head.model_copy(
                    update={
                        "state": "inactive",
                        "revision": head.revision + 1,
                        "publication_cursor": self.ledger._next_cursor(connection),
                    }
                )
                connection.execute(
                    "UPDATE memory_ledger_heads SET state='inactive', revision=?, "
                    "publication_cursor=?, body=? "
                    "WHERE dataset_id=? AND record_id=? AND revision=?",
                    (
                        retired.revision,
                        retired.publication_cursor,
                        retired.model_dump_json(),
                        head.dataset_id,
                        head.record_id,
                        head.revision,
                    ),
                )

        results = self.ledger.review_proposals_batch(
            exact,
            decision=decision,
            dataset_id=installation.dataset_id,
            permission_epoch=permission_epoch,
            idempotency_key=idempotency_key,
            transaction_guard=commit_governance,
        )
        entries: list[GovernanceEntry] = []
        for selection in exact:
            updated = self.ledger.get_proposal(selection.proposal_id)
            entries.append(self._entry(project, updated, context=context))
        del results  # The projection above is the stable Core-facing return.
        return tuple(entries)

    review_batch = review

    def revoke_source(
        self,
        project_id: str,
        source: SourceRef | Mapping[str, Any],
        *,
        context: RpcContext | None = None,
        reason: str = "source revoked",
    ) -> tuple[str, ...]:
        """Revoke one source and recursively block directly derived versions."""

        project = self._project(project_id)
        installation, binding = self._runtime_check(project, context=context, enabled=False)
        if installation is None:
            raise GovernancePermissionError("memory governance has no selected installation")
        assert binding is not None
        normalized = _as_source(source)
        if normalized.source_type == "mailbox_message":
            raise GovernancePermissionError(
                "private Mailbox cannot be revoked into public governance"
            )
        if normalized.scope != self._scope(project):
            raise GovernancePermissionError("source is outside the project scope")
        key = source_key(normalized)
        permission_epoch = normalized.permission_epoch
        if context is not None:
            if context.permission_epoch != binding.permission_epoch:
                raise GovernancePermissionError("stale source revocation epoch")
            permission_epoch = context.permission_epoch
        affected: list[str] = []
        queue: deque[str] = deque([key])
        seen: set[str] = set()
        with self.store._connect() as connection:
            self._require_schema(connection)
            self._register_source(
                connection,
                installation.dataset_id,
                normalized.model_copy(
                    update={"permission_epoch": permission_epoch, "availability": "revoked"}
                ),
                state="revoked",
                reason=reason,
            )
            while queue:
                source_key_value = queue.popleft()
                if source_key_value in seen:
                    continue
                seen.add(source_key_value)
                rows = connection.execute(
                    """
                    SELECT derived_record_id, derived_version
                    FROM b25_governance_dependencies
                    WHERE dataset_id=? AND source_key=? AND state != 'revoked'
                    ORDER BY derived_record_id, derived_version
                    """,
                    (installation.dataset_id, source_key_value),
                ).fetchall()
                for row in rows:
                    record_id = str(row["derived_record_id"])
                    version = int(row["derived_version"])
                    identifier = f"{record_id}@{version}"
                    if identifier not in affected:
                        affected.append(identifier)
                    connection.execute(
                        """
                        UPDATE b25_governance_dependencies
                        SET state='blocked', reason=?, updated_at=?
                        WHERE dataset_id=? AND derived_record_id=? AND derived_version=?
                            AND source_key=?
                        """,
                        (
                            reason,
                            _now_iso(),
                            installation.dataset_id,
                            record_id,
                            version,
                            source_key_value,
                        ),
                    )
                    # A derived memory version can itself be a source for a
                    # later derived version.  No body is copied; only the
                    # stable source identity is used for the next hop.
                    try:
                        derived = self.ledger.get_version(
                            installation.dataset_id, record_id, version
                        )
                    except Exception:
                        continue
                    queue.append(
                        source_key(
                            SourceRef(
                                source_type="memory_version",
                                source_id=derived.ref.record_id,
                                revision=derived.ref.version,
                                content_digest=derived.ref.content_digest,
                                scope=derived.scope,
                                permission_epoch=permission_epoch,
                                availability="available",
                            )
                        )
                    )
        from operant.memory_plugins.experience_skills import ExperienceSkillService

        # Current authorization already fails closed after the governance
        # transaction; also update the derived Skill lifecycle projection.
        affected.extend(
            ExperienceSkillService(self.manager).propagate_source_revocation(
                project_id, normalized, reason=reason[:500] or "source revoked"
            )
        )
        return tuple(affected)

    revoke = revoke_source

    def _history_source(
        self,
        project: Mapping[str, Any],
        item: Item,
        *,
        context: RpcContext | None,
    ) -> SourceRef:
        if context is not None:
            permission_epoch = context.permission_epoch
        else:
            installation = self._installation(project, enabled=False)
            if installation is None or installation.binding_id is None:
                permission_epoch = 0
            else:
                permission_epoch = self.manager.registry.get_binding(
                    installation.binding_id
                ).permission_epoch
        return SourceRef(
            source_type="item",
            source_id=item.id,
            revision=item.cursor or 0,
            content_digest=self._source_digest(item),
            scope=self._scope(project),
            permission_epoch=permission_epoch,
            availability="available",
        )

    def search_history(
        self,
        project_id: str,
        query: str | None = None,
        *,
        cutoff_cursor: int | None = None,
        after_cursor: int | None = None,
        limit: int = 100,
        thread_id: str | None = None,
        context: RpcContext | None = None,
    ) -> HistoryPage:
        """Search authorized canonical history at a fixed historical cursor."""

        if not 1 <= limit <= 100:
            raise ValueError("history limit must be between 1 and 100")
        project = self._project(project_id)
        self._runtime_check(project, context=context, enabled=False)
        workspace_ref = self._workspace_ref(project)
        current_cutoff = 0
        # The small read-only query below intentionally uses only canonical
        # ``threads`` and ``items``.  There is no Mailbox join or copy path.
        with self.store._connect() as connection:
            row = connection.execute("SELECT COALESCE(MAX(sequence), 0) FROM items").fetchone()
            current_cutoff = int(row[0]) if row is not None else 0
            effective_cutoff = current_cutoff if cutoff_cursor is None else cutoff_cursor
            if effective_cutoff < 0:
                raise ValueError("cutoff_cursor must be non-negative")
            if cutoff_cursor is not None and cutoff_cursor > current_cutoff:
                raise ValueError("cutoff_cursor cannot be in the future")
            if after_cursor is not None and after_cursor < 0:
                raise ValueError("after_cursor must be non-negative")
            clauses = [
                "t.workspace_ref = ?",
                "i.sequence <= ?",
                "i.sequence > ?",
            ]
            params: list[Any] = [workspace_ref, effective_cutoff, after_cursor or 0]
            if thread_id is not None:
                clauses.append("i.thread_id = ?")
                params.append(thread_id)
            if query:
                clauses.append("lower(i.body) LIKE ?")
                params.append("%" + query.casefold() + "%")
            params.append(limit + 1)
            rows = connection.execute(
                """
                SELECT i.body, i.sequence
                FROM items i JOIN threads t ON t.id=i.thread_id
                WHERE """
                + " AND ".join(clauses)
                + " ORDER BY i.sequence LIMIT ?",
                params,
            ).fetchall()
        entries: list[HistoryEntry] = []
        for row in rows[:limit]:
            item = Item.model_validate({**json.loads(row["body"]), "cursor": int(row["sequence"])})
            source = self._history_source(project, item, context=context)
            if not self._source_authorized(project, source, context=context):
                continue
            entries.append(
                HistoryEntry(
                    item_id=item.id,
                    thread_id=item.thread_id,
                    cursor=int(row["sequence"]),
                    occurred_at=item.created_at,
                    kind=item.item_type.value,
                    excerpt=_payload_excerpt(item),
                    source=source,
                )
            )
        scanned = rows[:limit]
        next_cursor = int(scanned[-1]["sequence"]) if len(rows) > limit and scanned else None
        return HistoryPage(
            project_id=project_id,
            items=entries,
            next_cursor=next_cursor,
            cutoff_cursor=effective_cutoff,
        )

    history_search = search_history
    search = search_history

    def history_detail(
        self,
        project_id: str,
        item_id: str,
        *,
        cutoff_cursor: int | None = None,
        context: RpcContext | None = None,
    ) -> HistoryDetail:
        """Expand one canonical item after repeating current permission checks."""

        project = self._project(project_id)
        self._runtime_check(project, context=context, enabled=False)
        if cutoff_cursor is not None:
            with self.store._connect() as connection:
                current_cursor = int(
                    connection.execute("SELECT COALESCE(MAX(sequence), 0) FROM items").fetchone()[0]
                )
            if cutoff_cursor > current_cursor:
                raise ValueError("cutoff_cursor cannot be in the future")
        try:
            item = self.store.get_item(item_id)
            thread = self.store.get_thread(item.thread_id)
        except Exception as exc:
            raise LedgerNotFoundError("canonical history item not found") from exc
        if thread.workspace_ref != self._workspace_ref(project):
            raise GovernancePermissionError("history item is outside the project scope")
        if cutoff_cursor is not None and (item.cursor or 0) > cutoff_cursor:
            raise GovernancePermissionError("history item is outside the requested cutoff")
        source = self._history_source(project, item, context=context)
        if not self._source_authorized(project, source, context=context):
            raise GovernancePermissionError("history item is no longer available")
        return HistoryDetail(
            project_id=project_id,
            entry=HistoryEntry(
                item_id=item.id,
                thread_id=item.thread_id,
                cursor=item.cursor or 0,
                occurred_at=item.created_at,
                kind=item.item_type.value,
                excerpt=_payload_excerpt(item),
                source=source,
            ),
            text=_payload_text(item),
        )

    get_history_item = history_detail
    detail = history_detail

    def current_records(
        self, project_id: str, *, context: RpcContext | None = None
    ) -> list[GovernanceRecord]:
        """Project the currently published agreement for one project."""

        project = self._project(project_id)
        installation, _binding = self._runtime_check(project, context=context, enabled=False)
        if installation is None:
            return []
        scope = self._scope(project)
        records: list[GovernanceRecord] = []
        for version in self.ledger.query(
            installation.dataset_id,
            scope=scope,
            include_candidates=False,
            include_inactive=True,
            include_legacy=False,
        ):
            head = self.ledger.get_head(installation.dataset_id, version.ref.record_id)
            if head.published_version != version.ref:
                continue
            # A current version has no proposal object in the projection.  Use
            # its own source list to check dependency states directly.
            source_blocked = False
            with self.store._connect() as connection:
                self._require_schema(connection)
                keys = tuple(source_key(item) for item in _unique_sources(version.sources))
                if keys:
                    placeholders = ",".join("?" for _ in keys)
                    rows = connection.execute(
                        "SELECT state FROM b25_governance_sources "
                        f"WHERE dataset_id=? AND source_key IN ({placeholders})",
                        (installation.dataset_id, *keys),
                    ).fetchall()
                    source_blocked = any(
                        row["state"] in {"revoked", "deleted", "unavailable", "blocked"}
                        for row in rows
                    )
            dependencies_valid = self.version_dependencies_valid(
                version.ref,
                project_id=project_id,
                context=context,
            )
            usable = (
                head.state == "published"
                and not source_blocked
                and dependencies_valid
                and self._conditions_reason(version) is None
                and installation.state == "enabled"
                and bool(getattr(self.manager, "_state", {}).get("global_enabled", True))
                and bool(project.get("memory_enabled", True))
            )
            records.append(
                GovernanceRecord(
                    version=version,
                    head=head,
                    independent_evidence_count=len(
                        self._independent_source_keys(
                            installation.dataset_id,
                            version.sources,
                        )
                    ),
                    relationships=self._relationships_for(
                        installation.dataset_id, version.ref.record_id, version.ref.version
                    ),
                    currently_usable=usable,
                    blocked_reason=(
                        "source_unavailable"
                        if source_blocked or not dependencies_valid
                        else self._conditions_reason(version)
                        if self._conditions_reason(version) is not None
                        else None
                    ),
                )
            )
        return records

    get_records = current_records

    def get_governance(
        self, project_id: str, *, context: RpcContext | None = None
    ) -> GovernanceState:
        project = self._project(project_id)
        installation, _binding = self._runtime_check(project, context=context, enabled=False)
        if installation is None:
            return GovernanceState(
                project_id=project_id,
                enabled=False,
                records=[],
                proposals=[],
                jobs=[],
            )
        enabled = (
            bool(getattr(self.manager, "_state", {}).get("global_enabled", True))
            and bool(project.get("memory_enabled", True))
            and installation.state == "enabled"
        )
        return GovernanceState(
            project_id=project_id,
            enabled=enabled,
            records=self.current_records(project_id, context=context),
            proposals=self.inbox(project_id, context=context),
            jobs=[],
        )

    state = get_governance

    def is_recallable(
        self,
        project_id: str,
        ref: MemoryVersionRef | Mapping[str, Any],
        *,
        context: RpcContext | None = None,
    ) -> bool:
        """Fail-closed check for Recall/Manager integration after revocation."""

        normalized = _as_ref(ref)
        try:
            project = self._project(project_id)
            installation, _binding = self._runtime_check(project, context=context, enabled=True)
            assert installation is not None
            if normalized.dataset_id != installation.dataset_id:
                return False
            version = self.ledger.get_version(
                normalized.dataset_id, normalized.record_id, normalized.version
            )
            head = self.ledger.get_head(normalized.dataset_id, normalized.record_id)
            if head.state != "published" or head.published_version != normalized:
                return False
            if self._conditions_reason(version) is not None:
                return False
            if self._dependency_blocked(
                normalized.dataset_id, normalized.record_id, normalized.version
            ):
                return False
            return all(
                self._source_authorized(project, source, context=context)
                for source in version.sources
            )
        except Exception:
            return False

    def source_authorized(
        self,
        project_id: str,
        source: SourceRef | Mapping[str, Any],
        *,
        context: RpcContext | None = None,
    ) -> bool:
        """Public Core authorization wrapper for a current source reference."""

        try:
            project = self._project(project_id)
            return self._source_authorized(project, _as_source(source), context=context)
        except Exception:
            return False

    authorize_source = source_authorized

    def _historical_version_dependencies(
        self,
        project: Mapping[str, Any],
        ref: MemoryVersionRef,
        *,
        context: RpcContext | None,
        seen: set[tuple[str, str, int]],
    ) -> bool:
        marker = (ref.dataset_id, ref.record_id, ref.version)
        if marker in seen:
            return False
        seen.add(marker)
        try:
            try:
                version = self.ledger.get_version(ref.dataset_id, ref.record_id, ref.version)
                head = self.ledger.get_head(ref.dataset_id, ref.record_id)
            except Exception:
                return False
            if (
                version.ref != ref
                or self._conditions_reason(version) is not None
                or head.state in {"inactive", "revoked", "deleted"}
            ):
                return False
            if self._dependency_blocked(ref.dataset_id, ref.record_id, ref.version):
                return False
            from operant.memory_plugins.worktree_knowledge import publication_active

            if not publication_active(self.manager, ref):
                return False
            for source in version.sources:
                if source.source_type == "memory_version":
                    if not self._historical_version_dependencies(
                        project,
                        MemoryVersionRef(
                            dataset_id=ref.dataset_id,
                            record_id=source.source_id,
                            version=source.revision,
                            content_digest=source.content_digest,
                        ),
                        context=context,
                        seen=seen,
                    ):
                        return False
                elif not self._source_authorized(project, source, context=context):
                    return False
            return True
        finally:
            seen.remove(marker)

    def version_dependencies_valid(
        self,
        ref: MemoryVersionRef | Mapping[str, Any],
        *,
        project_id: str | None = None,
        context: RpcContext | None = None,
    ) -> bool:
        """Validate conditions and source dependencies for frozen history.

        Unlike :meth:`is_recallable`, this does not require ``ref`` to remain
        the current head.  It is intended for a Run whose frozen cutoff still
        legitimately points to an older published version; current revocation,
        source permission and dependency blockers still take precedence.
        """

        normalized = _as_ref(ref)
        try:
            project: Mapping[str, Any] | None = None
            if project_id is not None:
                project = self._project(project_id)
            else:
                for candidate in getattr(self.manager, "_state", {}).get("projects", []):
                    if not candidate.get("installation_id"):
                        continue
                    installation = self.manager.registry.get_installation(
                        candidate["installation_id"]
                    )
                    if installation.dataset_id == normalized.dataset_id:
                        project = candidate
                        break
            if project is None:
                return False
            return self._historical_version_dependencies(
                project,
                normalized,
                context=context,
                seen=set(),
            )
        except Exception:
            return False

    check_version_dependencies = version_dependencies_valid


__all__ = [
    "GovernanceError",
    "GovernancePermissionError",
    "GovernanceSchemaError",
    "GovernanceService",
    "PreparedCandidate",
    "PreparedExtraction",
    "source_key",
]
