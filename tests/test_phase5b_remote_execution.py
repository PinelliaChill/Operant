from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from operant.api_phase56_target import (
    _validate_local_capability_action,
    install_phase56_target_routes,
)
from operant.application.phase45_gateway import Phase45ActionGateway
from operant.application.remote_execution import (
    RemoteAuthorization,
    RemoteExecutionController,
    observation_hash_for_job,
)
from operant.application.security import PolicyEngine
from operant.domain.remote_execution import (
    CapabilityActionRequest,
    CapabilityManifest,
    CapabilityObservation,
    RemoteActionIdempotency,
    RemoteCapability,
    RemoteExecutionResult,
    RemoteJobStatus,
    RemoteTargetRegistration,
)
from operant.domain.security import (
    ActionRequest,
    Capability,
    PolicyBundle,
    PolicyDecision,
    PolicyLayer,
    PolicyRule,
    SecurityAuditEvent,
)
from operant.persistence.phase45 import SQLitePhase45Repository
from operant.persistence.remote_execution import SQLiteRemoteExecutionRepository
from operant.persistence.security import SQLiteSecurityRepository
from operant.persistence.sqlite import ConflictError, SQLiteStore
from operant.protocol import canonical_action_hash
from operant.remote import ConnectorOutcome, InMemoryRemoteTargetConnector, RemoteOutcomeUnknown


def _now() -> datetime:
    return datetime(2026, 9, 4, 6, 0, tzinfo=timezone.utc)


def test_local_capability_preflight_rejects_persisted_credentials_and_url_query() -> None:
    action = CapabilityActionRequest(
        target_id="browser-test",
        capability=RemoteCapability.BROWSER_SUBMIT,
        operation="fill",
        target_ref="browser-test",
        observation_hash="a" * 64,
        arguments={"selector": "#query", "value": "sk-abcdefghijklmnopqrstuv"},
        idempotency_key="credential-preflight",
        idempotency=RemoteActionIdempotency.NON_IDEMPOTENT,
    )
    with pytest.raises(ValueError, match="arguments"):
        _validate_local_capability_action("browser", action)
    navigation = action.model_copy(
        update={
            "capability": RemoteCapability.BROWSER_NAVIGATE,
            "operation": "navigate",
            "arguments": {"url": "https://example.com/?token=value"},
        }
    )
    with pytest.raises(ValueError, match="query"):
        _validate_local_capability_action("browser", navigation)
    hidden_argument = action.model_copy(
        update={
            "arguments": {
                "selector": "#query",
                "value": "safe text",
                "secret": "sk-abcdefghijklmnopqrstuv",
            }
        }
    )
    with pytest.raises(ValueError, match="arguments"):
        _validate_local_capability_action("browser", hidden_argument)


def _allow_engine() -> PolicyEngine:
    return PolicyEngine(
        PolicyBundle(
            bundle_id="remote-test",
            version="phase56.test",
            default_decision=PolicyDecision.DENY,
            rules=(
                PolicyRule(
                    rule_id="test.remote.allow",
                    layer=PolicyLayer.SYSTEM,
                    decision=PolicyDecision.ALLOW,
                    reason="deterministic test policy",
                ),
            ),
        )
    )


class _Authorization(RemoteAuthorization):
    def __init__(self, gateway: Phase45ActionGateway) -> None:
        self.gateway = gateway

    def authorize(
        self,
        *,
        tool: str,
        operation: str,
        target_id: str,
        arguments: dict[str, Any],
        capabilities: Sequence[Capability],
        idempotency_key: str,
    ) -> ActionRequest:
        action, result, _ = self.gateway.guard(
            tool=tool,
            operation=operation,
            target_id=target_id,
            arguments=arguments,
            capabilities=capabilities,
            idempotency_key=idempotency_key,
        )
        assert result.decision.value == "allow"
        assert result.lease is not None
        self.gateway.consume(result.lease, action)
        return action


def _controller(tmp_path: Path) -> tuple[RemoteExecutionController, SQLiteStore]:
    store = SQLiteStore(tmp_path / "remote.sqlite3")
    store.initialize()
    security = SQLiteSecurityRepository(store)
    gateway = Phase45ActionGateway(
        security, SQLitePhase45Repository(store), _allow_engine(), principal="test:controller"
    )
    return (
        RemoteExecutionController(SQLiteRemoteExecutionRepository(store), _Authorization(gateway)),
        store,
    )


