from __future__ import annotations

import hashlib
import subprocess
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from operant.contracts.b2_1 import (
    DatasetOwner,
    MemoryConditions,
    MemoryVersion,
    MemoryVersionRef,
    PluginManifest,
    RunScope,
    SourceRef,
    WorkspaceScope,
)
from operant.contracts.b2_6_sharing import (
    ProjectWorktreeRegistration,
    SharingCommand,
    SharingGrant,
)
from operant.domain.commands import WorkspaceInitialization
from operant.domain.multiwriter import (
    MergeRun,
    MergeRunStatus,
    PatchCommitArtifact,
    WriterArtifactKind,
    WriterIsolationKind,
    WriterWorkspace,
)
from operant.memory_plugins.ledger import MemoryLedger
from operant.memory_plugins.sharing import (
    SharingPermissionError,
    SharingService,
)
from operant.multiwriter import TrustedGitMultiWriterAdapter
from operant.persistence.multiwriter import SQLiteMultiWriterRepository
from operant.persistence.sqlite import SQLiteStore
from operant.plugins import PluginRegistry, compute_package_digest


def _create_writer_graph(
    store: SQLiteStore, *, writer_ids: tuple[str, ...] = ("writer-a", "writer-b")
) -> tuple[str, dict[str, str]]:
    from operant.application.graph import GraphRuntime
    from operant.domain.graph import (
        EdgeSpec,
        IdempotencyClass,
        NodeKind,
        NodeSpec,
        PortSpec,
        WorkflowDefinition,
        WorkflowDefinitionStatus,
    )
    from operant.domain.multiwriter import MergeNodePolicy, WriterNodePolicy

    writer_nodes = tuple(
        NodeSpec(
            node_id=writer_id,
            node_kind=NodeKind.AGENT,
            output_ports=(PortSpec(name="artifact"),),
            writes_workspace=True,
            idempotency_class=IdempotencyClass.IDEMPOTENT,
            writer_policy=WriterNodePolicy(
                writer_key=writer_id,
                isolation_kind=WriterIsolationKind.WORKTREE,
                isolation_ref=f"writer-isolation-{writer_id}",
                ownership_paths=(f"src/{writer_id.rsplit('-', 1)[-1]}",),
            ),
        )
        for writer_id in writer_ids
    )
    graph = WorkflowDefinition(
        workflow_id="b26-writer-test",
        version=1,
        name="B2-6 writer test",
        status=WorkflowDefinitionStatus.PUBLISHED,
        nodes=(
            *writer_nodes,
            NodeSpec(
                node_id="merge",
                node_kind=NodeKind.MERGE,
                input_ports=(PortSpec(name="artifacts"),),
                writes_workspace=True,
                idempotency_class=IdempotencyClass.IDEMPOTENT,
                merge_policy=MergeNodePolicy(
                    source_writer_keys=tuple(writer_ids), require_review=False
                ),
            ),
        ),
        edges=tuple(
            EdgeSpec(
                edge_id=f"{writer_id}-merge",
                source_node=writer_id,
                source_port="artifact",
                target_node="merge",
                target_port="artifacts",
            )
            for writer_id in writer_ids
        ),
    )
    from operant.persistence.graph_team import SQLiteGraphRepository

    graph_repository = SQLiteGraphRepository(store)
    graph_repository.put_definition(graph)
    run = GraphRuntime(graph_repository).create_run(graph)
    node_runs = {node.node_id: node.id for node in graph_repository.list_node_runs(run.id)}
    return run.id, node_runs


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _owner(dataset_id: str, principal_id: str = "owner") -> DatasetOwner:
    return DatasetOwner(
        kind="plugin_dataset",
        owner_namespace=f"dataset:{dataset_id}",
        dataset_id=dataset_id,
        principal_id=principal_id,
    )


def _conditions(*, valid_until: Any = None) -> MemoryConditions:
    from datetime import datetime, timezone

    return MemoryConditions(
        commit_ref=None,
        tree_digest=None,
        file_fingerprints={},
        environment_digest=None,
        tool_versions={},
        verified_at=None,
        valid_from=datetime.now(timezone.utc),
        valid_until=valid_until,
    )


