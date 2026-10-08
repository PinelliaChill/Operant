from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

import operant.api_local_control as local_control_api
import operant.remote.operator as local_operator
from operant.api import create_app
from operant.application.local_control import snapshot_control_bindings
from operant.application.security import PolicyEngine
from operant.domain.messages import Message, ModelResponse, ProviderEvent, ToolCall, ToolDefinition
from operant.domain.models import ModelProfile, RolePreset, RoleSnapshot, ToolPolicy
from operant.domain.security import (
    Capability,
    PolicyBundle,
    PolicyDecision,
    PolicyLayer,
    PolicyRule,
)
from operant.persistence.security import SQLiteSecurityRepository
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
