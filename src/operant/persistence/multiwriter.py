from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from operant.application.graph import GraphConflictError
from operant.domain.multiwriter import (
    MergeRun,
    MergeRunStatus,
    PatchCommitArtifact,
    WriterConflict,
    WriterLease,
    WriterWorkspace,
)
from operant.persistence.sqlite import NotFoundError, SQLiteStore


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class SQLiteMultiWriterRepository:
    """v13 multi-writer authority with lease/fencing checks in write transactions."""

    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def create_workspace(self, workspace: WriterWorkspace) -> WriterWorkspace:
        with self.store._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO writer_workspaces(
                        writer_workspace_id, graph_run_id, node_run_id, writer_key,
                        isolation_kind, isolation_ref, base_revision, ownership_paths_json,
                        created_at, closed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        workspace.writer_workspace_id,
                        workspace.graph_run_id,
                        workspace.node_run_id,
                        workspace.writer_key,
                        workspace.isolation_kind.value,
                        workspace.isolation_ref,
                        workspace.base_revision,
                        _json(workspace.ownership_paths),
                        workspace.created_at.isoformat(),
                        None,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise GraphConflictError(
                    "writer workspace already exists or is not isolated"
                ) from exc
        return workspace

    def get_workspace(self, workspace_id: str) -> WriterWorkspace:
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT * FROM writer_workspaces WHERE writer_workspace_id = ?", (workspace_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"writer workspace not found: {workspace_id}")
        return self._workspace(row)

    def list_workspaces(self, graph_run_id: str) -> tuple[WriterWorkspace, ...]:
        with self.store._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM writer_workspaces WHERE graph_run_id = ? ORDER BY created_at",
                (graph_run_id,),
            ).fetchall()
        return tuple(self._workspace(row) for row in rows)

    def get_lease(self, workspace_id: str) -> WriterLease | None:
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT * FROM writer_leases WHERE writer_workspace_id = ?", (workspace_id,)
            ).fetchone()
        if row is None:
            return None
        return WriterLease(
            writer_workspace_id=row["writer_workspace_id"],
            owner=row["owner"],
            token="redacted-at-rest",
            fencing=row["fencing"],
            expires_at=datetime.fromisoformat(row["expires_at"]),
            released_at=(
                None if row["released_at"] is None else datetime.fromisoformat(row["released_at"])
            ),
        )

    def acquire_lease(
        self, workspace_id: str, *, owner: str, token: str, expires_at: datetime
    ) -> WriterLease:
        observed_at = _now()
        if expires_at <= observed_at:
            raise ValueError("writer lease expiry must be in the future")
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            workspace = connection.execute(
                "SELECT closed_at FROM writer_workspaces WHERE writer_workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            if workspace is None:
                raise NotFoundError(f"writer workspace not found: {workspace_id}")
            if workspace["closed_at"] is not None:
                raise GraphConflictError("writer workspace is closed")
            current = connection.execute(
                "SELECT * FROM writer_leases WHERE writer_workspace_id = ?", (workspace_id,)
            ).fetchone()
            if current is not None:
                active = (
                    current["released_at"] is None
                    and datetime.fromisoformat(str(current["expires_at"])) > observed_at
                )
                if active:
                    raise GraphConflictError("writer workspace already has an active lease")
                fencing = int(current["fencing"]) + 1
                connection.execute(
                    """
                    UPDATE writer_leases SET owner = ?, token_hash = ?, fencing = ?,
                        expires_at = ?, released_at = NULL
                    WHERE writer_workspace_id = ?
                    """,
                    (owner, _token_hash(token), fencing, expires_at.isoformat(), workspace_id),
                )
            else:
                fencing = 1
                connection.execute(
                    """
                    INSERT INTO writer_leases(
                        writer_workspace_id, owner, token_hash, fencing, expires_at, released_at
                    ) VALUES (?, ?, ?, ?, ?, NULL)
                    """,
                    (workspace_id, owner, _token_hash(token), fencing, expires_at.isoformat()),
                )
        return WriterLease(
            writer_workspace_id=workspace_id,
            owner=owner,
            token=token,
            fencing=fencing,
            expires_at=expires_at,
        )

    def renew_lease(self, lease: WriterLease, *, expires_at: datetime) -> WriterLease:
        observed_at = _now()
        if expires_at <= observed_at:
            raise ValueError("writer lease expiry must be in the future")
        with self.store._connect() as connection:
            result = connection.execute(
                """
                UPDATE writer_leases SET expires_at = ?
                WHERE writer_workspace_id = ? AND owner = ? AND token_hash = ? AND fencing = ?
                  AND released_at IS NULL AND expires_at > ?
                """,
                (
                    expires_at.isoformat(),
                    lease.writer_workspace_id,
                    lease.owner,
                    _token_hash(lease.token),
                    lease.fencing,
                    observed_at.isoformat(),
                ),
            )
            if result.rowcount != 1:
                raise GraphConflictError("writer lease is expired, released, or fenced")
        return lease.model_copy(update={"expires_at": expires_at})

    def release_lease(self, lease: WriterLease) -> WriterLease:
        released_at = _now()
        with self.store._connect() as connection:
            result = connection.execute(
                """
                UPDATE writer_leases SET released_at = ?, expires_at = ?
                WHERE writer_workspace_id = ? AND owner = ? AND token_hash = ? AND fencing = ?
                  AND released_at IS NULL
                """,
                (
                    released_at.isoformat(),
                    released_at.isoformat(),
                    lease.writer_workspace_id,
                    lease.owner,
                    _token_hash(lease.token),
                    lease.fencing,
                ),
            )
            if result.rowcount != 1:
                raise GraphConflictError("writer lease is released or fenced")
        return lease.model_copy(update={"expires_at": released_at, "released_at": released_at})

    def assert_lease(
        self, lease: WriterLease, *, connection: sqlite3.Connection | None = None
    ) -> None:
        if connection is None:
            with self.store._connect() as owned_connection:
                self.assert_lease(lease, connection=owned_connection)
            return
        row = connection.execute(
            """
            SELECT * FROM writer_leases
            WHERE writer_workspace_id = ? AND owner = ? AND token_hash = ? AND fencing = ?
              AND released_at IS NULL AND expires_at > ?
            """,
            (
                lease.writer_workspace_id,
                lease.owner,
                _token_hash(lease.token),
                lease.fencing,
                _now().isoformat(),
            ),
        ).fetchone()
        if row is None:
            raise GraphConflictError("writer lease is expired, released, or fenced")

    def publish_artifact(
        self, artifact: PatchCommitArtifact, *, lease: WriterLease
    ) -> PatchCommitArtifact:
        if artifact.writer_workspace_id != lease.writer_workspace_id:
            raise GraphConflictError("writer lease belongs to another workspace")
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.assert_lease(lease, connection=connection)
            workspace = self._workspace(
                connection.execute(
                    "SELECT * FROM writer_workspaces WHERE writer_workspace_id = ?",
                    (artifact.writer_workspace_id,),
                ).fetchone()
            )
            if artifact.base_revision != workspace.base_revision:
                raise GraphConflictError("writer artifact has a stale base revision")
            if any(
                not any(
                    path == owned or path.startswith(f"{owned}/")
                    for owned in workspace.ownership_paths
                )
                for path in artifact.changed_paths
            ):
                raise GraphConflictError("writer artifact changes paths outside ownership")
            if not artifact.changed_paths or not artifact.test_evidence_refs:
                raise GraphConflictError("writer artifact requires changed paths and test evidence")
            try:
                connection.execute(
                    """
                    INSERT INTO writer_artifacts(
                        writer_artifact_id, writer_workspace_id, artifact_kind, artifact_ref,
                        artifact_sha256, base_revision, result_revision, changed_paths_json,
                        test_evidence_refs_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact.writer_artifact_id,
                        artifact.writer_workspace_id,
                        artifact.artifact_kind.value,
                        artifact.artifact_ref,
                        artifact.artifact_sha256,
                        artifact.base_revision,
                        artifact.result_revision,
                        _json(artifact.changed_paths),
                        _json(artifact.test_evidence_refs),
                        artifact.created_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise GraphConflictError("writer artifact already exists or is invalid") from exc
        return artifact

    def get_artifact(self, artifact_id: str) -> PatchCommitArtifact:
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT * FROM writer_artifacts WHERE writer_artifact_id = ?", (artifact_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"writer artifact not found: {artifact_id}")
        return self._artifact(row)

    def list_artifacts(self, graph_run_id: str) -> tuple[PatchCommitArtifact, ...]:
        with self.store._connect() as connection:
            rows = connection.execute(
                """
                SELECT artifact.* FROM writer_artifacts AS artifact
                JOIN writer_workspaces AS workspace
                  ON workspace.writer_workspace_id = artifact.writer_workspace_id
                WHERE workspace.graph_run_id = ? ORDER BY artifact.created_at
                """,
                (graph_run_id,),
            ).fetchall()
        return tuple(self._artifact(row) for row in rows)

    def put_conflict(self, conflict: WriterConflict) -> WriterConflict:
        with self.store._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO writer_conflicts(
                    conflict_id, graph_run_id, left_artifact_id, right_artifact_id,
                    conflict_hash, paths_json, status, resolution_artifact_ref,
                    created_at, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conflict.conflict_id,
                    conflict.graph_run_id,
                    conflict.left_artifact_id,
                    conflict.right_artifact_id,
                    conflict.conflict_hash,
                    _json(conflict.paths),
                    conflict.status.value,
                    conflict.resolution_artifact_ref,
                    conflict.created_at.isoformat(),
                    None,
                ),
            )
            row = connection.execute(
                "SELECT * FROM writer_conflicts WHERE graph_run_id = ? AND conflict_hash = ?",
                (conflict.graph_run_id, conflict.conflict_hash),
            ).fetchone()
        return self._conflict(row)

    def list_conflicts(self, graph_run_id: str) -> tuple[WriterConflict, ...]:
        with self.store._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM writer_conflicts WHERE graph_run_id = ? ORDER BY created_at",
                (graph_run_id,),
            ).fetchall()
        return tuple(self._conflict(row) for row in rows)

    def resolve_conflict(
        self, conflict_id: str, *, resolution_artifact_ref: str, rejected: bool = False
    ) -> WriterConflict:
        resolved_at = _now()
        status = "rejected" if rejected else "resolved"
        with self.store._connect() as connection:
            result = connection.execute(
                """
                UPDATE writer_conflicts
                SET status = ?, resolution_artifact_ref = ?, resolved_at = ?
                WHERE conflict_id = ? AND status = 'open'
                """,
                (status, resolution_artifact_ref, resolved_at.isoformat(), conflict_id),
            )
            if result.rowcount != 1:
                raise GraphConflictError("writer conflict is missing or already resolved")
            row = connection.execute(
                "SELECT * FROM writer_conflicts WHERE conflict_id = ?", (conflict_id,)
            ).fetchone()
        return self._conflict(row)

    def create_merge_run(self, merge: MergeRun) -> MergeRun:
        with self.store._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO merge_runs(
                        merge_run_id, graph_run_id, merge_node_id, artifact_ids_json,
                        strategy, target_isolation_ref, base_revision, status,
                        expected_revision, result_artifact_ref, error_code, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        merge.merge_run_id,
                        merge.graph_run_id,
                        merge.merge_node_id,
                        _json(merge.artifact_ids),
                        merge.strategy.value,
                        merge.target_isolation_ref,
                        merge.base_revision,
                        merge.status.value,
                        merge.expected_revision,
                        merge.result_artifact_ref,
                        merge.error_code,
                        merge.created_at.isoformat(),
                        merge.updated_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise GraphConflictError("merge run already exists or is invalid") from exc
        return merge

    def get_merge_run(self, merge_run_id: str) -> MergeRun:
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT * FROM merge_runs WHERE merge_run_id = ?", (merge_run_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"merge run not found: {merge_run_id}")
        return self._merge(row)

    def list_merge_runs(self, graph_run_id: str) -> tuple[MergeRun, ...]:
        with self.store._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM merge_runs WHERE graph_run_id = ? ORDER BY created_at",
                (graph_run_id,),
            ).fetchall()
        return tuple(self._merge(row) for row in rows)

    def update_merge_run(self, merge: MergeRun, *, expected_revision: int) -> MergeRun:
        if merge.expected_revision != expected_revision + 1:
            raise GraphConflictError("merge run revision conflict")
        if merge.status is MergeRunStatus.RUNNING:
            raise GraphConflictError("running merge runs require an execution owner lease")
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                result = connection.execute(
                    """
                    UPDATE merge_runs
                    SET status = ?, expected_revision = ?, result_artifact_ref = ?,
                        error_code = ?, execution_owner_id = NULL,
                        execution_lease_expires_at = NULL, updated_at = ?
                    WHERE merge_run_id = ? AND expected_revision = ?
                    """,
                    (
                        merge.status.value,
                        merge.expected_revision,
                        merge.result_artifact_ref,
                        merge.error_code,
                        merge.updated_at.isoformat(),
                        merge.merge_run_id,
                        expected_revision,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise GraphConflictError(
                    "merge target already has an exclusive running merge"
                ) from exc
            if result.rowcount != 1:
                raise GraphConflictError("merge run revision conflict")
        return merge

    def claim_merge_run(
        self,
        merge: MergeRun,
        *,
        execution_owner_id: str,
        execution_lease_expires_at: datetime,
    ) -> MergeRun:
        claimed = merge.model_copy(
            update={
                "status": MergeRunStatus.RUNNING,
                "expected_revision": merge.expected_revision + 1,
                "result_artifact_ref": None,
                "error_code": None,
                "updated_at": _now(),
            }
        )
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                result = connection.execute(
                    """
                    UPDATE merge_runs
                    SET status='running', expected_revision=?, result_artifact_ref=NULL,
                        error_code=NULL, execution_owner_id=?, execution_lease_expires_at=?,
                        updated_at=?
                    WHERE merge_run_id=? AND expected_revision=?
                      AND status NOT IN ('running', 'succeeded', 'failed', 'rolled_back',
                                         'outcome_unknown')
                    """,
                    (
                        claimed.expected_revision,
                        execution_owner_id,
                        execution_lease_expires_at.isoformat(),
                        claimed.updated_at.isoformat(),
                        merge.merge_run_id,
                        merge.expected_revision,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise GraphConflictError(
                    "merge target already has an exclusive running merge"
                ) from exc
            if result.rowcount != 1:
                raise GraphConflictError("merge run execution claim conflicts")
        return claimed

    def renew_merge_run_lease(
        self,
        merge_run_id: str,
        *,
        execution_owner_id: str,
        execution_lease_expires_at: datetime,
    ) -> None:
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            result = connection.execute(
                """
                UPDATE merge_runs SET execution_lease_expires_at=?, updated_at=?
                WHERE merge_run_id=? AND status='running' AND execution_owner_id=?
                """,
                (
                    execution_lease_expires_at.isoformat(),
                    _now().isoformat(),
                    merge_run_id,
                    execution_owner_id,
                ),
            )
            if result.rowcount != 1:
                raise GraphConflictError("merge run execution lease was lost")

    def complete_owned_merge_run(
        self,
        merge: MergeRun,
        *,
        expected_revision: int,
        execution_owner_id: str,
    ) -> MergeRun:
        if merge.expected_revision != expected_revision + 1:
            raise GraphConflictError("merge run revision conflict")
        if merge.status is MergeRunStatus.RUNNING:
            raise GraphConflictError("owned merge completion requires a known terminal status")
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            result = connection.execute(
                """
                UPDATE merge_runs
                SET status=?, expected_revision=?, result_artifact_ref=?, error_code=?,
                    execution_owner_id=NULL, execution_lease_expires_at=NULL, updated_at=?
                WHERE merge_run_id=? AND expected_revision=? AND status='running'
                  AND execution_owner_id=?
                """,
                (
                    merge.status.value,
                    merge.expected_revision,
                    merge.result_artifact_ref,
                    merge.error_code,
                    merge.updated_at.isoformat(),
                    merge.merge_run_id,
                    expected_revision,
                    execution_owner_id,
                ),
            )
            if result.rowcount != 1:
                raise GraphConflictError("merge run owned completion conflicts")
        return merge

    def reconcile_incomplete_merge_runs(self, *, now: datetime) -> int:
        """Conservatively close only expired running owners; never replay Git writes."""
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            result = connection.execute(
                """
                UPDATE merge_runs
                SET status='outcome_unknown', expected_revision=expected_revision+1,
                    result_artifact_ref=NULL, error_code='merge.manual_reconcile_required',
                    execution_owner_id=NULL, execution_lease_expires_at=NULL, updated_at=?
                WHERE status='running' AND (
                    execution_lease_expires_at IS NULL OR execution_lease_expires_at<=?
                )
                """,
                (now.isoformat(), now.isoformat()),
            )
        return int(result.rowcount)

    def reconcile_merge_run(
        self,
        merge_run_id: str,
        *,
        expected_revision: int,
        status: MergeRunStatus,
        result_artifact_ref: str | None,
        error_code: str | None,
        now: datetime,
    ) -> MergeRun:
        if status not in {
            MergeRunStatus.SUCCEEDED,
            MergeRunStatus.FAILED,
            MergeRunStatus.ROLLED_BACK,
        }:
            raise GraphConflictError("merge reconciliation requires a known terminal status")
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            result = connection.execute(
                """
                UPDATE merge_runs
                SET status=?, expected_revision=expected_revision+1,
                    result_artifact_ref=?, error_code=?, updated_at=?
                WHERE merge_run_id=? AND expected_revision=? AND status='outcome_unknown'
                """,
                (
                    status.value,
                    result_artifact_ref,
                    error_code,
                    now.isoformat(),
                    merge_run_id,
                    expected_revision,
                ),
            )
            if result.rowcount != 1:
                raise GraphConflictError("merge reconciliation state conflicts")
        return self.get_merge_run(merge_run_id)

    @staticmethod
    def _workspace(row: sqlite3.Row) -> WriterWorkspace:
        return WriterWorkspace(
            writer_workspace_id=row["writer_workspace_id"],
            graph_run_id=row["graph_run_id"],
            node_run_id=row["node_run_id"],
            writer_key=row["writer_key"],
            isolation_kind=row["isolation_kind"],
            isolation_ref=row["isolation_ref"],
            base_revision=row["base_revision"],
            ownership_paths=tuple(json.loads(row["ownership_paths_json"])),
            created_at=datetime.fromisoformat(row["created_at"]),
            closed_at=None
            if row["closed_at"] is None
            else datetime.fromisoformat(row["closed_at"]),
        )

    @staticmethod
    def _artifact(row: sqlite3.Row) -> PatchCommitArtifact:
        return PatchCommitArtifact(
            writer_artifact_id=row["writer_artifact_id"],
            writer_workspace_id=row["writer_workspace_id"],
            artifact_kind=row["artifact_kind"],
            artifact_ref=row["artifact_ref"],
            artifact_sha256=row["artifact_sha256"],
            base_revision=row["base_revision"],
            result_revision=row["result_revision"],
            changed_paths=tuple(json.loads(row["changed_paths_json"])),
            test_evidence_refs=tuple(json.loads(row["test_evidence_refs_json"])),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _conflict(row: sqlite3.Row) -> WriterConflict:
        return WriterConflict(
            conflict_id=row["conflict_id"],
            graph_run_id=row["graph_run_id"],
            left_artifact_id=row["left_artifact_id"],
            right_artifact_id=row["right_artifact_id"],
            conflict_hash=row["conflict_hash"],
            paths=tuple(json.loads(row["paths_json"])),
            status=row["status"],
            resolution_artifact_ref=row["resolution_artifact_ref"],
            created_at=datetime.fromisoformat(row["created_at"]),
            resolved_at=None
            if row["resolved_at"] is None
            else datetime.fromisoformat(row["resolved_at"]),
        )

    @staticmethod
    def _merge(row: sqlite3.Row) -> MergeRun:
        return MergeRun(
            merge_run_id=row["merge_run_id"],
            graph_run_id=row["graph_run_id"],
            merge_node_id=row["merge_node_id"],
            artifact_ids=tuple(json.loads(row["artifact_ids_json"])),
            strategy=row["strategy"],
            target_isolation_ref=row["target_isolation_ref"],
            base_revision=row["base_revision"],
            status=row["status"],
            expected_revision=row["expected_revision"],
            result_artifact_ref=row["result_artifact_ref"],
            error_code=row["error_code"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