def _version(
    *,
    dataset_id: str,
    record_id: str,
    scope: WorkspaceScope | RunScope,
    owner: DatasetOwner | None = None,
    content: str = "A verified memory",
    number: int = 1,
    role_ids: tuple[str, ...] = (),
    agent_ids: tuple[str, ...] = (),
    sensitivity: str = "internal",
    sources: tuple[SourceRef, ...] | None = None,
    evidence: str = "user_asserted",
    valid_until: Any = None,
) -> MemoryVersion:
    source = SourceRef(
        source_type="artifact",
        source_id="artifact-source",
        revision=1,
        content_digest="a" * 64,
        scope=scope,
        permission_epoch=0,
        availability="available",
    )
    return MemoryVersion(
        ref=MemoryVersionRef(
            dataset_id=dataset_id,
            record_id=record_id,
            version=number,
            content_digest=hashlib.sha256(content.encode()).hexdigest(),
        ),
        owner=owner or _owner(dataset_id),
        kind="project",
        content_type="fact",
        scope=scope,
        role_ids=role_ids,
        agent_ids=agent_ids,
        content=content,
        sources=(source,) if sources is None else sources,
        evidence=evidence,  # type: ignore[arg-type]
        sensitivity=sensitivity,  # type: ignore[arg-type]
        retention_policy_id="retention-default",
        conditions=_conditions(valid_until=valid_until),
        recorded_at=_conditions().valid_from,
    )


class _Context:
    def __init__(
        self,
        store: SQLiteStore,
        registry: Any,
        projects: dict[str, dict[str, Any]],
        ledger: MemoryLedger | None = None,
    ) -> None:
        self.store = store
        self.registry = registry
        self.projects = projects
        self.ledger = ledger
        self._state = {"projects": list(projects.values()), "global_enabled": True}

    def _project(self, project_id: str) -> dict[str, Any]:
        try:
            return self.projects[project_id]
        except KeyError as exc:
            raise ValueError("project not found") from exc

    @staticmethod
    def _scope(project: dict[str, Any]) -> WorkspaceScope:
        return WorkspaceScope(
            kind="workspace",
            project_id=project["project_id"],
            workspace_id=project["workspace_id"],
        )


def _registration(
    project_id: str,
    workspace_id: str,
    worktree_id: str,
    principal_id: str = "client-principal",
) -> ProjectWorktreeRegistration:
    return ProjectWorktreeRegistration(
        registration_id=f"registration-{project_id}",
        project_id=project_id,
        workspace_id=workspace_id,
        worktree_id=worktree_id,
        workspace_ref="workspace:" + "b" * 64,
        branch_ref="main",
        commit_ref="git:abcdef0",
        tree_digest="c" * 64,
        principal_id=principal_id,
    )


def _publish(ledger: MemoryLedger, version: MemoryVersion) -> MemoryVersion:
    saved = ledger.save_version(version)
    proposal = ledger.propose(saved)
    ledger.confirm_proposal(proposal, expected_head_revision=0)
    return saved


def _service(
    tmp_path: Path,
    *,
    registry: Any = None,
    projects: dict[str, dict[str, Any]] | None = None,
) -> tuple[SharingService, SQLiteStore, MemoryLedger]:
    store = SQLiteStore(tmp_path / "operant.sqlite3")
    store.initialize()
    ledger = MemoryLedger(store.path, initialize=False)
    projects = projects or {"project-a": {"project_id": "project-a", "workspace_id": "workspace-a"}}
    context = _Context(store, registry, projects, ledger)
    return (
        SharingService(
            context,
            ledger=ledger,
            principal_resolver=lambda project_id: (
                registry.get_installation(
                    projects[project_id]["installation_id"]
                ).owner.principal_id
                if registry is not None and projects[project_id].get("installation_id")
                else "owner"
            ),
            writer_repository=SQLiteMultiWriterRepository(store),
        ),
        store,
        ledger,
    )


def test_cross_project_grant_is_explicit_and_revocable(tmp_path: Path) -> None:
    service, _store, ledger = _service(
        tmp_path,
        projects={
            "project-a": {"project_id": "project-a", "workspace_id": "workspace-a"},
            "project-b": {"project_id": "project-b", "workspace_id": "workspace-b"},
        },
    )
    service.register_worktree(_registration("project-a", "workspace-a", "worktree-a"))
    service.register_worktree(_registration("project-b", "workspace-b", "worktree-b"))
    source_scope = WorkspaceScope(
        kind="workspace", project_id="project-a", workspace_id="workspace-a"
    )
    target_scope = WorkspaceScope(
        kind="workspace", project_id="project-b", workspace_id="workspace-b"
    )
    version = _publish(
        ledger,
        _version(dataset_id="dataset-a", record_id="memory-a", scope=source_scope),
    )
    grant = service.create_grant(
        SharingGrant(
            project_id="project-a",
            source_dataset_id="dataset-a",
            source_scope=source_scope,
            target_scope=target_scope,
            subject_id="owner-b",
            grantor_id="client-grantor",
            purpose="share",
            memory_refs=(version.ref,),
            expires_at=version.recorded_at + timedelta(hours=1),
        )
    )
    decision = service.authorize(
        version.ref,
        target_scope=target_scope,
        subject_id="owner-b",
        purpose="recall",
    )
    assert decision.allowed is True
    assert decision.grant_id == grant.grant_id
    with pytest.raises(ValueError):
        SharingGrant(
            project_id="project-a",
            source_dataset_id="dataset-a",
            source_scope=source_scope,
            target_scope=target_scope,
            subject_id="owner-b",
            grantor_id="owner",
            purpose="share",
            memory_refs=(),
            expires_at=version.recorded_at + timedelta(hours=1),
        )
    service.revoke_grant(grant.grant_id, expected_revision=0, reason="access ended")
    assert (
        service.authorize(
            version.ref,
            target_scope=target_scope,
            subject_id="owner-b",
            purpose="recall",
        ).allowed
        is False
    )


def test_personal_preference_is_published_and_projected_as_exact_ref(tmp_path: Path) -> None:
    class _Install:
        owner = _owner("dataset-a", "owner-a")
        permission_epoch = 0
        dataset_id = "dataset-a"

    class _Registry:
        def get_installation(self, _installation_id: str) -> Any:
            return _Install()

    service, _store, ledger = _service(
        tmp_path,
        registry=_Registry(),
        projects={
            "project-a": {
                "project_id": "project-a",
                "workspace_id": "workspace-a",
                "installation_id": "installation-a",
            }
        },
    )
    source_scope = WorkspaceScope(
        kind="workspace", project_id="project-a", workspace_id="workspace-a"
    )
    source = _publish(
        ledger,
        _version(
            dataset_id="dataset-a",
            record_id="preference-source",
            scope=source_scope,
            owner=_owner("dataset-a", "owner-a"),
            role_ids=("role-a",),
            agent_ids=("agent-a",),
            sensitivity="sensitive",
            sources=(),
            evidence="legacy_unverified",
        ),
    )
    source_ref = SourceRef(
        source_type="memory_version",
        source_id=source.ref.record_id,
        revision=source.ref.version,
        content_digest=source.ref.content_digest,
        scope=source.scope,
        permission_epoch=0,
        availability="available",
    )
    preference = service.create_personal_preference(
        project_id="project-a",
        content="回答时先给结论，再给最小必要步骤",
        source_refs=(source_ref,),
        record_id="response-style",
    )
    assert preference.kind == "episodic"
    assert preference.content_type == "preference"
    assert preference.evidence == "user_asserted"
    assert preference.role_ids == ("role-a",)
    assert preference.agent_ids == ("agent-a",)
    assert preference.sensitivity == "sensitive"
    assert preference.scope.kind == "personal"
    assert preference.scope.principal_id == "owner-a"
    assert ledger.get_head("dataset-a", "response-style").published_version == preference.ref
    assert service.state("project-a").personal_preferences == (preference,)


