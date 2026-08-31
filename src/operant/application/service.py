from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Collection
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from operant.application.context import PersistentContextComposer
from operant.application.defaults import default_role_presets
from operant.application.factory import AgentFactory
from operant.application.trace import (
    WorkflowTraceSummary,
    summarize_session_trace,
    summarize_workflow_trace,
    workflow_trace_jsonl,
)
from operant.artifacts import ArtifactStore
from operant.domain.actions import (
    ApprovalRequest,
    ApprovalStatus,
    ToolActionReceipt,
    ToolActionReceiptStatus,
)
from operant.domain.context import ContextRevision, ReferenceRequest
from operant.domain.evaluation import (
    EvaluationResult,
    EvaluationRun,
    EvaluationRunEvent,
    EvaluationRunStatus,
    EvaluationSuite,
    EvaluationSuiteStatus,
)
from operant.domain.memory import (
    Memory,
    MemoryKind,
    MemorySource,
    MemoryStatus,
    default_memory_status,
    parse_memory_scope,
    passes_conservative_activation,
)
from operant.domain.models import (
    AgentStatus,
    Event,
    ModelProfile,
    RolePreset,
    RoleSnapshot,
    Session,
    new_id,
)
from operant.domain.threads import (
    Artifact,
    ArtifactSensitivity,
    ArtifactSourceRef,
    ConversationThread,
    Item,
    ItemPayload,
    ThreadStatus,
    Turn,
)
from operant.domain.workflow import WorkflowRun, WorkflowRunEvent, WorkflowRunStatus
from operant.persistence.sqlite import (
    ActionOutcomeUnknownError,
    ConflictError,
    IdempotencyConflictError,
    NotFoundError,
    SessionRunLease,
    SQLiteStore,
    WorkflowExecutionLease,
)
from operant.protocol import redact_public_data, redact_public_text
from operant.providers.base import ModelProvider
from operant.runtime.loop import AgentLoop, RuntimeEvent, ToolActionClaim
from operant.tools.workspace import ApprovalCallback, ToolError, WorkspaceTools

DEFAULT_ARTIFACT_MAX_SIZE_BYTES = 16 * 1024 * 1024
_ITEM_PAYLOAD_ADAPTER: TypeAdapter[ItemPayload] = TypeAdapter(ItemPayload)


class _PersistentActionGateway:
    """Bind one Agent attempt to durable, argument-free Action receipts."""

    def __init__(
        self,
        *,
        store: SQLiteStore,
        session_id: str,
        agent_id: str,
        tools: WorkspaceTools,
        lease: SessionRunLease | None = None,
    ) -> None:
        self.store = store
        self.session_id = session_id
        self.agent_id = agent_id
        self.tools = tools
        self.lease = lease
        self.scope = f"session:{session_id}:agent:{agent_id}:attempt:1"

    def reserve_tool_action(
        self,
        *,
        tool_call_id: str,
        name: str,
        arguments: dict[str, Any],
    ) -> ToolActionClaim:
        action_hash = self.tools.action_hash(name, arguments)
        try:
            receipt, created = self.store.reserve_tool_action(
                ToolActionReceipt(
                    scope=self.scope,
                    idempotency_key=tool_call_id,
                    action_hash=action_hash,
                    command_name=name,
                    session_id=self.session_id,
                    agent_id=self.agent_id,
                ),
                lease=self.lease,
            )
        except IdempotencyConflictError as exc:
            raise ToolError("tool idempotency key conflicts with another action") from exc
        except ActionOutcomeUnknownError as exc:
            raise ToolError(
                "tool action outcome is unknown; manual reconciliation required"
            ) from exc
        if created:
            return ToolActionClaim(receipt_id=receipt.id, action_hash=action_hash)
        if receipt.status is ToolActionReceiptStatus.COMPLETED and receipt.result_json is not None:
            return ToolActionClaim(
                receipt_id=receipt.id,
                action_hash=action_hash,
                replay_result=receipt.result_json,
            )
        if receipt.status is ToolActionReceiptStatus.FAILED and receipt.result_json is not None:
            return ToolActionClaim(
                receipt_id=receipt.id,
                action_hash=action_hash,
                replay_result=receipt.result_json,
                replay_is_error=True,
            )
        raise ToolError("tool action outcome is unknown; manual reconciliation required")

    def complete_tool_action(self, claim: ToolActionClaim, result: str) -> None:
        self.store.complete_tool_action(
            claim.receipt_id,
            action_hash=claim.action_hash,
            result_json=redact_public_text(result),
        )

    def fail_tool_action(
        self,
        claim: ToolActionClaim,
        *,
        error_code: str,
        result: str,
    ) -> None:
        self.store.fail_tool_action(
            claim.receipt_id,
            action_hash=claim.action_hash,
            error_code=redact_public_text(error_code, max_chars=200),
            result_json=redact_public_text(result),
        )

    def request_approval(
        self,
        claim: ToolActionClaim,
        *,
        tool_call_id: str,
        category: str,
        detail: str,
    ) -> dict[str, Any]:
        approval = self.store.create_approval_request(
            ApprovalRequest(
                session_id=self.session_id,
                agent_id=self.agent_id,
                tool_action_receipt_id=claim.receipt_id,
                tool_call_id=tool_call_id,
                action_hash=claim.action_hash,
                category=category,
                detail_summary=redact_public_text(detail, max_chars=500),
            )
        )
        return {
            "approval_id": approval.id,
            "action_hash": approval.action_hash,
            "expires_at": approval.expires_at.isoformat(),
            "continuation_available": True,
        }

    def verify_approval(
        self,
        claim: ToolActionClaim,
        *,
        tool_call_id: str,
        name: str,
        arguments: dict[str, Any],
    ) -> None:
        normalized_hash = self.tools.action_hash(name, arguments)
        if normalized_hash != claim.action_hash:
            raise ToolError("approved action changed before execution")
        approval = self.store.get_approval_request(self.session_id, tool_call_id)
        try:
            receipt = self.store.get_tool_action_receipt(claim.receipt_id)
        except NotFoundError as exc:
            raise ToolError("approval receipt is unavailable") from exc
        if (
            approval.agent_id != self.agent_id
            or approval.tool_action_receipt_id != claim.receipt_id
            or approval.action_hash != normalized_hash
            or receipt.session_id != self.session_id
            or receipt.agent_id != self.agent_id
            or receipt.idempotency_key != tool_call_id
            or receipt.action_hash != normalized_hash
            or receipt.command_name != name
            or receipt.status is not ToolActionReceiptStatus.IN_PROGRESS
        ):
            raise ToolError("approval does not match this exact agent action")
        decision = self.store.get_approval_decision(approval.id)
        if (
            approval.status is not ApprovalStatus.APPROVED
            or decision is None
            or decision.approval_id != approval.id
            or not decision.approved
        ):
            raise ToolError("approval is not valid for execution")

    def verify_execution(self) -> None:
        if self.lease is None:
            return
        try:
            self.store.assert_session_run_lease(self.lease)
        except ConflictError as exc:
            raise ToolError("session run lease is expired, cancelled, or fenced") from exc


