from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from operant.application.security import (
    ActionNormalizer,
    CapabilityBroker,
    CapabilityDenied,
    DenialRemediator,
    PolicyEngine,
    balanced_policy_bundle,
)
from operant.domain.security import ActionRequest, Capability, PolicyDecision, SecurityAuditEvent
from operant.persistence.security import SQLiteSecurityRepository
from operant.persistence.sqlite import ConflictError, IdempotencyConflictError, SQLiteStore


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


def install_phase45_routes(app: FastAPI, store: SQLiteStore) -> None:
    repository = SQLiteSecurityRepository(store)
    normalizer = ActionNormalizer()
    engine = PolicyEngine(balanced_policy_bundle())
    broker = CapabilityBroker(repository)
    remediator = DenialRemediator(repository)
    app.state.security_repository = repository
    app.state.policy_engine = engine
    app.state.capability_broker = broker

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