def _target() -> RemoteTargetRegistration:
    return RemoteTargetRegistration(
        target_id="target-test",
        display_name="Test target",
        endpoint_ref="REMOTE_TARGET_ENDPOINT",
        identity_public_key="public-key-" + "x" * 32,
        credential_ref="REMOTE_TARGET_TOKEN",
        policy_ref="balanced",
        artifact_namespace="target-test-artifacts",
        capability_manifest=CapabilityManifest(
            version="1",
            capabilities=(
                RemoteCapability.TARGET_EXEC,
                RemoteCapability.BROWSER_OBSERVE,
                RemoteCapability.BROWSER_NAVIGATE,
                RemoteCapability.BROWSER_SUBMIT,
                RemoteCapability.COMPUTER_OBSERVE,
                RemoteCapability.COMPUTER_INPUT,
            ),
            supported_operations=(
                "run",
                "observe_browser",
                "observe_computer",
                "navigate",
                "submit",
                "input",
            ),
            max_concurrent_jobs=8,
            platform="test",
        ),
        created_at=_now(),
        updated_at=_now(),
    )


def _online_lease(
    controller: RemoteExecutionController,
) -> tuple[RemoteTargetRegistration, Any]:
    target = controller.register_target(_target())
    controller.heartbeat_target(
        target.target_id, identity_public_key=target.identity_public_key, now=_now()
    )
    lease = controller.acquire_lease(
        target.target_id,
        owner="connector:test",
        workspace_ref="workspace:test",
        ttl_seconds=120,
        now=_now(),
        idempotency_key="lease-1",
    )
    return target, lease


def test_remote_target_lease_fencing_pull_result_and_checksum(tmp_path: Path) -> None:
    controller, store = _controller(tmp_path)
    target, lease = _online_lease(controller)
    with store._connect() as connection:
        persisted_token = connection.execute(
            "SELECT token_hash FROM remote_target_leases WHERE lease_id=?", (lease.lease_id,)
        ).fetchone()[0]
    assert persisted_token != lease.token
    assert len(persisted_token) == 64
    job = controller.create_job(
        target_id=target.target_id,
        lease_id=lease.lease_id,
        lease_token=lease.token,
        lease_fencing=lease.fencing,
        capability=RemoteCapability.TARGET_EXEC,
        operation="run",
        arguments={"argv": ["pytest", "-q"]},
        idempotency_key="job-run-1",
        idempotency=RemoteActionIdempotency.IDEMPOTENT,
        now=_now(),
    )
    assert controller.repository.get_result(job.job_id) is None
    polled = controller.poll_jobs(
        target_id=target.target_id,
        lease_id=lease.lease_id,
        token=lease.token,
        fencing=lease.fencing,
        limit=1,
        now=_now(),
        idempotency_key="poll-1",
    )
    assert [item.job_id for item in polled] == [job.job_id]
    artifact = b"deterministic artifact"
    result = RemoteExecutionResult(
        job_id=job.job_id,
        result_idempotency_key="result-1",
        status=RemoteJobStatus.SUCCEEDED,
        artifact_ref="artifact:test",
        artifact_sha256=hashlib.sha256(artifact).hexdigest(),
        postcondition={"exit_code": 0},
        completed_at=_now(),
    )
    completed = controller.complete_job(
        result,
        target_id=target.target_id,
        lease_id=lease.lease_id,
        token=lease.token,
        fencing=lease.fencing,
        now=_now(),
        artifact_bytes=artifact,
    )
    assert completed.status is RemoteJobStatus.SUCCEEDED
    assert controller.repository.get_result(job.job_id) == completed
    with pytest.raises(ConflictError, match="checksum"):
        controller.complete_job(
            result.model_copy(update={"job_id": "missing"}),
            target_id=target.target_id,
            lease_id=lease.lease_id,
            token=lease.token,
            fencing=lease.fencing,
            now=_now(),
            artifact_bytes=b"tampered",
        )

    controller.release_lease(
        target_id=target.target_id,
        lease_id=lease.lease_id,
        token=lease.token,
        fencing=lease.fencing,
        now=_now(),
        idempotency_key="release-1",
    )
    controller.heartbeat_target(
        target.target_id,
        identity_public_key=target.identity_public_key,
        now=_now() + timedelta(seconds=1),
    )
    newer = controller.acquire_lease(
        target.target_id,
        owner="connector:new",
        workspace_ref="workspace:new",
        ttl_seconds=60,
        now=_now() + timedelta(seconds=1),
        idempotency_key="lease-2",
    )
    assert newer.fencing == lease.fencing + 1
    with pytest.raises(ConflictError, match="stale"):
        controller.repository.poll_jobs(
            target_id=target.target_id,
            lease_id=lease.lease_id,
            token=lease.token,
            fencing=lease.fencing,
            now=_now() + timedelta(seconds=1),
            limit=1,
        )


