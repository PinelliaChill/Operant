from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
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
    CapabilityBroker,
    CapabilityDenied,
    DenialRemediator,
    PolicyEngine,
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
    cwd_ref: str | None = Field(default=None, pattern=_SECRET_REF_PATTERN)
    environment_refs: dict[str, str] = Field(default_factory=dict)
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
                or self.endpoint_ref is not None
                or self.secret_ref is not None
            ):
                raise ValueError("stdio requires argv and forbids remote endpoint fields")
        elif (
            self.endpoint_ref is None
            or self.stdio_argv is not None
            or self.cwd_ref is not None
            or self.environment_refs
        ):
            raise ValueError("legacy SSE requires endpoint_ref and forbids stdio fields")
        return self


class McpToolCallBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    arguments: dict[str, Any] = Field(default_factory=dict)


class ScheduleStatusBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ScheduleStatus
    expected_version: int = Field(ge=1)


class IdempotentTriggerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=300)


class EnvironmentReferenceResolver:
    def resolve(self, reference: str) -> str:
        value = os.environ.get(reference)
        if not value:
            raise McpError("mcp.reference_unavailable", "configured reference is unavailable")
        return value


def install_phase45_routes(
    app: FastAPI,
    store: SQLiteStore,
    *,
    skill_roots: Mapping[str, str | Path] | None = None,
    policy_engine: PolicyEngine | None = None,
) -> None:
    repository = SQLiteSecurityRepository(store)
    phase_repository = SQLitePhase45Repository(store)
    scheduler_store = SQLiteSchedulerStore(store.path)
    normalizer = ActionNormalizer()
    engine = policy_engine or PolicyEngine(balanced_policy_bundle())
    broker = CapabilityBroker(repository)
    remediator = DenialRemediator(repository)
    phase_gateway = Phase45ActionGateway(repository, engine)
    reference_resolver = EnvironmentReferenceResolver()
    trusted_skill_roots: dict[str, Path] = {}
    for root_ref, configured_root in (skill_roots or {}).items():
        root = Path(configured_root)
        if not root_ref or len(root_ref) > 200 or not root.is_absolute():
            raise ValueError("configured skill roots require bounded references and absolute paths")
        trusted_skill_roots[root_ref] = root.resolve(strict=True)
    mcp_runtimes: dict[str, McpAdapter] = {}
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
    app.state.scheduler_coordinator = scheduler_coordinator

    async def close_mcp_runtimes() -> None:
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
    ) -> ActionRequest:
        bounded(arguments)
        action, result = phase_gateway.guard(
            tool=tool,
            operation=operation,
            target_id=target_id,
            arguments=arguments,
            capabilities=capabilities,
            idempotency_key=idempotency_key,
            secret_refs=secret_refs,
        )
        if result.decision.value != PolicyDecision.ALLOW.value or result.lease is None:
            status = 403 if result.decision.value == PolicyDecision.DENY.value else 409
            raise HTTPException(status_code=status, detail=result.reason_code)
        phase_gateway.consume(result.lease, action)
        return action

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

    def guard_mcp_config(body: McpServerBody, operation: str) -> None:
        guard_side_effect(
            tool="mcp_config",
            operation=operation,
            target_id=body.server_id,
            arguments={
                "transport": body.transport,
                "endpoint_ref": body.endpoint_ref,
                "secret_ref": body.secret_ref,
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

    @app.get("/v1/mcp/servers/{server_id}/tools", operation_id="listMcpTools")
    async def list_mcp_tools(server_id: str) -> dict[str, Any]:
        try:
            phase_repository.get_mcp_server(server_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="MCP server not found") from exc
        return {"items": list(phase_repository.list_mcp_tools(server_id))}

    def build_transport(config: dict[str, Any]) -> StdioTransport | LegacySseTransport:
        if config["transport"] == "stdio":
            cwd = (
                None if config["cwd_ref"] is None else reference_resolver.resolve(config["cwd_ref"])
            )
            return StdioTransport(
                McpStdioConfig(
                    argv=tuple(config["stdio_argv"]),
                    cwd=cwd,
                    environment_refs=config["environment_refs"],
                ),
                reference_resolver=reference_resolver,
            )
        return LegacySseTransport(
            McpServerConfig(
                server_id=config["server_id"],
                endpoint_ref=config["endpoint_ref"],
                secret_ref=config["secret_ref"],
                allow_loopback_http=config["allow_loopback_http"],
            ),
            reference_resolver=reference_resolver,
        )

    @app.post("/v1/mcp/servers/{server_id}/start", operation_id="startMcpServer")
    async def start_mcp_server(server_id: str) -> dict[str, Any]:
        if server_id in mcp_runtimes:
            raise HTTPException(status_code=409, detail="MCP server is already started")
        try:
            config = phase_repository.get_mcp_server(server_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="MCP server not found") from exc
        capabilities: tuple[Capability, ...] = (
            (Capability.NETWORK_EGRESS,)
            if config["transport"] == "legacy_sse"
            # A host stdio child has no OS-enforced network sandbox. Clearing its
            # environment does not justify the no-network capability.
            else (Capability.PROCESS_EXEC,)
        )
        secret_refs = tuple(config["environment_refs"].values())
        if config["secret_ref"] is not None:
            secret_refs += (config["secret_ref"],)
        if secret_refs:
            capabilities += (Capability.SECRET_USE,)
        guard_side_effect(
            tool="mcp",
            operation="start",
            target_id=server_id,
            arguments={"transport": config["transport"]},
            capabilities=capabilities,
            idempotency_key=f"mcp-start:{server_id}:{config['updated_at']}",
            secret_refs=secret_refs,
        )
        phase_repository.set_mcp_lifecycle(server_id, "starting", "mcp.starting")
        adapter = McpAdapter(
            server_id=server_id,
            target_ref=f"{config['transport']}:{server_id}",
            transport=build_transport(config),
            action_gateway=phase_gateway,
            limits=McpLimits(),
        )
        try:
            tools = await adapter.start()
            snapshot_version = phase_repository.replace_mcp_tools(server_id, tools)
            phase_repository.set_mcp_lifecycle(
                server_id,
                "running",
                "mcp.started",
                detail={"snapshot_version": snapshot_version, "tool_count": len(tools)},
            )
        except Exception as exc:
            phase_repository.set_mcp_lifecycle(
                server_id,
                "failed",
                "mcp.start_failed",
                detail={
                    "error_code": exc.code if isinstance(exc, McpError) else "mcp.start_failed"
                },
            )
            raise HTTPException(status_code=502, detail="MCP server failed to start") from exc
        mcp_runtimes[server_id] = adapter
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
            status = 403 if exc.code in {"mcp.policy_denied", "mcp.approval_required"} else 422
            raise HTTPException(status_code=status, detail=exc.code) from exc
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
