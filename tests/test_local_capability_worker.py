from __future__ import annotations

import asyncio
import json
import os
import socket
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from operant.api_phase56_target import install_phase56_target_routes
from operant.application.phase45_gateway import Phase45ActionGateway
from operant.application.security import PolicyEngine
from operant.cli import app as operant_cli
from operant.domain.models import ToolPolicy
from operant.domain.remote_execution import (
    CapabilityManifest,
    RemoteActionIdempotency,
    RemoteCapability,
    RemoteExecutionJob,
    RemoteExecutionResult,
    RemoteJobStatus,
    RemoteTargetRegistration,
    RemoteTargetStatus,
)
from operant.domain.security import PolicyBundle, PolicyDecision, PolicyLayer, PolicyRule
from operant.persistence.phase45 import SQLitePhase45Repository
from operant.persistence.security import SQLiteSecurityRepository
from operant.persistence.sqlite import SQLiteStore
from operant.plugins.capability_registry import CapabilityPluginRegistry
from operant.protocol import canonical_action_hash
from operant.remote.connector import RemoteOutcomeUnknown
from operant.remote.local_browser import (
    BrowserTargetPolicy,
    IsolatedChromeBrowser,
    LocalBrowserConnector,
)
from operant.remote.local_computer import LocalComputerConnector
from operant.remote.local_worker import (
    BROWSER_PLUGIN,
    COMPUTER_PLUGIN,
    LocalCapabilityWorker,
    browser_worker,
)
from operant.remote.operator import (
    BrowserCapabilityOperator,
    CapabilityLeaseBinding,
    ComputerCapabilityOperator,
)
from operant.remote.sealed_input import seal_browser_input
from operant.remote.tool_extensions import local_capability_tool_extensions
from operant.tools.workspace import WorkspaceTools
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
        thread.join(timeout=5)
        listener.close()
        raise RuntimeError("local Core did not start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()


def test_real_browser_plugin_core_gateway_observation_and_action(tmp_path: Path) -> None:
    if os.environ.get("OPERANT_LOCAL_BROWSER_TEST") != "1":
        pytest.skip("set OPERANT_LOCAL_BROWSER_TEST=1 for real Chrome acceptance")
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if not chrome.is_file():
        pytest.skip("local Chrome is not installed")

    class Page(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            content = (
                b"<title>Operant</title>"
                b"<input id='query'>"
                b"<button id='go' onclick=\"document.body.append(' done')\">Go</button>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *_: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    store = SQLiteStore(tmp_path / "core.sqlite3")
    store.initialize()
    gateway = Phase45ActionGateway(
        SQLiteSecurityRepository(store),
        SQLitePhase45Repository(store),
        PolicyEngine(
            PolicyBundle(
                bundle_id="browser-test",
                version="test.v1",
                default_decision=PolicyDecision.DENY,
                rules=(
                    PolicyRule(
                        rule_id="test.allow",
                        layer=PolicyLayer.SYSTEM,
                        decision=PolicyDecision.ALLOW,
                        reason="isolated browser simulation",
                    ),
                ),
            )
        ),
        principal="test:local-browser",
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

    plugin_registry = CapabilityPluginRegistry(tmp_path / "capability-plugins")
    page_origin = f"http://127.0.0.1:{server.server_port}"
    plugin_registry.install(BROWSER_PLUGIN.plugin_id, (page_origin,))
    plugin_registry.set_enabled(BROWSER_PLUGIN.plugin_id, True)
    target_id = "local-browser-test"
    identity = "public-key-" + "x" * 32
    try:
        with (
            _running_core(app) as core_origin,
            httpx.Client(base_url=core_origin, trust_env=False) as client,
        ):
            registered = client.post(
                "/v1/remote-targets",
                json={
                    "target_id": target_id,
                    "display_name": "Isolated browser",
                    "endpoint_ref": BROWSER_PLUGIN.plugin_id,
                    "identity_public_key": identity,
                    "credential_ref": "LOCAL_BROWSER_TOKEN",
                    "policy_ref": "test",
                    "artifact_namespace": "browser-test",
                    "capability_manifest": {
                        "version": "phase56.v1",
                        "capabilities": sorted(item.value for item in BROWSER_PLUGIN.capabilities),
                        "supported_operations": sorted(BROWSER_PLUGIN.operations),
                        "platform": "macOS",
                    },
                },
            )
            assert registered.status_code == 201, registered.text
            assert (
                client.post(
                    f"/v1/remote-targets/{target_id}/heartbeat",
                    json={"identity_public_key": identity},
                ).status_code
                == 200
            )
            lease_response = client.post(
                f"/v1/remote-targets/{target_id}/leases",
                json={
                    "owner": "local-browser-test",
                    "workspace_ref": str(tmp_path),
                    "ttl_seconds": 180,
                    "idempotency_key": "lease-test",
                },
            )
            assert lease_response.status_code == 201, lease_response.text
            lease = lease_response.json()
            binding = {
                "lease_id": lease["lease_id"],
                "token": lease["token"],
                "fencing": lease["fencing"],
            }
            bypass = client.post(
                f"/v1/remote-targets/{target_id}/jobs",
                json={
                    **binding,
                    "capability": "browser.submit",
                    "operation": "fill",
                    "arguments": {"value": "sk-abcdefghijklmnopqrstuv"},
                    "idempotency_key": "reject-raw-local-job",
                    "idempotency": "non_idempotent",
                },
            )
            assert bypass.status_code == 403
            with IsolatedChromeBrowser(
                chrome, BrowserTargetPolicy(frozenset({page_origin}))
            ) as browser:
                worker = LocalCapabilityWorker(
                    core_origin=core_origin,
                    plugin=BROWSER_PLUGIN,
                    connector=LocalBrowserConnector(
                        target_id=target_id,
                        lease_id=lease["lease_id"],
                        lease_token=lease["token"],
                        lease_fencing=lease["fencing"],
                        browser=browser,
                    ),
                    client=client,
                    lifecycle_check=lambda: plugin_registry.get(
                        BROWSER_PLUGIN.plugin_id, require_enabled=True
                    ),
                )

                def observe() -> tuple[str, dict[str, object]]:
                    response = client.post(
                        f"/v1/browser/{target_id}/observe",
                        json={**binding, "target_ref": target_id, "idempotency_key": uuid4().hex},
                    )
                    assert response.status_code == 200, response.text
                    result = worker.poll_once()[0]
                    assert result.status is RemoteJobStatus.SUCCEEDED
                    body = result.postcondition["observation"]
                    assert isinstance(body, dict)
                    observation_hash = canonical_action_hash(
                        {"target_id": target_id, "target_ref": target_id, "body": body}
                    )
                    readback = client.get(f"/v1/remote-targets/jobs/{result.job_id}/result")
                    assert readback.status_code == 200
                    assert readback.json()["observation"]["observation_hash"] == observation_hash
                    return observation_hash, body

                initial_hash, initial = observe()
                assert initial["url"] == "about:blank"
                url = f"http://127.0.0.1:{server.server_port}/"
                navigation = client.post(
                    "/v1/browser/act",
                    json={
                        **binding,
                        "action": {
                            "target_id": target_id,
                            "capability": "browser.navigate",
                            "operation": "navigate",
                            "target_ref": target_id,
                            "observation_hash": initial_hash,
                            "arguments": {"url": url},
                            "idempotency_key": "navigate-test",
                            "idempotency": "non_idempotent",
                        },
                    },
                )
                assert navigation.status_code == 200, navigation.text
                assert worker.poll_once()[0].status is RemoteJobStatus.SUCCEEDED
                current_hash, current = observe()
                assert current["title"] == "Operant"
                prior_jobs = len(
                    client.get(f"/v1/remote-targets/jobs?target_id={target_id}").json()["items"]
                )
                credential_attempt = client.post(
                    "/v1/browser/act",
                    json={
                        **binding,
                        "action": {
                            "target_id": target_id,
                            "capability": "browser.submit",
                            "operation": "fill",
                            "target_ref": target_id,
                            "observation_hash": current_hash,
                            "arguments": {
                                "selector": "#query",
                                "value": "sk-abcdefghijklmnopqrstuv",
                            },
                            "idempotency_key": "reject-credential-before-storage",
                            "idempotency": "non_idempotent",
                        },
                    },
                )
                assert credential_attempt.status_code == 422
                assert (
                    len(
                        client.get(f"/v1/remote-targets/jobs?target_id={target_id}").json()["items"]
                    )
                    == prior_jobs
                )
                filled = client.post(
                    "/v1/browser/act",
                    json={
                        **binding,
                        "action": {
                            "target_id": target_id,
                            "capability": "browser.submit",
                            "operation": "fill",
                            "target_ref": target_id,
                            "observation_hash": current_hash,
                            "arguments": {
                                "selector": "#query",
                                "value_sealed": seal_browser_input(
                                    "sample search",
                                    token=lease["token"],
                                    target_id=target_id,
                                    lease_id=lease["lease_id"],
                                    fencing=lease["fencing"],
                                    observation_hash=current_hash,
                                    selector="#query",
                                    idempotency_key="fill-test",
                                ),
                            },
                            "idempotency_key": "fill-test",
                            "idempotency": "non_idempotent",
                        },
                    },
                )
                assert filled.status_code == 200, filled.text
                persisted_fill = client.get(
                    f"/v1/remote-targets/jobs/{filled.json()['job']['job_id']}/result"
                )
                assert persisted_fill.status_code == 200
                assert "sample search" not in persisted_fill.text
                assert "value_sealed" not in persisted_fill.text
                with store._connect() as connection:
                    stored_arguments = connection.execute(
                        "SELECT arguments_json FROM remote_execution_jobs WHERE job_id=?",
                        (filled.json()["job"]["job_id"],),
                    ).fetchone()[0]
                assert "sample search" not in stored_arguments
                assert "value_sealed" in stored_arguments
                fill_result = worker.poll_once()[0]
                assert fill_result.status is RemoteJobStatus.SUCCEEDED
                assert "sample search" not in str(fill_result.postcondition)
                fill_readback = client.get(f"/v1/remote-targets/jobs/{fill_result.job_id}/result")
                assert fill_readback.status_code == 200
                assert fill_readback.json()["status"] == "succeeded"
                assert "sample search" not in fill_readback.text
                assert "arguments" not in fill_readback.json()
                cli_status = CliRunner().invoke(
                    operant_cli,
                    [
                        "capability-browser",
                        "status",
                        fill_result.job_id,
                        "--core-origin",
                        core_origin,
                    ],
                )
                assert cli_status.exit_code == 0, cli_status.output
                assert "succeeded" in cli_status.output
                assert "sample search" not in cli_status.output
                assert lease["token"] not in cli_status.output
                current_hash, _ = observe()
                clicked = client.post(
                    "/v1/browser/act",
                    json={
                        **binding,
                        "action": {
                            "target_id": target_id,
                            "capability": "browser.submit",
                            "operation": "click",
                            "target_ref": target_id,
                            "observation_hash": current_hash,
                            "arguments": {"selector": "#go"},
                            "idempotency_key": "click-test",
                            "idempotency": "non_idempotent",
                        },
                    },
                )
                assert clicked.status_code == 200, clicked.text
                result = worker.poll_once()[0]
                assert result.status is RemoteJobStatus.SUCCEEDED
                assert "done" in result.postcondition["text"]
                stop = Event()
                worker_errors: list[str] = []

                def pump_worker() -> None:
                    while not stop.is_set():
                        try:
                            worker.poll_once()
                        except Exception as exc:
                            worker_errors.append(type(exc).__name__)
                            return
                        time.sleep(0.05)

                pump = Thread(target=pump_worker, daemon=True)
                pump.start()
                try:
                    with httpx.Client(trust_env=False, timeout=5) as operator_http:

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

                        operator = BrowserCapabilityOperator(
                            Phase56Client(core_origin, transport=transport),
                            CapabilityLeaseBinding(
                                target_id=target_id,
                                lease_id=lease["lease_id"],
                                token=lease["token"],
                                fencing=lease["fencing"],
                            ),
                            BrowserTargetPolicy(frozenset({page_origin})),
                            wait_seconds=10,
                        )
                        operator_observation = operator.observe()["observation"]
                        operator_result = operator.act(
                            "fill",
                            observation_hash=operator_observation["observation_hash"],
                            idempotency_key="operator-fill-test",
                            selector="#query",
                            value="operator path",
                        )
                        assert operator_result["status"] == "succeeded"
                        assert "operator path" not in json.dumps(operator_result)
                        cli_env = {
                            "OPERANT_DB_PATH": str(tmp_path / "core.sqlite3"),
                            "OPERANT_TARGET_LEASE_TOKEN": lease["token"],
                            "OPERANT_BROWSER_TARGET_ID": target_id,
                            "OPERANT_BROWSER_LEASE_ID": lease["lease_id"],
                            "OPERANT_BROWSER_LEASE_FENCING": str(lease["fencing"]),
                            "OPERANT_CORE_ORIGIN": core_origin,
                        }
                        cli_observe = CliRunner().invoke(
                            operant_cli, ["capability-browser", "observe"], env=cli_env
                        )
                        assert cli_observe.exit_code == 0, cli_observe.output
                        cli_hash = json.loads(cli_observe.output)["observation"]["observation_hash"]
                        cli_fill = CliRunner().invoke(
                            operant_cli,
                            [
                                "capability-browser",
                                "fill",
                                "--selector",
                                "#query",
                                "--observation-hash",
                                cli_hash,
                                "--idempotency-key",
                                "cli-fill-test",
                            ],
                            input="cli value",
                            env=cli_env,
                        )
                        assert cli_fill.exit_code == 0, cli_fill.output
                        assert "cli value" not in cli_fill.output
                        cli_observe_again = CliRunner().invoke(
                            operant_cli, ["capability-browser", "observe"], env=cli_env
                        )
                        assert cli_observe_again.exit_code == 0, cli_observe_again.output
                        click_hash = json.loads(cli_observe_again.output)["observation"][
                            "observation_hash"
                        ]
                        cli_click = CliRunner().invoke(
                            operant_cli,
                            [
                                "capability-browser",
                                "click",
                                "--selector",
                                "#go",
                                "--observation-hash",
                                click_hash,
                                "--idempotency-key",
                                "cli-click-test",
                            ],
                            env=cli_env,
                        )
                        assert cli_click.exit_code == 0, cli_click.output
                        assert lease["token"] not in cli_click.output
                        cli_observe_before_navigation = CliRunner().invoke(
                            operant_cli, ["capability-browser", "observe"], env=cli_env
                        )
                        assert cli_observe_before_navigation.exit_code == 0, (
                            cli_observe_before_navigation.output
                        )
                        navigation_hash = json.loads(cli_observe_before_navigation.output)[
                            "observation"
                        ]["observation_hash"]
                        cli_navigate = CliRunner().invoke(
                            operant_cli,
                            [
                                "capability-browser",
                                "navigate",
                                "--url",
                                f"{page_origin}/again",
                                "--observation-hash",
                                navigation_hash,
                                "--idempotency-key",
                                "cli-navigate-test",
                            ],
                            env=cli_env,
                        )
                        assert cli_navigate.exit_code == 0, cli_navigate.output
                        assert lease["token"] not in cli_navigate.output
                        with patch.dict(
                            os.environ,
                            {
                                "OPERANT_BROWSER_TARGET_ID": target_id,
                                "OPERANT_BROWSER_LEASE_ID": lease["lease_id"],
                                "OPERANT_BROWSER_LEASE_FENCING": str(lease["fencing"]),
                                "OPERANT_BROWSER_LEASE_TOKEN": lease["token"],
                                "OPERANT_CORE_ORIGIN": core_origin,
                            },
                        ):
                            tool_policy = ToolPolicy(allowed_tools=("ext_browser_observe",))
                            extensions = local_capability_tool_extensions(
                                tmp_path / "core.sqlite3", tool_policy
                            )
                            tools = WorkspaceTools(
                                tmp_path, policy=tool_policy, extensions=extensions
                            )
                            tool_result = json.loads(
                                asyncio.run(tools.execute("ext_browser_observe", {}))
                            )
                            assert tool_result["observation"]["body"]["url"] == (
                                f"{page_origin}/again"
                            )
                finally:
                    stop.set()
                    pump.join(timeout=5)
                assert not worker_errors
                plugin_registry.set_enabled(BROWSER_PLUGIN.plugin_id, False)
                with pytest.raises(PermissionError, match="disabled"):
                    worker.poll_once()
                worker.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_browser_worker_blocks_its_core_control_endpoint() -> None:
    worker = browser_worker(
        core_origin="http://127.0.0.1:8000",
        target_id="browser-test",
        lease_id="lease-test",
        lease_token="test-lease-token-123456",
        lease_fencing=1,
        allowed_origins=frozenset({"http://127.0.0.1:8000", "http://127.0.0.1:8765"}),
        chrome_path=Path("/nonexistent/chrome"),
    )
    try:
        assert isinstance(worker.connector, LocalBrowserConnector)
        assert worker.connector.browser.blocked_ports == frozenset({8000})
        with pytest.raises(RuntimeError, match="internal control"):
            worker.connector.browser.policy.check_url("http://127.0.0.1:8000/v1")
    finally:
        worker.close()


def test_simulated_computer_plugin_uses_core_gateway_and_durable_result(tmp_path: Path) -> None:
    class FakeComputer:
        clicks = 0

        def observe(self) -> dict[str, object]:
            return {
                "bundle_id": "dev.operant.acceptance",
                "window_title": "Clicked" if self.clicks else "Pending",
                "buttons": ["Run Check"],
            }

        def click_button(
            self, *, expected: dict[str, object], button_name: str
        ) -> dict[str, object]:
            assert expected == self.observe() and button_name == "Run Check"
            self.clicks += 1
            return self.observe()

    store = SQLiteStore(tmp_path / "core.sqlite3")
    store.initialize()
    gateway = Phase45ActionGateway(
        SQLiteSecurityRepository(store),
        SQLitePhase45Repository(store),
        PolicyEngine(
            PolicyBundle(
                bundle_id="computer-test",
                version="test.v1",
                default_decision=PolicyDecision.DENY,
                rules=(
                    PolicyRule(
                        rule_id="test.allow",
                        layer=PolicyLayer.SYSTEM,
                        decision=PolicyDecision.ALLOW,
                        reason="isolated computer simulation",
                    ),
                ),
            )
        ),
        principal="test:local-computer",
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
    target_id = "local-computer-test"
    identity = "public-key-" + "x" * 32
    core_origin = "http://127.0.0.1:8000"
    with TestClient(app) as worker_http:
        registered = worker_http.post(
            "/v1/remote-targets",
            json={
                "target_id": target_id,
                "display_name": "Temporary computer",
                "endpoint_ref": COMPUTER_PLUGIN.plugin_id,
                "identity_public_key": identity,
                "credential_ref": "LOCAL_COMPUTER_TOKEN",
                "policy_ref": "test",
                "artifact_namespace": "computer-test",
                "capability_manifest": {
                    "version": "phase56.v1",
                    "capabilities": sorted(item.value for item in COMPUTER_PLUGIN.capabilities),
                    "supported_operations": sorted(COMPUTER_PLUGIN.operations),
                    "platform": "macOS",
                },
            },
        )
        assert registered.status_code == 201, registered.text
        assert (
            worker_http.post(
                f"/v1/remote-targets/{target_id}/heartbeat",
                json={"identity_public_key": identity},
            ).status_code
            == 200
        )
        lease_response = worker_http.post(
            f"/v1/remote-targets/{target_id}/leases",
            json={
                "owner": "computer-test",
                "workspace_ref": str(tmp_path),
                "ttl_seconds": 180,
                "idempotency_key": "computer-lease-test",
            },
        )
        assert lease_response.status_code == 201, lease_response.text
        lease = lease_response.json()
        computer = FakeComputer()
        worker = LocalCapabilityWorker(
            core_origin=core_origin,
            plugin=COMPUTER_PLUGIN,
            connector=LocalComputerConnector(
                target_id=target_id,
                lease_id=lease["lease_id"],
                lease_token=lease["token"],
                lease_fencing=lease["fencing"],
                computer=computer,  # type: ignore[arg-type]
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
            with TestClient(app) as operator_http:

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
                observation = operator.observe()["observation"]
                assert observation["body"]["window_title"] == "Pending"
                result = operator.click_button(
                    button_name="Run Check",
                    observation_hash=observation["observation_hash"],
                    idempotency_key="computer-click-test",
                )
                assert result["status"] == "succeeded"
                assert result["result"]["postcondition"]["window_title"] == "Clicked"
                assert computer.clicks == 1
                readback = operator.client.get_remote_target_job_result(result["job_id"])
                assert readback["status"] == "succeeded"
        finally:
            stop.set()
            pump_thread.join(timeout=5)
            worker.close()
        assert not failures


def test_unknown_browser_click_requires_manual_reconciliation_even_if_marked_idempotent() -> None:
    target = RemoteTargetRegistration(
        target_id="browser-test",
        display_name="Browser test",
        endpoint_ref=BROWSER_PLUGIN.plugin_id,
        identity_public_key="public-key-" + "x" * 32,
        credential_ref="BROWSER_TEST_TOKEN",
        policy_ref="test",
        artifact_namespace="browser-test",
        capability_manifest=CapabilityManifest(
            version=BROWSER_PLUGIN.protocol_version,
            capabilities=tuple(BROWSER_PLUGIN.capabilities),
            supported_operations=tuple(BROWSER_PLUGIN.operations),
            platform="test",
        ),
        status=RemoteTargetStatus.ONLINE,
    )
    job = RemoteExecutionJob(
        target_id=target.target_id,
        lease_id="lease-test",
        lease_fencing=1,
        capability=RemoteCapability.BROWSER_SUBMIT,
        operation="click",
        arguments={"target_ref": target.target_id},
        action_hash="a" * 64,
        idempotency_key="click-test",
        idempotency=RemoteActionIdempotency.IDEMPOTENT,
    )
    submitted: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=target.model_dump(mode="json"))
        if request.url.path.endswith("/renew"):
            return httpx.Response(200, json={})
        if request.url.path.endswith("/poll"):
            return httpx.Response(200, json={"items": [job.model_dump(mode="json")]})
        payload = json.loads(request.content)
        submitted.append(payload)
        result = RemoteExecutionResult(
            result_id=payload["result_id"],
            job_id=job.job_id,
            result_idempotency_key=payload["result_idempotency_key"],
            status=RemoteJobStatus(payload["status"]),
            error_code=payload["error_code"],
        )
        return httpx.Response(200, json={"result": result.model_dump(mode="json")})

    class UnknownConnector:
        target_id = target.target_id
        lease_id = "lease-test"
        lease_token = "token-test-1234567890"
        lease_fencing = 1

        def execute(self, _: RemoteExecutionJob) -> None:
            raise RemoteOutcomeUnknown("unknown click outcome")

        def cancel(self, _: str) -> None:
            pass

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        worker = LocalCapabilityWorker(
            core_origin="http://127.0.0.1:8000",
            plugin=BROWSER_PLUGIN,
            connector=UnknownConnector(),  # type: ignore[arg-type]
            client=client,
        )
        result = worker.poll_once()[0]
    assert result.status is RemoteJobStatus.MANUAL_RECONCILE_REQUIRED
    assert submitted[0]["status"] == "manual_reconcile_required"