def test_browser_observation_binding_and_unknown_submit_are_fail_closed(
    tmp_path: Path,
) -> None:
    controller, _store = _controller(tmp_path)
    target, lease = _online_lease(controller)
    observe_job = controller.observe(
        kind="browser",
        target_id=target.target_id,
        lease_id=lease.lease_id,
        lease_token=lease.token,
        lease_fencing=lease.fencing,
        target_ref="browser:tab-1",
        idempotency_key="observe-1",
        now=_now(),
    )
    assert "observation_generation" not in observe_job.arguments
    connector = InMemoryRemoteTargetConnector(
        target_id=target.target_id,
        lease_id=lease.lease_id,
        lease_token=lease.token,
        lease_fencing=lease.fencing,
    )
    connector.outcomes["observe_browser"] = ConnectorOutcome(
        RemoteExecutionResult(
            job_id=observe_job.job_id,
            result_idempotency_key="observe-result-1",
            status=RemoteJobStatus.SUCCEEDED,
            postcondition={
                "target_ref": "browser:tab-1",
                "observation": {"url": "https://example.test/form", "form_ready": True},
            },
            completed_at=_now(),
        )
    )
    observation_result = controller.dispatch_available(connector, now=_now())[0]
    assert observation_result.status is RemoteJobStatus.SUCCEEDED
    observation_hash = canonical_action_hash(
        {
            "target_id": target.target_id,
            "target_ref": "browser:tab-1",
            "body": {"url": "https://example.test/form", "form_ready": True},
        }
    )
    action = CapabilityActionRequest(
        target_id=target.target_id,
        capability=RemoteCapability.BROWSER_SUBMIT,
        operation="submit",
        target_ref="browser:tab-1",
        observation_hash=observation_hash,
        precondition={"form_ready": True},
        arguments={"button": "Send"},
        postcondition={"confirmation": True},
        idempotency_key="submit-1",
        idempotency=RemoteActionIdempotency.NON_IDEMPOTENT,
    )
    with pytest.raises(ConflictError, match="precondition"):
        controller.act(
            action.model_copy(
                update={
                    "idempotency_key": "submit-precondition-mismatch",
                    "precondition": {"form_ready": False},
                }
            ),
            kind="browser",
            lease_id=lease.lease_id,
            lease_token=lease.token,
            lease_fencing=lease.fencing,
            now=_now(),
        )
    submit_job, receipt = controller.act(
        action,
        kind="browser",
        lease_id=lease.lease_id,
        lease_token=lease.token,
        lease_fencing=lease.fencing,
        now=_now(),
    )
    assert receipt.observation_hash == observation_hash
    connector.outcomes["submit"] = RemoteOutcomeUnknown("ack lost")
    [unknown] = controller.dispatch_available(connector, now=_now())
    assert unknown.job_id == submit_job.job_id
    assert unknown.status is RemoteJobStatus.MANUAL_RECONCILE_REQUIRED
    stored_receipt = controller.repository.complete_action_receipt(
        submit_job.action_hash,
        status=RemoteJobStatus.MANUAL_RECONCILE_REQUIRED,
        result={},
        error_code="remote.outcome_unknown",
        now=_now(),
    )
    assert stored_receipt.status is RemoteJobStatus.MANUAL_RECONCILE_REQUIRED


