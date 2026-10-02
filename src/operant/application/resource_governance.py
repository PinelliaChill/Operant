"""Inventory and conservatively reclaim real Workbench temporary snapshots."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Literal, cast

from operant.application.service import ApplicationService
from operant.artifacts import ArtifactNotFoundError, ArtifactStoreError
from operant.domain.models import utc_now
from operant.domain.resource_governance import (
    ResourceCleanupItem,
    ResourceCleanupPreview,
    ResourceCleanupResult,
    ResourceInventory,
    ResourceItem,
    ResourcePolicy,
)
from operant.domain.threads import ArtifactAccessLevel, ArtifactSensitivity, RetentionLifecycle
from operant.persistence.resource_governance import ResourcePolicyRepository
from operant.persistence.sqlite import ConflictError, NotFoundError
from operant.protocol import canonical_action_hash
from operant.remote.local_browser import browser_profile_inventory

CleanupMode = Literal["manual", "automatic"]


class ResourceGovernanceService:
    def __init__(self, service: ApplicationService) -> None:
        self.service = service
        self.repository = ResourcePolicyRepository(service.store)
        self._scan_thread_cursor = ""
        self._scan_artifact_cursors: dict[str, int] = {}

    def update_policy(
        self, thread_id: str, *, completed_ttl_seconds: int, unanswered_ttl_seconds: int
    ) -> ResourcePolicy:
        ResourcePolicy(
            completed_ttl_seconds=completed_ttl_seconds,
            unanswered_ttl_seconds=unanswered_ttl_seconds,
        )
        return self.repository.patch_ttl(
            thread_id,
            completed_ttl_seconds=completed_ttl_seconds,
            unanswered_ttl_seconds=unanswered_ttl_seconds,
        )

    def confirm(self, thread_id: str, *, completed: bool) -> ResourcePolicy:
        return self.repository.confirm(thread_id, completed=completed)

    def _session_id(self, thread_id: str) -> str | None:
        thread = self.service.get_thread(thread_id)
        return next(
            (ref.source_id for ref in thread.legacy_refs if ref.source_type.value == "session"),
            None,
        )

    def _active_or_unknown_run(self, thread_id: str) -> bool:
        session_id = self._session_id(thread_id)
        if session_id is None:
            return True
        with self.service.store._connect() as connection:
            if connection.execute(
                "SELECT 1 FROM session_run_leases WHERE session_id=? AND released_at IS NULL",
                (session_id,),
            ).fetchone():
                return True
            if connection.execute(
                """SELECT 1 FROM tool_action_receipts
                WHERE session_id=? AND status IN ('in_progress','outcome_unknown') LIMIT 1""",
                (session_id,),
            ).fetchone():
                return True
            if connection.execute(
                """SELECT 1 FROM workbench_children
                WHERE thread_id=? AND status IN ('queued','running','interrupted') LIMIT 1""",
                (thread_id,),
            ).fetchone():
                return True
        return False

    @staticmethod
    def _due(policy: ResourcePolicy, created_at: datetime) -> datetime | None:
        clocks = []
        if policy.completed_at is not None:
            clocks.append(
                max(policy.completed_at, created_at)
                + timedelta(seconds=policy.completed_ttl_seconds)
            )
        if policy.unanswered_since is not None:
            clocks.append(
                max(policy.unanswered_since, created_at)
                + timedelta(seconds=policy.unanswered_ttl_seconds)
            )
        return min(clocks) if clocks else None

    def _artifact_item(
        self, thread_id: str, artifact_id: str, policy: ResourcePolicy
    ) -> ResourceItem:
        artifact = self.service.get_artifact(artifact_id, verify=False)
        state = self.service.get_artifact_retention_state(artifact_id)
        temporary = artifact.retention_policy_ref == "context-reference-temporary"
        legacy = artifact.retention_policy_ref == "context-reference"
        kind: Literal["context_reference_snapshot", "artifact"] = (
            "context_reference_snapshot" if temporary or legacy else "artifact"
        )
        due = self._due(policy, artifact.created_at) if temporary else None
        hold: str | None = None
        if not temporary:
            hold = "legacy_snapshot_policy" if legacy else "authoritative_artifact"
        elif state.lifecycle is RetentionLifecycle.DELETED:
            hold = f"retention_{state.lifecycle.value}"
        elif state.pinned:
            hold = "pinned"
        elif self._active_or_unknown_run(thread_id):
            hold = "active_or_recoverable_run"
        else:
            blockers = self.service.store.artifact_deletion_blockers(artifact_id)
            if blockers:
                hold = ",".join(blockers)
            elif state.lifecycle is RetentionLifecycle.TRASHED:
                with self.service.store._connect() as connection:
                    unknown = connection.execute(
                        """SELECT 1 FROM artifact_retention_audit_events
                        WHERE artifact_id=? AND
                          event_type='retention.physical_delete_outcome_unknown'
                        LIMIT 1""",
                        (artifact_id,),
                    ).fetchone()
                if unknown:
                    hold = "physical_outcome_unknown"
        eligible = temporary and hold is None and due is not None and utc_now() >= due
        return ResourceItem(
            id=f"artifact:{artifact_id}",
            kind=kind,
            owner_thread_id=thread_id,
            size_bytes=0 if state.lifecycle is RetentionLifecycle.DELETED else artifact.size_bytes,
            retention_reason=(
                "temporary reference snapshot"
                if temporary
                else "legacy snapshot; original policy forbids physical deletion"
                if legacy
                else "Artifact may be authoritative evidence"
            ),
            pinned=state.pinned,
            hold_reason=hold,
            state=state.lifecycle.value,
            auto_cleanup_eligible=eligible,
            due_at=due,
        )

    def inventory(
        self,
        thread_id: str,
        *,
        after_artifact: int = 0,
        after_revision: int = 0,
        after_compaction: int = 0,
    ) -> ResourceInventory:
        self.service.get_thread(thread_id)
        policy = self.repository.get(thread_id)
        session_id = self._session_id(thread_id)
        resources: list[ResourceItem] = []
        with self.service.store._connect() as connection:
            artifacts = connection.execute(
                """SELECT DISTINCT a.id,a.sequence FROM artifacts a
                JOIN artifact_source_refs s ON s.artifact_id=a.id
                WHERE ((s.source_type='thread' AND s.source_id=?)
                    OR (s.source_type='session' AND s.source_id=?))
                    AND a.sequence>?
                ORDER BY a.sequence LIMIT 501""",
                (thread_id, session_id, after_artifact),
            ).fetchall()
            revisions = connection.execute(
                """SELECT sequence,id,
                    length(CAST(artifact_refs_json AS BLOB))
                    +length(CAST(source_snapshots_json AS BLOB))
                    +length(CAST(message_ids_json AS BLOB)) AS size_bytes
                FROM context_revisions WHERE thread_id=? AND sequence>?
                ORDER BY sequence LIMIT 201""",
                (thread_id, after_revision),
            ).fetchall()
            compactions = connection.execute(
                """SELECT sequence,id, length(CAST(summary_json AS BLOB)) AS size_bytes
                FROM compactions WHERE thread_id=? AND sequence>?
                ORDER BY sequence LIMIT 201""",
                (thread_id, after_compaction),
            ).fetchall()
            history = connection.execute(
                """SELECT COUNT(*) AS count,
                    COALESCE(SUM(length(CAST(body AS BLOB))),0) AS size_bytes
                FROM items WHERE thread_id=?""",
                (thread_id,),
            ).fetchone()
            memory = connection.execute(
                """SELECT COUNT(*) AS count,
                    COALESCE(SUM(length(CAST(body AS BLOB))),0) AS bytes
                FROM memory_versions WHERE source_session_id=?""",
                (session_id,),
            ).fetchone()
            approvals = connection.execute(
                """SELECT COUNT(*) AS count,
                    COALESCE(SUM(length(CAST(detail_summary AS BLOB))),0) AS bytes
                FROM approval_requests WHERE session_id=?""",
                (session_id,),
            ).fetchone()
            approval_audit = connection.execute(
                """SELECT COUNT(*) AS count,
                    COALESCE(SUM(length(CAST(e.body AS BLOB))),0) AS bytes
                FROM approval_audit_events e JOIN approval_requests a ON a.id=e.approval_id
                WHERE a.session_id=?""",
                (session_id,),
            ).fetchone()
            security_audit = connection.execute(
                """SELECT COUNT(*) AS count,
                    COALESCE(SUM(length(CAST(detail AS BLOB))),0) AS bytes
                FROM security_audit_events WHERE json_extract(detail,'$.thread_id')=?""",
                (thread_id,),
            ).fetchone()
            leases = connection.execute(
                """SELECT COUNT(*) AS count,
                    COALESCE(SUM(length(CAST(owner_id AS BLOB))
                    +length(CAST(lease_token AS BLOB))),0) AS bytes
                FROM session_run_leases WHERE session_id=? AND released_at IS NULL""",
                (session_id,),
            ).fetchone()
            receipts = connection.execute(
                """SELECT COUNT(*) AS count,
                    COALESCE(SUM(length(CAST(COALESCE(result_json,'') AS BLOB))),0) AS bytes
                FROM tool_action_receipts WHERE session_id=?""",
                (session_id,),
            ).fetchone()
            tool_snapshots = connection.execute(
                """SELECT COUNT(*) AS count,
                    COALESCE(SUM(length(CAST(tool_json AS BLOB))),0) AS bytes
                FROM mcp_tool_snapshots"""
            ).fetchone()
        next_cursor: dict[str, int] = {
            "after_artifact": after_artifact,
            "after_revision": after_revision,
            "after_compaction": after_compaction,
        }
        has_more = False
        if len(artifacts) > 500:
            has_more = True
            next_cursor["after_artifact"] = int(artifacts[499]["sequence"])
        elif artifacts:
            next_cursor["after_artifact"] = int(artifacts[-1]["sequence"])
        if len(revisions) > 200:
            has_more = True
            next_cursor["after_revision"] = int(revisions[199]["sequence"])
        elif revisions:
            next_cursor["after_revision"] = int(revisions[-1]["sequence"])
        if len(compactions) > 200:
            has_more = True
            next_cursor["after_compaction"] = int(compactions[199]["sequence"])
        elif compactions:
            next_cursor["after_compaction"] = int(compactions[-1]["sequence"])
        resources.extend(
            self._artifact_item(thread_id, str(row["id"]), policy) for row in artifacts[:500]
        )
        resources.extend(
            ResourceItem(
                id=f"context_revision:{row['id']}",
                kind="context_revision",
                owner_thread_id=thread_id,
                size_bytes=int(row["size_bytes"]),
                retention_reason="immutable Provider input and recovery evidence",
                hold_reason="context_revision",
                state="retained",
            )
            for row in revisions[:200]
        )
        resources.extend(
            ResourceItem(
                id=f"compaction:{row['id']}",
                kind="compaction",
                owner_thread_id=thread_id,
                size_bytes=int(row["size_bytes"]),
                retention_reason="compaction preserves thread meaning",
                hold_reason="compaction",
                state="retained",
            )
            for row in compactions[:200]
        )
        if (
            after_artifact == after_revision == after_compaction == 0
            and history
            and int(history["count"]) > 0
        ):
            resources.append(
                ResourceItem(
                    id=f"thread_history:{thread_id}",
                    kind="thread_history",
                    owner_thread_id=thread_id,
                    size_bytes=int(history["size_bytes"]),
                    retention_reason="canonical chat history",
                    hold_reason="canonical_history",
                    state="retained",
                )
            )
        if after_artifact == after_revision == after_compaction == 0:
            for kind, code, label, row in (
                ("memory", "memory", "long-term Memory is versioned and retained", memory),
                ("approval_audit", "approval", "approval decisions are authoritative", approvals),
                (
                    "approval_audit",
                    "approval_events",
                    "approval audit is append-only",
                    approval_audit,
                ),
                (
                    "approval_audit",
                    "security_events",
                    "security audit is append-only",
                    security_audit,
                ),
                ("run_state", "lease", "active Run can be resumed or reconciled", leases),
                ("run_state", "receipts", "tool receipts preserve side-effect evidence", receipts),
                ("tool_snapshot", "mcp_schema", "shared MCP tool schema snapshot", tool_snapshots),
            ):
                if row and int(row["count"]) > 0:
                    resources.append(
                        ResourceItem(
                            id=f"{kind}:{thread_id}:{code}",
                            kind=cast(
                                Literal["memory", "approval_audit", "run_state", "tool_snapshot"],
                                kind,
                            ),
                            owner_thread_id=(None if kind == "tool_snapshot" else thread_id),
                            size_bytes=int(row["bytes"]),
                            retention_reason=f"{label}; {int(row['count'])} records",
                            hold_reason=kind,
                            state="retained",
                        )
                    )
            manager = getattr(self.service, "terminal_manager", None)
            if manager is not None:
                for terminal in tuple(manager.sessions.values()):
                    if terminal.thread_id == thread_id:
                        resources.append(
                            ResourceItem(
                                id=f"terminal:{terminal.id}",
                                kind="terminal",
                                owner_thread_id=thread_id,
                                size_bytes=0,
                                retention_reason="PTY process and bounded output queue",
                                hold_reason=(
                                    "active_terminal"
                                    if terminal.status in {"waiting", "running"}
                                    else "terminal_lifecycle_owned_by_manager"
                                ),
                                state=terminal.status,
                            )
                        )
        if after_artifact == after_revision == after_compaction == 0:
            for profile in browser_profile_inventory():
                resources.append(
                    ResourceItem(
                        id=f"browser_profile:{profile.profile_id}",
                        kind="browser_profile",
                        size_bytes=profile.size_bytes,
                        retention_reason=profile.retention_reason,
                        hold_reason="thread_owner_unverified",
                        state=profile.state,
                    )
                )
        return ResourceInventory(
            thread_id=thread_id,
            policy=policy,
            resources=resources,
            truncated=has_more,
            next_cursor=next_cursor,
        )

    def pin(self, thread_id: str, resource_id: str, *, pinned: bool) -> ResourceItem:
        artifact_id = self._owned_artifact_id(thread_id, resource_id, require_normal=True)
        item = self._artifact_item(thread_id, artifact_id, self.repository.get(thread_id))
        if item.state not in {"active", "archived"}:
            raise ConflictError("only active Artifacts can change Pin state")
        capability = self.service.issue_artifact_capability(
            artifact_id, operation="retention_pin", access_level=ArtifactAccessLevel.NORMAL
        )
        self.service.set_artifact_pin(artifact_id, pinned=pinned, capability=capability)
        return self._artifact_item(thread_id, artifact_id, self.repository.get(thread_id))

    def _owned_artifact_id(
        self, thread_id: str, resource_id: str, *, require_normal: bool = False
    ) -> str:
        if not resource_id.startswith("artifact:"):
            raise NotFoundError("Artifact not found in this conversation")
        artifact_id = resource_id.removeprefix("artifact:")
        artifact = self.service.get_artifact(artifact_id, verify=False)
        session_id = self._session_id(thread_id)
        if not any(
            (ref.source_type.value == "thread" and ref.source_id == thread_id)
            or (ref.source_type.value == "session" and ref.source_id == session_id)
            for ref in artifact.source_refs
        ):
            raise NotFoundError("Artifact not found in this conversation")
        if require_normal and artifact.sensitivity is not ArtifactSensitivity.NORMAL:
            raise PermissionError("sensitive Artifact Pin requires an explicit capability")
        return artifact_id

    def _owned_snapshot(self, thread_id: str, resource_id: str) -> ResourceItem:
        if not resource_id.startswith("artifact:"):
            raise NotFoundError("temporary snapshot not found")
        artifact_id = resource_id.removeprefix("artifact:")
        artifact = self.service.get_artifact(artifact_id, verify=False)
        if artifact.retention_policy_ref != "context-reference-temporary" or not any(
            ref.source_type.value == "thread" and ref.source_id == thread_id
            for ref in artifact.source_refs
        ):
            raise NotFoundError("temporary snapshot not found")
        return self._artifact_item(thread_id, artifact_id, self.repository.get(thread_id))

    def _physical_delete_temporary(self, thread_id: str, artifact_id: str) -> None:
        """Hold the SQLite writer and managed blob lock across final checks and unlink."""
        blob_store = self.service._artifact_blob_store()
        missing_blob = False
        with blob_store.mutation_guard(), self.service.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT a.content_hash,a.size_bytes,a.retention_policy_ref,
                              r.lifecycle,r.pinned,r.updated_at
                    FROM artifacts a JOIN artifact_retention_states r ON r.artifact_id=a.id
                    JOIN artifact_source_refs s ON s.artifact_id=a.id
                    WHERE a.id=? AND s.source_type='thread' AND s.source_id=?""",
                (artifact_id, thread_id),
            ).fetchone()
            if row is None or row["retention_policy_ref"] != "context-reference-temporary":
                raise NotFoundError("temporary snapshot not found")
            if row["lifecycle"] != "trashed" or row["pinned"]:
                raise ConflictError("temporary snapshot is not ready for physical cleanup")
            if self._active_or_unknown_run(thread_id):
                raise ConflictError("active_or_recoverable_run")
            if self.service.store.artifact_deletion_blockers(artifact_id):
                raise ConflictError("temporary snapshot has protected references")
            unknown = connection.execute(
                """SELECT 1 FROM artifact_retention_audit_events
                    WHERE artifact_id=? AND event_type='retention.physical_delete_outcome_unknown'
                    LIMIT 1""",
                (artifact_id,),
            ).fetchone()
            if unknown:
                raise ConflictError("physical cleanup outcome needs manual reconciliation")
            try:
                blob_store.verify(str(row["content_hash"]), int(row["size_bytes"]))
            except ArtifactNotFoundError:
                self.service.store._append_artifact_audit_event_on_connection(
                    connection,
                    artifact_id=artifact_id,
                    event_type="retention.physical_delete_outcome_unknown",
                    content_hash=str(row["content_hash"]),
                    outcome="outcome_unknown",
                )
                missing_blob = True
            if not missing_blob:
                deleted_blob = False
                try:
                    blob_store.delete_verified(str(row["content_hash"]), int(row["size_bytes"]))
                    deleted_blob = True
                    now = utc_now().isoformat()
                    updated = connection.execute(
                        """UPDATE artifact_retention_states SET lifecycle='deleted',
                               deleted_at=?,updated_at=?
                            WHERE artifact_id=? AND lifecycle='trashed' AND pinned=0
                              AND updated_at=?""",
                        (now, now, artifact_id, row["updated_at"]),
                    ).rowcount
                    if updated != 1:
                        raise ConflictError("temporary snapshot changed during cleanup")
                    self.service.store._append_artifact_audit_event_on_connection(
                        connection,
                        artifact_id=artifact_id,
                        event_type="retention.physical_delete_completed",
                        content_hash=str(row["content_hash"]),
                        action_hash=canonical_action_hash(
                            {"action": "resource.cleanup", "artifact_id": artifact_id}
                        ),
                        outcome="completed",
                    )
                except Exception:
                    if deleted_blob:
                        # The file is gone but the DB transaction may roll back.
                        # Record the unknown outcome after rollback for manual review.
                        connection.rollback()
                        with self.service.store._connect() as audit:
                            audit.execute("BEGIN IMMEDIATE")
                            self.service.store._append_artifact_audit_event_on_connection(
                                audit,
                                artifact_id=artifact_id,
                                event_type="retention.physical_delete_outcome_unknown",
                                content_hash=str(row["content_hash"]),
                                outcome="outcome_unknown",
                            )
                    raise
        if missing_blob:
            raise ConflictError("physical cleanup outcome needs manual reconciliation")

    def preview_cleanup(
        self,
        thread_id: str,
        *,
        resource_ids: list[str] | None,
        mode: CleanupMode,
        after_artifact: int = 0,
    ) -> ResourceCleanupPreview:
        inventory = self.inventory(thread_id, after_artifact=after_artifact)
        selected = (
            [item for item in inventory.resources if item.kind == "context_reference_snapshot"]
            if resource_ids is None
            else [
                self._artifact_item(
                    thread_id,
                    self._owned_artifact_id(thread_id, resource_id),
                    inventory.policy,
                )
                if resource_id.startswith("artifact:")
                else next((item for item in inventory.resources if item.id == resource_id), None)
                for resource_id in resource_ids
            ]
        )
        items: list[ResourceCleanupItem] = []
        for item in selected:
            if item is None:
                raise NotFoundError("resource not found in this thread")
            eligible = item.kind == "context_reference_snapshot" and item.hold_reason is None
            if mode == "automatic":
                eligible = eligible and item.auto_cleanup_eligible
            reason = (
                "eligible" if eligible else item.hold_reason or "ttl_not_due_or_protected_resource"
            )
            items.append(
                ResourceCleanupItem(
                    resource_id=item.id,
                    eligible=eligible,
                    reason=reason,
                    size_bytes=item.size_bytes,
                )
            )
        has_more_artifacts = False
        if resource_ids is None:
            session_id = self._session_id(thread_id)
            with self.service.store._connect() as connection:
                has_more_artifacts = (
                    connection.execute(
                        """SELECT 1 FROM artifacts a
                    JOIN artifact_source_refs s ON s.artifact_id=a.id
                    WHERE a.sequence>? AND
                      ((s.source_type='thread' AND s.source_id=?)
                       OR (s.source_type='session' AND s.source_id=?)) LIMIT 1""",
                        (inventory.next_cursor["after_artifact"], thread_id, session_id),
                    ).fetchone()
                    is not None
                )
        return ResourceCleanupPreview(
            items=items,
            total_bytes=sum(item.size_bytes for item in items if item.eligible),
            truncated=has_more_artifacts,
            next_cursor=inventory.next_cursor["after_artifact"],
        )

    def cleanup(
        self, thread_id: str, *, resource_ids: list[str], mode: CleanupMode
    ) -> ResourceCleanupResult:
        removed: list[str] = []
        skipped: list[ResourceCleanupItem] = []
        for resource_id in resource_ids[:100]:
            latest = self.preview_cleanup(thread_id, resource_ids=[resource_id], mode=mode).items[0]
            if not latest.eligible:
                skipped.append(latest)
                continue
            artifact_id = resource_id.removeprefix("artifact:")
            try:
                for operation, action in (
                    ("retention_schedule", self.service.schedule_artifact_deletion),
                    ("retention_trash", self.service.trash_artifact),
                ):
                    state = self.service.get_artifact_retention_state(artifact_id)
                    if operation == "retention_schedule" and state.lifecycle in {
                        RetentionLifecycle.DELETION_SCHEDULED,
                        RetentionLifecycle.TRASHED,
                    }:
                        continue
                    if (
                        operation == "retention_trash"
                        and state.lifecycle is RetentionLifecycle.TRASHED
                    ):
                        continue
                    rechecked = self.preview_cleanup(
                        thread_id, resource_ids=[resource_id], mode=mode
                    ).items[0]
                    if not rechecked.eligible:
                        raise ConflictError(rechecked.reason)
                    if self._active_or_unknown_run(thread_id):
                        raise ConflictError("active_or_recoverable_run")
                    capability = self.service.issue_artifact_capability(
                        artifact_id, operation=operation, access_level=ArtifactAccessLevel.NORMAL
                    )
                    action(artifact_id, capability=capability)
                rechecked = self.preview_cleanup(
                    thread_id, resource_ids=[resource_id], mode=mode
                ).items[0]
                if not rechecked.eligible:
                    raise ConflictError(rechecked.reason)
                self._physical_delete_temporary(thread_id, artifact_id)
                removed.append(resource_id)
            except (ArtifactStoreError, ConflictError, PermissionError, NotFoundError) as exc:
                skipped.append(
                    ResourceCleanupItem(
                        resource_id=resource_id,
                        eligible=False,
                        reason=str(exc),
                        size_bytes=latest.size_bytes,
                    )
                )
        return ResourceCleanupResult(removed_ids=removed, skipped=skipped)

    def scan_due(
        self,
        *,
        thread_limit: int = 100,
        resource_limit: int = 100,
        stop_requested: Callable[[], bool] | None = None,
    ) -> int:
        removed = 0
        threads = self.repository.scan_threads(
            after_thread_id=self._scan_thread_cursor, limit=thread_limit
        )
        if not threads:
            self._scan_thread_cursor = ""
            threads = self.repository.scan_threads(limit=thread_limit)
        for thread_id in threads:
            if stop_requested is not None and stop_requested():
                break
            cursor = self._scan_artifact_cursors.get(thread_id, 0)
            preview = self.preview_cleanup(
                thread_id, resource_ids=None, mode="automatic", after_artifact=cursor
            )
            if not preview.truncated and not preview.items and cursor:
                cursor = 0
                preview = self.preview_cleanup(
                    thread_id, resource_ids=None, mode="automatic", after_artifact=0
                )
            self._scan_artifact_cursors[thread_id] = preview.next_cursor if preview.truncated else 0
            due = [item.resource_id for item in preview.items if item.eligible][:resource_limit]
            if due:
                removed += len(
                    self.cleanup(thread_id, resource_ids=due, mode="automatic").removed_ids
                )
            self._scan_thread_cursor = thread_id
        return removed
