"""Opt-in macOS accessibility acceptance against an isolated temporary App."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Thread

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from operant.api_phase56_target import install_phase56_target_routes
from operant.application.phase45_gateway import Phase45ActionGateway
from operant.application.security import PolicyEngine
from operant.domain.security import PolicyBundle, PolicyDecision, PolicyLayer, PolicyRule
from operant.persistence.phase45 import SQLitePhase45Repository
from operant.persistence.security import SQLiteSecurityRepository
from operant.persistence.sqlite import SQLiteStore
from operant.plugins.capability_registry import CapabilityPluginRegistry
from operant.remote.local_computer import (
    ComputerTargetError,
    ComputerTargetPolicy,
    LocalComputerConnector,
    MacComputer,
)
from operant.remote.local_worker import COMPUTER_PLUGIN, LocalCapabilityWorker
from operant.remote.operator import CapabilityLeaseBinding, ComputerCapabilityOperator
from sdk.python_client.phase56_generated import (
    PHASE56_PROTOCOL_VERSION,
    PHASE56_SCHEMA_DIGEST,
    Phase56Client,
)
from sdk.python_client.transport import TransportRequest, TransportResponse


@contextmanager
def _running_core(app: FastAPI) -> Iterator[str]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False)
    )
    thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=3)
        listener.close()
        raise RuntimeError("temporary computer Core did not start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()


def test_real_frontmost_app_button_click(tmp_path: Path) -> None:
    if os.environ.get("OPERANT_LOCAL_COMPUTER_TEST") != "1":
        pytest.skip("set OPERANT_LOCAL_COMPUTER_TEST=1 for real macOS accessibility acceptance")
    if sys.platform != "darwin" or shutil.which("swiftc") is None:
        pytest.skip("macOS and swiftc are required")
    source = Path(__file__).parent / "fixtures" / "local_computer"
    contents = tmp_path / "OperantAcceptance.app" / "Contents"
    executable = contents / "MacOS" / "OperantAcceptance"
    executable.parent.mkdir(parents=True)
    shutil.copyfile(source / "Info.plist", contents / "Info.plist")
    compile_env = {
        **os.environ,
        "SWIFT_MODULECACHE_PATH": str(tmp_path / "swift-cache"),
        "CLANG_MODULE_CACHE_PATH": str(tmp_path / "clang-cache"),
    }
    subprocess.run(
        ["swiftc", str(source / "Acceptance.swift"), "-o", str(executable)],
        env=compile_env,
        check=True,
        timeout=120,
        capture_output=True,
    )
    process = subprocess.Popen(
        [str(executable)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        env={
            "HOME": os.environ.get("HOME", str(Path.home())),
            "TMPDIR": os.environ.get("TMPDIR", "/private/tmp"),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": os.environ.get("LANG", "C.UTF-8"),
        },
    )
    try:
        computer = MacComputer(ComputerTargetPolicy(frozenset({"dev.operant.acceptance"})))
        deadline = time.monotonic() + 5
        while True:
            try:
                observation = computer.observe()
                break
            except ComputerTargetError:
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        assert observation["bundle_id"] == "dev.operant.acceptance"
        assert "Run Check" in observation["buttons"]
        _formal_computer_roundtrip(tmp_path, computer)
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        shutil.rmtree(contents.parent)


def _formal_computer_roundtrip(tmp_path: Path, computer: MacComputer) -> None:
    store = SQLiteStore(tmp_path / "core.sqlite3")
    store.initialize()
    gateway = Phase45ActionGateway(
        SQLiteSecurityRepository(store),
        SQLitePhase45Repository(store),
        PolicyEngine(
            PolicyBundle(
                bundle_id="real-computer-acceptance",
                version="test.v1",
                default_decision=PolicyDecision.DENY,
                rules=(
                    PolicyRule(
                        rule_id="allow.acceptance.app",
                        layer=PolicyLayer.SYSTEM,
                        decision=PolicyDecision.ALLOW,
                        reason="isolated real computer acceptance",
                    ),
                ),
            )
        ),
        principal="test:real-computer",
    )
    app = FastAPI()
    install_phase56_target_routes(app, store, action_gateway=gateway)

    @app.get("/v1/protocol/phase56")
    def protocol() -> dict[str, object]:
        return {
            "protocol_version": PHASE56_PROTOCOL_VERSION,
            "schema_digest": PHASE56_SCHEMA_DIGEST,
            "min_client_version": PHASE56_PROTOCOL_VERSION,
            "capabilities": ["remote_control", "remote_execution", "multi_writer"],
        }

    registry = CapabilityPluginRegistry(tmp_path / "capability-plugins")
    registry.install(COMPUTER_PLUGIN.plugin_id, ("dev.operant.acceptance",))
    registry.set_enabled(COMPUTER_PLUGIN.plugin_id, True)
    target_id = "real-computer-test"
    identity = "public-key-" + "x" * 32
    with (
        _running_core(app) as core_origin,
        httpx.Client(base_url=core_origin, trust_env=False, timeout=10) as worker_http,
    ):
        registered = worker_http.post(
            "/v1/remote-targets",
            json={
                "target_id": target_id,
                "display_name": "Temporary computer",
                "endpoint_ref": COMPUTER_PLUGIN.plugin_id,
                "identity_public_key": identity,
                "credential_ref": "LOCAL_COMPUTER_TOKEN",
                "policy_ref": "test",
                "artifact_namespace": "real-computer-test",
                "capability_manifest": {
                    "version": COMPUTER_PLUGIN.protocol_version,
                    "capabilities": sorted(item.value for item in COMPUTER_PLUGIN.capabilities),
                    "supported_operations": sorted(COMPUTER_PLUGIN.operations),
                    "platform": "macOS",
                },
            },
        )
        assert registered.status_code == 201, registered.text
        heartbeat = worker_http.post(
            f"/v1/remote-targets/{target_id}/heartbeat",
            json={"identity_public_key": identity},
        )
        assert heartbeat.status_code == 200, heartbeat.text
        lease_response = worker_http.post(
            f"/v1/remote-targets/{target_id}/leases",
            json={
                "owner": "real-computer-acceptance",
                "workspace_ref": str(tmp_path),
                "ttl_seconds": 180,
                "idempotency_key": "real-computer-lease",
            },
        )
        assert lease_response.status_code == 201, lease_response.text
        lease = lease_response.json()
        worker = LocalCapabilityWorker(
            core_origin=core_origin,
            plugin=COMPUTER_PLUGIN,
            connector=LocalComputerConnector(
                target_id=target_id,
                lease_id=lease["lease_id"],
                lease_token=lease["token"],
                lease_fencing=lease["fencing"],
                computer=computer,
            ),
            client=worker_http,
            lifecycle_check=lambda: registry.get(COMPUTER_PLUGIN.plugin_id, require_enabled=True),
        )
        stop = Event()
        failures: list[str] = []

        def pump() -> None:
            while not stop.is_set():
                try:
                    worker.poll_once()
                except Exception as exc:
                    failures.append(type(exc).__name__)
                    return
                time.sleep(0.05)

        pump_thread = Thread(target=pump, daemon=True)
        pump_thread.start()
        try:
            with httpx.Client(base_url=core_origin, trust_env=False, timeout=10) as operator_http:

                def transport(request: TransportRequest) -> TransportResponse:
                    response = operator_http.request(
                        request.method,
                        request.url,
                        headers=dict(request.headers),
                        content=request.body,
                    )
                    return TransportResponse(
                        status=response.status_code,
                        headers=dict(response.headers),
                        body=response.content,
                    )

                operator = ComputerCapabilityOperator(
                    Phase56Client(core_origin, transport=transport),
                    CapabilityLeaseBinding(
                        target_id=target_id,
                        lease_id=lease["lease_id"],
                        token=lease["token"],
                        fencing=lease["fencing"],
                    ),
                    frozenset({"dev.operant.acceptance"}),
                    wait_seconds=10,
                )
                observed = operator.observe()["observation"]
                assert observed["body"]["window_title"] == "Operant Acceptance"
                result = operator.click_button(
                    button_name="Run Check",
                    observation_hash=observed["observation_hash"],
                    idempotency_key="real-computer-click",
                )
                assert result["status"] == "succeeded"
                postcondition = result["result"]["postcondition"]
                assert postcondition["window_title"] == "Operant Acceptance Clicked"
                assert postcondition["pre_observation_hash"] == observed["observation_hash"]
                readback = operator.client.get_remote_target_job_result(result["job_id"])
                assert readback["status"] == "succeeded"
        finally:
            stop.set()
            pump_thread.join(timeout=5)
            worker.close()
        assert not failures