def test_local_same_content_reobserve_has_fresh_generation_and_old_expiry(
    tmp_path: Path,
) -> None:
    controller, _store = _controller(tmp_path)
    target = controller.register_target(
        _target().model_copy(update={"policy_ref": "local-control:test-digest"})
    )
    controller.heartbeat_target(
        target.target_id, identity_public_key=target.identity_public_key, now=_now()
    )
    lease = controller.acquire_lease(
        target.target_id,
        owner="local-control:test",
        workspace_ref="local-control",
        ttl_seconds=120,
        now=_now(),
        idempotency_key="local-generation-lease",
    )
    body = {"url": "https://example.test/same", "elements": []}

    def observe(key: str, when: datetime) -> tuple[str, CapabilityObservation]:
        job = controller.observe(
            kind="browser",
            target_id=target.target_id,
            lease_id=lease.lease_id,
            lease_token=lease.token,
            lease_fencing=lease.fencing,
            target_ref=target.target_id,
            idempotency_key=key,
            now=when,
        )
        assert job.arguments["observation_generation"] == "job.v1"
        [polled] = controller.poll_jobs(
            target_id=target.target_id,
            lease_id=lease.lease_id,
            token=lease.token,
            fencing=lease.fencing,
            limit=1,
            now=when,
            idempotency_key=f"poll:{key}",
        )
        assert polled.job_id == job.job_id
        controller.complete_job(
            RemoteExecutionResult(
                job_id=job.job_id,
                result_idempotency_key=f"result:{key}",
                status=RemoteJobStatus.SUCCEEDED,
                postcondition={"target_ref": target.target_id, "observation": body},
            ),
            target_id=target.target_id,
            lease_id=lease.lease_id,
            token=lease.token,
            fencing=lease.fencing,
            now=when,
        )
        observed_hash = observation_hash_for_job(job, target_ref=target.target_id, body=body)
        return observed_hash, controller.repository.get_observation(target.target_id, observed_hash)

    old_hash, old = observe("local-observe-1", _now())
    later = _now() + timedelta(seconds=61)
    fresh_hash, fresh = observe("local-observe-2", later)
    assert old_hash != fresh_hash
    assert old.observation_id != fresh.observation_id
    assert old.expires_at <= later < fresh.expires_at
    assert controller.repository.get_observation(target.target_id, old_hash) == old

    request = CapabilityActionRequest(
        target_id=target.target_id,
        capability=RemoteCapability.BROWSER_NAVIGATE,
        operation="navigate",
        target_ref=target.target_id,
        observation_hash=old_hash,
        arguments={"url": "https://example.test/next"},
        idempotency_key="navigate-with-expired-generation",
        idempotency=RemoteActionIdempotency.NON_IDEMPOTENT,
    )
    with pytest.raises(ConflictError, match="expired"):
        controller.act(
            request,
            kind="browser",
            lease_id=lease.lease_id,
            lease_token=lease.token,
            lease_fencing=lease.fencing,
            now=later,
        )
    new_job, _ = controller.act(
        request.model_copy(
            update={
                "observation_hash": fresh_hash,
                "idempotency_key": "navigate-with-fresh-generation",
            }
        ),
        kind="browser",
        lease_id=lease.lease_id,
        lease_token=lease.token,
        lease_fencing=lease.fencing,
        now=later,
    )
    assert new_job.arguments["observation_hash"] == fresh_hash
    assert new_job.arguments["observation_content_hash"] == canonical_action_hash(
        {"target_id": target.target_id, "target_ref": target.target_id, "body": body}
    )
    direct_job = controller.create_job(
        target_id=target.target_id,
        lease_id=lease.lease_id,
        lease_token=lease.token,
        lease_fencing=lease.fencing,
        capability=RemoteCapability.BROWSER_NAVIGATE,
        operation="navigate",
        arguments={
            "target_ref": target.target_id,
            "observation_hash": fresh_hash,
            "observation_content_hash": "0" * 64,
            "arguments": {"url": "https://example.test/another"},
        },
        idempotency_key="direct-local-action-cannot-forge-content-hash",
        idempotency=RemoteActionIdempotency.NON_IDEMPOTENT,
        now=later,
    )
    assert (
        direct_job.arguments["observation_content_hash"]
        == new_job.arguments["observation_content_hash"]
    )


