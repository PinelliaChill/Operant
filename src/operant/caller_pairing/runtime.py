"""Scoped caller pairing, authenticated dispatch and crash-safe receipts."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any, Literal, cast

from pydantic import ValidationError

from operant.application.skill_sources import SkillSourceEffects, SkillSourceOperationError
from operant.caller_pairing.crypto import (
    COMMAND_PATH,
    PAIR_PATH,
    READBACK_PATH,
    CallerCryptoError,
    CoreKeys,
    b64,
    canonical_bytes,
    decrypt,
    derive_key,
    encrypt,
    new_id,
    request_hash,
    sign,
    unb64,
    verify,
)
from operant.caller_pairing.crypto import (
    device_id as derived_device_id,
)
from operant.caller_pairing.persistence import CallerPairingRepository, CallerStateError
from operant.contracts.caller_pairing import (
    CALLER_PAIRING_PROTOCOL,
    SKILL_SOURCE_SCOPE,
    CallerAddPayload,
    CallerCommand,
    CallerContinuePayload,
    CallerDeviceList,
    CallerDeviceView,
    CallerEncryptedReply,
    CallerListPayload,
    CallerPairingTicket,
    CallerPairPayload,
    CallerPairRequest,
    CallerReadbackPayload,
    CallerReceiptView,
    CallerRemovePayload,
)
from operant.persistence.phase45 import SQLitePhase45Repository
from operant.persistence.sqlite import SQLiteStore


class CallerPairingError(ValueError):
    def __init__(self, code: str, status_code: int = 403) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def _safe_json(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    data = json.loads(value)
    return data if isinstance(data, dict) else None


class CallerPairingRuntime:
    def __init__(
        self,
        store: SQLiteStore,
        *,
        effects_for_device: Callable[[str], SkillSourceEffects],
        clock: Callable[[], float] = time.time,
        keys: CoreKeys | None = None,
    ) -> None:
        self.repo = CallerPairingRepository(store)
        self.phase_repo = SQLitePhase45Repository(store)
        self.effects_for_device = effects_for_device
        self.clock = clock
        self.keys = keys or CoreKeys()
        self._active_lock = threading.Lock()
        self._active_requests: set[tuple[str, str]] = set()
        # Fixed-size stripes avoid an attacker-controlled lock map. A revoke
        # waits for the current command, then all later commands see revocation.
        self._device_locks = tuple(threading.RLock() for _ in range(64))

    def _device_lock(self, device_id: str) -> threading.RLock:
        return self._device_locks[int(hashlib.sha256(device_id.encode()).hexdigest()[:8], 16) % 64]

    def _now(self) -> int:
        return int(self.clock())

    def challenge(self, *, base_url: str, ttl_seconds: int) -> CallerPairingTicket:
        if not 30 <= ttl_seconds <= 300:
            raise CallerPairingError("invalid_ticket_ttl", 422)
        now = self._now()
        code = b64(secrets.token_bytes(32))
        ticket_id = new_id("ticket")
        ticket = CallerPairingTicket(
            ticket_id=ticket_id,
            core_epoch_id=self.keys.epoch_id,
            base_url=base_url,
            core_exchange_public_key=self.keys.exchange_public,
            core_signing_public_key=self.keys.signing_public,
            expires_at=now + ttl_seconds,
            one_time_code=code,
        )
        self.repo.create_challenge(
            {
                "ticket_id": ticket_id,
                "core_epoch_id": self.keys.epoch_id,
                "code_hash": hashlib.sha256(code.encode("ascii")).hexdigest(),
                "base_url": base_url,
                "expires_at": ticket.expires_at,
            }
        )
        return ticket

    def devices(self) -> CallerDeviceList:
        now = self._now()
        items = []
        for row in self.repo.list_devices():
            state = (
                "revoked"
                if row["revoked_at"] is not None
                else "expired"
                if int(row["expires_at"]) < now
                else "needs_pairing"
                if row["core_epoch_id"] != self.keys.epoch_id
                else "active"
            )
            items.append(
                CallerDeviceView(
                    device_id=row["device_id"],
                    display_name=row["display_name"],
                    signing_key_fingerprint=row["signing_key_fingerprint"],
                    core_epoch_id=row["core_epoch_id"],
                    scope=SKILL_SOURCE_SCOPE,
                    expires_at=row["expires_at"],
                    state=cast(Literal["active", "expired", "revoked", "needs_pairing"], state),
                )
            )
        return CallerDeviceList(items=items)

    def revoke(self, device_id: str) -> bool:
        with self._device_lock(device_id):
            return self.repo.revoke_device(device_id, self._now())

    def pair(self, body: CallerPairRequest) -> CallerEncryptedReply:
        try:
            with self._device_lock(body.device_id):
                return self._pair(body)
        except (CallerCryptoError, CallerStateError, ValidationError) as exc:
            raise CallerPairingError("pairing_rejected") from exc

    def _pair(self, body: CallerPairRequest) -> CallerEncryptedReply:
        now = self._now()
        if body.core_epoch_id != self.keys.epoch_id or body.scope != SKILL_SOURCE_SCOPE:
            raise CallerStateError("epoch_or_scope_changed")
        if derived_device_id(body.device_signing_public_key) != body.device_id:
            raise CallerCryptoError("invalid identity")
        if (
            body.expires_at <= now
            or body.expires_at <= body.issued_at
            or body.expires_at > body.issued_at + 60
            or abs(body.issued_at - now) > 30
        ):
            raise CallerStateError("request_expired")
        envelope = body.model_dump()
        verify(envelope, body.device_signing_public_key, path=PAIR_PATH)
        digest = request_hash(envelope, path=PAIR_PATH)
        ticket = self.repo.get_challenge(body.ticket_id)
        if ticket is None or ticket["core_epoch_id"] != self.keys.epoch_id:
            raise CallerStateError("ticket_unavailable")
        if ticket["consumed_at"] is not None:
            if (
                ticket["pair_request_id"] == body.pair_request_id
                and ticket["pair_request_hash"] == digest
            ):
                current = self.repo.get_device(body.device_id)
                if current is None or current["revoked_at"] is not None:
                    raise CallerStateError("device_rejected")
                return CallerEncryptedReply.model_validate_json(ticket["pair_reply_json"])
            raise CallerStateError("ticket_consumed")
        if int(ticket["expires_at"]) <= now:
            raise CallerStateError("ticket_expired")
        key = derive_key(
            self.keys.exchange,
            body.device_exchange_public_key,
            epoch_id=self.keys.epoch_id,
            device_id_value=body.device_id,
            purpose="pair-request",
        )
        payload = CallerPairPayload.model_validate(decrypt(envelope, key, path=PAIR_PATH))
        if not hmac.compare_digest(
            str(ticket["code_hash"]),
            hashlib.sha256(payload.one_time_code.encode("ascii")).hexdigest(),
        ):
            raise CallerStateError("invalid_code")
        expires_at = min(now + 8 * 3600, 2**53 - 1)
        reply = self._reply(
            device_id=body.device_id,
            request_id=body.pair_request_id,
            operation="pair",
            request_digest=digest,
            peer_exchange=body.device_exchange_public_key,
            plaintext={
                "device_id": body.device_id,
                "scope": SKILL_SOURCE_SCOPE,
                "core_epoch_id": self.keys.epoch_id,
                "expires_at": expires_at,
            },
            purpose="pair-reply",
        )
        saved = self.repo.complete_pair(
            ticket_id=body.ticket_id,
            pair_request_id=body.pair_request_id,
            pair_request_hash=digest,
            nonce=body.nonce,
            nonce_expires_at=body.expires_at,
            device={
                "device_id": body.device_id,
                "display_name": payload.display_name,
                "signing_public_key": body.device_signing_public_key,
                "exchange_public_key": body.device_exchange_public_key,
                "signing_key_fingerprint": hashlib.sha256(
                    unb64(body.device_signing_public_key, length=65)
                ).hexdigest(),
                "core_epoch_id": self.keys.epoch_id,
                "scope": SKILL_SOURCE_SCOPE,
                "expires_at": expires_at,
            },
            reply_json=reply.model_dump_json(),
            now=now,
        )
        return CallerEncryptedReply.model_validate_json(saved)

    def _reply(
        self,
        *,
        device_id: str,
        request_id: str,
        operation: str,
        request_digest: str,
        peer_exchange: str,
        plaintext: Mapping[str, Any],
        purpose: str,
    ) -> CallerEncryptedReply:
        now = self._now()
        envelope: dict[str, Any] = {
            "protocol_version": CALLER_PAIRING_PROTOCOL,
            "core_epoch_id": self.keys.epoch_id,
            "device_id": device_id,
            "request_id": request_id,
            "operation": operation,
            "request_hash": request_digest,
            "issued_at": now,
            "expires_at": now + 60,
            "nonce": b64(secrets.token_bytes(12)),
        }
        key = derive_key(
            self.keys.exchange,
            peer_exchange,
            epoch_id=self.keys.epoch_id,
            device_id_value=device_id,
            purpose=purpose,
        )
        path = (
            PAIR_PATH
            if operation == "pair"
            else (READBACK_PATH if operation == "readback" else COMMAND_PATH)
        )
        envelope["ciphertext"] = encrypt(plaintext, envelope, key, path=path)
        envelope["signature"] = sign(envelope, self.keys.signing, path=path)
        return CallerEncryptedReply.model_validate(envelope)

    def command(self, body: CallerCommand, *, path: str = COMMAND_PATH) -> CallerEncryptedReply:
        try:
            with self._device_lock(body.device_id):
                return self._command(body, path=path)
        except (CallerCryptoError, CallerStateError, ValidationError) as exc:
            raise CallerPairingError(
                exc.code if isinstance(exc, CallerStateError) else "command_rejected",
                409 if isinstance(exc, CallerStateError) else 403,
            ) from exc

    def _command(self, body: CallerCommand, *, path: str) -> CallerEncryptedReply:
        now = self._now()
        if path not in {COMMAND_PATH, READBACK_PATH} or (
            (path == READBACK_PATH) != (body.operation == "readback")
        ):
            raise CallerStateError("operation_rejected")
        if body.core_epoch_id != self.keys.epoch_id:
            raise CallerStateError("epoch_changed")
        if (
            body.expires_at <= now
            or body.expires_at <= body.issued_at
            or body.expires_at > body.issued_at + 60
            or abs(body.issued_at - now) > 30
        ):
            raise CallerStateError("request_expired")
        device = self.repo.get_device(body.device_id)
        if (
            device is None
            or device["revoked_at"] is not None
            or device["core_epoch_id"] != self.keys.epoch_id
            or int(device["expires_at"]) <= now
        ):
            raise CallerStateError("device_unavailable")
        envelope = body.model_dump()
        verify(envelope, str(device["signing_public_key"]), path=path)
        digest = request_hash(envelope, path=path)
        key = derive_key(
            self.keys.exchange,
            str(device["exchange_public_key"]),
            epoch_id=self.keys.epoch_id,
            device_id_value=body.device_id,
            purpose="command-request",
        )
        plaintext = decrypt(envelope, key, path=path)
        payload: Any
        if body.operation == "list":
            payload = CallerListPayload.model_validate(plaintext)
        elif body.operation == "add":
            payload = CallerAddPayload.model_validate(plaintext)
        elif body.operation == "remove":
            payload = CallerRemovePayload.model_validate(plaintext)
        elif body.operation == "readback":
            payload = CallerReadbackPayload.model_validate(plaintext)
        else:
            payload = CallerContinuePayload.model_validate(plaintext)
        if body.operation in {"readback", "continue"}:
            self.repo.reserve_nonce(
                body.device_id, self.keys.epoch_id, body.nonce, digest, body.expires_at, now
            )
            receipt = self._readback(body.device_id, payload.original_request_id)
            if body.operation == "continue":
                assert isinstance(payload, CallerContinuePayload)
                receipt = self._continue(body.device_id, receipt, payload)
        else:
            args = payload.model_dump()
            payload_hash = hashlib.sha256(
                canonical_bytes({"operation": body.operation, "args": args})
            ).hexdigest()
            receipt, created = self.repo.reserve_command(
                device_id=body.device_id,
                epoch_id=self.keys.epoch_id,
                nonce=body.nonce,
                envelope_hash=digest,
                request_id=body.request_id,
                operation=body.operation,
                payload_hash=payload_hash,
                original_args=args,
                now=now,
                expires_at=body.expires_at,
            )
            if created:
                receipt = self._run(body.device_id, receipt)
        view = self._view(receipt)
        return self._reply(
            device_id=body.device_id,
            request_id=body.request_id,
            operation=body.operation,
            request_digest=digest,
            peer_exchange=str(device["exchange_public_key"]),
            plaintext={"receipt": view.model_dump()},
            purpose="command-reply",
        )

    def _readback(self, device_id: str, request_id: str) -> dict[str, Any]:
        receipt = self.repo.get_receipt(device_id, request_id)
        if receipt is None:
            raise CallerStateError("receipt_unavailable")
        return receipt

    def native_readback(self, device_id: str, request_id: str) -> CallerReceiptView:
        return self._view(self._readback(device_id, request_id))

    def _view(self, row: Mapping[str, Any]) -> CallerReceiptView:
        state = row["state"]
        if state == "in_progress":
            with self._active_lock:
                if (str(row["device_id"]), str(row["request_id"])) not in self._active_requests:
                    state = "unconfirmed"
        return CallerReceiptView(
            request_id=row["request_id"],
            device_id=row["device_id"],
            operation=row["operation"],
            payload_hash=row["payload_hash"],
            state=state,
            http_status=row["http_status"],
            result=_safe_json(row["result_json"]),
            approval_id=row["approval_id"],
            action_hash=row["action_hash"],
            error_code=row["error_code"] if state != "unconfirmed" else "manual_reconcile_required",
        )

    def _continue(
        self, device_id: str, receipt: dict[str, Any], payload: CallerContinuePayload
    ) -> dict[str, Any]:
        if (
            receipt["state"] != "awaiting_approval"
            or receipt["payload_hash"] != payload.payload_hash
            or receipt["approval_id"] != payload.approval_id
            or receipt["action_hash"] != payload.action_hash
        ):
            raise CallerStateError("approval_binding_changed")
        approval = self.phase_repo.get_phase45_approval(payload.approval_id)
        if approval["action_hash"] != payload.action_hash or approval["status"] != "approved":
            raise CallerStateError("approval_not_granted")
        claimed = self.repo.set_receipt(
            device_id,
            receipt["request_id"],
            state="in_progress",
            http_status=202,
            now=self._now(),
            expected_state="awaiting_approval",
        )
        return self._run(device_id, claimed)

    def _run(self, device_id: str, receipt: Mapping[str, Any]) -> dict[str, Any]:
        request_id = str(receipt["request_id"])
        with self._active_lock:
            self._active_requests.add((device_id, request_id))
        try:
            return self._run_active(device_id, receipt)
        finally:
            with self._active_lock:
                self._active_requests.discard((device_id, request_id))

    def _run_active(self, device_id: str, receipt: Mapping[str, Any]) -> dict[str, Any]:
        request_id = str(receipt["request_id"])
        op = str(receipt["operation"])
        args = _safe_json(receipt["original_args_json"])
        if args is None:
            raise CallerStateError("receipt_unavailable")
        # Separate from native and from every other paired caller's command key.
        idem_key = (
            "caller-pairing.v1:"
            + hashlib.sha256(f"{device_id}\x00{request_id}".encode()).hexdigest()
        )
        result: Any
        try:
            effects = self.effects_for_device(device_id)
            if op == "list":
                result = effects.list_sources()
            elif op == "add":
                result = effects.add_source(args["path"], idem_key)
            elif op == "remove":
                result = effects.remove_source(args["root_ref"], idem_key)
            else:
                raise CallerStateError("operation_rejected")
        except SkillSourceOperationError as exc:
            detail = exc.detail
            code = detail.get("code") if isinstance(detail, dict) else None
            if code == "approval_required":
                approval_id = detail.get("approval_id") if isinstance(detail, dict) else None
                if not isinstance(approval_id, str):
                    raise CallerStateError("approval_unavailable") from exc
                approval = self.phase_repo.get_phase45_approval(approval_id)
                return self.repo.set_receipt(
                    device_id,
                    request_id,
                    state="awaiting_approval",
                    http_status=409,
                    approval_id=approval_id,
                    action_hash=approval["action_hash"],
                    now=self._now(),
                )
            if code == "command_outcome_unknown":
                return self.repo.set_receipt(
                    device_id,
                    request_id,
                    state="unconfirmed",
                    http_status=409,
                    error_code="manual_reconcile_required",
                    now=self._now(),
                )
            return self.repo.set_receipt(
                device_id,
                request_id,
                state="failed",
                http_status=exc.status_code,
                error_code="skill_source_rejected",
                now=self._now(),
            )
        except (OSError, ValueError):
            # Settings may already have changed when discovery fails. A general
            # OS/validation exception is not proof of no effect.
            return self.repo.set_receipt(
                device_id,
                request_id,
                state="unconfirmed",
                http_status=409,
                error_code="manual_reconcile_required",
                now=self._now(),
            )
        except Exception:
            # The effect may have applied; the durable reservation must stay unknown.
            return self.repo.set_receipt(
                device_id,
                request_id,
                state="unconfirmed",
                http_status=409,
                error_code="manual_reconcile_required",
                now=self._now(),
            )
        return self.repo.set_receipt(
            device_id,
            request_id,
            state="completed",
            http_status=200,
            result=result.model_dump(mode="json"),
            now=self._now(),
        )
