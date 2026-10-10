from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

import operant.api_local_control as local_control_api
import operant.remote.operator as local_operator
import operant.remote.tool_extensions as local_tool_extensions
from operant.api import _sse_event, create_app
from operant.application.local_control import snapshot_control_bindings
from operant.application.remote_execution import observation_hash_for_job
from operant.application.security import PolicyEngine
from operant.domain.messages import Message, ModelResponse, ProviderEvent, ToolCall, ToolDefinition
from operant.domain.models import ModelProfile, RolePreset, RoleSnapshot, ToolPolicy
from operant.domain.remote_execution import (
    CapabilityObservation,
    RemoteCapability,
    RemoteExecutionResult,
    RemoteJobStatus,
)
from operant.domain.security import (
    Capability,
    PolicyBundle,
    PolicyDecision,
    PolicyLayer,
    PolicyRule,
)
from operant.persistence.security import SQLiteSecurityRepository
from operant.protocol import canonical_action_hash
from operant.remote.operator import CapabilityOperationError
from operant.remote.sealed_input import seal_browser_input
from sdk.python_client.phase56_generated import Phase56Client
from sdk.python_client.transport import Phase1EError, TransportRequest, TransportResponse


class _Connector:
    def __init__(self, lease: Any) -> None:
        self.target_id = lease.target_id
        self.lease_id = lease.lease_id
        self.lease_token = lease.token
        self.lease_fencing = lease.fencing

    def cancel(self, _job_id: str) -> None:
        pass


