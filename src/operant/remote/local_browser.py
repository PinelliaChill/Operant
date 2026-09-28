"""An isolated, allowlisted Chrome target for the existing capability job protocol.

The adapter owns a fresh browser profile.  It accepts only typed operations;
neither callers nor page content can supply JavaScript or CDP method names.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import queue
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import SplitResult, urlsplit, urlunsplit

import httpx
from pydantic import JsonValue
from websockets.sync.client import ClientConnection, connect

from operant.domain.remote_execution import (
    RemoteCapability,
    RemoteExecutionJob,
    RemoteExecutionResult,
    RemoteJobStatus,
)
from operant.protocol import canonical_action_hash, redact_public_text
from operant.remote.browser_proxy import BrowserNetworkProxy
from operant.remote.connector import ConnectorOutcome, RemoteOutcomeUnknown
from operant.remote.sealed_input import open_browser_input


class BrowserTargetError(RuntimeError):
    """A rejected target or a browser protocol failure before any input."""


_PROFILE_NAME = re.compile(r"^profile-[a-z0-9_-]+$")


def _profile_root() -> Path:
    root = Path(tempfile.gettempdir()) / "operant-browser-profiles"
    if root.is_symlink():
        raise BrowserTargetError("browser profile root is a symlink")
    root.mkdir(mode=0o700, exist_ok=True)
    info = root.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise BrowserTargetError("browser profile root is not private")
    return root


@dataclass(frozen=True)
class BrowserProfileSummary:
    profile_id: str
    size_bytes: int
    created_at: float | None
    state: str
    retention_reason: str


def browser_profile_inventory(*, min_age_seconds: int = 3600) -> tuple[BrowserProfileSummary, ...]:
    """Preview only managed browser profiles; never read page data or cookies."""
    if min_age_seconds < 3600:
        raise ValueError("browser profile grace period must be at least one hour")
    root = _profile_root()
    summaries: list[BrowserProfileSummary] = []
    for profile in sorted(root.iterdir()):
        if (
            profile.is_symlink()
            or not profile.is_dir()
            or not _PROFILE_NAME.fullmatch(profile.name)
        ):
            continue
        size = 0
        for walk_root, _, files in os.walk(profile, followlinks=False):
            for filename in files:
                try:
                    size += (Path(walk_root) / filename).lstat().st_size
                except OSError:
                    continue
        marker = profile / "operant-profile-owner.json"
        created_at: float | None = None
        state, reason = "unmanaged", "missing or invalid ownership marker"
        if not marker.is_symlink() and marker.is_file() and profile.stat().st_uid == os.getuid():
            try:
                owner = json.loads(marker.read_text())
                if (
                    owner.get("schema") == "operant-browser-profile.v1"
                    and owner.get("name") == profile.name
                    and owner.get("uid") == os.getuid()
                ):
                    pid = int(owner["pid"])
                    created_at = float(owner["created_at"])
                    if pid > 0:
                        try:
                            os.kill(pid, 0)
                        except ProcessLookupError:
                            if time.time() - created_at >= min_age_seconds:
                                state, reason = "eligible", "owner exited and grace period elapsed"
                            else:
                                state, reason = "grace", "owner exited; grace period active"
                        except PermissionError:
                            state, reason = "held", "owner process cannot be verified"
                        else:
                            state, reason = "active", "owner process is running"
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                pass
        summaries.append(
            BrowserProfileSummary(
                profile_id=profile.name,
                size_bytes=size,
                created_at=created_at,
                state=state,
                retention_reason=reason,
            )
        )
    return tuple(summaries)


def reap_stale_browser_profiles(*, min_age_seconds: int = 3600) -> tuple[str, ...]:
    """Remove only our expired, unowned temporary profiles after a crash."""
    if min_age_seconds < 3600:
        raise ValueError("browser profile grace period must be at least one hour")
    root = _profile_root()
    removed: list[str] = []
    for profile in root.iterdir():
        if (
            profile.is_symlink()
            or not profile.is_dir()
            or not _PROFILE_NAME.fullmatch(profile.name)
        ):
            continue
        marker = profile / "operant-profile-owner.json"
        if marker.is_symlink() or not marker.is_file() or profile.stat().st_uid != os.getuid():
            continue
        try:
            owner = json.loads(marker.read_text())
            if (
                owner.get("schema") != "operant-browser-profile.v1"
                or owner.get("name") != profile.name
                or owner.get("uid") != os.getuid()
            ):
                continue
            pid = int(owner["pid"])
            created_at = float(owner["created_at"])
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            continue
        if time.time() - created_at < min_age_seconds or pid <= 0:
            continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        except PermissionError:
            continue
        else:
            continue
        shutil.rmtree(profile)
        removed.append(profile.name)
    return tuple(removed)


@dataclass(frozen=True)
class BrowserTargetPolicy:
    allowed_origins: frozenset[str]
    blocked_origins: frozenset[str] = frozenset()
    max_observation_chars: int = 12_000

    def __post_init__(self) -> None:
        if not self.allowed_origins:
            raise BrowserTargetError("browser origin allowlist is empty")
        normalized: set[str] = set()
        for value in self.allowed_origins:
            parsed = urlsplit(value)
            if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
                raise BrowserTargetError("browser allowlist requires an exact origin")
            origin = self._origin(parsed)
            host = parsed.hostname or ""
            if parsed.scheme == "http" and host not in {"localhost", "127.0.0.1"}:
                raise BrowserTargetError("public browser origins require HTTPS")
            normalized.add(origin)
        object.__setattr__(self, "allowed_origins", frozenset(normalized))
        blocked: set[str] = set()
        for value in self.blocked_origins:
            parsed = urlsplit(value)
            if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
                raise BrowserTargetError("blocked browser target requires an exact origin")
            blocked.add(self._origin(parsed))
        object.__setattr__(self, "blocked_origins", frozenset(blocked))

    @staticmethod
    def _origin(parsed: SplitResult) -> str:
        host = (parsed.hostname or "").lower().rstrip(".")
        if (
            not host
            or not host.isascii()
            or not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", host)
            or parsed.username
            or parsed.password
            or parsed.scheme not in {"http", "https"}
            or ":" in host
        ):
            raise BrowserTargetError("browser origin is invalid")
        try:
            port = (
                parsed.port
                if parsed.port is not None
                else (443 if parsed.scheme == "https" else 80)
            )
        except ValueError as exc:
            raise BrowserTargetError("browser origin port is invalid") from exc
        if port < 1:
            raise BrowserTargetError("browser origin port is invalid")
        return f"{parsed.scheme}://{host}:{port}"

    @property
    def allowed_hosts(self) -> frozenset[str]:
        return frozenset(urlsplit(origin).hostname or "" for origin in self.allowed_origins)

    def pinned_resolvers(self) -> str:
        """Pin approved public names for Chrome's lifetime to prevent DNS rebinding."""
        rules: list[str] = []
        for host in sorted(self.allowed_hosts):
            if host in {"localhost", "127.0.0.1"}:
                continue
            approved = next(
                origin for origin in self.allowed_origins if urlsplit(origin).hostname == host
            )
            self.check_url(approved)
            addresses = socket.getaddrinfo(
                host, urlsplit(approved).port or 443, family=socket.AF_INET
            )
            if not addresses:
                raise BrowserTargetError("browser host has no approved IPv4 address")
            address = addresses[0][4][0]
            if not ipaddress.ip_address(address).is_global:
                raise BrowserTargetError("browser host resolves to a private address")
            rules.append(f"MAP {host} {address}")
        return ",".join(rules)

    def check_url(self, value: str) -> str:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        origin = self._origin(parsed)
        if origin not in self.allowed_origins:
            raise BrowserTargetError("browser URL is outside the approved origin set")
        if origin in self.blocked_origins:
            raise BrowserTargetError("browser URL is an internal control origin")
        try:
            port = (
                parsed.port
                if parsed.port is not None
                else (443 if parsed.scheme == "https" else 80)
            )
            addresses = {item[4][0] for item in socket.getaddrinfo(host, port)}
        except OSError as exc:
            raise BrowserTargetError("browser host could not be resolved") from exc
        for address in addresses:
            ip = ipaddress.ip_address(address)
            local_target = host in {"localhost", "127.0.0.1"}
            if (local_target and not ip.is_loopback) or (not local_target and not ip.is_global):
                raise BrowserTargetError("browser host resolves to a private address")
        return value


