from __future__ import annotations

import asyncio
import os
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from operant.application.graph import GraphRuntime
from operant.application.phase45_gateway import Phase45ActionGateway
from operant.application.scheduler import (
    SchedulerConflictError,
    SchedulerValidationError,
    TriggerService,
)
from operant.application.security import (
    ActionNormalizer,
    ApprovalReviewerAdapter,
    CapabilityBroker,
    CapabilityDenied,
    DenialRemediator,
    PolicyDenied,
    PolicyEngine,
    SecretBroker,
    SecretMaterial,
    SecretUnavailable,
    balanced_policy_bundle,
)
from operant.domain.scheduler import RunRequestStatus, ScheduleDefinition, ScheduleStatus
from operant.domain.security import (
    ActionRequest,
    Capability,
    PolicyDecision,
    SecurityAuditEvent,
)
from operant.mcp import (
    LegacySseTransport,
    McpAdapter,
    McpError,
    McpLimits,
    McpServerConfig,
    McpStdioConfig,
    StdioTransport,
)
from operant.persistence.graph_team import SQLiteGraphRepository
from operant.persistence.phase45 import SQLitePhase45Repository
from operant.persistence.scheduler import SQLiteSchedulerStore
from operant.persistence.security import SQLiteSecurityRepository
from operant.persistence.sqlite import ConflictError, IdempotencyConflictError, SQLiteStore
from operant.runtime.scheduler import SchedulerWorker
from operant.runtime.scheduler_integration import (
    GraphSchedulerActionGateway,
    SchedulerCoordinator,
    SchedulerSecurityService,
    SQLiteGraphDispatchRegistry,
    fastapi_scheduler_lifespan,
)
from operant.skills import SkillDiscovery, SkillDiscoveryLimits

_SECRET_REF_PATTERN = r"^[A-Z][A-Z0-9_]{1,127}$"
_ROOT_REF_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
_DOCKER_IMAGE_PATTERN = r"^(?:[A-Za-z0-9][A-Za-z0-9._:/-]*@)?sha256:[0-9a-f]{64}$"
_MAX_PHASE45_JSON_BYTES = 256_000


class NormalizeActionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    principal: str = Field(min_length=1, max_length=300)
    tool: str = Field(min_length=1, max_length=200)
    operation: str = Field(min_length=1, max_length=200)
    arguments: dict[str, Any] = Field(default_factory=dict)
    requested_capabilities: tuple[Capability, ...] = Field(min_length=1, max_length=32)
    idempotency_key: str = Field(min_length=1, max_length=300)
    workspace: str | None = Field(default=None, max_length=4096)
    workspace_id: str | None = Field(default=None, max_length=300)
    session_id: str | None = Field(default=None, max_length=300)
    workflow_run_id: str | None = Field(default=None, max_length=300)
    node_run_id: str | None = Field(default=None, max_length=300)
    agent_instance_id: str | None = Field(default=None, max_length=300)
    secret_refs: tuple[str, ...] = Field(default=(), max_length=32)
    data_classification: str = Field(default="internal", max_length=100)
    sandbox_profile: str = Field(default="isolated", max_length=100)
    network_profile: str = Field(default="none", max_length=100)
    dry_run: bool = False


class PolicyTestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actions: tuple[NormalizeActionBody, ...] = Field(min_length=1, max_length=100)


class IssueCapabilityBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    capability: Capability
    issued_by: str = Field(min_length=1, max_length=300)
    ttl_seconds: int = Field(default=60, ge=1, le=300)
    max_uses: int = Field(default=1, ge=1, le=100)


class ConsumeCapabilityBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    capability: Capability


class SkillDiscoverBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root_refs: tuple[str, ...] = Field(default=(), max_length=16)


class McpServerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server_id: str = Field(min_length=1, max_length=200)
    transport: Literal["stdio", "legacy_sse"]
    endpoint_ref: str | None = Field(default=None, pattern=_SECRET_REF_PATTERN)
    secret_ref: str | None = Field(default=None, pattern=_SECRET_REF_PATTERN)
    stdio_argv: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=64)
    cwd_ref: str | None = Field(default=None, min_length=1, max_length=4096)
    environment_refs: dict[str, str] = Field(default_factory=dict)
    workspace_root_ref: str | None = Field(default=None, pattern=_ROOT_REF_PATTERN)
    docker_image: str | None = Field(
        default=None,
        min_length=1,
        max_length=300,
        pattern=_DOCKER_IMAGE_PATTERN,
    )
    allow_loopback_http: bool = False

    @model_validator(mode="after")
    def validate_transport(self) -> McpServerBody:
        if len(self.environment_refs) > 64 or any(
            not key or len(key) > 128 or not value or len(value) > 128
            for key, value in self.environment_refs.items()
        ):
            raise ValueError("MCP environment references are invalid or too numerous")
        if self.transport == "stdio":
            if (
                self.stdio_argv is None
                or self.workspace_root_ref is None
                or self.docker_image is None
                or self.endpoint_ref is not None
                or self.secret_ref is not None
                or self.environment_refs
            ):
                raise ValueError(
                    "stdio requires argv, workspace root, and pinned Docker image; "
                    "endpoint and environment secrets are forbidden"
                )
            if self.cwd_ref is not None and Path(self.cwd_ref).is_absolute():
                raise ValueError("stdio cwd_ref must be relative to the configured workspace")
        elif (
            self.endpoint_ref is None
            or self.stdio_argv is not None
            or self.cwd_ref is not None
            or self.environment_refs
            or self.workspace_root_ref is not None
            or self.docker_image is not None
        ):
            raise ValueError("legacy SSE requires endpoint_ref and forbids stdio fields")
        return self


class McpToolCallBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    arguments: dict[str, Any] = Field(default_factory=dict)


class Phase45ApprovalDecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool
    reason_code: str | None = Field(default=None, max_length=200)


class ScheduleStatusBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ScheduleStatus
    expected_version: int = Field(ge=1)


class IdempotentTriggerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=300)


class LeasedReferenceResolver:
    """Keep exact SecretBroker material only until the shortest lease expires."""

    def __init__(self, materials: tuple[SecretMaterial, ...]) -> None:
        if not materials:
            raise ValueError("leased reference resolver requires secret material")
        self._values = {
            key: value for material in materials for key, value in material.environment.items()
        }
        self.expires_at = min(material.lease.expires_at for material in materials)

    @property
    def secret_values(self) -> tuple[str, ...]:
        return tuple(self._values.values())

    def resolve(self, reference: str) -> str:
        if datetime.now(timezone.utc) >= self.expires_at:
            self.clear()
            raise McpError("mcp.secret_lease_expired", "MCP secret lease expired")
        try:
            return self._values[reference]
        except KeyError as exc:
            raise McpError(
                "mcp.reference_unavailable", "configured reference is unavailable"
            ) from exc

    def clear(self) -> None:
        self._values.clear()


def install_phase45_routes(
    app: FastAPI,
    store: SQLiteStore,
    *,
    skill_roots: Mapping[str, str | Path] | None = None,
    mcp_workspace_roots: Mapping[str, str | Path] | None = None,
    policy_engine: PolicyEngine | None = None,
    approval_reviewer: ApprovalReviewerAdapter | None = None,
) -> None:
    repository = SQLiteSecurityRepository(store)
    phase_repository = SQLitePhase45Repository(store)
    scheduler_store = SQLiteSchedulerStore(store.path)
    normalizer = ActionNormalizer()
    engine = policy_engine or PolicyEngine(balanced_policy_bundle())
    broker = CapabilityBroker(repository)
    remediator = DenialRemediator(repository)
    phase_gateway = Phase45ActionGateway(repository, phase_repository, engine)
    secret_broker = SecretBroker()
    trusted_skill_roots: dict[str, Path] = {}
    for root_ref, configured_root in (skill_roots or {}).items():
        root = Path(configured_root)
        if not root_ref or len(root_ref) > 200 or not root.is_absolute():
            raise ValueError("configured skill roots require bounded references and absolute paths")
        trusted_skill_roots[root_ref] = root.resolve(strict=True)
    trusted_mcp_workspace_roots: dict[str, Path] = {}
    for root_ref, configured_root in (mcp_workspace_roots or {}).items():
        root = Path(configured_root)
        resolved = root.resolve(strict=True)
        if (
            re.fullmatch(_ROOT_REF_PATTERN, root_ref) is None
            or not root.is_absolute()
            or not resolved.is_dir()
            or resolved in {Path(resolved.anchor), Path.home().resolve()}
        ):
            raise ValueError("configured MCP roots require safe references and directories")
        trusted_mcp_workspace_roots[root_ref] = resolved
    mcp_runtimes: dict[str, McpAdapter] = {}
    mcp_expiry_tasks: dict[str, asyncio.Task[None]] = {}
    phase_repository.reconcile_mcp_lifecycle()
    graph_repository = SQLiteGraphRepository(store)
    graph_runtime = GraphRuntime(graph_repository)
    trigger_service = TriggerService(scheduler_store, workflow_repository=graph_repository)
    scheduler_security = SchedulerSecurityService(
        repository=repository,
        normalizer=normalizer,
        policy_engine=engine,
        capability_broker=broker,
    )
    scheduler_gateway = GraphSchedulerActionGateway(
        graph_repository=graph_repository,
        graph_runtime=graph_runtime,
        security=scheduler_security,
        dispatch_registry=SQLiteGraphDispatchRegistry(store.path),
    )
    scheduler_worker = SchedulerWorker(
        store=scheduler_store,
        gateway=scheduler_gateway,
        owner=f"operant-core-{os.getpid()}",
    )
    scheduler_coordinator = SchedulerCoordinator(
        store=scheduler_store,
        trigger_service=trigger_service,
        worker=scheduler_worker,
        owner=f"operant-core-{os.getpid()}",
    )
    app.state.security_repository = repository
    app.state.policy_engine = engine
    app.state.capability_broker = broker
    app.state.phase45_repository = phase_repository
    app.state.scheduler_store = scheduler_store
    app.state.trigger_service = trigger_service
    app.state.phase45_action_gateway = phase_gateway
    app.state.mcp_runtimes = mcp_runtimes
    app.state.mcp_expiry_tasks = mcp_expiry_tasks
    app.state.scheduler_coordinator = scheduler_coordinator

    async def close_mcp_runtimes() -> None:
        for task in mcp_expiry_tasks.values():
            task.cancel()
        if mcp_expiry_tasks:
            await asyncio.gather(*mcp_expiry_tasks.values(), return_exceptions=True)
        mcp_expiry_tasks.clear()
        runtimes = tuple(mcp_runtimes.items())
        mcp_runtimes.clear()
        for server_id, runtime in runtimes:
            try:
                await runtime.close()
                phase_repository.set_mcp_lifecycle(
                    server_id, "stopped", "mcp.stopped", detail={"reason": "core_shutdown"}
                )
            except Exception:
                phase_repository.set_mcp_lifecycle(
                    server_id, "failed", "mcp.stop_failed", detail={"reason": "core_shutdown"}
                )

    app.router.add_event_handler("shutdown", close_mcp_runtimes)
    previous_lifespan = app.router.lifespan_context
    scheduler_lifespan = fastapi_scheduler_lifespan(scheduler_coordinator)

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI) -> AsyncIterator[None]:
        async with previous_lifespan(current_app), scheduler_lifespan(current_app):
            yield

    app.router.lifespan_context = combined_lifespan

    def bounded(value: Any) -> None:
        import json

        try:
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeError) as exc:
            raise HTTPException(status_code=422, detail="request contains invalid JSON") from exc
        if len(encoded) > _MAX_PHASE45_JSON_BYTES:
            raise HTTPException(status_code=413, detail="Phase 4/5A request is too large")

    def guard_side_effect(
        *,
        tool: str,
        operation: str,
        target_id: str,
        arguments: dict[str, Any],
        capabilities: tuple[Capability, ...],
        idempotency_key: str,
        secret_refs: tuple[str, ...] = (),
        workspace: str | None = None,
        sandbox_profile: str = "isolated",
        network_profile: str = "none",
    ) -> tuple[ActionRequest, Any]:
        bounded(arguments)
        action, result, evaluation = phase_gateway.guard(
            tool=tool,
            operation=operation,
            target_id=target_id,
            arguments=arguments,
            capabilities=capabilities,
            idempotency_key=idempotency_key,
            secret_refs=secret_refs,
            workspace=workspace,
            sandbox_profile=sandbox_profile,
            network_profile=network_profile,
        )
        if result.decision.value != PolicyDecision.ALLOW.value or result.lease is None:
            status = 403 if result.decision.value == PolicyDecision.DENY.value else 409
            detail: Any = result.reason_code
            if result.decision.value == PolicyDecision.ASK.value and result.approval_id is not None:
                detail = {
                    "code": "approval_required",
                    "reason_code": result.reason_code,
                    "approval_id": result.approval_id,
                    "action_hash": action.action_hash,
                    "policy_version": action.policy_version,
                    "expires_at": phase_repository.get_phase45_approval(result.approval_id)[
                        "expires_at"
                    ],
                }
            raise HTTPException(status_code=status, detail=detail)
        phase_gateway.consume(result.lease, action)
        return action, evaluation

    def normalize(body: NormalizeActionBody) -> ActionRequest:
        try:
            return normalizer.normalize(
                principal=body.principal,
                tool=body.tool,
                operation=body.operation,
                arguments=body.arguments,
                requested_capabilities=body.requested_capabilities,
                idempotency_key=body.idempotency_key,
                policy_version=engine.bundle.version,
                workspace=None if body.workspace is None else Path(body.workspace),
                workspace_id=body.workspace_id,
                session_id=body.session_id,
                workflow_run_id=body.workflow_run_id,
                node_run_id=body.node_run_id,
                agent_instance_id=body.agent_instance_id,
                secret_refs=body.secret_refs,
                data_classification=body.data_classification,
                sandbox_profile=body.sandbox_profile,
                network_profile=body.network_profile,
                dry_run=body.dry_run,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def persist(action: ActionRequest) -> ActionRequest:
        try:
            return repository.record_security_action(action)
        except IdempotencyConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    def evaluate(action: ActionRequest) -> dict[str, Any]:
        evaluation = engine.evaluate(action)
        repository.append_security_audit(
            SecurityAuditEvent(
                action_hash=action.action_hash,
                principal=action.principal,
                event_type="policy.evaluated",
                decision=evaluation.decision,
                rule_ids=evaluation.matched_rule_ids,
                detail={
                    "reason_code": evaluation.reason_code,
                    "risk_level": evaluation.risk_level.value,
                    "hard_deny": evaluation.hard_deny,
                },
            )
        )
        result: dict[str, Any] = {"evaluation": evaluation.model_dump(mode="json")}
        if evaluation.decision is PolicyDecision.DENY:
            result["remediation"] = remediator.build(action, evaluation).model_dump(mode="json")
        return result

    @app.post("/v1/security/actions/normalize", operation_id="normalizeAction")
    async def normalize_action(body: NormalizeActionBody) -> dict[str, Any]:
        return persist(normalize(body)).model_dump(mode="json")

    @app.post("/v1/security/policy/check", operation_id="checkPolicy")
    async def check_policy(body: NormalizeActionBody) -> dict[str, Any]:
        return evaluate(persist(normalize(body)))

    @app.post("/v1/security/policy/explain", operation_id="explainPolicy")
    async def explain_policy(body: NormalizeActionBody) -> dict[str, Any]:
        action = persist(normalize(body))
        result = evaluate(action)
        result["explanation"] = engine.explain(action)
        return result

    @app.post("/v1/security/policy/test", operation_id="testPolicy")
    async def test_policy(body: PolicyTestBody) -> dict[str, Any]:
        actions = tuple(persist(normalize(item)) for item in body.actions)
        return {"results": [evaluate(action) for action in actions]}

    @app.get("/v1/security/approvals/{approval_id}", operation_id="getPhase45Approval")
    async def get_phase45_approval(approval_id: str) -> dict[str, Any]:
        try:
            return phase_repository.get_phase45_approval(approval_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="approval not found") from exc

    def audit_approval_decision(
        approval: dict[str, Any], *, approved: bool, decided_by: str, changed: bool
    ) -> None:
        if not changed:
            return
        action = repository.get_security_action(str(approval["action_hash"]))
        repository.append_security_audit(
            SecurityAuditEvent(
                action_hash=action.action_hash,
                principal=action.principal,
                event_type="approval.decided",
                decision=PolicyDecision.ALLOW if approved else PolicyDecision.DENY,
                detail={
                    "approval_id": approval["approval_id"],
                    "decided_by": decided_by,
                    "reason_code": approval["reason_code"],
                },
            )
        )

    @app.post("/v1/security/approvals/{approval_id}", operation_id="decidePhase45Approval")
    async def decide_phase45_approval(
        approval_id: str, body: Phase45ApprovalDecisionBody
    ) -> dict[str, Any]:
        try:
            approval, changed = phase_repository.decide_phase45_approval(
                approval_id,
                approved=body.approved,
                decided_by="user",
                reason_code=body.reason_code,
            )
            audit_approval_decision(
                approval,
                approved=body.approved,
                decided_by="user",
                changed=changed,
            )
            return approval
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="approval not found") from exc
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/v1/security/approvals/{approval_id}/review",
        operation_id="reviewPhase45Approval",
    )
    async def review_phase45_approval(approval_id: str) -> dict[str, Any]:
        if approval_reviewer is None:
            raise HTTPException(status_code=409, detail="approval reviewer is not configured")
        try:
            pending = phase_repository.get_phase45_approval(approval_id)
            action = repository.get_security_action(str(pending["action_hash"]))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="approval not found") from exc
        evaluation = engine.evaluate(action)
        try:
            reviewer_decision = await approval_reviewer.review(action, evaluation)
        except PolicyDenied as exc:
            raise HTTPException(
                status_code=409, detail="approval is not reviewer eligible"
            ) from exc
        approved = reviewer_decision.decision is PolicyDecision.ALLOW
        try:
            approval, changed = phase_repository.decide_phase45_approval(
                approval_id,
                approved=approved,
                decided_by="reviewer",
                reason_code=reviewer_decision.reason_code,
            )
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        audit_approval_decision(
            approval,
            approved=approved,
            decided_by="reviewer",
            changed=changed,
        )
        return {**approval, "review_summary": reviewer_decision.summary}

    @app.post("/v1/security/capability-leases", operation_id="issueCapabilityLease")
    async def issue_capability(body: IssueCapabilityBody) -> dict[str, Any]:
        try:
            action = repository.get_security_action(body.action_hash)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="security action not found") from exc
        evaluation = engine.evaluate(action)
        try:
            lease = broker.issue(
                action,
                evaluation,
                body.capability,
                issued_by=body.issued_by,
                ttl_seconds=body.ttl_seconds,
                max_uses=body.max_uses,
            )
        except CapabilityDenied as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        repository.append_security_audit(
            SecurityAuditEvent(
                action_hash=action.action_hash,
                principal=action.principal,
                event_type="capability.issued",
                decision=PolicyDecision.ALLOW,
                rule_ids=evaluation.matched_rule_ids,
                detail={
                    "lease_id": lease.lease_id,
                    "capability": lease.capability.value,
                    "expires_at": lease.expires_at.isoformat(),
                    "max_uses": lease.max_uses,
                },
            )
        )
        return lease.model_dump(mode="json")

    @app.post(
        "/v1/security/capability-leases/{lease_id}/consume",
        operation_id="consumeCapabilityLease",
    )
    async def consume_capability(lease_id: str, body: ConsumeCapabilityBody) -> dict[str, Any]:
        try:
            action = repository.get_security_action(body.action_hash)
            lease = broker.consume(lease_id, action=action, capability=body.capability)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="security action not found") from exc
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        repository.append_security_audit(
            SecurityAuditEvent(
                action_hash=action.action_hash,
                principal=action.principal,
                event_type="capability.consumed",
                decision=PolicyDecision.ALLOW,
                detail={
                    "lease_id": lease.lease_id,
                    "capability": lease.capability.value,
                    "uses": lease.uses,
                    "max_uses": lease.max_uses,
                },
            )
        )
        return lease.model_dump(mode="json")

    @app.get(
        "/v1/security/actions/{action_hash}/audit",
        operation_id="listSecurityAudit",
    )
    async def list_security_audit(
        action_hash: str,
        after_cursor: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        events = repository.list_security_audit(action_hash, after_cursor=after_cursor, limit=limit)
        return {"items": [event.model_dump(mode="json") for event in events]}

    @app.post("/v1/skills/discover", operation_id="discoverSkills")
    async def discover_skills(body: SkillDiscoverBody) -> dict[str, Any]:
        selected_refs = body.root_refs or tuple(sorted(trusted_skill_roots))
        if any(ref not in trusted_skill_roots for ref in selected_refs):
            raise HTTPException(status_code=404, detail="configured skill root not found")
        selected_roots = tuple(trusted_skill_roots[ref] for ref in selected_refs)
        guard_side_effect(
            tool="skill_discovery",
            operation="read",
            target_id="skill-roots",
            arguments={"root_refs": list(selected_refs)},
            capabilities=(Capability.WORKSPACE_READ,),
            idempotency_key="skill-discovery:"
            + ActionRequest.calculate_hash({"root_refs": list(selected_refs)}),
        )
        try:
            result = SkillDiscovery(selected_roots, limits=SkillDiscoveryLimits()).discover()
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        persisted: list[dict[str, Any]] = []
        for root_index, root_ref in enumerate(selected_refs):
            root_candidates = tuple(
                candidate for candidate in result.candidates if candidate.root_index == root_index
            )
            persisted.extend(phase_repository.replace_skill_candidates(root_ref, root_candidates))
        return {
            "candidates": persisted,
            "issues": [issue.model_dump(mode="json") for issue in result.issues],
        }

    @app.get("/v1/skills", operation_id="listSkills")
    async def list_skills(limit: int = Query(default=200, ge=1, le=500)) -> dict[str, Any]:
        return {"items": list(phase_repository.list_skill_candidates(limit=limit))}

    def server_payload(body: McpServerBody) -> dict[str, Any]:
        return body.model_dump(mode="json")

    def mcp_runtime_target_ref(config: dict[str, Any]) -> str:
        """Bind every tool receipt/approval to this exact persisted server revision."""

        binding = {
            key: config.get(key)
            for key in (
                "server_id",
                "transport",
                "endpoint_ref",
                "secret_ref",
                "stdio_argv",
                "cwd_ref",
                "workspace_root_ref",
                "docker_image",
                "allow_loopback_http",
            )
        }
        return (
            f"{config['transport']}:{config['server_id']}:{ActionRequest.calculate_hash(binding)}"
        )

    def guard_mcp_config(body: McpServerBody, operation: str) -> None:
        if body.transport == "stdio" and body.workspace_root_ref not in trusted_mcp_workspace_roots:
            raise HTTPException(status_code=404, detail="configured MCP workspace root not found")
        guard_side_effect(
            tool="mcp_config",
            operation=operation,
            target_id=body.server_id,
            arguments={
                "transport": body.transport,
                "endpoint_ref": body.endpoint_ref,
                "secret_ref": body.secret_ref,
                "workspace_root_ref": body.workspace_root_ref,
                "cwd_ref": body.cwd_ref,
                "docker_image": body.docker_image,
                "argv_sha256": None
                if body.stdio_argv is None
                else ActionRequest.calculate_hash({"argv": body.stdio_argv}),
            },
            capabilities=(Capability.WORKSPACE_WRITE,),
            idempotency_key=f"mcp-config:{operation}:{body.server_id}:"
            + ActionRequest.calculate_hash(server_payload(body)),
        )

    @app.post("/v1/mcp/servers", operation_id="createMcpServer", status_code=201)
    async def create_mcp_server(body: McpServerBody) -> dict[str, Any]:
        guard_mcp_config(body, "create")
        try:
            return phase_repository.put_mcp_server(server_payload(body), create_only=True)
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.put("/v1/mcp/servers/{server_id}", operation_id="updateMcpServer")
    async def update_mcp_server(server_id: str, body: McpServerBody) -> dict[str, Any]:
        if server_id != body.server_id:
            raise HTTPException(status_code=409, detail="MCP server ID does not match path")
        guard_mcp_config(body, "update")
        try:
            phase_repository.get_mcp_server(server_id)
            return phase_repository.put_mcp_server(server_payload(body), create_only=False)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="MCP server not found") from exc
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/v1/mcp/servers", operation_id="listMcpServers")
    async def list_mcp_servers() -> dict[str, Any]:
        return {"items": list(phase_repository.list_mcp_servers())}

    @app.get("/v1/mcp/workspace-roots", operation_id="listMcpWorkspaceRoots")
    async def list_mcp_workspace_roots() -> dict[str, Any]:
        # Paths are host authority and never belong in the remote/UI projection.
        return {"items": [{"root_ref": ref} for ref in sorted(trusted_mcp_workspace_roots)]}

    @app.get("/v1/mcp/servers/{server_id}/tools", operation_id="listMcpTools")
    async def list_mcp_tools(server_id: str) -> dict[str, Any]:
        try:
            phase_repository.get_mcp_server(server_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="MCP server not found") from exc
        return {"items": list(phase_repository.list_mcp_tools(server_id))}

    @app.get(
        "/v1/mcp/action-receipts/{action_hash}",
        operation_id="getMcpActionReceipt",
    )
    async def get_mcp_action_receipt(action_hash: str) -> dict[str, Any]:
        if len(action_hash) != 64 or any(
            character not in "0123456789abcdef" for character in action_hash
        ):
            raise HTTPException(status_code=422, detail="invalid MCP action hash")
        try:
            return phase_repository.get_mcp_action_receipt(action_hash)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="MCP action receipt not found") from exc

    def build_transport(
        config: dict[str, Any],
        *,
        secret_resolver: LeasedReferenceResolver | None = None,
    ) -> StdioTransport | LegacySseTransport:
        if config["transport"] == "stdio":
            root_ref = str(config["workspace_root_ref"])
            try:
                workspace = trusted_mcp_workspace_roots[root_ref]
            except KeyError as exc:
                raise McpError(
                    "mcp.workspace_root_unavailable",
                    "configured MCP workspace root is unavailable",
                ) from exc
            return StdioTransport(
                McpStdioConfig(
                    argv=tuple(config["stdio_argv"]),
                    workspace=str(workspace),
                    cwd="." if config["cwd_ref"] is None else config["cwd_ref"],
                    docker_image=config["docker_image"],
                ),
            )
        if secret_resolver is None:
            raise McpError(
                "mcp.secret_lease_required",
                "remote MCP requires a short-lived SecretBroker lease",
            )
        return LegacySseTransport(
            McpServerConfig(
                server_id=config["server_id"],
                endpoint_ref=config["endpoint_ref"],
                secret_ref=config["secret_ref"],
                allow_loopback_http=config["allow_loopback_http"],
            ),
            reference_resolver=secret_resolver,
        )

    async def expire_mcp_secret_lease(
        server_id: str,
        adapter: McpAdapter,
        resolver: LeasedReferenceResolver,
    ) -> None:
        delay = max(
            0.0,
            (resolver.expires_at - datetime.now(timezone.utc)).total_seconds(),
        )
        try:
            await asyncio.sleep(delay)
            if mcp_runtimes.get(server_id) is adapter:
                with suppress(Exception):
                    await adapter.close()
                mcp_runtimes.pop(server_id, None)
                with suppress(KeyError, ConflictError):
                    phase_repository.set_mcp_lifecycle(
                        server_id,
                        "stopped",
                        "mcp.secret_lease_expired",
                        detail={"reason": "secret_lease_expired"},
                    )
        finally:
            resolver.clear()
            mcp_expiry_tasks.pop(server_id, None)

    @app.post("/v1/mcp/servers/{server_id}/start", operation_id="startMcpServer")
    async def start_mcp_server(server_id: str) -> dict[str, Any]:
        if server_id in mcp_runtimes:
            raise HTTPException(status_code=409, detail="MCP server is already started")
        try:
            config = phase_repository.get_mcp_server(server_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="MCP server not found") from exc
        is_stdio = config["transport"] == "stdio"
        workspace = (
            trusted_mcp_workspace_roots.get(str(config["workspace_root_ref"])) if is_stdio else None
        )
        if is_stdio and workspace is None:
            raise HTTPException(status_code=409, detail="configured MCP workspace is unavailable")
        capabilities: tuple[Capability, ...] = (
            (Capability.PROCESS_EXEC_NO_NETWORK, Capability.WORKSPACE_READ)
            if is_stdio
            else (Capability.NETWORK_EGRESS, Capability.SECRET_USE)
        )
        secret_refs = (
            ()
            if is_stdio
            else tuple(
                ref for ref in (config["endpoint_ref"], config["secret_ref"]) if ref is not None
            )
        )
        security_arguments: dict[str, Any] = {
            "transport": config["transport"],
            "network_profile": "none" if is_stdio else "https",
        }
        if is_stdio:
            security_arguments.update(
                {
                    "argv": config["stdio_argv"],
                    "cwd": "." if config["cwd_ref"] is None else config["cwd_ref"],
                    "workspace_root_ref": config["workspace_root_ref"],
                    "docker_image": config["docker_image"],
                }
            )
        else:
            security_arguments.update(
                {
                    "endpoint_ref": config["endpoint_ref"],
                    "secret_ref": config["secret_ref"],
                }
            )
        action, evaluation = guard_side_effect(
            tool="mcp",
            operation="start",
            target_id=server_id,
            arguments=security_arguments,
            capabilities=capabilities,
            idempotency_key=f"mcp-start:{server_id}:{config['updated_at']}",
            secret_refs=secret_refs,
            workspace=None if workspace is None else str(workspace),
            sandbox_profile="docker-read-only" if is_stdio else "remote-https",
            network_profile="none" if is_stdio else "https",
        )
        secret_resolver: LeasedReferenceResolver | None = None
        if secret_refs:
            try:
                materials = tuple(
                    secret_broker.issue(
                        action,
                        evaluation,
                        secret_ref=secret_ref,
                        ttl_seconds=60,
                    )
                    for secret_ref in secret_refs
                )
            except SecretUnavailable as exc:
                raise HTTPException(
                    status_code=409,
                    detail="configured MCP secret reference is unavailable",
                ) from exc
            secret_resolver = LeasedReferenceResolver(materials)
        transport = build_transport(config, secret_resolver=secret_resolver)
        try:
            start_token, start_fencing = phase_repository.claim_mcp_start(
                server_id,
                owner=f"operant-core-{os.getpid()}",
                expected_updated_at=config["updated_at"],
            )
        except ConflictError as exc:
            if secret_resolver is not None:
                secret_resolver.clear()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        adapter = McpAdapter(
            server_id=server_id,
            target_ref=mcp_runtime_target_ref(config),
            transport=transport,
            action_gateway=phase_gateway,
            limits=McpLimits(),
            redact_values=() if secret_resolver is None else secret_resolver.secret_values,
        )
        try:
            tools = await adapter.start()
            snapshot_version = phase_repository.replace_mcp_tools(server_id, tools)
            phase_repository.finish_mcp_start(
                server_id,
                start_token,
                start_fencing,
                status="running",
                event_type="mcp.started",
                detail={"snapshot_version": snapshot_version, "tool_count": len(tools)},
            )
        except Exception as exc:
            with suppress(Exception):
                await adapter.close()
            with suppress(ConflictError):
                phase_repository.finish_mcp_start(
                    server_id,
                    start_token,
                    start_fencing,
                    status="failed",
                    event_type="mcp.start_failed",
                    detail={
                        "error_code": exc.code if isinstance(exc, McpError) else "mcp.start_failed"
                    },
                )
            raise HTTPException(status_code=502, detail="MCP server failed to start") from exc
        mcp_runtimes[server_id] = adapter
        if secret_resolver is not None:
            mcp_expiry_tasks[server_id] = asyncio.create_task(
                expire_mcp_secret_lease(server_id, adapter, secret_resolver)
            )
        return phase_repository.get_mcp_server(server_id)

    @app.post("/v1/mcp/servers/{server_id}/stop", operation_id="stopMcpServer")
    async def stop_mcp_server(server_id: str) -> dict[str, Any]:
        adapter = mcp_runtimes.get(server_id)
        if adapter is None:
            raise HTTPException(status_code=409, detail="MCP server is not running")
        guard_side_effect(
            tool="mcp",
            operation="stop",
            target_id=server_id,
            arguments={},
            capabilities=(Capability.PROCESS_EXEC_NO_NETWORK,),
            idempotency_key=f"mcp-stop:{server_id}",
        )
        expiry_task = mcp_expiry_tasks.pop(server_id, None)
        if expiry_task is not None:
            expiry_task.cancel()
            await asyncio.gather(expiry_task, return_exceptions=True)
        await adapter.close()
        mcp_runtimes.pop(server_id, None)
        phase_repository.set_mcp_lifecycle(server_id, "stopped", "mcp.stopped")
        return phase_repository.get_mcp_server(server_id)

    @app.post(
        "/v1/mcp/servers/{server_id}/tools/{tool_name}/call",
        operation_id="callMcpTool",
    )
    async def call_mcp_tool(
        server_id: str, tool_name: str, body: McpToolCallBody
    ) -> dict[str, Any]:
        bounded(body.arguments)
        adapter = mcp_runtimes.get(server_id)
        if adapter is None:
            raise HTTPException(status_code=409, detail="MCP server is not running")
        try:
            result = await adapter.call_tool(tool_name, body.arguments)
        except McpError as exc:
            if exc.code in {
                "mcp.action_receipt_conflict",
                "mcp.approval_required",
                "mcp.outcome_unknown",
            }:
                status = 409
            elif exc.code == "mcp.policy_denied":
                status = 403
            else:
                status = 422
            detail: Any = exc.code
            if exc.detail:
                detail = {"code": exc.code, **exc.detail}
            raise HTTPException(status_code=status, detail=detail) from exc
        return {"result": result}

    @app.delete("/v1/mcp/servers/{server_id}", operation_id="deleteMcpServer")
    async def delete_mcp_server(server_id: str) -> dict[str, bool]:
        guard_side_effect(
            tool="mcp_config",
            operation="delete",
            target_id=server_id,
            arguments={},
            capabilities=(Capability.WORKSPACE_WRITE,),
            idempotency_key=f"mcp-delete:{server_id}",
        )
        try:
            phase_repository.delete_mcp_server(server_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="MCP server not found") from exc
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"deleted": True}

    def guard_schedule(operation: str, target_id: str, payload: dict[str, Any], key: str) -> None:
        guard_side_effect(
            tool="scheduler",
            operation=operation,
            target_id=target_id,
            arguments=payload,
            capabilities=(Capability.WORKSPACE_WRITE,),
            idempotency_key=key,
        )

    @app.post("/v1/schedules", operation_id="createSchedule", status_code=201)
    async def create_schedule(schedule: ScheduleDefinition) -> dict[str, Any]:
        payload = schedule.model_dump(mode="json")
        try:
            trigger_service.validate_schedule(schedule)
        except SchedulerValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        guard_schedule("create", schedule.id, payload, f"schedule-create:{schedule.id}:v1")
        try:
            trigger_service.create_schedule(schedule)
        except SchedulerValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except SchedulerConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return scheduler_store.get_schedule(schedule.id).model_dump(mode="json")

    @app.put("/v1/schedules/{schedule_id}", operation_id="updateSchedule")
    async def update_schedule(schedule_id: str, schedule: ScheduleDefinition) -> dict[str, Any]:
        if schedule.id != schedule_id:
            raise HTTPException(status_code=409, detail="schedule ID does not match path")
        payload = schedule.model_dump(mode="json")
        try:
            trigger_service.validate_schedule(schedule)
        except SchedulerValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        guard_schedule(
            "update", schedule.id, payload, f"schedule-update:{schedule.id}:v{schedule.version}"
        )
        try:
            trigger_service.create_schedule(schedule)
        except SchedulerValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except SchedulerConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return scheduler_store.get_schedule(schedule.id).model_dump(mode="json")

    @app.get("/v1/schedules", operation_id="listSchedules")
    async def list_schedules(limit: int = Query(default=200, ge=1, le=500)) -> dict[str, Any]:
        return {
            "items": [
                schedule.model_dump(mode="json")
                for schedule in scheduler_store.list_schedules(limit=limit)
            ]
        }

    @app.get("/v1/schedules/{schedule_id}", operation_id="getSchedule")
    async def get_schedule(schedule_id: str) -> dict[str, Any]:
        try:
            return scheduler_store.get_schedule(schedule_id).model_dump(mode="json")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="schedule not found") from exc

    @app.post("/v1/schedules/{schedule_id}/status", operation_id="setScheduleStatus")
    async def set_schedule_status(schedule_id: str, body: ScheduleStatusBody) -> dict[str, Any]:
        guard_schedule(
            "status",
            schedule_id,
            body.model_dump(mode="json"),
            f"schedule-status:{schedule_id}:v{body.expected_version}:{body.status.value}",
        )
        try:
            return trigger_service.set_status(
                schedule_id, body.status, expected_version=body.expected_version
            ).model_dump(mode="json")
        except SchedulerConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v1/schedules/{schedule_id}/trigger", operation_id="triggerSchedule")
    async def trigger_schedule(schedule_id: str, body: IdempotentTriggerBody) -> dict[str, Any]:
        guard_schedule(
            "trigger",
            schedule_id,
            {},
            f"schedule-trigger:{schedule_id}:{body.idempotency_key}",
        )
        try:
            request = trigger_service.manual_trigger(
                schedule_id, idempotency_key=body.idempotency_key
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="schedule not found") from exc
        except SchedulerConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return request.model_dump(mode="json")

    @app.get("/v1/scheduler/queue", operation_id="listSchedulerQueue")
    async def list_scheduler_queue(
        status: RunRequestStatus | None = None,
        limit: int = Query(default=200, ge=1, le=500),
    ) -> dict[str, Any]:
        return {
            "items": [
                item.model_dump(mode="json")
                for item in scheduler_store.list_requests(status=status, limit=limit)
            ]
        }

    @app.get("/v1/scheduler/dead-letter", operation_id="listDeadLetter")
    async def list_dead_letter(
        limit: int = Query(default=200, ge=1, le=500),
    ) -> dict[str, Any]:
        return {
            "items": [
                item.model_dump(mode="json")
                for item in scheduler_store.list_requests(
                    status=RunRequestStatus.DEAD_LETTER, limit=limit
                )
            ]
        }

    @app.post(
        "/v1/scheduler/dead-letter/{request_id}/replay",
        operation_id="replayDeadLetter",
    )
    async def replay_dead_letter(request_id: str, body: IdempotentTriggerBody) -> dict[str, Any]:
        guard_schedule(
            "replay",
            request_id,
            {},
            f"dead-letter-replay:{request_id}:{body.idempotency_key}",
        )
        try:
            request = trigger_service.replay_dead_letter(
                request_id, idempotency_key=body.idempotency_key
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run request not found") from exc
        except SchedulerConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return request.model_dump(mode="json")
