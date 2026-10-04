from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from operant.api import create_app
from operant.application.remote_execution import observation_hash_for_job
from operant.application.security import PolicyEngine
from operant.domain.remote_execution import RemoteExecutionResult, RemoteJobStatus
from operant.domain.security import PolicyBundle, PolicyDecision, PolicyLayer, PolicyRule
from operant.remote.connector import ConnectorOutcome, RemoteOutcomeUnknown


class TestConnector:
    __test__ = False

    def __init__(self, lease: Any, calls: list[str]) -> None:
        self.target_id, self.lease_id = lease.target_id, lease.lease_id
        self.lease_token, self.lease_fencing = lease.token, lease.fencing
        self.calls = calls

    def execute(self, job: Any) -> ConnectorOutcome:
        self.calls.append(job.operation)
        if job.operation == "click":
            raise RemoteOutcomeUnknown("sent but not acknowledged")
        if job.operation == "observe_browser":
            body = {"url": "about:blank", "elements": []}
            postcondition = {"target_ref": self.target_id, "observation": body}
        else:
            assert "value" not in job.arguments["arguments"]
            postcondition = {"input_sha256": "bounded"}
        return ConnectorOutcome(
            RemoteExecutionResult(
                job_id=job.job_id,
                result_idempotency_key=job.job_id,
                status=RemoteJobStatus.SUCCEEDED,
                postcondition=postcondition,
            )
        )

    def cancel(self, job_id: str) -> None:
        pass


def _allow() -> PolicyEngine:
    return PolicyEngine(
        PolicyBundle(
            bundle_id="isolated-test",
            version="test.v1",
            default_decision=PolicyDecision.DENY,
            rules=(
                PolicyRule(
                    rule_id="bounded-tests",
                    layer=PolicyLayer.SYSTEM,
                    decision=PolicyDecision.ALLOW,
                    reason="test-only target",
                ),
            ),
        )
    )


def test_managed_takeover_fences_old_lease_and_requires_new_observation(tmp_path: Path) -> None:
    app = create_app(tmp_path / "core.sqlite3", phase45_policy_engine=_allow())
    manager = app.state.local_control_manager
    calls: list[str] = []
    manager.connector_factory = lambda record, lease, previous: TestConnector(lease, calls)
    record = manager.registry.install("operant.chrome.browser", ("https://example.com",))
    manager.registry.set_enabled(record.plugin_id, True)
    session = manager.open(record.plugin_id, "open-1")
    old = session.lease
    observed = manager.observe(session.session_id, "observe-1")
    manager._dispatch()
    result = manager.repository.get_result(observed["job_id"])
    observation_hash = observation_hash_for_job(
        manager.repository.get_job(observed["job_id"]),
        target_ref=session.target_id,
        body=result.postcondition["observation"],
    )
    manager.transition(session.session_id, "human_control")
    assert manager.repository.get_lease(old.lease_id).released_at is not None
    with pytest.raises(ValueError, match="paused"):
        manager.act(
            session.session_id,
            "fill",
            observation_hash,
            {"selector": "#draft", "value": "private local draft"},
            "fill-paused",
        )
    manager.transition(session.session_id, "active")
    assert session.lease.fencing > old.fencing
    with pytest.raises(ValueError, match="observe the current lease"):
        manager.act(
            session.session_id,
            "fill",
            observation_hash,
            {"selector": "#draft", "value": "private local draft"},
            "fill-stale",
        )
    observed_again = manager.observe(session.session_id, "observe-2")
    manager._dispatch()
    result_again = manager.repository.get_result(observed_again["job_id"])
    fresh_hash = observation_hash_for_job(
        manager.repository.get_job(observed_again["job_id"]),
        target_ref=session.target_id,
        body=result_again.postcondition["observation"],
    )
    assert fresh_hash != observation_hash
    with pytest.raises(ValueError, match="observe the current lease"):
        manager.act(
            session.session_id,
            "fill",
            observation_hash,
            {"selector": "#draft", "value": "private local draft"},
            "fill-old-generation",
        )
    fill = manager.act(
        session.session_id,
        "fill",
        fresh_hash,
        {"selector": "#draft", "value": "private local draft"},
        "fill-1",
    )
    job = manager.repository.get_job(fill["job_id"])
    assert "private local draft" not in json.dumps(job.model_dump(mode="json"))
    assert "token" not in json.dumps(session.projection())
    assert (
        manager.act(
            session.session_id,
            "fill",
            fresh_hash,
            {"selector": "#draft", "value": "private local draft"},
            "fill-1",
        )["job_id"]
        == fill["job_id"]
    )
    with pytest.raises(ValueError, match="changed request"):
        manager.act(
            session.session_id,
            "fill",
            fresh_hash,
            {"selector": "#draft", "value": "changed"},
            "fill-1",
        )
    manager._dispatch()
    assert calls.count("fill") == 1
    manager.transition(session.session_id, "closed")
    app.state.operant_service.close()