class _Cdp:
    def __init__(self, websocket: ClientConnection, policy: BrowserTargetPolicy) -> None:
        self.websocket = websocket
        self.policy = policy
        self.next_id = 0
        self._lock = threading.Lock()
        self._pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._closed = False
        self._mutation_until = 0.0
        self.recent_protocol_events: deque[str] = deque(maxlen=30)
        self.reader_error_type: str | None = None
        self._reader = threading.Thread(target=self._read_events, daemon=True)
        self._reader.start()

    def grant_mutations(self) -> None:
        with self._lock:
            self._mutation_until = time.monotonic() + 5

    def _send(
        self, method: str, params: dict[str, Any], *, wait: bool
    ) -> tuple[int, queue.Queue[dict[str, Any]] | None]:
        with self._lock:
            if self._closed:
                raise BrowserTargetError("browser protocol connection is closed")
            self.next_id += 1
            request_id = self.next_id
            response_queue: queue.Queue[dict[str, Any]] | None = (
                queue.Queue(maxsize=1) if wait else None
            )
            if response_queue is not None:
                self._pending[request_id] = response_queue
            try:
                self.websocket.send(
                    json.dumps({"id": request_id, "method": method, "params": params})
                )
            except Exception:
                self._pending.pop(request_id, None)
                raise
        return request_id, response_queue

    def _read_events(self) -> None:
        try:
            while not self._closed:
                try:
                    message = json.loads(self.websocket.recv(timeout=1))
                except TimeoutError:
                    continue
                event_name = message.get("method")
                response_id = message.get("id")
                self.recent_protocol_events.append(
                    str(event_name) if event_name is not None else f"response:{response_id}"
                )
                if message.get("method") == "Fetch.requestPaused":
                    paused = message["params"]
                    try:
                        self.policy.check_url(paused["request"]["url"])
                        method_name = str(paused["request"].get("method", "GET")).upper()
                        with self._lock:
                            mutation_allowed = time.monotonic() < self._mutation_until
                        if method_name not in {"GET", "HEAD"} and not mutation_allowed:
                            raise BrowserTargetError(
                                "page mutation requires an approved browser action"
                            )
                    except (BrowserTargetError, ValueError):
                        self._send(
                            "Fetch.failRequest",
                            {"requestId": paused["requestId"], "errorReason": "BlockedByClient"},
                            wait=False,
                        )
                    else:
                        self._send(
                            "Fetch.continueRequest", {"requestId": paused["requestId"]}, wait=False
                        )
                    continue
                if isinstance(response_id, int):
                    with self._lock:
                        destination = self._pending.get(response_id)
                    if destination is not None:
                        destination.put_nowait(message)
        except Exception as exc:
            self.reader_error_type = type(exc).__name__
        finally:
            with self._lock:
                self._closed = True
                pending = tuple(self._pending.values())
            for destination in pending:
                with suppress(queue.Full):
                    destination.put_nowait({"error": "connection_closed"})

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id: int | None = None
        try:
            request_id, response_queue = self._send(method, params or {}, wait=True)
            assert response_queue is not None
            try:
                message = response_queue.get(timeout=15)
            except queue.Empty as exc:
                if method in {"Page.navigate", "Input.dispatchMouseEvent"}:
                    raise RemoteOutcomeUnknown("browser action outcome is unknown") from exc
                raise BrowserTargetError("browser observation timed out") from exc
            if "error" in message:
                raise BrowserTargetError("browser protocol rejected the typed action")
            return cast(dict[str, Any], message.get("result", {}))
        finally:
            if request_id is not None:
                with self._lock:
                    self._pending.pop(request_id, None)

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self.websocket.close()
        self._reader.join(timeout=3)

    def evaluate(self, expression: str) -> Any:
        frame = self.call("Page.getFrameTree")["frameTree"]["frame"]["id"]
        context = self.call(
            "Page.createIsolatedWorld", {"frameId": frame, "worldName": "operant-observation"}
        )["executionContextId"]
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "contextId": context,
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
        if "exceptionDetails" in result:
            raise BrowserTargetError("browser element is unavailable")
        return result.get("result", {}).get("value")


