from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from operant.application.graph import GraphConflictError
from operant.application.multiwriter import (
    MergeAdapter,
    MultiWriterRuntime,
    WriterArtifactAdapter,
)
from operant.domain.graph import WorkflowDefinition
from operant.domain.multiwriter import (
    MergeRun,
    PatchCommitArtifact,
    WriterLease,
    WriterWorkspace,
)
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


class ResolveMergeConflictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resolution_artifact_ref: str = Field(min_length=1, max_length=500)
    rejected: bool = False


def install_phase56_writer_routes(
    app: FastAPI,
    store: SQLiteStore,
    *,
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
        workspace_id: str, body: AcquireWriterLeaseRequest
    ) -> dict[str, Any]:
        try:
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
            return runtime.renew_lease(
                lease(workspace_id, body.lease), ttl_seconds=body.ttl_seconds
            ).model_dump(mode="json")
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
            return repository.release_lease(lease(workspace_id, body.lease)).model_dump(mode="json")
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
            return runtime.finalize_merge_run(
                merge_run_id,
                definition=definition_for_run(merge.graph_run_id),
                leases=body.leases,
                review_approved=body.review_approved,
            ).model_dump(mode="json")
        except (NotFoundError, KeyError, GraphConflictError, ValueError) as exc:
            raise fail(exc) from exc
