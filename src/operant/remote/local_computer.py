"""Narrow macOS accessibility adapter for approved, foreground app controls.

This adapter never accepts AppleScript text from a caller.  Accessibility is
an operating-system permission; when it is absent the operation fails closed.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
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
from operant.remote.sealed_input import open_browser_input


class ComputerTargetError(RuntimeError):
    """The computer target is unavailable or no longer matches the observation."""


_OBSERVE_SCRIPT = r"""
on isSafeTextElement(elementRef)
    tell application "System Events"
        try
            set elementRole to role of elementRef as text
            if elementRole is not "AXTextField" and ¬
                elementRole is not "AXTextArea" then return false
            set elementSubrole to ""
            try
                set elementSubrole to subrole of elementRef as text
            end try
            if elementSubrole contains "Secure" or ¬
                elementSubrole contains "Password" then return false
            if elementRole contains "Secure" or elementRole contains "Password" then return false
            return true
        on error
            return false
        end try
    end tell
end isSafeTextElement

on textAreasWithin(elementRef, depth)
    if depth > 8 then return {}
    tell application "System Events"
        try
            if role of elementRef is "AXTextArea" then
                if my isSafeTextElement(elementRef) then return {elementRef}
                return {}
            end if
            set children to UI elements of elementRef
        on error
            set children to {}
        end try
    end tell
    set found to {}
    repeat with child in children
        set found to found & (my textAreasWithin(child, depth + 1))
    end repeat
    return found
end textAreasWithin

on run argv
    tell application "System Events"
        set targetProcess to first application process whose frontmost is true
        set appId to bundle identifier of targetProcess
        set windowTitle to name of front window of targetProcess
        set buttonNames to name of every button of front window of targetProcess
        set AppleScript's text item delimiters to character id 31
        set joinedButtons to buttonNames as text
        set AppleScript's text item delimiters to ""
        set fieldNames to {}
        set fieldValues to {}
        repeat with textField in (every text field of front window of targetProcess)
            try
                if my isSafeTextElement(textField) then
                    set end of fieldNames to (name of textField as text)
                    set end of fieldValues to (value of textField as text)
                end if
            end try
        end repeat
        set textAreaCount to 0
        set textAreas to my textAreasWithin(front window of targetProcess, 0)
        repeat with uiElement in textAreas
            try
                set textAreaCount to textAreaCount + 1
                set end of fieldNames to "AXTextArea:" & textAreaCount
                set end of fieldValues to (value of uiElement as text)
            end try
        end repeat
        set AppleScript's text item delimiters to character id 31
        set joinedFields to fieldNames as text
        set joinedValues to fieldValues as text
        set AppleScript's text item delimiters to ""
        return appId & (character id 30) & windowTitle & (character id 30) & ¬
            joinedButtons & (character id 30) & joinedFields & (character id 30) & joinedValues
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

_TYPE_TEXT_SCRIPT = r"""
on isSafeTextElement(elementRef)
    tell application "System Events"
        try
            set elementRole to role of elementRef as text
            if elementRole is not "AXTextField" and ¬
                elementRole is not "AXTextArea" then return false
            set elementSubrole to ""
            try
                set elementSubrole to subrole of elementRef as text
            end try
            if elementSubrole contains "Secure" or ¬
                elementSubrole contains "Password" then return false
            if elementRole contains "Secure" or elementRole contains "Password" then return false
            return true
        on error
            return false
        end try
    end tell
end isSafeTextElement

on textAreasWithin(elementRef, depth)
    if depth > 8 then return {}
    tell application "System Events"
        try
            if role of elementRef is "AXTextArea" then
                if my isSafeTextElement(elementRef) then return {elementRef}
                return {}
            end if
            set children to UI elements of elementRef
        on error
            set children to {}
        end try
    end tell
    set found to {}
    repeat with child in children
        set found to found & (my textAreasWithin(child, depth + 1))
    end repeat
    return found
end textAreasWithin

on run argv
    set expectedApp to item 1 of argv
    set expectedWindow to item 2 of argv
    set fieldName to item 3 of argv
    set enteredText to item 4 of argv
    tell application "System Events"
        set targetProcess to first application process whose frontmost is true
        if bundle identifier of targetProcess is not expectedApp then error "target app changed"
        if name of front window of targetProcess is not expectedWindow then ¬
            error "target window changed"
        if fieldName starts with "AXTextArea:" then
            set textAreas to my textAreasWithin(front window of targetProcess, 0)
            if (count of textAreas) is not 1 or fieldName is not "AXTextArea:1" then ¬
                error "text area is ambiguous"
            if not my isSafeTextElement(item 1 of textAreas) then error "secure text area"
            set value of (item 1 of textAreas) to enteredText
        else
            set matchingFields to (text fields of front window of targetProcess ¬
                whose name is fieldName)
            if (count of matchingFields) is not 1 then error "text field is ambiguous"
            set targetField to first item of matchingFields
            if not my isSafeTextElement(targetField) then error "secure text field"
            set value of targetField to enteredText
        end if
    end tell
end run
"""

