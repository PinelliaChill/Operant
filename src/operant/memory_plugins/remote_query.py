"""Authenticated, ephemeral B2-6 Remote Memory Query responses."""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from fastapi import FastAPI, HTTPException, Response

from operant.contracts.b2_6_query import (
    MAX_B26_REMOTE_QUERY_CIPHERTEXT_CHARS,
    B26EncryptedReply,
    reply_metadata,
)
from operant.domain.remote_control import (
    EncryptedRemoteCommand,
    RemoteCommandStatus,
    RemoteSession,
)
from operant.remote_control.crypto import RemoteCrypto
from operant.remote_control.runtime import (
    RemoteActionPayload,
    RemoteControlError,
    RemoteControlService,
)

MAX_B26_REMOTE_QUERY_PROJECTION_BYTES = 256_000
MAX_B26_REMOTE_QUERY_ARGUMENTS: frozenset[str] = frozenset()


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _projection_payload(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return dict(value)
    raise RemoteControlError(
        "remote.query_projection_invalid",
        "B2-6 projection is not a serializable mapping",
        status_code=500,
    )


def _validate_query_arguments(payload: RemoteActionPayload) -> None:
    if payload.secret_refs or payload.workspace is not None:
        raise RemoteControlError(
            "remote.query_payload_invalid",
            "memory query cannot carry secrets or a workspace override",
            status_code=403,
        )
    unknown = set(payload.arguments) - MAX_B26_REMOTE_QUERY_ARGUMENTS
    if unknown:
        raise RemoteControlError(
            "remote.query_arguments_invalid",
            "memory query contains unsupported arguments",
            status_code=403,
        )
    query = payload.arguments.get("query")
    if query is not None and (not isinstance(query, str) or not 1 <= len(query) <= 1_000):
        raise RemoteControlError(
            "remote.query_arguments_invalid",
            "memory query text is invalid",
            status_code=403,
        )
    for key in ("limit", "after_cursor"):
        value = payload.arguments.get(key)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise RemoteControlError(
                "remote.query_arguments_invalid",
                "memory query cursor or limit is invalid",
                status_code=403,
            )
    limit = payload.arguments.get("limit")
    if isinstance(limit, int) and limit > 100:
        raise RemoteControlError(
            "remote.query_arguments_invalid",
            "memory query limit is too large",
            status_code=403,
        )


def _preflight_query(
    remote_service: RemoteControlService,
    command: EncryptedRemoteCommand,
) -> tuple[RemoteActionPayload, RemoteSession, datetime]:
    """Authenticate and allowlist the command before invoking submit_command."""

    now = _utc_now()
    remote_service._validate_command_time(command, now)
    host = remote_service._active_host(command.host_id)
    device = remote_service._active_device(command.device_id, host_id=command.host_id)
    session = remote_service._active_session(command.remote_session_id, at=now)
    if (
        session.host_id != host.host_id
        or session.device_id != device.device_id
        or session.protocol_version != command.protocol_version
        or host.protocol_version != command.protocol_version
    ):
        raise RemoteControlError(
            "remote.command_binding_invalid",
            "remote query binding is invalid",
            status_code=403,
        )
    try:
        RemoteCrypto.verify_command(command, device.signing_public_key)
        payload_data = RemoteCrypto.decrypt_command(
            command,
            remote_service.key_store.get(session.session_key_ref),
        )
        payload = RemoteActionPayload.model_validate(payload_data)
    except (InvalidSignature, InvalidTag, ValueError, KeyError) as exc:
        raise RemoteControlError(
            "remote.command_authentication_failed",
            "remote query authentication failed",
            status_code=403,
        ) from exc
    if payload.tool != "memory" or payload.operation != "query":
        raise RemoteControlError(
            "remote.query_operation_required",
            "B2-6 Remote Query accepts only memory/query",
            status_code=403,
        )
    expected = remote_service.operation_capabilities.get(("memory", "query"))
    if expected is None:
        raise RemoteControlError(
            "remote.operation_not_registered",
            "memory query is not registered in Remote Control",
            status_code=403,
        )
    remote_service._require_scope(device, payload, remote_service.operation_capabilities)
    _validate_query_arguments(payload)
    return payload, session, now


def _reply(
    *,
    command: EncryptedRemoteCommand,
    session: RemoteSession,
    session_key: bytes,
    projection: Any,
    issued_at: datetime,
) -> B26EncryptedReply:
    body = _canonical(_projection_payload(projection))
    if len(body) > MAX_B26_REMOTE_QUERY_PROJECTION_BYTES:
        raise RemoteControlError(
            "remote.query_projection_too_large",
            "B2-6 projection exceeds the encrypted response limit",
            status_code=413,
        )
    if len(session_key) != 32:
        raise RemoteControlError(
            "remote.reply_key_invalid",
            "remote session key is invalid",
            status_code=500,
        )
    expires_at = min(
        command.expires_at.astimezone(timezone.utc),
        issued_at + timedelta(seconds=60),
    )
    if expires_at <= issued_at:
        raise RemoteControlError(
            "remote.command_expired",
            "remote query expired before its response was created",
            status_code=409,
        )
    nonce = os.urandom(12)
    provisional = B26EncryptedReply(
        command_id=command.command_id,
        host_id=session.host_id,
        device_id=session.device_id,
        remote_session_id=session.remote_session_id,
        issued_at=issued_at,
        expires_at=expires_at,
        nonce=_encode(nonce),
        ciphertext="pending",
    )
    ciphertext = ChaCha20Poly1305(session_key).encrypt(
        nonce,
        body,
        _canonical(reply_metadata(provisional)),
    )
    encoded = _encode(ciphertext)
    if len(encoded) > MAX_B26_REMOTE_QUERY_CIPHERTEXT_CHARS:
        raise RemoteControlError(
            "remote.query_projection_too_large",
            "B2-6 encrypted response exceeds the transport limit",
            status_code=413,
        )
    return provisional.model_copy(update={"ciphertext": encoded})


def decrypt_reply(
    reply: B26EncryptedReply,
    session_key: bytes,
    *,
    now: datetime | None = None,
) -> Any:
    """Decrypt and authenticate a reply using the paired session key."""

    at = (now or _utc_now()).astimezone(timezone.utc)
    if reply.expires_at <= at:
        raise ValueError("encrypted reply is expired")
    if len(session_key) != 32:
        raise ValueError("remote session key must be 32 bytes")
    plaintext = ChaCha20Poly1305(session_key).decrypt(
        _decode(reply.nonce),
        _decode(reply.ciphertext),
        _canonical(reply_metadata(reply)),
    )
    return json.loads(plaintext)


def install_remote_memory_query(
    app: FastAPI,
    remote_service: RemoteControlService,
    projection_callable: Callable[[str], Any],
) -> None:
    """Install the B2-6 encrypted query route after Remote Control setup."""

    @app.post(
        "/v1/b2-6/remote-query",
        response_model=B26EncryptedReply,
        operation_id="getB26RemoteQuery",
    )
    def remote_query(
        command: EncryptedRemoteCommand,
        response: Response,
    ) -> B26EncryptedReply:
        try:
            payload, _session, _preflight_at = _preflight_query(remote_service, command)
            receipt = remote_service.submit_command(command)
            if receipt.status is not RemoteCommandStatus.COMPLETED:
                raise RemoteControlError(
                    "remote.query_not_completed",
                    "Remote Control did not complete the authenticated query",
                    status_code=409,
                )
            current_projection = projection_callable(payload.target_id)
            # Re-read the binding after HostAck and projection generation.  A device revocation or
            # session close during projection generation must fail closed.
            now = _utc_now()
            host = remote_service._active_host(command.host_id)
            device = remote_service._active_device(command.device_id, host_id=command.host_id)
            session = remote_service._active_session(command.remote_session_id, at=now)
            if (
                session.host_id != host.host_id
                or session.device_id != device.device_id
                or session.protocol_version != command.protocol_version
                or host.protocol_version != command.protocol_version
            ):
                raise RemoteControlError(
                    "remote.command_binding_invalid",
                    "remote query binding changed before response",
                    status_code=403,
                )
            remote_service._require_scope(device, payload, remote_service.operation_capabilities)
            session_key = remote_service.key_store.get(session.session_key_ref)
            reply = _reply(
                command=command,
                session=session,
                session_key=session_key,
                projection=current_projection,
                issued_at=now,
            )
            response.headers["Cache-Control"] = "no-store"
            response.headers["x-operant-sensitive-response"] = "ephemeral-encrypted"
            return reply
        except RemoteControlError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc


__all__ = [
    "MAX_B26_REMOTE_QUERY_ARGUMENTS",
    "MAX_B26_REMOTE_QUERY_PROJECTION_BYTES",
    "decrypt_reply",
    "install_remote_memory_query",
]
