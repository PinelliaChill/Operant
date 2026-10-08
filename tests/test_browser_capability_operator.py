from __future__ import annotations

import asyncio

import pytest

from operant.remote.local_browser import BrowserTargetError, BrowserTargetPolicy
from operant.remote.operator import (
    BrowserCapabilityOperator,
    CapabilityLeaseBinding,
    CapabilityOperationError,
    ComputerCapabilityOperator,
    bound_observation_key,
)


class FakePhase56Client:
    def __init__(self) -> None:
        self.observations = 0
        self.observation_keys: list[str] = []
        self.actions: list[dict[str, object]] = []
        self.result: dict[str, object] = {}

    def observe_browser(
        self, target_id: str, request: dict[str, object], **_: object
    ) -> dict[str, object]:
        assert target_id == "target-test"
        assert request["token"] == "test-lease-token-123456"
        self.observation_keys.append(str(request["idempotency_key"]))
        self.observations += 1
        return {"job_id": "observation-job"}

    def act_browser(self, request: dict[str, object], **_: object) -> dict[str, object]:
        self.actions.append(request)
        return {"job": {"job_id": "action-job"}}

    def observe_computer(
        self, target_id: str, request: dict[str, object], **_: object
    ) -> dict[str, object]:
        return self.observe_browser(target_id, request)

    def act_computer(self, request: dict[str, object], **_: object) -> dict[str, object]:
        return self.act_browser(request)

    def get_remote_target_job_result(self, job_id: str) -> dict[str, object]:
        assert job_id in {"observation-job", "action-job"}
        return self.result


def _operator(client: FakePhase56Client) -> BrowserCapabilityOperator:
    return BrowserCapabilityOperator(
        client,  # type: ignore[arg-type]
        CapabilityLeaseBinding(
            target_id="target-test",
            lease_id="lease-test",
            token="test-lease-token-123456",
            fencing=1,
        ),
        BrowserTargetPolicy(frozenset({"http://127.0.0.1:8765"})),
    )


def test_operator_reads_durable_observation_without_exposing_lease() -> None:
    client = FakePhase56Client()
    client.result = {
        "status": "succeeded",
        "result": {"status": "succeeded"},
        "observation": {"observation_hash": "a" * 64, "body": {"url": "about:blank"}},
    }
    operator = _operator(client)
    assert operator.observe()["observation"]["observation_hash"] == "a" * 64
    assert "test-lease-token" not in repr(operator.binding)
    assert client.observations == 1


@pytest.mark.asyncio
async def test_bound_observation_key_survives_worker_thread_and_resets() -> None:
    client = FakePhase56Client()
    client.result = {
        "status": "succeeded",
        "result": {"status": "succeeded"},
        "observation": {"observation_hash": "a" * 64, "body": {"url": "about:blank"}},
    }
    operator = _operator(client)
    with bound_observation_key("same-tool-call-key"):
        await asyncio.to_thread(operator.observe)
        await asyncio.to_thread(operator.observe)
    await asyncio.to_thread(operator.observe)
    assert client.observation_keys[:2] == ["same-tool-call-key", "same-tool-call-key"]
    assert client.observation_keys[2] != "same-tool-call-key"


def test_operator_rejects_other_origin_and_credential_before_core_call() -> None:
    client = FakePhase56Client()
    operator = _operator(client)
    with pytest.raises(BrowserTargetError, match="approved origin"):
        operator.act(
            "navigate",
            observation_hash="a" * 64,
            idempotency_key="navigate-1",
            url="http://127.0.0.1:8766/",
        )
    with pytest.raises(ValueError, match="credential"):
        operator.act(
            "fill",
            observation_hash="a" * 64,
            idempotency_key="fill-1",
            selector="#field",
            value="sk-abcdefghijklmnopqrstuv",
        )
    assert client.actions == []


def test_operator_keeps_unknown_click_job_for_reconciliation() -> None:
    client = FakePhase56Client()
    client.result = {
        "status": "manual_reconcile_required",
        "result": {"error_code": "plugin.outcome_unknown"},
    }
    operator = _operator(client)
    with pytest.raises(CapabilityOperationError) as failure:
        operator.act(
            "click",
            observation_hash="a" * 64,
            idempotency_key="click-1",
            selector="#go",
        )
    assert failure.value.job_id == "action-job"
    assert failure.value.status == "manual_reconcile_required"
    assert len(client.actions) == 1


def test_computer_operator_binds_app_and_preserves_unknown_job() -> None:
    client = FakePhase56Client()
    binding = CapabilityLeaseBinding(
        target_id="target-test",
        lease_id="lease-test",
        token="test-lease-token-123456",
        fencing=1,
    )
    operator = ComputerCapabilityOperator(
        client,  # type: ignore[arg-type]
        binding,
        frozenset({"com.example.Editor"}),
    )
    client.result = {
        "status": "succeeded",
        "result": {"status": "succeeded"},
        "observation": {
            "observation_hash": "a" * 64,
            "body": {"bundle_id": "com.example.Editor", "buttons": ["Run"]},
        },
    }
    assert operator.observe()["observation"]["body"]["bundle_id"] == "com.example.Editor"
    client.result = {
        "status": "manual_reconcile_required",
        "result": {"error_code": "plugin.outcome_unknown"},
    }
    with pytest.raises(CapabilityOperationError) as failure:
        operator.click_button(
            button_name="Run", observation_hash="a" * 64, idempotency_key="click-1"
        )
    assert failure.value.job_id == "action-job"
    assert client.actions[0]["action"]["capability"] == "computer.input"  # type: ignore[index]
