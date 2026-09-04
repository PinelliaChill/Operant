from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from operant.application.graph import GraphConflictError
from operant.application.multiwriter import (
    MergeAdapter,
    MultiWriterRuntime,
    WriterArtifactAdapter,
)
from operant.application.phase45_gateway import Phase45ActionGateway
from operant.domain.graph import WorkflowDefinition
from operant.domain.multiwriter import (
    MergeRun,
    MergeRunStatus,
    PatchCommitArtifact,
    WriterLease,
    WriterWorkspace,
)
from operant.domain.security import Capability, PolicyDecision
from operant.persistence.graph_team import SQLiteGraphRepository
from operant.persistence.multiwriter import SQLiteMultiWriterRepository
from operant.persistence.sqlite import NotFoundError, SQLiteStore


class AcquireWriterLeaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner: str = Field(min_length=1, max_length=300)
    ttl_seconds: int = Field(default=60, ge=1, le=3600)


class RenewWriterLeaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lease: WriterLease
    ttl_seconds: int = Field(default=60, ge=1, le=3600)


class ReleaseWriterLeaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lease: WriterLease


class PublishWriterArtifactRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact: PatchCommitArtifact
    lease: WriterLease


class CreateMergeRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    merge_run: MergeRun
    leases: tuple[WriterLease, ...] = Field(min_length=2, max_length=64)


class FinalizeMergeRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    leases: tuple[WriterLease, ...] = Field(min_length=2, max_length=64)
    review_approved: bool = False


class ReconcileMergeRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    status: MergeRunStatus
    result_artifact_ref: str | None = Field(default=None, max_length=500)
    error_code: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def validate_terminal_status(self) -> ReconcileMergeRunRequest:
        if self.status not in {
            MergeRunStatus.SUCCEEDED,
            MergeRunStatus.FAILED,
            MergeRunStatus.ROLLED_BACK,
        }:
            raise ValueError("merge reconciliation requires a known terminal outcome")
        if self.status is MergeRunStatus.SUCCEEDED and self.result_artifact_ref is None:
            raise ValueError("successful merge reconciliation requires a result ref")
        return self


class ResolveMergeConflictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resolution_artifact_ref: str = Field(min_length=1, max_length=500)
    rejected: bool = False


