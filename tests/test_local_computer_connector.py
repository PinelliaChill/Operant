from __future__ import annotations

import json
import sys
from typing import Any

import pytest

from operant.domain.remote_execution import (
    RemoteActionIdempotency,
    RemoteCapability,
    RemoteExecutionJob,
    RemoteJobStatus,
)
from operant.protocol import canonical_action_hash
from operant.remote.local_computer import (
    ComputerTargetError,
    ComputerTargetPolicy,
    LocalComputerConnector,
    MacComputer,
)
from operant.remote.sealed_input import seal_browser_input


def _job(
    capability: RemoteCapability, operation: str, arguments: dict[str, Any]
) -> RemoteExecutionJob:
    return RemoteExecutionJob(
        target_id="local-computer-test",
        lease_id="lease-test",
        lease_fencing=1,
        capability=capability,
        operation=operation,
        arguments={"target_ref": "local-computer-test", **arguments},
        action_hash="b" * 64,
        idempotency_key=f"computer-{operation}",
        idempotency=RemoteActionIdempotency.NON_IDEMPOTENT,
    )


def test_computer_policy_blocks_unlisted_and_sensitive_apps() -> None:
    policy = ComputerTargetPolicy(frozenset({"com.example.Test"}))
    policy.check("com.example.Test")
    with pytest.raises(ComputerTargetError):
        policy.check("com.example.Other")
    with pytest.raises(ComputerTargetError):
        ComputerTargetPolicy(frozenset({"com.example.Test", "com.apple.Terminal"}))


def test_computer_connector_checks_observation_and_target_before_input() -> None:
    class FakeComputer:
        def __init__(self) -> None:
            self.clicks = 0

        def observe(self) -> dict[str, Any]:
            return {"bundle_id": "com.example.Test", "window_title": "Window", "buttons": ["Go"]}

        def click_button(self, *, expected: dict[str, Any], button_name: str) -> dict[str, Any]:
            assert expected == self.observe() and button_name == "Go"
            self.clicks += 1
            return self.observe()

    computer = FakeComputer()
    connector = LocalComputerConnector(
        target_id="local-computer-test",
        lease_id="lease-test",
        lease_token="token-test-1234567890",
        lease_fencing=1,
        computer=computer,  # type: ignore[arg-type]
    )
    observed = connector.execute(_job(RemoteCapability.COMPUTER_OBSERVE, "observe_computer", {}))
    assert observed.result.status is RemoteJobStatus.SUCCEEDED
    body = observed.result.postcondition["observation"]
    assert isinstance(body, dict)
    stale = connector.execute(
        _job(
            RemoteCapability.COMPUTER_INPUT,
            "click_button",
            {"observation_hash": "0" * 64, "arguments": {"button_name": "Go"}},
        )
    )
    assert stale.result.status is RemoteJobStatus.FAILED
    assert computer.clicks == 0
    current_hash = canonical_action_hash(
        {"target_id": "local-computer-test", "target_ref": "local-computer-test", "body": body}
    )
    clicked = connector.execute(
        _job(
            RemoteCapability.COMPUTER_INPUT,
            "click_button",
            {"observation_hash": current_hash, "arguments": {"button_name": "Go"}},
        )
    )
    assert clicked.result.status is RemoteJobStatus.SUCCEEDED
    assert computer.clicks == 1
    assert clicked.result.postcondition["pre_observation_hash"] == current_hash
    assert clicked.result.postcondition["post_observation_hash"] == current_hash


