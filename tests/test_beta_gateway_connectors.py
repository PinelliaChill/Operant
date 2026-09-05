from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from operant.domain.multiwriter import WriterIsolationKind, WriterWorkspace
from operant.domain.remote_control import EncryptedRemoteCommand, RelayEnvelope, RemoteCommandStatus
from operant.domain.remote_execution import (
    RemoteActionIdempotency,
    RemoteCapability,
    RemoteExecutionJob,
    RemoteExecutionResult,
    RemoteJobStatus,
)
from operant.multiwriter.container import (
    ContainerLifecycleStatus,
    ContainerOutcomeUnknown,
    ContainerResourceLimits,
    ContainerWriterLifecycle,
    ContainerWriterSpec,
)
from operant.remote.connector import RemoteOutcomeUnknown
from operant.remote.http_connector import HttpRemoteTargetConfig, HttpRemoteTargetConnector
from operant.remote_control.gateway import RemoteGatewayConfig, install_remote_gateway
from operant.remote_control.host_connector import HostConnectorConfig, RelayHostConnector


class _Receipt:
    command_id = "remote_command_1"

    def model_dump(self, *, mode: str) -> dict[str, Any]:
        assert mode == "json"
        return {
            "command_id": "remote_command_1",
            "status": RemoteCommandStatus.COMPLETED.value,
            "host_acknowledged_at": "2026-09-04T00:00:00Z",
        }


class _GatewayService:
    def validate_gateway_binding(
        self, host_id: str, device_id: str, session_id: str, protocol_version: str
    ) -> None:
        assert (host_id, device_id, session_id, protocol_version) == (
            "host_1",
            "device_1",
            "session_1",
            "phase56.v1",
        )

    def submit_command(self, _command: Any) -> _Receipt:
        return _Receipt()

    def list_events(
        self,
        host_id: str,
        *,
        session_id: str | None,
        after_cursor: int,
        limit: int,
        device_id: str | None = None,
    ) -> tuple[dict[str, Any], ...]:
        assert (host_id, device_id, session_id, after_cursor, limit) == (
            "host_1",
            "device_1",
            "session_1",
            4,
            200,
        )
        return ({"cursor": 5, "event_type": "remote.command.completed"},)


def _gateway_app() -> FastAPI:
    app = FastAPI()
    install_remote_gateway(
        app,
        _GatewayService(),
        config=RemoteGatewayConfig(
            token="g" * 32,
            allowed_origins=frozenset({"https://control.example"}),
            allow_insecure_loopback=True,
        ),
    )
    return app


def test_gateway_exposes_uvicorn_transport_limit() -> None:
    app = _gateway_app()
    assert app.state.remote_gateway_ws_max_size == 512 * 1024


def test_gateway_rejects_origin_before_accepting() -> None:
    with (
        TestClient(_gateway_app()) as client,
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(
            "/v1/remote-control/gateway",
            headers={"authorization": f"Bearer {'g' * 32}", "origin": "https://evil.example"},
            subprotocols=["operant.remote.v1"],
        ),
    ):
        pass


def test_gateway_requires_explicit_subprotocol() -> None:
    with (
        TestClient(_gateway_app()) as client,
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(
            "/v1/remote-control/gateway",
            headers={
                "authorization": f"Bearer {'g' * 32}",
                "origin": "https://control.example",
            },
        ),
    ):
        pass


def test_gateway_cursor_sync_is_authoritative_and_not_relay_ack() -> None:
    with (
        TestClient(_gateway_app()) as client,
        client.websocket_connect(
            "/v1/remote-control/gateway",
            headers={
                "authorization": f"Bearer {'g' * 32}",
                "origin": "https://control.example",
            },
            subprotocols=["operant.remote.v1"],
        ) as websocket,
    ):
        websocket.send_json(
            {
                "type": "hello",
                "host_id": "host_1",
                "device_id": "device_1",
                "remote_session_id": "session_1",
                "protocol_version": "phase56.v1",
            }
        )
        assert websocket.receive_json()["type"] == "hello_ack"
        websocket.send_json({"type": "cursor_sync", "after_cursor": 4})
        response = websocket.receive_json()
    assert response == {
        "type": "events",
        "items": [{"cursor": 5, "event_type": "remote.command.completed"}],
        "next_cursor": 5,
    }
    assert "ack" not in response


