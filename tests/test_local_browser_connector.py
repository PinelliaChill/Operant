from __future__ import annotations

import json
import os
import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread

import pytest

from operant.domain.remote_execution import (
    RemoteActionIdempotency,
    RemoteCapability,
    RemoteExecutionJob,
    RemoteJobStatus,
)
from operant.remote.local_browser import (
    BrowserTargetError,
    BrowserTargetPolicy,
    IsolatedChromeBrowser,
    LocalBrowserConnector,
    browser_profile_inventory,
    observation_hash,
    reap_stale_browser_profiles,
)
from operant.remote.sealed_input import seal_browser_input


def _job(
    capability: RemoteCapability,
    operation: str,
    arguments: dict[str, object],
) -> RemoteExecutionJob:
    return RemoteExecutionJob(
        target_id="local-browser-test",
        lease_id="lease-test",
        lease_fencing=1,
        capability=capability,
        operation=operation,
        arguments={"target_ref": "local-browser-test", **arguments},
        action_hash="a" * 64,
        idempotency_key=f"{operation}-{len(arguments)}",
        idempotency=RemoteActionIdempotency.NON_IDEMPOTENT,
    )


def test_browser_policy_rejects_private_and_unapproved_hosts() -> None:
    policy = BrowserTargetPolicy(frozenset({"https://10.0.0.1"}))
    with pytest.raises(BrowserTargetError, match="approved origin"):
        policy.check_url("http://10.0.0.1/")
    with pytest.raises(BrowserTargetError, match="private"):
        policy.check_url("https://10.0.0.1/")
    with pytest.raises(BrowserTargetError, match="approved origin"):
        policy.check_url("https://example.com/")
    loopback = BrowserTargetPolicy(frozenset({"http://127.0.0.1:8765"}))
    with pytest.raises(BrowserTargetError, match="approved origin"):
        loopback.check_url("http://127.0.0.1:8766/")
    with pytest.raises(BrowserTargetError, match="port"):
        BrowserTargetPolicy(frozenset({"http://127.0.0.1:0"}))
    with pytest.raises(BrowserTargetError, match="exact origin"):
        BrowserTargetPolicy(frozenset({"https://example.com/private"}))


def test_browser_policy_blocks_core_origin_even_if_allowlisted() -> None:
    policy = BrowserTargetPolicy(
        frozenset({"http://127.0.0.1:8000", "http://127.0.0.1:8765"}),
        blocked_origins=frozenset({"http://127.0.0.1:8000"}),
    )
    with pytest.raises(BrowserTargetError, match="internal control"):
        policy.check_url("http://127.0.0.1:8000/v1/remote-targets")
    assert policy.check_url("http://127.0.0.1:8765/") == "http://127.0.0.1:8765/"


def test_browser_profile_reaper_only_removes_owned_expired_profiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("operant.remote.local_browser.tempfile.gettempdir", lambda: str(tmp_path))
    root = tmp_path / "operant-browser-profiles"
    root.mkdir(mode=0o700)
    old = root / "profile-old"
    old.mkdir()
    (old / "operant-profile-owner.json").write_text(
        json.dumps(
            {
                "schema": "operant-browser-profile.v1",
                "name": old.name,
                "uid": os.getuid(),
                "pid": 99999999,
                "created_at": time.time() - 7200,
            }
        )
    )
    unmarked = root / "profile-unmarked"
    unmarked.mkdir()
    unrelated = root / "other-data"
    unrelated.mkdir()
    inventory = {item.profile_id: item for item in browser_profile_inventory()}
    assert inventory[old.name].state == "eligible"
    assert inventory[unmarked.name].state == "unmanaged"
    assert unrelated.name not in inventory
    assert reap_stale_browser_profiles() == (old.name,)
    assert not old.exists()
    assert unmarked.exists() and unrelated.exists()


