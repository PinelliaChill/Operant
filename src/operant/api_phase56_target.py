from __future__ import annotations

import base64
import binascii
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from typing import Any, TypeVar

from fastapi import FastAPI, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from operant.application.phase45_gateway import Phase45ActionGateway
from operant.application.remote_execution import RemoteAuthorization, RemoteExecutionController
from operant.domain.remote_execution import (
    CapabilityActionRequest,
    CapabilityManifest,
    RemoteActionIdempotency,
    RemoteCapability,
    RemoteExecutionResult,
    RemoteJobStatus,
    RemoteTargetRegistration,
)
from operant.domain.security import ActionRequest, Capability, PolicyDecision
from operant.persistence.remote_execution import SQLiteRemoteExecutionRepository
from operant.persistence.sqlite import ConflictError, IdempotencyConflictError, SQLiteStore
from operant.protocol import canonical_action_hash

T = TypeVar("T")


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterRemoteTargetBody(_Body):
    target_id: str = Field(min_length=1, max_length=200)
    display_name: str = Field(min_length=1, max_length=200)
    endpoint_ref: str = Field(min_length=1, max_length=2_000)
    identity_public_key: str = Field(min_length=32, max_length=500)
    credential_ref: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,127}$")
    policy_ref: str = Field(min_length=1, max_length=300)
    artifact_namespace: str = Field(min_length=1, max_length=300)
    capability_manifest: CapabilityManifest


class HeartbeatRemoteTargetBody(_Body):
    identity_public_key: str = Field(min_length=32, max_length=500)


class AcquireRemoteTargetLeaseBody(_Body):
    owner: str = Field(min_length=1, max_length=300)
    workspace_ref: str = Field(min_length=1, max_length=500)
    ttl_seconds: int = Field(default=60, ge=1, le=300)
    idempotency_key: str = Field(min_length=1, max_length=300)


class LeaseBindingBody(_Body):
    lease_id: str = Field(min_length=1, max_length=200)
    token: str = Field(min_length=16, max_length=300)
    fencing: int = Field(ge=1)


class ReleaseRemoteTargetLeaseBody(LeaseBindingBody):
    idempotency_key: str = Field(min_length=1, max_length=300)


class RenewRemoteTargetLeaseBody(LeaseBindingBody):
    ttl_seconds: int = Field(default=60, ge=1, le=300)


class PollRemoteTargetJobsBody(LeaseBindingBody):
    limit: int = Field(default=8, ge=1, le=32)
    idempotency_key: str = Field(min_length=1, max_length=300)


class CreateRemoteTargetJobBody(LeaseBindingBody):
    capability: RemoteCapability
    operation: str = Field(min_length=1, max_length=300)
    arguments: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=300)
    idempotency: RemoteActionIdempotency


class CompleteRemoteTargetJobBody(LeaseBindingBody):
    result_id: str = Field(min_length=1, max_length=200)
    result_idempotency_key: str = Field(min_length=1, max_length=300)
    status: RemoteJobStatus
    artifact_ref: str | None = Field(default=None, max_length=500)
    artifact_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    artifact_base64: str | None = Field(default=None, max_length=24_000_000)
    postcondition: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = Field(default=None, max_length=200)


class CancelRemoteTargetJobBody(_Body):
    idempotency_key: str = Field(min_length=1, max_length=300)


class ObserveCapabilityBody(LeaseBindingBody):
    target_ref: str = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=1, max_length=300)


class ActCapabilityBody(LeaseBindingBody):
    action: CapabilityActionRequest


class _Phase45RemoteAuthorization(RemoteAuthorization):
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
        action, result, _evaluation = self.gateway.guard(
            tool=tool,
            operation=operation,
            target_id=target_id,
            arguments=arguments,
            capabilities=capabilities,
            idempotency_key=idempotency_key,
        )
        if result.decision.value != PolicyDecision.ALLOW.value or result.lease is None:
            if result.decision.value == PolicyDecision.ASK.value:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "approval_required",
                        "reason_code": result.reason_code,
                        "approval_id": result.approval_id,
                        "action_hash": action.action_hash,
                        "policy_version": action.policy_version,
                    },
                )
            raise HTTPException(status_code=403, detail=result.reason_code)
        self.gateway.consume(result.lease, action)
        return action