def test_same_scope_visibility_rechecks_epoch_and_explicit_grant_state(tmp_path: Path) -> None:
    class _Install:
        owner = _owner("dataset-a", "owner-a")
        dataset_id = "dataset-a"

        def __init__(self, registry: Any) -> None:
            self._registry = registry

        @property
        def permission_epoch(self) -> int:
            return self._registry.epoch

    class _Registry:
        epoch = 0

        def get_installation(self, _installation_id: str) -> Any:
            return _Install(self)

    registry = _Registry()
    service, _store, ledger = _service(
        tmp_path,
        registry=registry,
        projects={
            "project-a": {
                "project_id": "project-a",
                "workspace_id": "workspace-a",
                "installation_id": "installation-a",
            }
        },
    )
    service.register_worktree(_registration("project-a", "workspace-a", "worktree-a"))
    scope = WorkspaceScope(kind="workspace", project_id="project-a", workspace_id="workspace-a")
    version = _publish(
        ledger,
        _version(dataset_id="dataset-a", record_id="same-scope", scope=scope),
    )
    grant = service.create_grant(
        SharingGrant(
            project_id="project-a",
            source_dataset_id="dataset-a",
            source_scope=scope,
            target_scope=scope,
            subject_id="owner-a",
            grantor_id="client-grantor",
            purpose="recall",
            memory_refs=(version.ref,),
            expires_at=version.recorded_at + timedelta(hours=1),
        )
    )
    assert service.authorize(
        version.ref,
        target_scope=scope,
        subject_id="owner-a",
        permission_epoch=0,
    ).allowed
    registry.epoch = 1
    stale = service.authorize(
        version.ref,
        target_scope=scope,
        subject_id="owner-a",
        permission_epoch=0,
    )
    assert stale.allowed is False
    assert stale.status == "permission_epoch_mismatch"
    # Supplying a grant id cannot bypass the same source epoch check.
    assert (
        service.authorize(
            version.ref,
            target_scope=scope,
            subject_id="owner-a",
            permission_epoch=1,
            grant_id=grant.grant_id,
        ).allowed
        is False
    )
    registry.epoch = 0
    service.revoke_grant(grant.grant_id, expected_revision=0, reason="preference revoked")
    assert (
        service.authorize(
            version.ref,
            target_scope=scope,
            subject_id="owner-a",
            grant_id=grant.grant_id,
        ).allowed
        is False
    )


def test_writer_candidate_inherits_source_restrictions(tmp_path: Path) -> None:
    class _Install:
        owner = _owner("dataset-a")
        permission_epoch = 0
        dataset_id = "dataset-a"

    class _Registry:
        def get_installation(self, _installation_id: str) -> Any:
            return _Install()

    service, store, ledger = _service(
        tmp_path,
        registry=_Registry(),
        projects={
            "project-a": {
                "project_id": "project-a",
                "workspace_id": "workspace-a",
                "installation_id": "installation-a",
            }
        },
    )
    service.register_worktree(_registration("project-a", "workspace-a", "worktree-a"))
    repository = SQLiteMultiWriterRepository(store)
    graph_run_id, node_run_ids = _create_writer_graph(store)
    workspace = repository.create_workspace(
        WriterWorkspace(
            writer_workspace_id="writer-a",
            graph_run_id=graph_run_id,
            node_run_id=node_run_ids["writer-a"],
            writer_key="writer-a",
            isolation_kind=WriterIsolationKind.WORKTREE,
            isolation_ref="writer-isolation-a",
            base_revision="abcdef0",
            ownership_paths=("src",),
        )
    )
    source_scope = WorkspaceScope(
        kind="workspace", project_id="project-a", workspace_id="workspace-a"
    )
    expiry = _conditions().valid_from + timedelta(hours=1)
    source = _publish(
        ledger,
        _version(
            dataset_id="dataset-a",
            record_id="source-memory",
            scope=source_scope,
            role_ids=("role-a", "role-b"),
            agent_ids=("agent-a", "agent-b"),
            sensitivity="sensitive",
            sources=(),
            evidence="legacy_unverified",
            valid_until=expiry,
        ),
    )
    source_ref = SourceRef(
        source_type="memory_version",
        source_id=source.ref.record_id,
        revision=source.ref.version,
        content_digest=source.ref.content_digest,
        scope=source.scope,
        permission_epoch=0,
        availability="available",
    )
    candidate = service.propose_writer_memory(
        project_id="project-a",
        worktree_id="worktree-a",
        writer_workspace_id=workspace.writer_workspace_id,
        run_id="run-a",
        content="A derived Writer procedure",
        source_refs=(source_ref,),
    )
    saved = ledger.get_version(
        candidate.memory_ref.dataset_id,
        candidate.memory_ref.record_id,
        candidate.memory_ref.version,
    )
    assert saved.evidence == "inferred"
    assert saved.role_ids == ("role-a", "role-b")
    assert saved.agent_ids == ("agent-a", "agent-b")
    assert saved.sensitivity == "sensitive"
    assert saved.conditions.valid_until == expiry

    weak = _version(
        dataset_id="dataset-a",
        record_id="weak-memory",
        scope=RunScope(
            kind="run",
            project_id="project-a",
            workspace_id="workspace-a",
            run_id="run-a",
            writer_id="writer-a",
        ),
        role_ids=(),
        sensitivity="internal",
        sources=(source_ref,),
    )
    weak = ledger.save_version(weak)
    with pytest.raises(SharingPermissionError, match="role restriction"):
        service.bind_writer_memory(
            weak.ref,
            project_id="project-a",
            worktree_id="worktree-a",
            writer_workspace_id="writer-a",
            run_id="run-a",
        )