def test_macos_adapter_fails_closed_without_accessibility(monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform != "darwin":
        pytest.skip("macOS accessibility adapter")

    def denied(*_args: Any, **_kwargs: Any) -> Any:
        raise ComputerTargetError("macOS accessibility denied")

    monkeypatch.setattr(MacComputer, "_script", denied)
    computer = MacComputer(ComputerTargetPolicy(frozenset({"com.example.Test"})))
    with pytest.raises(ComputerTargetError, match="accessibility"):
        computer.observe()


def test_chosen_computer_app_is_activated_without_launching_other_apps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform != "darwin":
        pytest.skip("macOS accessibility adapter")
    calls: list[str] = []

    def script(source: str, *_arguments: str) -> str:
        calls.append(source)
        if "set frontmost of targetProcess" in source:
            assert _arguments == ("com.example.Test",)
            assert "tell application id" not in source
            return "com.example.Test"
        return "com.example.Test\x1eDraft\x1eGo\x1eField\x1econtent"

    monkeypatch.setattr(MacComputer, "_script", staticmethod(script))
    computer = MacComputer(
        ComputerTargetPolicy(frozenset({"com.example.Test"})),
        target_bundle_id="com.example.Test",
    )
    assert computer.observe()["bundle_id"] == "com.example.Test"
    assert len(calls) == 2
    with pytest.raises(ComputerTargetError, match="outside the approved set"):
        MacComputer(
            ComputerTargetPolicy(frozenset({"com.example.Test"})),
            target_bundle_id="com.example.Other",
        )


def test_nested_text_area_selector_is_bound_and_content_stays_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform != "darwin":
        pytest.skip("macOS accessibility adapter")
    calls: list[tuple[str, tuple[str, ...]]] = []
    raw = "com.example.Test\x1eTemporary Document\x1eSave\x1eAXTextArea:1\x1eprivate draft"

    def script(source: str, *arguments: str) -> str:
        calls.append((source, arguments))
        return raw if "set fieldNames" in source else ""

    monkeypatch.setattr(MacComputer, "_script", staticmethod(script))
    computer = MacComputer(ComputerTargetPolicy(frozenset({"com.example.Test"})))
    observed = computer.observe()
    assert observed["fields"] == ["AXTextArea:1"]
    assert "private draft" not in json.dumps(observed)
    assert (
        computer.type_text(expected=observed, element_name="AXTextArea:1", value="replacement")
        == observed
    )
    assert any(
        "set value of (item 1 of textAreas)" in source
        and args[-2:] == ("AXTextArea:1", "replacement")
        for source, args in calls
    )
    with pytest.raises(ComputerTargetError, match="absent or ambiguous"):
        computer.type_text(expected=observed, element_name="AXTextArea:2", value="bad")


def test_macos_observation_redacts_labels_but_preserves_private_change_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform != "darwin":
        pytest.skip("macOS accessibility adapter")
    secret = "sk-abcdefghijklmnopqrstuv"
    raw = f"com.example.Test\x1eWork {secret}\x1eGo\x1fBearer {secret}\x1eDraft\x1e{secret}"

    def script(source: str, *_arguments: str) -> str:
        return raw if "set targetProcess" in source and "click (" not in source else ""

    monkeypatch.setattr(MacComputer, "_script", staticmethod(script))
    computer = MacComputer(ComputerTargetPolicy(frozenset({"com.example.Test"})))
    observed = computer.observe()
    assert secret not in json.dumps(observed)
    assert observed["buttons"][0] == "Go"  # type: ignore[index]
    assert computer.click_button(expected=observed, button_name="Go") == observed
    with pytest.raises(ComputerTargetError, match="approved observation"):
        computer.click_button(expected=observed, button_name=secret)
    changed = raw.replace("Work", "Other")
    assert changed != raw
    monkeypatch.setattr(
        MacComputer,
        "_script",
        staticmethod(lambda *_args: changed),
    )
    assert computer.observe()["window_title_sha256"] != observed["window_title_sha256"]


def test_computer_text_and_screenshot_keep_content_out_of_receipts() -> None:
    class FakeComputer:
        def observe(self) -> dict[str, Any]:
            return {
                "bundle_id": "com.example.Test",
                "fields": ["Draft"],
                "fields_state_sha256": "old",
            }

        def type_text(
            self, *, expected: dict[str, Any], element_name: str, value: str
        ) -> dict[str, Any]:
            assert expected == self.observe()
            assert (element_name, value) == ("Draft", "private draft")
            return {**expected, "fields_state_sha256": "new"}

        def capture_window(self, *, expected: dict[str, Any]) -> bytes:
            assert expected == self.observe()
            return (
                b"\x89PNG\r\n\x1a\n"
                + b"\x00" * 8
                + (20).to_bytes(4, "big")
                + (10).to_bytes(4, "big")
            )

    connector = LocalComputerConnector(
        target_id="local-computer-test",
        lease_id="lease-test",
        lease_token="token-test-1234567890",
        lease_fencing=1,
        computer=FakeComputer(),  # type: ignore[arg-type]
    )
    observed = connector.computer.observe()
    observed_hash = canonical_action_hash(
        {"target_id": connector.target_id, "target_ref": connector.target_id, "body": observed}
    )
    sealed = seal_browser_input(
        "private draft",
        token=connector.lease_token,
        target_id=connector.target_id,
        lease_id=connector.lease_id,
        fencing=connector.lease_fencing,
        observation_hash=observed_hash,
        selector="computer:type_text:Draft",
        idempotency_key="computer-type_text",
    )
    typed = connector.execute(
        _job(
            RemoteCapability.COMPUTER_INPUT,
            "type_text",
            {
                "observation_hash": observed_hash,
                "arguments": {"element_name": "Draft", "value_sealed": sealed},
            },
        )
    )
    assert typed.result.status is RemoteJobStatus.SUCCEEDED
    assert "private draft" not in typed.result.model_dump_json()
    screenshot = connector.execute(
        _job(
            RemoteCapability.COMPUTER_SCREENSHOT,
            "capture_window",
            {"observation_hash": observed_hash, "arguments": {}},
        )
    )
    assert screenshot.result.status is RemoteJobStatus.SUCCEEDED
    assert screenshot.result.postcondition["media_type"] == "image/png"
    assert screenshot.result.postcondition["post_observation_hash"] == observed_hash
    assert screenshot.artifact_bytes is not None
    assert screenshot.artifact_bytes.startswith(b"\x89PNG")
