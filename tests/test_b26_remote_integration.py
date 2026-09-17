"""Real Core B2-6, Remote Control and Target queue integration evidence.

The Target connector in this test is deliberately in-memory.  It exercises
the formal registration, lease, queue and completion controller, while making
no claim about an HTTPS or production connector.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from operant.api import create_app
from operant.application.security import PolicyEngine
from operant.contracts.b2_6_query import B26EncryptedReply
from operant.contracts.b2_6_remote import RemoteMemoryPack
from operant.domain.remote_control import (
    EncryptedRemoteCommand,
    RelayEnvelope,
    RemoteScope,
    RemoteTransportMode,
)
from operant.domain.remote_execution import (
    RemoteExecutionJob,
    RemoteJobStatus,
)
from operant.domain.security import PolicyBundle, PolicyDecision
from operant.memory_plugins.remote_memory import (
    RemoteMemoryError,
    RemoteMemoryTargetAdapter,
)
from operant.memory_plugins.remote_query import decrypt_reply
from operant.remote_control.crypto import RemoteCrypto


def _b23_command(client: TestClient, **body: Any) -> dict[str, Any]:
    response = client.post("/v1/b2-3/commands", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _allow_phase45() -> PolicyEngine:
    """Test-only policy for the bounded local integration fixture."""

    return PolicyEngine(
        PolicyBundle(
            bundle_id="b26-remote-integration-policy",
            version="b26-remote-integration-policy.v1",
            default_decision=PolicyDecision.ALLOW,
            rules=(),
        )
    )


def _project_with_memory(client: TestClient, workspace: Path) -> str:
    project = _b23_command(
        client,
        action="project_create",
        name="B2-6 Remote Integration",
        workspace_path=str(workspace),
    )["state"]["projects"][-1]["project_id"]
    installation = _b23_command(
        client,
        action="plugin_install",
        plugin_id="memory-standard",
        mode="trusted_in_process",
    )["state"]["installations"][-1]["installation_id"]
    _b23_command(
        client,
        action="binding_select",
        project_id=project,
        installation_id=installation,
    )
    _b23_command(
        client,
        action="memory_save",
        project_id=project,
        content="REMOTE_INTEGRATION_MEMORY_BODY",
        confirmed=True,
    )
    return project


def _register_target(client: TestClient, target_id: str) -> dict[str, Any]:
    target = client.post(
        "/v1/remote-targets",
        json={
            "target_id": target_id,
            "display_name": "B2-6 in-memory Target",
            "endpoint_ref": "REMOTE_TARGET_ENDPOINT",
            "identity_public_key": "target-public-key-" + "x" * 32,
            "credential_ref": "REMOTE_TARGET_CREDENTIAL",
            "policy_ref": "balanced",
            "artifact_namespace": "b26-remote-integration",
            "capability_manifest": {
                "version": "1",
                "capabilities": ["remote.target.exec"],
                "supported_operations": ["memory.consume"],
                "max_concurrent_jobs": 2,
                "max_payload_bytes": 1_048_576,
                "platform": "test-in-memory",
            },
        },
    )
    assert target.status_code == 201, target.text
    target_data = target.json()
    heartbeat = client.post(
        f"/v1/remote-targets/{target_id}/heartbeat",
        json={"identity_public_key": target_data["identity_public_key"]},
    )
    assert heartbeat.status_code == 200, heartbeat.text
    assert heartbeat.json()["status"] == "online"
    lease = client.post(
        f"/v1/remote-targets/{target_id}/leases",
        json={
            "owner": "b26-remote-integration",
            "workspace_ref": "workspace:b26-remote-integration",
            "ttl_seconds": 120,
            "idempotency_key": "b26-remote-integration-lease",
        },
    )
    assert lease.status_code == 201, lease.text
    return lease.json()


def _pair_remote_device(control: Any) -> tuple[Any, bytes, bytes]:
    host = control.enable_host(
        display_name="B2-6 integration Core",
        core_version="0.1.0",
        protocol_version="phase5b.v1",
    )
    ticket = control.create_pairing_challenge(
        host.host_id,
        allowed_scopes=(RemoteScope.OBSERVE, RemoteScope.COMMAND),
    )
    signing_public, signing_private = RemoteCrypto.create_signing_keypair()
    exchange_public, exchange_private = RemoteCrypto.create_exchange_keypair()
    device = control.pair_device(
        challenge_id=ticket.challenge_id,
        one_time_code=ticket.one_time_code,
        display_name="B2-6 integration device",
        signing_public_key=signing_public,
        exchange_public_key=exchange_public,
        scopes=(RemoteScope.OBSERVE, RemoteScope.COMMAND),
    )
    session = control.create_session(
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
    return session, signing_private, session_key


def _signed_command(
    *,
    session: Any,
    signing_private: bytes,
    session_key: bytes,
    command_id: str,
    idempotency_key: str,
    payload: dict[str, Any],
) -> EncryptedRemoteCommand:
    issued_at = datetime.now(timezone.utc)
    unsigned = EncryptedRemoteCommand(
        command_id=command_id,
        idempotency_key=idempotency_key,
        host_id=session.host_id,
        device_id=session.device_id,
        remote_session_id=session.remote_session_id,
        protocol_version=session.protocol_version,
        issued_at=issued_at,
        expires_at=issued_at + timedelta(minutes=2),
        nonce="0" * 16,
        ciphertext="placeholder",
        signature="0" * 32,
    )
    return RemoteCrypto.encrypt_command_payload(
        unsigned,
        payload,
        session_key,
        signing_private,
    )


def _remote_command_payload(
    *,
    project_id: str,
    target_id: str,
    action: str,
    package: RemoteMemoryPack | None = None,
) -> dict[str, Any]:
    command: dict[str, Any] = {
        "action": action,
        "project_id": project_id,
        "target_id": target_id,
    }
    if action == "remote_pack_create":
        command.update({"purpose": "remote_execution", "ttl_seconds": 30})
    else:
        assert package is not None
        command.update(
            {
                "package_id": package.package_id,
                "package_digest": package.package_digest,
            }
        )
    return {
        "tool": "memory",
        "operation": "command",
        "target_id": project_id,
        "arguments": command,
        "capabilities": ["remote.control.command"],
    }


def test_real_core_remote_query_command_and_target_queue(tmp_path: Path) -> None:
    app = create_app(
        tmp_path / "b26-remote-integration.sqlite3",
        phase45_policy_engine=_allow_phase45(),
    )
    with TestClient(app) as client:
        project_id = _project_with_memory(client, tmp_path)
        target_id = "b26-integration-target"
        lease = _register_target(client, target_id)

        from operant.remote import InMemoryRemoteTargetConnector

        connector = InMemoryRemoteTargetConnector(
            target_id=target_id,
            lease_id=lease["lease_id"],
            lease_token=lease["token"],
            lease_fencing=lease["fencing"],
        )
        # This is an explicit deterministic test connector.  The production
        # HTTPS Target connector is intentionally not claimed by this test.
        app.state.b26_remote_connectors[target_id] = connector

        control = app.state.remote_control_service
        session, signing_private, session_key = _pair_remote_device(control)
        # The default remote command policy is ASK.  This test-only bundle
        # allows the one bounded memory.command path so HostAck and the B26
        # command bridge can be exercised without inventing an approval UI.
        control.policy_engine = PolicyEngine(
            PolicyBundle(
                bundle_id="b26-remote-integration-test",
                version="b26-remote-integration-test.v1",
                default_decision=PolicyDecision.ALLOW,
                rules=(),
            )
        )

        create_command = _signed_command(
            session=session,
            signing_private=signing_private,
            session_key=session_key,
            command_id="b26-remote-create-command",
            idempotency_key="b26-remote-create-command-key",
            payload=_remote_command_payload(
                project_id=project_id,
                target_id=target_id,
                action="remote_pack_create",
            ),
        )
        created_receipt = client.post(
            "/v1/remote-control/commands",
            json=create_command.model_dump(mode="json"),
        )
        assert created_receipt.status_code == 200, created_receipt.text
        assert created_receipt.json()["status"] == "completed"

        with app.state.operant_service.store._connect() as connection:
            package_row = connection.execute(
                "SELECT body, status FROM b26_memory_packs WHERE project_id=?",
                (project_id,),
            ).fetchone()
        assert package_row is not None
        assert package_row["status"] == "active"
        package = RemoteMemoryPack.model_validate_json(package_row["body"])
        assert package.target_id == target_id

        jobs_response = client.get("/v1/remote-targets/jobs", params={"target_id": target_id})
        assert jobs_response.status_code == 200, jobs_response.text
        queued_jobs = [
            RemoteExecutionJob.model_validate(item) for item in jobs_response.json()["items"]
        ]
        assert len(queued_jobs) == 1
        assert queued_jobs[0].operation == "memory.consume"
        assert queued_jobs[0].arguments["remote_memory_pack"]["package_id"] == package.package_id

        lease_binding = {
            "lease_id": lease["lease_id"],
            "token": lease["token"],
            "fencing": lease["fencing"],
        }
        polled = client.post(
            f"/v1/remote-targets/{target_id}/jobs/poll",
            json={**lease_binding, "limit": 8, "idempotency_key": "b26-remote-poll"},
        )
        assert polled.status_code == 200, polled.text
        running_jobs = [RemoteExecutionJob.model_validate(item) for item in polled.json()["items"]]
        assert len(running_jobs) == 1
        target_adapter = RemoteMemoryTargetAdapter(target_id)
        consumed = target_adapter.consume_job(running_jobs[0])
        assert consumed.package_id == package.package_id
        completed = client.post(
            f"/v1/remote-targets/{target_id}/jobs/{running_jobs[0].job_id}/complete",
            json={
                **lease_binding,
                "result_id": "b26-remote-result",
                "result_idempotency_key": "b26-remote-result-key",
                "status": RemoteJobStatus.SUCCEEDED.value,
                "postcondition": {"package_id": package.package_id},
            },
        )
        assert completed.status_code == 200, completed.text
        assert completed.json()["result"]["status"] == RemoteJobStatus.SUCCEEDED.value

        query_command = _signed_command(
            session=session,
            signing_private=signing_private,
            session_key=session_key,
            command_id="b26-remote-query-command",
            idempotency_key="b26-remote-query-command-key",
            payload={
                "tool": "memory",
                "operation": "query",
                "target_id": project_id,
                "arguments": {},
                "capabilities": ["remote.control.observe"],
            },
        )
        query_response = client.post(
            "/v1/b2-6/remote-query",
            json=query_command.model_dump(mode="json"),
        )
        assert query_response.status_code == 200, query_response.text
        query_reply = B26EncryptedReply.model_validate(query_response.json())
        projection = decrypt_reply(query_reply, session_key)
        assert projection["project_id"] == project_id
        assert any(
            record["content"] == "REMOTE_INTEGRATION_MEMORY_BODY"
            for record in projection["remote"]["records"]
        )
        assert any(
            summary["package_id"] == package.package_id and summary["status"] == "active"
            for summary in projection["remote"]["packs"]
        )

        app.state.relay_service.publish(
            RelayEnvelope(
                route_ref=session.remote_session_id,
                sender_ref=session.device_id,
                recipient_ref=session.host_id,
                protocol_version=session.protocol_version,
                ciphertext=query_reply.ciphertext,
                nonce=query_reply.nonce,
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
            )
        )
        with app.state.operant_service.store._connect() as connection:
            relay_row = connection.execute("SELECT * FROM relay_envelopes LIMIT 1").fetchone()
        assert relay_row is not None
        assert "REMOTE_INTEGRATION_MEMORY_BODY" not in relay_row["ciphertext"]
        assert "body" not in set(relay_row.keys())

        revoke_command = _signed_command(
            session=session,
            signing_private=signing_private,
            session_key=session_key,
            command_id="b26-remote-revoke-command",
            idempotency_key="b26-remote-revoke-command-key",
            payload=_remote_command_payload(
                project_id=project_id,
                target_id=target_id,
                action="remote_pack_revoke",
                package=package,
            ),
        )
        revoked_receipt = client.post(
            "/v1/remote-control/commands",
            json=revoke_command.model_dump(mode="json"),
        )
        assert revoked_receipt.status_code == 200, revoked_receipt.text
        assert revoked_receipt.json()["status"] == "completed"

        with app.state.operant_service.store._connect() as connection:
            revoked_row = connection.execute(
                "SELECT status FROM b26_memory_packs WHERE package_id=?",
                (package.package_id,),
            ).fetchone()
        assert revoked_row is not None and revoked_row["status"] == "revoked"

        revoked_query = _signed_command(
            session=session,
            signing_private=signing_private,
            session_key=session_key,
            command_id="b26-remote-query-after-revoke",
            idempotency_key="b26-remote-query-after-revoke-key",
            payload={
                "tool": "memory",
                "operation": "query",
                "target_id": project_id,
                "arguments": {},
                "capabilities": ["remote.control.observe"],
            },
        )
        revoked_query_response = client.post(
            "/v1/b2-6/remote-query",
            json=revoked_query.model_dump(mode="json"),
        )
        assert revoked_query_response.status_code == 200, revoked_query_response.text
        revoked_projection = decrypt_reply(
            B26EncryptedReply.model_validate(revoked_query_response.json()), session_key
        )
        assert any(
            summary["package_id"] == package.package_id and summary["status"] == "revoked"
            for summary in revoked_projection["remote"]["packs"]
        )

        target_adapter.revoke(package)
        with pytest.raises(RemoteMemoryError, match="revoked or expired"):
            target_adapter.consume_job(running_jobs[0])
        expired_adapter = RemoteMemoryTargetAdapter(
            target_id,
            clock=lambda: package.expires_at + timedelta(seconds=1),
        )
        with pytest.raises(RemoteMemoryError, match="expired"):
            expired_adapter.consume_job(running_jobs[0])
