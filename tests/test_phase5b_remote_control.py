from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from operant.api_phase56_control import install_phase56_control_routes
from operant.domain.remote_control import (
    EncryptedRemoteCommand,
    RelayEnvelope,
    RelayEnvelopeStatus,
    RemoteCommandStatus,
    RemoteScope,
    RemoteTransportMode,
)
from operant.domain.security import Capability
from operant.persistence.sqlite import ConflictError, SQLiteStore
from operant.remote_control.crypto import RemoteCrypto, RemoteKeyStore
from operant.remote_control.runtime import (
    HostConnector,
    RelayService,
    RemoteControlError,
    RemoteControlService,
    remote_control_policy_engine,
)


def _service(tmp_path: Path) -> tuple[RemoteControlService, SQLiteStore, bytes, bytes]:
    store = SQLiteStore(tmp_path / "operant.sqlite3")
    store.initialize()
    device_signing_public, device_signing_private = RemoteCrypto.create_signing_keypair()
    device_exchange_public, device_exchange_private = RemoteCrypto.create_exchange_keypair()
    service = RemoteControlService(
        store,
        RemoteKeyStore((tmp_path / "remote-keys.json").absolute()),
        remote_control_policy_engine(),
        executor=lambda _payload, action: f"result:{action.action_hash}",
    )
    host = service.enable_host(
        display_name="Local Core",
        core_version="0.1.0",
        protocol_version="phase5b.v1",
    )
    ticket = service.create_pairing_challenge(host.host_id)
    device = service.pair_device(
        challenge_id=ticket.challenge_id,
        one_time_code=ticket.one_time_code,
        display_name="Phone",
        signing_public_key=device_signing_public,
        exchange_public_key=device_exchange_public,
        scopes=(RemoteScope.OBSERVE, RemoteScope.COMMAND),
    )
    session = service.create_session(
        host_id=host.host_id,
        device_id=device.device_id,
        transport_mode=RemoteTransportMode.RELAY,
        protocol_version="phase5b.v1",
    )
    peer_key = RemoteCrypto.derive_session_key(
        device_exchange_private,
        host.exchange_public_key,
        session_id=session.remote_session_id,
        host_id=host.host_id,
        device_id=device.device_id,
    )
    assert peer_key == service.key_store.get(session.session_key_ref)
    return service, store, device_signing_private, peer_key


def _signed_command(
    service: RemoteControlService,
    signing_private: bytes,
    session_key: bytes,
    *,
    suffix: str = "1",
    capabilities: tuple[Capability, ...] = (
        Capability.REMOTE_CONTROL_OBSERVE,
        Capability.WORKSPACE_READ,
    ),
) -> EncryptedRemoteCommand:
    with service.store._connect() as connection:
        row = connection.execute("SELECT * FROM remote_sessions LIMIT 1").fetchone()
    assert row is not None
    session = service.repository.get_session(row["remote_session_id"])
    now = datetime.now(timezone.utc)
    unsigned = EncryptedRemoteCommand(
        command_id=f"command-{suffix}",
        idempotency_key=f"remote-idempotency-{suffix}",
        host_id=session.host_id,
        device_id=session.device_id,
        remote_session_id=session.remote_session_id,
        protocol_version=session.protocol_version,
        issued_at=now,
        expires_at=now + timedelta(minutes=2),
        nonce="0" * 16,
        ciphertext="placeholder",
        signature="0" * 32,
    )
    return RemoteCrypto.encrypt_command_payload(
        unsigned,
        {
            "tool": "remote_projection",
            "operation": "read",
            "target_id": "thread:demo",
            "arguments": {"cursor": 0},
            "capabilities": [item.value for item in capabilities],
        },
        session_key,
        signing_private,
    )


def test_pairing_is_one_time_and_device_revoke_closes_sessions(tmp_path: Path) -> None:
    service, _store, _signing_private, _session_key = _service(tmp_path)
    with service.store._connect() as connection:
        challenge = connection.execute("SELECT * FROM remote_pairing_challenges").fetchone()
        device = connection.execute("SELECT * FROM remote_devices").fetchone()
        session = connection.execute("SELECT * FROM remote_sessions").fetchone()
    assert challenge is not None and challenge["consumed_at"] is not None
    assert device is not None and session is not None
    with pytest.raises(ConflictError, match="invalid, expired, or consumed"):
        service.repository.consume_challenge(
            challenge["challenge_id"],
            code_hash=challenge["code_hash"],
            now=datetime.now(timezone.utc),
        )
    revoked = service.revoke_device(device["device_id"])
    assert revoked.revoked_at is not None
    assert (
        service.repository.get_session(session["remote_session_id"]).connection_state.value
        == "closed"
    )


