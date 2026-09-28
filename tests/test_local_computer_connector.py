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


def test_macos_observation_redacts_labels_but_preserves_private_change_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform != "darwin":
        pytest.skip("macOS accessibility adapter")
    secret = "sk-abcdefghijklmnopqrstuv"
    raw = f"com.example.Test\x1eWork {secret}\x1eGo\x1fBearer {secret}"

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