class _ObserveProvider:
    def __init__(
        self,
        *,
        two_calls: bool = False,
        tool_name: str = "ext_browser_observe",
        arguments_json: str = "{}",
    ) -> None:
        self.turn = 0
        self.two_calls = two_calls
        self.tool_name = tool_name
        self.arguments_json = arguments_json

    async def list_models(self, *, base_url: str, secret_ref: str) -> list[str]:
        return ["test-model"]

    async def stream(
        self,
        *,
        snapshot: RoleSnapshot,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        self.turn += 1
        if self.turn == 1:
            yield ProviderEvent(
                event_type="model.completed",
                response=ModelResponse(
                    tool_calls=(
                        ToolCall(
                            id="observe-call",
                            name=self.tool_name,
                            arguments_json=self.arguments_json,
                        ),
                        *(
                            (
                                ToolCall(
                                    id="observe-call-2",
                                    name="ext_browser_observe",
                                    arguments_json="{}",
                                ),
                            )
                            if self.two_calls
                            else ()
                        ),
                    ),
                    finish_reason="tool_calls",
                ),
            )
            return
        yield ProviderEvent(
            event_type="model.completed",
            response=ModelResponse(content="Observed.", finish_reason="stop"),
        )


def test_phase56_observation_sdk_preserves_approval_binding_and_original_key(
    tmp_path: Path,
) -> None:
    app, service, manager, client = _app(tmp_path)
    browser = manager.registry.install("operant.chrome.browser", ("https://example.test",))
    manager.registry.set_enabled(browser.plugin_id, True)
    opened = client.post(
        "/v1/setup/local-control/sessions",
        json={"plugin_id": browser.plugin_id},
        headers={"Idempotency-Key": "sdk-browser-session"},
    )
    assert opened.status_code == 200, opened.text
    session = manager.get(opened.json()["session_id"])
    assert (
        app.state.remote_execution_controller.authorization.gateway
        is app.state.phase45_action_gateway
    )
    app.state.phase45_action_gateway.engine = PolicyEngine(
        PolicyBundle(
            bundle_id="ask-sdk-observation",
            version="ask-sdk.v1",
            default_decision=PolicyDecision.ASK,
            rules=(),
        )
    )

    def transport(request: TransportRequest) -> TransportResponse:
        path = urlsplit(request.url).path
        response = client.request(
            request.method,
            path,
            headers=dict(request.headers),
            content=request.body,
        )
        return TransportResponse(
            status=response.status_code, headers=dict(response.headers), body=response.content
        )

    sdk = Phase56Client("http://127.0.0.1:8000", transport=transport)
    key = "sdk-observe-original-key"
    body = {
        "lease_id": session.lease.lease_id,
        "token": session.lease.token,
        "fencing": session.lease.fencing,
        "target_ref": session.target_id,
        "idempotency_key": key,
    }
    try:
        with pytest.raises(Phase1EError) as asked:
            sdk.observe_browser(session.target_id, body, idempotency_key=key)
        assert asked.value.code == "http_409"
        assert isinstance(asked.value.detail, dict)
        assert asked.value.detail["code"] == "approval_required"
        approval_id = asked.value.detail["approval_id"]
        assert asked.value.detail["action_hash"]
        assert asked.value.detail["policy_version"] == "ask-sdk.v1"
        with service.store._connect() as connection:
            count = connection.execute("SELECT COUNT(*) FROM remote_execution_jobs").fetchone()[0]
        assert count == 0
        approved = client.post(
            f"/v1/security/approvals/{approval_id}",
            json={"approved": True, "reason_code": "test exact observation"},
        )
        assert approved.status_code == 200
        assert (
            app.state.phase45_action_gateway.phase_repository.get_phase45_approval(approval_id)[
                "status"
            ]
            == "approved"
        )
        inner_action = app.state.phase45_action_gateway.repository.get_security_action(
            asked.value.detail["action_hash"]
        )
        inner_evaluation = app.state.phase45_action_gateway.engine.evaluate(inner_action)
        assert inner_evaluation.decision is PolicyDecision.ASK
        assert inner_evaluation.policy_version == "ask-sdk.v1"
        assert (
            app.state.phase45_action_gateway.phase_repository.get_phase45_approval(approval_id)[
                "policy_version"
            ]
            == inner_evaluation.policy_version
        )
        created = sdk.observe_browser(session.target_id, body, idempotency_key=key)
        assert isinstance(created.get("job_id"), str)
        assert (
            app.state.phase45_action_gateway.phase_repository.get_phase45_approval(approval_id)[
                "status"
            ]
            == "consumed"
        )
        with service.store._connect() as connection:
            count = connection.execute("SELECT COUNT(*) FROM remote_execution_jobs").fetchone()[0]
        assert count == 1
    finally:
        service.close()


@pytest.mark.parametrize(
    "lease_state",
    ("released", "expired", "wrong_token", "fencing_revoked", "target_offline", "target_revoked"),
)
def test_phase56_sdk_rejects_stale_lease_before_phase45_ask(
    tmp_path: Path, lease_state: str
) -> None:
    app, service, manager, client = _app(tmp_path)
    plugin = manager.registry.install("operant.chrome.browser", ("http://127.0.0.1:18780",))
    manager.registry.set_enabled(plugin.plugin_id, True)
    opened = client.post(
        "/v1/setup/local-control/sessions",
        json={"plugin_id": plugin.plugin_id},
        headers={"Idempotency-Key": "stale-lease-session"},
    )
    assert opened.status_code == 200, opened.text
    session = manager.get(opened.json()["session_id"])
    lease = session.lease
    repository = app.state.remote_execution_repository
    gateway = app.state.phase45_action_gateway
    gateway.engine = PolicyEngine(
        PolicyBundle(
            bundle_id="ask-stale-lease",
            version="ask-stale-lease.v1",
            default_decision=PolicyDecision.ASK,
            rules=(),
        )
    )
    token = lease.token
    if lease_state == "released":
        repository.release_lease(
            target_id=session.target_id,
            lease_id=lease.lease_id,
            token=token,
            fencing=lease.fencing,
            now=datetime.now(timezone.utc),
        )
    elif lease_state == "wrong_token":
        token = "wrong-token-1234567890"
    else:
        with service.store._connect() as connection:
            if lease_state == "expired":
                connection.execute(
                    "UPDATE remote_target_leases SET expires_at=? WHERE lease_id=?",
                    (
                        (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                        lease.lease_id,
                    ),
                )
            elif lease_state == "fencing_revoked":
                connection.execute(
                    "UPDATE remote_execution_targets SET fencing=? WHERE target_id=?",
                    (lease.fencing + 1, session.target_id),
                )
            else:
                connection.execute(
                    "UPDATE remote_execution_targets SET status=? WHERE target_id=?",
                    (
                        "revoked" if lease_state == "target_revoked" else "offline",
                        session.target_id,
                    ),
                )

    def transport(request: TransportRequest) -> TransportResponse:
        response = client.request(
            request.method,
            urlsplit(request.url).path,
            headers=dict(request.headers),
            content=request.body,
        )
        return TransportResponse(
            status=response.status_code, headers=dict(response.headers), body=response.content
        )

    sdk = Phase56Client("http://127.0.0.1:8000", transport=transport)
    try:
        with pytest.raises(Phase1EError) as rejected:
            sdk.observe_browser(
                session.target_id,
                {
                    "lease_id": lease.lease_id,
                    "token": token,
                    "fencing": lease.fencing,
                    "target_ref": session.target_id,
                    "idempotency_key": f"stale-observe-{lease_state}",
                },
                idempotency_key=f"stale-observe-{lease_state}",
            )
        assert rejected.value.code == "http_409"
        if isinstance(rejected.value.detail, dict):
            assert rejected.value.detail.get("code") != "approval_required"
        with service.store._connect() as connection:
            assert (
                connection.execute("SELECT COUNT(*) FROM phase45_approval_requests").fetchone()[0]
                == 0
            )
            assert (
                connection.execute("SELECT COUNT(*) FROM remote_execution_jobs").fetchone()[0] == 0
            )
    finally:
        service.close()


def test_phase56_sdk_chains_verified_post_observations_under_one_local_lease(
    tmp_path: Path,
) -> None:
    app, service, manager, client = _app(tmp_path)
    origin = "http://127.0.0.1:18780"
    plugin = manager.registry.install("operant.chrome.browser", (origin,))
    manager.registry.set_enabled(plugin.plugin_id, True)
    opened = client.post(
        "/v1/setup/local-control/sessions",
        json={"plugin_id": plugin.plugin_id},
        headers={"Idempotency-Key": "post-observation-session"},
    )
    assert opened.status_code == 200, opened.text
    session = manager.get(opened.json()["session_id"])
    target_id = session.target_id
    lease = session.lease
    repository = app.state.remote_execution_repository
    controller = app.state.remote_execution_controller

    def transport(request: TransportRequest) -> TransportResponse:
        response = client.request(
            request.method,
            urlsplit(request.url).path,
            headers=dict(request.headers),
            content=request.body,
        )
        return TransportResponse(
            status=response.status_code, headers=dict(response.headers), body=response.content
        )

    sdk = Phase56Client("http://127.0.0.1:8000", transport=transport)
    binding = {"lease_id": lease.lease_id, "token": lease.token, "fencing": lease.fencing}

    def complete(job_id: str, body: dict[str, Any], *, previous_hash: str | None) -> str:
        [claimed] = repository.poll_jobs(
            target_id=target_id,
            lease_id=lease.lease_id,
            token=lease.token,
            fencing=lease.fencing,
            now=datetime.now(timezone.utc),
            limit=1,
        )
        assert claimed.job_id == job_id
        postcondition: dict[str, Any] = (
            {"target_ref": target_id, "observation": body}
            if previous_hash is None
            else {
                **body,
                "pre_observation_hash": previous_hash,
                "post_observation_hash": canonical_action_hash(
                    {"target_id": target_id, "target_ref": target_id, "body": body}
                ),
            }
        )
        completed = controller.complete_job(
            RemoteExecutionResult(
                job_id=job_id,
                result_idempotency_key=f"synthetic-result:{job_id}",
                status=RemoteJobStatus.SUCCEEDED,
                postcondition=postcondition,
            ),
            target_id=target_id,
            lease_id=lease.lease_id,
            token=lease.token,
            fencing=lease.fencing,
            now=datetime.now(timezone.utc),
        )
        assert completed.status is RemoteJobStatus.SUCCEEDED
        if previous_hash is not None:
            with service.store._connect() as connection:
                receipt = connection.execute(
                    "SELECT status FROM capability_action_receipts WHERE action_hash=?",
                    (repository.get_job(job_id).action_hash,),
                ).fetchone()
            assert receipt is not None and receipt[0] == "succeeded"
        if previous_hash is None:
            projected = sdk.get_remote_target_job_result(job_id)["observation"]
            result_hash = projected["observation_hash"]
        else:
            projected = sdk.get_remote_target_job_result(job_id)["result"]
            projected_postcondition = projected["postcondition"]
            result_hash = projected_postcondition["post_observation_hash"]
            assert {
                key: value
                for key, value in projected_postcondition.items()
                if key not in {"pre_observation_hash", "post_observation_hash"}
            } == body
        observed = repository.get_observation(target_id, result_hash)
        assert observed.body == body
        source = repository.get_observation_source(observed.observation_id)
        assert source is not None
        assert source.source_job_id == job_id
        assert source.lease_id == lease.lease_id and source.lease_fencing == lease.fencing
        assert observed.expires_at - observed.created_at == timedelta(seconds=60)
        return result_hash

    def act(
        capability: str,
        operation: str,
        observation_hash: str,
        arguments: dict[str, Any],
        key: str,
        *,
        current_binding: dict[str, Any] | None = None,
        current_target: str | None = None,
    ) -> str:
        action_target = current_target or target_id
        response = sdk.act_browser(
            {
                **(current_binding or binding),
                "action": {
                    "target_id": action_target,
                    "capability": capability,
                    "operation": operation,
                    "target_ref": action_target,
                    "observation_hash": observation_hash,
                    "arguments": arguments,
                    "idempotency_key": key,
                    "idempotency": "non_idempotent",
                },
            },
            idempotency_key=key,
        )
        return response["job"]["job_id"]

    def page(form_hash: str, *, text: str = "Ready") -> dict[str, Any]:
        return {
            "url": origin + "/",
            "title": "Synthetic page",
            "text": text,
            "form_state_sha256": form_hash,
            "elements": [{"selector": "#apply", "tag": "button", "text": "Apply"}],
            "fields": [{"selector": "#marker", "tag": "input", "type": "text"}],
            "active_selector": None,
        }

    try:
        observed = sdk.observe_browser(
            target_id,
            {**binding, "target_ref": target_id, "idempotency_key": "initial-observe"},
            idempotency_key="initial-observe",
        )
        initial_hash = complete(
            observed["job_id"],
            {
                "url": "about:blank",
                "title": "",
                "text": "",
                "form_state_sha256": "0" * 64,
                "elements": [],
                "fields": [],
                "active_selector": None,
            },
            previous_hash=None,
        )
        nav_job = act(
            "browser.navigate", "navigate", initial_hash, {"url": origin}, "navigate-step"
        )
        navigated = page("1" * 64)
        navigate_hash = complete(nav_job, navigated, previous_hash=initial_hash)
        assert navigate_hash != canonical_action_hash(
            {"target_id": target_id, "target_ref": target_id, "body": navigated}
        )
        sealed = seal_browser_input(
            "synthetic marker",
            token=lease.token,
            target_id=target_id,
            lease_id=lease.lease_id,
            fencing=lease.fencing,
            observation_hash=navigate_hash,
            selector="#marker",
            idempotency_key="fill-step",
        )
        fill_job = act(
            "browser.submit",
            "fill",
            navigate_hash,
            {"selector": "#marker", "value_sealed": sealed},
            "fill-step",
        )
        filled = page("2" * 64)
        fill_hash = complete(fill_job, filled, previous_hash=navigate_hash)
        fill_expiry = repository.get_observation(target_id, fill_hash).expires_at
        click_job = act("browser.submit", "click", fill_hash, {"selector": "#apply"}, "click-step")
        click_hash = complete(click_job, filled, previous_hash=fill_hash)
        assert click_hash != fill_hash
        assert repository.get_observation(target_id, fill_hash).expires_at == fill_expiry
        click_observation = repository.get_observation(target_id, click_hash)
        replayed = controller.complete_job(
            RemoteExecutionResult(
                job_id=click_job,
                result_idempotency_key=f"synthetic-result:{click_job}",
                status=RemoteJobStatus.SUCCEEDED,
                postcondition={
                    **filled,
                    "pre_observation_hash": fill_hash,
                    "post_observation_hash": canonical_action_hash(
                        {"target_id": target_id, "target_ref": target_id, "body": filled}
                    ),
                },
            ),
            target_id=target_id,
            lease_id=lease.lease_id,
            token=lease.token,
            fencing=lease.fencing,
            now=datetime.now(timezone.utc),
        )
        assert replayed.postcondition["post_observation_hash"] == click_hash
        assert repository.get_observation(target_id, click_hash) == click_observation
        assert len({initial_hash, navigate_hash, fill_hash, click_hash}) == 4
        assert len(repository.list_jobs(target_id=target_id)) == 4

        renewed_at = datetime.now(timezone.utc)
        renewed = repository.renew_lease(
            target_id=target_id,
            lease_id=lease.lease_id,
            token=lease.token,
            fencing=lease.fencing,
            now=renewed_at,
            expires_at=renewed_at + timedelta(minutes=10),
        )
        assert renewed.lease_id == lease.lease_id and renewed.fencing == lease.fencing
        read_job = act("browser.screenshot", "capture_viewport", click_hash, {}, "renewed-read")
        [read_claim] = repository.poll_jobs(
            target_id=target_id,
            lease_id=lease.lease_id,
            token=lease.token,
            fencing=lease.fencing,
            now=datetime.now(timezone.utc),
            limit=1,
        )
        assert read_claim.job_id == read_job
        controller.complete_job(
            RemoteExecutionResult(
                job_id=read_job,
                result_idempotency_key=f"synthetic-result:{read_job}",
                status=RemoteJobStatus.SUCCEEDED,
                postcondition={},
            ),
            target_id=target_id,
            lease_id=lease.lease_id,
            token=lease.token,
            fencing=lease.fencing,
            now=datetime.now(timezone.utc),
        )
        assert repository.get_observation(target_id, click_hash).body == filled
        jobs_before_rejection = len(repository.list_jobs(target_id=target_id))
        with pytest.raises(Phase1EError) as missing:
            act("browser.submit", "click", "f" * 64, {"selector": "#apply"}, "missing-observe")
        assert missing.value.code == "observation_unavailable"

        with service.store._connect() as connection:
            connection.execute(
                "UPDATE capability_observations SET expires_at=? WHERE target_id=? "
                "AND observation_hash=?",
                (
                    (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                    target_id,
                    initial_hash,
                ),
            )
        with pytest.raises(Phase1EError) as expired:
            act("browser.submit", "click", initial_hash, {"selector": "#apply"}, "old-expired")
        assert expired.value.code == "observation_expired"

        gateway = app.state.phase45_action_gateway
        original_engine = gateway.engine
        gateway.engine = PolicyEngine(
            PolicyBundle(
                bundle_id="deny-post-observation",
                version="deny-post-observation.v1",
                default_decision=PolicyDecision.DENY,
                rules=(),
            )
        )
        try:
            with pytest.raises(Phase1EError) as denied:
                act("browser.submit", "click", click_hash, {"selector": "#apply"}, "denied")
            assert denied.value.code == "http_403"
        finally:
            gateway.engine = original_engine

        manager.transition(session.session_id, "human_control")
        manager.transition(session.session_id, "active")
        replacement = manager.get(session.session_id).lease
        assert replacement.lease_id != lease.lease_id
        with pytest.raises(Phase1EError) as old_lease:
            act(
                "browser.submit",
                "click",
                click_hash,
                {"selector": "#apply"},
                "other-lease",
                current_binding={
                    "lease_id": replacement.lease_id,
                    "token": replacement.token,
                    "fencing": replacement.fencing,
                },
            )
        assert old_lease.value.code == "observation_unavailable"

        manager.transition(session.session_id, "closed")
        other = client.post(
            "/v1/setup/local-control/sessions",
            json={"plugin_id": plugin.plugin_id},
            headers={"Idempotency-Key": "other-post-observation-session"},
        )
        assert other.status_code == 200
        other_session = manager.get(other.json()["session_id"])
        with pytest.raises(Phase1EError) as other_target:
            act(
                "browser.submit",
                "click",
                click_hash,
                {"selector": "#apply"},
                "other-target",
                current_binding={
                    "lease_id": other_session.lease.lease_id,
                    "token": other_session.lease.token,
                    "fencing": other_session.lease.fencing,
                },
                current_target=other_session.target_id,
            )
        assert other_target.value.code == "observation_unavailable"
        assert len(repository.list_jobs(target_id=target_id)) == jobs_before_rejection
        assert repository.list_jobs(target_id=other_session.target_id) == ()
    finally:
        service.close()


def _app(tmp_path: Path) -> tuple[Any, Any, Any, TestClient]:
    engine = PolicyEngine(
        PolicyBundle(
            bundle_id="test-local-conversation",
            version="test.v1",
            default_decision=PolicyDecision.DENY,
            rules=(
                PolicyRule(
                    rule_id="allow-test-setup",
                    layer=PolicyLayer.SYSTEM,
                    decision=PolicyDecision.ALLOW,
                    reason="isolated local-control test",
                ),
            ),
        )
    )
    app = create_app(
        tmp_path / "core.sqlite3", phase45_policy_engine=engine, phase45_skill_roots={}
    )
    service = app.state.operant_service
    profile = service.add_model_profile(
        ModelProfile(
            name="test model",
            model_id="test-model",
            base_url="https://example.test/v1",
            secret_ref="TEST_MODEL_KEY",
            effort_parameter=None,
        )
    )
    manager = app.state.local_control_manager
    manager.connector_factory = lambda _record, lease, _previous: _Connector(lease)
    app.state.local_control_origin = "http://127.0.0.1:8000"
    client = TestClient(app)
    bootstrapped = client.post("/v1/setup/bootstrap", json={"model_profile_id": profile.id})
    assert bootstrapped.status_code == 200, bootstrapped.text
    return app, service, manager, client


def test_expired_observation_returns_typed_pre_dispatch_error(tmp_path: Path) -> None:
    app, service, manager, client = _app(tmp_path)
    browser = manager.registry.install("operant.chrome.browser", ("https://example.test",))
    manager.registry.set_enabled(browser.plugin_id, True)
    opened = client.post(
        "/v1/setup/local-control/sessions",
        json={"plugin_id": browser.plugin_id},
        headers={"Idempotency-Key": "expired-browser-session"},
    )
    assert opened.status_code == 200
    selected = manager.get(opened.json()["session_id"])
    now = datetime.now(timezone.utc)
    app.state.remote_execution_repository.record_observation(
        CapabilityObservation(
            target_id=selected.target_id,
            capability=RemoteCapability.BROWSER_OBSERVE,
            target_ref=selected.target_id,
            observation_hash="a" * 64,
            body={"url": "https://example.test/start"},
            created_at=now - timedelta(minutes=2),
            expires_at=now - timedelta(minutes=1),
        )
    )
    try:
        response = client.post(
            "/v1/browser/act",
            json={
                "lease_id": selected.lease.lease_id,
                "token": selected.lease.token,
                "fencing": selected.lease.fencing,
                "action": {
                    "target_id": selected.target_id,
                    "capability": "browser.navigate",
                    "operation": "navigate",
                    "target_ref": selected.target_id,
                    "observation_hash": "a" * 64,
                    "arguments": {"url": "https://example.test/next"},
                    "idempotency_key": "expired-navigation",
                    "idempotency": "non_idempotent",
                },
            },
            headers={"Idempotency-Key": "expired-navigation"},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "observation_expired"
        assert response.json()["error"]["message"] == "页面或窗口信息已过期，请重新查看后再操作。"
        assert "https://example.test/next" not in response.text
        with service.store._connect() as connection:
            assert (
                connection.execute("SELECT COUNT(*) FROM remote_execution_jobs").fetchone()[0] == 0
            )
    finally:
        service.close()


@pytest.mark.parametrize(
    ("terminal", "reason_code", "expected_message"),
    (
        (RemoteJobStatus.FAILED, "computer.permission_required", "辅助功能权限"),
        (RemoteJobStatus.FAILED, "computer.target_rejected", "检查应用和权限"),
        (RemoteJobStatus.MANUAL_RECONCILE_REQUIRED, "plugin.outcome_unknown", None),
    ),
)
@pytest.mark.asyncio
async def test_computer_observe_failed_job_settles_only_known_terminal_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal: RemoteJobStatus,
    reason_code: str,
    expected_message: str | None,
) -> None:
    app, service, manager, client = _app(tmp_path)
    plugin = manager.registry.install("operant.macos.computer", ("dev.operant.testfixture",))
    manager.registry.set_enabled(plugin.plugin_id, True)
    opened = client.post(
        "/v1/setup/local-control/sessions",
        json={"plugin_id": plugin.plugin_id, "computer_bundle_id": "dev.operant.testfixture"},
        headers={"Idempotency-Key": "observe-failure-session"},
    )
    assert opened.status_code == 200, opened.text
    selected = manager.get(opened.json()["session_id"])
    created = client.post(
        "/v1/setup/conversations",
        json={"local_control_session_ids": [selected.session_id]},
        headers={"Idempotency-Key": "observe-failure-conversation"},
    )
    assert created.status_code == 200, created.text
    session_id = created.json()["session_id"]
    workspace = service.get_session(session_id).role_snapshot.config_workspace_ref
    assert workspace is not None
    repository = app.state.remote_execution_repository
    controller = app.state.remote_execution_controller

    def transport(request: TransportRequest) -> TransportResponse:
        path = urlsplit(request.url).path
        response = client.request(
            request.method, path, headers=dict(request.headers), content=request.body
        )
        if path.endswith("/observe") and response.status_code == 200:
            job_id = response.json()["job_id"]
            claimed = repository.poll_jobs(
                target_id=selected.target_id,
                lease_id=selected.lease.lease_id,
                token=selected.lease.token,
                fencing=selected.lease.fencing,
                now=datetime.now(timezone.utc),
                limit=1,
            )
            assert len(claimed) == 1 and claimed[0].job_id == job_id
            controller.complete_job(
                RemoteExecutionResult(
                    job_id=job_id,
                    result_idempotency_key=f"synthetic-result:{job_id}",
                    status=terminal,
                    error_code=reason_code,
                ),
                target_id=selected.target_id,
                lease_id=selected.lease.lease_id,
                token=selected.lease.token,
                fencing=selected.lease.fencing,
                now=datetime.now(timezone.utc),
            )
        return TransportResponse(
            status=response.status_code, headers=dict(response.headers), body=response.content
        )

    sdk = Phase56Client("http://127.0.0.1:8000", transport=transport)
    monkeypatch.setattr(local_tool_extensions, "_client", lambda _origin: sdk)
    service.provider.delegate = _ObserveProvider(tool_name="ext_computer_observe")

    async def consume() -> list[Any]:
        return [
            event
            async for event in service.run_session(
                session_id, user_message="View the test window", workspace=workspace
            )
        ]

    try:
        running = asyncio.create_task(consume())
        pending: list[dict[str, Any]] = []
        for _ in range(100):
            pending = service.list_pending_approvals(session_id)
            if pending:
                break
            await asyncio.sleep(0.01)
        assert len(pending) == 1
        service.decide_approval(session_id, pending[0]["tool_call_id"], approved=True)
        events = await asyncio.wait_for(running, timeout=10)
        jobs = repository.list_jobs(target_id=selected.target_id)
        assert len(jobs) == 1 and jobs[0].operation == "observe_computer"
        approval = service.store.list_approval_requests(session_id, status=None)[0]
        receipt = service.store.get_tool_action_receipt(approval.tool_action_receipt_id)
        if expected_message is None:
            assert events[-1].event_type == "agent.failed"
            assert events[-1].payload == {"error_type": "CapabilityOutcomeUnknownError"}
            frame = _sse_event(
                events[-1].event_type,
                events[-1].model_dump(mode="json"),
                cursor=events[-1].cursor,
            )
            assert '"error_type": "CapabilityOutcomeUnknownError"' in frame
            history = client.get(f"/v1/sessions/{session_id}/events")
            assert history.status_code == 200
            assert history.json()[-1]["payload"]["error_type"] == "CapabilityOutcomeUnknownError"
            assert history.json()[-1]["payload"]["turn"] == 0
            assert [event.event_type for event in events].count("tool.failed") == 0
            assert receipt.status.value == "in_progress"
        else:
            assert events[-1].event_type == "agent.completed"
            assert [event.event_type for event in events].count("tool.failed") == 1
            assert receipt.status.value == "failed"
            assert receipt.error_code == "ToolError"
            assert receipt.result_json is not None
            assert expected_message in receipt.result_json
            assert reason_code not in receipt.result_json
        assert [event.event_type for event in events].count("tool.completed") == 0
    finally:
        service.close()


@pytest.mark.parametrize("status", ("failed", "manual_reconcile_required", "timeout"))
@pytest.mark.asyncio
async def test_computer_input_failure_never_becomes_observation_tool_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    app, service, manager, client = _app(tmp_path)
    plugin = manager.registry.install("operant.macos.computer", ("dev.operant.testfixture",))
    manager.registry.set_enabled(plugin.plugin_id, True)
    opened = client.post(
        "/v1/setup/local-control/sessions",
        json={"plugin_id": plugin.plugin_id, "computer_bundle_id": "dev.operant.testfixture"},
        headers={"Idempotency-Key": "input-failure-session"},
    )
    assert opened.status_code == 200
    created = client.post(
        "/v1/setup/conversations",
        json={"local_control_session_ids": [opened.json()["session_id"]]},
        headers={"Idempotency-Key": "input-failure-conversation"},
    )
    assert created.status_code == 200
    session_id = created.json()["session_id"]
    workspace = service.get_session(session_id).role_snapshot.config_workspace_ref
    assert workspace is not None
    original = local_control_api.local_capability_tool_extensions

    def extensions(path: Path, policy: ToolPolicy, **kwargs: Any) -> dict[str, Any]:
        result = original(path, policy, **kwargs)
        extension = result.get("ext_computer_type_text")
        if extension is not None:

            async def failed(_arguments: dict[str, Any]) -> dict[str, Any]:
                raise CapabilityOperationError("computer.target_rejected", "test-job", status)

            result["ext_computer_type_text"] = replace(extension, execute=failed)
        return result

    monkeypatch.setattr(local_control_api, "local_capability_tool_extensions", extensions)
    service.provider.delegate = _ObserveProvider(
        tool_name="ext_computer_type_text",
        arguments_json=json.dumps(
            {
                "element_name": "Verification marker",
                "value": "synthetic marker",
                "observation_hash": "a" * 64,
                "idempotency_key": "model-key",
            }
        ),
    )

    async def consume() -> list[Any]:
        return [
            event
            async for event in service.run_session(
                session_id, user_message="Enter a test marker", workspace=workspace
            )
        ]

    try:
        running = asyncio.create_task(consume())
        pending: list[dict[str, Any]] = []
        for _ in range(100):
            pending = service.list_pending_approvals(session_id)
            if pending:
                break
            await asyncio.sleep(0.01)
        assert len(pending) == 1
        service.decide_approval(session_id, pending[0]["tool_call_id"], approved=True)
        events = await asyncio.wait_for(running, timeout=10)
        assert events[-1].event_type == "agent.failed"
        assert [event.event_type for event in events].count("tool.failed") == 0
        approval = service.store.list_approval_requests(session_id, status=None)[0]
        receipt = service.store.get_tool_action_receipt(approval.tool_action_receipt_id)
        assert receipt.status.value == "in_progress"
    finally:
        service.close()


@pytest.mark.parametrize("kind", ("browser", "computer"))
@pytest.mark.parametrize("mode", ("approved", "deny", "changed_scope", "expired"))
@pytest.mark.asyncio
async def test_sealed_local_input_continues_exact_phase56_ask_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, mode: str
) -> None:
    app, service, manager, client = _app(tmp_path)
    plugin_id = "operant.chrome.browser" if kind == "browser" else "operant.macos.computer"
    target = "https://example.test" if kind == "browser" else "dev.operant.testfixture"
    plugin = manager.registry.install(plugin_id, (target,))
    manager.registry.set_enabled(plugin.plugin_id, True)
    opened = client.post(
        "/v1/setup/local-control/sessions",
        json={
            "plugin_id": plugin.plugin_id,
            **({"computer_bundle_id": target} if kind == "computer" else {}),
        },
        headers={"Idempotency-Key": f"sealed-{kind}-session"},
    )
    assert opened.status_code == 200
    selected = manager.get(opened.json()["session_id"])
    created = client.post(
        "/v1/setup/conversations",
        json={"local_control_session_ids": [selected.session_id]},
        headers={"Idempotency-Key": f"sealed-{kind}-conversation"},
    )
    assert created.status_code == 200
    session_id = created.json()["session_id"]
    workspace = service.get_session(session_id).role_snapshot.config_workspace_ref
    assert workspace is not None
    repository = app.state.remote_execution_repository
    controller = app.state.remote_execution_controller
    now = datetime.now(timezone.utc)
    observation_body = {"url": target} if kind == "browser" else {"bundle_id": target}
    observation_job = controller.observe(
        kind=kind,
        target_id=selected.target_id,
        lease_id=selected.lease.lease_id,
        lease_token=selected.lease.token,
        lease_fencing=selected.lease.fencing,
        target_ref=selected.target_id,
        idempotency_key=f"sealed-{kind}-initial-observe",
        now=now,
    )
    [claimed_observation] = repository.poll_jobs(
        target_id=selected.target_id,
        lease_id=selected.lease.lease_id,
        token=selected.lease.token,
        fencing=selected.lease.fencing,
        now=now,
        limit=1,
    )
    assert claimed_observation.job_id == observation_job.job_id
    controller.complete_job(
        RemoteExecutionResult(
            job_id=observation_job.job_id,
            result_idempotency_key=f"sealed-{kind}-initial-result",
            status=RemoteJobStatus.SUCCEEDED,
            postcondition={"target_ref": selected.target_id, "observation": observation_body},
        ),
        target_id=selected.target_id,
        lease_id=selected.lease.lease_id,
        token=selected.lease.token,
        fencing=selected.lease.fencing,
        now=now,
    )
    observation_hash = observation_hash_for_job(
        observation_job, target_ref=selected.target_id, body=observation_body
    )
    if mode == "expired":
        with service.store._connect() as connection:
            connection.execute(
                "UPDATE capability_observations SET expires_at=? WHERE target_id=? "
                "AND observation_hash=?",
                ((now - timedelta(seconds=1)).isoformat(), selected.target_id, observation_hash),
            )
    gateway = app.state.phase45_action_gateway
    gateway.engine = PolicyEngine(
        PolicyBundle(
            bundle_id=f"ask-sealed-{kind}",
            version=f"ask-sealed-{kind}.v1",
            default_decision=PolicyDecision.ASK,
            rules=(
                (
                    PolicyRule(
                        rule_id="deny-local-input",
                        layer=PolicyLayer.SYSTEM,
                        decision=PolicyDecision.DENY,
                        tools=("remote_target_job",),
                        operations=("fill" if kind == "browser" else "type_text",),
                        reason="isolated hard deny",
                        hard=True,
                    ),
                )
                if mode == "deny"
                else ()
            ),
        )
    )
    action_path = f"/v1/{kind}/act"
    jobs_completed: list[str] = []
    scope_changed = False

    def transport(request: TransportRequest) -> TransportResponse:
        nonlocal scope_changed
        path = urlsplit(request.url).path
        response = client.request(
            request.method, path, headers=dict(request.headers), content=request.body
        )
        if (
            mode == "changed_scope"
            and not scope_changed
            and path == action_path
            and response.status_code == 409
            and isinstance(response.json().get("detail"), dict)
            and response.json()["detail"].get("code") == "approval_required"
        ):
            manager.transition(selected.session_id, "human_control")
            scope_changed = True
        if path == action_path and response.status_code == 200:
            job_id = response.json()["job"]["job_id"]
            claimed = repository.poll_jobs(
                target_id=selected.target_id,
                lease_id=selected.lease.lease_id,
                token=selected.lease.token,
                fencing=selected.lease.fencing,
                now=datetime.now(timezone.utc),
                limit=1,
            )
            assert len(claimed) == 1 and claimed[0].job_id == job_id
            controller.complete_job(
                RemoteExecutionResult(
                    job_id=job_id,
                    result_idempotency_key=f"synthetic-result:{job_id}",
                    status=RemoteJobStatus.SUCCEEDED,
                    postcondition={},
                ),
                target_id=selected.target_id,
                lease_id=selected.lease.lease_id,
                token=selected.lease.token,
                fencing=selected.lease.fencing,
                now=datetime.now(timezone.utc),
            )
            jobs_completed.append(job_id)
        return TransportResponse(
            status=response.status_code, headers=dict(response.headers), body=response.content
        )

    sdk = Phase56Client("http://127.0.0.1:8000", transport=transport)
    monkeypatch.setattr(local_tool_extensions, "_client", lambda _origin: sdk)
    tool_name = "ext_browser_fill" if kind == "browser" else "ext_computer_type_text"
    tool_args = (
        {
            "selector": "#marker",
            "value": "synthetic marker",
            "observation_hash": observation_hash,
            "idempotency_key": "model-key",
        }
        if kind == "browser"
        else {
            "element_name": "Verification marker",
            "value": "synthetic marker",
            "observation_hash": observation_hash,
            "idempotency_key": "model-key",
        }
    )
    service.provider.delegate = _ObserveProvider(
        tool_name=tool_name, arguments_json=json.dumps(tool_args)
    )

    async def consume() -> list[Any]:
        return [
            event
            async for event in service.run_session(
                session_id, user_message="Enter a test marker", workspace=workspace
            )
        ]

    try:
        running = asyncio.create_task(consume())
        pending: list[dict[str, Any]] = []
        for _ in range(100):
            pending = service.list_pending_approvals(session_id)
            if pending:
                break
            await asyncio.sleep(0.01)
        assert len(pending) == 1
        service.decide_approval(session_id, pending[0]["tool_call_id"], approved=True)
        events = await asyncio.wait_for(running, timeout=10)
        jobs = tuple(
            job
            for job in repository.list_jobs(target_id=selected.target_id)
            if job.job_id != observation_job.job_id
        )
        if mode == "approved":
            assert events[-1].event_type == "agent.completed"
            assert [event.event_type for event in events].count("tool.completed") == 1
            assert len(jobs_completed) == len(jobs) == 1
            assert jobs[0].operation == ("fill" if kind == "browser" else "type_text")
        elif mode == "expired":
            assert events[-1].event_type == "agent.completed"
            assert [event.event_type for event in events].count("tool.failed") == 1
            assert jobs == () and jobs_completed == []
            approval = service.store.list_approval_requests(session_id, status=None)[0]
            receipt = service.store.get_tool_action_receipt(approval.tool_action_receipt_id)
            assert receipt.status.value == "failed" and receipt.error_code == "ToolError"
        else:
            assert events[-1].event_type == "agent.failed"
            assert jobs == () and jobs_completed == []
            if mode == "changed_scope":
                assert scope_changed
        with service.store._connect() as connection:
            inner = connection.execute(
                "SELECT status,action_hash FROM phase45_approval_requests "
                "WHERE reason_code LIKE 'session_approval:%'"
            ).fetchall()
            assert [row[0] for row in inner] == (["consumed"] if mode == "approved" else [])
            if mode == "approved":
                assert inner[0][1] == jobs[0].action_hash
            persisted = connection.execute(
                "SELECT body FROM security_action_requests WHERE operation=?",
                ("fill" if kind == "browser" else "type_text",),
            ).fetchone()
            assert persisted is None or "synthetic marker" not in persisted[0]
    finally:
        service.close()


def test_new_conversation_binds_only_selected_sessions_and_survives_same_session_resume(
    tmp_path: Path,
) -> None:
    _app_state, service, manager, client = _app(tmp_path)
    browser = manager.registry.install(
        "operant.chrome.browser", ("https://first.example", "https://second.example")
    )
    manager.registry.set_enabled(browser.plugin_id, True)
    computer = manager.registry.install("operant.macos.computer", ("com.apple.TextEdit",))
    manager.registry.set_enabled(computer.plugin_id, True)
    browser_open = client.post(
        "/v1/setup/local-control/sessions",
        json={"plugin_id": browser.plugin_id},
        headers={"Idempotency-Key": "browser-session"},
    )
    computer_open = client.post(
        "/v1/setup/local-control/sessions",
        json={"plugin_id": computer.plugin_id, "computer_bundle_id": "com.apple.TextEdit"},
        headers={"Idempotency-Key": "computer-session"},
    )
    assert browser_open.status_code == computer_open.status_code == 200
    assert browser_open.headers["Idempotency-Key"] == "browser-session"
    assert browser_open.json()["allowed_targets"] == list(
        manager.registry.get(browser.plugin_id).allowed_targets
    )
    browser_session = manager.get(browser_open.json()["session_id"])
    computer_session = manager.get(computer_open.json()["session_id"])
    assert manager.bindings() == {}
    listed = client.get("/v1/setup/local-control/sessions")
    assert listed.status_code == 200
    assert {item["session_id"] for item in listed.json()["items"]} == {
        browser_session.session_id,
        computer_session.session_id,
    }
    by_request = client.get(
        "/v1/setup/local-control/sessions", params={"request_id": "browser-session"}
    )
    assert by_request.status_code == 200
    assert by_request.json()["items"] == [browser_open.json()]
    try:
        created = client.post(
            "/v1/setup/conversations",
            json={
                "local_control_session_ids": [
                    browser_session.session_id,
                    computer_session.session_id,
                ]
            },
            headers={"Idempotency-Key": "bound-conversation"},
        )
        assert created.status_code == 200, created.text
        snapshot = service.get_session(created.json()["session_id"]).role_snapshot
        bindings = snapshot_control_bindings(snapshot.config_sources)
        assert {item.session_id for item in bindings} == {
            browser_session.session_id,
            computer_session.session_id,
        }
        assert all(item.scope_digest for item in bindings)
        serialized = json.dumps(snapshot.model_dump(mode="json"))
        assert browser_session.lease.token not in serialized
        assert computer_session.lease.token not in serialized
        allowed = set(snapshot.tool_policy.allowed_tools)
        assert {"ext_browser_observe", "ext_browser_navigate", "ext_computer_type_text"} <= allowed
        assert "ext_computer_read_clipboard" not in allowed
        assert "ext_computer_write_clipboard" not in allowed
        factory = service.tool_extension_factory
        assert factory is not None
        extensions = factory.for_snapshot(service.store.path, snapshot)
        assert {"ext_browser_navigate", "ext_computer_type_text"} <= set(extensions)
        assert "ext_computer_read_clipboard" not in extensions
        old_lease = browser_session.lease.lease_id
        manager.transition(browser_session.session_id, "human_control")
        with pytest.raises(PermissionError):
            factory.for_snapshot(service.store.path, snapshot)
        manager.transition(browser_session.session_id, "active")
        assert browser_session.lease.lease_id != old_lease
        assert "ext_browser_navigate" in factory.for_snapshot(service.store.path, snapshot)

        manager.transition(browser_session.session_id, "closed")
        closed = client.get(
            "/v1/setup/local-control/sessions", params={"request_id": "browser-session"}
        )
        assert closed.status_code == 200
        assert closed.json()["items"][0]["state"] == "closed"
        closed_replay = client.post(
            "/v1/setup/local-control/sessions",
            json={"plugin_id": browser.plugin_id},
            headers={"Idempotency-Key": "browser-session"},
        )
        assert closed_replay.status_code == 200
        assert closed_replay.json()["state"] == "closed"
        replacement_open = client.post(
            "/v1/setup/local-control/sessions",
            json={"plugin_id": browser.plugin_id},
            headers={"Idempotency-Key": "replacement-browser"},
        )
        assert replacement_open.status_code == 200
        replacement = manager.get(replacement_open.json()["session_id"])
        assert replacement.target_id != browser_session.target_id
        with pytest.raises(PermissionError):
            factory.for_snapshot(service.store.path, snapshot)
        replay = client.post(
            "/v1/setup/conversations",
            json={
                "local_control_session_ids": [
                    browser_session.session_id,
                    computer_session.session_id,
                ]
            },
            headers={"Idempotency-Key": "bound-conversation"},
        )
        assert replay.status_code == 200
        assert replay.json() == created.json()
        changed = client.post(
            "/v1/setup/conversations",
            json={
                "thread_id": created.json()["thread_id"],
                "local_control_session_ids": [replacement.session_id],
            },
            headers={"Idempotency-Key": "replace-bound-session"},
        )
        assert changed.status_code == 409
    finally:
        service.close()


def test_legacy_explicit_role_retains_only_legacy_control_binding(tmp_path: Path) -> None:
    _app_state, service, manager, client = _app(tmp_path)
    browser = manager.registry.install("operant.chrome.browser", ("https://legacy.example",))
    manager.registry.set_enabled(browser.plugin_id, True)
    session = manager.open(browser.plugin_id, "legacy-open")
    try:
        assert not session.conversation_only
        assert manager.bindings()["browser"].target_id == session.target_id
        profile_id = service.list_model_profiles()[0].id
        role = service.create_role(
            RolePreset(
                id="role_explicit_local_extension",
                name="Explicit local tool",
                system_prompt="Use only the explicitly configured tool.",
                model_profile_id=profile_id,
                tool_policy=ToolPolicy(allowed_tools=("ext_browser_observe",)),
            )
        )
        snapshot = service.create_session(role.id).role_snapshot
        assert snapshot_control_bindings(snapshot.config_sources) == ()
        factory = service.tool_extension_factory
        assert factory is not None
        assert "ext_browser_observe" in factory(service.store.path, snapshot.tool_policy)
        cannot_bind = client.post(
            "/v1/setup/conversations",
            json={"local_control_session_ids": [session.session_id]},
            headers={"Idempotency-Key": "manual-session-is-not-conversation-only"},
        )
        assert cannot_bind.status_code == 422
    finally:
        service.close()


def test_conversation_control_receipt_reports_closed_after_core_restart(tmp_path: Path) -> None:
    _app_state, service, manager, client = _app(tmp_path)
    record = manager.registry.install("operant.chrome.browser", ("https://example.test",))
    manager.registry.set_enabled(record.plugin_id, True)
    created = client.post(
        "/v1/setup/local-control/sessions",
        json={"plugin_id": record.plugin_id},
        headers={"Idempotency-Key": "before-core-restart"},
    )
    assert created.status_code == 200, created.text
    service.close()

    reopened = create_app(tmp_path / "core.sqlite3", phase45_skill_roots={})
    try:
        recovered = TestClient(reopened).get(
            "/v1/setup/local-control/sessions",
            params={"request_id": "before-core-restart"},
        )
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["items"][0]["session_id"] == created.json()["session_id"]
        assert recovered.json()["items"][0]["state"] == "closed"
        repeated = TestClient(reopened).post(
            "/v1/setup/local-control/sessions",
            json={"plugin_id": record.plugin_id},
            headers={"Idempotency-Key": "before-core-restart"},
        )
        assert repeated.status_code == 200
        assert repeated.json()["state"] == "closed"
        assert reopened.state.local_control_manager.bindings() == {}
    finally:
        reopened.state.operant_service.close()


def test_conversation_control_receipts_fail_closed_on_unknown_or_changed_identity(
    tmp_path: Path,
) -> None:
    _app_state, service, manager, client = _app(tmp_path)
    missing = client.get(
        "/v1/setup/local-control/sessions", params={"request_id": "missing-request"}
    )
    assert missing.status_code == 409
    assert missing.json()["error"]["code"] == "command_outcome_unknown"
    assert missing.json()["error"]["recovery"] == "manual_reconcile"

    record = manager.registry.install("operant.chrome.browser", ("https://example.test",))
    manager.registry.set_enabled(record.plugin_id, True)
    created = client.post(
        "/v1/setup/local-control/sessions",
        json={"plugin_id": record.plugin_id},
        headers={"Idempotency-Key": "receipt-identity"},
    )
    assert created.status_code == 200, created.text
    try:
        with service.store._connect() as connection:
            row = connection.execute(
                "SELECT id,response_json FROM command_executions "
                "WHERE command_type='local-control.management' AND idempotency_key=?",
                ("receipt-identity",),
            ).fetchone()
            assert row is not None
            body = json.loads(row["response_json"])
            other_id = "local-control-" + hashlib.sha256(b"different-request").hexdigest()[:24]
            body["session"]["session_id"] = other_id
            body["session"]["target_id"] = other_id
            connection.execute(
                "UPDATE command_executions SET response_json=? WHERE id=?",
                (json.dumps(body), row["id"]),
            )
        with service.store._connect() as connection:
            altered = connection.execute(
                "SELECT response_json FROM command_executions WHERE id=?", (row["id"],)
            ).fetchone()
        assert json.loads(altered["response_json"])["session"]["session_id"] == other_id
        tampered = client.get(
            "/v1/setup/local-control/sessions", params={"request_id": "receipt-identity"}
        )
        assert tampered.status_code == 409, tampered.json()
        replay = client.post(
            "/v1/setup/local-control/sessions",
            json={"plugin_id": record.plugin_id},
            headers={"Idempotency-Key": "receipt-identity"},
        )
        assert replay.status_code == 409, replay.json()
        with service.store._connect() as connection:
            connection.execute(
                "UPDATE command_executions SET response_json='{' WHERE id=?", (row["id"],)
            )
        unreadable = client.get(
            "/v1/setup/local-control/sessions", params={"request_id": "receipt-identity"}
        )
        assert unreadable.status_code == 409
        with service.store._connect() as connection:
            connection.execute(
                "UPDATE command_executions SET status='manual_reconcile_required',"
                "response_json=NULL WHERE id=?",
                (row["id"],),
            )
        unresolved = client.get(
            "/v1/setup/local-control/sessions", params={"request_id": "receipt-identity"}
        )
        assert unresolved.status_code == 409
        assert unresolved.json()["error"]["code"] == "command_outcome_unknown"
        assert unresolved.json()["error"]["recovery"] == "manual_reconcile"
    finally:
        service.close()


def test_conversation_control_open_obeys_ask_and_hard_deny(tmp_path: Path) -> None:
    app, service, manager, client = _app(tmp_path)
    record = manager.registry.install("operant.chrome.browser", ("https://example.test",))
    manager.registry.set_enabled(record.plugin_id, True)
    try:
        app.state.phase45_action_gateway.engine = PolicyEngine(
            PolicyBundle(
                bundle_id="ask-local",
                version="ask.v1",
                default_decision=PolicyDecision.ASK,
                rules=(),
            )
        )
        asked = client.post(
            "/v1/setup/local-control/sessions",
            json={"plugin_id": record.plugin_id},
            headers={"Idempotency-Key": "approval-needed"},
        )
        assert asked.status_code == 409
        assert asked.json()["error"]["code"] == "approval_required"
        approval_id = asked.json()["detail"]["approval_id"]
        approved = client.post(
            f"/v1/security/approvals/{approval_id}",
            json={"approved": True, "reason_code": "isolated test approval"},
        )
        assert approved.status_code == 200, approved.text
        retried = client.post(
            "/v1/setup/local-control/sessions",
            json={"plugin_id": record.plugin_id},
            headers={"Idempotency-Key": "approval-needed"},
        )
        assert retried.status_code == 200, retried.text
        assert manager.get(retried.json()["session_id"]).conversation_only
        app.state.phase45_action_gateway.engine = PolicyEngine(
            PolicyBundle(
                bundle_id="deny-local",
                version="deny.v1",
                default_decision=PolicyDecision.ALLOW,
                rules=(
                    PolicyRule(
                        rule_id="deny-conversation-open",
                        layer=PolicyLayer.SYSTEM,
                        decision=PolicyDecision.DENY,
                        tools=("local_control",),
                        operations=("conversation_open",),
                        capabilities=(Capability.REMOTE_TARGET_EXEC,),
                        reason="conversation control is disabled",
                        hard=True,
                    ),
                ),
            )
        )
        denied = client.post(
            "/v1/setup/local-control/sessions",
            json={"plugin_id": record.plugin_id},
            headers={"Idempotency-Key": "hard-denied"},
        )
        assert denied.status_code == 403
        assert set(manager.sessions) == {retried.json()["session_id"]}
    finally:
        service.close()


def test_bound_conversation_rejects_stale_persistent_lease(
    tmp_path: Path,
) -> None:
    _app_state, service, manager, client = _app(tmp_path)
    browser = manager.registry.install("operant.chrome.browser", ("https://first.example",))
    manager.registry.set_enabled(browser.plugin_id, True)
    opened = client.post(
        "/v1/setup/local-control/sessions",
        json={"plugin_id": browser.plugin_id},
        headers={"Idempotency-Key": "scope-session"},
    )
    assert opened.status_code == 200
    session = manager.get(opened.json()["session_id"])
    try:
        response = client.post(
            "/v1/setup/conversations",
            json={"local_control_session_ids": [session.session_id]},
            headers={"Idempotency-Key": "scope-bound"},
        )
        assert response.status_code == 200, response.text
        snapshot = service.get_session(response.json()["session_id"]).role_snapshot
        factory = service.tool_extension_factory
        assert factory is not None
        assert "ext_browser_observe" in factory.for_snapshot(service.store.path, snapshot)

        lease = session.lease
        manager.repository.release_lease(
            target_id=lease.target_id,
            lease_id=lease.lease_id,
            token=lease.token,
            fencing=lease.fencing,
            now=datetime.now(timezone.utc),
        )
        with pytest.raises(PermissionError):
            factory.for_snapshot(service.store.path, snapshot)
    finally:
        service.close()


def test_bound_conversation_rejects_changed_authorized_scope(tmp_path: Path) -> None:
    _app_state, service, manager, client = _app(tmp_path)
    browser = manager.registry.install("operant.chrome.browser", ("https://first.example",))
    manager.registry.set_enabled(browser.plugin_id, True)
    opened = client.post(
        "/v1/setup/local-control/sessions",
        json={"plugin_id": browser.plugin_id},
        headers={"Idempotency-Key": "original-scope"},
    )
    assert opened.status_code == 200
    session = manager.get(opened.json()["session_id"])
    try:
        response = client.post(
            "/v1/setup/conversations",
            json={"local_control_session_ids": [session.session_id]},
            headers={"Idempotency-Key": "scope-change"},
        )
        assert response.status_code == 200, response.text
        snapshot = service.get_session(response.json()["session_id"]).role_snapshot
        factory = service.tool_extension_factory
        assert factory is not None
        assert "ext_browser_observe" in factory.for_snapshot(service.store.path, snapshot)
        manager.registry.set_enabled(browser.plugin_id, False)
        with pytest.raises(PermissionError):
            factory.for_snapshot(service.store.path, snapshot)
        manager.registry.install(browser.plugin_id, ("https://different.example",))
        manager.registry.set_enabled(browser.plugin_id, True)
        with pytest.raises(PermissionError):
            factory.for_snapshot(service.store.path, snapshot)
    finally:
        service.close()


def test_retained_local_tool_rechecks_same_session_after_takeover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _app_state, service, manager, client = _app(tmp_path)
    record = manager.registry.install("operant.chrome.browser", ("https://example.test",))
    manager.registry.set_enabled(record.plugin_id, True)
    opened = client.post(
        "/v1/setup/local-control/sessions",
        json={"plugin_id": record.plugin_id},
        headers={"Idempotency-Key": "retained-tool-session"},
    )
    assert opened.status_code == 200, opened.text
    selected = manager.get(opened.json()["session_id"])
    created = client.post(
        "/v1/setup/conversations",
        json={"local_control_session_ids": [selected.session_id]},
        headers={"Idempotency-Key": "retained-tool-conversation"},
    )
    assert created.status_code == 200, created.text
    snapshot = service.get_session(created.json()["session_id"]).role_snapshot
    real_factory = local_control_api.local_capability_tool_extensions
    calls: list[str] = []

    def fake_extensions(path: Any, policy: Any, **kwargs: Any) -> Any:
        extensions = real_factory(path, policy, **kwargs)
        binding = (kwargs.get("bindings") or {}).get("browser")
        if binding is None:
            return extensions
        for name, extension in tuple(extensions.items()):
            if not name.startswith("ext_browser_"):
                continue

            async def execute(
                _arguments: dict[str, Any],
                *,
                lease_id: str = binding.lease_id,
                tool_name: str = name,
            ) -> dict[str, Any]:
                calls.append(lease_id)
                if tool_name == "ext_browser_click":
                    raise RuntimeError("outcome unknown; do not replay")
                return {"lease_id": lease_id}

            extensions[name] = replace(extension, execute=execute)
        return extensions

    monkeypatch.setattr(local_control_api, "local_capability_tool_extensions", fake_extensions)
    try:
        factory = service.tool_extension_factory
        assert factory is not None
        retained = factory.for_snapshot(service.store.path, snapshot)
        observe = retained["ext_browser_observe"]
        click = retained["ext_browser_click"]
        original_lease_id = selected.lease.lease_id
        assert asyncio.run(observe.execute({})) == {"lease_id": original_lease_id}

        manager.transition(selected.session_id, "human_control")
        with pytest.raises(PermissionError):
            asyncio.run(observe.execute({}))
        assert calls == [original_lease_id]

        manager.transition(selected.session_id, "active")
        resumed_lease_id = selected.lease.lease_id
        assert resumed_lease_id != original_lease_id
        assert asyncio.run(observe.execute({})) == {"lease_id": resumed_lease_id}
        with pytest.raises(RuntimeError, match="outcome unknown"):
            asyncio.run(click.execute({}))
        assert calls == [original_lease_id, resumed_lease_id, resumed_lease_id]

        manager.transition(selected.session_id, "closed")
        replacement = client.post(
            "/v1/setup/local-control/sessions",
            json={"plugin_id": record.plugin_id},
            headers={"Idempotency-Key": "other-tool-session"},
        )
        assert replacement.status_code == 200
        with pytest.raises(PermissionError):
            asyncio.run(observe.execute({}))
        assert calls == [original_lease_id, resumed_lease_id, resumed_lease_id]
    finally:
        service.close()


@pytest.mark.parametrize(
    "mode",
    (
        "approved",
        "two_calls",
        "denied",
        "cancelled",
        "wrong_key",
        "wrong_inner_arguments",
        "navigate",
        "navigate_observation_expired",
        "navigate_observation_unavailable",
        "navigate_expired_after_nested_ask",
        "navigate_wrong_url",
        "hard_deny",
        "expired",
        "takeover",
        "unknown",
    ),
)
@pytest.mark.asyncio
async def test_bound_observation_uses_session_approval_then_exact_phase45_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    app, service, manager, client = _app(tmp_path)
    browser = manager.registry.install("operant.chrome.browser", ("https://example.test",))
    manager.registry.set_enabled(browser.plugin_id, True)
    opened = client.post(
        "/v1/setup/local-control/sessions",
        json={"plugin_id": browser.plugin_id},
        headers={"Idempotency-Key": "approval-browser-session"},
    )
    assert opened.status_code == 200, opened.text
    created = client.post(
        "/v1/setup/conversations",
        json={"local_control_session_ids": [opened.json()["session_id"]]},
        headers={"Idempotency-Key": "approval-conversation"},
    )
    assert created.status_code == 200, created.text
    session_id = created.json()["session_id"]
    workspace = service.get_session(session_id).role_snapshot.config_workspace_ref
    assert workspace is not None
    gateway = app.state.phase45_action_gateway
    assert app.state.remote_execution_controller.authorization.gateway is gateway
    gateway.engine = PolicyEngine(
        PolicyBundle(
            bundle_id="ask-bound-observation",
            version="ask-observe.v1",
            default_decision=PolicyDecision.ASK,
            rules=(),
        )
    )
    original = local_control_api.local_capability_tool_extensions
    action_mode = mode.startswith("navigate")
    tool_name = "ext_browser_navigate" if action_mode else "ext_browser_observe"
    tool_arguments = (
        {
            "url": "https://example.test/page",
            "observation_hash": "a" * 64,
            "idempotency_key": "model-supplied-key",
        }
        if action_mode
        else {}
    )
    keys: list[str] = []
    nested_ids: list[str] = []

    def extensions(path: Path, policy: ToolPolicy, **kwargs: Any) -> dict[str, Any]:
        result = original(path, policy, **kwargs)
        bindings = kwargs.get("bindings")
        if not isinstance(bindings, dict) or "browser" not in bindings:
            return result
        binding = bindings["browser"]
        extension = result[tool_name]

        async def observe(arguments: dict[str, Any]) -> dict[str, Any]:
            key = (
                arguments.get("idempotency_key")
                if action_mode
                else local_operator._OBSERVATION_KEY.get()
            )
            assert isinstance(key, str)
            if action_mode:
                assert key != "model-supplied-key"
            else:
                assert arguments == {}
            keys.append(key)
            if mode == "navigate_observation_expired":
                raise Phase1EError(
                    "observation_expired",
                    "Observation expired; observe again before acting",
                )
            if mode == "navigate_observation_unavailable":
                raise Phase1EError(
                    "observation_unavailable",
                    "Observation unavailable; observe again before acting",
                )
            if mode == "navigate_expired_after_nested_ask" and len(keys) == 2:
                raise Phase1EError(
                    "observation_expired",
                    "Observation expired; observe again before acting",
                )
            if mode == "hard_deny":
                gateway.engine = PolicyEngine(
                    PolicyBundle(
                        bundle_id="deny-bound-observation",
                        version="deny-observe.v1",
                        default_decision=PolicyDecision.DENY,
                        rules=(),
                    )
                )
            action, decision, _ = gateway.guard(
                tool="remote_target_job",
                operation="navigate" if action_mode else "observe_browser",
                target_id=binding.target_id,
                arguments={
                    "target_id": binding.target_id,
                    "lease_id": binding.lease_id,
                    "lease_fencing": binding.fencing,
                    "capability": "browser.navigate" if action_mode else "browser.observe",
                    "arguments": {
                        "target_ref": binding.target_id,
                        **(
                            {
                                "observation_hash": "a" * 64,
                                "precondition": {},
                                "arguments": {
                                    "url": "https://example.test/other"
                                    if mode == "navigate_wrong_url"
                                    else "https://example.test/page"
                                },
                                "postcondition": {},
                                "observation_content_hash": "b" * 64,
                            }
                            if action_mode
                            else {}
                        ),
                        **(
                            {"unexpected": "other-action"}
                            if mode == "wrong_inner_arguments"
                            else {}
                        ),
                    },
                },
                capabilities=(
                    Capability.REMOTE_TARGET_EXEC,
                    Capability.BROWSER_NAVIGATE if action_mode else Capability.BROWSER_OBSERVE,
                ),
                idempotency_key=key + "-changed" if mode == "wrong_key" else key,
            )
            if decision.decision.value == "ask":
                assert decision.approval_id is not None
                nested_ids.append(decision.approval_id)
                if mode == "expired":
                    with service.store._connect() as connection:
                        connection.execute(
                            "UPDATE phase45_approval_requests SET expires_at=? WHERE id=?",
                            ("2000-01-01T00:00:00+00:00", decision.approval_id),
                        )
                if mode == "takeover":
                    manager.transition(opened.json()["session_id"], "human_control")
                raise Phase1EError(
                    "http_409",
                    "approval required",
                    detail={
                        "code": "approval_required",
                        "approval_id": decision.approval_id,
                        "action_hash": action.action_hash,
                        "policy_version": action.policy_version,
                    },
                )
            if decision.decision.value == "deny":
                raise Phase1EError("http_403", "policy denied")
            assert decision.decision.value == "allow" and decision.lease is not None
            gateway.consume(decision.lease, action)
            if mode == "unknown":
                raise Phase1EError("manual_reconcile_required", "Job outcome unknown")
            return {
                "job_id": "fixture-job",
                "observation": {"body": {"url": "https://example.test"}},
            }

        result[tool_name] = replace(extension, execute=observe)
        return result

    monkeypatch.setattr(local_control_api, "local_capability_tool_extensions", extensions)
    service.provider.delegate = _ObserveProvider(
        two_calls=mode == "two_calls",
        tool_name=tool_name,
        arguments_json=json.dumps(tool_arguments),
    )

    async def consume() -> list[Any]:
        return [
            event
            async for event in service.run_session(
                session_id, user_message="Observe the browser", workspace=workspace
            )
        ]

    try:
        task = asyncio.create_task(consume())
        for _ in range(100):
            pending = service.list_pending_approvals(session_id)
            if pending:
                break
            await asyncio.sleep(0.01)
        assert pending and pending[0]["tool_call_id"] == "observe-call"
        assert keys == []
        if mode == "cancelled":
            assert service.cancel_session(session_id)
        else:
            service.decide_approval(session_id, "observe-call", approved=mode != "denied")
        if mode == "two_calls":
            for _ in range(100):
                pending = service.list_pending_approvals(session_id)
                if pending:
                    break
                await asyncio.sleep(0.01)
            assert pending and pending[0]["tool_call_id"] == "observe-call-2"
            service.decide_approval(session_id, "observe-call-2", approved=True)
        events = await task
        assert [event.event_type for event in events].count("tool.approval_required") == (
            2 if mode == "two_calls" else 1
        )
        if mode == "denied":
            assert events[-1].event_type == "agent.completed"
            assert keys == [] and nested_ids == []
            assert [event.event_type for event in events].count("tool.failed") == 1
        elif mode == "cancelled":
            assert events[-1].event_type == "agent.cancelled"
            assert keys == [] and nested_ids == []
        elif mode in {"approved", "navigate", "unknown"}:
            assert events[-1].event_type == (
                "agent.failed" if mode == "unknown" else "agent.completed"
            )
            assert len(keys) == 2 and keys[0] == keys[1]
            assert len(nested_ids) == 1
            assert (
                gateway.phase_repository.get_phase45_approval(nested_ids[0])["status"] == "consumed"
            )
            nested = gateway.phase_repository.get_phase45_approval(nested_ids[0])
            audit = SQLiteSecurityRepository(service.store).list_security_audit(
                nested["action_hash"]
            )
            assert [item.event_type for item in audit].count("approval.decided") == 1
            assert [item.event_type for item in audit].count("approval.consumed") == 1
            assert [event.event_type for event in events].count("tool.completed") == (
                0 if mode == "unknown" else 1
            )
        elif mode in {
            "navigate_observation_expired",
            "navigate_observation_unavailable",
            "navigate_expired_after_nested_ask",
        }:
            assert events[-1].event_type == "agent.completed"
            assert [event.event_type for event in events].count("tool.failed") == 1
            assert len(keys) == (2 if mode == "navigate_expired_after_nested_ask" else 1)
            assert len(nested_ids) == (1 if mode == "navigate_expired_after_nested_ask" else 0)
            if nested_ids:
                assert (
                    gateway.phase_repository.get_phase45_approval(nested_ids[0])["status"]
                    == "approved"
                )
            approval = service.store.list_approval_requests(session_id, status=None)[0]
            receipt = service.store.get_tool_action_receipt(approval.tool_action_receipt_id)
            assert receipt.status.value == "failed"
            assert receipt.error_code == "ToolError"
            assert receipt.result_json is not None
            assert "请重新查看后再操作" in receipt.result_json
            assert "https://example.test/page" not in receipt.result_json
        elif mode == "two_calls":
            assert events[-1].event_type == "agent.completed"
            assert len(keys) == 4 and keys[0] == keys[1] and keys[2] == keys[3]
            assert keys[0] != keys[2]
            assert len(nested_ids) == 2
            assert all(
                gateway.phase_repository.get_phase45_approval(approval_id)["status"] == "consumed"
                for approval_id in nested_ids
            )
        else:
            assert events[-1].event_type == "agent.failed"
            assert len(keys) == 1
            if nested_ids:
                assert (
                    gateway.phase_repository.get_phase45_approval(nested_ids[0])["status"]
                    != "consumed"
                )
    finally:
        service.close()