def install_phase56_target_routes(
    app: FastAPI,
    store: SQLiteStore,
    *,
    action_gateway: Phase45ActionGateway,
) -> None:
    repository = SQLiteRemoteExecutionRepository(store)
    reconciled = repository.reconcile_expired(now=datetime.now(timezone.utc))
    controller = RemoteExecutionController(repository, _Phase45RemoteAuthorization(action_gateway))
    app.state.remote_execution_repository = repository
    app.state.remote_execution_controller = controller
    app.state.remote_execution_reconciled_jobs = tuple(item.job_id for item in reconciled)

    def now() -> datetime:
        return datetime.now(timezone.utc)

    def call(operation: Callable[[], T]) -> T:
        try:
            return operation()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="remote target resource not found") from exc
        except IdempotencyConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/v1/remote-targets", operation_id="registerRemoteTarget", status_code=201)
    async def register_remote_target(body: RegisterRemoteTargetBody) -> dict[str, Any]:
        target = RemoteTargetRegistration(**body.model_dump(), created_at=now(), updated_at=now())
        return call(lambda: controller.register_target(target)).model_dump(mode="json")

    @app.get("/v1/remote-targets", operation_id="listRemoteTargets")
    async def list_remote_targets(
        limit: int = Query(default=200, ge=1, le=500),
    ) -> dict[str, Any]:
        return {
            "items": [item.model_dump(mode="json") for item in repository.list_targets(limit=limit)]
        }

    # Keep this static route ahead of /v1/remote-targets/{target_id}; Starlette
    # resolves matching routes in registration order.
    @app.get("/v1/remote-targets/jobs", operation_id="listRemoteTargetJobs")
    async def list_remote_target_jobs(
        target_id: str | None = None,
        limit: int = Query(default=200, ge=1, le=500),
    ) -> dict[str, Any]:
        return {
            "items": [
                item.model_dump(mode="json")
                for item in repository.list_jobs(target_id=target_id, limit=limit)
            ]
        }

    @app.get("/v1/remote-targets/{target_id}", operation_id="getRemoteTarget")
    async def get_remote_target(target_id: str) -> dict[str, Any]:
        return call(lambda: repository.get_target(target_id)).model_dump(mode="json")

    @app.post("/v1/remote-targets/{target_id}/heartbeat", operation_id="heartbeatRemoteTarget")
    async def heartbeat_remote_target(
        target_id: str, body: HeartbeatRemoteTargetBody
    ) -> dict[str, Any]:
        return call(
            lambda: controller.heartbeat_target(
                target_id, identity_public_key=body.identity_public_key, now=now()
            )
        ).model_dump(mode="json")

    @app.post(
        "/v1/remote-targets/{target_id}/leases",
        operation_id="acquireRemoteTargetLease",
        status_code=201,
    )
    async def acquire_remote_target_lease(
        target_id: str, body: AcquireRemoteTargetLeaseBody, response: Response
    ) -> dict[str, Any]:
        response.headers["x-operant-sensitive-response"] = "one-time"
        response.headers["Cache-Control"] = "no-store"
        return call(
            lambda: controller.acquire_lease(
                target_id,
                owner=body.owner,
                workspace_ref=body.workspace_ref,
                ttl_seconds=body.ttl_seconds,
                now=now(),
                idempotency_key=body.idempotency_key,
            )
        ).model_dump(mode="json")

    @app.post(
        "/v1/remote-targets/{target_id}/leases/renew",
        operation_id="renewRemoteTargetLease",
    )
    async def renew_remote_target_lease(
        target_id: str, body: RenewRemoteTargetLeaseBody
    ) -> dict[str, Any]:
        renewed = call(
            lambda: controller.renew_lease(
                target_id=target_id,
                lease_id=body.lease_id,
                token=body.token,
                fencing=body.fencing,
                ttl_seconds=body.ttl_seconds,
                now=now(),
            )
        )
        return renewed.model_dump(mode="json", exclude={"token"})

    @app.post(
        "/v1/remote-targets/{target_id}/leases/release",
        operation_id="releaseRemoteTargetLease",
    )
    async def release_remote_target_lease(
        target_id: str, body: ReleaseRemoteTargetLeaseBody
    ) -> dict[str, Any]:
        released = call(
            lambda: controller.release_lease(
                target_id=target_id,
                lease_id=body.lease_id,
                token=body.token,
                fencing=body.fencing,
                now=now(),
                idempotency_key=body.idempotency_key,
            )
        )
        return released.model_dump(mode="json", exclude={"token"})

    @app.post(
        "/v1/remote-targets/{target_id}/jobs",
        operation_id="createRemoteTargetJob",
        status_code=201,
    )
    async def create_remote_target_job(
        target_id: str, body: CreateRemoteTargetJobBody
    ) -> dict[str, Any]:
        return call(
            lambda: controller.create_job(
                target_id=target_id,
                lease_id=body.lease_id,
                lease_token=body.token,
                lease_fencing=body.fencing,
                capability=body.capability,
                operation=body.operation,
                arguments=body.arguments,
                idempotency_key=body.idempotency_key,
                idempotency=body.idempotency,
                now=now(),
            )
        ).model_dump(mode="json")

    @app.post("/v1/remote-targets/{target_id}/jobs/poll", operation_id="pollRemoteTargetJobs")
    async def poll_remote_target_jobs(
        target_id: str, body: PollRemoteTargetJobsBody
    ) -> dict[str, Any]:
        jobs = call(
            lambda: controller.poll_jobs(
                target_id=target_id,
                lease_id=body.lease_id,
                token=body.token,
                fencing=body.fencing,
                limit=body.limit,
                now=now(),
                idempotency_key=body.idempotency_key,
            )
        )
        return {"items": [job.model_dump(mode="json") for job in jobs]}

    @app.post(
        "/v1/remote-targets/{target_id}/jobs/{job_id}/complete",
        operation_id="completeRemoteTargetJob",
    )
    async def complete_remote_target_job(
        target_id: str, job_id: str, body: CompleteRemoteTargetJobBody
    ) -> dict[str, Any]:
        artifact_bytes: bytes | None = None
        if body.artifact_base64 is not None:
            try:
                artifact_bytes = base64.b64decode(body.artifact_base64, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise HTTPException(status_code=422, detail="artifact_base64 is invalid") from exc
        completed_at = now()
        result = RemoteExecutionResult(
            result_id=body.result_id,
            job_id=job_id,
            result_idempotency_key=body.result_idempotency_key,
            status=body.status,
            artifact_ref=body.artifact_ref,
            artifact_sha256=body.artifact_sha256,
            postcondition=body.postcondition,
            error_code=body.error_code,
            completed_at=completed_at,
        )
        completed = call(
            lambda: controller.complete_job(
                result,
                target_id=target_id,
                lease_id=body.lease_id,
                token=body.token,
                fencing=body.fencing,
                now=completed_at,
                artifact_bytes=artifact_bytes,
            )
        )
        response: dict[str, Any] = {"result": completed.model_dump(mode="json")}
        job = repository.get_job(job_id)
        if (
            job.capability
            in {
                RemoteCapability.BROWSER_OBSERVE,
                RemoteCapability.COMPUTER_OBSERVE,
            }
            and completed.status is RemoteJobStatus.SUCCEEDED
        ):
            observation_hash = canonical_action_hash(
                {
                    "target_id": target_id,
                    "target_ref": completed.postcondition["target_ref"],
                    "body": completed.postcondition["observation"],
                }
            )
            response["observation"] = repository.get_observation(
                target_id, observation_hash
            ).model_dump(mode="json")
        return response

    @app.post("/v1/remote-targets/jobs/{job_id}/cancel", operation_id="cancelRemoteTargetJob")
    async def cancel_remote_target_job(
        job_id: str, body: CancelRemoteTargetJobBody
    ) -> dict[str, Any]:
        return call(
            lambda: controller.cancel_job(job_id, now=now(), idempotency_key=body.idempotency_key)
        ).model_dump(mode="json")

    async def observe(kind: str, target_id: str, body: ObserveCapabilityBody) -> dict[str, Any]:
        return call(
            lambda: controller.observe(
                kind=kind,
                target_id=target_id,
                lease_id=body.lease_id,
                lease_token=body.token,
                lease_fencing=body.fencing,
                target_ref=body.target_ref,
                idempotency_key=body.idempotency_key,
                now=now(),
            )
        ).model_dump(mode="json")

    @app.post("/v1/browser/{target_id}/observe", operation_id="observeBrowser")
    async def observe_browser(target_id: str, body: ObserveCapabilityBody) -> dict[str, Any]:
        return await observe("browser", target_id, body)

    @app.post("/v1/computer/{target_id}/observe", operation_id="observeComputer")
    async def observe_computer(target_id: str, body: ObserveCapabilityBody) -> dict[str, Any]:
        return await observe("computer", target_id, body)

    async def act(kind: str, body: ActCapabilityBody) -> dict[str, Any]:
        job, receipt = call(
            lambda: controller.act(
                body.action,
                kind=kind,
                lease_id=body.lease_id,
                lease_token=body.token,
                lease_fencing=body.fencing,
                now=now(),
            )
        )
        return {
            "job": job.model_dump(mode="json"),
            "receipt": receipt.model_dump(mode="json"),
        }

    @app.post("/v1/browser/act", operation_id="actBrowser")
    async def act_browser(body: ActCapabilityBody) -> dict[str, Any]:
        return await act("browser", body)

    @app.post("/v1/computer/act", operation_id="actComputer")
    async def act_computer(body: ActCapabilityBody) -> dict[str, Any]:
        return await act("computer", body)
