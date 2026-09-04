from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from secrets import token_urlsafe
from typing import Literal

from operant.application.scheduler import SchedulerConflictError
from operant.domain.scheduler import (
    AttemptStatus,
    DispatchIdempotency,
    JobAttempt,
    JobLease,
    RunRequest,
    RunRequestStatus,
    ScheduleDefinition,
    SchedulerLease,
    ScheduleStatus,
    require_aware_utc,
)

SCHEDULER_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schedule_definitions (
    schedule_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    definition_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(schedule_id, version)
);
CREATE TABLE IF NOT EXISTS schedule_heads (
    schedule_id TEXT PRIMARY KEY,
    current_version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('enabled','paused','cancelled')),
    cursor_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(schedule_id, current_version)
      REFERENCES schedule_definitions(schedule_id, version)
);
CREATE TABLE IF NOT EXISTS run_requests (
    request_id TEXT PRIMARY KEY,
    schedule_id TEXT NOT NULL,
    schedule_version INTEGER NOT NULL,
    occurrence_at TEXT NOT NULL,
    available_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    replay_of_request_id TEXT REFERENCES run_requests(request_id),
    workflow_id TEXT NOT NULL,
    workflow_version INTEGER NOT NULL,
    workflow_input_json TEXT NOT NULL,
    dispatch_idempotency TEXT NOT NULL
      CHECK(dispatch_idempotency IN ('idempotent','non_idempotent')),
    max_attempts INTEGER NOT NULL CHECK(max_attempts BETWEEN 1 AND 20),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count BETWEEN 0 AND 20),
    status TEXT NOT NULL CHECK(status IN
      ('queued','leased','retry_wait','succeeded','cancelled','dead_letter','manual_reconcile_required')),
    workflow_run_id TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),
    last_error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(schedule_id, schedule_version)
      REFERENCES schedule_definitions(schedule_id, version)
);
CREATE INDEX IF NOT EXISTS idx_run_requests_due
  ON run_requests(status, available_at, created_at);
CREATE INDEX IF NOT EXISTS idx_run_requests_schedule_status
  ON run_requests(schedule_id, status);
