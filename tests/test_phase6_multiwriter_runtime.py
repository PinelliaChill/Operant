from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from operant.application.graph import (
    GraphCompilationError,
    GraphCompiler,
    GraphConflictError,
    GraphRuntime,
)
from operant.application.multiwriter import MultiWriterRuntime
from operant.domain.graph import (
    EdgeSpec,
    IdempotencyClass,
    NodeKind,
    NodeSpec,
    PortSpec,
    WorkflowDefinition,
    WorkflowDefinitionStatus,
)
from operant.domain.multiwriter import (
    MergeNodePolicy,
    MergeRun,
    MergeRunStatus,
    MergeStrategy,
    PatchCommitArtifact,
    WriterArtifactKind,
    WriterIsolationKind,
    WriterLease,
    WriterNodePolicy,
    WriterWorkspace,
)
from operant.persistence.graph_team import SQLiteGraphRepository
from operant.persistence.multiwriter import SQLiteMultiWriterRepository
from operant.persistence.sqlite import SQLiteStore


def definition(*, overlap: bool = False, require_review: bool = False) -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id="multi-writer",
        version=1,
        name="multi writer",
        status=WorkflowDefinitionStatus.PUBLISHED,
        nodes=(
            NodeSpec(
                node_id="writer-a",
                node_kind=NodeKind.AGENT,
                output_ports=(PortSpec(name="artifact"),),
                writes_workspace=True,
                idempotency_class=IdempotencyClass.IDEMPOTENT,
                writer_policy=WriterNodePolicy(
                    writer_key="a",
                    isolation_kind=WriterIsolationKind.WORKTREE,
                    isolation_ref="worktree:a",
                    ownership_paths=("src/shared" if overlap else "src/a",),
                ),
            ),
            NodeSpec(
                node_id="writer-b",
                node_kind=NodeKind.AGENT,
                output_ports=(PortSpec(name="artifact"),),
                writes_workspace=True,
                idempotency_class=IdempotencyClass.IDEMPOTENT,
                writer_policy=WriterNodePolicy(
                    writer_key="b",
                    isolation_kind=WriterIsolationKind.CONTAINER,
                    isolation_ref="container:b",
                    ownership_paths=("src/shared" if overlap else "src/b",),
                ),
            ),
            NodeSpec(
                node_id="merge",
                node_kind=NodeKind.MERGE,
                input_ports=(PortSpec(name="artifacts"),),
                writes_workspace=True,
                idempotency_class=IdempotencyClass.IDEMPOTENT,
                merge_policy=MergeNodePolicy(
                    source_writer_keys=("a", "b"), require_review=require_review
                ),
            ),
        ),
        edges=(
            EdgeSpec(
                edge_id="a-merge",
                source_node="writer-a",
                source_port="artifact",
                target_node="merge",
                target_port="artifacts",
            ),
            EdgeSpec(
                edge_id="b-merge",
                source_node="writer-b",
                source_port="artifact",
                target_node="merge",
                target_port="artifacts",
            ),
        ),
    )


class FakeArtifactAdapter:
    def verify(self, workspace: WriterWorkspace, artifact: PatchCommitArtifact) -> None:
        assert artifact.artifact_ref.startswith(workspace.isolation_ref)


class FakeMergeAdapter:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.rolled_back = False

    def merge(self, **_: object) -> str:
        if self.fail:
            raise RuntimeError("deterministic failure")
        return "isolated-merge:result"

    def rollback(self, **_: object) -> None:
        self.rolled_back = True


def setup_runtime(tmp_path: Path, graph: WorkflowDefinition):
    store = SQLiteStore(tmp_path / "operant.sqlite3")
    store.initialize()
    graph_repository = SQLiteGraphRepository(store)
    graph_repository.put_definition(graph)
    run = GraphRuntime(graph_repository).create_run(graph)
    node_runs = {item.node_id: item for item in graph_repository.list_node_runs(run.id)}
    repository = SQLiteMultiWriterRepository(store)
    adapter = FakeMergeAdapter()
    runtime = MultiWriterRuntime(
        repository, artifact_adapter=FakeArtifactAdapter(), merge_adapter=adapter
    )
    workspaces = {}
    for node_id in ("writer-a", "writer-b"):
        policy = next(item for item in graph.nodes if item.node_id == node_id).writer_policy
        assert policy is not None
        workspace = WriterWorkspace(
            graph_run_id=run.id,
            node_run_id=node_runs[node_id].id,
            writer_key=policy.writer_key,
            isolation_kind=policy.isolation_kind,
            isolation_ref=policy.isolation_ref,
            base_revision="abcdef0",
            ownership_paths=policy.ownership_paths,
        )
        workspaces[policy.writer_key] = runtime.create_workspace(workspace)
    return runtime, repository, adapter, run, workspaces


