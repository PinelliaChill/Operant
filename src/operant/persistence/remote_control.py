from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from operant.domain.remote_control import (
    HostInstance,
    PairingChallenge,
    RelayEnvelope,
    RelayEnvelopeStatus,
    RemoteCommandReceipt,
    RemoteCommandStatus,
    RemoteDevice,
    RemoteHostOnlineState,
    RemoteScope,
    RemoteSession,
    RemoteSessionState,
    RemoteTransportMode,
)
from operant.persistence.sqlite import ConflictError, IdempotencyConflictError, SQLiteStore


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


class SQLiteRemoteControlRepository:
    """Persistence over the frozen v13 Remote Control and Relay tables."""

    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def put_host(self, host: HostInstance) -> HostInstance:
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM remote_control_hosts WHERE host_id=?", (host.host_id,)
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO remote_control_hosts(
                        host_id, display_name, signing_public_key, exchange_public_key,
                        core_version, protocol_version, capabilities_json, enabled,
                        online_state, last_seen_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        host.host_id,
                        host.display_name,
                        host.signing_public_key,
                        host.exchange_public_key,
                        host.core_version,
                        host.protocol_version,
                        _json(host.capabilities),
                        int(host.enabled),
                        host.online_state.value,
                        None if host.last_seen_at is None else _utc(host.last_seen_at),
                        _utc(host.created_at),
                        _utc(host.updated_at),
                    ),
                )
            else:
                immutable = (
                    row["signing_public_key"],
                    row["exchange_public_key"],
                    row["created_at"],
                )
                expected = (
                    host.signing_public_key,
                    host.exchange_public_key,
                    _utc(host.created_at),
                )
                if immutable != expected:
                    raise ConflictError("remote host identity is immutable")
                connection.execute(
                    """
                    UPDATE remote_control_hosts SET display_name=?, core_version=?,
                        protocol_version=?, capabilities_json=?, enabled=?, online_state=?,
                        last_seen_at=?, updated_at=? WHERE host_id=?
                    """,
                    (
                        host.display_name,
                        host.core_version,
                        host.protocol_version,
                        _json(host.capabilities),
                        int(host.enabled),
                        host.online_state.value,
                        None if host.last_seen_at is None else _utc(host.last_seen_at),
                        _utc(host.updated_at),
                        host.host_id,
                    ),
                )
        return host

    def get_host(self, host_id: str) -> HostInstance:
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT * FROM remote_control_hosts WHERE host_id=?", (host_id,)
            ).fetchone()
        if row is None:
            raise KeyError(host_id)
        return self._host(row)

    def list_hosts(self, *, limit: int = 200) -> list[HostInstance]:
        if not 1 <= limit <= 500:
            raise ValueError("invalid remote host page")
        with self.store._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM remote_control_hosts ORDER BY created_at, host_id LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._host(row) for row in rows]

    def create_challenge(self, challenge: PairingChallenge) -> PairingChallenge:
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO remote_pairing_challenges(
                        challenge_id, host_id, code_hash, allowed_scopes_json, expires_at,
                        max_uses, uses, consumed_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        challenge.challenge_id,
                        challenge.host_id,
                        challenge.code_hash,
                        _json([scope.value for scope in challenge.allowed_scopes]),
                        _utc(challenge.expires_at),
                        challenge.max_uses,
                        challenge.uses,
                        None if challenge.consumed_at is None else _utc(challenge.consumed_at),
                        _utc(challenge.created_at),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("unable to create pairing challenge") from exc
        return challenge

    def get_challenge(self, challenge_id: str) -> PairingChallenge:
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT * FROM remote_pairing_challenges WHERE challenge_id=?", (challenge_id,)
            ).fetchone()
        if row is None:
            raise KeyError(challenge_id)
        return self._challenge(row)

    def consume_challenge(
        self, challenge_id: str, *, code_hash: str, now: datetime
    ) -> PairingChallenge:
        now_text = _utc(now)
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE remote_pairing_challenges
                SET uses=uses+1,
                    consumed_at=CASE WHEN uses+1 >= max_uses THEN ? ELSE consumed_at END
                WHERE challenge_id=? AND code_hash=? AND expires_at>?
                  AND consumed_at IS NULL AND uses<max_uses
                """,
                (now_text, challenge_id, code_hash, now_text),
            )
            if updated.rowcount != 1:
                raise ConflictError("pairing challenge is invalid, expired, or consumed")
            row = connection.execute(
                "SELECT * FROM remote_pairing_challenges WHERE challenge_id=?", (challenge_id,)
            ).fetchone()
        assert row is not None
        return self._challenge(row)

    def put_device(self, device: RemoteDevice) -> RemoteDevice:
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO remote_devices(
                        device_id, host_id, display_name, signing_public_key,
                        exchange_public_key, scopes_json, version, paired_at,
                        last_seen_at, revoked_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        device.device_id,
                        device.host_id,
                        device.display_name,
                        device.signing_public_key,
                        device.exchange_public_key,
                        _json([scope.value for scope in device.scopes]),
                        device.version,
                        _utc(device.paired_at),
                        None if device.last_seen_at is None else _utc(device.last_seen_at),
                        None if device.revoked_at is None else _utc(device.revoked_at),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("remote device identity already exists") from exc
        return device

    def get_device(self, device_id: str) -> RemoteDevice:
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT * FROM remote_devices WHERE device_id=?", (device_id,)
            ).fetchone()
        if row is None:
            raise KeyError(device_id)
        return self._device(row)

    def list_devices(self, host_id: str) -> list[RemoteDevice]:
        with self.store._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM remote_devices WHERE host_id=? ORDER BY paired_at", (host_id,)
            ).fetchall()
        return [self._device(row) for row in rows]

    def revoke_device(self, device_id: str, *, now: datetime) -> RemoteDevice:
        now_text = _utc(now)
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE remote_devices SET revoked_at=COALESCE(revoked_at, ?), version=version+1 "
                "WHERE device_id=? AND revoked_at IS NULL",
                (now_text, device_id),
            )
            connection.execute(
                "UPDATE remote_sessions SET connection_state='closed', "
                "disconnected_at=COALESCE(disconnected_at, ?) "
                "WHERE device_id=? AND connection_state!='closed'",
                (now_text, device_id),
            )
            row = connection.execute(
                "SELECT * FROM remote_devices WHERE device_id=?", (device_id,)
            ).fetchone()
        if row is None:
            raise KeyError(device_id)
        return self._device(row)

    def put_session(self, session: RemoteSession) -> RemoteSession:
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO remote_sessions(
                        remote_session_id, host_id, device_id, transport_mode,
                        protocol_version, event_cursor, connection_state, session_key_ref,
                        expires_at, connected_at, disconnected_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session.remote_session_id,
                        session.host_id,
                        session.device_id,
                        session.transport_mode.value,
                        session.protocol_version,
                        session.event_cursor,
                        session.connection_state.value,
                        session.session_key_ref,
                        _utc(session.expires_at),
                        None if session.connected_at is None else _utc(session.connected_at),
                        None if session.disconnected_at is None else _utc(session.disconnected_at),
                        _utc(session.created_at),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("unable to create remote session") from exc
        return session

    def get_session(self, session_id: str) -> RemoteSession:
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT * FROM remote_sessions WHERE remote_session_id=?", (session_id,)
            ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return self._session(row)

    def list_sessions(self, host_id: str) -> list[RemoteSession]:
        with self.store._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM remote_sessions WHERE host_id=? ORDER BY created_at", (host_id,)
            ).fetchall()
        return [self._session(row) for row in rows]

    def close_session(self, session_id: str, *, now: datetime) -> RemoteSession:
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE remote_sessions SET connection_state='closed', "
                "disconnected_at=COALESCE(disconnected_at, ?) WHERE remote_session_id=?",
                (_utc(now), session_id),
            )
            row = connection.execute(
                "SELECT * FROM remote_sessions WHERE remote_session_id=?", (session_id,)
            ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return self._session(row)

    def advance_cursor(self, session_id: str, cursor: int) -> RemoteSession:
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE remote_sessions SET event_cursor=MAX(event_cursor, ?) "
                "WHERE remote_session_id=?",
                (cursor, session_id),
            )
        return self.get_session(session_id)

    def reserve_command(
        self,
        *,
        command_id: str,
        idempotency_key: str,
        action_hash: str,
        host_id: str,
        device_id: str,
        session_id: str,
        payload_hash: str,
        nonce: str,
        signature: str,
        execution_owner_id: str,
        execution_lease_expires_at: datetime,
        now: datetime,
    ) -> tuple[RemoteCommandReceipt, bool]:
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT rowid AS cursor, * FROM remote_command_receipts "
                "WHERE command_id=? OR idempotency_key=?",
                (command_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if (
                    existing["command_id"] != command_id
                    or existing["payload_hash"] != payload_hash
                    or existing["device_id"] != device_id
                ):
                    raise IdempotencyConflictError("remote command key is already bound")
                return self._receipt(existing), False
            try:
                connection.execute(
                    """
                    INSERT INTO remote_command_receipts(
                        command_id, idempotency_key, action_hash, host_id, device_id,
                        remote_session_id, payload_hash, nonce, signature, status,
                        execution_owner_id, execution_lease_expires_at,
                        host_acknowledged_at, result_ref, error_code, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'received', ?, ?, NULL, NULL, NULL, ?, ?)
                    """,
                    (
                        command_id,
                        idempotency_key,
                        action_hash,
                        host_id,
                        device_id,
                        session_id,
                        payload_hash,
                        nonce,
                        signature,
                        execution_owner_id,
                        _utc(execution_lease_expires_at),
                        _utc(now),
                        _utc(now),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("remote command replay or binding conflict") from exc
        return self.get_command(command_id), True

    def claim_command_execution(
        self,
        command_id: str,
        *,
        execution_owner_id: str,
        execution_lease_expires_at: datetime,
        now: datetime,
    ) -> RemoteCommandReceipt:
        """CAS a never-executed receipt into accepted under this live owner."""
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE remote_command_receipts
                SET status='accepted', execution_owner_id=?, execution_lease_expires_at=?,
                    result_ref=NULL, error_code=NULL,
                    host_acknowledged_at=COALESCE(host_acknowledged_at, ?), updated_at=?
                WHERE command_id=? AND (
                    (status='received' AND execution_owner_id=?)
                    OR (status='rejected' AND error_code='remote.approval_required')
                )
                """,
                (
                    execution_owner_id,
                    _utc(execution_lease_expires_at),
                    _utc(now),
                    _utc(now),
                    command_id,
                    execution_owner_id,
                ),
            )
            if updated.rowcount != 1:
                raise ConflictError("remote command execution claim conflicts")
        return self.get_command(command_id)

    def renew_command_execution_lease(
        self,
        command_id: str,
        *,
        execution_owner_id: str,
        execution_lease_expires_at: datetime,
        now: datetime,
    ) -> None:
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE remote_command_receipts
                SET execution_lease_expires_at=?, updated_at=?
                WHERE command_id=? AND status='accepted' AND execution_owner_id=?
                """,
                (
                    _utc(execution_lease_expires_at),
                    _utc(now),
                    command_id,
                    execution_owner_id,
                ),
            )
            if updated.rowcount != 1:
                raise ConflictError("remote command execution lease was lost")

    def set_command_status(
        self,
        command_id: str,
        status: RemoteCommandStatus,
        *,
        expected_status: RemoteCommandStatus,
        execution_owner_id: str,
        now: datetime,
        result_ref: str | None = None,
        error_code: str | None = None,
        host_ack: bool = False,
    ) -> RemoteCommandReceipt:
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE remote_command_receipts SET status=?, result_ref=?, error_code=?,
                    execution_owner_id=NULL, execution_lease_expires_at=NULL,
                    host_acknowledged_at=CASE
                        WHEN ? THEN COALESCE(host_acknowledged_at, ?)
                        ELSE host_acknowledged_at
                    END,
                    updated_at=? WHERE command_id=? AND status=? AND execution_owner_id=?
                """,
                (
                    status.value,
                    result_ref,
                    error_code,
                    int(host_ack),
                    _utc(now),
                    _utc(now),
                    command_id,
                    expected_status.value,
                    execution_owner_id,
                ),
            )
            if updated.rowcount != 1:
                raise ConflictError("remote command status transition conflicts")
        return self.get_command(command_id)

    def get_command(self, command_id: str) -> RemoteCommandReceipt:
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT rowid AS cursor, * FROM remote_command_receipts WHERE command_id=?",
                (command_id,),
            ).fetchone()
        if row is None:
            raise KeyError(command_id)
        return self._receipt(row)

    def reconcile_incomplete_commands(self, *, now: datetime) -> tuple[int, int]:
        """Close receipts left across a process boundary without replaying any action."""
        now_text = _utc(now)
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            accepted = connection.execute(
                """
                UPDATE remote_command_receipts
                SET status='outcome_unknown', error_code='remote.manual_reconcile_required',
                    execution_owner_id=NULL, execution_lease_expires_at=NULL, updated_at=?
                WHERE status='accepted' AND (
                    execution_lease_expires_at IS NULL OR execution_lease_expires_at<=?
                )
                """,
                (now_text, now_text),
            ).rowcount
            received = connection.execute(
                """
                UPDATE remote_command_receipts
                SET status='rejected', error_code='remote.interrupted_before_execution',
                    execution_owner_id=NULL, execution_lease_expires_at=NULL, updated_at=?
                WHERE status='received' AND (
                    execution_lease_expires_at IS NULL OR execution_lease_expires_at<=?
                )
                """,
                (now_text, now_text),
            ).rowcount
        return int(accepted), int(received)

    def reconcile_command(
        self,
        command_id: str,
        *,
        status: RemoteCommandStatus,
        result_ref: str | None,
        error_code: str | None,
        now: datetime,
    ) -> RemoteCommandReceipt:
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE remote_command_receipts
                SET status=?, result_ref=?, error_code=?, execution_owner_id=NULL,
                    execution_lease_expires_at=NULL, updated_at=?
                WHERE command_id=? AND status='outcome_unknown'
                """,
                (status.value, result_ref, error_code, _utc(now), command_id),
            )
            if updated.rowcount != 1:
                raise ConflictError("remote command cannot be reconciled from its current state")
        return self.get_command(command_id)

    def list_command_events(
        self, host_id: str, *, after_cursor: int = 0, limit: int = 100
    ) -> list[dict[str, Any]]:
        if after_cursor < 0 or not 1 <= limit <= 500:
            raise ValueError("invalid remote event page")
        with self.store._connect() as connection:
            rows = connection.execute(
                """
                SELECT rowid AS cursor, command_id, device_id, remote_session_id,
                       status, host_acknowledged_at, error_code, updated_at
                FROM remote_command_receipts
                WHERE host_id=? AND rowid>? ORDER BY rowid LIMIT ?
                """,
                (host_id, after_cursor, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def publish_envelope(self, envelope: RelayEnvelope) -> RelayEnvelope:
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO relay_envelopes(
                        envelope_id, route_ref, sender_ref, recipient_ref, protocol_version,
                        ciphertext, nonce, status, expires_at, created_at, delivered_at,
                        acknowledged_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        envelope.envelope_id,
                        envelope.route_ref,
                        envelope.sender_ref,
                        envelope.recipient_ref,
                        envelope.protocol_version,
                        envelope.ciphertext,
                        envelope.nonce,
                        envelope.status.value,
                        _utc(envelope.expires_at),
                        _utc(envelope.created_at),
                        None if envelope.delivered_at is None else _utc(envelope.delivered_at),
                        None
                        if envelope.acknowledged_at is None
                        else _utc(envelope.acknowledged_at),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("relay envelope replay or identity conflict") from exc
        return envelope

    def pull_envelopes(
        self, route_ref: str, recipient_ref: str, *, now: datetime, limit: int = 50
    ) -> list[RelayEnvelope]:
        now_text = _utc(now)
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE relay_envelopes SET status='expired' "
                "WHERE status IN ('queued','delivered') AND expires_at<=?",
                (now_text,),
            )
            rows = connection.execute(
                """
                SELECT * FROM relay_envelopes WHERE route_ref=? AND recipient_ref=?
                  AND status IN ('queued','delivered') AND expires_at>?
                ORDER BY created_at LIMIT ?
                """,
                (route_ref, recipient_ref, now_text, limit),
            ).fetchall()
            ids = [row["envelope_id"] for row in rows]
            for envelope_id in ids:
                connection.execute(
                    "UPDATE relay_envelopes SET status='delivered', "
                    "delivered_at=COALESCE(delivered_at, ?) WHERE envelope_id=?",
                    (now_text, envelope_id),
                )
            if ids:
                placeholders = ",".join("?" for _ in ids)
                rows = connection.execute(
                    f"SELECT * FROM relay_envelopes WHERE envelope_id IN ({placeholders}) "
                    "ORDER BY created_at",
                    ids,
                ).fetchall()
        return [self._envelope(row) for row in rows]

    def acknowledge_envelope(
        self, envelope_id: str, *, recipient_ref: str, now: datetime
    ) -> RelayEnvelope:
        now_text = _utc(now)
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE relay_envelopes SET status='acknowledged', acknowledged_at=?
                WHERE envelope_id=? AND recipient_ref=? AND status='delivered' AND expires_at>?
                """,
                (now_text, envelope_id, recipient_ref, now_text),
            )
            if updated.rowcount != 1:
                raise ConflictError("relay envelope is not deliverable for this recipient")
            row = connection.execute(
                "SELECT * FROM relay_envelopes WHERE envelope_id=?", (envelope_id,)
            ).fetchone()
        assert row is not None
        return self._envelope(row)

    def relay_health(self, *, now: datetime) -> dict[str, int | str]:
        now_text = _utc(now)
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE relay_envelopes SET status='expired' "
                "WHERE status IN ('queued','delivered') AND expires_at<=?",
                (now_text,),
            )
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM relay_envelopes GROUP BY status"
            ).fetchall()
        counts = {status.value: 0 for status in RelayEnvelopeStatus}
        counts.update({str(row["status"]): int(row["count"]) for row in rows})
        return {"status": "ok", **counts}

    @staticmethod
    def _host(row: sqlite3.Row) -> HostInstance:
        return HostInstance(
            host_id=row["host_id"],
            display_name=row["display_name"],
            signing_public_key=row["signing_public_key"],
            exchange_public_key=row["exchange_public_key"],
            core_version=row["core_version"],
            protocol_version=row["protocol_version"],
            capabilities=tuple(json.loads(row["capabilities_json"])),
            enabled=bool(row["enabled"]),
            online_state=RemoteHostOnlineState(row["online_state"]),
            last_seen_at=row["last_seen_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _challenge(row: sqlite3.Row) -> PairingChallenge:
        return PairingChallenge(
            challenge_id=row["challenge_id"],
            host_id=row["host_id"],
            code_hash=row["code_hash"],
            allowed_scopes=tuple(
                RemoteScope(value) for value in json.loads(row["allowed_scopes_json"])
            ),
            expires_at=row["expires_at"],
            max_uses=int(row["max_uses"]),
            uses=int(row["uses"]),
            consumed_at=row["consumed_at"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _device(row: sqlite3.Row) -> RemoteDevice:
        return RemoteDevice(
            device_id=row["device_id"],
            host_id=row["host_id"],
            display_name=row["display_name"],
            signing_public_key=row["signing_public_key"],
            exchange_public_key=row["exchange_public_key"],
            scopes=tuple(RemoteScope(value) for value in json.loads(row["scopes_json"])),
            version=int(row["version"]),
            paired_at=row["paired_at"],
            last_seen_at=row["last_seen_at"],
            revoked_at=row["revoked_at"],
        )

    @staticmethod
    def _session(row: sqlite3.Row) -> RemoteSession:
        return RemoteSession(
            remote_session_id=row["remote_session_id"],
            host_id=row["host_id"],
            device_id=row["device_id"],
            transport_mode=RemoteTransportMode(row["transport_mode"]),
            protocol_version=row["protocol_version"],
            event_cursor=int(row["event_cursor"]),
            connection_state=RemoteSessionState(row["connection_state"]),
            session_key_ref=row["session_key_ref"],
            expires_at=row["expires_at"],
            connected_at=row["connected_at"],
            disconnected_at=row["disconnected_at"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _receipt(row: sqlite3.Row) -> RemoteCommandReceipt:
        return RemoteCommandReceipt(
            command_id=row["command_id"],
            idempotency_key=row["idempotency_key"],
            action_hash=row["action_hash"],
            status=RemoteCommandStatus(row["status"]),
            host_acknowledged_at=row["host_acknowledged_at"],
            result_ref=row["result_ref"],
            error_code=row["error_code"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _envelope(row: sqlite3.Row) -> RelayEnvelope:
        return RelayEnvelope(
            envelope_id=row["envelope_id"],
            route_ref=row["route_ref"],
            sender_ref=row["sender_ref"],
            recipient_ref=row["recipient_ref"],
            protocol_version=row["protocol_version"],
            ciphertext=row["ciphertext"],
            nonce=row["nonce"],
            expires_at=row["expires_at"],
            status=RelayEnvelopeStatus(row["status"]),
            created_at=row["created_at"],
            delivered_at=row["delivered_at"],
            acknowledged_at=row["acknowledged_at"],
        )
