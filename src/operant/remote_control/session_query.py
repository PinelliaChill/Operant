"""Bounded encrypted Session result readback for the owning paired device."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from cryptography.exceptions import InvalidSignature, InvalidTag
from fastapi import FastAPI, HTTPException, Response

from operant.application.service import ApplicationService
from operant.contracts.b2_6_query import B26EncryptedReply
from operant.domain.remote_control import EncryptedRemoteCommand, RemoteCommandStatus
from operant.memory_plugins.remote_query import _reply
from operant.protocol import redact_public_text
from operant.remote_control.crypto import RemoteCrypto
from operant.remote_control.runtime import (
    RemoteActionPayload,
    RemoteControlError,
    RemoteControlService,
)
from operant.remote_control.session_executor import _bound_workspace

_RESULT_EVENTS = frozenset(
    {
        "agent.completed",
        "agent.failed",
        "agent.cancelled",
        "agent.timed_out",
        "agent.stream_error",
        "session.run_failed",
    }
)


def install_remote_session_query(
    app: FastAPI, service: ApplicationService, remote: RemoteControlService
) -> None:
    @app.post(
        "/v1/remote-control/session-query",
        response_model=B26EncryptedReply,
        operation_id="queryRemoteSessionResult",
    )
    def query_remote_session_result(
        command: EncryptedRemoteCommand, response: Response
    ) -> B26EncryptedReply:
        try:
            at = datetime.now(timezone.utc)
            remote._validate_command_time(command, at)
            host = remote._active_host(command.host_id)
            device = remote._active_device(command.device_id, host_id=command.host_id)
            session = remote._active_session(command.remote_session_id, at=at)
            if (
                session.host_id != host.host_id
                or session.device_id != device.device_id
                or session.protocol_version != command.protocol_version
                or host.protocol_version != command.protocol_version
            ):
                raise RemoteControlError(
                    "remote.session_binding_invalid", "Session binding changed", status_code=403
                )
            session_key = remote.key_store.get(session.session_key_ref)
            try:
                RemoteCrypto.verify_command(command, device.signing_public_key)
                payload = RemoteActionPayload.model_validate(
                    RemoteCrypto.decrypt_command(command, session_key)
                )
            except (InvalidSignature, InvalidTag, ValueError, KeyError) as exc:
                raise RemoteControlError(
                    "remote.command_authentication_failed",
                    "Command authentication failed",
                    status_code=403,
                ) from exc
            if (
                payload.tool != "session"
                or payload.operation != "status"
                or payload.arguments
                or payload.workspace is not None
                or payload.secret_refs
            ):
                raise RemoteControlError(
                    "remote.query_operation_required",
                    "Only Session status is queryable",
                    status_code=403,
                )
            remote._require_scope(device, payload, remote.operation_capabilities)
            _bound_workspace(service, payload.target_id, f"remote-device:{device.device_id}")
            receipt = remote.submit_command(command)
            if receipt.status is not RemoteCommandStatus.COMPLETED:
                raise RemoteControlError(
                    "remote.query_not_completed", "Session query did not complete", status_code=409
                )
            with service.store._connect() as connection:
                latest = connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) FROM events WHERE session_id=?",
                    (payload.target_id,),
                ).fetchone()
                events = connection.execute(
                    "SELECT sequence,event_type,body FROM events WHERE session_id=? "
                    "AND event_type IN (?,?,?,?,?,?) ORDER BY sequence DESC LIMIT 20",
                    (payload.target_id, *_RESULT_EVENTS),
                ).fetchall()
            summaries: list[dict[str, Any]] = []
            for event in reversed(events):
                item: dict[str, Any] = {
                    "event_type": event["event_type"],
                    "cursor": event["sequence"],
                }
                if event["event_type"] == "agent.completed":
                    content = json.loads(event["body"]).get("content")
                    if isinstance(content, str):
                        item["content"] = redact_public_text(content, max_chars=4_000)
                summaries.append(item)
            # Revocation and session close can race with projection generation.
            at = datetime.now(timezone.utc)
            remote._active_host(command.host_id)
            remote._active_device(command.device_id, host_id=command.host_id)
            fresh = remote._active_session(command.remote_session_id, at=at)
            if fresh.device_id != device.device_id or fresh.host_id != host.host_id:
                raise RemoteControlError(
                    "remote.session_binding_invalid", "Session binding changed", status_code=403
                )
            _bound_workspace(service, payload.target_id, f"remote-device:{device.device_id}")
            reply = _reply(
                command=command,
                session=fresh,
                session_key=session_key,
                projection={
                    "session_id": payload.target_id,
                    "latest_cursor": 0 if latest is None else int(latest[0]),
                    "results": summaries,
                },
                issued_at=at,
            )
            response.headers["Cache-Control"] = "no-store"
            response.headers["x-operant-sensitive-response"] = "ephemeral-encrypted"
            return reply
        except RemoteControlError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc
