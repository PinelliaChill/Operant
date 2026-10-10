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
    ObservationExpiredError,
    ObservationUnavailableError,
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
from operant.persistence.remote_execution import ObservationSource, SQLiteRemoteExecutionRepository
from operant.persistence.security import SQLiteSecurityRepository
from operant.persistence.sqlite import ConflictError, SQLiteStore
from operant.protocol import canonical_action_hash
from operant.remote import ConnectorOutcome, InMemoryRemoteTargetConnector, RemoteOutcomeUnknown
from operant.remote.target_cli import dispatch as dispatch_remote_target
from operant.remote.target_service import TargetService, TargetServiceConfig


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


def test_target_live_authority_rejects_released_lease_and_changed_job(tmp_path: Path) -> None:
    controller, store = _controller(tmp_path)
    at = datetime.now(timezone.utc)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "safe.txt").write_text("safe content", encoding="utf-8")
    target = _target().model_copy(
        update={
            "capability_manifest": CapabilityManifest(
                version="1",
                capabilities=(RemoteCapability.TARGET_READ,),
                supported_operations=("read_text",),
                platform="test",
            )
        }
    )
    controller.register_target(target)
    controller.heartbeat_target(
        target.target_id, identity_public_key=target.identity_public_key, now=at
    )
    lease = controller.acquire_lease(
        target.target_id,
        owner="test-worker",
        workspace_ref=str(workspace),
        ttl_seconds=120,
        now=at,
        idempotency_key="lease-live-check",
    )
    job = controller.create_job(
        target_id=target.target_id,
        lease_id=lease.lease_id,
        lease_token=lease.token,
        lease_fencing=lease.fencing,
        capability=RemoteCapability.TARGET_READ,
        operation="read_text",
        arguments={"path": "safe.txt"},
        idempotency_key="read-live-check",
        idempotency=RemoteActionIdempotency.IDEMPOTENT,
        now=at,
    )
    claimed = controller.poll_jobs(
        target_id=target.target_id,
        lease_id=lease.lease_id,
        token=lease.token,
        fencing=lease.fencing,
        limit=1,
        now=at,
        idempotency_key="poll-live-check",
    )[0]
    core_app = FastAPI()
    gateway = Phase45ActionGateway(
        SQLiteSecurityRepository(store),
        SQLitePhase45Repository(store),
        _allow_engine(),
        principal="test:core",
    )
    install_phase56_target_routes(core_app, store, action_gateway=gateway)
    verify_body = {
        "lease_id": lease.lease_id,
        "token": lease.token,
        "fencing": lease.fencing,
        "workspace_ref": str(workspace),
        "job": claimed.model_dump(mode="json"),
    }
    with TestClient(core_app) as core_client:
        checked = core_client.post(
            f"/v1/remote-targets/{target.target_id}/leases/verify", json=verify_body
        )
    assert checked.status_code == 200, checked.text
    assert checked.json()["valid"] is True
    assert checked.headers["cache-control"] == "no-store"
    ca = tmp_path / "ca.pem"
    ca.write_text("test", encoding="utf-8")
    worker = TargetService(
        TargetServiceConfig(
            target_id=target.target_id,
            lease_id=lease.lease_id,
            lease_token=lease.token,
            lease_fencing=lease.fencing,
            lease_expires_at=lease.expires_at,
            bearer_token="b" * 32,
            signing_private_key=b"k" * 32,
            workspace=workspace,
            ledger_path=tmp_path / "worker.sqlite3",
            allowed_argv=(),
            core_origin="https://127.0.0.1:18805",
            core_ca_file=ca,
        )
    )
    worker.verify_core_job = lambda incoming, purpose="execute": (
        controller.repository.verify_job_execution(  # type: ignore[method-assign]
            job=incoming,
            token=lease.token,
            workspace_ref=str(workspace),
            purpose=purpose,
            now=at,
        )
    )
    with pytest.raises(ConflictError, match="changed"):
        worker.execute(claimed.model_copy(update={"arguments": {"path": ".env"}}))
    assert worker.execute(claimed).postcondition["content"] == "safe content"
    with pytest.raises(ConflictError, match="stale or changed"):
        worker.cancel(job.job_id)
    controller.cancel_job(job.job_id, now=at, idempotency_key="cancel-live-check")
    assert worker.cancel(job.job_id)["status"] == "cancel_requested"
    controller.release_lease(
        target_id=target.target_id,
        lease_id=lease.lease_id,
        token=lease.token,
        fencing=lease.fencing,
        now=at,
        idempotency_key="release-live-check",
    )
    with pytest.raises(ConflictError, match="stale"):
        worker.execute(claimed)
    with pytest.raises(ConflictError, match="stale"):
        worker.cancel(job.job_id)
    with TestClient(core_app) as core_client:
        denied = core_client.post(
            f"/v1/remote-targets/{target.target_id}/leases/verify", json=verify_body
        )
    assert denied.status_code == 409
    assert controller.repository.get_job(job.job_id).job_id == job.job_id


