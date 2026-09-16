"""Bounded, idempotent memory maintenance execution.

This module deliberately sits between the existing Scheduler and the memory
ledger.  The Scheduler owns queue state, leases, cancellation and retry/DLQ;
the maintenance runner owns only a frozen input snapshot and the short
extract/validate/commit sequence.  Model work happens before the commit
callback is entered.  A ledger adapter supplied by the Core/治理 service must
insert pending proposals and advance the source watermark on the same SQLite
transaction.

The public B2-5 API can project these internal records later.  The classes here
are intentionally small protocols so the Core can compose them without giving
an optional plugin a second scheduler or publication authority.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import Awaitable, Callable, Iterable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from operant.contracts.b2_1 import (
    Scope,
    SourceRef,
)
from operant.contracts.b2_5 import B25Command, MaintenanceJobView
from operant.domain.graph import (
    GraphRunStatus,
    NodeKind,
    NodeSpec,
    WorkflowDefinition,
    WorkflowDefinitionStatus,
)
from operant.domain.models import RolePreset, RoleStatus, ToolPolicy
from operant.domain.scheduler import (
    DispatchIdempotency,
    MisfirePolicy,
    ScheduleDefinition,
    ScheduleStatus,
    TriggerKind,
)
from operant.domain.threads import ConversationThread, LegacySourceType
from operant.memory_plugins.maintenance_schema import SCHEMA_SQL

if TYPE_CHECKING:
    from operant.application.service import ApplicationService
    from operant.memory_plugins.governance import GovernanceService


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


class MaintenanceState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    NO_CHANGE = "no_change"
    CANCELLED = "cancelled"
    RETRY_WAIT = "retry_wait"
    DEAD_LETTER = "dead_letter"
    MANUAL_RECONCILE_REQUIRED = "manual_reconcile_required"
    BLOCKED = "blocked"


class MaintenanceError(RuntimeError):
    """A safe maintenance failure with Scheduler routing information."""

    def __init__(
        self,
        code: str,
        *,
        retryable: bool = True,
        outcome_unknown: bool = False,
    ) -> None:
        self.code = code
        self.retryable = retryable
        self.outcome_unknown = outcome_unknown
        super().__init__(code)


class MaintenanceBudget(BaseModel):
    """Budget independent from foreground Session/Graph budgets."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_sources: int = Field(default=20, ge=1, le=100)
    max_input_tokens: int = Field(default=8_000, ge=0, le=1_000_000)
    max_output_tokens: int = Field(default=1_024, ge=64, le=16_384)
    timeout_seconds: int = Field(default=120, ge=1, le=3_600)
    max_attempts: int = Field(default=3, ge=1, le=20)


class MaintenanceSnapshot(BaseModel):
    """All mutable inputs frozen before a maintenance model call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str = Field(default_factory=lambda: f"maintenance_{uuid4().hex}")
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=300)
    project_id: str = Field(min_length=1, max_length=200)
    dataset_id: str = Field(min_length=1, max_length=200)
    installation_id: str = Field(min_length=1, max_length=200)
    binding_id: str = Field(min_length=1, max_length=200)
    binding_epoch: int = Field(default=0, ge=0)
    permission_epoch: int = Field(default=0, ge=0)
    scope: Scope
    source_stream: str = Field(default="project_history", min_length=1, max_length=200)
    source_cursor: int = Field(ge=0)
    expected_processed_cursor: int = Field(default=0, ge=0)
    source_digest: str = Field(min_length=1, max_length=128)
    workflow_id: str = Field(min_length=1, max_length=200)
    workflow_version: int = Field(ge=1)
    plugin_id: str = Field(min_length=1, max_length=200)
    plugin_version: str = Field(min_length=1, max_length=200)
    package_digest: str = Field(min_length=1, max_length=128)
    config_id: str = Field(min_length=1, max_length=200)
    config_revision: int = Field(ge=0)
    config_digest: str = Field(min_length=1, max_length=128)
    model_profile_id: str = Field(min_length=1, max_length=200)
    model_id: str = Field(min_length=1, max_length=200)
    model_digest: str = Field(min_length=1, max_length=128)
    prompt_version: str = Field(min_length=1, max_length=200)
    # Internal extractor binding.  These values are deliberately kept in the
    # frozen snapshot even though the public JobView only exposes the model.
    role_id: str | None = Field(default=None, max_length=200)
    role_version: int | None = Field(default=None, ge=1)
    workspace: str | None = Field(default=None, max_length=4096)
    budget: MaintenanceBudget = Field(default_factory=MaintenanceBudget)
    action: str = Field(default="conflict_check", min_length=1, max_length=50)

    @model_validator(mode="after")
    def validate_snapshot(self) -> MaintenanceSnapshot:
        if self.source_cursor < self.expected_processed_cursor:
            raise ValueError("source_cursor cannot be before expected_processed_cursor")
        return self

    @property
    def stable_idempotency_key(self) -> str:
        return self.idempotency_key or self.job_id

    @property
    def request_digest(self) -> str:
        return _digest(self.model_dump(mode="json", exclude={"idempotency_key"}))

    @property
    def snapshot_digest(self) -> str:
        return _digest(self.model_dump(mode="json"))


class MaintenanceResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str
    idempotency_key: str
    state: MaintenanceState
    source_cursor: int
    processed_cursor: int
    proposal_ids: tuple[str, ...] = ()
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    error_code: str | None = None
    graph_run_id: str | None = None
    run_request_id: str | None = None
    updated_at: datetime = Field(default_factory=utc_now)


class MaintenanceSourceProvider(Protocol):
    """Return only Core-authorized canonical Item source refs at a cutoff."""

    def list_sources(
        self,
        snapshot: MaintenanceSnapshot,
        *,
        limit: int,
    ) -> tuple[SourceRef, ...]: ...


class MaintenanceExtractor(Protocol):
    """Finite semantic extraction outside the ledger transaction."""

    async def extract(
        self,
        snapshot: MaintenanceSnapshot,
        sources: tuple[SourceRef, ...],
        *,
        cancel_event: asyncio.Event,
    ) -> MaintenanceExtraction: ...


class PreparedCandidate(BaseModel):
    """The only semantic object a model may produce for maintenance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    content: str = Field(min_length=1, max_length=100_000)
    source_indices: tuple[int, ...] = Field(min_length=1, max_length=20)


