"""Atomic, private SQLite state for caller-pairing.v1."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from operant.persistence.sqlite import SQLiteStore


class CallerStateError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class CallerPairingRepository:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def create_challenge(self, record: Mapping[str, Any]) -> None:
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO caller_pairing_challenges"
                "(ticket_id,core_epoch_id,code_hash,base_url,expires_at) VALUES (?,?,?,?,?)",
                (
                    record["ticket_id"],
                    record["core_epoch_id"],
                    record["code_hash"],
                    record["base_url"],
                    record["expires_at"],
                ),
            )

    def get_challenge(self, ticket_id: str) -> dict[str, Any] | None:
        with self.store._connect() as db:
            return _row(
                db.execute(
                    "SELECT * FROM caller_pairing_challenges WHERE ticket_id=?", (ticket_id,)
                ).fetchone()
            )

    def complete_pair(
        self,
        *,
        ticket_id: str,
        pair_request_id: str,
        pair_request_hash: str,
        nonce: str,
        nonce_expires_at: int,
        device: Mapping[str, Any],
        reply_json: str,
        now: int,
    ) -> str:
        """Consume ticket and issue device together; exact replay recovers reply."""
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            ticket = db.execute(
                "SELECT * FROM caller_pairing_challenges WHERE ticket_id=?", (ticket_id,)
            ).fetchone()
            if ticket is None or ticket["core_epoch_id"] != device["core_epoch_id"]:
                raise CallerStateError("ticket_unavailable")
            if ticket["consumed_at"] is not None:
                if (
                    ticket["pair_request_id"] == pair_request_id
                    and ticket["pair_request_hash"] == pair_request_hash
                ):
                    current = db.execute(
                        "SELECT revoked_at FROM caller_pairing_devices WHERE device_id=?",
                        (ticket["device_id"],),
                    ).fetchone()
                    if current is None or current["revoked_at"] is not None:
                        raise CallerStateError("device_rejected")
                    return str(ticket["pair_reply_json"])
                raise CallerStateError("ticket_consumed")
            if int(ticket["expires_at"]) <= now:
                raise CallerStateError("ticket_expired")
            prior = db.execute(
                "SELECT * FROM caller_pairing_devices WHERE device_id=?", (device["device_id"],)
            ).fetchone()
            if prior is not None and (
                prior["revoked_at"] is not None
                or prior["signing_public_key"] != device["signing_public_key"]
            ):
                raise CallerStateError("device_rejected")
            db.execute(
                """INSERT INTO caller_pairing_devices(
                device_id,display_name,signing_public_key,exchange_public_key,
                signing_key_fingerprint,core_epoch_id,scope,expires_at,created_at,revoked_at)
                VALUES (?,?,?,?,?,?,?,?,?,NULL)
                ON CONFLICT(device_id) DO UPDATE SET display_name=excluded.display_name,
                exchange_public_key=excluded.exchange_public_key,
                core_epoch_id=excluded.core_epoch_id,
                expires_at=excluded.expires_at""",
                (
                    device["device_id"],
                    device["display_name"],
                    device["signing_public_key"],
                    device["exchange_public_key"],
                    device["signing_key_fingerprint"],
                    device["core_epoch_id"],
                    device["scope"],
                    device["expires_at"],
                    now,
                ),
            )
            try:
                db.execute(
                    "INSERT INTO caller_pairing_nonces"
                    "(device_id,core_epoch_id,nonce,request_hash,expires_at) VALUES (?,?,?,?,?)",
                    (
                        device["device_id"],
                        device["core_epoch_id"],
                        nonce,
                        pair_request_hash,
                        nonce_expires_at,
                    ),
                )
                changed = db.execute(
                    """UPDATE caller_pairing_challenges SET
                    consumed_at=?,pair_request_id=?,pair_request_hash=?,
                    device_id=?,pair_reply_json=? WHERE ticket_id=? AND consumed_at IS NULL""",
                    (
                        now,
                        pair_request_id,
                        pair_request_hash,
                        device["device_id"],
                        reply_json,
                        ticket_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise CallerStateError("pair_request_replayed") from exc
            if changed.rowcount != 1:
                raise CallerStateError("ticket_consumed")
            return reply_json

    def get_device(self, device_id: str) -> dict[str, Any] | None:
        with self.store._connect() as db:
            return _row(
                db.execute(
                    "SELECT * FROM caller_pairing_devices WHERE device_id=?", (device_id,)
                ).fetchone()
            )

    def list_devices(self) -> list[dict[str, Any]]:
        with self.store._connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM caller_pairing_devices ORDER BY created_at,device_id"
                )
            ]

    def revoke_device(self, device_id: str, now: int) -> bool:
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE caller_pairing_devices SET revoked_at=? "
                "WHERE device_id=? AND revoked_at IS NULL",
                (now, device_id),
            )
            return changed.rowcount == 1

    def reserve_command(
        self,
        *,
        device_id: str,
        epoch_id: str,
        nonce: str,
        envelope_hash: str,
        request_id: str,
        operation: str,
        payload_hash: str,
        original_args: Mapping[str, Any],
        now: int,
        expires_at: int,
    ) -> tuple[dict[str, Any], bool]:
        """Replay prevention and receipt reservation share one write transaction."""
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            device = db.execute(
                "SELECT * FROM caller_pairing_devices WHERE device_id=?", (device_id,)
            ).fetchone()
            if (
                device is None
                or device["revoked_at"] is not None
                or device["core_epoch_id"] != epoch_id
                or int(device["expires_at"]) <= now
            ):
                raise CallerStateError("device_unavailable")
            try:
                db.execute(
                    "INSERT INTO caller_pairing_nonces"
                    "(device_id,core_epoch_id,nonce,request_hash,expires_at) VALUES (?,?,?,?,?)",
                    (device_id, epoch_id, nonce, envelope_hash, expires_at),
                )
            except sqlite3.IntegrityError as exc:
                raise CallerStateError("nonce_replayed") from exc
            prior = db.execute(
                "SELECT * FROM caller_pairing_receipts WHERE device_id=? AND request_id=?",
                (device_id, request_id),
            ).fetchone()
            if prior is not None:
                if prior["operation"] != operation or prior["payload_hash"] != payload_hash:
                    raise CallerStateError("request_identity_changed")
                return dict(prior), False
            db.execute(
                """INSERT INTO caller_pairing_receipts(
                device_id,request_id,operation,payload_hash,state,http_status,
                original_args_json,core_epoch_id,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    device_id,
                    request_id,
                    operation,
                    payload_hash,
                    "in_progress",
                    202,
                    json.dumps(original_args, sort_keys=True, separators=(",", ":")),
                    epoch_id,
                    now,
                    now,
                ),
            )
            result = db.execute(
                "SELECT * FROM caller_pairing_receipts WHERE device_id=? AND request_id=?",
                (device_id, request_id),
            ).fetchone()
            assert result is not None
            return dict(result), True

    def reserve_nonce(
        self,
        device_id: str,
        epoch_id: str,
        nonce: str,
        envelope_hash: str,
        expires_at: int,
        now: int,
    ) -> None:
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            device = db.execute(
                "SELECT * FROM caller_pairing_devices WHERE device_id=?", (device_id,)
            ).fetchone()
            if (
                device is None
                or device["revoked_at"] is not None
                or device["core_epoch_id"] != epoch_id
                or int(device["expires_at"]) <= now
            ):
                raise CallerStateError("device_unavailable")
            try:
                db.execute(
                    "INSERT INTO caller_pairing_nonces VALUES (?,?,?,?,?)",
                    (device_id, epoch_id, nonce, envelope_hash, expires_at),
                )
            except sqlite3.IntegrityError as exc:
                raise CallerStateError("nonce_replayed") from exc

    def get_receipt(self, device_id: str, request_id: str) -> dict[str, Any] | None:
        with self.store._connect() as db:
            return _row(
                db.execute(
                    "SELECT * FROM caller_pairing_receipts WHERE device_id=? AND request_id=?",
                    (device_id, request_id),
                ).fetchone()
            )

    def set_receipt(
        self,
        device_id: str,
        request_id: str,
        *,
        state: str,
        http_status: int,
        result: Mapping[str, Any] | None = None,
        approval_id: str | None = None,
        action_hash: str | None = None,
        error_code: str | None = None,
        now: int,
        expected_state: str = "in_progress",
    ) -> dict[str, Any]:
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                """UPDATE caller_pairing_receipts SET
                state=?,http_status=?,result_json=?,approval_id=?,
                action_hash=?,error_code=?,updated_at=?
                WHERE device_id=? AND request_id=? AND state=?""",
                (
                    state,
                    http_status,
                    json.dumps(result, sort_keys=True, separators=(",", ":"))
                    if result is not None
                    else None,
                    approval_id,
                    action_hash,
                    error_code,
                    now,
                    device_id,
                    request_id,
                    expected_state,
                ),
            )
            if changed.rowcount != 1:
                raise CallerStateError("receipt_state_changed")
            row = db.execute(
                "SELECT * FROM caller_pairing_receipts WHERE device_id=? AND request_id=?",
                (device_id, request_id),
            ).fetchone()
            assert row is not None
            return dict(row)