def test_unknown_action_stops_dispatch_and_keeps_original_job(tmp_path: Path) -> None:
    app = create_app(tmp_path / "core.sqlite3", phase45_policy_engine=_allow())
    manager = app.state.local_control_manager
    calls: list[str] = []
    manager.connector_factory = lambda record, lease, previous: TestConnector(lease, calls)
    manager.registry.install("operant.chrome.browser", ("https://example.com",))
    manager.registry.set_enabled("operant.chrome.browser", True)
    session = manager.open("operant.chrome.browser", "unknown-test")
    manager.observe(session.session_id, "observe")
    manager._dispatch()
    request = manager.act(
        session.session_id,
        "click",
        session.observation["observation_hash"],
        {"selector": "#go"},
        "unknown-click",
    )
    manager._dispatch()
    assert session.state == "failed"
    assert (
        manager.repository.get_job(request["job_id"]).status
        is RemoteJobStatus.MANUAL_RECONCILE_REQUIRED
    )
    manager._dispatch()
    assert calls.count("click") == 1
    with pytest.raises(ValueError):
        manager.transition(session.session_id, "active")
    manager.transition(session.session_id, "closed")
    with pytest.raises(ValueError, match="manually check"):
        manager.open("operant.chrome.browser", "new-session")
    app.state.operant_service.close()

    restarted = create_app(
        tmp_path / "core.sqlite3",
        phase45_policy_engine=_allow(),
        phase56_local_authorizer=lambda request: True,
    )
    restored = restarted.state.local_control_manager
    restored.connector_factory = lambda record, lease, previous: TestConnector(lease, calls)
    with pytest.raises(ValueError, match="manually check"):
        restored.open("operant.chrome.browser", "after-restart")
    with TestClient(restarted) as client:
        assert (
            client.get("/v1/local-control/unknown-jobs").json()["jobs"][0]["job_id"]
            == request["job_id"]
        )
        endpoint = f"/v1/local-control/unknown-jobs/{request['job_id']}/reconcile"
        checked = client.post(
            endpoint,
            json={
                "idempotency_key": "human-checked",
                "observed_outcome": "not_applied",
                "evidence_note": "Synthetic page checked manually; no change occurred.",
            },
        )
        assert checked.status_code == 200, checked.text
        assert checked.json()["status"] == "manual_reconcile_required"
        assert client.get("/v1/local-control/unknown-jobs").json() == {"jobs": []}
        assert restored.repository.get_job(request["job_id"]).status is (
            RemoteJobStatus.MANUAL_RECONCILE_REQUIRED
        )
        restored.open("operant.chrome.browser", "after-human-check")
        assert calls.count("click") == 1
        with restored.repository.store._connect() as connection:
            audit = connection.execute(
                "SELECT detail FROM security_audit_events "
                "WHERE event_type='local_control.unknown_checked'"
            ).fetchone()[0]
        assert "Synthetic page" not in audit
        assert json.loads(audit)["observed_outcome"] == "not_applied"


def test_local_api_denies_remote_clients_and_disabled_adapters(tmp_path: Path) -> None:
    app = create_app(
        tmp_path / "core.sqlite3",
        phase45_policy_engine=_allow(),
        phase56_local_authorizer=lambda request: request.headers.get("x-test-local") == "yes",
    )
    with TestClient(app) as client:
        assert client.get("/v1/local-control/sessions").status_code == 403
        assert (
            client.post(
                "/v1/local-control/plugins",
                headers={"x-test-local": "yes"},
                json={
                    "plugin_id": "operant.chrome.browser",
                    "allowed_targets": ["https://example.com"],
                    "idempotency_key": "install",
                },
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/v1/local-control/sessions",
                headers={"x-test-local": "yes"},
                json={"plugin_id": "operant.chrome.browser", "idempotency_key": "disabled-open"},
            ).status_code
            == 403
        )


