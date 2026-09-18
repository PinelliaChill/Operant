"""End-to-end Writer promotion through a real local Git worktree.

The provider in this test is deterministic.  It proves Core reachability and
revocation boundaries; it is not real-model acceptance.  ``run_writer_chain``
also accepts a formal provider and profile for the bounded live harness.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from operant.api import create_app
from operant.application.graph import GraphRuntime
from operant.contracts.b2_1 import SourceRef
from operant.contracts.b2_3 import ManagementCommand
from operant.contracts.b2_6_sharing import SharingCommand
from operant.domain.graph import (
    EdgeSpec,
    IdempotencyClass,
    NodeKind,
    NodeSpec,
    PortSpec,
    WorkflowDefinition,
    WorkflowDefinitionStatus,
)
from operant.domain.messages import Message, ModelResponse, ProviderEvent, ToolDefinition
from operant.domain.models import Budget, ModelProfile, RolePreset, RoleSnapshot, ToolPolicy
from operant.domain.multiwriter import (
    MergeNodePolicy,
    MergeRun,
    MergeRunStatus,
    MergeStrategy,
    PatchCommitArtifact,
    WriterArtifactKind,
    WriterIsolationKind,
    WriterNodePolicy,
    WriterWorkspace,
)
from operant.memory_plugins.manager import MemoryManager
from operant.memory_plugins.recall import load_manifest
from operant.memory_plugins.sharing import SharingService
from operant.multiwriter import TrustedGitMultiWriterAdapter
from operant.persistence.graph_team import SQLiteGraphRepository
from operant.persistence.multiwriter import SQLiteMultiWriterRepository
from operant.providers.base import ModelProvider

WRITER_MARKER = "WRITER_PROMOTED_PROCEDURE_42"


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _git_bytes(root: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return result.stdout


def _writer_definition() -> WorkflowDefinition:
    writer_nodes = tuple(
        NodeSpec(
            node_id=f"writer-{writer}",
            node_kind=NodeKind.AGENT,
            output_ports=(PortSpec(name="artifact"),),
            writes_workspace=True,
            idempotency_class=IdempotencyClass.IDEMPOTENT,
            writer_policy=WriterNodePolicy(
                writer_key=writer,
                isolation_kind=WriterIsolationKind.WORKTREE,
                isolation_ref=f"writer-{writer}",
                ownership_paths=(f"src/{writer}",),
            ),
        )
        for writer in ("a", "b")
    )
    return WorkflowDefinition(
        workflow_id="b26-writer-integration",
        version=1,
        name="B2-6 Writer integration",
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
                    strategy=MergeStrategy.CHERRY_PICK,
                    source_writer_keys=("a", "b"),
                    require_review=False,
                ),
            ),
        ),
        edges=tuple(
            EdgeSpec(
                edge_id=f"writer-{writer}-merge",
                source_node=f"writer-{writer}",
                source_port="artifact",
                target_node="merge",
                target_port="artifacts",
            )
            for writer in ("a", "b")
        ),
    )


class _WriterRecallProvider(ModelProvider):
    def __init__(self) -> None:
        self.calls = 0
        self.last_serialized = ""

    async def list_models(self, *, base_url: str, secret_ref: str) -> list[str]:
        del base_url, secret_ref
        return ["b26-writer-deterministic-model"]

    async def stream(
        self,
        *,
        snapshot: RoleSnapshot,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        del snapshot, tools
        self.calls += 1
        serialized = "\n".join(message.content or "" for message in messages)
        self.last_serialized = serialized
        assert WRITER_MARKER in serialized
        yield ProviderEvent(
            event_type="model.completed",
            response=ModelResponse(content="deterministic writer recall", finish_reason="stop"),
        )


def _deterministic_profile() -> ModelProfile:
    return ModelProfile(
        name="B2-6 Writer deterministic",
        model_id="b26-writer-deterministic-model",
        base_url="https://invalid.test/v1",
        secret_ref="B26_WRITER_TEST_KEY",
    )


def _artifact(
    root: Path,
    *,
    writer_workspace_id: str,
    base_revision: str,
    writer: str,
) -> PatchCommitArtifact:
    result_revision = _git(root, "rev-parse", "HEAD")
    diff = _git_bytes(root, "diff", "--binary", base_revision, result_revision)
    changed_paths = tuple(
        path
        for path in _git(root, "diff", "--name-only", base_revision, result_revision).splitlines()
        if path
    )
    return PatchCommitArtifact(
        writer_workspace_id=writer_workspace_id,
        artifact_kind=WriterArtifactKind.COMMIT,
        artifact_ref=f"git:{result_revision}",
        artifact_sha256=hashlib.sha256(diff).hexdigest(),
        base_revision=base_revision,
        result_revision=result_revision,
        changed_paths=changed_paths,
        test_evidence_refs=(f"writer-{writer}-test",),
    )


async def run_writer_chain(
    tmp_path: Path,
    *,
    model_profile: ModelProfile,
    provider: ModelProvider,
    role_budget: Budget | None = None,
    before_publish_check: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    after_publish_check: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    _git(repository_root, "init", "-b", "main")
    _git(repository_root, "config", "user.name", "B2-6 Writer Test")
    _git(repository_root, "config", "user.email", "b26-writer@example.invalid")
    for writer in ("a", "b"):
        path = repository_root / "src" / writer / "main.py"
        path.parent.mkdir(parents=True)
        path.write_text(f"{writer} = 0\n", encoding="utf-8")
    _git(repository_root, "add", "src")
    _git(repository_root, "commit", "-m", "base")
    base_revision = _git(repository_root, "rev-parse", "HEAD")

    writer_roots = {writer: tmp_path / f"writer-{writer}" for writer in ("a", "b")}
    target_root = tmp_path / "target"
    for writer, root in writer_roots.items():
        _git(
            repository_root,
            "worktree",
            "add",
            "-b",
            f"writer-{writer}",
            str(root),
            base_revision,
        )
        (root / "src" / writer / "main.py").write_text(f"{writer} = 1\n", encoding="utf-8")
        _git(root, "add", f"src/{writer}/main.py")
        _git(root, "commit", "-m", f"Writer {writer} change")
    _git(repository_root, "worktree", "add", "-b", "target", str(target_root), base_revision)

    app = create_app(
        tmp_path / "core.sqlite3",
        artifact_root=tmp_path / "artifacts",
        phase56_multiwriter_roots={
            "writer-a": writer_roots["a"],
            "writer-b": writer_roots["b"],
            "target": target_root,
        },
    )
    service = app.state.operant_service
    manager = MemoryManager(service)
    service.memory_manager = manager
    try:

        async def b23(**values: Any):
            return await manager.execute(ManagementCommand(**values))

        project_result = await b23(
            action="project_create",
            name="B2-6 Writer integration",
            workspace_path=str(target_root),
        )
        project_id = project_result.state.projects[-1].project_id
        installation_result = await b23(
            action="plugin_install",
            plugin_id="memory-standard",
            mode="trusted_in_process",
        )
        installation_id = installation_result.state.installations[-1].installation_id
        await b23(
            action="binding_select",
            project_id=project_id,
            installation_id=installation_id,
        )
        source_result = await b23(
            action="memory_save",
            project_id=project_id,
            content="Writer source fact for the merged procedure.",
            confirmed=True,
        )
        source_record = source_result.state.records[-1]
        source_refs = tuple(SourceRef.model_validate(item) for item in source_record.sources)
        assert source_refs

        sharing = SharingService(
            manager,
            writer_adapter=app.state.multiwriter_runtime.merge_adapter,
        )
        registration_result = await sharing.execute(
            SharingCommand(
                action="worktree_register",
                project_id=project_id,
                workspace_path=str(target_root),
                branch_ref="target",
            )
        )
        assert registration_result.status == "completed"
        assert registration_result.state is not None
        registration = registration_result.state.worktrees[0]

        graph_repository = SQLiteGraphRepository(manager.store)
        definition = _writer_definition()
        graph_run = GraphRuntime(graph_repository).create_run(
            definition,
            workspace_or_target="b26-graph-controller",
        )
        node_runs = {
            item.node_id: item.id for item in graph_repository.list_node_runs(graph_run.id)
        }
        writer_repository = SQLiteMultiWriterRepository(manager.store)
        runtime = app.state.multiwriter_runtime
        assert runtime.repository.store.path == writer_repository.store.path
        assert isinstance(runtime.merge_adapter, TrustedGitMultiWriterAdapter)
        for writer in ("a", "b"):
            runtime.create_workspace(
                WriterWorkspace(
                    writer_workspace_id=f"writer-{writer}",
                    graph_run_id=graph_run.id,
                    node_run_id=node_runs[f"writer-{writer}"],
                    writer_key=writer,
                    isolation_kind=WriterIsolationKind.WORKTREE,
                    isolation_ref=f"writer-{writer}",
                    base_revision=base_revision,
                    ownership_paths=(f"src/{writer}",),
                )
            )
        leases = tuple(
            runtime.acquire_lease(
                f"writer-{writer}", owner=f"writer-{writer}-owner", ttl_seconds=300
            )
            for writer in ("a", "b")
        )
        artifacts = tuple(
            runtime.publish_artifact(
                _artifact(
                    writer_roots[writer],
                    writer_workspace_id=f"writer-{writer}",
                    base_revision=base_revision,
                    writer=writer,
                ),
                lease=lease,
            )
            for writer, lease in zip(("a", "b"), leases, strict=True)
        )

        candidate_result = await sharing.execute(
            SharingCommand(
                action="writer_memory_propose",
                project_id=project_id,
                worktree_id=registration.worktree_id,
                writer_workspace_id="writer-a",
                graph_run_id=graph_run.id,
                run_id="writer-run-42",
                content=f"Merged Writer procedure {WRITER_MARKER}: verify the target tree.",
                source_refs=source_refs,
                content_type="procedure",
            )
        )
        assert candidate_result.status == "completed"
        assert candidate_result.state is not None
        evidence = candidate_result.state.writer_evidence[0]
        assert evidence.state == "candidate"
        assert (
            manager.ledger.get_version(
                evidence.memory_ref.dataset_id,
                evidence.memory_ref.record_id,
                evidence.memory_ref.version,
            ).evidence
            == "inferred"
        )
        before_merge = await sharing.execute(
            SharingCommand(
                action="writer_memory_promote",
                project_id=project_id,
                evidence_id=evidence.evidence_id,
                expected_revision=evidence.revision,
            )
        )
        assert before_merge.status == "blocked"
        assert before_merge.state is not None
        assert before_merge.state.writer_evidence[0].state == "candidate"

        merge = runtime.create_merge_run(
            MergeRun(
                merge_run_id="writer-merge-42",
                graph_run_id=graph_run.id,
                merge_node_id="merge",
                artifact_ids=tuple(item.writer_artifact_id for item in artifacts),
                strategy=MergeStrategy.CHERRY_PICK,
                target_isolation_ref="target",
                base_revision=base_revision,
                status=MergeRunStatus.CREATED,
            ),
            definition=definition,
            leases=leases,
        )
        completed_merge = runtime.finalize_merge_run(
            merge.merge_run_id,
            definition=definition,
            leases=leases,
            review_approved=True,
        )
        assert completed_merge.status is MergeRunStatus.SUCCEEDED
        assert completed_merge.result_artifact_ref is not None
        assert _git(
            target_root, "rev-parse", "HEAD"
        ) == completed_merge.result_artifact_ref.removeprefix("git:")
        assert _git(target_root, "status", "--porcelain", "--untracked-files=all") == ""
        assert (target_root / "src" / "a" / "main.py").read_text(encoding="utf-8") == "a = 1\n"
        assert (target_root / "src" / "b" / "main.py").read_text(encoding="utf-8") == "b = 1\n"
        for lease in leases:
            writer_repository.release_lease(lease)

        unverified_result = await sharing.execute(
            SharingCommand(
                action="writer_memory_verify",
                project_id=project_id,
                evidence_id=evidence.evidence_id,
                merge_run_id=completed_merge.merge_run_id,
                verification_artifact_refs=("artifact-missing-verification",),
                expected_revision=evidence.revision,
            )
        )
        assert unverified_result.status == "blocked", unverified_result.message
        assert unverified_result.state is not None
        assert unverified_result.state.writer_evidence[0].state == "blocked"
        evidence = unverified_result.state.writer_evidence[0]
        verified_result = await sharing.execute(
            SharingCommand(
                action="writer_memory_verify",
                project_id=project_id,
                evidence_id=evidence.evidence_id,
                merge_run_id=completed_merge.merge_run_id,
                verification_artifact_refs=(),
                expected_revision=evidence.revision,
            )
        )
        assert verified_result.status == "completed", verified_result.message
        assert verified_result.state is not None
        eligible = verified_result.state.writer_evidence[0]
        assert eligible.state == "eligible"
        assert eligible.verification_artifact_refs
        for artifact_id in eligible.verification_artifact_refs:
            artifact = service.get_artifact(artifact_id)
            body = service._read_artifact_for_context(artifact_id)
            assert hashlib.sha256(body).hexdigest() == artifact.content_hash
            report = body.decode("utf-8")
            assert completed_merge.merge_run_id in report
            assert completed_merge.result_artifact_ref.removeprefix("git:") in report
            assert eligible.target_tree_digest in report
        promotion_context = {
            "tmp_path": tmp_path,
            "repository_root": repository_root,
            "target_root": target_root,
            "runtime": runtime,
            "writer_repository": writer_repository,
            "sharing": sharing,
            "project_id": project_id,
            "registration": registration,
            "graph_run": graph_run,
            "completed_merge": completed_merge,
            "evidence": evidence,
            "eligible": eligible,
        }
        if before_publish_check is not None:
            await before_publish_check(promotion_context)
        promoted_result = await sharing.execute(
            SharingCommand(
                action="writer_memory_promote",
                project_id=project_id,
                evidence_id=eligible.evidence_id,
                expected_revision=eligible.revision,
            )
        )
        assert promoted_result.status == "completed"
        assert promoted_result.state is not None
        published = promoted_result.state.writer_evidence[0]
        assert published.state == "published"
        assert published.published_memory_ref is not None
        promoted = manager.ledger.get_version(
            published.published_memory_ref.dataset_id,
            published.published_memory_ref.record_id,
            published.published_memory_ref.version,
        )
        assert promoted.content_type == "procedure"
        assert promoted.evidence == "inferred"
        assert promoted.conditions.commit_ref == completed_merge.result_artifact_ref
        assert (
            promoted.conditions.tree_digest
            == hashlib.sha256(
                f"git-tree:{_git(target_root, 'rev-parse', 'HEAD^{tree}')}".encode("ascii")
            ).hexdigest()
        )
        promotion_context["published"] = published
        promotion_context["promoted"] = promoted
        if after_publish_check is not None:
            await after_publish_check(promotion_context)

        profile = service.add_model_profile(model_profile)
        role = service.create_role(
            RolePreset(
                name="B2-6 Writer reader",
                system_prompt="Use only authorized project memory.",
                model_profile_id=profile.id,
                memory_scope="read: [project]; write: []",
                tool_policy=ToolPolicy(allowed_tools=()),
                budget=role_budget or Budget(max_turns=1, timeout_seconds=90),
            )
        )
        session = service.create_session(role.id)
        service.provider = provider
        first = [
            event
            async for event in service.run_session(
                session.id,
                user_message=f"请复用 {WRITER_MARKER}",
                workspace=target_root,
            )
        ]
        assert any(event.event_type == "agent.completed" for event in first), [
            (event.event_type, event.payload) for event in first
        ]
        provider_calls = getattr(provider, "calls", None)
        if isinstance(provider_calls, int):
            assert provider_calls == 1
        provider_input = getattr(provider, "last_serialized", "")
        if provider_input:
            assert WRITER_MARKER in provider_input
        revisions = service.store.list_context_revisions(session.id)
        assert any(
            WRITER_MARKER in (message.content or "")
            for revision in revisions
            for message in revision.messages
        )
        manifest = load_manifest(manager, session.id)
        assert manifest is not None
        assert any(
            ref["record_id"] == published.published_memory_ref.record_id
            and ref["version"] == published.published_memory_ref.version
            for ref in manifest["used_refs"]
        )

        dirty = target_root / "unreviewed-writer-change.txt"
        dirty.write_text("unreviewed", encoding="utf-8")
        try:
            after_dirty = [
                event
                async for event in service.run_session(
                    session.id,
                    user_message="继续复用 Merged Writer procedure",
                    workspace=target_root,
                )
            ]
        finally:
            dirty.unlink(missing_ok=True)
        assert not any(event.event_type == "agent.completed" for event in after_dirty)
        if isinstance(provider_calls, int):
            assert getattr(provider, "calls", None) == provider_calls
        assert len(service.store.list_context_revisions(session.id)) == len(revisions)

        current = sharing.writer_evidence(project_id)[0]
        revoked_result = await sharing.execute(
            SharingCommand(
                action="writer_memory_revoke",
                project_id=project_id,
                evidence_id=current.evidence_id,
                expected_revision=current.revision,
                reason="Writer evidence corrected",
            )
        )
        assert revoked_result.status == "revoked"
        assert revoked_result.state is not None
        assert revoked_result.state.writer_evidence[0].state == "revoked"
        after_revoke = [
            event
            async for event in service.run_session(
                session.id,
                user_message="继续复用 Merged Writer procedure",
                workspace=target_root,
            )
        ]
        assert not any(event.event_type == "agent.completed" for event in after_revoke)
        if isinstance(provider_calls, int):
            assert getattr(provider, "calls", None) == provider_calls
        assert len(service.store.list_context_revisions(session.id)) == len(revisions)
        return {
            "project_id": project_id,
            "graph_run_id": graph_run.id,
            "merge_run_id": completed_merge.merge_run_id,
            "target_commit_ref": completed_merge.result_artifact_ref,
            "target_tree_digest": promoted.conditions.tree_digest,
            "verification_artifact_refs": list(eligible.verification_artifact_refs),
            "candidate_memory_ref": evidence.memory_ref.model_dump(mode="json"),
            "published_memory_ref": published.published_memory_ref.model_dump(mode="json"),
            "session_id": session.id,
            "context_revision_count": len(revisions),
            "dirty_tree_blocked": True,
            "evidence_revocation_blocked": True,
        }
    finally:
        await manager.close()
        service.close()


@pytest.mark.asyncio
async def test_writer_candidate_merge_promotion_and_next_send_revocation(
    tmp_path: Path,
) -> None:
    result = await run_writer_chain(
        tmp_path,
        model_profile=_deterministic_profile(),
        provider=_WriterRecallProvider(),
    )
    assert result["dirty_tree_blocked"] is True
    assert result["evidence_revocation_blocked"] is True


@pytest.mark.asyncio
async def test_writer_promotion_rejects_clean_target_outside_registration(
    tmp_path: Path,
) -> None:
    async def check(context: dict[str, Any]) -> None:
        merge = context["completed_merge"]
        adapter = context["runtime"].merge_adapter
        assert isinstance(adapter, TrustedGitMultiWriterAdapter)
        assert merge.result_artifact_ref is not None
        rogue_root = context["tmp_path"] / "rogue-target"
        _git(
            context["repository_root"],
            "worktree",
            "add",
            "--detach",
            str(rogue_root),
            merge.result_artifact_ref.removeprefix("git:"),
        )
        original_root = adapter._roots["target"]
        adapter._roots["target"] = rogue_root.resolve()
        try:
            eligible = context["eligible"]
            result = await context["sharing"].execute(
                SharingCommand(
                    action="writer_memory_promote",
                    project_id=context["project_id"],
                    evidence_id=eligible.evidence_id,
                    expected_revision=eligible.revision,
                )
            )
            assert result.status == "blocked", result.message
            assert result.state is not None
            assert result.state.writer_evidence[0].state == "eligible"
        finally:
            adapter._roots["target"] = original_root
        assert _git(rogue_root, "status", "--porcelain", "--untracked-files=all") == ""

    result = await run_writer_chain(
        tmp_path,
        model_profile=_deterministic_profile(),
        provider=_WriterRecallProvider(),
        before_publish_check=check,
    )
    assert result["dirty_tree_blocked"] is True


@pytest.mark.asyncio
async def test_writer_promotion_rejects_opaque_reconcile_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def check(context: dict[str, Any]) -> None:
        sharing = context["sharing"]
        repository = sharing.writer_repository
        original_get_merge_run = repository.get_merge_run

        def opaque_merge_result(merge_run_id: str) -> Any:
            merge = original_get_merge_run(merge_run_id)
            return merge.model_copy(update={"result_artifact_ref": "reconcile:opaque-result"})

        eligible = context["eligible"]
        with monkeypatch.context() as patch:
            patch.setattr(repository, "get_merge_run", opaque_merge_result)
            result = await sharing.execute(
                SharingCommand(
                    action="writer_memory_promote",
                    project_id=context["project_id"],
                    evidence_id=eligible.evidence_id,
                    expected_revision=eligible.revision,
                )
            )
        assert result.status == "blocked", result.message
        assert result.state is not None
        assert result.state.writer_evidence[0].state == "eligible"

    result = await run_writer_chain(
        tmp_path,
        model_profile=_deterministic_profile(),
        provider=_WriterRecallProvider(),
        before_publish_check=check,
    )
    assert result["dirty_tree_blocked"] is True


@pytest.mark.asyncio
async def test_published_writer_verify_with_refs_is_idempotent_and_recall_continues(
    tmp_path: Path,
) -> None:
    async def check(context: dict[str, Any]) -> None:
        published = context["published"]
        sharing = context["sharing"]
        before = sharing.writer_evidence(context["project_id"])[0]
        assert before == published
        result = await sharing.execute(
            SharingCommand(
                action="writer_memory_verify",
                project_id=context["project_id"],
                evidence_id=published.evidence_id,
                merge_run_id=context["completed_merge"].merge_run_id,
                verification_artifact_refs=("forged-client-ref",),
                expected_revision=published.revision,
            )
        )
        assert result.status == "completed", result.message
        assert result.state is not None
        after = result.state.writer_evidence[0]
        assert after.state == "published"
        assert after.revision == before.revision
        assert after.published_memory_ref == before.published_memory_ref
        assert after.verification_artifact_refs == before.verification_artifact_refs

    result = await run_writer_chain(
        tmp_path,
        model_profile=_deterministic_profile(),
        provider=_WriterRecallProvider(),
        after_publish_check=check,
    )
    assert result["context_revision_count"] == 1
