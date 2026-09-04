from __future__ import annotations

import hashlib
from collections.abc import Sequence
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from secrets import token_urlsafe
from typing import Any, Protocol

from operant.domain.remote_execution import (
    CapabilityActionReceipt,
    CapabilityActionRequest,
    CapabilityObservation,
    RemoteActionIdempotency,
    RemoteCapability,
    RemoteExecutionJob,
    RemoteExecutionResult,
    RemoteJobStatus,
    RemoteTargetLease,
    RemoteTargetRegistration,
)
from operant.domain.security import ActionRequest, Capability
from operant.persistence.remote_execution import SQLiteRemoteExecutionRepository
from operant.persistence.sqlite import ConflictError
from operant.protocol import canonical_action_hash
from operant.remote.connector import RemoteOutcomeUnknown, RemoteTargetConnector


def _matches_evidence(actual: Any, expected: Any) -> bool:
    """Require every declared pre/postcondition field in authoritative evidence."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _matches_evidence(actual[key], value)
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        return isinstance(actual, list) and actual == expected
    return bool(actual == expected)


class RemoteAuthorization(Protocol):
    def authorize(
        self,
        *,
        tool: str,
        operation: str,
        target_id: str,
        arguments: dict[str, Any],
        capabilities: Sequence[Capability],
        idempotency_key: str,
    ) -> ActionRequest: ...


class RemoteExecutionController:
    def __init__(
        self,
        repository: SQLiteRemoteExecutionRepository,
        authorization: RemoteAuthorization,
        *,
        max_lease_seconds: int = 300,
        observation_ttl_seconds: int = 60,
        max_artifact_bytes: int = 16_777_216,
    ) -> None:
        self.repository = repository
        self.authorization = authorization
        self.max_lease_seconds = max_lease_seconds
        self.observation_ttl_seconds = observation_ttl_seconds
        self.max_artifact_bytes = max_artifact_bytes

    @staticmethod
    def _security_capability(capability: RemoteCapability) -> Capability:
        return Capability(capability.value)

    def register_target(self, target: RemoteTargetRegistration) -> RemoteTargetRegistration:
        self.authorization.authorize(
            tool="remote_target",
            operation="register",
            target_id=target.target_id,
            arguments={
                "endpoint_ref": target.endpoint_ref,
                "credential_ref": target.credential_ref,
                "policy_ref": target.policy_ref,
                "artifact_namespace": target.artifact_namespace,
                "manifest_sha256": canonical_action_hash(
                    target.capability_manifest.model_dump(mode="json")
                ),
            },
            capabilities=(Capability.REMOTE_TARGET_EXEC,),
            idempotency_key=f"remote-target:register:{target.target_id}",
        )
        return self.repository.register_target(target)

    def heartbeat_target(
        self,
        target_id: str,
        *,
        identity_public_key: str,
        now: datetime,
    ) -> RemoteTargetRegistration:
        self.authorization.authorize(
            tool="remote_target",
            operation="heartbeat",
            target_id=target_id,
            arguments={
                "identity_key_sha256": hashlib.sha256(identity_public_key.encode()).hexdigest()
            },
            capabilities=(Capability.REMOTE_TARGET_EXEC,),
            idempotency_key=f"remote-target:heartbeat:{target_id}:{int(now.timestamp())}",
        )
        return self.repository.heartbeat_target(
            target_id, identity_public_key=identity_public_key, now=now
        )

    def acquire_lease(
        self,
        target_id: str,
        *,
        owner: str,
        workspace_ref: str,
        ttl_seconds: int,
        now: datetime,
        idempotency_key: str,
    ) -> RemoteTargetLease:
        if not 1 <= ttl_seconds <= self.max_lease_seconds:
            raise ValueError("remote target lease TTL is out of bounds")
        self.authorization.authorize(
            tool="remote_target",
            operation="acquire_lease",
            target_id=target_id,
            arguments={"owner": owner, "workspace_ref": workspace_ref, "ttl_seconds": ttl_seconds},
            capabilities=(Capability.REMOTE_TARGET_EXEC,),
            idempotency_key=idempotency_key,
        )
        # A new fenced owner must not inherit stale concurrency usage. Running
        # non-idempotent work is closed conservatively and is never replayed.
        self.repository.reconcile_expired(now=now)
        lease = RemoteTargetLease(
            target_id=target_id,
            owner=owner,
            token=token_urlsafe(32),
            fencing=1,
            workspace_ref=workspace_ref,
            expires_at=now.astimezone(timezone.utc) + timedelta(seconds=ttl_seconds),
        )
        return self.repository.acquire_lease(lease, now=now)

    def renew_lease(
        self,
        *,
        target_id: str,
        lease_id: str,
        token: str,
        fencing: int,
        ttl_seconds: int,
        now: datetime,
    ) -> RemoteTargetLease:
        if not 1 <= ttl_seconds <= self.max_lease_seconds:
            raise ValueError("remote target lease TTL is out of bounds")
        self.authorization.authorize(
            tool="remote_target",
            operation="renew_lease",
            target_id=target_id,
            arguments={"lease_id": lease_id, "fencing": fencing, "ttl_seconds": ttl_seconds},
            capabilities=(Capability.REMOTE_TARGET_EXEC,),
            idempotency_key=f"remote-target:renew:{lease_id}:{fencing}:{int(now.timestamp())}",
        )
        return self.repository.renew_lease(
            target_id=target_id,
            lease_id=lease_id,
            token=token,
            fencing=fencing,
            now=now,
            expires_at=now.astimezone(timezone.utc) + timedelta(seconds=ttl_seconds),
        )

    def release_lease(
        self,
        *,
        target_id: str,
        lease_id: str,
        token: str,
        fencing: int,
        now: datetime,
        idempotency_key: str,
    ) -> RemoteTargetLease:
        self.authorization.authorize(
            tool="remote_target",
            operation="release_lease",
            target_id=target_id,
            arguments={"lease_id": lease_id, "fencing": fencing},
            capabilities=(Capability.REMOTE_TARGET_EXEC,),
            idempotency_key=idempotency_key,
        )
        return self.repository.release_lease(
            target_id=target_id,
            lease_id=lease_id,
            token=token,
            fencing=fencing,
            now=now,
        )

    def create_job(
        self,
        *,
        target_id: str,
        lease_id: str,
        lease_token: str,
        lease_fencing: int,
        capability: RemoteCapability,
        operation: str,
        arguments: dict[str, Any],
        idempotency_key: str,
        idempotency: RemoteActionIdempotency,
        now: datetime,
        extra_capabilities: Sequence[Capability] = (),
    ) -> RemoteExecutionJob:
        security_capability = self._security_capability(capability)
        action = self.authorization.authorize(
            tool="remote_target_job",
            operation=operation,
            target_id=target_id,
            arguments={
                "target_id": target_id,
                "lease_id": lease_id,
                "lease_fencing": lease_fencing,
                "capability": capability.value,
                "arguments": arguments,
            },
            capabilities=tuple(
                dict.fromkeys(
                    (Capability.REMOTE_TARGET_EXEC, security_capability, *extra_capabilities)
                )
            ),
            idempotency_key=idempotency_key,
        )
        job = RemoteExecutionJob(
            target_id=target_id,
            lease_id=lease_id,
            lease_fencing=lease_fencing,
            capability=capability,
            operation=operation,
            arguments=arguments,
            action_hash=action.action_hash,
            idempotency_key=idempotency_key,
            idempotency=idempotency,
            created_at=now,
        )
        return self.repository.create_job(job, lease_token=lease_token, now=now)

    def observe(
        self,
        *,
        kind: str,
        target_id: str,
        lease_id: str,
        lease_token: str,
        lease_fencing: int,
        target_ref: str,
        idempotency_key: str,
        now: datetime,
    ) -> RemoteExecutionJob:
        if kind not in {"browser", "computer"}:
            raise ValueError("observation kind must be browser or computer")
        capability = (
            RemoteCapability.BROWSER_OBSERVE
            if kind == "browser"
            else RemoteCapability.COMPUTER_OBSERVE
        )
        return self.create_job(
            target_id=target_id,
            lease_id=lease_id,
            lease_token=lease_token,
            lease_fencing=lease_fencing,
            capability=capability,
            operation=f"observe_{kind}",
            arguments={"target_ref": target_ref},
            idempotency_key=idempotency_key,
            idempotency=RemoteActionIdempotency.IDEMPOTENT,
            now=now,
        )

    def act(
        self,
        request: CapabilityActionRequest,
        *,
        kind: str,
        lease_id: str,
        lease_token: str,
        lease_fencing: int,
        now: datetime,
    ) -> tuple[RemoteExecutionJob, CapabilityActionReceipt]:
        if kind not in {"browser", "computer"}:
            raise ValueError("action kind must be browser or computer")
        expected = (
            {RemoteCapability.BROWSER_NAVIGATE, RemoteCapability.BROWSER_SUBMIT}
            if kind == "browser"
            else {
                RemoteCapability.COMPUTER_INPUT,
                RemoteCapability.CLIPBOARD_READ,
                RemoteCapability.CLIPBOARD_WRITE,
            }
        )
        if request.capability not in expected:
            raise ValueError(f"invalid {kind} capability")
        observation = self.repository.get_observation(request.target_id, request.observation_hash)
        if observation.target_ref != request.target_ref:
            raise ConflictError("observation target binding does not match")
        if observation.expires_at <= now.astimezone(timezone.utc):
            raise ConflictError("observation is expired; observe again before acting")
        if not _matches_evidence(observation.body, request.precondition):
            raise ConflictError("action precondition does not match the bound observation")
        arguments = {
            "target_ref": request.target_ref,
            "observation_hash": request.observation_hash,
            "precondition": request.precondition,
            "arguments": request.arguments,
            "postcondition": request.postcondition,
        }
        job = self.create_job(
            target_id=request.target_id,
            lease_id=lease_id,
            lease_token=lease_token,
            lease_fencing=lease_fencing,
            capability=request.capability,
            operation=request.operation,
            arguments=arguments,
            idempotency_key=request.idempotency_key,
            idempotency=request.idempotency,
            now=now,
        )
        receipt = self.repository.reserve_action_receipt(
            CapabilityActionReceipt(
                target_id=request.target_id,
                capability=request.capability,
                action_hash=job.action_hash,
                observation_hash=request.observation_hash,
                status=RemoteJobStatus.QUEUED,
                created_at=now,
            )
        )
        return job, receipt

    def complete_job(
        self,
        result: RemoteExecutionResult,
        *,
        target_id: str,
        lease_id: str,
        token: str,
        fencing: int,
        now: datetime,
        artifact_bytes: bytes | None = None,
    ) -> RemoteExecutionResult:
        if result.artifact_sha256 is not None:
            if artifact_bytes is None:
                raise ValueError("artifact bytes are required for checksum verification")
            if len(artifact_bytes) > self.max_artifact_bytes:
                raise ValueError("remote artifact exceeds the configured size limit")
            actual = hashlib.sha256(artifact_bytes).hexdigest()
            if actual != result.artifact_sha256:
                raise ConflictError("remote artifact checksum mismatch")
        elif artifact_bytes is not None:
            raise ValueError("artifact bytes require artifact_ref and artifact_sha256")
        job = self.repository.get_job(result.job_id)
        observation_body: dict[str, Any] | None = None
        observation_target_ref: str | None = None
        if (
            job.capability
            in {
                RemoteCapability.BROWSER_OBSERVE,
                RemoteCapability.COMPUTER_OBSERVE,
            }
            and result.status is RemoteJobStatus.SUCCEEDED
        ):
            candidate_body = result.postcondition.get("observation")
            candidate_target_ref = result.postcondition.get("target_ref")
            if not isinstance(candidate_body, dict) or not isinstance(candidate_target_ref, str):
                raise ConflictError("successful observation result lacks observation evidence")
            if candidate_target_ref != job.arguments.get("target_ref"):
                raise ConflictError("observation result changed the requested target binding")
            observation_body = candidate_body
            observation_target_ref = candidate_target_ref
        elif result.status is RemoteJobStatus.SUCCEEDED and job.capability not in {
            RemoteCapability.TARGET_READ,
            RemoteCapability.TARGET_EXEC,
        }:
            expected_postcondition = job.arguments.get("postcondition", {})
            if not _matches_evidence(result.postcondition, expected_postcondition):
                result = result.model_copy(
                    update={
                        "status": RemoteJobStatus.FAILED,
                        "error_code": "remote.postcondition_failed",
                    }
                )
        completed = self.repository.complete_job(
            result,
            target_id=target_id,
            lease_id=lease_id,
            token=token,
            fencing=fencing,
            now=now,
        )
        if observation_body is not None and observation_target_ref is not None:
            observation_hash = canonical_action_hash(
                {
                    "target_id": target_id,
                    "target_ref": observation_target_ref,
                    "body": observation_body,
                }
            )
            self.repository.record_observation(
                CapabilityObservation(
                    target_id=target_id,
                    capability=job.capability,
                    target_ref=observation_target_ref,
                    observation_hash=observation_hash,
                    body=observation_body,
                    artifact_ref=result.artifact_ref,
                    created_at=now,
                    expires_at=now.astimezone(timezone.utc)
                    + timedelta(seconds=self.observation_ttl_seconds),
                )
            )
        elif job.capability not in {
            RemoteCapability.TARGET_READ,
            RemoteCapability.TARGET_EXEC,
        }:
            with suppress(KeyError):
                self.repository.complete_action_receipt(
                    job.action_hash,
                    status=result.status,
                    result=result.postcondition,
                    error_code=result.error_code,
                    now=now,
                )
        return completed

    def poll_jobs(
        self,
        *,
        target_id: str,
        lease_id: str,
        token: str,
        fencing: int,
        limit: int,
        now: datetime,
        idempotency_key: str,
    ) -> tuple[RemoteExecutionJob, ...]:
        self.authorization.authorize(
            tool="remote_target_connector",
            operation="poll",
            target_id=target_id,
            arguments={"lease_id": lease_id, "fencing": fencing, "limit": limit},
            capabilities=(Capability.REMOTE_TARGET_EXEC,),
            idempotency_key=idempotency_key,
        )
        return self.repository.poll_jobs(
            target_id=target_id,
            lease_id=lease_id,
            token=token,
            fencing=fencing,
            now=now,
            limit=limit,
        )

    def cancel_job(self, job_id: str, *, now: datetime, idempotency_key: str) -> RemoteExecutionJob:
        job = self.repository.get_job(job_id)
        self.authorization.authorize(
            tool="remote_target_job",
            operation="cancel",
            target_id=job.target_id,
            arguments={"job_id": job_id, "lease_fencing": job.lease_fencing},
            capabilities=(Capability.REMOTE_TARGET_EXEC,),
            idempotency_key=idempotency_key,
        )
        return self.repository.cancel_job(job_id, now=now)

    def dispatch_available(
        self,
        connector: RemoteTargetConnector,
        *,
        now: datetime,
        limit: int = 8,
    ) -> tuple[RemoteExecutionResult, ...]:
        jobs = self.repository.poll_jobs(
            target_id=connector.target_id,
            lease_id=connector.lease_id,
            token=connector.lease_token,
            fencing=connector.lease_fencing,
            now=now,
            limit=limit,
        )
        results: list[RemoteExecutionResult] = []
        for job in jobs:
            try:
                outcome = connector.execute(job)
                result = self.complete_job(
                    outcome.result,
                    target_id=connector.target_id,
                    lease_id=connector.lease_id,
                    token=connector.lease_token,
                    fencing=connector.lease_fencing,
                    now=now,
                    artifact_bytes=outcome.artifact_bytes,
                )
            except RemoteOutcomeUnknown:
                status = (
                    RemoteJobStatus.MANUAL_RECONCILE_REQUIRED
                    if job.idempotency is RemoteActionIdempotency.NON_IDEMPOTENT
                    else RemoteJobStatus.FAILED
                )
                result = self.complete_job(
                    RemoteExecutionResult(
                        job_id=job.job_id,
                        result_idempotency_key=f"connector-unknown:{job.job_id}",
                        status=status,
                        error_code="remote.outcome_unknown",
                        completed_at=now,
                    ),
                    target_id=connector.target_id,
                    lease_id=connector.lease_id,
                    token=connector.lease_token,
                    fencing=connector.lease_fencing,
                    now=now,
                )
            results.append(result)
        return tuple(results)
