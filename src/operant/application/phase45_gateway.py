from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from operant.application.security import ActionNormalizer, CapabilityBroker, PolicyEngine
from operant.domain.security import (
    ActionRequest,
    Capability,
    PolicyDecision,
    SecurityAuditEvent,
)
from operant.mcp import (
    GatewayDecision,
    McpAuditFact,
    McpCapabilityLease,
    McpError,
    McpGatewayResult,
)
from operant.persistence.security import SQLiteSecurityRepository
from operant.protocol import canonical_action_hash


class Phase45ActionGateway:
    """One Phase 4 policy/capability/audit fence for Phase 5A adapters."""

    def __init__(
        self,
        repository: SQLiteSecurityRepository,
        engine: PolicyEngine,
        *,
        principal: str = "core:phase45",
    ) -> None:
        self.repository = repository
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
    ) -> tuple[ActionRequest, McpGatewayResult]:
        action = self.normalizer.normalize(
            principal=self.principal,
            tool=tool,
            operation=operation,
            arguments=arguments,
            requested_capabilities=capabilities,
            idempotency_key=idempotency_key,
            policy_version=self.engine.bundle.version,
            workspace_id=target_id,
            secret_refs=secret_refs,
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
        if decision is not GatewayDecision.ALLOW:
            return action, McpGatewayResult(
                decision=decision,
                reason_code=evaluation.reason_code,
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
        return action, McpGatewayResult(
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
            else (Capability.PROCESS_EXEC_NO_NETWORK,)
        )
        security_action, result = self.guard(
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
