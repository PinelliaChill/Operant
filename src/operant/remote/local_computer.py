"""Narrow macOS accessibility adapter for approved, foreground app controls.

This adapter never accepts AppleScript text from a caller.  Accessibility is
an operating-system permission; when it is absent the operation fails closed.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, cast

from pydantic import JsonValue

from operant.domain.remote_execution import (
    RemoteCapability,
    RemoteExecutionJob,
    RemoteExecutionResult,
    RemoteJobStatus,
)
from operant.protocol import canonical_action_hash, redact_public_text
from operant.remote.connector import ConnectorOutcome, RemoteOutcomeUnknown


class ComputerTargetError(RuntimeError):
    """The computer target is unavailable or no longer matches the observation."""


_OBSERVE_SCRIPT = r"""
on run argv
    tell application "System Events"
        set targetProcess to first application process whose frontmost is true
        set appId to bundle identifier of targetProcess
        set windowTitle to name of front window of targetProcess
        set buttonNames to name of every button of front window of targetProcess
        set AppleScript's text item delimiters to character id 31
        set joinedButtons to buttonNames as text
        set AppleScript's text item delimiters to ""
        return appId & (character id 30) & windowTitle & (character id 30) & joinedButtons
    end tell
end run
"""

_CLICK_BUTTON_SCRIPT = r"""
on run argv
    set expectedApp to item 1 of argv
    set expectedWindow to item 2 of argv
    set buttonName to item 3 of argv
    tell application "System Events"
        set targetProcess to first application process whose frontmost is true
        if bundle identifier of targetProcess is not expectedApp then error "target app changed"
        if name of front window of targetProcess is not expectedWindow then
            error "target window changed"
        end if
        click (first button of front window of targetProcess whose name is buttonName)
    end tell
