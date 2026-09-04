from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from operant.api_phase56_control import install_phase56_control_routes
from operant.application.phase45_gateway import Phase45ActionGateway
from operant.application.security import PolicyEngine
from operant.domain.remote_control import (
    EncryptedRemoteCommand,
    RelayEnvelope,
    RelayEnvelopeStatus,
    RemoteCommandStatus,
    RemoteScope,
    RemoteTransportMode,
)
from operant.domain.security import Capability, PolicyBundle, PolicyDecision
from operant.persistence.phase45 import SQLitePhase45Repository
from operant.persistence.security import SQLiteSecurityRepository
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
    ticket = service.create_pairing_challenge(
        host.host_id,
        allowed_scopes=(RemoteScope.OBSERVE, RemoteScope.COMMAND),
    )
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
    tool: str = "remote_projection",
    operation: str = "read",
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
            "tool": tool,
            "operation": operation,
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
        tool="workspace",
        operation="write",
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


def test_host_disable_closes_sessions_and_event_cursor_is_host_bound(tmp_path: Path) -> None:
    service, _store, _signing_private, _session_key = _service(tmp_path)
    with service.store._connect() as connection:
        session_id = connection.execute(
            "SELECT remote_session_id FROM remote_sessions LIMIT 1"
        ).fetchone()[0]
        host_id = connection.execute("SELECT host_id FROM remote_control_hosts LIMIT 1").fetchone()[
            0
        ]
    second = service.enable_host(
        display_name="Second Core",
        core_version="0.1.0",
        protocol_version="phase5b.v1",
    )
    with pytest.raises(RemoteControlError, match="another host"):
        service.list_events(second.host_id, session_id=session_id)

    host = service.repository.get_host(host_id)
    service.enable_host(
        host_id=host.host_id,
        display_name=host.display_name,
        core_version=host.core_version,
        protocol_version=host.protocol_version,
        capabilities=host.capabilities,
        enabled=False,
    )
    closed = service.repository.get_session(session_id)
    assert closed.connection_state.value == "closed"
    with pytest.raises(KeyError):
        service.key_store.get(closed.session_key_ref)


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
        action_gateway=Phase45ActionGateway(
            SQLiteSecurityRepository(store),
            SQLitePhase45Repository(store),
            remote_control_policy_engine(),
        ),
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
        "listRemoteHosts",
        "createPairingChallenge",
        "pairRemoteDevice",
        "listRemoteDevices",
        "revokeRemoteDevice",
        "createRemoteSession",
        "closeRemoteSession",
        "listRemoteSessions",
        "submitRemoteCommand",
        "getRemoteCommand",
        "reconcileRemoteCommand",
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
    listed = client.get("/v1/remote-control/hosts", headers={"x-local": "ok"})
    assert listed.status_code == 200
    assert listed.json()["items"][0]["host_id"] == response.json()["host_id"]


def test_key_store_serializes_concurrent_writers(tmp_path: Path) -> None:
    path = (tmp_path / "concurrent-keys.json").absolute()

    def write(index: int) -> None:
        RemoteKeyStore(path).put(f"key:{index}", bytes([index]) * 32)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(32)))
    key_store = RemoteKeyStore(path)
    assert [key_store.get(f"key:{index}") for index in range(32)] == [
        bytes([index]) * 32 for index in range(32)
    ]


def test_unconfigured_executor_rejects_without_unknown_outcome(tmp_path: Path) -> None:
    service, _store, signing_private, session_key = _service(tmp_path)
    service.executor = service._unsupported_executor
    command = _signed_command(service, signing_private, session_key, suffix="unsupported")
    receipt = service.submit_command(command)
    assert receipt.status is RemoteCommandStatus.REJECTED
    assert receipt.error_code == "remote.operation_unavailable"


def test_pairing_scope_is_locally_bounded_without_consuming_ticket(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "scope.sqlite3")
    store.initialize()
    service = RemoteControlService(
        store,
        RemoteKeyStore((tmp_path / "scope-keys.json").absolute()),
        remote_control_policy_engine(),
    )
    host = service.enable_host(
        display_name="Local Core", core_version="0.1.0", protocol_version="phase5b.v1"
    )
    ticket = service.create_pairing_challenge(host.host_id)
    signing_public, _ = RemoteCrypto.create_signing_keypair()
    exchange_public, _ = RemoteCrypto.create_exchange_keypair()
    with pytest.raises(RemoteControlError, match="exceed the locally approved"):
        service.pair_device(
            challenge_id=ticket.challenge_id,
            one_time_code=ticket.one_time_code,
            display_name="Escalating phone",
            signing_public_key=signing_public,
            exchange_public_key=exchange_public,
            scopes=(RemoteScope.OBSERVE, RemoteScope.COMMAND),
        )
    paired = service.pair_device(
        challenge_id=ticket.challenge_id,
        one_time_code=ticket.one_time_code,
        display_name="Observe-only phone",
        signing_public_key=signing_public,
        exchange_public_key=exchange_public,
    )
    assert paired.scopes == (RemoteScope.OBSERVE,)


def test_operation_capabilities_are_trusted_not_device_declared(tmp_path: Path) -> None:
    service, _store, signing_private, session_key = _service(tmp_path)
    disguised_write = _signed_command(
        service,
        signing_private,
        session_key,
        suffix="scope-bypass",
        capabilities=(Capability.REMOTE_CONTROL_OBSERVE, Capability.WORKSPACE_WRITE),
    )
    with pytest.raises(RemoteControlError, match="trusted operation registration"):
        service.submit_command(disguised_write)