def test_gateway_requires_hello_and_rejects_identity_override() -> None:
    with (
        TestClient(_gateway_app()) as client,
        client.websocket_connect(
            "/v1/remote-control/gateway",
            headers={
                "authorization": f"Bearer {'g' * 32}",
                "origin": "https://control.example",
            },
            subprotocols=["operant.remote.v1"],
        ) as websocket,
    ):
        websocket.send_json({"type": "cursor_sync", "after_cursor": 0})
        assert websocket.receive_json()["code"] == "gateway.hello_required"
        websocket.send_json(
            {
                "type": "hello",
                "host_id": "host_1",
                "device_id": "device_1",
                "remote_session_id": "session_1",
                "protocol_version": "phase56.v1",
            }
        )
        assert websocket.receive_json()["type"] == "hello_ack"
        websocket.send_json({"type": "cursor_sync", "after_cursor": 0, "host_id": "another-host"})
        assert websocket.receive_json()["code"] == "gateway.invalid_cursor"
        now = datetime.now(timezone.utc)
        websocket.send_json(
            {
                "type": "command",
                "command": EncryptedRemoteCommand(
                    command_id="remote_command_other",
                    idempotency_key="other",
                    host_id="another-host",
                    device_id="device_1",
                    remote_session_id="session_1",
                    protocol_version="phase56.v1",
                    issued_at=now,
                    expires_at=now + timedelta(seconds=30),
                    nonce="n" * 16,
                    ciphertext="opaque",
                    signature="s" * 32,
                ).model_dump(mode="json"),
            }
        )
        assert websocket.receive_json()["code"] == "gateway.command_binding_invalid"


def _key_material() -> tuple[Ed25519PrivateKey, str]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return private, base64.urlsafe_b64encode(public).decode().rstrip("=")


def _job() -> RemoteExecutionJob:
    return RemoteExecutionJob(
        job_id="remote_job_1",
        target_id="target_1",
        lease_id="lease_1",
        lease_fencing=7,
        capability=RemoteCapability.TARGET_EXEC,
        operation="run",
        arguments={"argv": ["true"]},
        action_hash="a" * 64,
        idempotency_key="job-stable",
        idempotency=RemoteActionIdempotency.NON_IDEMPOTENT,
    )


def test_http_target_connector_verifies_identity_and_fencing() -> None:
    private, public = _key_material()
    result = RemoteExecutionResult(
        job_id="remote_job_1",
        result_idempotency_key="result-stable",
        status=RemoteJobStatus.SUCCEEDED,
    )
    content = json.dumps({"result": result.model_dump(mode="json")}, separators=(",", ":")).encode()
    signature = private.sign(hashlib.sha256(content).digest())

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-operant-lease-fencing"] == "7"
        assert request.headers["x-operant-lease-token"] == "l" * 32
        assert request.headers["idempotency-key"] == "job-stable"
        return httpx.Response(
            200,
            content=content,
            headers={
                "content-type": "application/json",
                "x-operant-target-signature": base64.urlsafe_b64encode(signature)
                .decode()
                .rstrip("="),
            },
        )

    connector = HttpRemoteTargetConnector(
        target_id="target_1",
        lease_id="lease_1",
        lease_token="l" * 32,
        lease_fencing=7,
        config=HttpRemoteTargetConfig(
            endpoint="https://target.example",
            bearer_token="t" * 32,
            identity_public_key=public,
        ),
        transport=httpx.MockTransport(handler),
    )
    assert connector.execute(_job()).result.status is RemoteJobStatus.SUCCEEDED


