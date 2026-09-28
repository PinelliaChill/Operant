"""Isolated real-model acceptance for the installed browser Agent tool."""

from __future__ import annotations

import asyncio
import os
import socket
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread

import httpx
import uvicorn
from fastapi import FastAPI

from operant.api_phase56_target import install_phase56_target_routes
from operant.application.phase45_gateway import Phase45ActionGateway
from operant.application.security import PolicyEngine
from operant.application.service import ApplicationService
from operant.domain.models import Budget, Effort, ModelProfile, RolePreset, ToolPolicy
from operant.domain.security import PolicyBundle, PolicyDecision, PolicyLayer, PolicyRule
from operant.persistence.phase45 import SQLitePhase45Repository
from operant.persistence.security import SQLiteSecurityRepository
from operant.persistence.sqlite import SQLiteStore
from operant.plugins.capability_registry import CapabilityPluginRegistry
from operant.protocol import redact_public_data
from operant.providers.openai_compatible import OpenAICompatibleProvider
from operant.remote.local_browser import (
    BrowserTargetPolicy,
    IsolatedChromeBrowser,
    LocalBrowserConnector,
)
from operant.remote.local_worker import BROWSER_PLUGIN, LocalCapabilityWorker
from operant.remote.tool_extensions import local_capability_tool_extensions
from operant.settings import load_local_env
from sdk.python_client.phase56_generated import PHASE56_PROTOCOL_VERSION, PHASE56_SCHEMA_DIGEST