def test_capture_bytes_are_ephemeral_bounded_and_expire(monkeypatch: Any) -> None:
    from operant.remote.transient_artifacts import TransientCapabilityArtifacts

    clock = [0.0]
    monkeypatch.setattr("operant.remote.transient_artifacts.time.monotonic", lambda: clock[0])
    artifacts = TransientCapabilityArtifacts(ttl_seconds=5, max_bytes=20)
    artifacts.put("one", b"\x89PNG\r\n\x1a\n123", "image/png")
    artifacts.put("two", b"local clipboard", "text/plain")
    with pytest.raises(KeyError):
        artifacts.get("one")
    assert artifacts.get("two").content == b"local clipboard"
    clock[0] = 6
    with pytest.raises(KeyError):
        artifacts.get("two")
    with pytest.raises(ValueError):
        artifacts.put("bad", b"not a PNG", "image/png")


def test_management_ask_replay_survives_restart_without_reinstall(tmp_path: Path) -> None:
    database = tmp_path / "approval.sqlite3"
    body = {
        "plugin_id": "operant.chrome.browser",
        "allowed_targets": ["https://example.com"],
        "idempotency_key": "durable-install",
    }
    app = create_app(database, phase56_local_authorizer=lambda request: True)
    with TestClient(app) as client:
        asked = client.post("/v1/local-control/plugins", json=body)
        assert asked.status_code == 409
        approval_id = asked.json()["detail"]["approval_id"]
        approved = client.post(
            f"/v1/security/approvals/{approval_id}",
            json={"approved": True, "reason_code": "synthetic-test"},
        )
        assert approved.status_code == 200
        installed = client.post("/v1/local-control/plugins", json=body)
        assert installed.status_code == 200, installed.text
        original = installed.json()
    restarted = create_app(database, phase56_local_authorizer=lambda request: True)
    manager = restarted.state.local_control_manager
    manager.registry.set_enabled("operant.chrome.browser", True)
    with TestClient(restarted) as client:
        repeated = client.post("/v1/local-control/plugins", json=body)
        assert repeated.status_code == 200, repeated.text
        assert repeated.json() == original
        assert manager.registry.get("operant.chrome.browser", require_enabled=True)
        changed = client.post(
            "/v1/local-control/plugins",
            json={**body, "allowed_targets": ["https://different.example"]},
        )
        assert changed.status_code == 422


@pytest.mark.parametrize("entry", ["get", "bindings", "dispatch", "resume"])
def test_external_registry_reenable_revokes_old_session(tmp_path: Path, entry: str) -> None:
    app = create_app(tmp_path / "generation.sqlite3", phase45_policy_engine=_allow())
    manager = app.state.local_control_manager
    calls: list[str] = []
    manager.connector_factory = lambda record, lease, previous: TestConnector(lease, calls)
    record = manager.registry.install("operant.chrome.browser", ("https://example.com",))
    manager.registry.set_enabled(record.plugin_id, True)
    session = manager.open(record.plugin_id, "original-open")
    lease = session.lease
    if entry == "resume":
        manager.transition(session.session_id, "human_control")
    manager.registry.set_enabled(record.plugin_id, False)
    manager.registry.set_enabled(record.plugin_id, True)
    if entry == "bindings":
        assert manager.bindings() == {}
    elif entry == "dispatch":
        manager._dispatch()
    elif entry == "resume":
        with pytest.raises(PermissionError, match="authorization changed"):
            manager.transition(session.session_id, "active")
    else:
        with pytest.raises(PermissionError, match="authorization changed"):
            manager.get(session.session_id, active=True)
    assert session.state == "failed"
    assert manager.repository.get_lease(lease.lease_id).released_at is not None
    assert calls == []


def test_background_dispatch_expires_unused_plaintext(tmp_path: Path, monkeypatch: Any) -> None:
    app = create_app(tmp_path / "idle.sqlite3", phase45_policy_engine=_allow())
    manager = app.state.local_control_manager
    clock = [0.0]
    monkeypatch.setattr("operant.remote.transient_artifacts.time.monotonic", lambda: clock[0])
    manager.artifacts.put("unused", b"temporary clipboard evidence", "text/plain")
    assert manager.artifacts._items
    clock[0] = 301
    manager._dispatch()
    assert manager.artifacts._items == {}
