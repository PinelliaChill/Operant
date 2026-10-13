from __future__ import annotations

import hashlib
import time
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
from operant.persistence.remote_execution import ObservationSource, SQLiteRemoteExecutionRepository
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


_LOCAL_OBSERVATION_GENERATION = "job.v1"


class ObservationExpiredError(ConflictError):
    """A pre-dispatch action conflict that cannot have reached the Target."""


class ObservationUnavailableError(ConflictError):
    """The requested local observation is absent or belongs to another binding."""


def _local_scope_digest(target: RemoteTargetRegistration) -> str:
    return canonical_action_hash(
        {
            "target_id": target.target_id,
            "endpoint_ref": target.endpoint_ref,
            "identity_public_key": target.identity_public_key,
            "policy_ref": target.policy_ref,
            "capability_manifest": target.capability_manifest.model_dump(mode="json"),
        }
    )


def _post_action_observation_body(
    job: RemoteExecutionJob, result: RemoteExecutionResult
) -> dict[str, Any] | None:
    if job.capability in {RemoteCapability.BROWSER_NAVIGATE, RemoteCapability.BROWSER_SUBMIT}:
        required = {
            "url",
            "title",
            "text",
            "form_state_sha256",
            "elements",
            "fields",
            "active_selector",
        }
        allowed = required | {"query_sha256"}
        excluded = {"pre_observation_hash", "post_observation_hash"}
    elif job.capability in {RemoteCapability.COMPUTER_INPUT, RemoteCapability.CLIPBOARD_WRITE}:
        required = {
            "bundle_id",
            "window_title",
            "window_title_sha256",
            "buttons",
            "buttons_sha256",
            "fields",
            "fields_state_sha256",
        }
        allowed = required
        excluded = {"pre_observation_hash", "post_observation_hash"}
        if job.capability is RemoteCapability.CLIPBOARD_WRITE:
            excluded.add("clipboard_sha256")
    else:
        return None
    body = {key: value for key, value in result.postcondition.items() if key not in excluded}
    if set(body) - allowed or not required.issubset(body):
        return None
    if job.capability in {RemoteCapability.BROWSER_NAVIGATE, RemoteCapability.BROWSER_SUBMIT}:
        if not isinstance(body.get("url"), str) or not all(
            isinstance(body.get(key), list) for key in ("elements", "fields")
        ):
            return None
    elif not isinstance(body.get("bundle_id"), str) or not all(
        isinstance(body.get(key), list) for key in ("buttons", "fields")
    ):
        return None
    return body


def _post_action_observation_hash(
    job: RemoteExecutionJob, *, target_ref: str, body: dict[str, Any]
) -> str:
    # A new action Job is a new observation generation, even if the page or
    # window pixels are identical. This never extends a prior observation TTL.
    return canonical_action_hash(
        {
            "target_id": job.target_id,
            "target_ref": target_ref,
            "body": body,
            "post_action_job_id": job.job_id,
        }
    )