def test_restart_marks_accepted_command_unknown_and_requires_reconciliation(
    tmp_path: Path,
) -> None:
    service, store, signing_private, session_key = _service(tmp_path)
    command = _signed_command(service, signing_private, session_key, suffix="crash")
    receipt = service.submit_command(command)
    expired = datetime.now(timezone.utc) - timedelta(seconds=1)
    with store._connect() as connection:
        connection.execute(
            """
            UPDATE remote_command_receipts
            SET status='accepted', execution_owner_id='dead-core',
                execution_lease_expires_at=?
            WHERE command_id=?
            """,
            (expired.isoformat(), receipt.command_id),
        )

    restarted = RemoteControlService(
        store,
        service.key_store,
        remote_control_policy_engine(),
        executor=service.executor,
    )
    unknown = restarted.repository.get_command(receipt.command_id)
    assert unknown.status is RemoteCommandStatus.OUTCOME_UNKNOWN
    assert unknown.error_code == "remote.manual_reconcile_required"
    reconciled = restarted.reconcile_command(
        receipt.command_id,
        status=RemoteCommandStatus.COMPLETED,
        result_ref="verified:external-state",
    )
    assert reconciled.status is RemoteCommandStatus.COMPLETED


def test_live_command_owner_is_not_reconciled_and_old_owner_cannot_overwrite(
    tmp_path: Path,
) -> None:
    service, store, signing_private, session_key = _service(tmp_path)
    command = _signed_command(service, signing_private, session_key, suffix="owner-cas")
    receipt = service.submit_command(command)
    future = datetime.now(timezone.utc) + timedelta(minutes=1)
    with store._connect() as connection:
        connection.execute(
            """
            UPDATE remote_command_receipts
            SET status='accepted', execution_owner_id='live-core',
                execution_lease_expires_at=?, result_ref=NULL
            WHERE command_id=?
            """,
            (future.isoformat(), receipt.command_id),
        )

    other = RemoteControlService(
        store,
        service.key_store,
        remote_control_policy_engine(),
        executor=service.executor,
        execution_owner_id="other-core",
    )
    assert other.repository.get_command(receipt.command_id).status is (RemoteCommandStatus.ACCEPTED)

    other.repository.reconcile_incomplete_commands(now=future + timedelta(seconds=1))
    reconciled = other.reconcile_command(
        receipt.command_id,
        status=RemoteCommandStatus.REJECTED,
        error_code="operator_verified_failure",
    )
    assert reconciled.status is RemoteCommandStatus.REJECTED
    with pytest.raises(ConflictError, match="transition conflicts"):
        service.repository.set_command_status(
            receipt.command_id,
            RemoteCommandStatus.COMPLETED,
            expected_status=RemoteCommandStatus.ACCEPTED,
            execution_owner_id="live-core",
            now=datetime.now(timezone.utc),
            result_ref="late-result",
        )
    assert service.repository.get_command(receipt.command_id).error_code == (
        "operator_verified_failure"
    )


def test_remote_command_reconciliation_requires_local_gateway_and_consumes_lease(
    tmp_path: Path,
) -> None:
    service, store, signing_private, session_key = _service(tmp_path)
    command = _signed_command(service, signing_private, session_key, suffix="reconcile-gateway")
    receipt = service.submit_command(command)
    with store._connect() as connection:
        connection.execute(
            """
            UPDATE remote_command_receipts
            SET status='outcome_unknown', result_ref=NULL,
                error_code='remote.manual_reconcile_required'
            WHERE command_id=?
            """,
            (receipt.command_id,),
        )

    allow = PolicyEngine(
        PolicyBundle(
            bundle_id="remote-reconcile-test",
            version="remote-reconcile-test.v1",
            default_decision=PolicyDecision.ALLOW,
            rules=(),
        )
    )
    app = FastAPI()
    install_phase56_control_routes(
        app,
        store,
        key_store_path=(tmp_path / "remote-keys.json").absolute(),
        local_authorizer=lambda request: request.headers.get("x-local") == "ok",
        relay_authorizer=lambda _request: False,
        action_gateway=Phase45ActionGateway(
            SQLiteSecurityRepository(store),
            SQLitePhase45Repository(store),
            allow,
            principal="local:remote-reconcile",
        ),
    )
    client = TestClient(app)
    path = f"/v1/remote-control/commands/{receipt.command_id}/reconcile"
    body = {"status": "completed", "result_ref": "verified:external-state"}
    assert client.post(path, json=body).status_code == 401
    missing = client.post(
        "/v1/remote-control/commands/does-not-exist/reconcile",
        json=body,
        headers={"x-local": "ok"},
    )
    assert missing.status_code == 404
    response = client.post(path, json=body, headers={"x-local": "ok"})
    assert response.status_code == 200
    assert response.json()["status"] == "completed"

    with store._connect() as connection:
        action = connection.execute(
            """
            SELECT action_hash, body FROM security_action_requests
            WHERE principal='local:remote-reconcile' AND tool='remote_control'
              AND operation='reconcile_command'
            """
        ).fetchone()
        assert action is not None
        assert receipt.action_hash in action["body"]
        lease = connection.execute(
            "SELECT uses, max_uses FROM capability_leases WHERE action_hash=?",
            (action["action_hash"],),
        ).fetchone()
        assert lease is not None
        assert (lease["uses"], lease["max_uses"]) == (1, 1)