end run
"""


@dataclass(frozen=True)
class ComputerTargetPolicy:
    allowed_bundle_ids: frozenset[str]

    def __post_init__(self) -> None:
        if not self.allowed_bundle_ids:
            raise ComputerTargetError("computer App allowlist is empty")
        if self.allowed_bundle_ids & self.protected_bundle_ids():
            raise ComputerTargetError("protected App cannot be approved")

    @staticmethod
    def protected_bundle_ids() -> frozenset[str]:
        return frozenset(
            {
                "com.apple.keychainaccess",
                "com.apple.systempreferences",
                "com.apple.Terminal",
                "com.googlecode.iterm2",
                "com.1password.1password",
            }
        )

    def check(self, bundle_id: str) -> None:
        if bundle_id not in self.allowed_bundle_ids or bundle_id in self.protected_bundle_ids():
            raise ComputerTargetError("computer app is outside the approved set")


class MacComputer:
    def __init__(self, policy: ComputerTargetPolicy) -> None:
        self.policy = policy
        if sys.platform != "darwin":
            raise ComputerTargetError("macOS accessibility is unavailable on this platform")

    @staticmethod
    def _script(source: str, *arguments: str) -> str:
        try:
            result = subprocess.run(
                ["/usr/bin/osascript", "-e", source, *arguments],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                env={
                    "HOME": os.path.expanduser("~"),
                    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                    "LANG": os.environ.get("LANG", "C.UTF-8"),
                },
            )
        except subprocess.TimeoutExpired as exc:
            raise RemoteOutcomeUnknown("computer action timed out; outcome is unknown") from exc
        if result.returncode != 0:
            # System Events can include private UI text in stderr. Do not expose it.
            if "-1719" in result.stderr:
                raise ComputerTargetError("macOS Accessibility permission is required")
            raise ComputerTargetError("macOS accessibility denied or target is unavailable")
        if len(result.stdout) > 20_000:
            raise ComputerTargetError("macOS observation exceeds the bounded response size")
        return result.stdout.rstrip("\r\n")

    def _raw_observation(self) -> tuple[str, str, list[str]]:
        response = self._script(_OBSERVE_SCRIPT).split("\x1e", 2)
        if len(response) != 3:
            raise ComputerTargetError("macOS returned an incomplete window observation")
        bundle_id, window_title, buttons = response
        self.policy.check(bundle_id)
        return (
            bundle_id,
            window_title,
            [name.strip() for name in buttons.split("\x1f") if name.strip()][:100],
        )

    @staticmethod
    def _public_observation(
        bundle_id: str, window_title: str, buttons: list[str]
    ) -> dict[str, JsonValue]:
        return {
            "bundle_id": bundle_id,
            "window_title": redact_public_text(window_title[:500]),
            "window_title_sha256": hashlib.sha256(window_title.encode()).hexdigest(),
            "buttons": cast(
                list[JsonValue],
                [redact_public_text(name[:200]) for name in buttons],
            ),
            "buttons_sha256": hashlib.sha256("\x1f".join(buttons).encode()).hexdigest(),
        }

    def observe(self) -> dict[str, JsonValue]:
        return self._public_observation(*self._raw_observation())

    def click_button(
        self, *, expected: dict[str, JsonValue], button_name: str
    ) -> dict[str, JsonValue]:
        bundle_id = expected.get("bundle_id")
        buttons = expected.get("buttons")
        if (
            not isinstance(bundle_id, str)
            or not isinstance(buttons, list)
            or button_name not in buttons
            or not 1 <= len(button_name) <= 200
        ):
            raise ComputerTargetError("computer button is not in the approved observation")
        self.policy.check(bundle_id)
        current_bundle, current_title, current_buttons = self._raw_observation()
        if self._public_observation(current_bundle, current_title, current_buttons) != expected:
            raise ComputerTargetError("computer window changed since observation")
        if (
            current_buttons.count(button_name) != 1
            or redact_public_text(button_name) != button_name
        ):
            raise ComputerTargetError("computer button has an ambiguous or sensitive label")
        try:
            self._script(_CLICK_BUTTON_SCRIPT, bundle_id, current_title, button_name)
        except ComputerTargetError as exc:
            # An accessibility error after dispatch cannot prove whether the
            # click occurred. The caller must inspect the app before retrying.
            raise RemoteOutcomeUnknown("computer click outcome is unknown") from exc
        try:
            return self.observe()
        except ComputerTargetError as exc:
            raise RemoteOutcomeUnknown("computer click outcome is unknown") from exc


class LocalComputerConnector:
    def __init__(
        self,
        *,
        target_id: str,
        lease_id: str,
        lease_token: str,
        lease_fencing: int,
        computer: MacComputer,
    ) -> None:
        self.target_id = target_id
        self.lease_id = lease_id
        self.lease_token = lease_token
        self.lease_fencing = lease_fencing
        self.computer = computer

    def execute(self, job: RemoteExecutionJob) -> ConnectorOutcome:
        if (job.target_id, job.lease_id, job.lease_fencing) != (
            self.target_id,
            self.lease_id,
            self.lease_fencing,
        ):
            raise ComputerTargetError("computer job does not match the target lease")
        target_ref = job.arguments.get("target_ref")
        if target_ref != self.target_id:
            raise ComputerTargetError("computer job changed its target reference")
        try:
            if (
                job.capability is RemoteCapability.COMPUTER_OBSERVE
                and job.operation == "observe_computer"
            ):
                postcondition: dict[str, Any] = {
                    "target_ref": target_ref,
                    "observation": self.computer.observe(),
                }
            else:
                expected = self.computer.observe()
                expected_hash = job.arguments.get("observation_hash")
                if (
                    not isinstance(expected_hash, str)
                    or canonical_action_hash(
                        {"target_id": self.target_id, "target_ref": target_ref, "body": expected}
                    )
                    != expected_hash
                ):
                    raise ComputerTargetError(
                        "computer window changed since the approved observation"
                    )
                arguments = job.arguments.get("arguments")
                if not isinstance(arguments, dict):
                    raise ComputerTargetError("computer action arguments are invalid")
                if (
                    job.capability is RemoteCapability.COMPUTER_INPUT
                    and job.operation == "click_button"
                ):
                    button_name = arguments.get("button_name")
                    if not isinstance(button_name, str):
                        raise ComputerTargetError("computer button name is required")
                    postcondition = self.computer.click_button(
                        expected=expected, button_name=button_name
                    )
                else:
                    raise ComputerTargetError("computer operation is not supported")
        except ComputerTargetError:
            return ConnectorOutcome(
                result=RemoteExecutionResult(
                    job_id=job.job_id,
                    result_idempotency_key=f"local-computer:{job.job_id}",
                    status=RemoteJobStatus.FAILED,
                    error_code="computer.target_rejected",
                )
            )
        return ConnectorOutcome(
            result=RemoteExecutionResult(
                job_id=job.job_id,
                result_idempotency_key=f"local-computer:{job.job_id}",
                status=RemoteJobStatus.SUCCEEDED,
                postcondition=cast(dict[str, JsonValue], postcondition),
            )
        )

    def cancel(self, job_id: str) -> None:
        del job_id