_PRESS_KEY_SCRIPT = r"""
on run argv
    set expectedApp to item 1 of argv
    set expectedWindow to item 2 of argv
    set keyNumber to (item 3 of argv) as integer
    tell application "System Events"
        set targetProcess to first application process whose frontmost is true
        if bundle identifier of targetProcess is not expectedApp then error "target app changed"
        if name of front window of targetProcess is not expectedWindow then ¬
            error "target window changed"
        try
            set focusedElement to value of attribute "AXFocusedUIElement" of targetProcess
            set focusedRole to role of focusedElement as text
            set focusedSubrole to ""
            try
                set focusedSubrole to subrole of focusedElement as text
            end try
            if focusedRole contains "Secure" or focusedRole contains "Password" or ¬
                focusedSubrole contains "Secure" or focusedSubrole contains "Password" then ¬
                error "secure focused field"
        on error
            error "focused element is unavailable or secure"
        end try
        key code keyNumber
    end tell
end run
"""

_ACTIVATE_SCRIPT = r"""
on run argv
    set expectedApp to item 1 of argv
    tell application "System Events"
        set matchingProcesses to (application processes whose bundle identifier is expectedApp)
        if (count of matchingProcesses) is not 1 then error "target app is not running"
        set targetProcess to first item of matchingProcesses
        set frontmost of targetProcess to true
        repeat 20 times
            if frontmost of targetProcess then return expectedApp
            delay 0.05
        end repeat
        error "target app did not become foreground"
    end tell
end run
"""

_WINDOW_PID_SCRIPT = r"""
on run argv
    set expectedApp to item 1 of argv
    set expectedWindow to item 2 of argv
    tell application "System Events"
        set matchingProcesses to (application processes whose bundle identifier is expectedApp)
        if (count of matchingProcesses) is not 1 then error "target app is not running"
        set targetProcess to first item of matchingProcesses
        if frontmost of targetProcess is not true then error "target app is not foreground"
        if name of front window of targetProcess is not expectedWindow then ¬
            error "target window changed"
        return unix id of targetProcess as text
    end tell
end run
"""

