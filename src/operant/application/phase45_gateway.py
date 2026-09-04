from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from operant.application.security import ActionNormalizer, CapabilityBroker, PolicyEngine
from operant.domain.models import new_id
from operant.domain.security import (
    ActionRequest,
    Capability,
    PolicyDecision,
    PolicyEvaluation,
    SecurityAuditEvent,
)
from operant.mcp import (
    GatewayDecision,
    McpAuditFact,
    McpCapabilityLease,
    McpError,
    McpGatewayResult,
)
from operant.persistence.phase45 import SQLitePhase45Repository
from operant.persistence.security import SQLiteSecurityRepository
from operant.persistence.sqlite import ConflictError
from operant.protocol import canonical_action_hash


class Phase45ActionGateway:
    """One Phase 4 policy/capability/audit fence for Phase 5A adapters."""

    def __init__(
        self,
        repository: SQLiteSecurityRepository,
        phase_repository: SQLitePhase45Repository,
        engine: PolicyEngine,
        *,
        principal: str = "core:phase45",
    ) -> None:
        self.repository = repository
        self.phase_repository = phase_repository
        self.engine = engine
        self.principal = principal
        self.normalizer = ActionNormalizer()
        self.broker = CapabilityBroker(repository)
        self._external_actions: dict[str, str] = {}

    def guard(
        self,
        *,
        tool: str,
        operation: str,
        target_id: str,
        arguments: dict[str, Any],
        capabilities: Sequence[Capability],
        idempotency_key: str,
        secret_refs: Sequence[str] = (),
        workspace: str | None = None,
        sandbox_profile: str = "isolated",
        network_profile: str = "none",
    ) -> tuple[ActionRequest, McpGatewayResult, PolicyEvaluation]:
        action = self.normalizer.normalize(
            principal=self.principal,
            tool=tool,
            operation=operation,
            arguments=arguments,
            requested_capabilities=capabilities,
            idempotency_key=idempotency_key,
            policy_version=self.engine.bundle.version,
            workspace=workspace,
            workspace_id=target_id,
            secret_refs=secret_refs,
            sandbox_profile=sandbox_profile,
            network_profile=network_profile,
        )
        action = self.repository.record_security_action(action)
        evaluation = self.engine.evaluate(action)
        self.repository.append_security_audit(
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
        decision = GatewayDecision(evaluation.decision.value)
        approval_id: str | None = None
        if decision is GatewayDecision.ASK:
            approval, created = self.phase_repository.ensure_phase45_approval(
                approval_id=new_id("approval"),
                action_hash=action.action_hash,
                target=action.normalized_target.model_dump(mode="json"),
                policy_version=action.policy_version,
                expires_at=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
            )
            approval_id = str(approval["approval_id"])
            if created:
                self.repository.append_security_audit(
                    SecurityAuditEvent(
                        action_hash=action.action_hash,
                        principal=action.principal,
                        event_type="approval.requested",
                        decision=PolicyDecision.ASK,
                        detail={
                            "approval_id": approval_id,
                            "policy_version": action.policy_version,
                            "expires_at": approval["expires_at"],
                        },
                    )
                )
            if approval["status"] == "approved":
                # Re-evaluate first: a changed policy or a hard DENY can never be
                # overridden by an older human/reviewer decision.
                current = self.engine.evaluate(action)
                if (
                    current.decision is PolicyDecision.ASK
                    and current.policy_version == approval["policy_version"]
                    and self.phase_repository.consume_phase45_approval(
                        action.action_hash, policy_version=current.policy_version
                    )
                ):
                    evaluation = current.model_copy(
                        update={"decision": PolicyDecision.ALLOW, "reason_code": "approval.granted"}
                    )
                    decision = GatewayDecision.ALLOW
                    self.repository.append_security_audit(
                        SecurityAuditEvent(
                            action_hash=action.action_hash,
                            principal=action.principal,
                            event_type="approval.consumed",
                            decision=PolicyDecision.ALLOW,
                            detail={
                                "approval_id": approval_id,
                                "policy_version": current.policy_version,
                            },
                        )
                    )
            elif approval["status"] in {"denied", "expired", "consumed"}:
                evaluation = evaluation.model_copy(
                    update={
                        "decision": PolicyDecision.DENY,
                        "reason_code": f"approval.{approval['status']}",
                    }
                )
                decision = GatewayDecision.DENY
        if decision is not GatewayDecision.ALLOW:
            return (
                action,
                McpGatewayResult(
                    decision=decision,
                    reason_code=evaluation.reason_code,
                    approval_id=approval_id,
                ),
                evaluation,
            )
        leases = tuple(
            self.broker.issue(
                action,
                evaluation,
                capability,
                issued_by="phase45-action-gateway",
                ttl_seconds=60,
                max_uses=1,
            )
            for capability in dict.fromkeys(capabilities)
        )
        lease = leases[0]
        remaining = max(
            0.001,
            (lease.expires_at - datetime.now(timezone.utc)).total_seconds(),
        )
        return (
            action,
            McpGatewayResult(
                decision=GatewayDecision.ALLOW,
                reason_code=evaluation.reason_code,
                lease=McpCapabilityLease(
                    lease_id=lease.lease_id,
                    lease_ids=tuple(item.lease_id for item in leases),
                    action_hash=action.action_hash,
                    security_action_hash=action.action_hash,
                    server_id=target_id,
                    tool_name=operation,
                    expires_at_monotonic=time.monotonic() + remaining,
                    max_uses=lease.max_uses,
                    policy_version=lease.policy_version,
                ),
            ),
            evaluation,
        )

    def consume(self, lease: McpCapabilityLease, action: ActionRequest) -> None:
        try:
            for lease_id in lease.lease_ids or (lease.lease_id,):
                persisted = self.repository.get_capability_lease(lease_id)
                self.broker.consume(
                    lease_id,
                    action=action,
                    capability=persisted.capability,
                )
        except Exception as exc:
            raise McpError("mcp.capability_lease_invalid", "capability lease is invalid") from exc

    async def authorize(
        self,
        *,
        server_id: str,
        tool_name: str,
        action_hash: str,
        target_ref: str,
        schema_sha256: str,
        arguments: Mapping[str, Any],
    ) -> McpGatewayResult:
        expected_hash = canonical_action_hash(
            {
                "kind": "mcp_tool_call",
                "server_id": server_id,
                "target_ref": target_ref,
                "tool_name": tool_name,
                "schema_sha256": schema_sha256,
                "arguments": dict(arguments),
            }
        )
        if expected_hash != action_hash:
            raise McpError("mcp.action_hash_mismatch", "MCP action hash is not canonical")
        capabilities = (
            (Capability.NETWORK_EGRESS,)
            if target_ref.startswith("legacy_sse:")
            else (Capability.PROCESS_EXEC_NO_NETWORK, Capability.WORKSPACE_READ)
        )
        security_action, result, _evaluation = self.guard(
            tool="mcp",
            operation=tool_name,
            target_id=server_id,
            arguments={
                "target_ref": target_ref,
                "schema_sha256": schema_sha256,
                "arguments": dict(arguments),
                "mcp_action_hash": action_hash,
            },
            capabilities=capabilities,
            idempotency_key=f"mcp-call:{action_hash}",
        )
        if result.lease is not None:
            self._external_actions[action_hash] = security_action.action_hash
            result = result.model_copy(
                update={"lease": result.lease.model_copy(update={"action_hash": action_hash})}
            )
        return result

    async def reserve_outcome(
        self,
        *,
        action_hash: str,
        server_id: str,
        tool_name: str,
        schema_sha256: str,
        arguments: Mapping[str, Any],
    ) -> tuple[str, Any | None]:
        arguments_sha256 = canonical_action_hash(dict(arguments))
        try:
            row = self.phase_repository.reserve_mcp_action(
                action_hash=action_hash,
                server_id=server_id,
                tool_name=tool_name,
                schema_sha256=schema_sha256,
                arguments_sha256=arguments_sha256,
            )
        except ConflictError as exc:
            raise McpError(
                "mcp.action_receipt_conflict",
                "MCP action receipt is bound to a different call",
            ) from exc
        replay = None if row["result_json"] is None else json.loads(row["result_json"])
        return str(row["status"]), replay

    async def mark_sent(self, action_hash: str) -> None:
        self.phase_repository.mark_mcp_action_sent(action_hash)

    async def complete_outcome(self, action_hash: str, result: Any) -> None:
        self.phase_repository.complete_mcp_action(action_hash, result)

    async def mark_outcome_unknown(self, action_hash: str, error_code: str) -> None:
        self.phase_repository.mark_mcp_action_unknown(action_hash, error_code)

    async def verify_lease(
        self,
        lease: McpCapabilityLease,
        *,
        action_hash: str,
        server_id: str,
        tool_name: str,
    ) -> None:
        if (
            lease.action_hash != action_hash
            or lease.server_id != server_id
            or lease.tool_name != tool_name
        ):
            raise McpError("mcp.capability_lease_invalid", "capability lease is mismatched")
        action = self.repository.get_security_action(lease.security_action_hash or action_hash)
        self.consume(lease, action)

    async def record_audit(self, fact: McpAuditFact) -> None:
        try:
            action = self.repository.get_security_action(
                self._external_actions.get(fact.action_hash, fact.action_hash)
            )
        except KeyError:
            return
        decision = PolicyDecision.DENY if fact.event_type == "mcp.tool_denied" else None
        self.repository.append_security_audit(
            SecurityAuditEvent(
                action_hash=action.action_hash,
                principal=action.principal,
                event_type=fact.event_type,
                decision=decision,
                detail={
                    key: value
                    for key, value in {
                        "server_id": fact.server_id,
                        "tool_name": fact.tool_name,
                        "policy_version": fact.policy_version,
                        "reason_code": fact.reason_code,
                        "result_sha256": fact.result_sha256,
                        "duration_ms": fact.duration_ms,
                    }.items()
                    if value is not None
                },
            )
        )
        if fact.event_type in {"mcp.tool_completed", "mcp.tool_failed", "mcp.tool_denied"}:
            self._external_actions.pop(fact.action_hash, None)
