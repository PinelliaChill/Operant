from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, cast

from operant.domain.remote_execution import (
    CapabilityActionReceipt,
    CapabilityManifest,
    CapabilityObservation,
    RemoteActionIdempotency,
    RemoteExecutionJob,
    RemoteExecutionResult,
    RemoteJobStatus,
    RemoteTargetLease,
    RemoteTargetRegistration,
    RemoteTargetStatus,
)
from operant.persistence.sqlite import ConflictError, IdempotencyConflictError, SQLiteStore


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class SQLiteRemoteExecutionRepository:
    """Persistence and CAS authority for Phase 5B remote execution targets."""

    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    @staticmethod
    def _target(row: sqlite3.Row) -> RemoteTargetRegistration:
        return RemoteTargetRegistration(
            target_id=row["target_id"],
            display_name=row["display_name"],
            endpoint_ref=row["endpoint_ref"],
            identity_public_key=row["identity_public_key"],
            credential_ref=row["credential_ref"],
            policy_ref=row["policy_ref"],
            artifact_namespace=row["artifact_namespace"],
            capability_manifest=CapabilityManifest.model_validate_json(
                row["capability_manifest_json"]
            ),
            status=RemoteTargetStatus(row["status"]),
            fencing=int(row["fencing"]),
            last_seen_at=row["last_seen_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _lease(row: sqlite3.Row) -> RemoteTargetLease:
        return RemoteTargetLease(
            lease_id=row["lease_id"],
            target_id=row["target_id"],
            owner=row["owner"],
            token="redacted-at-rest",
            fencing=int(row["fencing"]),
            workspace_ref=row["workspace_ref"],
            expires_at=row["expires_at"],
            released_at=row["released_at"],
        )

    @staticmethod
    def _job(row: sqlite3.Row) -> RemoteExecutionJob:
        return RemoteExecutionJob(
            job_id=row["job_id"],
            target_id=row["target_id"],
            lease_id=row["lease_id"],
            lease_fencing=int(row["lease_fencing"]),
            capability=row["capability"],
            operation=row["operation"],
            arguments=json.loads(row["arguments_json"]),
            action_hash=row["action_hash"],
            idempotency_key=row["idempotency_key"],
            idempotency=RemoteActionIdempotency(row["idempotency"]),
            status=RemoteJobStatus(row["status"]),
            cancellation_requested=bool(row["cancellation_requested"]),
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )

    @staticmethod
    def _result(row: sqlite3.Row) -> RemoteExecutionResult:
        return RemoteExecutionResult(
            result_id=row["result_id"],
            job_id=row["job_id"],
            result_idempotency_key=row["result_idempotency_key"],
            status=RemoteJobStatus(row["status"]),
            artifact_ref=row["artifact_ref"],
            artifact_sha256=row["artifact_sha256"],
            postcondition=json.loads(row["postcondition_json"]),
            error_code=row["error_code"],
            completed_at=row["completed_at"],
        )

    def register_target(self, target: RemoteTargetRegistration) -> RemoteTargetRegistration:
        payload = target.capability_manifest.model_dump(mode="json")
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM remote_execution_targets WHERE target_id=?", (target.target_id,)
            ).fetchone()
            if existing is not None:
                current = self._target(existing)
                immutable = (
                    "display_name",
                    "endpoint_ref",
                    "identity_public_key",
                    "credential_ref",
                    "policy_ref",
                    "artifact_namespace",
                    "capability_manifest",
                )
                if any(getattr(current, name) != getattr(target, name) for name in immutable):
                    raise ConflictError("remote target registration is immutable")
                return current
            connection.execute(
                """
                INSERT INTO remote_execution_targets(
                    target_id, display_name, endpoint_ref, identity_public_key, credential_ref,
                    policy_ref, artifact_namespace, capability_manifest_json, status, fencing,
                    last_seen_at, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    target.target_id,
                    target.display_name,
                    target.endpoint_ref,
                    target.identity_public_key,
                    target.credential_ref,
                    target.policy_ref,
                    target.artifact_namespace,
                    _json(payload),
                    target.status.value,
                    target.fencing,
                    None if target.last_seen_at is None else target.last_seen_at.isoformat(),
                    target.created_at.isoformat(),
                    target.updated_at.isoformat(),
                ),
            )
        return target

    def get_target(self, target_id: str) -> RemoteTargetRegistration:
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT * FROM remote_execution_targets WHERE target_id=?", (target_id,)
            ).fetchone()
        if row is None:
            raise KeyError(target_id)
        return self._target(row)

    def list_targets(self, *, limit: int = 200) -> tuple[RemoteTargetRegistration, ...]:
        if not 1 <= limit <= 500:
            raise ValueError("invalid remote target page")
        with self.store._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM remote_execution_targets ORDER BY created_at, target_id LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(self._target(row) for row in rows)

    def heartbeat_target(
        self,
        target_id: str,
        *,
        identity_public_key: str,
        now: datetime,
    ) -> RemoteTargetRegistration:
        timestamp = _utc(now).isoformat()
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM remote_execution_targets WHERE target_id=?", (target_id,)
            ).fetchone()
            if row is None:
                raise KeyError(target_id)
            if row["identity_public_key"] != identity_public_key:
                raise ConflictError("remote target identity does not match")
            if row["status"] == RemoteTargetStatus.REVOKED.value:
                raise ConflictError("remote target is revoked")
            connection.execute(
                "UPDATE remote_execution_targets SET status=?, last_seen_at=?, updated_at=? "
                "WHERE target_id=?",
                (RemoteTargetStatus.ONLINE.value, timestamp, timestamp, target_id),
            )
            updated = connection.execute(
                "SELECT * FROM remote_execution_targets WHERE target_id=?", (target_id,)
            ).fetchone()
        assert updated is not None
        return self._target(updated)

    def acquire_lease(self, lease: RemoteTargetLease, *, now: datetime) -> RemoteTargetLease:
        timestamp = _utc(now).isoformat()
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            target = connection.execute(
                "SELECT * FROM remote_execution_targets WHERE target_id=?", (lease.target_id,)
            ).fetchone()
            if target is None:
                raise KeyError(lease.target_id)
            if target["status"] != RemoteTargetStatus.ONLINE.value:
                raise ConflictError("remote target must be online before leasing")
            active = connection.execute(
                "SELECT lease_id FROM remote_target_leases WHERE target_id=? "
                "AND released_at IS NULL AND expires_at>?",
                (lease.target_id, timestamp),
            ).fetchone()
            if active is not None:
                raise ConflictError("remote target already has an active lease")
            fencing = int(target["fencing"]) + 1
            connection.execute(
                "UPDATE remote_execution_targets SET fencing=?, updated_at=? WHERE target_id=?",
                (fencing, timestamp, lease.target_id),
            )
            persisted = lease.model_copy(update={"fencing": fencing})
            connection.execute(
                "INSERT INTO remote_target_leases VALUES (?,?,?,?,?,?,?,?)",
                (
                    persisted.lease_id,
                    persisted.target_id,
                    persisted.owner,
                    _token_hash(persisted.token),
                    persisted.fencing,
                    persisted.workspace_ref,
                    persisted.expires_at.isoformat(),
                    None,
                ),
            )
        return persisted

    def get_lease(self, lease_id: str) -> RemoteTargetLease:
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT * FROM remote_target_leases WHERE lease_id=?", (lease_id,)
            ).fetchone()
        if row is None:
            raise KeyError(lease_id)
        return self._lease(row)

    @staticmethod
    def _require_live_lease(
        connection: sqlite3.Connection,
        *,
        target_id: str,
        lease_id: str,
        token: str,
        fencing: int,
        now: datetime,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM remote_target_leases WHERE lease_id=? AND target_id=?",
            (lease_id, target_id),
        ).fetchone()
        if (
            row is None
            or row["token_hash"] != _token_hash(token)
            or int(row["fencing"]) != fencing
            or row["released_at"] is not None
            or datetime.fromisoformat(row["expires_at"]) <= _utc(now)
        ):
            raise ConflictError("remote target lease is stale or mismatched")
        target = connection.execute(
            "SELECT fencing, status FROM remote_execution_targets WHERE target_id=?", (target_id,)
        ).fetchone()
        if (
            target is None
            or int(target["fencing"]) != fencing
            or target["status"] != RemoteTargetStatus.ONLINE.value
        ):
            raise ConflictError("remote target fencing is stale")
        return cast(sqlite3.Row, row)

    def renew_lease(
        self,
        *,
        target_id: str,
        lease_id: str,
        token: str,
        fencing: int,
        now: datetime,
        expires_at: datetime,
    ) -> RemoteTargetLease:
        if _utc(expires_at) <= _utc(now):
            raise ValueError("lease expiry must follow renewal time")
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_live_lease(
                connection,
                target_id=target_id,
                lease_id=lease_id,
                token=token,
                fencing=fencing,
                now=now,
            )
            connection.execute(
                "UPDATE remote_target_leases SET expires_at=? WHERE lease_id=?",
                (_utc(expires_at).isoformat(), lease_id),
            )
            row = connection.execute(
                "SELECT * FROM remote_target_leases WHERE lease_id=?", (lease_id,)
            ).fetchone()
        assert row is not None
        return self._lease(row)

    def release_lease(
        self,
        *,
        target_id: str,
        lease_id: str,
        token: str,
        fencing: int,
        now: datetime,
    ) -> RemoteTargetLease:
        timestamp = _utc(now).isoformat()
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_live_lease(
                connection,
                target_id=target_id,
                lease_id=lease_id,
                token=token,
                fencing=fencing,
                now=now,
            )
            connection.execute(
                "UPDATE remote_target_leases SET released_at=? WHERE lease_id=?",
                (timestamp, lease_id),
            )
            row = connection.execute(
                "SELECT * FROM remote_target_leases WHERE lease_id=?", (lease_id,)
            ).fetchone()
        assert row is not None
        return self._lease(row)

    def create_job(
        self,
        job: RemoteExecutionJob,
        *,
        lease_token: str,
        now: datetime,
    ) -> RemoteExecutionJob:
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_live_lease(
                connection,
                target_id=job.target_id,
                lease_id=job.lease_id,
                token=lease_token,
                fencing=job.lease_fencing,
                now=now,
            )
            existing = connection.execute(
                "SELECT * FROM remote_execution_jobs WHERE target_id=? AND idempotency_key=?",
                (job.target_id, job.idempotency_key),
            ).fetchone()
            if existing is not None:
                current = self._job(existing)
                if current.action_hash != job.action_hash:
                    raise IdempotencyConflictError(
                        "remote job idempotency key is bound to another action"
                    )
                return current
            target = connection.execute(
                "SELECT capability_manifest_json FROM remote_execution_targets WHERE target_id=?",
                (job.target_id,),
            ).fetchone()
            assert target is not None
            manifest = CapabilityManifest.model_validate_json(target["capability_manifest_json"])
            if job.capability not in manifest.capabilities:
                raise ConflictError("remote target does not advertise the requested capability")
            if job.operation not in manifest.supported_operations:
                raise ConflictError("remote target does not advertise the requested operation")
            if len(_json(job.arguments).encode("utf-8")) > manifest.max_payload_bytes:
                raise ConflictError("remote job payload exceeds the target manifest limit")
            active_count = connection.execute(
                "SELECT COUNT(*) AS count FROM remote_execution_jobs WHERE target_id=? "
                "AND status IN ('queued','leased','running')",
                (job.target_id,),
            ).fetchone()
            assert active_count is not None
            if int(active_count["count"]) >= manifest.max_concurrent_jobs:
                raise ConflictError("remote target concurrency limit reached")
            try:
                connection.execute(
                    """
                    INSERT INTO remote_execution_jobs(
                        job_id,target_id,lease_id,lease_fencing,capability,operation,
                        arguments_json,action_hash,idempotency_key,idempotency,status,
                        cancellation_requested,created_at,started_at,finished_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        job.job_id,
                        job.target_id,
                        job.lease_id,
                        job.lease_fencing,
                        job.capability.value,
                        job.operation,
                        _json(job.arguments),
                        job.action_hash,
                        job.idempotency_key,
                        job.idempotency.value,
                        job.status.value,
                        int(job.cancellation_requested),
                        job.created_at.isoformat(),
                        None,
                        None,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("unable to persist remote execution job") from exc
        return job

    def poll_jobs(
        self,
        *,
        target_id: str,
        lease_id: str,
        token: str,
        fencing: int,
        now: datetime,
        limit: int,
    ) -> tuple[RemoteExecutionJob, ...]:
        if not 1 <= limit <= 32:
            raise ValueError("invalid remote job poll limit")
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_live_lease(
                connection,
                target_id=target_id,
                lease_id=lease_id,
                token=token,
                fencing=fencing,
                now=now,
            )
            rows = connection.execute(
                "SELECT job_id FROM remote_execution_jobs WHERE target_id=? AND lease_id=? "
                "AND lease_fencing=? AND status='queued' ORDER BY created_at,job_id LIMIT ?",
                (target_id, lease_id, fencing, limit),
            ).fetchall()
            job_ids = tuple(row["job_id"] for row in rows)
            if not job_ids:
                return ()
            placeholders = ",".join("?" for _ in job_ids)
            connection.execute(
                f"UPDATE remote_execution_jobs SET status='running',started_at=? "
                f"WHERE job_id IN ({placeholders}) AND status='queued'",
                (_utc(now).isoformat(), *job_ids),
            )
            claimed = connection.execute(
                f"SELECT * FROM remote_execution_jobs WHERE job_id IN ({placeholders}) "
                "ORDER BY created_at,job_id",
                job_ids,
            ).fetchall()
        return tuple(self._job(row) for row in claimed)

    def get_job(self, job_id: str) -> RemoteExecutionJob:
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT * FROM remote_execution_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._job(row)

    def list_jobs(
        self, *, target_id: str | None = None, limit: int = 200
    ) -> tuple[RemoteExecutionJob, ...]:
        if not 1 <= limit <= 500:
            raise ValueError("invalid remote job page")
        query = "SELECT * FROM remote_execution_jobs"
        params: tuple[Any, ...]
        if target_id is None:
            query += " ORDER BY created_at DESC,job_id LIMIT ?"
            params = (limit,)
        else:
            query += " WHERE target_id=? ORDER BY created_at DESC,job_id LIMIT ?"
            params = (target_id, limit)
        with self.store._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return tuple(self._job(row) for row in rows)

    def complete_job(
        self,
        result: RemoteExecutionResult,
        *,
        target_id: str,
        lease_id: str,
        token: str,
        fencing: int,
        now: datetime,
    ) -> RemoteExecutionResult:
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_live_lease(
                connection,
                target_id=target_id,
                lease_id=lease_id,
                token=token,
                fencing=fencing,
                now=now,
            )
            job_row = connection.execute(
                "SELECT * FROM remote_execution_jobs WHERE job_id=?", (result.job_id,)
            ).fetchone()
            if job_row is None:
                raise KeyError(result.job_id)
            job = self._job(job_row)
            if (
                job.target_id != target_id
                or job.lease_id != lease_id
                or job.lease_fencing != fencing
            ):
                raise ConflictError("remote result is bound to another target lease")
            existing = connection.execute(
                "SELECT * FROM remote_execution_results WHERE job_id=?", (result.job_id,)
            ).fetchone()
            if existing is not None:
                current = self._result(existing)
                if (
                    current.result_idempotency_key != result.result_idempotency_key
                    or current.status != result.status
                    or current.artifact_ref != result.artifact_ref
                    or current.artifact_sha256 != result.artifact_sha256
                    or current.postcondition != result.postcondition
                    or current.error_code != result.error_code
                ):
                    raise IdempotencyConflictError("remote result is already bound")
                return current
            if job.status not in {RemoteJobStatus.RUNNING, RemoteJobStatus.LEASED}:
                raise ConflictError("remote job is not running")
            connection.execute(
                """
                INSERT INTO remote_execution_results(
                    result_id,job_id,result_idempotency_key,status,artifact_ref,
                    artifact_sha256,postcondition_json,error_code,completed_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    result.result_id,
                    result.job_id,
                    result.result_idempotency_key,
                    result.status.value,
                    result.artifact_ref,
                    result.artifact_sha256,
                    _json(result.postcondition),
                    result.error_code,
                    result.completed_at.isoformat(),
                ),
            )
            connection.execute(
                "UPDATE remote_execution_jobs SET status=?,finished_at=? WHERE job_id=?",
                (result.status.value, result.completed_at.isoformat(), result.job_id),
            )
        return result

    def cancel_job(self, job_id: str, *, now: datetime) -> RemoteExecutionJob:
        timestamp = _utc(now).isoformat()
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM remote_execution_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            job = self._job(row)
            if job.status is RemoteJobStatus.QUEUED:
                connection.execute(
                    "UPDATE remote_execution_jobs SET status='cancelled',"
                    "cancellation_requested=1,finished_at=? WHERE job_id=?",
                    (timestamp, job_id),
                )
            elif job.status in {RemoteJobStatus.LEASED, RemoteJobStatus.RUNNING}:
                connection.execute(
                    "UPDATE remote_execution_jobs SET cancellation_requested=1 WHERE job_id=?",
                    (job_id,),
                )
            updated = connection.execute(
                "SELECT * FROM remote_execution_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        assert updated is not None
        return self._job(updated)

    def reconcile_expired(self, *, now: datetime) -> tuple[RemoteExecutionJob, ...]:
        timestamp = _utc(now).isoformat()
        changed: list[RemoteExecutionJob] = []
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT j.* FROM remote_execution_jobs j
                JOIN remote_target_leases l ON l.lease_id=j.lease_id
                WHERE j.status IN ('queued','leased','running')
                  AND (l.released_at IS NOT NULL OR l.expires_at<=?)
                ORDER BY j.created_at,j.job_id
                """,
                (timestamp,),
            ).fetchall()
            for row in rows:
                job = self._job(row)
                status = (
                    RemoteJobStatus.MANUAL_RECONCILE_REQUIRED
                    if job.status is RemoteJobStatus.RUNNING
                    and job.idempotency is RemoteActionIdempotency.NON_IDEMPOTENT
                    else RemoteJobStatus.FAILED
                )
                connection.execute(
                    "UPDATE remote_execution_jobs SET status=?,finished_at=? WHERE job_id=?",
                    (status.value, timestamp, job.job_id),
                )
                changed.append(job.model_copy(update={"status": status, "finished_at": _utc(now)}))
        return tuple(changed)

    def record_observation(self, observation: CapabilityObservation) -> CapabilityObservation:
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM capability_observations WHERE target_id=? AND observation_hash=?",
                (observation.target_id, observation.observation_hash),
            ).fetchone()
            if existing is not None:
                return self._observation(existing)
            connection.execute(
                "INSERT INTO capability_observations VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    observation.observation_id,
                    observation.target_id,
                    observation.capability.value,
                    observation.target_ref,
                    observation.observation_hash,
                    _json(observation.body),
                    observation.artifact_ref,
                    observation.created_at.isoformat(),
                    observation.expires_at.isoformat(),
                ),
            )
        return observation

    @staticmethod
    def _observation(row: sqlite3.Row) -> CapabilityObservation:
        return CapabilityObservation(
            observation_id=row["observation_id"],
            target_id=row["target_id"],
            capability=row["capability"],
            target_ref=row["target_ref"],
            observation_hash=row["observation_hash"],
            body=json.loads(row["body_json"]),
            artifact_ref=row["artifact_ref"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
        )

    def get_observation(self, target_id: str, observation_hash: str) -> CapabilityObservation:
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT * FROM capability_observations WHERE target_id=? AND observation_hash=?",
                (target_id, observation_hash),
            ).fetchone()
        if row is None:
            raise KeyError(observation_hash)
        return self._observation(row)

    def reserve_action_receipt(self, receipt: CapabilityActionReceipt) -> CapabilityActionReceipt:
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM capability_action_receipts WHERE action_hash=?",
                (receipt.action_hash,),
            ).fetchone()
            if row is not None:
                return self._receipt(row)
            connection.execute(
                "INSERT INTO capability_action_receipts VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    receipt.receipt_id,
                    receipt.target_id,
                    receipt.capability.value,
                    receipt.action_hash,
                    receipt.observation_hash,
                    receipt.status.value,
                    _json(receipt.result),
                    receipt.error_code,
                    receipt.created_at.isoformat(),
                    None if receipt.completed_at is None else receipt.completed_at.isoformat(),
                ),
            )
        return receipt

    @staticmethod
    def _receipt(row: sqlite3.Row) -> CapabilityActionReceipt:
        return CapabilityActionReceipt(
            receipt_id=row["receipt_id"],
            target_id=row["target_id"],
            capability=row["capability"],
            action_hash=row["action_hash"],
            observation_hash=row["observation_hash"],
            status=row["status"],
            result=json.loads(row["result_json"]),
            error_code=row["error_code"],
            created_at=row["created_at"],
            completed_at=row["completed_at"],
        )

    def complete_action_receipt(
        self,
        action_hash: str,
        *,
        status: RemoteJobStatus,
        result: dict[str, Any],
        error_code: str | None,
        now: datetime,
    ) -> CapabilityActionReceipt:
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM capability_action_receipts WHERE action_hash=?", (action_hash,)
            ).fetchone()
            if row is None:
                raise KeyError(action_hash)
            current = self._receipt(row)
            if current.status in {
                RemoteJobStatus.SUCCEEDED,
                RemoteJobStatus.FAILED,
                RemoteJobStatus.CANCELLED,
                RemoteJobStatus.MANUAL_RECONCILE_REQUIRED,
            }:
                return current
            connection.execute(
                "UPDATE capability_action_receipts SET status=?,result_json=?,error_code=?,"
                "completed_at=? WHERE action_hash=?",
                (status.value, _json(result), error_code, _utc(now).isoformat(), action_hash),
            )
            updated = connection.execute(
                "SELECT * FROM capability_action_receipts WHERE action_hash=?", (action_hash,)
            ).fetchone()
        assert updated is not None
        return self._receipt(updated)