def test_target_protected_read_and_output_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").write_text("SECRET=private", encoding="utf-8")
    (workspace / "safe.txt").write_text("Bearer abcdefghijklmnopqrstuvwxyz", encoding="utf-8")
    ca = tmp_path / "ca.pem"
    ca.write_text("test", encoding="utf-8")
    python = Path(__import__("sys").executable)
    argv = (str(python), "-c", "print('x' * 1000000)")
    worker = TargetService(
        TargetServiceConfig(
            target_id="target-test",
            lease_id="lease-test",
            lease_token="l" * 32,
            lease_fencing=1,
            lease_expires_at=_now(),
            bearer_token="b" * 32,
            signing_private_key=b"k" * 32,
            workspace=workspace,
            ledger_path=tmp_path / "worker.sqlite3",
            allowed_argv=(argv,),
            core_origin="https://127.0.0.1:18805",
            core_ca_file=ca,
        )
    )
    base = dict(
        target_id="target-test",
        lease_id="lease-test",
        lease_fencing=1,
        action_hash="a" * 64,
        idempotency_key="protected-test",
        idempotency=RemoteActionIdempotency.NON_IDEMPOTENT,
    )
    from operant.domain.remote_execution import RemoteExecutionJob

    protected = RemoteExecutionJob(
        **base,
        capability=RemoteCapability.TARGET_READ,
        operation="read_text",
        arguments={"path": ".env"},
    )
    assert worker._perform(protected).error_code == "remote.invalid_path"
    safe = protected.model_copy(update={"arguments": {"path": "safe.txt"}})
    assert "abcdefghijklmnopqrstuvwxyz" not in worker._perform(safe).postcondition["content"]
    noisy = protected.model_copy(
        update={
            "capability": RemoteCapability.TARGET_EXEC,
            "operation": "run_allowlisted",
            "arguments": {"argv": list(argv)},
        }
    )
    assert worker._perform(noisy).status is RemoteJobStatus.MANUAL_RECONCILE_REQUIRED

    def configured_only(command: tuple[str, ...], **_kwargs: Any) -> Any:
        assert command is worker.config.allowed_argv[0]

        class FinishedProcess:
            returncode = 0

        return FinishedProcess()

    monkeypatch.setattr("operant.remote.target_service.subprocess.Popen", configured_only)
    monkeypatch.setattr(worker, "_collect_bounded", lambda _process: (b"ok", b""))
    denied = noisy.model_copy(update={"arguments": {"argv": [*argv, "extra"]}})
    assert worker._perform(denied).error_code == "remote.argv_denied"
    assert worker._perform(noisy).postcondition["stdout"] == "ok"


