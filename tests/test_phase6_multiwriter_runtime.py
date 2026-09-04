from __future__ import annotations

import hashlib
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from operant.api import create_app
from operant.application.graph import (
    GraphCompilationError,
    GraphCompiler,
    GraphConflictError,
    GraphRuntime,
)
from operant.application.multiwriter import MergeOutcomeUnknownError, MultiWriterRuntime
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
from operant.multiwriter import TrustedGitMultiWriterAdapter
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
    def __init__(
        self, *, fail: bool = False, outcome_unknown: bool = False, root: Path | None = None
    ) -> None:
        self.fail = fail
        self.outcome_unknown = outcome_unknown
        self.rolled_back = False
        self.root = root

    def merge(self, **_: object) -> str:
        if self.outcome_unknown:
            raise MergeOutcomeUnknownError("target interference")
        if self.fail:
            raise RuntimeError("deterministic failure")
        return "isolated-merge:result"

    def rollback(self, **_: object) -> None:
        self.rolled_back = True

    def workspace_path(self, _isolation_ref: str) -> Path:
        if self.root is None:
            raise ValueError("test adapter has no configured root")
        return self.root


def git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


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
    runtime, repository, _, _, workspaces = setup_runtime(tmp_path, definition())
    lease = runtime.acquire_lease(workspaces["a"].writer_workspace_id, owner="coder-a")
    with repository.store._connect() as connection:
        persisted_token = connection.execute(
            "SELECT token_hash FROM writer_leases WHERE writer_workspace_id=?",
            (lease.writer_workspace_id,),
        ).fetchone()[0]
    assert persisted_token != lease.token
    assert len(persisted_token) == 64
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

    adapter.fail = False
    adapter.outcome_unknown = True
    adapter.rolled_back = False
    third = repository.create_merge_run(
        merge.model_copy(
            update={
                "merge_run_id": "merge_run_unknown",
                "status": MergeRunStatus.CREATED,
                "expected_revision": 0,
                "result_artifact_ref": None,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
        )
    )
    unknown = runtime.finalize_merge_run(
        third.merge_run_id,
        definition=definition(),
        leases=leases,
        review_approved=True,
    )
    assert unknown.status is MergeRunStatus.OUTCOME_UNKNOWN
    assert unknown.error_code == "merge.manual_reconcile_required"
    assert adapter.rolled_back is False


def test_expired_lease_cannot_finalize_merge(tmp_path: Path) -> None:
    runtime, _, _, _, workspaces = setup_runtime(tmp_path, definition())
    lease = runtime.acquire_lease(workspaces["a"].writer_workspace_id, owner="coder-a")
    expired = WriterLease(
        **lease.model_dump(exclude={"expires_at"}),
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    with pytest.raises(GraphConflictError, match="fenced"):
        runtime.repository.assert_lease(expired.model_copy(update={"token": "x" * 16}))


def test_trusted_git_adapter_verifies_and_merges_isolated_commits(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "-b", "main")
    git(repository, "config", "user.name", "Test Writer")
    git(repository, "config", "user.email", "writer@example.invalid")
    (repository / "src" / "a").mkdir(parents=True)
    (repository / "src" / "b").mkdir(parents=True)
    (repository / "src" / "a" / "main.py").write_text("A = 0\n", encoding="utf-8")
    (repository / "src" / "b" / "main.py").write_text("B = 0\n", encoding="utf-8")
    git(repository, "add", "src")
    git(repository, "commit", "-m", "base")
    base = git(repository, "rev-parse", "HEAD")

    roots = {
        "worktree:a": tmp_path / "writer-a",
        "worktree:b": tmp_path / "writer-b",
        "worktree:merge": tmp_path / "merge",
        "worktree:polluted": tmp_path / "polluted-merge",
    }
    git(repository, "worktree", "add", "-b", "writer-a", str(roots["worktree:a"]), base)
    git(repository, "worktree", "add", "-b", "writer-b", str(roots["worktree:b"]), base)
    git(repository, "worktree", "add", "-b", "merge", str(roots["worktree:merge"]), base)
    git(
        repository,
        "worktree",
        "add",
        "-b",
        "polluted-merge",
        str(roots["worktree:polluted"]),
        base,
    )

    revisions: dict[str, str] = {}
    artifacts: list[PatchCommitArtifact] = []
    workspaces: list[WriterWorkspace] = []
    for writer in ("a", "b"):
        root = roots[f"worktree:{writer}"]
        path = f"src/{writer}/main.py"
        (root / path).write_text(f"{writer.upper()} = 1\n", encoding="utf-8")
        git(root, "add", path)
        git(root, "commit", "-m", f"writer {writer}")
        revision = git(root, "rev-parse", "HEAD")
        revisions[writer] = revision
        content = subprocess.run(
            ["git", "diff", "--binary", base, revision],
            cwd=root,
            check=True,
            capture_output=True,
        ).stdout
        workspace = WriterWorkspace(
            writer_workspace_id=f"workspace-{writer}",
            graph_run_id="graph-run",
            node_run_id=f"node-{writer}",
            writer_key=writer,
            isolation_kind=WriterIsolationKind.WORKTREE,
            isolation_ref=f"worktree:{writer}",
            base_revision=base,
            ownership_paths=(f"src/{writer}",),
        )
        workspaces.append(workspace)
        artifacts.append(
            PatchCommitArtifact(
                writer_artifact_id=f"artifact-{writer}",
                writer_workspace_id=workspace.writer_workspace_id,
                artifact_kind=WriterArtifactKind.COMMIT,
                artifact_ref=f"git:{revision}",
                artifact_sha256=hashlib.sha256(content).hexdigest(),
                base_revision=base,
                result_revision=revision,
                changed_paths=(path,),
            )
        )

    adapter = TrustedGitMultiWriterAdapter(roots)
    for workspace, item in zip(workspaces, artifacts, strict=True):
        adapter.verify(workspace, item)
    with pytest.raises(ValueError, match="only accepts worktree"):
        adapter.verify(
            workspaces[0].model_copy(update={"isolation_kind": WriterIsolationKind.CONTAINER}),
            artifacts[0],
        )
    result = adapter.merge(
        merge=MergeRun(
            merge_run_id="merge-run",
            graph_run_id="graph-run",
            merge_node_id="merge-node",
            artifact_ids=tuple(item.writer_artifact_id for item in artifacts),
            strategy=MergeStrategy.CHERRY_PICK,
            target_isolation_ref="worktree:merge",
            base_revision=base,
        ),
        artifacts=tuple(artifacts),
        workspaces=tuple(workspaces),
        resolutions=(),
    )
    assert result == f"git:{git(roots['worktree:merge'], 'rev-parse', 'HEAD')}"
    assert (roots["worktree:merge"] / "src/a/main.py").read_text() == "A = 1\n"
    assert (roots["worktree:merge"] / "src/b/main.py").read_text() == "B = 1\n"

    original_assert = adapter._assert_target_identity
    injected = False

    def assert_then_pollute(target: Path, baseline: str) -> None:
        nonlocal injected
        original_assert(target, baseline)
        if target == roots["worktree:polluted"] and not injected:
            injected = True
            (target / "outside-artifacts.txt").write_text("intrusion\n", encoding="utf-8")
            git(target, "add", "outside-artifacts.txt")

    adapter._assert_target_identity = assert_then_pollute  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="inspect it before retrying"):
        adapter.merge(
            merge=MergeRun(
                merge_run_id="polluted-merge-run",
                graph_run_id="graph-run",
                merge_node_id="merge-node",
                artifact_ids=tuple(item.writer_artifact_id for item in artifacts),
                strategy=MergeStrategy.CHERRY_PICK,
                target_isolation_ref="worktree:polluted",
                base_revision=base,
            ),
            artifacts=tuple(artifacts),
            workspaces=tuple(workspaces),
            resolutions=(),
        )
    assert git(roots["worktree:polluted"], "rev-parse", "HEAD") == base
    assert (roots["worktree:polluted"] / "outside-artifacts.txt").read_text() == "intrusion\n"


def test_trusted_git_adapter_rejects_missing_commit_and_checksum_tampering(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "-b", "main")
    git(repository, "config", "user.name", "Test Writer")
    git(repository, "config", "user.email", "writer@example.invalid")
    (repository / "owned.txt").write_text("base\n", encoding="utf-8")
    git(repository, "add", "owned.txt")
    git(repository, "commit", "-m", "base")
    base = git(repository, "rev-parse", "HEAD")
    adapter = TrustedGitMultiWriterAdapter({"worktree:a": repository})
    workspace = WriterWorkspace(
        writer_workspace_id="workspace-a",
        graph_run_id="graph-run",
        node_run_id="node-a",
        writer_key="a",
        isolation_kind=WriterIsolationKind.WORKTREE,
        isolation_ref="worktree:a",
        base_revision=base,
        ownership_paths=("owned.txt",),
    )
    missing = PatchCommitArtifact(
        writer_workspace_id=workspace.writer_workspace_id,
        artifact_kind=WriterArtifactKind.COMMIT,
        artifact_ref=f"git:{'f' * 40}",
        artifact_sha256="0" * 64,
        base_revision=base,
        result_revision="f" * 40,
        changed_paths=("owned.txt",),
    )
    with pytest.raises(RuntimeError, match="isolated Git operation failed"):
        adapter.verify(workspace, missing)

    (repository / "owned.txt").write_text("changed\n", encoding="utf-8")
    git(repository, "add", "owned.txt")
    git(repository, "commit", "-m", "change")
    result = git(repository, "rev-parse", "HEAD")
    tampered = missing.model_copy(
        update={"artifact_ref": f"git:{result}", "result_revision": result}
    )
    with pytest.raises(ValueError, match="checksum"):
        adapter.verify(workspace, tampered)


def test_patch_merge_uses_verified_immutable_snapshots(tmp_path: Path) -> None:
    repository = tmp_path / "patch-repository"
    repository.mkdir()
    git(repository, "init", "-b", "main")
    git(repository, "config", "user.name", "Test Writer")
    git(repository, "config", "user.email", "writer@example.invalid")
    for writer in ("a", "b"):
        path = repository / "src" / writer / "main.py"
        path.parent.mkdir(parents=True)
        path.write_text(f"{writer.upper()} = 0\n", encoding="utf-8")
    git(repository, "add", "src")
    git(repository, "commit", "-m", "base")
    base = git(repository, "rev-parse", "HEAD")
    roots = {
        "worktree:a": tmp_path / "patch-writer-a",
        "worktree:b": tmp_path / "patch-writer-b",
        "worktree:merge": tmp_path / "patch-merge",
    }
    for writer in ("a", "b"):
        git(
            repository,
            "worktree",
            "add",
            "-b",
            f"patch-{writer}",
            str(roots[f"worktree:{writer}"]),
            base,
        )
    git(repository, "worktree", "add", "-b", "patch-merge", str(roots["worktree:merge"]), base)

    workspaces: list[WriterWorkspace] = []
    artifacts: list[PatchCommitArtifact] = []
    for writer in ("a", "b"):
        root = roots[f"worktree:{writer}"]
        relative = f"src/{writer}/main.py"
        (root / relative).write_text(f"{writer.upper()} = 1\n", encoding="utf-8")
        patch = subprocess.run(
            ["git", "diff", "--binary", "--", relative],
            cwd=root,
            check=True,
            capture_output=True,
        ).stdout
        git(root, "restore", relative)
        patch_path = root / "writer.patch"
        patch_path.write_bytes(patch)
        workspace = WriterWorkspace(
            writer_workspace_id=f"patch-workspace-{writer}",
            graph_run_id="patch-graph-run",
            node_run_id=f"patch-node-{writer}",
            writer_key=writer,
            isolation_kind=WriterIsolationKind.WORKTREE,
            isolation_ref=f"worktree:{writer}",
            base_revision=base,
            ownership_paths=(f"src/{writer}",),
        )
        workspaces.append(workspace)
        artifacts.append(
            PatchCommitArtifact(
                writer_artifact_id=f"patch-artifact-{writer}",
                writer_workspace_id=workspace.writer_workspace_id,
                artifact_kind=WriterArtifactKind.PATCH,
                artifact_ref="patch:writer.patch",
                artifact_sha256=hashlib.sha256(patch).hexdigest(),
                base_revision=base,
                changed_paths=(relative,),
            )
        )

    adapter = TrustedGitMultiWriterAdapter(roots)
    original_verify = adapter._verify

    def verify_then_replace(workspace: WriterWorkspace, item: PatchCommitArtifact):
        verified = original_verify(workspace, item)
        if workspace.writer_key == "a":
            root = roots[workspace.isolation_ref]
            relative = "src/a/main.py"
            (root / relative).write_text("A = 999\n", encoding="utf-8")
            replacement = subprocess.run(
                ["git", "diff", "--binary", "--", relative],
                cwd=root,
                check=True,
                capture_output=True,
            ).stdout
            (root / "writer.patch").write_bytes(replacement)
            git(root, "restore", relative)
        return verified

    adapter._verify = verify_then_replace  # type: ignore[method-assign]
    adapter.merge(
        merge=MergeRun(
            merge_run_id="patch-merge-run",
            graph_run_id="patch-graph-run",
            merge_node_id="patch-merge-node",
            artifact_ids=tuple(item.writer_artifact_id for item in artifacts),
            strategy=MergeStrategy.APPLY_PATCH,
            target_isolation_ref="worktree:merge",
            base_revision=base,
        ),
        artifacts=tuple(artifacts),
        workspaces=tuple(workspaces),
        resolutions=(),
    )
    assert (roots["worktree:merge"] / "src/a/main.py").read_text() == "A = 1\n"
    assert (roots["worktree:merge"] / "src/b/main.py").read_text() == "B = 1\n"


def test_only_one_running_merge_is_admitted_per_target(tmp_path: Path) -> None:
    _runtime, repository, _adapter, run, _workspaces = setup_runtime(tmp_path, definition())
    now = datetime.now(timezone.utc)
    first = repository.create_merge_run(
        MergeRun(
            merge_run_id="exclusive-first",
            graph_run_id=run.id,
            merge_node_id="merge",
            artifact_ids=("artifact-a", "artifact-b"),
            target_isolation_ref="worktree:exclusive-target",
            base_revision="abcdef0",
            created_at=now,
            updated_at=now,
        )
    )
    second = repository.create_merge_run(
        first.model_copy(update={"merge_run_id": "exclusive-second"})
    )
    repository.claim_merge_run(
        first,
        execution_owner_id="core-first",
        execution_lease_expires_at=now + timedelta(minutes=1),
    )
    with pytest.raises(GraphConflictError, match="exclusive running merge"):
        repository.claim_merge_run(
            second,
            execution_owner_id="core-second",
            execution_lease_expires_at=now + timedelta(minutes=1),
        )


def test_expired_merge_owner_requires_manual_reconciliation_and_releases_target(
    tmp_path: Path,
) -> None:
    _runtime, repository, adapter, run, _workspaces = setup_runtime(tmp_path, definition())
    now = datetime.now(timezone.utc)
    first = repository.create_merge_run(
        MergeRun(
            merge_run_id="crashed-merge",
            graph_run_id=run.id,
            merge_node_id="merge",
            artifact_ids=("artifact-a", "artifact-b"),
            target_isolation_ref="worktree:recovery-target",
            base_revision="abcdef0",
            created_at=now,
            updated_at=now,
        )
    )
    running = repository.claim_merge_run(
        first,
        execution_owner_id="dead-core",
        execution_lease_expires_at=now - timedelta(seconds=1),
    )

    restarted = MultiWriterRuntime(
        repository,
        merge_adapter=adapter,
        execution_owner_id="new-core",
    )
    unknown = repository.get_merge_run(first.merge_run_id)
    assert unknown.status is MergeRunStatus.OUTCOME_UNKNOWN
    assert unknown.error_code == "merge.manual_reconcile_required"
    with pytest.raises(GraphConflictError, match="owned completion conflicts"):
        repository.complete_owned_merge_run(
            running.model_copy(
                update={
                    "status": MergeRunStatus.SUCCEEDED,
                    "expected_revision": running.expected_revision + 1,
                    "result_artifact_ref": "git:late",
                }
            ),
            expected_revision=running.expected_revision,
            execution_owner_id="dead-core",
        )
    reconciled = restarted.reconcile_merge_run(
        first.merge_run_id,
        expected_revision=unknown.expected_revision,
        status=MergeRunStatus.FAILED,
        error_code="operator_verified_failure",
    )
    assert reconciled.status is MergeRunStatus.FAILED

    second = repository.create_merge_run(
        first.model_copy(update={"merge_run_id": "after-recovery"})
    )
    claimed = repository.claim_merge_run(
        second,
        execution_owner_id="new-core",
        execution_lease_expires_at=now + timedelta(minutes=1),
    )
    assert claimed.status is MergeRunStatus.RUNNING


def test_merge_api_requires_action_gateway_approval_before_git_side_effect(
    tmp_path: Path,
) -> None:
    merge_root = tmp_path / "merge-target"
    merge_root.mkdir()
    adapter = FakeMergeAdapter(root=merge_root)
    app = create_app(tmp_path / "api.sqlite3", phase56_merge_adapter=adapter)
    store = app.state.operant_service.store
    graph = definition()
    graph_repository = SQLiteGraphRepository(store)
    graph_repository.put_definition(graph)
    run = GraphRuntime(graph_repository).create_run(graph)
    node_runs = {item.node_id: item for item in graph_repository.list_node_runs(run.id)}
    runtime = app.state.multiwriter_runtime
    workspaces: dict[str, WriterWorkspace] = {}
    for writer in ("a", "b"):
        node = next(item for item in graph.nodes if item.node_id == f"writer-{writer}")
        assert node.writer_policy is not None
        workspaces[writer] = runtime.create_workspace(
            WriterWorkspace(
                graph_run_id=run.id,
                node_run_id=node_runs[f"writer-{writer}"].id,
                writer_key=writer,
                isolation_kind=node.writer_policy.isolation_kind,
                isolation_ref=node.writer_policy.isolation_ref,
                base_revision="abcdef0",
                ownership_paths=node.writer_policy.ownership_paths,
            )
        )
    leases = tuple(
        runtime.acquire_lease(workspaces[writer].writer_workspace_id, owner=f"coder-{writer}")
        for writer in ("a", "b")
    )
    artifacts = tuple(
        runtime.publish_artifact(
            artifact(workspaces[writer], f"src/{writer}/main.py"), lease=leases[index]
        )
        for index, writer in enumerate(("a", "b"))
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
        definition=graph,
        leases=leases,
    )
    with TestClient(app) as client:
        response = client.post(
            f"/v1/merge-runs/{merge.merge_run_id}/finalize",
            headers={"Idempotency-Key": "merge-finalize-gated"},
            json={
                "leases": [item.model_dump(mode="json") for item in leases],
                "review_approved": True,
            },
        )
    assert response.status_code == 409
    assert "approval_required" in response.text
    assert adapter.rolled_back is False
    assert app.state.multiwriter_repository.get_merge_run(merge.merge_run_id).status is (
        MergeRunStatus.CREATED
    )
    with store._connect() as connection:
        guarded = connection.execute(
            "SELECT action_hash FROM security_action_requests "
            "WHERE tool='multiwriter' AND operation='finalize_merge'"
        ).fetchall()
    assert len(guarded) == 1