async def main() -> None:
    if os.environ.get("OPERANT_PHASE4_REAL_MODEL") != "1":
        raise RuntimeError("set OPERANT_PHASE4_REAL_MODEL=1 for this real-model acceptance")
    load_local_env(os.environ.get("OPERANT_ACCEPTANCE_ENV_FILE", ".env"))
    model_id = os.environ["OPERANT_ACCEPTANCE_MODEL_ID"]
    chrome_path = Path(
        os.environ.get(
            "OPERANT_CHROME_PATH", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        )
    )

    class Page(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = (
                b"<title>Operant acceptance</title><body>"
                b"<input id='draft' value=''>"
                b"<button id='go' onclick=\"if("
                b"document.querySelector('#draft').value==='phase four')"
                b"document.body.append(' accepted')\">Submit</button>"
                b"</body>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: object) -> None:
            pass

    page_server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
    page_thread = Thread(target=page_server.serve_forever, daemon=True)
    page_thread.start()
    page_origin = f"http://127.0.0.1:{page_server.server_port}"
    with tempfile.TemporaryDirectory(prefix="operant-phase4-model-") as temporary:
        root = Path(temporary).resolve()
        store = SQLiteStore(root / "core.sqlite3")
        service = ApplicationService(
            store,
            OpenAICompatibleProvider(),
            tool_extension_factory=local_capability_tool_extensions,
        )
        service.initialize()
        profile = service.add_model_profile(
            ModelProfile(
                name="Phase4 acceptance",
                model_id=model_id,
                base_url=os.environ["OPERANT_BASE_URL"],
                secret_ref="OPERANT_API_KEY",
                supported_efforts=(Effort.LOW,),
                default_effort=Effort.LOW,
                effort_parameter=None,
            )
        )
        role = service.create_role(
            RolePreset(
                name="Browser observation acceptance",
                system_prompt=(
                    "Use browser tools to complete the requested local test page. "
                    "Observe before every action, copy the latest observation_hash exactly, "
                    "and use a unique idempotency_key for each action. "
                    "Never claim success without observing the word accepted after clicking."
                ),
                model_profile_id=profile.id,
                effort=Effort.LOW,
                tool_policy=ToolPolicy(
                    allowed_tools=(
                        "ext_browser_observe",
                        "ext_browser_navigate",
                        "ext_browser_fill",
                        "ext_browser_click",
                    )
                ),
                budget=Budget(max_turns=12, timeout_seconds=240),
            )
        )
        session = service.create_session(role.id, workspace_ref=str(root))
        gateway = Phase45ActionGateway(
            SQLiteSecurityRepository(store),
            SQLitePhase45Repository(store),
            PolicyEngine(
                PolicyBundle(
                    bundle_id="browser-model-acceptance",
                    version="test.v1",
                    default_decision=PolicyDecision.DENY,
                    rules=(
                        PolicyRule(
                            rule_id="allow.isolated.browser.observe",
                            layer=PolicyLayer.SYSTEM,
                            decision=PolicyDecision.ALLOW,
                            reason="bounded local acceptance",
                        ),
                    ),
                )
            ),
            principal="test:browser-model",
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

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        port = listener.getsockname()[1]
        core_origin = f"http://127.0.0.1:{port}"
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        server_thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        server_thread.start()
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        if not server.started:
            raise RuntimeError("Core did not start")
        registry = CapabilityPluginRegistry(root / "capability-plugins")
        registry.install(BROWSER_PLUGIN.plugin_id, (page_origin,))
        registry.set_enabled(BROWSER_PLUGIN.plugin_id, True)
        target_id = "local-browser-model-test"
        identity = "public-key-" + "x" * 32
        stop = Event()
        worker_errors: list[str] = []
        pump: Thread | None = None
        worker: LocalCapabilityWorker | None = None
        worker_http: httpx.Client | None = None
        try:
            worker_http = httpx.Client(base_url=core_origin, trust_env=False, timeout=10)
            registration = worker_http.post(
                "/v1/remote-targets",
                json={
                    "target_id": target_id,
                    "display_name": "Temporary browser",
                    "endpoint_ref": BROWSER_PLUGIN.plugin_id,
                    "identity_public_key": identity,
                    "credential_ref": "LOCAL_BROWSER_TOKEN",
                    "policy_ref": "acceptance",
                    "artifact_namespace": "browser-model-test",
                    "capability_manifest": {
                        "version": BROWSER_PLUGIN.protocol_version,
                        "capabilities": sorted(item.value for item in BROWSER_PLUGIN.capabilities),
                        "supported_operations": sorted(BROWSER_PLUGIN.operations),
                        "platform": "macOS",
                    },
                },
            )
            registration.raise_for_status()
            worker_http.post(
                f"/v1/remote-targets/{target_id}/heartbeat",
                json={"identity_public_key": identity},
            ).raise_for_status()
            response = worker_http.post(
                f"/v1/remote-targets/{target_id}/leases",
                json={
                    "owner": "browser-model-acceptance",
                    "workspace_ref": str(root),
                    "ttl_seconds": 180,
                    "idempotency_key": "browser-model-lease",
                },
            )
            response.raise_for_status()
            lease = response.json()
            os.environ.update(
                {
                    "OPERANT_CORE_ORIGIN": core_origin,
                    "OPERANT_BROWSER_TARGET_ID": target_id,
                    "OPERANT_BROWSER_LEASE_ID": lease["lease_id"],
                    "OPERANT_BROWSER_LEASE_FENCING": str(lease["fencing"]),
                    "OPERANT_BROWSER_LEASE_TOKEN": lease["token"],
                }
            )
            browser = IsolatedChromeBrowser(
                chrome_path, BrowserTargetPolicy(frozenset({page_origin}))
            )
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
                client=worker_http,
                lifecycle_check=lambda: registry.get(
                    BROWSER_PLUGIN.plugin_id, require_enabled=True
                ),
            )

            def pump_worker() -> None:
                assert worker is not None
                while not stop.is_set():
                    try:
                        worker.poll_once()
                    except Exception as exc:
                        worker_errors.append(type(exc).__name__)
                        return
                    time.sleep(0.05)

            pump = Thread(target=pump_worker, daemon=True)
            pump.start()
            events = []
            approvals = 0
            async for event in service.run_session(
                session.id,
                user_message=(
                    f"In the dedicated browser open {page_origin}/, fill #draft with "
                    "phase four, click #go, and report whether the page says accepted. "
                    "This page is a temporary local test fixture."
                ),
                workspace=root,
                memory_enabled=False,
            ):
                events.append(event)
                if event.event_type == "tool.approval_required":
                    approvals += 1
                    if approvals > 3:
                        raise RuntimeError("browser test requested too many approvals")
                    if not service.submit_approval(
                        session.id, str(event.payload["tool_call_id"]), approved=True
                    ):
                        raise RuntimeError("browser test approval was not accepted")
            names = [event.event_type for event in events]
            model_events = [event for event in events if event.event_type == "model.completed"]
            redacted_calls = all(
                "input_sha256" in call.get("arguments_json", "")
                for event in model_events
                for call in event.payload.get("tool_calls", [])
                if call.get("name", "").startswith("ext_")
            )
            print("model_id", model_id)
            print("entry", "ApplicationService.run_session")
            print("tool_started", "tool.started" in names)
            print("tool_completed", "tool.completed" in names)
            print("agent_completed", "agent.completed" in names)
            print("worker_errors", worker_errors)
            print("tool_arguments_redacted", redacted_calls)
            print("test_fixture_approvals", approvals)
            final_observation = browser.observe()
            completed_page = "accepted" in str(final_observation.get("text", ""))
            print("page_action_completed", completed_page)
            if not {"tool.started", "tool.completed", "agent.completed"}.issubset(names):
                print("event_types", names)
                for event in events:
                    if event.event_type in {"agent.failed", "agent.stream_error"}:
                        print("failure", redact_public_data(event.payload, max_chars=500))
                raise RuntimeError("real-model browser tool acceptance did not complete")
            if worker_errors:
                raise RuntimeError("local browser worker failed")
            if not redacted_calls:
                raise RuntimeError("extension call arguments were not redacted")
            if not completed_page:
                raise RuntimeError("real-model browser page action was not completed")
        finally:
            stop.set()
            if pump is not None:
                pump.join(timeout=5)
            if worker is not None:
                worker.close()
            if worker_http is not None:
                worker_http.close()
            server.should_exit = True
            server_thread.join(timeout=5)
            listener.close()
            service.close()
    page_server.shutdown()
    page_server.server_close()
    page_thread.join(timeout=5)


asyncio.run(main())