def test_dispatch_stops_after_unknown_without_claiming_later_job(
    tmp_path: Path, monkeypatch: Any
) -> None:
    controller, store = _controller(tmp_path)
    target, lease = _online_lease(controller)
    first = controller.create_job(
        target_id=target.target_id,
        lease_id=lease.lease_id,
        lease_token=lease.token,
        lease_fencing=lease.fencing,
        capability=RemoteCapability.TARGET_EXEC,
        operation="run",
        arguments={"step": 1},
        idempotency_key="unknown-first",
        idempotency=RemoteActionIdempotency.NON_IDEMPOTENT,
        now=_now(),
    )
    second = controller.create_job(
        target_id=target.target_id,
        lease_id=lease.lease_id,
        lease_token=lease.token,
        lease_fencing=lease.fencing,
        capability=RemoteCapability.TARGET_EXEC,
        operation="run",
        arguments={"step": 2},
        idempotency_key="unknown-second",
        idempotency=RemoteActionIdempotency.NON_IDEMPOTENT,
        now=_now() + timedelta(milliseconds=1),
    )
    connector = InMemoryRemoteTargetConnector(
        target_id=target.target_id,
        lease_id=lease.lease_id,
        lease_token=lease.token,
        lease_fencing=lease.fencing,
        outcomes={"run": RemoteOutcomeUnknown("synthetic lost acknowledgement")},
    )
    [unknown] = controller.dispatch_available(connector, now=_now(), limit=8)
    assert unknown.job_id == first.job_id
    assert unknown.status is RemoteJobStatus.MANUAL_RECONCILE_REQUIRED
    assert connector.calls == [first.job_id]
    assert controller.repository.get_job(second.job_id).status is RemoteJobStatus.QUEUED

    SQLiteSecurityRepository(store).append_security_audit(
        SecurityAuditEvent(
            action_hash=first.action_hash,
            principal="test:operator",
            event_type="local_control.unknown_checked",
            detail={"job_id": first.job_id, "observed_outcome": "not_applied"},
        )
    )
    restarted = RemoteExecutionController(
        SQLiteRemoteExecutionRepository(store), controller.authorization
    )
    with pytest.raises(ConflictError, match="new fenced lease"):
        restarted.poll_jobs(
            target_id=target.target_id,
            lease_id=lease.lease_id,
            token=lease.token,
            fencing=lease.fencing,
            limit=8,
            now=_now(),
            idempotency_key="poll-after-human-check",
        )
    with pytest.raises(ConflictError, match="new fenced lease"):
        restarted.dispatch_available(connector, now=_now(), limit=8)
    assert connector.calls == [first.job_id]
    assert restarted.repository.get_job(second.job_id).status is RemoteJobStatus.QUEUED

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return _now()

    monkeypatch.setattr("operant.api_phase56_target.datetime", FixedDatetime)
    http_app = FastAPI()
    install_phase56_target_routes(
        http_app,
        store,
        action_gateway=Phase45ActionGateway(
            SQLiteSecurityRepository(store),
            SQLitePhase45Repository(store),
            _allow_engine(),
            principal="test:http-unknown-restart",
        ),
    )
    with TestClient(http_app) as client:
        blocked = client.post(
            f"/v1/remote-targets/{target.target_id}/jobs/poll",
            json={
                "lease_id": lease.lease_id,
                "token": lease.token,
                "fencing": lease.fencing,
                "limit": 1,
                "idempotency_key": "poll-http-after-human-check",
            },
        )
    assert blocked.status_code == 409
    assert "new fenced lease" in blocked.json()["detail"]