def test_authenticated_command_uses_gateway_and_replay_returns_receipt(tmp_path: Path) -> None:
    service, _store, signing_private, session_key = _service(tmp_path)
    command = _signed_command(service, signing_private, session_key)
    first = service.submit_command(command)
    second = service.submit_command(command)
    assert first.status is RemoteCommandStatus.COMPLETED
    assert first.host_acknowledged_at is not None
    assert second == first
    with service.store._connect() as connection:
        action = connection.execute(
            "SELECT principal FROM security_action_requests WHERE action_hash=?",
            (first.action_hash,),
        ).fetchone()
    assert action is not None
    assert action["principal"].startswith("remote-device:")


def test_scope_revocation_bad_signature_and_expiry_fail_closed(tmp_path: Path) -> None:
    service, _store, signing_private, session_key = _service(tmp_path)
    command = _signed_command(
        service,
        signing_private,
        session_key,
        capabilities=(Capability.REMOTE_CONTROL_COMMAND, Capability.WORKSPACE_WRITE),
    )
    receipt = service.submit_command(command)
    assert receipt.status is RemoteCommandStatus.REJECTED
    assert receipt.error_code == "remote.approval_required"

    bad = command.model_copy(update={"command_id": "tampered"})
    with pytest.raises(RemoteControlError, match="authentication failed"):
        service.submit_command(bad)

    expired = _signed_command(service, signing_private, session_key, suffix="expired").model_copy(
        update={
            "issued_at": datetime.now(timezone.utc) - timedelta(minutes=10),
            "expires_at": datetime.now(timezone.utc) - timedelta(minutes=5),
        }
    )
    with pytest.raises(RemoteControlError, match="expired"):
        service.submit_command(expired)


def test_relay_is_bounded_opaque_and_ack_is_not_host_ack(tmp_path: Path) -> None:
    service, _store, signing_private, session_key = _service(tmp_path)
    command = _signed_command(service, signing_private, session_key, suffix="relay")
    session = service.repository.get_session(command.remote_session_id)
    ciphertext, nonce = RemoteCrypto.encrypt_relay_payload(
        command.model_dump_json().encode(), session_key, route_ref=session.remote_session_id
    )
    envelope = RelayEnvelope(
        route_ref=session.remote_session_id,
        sender_ref=session.device_id,
        recipient_ref=session.host_id,
        protocol_version=session.protocol_version,
        ciphertext=ciphertext,
        nonce=nonce,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
    )
    relay = RelayService(service.repository)
    relay.publish(envelope)
    pulled = relay.pull(session.remote_session_id, session.host_id)
    assert pulled[0].status is RelayEnvelopeStatus.DELIVERED
    relay.acknowledge(envelope.envelope_id, session.host_id)
    with pytest.raises(KeyError):
        service.repository.get_command(command.command_id)

    relay.publish(
        envelope.model_copy(update={"envelope_id": "envelope-connector", "nonce": "1" * 16})
    )
    # Publish a valid independently encrypted envelope for the Host Connector.
    connector_ciphertext, connector_nonce = RemoteCrypto.encrypt_relay_payload(
        command.model_dump_json().encode(), session_key, route_ref=session.remote_session_id
    )
    valid = envelope.model_copy(
        update={
            "envelope_id": "envelope-valid",
            "ciphertext": connector_ciphertext,
            "nonce": connector_nonce,
        }
    )
    relay.publish(valid)
    receipts = HostConnector(service, relay).poll_once(session.remote_session_id)
    assert [item.command_id for item in receipts] == [command.command_id]


def test_api_installer_requires_explicit_auth_and_stable_operation_ids(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "api.sqlite3")
    store.initialize()
    app = FastAPI()
    install_phase56_control_routes(
        app,
        store,
        key_store_path=(tmp_path / "api-keys.json").absolute(),
        local_authorizer=lambda request: request.headers.get("x-local") == "ok",
        relay_authorizer=lambda request: request.headers.get("x-relay") == "ok",
    )
    operations = {
        operation["operationId"]
        for item in app.openapi()["paths"].values()
        for operation in item.values()
        if isinstance(operation, dict) and "operationId" in operation
    }
    assert {
        "enableRemoteHost",
        "getRemoteHost",
        "createPairingChallenge",
        "pairRemoteDevice",
        "listRemoteDevices",
        "revokeRemoteDevice",
        "createRemoteSession",
        "closeRemoteSession",
        "listRemoteSessions",
        "submitRemoteCommand",
        "getRemoteCommand",
        "listRemoteControlEvents",
        "publishRelayEnvelope",
        "pullRelayEnvelopes",
        "acknowledgeRelayEnvelope",
        "getRelayHealth",
    } <= operations
    client = TestClient(app)
    body = {
        "display_name": "Local",
        "core_version": "0.1.0",
        "protocol_version": "phase5b.v1",
    }
    assert client.post("/v1/remote-control/hosts/enable", json=body).status_code == 401
    response = client.post("/v1/remote-control/hosts/enable", json=body, headers={"x-local": "ok"})
    assert response.status_code == 200
    assert response.json()["enabled"] is True
