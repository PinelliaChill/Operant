"""Run a trusted local capability adapter against Core's leased target protocol.

The worker has no Core database access. It needs an explicitly registered target
and a short-lived lease token, and it stops on an unknown completion outcome.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from operant.domain.remote_execution import (
    CapabilityManifest,
    RemoteCapability,
    RemoteExecutionJob,
    RemoteExecutionResult,
    RemoteJobStatus,
    RemoteTargetRegistration,
)
from operant.remote.connector import RemoteOutcomeUnknown, RemoteTargetConnector
from operant.remote.local_browser import (
    BrowserTargetPolicy,
    IsolatedChromeBrowser,
    LocalBrowserConnector,
)
from operant.remote.local_computer import (
    ComputerTargetPolicy,
    LocalComputerConnector,
    MacComputer,
)


@dataclass(frozen=True)
class LocalCapabilityPlugin:
    plugin_id: Literal["operant.chrome.browser", "operant.macos.computer"]
    protocol_version: Literal["phase56.v1"]
    capabilities: frozenset[RemoteCapability]
    operations: frozenset[str]


BROWSER_PLUGIN = LocalCapabilityPlugin(
    plugin_id="operant.chrome.browser",
    protocol_version="phase56.v1",
    capabilities=frozenset(
        {
            RemoteCapability.BROWSER_OBSERVE,
            RemoteCapability.BROWSER_NAVIGATE,
            RemoteCapability.BROWSER_SUBMIT,
            RemoteCapability.BROWSER_SCREENSHOT,
        }
    ),
    operations=frozenset(
        {"observe_browser", "navigate", "click", "fill", "press_key", "capture_viewport"}
    ),
)
COMPUTER_PLUGIN = LocalCapabilityPlugin(
    plugin_id="operant.macos.computer",
    protocol_version="phase56.v1",
    capabilities=frozenset(
        {
            RemoteCapability.COMPUTER_OBSERVE,
            RemoteCapability.COMPUTER_INPUT,
            RemoteCapability.COMPUTER_SCREENSHOT,
            RemoteCapability.CLIPBOARD_READ,
            RemoteCapability.CLIPBOARD_WRITE,
        }
    ),
    operations=frozenset(
        {
            "observe_computer",
            "click_button",
            "type_text",
            "press_key",
            "capture_window",
            "read_clipboard",
            "write_clipboard",
        }
    ),
)


def _validate_core_origin(origin: str) -> str:
    parsed = urlsplit(origin)
    if (
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or parsed.hostname is None
    ):
        raise ValueError("Core origin must not contain credentials or a path")
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    ):
        raise ValueError("local capability worker requires HTTPS or loopback Core")
    return origin.rstrip("/")


class LocalCapabilityWorker:
    """Poll, validate, execute, and complete leased jobs exactly once per poll."""

    def __init__(
        self,
        *,
        core_origin: str,
        plugin: LocalCapabilityPlugin,
        connector: RemoteTargetConnector,
        client: httpx.Client | None = None,
        lifecycle_check: Callable[[], None] | None = None,
    ) -> None:
        self.core_origin = _validate_core_origin(core_origin)
        self.plugin = plugin
        self.connector = connector
        self.client = client or httpx.Client(timeout=15, trust_env=False)
        self._owns_client = client is None
        self._verified = False
        self._last_renewed = 0.0
        self.lifecycle_check = lifecycle_check
        self._paused = Event()
        self._closed = False

    def _url(self, path: str) -> str:
        return f"{self.core_origin}{path}"

    def verify_target(self) -> RemoteTargetRegistration:
        response = self.client.get(self._url(f"/v1/remote-targets/{self.connector.target_id}"))
        response.raise_for_status()
        target = RemoteTargetRegistration.model_validate(response.json())
        manifest: CapabilityManifest = target.capability_manifest
        if target.status.value != "online":
            raise ValueError("capability target must be online")
        if (
            target.endpoint_ref != self.plugin.plugin_id
            or manifest.version != self.plugin.protocol_version
        ):
            raise ValueError("capability target is not bound to the requested plugin version")
        if not self.plugin.capabilities.issubset(
            set(manifest.capabilities)
        ) or not self.plugin.operations.issubset(set(manifest.supported_operations)):
            raise ValueError("capability target manifest does not admit this plugin")
        self._verified = True
        return target

    def poll_once(self) -> tuple[RemoteExecutionResult, ...]:
        if self._closed or self._paused.is_set():
            return ()
        if self.lifecycle_check is not None:
            self.lifecycle_check()
        if not self._verified:
            self.verify_target()
        if time.monotonic() - self._last_renewed >= 60:
            renewed = self.client.post(
                self._url(f"/v1/remote-targets/{self.connector.target_id}/leases/renew"),
                json={
                    "lease_id": self.connector.lease_id,
                    "token": self.connector.lease_token,
                    "fencing": self.connector.lease_fencing,
                    "ttl_seconds": 180,
                },
            )
            renewed.raise_for_status()
            self._last_renewed = time.monotonic()
        response = self.client.post(
            self._url(f"/v1/remote-targets/{self.connector.target_id}/jobs/poll"),
            json={
                "lease_id": self.connector.lease_id,
                "token": self.connector.lease_token,
                "fencing": self.connector.lease_fencing,
                "limit": 1,
                "idempotency_key": f"local-plugin-poll:{uuid4().hex}",
            },
        )
        response.raise_for_status()
        items = response.json().get("items")
        if not isinstance(items, list):
            raise RemoteOutcomeUnknown("Core returned an invalid job page")
        results: list[RemoteExecutionResult] = []
        for item in items:
            job = RemoteExecutionJob.model_validate(item)
            artifact_bytes: bytes | None = None
            disabled = False
            if self.lifecycle_check is not None:
                try:
                    self.lifecycle_check()
                except (PermissionError, ValueError, KeyError):
                    disabled = True
            if (
                disabled
                or job.capability not in self.plugin.capabilities
                or job.operation not in self.plugin.operations
            ):
                result = RemoteExecutionResult(
                    job_id=job.job_id,
                    result_idempotency_key=f"local-plugin:{job.job_id}",
                    status=RemoteJobStatus.FAILED,
                    error_code=("plugin.disabled" if disabled else "plugin.unsupported_operation"),
                )
            else:
                try:
                    outcome = self.connector.execute(job)
                    result = outcome.result
                    artifact_bytes = outcome.artifact_bytes
                except RemoteOutcomeUnknown:
                    potentially_side_effecting = job.capability in {
                        RemoteCapability.BROWSER_NAVIGATE,
                        RemoteCapability.BROWSER_SUBMIT,
                        RemoteCapability.COMPUTER_INPUT,
                        RemoteCapability.CLIPBOARD_WRITE,
                    }
                    result = RemoteExecutionResult(
                        job_id=job.job_id,
                        result_idempotency_key=f"local-plugin-unknown:{job.job_id}",
                        status=(
                            RemoteJobStatus.MANUAL_RECONCILE_REQUIRED
                            if potentially_side_effecting
                            or job.idempotency.value == "non_idempotent"
                            else RemoteJobStatus.FAILED
                        ),
                        error_code="plugin.outcome_unknown",
                    )
            if result.status is RemoteJobStatus.MANUAL_RECONCILE_REQUIRED:
                # The completion acknowledgement can fail. Stop this process
                # before sending it, so a caller cannot poll another job on
                # the old lease while the first outcome remains uncertain.
                self.pause()
            completion = self.client.post(
                self._url(
                    f"/v1/remote-targets/{self.connector.target_id}/jobs/{job.job_id}/complete"
                ),
                json={
                    "lease_id": self.connector.lease_id,
                    "token": self.connector.lease_token,
                    "fencing": self.connector.lease_fencing,
                    "result_id": result.result_id,
                    "result_idempotency_key": result.result_idempotency_key,
                    "status": result.status.value,
                    "artifact_ref": result.artifact_ref,
                    "artifact_sha256": result.artifact_sha256,
                    "artifact_base64": (
                        base64.b64encode(artifact_bytes).decode("ascii")
                        if artifact_bytes is not None
                        else None
                    ),
                    "postcondition": result.postcondition,
                    "error_code": result.error_code,
                },
            )
            if completion.status_code >= 400:
                raise RemoteOutcomeUnknown("Core did not acknowledge capability completion")
            acknowledged = RemoteExecutionResult.model_validate(completion.json()["result"])
            if acknowledged.job_id != job.job_id or acknowledged.status is not result.status:
                raise RemoteOutcomeUnknown("Core returned a mismatched capability receipt")
            results.append(result)
            if result.status is RemoteJobStatus.MANUAL_RECONCILE_REQUIRED:
                break
        return tuple(results)

    def run(self, *, poll_interval_seconds: float = 0.5) -> None:
        if not 0.1 <= poll_interval_seconds <= 30:
            raise ValueError("poll interval is out of bounds")
        while not self._paused.is_set() and not self._closed:
            self.poll_once()
            self._paused.wait(poll_interval_seconds)

    def pause(self) -> None:
        """Stop polling for handoff while keeping the dedicated browser alive."""
        self._paused.set()

    def resume(self, *, lease_id: str, lease_token: str, lease_fencing: int) -> None:
        """Bind a new fenced lease after the manager has released the old one."""
        if self._closed or not self._paused.is_set():
            raise ValueError("worker must be paused and open before resume")
        if not lease_id or len(lease_token) < 16 or lease_fencing <= self.connector.lease_fencing:
            raise ValueError("resumed worker requires a newer fenced lease")
        self.connector.lease_id = lease_id
        self.connector.lease_token = lease_token
        self.connector.lease_fencing = lease_fencing
        self._verified = False
        self._last_renewed = 0.0
        self._paused.clear()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._paused.set()
        if isinstance(self.connector, LocalBrowserConnector):
            self.connector.browser.close()
        if self._owns_client:
            self.client.close()


def browser_worker(
    *,
    core_origin: str,
    target_id: str,
    lease_id: str,
    lease_token: str,
    lease_fencing: int,
    allowed_origins: frozenset[str],
    chrome_path: Path,
    visible: bool = False,
) -> LocalCapabilityWorker:
    core = urlsplit(_validate_core_origin(core_origin))
    blocked_origins = frozenset({core_origin}) if core.hostname != "::1" else frozenset()
    blocked_ports = (
        frozenset({core.port or (443 if core.scheme == "https" else 80)})
        if core.hostname in {"127.0.0.1", "localhost", "::1"}
        else frozenset()
    )
    browser = IsolatedChromeBrowser(
        chrome_path,
        BrowserTargetPolicy(allowed_origins, blocked_origins=blocked_origins),
        visible=visible,
        blocked_ports=blocked_ports,
    )
    return LocalCapabilityWorker(
        core_origin=core_origin,
        plugin=BROWSER_PLUGIN,
        connector=LocalBrowserConnector(
            target_id=target_id,
            lease_id=lease_id,
            lease_token=lease_token,
            lease_fencing=lease_fencing,
            browser=browser,
        ),
    )


def computer_worker(
    *,
    core_origin: str,
    target_id: str,
    lease_id: str,
    lease_token: str,
    lease_fencing: int,
    allowed_bundle_ids: frozenset[str],
) -> LocalCapabilityWorker:
    computer = MacComputer(ComputerTargetPolicy(allowed_bundle_ids))
    return LocalCapabilityWorker(
        core_origin=core_origin,
        plugin=COMPUTER_PLUGIN,
        connector=LocalComputerConnector(
            target_id=target_id,
            lease_id=lease_id,
            lease_token=lease_token,
            lease_fencing=lease_fencing,
            computer=computer,
        ),
    )
