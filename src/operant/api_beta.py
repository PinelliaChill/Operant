from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from operant.application.graph import GraphConflictError
from operant.application.phase45_gateway import Phase45ActionGateway
from operant.domain.multiwriter import WriterIsolationKind, WriterLease
from operant.domain.security import Capability
from operant.mcp import GatewayDecision, McpError
from operant.multiwriter.container import (
    ContainerLifecycleStatus,
    ContainerOutcomeUnknown,
    ContainerResourceLimits,
    ContainerWriterLifecycle,
    ContainerWriterSpec,
)
from operant.persistence.beta import (
    SQLiteContainerLifecycleRepository,
    SQLiteRemoteGatewayConnectionRepository,
)
from operant.persistence.multiwriter import SQLiteMultiWriterRepository
from operant.persistence.sqlite import NotFoundError, SQLiteStore


class ContainerResourcesBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cpus: float = Field(default=2.0, ge=0.1, le=64)
    memory_bytes: int = Field(
        default=2 * 1024 * 1024 * 1024,
        ge=64 * 1024 * 1024,
        le=128 * 1024 * 1024 * 1024,
    )
    pids: int = Field(default=256, ge=16, le=4096)


class CreateContainerWriterBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    command: tuple[str, ...] = Field(min_length=1, max_length=128)
    environment: dict[str, str] = Field(default_factory=dict, max_length=32)
    user_uid: int = Field(ge=1, le=2**31 - 1)
    user_gid: int = Field(ge=1, le=2**31 - 1)
    resources: ContainerResourcesBody = Field(default_factory=ContainerResourcesBody)
    lease: WriterLease


class ContainerLifecycleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease: WriterLease


class ContainerProjection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    writer_workspace_id: str
    container_name: str
    image_ref: str
    mount_ref: str
    status: Literal[
        "creating",
        "created",
        "running",
        "stopping",
        "stopped",
        "removing",
        "removed",
        "outcome_unknown",
    ]
    revision: int
    owner_id: str | None
    fencing: int
    lease_expires_at: str | None
    action_hash: str | None
    resource_limits: dict[str, Any]
    last_error_code: str | None
    created_at: str
    updated_at: str
    stopped_at: str | None
    removed_at: str | None


class GatewayConnectionProjection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: str
    remote_session_id: str
    device_id: str
    transport_mode: Literal["direct", "relay"]
    event_cursor: int
    status: Literal[
        "connecting", "connected", "backoff", "disconnected", "closed", "outcome_unknown"
    ]
    owner_id: str
    fencing: int
    lease_expires_at: str
    last_seen_at: str
    closed_at: str | None
    error_code: str | None
    created_at: str
    updated_at: str


class GatewayConnectionList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[GatewayConnectionProjection]


def install_beta_gateway_routes(
    app: FastAPI,
    repository: SQLiteRemoteGatewayConnectionRepository,
    *,
    local_authorizer: Callable[[Request], bool],
) -> None:
    app.state.remote_gateway_connection_repository = repository

    @app.get(
        "/v1/remote-control/gateway/connections",
        operation_id="listRemoteGatewayConnections",
        response_model=GatewayConnectionList,
    )
    def list_remote_gateway_connections(request: Request) -> dict[str, Any]:
        if not local_authorizer(request):
            raise HTTPException(status_code=401, detail="local authorization required")
        return {"items": repository.list()}