@pytest.mark.parametrize("connection_lost", [False, True])
def test_dispatch_release_during_execution_never_records_success(
    tmp_path: Path, connection_lost: bool
) -> None:
    controller, store = _controller(tmp_path)
    target, lease = _online_lease(controller)
    job = controller.create_job(
        target_id=target.target_id,
        lease_id=lease.lease_id,
        lease_token=lease.token,
        lease_fencing=lease.fencing,
        capability=RemoteCapability.TARGET_EXEC,
        operation="run",
        arguments={"argv": ["/bin/true"]},
        idempotency_key="lease-race-job",
        idempotency=RemoteActionIdempotency.NON_IDEMPOTENT,
        now=_now(),
    )

    class ReleasingConnector:
        target_id = target.target_id
        lease_id = lease.lease_id
        lease_token = lease.token
        lease_fencing = lease.fencing

        def execute(self, incoming: Any) -> ConnectorOutcome:
            controller.repository.release_lease(
                target_id=target.target_id,
                lease_id=lease.lease_id,
                token=lease.token,
                fencing=lease.fencing,
                now=_now(),
            )
            if connection_lost:
                raise RemoteOutcomeUnknown("SSH return path closed after execution")
            return ConnectorOutcome(
                RemoteExecutionResult(
                    job_id=incoming.job_id,
                    result_idempotency_key="release-race-result",
                    status=RemoteJobStatus.SUCCEEDED,
                )
            )

        def cancel(self, job_id: str) -> None:
            raise AssertionError(job_id)

    [result] = controller.dispatch_available(ReleasingConnector(), now=_now())
    assert result.status is RemoteJobStatus.MANUAL_RECONCILE_REQUIRED
    assert (
        controller.repository.get_job(job.job_id).status
        is RemoteJobStatus.MANUAL_RECONCILE_REQUIRED
    )
    persisted = controller.repository.get_result(job.job_id)
    assert persisted == result
    assert persisted is not None
    assert persisted.result_idempotency_key == f"core-lease-invalidated:{job.job_id}"
    assert persisted.error_code == "remote.lease_invalidated_outcome_unknown"
    assert persisted.postcondition == {}
    assert persisted.artifact_ref is None
    gateway = Phase45ActionGateway(
        SQLiteSecurityRepository(store),
        SQLitePhase45Repository(store),
        _allow_engine(),
        principal="test:core",
    )
    app = FastAPI()
    install_phase56_target_routes(app, store, action_gateway=gateway)
    with TestClient(app) as client:
        projected = client.get(f"/v1/remote-targets/jobs/{job.job_id}/result")
    assert projected.status_code == 200
    assert projected.json()["status"] == result.status.value
    assert projected.json()["result"] == result.model_dump(mode="json")
    with pytest.raises(ConflictError, match="stale"):
        controller.complete_job(
            RemoteExecutionResult(
                job_id=job.job_id,
                result_idempotency_key="late-target-success",
                status=RemoteJobStatus.SUCCEEDED,
            ),
            target_id=target.target_id,
            lease_id=lease.lease_id,
            token=lease.token,
            fencing=lease.fencing,
            now=_now(),
        )
    with pytest.raises(ConflictError, match="stale"):
        controller.repository.poll_jobs(
            target_id=target.target_id,
            lease_id=lease.lease_id,
            token=lease.token,
            fencing=lease.fencing,
            now=_now(),
            limit=1,
        )
    assert controller.repository.get_result(job.job_id) == persisted