CREATE TABLE IF NOT EXISTS scheduler_authority_leases (
    kind TEXT PRIMARY KEY CHECK(kind IN ('scheduler_leader','runtime_writer')),
    owner TEXT NOT NULL,
    token TEXT NOT NULL,
    fencing INTEGER NOT NULL CHECK(fencing >= 1),
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_leases (
    run_request_id TEXT PRIMARY KEY REFERENCES run_requests(request_id),
    owner TEXT NOT NULL,
    token TEXT NOT NULL,
    fencing INTEGER NOT NULL CHECK(fencing >= 1),
    expires_at TEXT NOT NULL,
    attempt_number INTEGER NOT NULL CHECK(attempt_number BETWEEN 1 AND 20)
);
CREATE TABLE IF NOT EXISTS job_attempts (
    run_request_id TEXT NOT NULL REFERENCES run_requests(request_id),
    attempt_number INTEGER NOT NULL,
    lease_owner TEXT NOT NULL,
    lease_token TEXT NOT NULL,
    lease_fencing INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed','outcome_unknown')),
    side_effect_started INTEGER NOT NULL DEFAULT 0 CHECK(side_effect_started IN (0,1)),
    workflow_run_id TEXT,
    error_code TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    PRIMARY KEY(run_request_id, attempt_number)
);
"""


def _dt(value: datetime) -> str:
    return require_aware_utc(value, field="timestamp").isoformat()


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


class SQLiteSchedulerStore:
    """Independent Phase 5A store; its DDL is intended for the main migration owner."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)

    def initialize(self) -> None:
        with self._transaction() as connection:
            connection.executescript(SCHEDULER_SCHEMA_SQL)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def put_schedule(self, schedule: ScheduleDefinition) -> None:
        payload = schedule.model_dump_json()
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT definition_json FROM schedule_definitions "
                "WHERE schedule_id=? AND version=?",
                (schedule.id, schedule.version),
            ).fetchone()
            if existing is not None:
                if existing["definition_json"] != payload:
                    raise SchedulerConflictError("schedule revisions are immutable")
                return
            head = connection.execute(
                "SELECT current_version FROM schedule_heads WHERE schedule_id=?", (schedule.id,)
            ).fetchone()
            if head is not None and schedule.version != int(head["current_version"]) + 1:
                raise SchedulerConflictError("schedule version must advance by one")
            if head is None and schedule.version != 1:
                raise SchedulerConflictError("new schedules must start at version one")
            connection.execute(
                "INSERT INTO schedule_definitions VALUES (?,?,?,?)",
                (schedule.id, schedule.version, payload, _dt(schedule.created_at)),
            )
            if head is None:
                connection.execute(
                    "INSERT INTO schedule_heads VALUES (?,?,?,?,?)",
                    (
                        schedule.id,
                        schedule.version,
                        schedule.status.value,
                        _dt(schedule.created_at),
                        _dt(schedule.created_at),
                    ),
                )
            else:
                connection.execute(
                    "UPDATE schedule_heads SET current_version=?, status=?, updated_at=? "
                    "WHERE schedule_id=?",
                    (
                        schedule.version,
                        schedule.status.value,
                        _dt(schedule.created_at),
                        schedule.id,
                    ),
                )

    def get_schedule(self, schedule_id: str) -> ScheduleDefinition:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT d.definition_json, h.status FROM schedule_heads h "
                "JOIN schedule_definitions d ON d.schedule_id=h.schedule_id "
                "AND d.version=h.current_version WHERE h.schedule_id=?",
                (schedule_id,),
            ).fetchone()
        if row is None:
            raise KeyError(schedule_id)
        schedule = ScheduleDefinition.model_validate_json(row["definition_json"])
        return schedule.model_copy(update={"status": ScheduleStatus(row["status"])})

    def get_schedule_revision(self, schedule_id: str, version: int) -> ScheduleDefinition:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT definition_json FROM schedule_definitions "
                "WHERE schedule_id=? AND version=?",
                (schedule_id, version),
            ).fetchone()
        if row is None:
            raise KeyError((schedule_id, version))
        return ScheduleDefinition.model_validate_json(row["definition_json"])

    def get_schedule_cursor(self, schedule_id: str) -> datetime:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT cursor_at FROM schedule_heads WHERE schedule_id=?", (schedule_id,)
            ).fetchone()
        if row is None:
            raise KeyError(schedule_id)
        return _parse_dt(row["cursor_at"])

    def list_enabled_schedule_ids(self) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT schedule_id FROM schedule_heads WHERE status='enabled' ORDER BY schedule_id"
            ).fetchall()
        return tuple(str(row["schedule_id"]) for row in rows)

    def list_schedules(self, *, limit: int = 200) -> tuple[ScheduleDefinition, ...]:
        if not 1 <= limit <= 500:
            raise ValueError("schedule limit is invalid")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT d.definition_json, h.status FROM schedule_heads h "
                "JOIN schedule_definitions d ON d.schedule_id=h.schedule_id "
                "AND d.version=h.current_version ORDER BY h.schedule_id LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(
            ScheduleDefinition.model_validate_json(row["definition_json"]).model_copy(
                update={"status": ScheduleStatus(row["status"])}
            )
            for row in rows
        )

    def update_schedule_status(
        self, schedule_id: str, status: ScheduleStatus, *, expected_version: int
    ) -> ScheduleDefinition:
        now = datetime.now(timezone.utc)
        with self._transaction() as connection:
            changed = connection.execute(
                "UPDATE schedule_heads SET status=?, updated_at=? "
                "WHERE schedule_id=? AND current_version=? AND status!='cancelled'",
                (status.value, _dt(now), schedule_id, expected_version),
            ).rowcount
            if changed != 1:
                raise SchedulerConflictError("schedule status/version conflict")
            if status is ScheduleStatus.CANCELLED:
                connection.execute(
                    "UPDATE run_requests SET status='cancelled', updated_at=? "
                    "WHERE schedule_id=? AND status IN ('queued','retry_wait')",
                    (_dt(now), schedule_id),
                )
                connection.execute(
                    "UPDATE run_requests SET cancel_requested=1, updated_at=? "
                    "WHERE schedule_id=? AND status='leased'",
                    (_dt(now), schedule_id),
                )
        return self.get_schedule(schedule_id)

    def enqueue_occurrences(
        self,
        schedule: ScheduleDefinition,
        occurrences: tuple[datetime, ...],
        *,
        leader_lease: SchedulerLease,
        advance_cursor_to: datetime,
        available_at: datetime,
    ) -> tuple[RunRequest, ...]:
        if leader_lease.kind != "scheduler_leader":
            raise SchedulerConflictError("a scheduler leader lease is required")
        current_time = require_aware_utc(available_at, field="available_at")
        requests = tuple(self._request_for(schedule, value, available_at) for value in occurrences)
        with self._transaction() as connection:
            self._assert_authority(connection, leader_lease, current_time)
            head = connection.execute(
                "SELECT current_version, status FROM schedule_heads WHERE schedule_id=?",
                (schedule.id,),
            ).fetchone()
            if (
                head is None
                or int(head["current_version"]) != schedule.version
                or head["status"] != ScheduleStatus.ENABLED.value
            ):
                raise SchedulerConflictError("schedule changed while materializing")
            stored = tuple(self._insert_or_get_request(connection, request) for request in requests)
            connection.execute(
                "UPDATE schedule_heads SET cursor_at=?, updated_at=? WHERE schedule_id=?",
                (_dt(advance_cursor_to), _dt(advance_cursor_to), schedule.id),
            )
        return stored

    def enqueue_manual(
        self,
        schedule: ScheduleDefinition,
        *,
        idempotency_key: str,
        requested_at: datetime,
    ) -> RunRequest:
        if not idempotency_key:
            raise ValueError("idempotency_key must not be empty")
        request = self._request_for(
            schedule,
            requested_at,
            requested_at,
            idempotency_key=f"manual:{schedule.id}:{idempotency_key}",
        )
        with self._transaction() as connection:
            head = connection.execute(
                "SELECT current_version, status FROM schedule_heads WHERE schedule_id=?",
                (schedule.id,),
            ).fetchone()
            if (
                head is None
                or int(head["current_version"]) != schedule.version
                or head["status"] == ScheduleStatus.CANCELLED.value
            ):
                raise SchedulerConflictError("schedule changed while triggering manually")
            return self._insert_or_get_request(connection, request)

    def replay_dead_letter(
        self,
        request_id: str,
        *,
        idempotency_key: str,
        requested_at: datetime,
    ) -> RunRequest:
        if not idempotency_key:
            raise ValueError("idempotency_key must not be empty")
        current_time = require_aware_utc(requested_at, field="requested_at")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM run_requests WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is None:
                raise KeyError(request_id)
            source = self._run_request(row)
            if source.status is not RunRequestStatus.DEAD_LETTER:
                raise SchedulerConflictError("only dead-letter requests can be replayed")
            request = RunRequest(
                schedule_id=source.schedule_id,
                schedule_version=source.schedule_version,
                occurrence_at=current_time,
                available_at=current_time,
                idempotency_key=f"replay:{source.id}:{idempotency_key}",
                replay_of_request_id=source.id,
                workflow_id=source.workflow_id,
                workflow_version=source.workflow_version,
                workflow_input=source.workflow_input,
                dispatch_idempotency=source.dispatch_idempotency,
                max_attempts=source.max_attempts,
            )
            return self._insert_or_get_request(connection, request)

    @staticmethod
    def _request_for(
        schedule: ScheduleDefinition,
        occurrence_at: datetime,
        available_at: datetime,
        *,
        idempotency_key: str | None = None,
    ) -> RunRequest:
        key = idempotency_key or (
            f"schedule:{schedule.id}:v{schedule.version}:{_dt(occurrence_at)}"
        )
        return RunRequest(
            schedule_id=schedule.id,
            schedule_version=schedule.version,
            occurrence_at=occurrence_at,
            available_at=available_at,
            idempotency_key=key,
            workflow_id=schedule.workflow_id,
            workflow_version=schedule.workflow_version,
            workflow_input=schedule.workflow_input,
            dispatch_idempotency=schedule.dispatch_idempotency,
            max_attempts=schedule.max_attempts,
        )

    def _insert_or_get_request(
        self, connection: sqlite3.Connection, request: RunRequest
    ) -> RunRequest:
        try:
            connection.execute(
                "INSERT INTO run_requests "
                "(request_id,schedule_id,schedule_version,occurrence_at,available_at,"
                "idempotency_key,replay_of_request_id,workflow_id,workflow_version,"
                "workflow_input_json,dispatch_idempotency,max_attempts,attempt_count,status,"
                "workflow_run_id,cancel_requested,last_error_code,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    request.id,
                    request.schedule_id,
                    request.schedule_version,
                    _dt(request.occurrence_at),
                    _dt(request.available_at),
                    request.idempotency_key,
                    request.replay_of_request_id,
                    request.workflow_id,
                    request.workflow_version,
                    json.dumps(request.workflow_input, sort_keys=True, separators=(",", ":")),
                    request.dispatch_idempotency.value,
                    request.max_attempts,
                    request.attempt_count,
                    request.status.value,
                    request.workflow_run_id,
                    int(request.cancel_requested),
                    request.last_error_code,
                    _dt(request.created_at),
                    _dt(request.updated_at),
                ),
            )
        except sqlite3.IntegrityError:
            row = connection.execute(
                "SELECT * FROM run_requests WHERE idempotency_key=?", (request.idempotency_key,)
            ).fetchone()
            if row is None:
                raise
            existing = self._run_request(row)
            binding = (
                request.schedule_id,
                request.schedule_version,
                request.workflow_id,
                request.workflow_version,
                request.workflow_input,
                request.dispatch_idempotency,
                request.max_attempts,
            )
            existing_binding = (
                existing.schedule_id,
                existing.schedule_version,
                existing.workflow_id,
                existing.workflow_version,
                existing.workflow_input,
                existing.dispatch_idempotency,
                existing.max_attempts,
            )
            if binding != existing_binding:
                raise SchedulerConflictError(
                    "idempotency key is bound to another request"
                ) from None
            return existing
        return request

    def acquire_authority(
        self,
        kind: Literal["scheduler_leader", "runtime_writer"],
        *,
        owner: str,
        ttl_seconds: int,
        now: datetime | None = None,
    ) -> SchedulerLease:
        if not 1 <= len(owner) <= 200:
            raise ValueError("owner must contain between 1 and 200 characters")
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be positive")
        current_time = require_aware_utc(now or datetime.now(timezone.utc), field="now")
        expires = current_time + timedelta(seconds=ttl_seconds)
        token = token_urlsafe(24)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM scheduler_authority_leases WHERE kind=?", (kind,)
            ).fetchone()
            if row is not None and _parse_dt(row["expires_at"]) > current_time:
                raise SchedulerConflictError(f"{kind} already has a live owner")
            fencing = 1 if row is None else int(row["fencing"]) + 1
            connection.execute(
                "INSERT INTO scheduler_authority_leases VALUES (?,?,?,?,?) "
                "ON CONFLICT(kind) DO UPDATE SET owner=excluded.owner, token=excluded.token, "
                "fencing=excluded.fencing, expires_at=excluded.expires_at",
                (kind, owner, token, fencing, _dt(expires)),
            )
        return SchedulerLease(
            kind=kind, owner=owner, token=token, fencing=fencing, expires_at=expires
        )

    def renew_authority(
        self, lease: SchedulerLease, *, ttl_seconds: int, now: datetime | None = None
    ) -> SchedulerLease:
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be positive")
        current_time = require_aware_utc(now or datetime.now(timezone.utc), field="now")
        expires = current_time + timedelta(seconds=ttl_seconds)
        with self._transaction() as connection:
            changed = connection.execute(
                "UPDATE scheduler_authority_leases SET expires_at=? WHERE kind=? AND owner=? "
                "AND token=? AND fencing=? AND expires_at>?",
                (
                    _dt(expires),
                    lease.kind,
                    lease.owner,
                    lease.token,
                    lease.fencing,
                    _dt(current_time),
                ),
            ).rowcount
            if changed != 1:
                raise SchedulerConflictError("authority lease was lost or expired")
        return lease.model_copy(update={"expires_at": expires})

    def claim_due(
        self,
        writer_lease: SchedulerLease,
        *,
        owner: str,
        ttl_seconds: int,
        now: datetime | None = None,
    ) -> tuple[RunRequest, JobLease] | None:
        if not 1 <= len(owner) <= 200:
            raise ValueError("owner must contain between 1 and 200 characters")
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be positive")
        if writer_lease.kind != "runtime_writer" or writer_lease.owner != owner:
            raise SchedulerConflictError("a matching runtime writer lease is required")
        current_time = require_aware_utc(now or datetime.now(timezone.utc), field="now")
        with self._transaction() as connection:
            self._assert_authority(connection, writer_lease, current_time)
            self._recover_expired_jobs(connection, current_time)
            candidates = connection.execute(
                "SELECT r.*, d.definition_json FROM run_requests r "
                "JOIN schedule_heads h ON h.schedule_id=r.schedule_id "
                "JOIN schedule_definitions d ON d.schedule_id=r.schedule_id "
                "AND d.version=r.schedule_version "
                "WHERE r.status IN ('queued','retry_wait') AND r.available_at<=? "
                "AND r.cancel_requested=0 AND h.status!='cancelled' "
                "ORDER BY r.available_at, r.created_at, r.request_id LIMIT 100",
                (_dt(current_time),),
            ).fetchall()
            selected: sqlite3.Row | None = None
            for row in candidates:
                schedule = ScheduleDefinition.model_validate_json(row["definition_json"])
                active = connection.execute(
                    "SELECT COUNT(*) FROM run_requests WHERE schedule_id=? AND status='leased'",
                    (row["schedule_id"],),
                ).fetchone()[0]
                if int(active) < schedule.concurrency_limit:
                    selected = row
                    break
            if selected is None:
                return None
            request = self._run_request(selected)
            attempt_number = request.attempt_count + 1
            token = token_urlsafe(24)
            prior = connection.execute(
                "SELECT MAX(lease_fencing) FROM job_attempts WHERE run_request_id=?", (request.id,)
            ).fetchone()[0]
            fencing = 1 if prior is None else int(prior) + 1
            expires = min(
                current_time + timedelta(seconds=ttl_seconds),
                writer_lease.expires_at,
            )
            changed = connection.execute(
                "UPDATE run_requests SET status='leased', attempt_count=?, updated_at=? "
                "WHERE request_id=? AND status IN ('queued','retry_wait')",
                (attempt_number, _dt(current_time), request.id),
            ).rowcount
            if changed != 1:
                raise SchedulerConflictError("request claim conflict")
            connection.execute(
                "INSERT INTO job_leases VALUES (?,?,?,?,?,?)",
                (request.id, owner, token, fencing, _dt(expires), attempt_number),
            )
            connection.execute(
                "INSERT INTO job_attempts VALUES (?,?,?,?,?,'running',0,NULL,NULL,?,NULL)",
                (request.id, attempt_number, owner, token, fencing, _dt(current_time)),
            )
        claimed = request.model_copy(
            update={
                "status": RunRequestStatus.LEASED,
                "attempt_count": attempt_number,
                "updated_at": current_time,
            }
        )
        lease = JobLease(
            run_request_id=request.id,
            owner=owner,
            token=token,
            fencing=fencing,
            expires_at=expires,
            attempt_number=attempt_number,
        )
        return claimed, lease

    def renew_job(
        self,
        lease: JobLease,
        writer_lease: SchedulerLease,
        *,
        ttl_seconds: int,
        now: datetime | None = None,
    ) -> JobLease:
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be positive")
        if writer_lease.kind != "runtime_writer" or writer_lease.owner != lease.owner:
            raise SchedulerConflictError("a matching runtime writer lease is required")
        current_time = require_aware_utc(now or datetime.now(timezone.utc), field="now")
        with self._transaction() as connection:
            self._assert_authority(connection, writer_lease, current_time)
            expires = min(current_time + timedelta(seconds=ttl_seconds), writer_lease.expires_at)
            changed = connection.execute(
                "UPDATE job_leases SET expires_at=? WHERE run_request_id=? AND owner=? AND token=? "
                "AND fencing=? AND expires_at>?",
                (
                    _dt(expires),
                    lease.run_request_id,
                    lease.owner,
                    lease.token,
                    lease.fencing,
                    _dt(current_time),
                ),
            ).rowcount
            if changed != 1:
                raise SchedulerConflictError("job lease was lost or expired")
        return lease.model_copy(update={"expires_at": expires})

    def mark_side_effect_started(self, lease: JobLease, *, now: datetime | None = None) -> None:
        current_time = require_aware_utc(now or datetime.now(timezone.utc), field="now")
        with self._transaction() as connection:
            self._assert_job_lease(connection, lease, current_time)
            changed = connection.execute(
                "UPDATE job_attempts SET side_effect_started=1 WHERE run_request_id=? "
                "AND attempt_number=? AND status='running' AND EXISTS "
                "(SELECT 1 FROM run_requests WHERE request_id=? AND status='leased' "
                "AND cancel_requested=0)",
                (lease.run_request_id, lease.attempt_number, lease.run_request_id),
            ).rowcount
            if changed != 1:
                raise SchedulerConflictError("attempt is cancelled or no longer running")

    def mark_workflow_dispatched(
        self,
        lease: JobLease,
        *,
        workflow_run_id: str,
        now: datetime | None = None,
    ) -> RunRequest:
        """Bind a leased request to its GraphRun without releasing concurrency."""
        if not workflow_run_id:
            raise ValueError("workflow_run_id must not be empty")
        current_time = require_aware_utc(now or datetime.now(timezone.utc), field="now")
        with self._transaction() as connection:
            self._assert_job_lease(connection, lease, current_time)
            changed = connection.execute(
                "UPDATE job_attempts SET workflow_run_id=? WHERE run_request_id=? "
                "AND attempt_number=? AND status='running' AND side_effect_started=1 "
                "AND (workflow_run_id IS NULL OR workflow_run_id=?)",
                (
                    workflow_run_id,
                    lease.run_request_id,
                    lease.attempt_number,
                    workflow_run_id,
                ),
            ).rowcount
            if changed != 1:
                raise SchedulerConflictError("workflow dispatch binding conflict")
            changed = connection.execute(
                "UPDATE run_requests SET workflow_run_id=?, updated_at=? "
                "WHERE request_id=? AND status='leased' "
                "AND (workflow_run_id IS NULL OR workflow_run_id=?)",
                (workflow_run_id, _dt(current_time), lease.run_request_id, workflow_run_id),
            ).rowcount
            if changed != 1:
                raise SchedulerConflictError("run request dispatch binding conflict")
            row = connection.execute(
                "SELECT * FROM run_requests WHERE request_id=?", (lease.run_request_id,)
            ).fetchone()
        return self._run_request(row)

    def renew_dispatched_jobs(
        self,
        writer_lease: SchedulerLease,
        *,
        owner: str,
        ttl_seconds: int,
        now: datetime | None = None,
    ) -> tuple[tuple[RunRequest, JobLease], ...]:
        """Atomically recover expired work and renew every live dispatched lease."""
        if ttl_seconds < 1:
            raise ValueError("ttl_seconds must be positive")
        if writer_lease.kind != "runtime_writer" or writer_lease.owner != owner:
            raise SchedulerConflictError("a matching runtime writer lease is required")
        current_time = require_aware_utc(now or datetime.now(timezone.utc), field="now")
        expires = min(current_time + timedelta(seconds=ttl_seconds), writer_lease.expires_at)
        with self._transaction() as connection:
            self._assert_authority(connection, writer_lease, current_time)
            self._recover_expired_jobs(connection, current_time)
            rows = connection.execute(
                "SELECT r.*, l.owner AS lease_owner, l.token AS lease_token, "
                "l.fencing AS lease_fencing, l.attempt_number AS lease_attempt_number "
                "FROM run_requests r JOIN job_leases l ON l.run_request_id=r.request_id "
                "WHERE r.status='leased' AND r.workflow_run_id IS NOT NULL "
                "AND l.owner=? AND l.expires_at>? ORDER BY r.created_at, r.request_id",
                (owner, _dt(current_time)),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE job_leases SET expires_at=? WHERE run_request_id=? AND owner=? "
                    "AND token=? AND fencing=? AND attempt_number=?",
                    (
                        _dt(expires),
                        row["request_id"],
                        owner,
                        row["lease_token"],
                        row["lease_fencing"],
                        row["lease_attempt_number"],
                    ),
                )
        return tuple(
            (
                self._run_request(row),
                JobLease(
                    run_request_id=row["request_id"],
                    owner=owner,
                    token=row["lease_token"],
                    fencing=row["lease_fencing"],
                    expires_at=expires,
                    attempt_number=row["lease_attempt_number"],
                ),
            )
            for row in rows
        )

    def cancel_claimed_job(self, lease: JobLease, *, now: datetime | None = None) -> RunRequest:
        current_time = require_aware_utc(now or datetime.now(timezone.utc), field="now")
        with self._transaction() as connection:
            self._assert_job_lease(connection, lease, current_time)
            attempt = connection.execute(
                "SELECT side_effect_started FROM job_attempts WHERE run_request_id=? "
                "AND attempt_number=? AND status='running'",
                (lease.run_request_id, lease.attempt_number),
            ).fetchone()
            if attempt is None or bool(attempt["side_effect_started"]):
                raise SchedulerConflictError(
                    "a started side effect cannot be reported as cancelled"
                )
            connection.execute(
                "UPDATE job_attempts SET status='failed', error_code='cancelled', finished_at=? "
                "WHERE run_request_id=? AND attempt_number=?",
                (_dt(current_time), lease.run_request_id, lease.attempt_number),
            )
            connection.execute(
                "UPDATE run_requests SET status='cancelled', cancel_requested=1, "
                "last_error_code='cancelled', updated_at=? WHERE request_id=? AND status='leased'",
                (_dt(current_time), lease.run_request_id),
            )
            connection.execute(
                "DELETE FROM job_leases WHERE run_request_id=?", (lease.run_request_id,)
            )
            row = connection.execute(
                "SELECT * FROM run_requests WHERE request_id=?", (lease.run_request_id,)
            ).fetchone()
        return self._run_request(row)

    def complete_job(
        self,
        lease: JobLease,
        *,
        workflow_run_id: str,
        now: datetime | None = None,
    ) -> RunRequest:
        current_time = require_aware_utc(now or datetime.now(timezone.utc), field="now")
        with self._transaction() as connection:
            self._assert_job_lease(connection, lease, current_time)
            connection.execute(
                "UPDATE job_attempts SET status='succeeded', workflow_run_id=?, finished_at=? "
                "WHERE run_request_id=? AND attempt_number=? AND status='running'",
                (workflow_run_id, _dt(current_time), lease.run_request_id, lease.attempt_number),
            )
            connection.execute(
                "UPDATE run_requests SET status='succeeded', workflow_run_id=?, updated_at=? "
                "WHERE request_id=? AND status='leased'",
                (workflow_run_id, _dt(current_time), lease.run_request_id),
            )
            connection.execute(
                "DELETE FROM job_leases WHERE run_request_id=?", (lease.run_request_id,)
            )
            row = connection.execute(
                "SELECT * FROM run_requests WHERE request_id=?", (lease.run_request_id,)
            ).fetchone()
        return self._run_request(row)

    def finish_dispatched_job(
        self,
        lease: JobLease,
        *,
        request_status: RunRequestStatus,
        error_code: str,
        now: datetime | None = None,
    ) -> RunRequest:
        if request_status not in {
            RunRequestStatus.CANCELLED,
            RunRequestStatus.DEAD_LETTER,
            RunRequestStatus.MANUAL_RECONCILE_REQUIRED,
        }:
            raise ValueError("invalid dispatched terminal status")
        current_time = require_aware_utc(now or datetime.now(timezone.utc), field="now")
        attempt_status = (
            AttemptStatus.OUTCOME_UNKNOWN
            if request_status is RunRequestStatus.MANUAL_RECONCILE_REQUIRED
            else AttemptStatus.FAILED
        )
        with self._transaction() as connection:
            self._assert_job_lease(connection, lease, current_time)
            connection.execute(
                "UPDATE job_attempts SET status=?, error_code=?, finished_at=? "
                "WHERE run_request_id=? AND attempt_number=? AND status='running'",
                (
                    attempt_status.value,
                    error_code,
                    _dt(current_time),
                    lease.run_request_id,
                    lease.attempt_number,
                ),
            )
            connection.execute(
                "UPDATE run_requests SET status=?, last_error_code=?, updated_at=? "
                "WHERE request_id=? AND status='leased'",
                (
                    request_status.value,
                    error_code,
                    _dt(current_time),
                    lease.run_request_id,
                ),
            )
            connection.execute(
                "DELETE FROM job_leases WHERE run_request_id=?", (lease.run_request_id,)
            )
            row = connection.execute(
                "SELECT * FROM run_requests WHERE request_id=?", (lease.run_request_id,)
            ).fetchone()
        return self._run_request(row)

    def fail_job(
        self,
        lease: JobLease,
        *,
        error_code: str,
        outcome_unknown: bool,
        retry_base_seconds: int,
        retry_max_seconds: int,
        now: datetime | None = None,
    ) -> RunRequest:
        current_time = require_aware_utc(now or datetime.now(timezone.utc), field="now")
        with self._transaction() as connection:
            self._assert_job_lease(connection, lease, current_time)
            row = connection.execute(
                "SELECT r.*, a.side_effect_started FROM run_requests r JOIN job_attempts a "
                "ON a.run_request_id=r.request_id AND a.attempt_number=? WHERE r.request_id=?",
                (lease.attempt_number, lease.run_request_id),
            ).fetchone()
            if row is None:
                raise SchedulerConflictError("request attempt is missing")
            # An explicit known failure (for example Policy DENY/ASK) remains
            # retryable even though the generic gateway boundary was entered.
            # Only the caller's unknown verdict requires manual reconciliation.
            non_idempotent_unknown = (
                row["dispatch_idempotency"] == DispatchIdempotency.NON_IDEMPOTENT.value
                and outcome_unknown
            )
            if non_idempotent_unknown:
                request_status = RunRequestStatus.MANUAL_RECONCILE_REQUIRED
                attempt_status = AttemptStatus.OUTCOME_UNKNOWN
                available_at = current_time
            elif int(row["attempt_count"]) >= int(row["max_attempts"]):
                request_status = RunRequestStatus.DEAD_LETTER
                attempt_status = AttemptStatus.FAILED
                available_at = current_time
            else:
                request_status = RunRequestStatus.RETRY_WAIT
                attempt_status = AttemptStatus.FAILED
                delay = min(
                    retry_max_seconds,
                    retry_base_seconds * (2 ** (int(row["attempt_count"]) - 1)),
                )
                available_at = current_time + timedelta(seconds=delay)
            connection.execute(
                "UPDATE job_attempts SET status=?, error_code=?, finished_at=? "
                "WHERE run_request_id=? AND attempt_number=? AND status='running'",
                (
                    attempt_status.value,
                    error_code,
                    _dt(current_time),
                    lease.run_request_id,
                    lease.attempt_number,
                ),
            )
            connection.execute(
                "UPDATE run_requests SET status=?, available_at=?, last_error_code=?, updated_at=? "
                "WHERE request_id=? AND status='leased'",
                (
                    request_status.value,
                    _dt(available_at),
                    error_code,
                    _dt(current_time),
                    lease.run_request_id,
                ),
            )
            connection.execute(
                "DELETE FROM job_leases WHERE run_request_id=?", (lease.run_request_id,)
            )
            result = connection.execute(
                "SELECT * FROM run_requests WHERE request_id=?", (lease.run_request_id,)
            ).fetchone()
        return self._run_request(result)

    def request_cancel(self, request_id: str, *, now: datetime | None = None) -> RunRequest:
        current_time = require_aware_utc(now or datetime.now(timezone.utc), field="now")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT status FROM run_requests WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is None:
                raise KeyError(request_id)
            if row["status"] in (RunRequestStatus.QUEUED.value, RunRequestStatus.RETRY_WAIT.value):
                connection.execute(
                    "UPDATE run_requests SET status='cancelled', cancel_requested=1, updated_at=? "
                    "WHERE request_id=?",
                    (_dt(current_time), request_id),
                )
            elif row["status"] == RunRequestStatus.LEASED.value:
                connection.execute(
                    "UPDATE run_requests SET cancel_requested=1, updated_at=? WHERE request_id=?",
                    (_dt(current_time), request_id),
                )
            result = connection.execute(
                "SELECT * FROM run_requests WHERE request_id=?", (request_id,)
            ).fetchone()
        return self._run_request(result)

    def get_request(self, request_id: str) -> RunRequest:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM run_requests WHERE request_id=?", (request_id,)
            ).fetchone()
        if row is None:
            raise KeyError(request_id)
        return self._run_request(row)

    def list_requests(
        self,
        *,
        status: RunRequestStatus | None = None,
        limit: int = 200,
    ) -> tuple[RunRequest, ...]:
        if not 1 <= limit <= 500:
            raise ValueError("run request limit is invalid")
        query = "SELECT * FROM run_requests"
        parameters: tuple[object, ...]
        if status is None:
            parameters = (limit,)
        else:
            query += " WHERE status = ?"
            parameters = (status.value, limit)
        query += " ORDER BY created_at DESC, request_id LIMIT ?"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return tuple(self._run_request(row) for row in rows)

    def list_attempts(self, request_id: str) -> tuple[JobAttempt, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM job_attempts WHERE run_request_id=? ORDER BY attempt_number",
                (request_id,),
            ).fetchall()
        return tuple(
            JobAttempt(
                run_request_id=row["run_request_id"],
                attempt_number=row["attempt_number"],
                lease_owner=row["lease_owner"],
                lease_token=row["lease_token"],
                lease_fencing=row["lease_fencing"],
                status=AttemptStatus(row["status"]),
                side_effect_started=bool(row["side_effect_started"]),
                workflow_run_id=row["workflow_run_id"],
                error_code=row["error_code"],
                started_at=_parse_dt(row["started_at"]),
                finished_at=_parse_dt(row["finished_at"]) if row["finished_at"] else None,
            )
            for row in rows
        )

    @staticmethod
    def _assert_authority(
        connection: sqlite3.Connection, lease: SchedulerLease, now: datetime
    ) -> None:
        row = connection.execute(
            "SELECT * FROM scheduler_authority_leases WHERE kind=?", (lease.kind,)
        ).fetchone()
        if (
            row is None
            or row["owner"] != lease.owner
            or row["token"] != lease.token
            or int(row["fencing"]) != lease.fencing
            or _parse_dt(row["expires_at"]) <= now
        ):
            raise SchedulerConflictError("authority lease was lost or expired")

    @staticmethod
    def _assert_job_lease(
        connection: sqlite3.Connection, lease: JobLease, now: datetime | None = None
    ) -> None:
        row = connection.execute(
            "SELECT * FROM job_leases WHERE run_request_id=?", (lease.run_request_id,)
        ).fetchone()
        if (
            row is None
            or row["owner"] != lease.owner
            or row["token"] != lease.token
            or int(row["fencing"]) != lease.fencing
            or int(row["attempt_number"]) != lease.attempt_number
            or (now is not None and _parse_dt(row["expires_at"]) <= now)
        ):
            raise SchedulerConflictError("job lease was lost or expired")

    def _recover_expired_jobs(self, connection: sqlite3.Connection, now: datetime) -> None:
        rows = connection.execute(
            "SELECT r.*, l.attempt_number, a.side_effect_started FROM job_leases l "
            "JOIN run_requests r ON r.request_id=l.run_request_id "
            "JOIN job_attempts a ON a.run_request_id=l.run_request_id "
            "AND a.attempt_number=l.attempt_number WHERE l.expires_at<=?",
            (_dt(now),),
        ).fetchall()
        for row in rows:
            unsafe = (
                row["dispatch_idempotency"] == DispatchIdempotency.NON_IDEMPOTENT.value
                and bool(row["side_effect_started"])
                and row["workflow_run_id"] is None
            )
            if unsafe:
                status = RunRequestStatus.MANUAL_RECONCILE_REQUIRED
                attempt = AttemptStatus.OUTCOME_UNKNOWN
            elif int(row["attempt_count"]) >= int(row["max_attempts"]):
                status = RunRequestStatus.DEAD_LETTER
                attempt = AttemptStatus.FAILED
            else:
                status = RunRequestStatus.RETRY_WAIT
                attempt = AttemptStatus.FAILED
            connection.execute(
                "UPDATE job_attempts SET status=?, error_code='lease_expired', finished_at=? "
                "WHERE run_request_id=? AND attempt_number=? AND status='running'",
                (attempt.value, _dt(now), row["request_id"], row["attempt_number"]),
            )
            connection.execute(
                "UPDATE run_requests SET status=?, available_at=?, "
                "last_error_code='lease_expired', "
                "updated_at=? WHERE request_id=? AND status='leased'",
                (status.value, _dt(now), _dt(now), row["request_id"]),
            )
            connection.execute(
                "DELETE FROM job_leases WHERE run_request_id=?", (row["request_id"],)
            )

    @staticmethod
    def _run_request(row: sqlite3.Row) -> RunRequest:
        return RunRequest(
            id=row["request_id"],
            schedule_id=row["schedule_id"],
            schedule_version=row["schedule_version"],
            occurrence_at=_parse_dt(row["occurrence_at"]),
            available_at=_parse_dt(row["available_at"]),
            idempotency_key=row["idempotency_key"],
            replay_of_request_id=row["replay_of_request_id"],
            workflow_id=row["workflow_id"],
            workflow_version=row["workflow_version"],
            workflow_input=json.loads(row["workflow_input_json"]),
            dispatch_idempotency=DispatchIdempotency(row["dispatch_idempotency"]),
            max_attempts=row["max_attempts"],
            attempt_count=row["attempt_count"],
            status=RunRequestStatus(row["status"]),
            workflow_run_id=row["workflow_run_id"],
            cancel_requested=bool(row["cancel_requested"]),
            last_error_code=row["last_error_code"],
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
        )