def test_http_target_connector_treats_unsigned_result_as_unknown() -> None:
    _private, public = _key_material()
    connector = HttpRemoteTargetConnector(
        target_id="target_1",
        lease_id="lease_1",
        lease_token="l" * 32,
        lease_fencing=7,
        config=HttpRemoteTargetConfig(
            endpoint="https://target.example",
            bearer_token="t" * 32,
            identity_public_key=public,
        ),
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={})),
    )
    with pytest.raises(RemoteOutcomeUnknown):
        connector.execute(_job())


def test_http_target_connector_treats_unsigned_4xx_as_unknown() -> None:
    _private, public = _key_material()
    connector = HttpRemoteTargetConnector(
        target_id="target_1",
        lease_id="lease_1",
        lease_token="l" * 32,
        lease_fencing=7,
        config=HttpRemoteTargetConfig(
            endpoint="https://target.example",
            bearer_token="t" * 32,
            identity_public_key=public,
        ),
        transport=httpx.MockTransport(lambda _request: httpx.Response(409, json={})),
    )
    with pytest.raises(RemoteOutcomeUnknown, match="unsigned"):
        connector.execute(_job())


def test_http_target_connector_limits_stream_before_signature_check() -> None:
    _private, public = _key_material()
    connector = HttpRemoteTargetConnector(
        target_id="target_1",
        lease_id="lease_1",
        lease_token="l" * 32,
        lease_fencing=7,
        config=HttpRemoteTargetConfig(
            endpoint="https://target.example",
            bearer_token="t" * 32,
            identity_public_key=public,
            max_response_bytes=1024,
        ),
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=b"x" * 1025)),
    )
    with pytest.raises(RemoteOutcomeUnknown, match="exceeded"):
        connector.execute(_job())


def test_http_target_connector_requires_signed_cancel_receipt() -> None:
    _private, public = _key_material()
    connector = HttpRemoteTargetConnector(
        target_id="target_1",
        lease_id="lease_1",
        lease_token="l" * 32,
        lease_fencing=7,
        config=HttpRemoteTargetConfig(
            endpoint="https://target.example",
            bearer_token="t" * 32,
            identity_public_key=public,
        ),
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={})),
    )
    with pytest.raises(RemoteOutcomeUnknown, match="unsigned"):
        connector.cancel("remote_job_1")


class _DockerRunner:
    def __init__(self, responses: list[subprocess.CompletedProcess[str] | Exception]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: Any, *, timeout_seconds: int) -> subprocess.CompletedProcess[str]:
        assert timeout_seconds == 30
        self.calls.append(tuple(argv))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _inspect(
    status: str,
    running: bool,
    workspace_id: str = "writer_workspace_1",
    root: Path | None = None,
) -> str:
    root = root or Path("/tmp/writer")
    return json.dumps(
        {
            "Config": {
                "Labels": {
                    "operant.managed": "true",
                    "operant.writer_workspace_id": workspace_id,
                    "operant.isolation_ref": "container_root_1",
                    "operant.user": "10001:10001",
                },
                "User": "10001:10001",
            },
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": str(root),
                    "Destination": "/workspace",
                    "RW": True,
                }
            ],
            "State": {
                "Running": running,
                "Status": status,
                "StartedAt": (
                    "0001-01-01T00:00:00Z" if status == "created" else "2026-09-04T00:00:00Z"
                ),
            },
        }
    )


def _workspace() -> WriterWorkspace:
    return WriterWorkspace(
        writer_workspace_id="writer_workspace_1",
        graph_run_id="graph_1",
        node_run_id="node_1",
        writer_key="writer_1",
        isolation_kind=WriterIsolationKind.CONTAINER,
        isolation_ref="container_root_1",
        base_revision="1234567",
        ownership_paths=("src",),
    )