def test_dispatch_cli_never_runs_core_startup_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SQLiteStore(tmp_path / "live.sqlite3")
    store.initialize()
    ca = tmp_path / "ca.pem"
    ca.write_text("test", encoding="utf-8")

    def forbidden_initialize(_store: SQLiteStore) -> None:
        raise AssertionError("dispatch must not run Core startup recovery")

    monkeypatch.setattr(SQLiteStore, "initialize", forbidden_initialize)
    with pytest.raises(KeyError):
        dispatch_remote_target(
            target_id="missing-target",
            lease_id="missing-lease",
            fencing=1,
            db_path=store.path,
            ca_file=ca,
        )


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
    jobs_before_expired = len(controller.repository.list_jobs(target_id=target.target_id))
    with pytest.raises(ObservationExpiredError, match="expired"):
        controller.act(
            request,
            kind="browser",
            lease_id=lease.lease_id,
            lease_token=lease.token,
            lease_fencing=lease.fencing,
            now=later,
        )
    assert len(controller.repository.list_jobs(target_id=target.target_id)) == jobs_before_expired
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
        now=later + timedelta(milliseconds=1),
    )
    assert (
        direct_job.arguments["observation_content_hash"]
        == new_job.arguments["observation_content_hash"]
    )
    [claimed] = controller.poll_jobs(
        target_id=target.target_id,
        lease_id=lease.lease_id,
        token=lease.token,
        fencing=lease.fencing,
        limit=1,
        now=later,
        idempotency_key="invalid-post-hash-poll",
    )
    assert claimed.job_id == new_job.job_id
    invalid_post = controller.complete_job(
        RemoteExecutionResult(
            job_id=new_job.job_id,
            result_idempotency_key="invalid-post-hash-result",
            status=RemoteJobStatus.SUCCEEDED,
            postcondition={
                "url": "https://example.test/next",
                "title": "Synthetic",
                "text": "Ready",
                "form_state_sha256": "1" * 64,
                "elements": [],
                "fields": [],
                "active_selector": None,
                "pre_observation_hash": fresh_hash,
                "post_observation_hash": "0" * 64,
            },
        ),
        target_id=target.target_id,
        lease_id=lease.lease_id,
        token=lease.token,
        fencing=lease.fencing,
        now=later,
    )
    assert invalid_post.status is RemoteJobStatus.MANUAL_RECONCILE_REQUIRED
    assert invalid_post.error_code == "remote.post_observation_invalid"
    with pytest.raises(KeyError):
        controller.repository.get_observation(target.target_id, "0" * 64)


def test_local_observation_provenance_rejects_missing_forged_and_changed_scope(
    tmp_path: Path,
) -> None:
    controller, store = _controller(tmp_path)
    target = controller.register_target(
        _target().model_copy(update={"policy_ref": "local-control:test-digest"})
    )
    controller.heartbeat_target(
        target.target_id, identity_public_key=target.identity_public_key, now=_now()
    )
    lease = controller.acquire_lease(
        target.target_id,
        owner="local-control:provenance",
        workspace_ref="local-control",
        ttl_seconds=120,
        now=_now(),
        idempotency_key="provenance-lease",
    )
    body = {"url": "https://example.test/form"}

    def observed(key: str) -> tuple[str, CapabilityObservation, str]:
        job = controller.observe(
            kind="browser",
            target_id=target.target_id,
            lease_id=lease.lease_id,
            lease_token=lease.token,
            lease_fencing=lease.fencing,
            target_ref=target.target_id,
            idempotency_key=key,
            now=_now(),
        )
        [claimed] = controller.poll_jobs(
            target_id=target.target_id,
            lease_id=lease.lease_id,
            token=lease.token,
            fencing=lease.fencing,
            limit=1,
            now=_now(),
            idempotency_key=f"poll:{key}",
        )
        assert claimed.job_id == job.job_id
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
            now=_now(),
        )
        observed_hash = observation_hash_for_job(job, target_ref=target.target_id, body=body)
        return (
            observed_hash,
            controller.repository.get_observation(target.target_id, observed_hash),
            job.job_id,
        )

    first_hash, first, first_job = observed("provenance-first")
    second_hash, _, second_job = observed("provenance-second")
    assert first_hash != second_hash
    source = controller.repository.get_observation_source(first.observation_id)
    assert source is not None and source.source_job_id == first_job
    request = CapabilityActionRequest(
        target_id=target.target_id,
        capability=RemoteCapability.BROWSER_NAVIGATE,
        operation="navigate",
        target_ref=target.target_id,
        observation_hash=first_hash,
        arguments={"url": "https://example.test/next"},
        idempotency_key="provenance-check",
        idempotency=RemoteActionIdempotency.NON_IDEMPOTENT,
    )

    def must_reject(key: str) -> None:
        with pytest.raises(ObservationUnavailableError):
            controller.act(
                request.model_copy(update={"idempotency_key": key}),
                kind="browser",
                lease_id=lease.lease_id,
                lease_token=lease.token,
                lease_fencing=lease.fencing,
                now=_now(),
            )
        assert len(controller.repository.list_jobs(target_id=target.target_id)) == 2

    with store._connect() as connection:
        connection.execute(
            "UPDATE capability_observation_sources SET scope_digest=? WHERE observation_id=?",
            ("0" * 64, first.observation_id),
        )
    must_reject("forged-scope")
    with store._connect() as connection:
        connection.execute(
            "UPDATE capability_observation_sources SET scope_digest=?,source_job_id=? "
            "WHERE observation_id=?",
            (source.scope_digest, second_job, first.observation_id),
        )
    must_reject("forged-source")
    with store._connect() as connection:
        connection.execute(
            "DELETE FROM capability_observation_sources WHERE observation_id=?",
            (first.observation_id,),
        )
    must_reject("missing-source")

    forged_hash = "f" * 64
    with pytest.raises(ConflictError, match="source Job"):
        controller.repository.record_observation(
            CapabilityObservation(
                target_id=target.target_id,
                capability=RemoteCapability.BROWSER_OBSERVE,
                target_ref=target.target_id,
                observation_hash=forged_hash,
                body=body,
                created_at=_now(),
                expires_at=_now() + timedelta(seconds=60),
            ),
            source=ObservationSource(
                source_job_id="remote_job_missing",
                lease_id=lease.lease_id,
                lease_fencing=lease.fencing,
                scope_digest=source.scope_digest,
            ),
        )
    with pytest.raises(KeyError):
        controller.repository.get_observation(target.target_id, forged_hash)


