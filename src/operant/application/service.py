from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import stat
from collections.abc import AsyncIterator, Collection, Mapping
from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import TypeAdapter, ValidationError

from operant.application.client_projection import (
    get_workspace_initialization,
    list_project_projections,
    list_workspace_files,
)
from operant.application.context import PersistentContextComposer
from operant.application.defaults import default_role_presets
from operant.application.factory import AgentFactory
from operant.application.security import (
    ActionNormalizer,
    CapabilityBroker,
    DenialRemediator,
    PolicyEngine,
    balanced_policy_bundle,
)
from operant.application.slash_commands import SlashCommandRegistry
from operant.application.trace import (
    WorkflowTraceSummary,
    summarize_session_trace,
    summarize_workflow_trace,
    workflow_trace_jsonl,
)
from operant.artifacts import (
    ArtifactCapabilityAuthority,
    ArtifactContentDeletedError,
    ArtifactCorruptionError,
    ArtifactNotFoundError,
    ArtifactSecurityError,
    ArtifactStore,
    BlobInventoryRecord,
    artifact_export_scope_fingerprint,
    export_artifact_bytes,
)
from operant.domain.actions import (
    ApprovalRequest,
    ApprovalStatus,
    CommandExecutionStatus,
    ToolActionReceipt,
    ToolActionReceiptStatus,
)
from operant.domain.commands import (
    BTWSidecarEvent,
    BTWSidecarRun,
    BTWSidecarStatus,
    ContextBaseline,
    ContextBaselineOperation,
    Phase1DCommandAuditEvent,
    ReviewRun,
    ReviewRunStatus,
    SlashCommandDefinition,
    SlashCommandKind,
    SlashCommandResolution,
    WorkspaceInitialization,
)
from operant.domain.context import (
    Compaction,
    CompactionSourceType,
    CompactionSummary,
    ContextRevision,
    ContextSourceRef,
    ContextSourceType,
    ReferenceRequest,
    deterministic_compaction_id,
)
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
    ToolPolicy,
    new_id,
    utc_now,
)
from operant.domain.projections import ProjectProjection, WorkspaceFilesPage
from operant.domain.security import (
    ActionRequest,
    Capability,
    PolicyDecision,
    PolicyEvaluation,
    SecurityAuditEvent,
)
from operant.domain.threads import (
    AgentMessagePayload,
    Artifact,
    ArtifactAccessLevel,
    ArtifactAuditFinding,
    ArtifactAuditReport,
    ArtifactRepairResult,
    ArtifactRetentionState,
    ArtifactSensitivity,
    ArtifactSourceRef,
    ArtifactSourceType,
    CacheHitStatus,
    CacheObservation,
    ConversationThread,
    Item,
    ItemPayload,
    RetentionLifecycle,
    RetentionPolicy,
    SteeringPayload,
    SystemEventPayload,
    ThreadStatus,
    ToolCallPayload,
    Turn,
    UserMessagePayload,
)
from operant.domain.workflow import WorkflowRun, WorkflowRunEvent, WorkflowRunStatus
from operant.persistence.security import SQLiteSecurityRepository
from operant.persistence.sqlite import (
    ActionOutcomeUnknownError,
    ConflictError,
    IdempotencyConflictError,
    NotFoundError,
    SessionRunLease,
    SQLiteStore,
    WorkflowExecutionLease,
)
from operant.protocol import canonical_action_hash, redact_public_data, redact_public_text
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
        self.security_repository = SQLiteSecurityRepository(store)
        self.policy_engine = PolicyEngine(balanced_policy_bundle())
        self.capability_broker = CapabilityBroker(self.security_repository)
        self.denial_remediator = DenialRemediator(self.security_repository)
        self._security_claims: dict[str, tuple[ActionRequest, PolicyEvaluation]] = {}
        self._authorized_claims: set[str] = set()

    def _capabilities_for(self, name: str, arguments: dict[str, Any]) -> tuple[Capability, ...]:
        if name == "apply_patch":
            return (Capability.WORKSPACE_WRITE,)
        category = self.tools.required_approval_category(name, arguments)
        if category == "privileged":
            return (Capability.POLICY_MODIFY, Capability.PROCESS_EXEC)
        if category == "network":
            return (Capability.NETWORK_EGRESS, Capability.PROCESS_EXEC)
        if category == "destructive":
            return (Capability.WORKSPACE_DELETE, Capability.PROCESS_EXEC_NO_NETWORK)
        if category == "git_write":
            argv = arguments.get("argv", [])
            if isinstance(argv, list) and len(argv) > 1 and argv[1] == "push":
                return (Capability.GIT_PUSH, Capability.NETWORK_EGRESS, Capability.PROCESS_EXEC)
            return (Capability.GIT_COMMIT, Capability.PROCESS_EXEC_NO_NETWORK)
        runner = self.tools.policy.command_execution_policy.runner.value
        if runner == "docker":
            return (Capability.PROCESS_EXEC_NO_NETWORK,)
        return (Capability.PROCESS_EXEC,)

    def _audit(
        self,
        action: ActionRequest,
        event_type: str,
        *,
        evaluation: PolicyEvaluation | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        self.security_repository.append_security_audit(
            SecurityAuditEvent(
                action_hash=action.action_hash,
                principal=action.principal,
                event_type=event_type,
                decision=None if evaluation is None else evaluation.decision,
                rule_ids=() if evaluation is None else evaluation.matched_rule_ids,
                detail={} if detail is None else dict(detail),
            )
        )

    def reserve_tool_action(
        self,
        *,
        tool_call_id: str,
        name: str,
        arguments: dict[str, Any],
    ) -> ToolActionClaim:
        capabilities = self._capabilities_for(name, arguments)
        action = ActionNormalizer().normalize(
            principal=f"agent:{self.agent_id}",
            tool=name,
            operation="execute",
            arguments=arguments,
            requested_capabilities=capabilities,
            idempotency_key=f"{self.scope}:{tool_call_id}",
            policy_version=self.policy_engine.bundle.version,
            workspace=self.tools.root,
            session_id=self.session_id,
            agent_instance_id=self.agent_id,
            sandbox_profile=self.tools.policy.command_execution_policy.runner.value,
            network_profile=("egress" if Capability.NETWORK_EGRESS in capabilities else "none"),
        )
        action = self.security_repository.record_security_action(action)
        evaluation = self.policy_engine.evaluate(action)
        self._audit(
            action,
            "policy.evaluated",
            evaluation=evaluation,
            detail={
                "reason_code": evaluation.reason_code,
                "risk_level": evaluation.risk_level.value,
                "hard_deny": evaluation.hard_deny,
            },
        )
        if evaluation.decision is PolicyDecision.DENY:
            remediation = self.denial_remediator.build(action, evaluation)
            self._audit(
                action,
                "policy.denied",
                evaluation=evaluation,
                detail={
                    "reason_code": remediation.reason_code,
                    "denial_signature": remediation.denial_signature,
                    "repeated_count": remediation.repeated_count,
                    "no_progress": remediation.no_progress,
                },
            )
            raise ToolError(
                f"security policy denied action: {evaluation.reason_code}; "
                f"no_progress={str(remediation.no_progress).lower()}"
            )
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
            claim = ToolActionClaim(receipt_id=receipt.id, action_hash=action_hash)
            self._security_claims[receipt.id] = (action, evaluation)
            return claim
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

    def approval_requirement(self, claim: ToolActionClaim) -> tuple[str, str] | None:
        bound = self._security_claims.get(claim.receipt_id)
        if bound is None:
            return None
        action, evaluation = bound
        if evaluation.decision is not PolicyDecision.ASK:
            return None
        category = (
            self.tools.required_approval_category(action.tool, action.normalized_arguments)
            or "security_policy"
        )
        return (
            category,
            self.tools.safe_action_summary(
                action.tool, action.normalized_arguments, category=category
            ),
        )

    def complete_tool_action(self, claim: ToolActionClaim, result: str) -> None:
        self.store.complete_tool_action(
            claim.receipt_id,
            action_hash=claim.action_hash,
            result_json=redact_public_text(result),
        )
        bound = self._security_claims.get(claim.receipt_id)
        if bound is not None:
            self._audit(bound[0], "action.completed", detail={"receipt_id": claim.receipt_id})

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
        bound = self._security_claims.get(claim.receipt_id)
        if bound is not None:
            self._audit(
                bound[0],
                "action.failed",
                detail={"receipt_id": claim.receipt_id, "error_code": error_code},
            )

    def request_approval(
        self,
        claim: ToolActionClaim,
        *,
        tool_call_id: str,
        category: str,
        detail: str,
    ) -> Mapping[str, Any]:
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

        bound = self._security_claims.get(claim.receipt_id)
        if bound is None or bound[1].decision is PolicyDecision.DENY:
            raise ToolError("security policy approval binding is unavailable")
        action, evaluation = bound
        allowed = evaluation
        if evaluation.decision is PolicyDecision.ASK:
            allowed = evaluation.model_copy(
                update={
                    "decision": PolicyDecision.ALLOW,
                    "reviewer_eligible": False,
                    "reason_code": "approval.allowed",
                    "explanation": "exact action was approved",
                }
            )
        self._security_claims[claim.receipt_id] = (action, allowed)
        self._audit(
            action,
            "approval.allowed",
            evaluation=allowed,
            detail={"approval_id": approval.id},
        )

    def verify_execution(self) -> None:
        if self.lease is None:
            return
        try:
            self.store.assert_session_run_lease(self.lease)
        except ConflictError as exc:
            raise ToolError("session run lease is expired, cancelled, or fenced") from exc

    def authorize_tool_action(self, claim: ToolActionClaim) -> None:
        if claim.receipt_id in self._authorized_claims:
            return
        bound = self._security_claims.get(claim.receipt_id)
        if bound is None:
            raise ToolError("security action binding is unavailable")
        action, evaluation = bound
        if evaluation.decision is not PolicyDecision.ALLOW:
            raise ToolError("security policy approval is required before execution")
        lease_ids: list[str] = []
        for capability in action.requested_capabilities:
            capability_lease = self.capability_broker.issue(
                action,
                evaluation,
                capability,
                issued_by="action_gateway",
                ttl_seconds=60,
            )
            self.capability_broker.consume(
                capability_lease.lease_id,
                action=action,
                capability=capability,
            )
            lease_ids.append(capability_lease.lease_id)
        self._authorized_claims.add(claim.receipt_id)
        self._audit(
            action,
            "capability.consumed",
            evaluation=evaluation,
            detail={"lease_ids": lease_ids, "receipt_id": claim.receipt_id},
        )


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
        artifact_capability_secret: bytes | None = None,
        physical_delete_enabled: bool = False,
        physical_delete_authorization: str | None = None,
    ) -> None:
        if session_lease_ttl_seconds <= 0:
            raise ValueError("session lease TTL must be positive")
        self.store = store
        self.provider = provider
        self.factory = AgentFactory(store)
        self.slash_commands = SlashCommandRegistry()
        self._cancellations: dict[str, asyncio.Event] = {}
        self._sidecar_cancellations: dict[str, asyncio.Event] = {}
        self._approval_futures: dict[tuple[str, str], asyncio.Future[bool]] = {}
        self._approval_details: dict[tuple[str, str], dict[str, str]] = {}
        self._session_run_leases: dict[str, SessionRunLease] = {}
        self._workflow_execution_leases: dict[str, WorkflowExecutionLease] = {}
        # Workspace registration stores the canonical path, but intentionally
        # does not add filesystem identity columns or a new Migration. Keep a
        # process-local identity fence for directory browsing so a root
        # replacement during this Core lifetime fails closed.
        self._workspace_file_identities: dict[str, tuple[int, int]] = {}
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
        self._artifact_capabilities = ArtifactCapabilityAuthority(
            artifact_capability_secret or secrets.token_bytes(32)
        )
        self._physical_delete_enabled = physical_delete_enabled
        self._physical_delete_authorization = physical_delete_authorization
        if physical_delete_enabled and (
            physical_delete_authorization is None or len(physical_delete_authorization) < 32
        ):
            raise ValueError(
                "enabled physical Artifact deletion requires a 32-character authorization"
            )

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
        thread_id: str | None = None,
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
            thread_id=thread_id,
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

    # Phase 1D explicit commands.  Registration only describes routing; it
    # never grants a tool or workspace capability.

    def list_slash_commands(self) -> tuple[SlashCommandDefinition, ...]:
        return self.slash_commands.list_commands()

    def resolve_slash_command(
        self,
        text: str,
        *,
        registry_version: str | None = None,
    ) -> SlashCommandResolution:
        return self.slash_commands.resolve(text, registry_version=registry_version)

    def initialize_workspace(self, workspace: str | Path) -> tuple[WorkspaceInitialization, bool]:
        candidate = Path(workspace)
        if not candidate.is_absolute():
            raise ValueError("workspace must be an absolute path")
        resolved = candidate.resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError("workspace must be a directory")
        readable = os.access(resolved, os.R_OK)
        writable = os.access(resolved, os.W_OK)
        if not readable:
            raise PermissionError("workspace is not readable")
        workspace_ref = str(resolved)
        initialization = WorkspaceInitialization(
            workspace_ref=workspace_ref,
            workspace_hash=hashlib.sha256(workspace_ref.encode("utf-8")).hexdigest(),
            readable=readable,
            writable=writable,
        )
        return self.store.register_workspace(initialization)

    # Phase 1E read-only client projections. These methods aggregate existing
    # durable facts and never create Project entities or infer legacy links.

    def list_project_projections(
        self,
        *,
        after_cursor: int | None = None,
        limit: int = 100,
    ) -> list[ProjectProjection]:
        return list_project_projections(
            self.store,
            after_cursor=after_cursor,
            limit=limit,
        )

    def list_workspace_files(
        self,
        workspace_id: str,
        *,
        path: str = ".",
        limit: int = 100,
        page_token: str | None = None,
        snapshot: str | None = None,
        after_name: str | None = None,
    ) -> WorkspaceFilesPage:
        initialization = get_workspace_initialization(self.store, workspace_id)
        page, identity = list_workspace_files(
            initialization,
            path=path,
            limit=limit,
            page_token=page_token,
            snapshot=snapshot,
            after_name=after_name,
            expected_identity=self._workspace_file_identities.get(workspace_id),
        )
        self._workspace_file_identities.setdefault(workspace_id, identity)
        return page

    def append_context_baseline(
        self,
        *,
        session_id: str,
        thread_id: str,
        operation: ContextBaselineOperation,
        agent_id: str | None = None,
    ) -> ContextBaseline:
        self.get_session(session_id)
        self.get_thread(thread_id)
        latest_cursor = self._latest_thread_item_cursor(thread_id)
        previous = self.store.get_active_context_baseline(session_id, thread_id)
        compaction = None
        if operation is ContextBaselineOperation.COMPACT:
            if agent_id is None:
                raise ValueError("compact context requires an agent_id")
            agent = self.store.get_agent(agent_id)
            if agent.session_id != session_id:
                raise PermissionError("agent belongs to a different Session")
            compaction = self._build_explicit_thread_compaction(
                session_id=session_id,
                agent_id=agent_id,
                thread_id=thread_id,
                item_cursor_end=latest_cursor,
            )
        baseline = ContextBaseline(
            session_id=session_id,
            thread_id=thread_id,
            item_cursor_end=latest_cursor,
            operation=operation,
            compaction_id=None if compaction is None else compaction.id,
            previous_baseline_id=None if previous is None else previous.id,
        )
        return self.store.append_context_baseline(baseline, compaction=compaction)

    def get_active_context_baseline(
        self,
        session_id: str,
        thread_id: str,
    ) -> ContextBaseline | None:
        self.get_session(session_id)
        self.get_thread(thread_id)
        return self.store.get_active_context_baseline(session_id, thread_id)

    def list_context_baselines(
        self,
        session_id: str,
        thread_id: str,
        *,
        after_cursor: int | None = None,
        limit: int = 100,
    ) -> list[ContextBaseline]:
        self.get_session(session_id)
        self.get_thread(thread_id)
        return self.store.list_context_baselines(
            session_id,
            thread_id,
            after_cursor=after_cursor,
            limit=limit,
        )

    def _latest_thread_item_cursor(self, thread_id: str) -> int:
        cursor: int | None = None
        while True:
            page = self.store.list_items(thread_id, after_cursor=cursor, limit=1000)
            if not page:
                return 0 if cursor is None else cursor
            assert page[-1].cursor is not None
            cursor = page[-1].cursor
            if len(page) < 1000:
                return cursor

    def _build_explicit_thread_compaction(
        self,
        *,
        session_id: str,
        agent_id: str,
        thread_id: str,
        item_cursor_end: int,
    ) -> Compaction:
        start_after = 0
        baseline_cursor: int | None = None
        while True:
            baseline_page = self.store.list_context_baselines(
                session_id,
                thread_id,
                after_cursor=baseline_cursor,
                limit=1000,
            )
            for baseline in baseline_page:
                if baseline.operation is ContextBaselineOperation.CLEAR:
                    start_after = baseline.item_cursor_end
            if len(baseline_page) < 1000:
                break
            assert baseline_page[-1].cursor is not None
            baseline_cursor = baseline_page[-1].cursor

        items: list[Item] = []
        cursor: int | None = start_after
        while True:
            item_page = self.store.list_items(thread_id, after_cursor=cursor, limit=1000)
            selected = [
                item
                for item in item_page
                if item.cursor is not None and item.cursor <= item_cursor_end
            ]
            items.extend(selected)
            if len(item_page) < 1000 or not item_page or item_page[-1].cursor == item_cursor_end:
                break
            assert item_page[-1].cursor is not None
            if item_page[-1].cursor > item_cursor_end:
                break
            cursor = item_page[-1].cursor
        if not items:
            raise ConflictError("there are no active Thread Items to compact")

        refs: list[ContextSourceRef] = []
        coverage = hashlib.sha256()
        active_goal = ""
        for item in items:
            assert item.cursor is not None
            item_hash = hashlib.sha256(
                item.model_copy(update={"cursor": None}).model_dump_json().encode("utf-8")
            ).hexdigest()
            ref = ContextSourceRef(
                source_type=ContextSourceType.ITEM,
                source_id=item.id,
                cursor=item.cursor,
                content_hash=item_hash,
            )
            refs.append(ref)
            coverage.update(
                json.dumps(
                    {
                        "id": ref.source_id,
                        "cursor": ref.cursor,
                        "content_hash": ref.content_hash,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            coverage.update(b"\n")
            if item.item_type.value in {"user_message", "steering"}:
                text = item.payload.model_dump(mode="json").get("text")
                if isinstance(text, str):
                    active_goal = redact_public_text(text, max_chars=500)
        summary = CompactionSummary(
            active_goal=active_goal,
            completed_steps=(f"Compacted {len(refs)} committed Canonical Thread Items.",),
            next_actions=("Continue from Items appended after the explicit baseline.",),
        )
        compaction = Compaction(
            session_id=session_id,
            agent_id=agent_id,
            thread_id=thread_id,
            source_type=CompactionSourceType.THREAD_ITEMS,
            source_cursor_start=refs[0].cursor or 1,
            source_cursor_end=refs[-1].cursor or 1,
            source_snapshot_hash=coverage.hexdigest(),
            summary=summary,
            content_hash=hashlib.sha256(summary.model_dump_json().encode("utf-8")).hexdigest(),
            covered_item_refs=tuple(refs),
        )
        return compaction.model_copy(update={"id": deterministic_compaction_id(compaction)})

    def append_phase1d_audit(
        self,
        *,
        command_execution_id: str,
        command_kind: SlashCommandKind,
        event_type: str,
        resource_type: str | None = None,
        resource_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> Phase1DCommandAuditEvent:
        safe_detail = redact_public_data(detail or {}, max_chars=2_000)
        if not isinstance(safe_detail, dict):
            safe_detail = {}
        return self.store.append_phase1d_command_audit_event(
            Phase1DCommandAuditEvent(
                command_execution_id=command_execution_id,
                command_kind=command_kind,
                event_type=event_type,
                resource_type=resource_type,
                resource_id=resource_id,
                detail=safe_detail,
            )
        )

    def list_phase1d_audit_events(
        self,
        command_execution_id: str,
        *,
        after_cursor: int | None = None,
        limit: int = 100,
    ) -> list[Phase1DCommandAuditEvent]:
        return self.store.list_phase1d_command_audit_events(
            command_execution_id,
            after_cursor=after_cursor,
            limit=limit,
        )

    async def run_review(
        self,
        *,
        command_execution_id: str,
        reviewer_role_id: str,
        workspace: str | Path,
        scope: str,
        thread_id: str | None = None,
    ) -> AsyncIterator[Phase1DCommandAuditEvent]:
        workspace_path = Path(workspace)
        if not workspace_path.is_absolute():
            raise ValueError("workspace must be absolute")
        workspace_ref = str(workspace_path.resolve(strict=True))
        if not Path(workspace_ref).is_dir():
            raise ValueError("workspace must be a directory")
        if thread_id is not None:
            thread = self.get_thread(thread_id)
            if (
                thread.workspace_ref is None
                or str(Path(thread.workspace_ref).resolve()) != workspace_ref
            ):
                raise PermissionError("Review Thread workspace binding does not match")

        expected_policy = ToolPolicy(allowed_tools=("read_file", "search_files", "git_diff"))
        reviewer_role = self.get_role(reviewer_role_id)
        if reviewer_role.tool_policy != expected_policy:
            raise PermissionError("Reviewer role is not strictly read-only")
        review_session = self.create_session(reviewer_role_id)
        if (
            review_session.role_snapshot.role_id != reviewer_role.id
            or review_session.role_snapshot.role_version != reviewer_role.version
            or review_session.role_snapshot.tool_policy != expected_policy
        ):
            raise PermissionError("Reviewer role changed while the Session was created")
        review_run = self.store.create_review_run(
            ReviewRun(
                session_id=review_session.id,
                thread_id=thread_id,
                workspace_ref=workspace_ref,
                scope=redact_public_text(scope, max_chars=2_000),
            )
        )
        result_text: str | None = None
        failure_code: str | None = None
        terminal_recorded = False

        def record_failure(error_code: str) -> Phase1DCommandAuditEvent:
            failed_run = self.store.update_review_run(
                review_run.id,
                status=ReviewRunStatus.FAILED,
                error_code=error_code,
            )
            return self.append_phase1d_audit(
                command_execution_id=command_execution_id,
                command_kind=SlashCommandKind.REVIEW,
                event_type="review.failed",
                resource_type="review",
                resource_id=review_run.id,
                detail={
                    "review_run_id": review_run.id,
                    "error_code": failed_run.error_code,
                },
            )

        try:
            started = self.append_phase1d_audit(
                command_execution_id=command_execution_id,
                command_kind=SlashCommandKind.REVIEW,
                event_type="review.started",
                resource_type="review",
                resource_id=review_run.id,
                detail={"review_run_id": review_run.id, "session_id": review_session.id},
            )
            yield started

            async for event in self.run_session(
                review_session.id,
                user_message=(
                    "Perform a strict read-only code review. Use only read_file, "
                    "search_files, and git_diff. Do not request command execution or file "
                    f"changes. Review scope: {redact_public_text(scope, max_chars=2_000)}"
                ),
                workspace=workspace_ref,
                thread_id=thread_id,
            ):
                if event.event_type == "agent.completed":
                    content = event.payload.get("content")
                    if isinstance(content, str):
                        result_text = redact_public_text(content, max_chars=100_000)
                elif event.event_type in {
                    "agent.failed",
                    "agent.cancelled",
                    "agent.timed_out",
                    "agent.stream_error",
                    "budget.exhausted",
                }:
                    failure_code = event.event_type.replace(".", "_")[:200]
            if result_text is None:
                failure_code = failure_code or "review_result_missing"
            if failure_code is not None:
                failed_event = record_failure(failure_code)
                terminal_recorded = True
                yield failed_event
                return
            assert result_text is not None
            source_refs = [
                ArtifactSourceRef(
                    source_type=ArtifactSourceType.SESSION,
                    source_id=review_session.id,
                )
            ]
            if thread_id is not None:
                source_refs.append(
                    ArtifactSourceRef(source_type=ArtifactSourceType.THREAD, source_id=thread_id)
                )
            artifact, _created = self.create_artifact(
                content=result_text.encode("utf-8"),
                media_type="text/markdown; profile=operant-review",
                sensitivity=ArtifactSensitivity.SENSITIVE,
                source_refs=source_refs,
                retention_policy_ref="review-result",
            )
            completed_run = self.store.update_review_run(
                review_run.id,
                status=ReviewRunStatus.COMPLETED,
                artifact_id=artifact.id,
            )
            completed_event = self.append_phase1d_audit(
                command_execution_id=command_execution_id,
                command_kind=SlashCommandKind.REVIEW,
                event_type="review.completed",
                resource_type="review",
                resource_id=review_run.id,
                detail={
                    "review_run_id": review_run.id,
                    "artifact_id": completed_run.artifact_id,
                },
            )
            terminal_recorded = True
            yield completed_event
        except asyncio.CancelledError:
            if not terminal_recorded:
                record_failure("stream_cancelled")
                terminal_recorded = True
            raise
        except Exception as exc:
            error_code = self._safe_execution_error_code(exc, prefix="review")
            failed_event = record_failure(error_code)
            terminal_recorded = True
            yield failed_event
        finally:
            if not terminal_recorded:
                with suppress(ConflictError):
                    record_failure("stream_closed")

    async def run_btw_sidecar(
        self,
        *,
        session_id: str,
        thread_id: str,
        workspace: str | Path,
        prompt: str,
    ) -> AsyncIterator[BTWSidecarEvent]:
        session = self.get_session(session_id)
        thread = self.get_thread(thread_id)
        workspace_path = Path(workspace)
        if not workspace_path.is_absolute():
            raise ValueError("workspace must be absolute")
        workspace_ref = str(workspace_path.resolve(strict=True))
        if (
            thread.workspace_ref is None
            or str(Path(thread.workspace_ref).resolve()) != workspace_ref
        ):
            raise PermissionError("BTW Thread workspace binding does not match")
        safe_prompt = redact_public_text(prompt, max_chars=100_000)
        agent = self.factory.create_agent(session.id)
        self.store.update_agent_status(agent.id, AgentStatus.RUNNING)
        source_cursor = self._latest_thread_item_cursor(thread.id)
        run = self.store.create_btw_sidecar_run(
            BTWSidecarRun(
                session_id=session.id,
                agent_id=agent.id,
                thread_id=thread.id,
                workspace_ref=workspace_ref,
                source_item_cursor_end=source_cursor,
                prompt=safe_prompt,
                prompt_hash=hashlib.sha256(safe_prompt.encode("utf-8")).hexdigest(),
            )
        )
        started = self.store.append_btw_sidecar_event(
            BTWSidecarEvent(
                sidecar_run_id=run.id,
                event_type="btw.started",
                payload={"sidecar_run_id": run.id, "status": run.status.value},
            )
        )
        cancellation = asyncio.Event()
        self._sidecar_cancellations[run.id] = cancellation
        iterator = None
        next_event: asyncio.Future[RuntimeEvent] | None = None
        cancelled: asyncio.Task[bool] | None = None
        terminal_recorded = False
        try:
            yield started

            sidecar_snapshot = session.role_snapshot.model_copy(
                update={
                    "system_prompt": (
                        session.role_snapshot.system_prompt
                        + "\nThis is an isolated BTW Sidecar call. Answer the question only. "
                        "You have no tools and must not claim to modify the main Thread."
                    ),
                    "tool_policy": ToolPolicy(),
                }
            )
            composer = PersistentContextComposer(
                store=self.store,
                session=session,
                agent_id=agent.id,
                workspace=workspace_ref,
                thread_id=thread.id,
                references=(),
                memory_resolver=lambda memory_id: self.get_memory(
                    memory_id,
                    snapshot=sidecar_snapshot,
                    session_id=session.id,
                    project_scope=workspace_ref,
                ),
                artifact_reader=self._read_artifact_for_context,
                artifact_writer=lambda content: self._write_tool_result_artifact(content=content),
                thread_item_cursor_end=source_cursor,
            )
            # Use the same AgentLoop budget guards as a normal Agent run while
            # keeping the Sidecar outside the main Session lease and Event log.
            # The empty ToolPolicy makes the advertised tool set empty.  If a
            # provider nevertheless returns a tool call, iteration stops at
            # ``tool.started`` before AgentLoop can execute it.
            loop = AgentLoop(
                self.provider,
                WorkspaceTools(workspace_ref, policy=ToolPolicy()),
                context_composer=composer,
            )
            iterator = loop.run(
                snapshot=sidecar_snapshot,
                user_message=safe_prompt,
            )
            deadline = asyncio.get_running_loop().time() + sidecar_snapshot.budget.timeout_seconds
            completed_payload: dict[str, Any] | None = None
            failure_code: str | None = None
            failure_detail: dict[str, Any] | None = None
            failure_status = AgentStatus.FAILED

            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    failure_code = "budget_time_limit_reached"
                    failure_detail = {
                        "budget": {
                            "kind": "time",
                            "reason": "limit_reached",
                            "limit": sidecar_snapshot.budget.timeout_seconds,
                            "observed": None,
                            "usage_state": "unknown",
                        }
                    }
                    failure_status = AgentStatus.TIMED_OUT
                    break
                next_event = asyncio.ensure_future(anext(iterator))
                cancelled = asyncio.create_task(cancellation.wait())
                done, pending = await asyncio.wait(
                    {next_event, cancelled},
                    timeout=remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for waiter in pending:
                    waiter.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                if not done:
                    failure_code = "budget_time_limit_reached"
                    failure_detail = {
                        "budget": {
                            "kind": "time",
                            "reason": "limit_reached",
                            "limit": sidecar_snapshot.budget.timeout_seconds,
                            "observed": None,
                            "usage_state": "unknown",
                        }
                    }
                    failure_status = AgentStatus.TIMED_OUT
                    break
                if cancelled in done and cancelled.result():
                    next_event.cancel()
                    await asyncio.gather(next_event, return_exceptions=True)
                    failure_code = "cancelled"
                    failure_status = AgentStatus.CANCELLED
                    break
                try:
                    runtime_event = next_event.result()
                except StopAsyncIteration:
                    failure_code = "runtime_result_missing"
                    break
                if runtime_event.event_type == "model.completed":
                    completed_payload = runtime_event.payload
                    self._record_runtime_cache_observation(session, runtime_event)
                elif runtime_event.event_type == "budget.exhausted":
                    kind = str(runtime_event.payload.get("kind", "unknown"))
                    reason = str(runtime_event.payload.get("reason", "unknown"))
                    if kind not in {"output_tokens", "cost", "tool_calls"}:
                        kind = "unknown"
                    if reason not in {
                        "limit_reached",
                        "usage_unknown",
                        "pricing_unknown",
                    }:
                        reason = "unknown"
                    failure_code = f"budget_{kind}_{reason}"
                    safe_budget = redact_public_data(runtime_event.payload, max_chars=500)
                    failure_detail = {
                        "budget": safe_budget if isinstance(safe_budget, dict) else {}
                    }
                    break
                elif runtime_event.event_type == "tool.started":
                    failure_code = "provider_tool_call_rejected"
                    break
                elif runtime_event.event_type == "agent.completed":
                    if completed_payload is None:
                        failure_code = "runtime_result_missing"
                        break
                    context_revision_id = completed_payload.get("context_revision_id")
                    if not isinstance(context_revision_id, str):
                        failure_code = "context_revision_missing"
                        break
                    response = redact_public_text(
                        str(runtime_event.payload.get("content", "")),
                        max_chars=100_000,
                    )
                    response = response.replace(workspace_ref, "[WORKSPACE]")
                    completed_run = self.store.update_btw_sidecar_run(
                        run.id,
                        status=BTWSidecarStatus.COMPLETED,
                        response=response,
                        response_hash=hashlib.sha256(response.encode("utf-8")).hexdigest(),
                        context_revision_id=context_revision_id,
                    )
                    self.store.update_agent_status(agent.id, AgentStatus.COMPLETED)
                    terminal_recorded = True
                    usage = completed_payload.get("usage")
                    yield self.store.append_btw_sidecar_event(
                        BTWSidecarEvent(
                            sidecar_run_id=run.id,
                            event_type="btw.model_completed",
                            payload={
                                "sidecar_run_id": run.id,
                                "status": completed_run.status.value,
                                "context_revision_id": completed_run.context_revision_id,
                                "usage": usage,
                            },
                        )
                    )
                    return
                elif runtime_event.event_type in {
                    "agent.max_turns",
                    "agent.no_progress",
                }:
                    failure_code = runtime_event.event_type.replace(".", "_")
                    break

            assert failure_code is not None
            failed = self._fail_btw_sidecar(
                run.id,
                agent.id,
                error_code=failure_code,
                detail=failure_detail,
                agent_status=failure_status,
            )
            terminal_recorded = True
            yield failed
        except asyncio.CancelledError:
            if not terminal_recorded:
                self._fail_btw_sidecar(
                    run.id,
                    agent.id,
                    error_code="stream_cancelled",
                    agent_status=AgentStatus.CANCELLED,
                )
                terminal_recorded = True
            raise
        except Exception as exc:
            error_code = (
                "provider_tool_call_rejected"
                if isinstance(exc, PermissionError) and "tool call" in str(exc)
                else self._safe_execution_error_code(exc, prefix="btw")
            )
            failed = self._fail_btw_sidecar(run.id, agent.id, error_code=error_code)
            terminal_recorded = True
            yield failed
        finally:
            cleanup_waiters: list[asyncio.Future[Any]] = []
            if next_event is not None:
                cleanup_waiters.append(next_event)
            if cancelled is not None:
                cleanup_waiters.append(cancelled)
            for cleanup_waiter in cleanup_waiters:
                if not cleanup_waiter.done():
                    cleanup_waiter.cancel()
            await asyncio.gather(
                *cleanup_waiters,
                return_exceptions=True,
            )
            if iterator is not None:
                await iterator.aclose()
            if self._sidecar_cancellations.get(run.id) is cancellation:
                self._sidecar_cancellations.pop(run.id, None)
            if not terminal_recorded:
                with suppress(ConflictError):
                    self._fail_btw_sidecar(
                        run.id,
                        agent.id,
                        error_code="stream_closed",
                        agent_status=AgentStatus.CANCELLED,
                    )

    def _fail_btw_sidecar(
        self,
        run_id: str,
        agent_id: str,
        *,
        error_code: str,
        detail: dict[str, Any] | None = None,
        agent_status: AgentStatus = AgentStatus.FAILED,
    ) -> BTWSidecarEvent:
        self.store.update_btw_sidecar_run(
            run_id,
            status=BTWSidecarStatus.FAILED,
            error_code=error_code,
        )
        self.store.update_agent_status(agent_id, agent_status)
        payload: dict[str, Any] = {"sidecar_run_id": run_id, "error_code": error_code}
        if detail:
            safe_detail = redact_public_data(detail, max_chars=500)
            if isinstance(safe_detail, dict):
                payload.update(safe_detail)
        return self.store.append_btw_sidecar_event(
            BTWSidecarEvent(
                sidecar_run_id=run_id,
                event_type="btw.failed",
                payload=payload,
            )
        )

    def cancel_btw_sidecar(self, run_id: str) -> bool:
        run = self.get_btw_sidecar_run(run_id)
        if run.status is not BTWSidecarStatus.RUNNING:
            return False
        cancellation = self._sidecar_cancellations.get(run_id)
        if cancellation is None:
            return False
        cancellation.set()
        return True

    def get_btw_sidecar_run(self, run_id: str) -> BTWSidecarRun:
        return self.store.get_btw_sidecar_run(run_id)

    def list_btw_sidecar_events(
        self,
        run_id: str,
        *,
        after_cursor: int | None = None,
        limit: int = 100,
    ) -> list[BTWSidecarEvent]:
        return self.store.list_btw_sidecar_events(
            run_id,
            after_cursor=after_cursor,
            limit=limit,
        )

    def promote_btw_sidecar(self, run_id: str) -> tuple[BTWSidecarRun, Turn, Item]:
        run = self.get_btw_sidecar_run(run_id)
        if (
            run.status
            not in {
                BTWSidecarStatus.COMPLETED,
                BTWSidecarStatus.PROMOTED,
            }
            or run.response is None
        ):
            raise ConflictError("only a completed BTW Sidecar can be promoted")
        turn = Turn(thread_id=run.thread_id)
        item = Item(
            thread_id=run.thread_id,
            turn_id=turn.id,
            payload=SteeringPayload(text=run.response, mode="steer"),
        )
        promoted_run, promoted_turn, promoted_item, _created = self.store.promote_btw_sidecar(
            run.id,
            turn=turn,
            steering_item=item,
        )
        return promoted_run, promoted_turn, promoted_item

    @staticmethod
    def _safe_execution_error_code(exc: Exception, *, prefix: str) -> str:
        if isinstance(exc, (ValueError, ValidationError)):
            category = "validation_error"
        elif isinstance(exc, PermissionError):
            category = "permission_denied"
        elif isinstance(exc, OSError):
            category = "io_error"
        else:
            category = "execution_failed"
        return f"{prefix}_{category}"[:200]

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
        blob_store = self._artifact_blob_store()
        with blob_store.mutation_guard():
            blob = blob_store.put_bytes(content)
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

    def issue_artifact_capability(
        self,
        artifact_id: str,
        *,
        operation: str,
        access_level: ArtifactAccessLevel,
        ttl_seconds: int = 300,
        workspace_root: str | Path | None = None,
        relative_path: str | None = None,
    ) -> str:
        """Trusted embedding hook; the unauthenticated HTTP API cannot issue grants."""

        artifact = self.store.get_artifact(artifact_id)
        ranks = {
            ArtifactAccessLevel.NORMAL: 0,
            ArtifactAccessLevel.SENSITIVE: 1,
            ArtifactAccessLevel.RESTRICTED: 2,
        }
        required = ArtifactAccessLevel(artifact.sensitivity.value)
        if ranks[access_level] < ranks[required]:
            raise PermissionError("requested Artifact capability clearance is insufficient")
        export_scope_hash: str | None = None
        if operation == "export":
            if workspace_root is None or relative_path is None:
                raise ValueError("export capability requires an exact destination scope")
            export_scope_hash = self._export_scope_hash(workspace_root, relative_path)
        elif workspace_root is not None or relative_path is not None:
            raise ValueError("destination scope is valid only for export capabilities")
        return self._artifact_capabilities.issue(
            artifact_id=artifact_id,
            operation=operation,
            access_level=access_level,
            ttl_seconds=ttl_seconds,
            operation_scope_hash=export_scope_hash,
        )

    def read_artifact_text(self, artifact_id: str, *, capability: str) -> dict[str, Any]:
        artifact, content = self._authorized_artifact_content(
            artifact_id,
            operation="read",
            capability=capability,
        )
        if not (
            artifact.media_type.startswith("text/")
            or artifact.media_type in {"application/json", "application/xml"}
        ):
            raise ValueError("Artifact is not a supported textual media type")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Artifact text is not valid UTF-8") from exc
        return {
            "artifact": artifact.model_dump(mode="json"),
            "content_text": redact_public_text(text),
            "content_redacted": True,
        }

    def download_artifact(self, artifact_id: str, *, capability: str) -> tuple[Artifact, bytes]:
        return self._authorized_artifact_content(
            artifact_id,
            operation="download",
            capability=capability,
        )

    def export_artifact(
        self,
        artifact_id: str,
        *,
        capability: str,
        workspace_root: str | Path,
        relative_path: str,
    ) -> dict[str, Any]:
        scope_hash = self._export_scope_hash(workspace_root, relative_path)
        artifact, content = self._authorized_artifact_content(
            artifact_id,
            operation="export",
            capability=capability,
            operation_scope_hash=scope_hash,
        )
        export_artifact_bytes(
            content,
            workspace_root=workspace_root,
            relative_path=relative_path,
            expected_scope_hash=scope_hash,
        )
        return {
            "artifact_id": artifact.id,
            "content_hash": artifact.content_hash,
            "size_bytes": artifact.size_bytes,
            "exported": True,
        }

    def _authorized_artifact_content(
        self,
        artifact_id: str,
        *,
        operation: str,
        capability: str,
        operation_scope_hash: str | None = None,
    ) -> tuple[Artifact, bytes]:
        artifact = self.store.get_artifact(artifact_id)
        state = self.store.get_artifact_retention_state(artifact_id)
        if state.lifecycle is RetentionLifecycle.DELETED:
            raise ArtifactContentDeletedError("Artifact content was explicitly deleted")
        self._artifact_capabilities.verify(
            capability,
            artifact_id=artifact_id,
            operation=operation,
            sensitivity=artifact.sensitivity,
            operation_scope_hash=operation_scope_hash,
        )
        content = self._artifact_blob_store().read(
            artifact.content_hash,
            artifact.size_bytes,
        )
        return artifact, content

    @staticmethod
    def _export_scope_hash(workspace_root: str | Path, relative_path: str) -> str:
        return artifact_export_scope_fingerprint(
            workspace_root=workspace_root,
            relative_path=relative_path,
        )

    def create_retention_policy(self, policy: RetentionPolicy) -> RetentionPolicy:
        return self.store.create_retention_policy(policy)

    def get_artifact_retention_state(self, artifact_id: str) -> ArtifactRetentionState:
        return self.store.get_artifact_retention_state(artifact_id)

    def set_artifact_pin(
        self,
        artifact_id: str,
        *,
        pinned: bool,
        capability: str,
    ) -> ArtifactRetentionState:
        self._authorize_retention_mutation(artifact_id, "retention_pin", capability)
        current = self.store.get_artifact_retention_state(artifact_id)
        if current.lifecycle not in {RetentionLifecycle.ACTIVE, RetentionLifecycle.ARCHIVED}:
            raise ConflictError("only active or archived Artifacts may change Pin state")
        updated = current.model_copy(
            update={"pinned": pinned, "updated_at": self._next_retention_time(current)}
        )
        return self._save_retention_state(
            current,
            updated,
            event_type="retention.pin_changed",
            action="artifact.pin",
            fields={"pinned": pinned},
        )

    def archive_artifact(self, artifact_id: str, *, capability: str) -> ArtifactRetentionState:
        self._authorize_retention_mutation(artifact_id, "retention_archive", capability)
        current = self.store.get_artifact_retention_state(artifact_id)
        if current.lifecycle is RetentionLifecycle.ARCHIVED:
            return current
        if current.lifecycle is not RetentionLifecycle.ACTIVE:
            raise ConflictError("only an active Artifact may be archived")
        updated = current.model_copy(
            update={
                "lifecycle": RetentionLifecycle.ARCHIVED,
                "updated_at": self._next_retention_time(current),
            }
        )
        return self._save_retention_state(
            current,
            updated,
            event_type="retention.archived",
            action="artifact.archive",
        )

    def schedule_artifact_deletion(
        self,
        artifact_id: str,
        *,
        capability: str,
    ) -> ArtifactRetentionState:
        self._authorize_retention_mutation(artifact_id, "retention_schedule", capability)
        current = self.store.get_artifact_retention_state(artifact_id)
        if current.lifecycle is RetentionLifecycle.DELETION_SCHEDULED:
            return current
        if current.lifecycle not in {RetentionLifecycle.ACTIVE, RetentionLifecycle.ARCHIVED}:
            raise ConflictError("Artifact cannot be scheduled from its current lifecycle")
        if current.pinned:
            raise ConflictError("pinned Artifact cannot be scheduled for deletion")
        policy = self.store.get_retention_policy(current.policy_ref)
        now = self._next_retention_time(current)
        updated = current.model_copy(
            update={
                "lifecycle": RetentionLifecycle.DELETION_SCHEDULED,
                "scheduled_deletion_at": now + timedelta(seconds=policy.grace_period_seconds),
                "updated_at": now,
            }
        )
        return self._save_retention_state(
            current,
            updated,
            event_type="retention.deletion_scheduled",
            action="artifact.schedule_deletion",
        )

    def trash_artifact(self, artifact_id: str, *, capability: str) -> ArtifactRetentionState:
        self._authorize_retention_mutation(artifact_id, "retention_trash", capability)
        current = self.store.get_artifact_retention_state(artifact_id)
        if current.lifecycle is not RetentionLifecycle.DELETION_SCHEDULED:
            raise ConflictError("Artifact must be scheduled before recoverable trash")
        now = self._next_retention_time(current)
        assert current.scheduled_deletion_at is not None
        if now < current.scheduled_deletion_at:
            raise ConflictError("Artifact deletion grace period has not elapsed")
        if self.store.artifact_deletion_blockers(artifact_id):
            raise ConflictError("Artifact has protected retention references")
        updated = current.model_copy(
            update={
                "lifecycle": RetentionLifecycle.TRASHED,
                "trashed_at": now,
                "updated_at": now,
            }
        )
        return self._save_retention_state(
            current,
            updated,
            event_type="retention.trashed",
            action="artifact.trash",
        )

    def restore_artifact(self, artifact_id: str, *, capability: str) -> ArtifactRetentionState:
        self._authorize_retention_mutation(artifact_id, "retention_restore", capability)
        current = self.store.get_artifact_retention_state(artifact_id)
        if current.lifecycle not in {
            RetentionLifecycle.DELETION_SCHEDULED,
            RetentionLifecycle.TRASHED,
        }:
            raise ConflictError("only scheduled or trashed Artifacts may be restored")
        updated = current.model_copy(
            update={
                "lifecycle": RetentionLifecycle.ACTIVE,
                "scheduled_deletion_at": None,
                "trashed_at": None,
                "deleted_at": None,
                "updated_at": self._next_retention_time(current),
            }
        )
        return self._save_retention_state(
            current,
            updated,
            event_type="retention.restored",
            action="artifact.restore",
        )

    def _save_retention_state(
        self,
        current: ArtifactRetentionState,
        updated: ArtifactRetentionState,
        *,
        event_type: str,
        action: str,
        fields: dict[str, Any] | None = None,
    ) -> ArtifactRetentionState:
        action_payload = {"action": action, "artifact_id": current.artifact_id}
        action_payload.update(fields or {})
        return self.store.update_artifact_retention_state(
            updated,
            expected_updated_at=current.updated_at,
            event_type=event_type,
            action_hash=canonical_action_hash(action_payload),
        )

    def _authorize_retention_mutation(
        self,
        artifact_id: str,
        operation: str,
        capability: str,
    ) -> None:
        artifact = self.store.get_artifact(artifact_id)
        self._artifact_capabilities.verify(
            capability,
            artifact_id=artifact_id,
            operation=operation,
            sensitivity=artifact.sensitivity,
        )

    @staticmethod
    def _next_retention_time(current: ArtifactRetentionState) -> datetime:
        now = utc_now()
        return current.updated_at + timedelta(microseconds=1) if now <= current.updated_at else now

    def audit_artifacts(self) -> ArtifactAuditReport:
        """Compare verified blobs with SQLite references without writing either side."""

        references = self.store.list_artifact_blob_references()
        inventory = self._artifact_inventory_read_only()
        by_hash = {
            record.content_hash: record
            for record in inventory
            if record.content_hash is not None and not record.unsafe
        }
        findings: list[ArtifactAuditFinding] = []

        def add_finding(
            finding_type: Literal[
                "orphan_blob",
                "missing_blob",
                "corrupt_blob",
                "unsafe_entry",
                "purged_blob_present",
            ],
            *,
            content_hash: str | None = None,
            artifact_id: str | None = None,
            expected_size: int | None = None,
            observed_size: int | None = None,
            repairable: bool = False,
            occurrence: int = 0,
        ) -> None:
            fact = {
                "finding_type": finding_type,
                "content_hash": content_hash,
                "artifact_id": artifact_id,
                "expected_size_bytes": expected_size,
                "observed_size_bytes": observed_size,
                "occurrence": occurrence,
            }
            findings.append(
                ArtifactAuditFinding(
                    finding_type=finding_type,
                    content_hash=content_hash,
                    artifact_id=artifact_id,
                    expected_size_bytes=expected_size,
                    observed_size_bytes=observed_size,
                    finding_hash=canonical_action_hash(fact),
                    repairable=repairable,
                )
            )

        for ordinal, record in enumerate(inventory):
            if record.unsafe:
                add_finding("unsafe_entry", occurrence=ordinal)
                continue
            assert record.content_hash is not None and record.size_bytes is not None
            reference = references.get(record.content_hash)
            if reference is None:
                try:
                    self._artifact_blob_store().verify(record.content_hash, record.size_bytes)
                except (ArtifactCorruptionError, ArtifactSecurityError):
                    add_finding(
                        "corrupt_blob",
                        content_hash=record.content_hash,
                        observed_size=record.size_bytes,
                    )
                else:
                    add_finding(
                        "orphan_blob",
                        content_hash=record.content_hash,
                        observed_size=record.size_bytes,
                        repairable=True,
                    )
            elif reference[2] == RetentionLifecycle.DELETED.value:
                add_finding(
                    "purged_blob_present",
                    content_hash=record.content_hash,
                    artifact_id=reference[0],
                    expected_size=reference[1],
                    observed_size=record.size_bytes,
                )

        for content_hash, (artifact_id, expected_size, lifecycle) in references.items():
            if lifecycle == RetentionLifecycle.DELETED.value:
                continue
            blob_record = by_hash.get(content_hash)
            if blob_record is None:
                add_finding(
                    "missing_blob",
                    content_hash=content_hash,
                    artifact_id=artifact_id,
                    expected_size=expected_size,
                )
                continue
            try:
                self._artifact_blob_store().verify(content_hash, expected_size)
            except (ArtifactCorruptionError, ArtifactNotFoundError, ArtifactSecurityError):
                add_finding(
                    "corrupt_blob",
                    content_hash=content_hash,
                    artifact_id=artifact_id,
                    expected_size=expected_size,
                    observed_size=blob_record.size_bytes,
                )
        return ArtifactAuditReport(
            findings=tuple(findings),
            scanned_database_references=len(references),
            scanned_blobs=sum(record.content_hash is not None for record in inventory),
        )

    def _artifact_inventory_read_only(self) -> tuple[BlobInventoryRecord, ...]:
        """Inspect an existing store without creating its root or layout."""

        if self._artifact_store is not None:
            return self._artifact_store.inventory()
        try:
            root_status = self._artifact_root.lstat()
        except FileNotFoundError:
            return ()
        except OSError:
            return (BlobInventoryRecord(None, None, unsafe=True),)
        if not stat.S_ISDIR(root_status.st_mode):
            return (BlobInventoryRecord(None, None, unsafe=True),)
        for name in ("sha256", ".tmp"):
            try:
                child_status = (self._artifact_root / name).lstat()
            except OSError:
                return (BlobInventoryRecord(None, None, unsafe=True),)
            if not stat.S_ISDIR(child_status.st_mode):
                return (BlobInventoryRecord(None, None, unsafe=True),)
        return self._artifact_blob_store().inventory()

    def repair_orphan_blob(
        self,
        *,
        content_hash: str,
        finding_hash: str,
        capability: str,
    ) -> ArtifactRepairResult:
        self._authorize_physical_mutation(
            capability,
            resource_id=content_hash,
            operation="repair_orphan",
            operation_scope_hash=finding_hash,
        )
        action_hash = canonical_action_hash(
            {
                "action": "artifact.repair.delete_orphan_blob",
                "content_hash": content_hash,
                "finding_hash": finding_hash,
            }
        )
        blob_store = self._artifact_blob_store()
        with blob_store.mutation_guard():
            finding = next(
                (
                    item
                    for item in self.audit_artifacts().findings
                    if item.finding_type == "orphan_blob"
                    and item.content_hash == content_hash
                    and item.finding_hash == finding_hash
                    and item.repairable
                ),
                None,
            )
            if finding is None or finding.observed_size_bytes is None:
                result = ArtifactRepairResult(
                    finding_hash=finding_hash,
                    action="delete_orphan_blob",
                    repaired=False,
                    outcome="refused",
                )
                self.store.append_artifact_repair_event(
                    result,
                    content_hash=content_hash,
                    action_hash=action_hash,
                )
                return result
            if content_hash in self.store.list_artifact_blob_references():
                raise ConflictError("Artifact audit finding is stale")
            blob_store.delete_verified(content_hash, finding.observed_size_bytes)
            result = ArtifactRepairResult(
                finding_hash=finding_hash,
                action="delete_orphan_blob",
                repaired=True,
                outcome="completed",
            )
            self.store.append_artifact_repair_event(
                result,
                content_hash=content_hash,
                action_hash=action_hash,
            )
            return result

    def physically_delete_artifact(
        self,
        artifact_id: str,
        *,
        capability: str,
    ) -> ArtifactRetentionState:
        self._authorize_physical_mutation(
            capability,
            resource_id=artifact_id,
            operation="physical_delete",
        )
        blob_store = self._artifact_blob_store()
        with blob_store.mutation_guard():
            current = self.store.get_artifact_retention_state(artifact_id)
            if current.lifecycle is not RetentionLifecycle.TRASHED:
                raise ConflictError("Artifact must be in recoverable trash before deletion")
            policy = self.store.get_retention_policy(current.policy_ref)
            if not policy.allow_physical_delete:
                raise PermissionError("retention policy forbids physical deletion")
            if self.store.artifact_deletion_blockers(artifact_id):
                raise ConflictError("Artifact has protected retention references")
            artifact = self.store.get_artifact(artifact_id)
            blob_store.delete_verified(artifact.content_hash, artifact.size_bytes)
            now = self._next_retention_time(current)
            updated = current.model_copy(
                update={
                    "lifecycle": RetentionLifecycle.DELETED,
                    "deleted_at": now,
                    "updated_at": now,
                }
            )
            return self._save_retention_state(
                current,
                updated,
                event_type="retention.physical_delete_completed",
                action="artifact.physical_delete",
            )

    def reconcile_physically_deleted_artifact(
        self,
        artifact_id: str,
        *,
        prior_command_id: str,
        prior_action_hash: str,
        capability: str,
    ) -> ArtifactRetentionState:
        self._authorize_physical_mutation(
            capability,
            resource_id=artifact_id,
            operation="reconcile_delete",
            operation_scope_hash=prior_command_id,
        )
        receipt = self.store.get_command_execution(prior_command_id)
        if (
            receipt.status is not CommandExecutionStatus.MANUAL_RECONCILE_REQUIRED
            or receipt.action_hash != prior_action_hash
        ):
            raise ConflictError("physical deletion receipt is not eligible for reconciliation")
        current = self.store.get_artifact_retention_state(artifact_id)
        if current.lifecycle is not RetentionLifecycle.TRASHED:
            raise ConflictError("only a trashed Artifact can reconcile missing content")
        policy = self.store.get_retention_policy(current.policy_ref)
        if not policy.allow_physical_delete or self.store.artifact_deletion_blockers(artifact_id):
            raise ConflictError("Artifact is not eligible for physical deletion reconciliation")
        artifact = self.store.get_artifact(artifact_id)
        try:
            self._artifact_blob_store().verify(artifact.content_hash, artifact.size_bytes)
        except ArtifactNotFoundError:
            pass
        else:
            raise ConflictError("Artifact blob still exists; reconciliation is not deletion")
        now = self._next_retention_time(current)
        updated = current.model_copy(
            update={
                "lifecycle": RetentionLifecycle.DELETED,
                "deleted_at": now,
                "updated_at": now,
            }
        )
        return self.store.update_artifact_retention_state(
            updated,
            expected_updated_at=current.updated_at,
            event_type="retention.physical_delete_outcome_unknown",
            action_hash=canonical_action_hash(
                {
                    "action": "artifact.reconcile_physical_delete",
                    "artifact_id": artifact_id,
                    "prior_command_id": prior_command_id,
                }
            ),
            outcome="outcome_unknown",
        )

    def issue_physical_mutation_capability(
        self,
        *,
        bootstrap_authorization: str,
        operation: str,
        resource_id: str,
        operation_scope_hash: str | None = None,
        ttl_seconds: int = 120,
    ) -> str:
        """Trusted hook only; HTTP routes intentionally never issue this capability."""

        if (
            not self._physical_delete_enabled
            or self._physical_delete_authorization is None
            or not hmac.compare_digest(
                bootstrap_authorization,
                self._physical_delete_authorization,
            )
        ):
            raise PermissionError("physical Artifact mutation is disabled or unauthorized")
        if operation not in {"physical_delete", "repair_orphan", "reconcile_delete"}:
            raise ValueError("unsupported physical Artifact mutation")
        if not 1 <= ttl_seconds <= 300:
            raise ValueError("physical Artifact capability TTL must be at most 300 seconds")
        return self._artifact_capabilities.issue(
            artifact_id=resource_id,
            operation=operation,
            access_level=ArtifactAccessLevel.NORMAL,
            ttl_seconds=ttl_seconds,
            operation_scope_hash=operation_scope_hash,
        )

    def _authorize_physical_mutation(
        self,
        capability: str,
        *,
        resource_id: str,
        operation: str,
        operation_scope_hash: str | None = None,
    ) -> None:
        if not self._physical_delete_enabled:
            raise PermissionError("physical Artifact mutation is disabled")
        self._artifact_capabilities.verify(
            capability,
            artifact_id=resource_id,
            operation=operation,
            sensitivity=ArtifactSensitivity.NORMAL,
            operation_scope_hash=operation_scope_hash,
        )

    def record_cache_observation(self, observation: CacheObservation) -> CacheObservation:
        """Persist bounded facts only; content, provider keys, and secrets are absent."""

        sanitized = observation.model_copy(
            update={
                "provider": redact_public_text(observation.provider, max_chars=200),
                "model": redact_public_text(observation.model, max_chars=300),
                "request_id": (
                    None
                    if observation.request_id is None
                    else redact_public_text(observation.request_id, max_chars=300)
                ),
                "cache_scope": (
                    None
                    if observation.cache_scope is None
                    else redact_public_text(observation.cache_scope, max_chars=300)
                ),
                "breakpoint_id": (
                    None
                    if observation.breakpoint_id is None
                    else redact_public_text(observation.breakpoint_id, max_chars=300)
                ),
            }
        )
        return self.store.add_cache_observation(sanitized)

    def list_cache_observations(
        self,
        *,
        context_revision_id: str | None = None,
        after_cursor: int | None = None,
        limit: int = 100,
    ) -> list[CacheObservation]:
        return self.store.list_cache_observations(
            context_revision_id=context_revision_id,
            after_cursor=after_cursor,
            limit=limit,
        )

    def _record_runtime_cache_observation(
        self,
        session: Session,
        event: RuntimeEvent,
    ) -> CacheObservation:
        usage = event.payload.get("usage")
        usage_dict = usage if isinstance(usage, dict) else {}
        cache_read_tokens = usage_dict.get("cache_read_tokens")
        if not isinstance(cache_read_tokens, int) or isinstance(cache_read_tokens, bool):
            cache_read_tokens = None
        hit_status = (
            CacheHitStatus.UNKNOWN
            if cache_read_tokens is None
            else CacheHitStatus.HIT
            if cache_read_tokens > 0
            else CacheHitStatus.MISS
        )
        revision_id = event.payload.get("context_revision_id")
        context_revision_id = revision_id if isinstance(revision_id, str) else None
        stable_prefix_hash: str | None = None
        if context_revision_id is not None:
            revision = self.store.get_context_revision(context_revision_id)
            eligible_hashes = [
                block.content_hash for block in revision.blocks if block.cache_eligible
            ]
            if eligible_hashes:
                stable_prefix_hash = canonical_action_hash(
                    {"cache_eligible_prefix": eligible_hashes}
                )

        def optional_nonnegative_int(name: str) -> int | None:
            value = usage_dict.get(name)
            return (
                value
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0
                else None
            )

        request_id = event.payload.get("provider_request_id")
        return self.record_cache_observation(
            CacheObservation(
                provider=type(self.provider).__name__,
                model=session.role_snapshot.model_id,
                request_id=request_id if isinstance(request_id, str) else None,
                context_revision_id=context_revision_id,
                hit_status=hit_status,
                prompt_tokens=optional_nonnegative_int("prompt_tokens"),
                completion_tokens=optional_nonnegative_int("completion_tokens"),
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=optional_nonnegative_int("cache_write_tokens"),
                stable_prefix_hash=stable_prefix_hash,
                invalidation_reason=(
                    None
                    if hit_status is CacheHitStatus.HIT
                    else "provider_reported"
                    if hit_status is CacheHitStatus.MISS
                    else "unknown"
                ),
            )
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
        workspace_ref: str | None = None,
        limit: int | None = None,
    ) -> list[WorkflowRun]:
        return self.store.list_workflow_runs(
            status=status,
            workspace_ref=workspace_ref,
            limit=limit,
        )

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
        history_turn: Turn | None = None
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
            bound_history = thread_id is not None and any(
                ref.source_type.value == "session" and ref.source_id == session.id
                for ref in self.get_thread(thread_id).legacy_refs
            )
            history_cursor = (
                self._latest_thread_item_cursor(thread_id)
                if bound_history and thread_id is not None
                else None
            )
            context_composer = PersistentContextComposer(
                store=self.store,
                session=session,
                agent_id=agent.id,
                workspace=normalized_workspace,
                thread_id=thread_id,
                references=tuple(references),
                thread_item_cursor_end=history_cursor,
                memory_resolver=lambda memory_id: self.get_memory(
                    memory_id,
                    snapshot=session.role_snapshot,
                    session_id=session.id,
                    project_scope=normalized_workspace,
                ),
                artifact_reader=self._read_artifact_for_context,
                artifact_writer=lambda content: self._write_tool_result_artifact(content=content),
            )
            if bound_history and thread_id is not None:
                history_turn = self.store.create_turn(Turn(thread_id=thread_id))
                self.append_item(
                    Item(
                        thread_id=thread_id,
                        turn_id=history_turn.id,
                        payload=UserMessagePayload(text=user_message),
                    )
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
                    timeout_event = self._persist_runtime_event(
                        session.id, agent.id, timeout_event, history_turn=history_turn
                    )
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
                    timeout_event = self._persist_runtime_event(
                        session.id, agent.id, timeout_event, history_turn=history_turn
                    )
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
                    cancel_event = self._persist_runtime_event(
                        session.id, agent.id, cancel_event, history_turn=history_turn
                    )
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

                runtime_event = self._persist_runtime_event(
                    session.id, agent.id, runtime_event, history_turn=history_turn
                )
                yield runtime_event
                if runtime_event.event_type == "model.completed":
                    try:
                        self._record_runtime_cache_observation(session, runtime_event)
                    except Exception as exc:
                        observation_failure = self._persist_runtime_event(
                            session.id,
                            agent.id,
                            RuntimeEvent(
                                event_type="cache.observation_failed",
                                turn=runtime_event.turn,
                                payload={"error_type": type(exc).__name__},
                            ),
                        )
                        yield observation_failure
        except asyncio.CancelledError:
            final_status = AgentStatus.CANCELLED
            cancel_event = RuntimeEvent(
                event_type="agent.cancelled",
                turn=0,
                payload={"reason": "stream_cancelled"},
            )
            self._persist_runtime_event(
                session.id, agent.id, cancel_event, history_turn=history_turn
            )
            raise
        except Exception as exc:
            failure_event = RuntimeEvent(
                event_type="agent.failed",
                turn=0,
                payload={"error_type": type(exc).__name__},
            )
            failure_event = self._persist_runtime_event(
                session.id, agent.id, failure_event, history_turn=history_turn
            )
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
        self,
        session_id: str,
        agent_id: str | None,
        event: RuntimeEvent,
        *,
        history_turn: Turn | None = None,
    ) -> RuntimeEvent:
        sanitized_event = event.model_copy(update={"payload": redact_public_data(event.payload)})
        history_item = None
        if history_turn is not None and agent_id is not None:
            payload: ItemPayload | None = None
            content = sanitized_event.payload.get("content")
            if event.event_type == "model.completed" and isinstance(content, str) and content:
                payload = AgentMessagePayload(text=content, agent_id=agent_id)
            elif event.event_type == "tool.started":
                payload = ToolCallPayload(
                    tool_call_id=str(sanitized_event.payload["tool_call_id"]),
                    tool_name=str(sanitized_event.payload["name"]),
                )
            elif event.event_type.startswith(("agent.", "tool.")):
                payload = SystemEventPayload(
                    event_type=event.event_type,
                    summary=event.event_type,
                    source_ref=agent_id,
                )
            if payload is not None:
                history_item = Item(
                    thread_id=history_turn.thread_id,
                    turn_id=history_turn.id,
                    payload=payload,
                )
        persisted = self.store.append_event(
            Event(
                session_id=session_id,
                agent_id=agent_id,
                event_type=sanitized_event.event_type,
                payload={"turn": sanitized_event.turn, **sanitized_event.payload},
            ),
            history_item=history_item,
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
