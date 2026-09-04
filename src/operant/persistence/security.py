from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from operant.domain.security import (
    ActionRequest,
    Capability,
    CapabilityLease,
    NormalizedTarget,
    PolicyDecision,
    SecurityAuditEvent,
)
from operant.persistence.sqlite import ConflictError, IdempotencyConflictError, SQLiteStore
from operant.protocol import redact_public_data


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class SQLiteSecurityRepository:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def record_security_action(self, action: ActionRequest) -> ActionRequest:
        safe_body = action.model_dump(mode="json")
        safe_body["normalized_arguments"] = redact_public_data(
            safe_body["normalized_arguments"], max_chars=12_000
        )
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT body FROM security_action_requests "
                "WHERE principal = ? AND idempotency_key = ?",
                (action.principal, action.idempotency_key),
            ).fetchone()
            if row is not None:
                existing = ActionRequest.model_validate_json(row["body"])
                if existing.action_hash != action.action_hash:
                    raise IdempotencyConflictError(
                        "security action idempotency key is bound to a different action"
                    )
                return existing
            try:
                connection.execute(
                    """
                    INSERT INTO security_action_requests(
                        id, action_hash, principal, tool, operation, target_type, target_id,
                        policy_version, idempotency_key, body, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        action.request_id,
                        action.action_hash,
                        action.principal,
                        action.tool,
                        action.operation,
                        action.normalized_target.target_type,
                        action.normalized_target.target_id,
                        action.policy_version,
                        action.idempotency_key,
                        _json(safe_body),
                        action.created_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("unable to persist security action") from exc
        return ActionRequest.model_validate(safe_body)

    def get_security_action(self, action_hash: str) -> ActionRequest:
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT body FROM security_action_requests WHERE action_hash = ?", (action_hash,)
            ).fetchone()
        if row is None:
            raise KeyError(action_hash)
        return ActionRequest.model_validate_json(row["body"])

    def append_security_audit(self, event: SecurityAuditEvent) -> SecurityAuditEvent:
        safe_detail = redact_public_data(event.detail, max_chars=12_000)
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO security_audit_events(
                        id, action_hash, principal, event_type, decision, rule_ids,
                        detail, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.event_id,
                        event.action_hash,
                        event.principal,
                        event.event_type,
                        None if event.decision is None else event.decision.value,
                        _json(event.rule_ids),
                        _json(safe_detail),
                        event.created_at.isoformat(),
                    ),
                ).lastrowid
            except sqlite3.IntegrityError as exc:
                raise ConflictError("unable to append security audit event") from exc
        if cursor is None:
            raise RuntimeError("security audit insert did not produce a cursor")
        return event.model_copy(update={"cursor": int(cursor), "detail": safe_detail})

    def list_security_audit(
        self, action_hash: str, *, after_cursor: int = 0, limit: int = 100
    ) -> list[SecurityAuditEvent]:
        if after_cursor < 0 or not 1 <= limit <= 500:
            raise ValueError("invalid security audit page")
        with self.store._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM security_audit_events
                WHERE action_hash = ? AND sequence > ?
                ORDER BY sequence LIMIT ?
                """,
                (action_hash, after_cursor, limit),
            ).fetchall()
        return [
            SecurityAuditEvent(
                event_id=row["id"],
                cursor=int(row["sequence"]),
                action_hash=row["action_hash"],
                principal=row["principal"],
                event_type=row["event_type"],
                decision=None if row["decision"] is None else PolicyDecision(row["decision"]),
                rule_ids=tuple(json.loads(row["rule_ids"])),
                detail=json.loads(row["detail"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def issue_capability_lease(self, lease: CapabilityLease) -> CapabilityLease:
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO capability_leases(
                        id, action_hash, principal, capability, target_id, policy_version,
                        issued_by, constraints, issued_at, expires_at, max_uses, uses, revoked_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        lease.lease_id,
                        lease.action_hash,
                        lease.principal,
                        lease.capability.value,
                        lease.target.target_id,
                        lease.policy_version,
                        lease.issued_by,
                        _json(
                            {
                                **lease.constraints,
                                "target": lease.target.model_dump(mode="json"),
                                "workspace_id": lease.workspace_id,
                            }
                        ),
                        lease.issued_at.isoformat(),
                        lease.expires_at.isoformat(),
                        lease.max_uses,
                        lease.uses,
                        None,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("unable to issue capability lease") from exc
        return lease

    def consume_capability_lease(
        self,
        lease_id: str,
        *,
        action_hash: str,
        principal: str,
        capability: Capability,
        target_id: str,
        now: datetime,
    ) -> CapabilityLease:
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE capability_leases SET uses = uses + 1
                WHERE id = ? AND action_hash = ? AND principal = ? AND capability = ?
                  AND target_id = ? AND revoked_at IS NULL AND expires_at > ? AND uses < max_uses
                """,
                (lease_id, action_hash, principal, capability.value, target_id, now.isoformat()),
            )
            if updated.rowcount != 1:
                raise ConflictError(
                    "capability lease is expired, exhausted, revoked, or mismatched"
                )
            row = connection.execute(
                "SELECT * FROM capability_leases WHERE id = ?", (lease_id,)
            ).fetchone()
        assert row is not None
        return self._lease_from_row(row)

    def get_capability_lease(self, lease_id: str) -> CapabilityLease:
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT * FROM capability_leases WHERE id = ?", (lease_id,)
            ).fetchone()
        if row is None:
            raise KeyError(lease_id)
        return self._lease_from_row(row)

    def revoke_capability_lease(self, lease_id: str, *, at: datetime) -> CapabilityLease:
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE capability_leases SET revoked_at = COALESCE(revoked_at, ?) WHERE id = ?",
                (at.isoformat(), lease_id),
            )
            row = connection.execute(
                "SELECT * FROM capability_leases WHERE id = ?", (lease_id,)
            ).fetchone()
        if row is None:
            raise KeyError(lease_id)
        return self._lease_from_row(row)

    def record_denial_signature(self, signature: str, *, action_hash: str) -> int:
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = datetime.now().astimezone().isoformat()
            connection.execute(
                """
                INSERT INTO policy_denial_observations(
                    signature, last_action_hash, occurrences, created_at, updated_at
                ) VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(signature) DO UPDATE SET
                    last_action_hash = excluded.last_action_hash,
                    occurrences = occurrences + 1,
                    updated_at = excluded.updated_at
                """,
                (signature, action_hash, now, now),
            )
            row = connection.execute(
                "SELECT occurrences FROM policy_denial_observations WHERE signature = ?",
                (signature,),
            ).fetchone()
        assert row is not None
        return int(row["occurrences"])

    @staticmethod
    def _lease_from_row(row: sqlite3.Row) -> CapabilityLease:
        constraints = json.loads(row["constraints"])
        target = NormalizedTarget.model_validate(constraints.pop("target"))
        workspace_id = constraints.pop("workspace_id", None)
        return CapabilityLease(
            lease_id=row["id"],
            action_hash=row["action_hash"],
            principal=row["principal"],
            capability=Capability(row["capability"]),
            target=target,
            workspace_id=workspace_id,
            constraints=constraints,
            issued_at=row["issued_at"],
            expires_at=row["expires_at"],
            max_uses=int(row["max_uses"]),
            uses=int(row["uses"]),
            policy_version=row["policy_version"],
            issued_by=row["issued_by"],
            revoked_at=row["revoked_at"],
        )