def observation_hash_for_job(
    job: RemoteExecutionJob, *, target_ref: str, body: dict[str, Any]
) -> str:
    """Bind local observations to one completed observe job, even if pixels are unchanged."""
    payload: dict[str, Any] = {
        "target_id": job.target_id,
        "target_ref": target_ref,
        "body": body,
    }
    if job.arguments.get("observation_generation") == _LOCAL_OBSERVATION_GENERATION:
        payload["observe_job_id"] = job.job_id
    return canonical_action_hash(payload)


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
        target = self.repository.get_target(target_id)
        if target.policy_ref.startswith("local-control:") and capability in {
            RemoteCapability.BROWSER_NAVIGATE,
            RemoteCapability.BROWSER_SUBMIT,
            RemoteCapability.BROWSER_SCREENSHOT,
            RemoteCapability.COMPUTER_INPUT,
            RemoteCapability.COMPUTER_SCREENSHOT,
            RemoteCapability.CLIPBOARD_READ,
            RemoteCapability.CLIPBOARD_WRITE,
        }:
            observation_hash = arguments.get("observation_hash")
            target_ref = arguments.get("target_ref")
            if not isinstance(observation_hash, str) or not isinstance(target_ref, str):
                raise ConflictError("local action requires a bound observation")
            try:
                observation = self.repository.get_observation(target_id, observation_hash)
            except KeyError as exc:
                raise ObservationUnavailableError("local observation is unavailable") from exc
            if observation.target_ref != target_ref:
                raise ConflictError("observation target binding does not match")
            if observation.expires_at <= now.astimezone(timezone.utc):
                raise ObservationExpiredError("observation is expired; observe again before acting")
            self._require_local_observation_source(
                observation,
                target=target,
                lease_id=lease_id,
                lease_fencing=lease_fencing,
            )
            arguments = {
                **arguments,
                "observation_content_hash": canonical_action_hash(
                    {
                        "target_id": observation.target_id,
                        "target_ref": observation.target_ref,
                        "body": observation.body,
                    }
                ),
            }
        # A stale/revoked Lease must not create a fresh Phase45 ASK. The
        # repository repeats this check inside create_job's write transaction.
        self.repository.assert_live_lease(
            target_id=target_id,
            lease_id=lease_id,
            token=lease_token,
            fencing=lease_fencing,
            now=now,
        )
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

    def _require_local_observation_source(
        self,
        observation: CapabilityObservation,
        *,
        target: RemoteTargetRegistration,
        lease_id: str,
        lease_fencing: int,
    ) -> None:
        source = self.repository.get_observation_source(observation.observation_id)
        if (
            source is None
            or source.lease_id != lease_id
            or source.lease_fencing != lease_fencing
            or source.scope_digest != _local_scope_digest(target)
        ):
            raise ObservationUnavailableError("local observation belongs to another lease or scope")
        try:
            source_job = self.repository.get_job(source.source_job_id)
            source_result = self.repository.get_result(source.source_job_id)
        except KeyError as exc:
            raise ObservationUnavailableError("local observation source is missing") from exc
        if (
            source_job.status is not RemoteJobStatus.SUCCEEDED
            or source_result is None
            or source_result.status is not RemoteJobStatus.SUCCEEDED
            or source_job.target_id != observation.target_id
            or source_job.lease_id != lease_id
            or source_job.lease_fencing != lease_fencing
            or source_job.capability is not observation.capability
            or source_job.arguments.get("target_ref") != observation.target_ref
            or source_job.finished_at != observation.created_at
        ):
            raise ObservationUnavailableError("local observation source changed")
        if source_job.capability in {
            RemoteCapability.BROWSER_OBSERVE,
            RemoteCapability.COMPUTER_OBSERVE,
        }:
            body = source_result.postcondition.get("observation")
            expected_hash = (
                observation_hash_for_job(source_job, target_ref=observation.target_ref, body=body)
                if isinstance(body, dict)
                else None
            )
        else:
            body = _post_action_observation_body(source_job, source_result)
            expected_hash = (
                _post_action_observation_hash(
                    source_job, target_ref=observation.target_ref, body=body
                )
                if body is not None
                else None
            )
            if source_result.postcondition.get("post_observation_hash") != expected_hash:
                raise ObservationUnavailableError("local post-action observation changed")
        if body != observation.body or expected_hash != observation.observation_hash:
            raise ObservationUnavailableError("local observation evidence changed")

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
        target = self.repository.get_target(target_id)
        focus_capabilities = (
            (Capability.COMPUTER_INPUT,)
            if kind == "computer" and target.policy_ref.startswith("local-control:")
            else ()
        )
        return self.create_job(
            target_id=target_id,
            lease_id=lease_id,
            lease_token=lease_token,
            lease_fencing=lease_fencing,
            capability=capability,
            operation=f"observe_{kind}",
            arguments={
                "target_ref": target_ref,
                **(
                    {"observation_generation": _LOCAL_OBSERVATION_GENERATION}
                    if target.policy_ref.startswith("local-control:")
                    else {}
                ),
            },
            idempotency_key=idempotency_key,
            idempotency=RemoteActionIdempotency.IDEMPOTENT,
            now=now,
            extra_capabilities=focus_capabilities,
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
            {
                RemoteCapability.BROWSER_NAVIGATE,
                RemoteCapability.BROWSER_SUBMIT,
                RemoteCapability.BROWSER_SCREENSHOT,
            }
            if kind == "browser"
            else {
                RemoteCapability.COMPUTER_INPUT,
                RemoteCapability.COMPUTER_SCREENSHOT,
                RemoteCapability.CLIPBOARD_READ,
                RemoteCapability.CLIPBOARD_WRITE,
            }
        )
        if request.capability not in expected:
            raise ValueError(f"invalid {kind} capability")
        try:
            observation = self.repository.get_observation(
                request.target_id, request.observation_hash
            )
        except KeyError as exc:
            raise ObservationUnavailableError("action observation is unavailable") from exc
        if observation.target_ref != request.target_ref:
            raise ConflictError("observation target binding does not match")
        if observation.expires_at <= now.astimezone(timezone.utc):
            raise ObservationExpiredError("observation is expired; observe again before acting")
        if not _matches_evidence(observation.body, request.precondition):
            raise ConflictError("action precondition does not match the bound observation")
        arguments = {
            "target_ref": request.target_ref,
            "observation_hash": request.observation_hash,
            "precondition": request.precondition,
            "arguments": request.arguments,
            "postcondition": request.postcondition,
        }
        target = self.repository.get_target(request.target_id)
        focus_capabilities = (
            (Capability.COMPUTER_INPUT,)
            if kind == "computer" and target.policy_ref.startswith("local-control:")
            else ()
        )
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
            extra_capabilities=focus_capabilities,
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
        target = self.repository.get_target(target_id)
        local_target = target.policy_ref.startswith("local-control:")
        if local_target:
            previous_result = self.repository.get_result(result.job_id)
            result = result.model_copy(
                update={
                    "completed_at": previous_result.completed_at
                    if previous_result is not None
                    else now
                }
            )
        observation_body: dict[str, Any] | None = None
        observation_target_ref: str | None = None
        post_observation_hash: str | None = None
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
                potentially_applied_local_action = (
                    local_target
                    and job.idempotency is RemoteActionIdempotency.NON_IDEMPOTENT
                    and job.capability
                    in {
                        RemoteCapability.BROWSER_NAVIGATE,
                        RemoteCapability.BROWSER_SUBMIT,
                        RemoteCapability.COMPUTER_INPUT,
                        RemoteCapability.CLIPBOARD_WRITE,
                    }
                )
                result = result.model_copy(
                    update={
                        "status": (
                            RemoteJobStatus.MANUAL_RECONCILE_REQUIRED
                            if potentially_applied_local_action
                            else RemoteJobStatus.FAILED
                        ),
                        "error_code": (
                            "remote.outcome_unknown"
                            if potentially_applied_local_action
                            else "remote.postcondition_failed"
                        ),
                    }
                )
            elif (
                local_target
                and job.capability
                in {
                    RemoteCapability.BROWSER_NAVIGATE,
                    RemoteCapability.BROWSER_SUBMIT,
                    RemoteCapability.COMPUTER_INPUT,
                    RemoteCapability.CLIPBOARD_WRITE,
                }
                and "post_observation_hash" in result.postcondition
            ):
                target_ref = job.arguments.get("target_ref")
                body = _post_action_observation_body(job, result)
                expected_content_hash = (
                    canonical_action_hash(
                        {"target_id": job.target_id, "target_ref": target_ref, "body": body}
                    )
                    if isinstance(target_ref, str) and body is not None
                    else None
                )
                if result.postcondition.get("post_observation_hash") != expected_content_hash:
                    # The action may already have happened. Never publish an
                    # unverified observation or report an ordinary retryable failure.
                    result = result.model_copy(
                        update={
                            "status": RemoteJobStatus.MANUAL_RECONCILE_REQUIRED,
                            "error_code": "remote.post_observation_invalid",
                        }
                    )
                else:
                    assert isinstance(target_ref, str) and body is not None
                    observation_body = body
                    observation_target_ref = target_ref
                    post_observation_hash = _post_action_observation_hash(
                        job, target_ref=target_ref, body=body
                    )
                    result = result.model_copy(
                        update={
                            "postcondition": {
                                **result.postcondition,
                                "post_observation_hash": post_observation_hash,
                            }
                        }
                    )
        observation: CapabilityObservation | None = None
        source: ObservationSource | None = None
        if (
            local_target
            and result.status is RemoteJobStatus.SUCCEEDED
            and observation_body is not None
            and observation_target_ref is not None
        ):
            observation_hash = post_observation_hash or observation_hash_for_job(
                job, target_ref=observation_target_ref, body=observation_body
            )
            observation = CapabilityObservation(
                target_id=target_id,
                capability=job.capability,
                target_ref=observation_target_ref,
                observation_hash=observation_hash,
                body=observation_body,
                artifact_ref=result.artifact_ref,
                created_at=result.completed_at,
                expires_at=result.completed_at + timedelta(seconds=self.observation_ttl_seconds),
            )
            source = ObservationSource(
                source_job_id=job.job_id,
                lease_id=job.lease_id,
                lease_fencing=job.lease_fencing,
                scope_digest=_local_scope_digest(target),
            )
        completed = self.repository.complete_job(
            result,
            target_id=target_id,
            lease_id=lease_id,
            token=token,
            fencing=fencing,
            now=now,
            observation=observation,
            observation_source=source,
        )
        if (
            not local_target
            and completed.status is RemoteJobStatus.SUCCEEDED
            and observation_body is not None
            and observation_target_ref is not None
        ):
            observation_hash = post_observation_hash or observation_hash_for_job(
                job, target_ref=observation_target_ref, body=observation_body
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
                ),
            )
        if job.capability not in {
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
        self._require_no_unknown_on_lease(target_id, lease_id)
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

    def _require_no_unknown_on_lease(self, target_id: str, lease_id: str) -> None:
        """A checked unknown still requires a new fenced lease before more work."""
        with self.repository.store._connect() as connection:
            unknown = connection.execute(
                "SELECT 1 FROM remote_execution_jobs WHERE target_id=? AND lease_id=? "
                "AND status='manual_reconcile_required' LIMIT 1",
                (target_id, lease_id),
            ).fetchone()
        if unknown is not None:
            raise ConflictError("unknown remote action requires a new fenced lease")

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
        if not 1 <= limit <= 32:
            raise ValueError("invalid remote job dispatch limit")
        results: list[RemoteExecutionResult] = []
        dispatch_started = time.monotonic()
        for _ in range(limit):
            polled_at = now + timedelta(seconds=time.monotonic() - dispatch_started)
            self._require_no_unknown_on_lease(connector.target_id, connector.lease_id)
            jobs = self.repository.poll_jobs(
                target_id=connector.target_id,
                lease_id=connector.lease_id,
                token=connector.lease_token,
                fencing=connector.lease_fencing,
                now=polled_at,
                limit=1,
            )
            if not jobs:
                break
            job = jobs[0]
            try:
                outcome = connector.execute(job)
                pending_result = outcome.result
                artifact_bytes = outcome.artifact_bytes
            except RemoteOutcomeUnknown:
                status = (
                    RemoteJobStatus.MANUAL_RECONCILE_REQUIRED
                    if job.idempotency is RemoteActionIdempotency.NON_IDEMPOTENT
                    else RemoteJobStatus.FAILED
                )
                pending_result = RemoteExecutionResult(
                    job_id=job.job_id,
                    result_idempotency_key=f"connector-unknown:{job.job_id}",
                    status=status,
                    error_code="remote.outcome_unknown",
                )
                artifact_bytes = None
            completed_at = now + timedelta(seconds=time.monotonic() - dispatch_started)
            try:
                result = self.complete_job(
                    pending_result.model_copy(update={"completed_at": completed_at}),
                    target_id=connector.target_id,
                    lease_id=connector.lease_id,
                    token=connector.lease_token,
                    fencing=connector.lease_fencing,
                    now=completed_at,
                    artifact_bytes=artifact_bytes,
                )
            except ConflictError as exc:
                reconciled = self.repository.reconcile_expired(now=completed_at)
                persisted = self.repository.get_result(job.job_id)
                if (
                    persisted is not None
                    and persisted.result_idempotency_key == f"core-lease-invalidated:{job.job_id}"
                ):
                    result = persisted
                elif any(item.job_id == job.job_id for item in reconciled):
                    raise ConflictError("reconciled Job has no durable result") from exc
                else:
                    raise
            results.append(result)
            if result.status is RemoteJobStatus.MANUAL_RECONCILE_REQUIRED:
                break
        return tuple(results)