def test_real_git_writer_merge_is_required_for_ledger_promotion(tmp_path: Path) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _git(repository_root, "init", "-b", "main")
    _git(repository_root, "config", "user.name", "B2-6 Test")
    _git(repository_root, "config", "user.email", "b26@example.invalid")
    for writer in ("a", "b"):
        path = repository_root / "src" / writer / "main.py"
        path.parent.mkdir(parents=True)
        path.write_text(f"{writer} = 0\n", encoding="utf-8")
    _git(repository_root, "add", "src")
    _git(repository_root, "commit", "-m", "base")
    base = _git(repository_root, "rev-parse", "HEAD")
    writer_roots = {writer: tmp_path / f"writer-{writer}" for writer in ("a", "b")}
    for writer, root in writer_roots.items():
        _git(repository_root, "worktree", "add", "-b", f"writer-{writer}", str(root), base)
        (root / "src" / writer / "main.py").write_text(f"{writer} = 1\n", encoding="utf-8")
        _git(root, "add", "src")
        _git(root, "commit", "-m", f"writer {writer}")
    target_commit = base
    artifacts: list[PatchCommitArtifact] = []
    projects = {
        "project-a": {
            "project_id": "project-a",
            "workspace_id": "workspace-a",
            "installation_id": "installation-a",
        }
    }
    service, store, ledger = _service(
        tmp_path,
        registry=SimpleNamespace(
            get_installation=lambda _id: SimpleNamespace(
                owner=_owner("dataset-a"), permission_epoch=0, dataset_id="dataset-a"
            )
        ),
        projects=projects,
    )
    init, _created = store.register_workspace(
        WorkspaceInitialization(
            workspace_ref=str(repository_root),
            workspace_hash=hashlib.sha256(str(repository_root).encode()).hexdigest(),
            readable=True,
            writable=True,
        )
    )
    projects["project-a"]["workspace_id"] = init.id
    service.register_worktree(
        ProjectWorktreeRegistration(
            project_id="project-a",
            workspace_id=init.id,
            worktree_id="target-worktree",
            workspace_ref=f"workspace:{init.workspace_hash}",
            branch_ref="main",
            commit_ref=f"git:{base}",
            tree_digest="d" * 64,
            principal_id="ignored-client-principal",
        )
    )
    multiwriter = SQLiteMultiWriterRepository(store)
    graph_run_id, node_run_ids = _create_writer_graph(store)
    workspaces: list[WriterWorkspace] = []
    for writer, root in writer_roots.items():
        writer_workspace = multiwriter.create_workspace(
            WriterWorkspace(
                writer_workspace_id=f"writer-{writer}",
                graph_run_id=graph_run_id,
                node_run_id=node_run_ids[f"writer-{writer}"],
                writer_key=writer,
                isolation_kind=WriterIsolationKind.WORKTREE,
                isolation_ref=f"writer-{writer}",
                base_revision=base,
                ownership_paths=(f"src/{writer}",),
            )
        )
        workspaces.append(writer_workspace)
        writer_commit = _git(root, "rev-parse", "HEAD")
        diff = subprocess.run(
            ["git", "diff", "--binary", base, writer_commit],
            cwd=root,
            check=True,
            capture_output=True,
        ).stdout
        artifact = PatchCommitArtifact(
            writer_workspace_id=writer_workspace.writer_workspace_id,
            artifact_kind=WriterArtifactKind.COMMIT,
            artifact_ref=f"git:{writer_commit}",
            artifact_sha256=hashlib.sha256(diff).hexdigest(),
            base_revision=base,
            result_revision=writer_commit,
            changed_paths=(f"src/{writer}/main.py",),
            test_evidence_refs=(f"test:{writer}",),
        )
        lease = multiwriter.acquire_lease(
            writer_workspace.writer_workspace_id,
            owner=f"owner-{writer}",
            token=f"token-{writer}" * 4,
            expires_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc)
            + timedelta(minutes=5),
        )
        multiwriter.publish_artifact(artifact, lease=lease)
        multiwriter.release_lease(lease)
        artifacts.append(artifact)
        _git(repository_root, "cherry-pick", writer_commit)
        target_commit = _git(repository_root, "rev-parse", "HEAD")
    merge = multiwriter.create_merge_run(
        MergeRun(
            merge_run_id="merge-a",
            graph_run_id=graph_run_id,
            merge_node_id="merge-node",
            artifact_ids=tuple(item.writer_artifact_id for item in artifacts),
            target_isolation_ref="target",
            base_revision=base,
            status=MergeRunStatus.SUCCEEDED,
            result_artifact_ref=f"git:{target_commit}",
        )
    )
    adapter = TrustedGitMultiWriterAdapter(
        {
            "writer-a": writer_roots["a"],
            "writer-b": writer_roots["b"],
            "target": repository_root,
        }
    )
    service.writer_adapter = adapter
    from operant.application.service import ApplicationService

    core_service = ApplicationService(
        store,
        provider=cast(Any, None),
        artifact_root=tmp_path / "verification-artifacts",
    )
    service.manager.service = core_service
    candidate_source = _publish(
        ledger,
        _version(
            dataset_id="dataset-a",
            record_id="writer-candidate-source",
            scope=WorkspaceScope(kind="workspace", project_id="project-a", workspace_id=init.id),
            owner=_owner("dataset-a"),
            sources=(),
            evidence="legacy_unverified",
        ),
    )
    candidate_source_ref = SourceRef(
        source_type="memory_version",
        source_id=candidate_source.ref.record_id,
        revision=candidate_source.ref.version,
        content_digest=candidate_source.ref.content_digest,
        scope=candidate_source.scope,
        permission_epoch=0,
        availability="available",
    )
    candidate = _version(
        dataset_id="dataset-a",
        record_id="writer-memory",
        scope=RunScope(
            kind="run",
            project_id="project-a",
            workspace_id=init.id,
            run_id="run-a",
            writer_id="writer-a",
        ),
        owner=_owner("dataset-a"),
        sources=(candidate_source_ref,),
    )
    saved = ledger.save_version(candidate)
    evidence = service.bind_writer_memory(
        saved.ref,
        project_id="project-a",
        worktree_id="target-worktree",
        writer_workspace_id="writer-a",
        run_id="run-a",
        graph_run_id=merge.graph_run_id,
    )
    verified = service.verify_writer_memory(
        evidence.evidence_id,
        merge_run_id=merge.merge_run_id,
        verification_artifact_refs=(),
        expected_revision=evidence.revision,
    )
    assert verified.state == "eligible"
    assert verified.target_tree_digest is not None
    assert len(verified.target_tree_digest) == 64
    assert verified.verification_artifact_refs
    generated_artifact = core_service.get_artifact(verified.verification_artifact_refs[0])
    assert generated_artifact.content_hash
    published = service.promote_writer_memory(
        verified.evidence_id,
        expected_revision=verified.revision,
    )
    assert published.state == "published"
    assert published.published_memory_ref is not None
    promoted = ledger.get_version(
        published.published_memory_ref.dataset_id,
        published.published_memory_ref.record_id,
        published.published_memory_ref.version,
    )
    assert isinstance(promoted.scope, WorkspaceScope)
    assert promoted.sources[0].scope.kind == "workspace"
    assert promoted.evidence == candidate.evidence
    assert ledger.get_head("dataset-a", "writer-memory").published_version == promoted.ref
    with pytest.raises(ValueError):
        SharingCommand(
            action="writer_memory_promote",
            project_id="project-a",
            evidence_id=published.evidence_id,
            published_memory_ref=promoted.ref,  # type: ignore[call-arg]
        )
    core_service.close()