class IsolatedChromeBrowser:
    """Dedicated headless Chrome with network request interception and bounded observations."""

    def __init__(
        self,
        chrome_path: Path,
        policy: BrowserTargetPolicy,
        *,
        visible: bool = False,
        blocked_ports: frozenset[int] = frozenset(),
    ) -> None:
        self.chrome_path = chrome_path
        self.policy = policy
        self.visible = visible
        self.blocked_ports = blocked_ports
        self._profile: tempfile.TemporaryDirectory[str] | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._cdp: _Cdp | None = None
        self._proxy: BrowserNetworkProxy | None = None
        self._private_inputs: set[str] = set()

    def _hide_entered_text(self, value: str) -> str:
        for entered in sorted(self._private_inputs, key=len, reverse=True):
            value = value.replace(entered, "[entered text]")
        return value

    def start(self) -> None:
        if self._cdp is not None:
            return
        if not self.chrome_path.is_file():
            raise BrowserTargetError("approved Chrome executable is unavailable")
        pinned_resolvers = self.policy.pinned_resolvers()
        reap_stale_browser_profiles()
        profile = tempfile.TemporaryDirectory(prefix="profile-", dir=_profile_root())
        try:
            proxy = BrowserNetworkProxy(self.policy)
            for port in self.blocked_ports:
                proxy.block_port(port)
            proxy.start()
        except Exception:
            profile.cleanup()
            raise
        try:
            process = subprocess.Popen(
                [
                    str(self.chrome_path),
                    *([] if self.visible else ["--headless=new"]),
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-background-networking",
                    "--disable-component-update",
                    "--disable-default-apps",
                    "--disable-sync",
                    "--disable-extensions",
                    f"--proxy-server=http://127.0.0.1:{proxy.port}",
                    "--proxy-bypass-list=<-loopback>",
                    "--disable-quic",
                    "--dns-prefetch-disable",
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                    f"--host-resolver-rules={pinned_resolvers}",
                    "--remote-debugging-port=0",
                    f"--user-data-dir={profile.name}",
                    "about:blank",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
                env={
                    "HOME": os.environ.get("HOME", str(Path.home())),
                    "TMPDIR": os.environ.get("TMPDIR", tempfile.gettempdir()),
                    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                    "LANG": os.environ.get("LANG", "C.UTF-8"),
                },
            )
        except Exception:
            profile.cleanup()
            proxy.close()
            raise
        cdp: _Cdp | None = None
        try:
            (Path(profile.name) / "operant-profile-owner.json").write_text(
                json.dumps(
                    {
                        "schema": "operant-browser-profile.v1",
                        "name": Path(profile.name).name,
                        "uid": os.getuid(),
                        "pid": process.pid,
                        "created_at": time.time(),
                    }
                )
            )
            port_file = Path(profile.name) / "DevToolsActivePort"
            deadline = time.monotonic() + 10
            while not port_file.exists():
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise BrowserTargetError("isolated Chrome did not start")
                time.sleep(0.05)
            port = int(port_file.read_text().splitlines()[0])
            proxy.block_port(port)
            tabs = httpx.get(f"http://127.0.0.1:{port}/json", timeout=3).json()
            page = next(item for item in tabs if item.get("type") == "page")
            websocket = connect(page["webSocketDebuggerUrl"], open_timeout=3)
            cdp = _Cdp(websocket, self.policy)
            cdp.call("Page.enable")
            cdp.call("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})
        except Exception:
            if cdp is not None:
                cdp.close()
            if process.poll() is None:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
            finally:
                profile.cleanup()
                proxy.close()
            raise
        self._profile = profile
        self._process = process
        self._cdp = cdp
        self._proxy = proxy

    def close(self) -> None:
        cdp, process, profile, proxy = self._cdp, self._process, self._profile, self._proxy
        self._cdp = None
        self._process = None
        self._profile = None
        self._proxy = None
        self._private_inputs.clear()
        try:
            if cdp is not None:
                cdp.close()
        finally:
            try:
                if process is not None:
                    if process.poll() is None:
                        with suppress(ProcessLookupError):
                            os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        with suppress(ProcessLookupError):
                            os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=3)
            finally:
                if profile is not None:
                    profile.cleanup()
                if proxy is not None:
                    proxy.close()

    def __enter__(self) -> IsolatedChromeBrowser:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def observe(self) -> dict[str, Any]:
        self.start()
        assert self._cdp is not None
        body = self._cdp.evaluate(
            "(async()=>{"
            "const controls=Array.from(document.querySelectorAll('input,textarea,select'))"
            ".slice(0,200).map((e,i)=>[i,e.tagName,e.type||'',e.value||'',!!e.checked]);"
            "const bytes=new TextEncoder().encode(JSON.stringify(controls));"
            "if(!crypto.subtle&&controls.length)throw Error('form hash unavailable');"
            "const hash=crypto.subtle?await crypto.subtle.digest('SHA-256',bytes):null;"
            "const form_state_sha256=hash?Array.from(new Uint8Array(hash))"
            ".map(x=>x.toString(16).padStart(2,'0')).join('')"
            ":'4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945';"
            "const selectorFor=(e)=>{if(e.id)return '#'+CSS.escape(e.id);"
            "const parts=[];while(e&&e.nodeType===1){let n=1,s=e.previousElementSibling;"
            "while(s){if(s.tagName===e.tagName)n++;s=s.previousElementSibling;}"
            "parts.unshift(e.tagName.toLowerCase()+':nth-of-type('+n+')');e=e.parentElement;}"
            "return parts.join(' > ');};"
            "const elements=Array.from(document.querySelectorAll("
            "'button,a,[role=button],input[type=submit]'))"
            ".filter(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0;})"
            ".slice(0,100).map(e=>({selector:selectorFor(e),tag:e.tagName.toLowerCase(),"
            "text:(e.innerText||e.getAttribute('aria-label')||'').slice(0,160)}));"
            "const fields=Array.from(document.querySelectorAll('input,textarea'))"
            ".filter(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0;})"
            ".slice(0,100).map(e=>({selector:selectorFor(e),tag:e.tagName.toLowerCase(),"
            "type:(e.type||'text').toLowerCase()}));"
            "return {url:location.href,title:document.title,"
            "text:(document.body?.innerText||'').slice(0,12000),"
            "form_state_sha256,elements,fields};"
            "})()"
        )
        if not isinstance(body, dict):
            raise BrowserTargetError("browser returned no page observation")
        url = body.get("url")
        if url != "about:blank":
            self.policy.check_url(str(url))
            parsed = urlsplit(str(url))
            body["url"] = self._hide_entered_text(
                urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
            )
            if parsed.query:
                body["query_sha256"] = hashlib.sha256(parsed.query.encode()).hexdigest()
        body["title"] = redact_public_text(self._hide_entered_text(str(body.get("title", ""))))
        body["text"] = redact_public_text(
            self._hide_entered_text(str(body.get("text", ""))[: self.policy.max_observation_chars])
        )
        elements = body.get("elements")
        if not isinstance(elements, list):
            raise BrowserTargetError("browser returned no actionable element list")
        for element in elements:
            if isinstance(element, dict):
                element["text"] = redact_public_text(
                    self._hide_entered_text(str(element.get("text", ""))[:160])
                )
                element["selector"] = self._hide_entered_text(str(element.get("selector", "")))
        fields = body.get("fields")
        if not isinstance(fields, list):
            raise BrowserTargetError("browser returned no input field list")
        for field in fields:
            if isinstance(field, dict):
                field["selector"] = self._hide_entered_text(str(field.get("selector", "")))
        return body

    def navigate(self, url: str) -> dict[str, Any]:
        self.policy.check_url(url)
        parsed = urlsplit(url)
        if parsed.query or parsed.fragment or redact_public_text(url) != url:
            raise BrowserTargetError(
                "browser navigation URL cannot include query or credential data"
            )
        self.start()
        assert self._cdp is not None
        navigation = self._cdp.call("Page.navigate", {"url": url})
        if navigation.get("errorText") or navigation.get("isDownload"):
            raise RemoteOutcomeUnknown("browser navigation outcome is unknown")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if self._cdp.evaluate("document.readyState") == "complete":
                    observation = self.observe()
                    parsed = urlsplit(url)
                    safe_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
                    if observation["url"] == self._hide_entered_text(safe_url):
                        return observation
            except BrowserTargetError:
                pass
            time.sleep(0.05)
        raise RemoteOutcomeUnknown("browser navigation outcome is unknown")

    def click(self, selector: str, *, expected_observation: dict[str, Any]) -> dict[str, Any]:
        if not 1 <= len(selector) <= 500:
            raise BrowserTargetError("browser selector is invalid")
        self.start()
        assert self._cdp is not None
        observation = self.observe()
        if observation != expected_observation:
            raise BrowserTargetError("browser page changed before the click")
        if selector not in {
            item.get("selector") for item in observation["elements"] if isinstance(item, dict)
        }:
            raise BrowserTargetError("browser selector is absent from the page observation")
        encoded = json.dumps(selector)
        point = self._cdp.evaluate(
            "(()=>{const e=document.querySelector(" + encoded + ");"
            "if(!e)return null;const r=e.getBoundingClientRect();"
            "return {x:r.x+r.width/2,y:r.y+r.height/2,visible:r.width>0&&r.height>0}})()"
        )
        if not isinstance(point, dict) or not point.get("visible"):
            raise BrowserTargetError("browser target element is unavailable")
        try:
            self._cdp.grant_mutations()
            if self._proxy is not None:
                self._proxy.grant_mutations()
            for event in ("mousePressed", "mouseReleased"):
                self._cdp.call(
                    "Input.dispatchMouseEvent",
                    {
                        "type": event,
                        "x": point["x"],
                        "y": point["y"],
                        "button": "left",
                        "clickCount": 1,
                    },
                )
        except Exception as exc:
            raise RemoteOutcomeUnknown("browser click outcome is unknown") from exc
        try:
            # CDP acknowledges input before the page's handler can finish.
            # Keep observing briefly so the receipt can capture an ensuing change.
            deadline = time.monotonic() + 1.0
            while True:
                after = self.observe()
                if after != expected_observation or time.monotonic() >= deadline:
                    return after
                time.sleep(0.05)
        except BrowserTargetError as exc:
            raise RemoteOutcomeUnknown("browser click outcome is unknown") from exc

    def fill(
        self, selector: str, value: str, *, expected_observation: dict[str, Any]
    ) -> dict[str, Any]:
        if not 1 <= len(selector) <= 500 or len(value) > 2_000:
            raise BrowserTargetError("browser input is out of bounds")
        if redact_public_text(value) != value:
            raise BrowserTargetError("browser input contains a credential-shaped value")
        current = self.observe()
        if current != expected_observation:
            raise BrowserTargetError("browser page changed before the input")
        fields = current["fields"]
        if not any(
            isinstance(field, dict)
            and field.get("selector") == selector
            and field.get("type") in {"text", "search", "email", "url", "tel", "textarea"}
            for field in fields
        ):
            raise BrowserTargetError("browser input field is absent or protected")
        assert self._cdp is not None
        expression = (
            "(()=>{const e=document.querySelector(" + json.dumps(selector) + ");if(!e)return false;"
            "const proto=e.tagName==='TEXTAREA'?"
            "HTMLTextAreaElement.prototype:HTMLInputElement.prototype;"
            "const setter=Object.getOwnPropertyDescriptor(proto,'value')?.set;"
            "if(!setter)return false;setter.call(e,"
            + json.dumps(value)
            + ");e.dispatchEvent(new Event('input',{bubbles:true}));"
            "e.dispatchEvent(new Event('change',{bubbles:true}));return true;})()"
        )
        try:
            if value:
                self._private_inputs.add(value)
            accepted = self._cdp.evaluate(expression)
            if accepted is not True:
                raise RemoteOutcomeUnknown("browser input outcome is unknown")
            return self.observe()
        except Exception as exc:
            raise RemoteOutcomeUnknown("browser input outcome is unknown") from exc


class LocalBrowserConnector:
    """Execute approved browser jobs through the remote target receipt machinery."""

    def __init__(
        self,
        *,
        target_id: str,
        lease_id: str,
        lease_token: str,
        lease_fencing: int,
        browser: IsolatedChromeBrowser,
    ) -> None:
        self.target_id = target_id
        self.lease_id = lease_id
        self.lease_token = lease_token
        self.lease_fencing = lease_fencing
        self.browser = browser

    def execute(self, job: RemoteExecutionJob) -> ConnectorOutcome:
        if (job.target_id, job.lease_id, job.lease_fencing) != (
            self.target_id,
            self.lease_id,
            self.lease_fencing,
        ):
            raise BrowserTargetError("browser job does not match the target lease")
        target_ref = job.arguments.get("target_ref")
        if target_ref != self.target_id:
            raise BrowserTargetError("browser job changed its target reference")
        try:
            if (
                job.capability is RemoteCapability.BROWSER_OBSERVE
                and job.operation == "observe_browser"
            ):
                observation = self.browser.observe()
                postcondition = {"target_ref": target_ref, "observation": observation}
            else:
                expected_hash = job.arguments.get("observation_hash")
                current = self.browser.observe()
                if (
                    not isinstance(expected_hash, str)
                    or observation_hash(self.target_id, str(target_ref), current) != expected_hash
                ):
                    raise BrowserTargetError("browser page changed since the approved observation")
                arguments = job.arguments.get("arguments")
                if not isinstance(arguments, dict):
                    raise BrowserTargetError("browser action arguments are invalid")
                if (
                    job.capability is RemoteCapability.BROWSER_NAVIGATE
                    and job.operation == "navigate"
                ):
                    url = arguments.get("url")
                    if not isinstance(url, str):
                        raise BrowserTargetError("browser navigation requires a URL")
                    postcondition = self.browser.navigate(url)
                elif job.capability is RemoteCapability.BROWSER_SUBMIT and job.operation == "click":
                    selector = arguments.get("selector")
                    if not isinstance(selector, str):
                        raise BrowserTargetError("browser click requires a selector")
                    postcondition = self.browser.click(selector, expected_observation=current)
                elif job.capability is RemoteCapability.BROWSER_SUBMIT and job.operation == "fill":
                    selector = arguments.get("selector")
                    value_sealed = arguments.get("value_sealed")
                    if not isinstance(selector, str) or not isinstance(value_sealed, str):
                        raise BrowserTargetError("browser fill requires a selector and sealed text")
                    try:
                        value = open_browser_input(
                            value_sealed,
                            token=self.lease_token,
                            target_id=self.target_id,
                            lease_id=self.lease_id,
                            fencing=self.lease_fencing,
                            observation_hash=expected_hash,
                            selector=selector,
                            idempotency_key=job.idempotency_key,
                        )
                    except Exception as exc:
                        raise BrowserTargetError("browser input envelope cannot be opened") from exc
                    postcondition = self.browser.fill(selector, value, expected_observation=current)
                else:
                    raise BrowserTargetError("browser operation is not supported")
                postcondition = {
                    **postcondition,
                    "pre_observation_hash": expected_hash,
                    "post_observation_hash": observation_hash(
                        self.target_id, str(target_ref), postcondition
                    ),
                }
        except BrowserTargetError as exc:
            return ConnectorOutcome(
                result=RemoteExecutionResult(
                    job_id=job.job_id,
                    result_idempotency_key=f"local-browser:{job.job_id}",
                    status=RemoteJobStatus.FAILED,
                    error_code="browser.target_rejected",
                    postcondition={"reason": str(exc)},
                )
            )
        return ConnectorOutcome(
            result=RemoteExecutionResult(
                job_id=job.job_id,
                result_idempotency_key=f"local-browser:{job.job_id}",
                status=RemoteJobStatus.SUCCEEDED,
                postcondition=cast(dict[str, JsonValue], postcondition),
            )
        )

    def cancel(self, job_id: str) -> None:
        del job_id
        self.browser.close()


def observation_hash(target_id: str, target_ref: str, body: dict[str, Any]) -> str:
    observed = {
        key: value
        for key, value in body.items()
        if key not in {"pre_observation_hash", "post_observation_hash"}
    }
    return canonical_action_hash(
        {"target_id": target_id, "target_ref": target_ref, "body": observed}
    )
