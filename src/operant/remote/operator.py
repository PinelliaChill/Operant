"""Typed client-side browser workflow over the Phase56 Core protocol.

This is a local user control surface. It never stores a Target lease token,
replays an uncertain write, or treats a queued Job as completed.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import uuid4

from operant.protocol import redact_public_text
from operant.remote.local_browser import BrowserTargetPolicy
from operant.remote.sealed_input import seal_browser_input
from sdk.python_client.phase56_generated import (
    ActCapabilityBody,
    ObserveCapabilityBody,
    Phase56Client,
)

_OBSERVATION_KEY: ContextVar[str | None] = ContextVar("operant_local_observation_key", default=None)


@contextmanager
def bound_observation_key(key: str | None) -> Iterator[None]:
    token = _OBSERVATION_KEY.set(key)
    try:
        yield
    finally:
        _OBSERVATION_KEY.reset(token)


class CapabilityOperationError(RuntimeError):
    def __init__(self, code: str, job_id: str, status: str) -> None:
        super().__init__(f"capability Job {job_id} ended in {status}: {code}")
        self.code = code
        self.job_id = job_id
        self.status = status


@dataclass(frozen=True)
class CapabilityLeaseBinding:
    target_id: str
    lease_id: str
    token: str = field(repr=False)
    fencing: int

    def __post_init__(self) -> None:
        if (
            not self.target_id
            or not self.lease_id
            or not 16 <= len(self.token) <= 300
            or self.fencing < 1
        ):
            raise ValueError("capability lease binding is incomplete")

    def request_fields(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "token": self.token,
            "fencing": self.fencing,
        }


class _CapabilityOperator:
    def __init__(
        self,
        client: Phase56Client,
        binding: CapabilityLeaseBinding,
        *,
        wait_seconds: float = 30,
    ) -> None:
        if not 1 <= wait_seconds <= 300:
            raise ValueError("browser operation wait is out of bounds")
        self.client = client
        self.binding = binding
        self.wait_seconds = wait_seconds

    def _wait(self, job_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.wait_seconds
        while True:
            projection = self.client.get_remote_target_job_result(job_id)
            status = projection.get("status")
            result = projection.get("result")
            if status in {"succeeded", "failed", "cancelled", "manual_reconcile_required"}:
                if status == "succeeded" and isinstance(result, dict):
                    return projection
                error_code = (
                    redact_public_text(str(result.get("error_code") or "job_failed"), max_chars=200)
                    if isinstance(result, dict)
                    else "job_failed"
                )
                raise CapabilityOperationError(error_code, job_id, str(status))
            if status not in {"queued", "leased", "running"}:
                raise CapabilityOperationError("projection_invalid", job_id, str(status))
            if time.monotonic() >= deadline:
                # This requests cancellation but cannot retract a running
                # external action. Keep the original Job ID for reconciliation.
                with suppress(Exception):
                    self.client.cancel_remote_target_job(
                        job_id,
                        {"idempotency_key": f"operator-timeout:{job_id}"},
                        idempotency_key=f"operator-timeout:{job_id}",
                    )
                raise CapabilityOperationError("outcome_unknown", job_id, "timeout")
            time.sleep(0.1)


class BrowserCapabilityOperator(_CapabilityOperator):
    def __init__(
        self,
        client: Phase56Client,
        binding: CapabilityLeaseBinding,
        policy: BrowserTargetPolicy,
        *,
        wait_seconds: float = 30,
    ) -> None:
        super().__init__(client, binding, wait_seconds=wait_seconds)
        self.policy = policy

    def observe(self) -> dict[str, Any]:
        operation_id = _OBSERVATION_KEY.get() or f"browser-observe:{uuid4().hex}"
        response = self.client.observe_browser(
            self.binding.target_id,
            cast(
                ObserveCapabilityBody,
                {
                    **self.binding.request_fields(),
                    "target_ref": self.binding.target_id,
                    "idempotency_key": operation_id,
                },
            ),
            idempotency_key=operation_id,
        )
        job_id = response.get("job_id")
        if not isinstance(job_id, str):
            raise ValueError("Core returned no browser observation Job ID")
        projection = self._wait(job_id)
        observation = projection.get("observation")
        if not isinstance(observation, dict):
            raise CapabilityOperationError("observation_missing", job_id, "succeeded")
        return {"job_id": job_id, "observation": observation}

    def act(
        self,
        operation: str,
        *,
        observation_hash: str,
        idempotency_key: str,
        url: str | None = None,
        selector: str | None = None,
        value: str | None = None,
        key: str | None = None,
    ) -> dict[str, Any]:
        if not observation_hash or not idempotency_key:
            raise ValueError("browser action requires observation hash and idempotency key")
        arguments: dict[str, Any]
        if (
            operation == "navigate"
            and url is not None
            and selector is None
            and value is None
            and key is None
        ):
            self.policy.check_url(url)
            if urlsplit(url).query or urlsplit(url).fragment or redact_public_text(url) != url:
                raise ValueError("browser navigation URL cannot include query or credential data")
            arguments = {"url": url}
            capability = "browser.navigate"
        elif (
            operation == "click"
            and selector is not None
            and url is None
            and value is None
            and key is None
        ):
            arguments = {"selector": selector}
            capability = "browser.submit"
        elif (
            operation == "fill"
            and selector is not None
            and value is not None
            and url is None
            and key is None
        ):
            if len(value) > 2_000 or redact_public_text(value) != value:
                raise ValueError("browser input is too long or credential-shaped")
            arguments = {
                "selector": selector,
                "value_sealed": seal_browser_input(
                    value,
                    token=self.binding.token,
                    target_id=self.binding.target_id,
                    lease_id=self.binding.lease_id,
                    fencing=self.binding.fencing,
                    observation_hash=observation_hash,
                    selector=selector,
                    idempotency_key=idempotency_key,
                ),
            }
            capability = "browser.submit"
        elif (
            operation == "press_key"
            and key
            in {
                "Tab",
                "Return",
                "Enter",
                "Escape",
                "ArrowUp",
                "ArrowDown",
                "ArrowLeft",
                "ArrowRight",
                "Backspace",
                "Delete",
                "Home",
                "End",
                "PageUp",
                "PageDown",
            }
            and url is None
            and selector is None
            and value is None
        ):
            arguments = {"key": key}
            capability = "browser.submit"
        elif (
            operation == "capture_viewport"
            and url is None
            and selector is None
            and value is None
            and key is None
        ):
            arguments = {}
            capability = "browser.screenshot"
        else:
            raise ValueError("browser action arguments do not match a supported operation")
        response = self.client.act_browser(
            cast(
                ActCapabilityBody,
                {
                    **self.binding.request_fields(),
                    "action": {
                        "target_id": self.binding.target_id,
                        "capability": capability,
                        "operation": operation,
                        "target_ref": self.binding.target_id,
                        "observation_hash": observation_hash,
                        "arguments": arguments,
                        "idempotency_key": idempotency_key,
                        "idempotency": "non_idempotent",
                    },
                },
            ),
            idempotency_key=idempotency_key,
        )
        job = response.get("job")
        job_id = job.get("job_id") if isinstance(job, dict) else None
        if not isinstance(job_id, str):
            raise ValueError("Core returned no browser action Job ID")
        return self._wait(job_id)


class ComputerCapabilityOperator(_CapabilityOperator):
    """Control only a freshly observed button in an approved foreground App."""

    def __init__(
        self,
        client: Phase56Client,
        binding: CapabilityLeaseBinding,
        allowed_bundle_ids: frozenset[str],
        *,
        wait_seconds: float = 30,
    ) -> None:
        super().__init__(client, binding, wait_seconds=wait_seconds)
        if not allowed_bundle_ids:
            raise ValueError("computer App allowlist is empty")
        self.allowed_bundle_ids = allowed_bundle_ids

    def observe(self) -> dict[str, Any]:
        operation_id = _OBSERVATION_KEY.get() or f"computer-observe:{uuid4().hex}"
        response = self.client.observe_computer(
            self.binding.target_id,
            cast(
                ObserveCapabilityBody,
                {
                    **self.binding.request_fields(),
                    "target_ref": self.binding.target_id,
                    "idempotency_key": operation_id,
                },
            ),
            idempotency_key=operation_id,
        )
        job_id = response.get("job_id")
        if not isinstance(job_id, str):
            raise ValueError("Core returned no computer observation Job ID")
        projection = self._wait(job_id)
        observation = projection.get("observation")
        if not isinstance(observation, dict):
            raise CapabilityOperationError("observation_missing", job_id, "succeeded")
        body = observation.get("body")
        if not isinstance(body, dict) or body.get("bundle_id") not in self.allowed_bundle_ids:
            raise CapabilityOperationError("computer.target_rejected", job_id, "failed")
        return {"job_id": job_id, "observation": observation}

    def click_button(
        self,
        *,
        button_name: str,
        observation_hash: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self.act(
            "click_button",
            button_name=button_name,
            observation_hash=observation_hash,
            idempotency_key=idempotency_key,
        )

    def act(
        self,
        operation: str,
        *,
        observation_hash: str,
        idempotency_key: str,
        button_name: str | None = None,
        element_name: str | None = None,
        value: str | None = None,
        key: str | None = None,
    ) -> dict[str, Any]:
        if not observation_hash or not idempotency_key:
            raise ValueError("computer action requires observation hash and idempotency key")
        arguments: dict[str, Any]
        if (
            operation == "click_button"
            and button_name is not None
            and element_name is None
            and value is None
            and key is None
        ):
            if not 1 <= len(button_name) <= 200 or redact_public_text(button_name) != button_name:
                raise ValueError("computer button label is invalid or credential-shaped")
            arguments = {"button_name": button_name}
            capability = "computer.input"
        elif (
            operation == "type_text"
            and element_name is not None
            and value is not None
            and button_name is None
            and key is None
        ):
            if (
                not 1 <= len(element_name) <= 200
                or redact_public_text(element_name) != element_name
            ):
                raise ValueError("computer text field label is invalid or credential-shaped")
            if len(value) > 2_000 or redact_public_text(value) != value:
                raise ValueError("computer input is too long or credential-shaped")
            arguments = {
                "element_name": element_name,
                "value_sealed": seal_browser_input(
                    value,
                    token=self.binding.token,
                    target_id=self.binding.target_id,
                    lease_id=self.binding.lease_id,
                    fencing=self.binding.fencing,
                    observation_hash=observation_hash,
                    selector=f"computer:type_text:{element_name}",
                    idempotency_key=idempotency_key,
                ),
            }
            capability = "computer.input"
        elif (
            operation == "press_key"
            and key
            in {
                "Tab",
                "Return",
                "Enter",
                "Escape",
                "ArrowUp",
                "ArrowDown",
                "ArrowLeft",
                "ArrowRight",
                "Backspace",
                "Delete",
                "Home",
                "End",
                "PageUp",
                "PageDown",
            }
            and button_name is None
            and element_name is None
            and value is None
        ):
            arguments = {"key": key}
            capability = "computer.input"
        elif (
            operation == "capture_window"
            and button_name is None
            and element_name is None
            and value is None
            and key is None
        ):
            arguments = {}
            capability = "computer.screenshot"
        elif (
            operation == "read_clipboard"
            and button_name is None
            and element_name is None
            and value is None
            and key is None
        ):
            arguments = {}
            capability = "computer.clipboard.read"
        elif (
            operation == "write_clipboard"
            and value is not None
            and button_name is None
            and element_name is None
            and key is None
        ):
            if len(value) > 2_000 or redact_public_text(value) != value:
                raise ValueError("clipboard input is too long or credential-shaped")
            arguments = {
                "value_sealed": seal_browser_input(
                    value,
                    token=self.binding.token,
                    target_id=self.binding.target_id,
                    lease_id=self.binding.lease_id,
                    fencing=self.binding.fencing,
                    observation_hash=observation_hash,
                    selector="computer:write_clipboard",
                    idempotency_key=idempotency_key,
                )
            }
            capability = "computer.clipboard.write"
        else:
            raise ValueError("computer action arguments do not match a supported operation")
        response = self.client.act_computer(
            cast(
                ActCapabilityBody,
                {
                    **self.binding.request_fields(),
                    "action": {
                        "target_id": self.binding.target_id,
                        "capability": capability,
                        "operation": operation,
                        "target_ref": self.binding.target_id,
                        "observation_hash": observation_hash,
                        "arguments": arguments,
                        "idempotency_key": idempotency_key,
                        "idempotency": "non_idempotent",
                    },
                },
            ),
            idempotency_key=idempotency_key,
        )
        job = response.get("job")
        job_id = job.get("job_id") if isinstance(job, dict) else None
        if not isinstance(job_id, str):
            raise ValueError("Core returned no computer action Job ID")
        return self._wait(job_id)