def test_browser_postcondition_and_manifest_payload_limit_fail_closed(tmp_path: Path) -> None:
    controller, _store = _controller(tmp_path)
    target, lease = _online_lease(controller)
    observation_job = controller.observe(
        kind="browser",
        target_id=target.target_id,
        lease_id=lease.lease_id,
        lease_token=lease.token,
        lease_fencing=lease.fencing,
        target_ref="browser:tab-2",
        idempotency_key="observe-postcondition",
        now=_now(),
    )
    connector = InMemoryRemoteTargetConnector(
        target_id=target.target_id,
        lease_id=lease.lease_id,
        lease_token=lease.token,
        lease_fencing=lease.fencing,
        outcomes={
            "observe_browser": ConnectorOutcome(
                RemoteExecutionResult(
                    job_id=observation_job.job_id,
                    result_idempotency_key="observe-postcondition-result",
                    status=RemoteJobStatus.SUCCEEDED,
                    postcondition={
                        "target_ref": "browser:tab-2",
                        "observation": {"url": "https://example.test/form"},
                    },
                    completed_at=_now(),
                )
            )
        },
    )
    controller.dispatch_available(connector, now=_now())
    observation_hash = canonical_action_hash(
        {
            "target_id": target.target_id,
            "target_ref": "browser:tab-2",
            "body": {"url": "https://example.test/form"},
        }
    )
    action_job, _receipt = controller.act(
        CapabilityActionRequest(
            target_id=target.target_id,
            capability=RemoteCapability.BROWSER_NAVIGATE,
            operation="navigate",
            target_ref="browser:tab-2",
            observation_hash=observation_hash,
            precondition={"url": "https://example.test/form"},
            arguments={"url": "https://example.test/done"},
            postcondition={"url": "https://example.test/done"},
            idempotency_key="navigate-postcondition",
            idempotency=RemoteActionIdempotency.IDEMPOTENT,
        ),
        kind="browser",
        lease_id=lease.lease_id,
        lease_token=lease.token,
        lease_fencing=lease.fencing,
        now=_now(),
    )
    connector.outcomes["navigate"] = ConnectorOutcome(
        RemoteExecutionResult(
            job_id=action_job.job_id,
            result_idempotency_key="navigate-postcondition-result",
            status=RemoteJobStatus.SUCCEEDED,
            postcondition={"url": "https://example.test/wrong"},
            completed_at=_now(),
        )
    )
    [failed] = controller.dispatch_available(connector, now=_now())
    assert failed.status is RemoteJobStatus.FAILED
    assert failed.error_code == "remote.postcondition_failed"

    limited = target.model_copy(
        update={
            "target_id": "target-limited",
            "capability_manifest": target.capability_manifest.model_copy(
                update={"max_payload_bytes": 8}
            ),
        }
    )
    controller.register_target(limited)
    controller.heartbeat_target(
        limited.target_id, identity_public_key=limited.identity_public_key, now=_now()
    )
    limited_lease = controller.acquire_lease(
        limited.target_id,
        owner="connector:limited",
        workspace_ref="workspace:limited",
        ttl_seconds=60,
        now=_now(),
        idempotency_key="limited-lease",
    )
    with pytest.raises(ConflictError, match="payload"):
        controller.create_job(
            target_id=limited.target_id,
            lease_id=limited_lease.lease_id,
            lease_token=limited_lease.token,
            lease_fencing=limited_lease.fencing,
            capability=RemoteCapability.TARGET_EXEC,
            operation="run",
            arguments={"argv": ["too-large"]},
            idempotency_key="limited-job",
            idempotency=RemoteActionIdempotency.IDEMPOTENT,
            now=_now(),
        )


def test_expired_non_idempotent_running_job_requires_manual_reconcile(tmp_path: Path) -> None:
    controller, _store = _controller(tmp_path)
    target, lease = _online_lease(controller)
    job = controller.create_job(
        target_id=target.target_id,
        lease_id=lease.lease_id,
        lease_token=lease.token,
        lease_fencing=lease.fencing,
        capability=RemoteCapability.BROWSER_SUBMIT,
        operation="submit",
        arguments={"target_ref": "browser:tab"},
        idempotency_key="submit-expiry",
        idempotency=RemoteActionIdempotency.NON_IDEMPOTENT,
        now=_now(),
    )
    controller.repository.poll_jobs(
        target_id=target.target_id,
        lease_id=lease.lease_id,
        token=lease.token,
        fencing=lease.fencing,
        now=_now(),
        limit=1,
    )
    [changed] = controller.repository.reconcile_expired(now=_now() + timedelta(seconds=121))
    assert changed.job_id == job.job_id
    assert changed.status is RemoteJobStatus.MANUAL_RECONCILE_REQUIRED