def test_container_writer_create_is_digest_pinned_and_bounded(tmp_path: Path) -> None:
    root = tmp_path / "writer"
    root.mkdir()
    runner = _DockerRunner(
        [
            subprocess.CompletedProcess(
                ("docker", "inspect"),
                1,
                "[]\n",
                "error: no such object: operant-writer-writer-workspace-1\n",
            ),
            subprocess.CompletedProcess(("docker", "create"), 0, "container-id", ""),
        ]
    )
    lifecycle = ContainerWriterLifecycle({"container_root_1": root}, runner=runner)
    status = lifecycle.create(
        ContainerWriterSpec(
            workspace=_workspace(),
            image="sha256:" + "b" * 64,
            command=("python", "-m", "worker"),
            environment={"OPERANT_WRITER_MODE": "bounded"},
            user_uid=10001,
            user_gid=10001,
            resources=ContainerResourceLimits(cpus=1, memory_bytes=512 * 1024 * 1024, pids=64),
        )
    )
    assert status is ContainerLifecycleStatus.CREATED
    argv = runner.calls[1]
    assert argv[:2] == ("docker", "create")
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
    assert "--read-only" in argv and "--cap-drop" in argv
    assert argv[argv.index("--user") + 1] == "10001:10001"
    assert "sha256:" + "b" * 64 in argv
    assert "--label" in argv
    assert any(item.startswith("operant.spec_sha256=") for item in argv)
    assert "--rm" not in argv


def test_container_writer_rejects_non_hex_image_digest() -> None:
    with pytest.raises(ValueError, match="sha256 digest"):
        ContainerWriterSpec(
            workspace=_workspace(),
            image="sha256:" + "z" * 64,
            command=("true",),
            environment={},
            user_uid=10001,
            user_gid=10001,
        )


def test_container_crash_cleanup_preserves_running_container(tmp_path: Path) -> None:
    root = tmp_path / "writer"
    root.mkdir()
    runner = _DockerRunner(
        [
            subprocess.CompletedProcess(
                ("docker", "inspect"), 0, _inspect("running", True, root=root), ""
            )
        ]
    )
    lifecycle = ContainerWriterLifecycle({"container_root_1": root}, runner=runner)
    with pytest.raises(ContainerOutcomeUnknown):
        lifecycle.cleanup_after_crash("writer_workspace_1")
    assert len(runner.calls) == 1


def test_container_crash_cleanup_preserves_exited_container(tmp_path: Path) -> None:
    root = tmp_path / "writer"
    root.mkdir()
    runner = _DockerRunner(
        [
            subprocess.CompletedProcess(
                ("docker", "inspect"), 0, _inspect("exited", False, root=root), ""
            )
        ]
    )
    lifecycle = ContainerWriterLifecycle({"container_root_1": root}, runner=runner)
    with pytest.raises(ContainerOutcomeUnknown, match="preserve"):
        lifecycle.cleanup_after_crash("writer_workspace_1")
    assert len(runner.calls) == 1


def test_container_crash_cleanup_removes_only_never_started_created(tmp_path: Path) -> None:
    root = tmp_path / "writer"
    root.mkdir()
    created = subprocess.CompletedProcess(
        ("docker", "inspect"), 0, _inspect("created", False, root=root), ""
    )
    runner = _DockerRunner(
        [
            created,
            created,
            subprocess.CompletedProcess(("docker", "rm"), 0, "writer_workspace_1", ""),
        ]
    )
    lifecycle = ContainerWriterLifecycle({"container_root_1": root}, runner=runner)
    assert lifecycle.cleanup_after_crash("writer_workspace_1") is ContainerLifecycleStatus.REMOVED
    assert runner.calls[-1][:2] == ("docker", "rm")


def test_container_writer_roots_are_independent_and_revalidated(tmp_path: Path) -> None:
    root = tmp_path / "writer"
    root.mkdir()
    with pytest.raises(ValueError, match="independent"):
        ContainerWriterLifecycle({"one": root, "two": root})
    child = root / "child"
    child.mkdir()
    with pytest.raises(ValueError, match="non-overlapping"):
        ContainerWriterLifecycle({"one": root, "two": child})
    lifecycle = ContainerWriterLifecycle({"container_root_1": root})
    moved = tmp_path / "moved"
    root.rename(moved)
    root.symlink_to(moved, target_is_directory=True)
    with pytest.raises(ValueError, match="identity changed"):
        lifecycle.workspace_path("container_root_1")