class ApplicationService:
    """Use-case layer shared by CLI, API, and workflows."""

    def __init__(
        self,
        store: SQLiteStore,
        provider: ModelProvider,
        *,
        session_lease_ttl_seconds: float = 15.0,
        session_lease_heartbeat_seconds: float | None = None,
        artifact_root: str | Path | None = None,
        artifact_max_size_bytes: int = DEFAULT_ARTIFACT_MAX_SIZE_BYTES,
        artifact_store: ArtifactStore | None = None,
    ) -> None:
        if session_lease_ttl_seconds <= 0:
            raise ValueError("session lease TTL must be positive")
        self.store = store
        self.provider = provider
        self.factory = AgentFactory(store)
        self._cancellations: dict[str, asyncio.Event] = {}
        self._approval_futures: dict[tuple[str, str], asyncio.Future[bool]] = {}
        self._approval_details: dict[tuple[str, str], dict[str, str]] = {}
        self._session_run_leases: dict[str, SessionRunLease] = {}
        self._workflow_execution_leases: dict[str, WorkflowExecutionLease] = {}
        self._lease_owner_id = new_id("core")
        self._session_lease_ttl_seconds = session_lease_ttl_seconds
        self._session_lease_heartbeat_seconds = (
            session_lease_ttl_seconds / 3
            if session_lease_heartbeat_seconds is None
            else session_lease_heartbeat_seconds
        )
        if not 0 < self._session_lease_heartbeat_seconds < session_lease_ttl_seconds:
            raise ValueError("session and workflow lease heartbeat must be positive and below TTL")
        if artifact_max_size_bytes < 1:
            raise ValueError("artifact maximum size must be positive")
        if artifact_store is not None and artifact_root is not None:
            raise ValueError("provide artifact_store or artifact_root, not both")
        self._artifact_store = artifact_store
        self._artifact_root = (
            self.store.path.parent.absolute() / "artifacts"
            if artifact_root is None
            else Path(artifact_root)
        )
        if not self._artifact_root.is_absolute():
            raise ValueError("artifact root must be absolute")
        self._artifact_max_size_bytes = artifact_max_size_bytes

    def initialize(self) -> None:
        self.store.initialize()

    def close(self) -> None:
        """Release long-lived storage descriptors owned by this service."""

        if self._artifact_store is not None:
            self._artifact_store.close()
            self._artifact_store = None

    # Model Registry

    def add_model_profile(self, profile: ModelProfile) -> ModelProfile:
        return self.store.add_model_profile(profile)

    def get_model_profile(self, profile_id: str) -> ModelProfile:
        return self.store.get_model_profile(profile_id)

    def list_model_profiles(self) -> list[ModelProfile]:
        return self.store.list_model_profiles()

    def update_model_profile(self, profile_id: str, **changes: Any) -> ModelProfile:
        return self.store.update_model_profile(profile_id, **changes)

    def deactivate_model_profile(self, profile_id: str) -> ModelProfile:
        return self.store.deactivate_model_profile(profile_id)

    async def discover_models(self, *, base_url: str, secret_ref: str) -> list[str]:
        return await self.provider.list_models(base_url=base_url, secret_ref=secret_ref)

    async def check_model_profile(self, profile_id: str) -> dict[str, object]:
        profile = self.get_model_profile(profile_id)
        model_ids = await self.discover_models(
            base_url=profile.base_url,
            secret_ref=profile.secret_ref,
        )
        return {
            "profile_id": profile.id,
            "model_id": profile.model_id,
            "available": profile.model_id in model_ids,
            "discovered_models": len(model_ids),
        }

    # Role Registry

    def create_role(self, role: RolePreset) -> RolePreset:
        return self.store.create_role(role)

    def get_role(self, role_id: str, version: int | None = None) -> RolePreset:
        return self.store.get_role(role_id, version)

    def list_roles(self, *, include_inactive: bool = False) -> list[RolePreset]:
        return self.store.list_roles(include_inactive=include_inactive)

    def list_role_versions(self, role_id: str) -> list[RolePreset]:
        return self.store.list_role_versions(role_id)

    def update_role(self, role_id: str, **changes: Any) -> RolePreset:
        return self.store.update_role(role_id, **changes)

    def copy_role(self, role_id: str, *, name: str) -> RolePreset:
        return self.store.copy_role(role_id, name=name)

    def deactivate_role(self, role_id: str) -> RolePreset:
        return self.store.deactivate_role(role_id)

    def seed_default_roles(
        self,
        *,
        planner_model_profile_id: str,
        coder_model_profile_id: str,
        reviewer_model_profile_id: str,
    ) -> list[RolePreset]:
        for profile_id in {
            planner_model_profile_id,
            coder_model_profile_id,
            reviewer_model_profile_id,
        }:
            profile = self.get_model_profile(profile_id)
            if not profile.enabled:
                raise ValueError(f"model profile is inactive: {profile_id}")

        seeded: list[RolePreset] = []
        for role in default_role_presets(
            planner_model_profile_id=planner_model_profile_id,
            coder_model_profile_id=coder_model_profile_id,
            reviewer_model_profile_id=reviewer_model_profile_id,
        ):
            try:
                seeded.append(self.get_role(role.id))
            except NotFoundError:
                seeded.append(self.create_role(role))
        return seeded

    # Session and Agent Factory

    def create_session(
        self,
        role_id: str | None = None,
        *,
        new_role: RolePreset | None = None,
        model_profile_id: str | None = None,
        effort: str | None = None,
        budget_overrides: dict[str, Any] | None = None,
    ) -> Session:
        if (role_id is None) == (new_role is None):
            raise ValueError("provide exactly one of role_id or new_role")
        if new_role is not None:
            role_id = self.create_role(new_role).id
        assert role_id is not None
        return self.factory.create_session(
            role_id,
            model_profile_id=model_profile_id,
            effort=effort,
            budget_overrides=budget_overrides,
        )

    def get_session(self, session_id: str) -> Session:
        return self.store.get_session(session_id)

    def list_events(
        self,
        session_id: str,
        *,
        after_cursor: int | None = None,
        limit: int = 1000,
    ) -> list[Event]:
        self.get_session(session_id)
        return self.store.list_events(session_id, after_cursor=after_cursor, limit=limit)

    def get_context_revision(
        self,
        session_id: str,
        revision_id: str,
    ) -> ContextRevision:
        self.get_session(session_id)
        revision = self.store.get_context_revision(revision_id)
        if revision.session_id != session_id:
            raise NotFoundError(f"ContextRevision not found: {revision_id}")
        return revision

    def list_context_revisions(
        self,
        session_id: str,
        *,
        after_cursor: int | None = None,
        limit: int = 100,
    ) -> list[ContextRevision]:
        self.get_session(session_id)
        return self.store.list_context_revisions(
            session_id,
            after_cursor=after_cursor,
            limit=limit,
        )

    # Canonical Thread history and Artifact metadata

    def create_thread(self, thread: ConversationThread) -> ConversationThread:
        """Create one explicit Thread without synthesizing legacy history."""

        if thread.status is not ThreadStatus.ACTIVE or thread.archived_at is not None:
            raise ValueError("new threads must start active")
        return self.store.create_thread(thread)

    def get_thread(self, thread_id: str) -> ConversationThread:
        return self.store.get_thread(thread_id)

    def list_threads(
        self,
        *,
        after_cursor: int | None = None,
        limit: int = 100,
        parent_thread_id: str | None = None,
        workspace_ref: str | None = None,
        status: ThreadStatus | str | None = None,
    ) -> list[ConversationThread]:
        return self.store.list_threads(
            after_cursor=after_cursor,
            limit=limit,
            parent_thread_id=parent_thread_id,
            workspace_ref=workspace_ref,
            status=status,
        )

    def archive_thread(self, thread_id: str) -> ConversationThread:
        return self.store.archive_thread(thread_id)

    def set_thread_status(
        self,
        thread_id: str,
        status: ThreadStatus | str,
    ) -> ConversationThread:
        return self.store.set_thread_status(thread_id, status)

    def create_turn(self, turn: Turn) -> Turn:
        return self.store.create_turn(turn)

    def list_turns(
        self,
        thread_id: str,
        *,
        after_cursor: int | None = None,
        limit: int = 100,
    ) -> list[Turn]:
        return self.store.list_turns(
            thread_id,
            after_cursor=after_cursor,
            limit=limit,
        )

    def append_item(self, item: Item) -> Item:
        """Redact bounded dynamic content before accepting an immutable Item."""

        sanitized = redact_public_data(item.payload.model_dump(mode="json"))
        try:
            payload = _ITEM_PAYLOAD_ADAPTER.validate_python(sanitized)
        except ValidationError as exc:
            raise ValueError("item payload is invalid after safety filtering") from exc
        canonical = Item.model_validate({**item.model_dump(), "payload": payload})
        return self.store.append_item(canonical)

    def list_items(
        self,
        thread_id: str,
        *,
        after_cursor: int | None = None,
        limit: int = 100,
        turn_id: str | None = None,
    ) -> list[Item]:
        return self.store.list_items(
            thread_id,
            after_cursor=after_cursor,
            limit=limit,
            turn_id=turn_id,
        )

    def create_artifact(
        self,
        *,
        content: bytes,
        media_type: str,
        sensitivity: ArtifactSensitivity = ArtifactSensitivity.NORMAL,
        source_refs: Collection[ArtifactSourceRef] = (),
        retention_policy_ref: str = "default",
    ) -> tuple[Artifact, bool]:
        """Validate references before publishing, then register metadata atomically."""

        source_refs = tuple(source_refs)
        self.store.validate_artifact_source_refs(source_refs)
        artifact = Artifact(
            content_hash=hashlib.sha256(content).hexdigest(),
            media_type=media_type,
            size_bytes=len(content),
            sensitivity=sensitivity,
            source_refs=source_refs,
            retention_policy_ref=retention_policy_ref,
        )
        blob = self._artifact_blob_store().put_bytes(content)
        return self.store.register_artifact(artifact, storage_key=blob.storage_key)

    def get_artifact(self, artifact_id: str, *, verify: bool = True) -> Artifact:
        artifact = self.store.get_artifact(artifact_id)
        if verify:
            self._artifact_blob_store().verify(
                artifact.content_hash,
                artifact.size_bytes,
            )
        return artifact

    def list_artifacts(
        self,
        *,
        after_cursor: int | None = None,
        limit: int = 100,
        sensitivity: ArtifactSensitivity | str | None = None,
    ) -> list[Artifact]:
        # Listing is metadata-only and intentionally does not perform O(total
        # blob bytes) verification. A single-resource GET is the integrity gate.
        return self.store.list_artifacts(
            after_cursor=after_cursor,
            limit=limit,
            sensitivity=sensitivity,
        )

    def _artifact_blob_store(self) -> ArtifactStore:
        if self._artifact_store is None:
            self._artifact_store = ArtifactStore(
                self._artifact_root,
                max_size_bytes=self._artifact_max_size_bytes,
            )
        return self._artifact_store

    def _read_artifact_for_context(self, artifact_id: str) -> bytes:
        artifact = self.get_artifact(artifact_id)
        return self._artifact_blob_store().read(
            artifact.content_hash,
            artifact.size_bytes,
        )

    def _write_tool_result_artifact(
        self,
        *,
        content: bytes,
    ) -> Artifact:
        content_hash = hashlib.sha256(content).hexdigest()

        def reuse_existing() -> Artifact:
            existing = self.store.get_artifact_by_hash(content_hash)
            if existing.sensitivity is not ArtifactSensitivity.NORMAL:
                raise PermissionError(
                    "existing sensitive Artifact cannot be reused as a normal Tool Result Stub"
                )
            self._artifact_blob_store().verify(existing.content_hash, existing.size_bytes)
            return existing

        try:
            return reuse_existing()
        except NotFoundError:
            pass
        try:
            artifact, _created = self.create_artifact(
                content=content,
                media_type="text/plain; charset=utf-8",
                sensitivity=ArtifactSensitivity.NORMAL,
                # Artifact metadata is content-hash unique. Keep automatic Tool
                # Result metadata stable across agents/sessions; each immutable
                # ContextRevision carries the typed request-local association.
                source_refs=(),
                retention_policy_ref="context-tool-result",
            )
            return artifact
        except ConflictError:
            # Another writer may have registered the hash after our lookup.
            # Re-read and apply the same sensitivity/integrity gate.
            return reuse_existing()

    # Workflow persistence

    def create_workflow_run(self, workflow_run: WorkflowRun) -> WorkflowRun:
        return self.store.create_workflow_run(workflow_run)

    def get_workflow_run(self, workflow_run_id: str) -> WorkflowRun:
        return self.store.get_workflow_run(workflow_run_id)

    def list_workflow_runs(
        self,
        *,
        status: WorkflowRunStatus | str | None = None,
        limit: int | None = None,
    ) -> list[WorkflowRun]:
        return self.store.list_workflow_runs(status=status, limit=limit)

    def update_workflow_run(self, workflow_run_id: str, **changes: Any) -> WorkflowRun:
        return self.store.update_workflow_run(workflow_run_id, **changes)

    def update_workflow_run_if_status(
        self,
        workflow_run_id: str,
        *,
        expected_status: WorkflowRunStatus | str,
        **changes: Any,
    ) -> WorkflowRun | None:
        return self.store.update_workflow_run_if_status(
            workflow_run_id,
            expected_status=expected_status,
            **changes,
        )

    def append_workflow_event(self, event: WorkflowRunEvent) -> WorkflowRunEvent:
        return self.store.append_workflow_event(event)

    def list_workflow_events(
        self,
        workflow_run_id: str,
        *,
        after_cursor: int | None = None,
        limit: int = 1000,
    ) -> list[WorkflowRunEvent]:
        return self.store.list_workflow_events(
            workflow_run_id,
            after_cursor=after_cursor,
            limit=limit,
        )

    def cancel_workflow_run(self, workflow_run_id: str) -> bool:
        changed, leased_session_ids = self.store.cancel_workflow_run_atomically(workflow_run_id)
        if not changed:
            return False
        session_ids = set(leased_session_ids)
        historical_session_ids = {
            event.session_id
            for event in self.list_workflow_events(workflow_run_id)
            if event.session_id is not None
        }
        for session_id in historical_session_ids.difference(session_ids):
            self.store.cancel_session_run_lease(session_id)
        session_ids.update(historical_session_ids)
        for session_id in session_ids:
            cancellation = self._cancellations.get(session_id)
            if cancellation is not None:
                cancellation.set()
        return True

    def cancel_workflow_children(self, workflow_run_id: str) -> None:
        """Fence every active child without changing the Workflow status."""

        session_ids = set(self.store.cancel_workflow_run_leases(workflow_run_id))
        # Preserve cancellation for runs created before workflow_id was bound
        # into a v4 lease, while stopping every active child instead of only
        # the most recent event's Session.
        session_ids.update(
            event.session_id
            for event in self.list_workflow_events(workflow_run_id)
            if event.session_id is not None
        )
        for session_id in session_ids:
            self.store.cancel_session_run_lease(session_id)
            cancellation = self._cancellations.get(session_id)
            if cancellation is not None:
                cancellation.set()

    def get_workflow_trace(self, workflow_run_id: str) -> WorkflowTraceSummary:
        run = self.get_workflow_run(workflow_run_id)
        workflow_events = self.list_workflow_events(workflow_run_id)
        sessions = self._workflow_trace_sessions(workflow_events)
        summaries = [summarize_session_trace(session, events) for session, events in sessions]
        return summarize_workflow_trace(run, workflow_events, summaries)

    def export_workflow_trace_jsonl(self, workflow_run_id: str) -> list[str]:
        run = self.get_workflow_run(workflow_run_id)
        workflow_events = self.list_workflow_events(workflow_run_id)
        return list(
            workflow_trace_jsonl(
                run,
                workflow_events,
                self._workflow_trace_sessions(workflow_events),
            )
        )

    def _workflow_trace_sessions(
        self,
        workflow_events: list[WorkflowRunEvent],
    ) -> list[tuple[Session, list[Event]]]:
        session_ids = tuple(
            dict.fromkeys(event.session_id for event in workflow_events if event.session_id)
        )
        return [
            (self.get_session(session_id), self.list_events(session_id))
            for session_id in session_ids
        ]

    def create_workflow(self, workflow_run: WorkflowRun) -> WorkflowRun:
        return self.create_workflow_run(workflow_run)

    def get_workflow(self, workflow_run_id: str) -> WorkflowRun:
        return self.get_workflow_run(workflow_run_id)

    def list_workflows(
        self,
        *,
        status: WorkflowRunStatus | str | None = None,
        limit: int | None = None,
    ) -> list[WorkflowRun]:
        return self.list_workflow_runs(status=status, limit=limit)

    def update_workflow(self, workflow_run_id: str, **changes: Any) -> WorkflowRun:
        return self.update_workflow_run(workflow_run_id, **changes)

    def append_workflow_run_event(self, event: WorkflowRunEvent) -> WorkflowRunEvent:
        return self.append_workflow_event(event)

    def list_workflow_run_events(self, workflow_run_id: str) -> list[WorkflowRunEvent]:
        return self.list_workflow_events(workflow_run_id)

    # Evaluation persistence

    def create_evaluation_suite(self, suite: EvaluationSuite) -> EvaluationSuite:
        return self.store.create_evaluation_suite(suite)

    def get_evaluation_suite(self, suite_id: str) -> EvaluationSuite:
        return self.store.get_evaluation_suite(suite_id)

    def list_evaluation_suites(
        self,
        *,
        status: EvaluationSuiteStatus | str | None = None,
        limit: int | None = None,
    ) -> list[EvaluationSuite]:
        return self.store.list_evaluation_suites(status=status, limit=limit)

    def create_evaluation_run(self, evaluation_run: EvaluationRun) -> EvaluationRun:
        return self.store.create_evaluation_run(evaluation_run)

    def get_evaluation_run(self, evaluation_run_id: str) -> EvaluationRun:
        return self.store.get_evaluation_run(evaluation_run_id)

    def list_evaluation_runs(
        self,
        *,
        suite_id: str | None = None,
        status: EvaluationRunStatus | str | None = None,
        limit: int | None = None,
    ) -> list[EvaluationRun]:
        return self.store.list_evaluation_runs(
            suite_id=suite_id,
            status=status,
            limit=limit,
        )

    def update_evaluation_run(self, evaluation_run_id: str, **changes: Any) -> EvaluationRun:
        return self.store.update_evaluation_run(evaluation_run_id, **changes)

    def append_evaluation_event(self, event: EvaluationRunEvent) -> EvaluationRunEvent:
        return self.store.append_evaluation_event(event)

    def list_evaluation_events(
        self,
        evaluation_run_id: str,
        *,
        after_cursor: int | None = None,
        limit: int = 1000,
    ) -> list[EvaluationRunEvent]:
        return self.store.list_evaluation_events(
            evaluation_run_id,
            after_cursor=after_cursor,
            limit=limit,
        )

    def append_evaluation_result(self, result: EvaluationResult) -> EvaluationResult:
        return self.store.append_evaluation_result(result)

    def get_evaluation_result(self, result_id: str) -> EvaluationResult:
        return self.store.get_evaluation_result(result_id)

    def update_evaluation_result(self, result_id: str, **changes: Any) -> EvaluationResult:
        return self.store.update_evaluation_result(result_id, **changes)

    def list_evaluation_results(self, evaluation_run_id: str) -> list[EvaluationResult]:
        return self.store.list_evaluation_results(evaluation_run_id)

    # Memory use cases

    def save_memory(
        self,
        memory: Memory | RoleSnapshot | None = None,
        *,
        snapshot: RoleSnapshot | None = None,
        session_id: str | None = None,
        kind: MemoryKind | str | None = None,
        content: str | None = None,
        project_scope: str | None = None,
        role_scope: Collection[str] | str | None = None,
        source_session_id: str | None = None,
        source_task: str | None = None,
        confidence: float = 0.5,
        status: MemoryStatus | str | None = None,
        confirmed: bool = False,
        confirm: bool | None = None,
        allow_conservative_activation: bool = False,
    ) -> Memory:
        """Save a Memory after applying the RoleSnapshot's write scope.

        The method accepts either an already-created ``Memory`` or the fields
        needed to create one.  For convenience a RoleSnapshot may be the first
        positional argument; the normal explicit form is
        ``save_memory(memory, snapshot=snapshot)``.

        Durable episodic/project items start as candidates.  They become active
        only with explicit confirmation or when the caller opts into the narrow
        provenance-and-verification rule implemented by the domain layer.
        """

        if isinstance(memory, RoleSnapshot):
            if snapshot is not None:
                raise ValueError("provide the RoleSnapshot only once")
            snapshot = memory
            memory = None
        if snapshot is None:
            raise ValueError("snapshot is required for memory writes")
        if confirm is not None:
            confirmed = confirm

        if memory is None:
            if kind is None or content is None:
                raise ValueError("kind and content are required when memory is not provided")
            normalized_kind = self._coerce_memory_kind(kind)
            effective_session_id = source_session_id or session_id
            requested_status = (
                MemoryStatus(status)
                if status is not None
                else default_memory_status(normalized_kind)
            )
            memory = Memory(
                kind=normalized_kind,
                content=content,
                project_scope=project_scope,
                role_scope=self._normalize_role_scope(role_scope),
                source_session_id=effective_session_id,
                source_task=source_task,
                confidence=confidence,
                status=requested_status,
            )
        else:
            if any(
                value is not None
                for value in (
                    kind,
                    content,
                    project_scope,
                    role_scope,
                    source_session_id,
                    source_task,
                )
            ):
                raise ValueError("memory fields cannot be mixed with an existing Memory")
            if status is not None:
                memory = memory.model_copy(update={"status": MemoryStatus(status)})

        if memory.kind is MemoryKind.WORKING and memory.source_session_id is None and session_id:
            memory = memory.model_copy(update={"source_session_id": session_id})
        self._authorize_memory(
            snapshot,
            memory.kind,
            operation="write",
            memory=memory,
            session_id=session_id,
            project_scope=memory.project_scope,
        )
        memory = self._prepare_activation(
            memory,
            confirmed=confirmed,
            allow_conservative_activation=allow_conservative_activation,
        )
        return self.store.create_memory(memory)

    def create_memory(
        self,
        memory: Memory | RoleSnapshot | None = None,
        *,
        snapshot: RoleSnapshot | None = None,
        session_id: str | None = None,
        kind: MemoryKind | str | None = None,
        content: str | None = None,
        project_scope: str | None = None,
        role_scope: Collection[str] | str | None = None,
        source_session_id: str | None = None,
        source_task: str | None = None,
        confidence: float = 0.5,
        status: MemoryStatus | str | None = None,
        confirmed: bool = False,
        confirm: bool | None = None,
        allow_conservative_activation: bool = False,
    ) -> Memory:
        """Alias for :meth:`save_memory` used by CRUD-oriented callers."""

        return self.save_memory(
            memory,
            snapshot=snapshot,
            session_id=session_id,
            kind=kind,
            content=content,
            project_scope=project_scope,
            role_scope=role_scope,
            source_session_id=source_session_id,
            source_task=source_task,
            confidence=confidence,
            status=status,
            confirmed=confirmed,
            confirm=confirm,
            allow_conservative_activation=allow_conservative_activation,
        )

    def get_memory(
        self,
        memory_id: str,
        *,
        snapshot: RoleSnapshot,
        session_id: str | None = None,
        project_scope: str | None = None,
        version: int | None = None,
    ) -> Memory:
        memory = self.store.get_memory(memory_id, version)
        self._authorize_memory(
            snapshot,
            memory.kind,
            operation="read",
            memory=memory,
            session_id=session_id,
            project_scope=project_scope or memory.project_scope,
        )
        return memory

    def query_memories(
        self,
        query: str,
        *,
        snapshot: RoleSnapshot,
        session_id: str | None = None,
        project_scope: str | None = None,
        kinds: Collection[MemoryKind | str] | None = None,
        include_candidates: bool = False,
        limit: int = 20,
    ) -> list[Memory]:
        scope = parse_memory_scope(snapshot.memory_scope)
        requested_kinds: tuple[MemoryKind, ...]
        if kinds is None:
            requested_kinds = tuple(scope.read)
        else:
            requested_kinds = tuple(self._coerce_memory_kind(kind) for kind in kinds)
            denied = [kind.value for kind in requested_kinds if not scope.can_read(kind)]
            if denied:
                raise PermissionError(f"memory read scope does not allow: {sorted(set(denied))}")
        if not requested_kinds:
            return []
        for kind in requested_kinds:
            self._authorize_memory(
                snapshot,
                kind,
                operation="read",
                session_id=session_id,
                project_scope=project_scope,
            )
        return self.store.search_memories(
            query,
            project_scope=project_scope,
            source_session_id=session_id,
            kinds=requested_kinds,
            role_id=snapshot.role_id,
            role_name=snapshot.role_name,
            include_candidates=include_candidates,
            limit=limit,
        )

    def search_memories(
        self,
        query: str,
        *,
        snapshot: RoleSnapshot,
        session_id: str | None = None,
        project_scope: str | None = None,
        kinds: Collection[MemoryKind | str] | None = None,
        include_candidates: bool = False,
        limit: int = 20,
    ) -> list[Memory]:
        """Readable alias for :meth:`query_memories`."""

        return self.query_memories(
            query,
            snapshot=snapshot,
            session_id=session_id,
            project_scope=project_scope,
            kinds=kinds,
            include_candidates=include_candidates,
            limit=limit,
        )

    def update_memory(
        self,
        memory_id: str,
        *,
        snapshot: RoleSnapshot,
        session_id: str | None = None,
        project_scope: str | None = None,
        confirmed: bool = False,
        confirm: bool | None = None,
        allow_conservative_activation: bool = False,
        **changes: Any,
    ) -> Memory:
        current = self.store.get_memory(memory_id)
        self._authorize_memory(
            snapshot,
            current.kind,
            operation="write",
            memory=current,
            session_id=session_id,
            project_scope=project_scope or current.project_scope,
        )
        if confirm is not None:
            confirmed = confirm
        if "kind" in changes:
            next_kind = self._coerce_memory_kind(changes["kind"])
            changes["kind"] = next_kind
            self._authorize_memory(
                snapshot,
                next_kind,
                operation="write",
                memory=None,
                session_id=session_id,
                project_scope=changes.get("project_scope", project_scope or current.project_scope),
            )
        requested_status = changes.get("status")
        if requested_status is not None:
            changes["status"] = MemoryStatus(requested_status)
        if current.status is MemoryStatus.ACTIVE and "status" not in changes:
            changes["status"] = current.status
        else:
            preview = Memory.model_validate(
                {
                    **current.model_dump(),
                    **changes,
                    "version": current.version + 1,
                }
            )
            preview = self._prepare_activation(
                preview,
                confirmed=confirmed,
                allow_conservative_activation=allow_conservative_activation,
            )
            changes["status"] = preview.status
        return self.store.update_memory(memory_id, **changes)

    def confirm_memory(
        self,
        memory_id: str,
        *,
        snapshot: RoleSnapshot,
        session_id: str | None = None,
        project_scope: str | None = None,
    ) -> Memory:
        """Explicitly activate a candidate after a human or trusted caller confirms it."""

        current = self.store.get_memory(memory_id)
        self._authorize_memory(
            snapshot,
            current.kind,
            operation="write",
            memory=current,
            session_id=session_id,
            project_scope=project_scope or current.project_scope,
        )
        return self.store.update_memory(memory_id, status=MemoryStatus.ACTIVE)

    def activate_memory(
        self,
        memory_id: str,
        *,
        snapshot: RoleSnapshot,
        session_id: str | None = None,
        project_scope: str | None = None,
        confirmed: bool = False,
        allow_conservative_activation: bool = False,
    ) -> Memory:
        """Activate a candidate through explicit confirmation or the safe rule."""

        current = self.store.get_memory(memory_id)
        self._authorize_memory(
            snapshot,
            current.kind,
            operation="write",
            memory=current,
            session_id=session_id,
            project_scope=project_scope or current.project_scope,
        )
        self._prepare_activation(
            current,
            confirmed=confirmed,
            allow_conservative_activation=allow_conservative_activation,
            require_active=True,
        )
        return self.store.update_memory(memory_id, status=MemoryStatus.ACTIVE)

    def deactivate_memory(
        self,
        memory_id: str,
        *,
        snapshot: RoleSnapshot,
        session_id: str | None = None,
        project_scope: str | None = None,
    ) -> Memory:
        current = self.store.get_memory(memory_id)
        self._authorize_memory(
            snapshot,
            current.kind,
            operation="write",
            memory=current,
            session_id=session_id,
            project_scope=project_scope or current.project_scope,
        )
        return self.store.deactivate_memory(memory_id)

    def list_memory_versions(
        self,
        memory_id: str,
        *,
        snapshot: RoleSnapshot,
        session_id: str | None = None,
        project_scope: str | None = None,
    ) -> list[Memory]:
        versions = self.store.list_memory_versions(memory_id)
        if not versions:
            return []
        self._authorize_memory(
            snapshot,
            versions[-1].kind,
            operation="read",
            memory=versions[-1],
            session_id=session_id,
            project_scope=project_scope or versions[-1].project_scope,
        )
        return versions

    def trace_memory_source(
        self,
        memory_id: str,
        *,
        snapshot: RoleSnapshot,
        session_id: str | None = None,
        project_scope: str | None = None,
        version: int | None = None,
    ) -> MemorySource:
        memory = self.get_memory(
            memory_id,
            snapshot=snapshot,
            session_id=session_id,
            project_scope=project_scope,
            version=version,
        )
        return memory.source

    @staticmethod
    def _coerce_memory_kind(kind: MemoryKind | str) -> MemoryKind:
        return kind if isinstance(kind, MemoryKind) else MemoryKind(kind)

    @staticmethod
    def _normalize_role_scope(value: Collection[str] | str | None) -> tuple[str, ...]:
        if value is None:
            return ()
        values = value.split(",") if isinstance(value, str) else value
        return tuple(item.strip() for item in values if item.strip())

    @staticmethod
    def _prepare_activation(
        memory: Memory,
        *,
        confirmed: bool,
        allow_conservative_activation: bool,
        require_active: bool = False,
    ) -> Memory:
        if confirmed:
            return memory.model_copy(update={"status": MemoryStatus.ACTIVE})
        if allow_conservative_activation and passes_conservative_activation(memory):
            return memory.model_copy(update={"status": MemoryStatus.ACTIVE})
        if memory.status is MemoryStatus.ACTIVE and (
            memory.kind is MemoryKind.WORKING and passes_conservative_activation(memory)
        ):
            return memory
        if memory.status is not MemoryStatus.ACTIVE and not require_active:
            return memory
        raise PermissionError(
            "candidate knowledge requires explicit confirmation or conservative activation"
        )

    @staticmethod
    def _authorize_memory(
        snapshot: RoleSnapshot,
        kind: MemoryKind,
        *,
        operation: str,
        memory: Memory | None = None,
        session_id: str | None = None,
        project_scope: str | None = None,
    ) -> None:
        scope = parse_memory_scope(snapshot.memory_scope)
        if operation == "read":
            allowed = scope.can_read(kind)
        elif operation == "write":
            allowed = scope.can_write(kind)
        else:
            raise ValueError(f"unknown memory operation: {operation}")
        if not allowed:
            raise PermissionError(f"memory {operation} scope does not allow: {kind.value}")

        if kind is MemoryKind.WORKING:
            if session_id is None:
                raise ValueError("session_id is required for working memory")
            if memory is not None and memory.source_session_id != session_id:
                raise PermissionError("working memory belongs to another session")
        if kind is MemoryKind.PROJECT:
            if project_scope is None:
                raise ValueError("project_scope is required for project memory")
            if memory is not None and memory.project_scope != project_scope:
                raise PermissionError("project memory belongs to another project scope")
        if (
            memory is not None
            and memory.role_scope
            and snapshot.role_id not in memory.role_scope
            and snapshot.role_name not in memory.role_scope
            and "*" not in memory.role_scope
        ):
            raise PermissionError("memory role_scope does not include this role")

    # Runtime control

    @property
    def workflow_execution_heartbeat_seconds(self) -> float:
        return self._session_lease_heartbeat_seconds

    def acquire_workflow_execution_lease(
        self, workflow_run_id: str
    ) -> WorkflowExecutionLease | None:
        if workflow_run_id in self._workflow_execution_leases:
            return None
        lease = self.store.acquire_workflow_execution_lease(
            workflow_run_id,
            owner_id=self._lease_owner_id,
            ttl_seconds=self._session_lease_ttl_seconds,
        )
        if lease is not None:
            self._workflow_execution_leases[workflow_run_id] = lease
        return lease

    def activate_workflow_run(self, lease: WorkflowExecutionLease) -> WorkflowRun:
        current = self._workflow_execution_leases.get(lease.workflow_run_id)
        if current is None or not self._same_workflow_execution_lease(current, lease):
            raise ConflictError("workflow execution lease is no longer admitted")
        activated = self.store.activate_workflow_run(lease)
        if activated is None:
            raise ConflictError("workflow run was cancelled or its execution lease was fenced")
        return activated

    def renew_workflow_execution_lease(
        self, lease: WorkflowExecutionLease
    ) -> WorkflowExecutionLease | None:
        current = self._workflow_execution_leases.get(lease.workflow_run_id)
        if current is None or not self._same_workflow_execution_lease(current, lease):
            return None
        renewed = self.store.renew_workflow_execution_lease(
            lease,
            ttl_seconds=self._session_lease_ttl_seconds,
        )
        if renewed is not None:
            latest = self._workflow_execution_leases.get(lease.workflow_run_id)
            if latest is not None and self._same_workflow_execution_lease(latest, lease):
                self._workflow_execution_leases[lease.workflow_run_id] = renewed
        else:
            latest = self._workflow_execution_leases.get(lease.workflow_run_id)
            if latest is not None and self._same_workflow_execution_lease(latest, lease):
                self._workflow_execution_leases.pop(lease.workflow_run_id, None)
        return renewed

    def admitted_workflow_execution_lease(
        self, workflow_run_id: str
    ) -> WorkflowExecutionLease | None:
        return self._workflow_execution_leases.get(workflow_run_id)

    def verify_workflow_execution_lease(self, lease: WorkflowExecutionLease) -> bool:
        current = self._workflow_execution_leases.get(lease.workflow_run_id)
        if current is None or not self._same_workflow_execution_lease(current, lease):
            return False
        try:
            self.store.assert_workflow_execution_lease(current)
        except ConflictError:
            latest = self._workflow_execution_leases.get(lease.workflow_run_id)
            if latest is not None and self._same_workflow_execution_lease(latest, lease):
                self._workflow_execution_leases.pop(lease.workflow_run_id, None)
            return False
        return True

    def release_workflow_execution_lease(self, lease: WorkflowExecutionLease) -> None:
        current = self._workflow_execution_leases.get(lease.workflow_run_id)
        if current is not None and not self._same_workflow_execution_lease(current, lease):
            return
        if current is not None:
            self._workflow_execution_leases.pop(lease.workflow_run_id, None)
        self.store.release_workflow_execution_lease(lease)

    def interrupt_workflow_after_guard_loss(self, lease: WorkflowExecutionLease) -> bool:
        return self.store.interrupt_workflow_run_if_execution_lease_matches(
            lease,
            error_type="workflow_execution_lease_lost",
        )

    @staticmethod
    def _same_workflow_execution_lease(
        left: WorkflowExecutionLease,
        right: WorkflowExecutionLease,
    ) -> bool:
        return (
            left.lease_token == right.lease_token
            and left.generation == right.generation
            and left.owner_id == right.owner_id
        )

    def admit_session_run(
        self,
        session_id: str,
        *,
        workflow_run_id: str | None = None,
        workflow_execution_lease: WorkflowExecutionLease | None = None,
    ) -> bool:
        """Atomically reserve the cross-process run lease for one Session."""

        # A stale coroutine in this process still owns its in-memory Approval
        # futures.  Do not reclaim over it locally; cross-process reclaim is
        # handled by SQLite once the old process is gone.
        if session_id in self._session_run_leases:
            return False
        lease = self.store.acquire_session_run_lease(
            session_id,
            owner_id=self._lease_owner_id,
            ttl_seconds=self._session_lease_ttl_seconds,
            workflow_run_id=workflow_run_id,
            workflow_execution_lease=workflow_execution_lease,
        )
        if lease is None:
            return False
        self._session_run_leases[session_id] = lease
        return True

    def admitted_session_run_lease(self, session_id: str) -> SessionRunLease | None:
        return self._session_run_leases.get(session_id)

    @staticmethod
    def _same_lease(left: SessionRunLease, right: SessionRunLease) -> bool:
        return (
            left.lease_token == right.lease_token
            and left.generation == right.generation
            and left.owner_id == right.owner_id
        )

    def release_session_run(
        self,
        session_id: str,
        expected_lease: SessionRunLease | None = None,
    ) -> None:
        lease = self._session_run_leases.get(session_id)
        if lease is None:
            return
        if expected_lease is not None and not self._same_lease(lease, expected_lease):
            return
        self._session_run_leases.pop(session_id, None)
        self.store.release_session_run_lease(expected_lease or lease)

    async def run_session(
        self,
        session_id: str,
        *,
        user_message: str,
        workspace: str | Path,
        approval_callback: ApprovalCallback | None = None,
        workflow_run_id: str | None = None,
        workflow_execution_lease: WorkflowExecutionLease | None = None,
        thread_id: str | None = None,
        references: Collection[ReferenceRequest] = (),
        _admission_granted: bool = False,
    ) -> AsyncIterator[RuntimeEvent]:
        session = self.get_session(session_id)
        if not _admission_granted:
            try:
                admitted = self.admit_session_run(
                    session.id,
                    workflow_run_id=workflow_run_id,
                    workflow_execution_lease=workflow_execution_lease,
                )
            except ConflictError as exc:
                manual_reconcile = isinstance(exc, ActionOutcomeUnknownError) or (
                    "pending durable approval" in str(exc)
                )
                yield RuntimeEvent(
                    event_type="agent.stream_error",
                    turn=0,
                    payload={
                        "error": {
                            "code": (
                                "session_manual_reconcile_required"
                                if isinstance(exc, ActionOutcomeUnknownError)
                                else "session_pending_approval"
                                if manual_reconcile
                                else "session_run_conflict"
                            ),
                            "message": str(exc),
                            "retryable": not manual_reconcile,
                            "recovery": ("manual_reconcile" if manual_reconcile else "retry_later"),
                        }
                    },
                )
                return
            if not admitted:
                yield RuntimeEvent(
                    event_type="agent.stream_error",
                    turn=0,
                    payload={
                        "error": {
                            "code": "session_run_conflict",
                            "message": "session already has an active run",
                            "retryable": True,
                            "recovery": "retry_later",
                        }
                    },
                )
                return
        agent = None
        cancellation: asyncio.Event | None = None
        run_lease: SessionRunLease | None = None
        try:
            lease = self._session_run_leases.get(session.id)
            if lease is None:
                raise ConflictError("session run admission lease is unavailable")
            run_lease = lease
            agent = self.factory.create_agent(session.id)
            lease = self.store.bind_session_run_lease_agent(lease, agent.id)
            run_lease = lease
            self._session_run_leases[session.id] = lease
            self.store.update_agent_status(agent.id, AgentStatus.RUNNING)
            cancellation = asyncio.Event()
            self._cancellations[session.id] = cancellation
            tools = WorkspaceTools(
                workspace,
                policy=session.role_snapshot.tool_policy,
            )
            normalized_workspace = str(Path(workspace).resolve())
            context_composer = PersistentContextComposer(
                store=self.store,
                session=session,
                agent_id=agent.id,
                workspace=normalized_workspace,
                thread_id=thread_id,
                references=tuple(references),
                memory_resolver=lambda memory_id: self.get_memory(
                    memory_id,
                    snapshot=session.role_snapshot,
                    session_id=session.id,
                    project_scope=normalized_workspace,
                ),
                artifact_reader=self._read_artifact_for_context,
                artifact_writer=lambda content: self._write_tool_result_artifact(content=content),
            )
            loop = AgentLoop(
                self.provider,
                tools,
                action_gateway=_PersistentActionGateway(
                    store=self.store,
                    session_id=session.id,
                    agent_id=agent.id,
                    tools=tools,
                    lease=lease,
                ),
                context_composer=context_composer,
            )
        except Exception as exc:
            failure_event: RuntimeEvent | None = None
            try:
                if agent is not None:
                    failure_event = self._persist_runtime_event(
                        session.id,
                        agent.id,
                        RuntimeEvent(
                            event_type="agent.failed",
                            turn=0,
                            payload={"error_type": type(exc).__name__},
                        ),
                    )
                    self.store.update_agent_status(agent.id, AgentStatus.FAILED)
                else:
                    # AgentFactory failures happen before an Agent row exists.
                    # Keep the durable fact attached to the Session and leave
                    # ``agent_id`` null; emitting agent.failed here would claim
                    # an Agent lifecycle transition that never occurred.
                    failure_event = self._persist_runtime_event(
                        session.id,
                        None,
                        RuntimeEvent(
                            event_type="session.run_failed",
                            turn=0,
                            payload={"error_type": type(exc).__name__},
                        ),
                    )
            finally:
                if cancellation is not None and self._cancellations.get(session.id) is cancellation:
                    self._cancellations.pop(session.id, None)
                self.release_session_run(session.id, run_lease)
            if failure_event is not None:
                yield failure_event
                return
            raise
        except BaseException:
            if cancellation is not None and self._cancellations.get(session.id) is cancellation:
                self._cancellations.pop(session.id, None)
            self.release_session_run(session.id, run_lease)
            raise

        assert agent is not None
        assert cancellation is not None
        assert run_lease is not None

        async def wait_for_approval(tool_call_id: str, category: str, detail: str) -> bool:
            if approval_callback is not None:
                approved = await approval_callback(tool_call_id, category, detail)
                self.store.decide_approval(
                    session.id,
                    tool_call_id,
                    approved=approved,
                    decided_by="callback",
                )
                return approved
            key = (session.id, tool_call_id)
            future = self._approval_futures.get(key)
            if future is None:
                future = asyncio.get_running_loop().create_future()
                self._approval_futures[key] = future
                self._approval_details[key] = {
                    "category": category,
                    "detail": detail,
                }
            durable = self.store.get_approval_request(session.id, tool_call_id)
            if durable.status is not ApprovalStatus.PENDING and not future.done():
                future.set_result(durable.status is ApprovalStatus.APPROVED)
            return await future

        iterator = loop.run(
            snapshot=session.role_snapshot,
            user_message=user_message,
            approval_callback=wait_for_approval,
        )
        deadline = asyncio.get_running_loop().time() + session.role_snapshot.budget.timeout_seconds
        final_status = AgentStatus.FAILED

        async def watch_lease() -> None:
            current = run_lease
            while True:
                await asyncio.sleep(self._session_lease_heartbeat_seconds)
                renewed = self.store.renew_session_run_lease(
                    current,
                    ttl_seconds=self._session_lease_ttl_seconds,
                )
                if renewed is None:
                    cancellation.set()
                    return
                current = renewed
                mapped = self._session_run_leases.get(session.id)
                if mapped is not None and self._same_lease(mapped, run_lease):
                    self._session_run_leases[session.id] = renewed

        lease_watcher = asyncio.create_task(watch_lease())
        next_event: asyncio.Future[RuntimeEvent] | None = None
        cancelled: asyncio.Task[bool] | None = None

        try:
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    timeout_event = RuntimeEvent(
                        event_type="agent.timed_out",
                        turn=0,
                        payload={"timeout_seconds": (session.role_snapshot.budget.timeout_seconds)},
                    )
                    timeout_event = self._persist_runtime_event(session.id, agent.id, timeout_event)
                    yield timeout_event
                    final_status = AgentStatus.TIMED_OUT
                    break

                next_event = asyncio.ensure_future(anext(iterator))
                cancelled = asyncio.create_task(cancellation.wait())
                waiters: set[asyncio.Future[Any]] = {
                    next_event,
                    cancelled,
                }
                done, pending = await asyncio.wait(
                    waiters,
                    timeout=remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)

                if not done:
                    next_event.cancel()
                    cancelled.cancel()
                    await asyncio.gather(
                        next_event,
                        cancelled,
                        return_exceptions=True,
                    )
                    timeout_event = RuntimeEvent(
                        event_type="agent.timed_out",
                        turn=0,
                        payload={"timeout_seconds": (session.role_snapshot.budget.timeout_seconds)},
                    )
                    timeout_event = self._persist_runtime_event(session.id, agent.id, timeout_event)
                    yield timeout_event
                    final_status = AgentStatus.TIMED_OUT
                    break

                if cancelled in done and cancelled.result():
                    next_event.cancel()
                    await asyncio.gather(next_event, return_exceptions=True)
                    cancel_event = RuntimeEvent(
                        event_type="agent.cancelled",
                        turn=0,
                        payload={},
                    )
                    cancel_event = self._persist_runtime_event(session.id, agent.id, cancel_event)
                    yield cancel_event
                    final_status = AgentStatus.CANCELLED
                    break

                try:
                    runtime_event = next_event.result()
                except StopAsyncIteration:
                    final_status = self._status_from_events(self.store.list_events(session.id))
                    break

                if runtime_event.event_type == "tool.approval_required":
                    tool_call_id = str(runtime_event.payload["tool_call_id"])
                    key = (session.id, tool_call_id)
                    approval = self.store.get_approval_request(session.id, tool_call_id)
                    if (
                        approval.status is ApprovalStatus.PENDING
                        and key not in self._approval_futures
                    ):
                        self._approval_futures[key] = asyncio.get_running_loop().create_future()
                        self._approval_details[key] = {
                            "category": str(runtime_event.payload["category"]),
                            "detail": str(runtime_event.payload["detail"]),
                        }

                runtime_event = self._persist_runtime_event(session.id, agent.id, runtime_event)
                yield runtime_event
        except asyncio.CancelledError:
            final_status = AgentStatus.CANCELLED
            cancel_event = RuntimeEvent(
                event_type="agent.cancelled",
                turn=0,
                payload={"reason": "stream_cancelled"},
            )
            self._persist_runtime_event(session.id, agent.id, cancel_event)
            raise
        except Exception as exc:
            failure_event = RuntimeEvent(
                event_type="agent.failed",
                turn=0,
                payload={"error_type": type(exc).__name__},
            )
            failure_event = self._persist_runtime_event(session.id, agent.id, failure_event)
            yield failure_event
            final_status = AgentStatus.FAILED
        finally:
            try:
                for waiter in (next_event, cancelled):
                    if waiter is not None and not waiter.done():
                        waiter.cancel()
                await asyncio.gather(
                    *(waiter for waiter in (next_event, cancelled) if waiter is not None),
                    return_exceptions=True,
                )
                lease_watcher.cancel()
                await asyncio.gather(lease_watcher, return_exceptions=True)
                try:
                    await iterator.aclose()
                finally:
                    self.store.update_agent_status(agent.id, final_status)
            finally:
                if self._cancellations.get(session.id) is cancellation:
                    self._cancellations.pop(session.id, None)
                self._clear_session_approvals(session.id)
                self.release_session_run(session.id, run_lease)

    def cancel_session(self, session_id: str) -> bool:
        self.get_session(session_id)
        accepted = self.store.cancel_session_run_lease(session_id)
        cancellation = self._cancellations.get(session_id)
        if cancellation is not None:
            cancellation.set()
        return accepted

    def submit_approval(self, session_id: str, tool_call_id: str, *, approved: bool) -> bool:
        result = self.decide_approval(
            session_id,
            tool_call_id,
            approved=approved,
        )
        return bool(result["accepted"])

    def decide_approval(
        self,
        session_id: str,
        tool_call_id: str,
        *,
        approved: bool,
    ) -> dict[str, object]:
        self.get_session(session_id)
        request, decision, changed = self.store.decide_approval(
            session_id,
            tool_call_id,
            approved=approved,
        )
        key = (session_id, tool_call_id)
        future = self._approval_futures.get(key)
        future_available = future is not None and not future.done()
        continuation_available = future_available or session_id in self._cancellations
        if future_available:
            assert future is not None
            future.set_result(approved)
        return {
            "accepted": True,
            "changed": changed,
            "approved": decision.approved,
            "status": request.status.value,
            "approval_id": request.id,
            "continuation_available": continuation_available,
        }

    def list_pending_approvals(self, session_id: str) -> list[dict[str, object]]:
        self.get_session(session_id)
        approvals = self.store.list_approval_requests(
            session_id,
            status=ApprovalStatus.PENDING,
        )
        return [
            {
                "approval_id": approval.id,
                "tool_call_id": approval.tool_call_id,
                "category": approval.category,
                "detail": approval.detail_summary,
                "action_hash": approval.action_hash,
                "status": approval.status.value,
                "requested_at": approval.requested_at.isoformat(),
                "expires_at": approval.expires_at.isoformat(),
                "continuation_available": (
                    (session_id, approval.tool_call_id) in self._approval_futures
                    and not self._approval_futures[(session_id, approval.tool_call_id)].done()
                ),
            }
            for approval in approvals
        ]

    def _persist_runtime_event(
        self, session_id: str, agent_id: str | None, event: RuntimeEvent
    ) -> RuntimeEvent:
        sanitized_event = event.model_copy(update={"payload": redact_public_data(event.payload)})
        persisted = self.store.append_event(
            Event(
                session_id=session_id,
                agent_id=agent_id,
                event_type=sanitized_event.event_type,
                payload={"turn": sanitized_event.turn, **sanitized_event.payload},
            )
        )
        return sanitized_event.model_copy(update={"cursor": persisted.cursor})

    def _clear_session_approvals(self, session_id: str) -> None:
        keys = [key for key in self._approval_futures if key[0] == session_id]
        for key in keys:
            future = self._approval_futures.pop(key)
            if not future.done():
                future.cancel()
            self._approval_details.pop(key, None)

    @staticmethod
    def _status_from_events(events: list[Event]) -> AgentStatus:
        if events and events[-1].event_type == "agent.completed":
            return AgentStatus.COMPLETED
        if events and events[-1].event_type == "agent.cancelled":
            return AgentStatus.CANCELLED
        if events and events[-1].event_type == "agent.timed_out":
            return AgentStatus.TIMED_OUT
        return AgentStatus.FAILED