def install_beta_container_routes(
    app: FastAPI,
    store: SQLiteStore,
    *,
    action_gateway: Phase45ActionGateway,
    local_authorizer: Callable[[Request], bool],
    lifecycle: ContainerWriterLifecycle | None,
) -> None:
    repository = SQLiteContainerLifecycleRepository(store)
    writer_repository = SQLiteMultiWriterRepository(store)
    app.state.container_writer_repository = repository
    app.state.container_writer_lifecycle = lifecycle

    def require_local(request: Request) -> None:
        if not local_authorizer(request):
            raise HTTPException(status_code=401, detail="local authorization required")

    def require_lifecycle() -> ContainerWriterLifecycle:
        if lifecycle is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "container_writer_not_configured",
                    "message": "Container Writer administrator roots are not configured",
                },
            )
        return lifecycle

    def workspace(workspace_id: str):
        current = writer_repository.get_workspace(workspace_id)
        if current.isolation_kind is not WriterIsolationKind.CONTAINER:
            raise GraphConflictError("writer workspace is not container-isolated")
        return current

    def guard(
        *,
        operation: str,
        workspace_id: str,
        arguments: dict[str, Any],
        lease: WriterLease,
        workspace_path: str,
    ) -> str:
        action, result, _evaluation = action_gateway.guard(
            tool="container_writer",
            operation=operation,
            target_id=workspace_id,
            arguments=arguments,
            capabilities=(Capability.WORKSPACE_WRITE, Capability.PROCESS_EXEC_NO_NETWORK),
            idempotency_key=(
                f"container-writer:{operation}:{workspace_id}:{lease.fencing}:"
                f"{hashlib.sha256(json.dumps(arguments, sort_keys=True).encode()).hexdigest()}"
            ),
            workspace=workspace_path,
            sandbox_profile="container",
            network_profile="none",
        )
        if result.decision is GatewayDecision.ASK:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "approval_required",
                    "approval_id": result.approval_id,
                    "action_hash": action.action_hash,
                    "reason_code": result.reason_code,
                },
            )
        if result.decision is not GatewayDecision.ALLOW or result.lease is None:
            raise HTTPException(status_code=403, detail=result.reason_code)
        try:
            action_gateway.consume(result.lease, action)
        except McpError as exc:
            raise HTTPException(status_code=409, detail=exc.code) from exc
        return action.action_hash

    def handle_error(exc: Exception) -> HTTPException:
        if isinstance(exc, NotFoundError):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, (GraphConflictError, ValueError, PermissionError)):
            return HTTPException(status_code=409, detail=str(exc))
        return HTTPException(status_code=500, detail="Container Writer operation failed")

    def complete_unknown(
        *,
        workspace_id: str,
        lease: WriterLease,
        action_hash: str,
        exc: Exception,
    ) -> None:
        try:
            repository.mark_unknown(
                workspace_id,
                lease=lease,
                action_hash=action_hash,
                error_code="container_outcome_unknown",
            )
        except Exception as persistence_exc:
            raise HTTPException(
                status_code=500,
                detail="Container Writer outcome and projection both require reconciliation",
            ) from persistence_exc
        raise HTTPException(
            status_code=409,
            detail={
                "code": "container_outcome_unknown",
                "message": "inspect and reconcile the Container Writer before another action",
            },
        ) from exc

    @app.get(
        "/v1/writer-workspaces/{workspace_id}/container",
        operation_id="getContainerWriter",
        response_model=ContainerProjection,
    )
    def get_container_writer(workspace_id: str, request: Request) -> dict[str, Any]:
        require_local(request)
        try:
            workspace(workspace_id)
            return repository.get(workspace_id)
        except Exception as exc:
            raise handle_error(exc) from exc

    @app.post(
        "/v1/writer-workspaces/{workspace_id}/container",
        operation_id="createContainerWriter",
        response_model=ContainerProjection,
        status_code=201,
    )
    def create_container_writer(
        workspace_id: str, body: CreateContainerWriterBody, request: Request
    ) -> dict[str, Any]:
        require_local(request)
        adapter = require_lifecycle()
        try:
            current_workspace = workspace(workspace_id)
            if body.lease.writer_workspace_id != workspace_id:
                raise GraphConflictError("writer lease belongs to another workspace")
            resources = ContainerResourceLimits(**body.resources.model_dump())
            spec = ContainerWriterSpec(
                workspace=current_workspace,
                image=body.image,
                command=body.command,
                environment=body.environment,
                user_uid=body.user_uid,
                user_gid=body.user_gid,
                resources=resources,
            )
            arguments = {
                "image": body.image,
                "command": body.command,
                "environment_names": sorted(body.environment),
                "environment_sha256": hashlib.sha256(
                    json.dumps(body.environment, sort_keys=True).encode()
                ).hexdigest(),
                "user_uid": body.user_uid,
                "user_gid": body.user_gid,
                "resources": body.resources.model_dump(),
                "lease_fencing": body.lease.fencing,
            }
            action_hash = guard(
                operation="create",
                workspace_id=workspace_id,
                arguments=arguments,
                lease=body.lease,
                workspace_path=str(adapter.workspace_path(current_workspace.isolation_ref)),
            )
            repository.begin_create(
                workspace_id=workspace_id,
                container_name=adapter.container_name(workspace_id),
                image_ref=body.image,
                mount_ref=current_workspace.isolation_ref,
                resource_limits={
                    **body.resources.model_dump(),
                    "user_uid": body.user_uid,
                    "user_gid": body.user_gid,
                },
                lease=body.lease,
                action_hash=action_hash,
            )
            try:
                status = adapter.create(spec)
            except Exception as exc:
                complete_unknown(
                    workspace_id=workspace_id,
                    lease=body.lease,
                    action_hash=action_hash,
                    exc=exc,
                )
            return repository.complete(
                workspace_id,
                lease=body.lease,
                action_hash=action_hash,
                status=status.value,
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise handle_error(exc) from exc

    def lifecycle_action(
        *,
        operation: Literal["start", "stop", "remove"],
        workspace_id: str,
        body: ContainerLifecycleBody,
        request: Request,
    ) -> dict[str, Any]:
        require_local(request)
        adapter = require_lifecycle()
        try:
            current_workspace = workspace(workspace_id)
            arguments = {
                "container_name": adapter.container_name(workspace_id),
                "isolation_ref": current_workspace.isolation_ref,
                "lease_fencing": body.lease.fencing,
            }
            action_hash = guard(
                operation=operation,
                workspace_id=workspace_id,
                arguments=arguments,
                lease=body.lease,
                workspace_path=str(adapter.workspace_path(current_workspace.isolation_ref)),
            )
            repository.begin_action(
                workspace_id,
                lease=body.lease,
                action_hash=action_hash,
                operation=operation,
            )
            try:
                call = getattr(adapter, operation)
                status: ContainerLifecycleStatus = call(workspace_id)
            except Exception as exc:
                complete_unknown(
                    workspace_id=workspace_id,
                    lease=body.lease,
                    action_hash=action_hash,
                    exc=exc,
                )
            return repository.complete(
                workspace_id,
                lease=body.lease,
                action_hash=action_hash,
                status=status.value,
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise handle_error(exc) from exc

    @app.post(
        "/v1/writer-workspaces/{workspace_id}/container/start",
        operation_id="startContainerWriter",
        response_model=ContainerProjection,
    )
    def start_container_writer(
        workspace_id: str, body: ContainerLifecycleBody, request: Request
    ) -> dict[str, Any]:
        return lifecycle_action(
            operation="start", workspace_id=workspace_id, body=body, request=request
        )

    @app.post(
        "/v1/writer-workspaces/{workspace_id}/container/stop",
        operation_id="stopContainerWriter",
        response_model=ContainerProjection,
    )
    def stop_container_writer(
        workspace_id: str, body: ContainerLifecycleBody, request: Request
    ) -> dict[str, Any]:
        return lifecycle_action(
            operation="stop", workspace_id=workspace_id, body=body, request=request
        )

    @app.post(
        "/v1/writer-workspaces/{workspace_id}/container/remove",
        operation_id="removeContainerWriter",
        response_model=ContainerProjection,
    )
    def remove_container_writer(
        workspace_id: str, body: ContainerLifecycleBody, request: Request
    ) -> dict[str, Any]:
        return lifecycle_action(
            operation="remove", workspace_id=workspace_id, body=body, request=request
        )

    @app.post(
        "/v1/writer-workspaces/{workspace_id}/container/reconcile",
        operation_id="reconcileContainerWriter",
        response_model=ContainerProjection,
    )
    def reconcile_container_writer(
        workspace_id: str, body: ContainerLifecycleBody, request: Request
    ) -> dict[str, Any]:
        require_local(request)
        adapter = require_lifecycle()
        try:
            current_workspace = workspace(workspace_id)
            current = repository.get(workspace_id)
            if current["status"] != "outcome_unknown":
                raise GraphConflictError("only outcome-unknown Container Writers can reconcile")
            arguments = {
                "container_name": adapter.container_name(workspace_id),
                "isolation_ref": current_workspace.isolation_ref,
                "observed_revision": current["revision"],
                "lease_fencing": body.lease.fencing,
            }
            action_hash = guard(
                operation="reconcile",
                workspace_id=workspace_id,
                arguments=arguments,
                lease=body.lease,
                workspace_path=str(adapter.workspace_path(current_workspace.isolation_ref)),
            )
            try:
                observed = adapter.inspect(workspace_id)
            except ContainerOutcomeUnknown as exc:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "container_outcome_unknown",
                        "message": "Docker state is still not inspectable",
                    },
                ) from exc
            status = (
                ContainerLifecycleStatus.REMOVED
                if observed is ContainerLifecycleStatus.ABSENT
                else observed
            )
            return repository.reconcile(
                workspace_id,
                lease=body.lease,
                action_hash=action_hash,
                status=status.value,
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise handle_error(exc) from exc
