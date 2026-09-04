from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from operant.api_phase56_target import install_phase56_target_routes
from operant.application.phase45_gateway import Phase45ActionGateway
from operant.application.remote_execution import RemoteAuthorization, RemoteExecutionController
from operant.application.security import PolicyEngine
from operant.domain.remote_execution import (
    CapabilityActionRequest,
    CapabilityManifest,
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
)
from operant.persistence.phase45 import SQLitePhase45Repository
from operant.persistence.remote_execution import SQLiteRemoteExecutionRepository
from operant.persistence.security import SQLiteSecurityRepository
from operant.persistence.sqlite import ConflictError, SQLiteStore
from operant.protocol import canonical_action_hash
from operant.remote import ConnectorOutcome, InMemoryRemoteTargetConnector, RemoteOutcomeUnknown


def _now() -> datetime:
    return datetime(2026, 9, 4, 6, 0, tzinfo=timezone.utc)


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
    controller, _store = _controller(tmp_path)
    target, lease = _online_lease(controller)
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
        "releaseRemoteTargetLease",
        "createRemoteTargetJob",
        "pollRemoteTargetJobs",
        "completeRemoteTargetJob",
        "cancelRemoteTargetJob",
        "listRemoteTargetJobs",
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