def test_real_chrome_observe_navigate_click_and_stale_guard() -> None:
    if os.environ.get("OPERANT_LOCAL_BROWSER_TEST") != "1":
        pytest.skip("set OPERANT_LOCAL_BROWSER_TEST=1 for real Chrome acceptance")
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if not chrome.is_file():
        pytest.skip("local Chrome is not installed")

    class Page(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            content = (
                b"<html><head><title>Operant test</title></head><body>"
                b"<input id='draft' value='original' "
                b"oninput=\"document.querySelector('#echo').textContent=this.value\">"
                b"<span id='echo'></span>"
                b"<input id='password' type='password'>"
                b"<button id='go' onclick=\"document.body.append(' done')\">Go</button>"
                b"</body></html>"
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
    try:
        with IsolatedChromeBrowser(
            chrome,
            BrowserTargetPolicy(frozenset({f"http://127.0.0.1:{server.server_port}"})),
        ) as browser:
            assert browser._profile is not None
            profile_path = Path(browser._profile.name)
            profile = next(
                item for item in browser_profile_inventory() if item.profile_id == profile_path.name
            )
            assert profile.state == "active" and profile.size_bytes > 0
            connector = LocalBrowserConnector(
                target_id="local-browser-test",
                lease_id="lease-test",
                lease_token="token-test-1234567890",
                lease_fencing=1,
                browser=browser,
            )
            observed = connector.execute(
                _job(RemoteCapability.BROWSER_OBSERVE, "observe_browser", {})
            )
            initial = observed.result.postcondition["observation"]
            assert isinstance(initial, dict) and initial["url"] == "about:blank"
            initial_hash = observation_hash("local-browser-test", "local-browser-test", initial)
            url = f"http://127.0.0.1:{server.server_port}/"
            navigated = connector.execute(
                _job(
                    RemoteCapability.BROWSER_NAVIGATE,
                    "navigate",
                    {"observation_hash": initial_hash, "arguments": {"url": url}},
                )
            )
            assert navigated.result.status is RemoteJobStatus.SUCCEEDED
            assert navigated.result.postcondition["title"] == "Operant test"
            stale = connector.execute(
                _job(
                    RemoteCapability.BROWSER_SUBMIT,
                    "click",
                    {"observation_hash": initial_hash, "arguments": {"selector": "#go"}},
                )
            )
            assert stale.result.status is RemoteJobStatus.FAILED
            current_hash = observation_hash(
                "local-browser-test", "local-browser-test", navigated.result.postcondition
            )
            protected = connector.execute(
                _job(
                    RemoteCapability.BROWSER_SUBMIT,
                    "fill",
                    {
                        "observation_hash": current_hash,
                        "arguments": {
                            "selector": "#password",
                            "value_sealed": seal_browser_input(
                                "private",
                                token="token-test-1234567890",
                                target_id="local-browser-test",
                                lease_id="lease-test",
                                fencing=1,
                                observation_hash=current_hash,
                                selector="#password",
                                idempotency_key="fill-2",
                            ),
                        },
                    },
                )
            )
            assert protected.result.status is RemoteJobStatus.FAILED
            filled = connector.execute(
                _job(
                    RemoteCapability.BROWSER_SUBMIT,
                    "fill",
                    {
                        "observation_hash": current_hash,
                        "arguments": {
                            "selector": "#draft",
                            "value_sealed": seal_browser_input(
                                "new query",
                                token="token-test-1234567890",
                                target_id="local-browser-test",
                                lease_id="lease-test",
                                fencing=1,
                                observation_hash=current_hash,
                                selector="#draft",
                                idempotency_key="fill-2",
                            ),
                        },
                    },
                )
            )
            assert filled.result.status is RemoteJobStatus.SUCCEEDED
            assert "new query" not in json.dumps(filled.result.postcondition)
            assert "[entered text]" in filled.result.postcondition["text"]
            current_hash = observation_hash(
                "local-browser-test", "local-browser-test", filled.result.postcondition
            )
            assert browser._cdp is not None
            browser._cdp.evaluate("document.querySelector('#draft').value='user changed'")
            takeover = connector.execute(
                _job(
                    RemoteCapability.BROWSER_SUBMIT,
                    "click",
                    {"observation_hash": current_hash, "arguments": {"selector": "#go"}},
                )
            )
            assert takeover.result.status is RemoteJobStatus.FAILED
            current_hash = observation_hash(
                "local-browser-test", "local-browser-test", browser.observe()
            )
            clicked = connector.execute(
                _job(
                    RemoteCapability.BROWSER_SUBMIT,
                    "click",
                    {"observation_hash": current_hash, "arguments": {"selector": "#go"}},
                )
            )
            assert clicked.result.status is RemoteJobStatus.SUCCEEDED
            assert "done" in clicked.result.postcondition["text"]
        assert not profile_path.exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_real_chrome_blocks_unapproved_websocket_target() -> None:
    if os.environ.get("OPERANT_LOCAL_BROWSER_TEST") != "1":
        pytest.skip("set OPERANT_LOCAL_BROWSER_TEST=1 for real Chrome acceptance")
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if not chrome.is_file():
        pytest.skip("local Chrome is not installed")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(2)
    connected = Event()

    def accept() -> None:
        try:
            incoming, _ = listener.accept()
        except TimeoutError:
            return
        connected.set()
        incoming.close()

    Thread(target=accept, daemon=True).start()

    class Page(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            content = (
                b"<title>Approved page</title><body>Allowed"
                + f"<script>document.body.dataset.attempted='yes';new WebSocket('ws://127.0.0.1:{listener.getsockname()[1]}/blocked')</script>".encode()
                + b"</body>"
            )
            self.send_response(200)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *_: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with IsolatedChromeBrowser(
            chrome, BrowserTargetPolicy(frozenset({f"http://localhost:{server.server_port}"}))
        ) as browser:
            page = browser.navigate(f"http://localhost:{server.server_port}/")
            assert page["title"] == "Approved page"
            assert browser._cdp is not None
            assert browser._cdp.evaluate("document.body.dataset.attempted") == "yes"
            assert not connected.wait(0.5)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        listener.close()