@pytest.mark.asyncio
async def test_host_connector_acks_only_after_host_receipt() -> None:
    now = datetime.now(timezone.utc)
    envelope = RelayEnvelope(
        envelope_id="relay_1",
        route_ref="route_1",
        sender_ref="device_1",
        recipient_ref="host_1",
        protocol_version="phase56.v1",
        ciphertext=base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("="),
        nonce=base64.urlsafe_b64encode(b"n" * 12).decode().rstrip("="),
        expires_at=now + timedelta(seconds=60),
        created_at=now,
    )
    submitted: list[bool] = []

    class Sink:
        def submit_command(self, _command: Any) -> _Receipt:
            submitted.append(True)
            return _Receipt()

    command = EncryptedRemoteCommand(
        command_id="remote_command_1",
        idempotency_key="relay-command-1",
        host_id="host_1",
        device_id="device_1",
        remote_session_id="session_1",
        protocol_version="phase56.v1",
        issued_at=now,
        expires_at=now + timedelta(seconds=60),
        nonce=base64.urlsafe_b64encode(b"n" * 12).decode().rstrip("="),
        ciphertext=base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("="),
        signature="s" * 32,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"items": [envelope.model_dump(mode="json")]})
        assert submitted == [True]
        return httpx.Response(200, json={"status": "acknowledged"})

    connector = RelayHostConnector(
        HostConnectorConfig(
            base_url="https://relay.example",
            bearer_token="r" * 32,
            route_ref="route_1",
            recipient_ref="host_1",
        ),
        Sink(),
        decrypt_envelope=lambda _item: command,
        transport=httpx.MockTransport(handler),
    )
    async with httpx.AsyncClient(
        base_url="https://relay.example",
        transport=httpx.MockTransport(handler),
    ) as client:
        assert await connector.poll_once(client) == 1


@pytest.mark.asyncio
async def test_host_connector_rejects_misbound_envelope_without_ack() -> None:
    now = datetime.now(timezone.utc)
    envelope = RelayEnvelope(
        envelope_id="relay_2",
        route_ref="another-route",
        sender_ref="device_1",
        recipient_ref="host_1",
        protocol_version="phase56.v1",
        ciphertext=base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("="),
        nonce=base64.urlsafe_b64encode(b"n" * 12).decode().rstrip("="),
        expires_at=now + timedelta(seconds=60),
        created_at=now,
    )
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(200, json={"items": [envelope.model_dump(mode="json")]})

    connector = RelayHostConnector(
        HostConnectorConfig(
            base_url="https://relay.example",
            bearer_token="r" * 32,
            route_ref="route_1",
            recipient_ref="host_1",
        ),
        _GatewayService(),
        decrypt_envelope=lambda _item: pytest.fail("misbound envelope must not be decrypted"),
        transport=httpx.MockTransport(handler),
    )
    async with httpx.AsyncClient(
        base_url="https://relay.example",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ValueError, match="binding"):
            await connector.poll_once(client)
    assert methods == ["GET"]


@pytest.mark.asyncio
async def test_host_connector_revalidates_protocol_and_ttl() -> None:
    now = datetime.now(timezone.utc)
    envelope = RelayEnvelope(
        envelope_id="relay_3",
        route_ref="route_1",
        sender_ref="device_1",
        recipient_ref="host_1",
        protocol_version="wrong.v1",
        ciphertext=base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("="),
        nonce=base64.urlsafe_b64encode(b"n" * 12).decode().rstrip("="),
        expires_at=now + timedelta(seconds=121),
        created_at=now,
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [envelope.model_dump(mode="json")]})

    connector = RelayHostConnector(
        HostConnectorConfig(
            base_url="https://relay.example",
            bearer_token="r" * 32,
            route_ref="route_1",
            recipient_ref="host_1",
        ),
        _GatewayService(),
        decrypt_envelope=lambda _item: pytest.fail("invalid envelope must not be decrypted"),
    )
    async with httpx.AsyncClient(
        base_url="https://relay.example", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(ValueError, match="protocol"):
            await connector.poll_once(client)