def artifact(workspace: WriterWorkspace, path: str) -> PatchCommitArtifact:
    return PatchCommitArtifact(
        writer_workspace_id=workspace.writer_workspace_id,
        artifact_kind=WriterArtifactKind.PATCH,
        artifact_ref=f"{workspace.isolation_ref}:patch",
        artifact_sha256="a" * 64,
        base_revision=workspace.base_revision,
        changed_paths=(path,),
        test_evidence_refs=(f"{workspace.writer_key}:pytest",),
    )


def test_compiler_allows_typed_isolated_writers_with_explicit_merge() -> None:
    compiled = GraphCompiler().compile(definition())
    assert compiled.terminal_node_ids == ("merge",)


def test_compiler_rejects_ownership_overlap() -> None:
    with pytest.raises(GraphCompilationError) as caught:
        GraphCompiler().compile(definition(overlap=True))
    assert "writer_ownership_overlap" in {issue.code for issue in caught.value.issues}


def test_fenced_lease_cannot_publish_artifact(tmp_path: Path) -> None:
    runtime, _, _, _, workspaces = setup_runtime(tmp_path, definition())
    lease = runtime.acquire_lease(workspaces["a"].writer_workspace_id, owner="coder-a")
    stale = lease.model_copy(update={"fencing": lease.fencing + 1})
    with pytest.raises(GraphConflictError, match="fenced"):
        runtime.publish_artifact(artifact(workspaces["a"], "src/a/main.py"), lease=stale)


def test_detects_same_path_and_stale_base_conflicts(tmp_path: Path) -> None:
    runtime, repository, _, run, workspaces = setup_runtime(tmp_path, definition())
    # Runtime detector remains defensive even if malformed ownership bypassed the compiler.
    b = workspaces["b"].model_copy(update={"base_revision": "1234567"})
    with repository.store._connect() as connection:
        connection.execute(
            "UPDATE writer_workspaces SET base_revision = ?, ownership_paths_json = ? "
            "WHERE writer_workspace_id = ?",
            ("1234567", '["src/a"]', b.writer_workspace_id),
        )
    workspaces["b"] = repository.get_workspace(b.writer_workspace_id)
    leases = {
        key: runtime.acquire_lease(item.writer_workspace_id, owner=f"coder-{key}")
        for key, item in workspaces.items()
    }
    runtime.publish_artifact(artifact(workspaces["a"], "src/a/main.py"), lease=leases["a"])
    runtime.publish_artifact(artifact(workspaces["b"], "src/a/main.py"), lease=leases["b"])
    conflicts = runtime.detect_conflicts(run.id)
    assert len(conflicts) == 1
    assert conflicts[0].paths == ("src/a", "src/a/main.py")


def test_merge_uses_isolated_adapter_and_rolls_back_on_failure(tmp_path: Path) -> None:
    runtime, repository, adapter, run, workspaces = setup_runtime(tmp_path, definition())
    leases = tuple(
        runtime.acquire_lease(item.writer_workspace_id, owner=f"coder-{key}")
        for key, item in workspaces.items()
    )
    artifacts = (
        runtime.publish_artifact(artifact(workspaces["a"], "src/a/main.py"), lease=leases[0]),
        runtime.publish_artifact(artifact(workspaces["b"], "src/b/main.py"), lease=leases[1]),
    )
    merge = runtime.create_merge_run(
        MergeRun(
            graph_run_id=run.id,
            merge_node_id="merge",
            artifact_ids=tuple(item.writer_artifact_id for item in artifacts),
            strategy=MergeStrategy.THREE_WAY,
            target_isolation_ref="worktree:merge-target",
            base_revision="abcdef0",
        ),
        definition=definition(),
        leases=leases,
    )
    succeeded = runtime.finalize_merge_run(
        merge.merge_run_id,
        definition=definition(),
        leases=leases,
        review_approved=True,
    )
    assert succeeded.status is MergeRunStatus.SUCCEEDED
    assert succeeded.result_artifact_ref == "isolated-merge:result"

    adapter.fail = True
    second = repository.create_merge_run(
        merge.model_copy(
            update={
                "merge_run_id": "merge_run_second",
                "status": MergeRunStatus.CREATED,
                "expected_revision": 0,
                "result_artifact_ref": None,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
        )
    )
    failed = runtime.finalize_merge_run(
        second.merge_run_id,
        definition=definition(),
        leases=leases,
        review_approved=True,
    )
    assert failed.status is MergeRunStatus.ROLLED_BACK
    assert adapter.rolled_back


def test_expired_lease_cannot_finalize_merge(tmp_path: Path) -> None:
    runtime, _, _, _, workspaces = setup_runtime(tmp_path, definition())
    lease = runtime.acquire_lease(workspaces["a"].writer_workspace_id, owner="coder-a")
    expired = WriterLease(
        **lease.model_dump(exclude={"expires_at"}),
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    with pytest.raises(GraphConflictError, match="fenced"):
        runtime.repository.assert_lease(expired.model_copy(update={"token": "x" * 16}))