def test_local_job_result_and_observation_source_roll_back_and_retry_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
        owner="local-control:atomic-result",
        workspace_ref="local-control",
        ttl_seconds=120,
        now=_now(),
        idempotency_key="atomic-observation-lease",
    )
    job = controller.observe(
        kind="browser",
        target_id=target.target_id,
        lease_id=lease.lease_id,
        lease_token=lease.token,
        lease_fencing=lease.fencing,
        target_ref=target.target_id,
        idempotency_key="atomic-observation-job",
        now=_now(),
    )
    [claimed] = controller.poll_jobs(
        target_id=target.target_id,
        lease_id=lease.lease_id,
        token=lease.token,
        fencing=lease.fencing,
        limit=1,
        now=_now(),
        idempotency_key="atomic-observation-poll",
    )
    assert claimed.job_id == job.job_id
    body = {"url": "https://example.test/ready"}
    result = RemoteExecutionResult(
        job_id=job.job_id,
        result_idempotency_key="atomic-observation-result",
        status=RemoteJobStatus.SUCCEEDED,
        postcondition={"target_ref": target.target_id, "observation": body},
    )
    observed_hash = observation_hash_for_job(job, target_ref=target.target_id, body=body)
    original = controller.repository._record_observation_in_connection

    def interrupted(*_args: Any, **_kwargs: Any) -> CapabilityObservation:
        raise ConflictError("synthetic observation transaction interruption")

    monkeypatch.setattr(controller.repository, "_record_observation_in_connection", interrupted)
    with pytest.raises(ConflictError, match="transaction interruption"):
        controller.complete_job(
            result,
            target_id=target.target_id,
            lease_id=lease.lease_id,
            token=lease.token,
            fencing=lease.fencing,
            now=_now(),
        )
    assert controller.repository.get_result(job.job_id) is None
    assert controller.repository.get_job(job.job_id).status is RemoteJobStatus.RUNNING
    with pytest.raises(KeyError):
        controller.repository.get_observation(target.target_id, observed_hash)

    monkeypatch.setattr(controller.repository, "_record_observation_in_connection", original)
    completed = controller.complete_job(
        result,
        target_id=target.target_id,
        lease_id=lease.lease_id,
        token=lease.token,
        fencing=lease.fencing,
        now=_now(),
    )
    observed = controller.repository.get_observation(target.target_id, observed_hash)
    assert completed.status is RemoteJobStatus.SUCCEEDED
    assert controller.repository.get_observation_source(observed.observation_id) is not None
    replay = controller.complete_job(
        result,
        target_id=target.target_id,
        lease_id=lease.lease_id,
        token=lease.token,
        fencing=lease.fencing,
        now=_now() + timedelta(seconds=1),
    )
    assert replay == completed
    assert controller.repository.get_observation(target.target_id, observed_hash) == observed