_KEY_CODES = {
    "Tab": 48,
    "Return": 36,
    "Enter": 76,
    "Escape": 53,
    "ArrowLeft": 123,
    "ArrowRight": 124,
    "ArrowDown": 125,
    "ArrowUp": 126,
    "Backspace": 51,
    "Delete": 117,
    "Home": 115,
    "End": 119,
    "PageUp": 116,
    "PageDown": 121,
}


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
    def __init__(self, policy: ComputerTargetPolicy, target_bundle_id: str | None = None) -> None:
        self.policy = policy
        if target_bundle_id is not None:
            policy.check(target_bundle_id)
        self.target_bundle_id = target_bundle_id
        if sys.platform != "darwin":
            raise ComputerTargetError("macOS accessibility is unavailable on this platform")

    def activate_app(self) -> None:
        """Focus only an already running, explicitly chosen allowlisted App."""
        if self.target_bundle_id is None:
            return
        self.policy.check(self.target_bundle_id)
        if self._script(_ACTIVATE_SCRIPT, self.target_bundle_id) != self.target_bundle_id:
            raise ComputerTargetError("approved App did not become foreground")

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

    def _raw_observation(self) -> tuple[str, str, list[str], list[str], str]:
        self.activate_app()
        response = self._script(_OBSERVE_SCRIPT).split("\x1e", 4)
        if len(response) != 5:
            raise ComputerTargetError("macOS returned an incomplete window observation")
        bundle_id, window_title, buttons, fields, field_values = response
        self.policy.check(bundle_id)
        return (
            bundle_id,
            window_title,
            [name.strip() for name in buttons.split("\x1f") if name.strip()][:100],
            [name.strip() for name in fields.split("\x1f") if name.strip()][:100],
            field_values,
        )

    @staticmethod
    def _public_observation(
        bundle_id: str, window_title: str, buttons: list[str], fields: list[str], field_values: str
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
            "fields": cast(list[JsonValue], [redact_public_text(name[:200]) for name in fields]),
            "fields_state_sha256": hashlib.sha256(field_values.encode()).hexdigest(),
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
        current_bundle, current_title, current_buttons, current_fields, field_values = (
            self._raw_observation()
        )
        if (
            self._public_observation(
                current_bundle, current_title, current_buttons, current_fields, field_values
            )
            != expected
        ):
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

    def _checked_window(self, expected: dict[str, JsonValue]) -> tuple[str, str, list[str]]:
        bundle_id, title, buttons, fields, values = self._raw_observation()
        if self._public_observation(bundle_id, title, buttons, fields, values) != expected:
            raise ComputerTargetError("computer window changed since observation")
        return bundle_id, title, fields

    def type_text(
        self, *, expected: dict[str, JsonValue], element_name: str, value: str
    ) -> dict[str, JsonValue]:
        bundle_id, title, fields = self._checked_window(expected)
        if (
            fields.count(element_name) != 1
            or not 1 <= len(element_name) <= 200
            or len(value) > 2_000
        ):
            raise ComputerTargetError("computer text field is absent or ambiguous")
        if redact_public_text(element_name) != element_name or redact_public_text(value) != value:
            raise ComputerTargetError("computer text input is credential-shaped")
        try:
            self._script(_TYPE_TEXT_SCRIPT, bundle_id, title, element_name, value)
            return self.observe()
        except ComputerTargetError as exc:
            raise RemoteOutcomeUnknown("computer text input outcome is unknown") from exc

    def press_key(self, *, expected: dict[str, JsonValue], key: str) -> dict[str, JsonValue]:
        bundle_id, title, _ = self._checked_window(expected)
        if key not in _KEY_CODES:
            raise ComputerTargetError("computer key is outside the approved set")
        try:
            self._script(_PRESS_KEY_SCRIPT, bundle_id, title, str(_KEY_CODES[key]))
            return self.observe()
        except ComputerTargetError as exc:
            raise RemoteOutcomeUnknown("computer key outcome is unknown") from exc

    def capture_window(self, *, expected: dict[str, JsonValue]) -> bytes:
        bundle_id, title, _ = self._checked_window(expected)
        window_id = self._window_id(bundle_id, title)
        with tempfile.TemporaryDirectory(prefix="operant-window-") as directory:
            image_path = Path(directory) / "window.png"
            try:
                result = subprocess.run(
                    ["/usr/sbin/screencapture", "-x", "-l", str(window_id), str(image_path)],
                    capture_output=True,
                    timeout=10,
                    check=False,
                    env={"HOME": os.path.expanduser("~"), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
                )
            except subprocess.TimeoutExpired as exc:
                raise ComputerTargetError("macOS window screenshot timed out") from exc
            if result.returncode != 0 or not image_path.is_file():
                raise ComputerTargetError(
                    "macOS Screen Recording permission or window is unavailable"
                )
            if image_path.stat().st_size > 16_777_216:
                raise ComputerTargetError("macOS window screenshot exceeds size limit")
            image = image_path.read_bytes()
        if not image.startswith(b"\x89PNG\r\n\x1a\n") or self.observe() != expected:
            raise ComputerTargetError("macOS window screenshot or target changed")
        return image

    @classmethod
    def _window_id(cls, bundle_id: str, title: str) -> int:
        """Find exactly one on-screen window for the approved running App and title."""
        pid_text = cls._script(_WINDOW_PID_SCRIPT, bundle_id, title)
        if not pid_text.isdecimal() or not 1 <= int(pid_text) <= 2**31 - 1:
            raise ComputerTargetError("approved App PID is unavailable")
        coregraphics_path = ctypes.util.find_library("CoreGraphics")
        corefoundation_path = ctypes.util.find_library("CoreFoundation")
        if not coregraphics_path or not corefoundation_path:
            raise ComputerTargetError("macOS window enumeration is unavailable")
        cg = ctypes.CDLL(coregraphics_path)
        cf = ctypes.CDLL(corefoundation_path)
        cg.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        cg.CGWindowListCopyWindowInfo.restype = ctypes.c_void_p
        cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
        cf.CFArrayGetCount.restype = ctypes.c_long
        cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
        cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
        cf.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        cf.CFDictionaryGetValue.restype = ctypes.c_void_p
        cf.CFGetTypeID.argtypes = [ctypes.c_void_p]
        cf.CFGetTypeID.restype = ctypes.c_ulong
        cf.CFNumberGetTypeID.restype = ctypes.c_ulong
        cf.CFStringGetTypeID.restype = ctypes.c_ulong
        cf.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        cf.CFNumberGetValue.restype = ctypes.c_bool
        cf.CFStringGetCString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_long,
            ctypes.c_uint32,
        ]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        keys = {
            name: ctypes.c_void_p.in_dll(cg, name).value
            for name in ("kCGWindowOwnerPID", "kCGWindowName", "kCGWindowLayer", "kCGWindowNumber")
        }

        def number(dictionary: int, key: str) -> int | None:
            raw = cf.CFDictionaryGetValue(dictionary, keys[key])
            if not raw or cf.CFGetTypeID(raw) != cf.CFNumberGetTypeID():
                return None
            value = ctypes.c_int()
            if not cf.CFNumberGetValue(raw, 9, ctypes.byref(value)):
                return None
            return value.value

        # optionOnScreenOnly | excludeDesktopElements. Never query off-screen
        # windows or use a broad screenshot fallback.
        windows = cg.CGWindowListCopyWindowInfo(17, 0)
        if not windows:
            raise ComputerTargetError("macOS window enumeration is unavailable")
        matching: list[int] = []
        try:
            count = cf.CFArrayGetCount(windows)
            for index in range(min(count, 1_000)):
                item = cf.CFArrayGetValueAtIndex(windows, index)
                if not item or number(item, "kCGWindowOwnerPID") != int(pid_text):
                    continue
                if number(item, "kCGWindowLayer") != 0:
                    continue
                raw_name = cf.CFDictionaryGetValue(item, keys["kCGWindowName"])
                if not raw_name or cf.CFGetTypeID(raw_name) != cf.CFStringGetTypeID():
                    continue
                buffer = ctypes.create_string_buffer(2_048)
                if not cf.CFStringGetCString(raw_name, buffer, len(buffer), 0x08000100):
                    continue
                if buffer.value.decode("utf-8") != title:
                    continue
                window_id = number(item, "kCGWindowNumber")
                if window_id is not None and 1 <= window_id <= 2**32 - 1:
                    matching.append(window_id)
        finally:
            cf.CFRelease(windows)
        if len(matching) != 1:
            raise ComputerTargetError("approved App window is unavailable or ambiguous")
        return matching[0]

    def read_clipboard(self, *, expected: dict[str, JsonValue]) -> bytes:
        self._checked_window(expected)
        try:
            result = subprocess.run(
                ["/usr/bin/pbpaste", "-Prefer", "txt"],
                capture_output=True,
                timeout=5,
                check=False,
                env={"HOME": os.path.expanduser("~"), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )
        except subprocess.TimeoutExpired as exc:
            raise ComputerTargetError("clipboard read timed out") from exc
        if result.returncode != 0 or len(result.stdout) > 2_000:
            raise ComputerTargetError("clipboard text is unavailable or too long")
        try:
            value = result.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ComputerTargetError("clipboard is not UTF-8 text") from exc
        if redact_public_text(value) != value or self.observe() != expected:
            raise ComputerTargetError("clipboard may contain credentials or target changed")
        return result.stdout

    def write_clipboard(
        self, *, expected: dict[str, JsonValue], value: str
    ) -> dict[str, JsonValue]:
        self._checked_window(expected)
        if len(value) > 2_000 or redact_public_text(value) != value:
            raise ComputerTargetError("clipboard input is too long or credential-shaped")
        try:
            result = subprocess.run(
                ["/usr/bin/pbcopy"],
                input=value.encode(),
                capture_output=True,
                timeout=5,
                check=False,
                env={"HOME": os.path.expanduser("~"), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )
        except subprocess.TimeoutExpired as exc:
            raise RemoteOutcomeUnknown("clipboard write outcome is unknown") from exc
        if result.returncode != 0:
            raise RemoteOutcomeUnknown("clipboard write outcome is unknown")
        try:
            self._checked_window(expected)
            readback = subprocess.run(
                ["/usr/bin/pbpaste", "-Prefer", "txt"],
                capture_output=True,
                timeout=5,
                check=False,
                env={"HOME": os.path.expanduser("~"), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )
            if readback.returncode != 0 or readback.stdout != value.encode():
                raise RemoteOutcomeUnknown("clipboard write outcome is unknown")
        except ComputerTargetError as exc:
            raise RemoteOutcomeUnknown("clipboard write outcome is unknown") from exc
        except subprocess.TimeoutExpired as exc:
            raise RemoteOutcomeUnknown("clipboard write outcome is unknown") from exc
        return self.observe()


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
        artifact_bytes: bytes | None = None
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
                content_hash = job.arguments.get("observation_content_hash", expected_hash)
                if (
                    not isinstance(expected_hash, str)
                    or not isinstance(content_hash, str)
                    or canonical_action_hash(
                        {"target_id": self.target_id, "target_ref": target_ref, "body": expected}
                    )
                    != content_hash
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
                elif (
                    job.capability is RemoteCapability.COMPUTER_INPUT
                    and job.operation == "type_text"
                ):
                    element_name = arguments.get("element_name")
                    if not isinstance(element_name, str):
                        raise ComputerTargetError("computer text field name is required")
                    value = self._open_input(
                        arguments.get("value_sealed"),
                        expected_hash,
                        job.idempotency_key,
                        f"computer:type_text:{element_name}",
                    )
                    postcondition = self.computer.type_text(
                        expected=expected, element_name=element_name, value=value
                    )
                elif (
                    job.capability is RemoteCapability.COMPUTER_INPUT
                    and job.operation == "press_key"
                ):
                    key = arguments.get("key")
                    if not isinstance(key, str):
                        raise ComputerTargetError("computer key is required")
                    postcondition = self.computer.press_key(expected=expected, key=key)
                elif (
                    job.capability is RemoteCapability.COMPUTER_SCREENSHOT
                    and job.operation == "capture_window"
                    and not arguments
                ):
                    artifact_bytes = self.computer.capture_window(expected=expected)
                    postcondition = {
                        "media_type": "image/png",
                        "width": int.from_bytes(artifact_bytes[16:20], "big"),
                        "height": int.from_bytes(artifact_bytes[20:24], "big"),
                    }
                elif (
                    job.capability is RemoteCapability.CLIPBOARD_READ
                    and job.operation == "read_clipboard"
                    and not arguments
                ):
                    artifact_bytes = self.computer.read_clipboard(expected=expected)
                    postcondition = {
                        "media_type": "text/plain; charset=utf-8",
                        "byte_length": len(artifact_bytes),
                    }
                elif (
                    job.capability is RemoteCapability.CLIPBOARD_WRITE
                    and job.operation == "write_clipboard"
                ):
                    value = self._open_input(
                        arguments.get("value_sealed"),
                        expected_hash,
                        job.idempotency_key,
                        "computer:write_clipboard",
                    )
                    observed_after = self.computer.write_clipboard(expected=expected, value=value)
                    postcondition = {
                        **observed_after,
                        "clipboard_sha256": hashlib.sha256(value.encode()).hexdigest(),
                    }
                else:
                    raise ComputerTargetError("computer operation is not supported")
                postcondition = {
                    **postcondition,
                    "pre_observation_hash": expected_hash,
                    "post_observation_hash": canonical_action_hash(
                        {
                            "target_id": self.target_id,
                            "target_ref": target_ref,
                            "body": (
                                expected
                                if job.operation in {"capture_window", "read_clipboard"}
                                else observed_after
                                if job.operation == "write_clipboard"
                                else postcondition
                            ),
                        }
                    ),
                }
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
                artifact_ref=(
                    f"local-capability:{job.job_id}" if artifact_bytes is not None else None
                ),
                artifact_sha256=(
                    hashlib.sha256(artifact_bytes).hexdigest()
                    if artifact_bytes is not None
                    else None
                ),
                postcondition=cast(dict[str, JsonValue], postcondition),
            ),
            artifact_bytes=artifact_bytes,
        )

    def _open_input(
        self, sealed: Any, observation_hash: str, idempotency_key: str, selector: str
    ) -> str:
        if not isinstance(sealed, str):
            raise ComputerTargetError("computer action requires sealed text")
        try:
            return open_browser_input(
                sealed,
                token=self.lease_token,
                target_id=self.target_id,
                lease_id=self.lease_id,
                fencing=self.lease_fencing,
                observation_hash=observation_hash,
                selector=selector,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            raise ComputerTargetError("computer input envelope cannot be opened") from exc

    def cancel(self, job_id: str) -> None:
        del job_id