def install_phase56_writer_routes(
    app: FastAPI,
    store: SQLiteStore,
    *,
    action_gateway: Phase45ActionGateway,
    local_authorizer: Callable[[Request], bool],
    artifact_adapter: WriterArtifactAdapter | None = None,
    merge_adapter: MergeAdapter | None = None,
) -> None:
    repository = SQLiteMultiWriterRepository(store)
    runtime = MultiWriterRuntime(
        repository, artifact_adapter=artifact_adapter, merge_adapter=merge_adapter
    )
    graph_repository = SQLiteGraphRepository(store)
    app.state.multiwriter_repository = repository
    app.state.multiwriter_runtime = runtime

    def require_local(request: Request) -> None:
        if not local_authorizer(request):
            raise HTTPException(status_code=401, detail="local authorization required")

    def trusted_workspace_path(isolation_ref: str) -> str:
        for adapter in (artifact_adapter, merge_adapter):
            resolver = None if adapter is None else getattr(adapter, "workspace_path", None)
            if not callable(resolver):
                continue
            try:
                configured = Path(resolver(isolation_ref))
                resolved = configured.resolve(strict=True)
            except (KeyError, OSError, TypeError, ValueError):
                continue
            if configured.is_absolute() and resolved.is_dir():
                return str(resolved)
        raise ValueError("isolation reference is not mapped to a trusted workspace")

    def guard_side_effect(
        *,
        operation: str,
        target_id: str,
        arguments: dict[str, Any],
        capabilities: tuple[Capability, ...],
        idempotency_key: str,
        workspace: str | None = None,
    ) -> None:
        action, result, _evaluation = action_gateway.guard(
            tool="multiwriter",
            operation=operation,
            target_id=target_id,
            arguments=arguments,
            capabilities=capabilities,
            idempotency_key=idempotency_key,
            workspace=workspace,
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
        action_gateway.consume(result.lease, action)

    def fail(exc: Exception) -> HTTPException:
        if isinstance(exc, NotFoundError):
            return HTTPException(status_code=404, detail=str(exc))
        if isinstance(exc, (GraphConflictError, ValueError)):
            return HTTPException(status_code=409, detail=str(exc))
        return HTTPException(status_code=500, detail="multi-writer operation failed")

    def lease(workspace_id: str, claim: WriterLease) -> WriterLease:
        if claim.writer_workspace_id != workspace_id:
            raise ValueError("writer lease workspace does not match the route")
        return claim

    def definition_for_run(run_id: str) -> WorkflowDefinition:
        run = graph_repository.get_run(run_id)
        return graph_repository.get_definition(
            run.workflow_definition_id, run.workflow_definition_version
        )

    @app.post(
        "/v1/graph/runs/{run_id}/writer-workspaces",
        operation_id="createWriterWorkspace",
        status_code=201,
    )
    async def create_writer_workspace(run_id: str, workspace: WriterWorkspace) -> dict[str, Any]:
        try:
            if workspace.graph_run_id != run_id:
                raise ValueError("writer workspace graph_run_id does not match the route")
            definition = definition_for_run(run_id)
            node_run = graph_repository.get_node_run(workspace.node_run_id)
            node = next(item for item in definition.nodes if item.node_id == node_run.node_id)
            if node.writer_policy is None:
                raise ValueError("node is not a typed writer")
            policy = node.writer_policy
            expected = (
                policy.writer_key,
                policy.isolation_kind,
                policy.isolation_ref,
                policy.ownership_paths,
            )
            actual = (
                workspace.writer_key,
                workspace.isolation_kind,
                workspace.isolation_ref,
                workspace.ownership_paths,
            )
            if actual != expected:
                raise ValueError("writer workspace does not match the frozen node policy")
            guard_side_effect(
                operation="create_workspace",
                target_id=workspace.writer_workspace_id,
                arguments={
                    "graph_run_id": run_id,
                    "node_run_id": workspace.node_run_id,
                    "writer_key": workspace.writer_key,
                    "isolation_kind": workspace.isolation_kind.value,
                    "isolation_ref": workspace.isolation_ref,
                    "base_revision": workspace.base_revision,
                    "ownership_paths": workspace.ownership_paths,
                },
                capabilities=(Capability.WORKSPACE_WRITE,),
                idempotency_key=f"multiwriter:create:{workspace.writer_workspace_id}",
                workspace=trusted_workspace_path(workspace.isolation_ref),
            )
            return runtime.create_workspace(workspace).model_dump(mode="json")
        except (NotFoundError, KeyError, StopIteration, GraphConflictError, ValueError) as exc:
            raise fail(exc) from exc

    @app.get(
        "/v1/graph/runs/{run_id}/writer-workspaces",
        operation_id="listWriterWorkspaces",
    )
    async def list_writer_workspaces(run_id: str) -> list[dict[str, Any]]:
        result = []
        for item in repository.list_workspaces(run_id):
            projection = item.model_dump(mode="json")
            current_lease = repository.get_lease(item.writer_workspace_id)
            projection["lease"] = (
                None
                if current_lease is None
                else {
                    "owner": current_lease.owner,
                    "fencing": current_lease.fencing,
                    "expires_at": current_lease.expires_at,
                    "released_at": current_lease.released_at,
                }
            )
            result.append(projection)
        return result

    @app.post(
        "/v1/writer-workspaces/{workspace_id}/lease",
        operation_id="acquireWriterLease",
    )
    async def acquire_writer_lease(
        workspace_id: str, body: AcquireWriterLeaseRequest, response: Response
    ) -> dict[str, Any]:
        try:
            workspace = repository.get_workspace(workspace_id)
            guard_side_effect(
                operation="acquire_lease",
                target_id=workspace_id,
                arguments={"owner": body.owner, "ttl_seconds": body.ttl_seconds},
                capabilities=(Capability.WORKSPACE_WRITE,),
                idempotency_key=f"multiwriter:acquire:{workspace_id}:{body.owner}",
                workspace=trusted_workspace_path(workspace.isolation_ref),
            )
            response.headers["x-operant-sensitive-response"] = "one-time"
            response.headers["Cache-Control"] = "no-store"
            return runtime.acquire_lease(
                workspace_id, owner=body.owner, ttl_seconds=body.ttl_seconds
            ).model_dump(mode="json")
        except (NotFoundError, GraphConflictError, ValueError) as exc:
            raise fail(exc) from exc

    @app.post(
        "/v1/writer-workspaces/{workspace_id}/lease/renew",
        operation_id="renewWriterLease",
    )
    async def renew_writer_lease(
        workspace_id: str, body: RenewWriterLeaseRequest
    ) -> dict[str, Any]:
        try:
            workspace = repository.get_workspace(workspace_id)
            guard_side_effect(
                operation="renew_lease",
                target_id=workspace_id,
                arguments={
                    "owner": body.lease.owner,
                    "fencing": body.lease.fencing,
                    "ttl_seconds": body.ttl_seconds,
                },
                capabilities=(Capability.WORKSPACE_WRITE,),
                idempotency_key=(
                    f"multiwriter:renew:{workspace_id}:{body.lease.fencing}:"
                    f"{body.lease.expires_at.isoformat()}"
                ),
                workspace=trusted_workspace_path(workspace.isolation_ref),
            )
            renewed = runtime.renew_lease(
                lease(workspace_id, body.lease), ttl_seconds=body.ttl_seconds
            )
            return renewed.model_dump(mode="json", exclude={"token"})
        except (GraphConflictError, ValueError) as exc:
            raise fail(exc) from exc

    @app.post(
        "/v1/writer-workspaces/{workspace_id}/lease/release",
        operation_id="releaseWriterLease",
    )
    async def release_writer_lease(
        workspace_id: str, body: ReleaseWriterLeaseRequest
    ) -> dict[str, Any]:
        try:
            workspace = repository.get_workspace(workspace_id)
            guard_side_effect(
                operation="release_lease",
                target_id=workspace_id,
                arguments={"owner": body.lease.owner, "fencing": body.lease.fencing},
                capabilities=(Capability.WORKSPACE_WRITE,),
                idempotency_key=f"multiwriter:release:{workspace_id}:{body.lease.fencing}",
                workspace=trusted_workspace_path(workspace.isolation_ref),
            )
            released = repository.release_lease(lease(workspace_id, body.lease))
            return released.model_dump(mode="json", exclude={"token"})
        except (GraphConflictError, ValueError) as exc:
            raise fail(exc) from exc

    @app.post(
        "/v1/writer-workspaces/{workspace_id}/artifacts",
        operation_id="publishWriterArtifact",
        status_code=201,
    )
    async def publish_writer_artifact(
        workspace_id: str, body: PublishWriterArtifactRequest
    ) -> dict[str, Any]:
        try:
            if body.artifact.writer_workspace_id != workspace_id:
                raise ValueError("artifact workspace does not match the route")
            workspace = repository.get_workspace(workspace_id)
            guard_side_effect(
                operation="publish_artifact",
                target_id=workspace_id,
                arguments={
                    "writer_artifact_id": body.artifact.writer_artifact_id,
                    "artifact_kind": body.artifact.artifact_kind.value,
                    "artifact_ref": body.artifact.artifact_ref,
                    "artifact_sha256": body.artifact.artifact_sha256,
                    "base_revision": body.artifact.base_revision,
                    "result_revision": body.artifact.result_revision,
                    "changed_paths": body.artifact.changed_paths,
                    "lease_fencing": body.lease.fencing,
                },
                capabilities=(Capability.WORKSPACE_WRITE,),
                idempotency_key=f"multiwriter:artifact:{body.artifact.writer_artifact_id}",
                workspace=trusted_workspace_path(workspace.isolation_ref),
            )
            return runtime.publish_artifact(
                body.artifact, lease=lease(workspace_id, body.lease)
            ).model_dump(mode="json")
        except (NotFoundError, GraphConflictError, ValueError) as exc:
            raise fail(exc) from exc

    @app.get(
        "/v1/graph/runs/{run_id}/writer-artifacts",
        operation_id="listWriterArtifacts",
    )
    async def list_writer_artifacts(run_id: str) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in repository.list_artifacts(run_id)]

    @app.post(
        "/v1/graph/runs/{run_id}/writer-conflicts/detect",
        operation_id="detectWriterConflicts",
    )
    async def detect_writer_conflicts(run_id: str) -> list[dict[str, Any]]:
        try:
            artifact_ids = tuple(
                item.writer_artifact_id for item in repository.list_artifacts(run_id)
            )
            guard_side_effect(
                operation="detect_conflicts",
                target_id=run_id,
                arguments={"artifact_ids": artifact_ids},
                capabilities=(Capability.WORKSPACE_WRITE,),
                idempotency_key=f"multiwriter:detect:{run_id}:{':'.join(artifact_ids)}",
            )
            return [item.model_dump(mode="json") for item in runtime.detect_conflicts(run_id)]
        except (NotFoundError, GraphConflictError, ValueError) as exc:
            raise fail(exc) from exc

    @app.get(
        "/v1/graph/runs/{run_id}/writer-conflicts",
        operation_id="listWriterConflicts",
    )
    async def list_writer_conflicts(run_id: str) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in repository.list_conflicts(run_id)]

    @app.post("/v1/merge-runs", operation_id="createMergeRun", status_code=201)
    async def create_merge_run(body: CreateMergeRunRequest) -> dict[str, Any]:
        merge = body.merge_run
        try:
            graph_run = graph_repository.get_run(merge.graph_run_id)
            if merge.target_isolation_ref == graph_run.workspace_or_target:
                raise ValueError("merge target cannot be the user graph workspace")
            definition = definition_for_run(merge.graph_run_id)
            guard_side_effect(
                operation="create_merge_run",
                target_id=merge.merge_run_id,
                arguments={
                    "graph_run_id": merge.graph_run_id,
                    "merge_node_id": merge.merge_node_id,
                    "artifact_ids": merge.artifact_ids,
                    "strategy": merge.strategy.value,
                    "target_isolation_ref": merge.target_isolation_ref,
                    "base_revision": merge.base_revision,
                },
                capabilities=(Capability.WORKSPACE_WRITE,),
                idempotency_key=f"multiwriter:merge-create:{merge.merge_run_id}",
                workspace=trusted_workspace_path(merge.target_isolation_ref),
            )
            return runtime.create_merge_run(
                merge, definition=definition, leases=body.leases
            ).model_dump(mode="json")
        except (NotFoundError, KeyError, GraphConflictError, ValueError) as exc:
            raise fail(exc) from exc

    @app.get("/v1/merge-runs/{merge_run_id}", operation_id="getMergeRun")
    async def get_merge_run(merge_run_id: str) -> dict[str, Any]:
        try:
            return repository.get_merge_run(merge_run_id).model_dump(mode="json")
        except NotFoundError as exc:
            raise fail(exc) from exc

    @app.get(
        "/v1/graph/runs/{run_id}/merge-runs",
        operation_id="listMergeRuns",
    )
    async def list_merge_runs(run_id: str) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in repository.list_merge_runs(run_id)]

    @app.post(
        "/v1/writer-conflicts/{conflict_id}/resolve",
        operation_id="resolveMergeConflict",
    )
    async def resolve_merge_conflict(
        conflict_id: str, body: ResolveMergeConflictRequest
    ) -> dict[str, Any]:
        try:
            guard_side_effect(
                operation="resolve_conflict",
                target_id=conflict_id,
                arguments={
                    "resolution_artifact_ref": body.resolution_artifact_ref,
                    "rejected": body.rejected,
                },
                capabilities=(Capability.WORKSPACE_WRITE,),
                idempotency_key=(
                    f"multiwriter:resolve:{conflict_id}:"
                    f"{body.resolution_artifact_ref}:{body.rejected}"
                ),
            )
            return repository.resolve_conflict(
                conflict_id,
                resolution_artifact_ref=body.resolution_artifact_ref,
                rejected=body.rejected,
            ).model_dump(mode="json")
        except GraphConflictError as exc:
            raise fail(exc) from exc

    @app.post(
        "/v1/merge-runs/{merge_run_id}/finalize",
        operation_id="finalizeMergeRun",
    )
    async def finalize_merge_run(
        merge_run_id: str, body: FinalizeMergeRunRequest
    ) -> dict[str, Any]:
        try:
            merge = repository.get_merge_run(merge_run_id)
            guard_side_effect(
                operation="finalize_merge",
                target_id=merge_run_id,
                arguments={
                    "graph_run_id": merge.graph_run_id,
                    "merge_node_id": merge.merge_node_id,
                    "artifact_ids": merge.artifact_ids,
                    "strategy": merge.strategy.value,
                    "target_isolation_ref": merge.target_isolation_ref,
                    "base_revision": merge.base_revision,
                    "expected_revision": merge.expected_revision,
                    "review_approved": body.review_approved,
                },
                capabilities=(Capability.WORKSPACE_WRITE, Capability.GIT_COMMIT),
                idempotency_key=(
                    f"multiwriter:merge-finalize:{merge_run_id}:{merge.expected_revision}"
                ),
                workspace=trusted_workspace_path(merge.target_isolation_ref),
            )
            return runtime.finalize_merge_run(
                merge_run_id,
                definition=definition_for_run(merge.graph_run_id),
                leases=body.leases,
                review_approved=body.review_approved,
            ).model_dump(mode="json")
        except (NotFoundError, KeyError, GraphConflictError, ValueError) as exc:
            raise fail(exc) from exc

    @app.post(
        "/v1/merge-runs/{merge_run_id}/reconcile",
        operation_id="reconcileMergeRun",
    )
    async def reconcile_merge_run(
        merge_run_id: str, body: ReconcileMergeRunRequest, request: Request
    ) -> dict[str, Any]:
        require_local(request)
        try:
            merge = repository.get_merge_run(merge_run_id)
            guard_side_effect(
                operation="reconcile_merge",
                target_id=merge_run_id,
                arguments=body.model_dump(mode="json"),
                capabilities=(Capability.WORKSPACE_WRITE,),
                idempotency_key=(
                    f"multiwriter:merge-reconcile:{merge_run_id}:"
                    f"{body.expected_revision}:{body.status.value}"
                ),
                workspace=trusted_workspace_path(merge.target_isolation_ref),
            )
            return runtime.reconcile_merge_run(
                merge_run_id,
                expected_revision=body.expected_revision,
                status=body.status,
                result_artifact_ref=body.result_artifact_ref,
                error_code=body.error_code,
            ).model_dump(mode="json")
        except (NotFoundError, KeyError, GraphConflictError, ValueError) as exc:
            raise fail(exc) from exc