@pytest.mark.parametrize(
    "terminal",
    (
        RemoteJobStatus.FAILED,
        RemoteJobStatus.CANCELLED,
        RemoteJobStatus.MANUAL_RECONCILE_REQUIRED,
    ),
)
def test_local_non_successful_action_never_creates_post_observation(
    tmp_path: Path, terminal: RemoteJobStatus
) -> None:
    controller, store = _controller(tmp_path)
    target = controller.register_target(
        _target().model_copy(update={"policy_ref": "local-control:test-digest"})
    )
    controller.heartbeat_target(
        target.target_id, identity_public_key=target.identity_public_key, now=_now()
    )
    lease = controller.acquire_lease(
        target.target_id,
        owner="local-control:non-success",
        workspace_ref="local-control",
        ttl_seconds=120,
        now=_now(),
        idempotency_key="non-success-lease",
    )
    before = {"url": "https://example.test/start"}
    observed = controller.observe(
        kind="browser",
        target_id=target.target_id,
        lease_id=lease.lease_id,
        lease_token=lease.token,
        lease_fencing=lease.fencing,
        target_ref=target.target_id,
        idempotency_key="non-success-observe",
        now=_now(),
    )
    controller.poll_jobs(
        target_id=target.target_id,
        lease_id=lease.lease_id,
        token=lease.token,
        fencing=lease.fencing,
        limit=1,
        now=_now(),
        idempotency_key="non-success-observe-poll",
    )
    controller.complete_job(
        RemoteExecutionResult(
            job_id=observed.job_id,
            result_idempotency_key="non-success-observe-result",
            status=RemoteJobStatus.SUCCEEDED,
            postcondition={"target_ref": target.target_id, "observation": before},
        ),
        target_id=target.target_id,
        lease_id=lease.lease_id,
        token=lease.token,
        fencing=lease.fencing,
        now=_now(),
    )
    before_hash = observation_hash_for_job(observed, target_ref=target.target_id, body=before)
    job, _receipt = controller.act(
        CapabilityActionRequest(
            target_id=target.target_id,
            capability=RemoteCapability.BROWSER_NAVIGATE,
            operation="navigate",
            target_ref=target.target_id,
            observation_hash=before_hash,
            arguments={"url": "https://example.test/next"},
            idempotency_key=f"non-success-{terminal.value}",
            idempotency=RemoteActionIdempotency.NON_IDEMPOTENT,
        ),
        kind="browser",
        lease_id=lease.lease_id,
        lease_token=lease.token,
        lease_fencing=lease.fencing,
        now=_now(),
    )
    [claimed] = controller.poll_jobs(
        target_id=target.target_id,
        lease_id=lease.lease_id,
        token=lease.token,
        fencing=lease.fencing,
        limit=1,
        now=_now(),
        idempotency_key=f"non-success-poll-{terminal.value}",
    )
    assert claimed.job_id == job.job_id
    after = {"url": "https://example.test/next"}
    after_hash = canonical_action_hash(
        {"target_id": target.target_id, "target_ref": target.target_id, "body": after}
    )
    controller.complete_job(
        RemoteExecutionResult(
            job_id=job.job_id,
            result_idempotency_key=f"non-success-result-{terminal.value}",
            status=terminal,
            error_code="synthetic.non_success",
            postcondition={
                **after,
                "pre_observation_hash": before_hash,
                "post_observation_hash": after_hash,
            },
        ),
        target_id=target.target_id,
        lease_id=lease.lease_id,
        token=lease.token,
        fencing=lease.fencing,
        now=_now(),
    )
    with pytest.raises(KeyError):
        controller.repository.get_observation(target.target_id, after_hash)
    with store._connect() as connection:
        row = connection.execute(
            "SELECT status FROM capability_action_receipts WHERE action_hash=?",
            (job.action_hash,),
        ).fetchone()
    assert row is not None and row[0] == terminal.value


