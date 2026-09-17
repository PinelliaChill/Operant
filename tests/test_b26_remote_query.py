"""Focused encrypted B2-6 Remote Query boundary checks."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from cryptography.exceptions import InvalidTag
from fastapi import FastAPI
from fastapi.testclient import TestClient

from operant.contracts.b2_6_query import B26EncryptedReply
from operant.domain.remote_control import (
    EncryptedRemoteCommand,
    RelayEnvelope,
    RemoteScope,
    RemoteTransportMode,
)
from operant.domain.security import Capability
from operant.memory_plugins.remote_query import (
    decrypt_reply,
    install_remote_memory_query,
)
from operant.persistence.sqlite import SQLiteStore
from operant.remote_control.crypto import RemoteCrypto, RemoteKeyStore
from operant.remote_control.runtime import (
    RelayService,
    RemoteActionPayload,
    RemoteControlService,
    remote_control_policy_engine,
)


def _setup(tmp_path: Path):
    store = SQLiteStore(tmp_path / "remote-query.sqlite3")
    store.initialize()
    calls: list[RemoteActionPayload] = []

    def executor(payload: RemoteActionPayload, _action: Any) -> str:
        calls.append(payload)
        return "remote-query-ack"

    service = RemoteControlService(
        store,
        RemoteKeyStore((tmp_path / "remote-query-keys.json").absolute()),
        remote_control_policy_engine(),
        executor=executor,
    )
    host = service.enable_host(
        display_name="B2-6 Query Core",
        core_version="0.1.0",
        protocol_version="phase5b.v1",
    )
    ticket = service.create_pairing_challenge(
        host.host_id,
        allowed_scopes=(RemoteScope.OBSERVE, RemoteScope.COMMAND),
    )
    signing_public, signing_private = RemoteCrypto.create_signing_keypair()
    exchange_public, exchange_private = RemoteCrypto.create_exchange_keypair()
    device = service.pair_device(
        challenge_id=ticket.challenge_id,
        one_time_code=ticket.one_time_code,
        display_name="B2-6 Query Device",
        signing_public_key=signing_public,
        exchange_public_key=exchange_public,
        scopes=(RemoteScope.OBSERVE, RemoteScope.COMMAND),
    )
    session = service.create_session(
        host_id=host.host_id,
        device_id=device.device_id,
        transport_mode=RemoteTransportMode.RELAY,
        protocol_version="phase5b.v1",
    )
    session_key = RemoteCrypto.derive_session_key(
        exchange_private,
        host.exchange_public_key,
        session_id=session.remote_session_id,
        host_id=host.host_id,
        device_id=device.device_id,
    )
    app = FastAPI()
    install_remote_memory_query(
        app,
        service,
        lambda project_id: {
            "project_id": project_id,
            "records": [{"content": "REMOTE_QUERY_BODY"}],
        },
    )
    return (
        app,
        store,
        service,
        host,
        device,
        session,
        signing_private,
        session_key,
        calls,
    )


def _command(
    *,
    service: RemoteControlService,
    host_id: str,
    device_id: str,
    session_id: str,
    signing_private: bytes,
    session_key: bytes,
    project_id: str = "project-query",
    operation: str = "query",
    capabilities: tuple[Capability, ...] = (Capability.REMOTE_CONTROL_OBSERVE,),
    issued_at: datetime | None = None,
    expires_at: datetime | None = None,
    arguments: dict[str, Any] | None = None,
) -> EncryptedRemoteCommand:
    issued = issued_at or datetime.now(timezone.utc)
    expiry = expires_at or issued + timedelta(minutes=2)
    unsigned = EncryptedRemoteCommand(
        command_id="b26-query-command",
        idempotency_key="b26-query-idempotency",
        host_id=host_id,
        device_id=device_id,
        remote_session_id=session_id,
        protocol_version="phase5b.v1",
        issued_at=issued,
        expires_at=expiry,
        nonce="0" * 16,
        ciphertext="placeholder",
        signature="0" * 32,
    )
    return RemoteCrypto.encrypt_command_payload(
        unsigned,
        {
            "tool": "memory",
            "operation": operation,
            "target_id": project_id,
            "arguments": arguments or {},
            "capabilities": [item.value for item in capabilities],
        },
        session_key,
        signing_private,
    )


def test_encrypted_query_returns_decryptable_bounded_projection(tmp_path: Path) -> None:
    app, store, service, host, device, session, signing_private, session_key, calls = _setup(
        tmp_path
    )
    command = _command(
        service=service,
        host_id=host.host_id,
        device_id=device.device_id,
        session_id=session.remote_session_id,
        signing_private=signing_private,
        session_key=session_key,
    )
    with TestClient(app) as client:
        response = client.post("/v1/b2-6/remote-query", json=command.model_dump(mode="json"))
    assert response.status_code == 200, response.text
    reply = B26EncryptedReply.model_validate(response.json())
    assert reply.command_id == command.command_id
    assert response.headers["cache-control"] == "no-store"
    assert "REMOTE_QUERY_BODY" not in response.text
    assert decrypt_reply(reply, session_key)["project_id"] == "project-query"
    assert len(calls) == 1 and calls[0].operation == "query"
    with store._connect() as connection:
        row = connection.execute(
            "SELECT result_ref FROM remote_command_receipts WHERE command_id=?",
            (command.command_id,),
        ).fetchone()
    assert row is not None and row["result_ref"] == "remote-query-ack"
    service.close_session(session.remote_session_id)


def test_query_preflight_rejects_command_operation_before_submit(tmp_path: Path) -> None:
    app, _store, service, host, device, session, signing_private, session_key, calls = _setup(
        tmp_path
    )
    command = _command(
        service=service,
        host_id=host.host_id,
        device_id=device.device_id,
        session_id=session.remote_session_id,
        signing_private=signing_private,
        session_key=session_key,
        operation="command",
        capabilities=(Capability.REMOTE_CONTROL_COMMAND,),
    )
    with TestClient(app) as client:
        response = client.post("/v1/b2-6/remote-query", json=command.model_dump(mode="json"))
    assert response.status_code == 403
    assert calls == []

    mismatched_capabilities = _command(
        service=service,
        host_id=host.host_id,
        device_id=device.device_id,
        session_id=session.remote_session_id,
        signing_private=signing_private,
        session_key=session_key,
        capabilities=(Capability.REMOTE_CONTROL_COMMAND,),
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/b2-6/remote-query",
            json=mismatched_capabilities.model_dump(mode="json"),
        )
    assert response.status_code == 403
    assert calls == []


def test_query_rejects_expired_tampered_and_wrong_device_commands(tmp_path: Path) -> None:
    app, _store, service, host, device, session, signing_private, session_key, calls = _setup(
        tmp_path
    )
    now = datetime.now(timezone.utc)
    expired = _command(
        service=service,
        host_id=host.host_id,
        device_id=device.device_id,
        session_id=session.remote_session_id,
        signing_private=signing_private,
        session_key=session_key,
        issued_at=now - timedelta(minutes=5),
        expires_at=now - timedelta(minutes=4),
    )
    with TestClient(app) as client:
        response = client.post("/v1/b2-6/remote-query", json=expired.model_dump(mode="json"))
    assert response.status_code == 400

    valid = _command(
        service=service,
        host_id=host.host_id,
        device_id=device.device_id,
        session_id=session.remote_session_id,
        signing_private=signing_private,
        session_key=session_key,
    )
    tampered = valid.model_copy(update={"command_id": "tampered-command"})
    with TestClient(app) as client:
        response = client.post("/v1/b2-6/remote-query", json=tampered.model_dump(mode="json"))
    assert response.status_code == 403

    wrong_device = _command(
        service=service,
        host_id=host.host_id,
        device_id="device-not-paired",
        session_id=session.remote_session_id,
        signing_private=signing_private,
        session_key=session_key,
    )
    with TestClient(app) as client:
        response = client.post("/v1/b2-6/remote-query", json=wrong_device.model_dump(mode="json"))
    assert response.status_code in {403, 404}
    assert calls == []


def test_revoked_device_and_tampered_reply_fail_closed_and_relay_stays_opaque(
    tmp_path: Path,
) -> None:
    app, store, service, host, device, session, signing_private, session_key, calls = _setup(
        tmp_path
    )
    command = _command(
        service=service,
        host_id=host.host_id,
        device_id=device.device_id,
        session_id=session.remote_session_id,
        signing_private=signing_private,
        session_key=session_key,
    )
    with TestClient(app) as client:
        response = client.post("/v1/b2-6/remote-query", json=command.model_dump(mode="json"))
    reply = B26EncryptedReply.model_validate(response.json())
    with pytest.raises(InvalidTag):
        decrypt_reply(
            reply.model_copy(update={"ciphertext": "A" * len(reply.ciphertext)}), session_key
        )

    service.revoke_device(device.device_id)
    with TestClient(app) as client:
        response = client.post("/v1/b2-6/remote-query", json=command.model_dump(mode="json"))
    assert response.status_code == 403
    assert len(calls) == 1

    RelayService(service.repository).publish(
        RelayEnvelope(
            route_ref=session.remote_session_id,
            sender_ref=device.device_id,
            recipient_ref=host.host_id,
            protocol_version=session.protocol_version,
            ciphertext=reply.ciphertext,
            nonce=reply.nonce,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
        )
    )
    with store._connect() as connection:
        row = connection.execute("SELECT * FROM relay_envelopes LIMIT 1").fetchone()
    assert row is not None
    assert "REMOTE_QUERY_BODY" not in row["ciphertext"]
    assert "body" not in set(row.keys())