def test_remote_target_api_installer_exposes_bounded_operations(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "api.sqlite3")
    store.initialize()
    gateway = Phase45ActionGateway(
        SQLiteSecurityRepository(store),
        SQLitePhase45Repository(store),
        _allow_engine(),
    )
    app = FastAPI()
    install_phase56_target_routes(app, store, action_gateway=gateway)
    operations = {
        operation_id
        for route in app.routes
        if (operation_id := getattr(route, "operation_id", None)) is not None
    }
    assert {
        "registerRemoteTarget",
        "listRemoteTargets",
        "getRemoteTarget",
        "heartbeatRemoteTarget",
        "acquireRemoteTargetLease",
        "renewRemoteTargetLease",
        "releaseRemoteTargetLease",
        "createRemoteTargetJob",
        "pollRemoteTargetJobs",
        "completeRemoteTargetJob",
        "cancelRemoteTargetJob",
        "listRemoteTargetJobs",
        "getRemoteTargetJobResult",
        "observeBrowser",
        "actBrowser",
        "observeComputer",
        "actComputer",
    }.issubset(operations)
    with TestClient(app) as client:
        response = client.post(
            "/v1/remote-targets",
            json={
                "target_id": "api-target",
                "display_name": "API target",
                "endpoint_ref": "REMOTE_ENDPOINT",
                "identity_public_key": "public-key-" + "x" * 32,
                "credential_ref": "REMOTE_TARGET_TOKEN",
                "policy_ref": "balanced",
                "artifact_namespace": "api-artifacts",
                "capability_manifest": {
                    "version": "1",
                    "capabilities": ["remote.target.exec"],
                    "supported_operations": ["run"],
                    "platform": "test",
                },
            },
        )
        assert response.status_code == 201, response.text
        listed = client.get("/v1/remote-targets")
        assert listed.status_code == 200
        assert listed.json()["items"][0]["credential_ref"] == "REMOTE_TARGET_TOKEN"
        jobs = client.get("/v1/remote-targets/jobs", params={"target_id": "api-target"})
        assert jobs.status_code == 200
        assert jobs.json() == {"items": []}
        missing_result = client.get("/v1/remote-targets/jobs/missing/result")
        assert missing_result.status_code == 404


def test_existing_remote_browser_target_keeps_its_action_contract(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "remote-browser-api.sqlite3")
    store.initialize()
    gateway = Phase45ActionGateway(
        SQLiteSecurityRepository(store), SQLitePhase45Repository(store), _allow_engine()
    )
    app = FastAPI()
    install_phase56_target_routes(app, store, action_gateway=gateway)
    with TestClient(app) as client:
        target_id = "existing-browser-target"
        identity = "public-key-" + "x" * 32
        registered = client.post(
            "/v1/remote-targets",
            json={
                "target_id": target_id,
                "display_name": "Existing remote browser",
                "endpoint_ref": "REMOTE_ENDPOINT",
                "identity_public_key": identity,
                "credential_ref": "REMOTE_TARGET_TOKEN",
                "policy_ref": "balanced",
                "artifact_namespace": "existing-browser",
                "capability_manifest": {
                    "version": "1",
                    "capabilities": ["browser.submit", "browser.observe"],
                    "supported_operations": ["submit", "observe_browser"],
                    "platform": "test",
                },
            },
        )
        assert registered.status_code == 201, registered.text
        assert (
            client.post(
                f"/v1/remote-targets/{target_id}/heartbeat",
                json={"identity_public_key": identity},
            ).status_code
            == 200
        )
        lease_response = client.post(
            f"/v1/remote-targets/{target_id}/leases",
            json={
                "owner": "existing-browser-test",
                "workspace_ref": str(tmp_path),
                "ttl_seconds": 180,
                "idempotency_key": "existing-browser-lease",
            },
        )
        assert lease_response.status_code == 201, lease_response.text
        lease = lease_response.json()
        observation_body = {"form_ready": True}
        observation_hash = canonical_action_hash(
            {"target_id": target_id, "target_ref": "browser:tab-1", "body": observation_body}
        )
        SQLiteRemoteExecutionRepository(store).record_observation(
            CapabilityObservation(
                target_id=target_id,
                capability=RemoteCapability.BROWSER_OBSERVE,
                target_ref="browser:tab-1",
                observation_hash=observation_hash,
                body=observation_body,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=3),
            )
        )
        accepted = client.post(
            "/v1/browser/act",
            json={
                "lease_id": lease["lease_id"],
                "token": lease["token"],
                "fencing": lease["fencing"],
                "action": {
                    "target_id": target_id,
                    "capability": "browser.submit",
                    "operation": "submit",
                    "target_ref": "browser:tab-1",
                    "observation_hash": observation_hash,
                    "precondition": {"form_ready": True},
                    "arguments": {"button": "Send"},
                    "postcondition": {"confirmation": True},
                    "idempotency_key": "existing-browser-submit",
                    "idempotency": "non_idempotent",
                },
            },
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["job"]["operation"] == "submit"