@pytest.mark.parametrize(
    ("capability", "operation", "kind"),
    (
        (RemoteCapability.BROWSER_NAVIGATE, "navigate", "browser"),
        (RemoteCapability.BROWSER_SUBMIT, "submit", "browser"),
        (RemoteCapability.COMPUTER_INPUT, "input", "computer"),
    ),
)
@pytest.mark.parametrize(
    "idempotency",
    (RemoteActionIdempotency.NON_IDEMPOTENT, RemoteActionIdempotency.IDEMPOTENT),
)
def test_local_success_with_mismatched_postcondition_preserves_unknown_write(
    tmp_path: Path,
    capability: RemoteCapability,
    operation: str,
    kind: str,
    idempotency: RemoteActionIdempotency,
) -> None:
    controller, store = _controller(tmp_path)
    target = controller.register_target(
        _target().model_copy(update={"policy_ref": "local-control:test-digest"})
    )
    controller.heartbeat_target(
        target.target_id, identity_public_key=target.identity_public_key, now=_now()
    )
    lease = controller.acquire_lease(
        target.target_id,
        owner="local-control:postcondition",
        workspace_ref="local-control",
        ttl_seconds=120,
        now=_now(),
        idempotency_key=f"postcondition-lease:{kind}:{idempotency.value}",
    )
    body = (
        {"url": "https://example.test/start"}
        if kind == "browser"
        else {"bundle_id": "com.example.Test"}
    )
    observe_job = controller.observe(
        kind=kind,
        target_id=target.target_id,
        lease_id=lease.lease_id,
        lease_token=lease.token,
        lease_fencing=lease.fencing,
        target_ref=target.target_id,
        idempotency_key="postcondition-observe",
        now=_now(),
    )
    [claimed_observe] = controller.poll_jobs(
        target_id=target.target_id,
        lease_id=lease.lease_id,
        token=lease.token,
        fencing=lease.fencing,
        limit=1,
        now=_now(),
        idempotency_key="postcondition-observe-poll",
    )
    assert claimed_observe.job_id == observe_job.job_id
    controller.complete_job(
        RemoteExecutionResult(
            job_id=observe_job.job_id,
            result_idempotency_key="postcondition-observe-result",
            status=RemoteJobStatus.SUCCEEDED,
            postcondition={"target_ref": target.target_id, "observation": body},
        ),
        target_id=target.target_id,
        lease_id=lease.lease_id,
        token=lease.token,
        fencing=lease.fencing,
        now=_now(),
    )
    observed_hash = observation_hash_for_job(observe_job, target_ref=target.target_id, body=body)
    action_job, _ = controller.act(
        CapabilityActionRequest(
            target_id=target.target_id,
            capability=capability,
            operation=operation,
            target_ref=target.target_id,
            observation_hash=observed_hash,
            arguments={"synthetic": True},
            postcondition={"confirmation": True},
            idempotency_key=f"postcondition-action:{operation}:{idempotency.value}",
            idempotency=idempotency,
        ),
        kind=kind,
        lease_id=lease.lease_id,
        lease_token=lease.token,
        lease_fencing=lease.fencing,
        now=_now(),
    )
    [claimed_action] = controller.poll_jobs(
        target_id=target.target_id,
        lease_id=lease.lease_id,
        token=lease.token,
        fencing=lease.fencing,
        limit=1,
        now=_now(),
        idempotency_key="postcondition-action-poll",
    )
    assert claimed_action.job_id == action_job.job_id
    completed = controller.complete_job(
        RemoteExecutionResult(
            job_id=action_job.job_id,
            result_idempotency_key="postcondition-action-result",
            status=RemoteJobStatus.SUCCEEDED,
            postcondition={"confirmation": False},
        ),
        target_id=target.target_id,
        lease_id=lease.lease_id,
        token=lease.token,
        fencing=lease.fencing,
        now=_now(),
    )
    uncertain = idempotency is RemoteActionIdempotency.NON_IDEMPOTENT
    expected_status = (
        RemoteJobStatus.MANUAL_RECONCILE_REQUIRED if uncertain else RemoteJobStatus.FAILED
    )
    assert completed.status is expected_status
    assert completed.error_code == (
        "remote.outcome_unknown" if uncertain else "remote.postcondition_failed"
    )
    assert controller.repository.get_job(action_job.job_id).status is expected_status
    assert controller.repository.get_result(action_job.job_id) == completed
    with store._connect() as connection:
        receipt = connection.execute(
            "SELECT status,error_code FROM capability_action_receipts WHERE action_hash=?",
            (action_job.action_hash,),
        ).fetchone()
        action_sources = connection.execute(
            "SELECT COUNT(*) FROM capability_observation_sources WHERE source_job_id=?",
            (action_job.job_id,),
        ).fetchone()[0]
    assert receipt is not None and tuple(receipt) == (expected_status.value, completed.error_code)
    assert action_sources == 0
    assert len(controller.repository.list_jobs(target_id=target.target_id)) == 2
    if uncertain:
        with pytest.raises(
            ConflictError, match="unknown remote action requires a new fenced lease"
        ):
            controller.poll_jobs(
                target_id=target.target_id,
                lease_id=lease.lease_id,
                token=lease.token,
                fencing=lease.fencing,
                limit=1,
                now=_now(),
                idempotency_key="never-replay-postcondition-unknown",
            )