class MaintenanceExtraction(BaseModel):
    """Finite model output; Core/Governance create version and proposal IDs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1, max_length=300)
    source_watermark: int = Field(ge=0)
    candidates: tuple[PreparedCandidate, ...] = Field(max_length=100)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)


class FiniteSessionExtractor:
    """Use one formal, no-tool Session for bounded semantic extraction.

    The Session is attached to a marked maintenance Thread.  Its request and
    runtime events remain auditable, while the source provider excludes that
    marked thread from the project's canonical UserMessage source stream.  The
    Core caller must still provide the exact ModelProfile digest captured in
    :class:`MaintenanceSnapshot`.
    """

    def __init__(
        self,
        service: ApplicationService,
        *,
        role_id: str,
        workspace: str | Path,
        source_reader: Callable[[SourceRef], str | Awaitable[str]],
        role_version: int | None = None,
    ) -> None:
        self.service = service
        self.role_id = role_id
        self.workspace = str(Path(workspace).resolve(strict=True))
        self.source_reader = source_reader
        self.role_version = role_version
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost_usd: float | None = None

    async def extract(
        self,
        snapshot: MaintenanceSnapshot,
        sources: tuple[SourceRef, ...],
        *,
        cancel_event: asyncio.Event,
    ) -> MaintenanceExtraction:
        profile = self.service.get_model_profile(snapshot.model_profile_id)
        if (
            profile.model_id != snapshot.model_id
            or _digest(profile.model_dump(mode="json")) != snapshot.model_digest
        ):
            raise MaintenanceError("maintenance.model_snapshot_stale", retryable=False)
        role = self.service.get_role(self.role_id)
        if self.role_version is not None and role.version != self.role_version:
            raise MaintenanceError("maintenance.role_snapshot_stale", retryable=False)
        thread = self.service.create_thread(
            ConversationThread(id="thread_maintenance_" + uuid4().hex, workspace_ref=self.workspace)
        )
        session = self.service.create_session(
            role.id,
            model_profile_id=profile.id,
            budget_overrides={
                "max_output_tokens": snapshot.budget.max_output_tokens,
                "timeout_seconds": snapshot.budget.timeout_seconds,
            },
            # The marked Thread is auditable but is excluded by
            # CanonicalSourceProvider from the project source stream.
            thread_id=thread.id,
        )
        if session.role_snapshot.tool_policy != ToolPolicy():
            raise MaintenanceError("maintenance.extractor_tools_enabled", retryable=False)
        texts: list[str] = []
        for source in sources:
            if cancel_event.is_set():
                raise MaintenanceError("maintenance.cancelled", retryable=False)
            value = self.source_reader(source)
            if hasattr(value, "__await__"):
                value = await cast(Awaitable[str], value)
            texts.append(str(value)[:100_000])
        prompt = self._prompt(snapshot, sources, texts)
        from operant.application.token_counting import count_context_tokens

        count = count_context_tokens(
            snapshot.model_id,
            messages=(
                {"role": "system", "content": session.role_snapshot.system_prompt},
                {"role": "user", "content": prompt},
            ),
            tool_schemas=(),
        )
        # Reserve Core/Provider framing beyond this no-history, no-tool prompt.
        if count.input_tokens + 1024 > snapshot.budget.max_input_tokens:
            raise MaintenanceError("maintenance.input_budget_exceeded", retryable=False)
        final: str | None = None
        usage_payload: dict[str, Any] | None = None
        async for event in self.service.run_session(
            session.id,
            user_message=prompt,
            workspace=self.workspace,
            thread_id=thread.id,
            memory_enabled=False,
        ):
            if cancel_event.is_set():
                with suppress(Exception):
                    self.service.cancel_session(session.id)
                raise MaintenanceError("maintenance.cancelled", retryable=False)
            if event.event_type == "agent.completed":
                content = event.payload.get("content")
                if isinstance(content, str):
                    final = content
            elif event.event_type == "model.completed":
                usage = event.payload.get("usage")
                if isinstance(usage, dict):
                    usage_payload = usage
            elif event.event_type in {
                "agent.failed",
                "agent.cancelled",
                "agent.timed_out",
                "agent.stream_error",
                "budget.exhausted",
            }:
                raise MaintenanceError("maintenance.extractor_failed", retryable=True)
        if final is None:
            raise MaintenanceError("maintenance.extractor_result_missing", retryable=True)
        if usage_payload is None:
            raise MaintenanceError("maintenance.usage_unknown", retryable=False)

        def usage_int(name: str) -> int | None:
            value = usage_payload.get(name)
            return (
                value
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0
                else None
            )

        input_tokens = usage_int("prompt_tokens")
        output_tokens = usage_int("completion_tokens")
        if input_tokens is None or output_tokens is None:
            raise MaintenanceError("maintenance.usage_unknown", retryable=False)
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        if input_tokens > snapshot.budget.max_input_tokens:
            raise MaintenanceError("maintenance.input_budget_exceeded", retryable=False)
        if (
            profile.input_usd_per_million_tokens is not None
            and profile.output_usd_per_million_tokens is not None
        ):
            self.cost_usd = (
                input_tokens * profile.input_usd_per_million_tokens
                + output_tokens * profile.output_usd_per_million_tokens
            ) / 1_000_000
        try:
            payload = final.strip()
            if payload.startswith("```"):
                payload = payload.strip("`").strip()
                if payload.startswith("json"):
                    payload = payload[4:].lstrip()
            batch = MaintenanceExtraction.model_validate_json(payload)
        except Exception as exc:
            raise MaintenanceError("maintenance.extractor_invalid_json", retryable=False) from exc
        if batch.request_id != snapshot.job_id:
            raise MaintenanceError("maintenance.request_binding", retryable=False)
        if batch.source_watermark != snapshot.source_cursor:
            raise MaintenanceError("maintenance.watermark_mismatch", retryable=False)
        return batch.model_copy(
            update={
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": self.cost_usd,
            }
        )

    @staticmethod
    def _prompt(
        snapshot: MaintenanceSnapshot,
        sources: tuple[SourceRef, ...],
        texts: Sequence[str],
    ) -> str:
        evidence = [
            {"source": source.model_dump(mode="json"), "text": text}
            for source, text in zip(sources, texts, strict=True)
        ]
        return (
            "You are a bounded memory maintenance extractor. Return only a JSON "
            "MaintenanceExtraction object with request_id, integer source_watermark, "
            "and candidates. Each candidate contains content and zero-based "
            "source_indices. Do not emit owner, head, version, proposal ID, or "
            "publication state; Core and Governance create those identities. "
            "Cite only supplied canonical item sources and never claim user "
            "confirmation. No tools are available.\n"
            + _canonical(
                {
                    "request_id": snapshot.job_id,
                    "source_watermark": snapshot.source_cursor,
                    "action": snapshot.action,
                    "evidence": evidence,
                }
            )
        )


class CanonicalSourceProvider:
    """Read only authorized project ``UserMessage`` sources at a cutoff.

    ``GovernanceService.search_history`` already enforces workspace scope,
    source revocation and the no-Mailbox rule.  Filtering to ``user_message``
    here keeps Agent output, Tool output and the extractor's unbound Thread out
    of the next maintenance watermark.
    """

    def __init__(self, governance: GovernanceService) -> None:
        self.governance = governance
        self.last_source_cursor: int | None = None

    def list_sources(self, snapshot: MaintenanceSnapshot, *, limit: int) -> tuple[SourceRef, ...]:
        if limit < 1:
            raise ValueError("source limit must be positive")
        selected: list[SourceRef] = []
        after_cursor = snapshot.expected_processed_cursor
        last_scanned_cursor = snapshot.expected_processed_cursor
        cutoff_cursor = snapshot.source_cursor
        while len(selected) < limit:
            page = self.governance.search_history(
                snapshot.project_id,
                cutoff_cursor=cutoff_cursor,
                after_cursor=after_cursor,
                # The source cap applies to user messages, while this page
                # size lets us pass over Agent/Tool/System items safely.
                limit=100,
            )
            cutoff_cursor = page.cutoff_cursor
            for entry in page.items:
                last_scanned_cursor = max(last_scanned_cursor, entry.cursor)
                source = entry.source
                if entry.kind != "user_message" or source is None:
                    continue
                if source.source_type != "item" or source.scope != snapshot.scope:
                    continue
                if source.availability != "available":
                    continue
                try:
                    thread = self.governance.store.get_thread(entry.thread_id)
                except Exception:
                    continue
                if thread.id.startswith("thread_maintenance_") or any(
                    ref.source_type is LegacySourceType.SESSION
                    and ref.source_id.startswith("maintenance:")
                    for ref in thread.legacy_refs
                ):
                    continue
                selected.append(source)
                if len(selected) >= limit:
                    break
            next_cursor = page.next_cursor
            if next_cursor is None or next_cursor <= after_cursor:
                break
            after_cursor = next_cursor
        # If the cap selected real user messages, only advance to the last
        # selected Item cursor.  Otherwise advance across the scanned cutoff
        # so non-user canonical items do not cause an endless re-scan.
        self.last_source_cursor = (
            max(source.revision for source in selected) if selected else cutoff_cursor
        )
        return tuple(selected)


class AtomicMaintenanceCommitter(Protocol):
    """Governance/Ledger adapter for the one-transaction commit boundary."""

    def get_result(self, snapshot: MaintenanceSnapshot) -> MaintenanceResult | None: ...

    def mark_running(self, snapshot: MaintenanceSnapshot, *, attempt: int) -> None: ...

    def commit_batch(
        self,
        snapshot: MaintenanceSnapshot,
        extraction: MaintenanceExtraction,
        sources: tuple[SourceRef, ...],
        *,
        processed_cursor: int,
        attempt: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float | None = None,
    ) -> MaintenanceResult: ...

    def mark_failure(
        self,
        snapshot: MaintenanceSnapshot,
        *,
        state: MaintenanceState,
        error_code: str,
        attempt: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float | None = None,
    ) -> MaintenanceResult: ...


class SQLiteMaintenanceCommitter:
    """Metadata transaction adapter used by Core and isolated tests.

    ``propose_in_transaction`` is supplied by the governance/ledger owner. It
    receives the already-open SQLite connection, the frozen snapshot, the
    model's ``PreparedCandidate`` DTOs, and the exact source refs. Governance
    creates version/proposal identities and metadata there, so proposal rows
    and the watermark cannot commit separately. It must return the exact
    proposal IDs it inserted or replayed and must never publish a head.
    """

    def __init__(
        self,
        database_path: str | Path,
        *,
        propose_in_transaction: Callable[
            [
                sqlite3.Connection,
                MaintenanceSnapshot,
                tuple[PreparedCandidate, ...],
                tuple[SourceRef, ...],
            ],
            tuple[str, ...],
        ],
    ) -> None:
        self.database_path = str(database_path)
        self.propose_in_transaction = propose_in_transaction

    def initialize(self) -> None:
        with sqlite3.connect(self.database_path) as connection:
            connection.executescript(SCHEMA_SQL)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @staticmethod
    def _result(row: sqlite3.Row) -> MaintenanceResult:
        return MaintenanceResult.model_validate_json(row["result_json"])

    def get_result(self, snapshot: MaintenanceSnapshot) -> MaintenanceResult | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT request_digest, result_json FROM b25_maintenance_idempotency "
                "WHERE dataset_id=? AND idempotency_key=?",
                (snapshot.dataset_id, snapshot.stable_idempotency_key),
            ).fetchone()
        if row is None:
            return None
        if str(row["request_digest"]) != snapshot.request_digest:
            raise MaintenanceError("idempotency_conflict", retryable=False)
        return MaintenanceResult.model_validate_json(row["result_json"])

    def get_snapshot(self, job_id: str) -> MaintenanceSnapshot | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT snapshot_json FROM b25_maintenance_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        return (
            None if row is None else MaintenanceSnapshot.model_validate_json(row["snapshot_json"])
        )

    def get_snapshot_by_idempotency(
        self, dataset_id: str, idempotency_key: str
    ) -> MaintenanceSnapshot | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT snapshot_json FROM b25_maintenance_jobs "
                "WHERE dataset_id=? AND idempotency_key=?",
                (dataset_id, idempotency_key),
            ).fetchone()
        return (
            None if row is None else MaintenanceSnapshot.model_validate_json(row["snapshot_json"])
        )

    def get_watermark(self, dataset_id: str, source_stream: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT processed_cursor FROM b25_maintenance_watermarks "
                "WHERE dataset_id=? AND source_stream=?",
                (dataset_id, source_stream),
            ).fetchone()
        return 0 if row is None else int(row["processed_cursor"])

    def get_scheduler_ids(self, job_id: str) -> tuple[str | None, str | None]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT run_request_id, graph_run_id FROM b25_maintenance_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
        if row is None:
            return None, None
        return (
            None if row["run_request_id"] is None else str(row["run_request_id"]),
            None if row["graph_run_id"] is None else str(row["graph_run_id"]),
        )

    def bind_scheduler_ids(
        self,
        job_id: str,
        *,
        run_request_id: str | None = None,
        graph_run_id: str | None = None,
    ) -> None:
        """Attach durable Scheduler identifiers after dispatch succeeds."""

        assignments: list[str] = []
        values: list[str] = []
        if run_request_id is not None:
            assignments.append("run_request_id=?")
            values.append(run_request_id)
        if graph_run_id is not None:
            assignments.append("graph_run_id=?")
            values.append(graph_run_id)
        if not assignments:
            return
        values.append(job_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE b25_maintenance_jobs SET "
                + ",".join(assignments)
                + ",updated_at=? WHERE job_id=?",
                (*values[:-1], utc_now().isoformat(), values[-1]),
            )
            connection.commit()

    def list_jobs(self, project_id: str) -> list[MaintenanceJobView]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM b25_maintenance_jobs WHERE project_id=? ORDER BY updated_at DESC",
                (project_id,),
            ).fetchall()
        result: list[MaintenanceJobView] = []
        for row in rows:
            proposal_ids = json.loads(str(row["proposal_ids_json"]))
            result.append(
                MaintenanceJobView(
                    job_id=str(row["job_id"]),
                    project_id=str(row["project_id"]),
                    workflow_id=str(row["workflow_id"]),
                    workflow_version=int(row["workflow_version"]),
                    graph_run_id=None if row["graph_run_id"] is None else str(row["graph_run_id"]),
                    run_request_id=None
                    if row["run_request_id"] is None
                    else str(row["run_request_id"]),
                    state=str(row["state"]),
                    source_cursor=int(row["source_cursor"]),
                    processed_cursor=int(row["processed_cursor"]),
                    model_profile_id=str(row["model_profile_id"]),
                    model_id=str(row["model_id"]),
                    attempts=int(row["attempt_count"]),
                    max_attempts=int(row["max_attempts"]),
                    input_tokens=int(row["input_tokens"]),
                    output_tokens=int(row["output_tokens"]),
                    error_code=None if row["error_code"] is None else str(row["error_code"]),
                    proposal_ids=[str(item) for item in proposal_ids],
                    updated_at=datetime.fromisoformat(str(row["updated_at"])),
                )
            )
        return result

    def mark_running(self, snapshot: MaintenanceSnapshot, *, attempt: int) -> None:
        now = utc_now().isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._upsert_job(
                connection,
                snapshot,
                state=MaintenanceState.RUNNING,
                attempt=attempt,
                processed_cursor=snapshot.expected_processed_cursor,
                proposal_ids=(),
                error_code=None,
                input_tokens=0,
                output_tokens=0,
                now=now,
            )
            connection.commit()

    def commit_batch(
        self,
        snapshot: MaintenanceSnapshot,
        extraction: MaintenanceExtraction,
        sources: tuple[SourceRef, ...],
        *,
        processed_cursor: int,
        attempt: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float | None = None,
    ) -> MaintenanceResult:
        del cost_usd
        if processed_cursor != snapshot.source_cursor:
            raise MaintenanceError("source_cursor_mismatch", retryable=False)
        now = utc_now().isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cached = self._cached_in_transaction(connection, snapshot)
            if cached is not None:
                connection.commit()
                return cached
            current = connection.execute(
                "SELECT processed_cursor FROM b25_maintenance_watermarks "
                "WHERE dataset_id=? AND source_stream=?",
                (snapshot.dataset_id, snapshot.source_stream),
            ).fetchone()
            current_cursor = (
                snapshot.expected_processed_cursor if current is None else int(current[0])
            )
            if current_cursor != snapshot.expected_processed_cursor:
                connection.rollback()
                raise MaintenanceError("source_watermark_conflict", retryable=True)
            proposal_ids = self.propose_in_transaction(
                connection, snapshot, tuple(extraction.candidates), sources
            )
            state = MaintenanceState.SUCCEEDED if proposal_ids else MaintenanceState.NO_CHANGE
            result = MaintenanceResult(
                job_id=snapshot.job_id,
                idempotency_key=snapshot.stable_idempotency_key,
                state=state,
                source_cursor=snapshot.source_cursor,
                processed_cursor=processed_cursor,
                proposal_ids=tuple(proposal_ids),
                input_tokens=extraction.input_tokens,
                output_tokens=extraction.output_tokens,
                cost_usd=extraction.cost_usd,
                updated_at=utc_now(),
            )
            connection.execute(
                "INSERT INTO b25_maintenance_watermarks "
                "(dataset_id,source_stream,processed_cursor,last_job_id,"
                "snapshot_digest,updated_at) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(dataset_id,source_stream) DO UPDATE SET "
                "processed_cursor=excluded.processed_cursor,last_job_id=excluded.last_job_id, "
                "snapshot_digest=excluded.snapshot_digest,updated_at=excluded.updated_at",
                (
                    snapshot.dataset_id,
                    snapshot.source_stream,
                    processed_cursor,
                    snapshot.job_id,
                    snapshot.snapshot_digest,
                    now,
                ),
            )
            self._upsert_job(
                connection,
                snapshot,
                state=state,
                attempt=attempt,
                processed_cursor=processed_cursor,
                proposal_ids=tuple(proposal_ids),
                error_code=None,
                input_tokens=extraction.input_tokens,
                output_tokens=extraction.output_tokens,
                now=now,
            )
            connection.execute(
                "INSERT INTO b25_maintenance_idempotency "
                "(dataset_id,idempotency_key,request_digest,state,result_json,"
                "created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    snapshot.dataset_id,
                    snapshot.stable_idempotency_key,
                    snapshot.request_digest,
                    state.value,
                    result.model_dump_json(),
                    now,
                    now,
                ),
            )
            connection.commit()
            return result

    def register_queued(
        self,
        snapshot: MaintenanceSnapshot,
        *,
        run_request_id: str | None = None,
        graph_run_id: str | None = None,
    ) -> None:
        """Persist the queue projection after Scheduler accepted a request.

        The Scheduler remains the source of truth for leases and request state.
        This projection is deliberately best-effort metadata, but it is written
        before the controller returns so status queries never claim that a
        request is running when it was only registered in memory.
        """

        now = utc_now().isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._upsert_job(
                connection,
                snapshot,
                state=MaintenanceState.QUEUED,
                attempt=0,
                processed_cursor=snapshot.expected_processed_cursor,
                proposal_ids=(),
                error_code=None,
                input_tokens=0,
                output_tokens=0,
                run_request_id=run_request_id,
                graph_run_id=graph_run_id,
                now=now,
            )
            connection.commit()

    def mark_failure(
        self,
        snapshot: MaintenanceSnapshot,
        *,
        state: MaintenanceState,
        error_code: str,
        attempt: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float | None = None,
    ) -> MaintenanceResult:
        if state not in {
            MaintenanceState.RETRY_WAIT,
            MaintenanceState.DEAD_LETTER,
            MaintenanceState.MANUAL_RECONCILE_REQUIRED,
            MaintenanceState.CANCELLED,
            MaintenanceState.BLOCKED,
        }:
            raise ValueError("invalid maintenance failure state")
        result = MaintenanceResult(
            job_id=snapshot.job_id,
            idempotency_key=snapshot.stable_idempotency_key,
            state=state,
            source_cursor=snapshot.source_cursor,
            processed_cursor=snapshot.expected_processed_cursor,
            error_code=error_code,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
        )
        now = result.updated_at.isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._upsert_job(
                connection,
                snapshot,
                state=state,
                attempt=attempt,
                processed_cursor=snapshot.expected_processed_cursor,
                proposal_ids=(),
                error_code=error_code,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                now=now,
            )
            connection.commit()
        return result

    def _cached_in_transaction(
        self, connection: sqlite3.Connection, snapshot: MaintenanceSnapshot
    ) -> MaintenanceResult | None:
        row = connection.execute(
            "SELECT request_digest,result_json FROM b25_maintenance_idempotency "
            "WHERE dataset_id=? AND idempotency_key=?",
            (snapshot.dataset_id, snapshot.stable_idempotency_key),
        ).fetchone()
        if row is None:
            return None
        if str(row["request_digest"]) != snapshot.request_digest:
            raise MaintenanceError("idempotency_conflict", retryable=False)
        result = MaintenanceResult.model_validate_json(row["result_json"])
        return (
            result
            if result.state in {MaintenanceState.SUCCEEDED, MaintenanceState.NO_CHANGE}
            else None
        )

    @staticmethod
    def _upsert_job(
        connection: sqlite3.Connection,
        snapshot: MaintenanceSnapshot,
        *,
        state: MaintenanceState,
        attempt: int,
        processed_cursor: int,
        proposal_ids: tuple[str, ...],
        error_code: str | None,
        input_tokens: int,
        output_tokens: int,
        run_request_id: str | None = None,
        graph_run_id: str | None = None,
        now: str,
    ) -> None:
        connection.execute(
            "INSERT INTO b25_maintenance_jobs "
            "(job_id,idempotency_key,request_digest,project_id,dataset_id,installation_id,binding_id,"
            "source_stream,source_cursor,processed_cursor,source_digest,workflow_id,workflow_version,"
            "graph_run_id,run_request_id,"
            "plugin_id,plugin_version,package_digest,config_id,config_revision,config_digest,"
            "model_profile_id,model_id,model_digest,prompt_version,budget_tokens,max_sources,max_attempts,"
            "state,attempt_count,input_tokens,output_tokens,error_code,proposal_ids_json,snapshot_json,"
            "created_at,updated_at) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(job_id) DO UPDATE SET "
            "state=excluded.state,processed_cursor=excluded.processed_cursor,"
            "attempt_count=MAX(b25_maintenance_jobs.attempt_count,excluded.attempt_count),"
            "input_tokens=b25_maintenance_jobs.input_tokens+excluded.input_tokens,"
            "output_tokens=b25_maintenance_jobs.output_tokens+excluded.output_tokens,"
            "run_request_id=COALESCE(excluded.run_request_id,b25_maintenance_jobs.run_request_id),"
            "graph_run_id=CASE WHEN excluded.run_request_id IS NOT NULL THEN excluded.graph_run_id "
            "ELSE b25_maintenance_jobs.graph_run_id END,"
            "error_code=excluded.error_code,proposal_ids_json=excluded.proposal_ids_json,updated_at=excluded.updated_at",
            (
                snapshot.job_id,
                snapshot.stable_idempotency_key,
                snapshot.request_digest,
                snapshot.project_id,
                snapshot.dataset_id,
                snapshot.installation_id,
                snapshot.binding_id,
                snapshot.source_stream,
                snapshot.source_cursor,
                processed_cursor,
                snapshot.source_digest,
                snapshot.workflow_id,
                snapshot.workflow_version,
                graph_run_id,
                run_request_id,
                snapshot.plugin_id,
                snapshot.plugin_version,
                snapshot.package_digest,
                snapshot.config_id,
                snapshot.config_revision,
                snapshot.config_digest,
                snapshot.model_profile_id,
                snapshot.model_id,
                snapshot.model_digest,
                snapshot.prompt_version,
                snapshot.budget.max_output_tokens,
                snapshot.budget.max_sources,
                snapshot.budget.max_attempts,
                state.value,
                attempt,
                input_tokens,
                output_tokens,
                error_code,
                json.dumps(list(proposal_ids), separators=(",", ":")),
                snapshot.model_dump_json(),
                now,
                now,
            ),
        )


class MaintenanceExecutor:
    """Run one frozen maintenance job with foreground-aware admission."""

    def __init__(
        self,
        *,
        source_provider: MaintenanceSourceProvider,
        extractor: MaintenanceExtractor,
        committer: AtomicMaintenanceCommitter,
        foreground_active: Callable[[], bool] | None = None,
        max_concurrency: int = 1,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("maintenance max_concurrency must be positive")
        self.source_provider = source_provider
        self.extractor = extractor
        self.committer = committer
        self.foreground_active = foreground_active or (lambda: False)
        self._slots = asyncio.Semaphore(max_concurrency)
        self._cancel_events: dict[str, asyncio.Event] = {}

    async def execute(
        self,
        snapshot: MaintenanceSnapshot,
        *,
        attempt: int = 1,
        cancel_event: asyncio.Event | None = None,
    ) -> MaintenanceResult:
        cached = self.committer.get_result(snapshot)
        if cached is not None and cached.state in {
            MaintenanceState.SUCCEEDED,
            MaintenanceState.NO_CHANGE,
        }:
            return cached
        if attempt > snapshot.budget.max_attempts:
            return self.committer.mark_failure(
                snapshot,
                state=MaintenanceState.DEAD_LETTER,
                error_code="maintenance.max_attempts",
                attempt=attempt,
            )
        event = cancel_event or asyncio.Event()
        self._cancel_events[snapshot.job_id] = event
        async with self._slots:
            try:
                if self.foreground_active():
                    raise MaintenanceError("foreground_priority", retryable=True)
                self._check_cancelled(event)
                self.committer.mark_running(snapshot, attempt=attempt)
                sources = self.source_provider.list_sources(
                    snapshot, limit=snapshot.budget.max_sources
                )
                if len(sources) > snapshot.budget.max_sources:
                    raise MaintenanceError("maintenance.source_limit", retryable=False)
                self._validate_sources(snapshot, sources)
                if (
                    _digest([source.model_dump(mode="json") for source in sources])
                    != snapshot.source_digest
                ):
                    raise MaintenanceError("maintenance.source_snapshot_stale", retryable=True)
                if sources:
                    batch = await asyncio.wait_for(
                        self.extractor.extract(snapshot, sources, cancel_event=event),
                        timeout=snapshot.budget.timeout_seconds,
                    )
                else:
                    batch = MaintenanceExtraction(
                        request_id=snapshot.job_id,
                        source_watermark=snapshot.source_cursor,
                        candidates=(),
                    )
                self._check_cancelled(event)
                self._validate_batch(snapshot, sources, batch)
                return self.committer.commit_batch(
                    snapshot,
                    batch,
                    sources,
                    processed_cursor=snapshot.source_cursor,
                    attempt=attempt,
                )
            except asyncio.CancelledError:
                raise
            except MaintenanceError as exc:
                state = (
                    MaintenanceState.CANCELLED
                    if exc.code == "maintenance.cancelled"
                    else MaintenanceState.RETRY_WAIT
                    if exc.retryable and attempt < snapshot.budget.max_attempts
                    else MaintenanceState.MANUAL_RECONCILE_REQUIRED
                    if exc.outcome_unknown
                    else MaintenanceState.DEAD_LETTER
                )
                return self.committer.mark_failure(
                    snapshot,
                    state=state,
                    error_code=exc.code,
                    attempt=attempt,
                    input_tokens=int(getattr(self.extractor, "input_tokens", 0)),
                    output_tokens=int(getattr(self.extractor, "output_tokens", 0)),
                    cost_usd=getattr(self.extractor, "cost_usd", None),
                )
            except Exception as exc:
                code = f"maintenance.{type(exc).__name__}"
                state = (
                    MaintenanceState.RETRY_WAIT
                    if attempt < snapshot.budget.max_attempts
                    else MaintenanceState.DEAD_LETTER
                )
                return self.committer.mark_failure(
                    snapshot,
                    state=state,
                    error_code=code,
                    attempt=attempt,
                    input_tokens=int(getattr(self.extractor, "input_tokens", 0)),
                    output_tokens=int(getattr(self.extractor, "output_tokens", 0)),
                    cost_usd=getattr(self.extractor, "cost_usd", None),
                )
            finally:
                if self._cancel_events.get(snapshot.job_id) is event:
                    self._cancel_events.pop(snapshot.job_id, None)

    def cancel(self, job_id: str) -> bool:
        event = self._cancel_events.get(job_id)
        if event is None:
            return False
        event.set()
        return True

    @staticmethod
    def _check_cancelled(event: asyncio.Event) -> None:
        if event.is_set():
            raise MaintenanceError("maintenance.cancelled", retryable=False)

    @staticmethod
    def _validate_sources(snapshot: MaintenanceSnapshot, sources: tuple[SourceRef, ...]) -> None:
        seen: set[tuple[str, str, int, str]] = set()
        for source in sources:
            if source.source_type != "item":
                raise MaintenanceError("maintenance.source_type", retryable=False)
            if source.scope != snapshot.scope:
                raise MaintenanceError("maintenance.source_scope", retryable=False)
            if source.availability != "available":
                raise MaintenanceError("maintenance.source_unavailable", retryable=True)
            key = (source.source_type, source.source_id, source.revision, source.content_digest)
            if key in seen:
                raise MaintenanceError("maintenance.duplicate_source", retryable=False)
            seen.add(key)

    @staticmethod
    def _validate_batch(
        snapshot: MaintenanceSnapshot,
        sources: tuple[SourceRef, ...],
        batch: MaintenanceExtraction,
    ) -> None:
        if batch.request_id != snapshot.job_id:
            raise MaintenanceError("maintenance.request_binding", retryable=False)
        if batch.source_watermark != snapshot.source_cursor:
            raise MaintenanceError("maintenance.watermark_mismatch", retryable=False)
        for candidate in batch.candidates:
            if any(index < 0 or index >= len(sources) for index in candidate.source_indices):
                raise MaintenanceError("maintenance.source_claim", retryable=False)


@dataclass(frozen=True)
class MaintenanceRun:
    run_id: str
    snapshot: MaintenanceSnapshot
    task: asyncio.Task[MaintenanceResult]


class MaintenanceCoordinator:
    """Bounded async coordinator used by the Scheduler maintenance gateway."""

    def __init__(self, executor: _ControllerExecutor) -> None:
        self.executor = executor
        self._runs: dict[str, MaintenanceRun] = {}
        self._results: dict[str, MaintenanceResult] = {}
        self._attempts: dict[str, int] = {}

    def submit(self, snapshot: MaintenanceSnapshot, *, attempt: int = 1) -> str:
        key = snapshot.stable_idempotency_key
        current = self._runs.get(key)
        if current is not None:
            return current.run_id
        existing = self._results.get(key)
        if existing is not None:
            if existing.state in {MaintenanceState.SUCCEEDED, MaintenanceState.NO_CHANGE}:
                return existing.job_id
            if existing.state not in {
                MaintenanceState.RETRY_WAIT,
                MaintenanceState.DEAD_LETTER,
            }:
                return existing.job_id
            # Scheduler replay creates a new RunRequest while retaining the
            # frozen maintenance snapshot.  Advance the local attempt only at
            # that explicit replay boundary; duplicate dispatches remain
            # idempotent while the attempt is still active.
            attempt = max(attempt, self._attempts.get(key, 1) + 1)
            self._results.pop(key, None)
        task = asyncio.create_task(self._execute(key, snapshot, attempt), name=key)
        self._runs[key] = MaintenanceRun(key, snapshot, task)
        self._attempts[key] = attempt
        return key

    async def _execute(
        self, key: str, snapshot: MaintenanceSnapshot, attempt: int
    ) -> MaintenanceResult:
        try:
            result = await self.executor.execute(snapshot, attempt=attempt)
            self._results[key] = result
            return result
        except BaseException as exc:
            controller = self.executor.controller
            result = controller._projection_committer().mark_failure(
                snapshot,
                state=MaintenanceState.CANCELLED
                if isinstance(exc, asyncio.CancelledError)
                else MaintenanceState.DEAD_LETTER,
                error_code="maintenance.cancelled"
                if isinstance(exc, asyncio.CancelledError)
                else "maintenance.executor_failed",
                attempt=attempt,
            )
            self._results[key] = result
            return result
        finally:
            self._runs.pop(key, None)

    def cancel(self, job_id: str) -> bool:
        cancelled = False
        for run in tuple(self._runs.values()):
            if job_id in {run.run_id, run.snapshot.job_id, run.snapshot.stable_idempotency_key}:
                self.executor.cancel(run.snapshot.job_id)
                run.task.cancel()
                cancelled = True
        return cancelled

    def status(self, run_id: str) -> GraphRunStatus:
        result = self._results.get(run_id)
        if result is not None:
            if result.state is MaintenanceState.CANCELLED:
                return GraphRunStatus.CANCELLED
            if result.state in {
                MaintenanceState.DEAD_LETTER,
                MaintenanceState.MANUAL_RECONCILE_REQUIRED,
                MaintenanceState.BLOCKED,
                MaintenanceState.RETRY_WAIT,
            }:
                return GraphRunStatus.FAILED
            return GraphRunStatus.COMPLETED
        return GraphRunStatus.RUNNING

    def result(self, run_id: str) -> MaintenanceResult | None:
        """Return the completed result for a Scheduler status projection."""

        current = self._runs.get(run_id)
        if current is not None and current.task.done():
            with suppress(asyncio.CancelledError, Exception):
                return current.task.result()
        return self._results.get(run_id)

    async def wait(self, run_id: str) -> MaintenanceResult | None:
        current = self._runs.get(run_id)
        if current is not None:
            return await current.task
        return self._results.get(run_id)

    async def close(self) -> None:
        tasks = tuple(run.task for run in self._runs.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._runs.clear()


class MaintenanceRunGateway:
    """Small Scheduler gateway for already validated maintenance workflows.

    The caller supplies ``decode_snapshot`` so Graph/API composition remains
    Core-owned. Ordinary Graph dispatch never uses this class.
    """

    def __init__(
        self,
        coordinator: MaintenanceCoordinator,
        *,
        workflow_id: str,
        workflow_version: int,
        decode_snapshot: Callable[[dict[str, JsonValue]], MaintenanceSnapshot],
        on_dispatch: Callable[[MaintenanceSnapshot, str], None] | None = None,
    ) -> None:
        self.coordinator = coordinator
        self.workflow_id = workflow_id
        self.workflow_version = workflow_version
        self.decode_snapshot = decode_snapshot
        self.on_dispatch = on_dispatch
        self._graph_run_for_local: dict[str, str] = {}
        self._local_run_for_graph: dict[str, str] = {}

    def dispatch_workflow(self, action: Any, *, graph_run_id: str | None = None) -> str:
        try:
            snapshot_payload = action.workflow_input.get("maintenance")
            if not isinstance(snapshot_payload, dict):
                raise ValueError("maintenance snapshot is missing")
            snapshot = self.decode_snapshot(cast(dict[str, JsonValue], snapshot_payload))
        except Exception as exc:
            if isinstance(exc, MaintenanceError):
                raise
            raise MaintenanceError("maintenance.snapshot_invalid", retryable=False) from exc
        if (
            action.workflow_id != snapshot.workflow_id
            or action.workflow_version != snapshot.workflow_version
        ):
            raise MaintenanceError("maintenance.workflow_binding", retryable=False)
        # A replayed dead-letter request has a fresh Scheduler idempotency key,
        # but its workflow input still carries the exact frozen maintenance
        # snapshot.  The original key must remain the snapshot identity.
        expected_schedule_id = "maintenance_schedule_" + snapshot.job_id.removeprefix(
            "maintenance_"
        )
        allowed_scheduler_key = f"manual:{expected_schedule_id}:{snapshot.stable_idempotency_key}"
        if action.idempotency_key not in {
            snapshot.stable_idempotency_key,
            allowed_scheduler_key,
        } and not action.idempotency_key.startswith("replay:"):
            raise MaintenanceError("maintenance.idempotency_binding", retryable=False)
        run_id = self.coordinator.submit(snapshot)
        if graph_run_id is not None:
            self._graph_run_for_local[run_id] = graph_run_id
            self._local_run_for_graph[graph_run_id] = run_id
        if self.on_dispatch is not None:
            self.on_dispatch(snapshot, graph_run_id or run_id)
        return graph_run_id or run_id

    def workflow_status(self, workflow_run_id: str) -> GraphRunStatus:
        local_run_id = self._local_run_for_graph.get(workflow_run_id, workflow_run_id)
        return self.coordinator.status(local_run_id)

    def is_known_run(self, workflow_run_id: str) -> bool:
        return (
            workflow_run_id in self._local_run_for_graph
            or workflow_run_id in self.coordinator._runs
            or workflow_run_id in self.coordinator._results
        )

    def cancel_workflow(self, workflow_run_id: str) -> bool:
        local_run_id = self._local_run_for_graph.get(workflow_run_id, workflow_run_id)
        return self.coordinator.cancel(local_run_id)


class MaintenanceAwareSchedulerGateway:
    """Keep authorization, identity and terminal state in the actual Graph gateway."""

    def __init__(self, graph_gateway: Any, maintenance_gateway: MaintenanceRunGateway) -> None:
        self.graph_gateway = graph_gateway
        self.maintenance_gateway = maintenance_gateway

    def dispatch_workflow(self, action: Any) -> str:
        controller = self.maintenance_gateway.coordinator.executor.controller
        with controller.service.store._connect() as connection:
            registered = connection.execute(
                "SELECT snapshot_json FROM b25_maintenance_jobs WHERE run_request_id=?",
                (action.source_run_request_id,),
            ).fetchone()
        if registered is None:
            # The published definition survives a crash between Scheduler enqueue
            # and job registration. Its explicit execution marker must never fall
            # through to an ordinary Graph Agent without the maintenance guards.
            try:
                definition = self.graph_gateway.graph_repository.get_definition(
                    action.workflow_id, action.workflow_version
                )
            except KeyError:
                return cast(str, self.graph_gateway.dispatch_workflow(action))
            if any(node.metadata.get("maintenance") is True for node in definition.nodes):
                from operant.runtime.scheduler import DispatchError

                raise DispatchError("maintenance.registration_missing", outcome_unknown=True)
            return cast(str, self.graph_gateway.dispatch_workflow(action))
        registered_snapshot = MaintenanceSnapshot.model_validate_json(registered["snapshot_json"])
        if (
            action.workflow_id != registered_snapshot.workflow_id
            or action.workflow_version != registered_snapshot.workflow_version
            or _canonical(action.workflow_input)
            != _canonical({"maintenance": registered_snapshot.model_dump(mode="json")})
        ):
            from operant.runtime.scheduler import DispatchError

            raise DispatchError("maintenance.registered_request_mismatch", outcome_unknown=False)
        from operant.runtime.scheduler import DispatchError
        from operant.runtime.scheduler_integration import GraphSchedulerActionGateway

        snapshot = MaintenanceSnapshot.model_validate(action.workflow_input.get("maintenance"))
        controller = self.maintenance_gateway.coordinator.executor.controller
        if controller._foreground_active():
            raise DispatchError("maintenance.foreground_priority", outcome_unknown=False)
        if not snapshot.workspace or not Path(snapshot.workspace).is_absolute():
            raise DispatchError("maintenance.workspace_missing", outcome_unknown=False)
        gateway = GraphSchedulerActionGateway(
            graph_repository=self.graph_gateway.graph_repository,
            graph_runtime=self.graph_gateway.graph_runtime,
            security=self.graph_gateway.security,
            dispatch_registry=self.graph_gateway.dispatch_registry,
            workspace_resolver=lambda _action: snapshot.workspace,
        )
        run_id = gateway.dispatch_workflow(action)
        run = gateway.graph_repository.get_run(run_id)
        if run.status in {
            GraphRunStatus.COMPLETED,
            GraphRunStatus.FAILED,
            GraphRunStatus.CANCELLED,
            GraphRunStatus.MANUAL_RECONCILE_REQUIRED,
        }:
            return run_id
        nodes = gateway.graph_repository.list_node_runs(run_id)
        if len(nodes) != 1:
            gateway.graph_runtime.fail_run(run_id)
            raise DispatchError("maintenance.restricted_graph", outcome_unknown=False)
        if nodes[0].active_attempt_id is None:
            gateway.graph_runtime.start_attempt(nodes[0].id, worker_id="memory-maintenance")
        return self.maintenance_gateway.dispatch_workflow(action, graph_run_id=run_id)

    def workflow_status(self, workflow_run_id: str) -> GraphRunStatus:
        repository = self.graph_gateway.graph_repository
        runtime = self.graph_gateway.graph_runtime
        run = repository.get_run(workflow_run_id)
        controller = self.maintenance_gateway.coordinator.executor.controller
        with controller.service.store._connect() as connection:
            registered = connection.execute(
                "SELECT snapshot_json FROM b25_maintenance_jobs WHERE graph_run_id=?",
                (workflow_run_id,),
            ).fetchone()
        if registered is None:
            return cast(GraphRunStatus, run.status)
        registered_snapshot = MaintenanceSnapshot.model_validate_json(registered["snapshot_json"])
        if (
            run.workflow_definition_id != registered_snapshot.workflow_id
            or run.workflow_definition_version != registered_snapshot.workflow_version
            or _canonical(run.input)
            != _canonical({"maintenance": registered_snapshot.model_dump(mode="json")})
        ):
            return cast(GraphRunStatus, runtime.fail_run(workflow_run_id).status)
        if run.status in {
            GraphRunStatus.COMPLETED,
            GraphRunStatus.FAILED,
            GraphRunStatus.CANCELLED,
            GraphRunStatus.MANUAL_RECONCILE_REQUIRED,
        }:
            return cast(GraphRunStatus, run.status)
        local_id = self.maintenance_gateway._local_run_for_graph.get(workflow_run_id)
        controller = self.maintenance_gateway.coordinator.executor.controller
        if local_id is not None:
            result = self.maintenance_gateway.coordinator.result(local_id)
            if result is None:
                return cast(GraphRunStatus, run.status)
        else:
            snapshot = MaintenanceSnapshot.model_validate(run.input.get("maintenance"))
            committer = controller._projection_committer()
            result = committer.get_result(snapshot)
            if result is None or result.state not in {
                MaintenanceState.SUCCEEDED,
                MaintenanceState.NO_CHANGE,
            }:
                committer.mark_failure(
                    snapshot,
                    state=MaintenanceState.DEAD_LETTER,
                    error_code="maintenance.interrupted_before_commit",
                )
                return cast(GraphRunStatus, runtime.fail_run(workflow_run_id).status)
        if result.state == MaintenanceState.CANCELLED:
            return cast(GraphRunStatus, runtime.cancel_run(workflow_run_id).status)
        nodes = repository.list_node_runs(workflow_run_id)
        if (
            result.state in {MaintenanceState.SUCCEEDED, MaintenanceState.NO_CHANGE}
            and len(nodes) == 1
        ):
            attempts = repository.list_attempts(nodes[0].id)
            active = next((a for a in attempts if a.id == nodes[0].active_attempt_id), None)
            if active is None and nodes[0].status.value == "ready":
                active = runtime.start_attempt(nodes[0].id, worker_id="memory-maintenance")
            if active is not None:
                runtime.complete_attempt(active, succeeded=True, output_refs={})
            return cast(GraphRunStatus, repository.get_run(workflow_run_id).status)
        runtime.fail_run(workflow_run_id)
        return cast(GraphRunStatus, repository.get_run(workflow_run_id).status)


def _state_value(app: Any, name: str, default: Any = None) -> Any:
    state = getattr(app, "state", app)
    return getattr(state, name, default)


def _job_id_for(dataset_id: str, idempotency_key: str) -> str:
    return (
        "maintenance_"
        + hashlib.sha256(f"{dataset_id}\0{idempotency_key}".encode()).hexdigest()[:48]
    )


class _ControllerExecutor:
    """Hold a real Host lease across extraction and guarded atomic commit."""

    def __init__(self, controller: MaintenanceController) -> None:
        self.controller = controller
        self._active: dict[str, MaintenanceExecutor] = {}
        self._slots = asyncio.Semaphore(1)

    async def execute(
        self,
        snapshot: MaintenanceSnapshot,
        *,
        attempt: int = 1,
        cancel_event: asyncio.Event | None = None,
    ) -> MaintenanceResult:
        async with self._slots:
            return await self._execute(snapshot, attempt=attempt, cancel_event=cancel_event)

    async def _execute(
        self, snapshot: MaintenanceSnapshot, *, attempt: int, cancel_event: asyncio.Event | None
    ) -> MaintenanceResult:
        governance = self.controller._governance(snapshot.project_id)
        manager = governance.manager
        manager._maintenance_cancel_project = self.controller.cancel_project
        projection = self.controller._projection_committer()
        jobs = projection.list_jobs(snapshot.project_id)
        old_attempts = next((j.attempts for j in jobs if j.job_id == snapshot.job_id), 0)
        attempt = max(attempt, old_attempts + 1)
        installation = manager._installation(manager._project(snapshot.project_id))

        def current_guard() -> None:
            current = manager._installation(manager._project(snapshot.project_id))
            binding = manager.registry.get_binding(snapshot.binding_id)
            profile = self.controller.service.get_model_profile(snapshot.model_profile_id)
            if (
                current.installation_id != snapshot.installation_id
                or binding.binding_epoch != snapshot.binding_epoch
                or binding.permission_epoch != snapshot.permission_epoch
                or not binding.config.maintenance_enabled
                or _digest(binding.config.model_dump(mode="json")) != snapshot.config_digest
                or _digest(profile.model_dump(mode="json")) != snapshot.model_digest
                or not profile.enabled
                or current.manifest.package_digest != snapshot.package_digest
            ):
                raise MaintenanceError("maintenance.snapshot_stale", retryable=False)

        current_guard()
        await manager.host.start(
            installation.installation_id,
            mode=manager._state["modes"].get(installation.installation_id, "isolated"),
        )
        lease = manager.host.start_run(
            snapshot.binding_id,
            run_id="management_maintenance_" + snapshot.job_id,
            scope=snapshot.scope,
            ttl_seconds=snapshot.budget.timeout_seconds + 60,
        )
        context = manager._context(lease)
        context = context.model_copy(
            update={"deadline": utc_now() + timedelta(seconds=snapshot.budget.timeout_seconds + 30)}
        )
        base = self.controller._committer(governance, snapshot.project_id)

        def guarded_commit(
            connection: sqlite3.Connection,
            snap: MaintenanceSnapshot,
            candidates: tuple[PreparedCandidate, ...],
            sources: tuple[SourceRef, ...],
        ) -> tuple[str, ...]:
            current_guard()
            manager.registry.assert_lease(lease, context)
            manager.host._assert_active(lease, context)
            if cancel_event is not None and cancel_event.is_set():
                raise MaintenanceError("maintenance.cancelled", retryable=False)
            ids = base.propose_in_transaction(connection, snap, candidates, sources)
            if ids:
                connection.execute(
                    "INSERT INTO b25_events(dataset_id,project_id,action,affected_ids,occurred_at) "
                    "VALUES(?,?,?,?,?)",
                    (
                        snap.dataset_id,
                        snap.project_id,
                        "maintenance_candidates",
                        json.dumps(ids),
                        utc_now().isoformat(),
                    ),
                )
            return ids

        committer = SQLiteMaintenanceCommitter(
            manager.store.path, propose_in_transaction=guarded_commit
        )
        extractor = FiniteSessionExtractor(
            self.controller.service,
            role_id=snapshot.role_id or self.controller._role_id(snapshot.project_id),
            role_version=snapshot.role_version,
            workspace=snapshot.workspace or self.controller._workspace(snapshot.project_id),
            source_reader=lambda source: (
                governance.history_detail(
                    snapshot.project_id, source.source_id, cutoff_cursor=snapshot.source_cursor
                ).text
            ),
        )
        executor = MaintenanceExecutor(
            source_provider=CanonicalSourceProvider(governance),
            extractor=extractor,
            committer=committer,
            foreground_active=self.controller._foreground_active,
        )
        self._active[snapshot.job_id] = executor
        try:
            return await executor.execute(snapshot, attempt=attempt, cancel_event=cancel_event)
        finally:
            self._active.pop(snapshot.job_id, None)
            manager.registry.release_run(lease.lease_id)

    def cancel(self, job_id: str) -> bool:
        executor = self._active.get(job_id)
        return executor.cancel(job_id) if executor is not None else False


class MaintenanceController:
    """Core composition for one-shot maintenance commands.

    The controller only registers Scheduler work.  It never runs model work in
    ``execute_command``; the existing Scheduler worker later invokes
    :class:`MaintenanceRunGateway` with the immutable snapshot.
    """

    def __init__(
        self,
        app: Any,
        service: ApplicationService,
        governance_factory: Callable[..., GovernanceService],
    ) -> None:
        self.app = app
        self.service = service
        self.governance_factory = governance_factory
        self._committers: dict[str, SQLiteMaintenanceCommitter] = {}
        self._governances: dict[str, GovernanceService] = {}
        self._executor = _ControllerExecutor(self)
        self.coordinator = MaintenanceCoordinator(cast(Any, self._executor))
        workflow_id, workflow_version = self._workflow_identity()
        self.gateway = MaintenanceRunGateway(
            self.coordinator,
            workflow_id=workflow_id,
            workflow_version=workflow_version,
            decode_snapshot=lambda payload: MaintenanceSnapshot.model_validate(payload),
            on_dispatch=self._on_dispatch,
        )

    @property
    def scheduler_gateway(self) -> MaintenanceRunGateway:
        return self.gateway

    def _governance(self, project_id: str) -> GovernanceService:
        current = self._governances.get(project_id)
        if current is not None:
            return current
        try:
            value = self.governance_factory(project_id)
        except TypeError as first:
            try:
                value = self.governance_factory()
            except TypeError as second:
                raise first from second
        if not hasattr(value, "search_history"):
            raise MaintenanceError("maintenance.governance_unavailable", retryable=True)
        governance = value
        self._governances[project_id] = governance
        return governance

    def _committer(
        self, governance: GovernanceService, project_id: str
    ) -> SQLiteMaintenanceCommitter:
        manager = getattr(governance, "manager", None)
        if manager is None:
            raise MaintenanceError("maintenance.manager_unavailable", retryable=False)
        project = manager._project(project_id)
        installation = manager._installation(project, enabled=True)
        dataset_id = str(installation.dataset_id)
        cached = self._committers.get(dataset_id)
        if cached is not None:
            return cached

        adapter = _state_value(self.app, "maintenance_propose_in_transaction")
        if adapter is not None:
            factory = adapter
            adapter = None
            with suppress(TypeError):
                adapter = factory(governance)
        if adapter is None:
            adapter = getattr(governance, "propose_maintenance_in_transaction", None)
        if adapter is None:
            adapter = getattr(governance, "commit_maintenance_in_transaction", None)
        if adapter is not None:
            governance_adapter = adapter

            def adapter(
                connection: sqlite3.Connection,
                snapshot: MaintenanceSnapshot,
                candidates: tuple[PreparedCandidate, ...],
                sources: tuple[SourceRef, ...],
            ) -> tuple[str, ...]:
                from operant.memory_plugins.governance import (
                    PreparedCandidate as GovernanceCandidate,
                )
                from operant.memory_plugins.governance import (
                    PreparedExtraction,
                )

                extraction = PreparedExtraction(
                    source_cursor=snapshot.source_cursor,
                    sources=sources,
                    candidates=tuple(
                        GovernanceCandidate(
                            content=candidate.content,
                            source_indexes=candidate.source_indices,
                        )
                        for candidate in candidates
                    ),
                )
                return tuple(governance_adapter(connection, snapshot, extraction))

        if adapter is None:

            def missing_adapter(
                _connection: sqlite3.Connection,
                _snapshot: MaintenanceSnapshot,
                _candidates: tuple[PreparedCandidate, ...],
                _sources: tuple[SourceRef, ...],
            ) -> tuple[str, ...]:
                raise MaintenanceError("maintenance.atomic_ledger_adapter_missing", retryable=False)

            adapter = missing_adapter

        database_path = getattr(self.service.store, "path", None)
        if database_path is None:
            raise MaintenanceError("maintenance.database_unavailable", retryable=False)
        committer = SQLiteMaintenanceCommitter(
            database_path,
            propose_in_transaction=cast(
                Callable[
                    [
                        sqlite3.Connection,
                        MaintenanceSnapshot,
                        tuple[PreparedCandidate, ...],
                        tuple[SourceRef, ...],
                    ],
                    tuple[str, ...],
                ],
                adapter,
            ),
        )
        self._committers[dataset_id] = committer
        return committer

    def _projection_committer(self) -> SQLiteMaintenanceCommitter:
        database_path = getattr(self.service.store, "path", None)
        if database_path is None:
            raise MaintenanceError("maintenance.database_unavailable", retryable=False)

        def unused_adapter(
            _connection: sqlite3.Connection,
            _snapshot: MaintenanceSnapshot,
            _candidates: tuple[PreparedCandidate, ...],
            _sources: tuple[SourceRef, ...],
        ) -> tuple[str, ...]:
            raise MaintenanceError("maintenance.atomic_ledger_adapter_missing", retryable=False)

        return SQLiteMaintenanceCommitter(database_path, propose_in_transaction=unused_adapter)

    def _workflow_identity(self) -> tuple[str, int]:
        definition = _state_value(self.app, "maintenance_workflow_definition")
        workflow_id = _state_value(self.app, "maintenance_workflow_id")
        workflow_version = _state_value(self.app, "maintenance_workflow_version")
        if definition is not None:
            workflow_id = workflow_id or getattr(definition, "workflow_id", None)
            workflow_version = workflow_version or getattr(definition, "version", None)
        if not workflow_id:
            workflow_id = "maintenance"
        if workflow_version is None:
            workflow_version = 1
        return str(workflow_id), int(workflow_version)

    def _workflow_definition(self) -> Any | None:
        definition = _state_value(self.app, "maintenance_workflow_definition")
        if definition is not None:
            return definition
        repository = _state_value(self.app, "maintenance_graph_repository") or _state_value(
            self.app, "graph_repository"
        )
        if repository is None:
            try:
                from operant.persistence.graph_team import SQLiteGraphRepository

                repository = SQLiteGraphRepository(self.service.store)
            except Exception:
                repository = None
        if repository is None:
            return None
        workflow_id, workflow_version = self._workflow_identity()
        try:
            return repository.get_definition(workflow_id, workflow_version)
        except KeyError:
            return None

    def _ensure_workflow(self, model_profile_id: str) -> WorkflowDefinition:
        from operant.application.graph import GraphCompiler
        from operant.persistence.graph_team import SQLiteGraphRepository

        profile = self.service.get_model_profile(model_profile_id)
        provider_digest = _digest(profile.model_dump(mode="json"))
        workflow_id = "maintenance_" + provider_digest[:32]
        repository = SQLiteGraphRepository(self.service.store)
        try:
            return repository.get_definition(workflow_id, 1)
        except KeyError:
            pass
        role = self.service.get_role(self._role_for_profile(model_profile_id, create=True))
        node = NodeSpec(
            node_id="maintenance_extract",
            node_kind=NodeKind.AGENT,
            agent_or_action_ref=role.id,
            model_override=model_profile_id,
            metadata={"maintenance": True, "role_id": role.id},
        )
        definition = WorkflowDefinition(
            workflow_id=workflow_id,
            version=1,
            name="Memory Maintenance",
            description="Bounded memory extraction with frozen model profile",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            nodes=(node,),
            locked_role_versions={role.id: role.version},
            locked_provider_versions={model_profile_id: provider_digest},
            status=WorkflowDefinitionStatus.PUBLISHED,
        )
        GraphCompiler().compile(definition)
        repository.put_definition(definition)
        return definition

    def _role_for_profile(self, model_profile_id: str, *, create: bool) -> str:
        candidates = sorted(
            (
                role
                for role in self.service.list_roles(include_inactive=False)
                if role.id
                == "role_maintenance_" + hashlib.sha256(model_profile_id.encode()).hexdigest()[:24]
                and role.model_profile_id == model_profile_id
                and role.status is RoleStatus.ACTIVE
                and role.tool_policy == ToolPolicy()
            ),
            key=lambda role: (role.id, role.version),
        )
        if candidates:
            return candidates[0].id
        if create:
            role = RolePreset(
                id="role_maintenance_" + hashlib.sha256(model_profile_id.encode()).hexdigest()[:24],
                name="Memory Maintenance Extractor",
                system_prompt=(
                    "Extract bounded memory candidates as strict JSON. Do not use tools, "
                    "publish memory, or claim user confirmation."
                ),
                model_profile_id=model_profile_id,
                tool_policy=ToolPolicy(),
                status=RoleStatus.ACTIVE,
            )
            try:
                return self.service.create_role(role).id
            except Exception as exc:
                raise MaintenanceError("maintenance.role_not_configured", retryable=False) from exc
        raise MaintenanceError("maintenance.role_not_configured", retryable=False)

    def _validate_workflow(self) -> tuple[str, int, Any | None]:
        workflow_id, workflow_version = self._workflow_identity()
        definition = self._workflow_definition()
        if definition is None:
            return workflow_id, workflow_version, None
        if (
            str(getattr(definition, "workflow_id", "")) != workflow_id
            or int(getattr(definition, "version", 0)) != workflow_version
            or str(getattr(getattr(definition, "status", None), "value", "")) != "published"
        ):
            raise MaintenanceError("maintenance.workflow_not_published", retryable=False)
        nodes = tuple(getattr(definition, "nodes", ()))
        if not nodes or any(
            str(getattr(getattr(node, "node_kind", None), "value", "")) != "agent" for node in nodes
        ):
            raise MaintenanceError("maintenance.workflow_restricted_definition", retryable=False)
        return workflow_id, workflow_version, definition

    def _role_id(self, project_id: str) -> str:
        configured = _state_value(self.app, "maintenance_role_id")
        if configured:
            return str(configured)
        definition = self._workflow_definition()
        if definition is not None:
            for node in getattr(definition, "nodes", ()):
                metadata = getattr(node, "metadata", {}) or {}
                role_id = metadata.get("role_id")
                if role_id:
                    return str(role_id)
                reference = getattr(node, "agent_or_action_ref", None)
                if reference:
                    return str(reference)
        raise MaintenanceError("maintenance.role_not_configured", retryable=False)

    def _workspace(self, project_id: str, governance: GovernanceService | None = None) -> str:
        if governance is not None:
            manager = governance.manager
            project = manager._project(project_id)
            try:
                configured = governance._workspace_ref(project)
            except Exception as exc:
                raise MaintenanceError(
                    "maintenance.workspace_not_configured", retryable=False
                ) from exc
        else:
            configured = None
        state_workspace = _state_value(self.app, "maintenance_workspace")
        if state_workspace is not None:
            configured = state_workspace
        if callable(configured):
            configured = configured(project_id)
        if configured is None:
            raise MaintenanceError("maintenance.workspace_not_configured", retryable=False)
        path = Path(str(configured)).expanduser()
        try:
            return str(path.resolve(strict=True))
        except OSError as exc:
            raise MaintenanceError("maintenance.workspace_not_available", retryable=False) from exc

    def _foreground_active(self) -> bool:
        callback = _state_value(self.app, "foreground_active")
        if callable(callback):
            return bool(callback())
        with self.service.store._connect() as c:
            return (
                c.execute(
                    "SELECT 1 FROM session_run_leases WHERE released_at IS NULL "
                    "AND expires_at>? LIMIT 1",
                    (utc_now().isoformat(),),
                ).fetchone()
                is not None
            )

    def cancel_project(self, project_id: str) -> None:
        for run in tuple(self.coordinator._runs.values()):
            if run.snapshot.project_id == project_id:
                self.coordinator.cancel(run.snapshot.job_id)

    def _plugin_binding(
        self, governance: GovernanceService, project_id: str
    ) -> tuple[Any, Any, Any, dict[str, Any]]:
        manager = governance.manager
        project = manager._project(project_id)
        installation = manager._installation(project, enabled=True)
        if not installation.binding_id:
            raise MaintenanceError("maintenance.binding_missing", retryable=False)
        binding = manager.registry.get_binding(installation.binding_id)
        config = binding.config
        if not config.maintenance_enabled:
            raise MaintenanceError("maintenance.disabled", retryable=False)
        return (
            project,
            installation,
            binding,
            manager._scope(project).model_dump(),
        )

    def _make_snapshot(
        self, command: B25Command, idempotency_key: str
    ) -> tuple[MaintenanceSnapshot, GovernanceService, SQLiteMaintenanceCommitter]:
        if command.action != "maintenance_create":
            raise MaintenanceError("maintenance.command_action", retryable=False)
        governance = self._governance(command.project_id)
        project, installation, binding, _scope_payload = self._plugin_binding(
            governance, command.project_id
        )
        manager = governance.manager
        scope = manager._scope(project)
        committer = self._committer(governance, command.project_id)
        expected = committer.get_watermark(str(installation.dataset_id), "project_history")
        page = governance.search_history(
            command.project_id,
            cutoff_cursor=None,
            after_cursor=expected,
            limit=1,
        )
        source_cursor = int(page.cutoff_cursor)
        if source_cursor < expected:
            raise MaintenanceError("maintenance.source_watermark_conflict", retryable=True)
        model_profile_id = command.model_profile_id or binding.config.extraction_model_profile_id
        if not model_profile_id:
            raise MaintenanceError("maintenance.model_profile_not_configured", retryable=False)
        profile = self.service.get_model_profile(model_profile_id)
        if not profile.enabled:
            raise MaintenanceError("maintenance.model_profile_disabled", retryable=False)
        definition = self._ensure_workflow(profile.id)
        workflow_id, workflow_version = definition.workflow_id, definition.version
        role_id = str(definition.nodes[0].agent_or_action_ref)
        role_version: int | None = None
        try:
            role = self.service.get_role(role_id)
            role_version = role.version
            if definition is not None:
                locked = getattr(definition, "locked_role_versions", {}).get(role_id)
                if locked is not None:
                    role_version = int(locked)
                    role = self.service.get_role(role_id, role_version)
            if role.tool_policy != ToolPolicy():
                raise MaintenanceError("maintenance.extractor_tools_enabled", retryable=False)
        except MaintenanceError:
            raise
        except Exception as exc:
            raise MaintenanceError(
                "maintenance.role_snapshot_unavailable", retryable=False
            ) from exc
        workspace = self._workspace(command.project_id, governance)
        budget = MaintenanceBudget(
            max_sources=command.max_sources,
            max_output_tokens=command.max_output_tokens,
            max_attempts=command.max_attempts,
            max_input_tokens=int(_state_value(self.app, "maintenance_max_input_tokens", 8_000)),
            timeout_seconds=int(_state_value(self.app, "maintenance_timeout_seconds", 120)),
        )
        base = MaintenanceSnapshot(
            job_id=_job_id_for(str(installation.dataset_id), idempotency_key),
            idempotency_key=idempotency_key,
            project_id=command.project_id,
            dataset_id=str(installation.dataset_id),
            installation_id=str(installation.installation_id),
            binding_id=str(binding.binding_id),
            binding_epoch=binding.binding_epoch,
            permission_epoch=binding.permission_epoch,
            scope=scope,
            source_cursor=source_cursor,
            expected_processed_cursor=expected,
            source_digest="pending",
            workflow_id=workflow_id,
            workflow_version=workflow_version,
            plugin_id=str(installation.manifest.plugin_id),
            plugin_version=str(installation.manifest.plugin_version),
            package_digest=str(installation.manifest.package_digest),
            config_id=str(binding.config.config_id),
            config_revision=int(binding.config.revision),
            config_digest=_digest(binding.config.model_dump(mode="json")),
            model_profile_id=str(profile.id),
            model_id=str(profile.model_id),
            model_digest=_digest(profile.model_dump(mode="json")),
            prompt_version=f"role:{role.id}:v{role.version}",
            role_id=str(role.id),
            role_version=int(role.version),
            workspace=workspace,
            budget=budget,
            action="conflict_check",
        )
        source_provider = CanonicalSourceProvider(governance)
        sources = source_provider.list_sources(base, limit=budget.max_sources)
        selected_cursor = source_provider.last_source_cursor
        if selected_cursor is None:
            selected_cursor = base.source_cursor
        if selected_cursor != base.source_cursor:
            base = base.model_copy(update={"source_cursor": selected_cursor})
            # Re-read at the final frozen cursor.  This guarantees that a
            # capped batch advances only to its last actual UserMessage Item.
            sources = source_provider.list_sources(base, limit=budget.max_sources)
        snapshot = base.model_copy(
            update={
                "source_digest": _digest([source.model_dump(mode="json") for source in sources])
            }
        )
        return snapshot, governance, committer

    def _trigger_service(self) -> Any:
        value = (
            _state_value(self.app, "maintenance_trigger_service")
            or _state_value(self.app, "scheduler_trigger_service")
            or _state_value(self.app, "trigger_service")
        )
        if value is None:
            coordinator = _state_value(self.app, "scheduler_coordinator")
            value = getattr(coordinator, "trigger_service", None)
        if value is None:
            raise MaintenanceError("maintenance.scheduler_unavailable", retryable=True)
        return value

    def _scheduler_store(self, trigger: Any) -> Any | None:
        value = _state_value(self.app, "scheduler_store")
        return value or getattr(trigger, "_repository", None)

    async def execute_command(self, command: B25Command, idempotency_key: str) -> list[str]:
        if not idempotency_key:
            raise ValueError("idempotency key is required")
        if command.action == "maintenance_create":
            snapshot, _governance, committer = self._make_snapshot(command, idempotency_key)
            existing = committer.get_snapshot_by_idempotency(
                snapshot.dataset_id, snapshot.stable_idempotency_key
            )
            if existing is not None:
                if existing.request_digest != snapshot.request_digest:
                    raise MaintenanceError("idempotency_conflict", retryable=False)
                return [existing.job_id]
            trigger = self._trigger_service()
            now = utc_now()
            schedule = ScheduleDefinition(
                id="maintenance_schedule_" + snapshot.job_id.removeprefix("maintenance_"),
                name=f"Memory maintenance {snapshot.job_id}",
                trigger_kind=TriggerKind.TIMER,
                timer_at=now,
                timezone_name="UTC",
                misfire_policy=MisfirePolicy.FIRE_ONCE,
                workflow_id=snapshot.workflow_id,
                workflow_version=snapshot.workflow_version,
                workflow_input={
                    "maintenance": cast(dict[str, JsonValue], snapshot.model_dump(mode="json"))
                },
                concurrency_limit=1,
                max_attempts=snapshot.budget.max_attempts,
                # A model execution with an unknown result cannot safely be
                # repeated merely because its eventual DB commit is idempotent.
                dispatch_idempotency=DispatchIdempotency.NON_IDEMPOTENT,
                status=ScheduleStatus.PAUSED,
            )
            trigger.create_schedule(schedule)
            request = trigger.manual_trigger(
                schedule.id,
                idempotency_key=snapshot.stable_idempotency_key,
                now=now,
            )
            committer.register_queued(snapshot, run_request_id=request.id)
            return [snapshot.job_id]
        if command.action == "maintenance_cancel":
            return self._cancel(command)
        if command.action == "maintenance_retry":
            return self._retry(command, idempotency_key)
        raise MaintenanceError("maintenance.command_action", retryable=False)

    def _lookup(
        self, project_id: str, job_id: str
    ) -> tuple[MaintenanceSnapshot, SQLiteMaintenanceCommitter]:
        committer = self._projection_committer()
        snapshot = committer.get_snapshot(job_id)
        if snapshot is None or snapshot.project_id != project_id:
            raise ValueError("maintenance job not found")
        return snapshot, committer

    def _cancel(self, command: B25Command) -> list[str]:
        if not command.job_id:
            raise ValueError("job_id is required")
        snapshot, committer = self._lookup(command.project_id, command.job_id)
        cached = committer.get_result(snapshot)
        if cached is not None and cached.state in {
            MaintenanceState.SUCCEEDED,
            MaintenanceState.NO_CHANGE,
        }:
            return [snapshot.job_id]
        request_id, _graph_run_id = committer.get_scheduler_ids(snapshot.job_id)
        trigger = self._trigger_service()
        store = self._scheduler_store(trigger)
        if request_id is not None and store is not None:
            store.request_cancel(request_id)
        self.coordinator.cancel(snapshot.stable_idempotency_key)
        committer.mark_failure(
            snapshot,
            state=MaintenanceState.CANCELLED,
            error_code="maintenance.cancelled",
        )
        return [snapshot.job_id]

    def _retry(self, command: B25Command, idempotency_key: str) -> list[str]:
        if not command.job_id:
            raise ValueError("job_id is required")
        snapshot, committer = self._lookup(command.project_id, command.job_id)
        cached = committer.get_result(snapshot)
        if cached is not None and cached.state in {
            MaintenanceState.SUCCEEDED,
            MaintenanceState.NO_CHANGE,
        }:
            return [snapshot.job_id]
        request_id, _graph_run_id = committer.get_scheduler_ids(snapshot.job_id)
        if request_id is None:
            raise MaintenanceError("maintenance.request_missing", retryable=False)
        trigger = self._trigger_service()
        scheduler_store = self._scheduler_store(trigger)
        if (
            scheduler_store is None
            or scheduler_store.get_request(request_id).status.value != "dead_letter"
        ):
            raise MaintenanceError("maintenance.retry_not_ready", retryable=False)
        stored_job = next(
            j for j in committer.list_jobs(snapshot.project_id) if j.job_id == snapshot.job_id
        )
        if stored_job.attempts >= snapshot.budget.max_attempts:
            raise MaintenanceError("maintenance.attempt_budget_exhausted", retryable=False)
        replay_key = f"{snapshot.stable_idempotency_key}:retry:{idempotency_key}"[:300]
        request = trigger.replay_dead_letter(request_id, idempotency_key=replay_key)
        committer.register_queued(snapshot, run_request_id=request.id)
        return [snapshot.job_id]

    def _on_dispatch(self, snapshot: MaintenanceSnapshot, run_id: str) -> None:
        committer = self._projection_committer()
        request_id, _old_graph_id = committer.get_scheduler_ids(snapshot.job_id)
        committer.bind_scheduler_ids(
            snapshot.job_id,
            run_request_id=request_id,
            graph_run_id=run_id,
        )

    def jobs(self, project_id: str) -> list[MaintenanceJobView]:
        jobs = self._projection_committer().list_jobs(project_id)
        scheduler_store = _state_value(self.app, "scheduler_store")
        if scheduler_store is None:
            return jobs
        result = []
        for job in jobs:
            if job.run_request_id:
                request = scheduler_store.get_request(job.run_request_id)
                if request.status.value == "leased" and job.state in {
                    "succeeded",
                    "no_change",
                    "dead_letter",
                    "cancelled",
                    "manual_reconcile_required",
                }:
                    job = job.model_copy(update={"state": "running"})
                if request.status.value in {
                    "dead_letter",
                    "cancelled",
                    "manual_reconcile_required",
                    "retry_wait",
                }:
                    job = job.model_copy(
                        update={
                            "state": request.status.value,
                            "error_code": job.error_code or request.last_error_code,
                            "attempts": max(job.attempts, request.attempt_count),
                        }
                    )
            result.append(job)
        return result

    async def close(self) -> None:
        await self.coordinator.close()


def install_maintenance(
    app: Any,
    service: ApplicationService,
    governance_factory: Callable[..., GovernanceService],
) -> MaintenanceController:
    """Install the maintenance controller and expose its Scheduler gateway."""

    existing = _state_value(app, "b25_maintenance")
    if isinstance(existing, MaintenanceController):
        return existing
    controller = MaintenanceController(app, service, governance_factory)
    state = getattr(app, "state", app)
    state.b25_maintenance = controller
    state.maintenance_controller = controller
    state.maintenance_gateway = controller.scheduler_gateway
    # Phase45 creates the single Scheduler worker/gateway.  Replace only its
    # dispatch router; all ordinary Graph workflow IDs continue to use the
    # original gateway and worker lifecycle.
    scheduler_coordinator = _state_value(app, "scheduler_coordinator")
    worker = getattr(scheduler_coordinator, "worker", None)
    graph_gateway = getattr(worker, "_gateway", None)
    worker_object = cast(Any, worker)
    if graph_gateway is not None and not isinstance(
        graph_gateway, MaintenanceAwareSchedulerGateway
    ):
        worker_object._gateway = MaintenanceAwareSchedulerGateway(
            graph_gateway, controller.scheduler_gateway
        )
        state.scheduler_gateway = worker_object._gateway
    elif graph_gateway is None:
        state.scheduler_gateway = controller.scheduler_gateway
    # Core owns the Scheduler lifecycle.  This hook only drains local
    # maintenance tasks during shutdown and never starts another Scheduler.
    router = getattr(app, "router", None)
    if router is not None and hasattr(router, "add_event_handler"):
        router.on_shutdown.insert(0, controller.close)
    return controller


class StaticSourceProvider:
    """Bounded source provider useful for Core integration and isolated tests."""

    def __init__(self, sources: Iterable[SourceRef]) -> None:
        self.sources = tuple(sources)

    def list_sources(self, snapshot: MaintenanceSnapshot, *, limit: int) -> tuple[SourceRef, ...]:
        selected = tuple(
            source
            for source in self.sources
            if source.source_type == "item" and source.revision <= snapshot.source_cursor
        )
        return selected[:limit]


__all__ = [
    "AtomicMaintenanceCommitter",
    "CanonicalSourceProvider",
    "FiniteSessionExtractor",
    "MaintenanceBudget",
    "MaintenanceCoordinator",
    "MaintenanceError",
    "MaintenanceExecutor",
    "MaintenanceExtractor",
    "MaintenanceResult",
    "MaintenanceRunGateway",
    "MaintenanceSnapshot",
    "MaintenanceSourceProvider",
    "MaintenanceState",
    "SQLiteMaintenanceCommitter",
    "StaticSourceProvider",
]
