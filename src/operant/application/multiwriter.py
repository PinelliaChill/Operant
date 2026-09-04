from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from itertools import combinations
from typing import Protocol

from operant.application.graph import GraphConflictError
from operant.domain.graph import NodeKind, WorkflowDefinition
from operant.domain.multiwriter import (
    MergeNodePolicy,
    MergeRun,
    MergeRunStatus,
    PatchCommitArtifact,
    WriterArtifactKind,
    WriterConflict,
    WriterConflictStatus,
    WriterLease,
    WriterWorkspace,
)
from operant.persistence.multiwriter import SQLiteMultiWriterRepository


class WriterArtifactAdapter(Protocol):
    def verify(self, workspace: WriterWorkspace, artifact: PatchCommitArtifact) -> None: ...


class MergeAdapter(Protocol):
    def merge(
        self,
        *,
        merge: MergeRun,
        artifacts: tuple[PatchCommitArtifact, ...],
        resolutions: tuple[WriterConflict, ...],
    ) -> str: ...

    def rollback(self, *, merge: MergeRun) -> None: ...


class ReferenceArtifactAdapter:
    """Safe default: validate immutable metadata; deployment may inject Git verification."""

    def verify(self, workspace: WriterWorkspace, artifact: PatchCommitArtifact) -> None:
        del workspace
        if artifact.artifact_kind is WriterArtifactKind.PATCH and artifact.result_revision:
            raise ValueError("patch artifact cannot claim a result revision")


class UnavailableMergeAdapter:
    def merge(
        self,
        *,
        merge: MergeRun,
        artifacts: tuple[PatchCommitArtifact, ...],
        resolutions: tuple[WriterConflict, ...],
    ) -> str:
        del merge, artifacts, resolutions
        raise RuntimeError("isolated Git merge adapter is not configured")

    def rollback(self, *, merge: MergeRun) -> None:
        del merge


class MultiWriterRuntime:
    def __init__(
        self,
        repository: SQLiteMultiWriterRepository,
        *,
        artifact_adapter: WriterArtifactAdapter | None = None,
        merge_adapter: MergeAdapter | None = None,
    ) -> None:
        self.repository = repository
        self.artifact_adapter = artifact_adapter or ReferenceArtifactAdapter()
        self.merge_adapter = merge_adapter or UnavailableMergeAdapter()

    def create_workspace(self, workspace: WriterWorkspace) -> WriterWorkspace:
        if workspace.isolation_ref == workspace.graph_run_id:
            raise ValueError("writer isolation cannot be the graph run workspace")
        return self.repository.create_workspace(workspace)

    def acquire_lease(self, workspace_id: str, *, owner: str, ttl_seconds: int = 60) -> WriterLease:
        if not 1 <= ttl_seconds <= 3600:
            raise ValueError("writer lease TTL must be between 1 and 3600 seconds")
        return self.repository.acquire_lease(
            workspace_id,
            owner=owner,
            token=f"writer_lease_{secrets.token_hex(24)}",
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds),
        )

    def renew_lease(self, lease: WriterLease, *, ttl_seconds: int = 60) -> WriterLease:
        if not 1 <= ttl_seconds <= 3600:
            raise ValueError("writer lease TTL must be between 1 and 3600 seconds")
        return self.repository.renew_lease(
            lease, expires_at=datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        )

    def publish_artifact(
        self, artifact: PatchCommitArtifact, *, lease: WriterLease
    ) -> PatchCommitArtifact:
        workspace = self.repository.get_workspace(artifact.writer_workspace_id)
        self.artifact_adapter.verify(workspace, artifact)
        return self.repository.publish_artifact(artifact, lease=lease)

    def detect_conflicts(self, graph_run_id: str) -> tuple[WriterConflict, ...]:
        artifacts = self.repository.list_artifacts(graph_run_id)
        workspaces = {
            workspace.writer_workspace_id: workspace
            for workspace in self.repository.list_workspaces(graph_run_id)
        }
        detected: list[WriterConflict] = []
        for left, right in combinations(artifacts, 2):
            left_workspace = workspaces[left.writer_workspace_id]
            right_workspace = workspaces[right.writer_workspace_id]
            if left_workspace.writer_key == right_workspace.writer_key:
                continue
            changed_overlap = set(left.changed_paths).intersection(right.changed_paths)
            ownership_overlap = _ownership_overlap(
                left_workspace.ownership_paths, right_workspace.ownership_paths
            )
            stale_base = left.base_revision != right.base_revision
            paths = changed_overlap | ownership_overlap
            if stale_base and not paths:
                paths = set(left.changed_paths) | set(right.changed_paths)
            if not paths and not stale_base:
                continue
            left_id, right_id = sorted((left.writer_artifact_id, right.writer_artifact_id))
            reason = {
                "left": left_id,
                "right": right_id,
                "paths": sorted(paths),
                "stale_base": stale_base,
                "ownership_overlap": sorted(ownership_overlap),
            }
            conflict_hash = hashlib.sha256(
                json.dumps(reason, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            detected.append(
                self.repository.put_conflict(
                    WriterConflict(
                        graph_run_id=graph_run_id,
                        left_artifact_id=left_id,
                        right_artifact_id=right_id,
                        conflict_hash=conflict_hash,
                        paths=tuple(sorted(paths)),
                    )
                )
            )
        return tuple(detected)

    def create_merge_run(
        self,
        merge: MergeRun,
        *,
        definition: WorkflowDefinition,
        leases: tuple[WriterLease, ...],
    ) -> MergeRun:
        policy = self._merge_policy(definition, merge.merge_node_id)
        artifacts = tuple(self.repository.get_artifact(item) for item in merge.artifact_ids)
        if merge.strategy is not policy.strategy:
            raise GraphConflictError("merge strategy does not match the frozen Merge node")
        if len({artifact.writer_workspace_id for artifact in artifacts}) != len(artifacts):
            raise GraphConflictError("merge requires artifacts from distinct writer workspaces")
        workspaces = tuple(
            self.repository.get_workspace(artifact.writer_workspace_id) for artifact in artifacts
        )
        if {workspace.writer_key for workspace in workspaces} != set(policy.source_writer_keys):
            raise GraphConflictError("merge artifacts do not cover the Merge node writer set")
        if any(artifact.base_revision != merge.base_revision for artifact in artifacts):
            raise GraphConflictError("merge contains an artifact with a stale base revision")
        if merge.target_isolation_ref in {workspace.isolation_ref for workspace in workspaces}:
            raise GraphConflictError("merge target must use a separate isolated workspace")
        self._assert_workspace_leases(workspaces, leases)
        if any(
            item.status is WriterConflictStatus.OPEN
            for item in self.repository.list_conflicts(merge.graph_run_id)
        ):
            merge = merge.model_copy(update={"status": MergeRunStatus.CONFLICTED})
        return self.repository.create_merge_run(merge)

    def finalize_merge_run(
        self,
        merge_run_id: str,
        *,
        definition: WorkflowDefinition,
        leases: tuple[WriterLease, ...],
        review_approved: bool,
    ) -> MergeRun:
        merge = self.repository.get_merge_run(merge_run_id)
        if merge.status in {
            MergeRunStatus.SUCCEEDED,
            MergeRunStatus.FAILED,
            MergeRunStatus.ROLLED_BACK,
        }:
            raise GraphConflictError("terminal merge runs cannot be finalized again")
        if merge.status is MergeRunStatus.RUNNING:
            raise GraphConflictError("running merge result is unknown and requires reconciliation")
        policy = self._merge_policy(definition, merge.merge_node_id)
        artifacts = tuple(self.repository.get_artifact(item) for item in merge.artifact_ids)
        workspaces = tuple(
            self.repository.get_workspace(artifact.writer_workspace_id) for artifact in artifacts
        )
        self._assert_workspace_leases(workspaces, leases)
        conflicts = self.repository.list_conflicts(merge.graph_run_id)
        if any(conflict.status is WriterConflictStatus.OPEN for conflict in conflicts):
            if merge.status is MergeRunStatus.CONFLICTED:
                return merge
            return self._transition(merge, MergeRunStatus.CONFLICTED, error_code="conflict_open")
        if policy.require_review and not review_approved:
            if merge.status is MergeRunStatus.REVIEW_REQUIRED:
                return merge
            return self._transition(merge, MergeRunStatus.REVIEW_REQUIRED)
        running = self._transition(merge, MergeRunStatus.RUNNING)
        try:
            result_ref = self.merge_adapter.merge(
                merge=running, artifacts=artifacts, resolutions=conflicts
            )
        except Exception as exc:
            error_code = f"merge_adapter_{type(exc).__name__.lower()}"[:200]
            if policy.rollback_on_failure:
                try:
                    self.merge_adapter.rollback(merge=running)
                except Exception:
                    return self._transition(running, MergeRunStatus.FAILED, error_code=error_code)
                return self._transition(running, MergeRunStatus.ROLLED_BACK, error_code=error_code)
            return self._transition(running, MergeRunStatus.FAILED, error_code=error_code)
        return self._transition(running, MergeRunStatus.SUCCEEDED, result_artifact_ref=result_ref)

    def _transition(
        self,
        merge: MergeRun,
        status: MergeRunStatus,
        *,
        result_artifact_ref: str | None = None,
        error_code: str | None = None,
    ) -> MergeRun:
        updated = merge.model_copy(
            update={
                "status": status,
                "expected_revision": merge.expected_revision + 1,
                "result_artifact_ref": result_artifact_ref,
                "error_code": error_code,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        return self.repository.update_merge_run(updated, expected_revision=merge.expected_revision)

    def _assert_workspace_leases(
        self, workspaces: tuple[WriterWorkspace, ...], leases: tuple[WriterLease, ...]
    ) -> None:
        by_workspace = {lease.writer_workspace_id: lease for lease in leases}
        if set(by_workspace) != {workspace.writer_workspace_id for workspace in workspaces}:
            raise GraphConflictError("one current writer lease is required per merge artifact")
        for lease in by_workspace.values():
            self.repository.assert_lease(lease)

    @staticmethod
    def _merge_policy(definition: WorkflowDefinition, node_id: str) -> MergeNodePolicy:
        node = next(
            (item for item in definition.nodes if item.node_id == node_id),
            None,
        )
        if node is None or node.node_kind is not NodeKind.MERGE or node.merge_policy is None:
            raise GraphConflictError("merge run does not reference a frozen Merge node")
        return node.merge_policy


def _ownership_overlap(left: tuple[str, ...], right: tuple[str, ...]) -> set[str]:
    overlaps: set[str] = set()
    for left_path in left:
        for right_path in right:
            if (
                left_path == right_path
                or left_path.startswith(f"{right_path}/")
                or right_path.startswith(f"{left_path}/")
            ):
                overlaps.add(min(left_path, right_path, key=len))
    return overlaps