def test_remote_job_commit_rechecks_lease_after_gateway_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, _store = _controller(tmp_path)
    target, lease = _online_lease(controller)
    authorize = controller.authorization.authorize

    def release_after_decision(**kwargs: Any) -> ActionRequest:
        action = authorize(**kwargs)
        controller.repository.release_lease(
            target_id=target.target_id,
            lease_id=lease.lease_id,
            token=lease.token,
            fencing=lease.fencing,
            now=_now(),
        )
        return action

    monkeypatch.setattr(controller.authorization, "authorize", release_after_decision)
    with pytest.raises(ConflictError, match="lease"):
        controller.create_job(
            target_id=target.target_id,
            lease_id=lease.lease_id,
            lease_token=lease.token,
            lease_fencing=lease.fencing,
            capability=RemoteCapability.TARGET_EXEC,
            operation="run",
            arguments={"step": "synthetic"},
            idempotency_key="lease-changed-after-gateway",
            idempotency=RemoteActionIdempotency.NON_IDEMPOTENT,
            now=_now(),
        )
    assert controller.repository.list_jobs(target_id=target.target_id) == ()


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
    result = controller.repository.get_result(job.job_id)
    assert result is not None
    assert result.status is RemoteJobStatus.MANUAL_RECONCILE_REQUIRED
    assert result.error_code == "remote.lease_invalidated_outcome_unknown"
    assert controller.repository.reconcile_expired(now=_now() + timedelta(seconds=122)) == ()
    assert controller.repository.get_result(job.job_id) == result
    with pytest.raises(ConflictError, match="stale"):
        controller.complete_job(
            RemoteExecutionResult(
                job_id=job.job_id,
                result_idempotency_key="late-expired-success",
                status=RemoteJobStatus.SUCCEEDED,
            ),
            target_id=target.target_id,
            lease_id=lease.lease_id,
            token=lease.token,
            fencing=lease.fencing,
            now=_now() + timedelta(seconds=122),
        )


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