def _plugin_package(root: Path) -> tuple[Path, PluginManifest]:
    package = root / "package"
    package.mkdir(parents=True)
    dependencies = b'{"stdlib_only":true}\n'
    permissions = b'{"capabilities":[]}\n'
    (package / "dependencies.json").write_bytes(dependencies)
    (package / "permissions.json").write_bytes(permissions)
    return package, PluginManifest(
        plugin_id="b26.transfer.plugin",
        plugin_version="1.0.0",
        sdk_version="operant-memory-sdk.v1",
        host_api_versions=("operant-memory-sdk.v1",),
        package_digest=compute_package_digest(package),
        dependencies_digest=hashlib.sha256(dependencies).hexdigest(),
        permissions_digest=hashlib.sha256(permissions).hexdigest(),
        entrypoint="entry",
        config_schema_ref="config.v1",
        config_schema_digest="e" * 64,
        state_schema_version="state.v1",
        capabilities=("recall", "extract", "maintain", "on_index_event"),
        memory_mb=64,
        max_rpc_bytes=128_000,
        max_concurrency=2,
        export_supported=True,
        import_supported=True,
        recoverable=True,
    )


def test_transfer_between_ordinary_installations_preserves_source_data_on_uninstall(
    tmp_path: Path,
) -> None:
    package, manifest = _plugin_package(tmp_path / "source")
    registry = PluginRegistry(tmp_path / "managed")
    source_installation = registry.install(
        manifest,
        package,
        dataset_id="dataset-a",
        principal_id="owner-a",
        installation_id="installation-a",
    )
    destination_installation = registry.install(
        manifest,
        package,
        dataset_id="dataset-b",
        principal_id="owner-b",
        installation_id="installation-b",
    )
    service, _store, ledger = _service(
        tmp_path,
        registry=registry,
        projects={
            "project-a": {
                "project_id": "project-a",
                "workspace_id": "workspace-a",
                "installation_id": source_installation.installation_id,
            },
            "project-b": {
                "project_id": "project-b",
                "workspace_id": "workspace-b",
                "installation_id": destination_installation.installation_id,
            },
        },
    )
    service.register_worktree(_registration("project-a", "workspace-a", "worktree-a"))
    service.register_worktree(_registration("project-b", "workspace-b", "worktree-b"))
    source_scope = WorkspaceScope(
        kind="workspace", project_id="project-a", workspace_id="workspace-a"
    )
    target_scope = WorkspaceScope(
        kind="workspace", project_id="project-b", workspace_id="workspace-b"
    )
    source = _publish(
        ledger,
        _version(
            dataset_id="dataset-a",
            record_id="transfer-memory",
            scope=source_scope,
            owner=source_installation.owner,
        ),
    )
    grant = service.create_grant(
        SharingGrant(
            project_id="project-a",
            source_dataset_id="dataset-a",
            source_scope=source_scope,
            target_scope=target_scope,
            subject_id="owner-b",
            grantor_id="client-grantor",
            purpose="transfer",
            memory_refs=(source.ref,),
            expires_at=source.recorded_at + timedelta(hours=1),
        )
    )
    from operant.contracts.b2_1 import DatasetTransfer

    request = DatasetTransfer(
        dataset_id="dataset-a",
        destination_dataset_id="dataset-a",
        expected_revision=0,
        from_namespace="dataset:dataset-a",
        to_namespace="dataset:dataset-a",
        destination_installation_id=destination_installation.installation_id,
        authorization_grant_id=grant.grant_id,
        mode="transfer",
        idempotency_key="transfer-request-a",
    )
    pending = service.begin_transfer(request, project_id="project-a")
    validated = service.validate_transfer(
        pending.transfer_id,
        expected_revision=pending.revision,
    )
    assert validated.evidence_refs
    committed = service.commit_transfer(
        validated.transfer_id,
        expected_revision=validated.revision,
    )
    assert committed.state == "committed"
    assert (
        registry.get_dataset("dataset-a").installation_id
        == destination_installation.installation_id
    )
    assert registry.get_dataset("dataset-a").state == "bound"
    assert registry.get_dataset("dataset-b").state == "retained"
    assert (
        registry.get_installation(destination_installation.installation_id).dataset_id
        == "dataset-a"
    )

    plan = registry.begin_uninstall(
        source_installation.installation_id,
        data_policy="delete",
    )
    assert plan.state == "pending"
    assert all(item.outcome == "retained" for item in plan.items)
    registry.complete_uninstall(plan.operation_id)
    assert (
        registry.get_dataset("dataset-a").installation_id
        == destination_installation.installation_id
    )
    assert registry.get_dataset("dataset-a").state == "bound"
