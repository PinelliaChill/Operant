from __future__ import annotations

import asyncio
import inspect
import os
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

from operant.domain.security import (
    ActionRequest,
    Capability,
    CapabilityLease,
    DenialRemediation,
    IdempotencyLevel,
    NormalizedTarget,
    PolicyBundle,
    PolicyDecision,
    PolicyEvaluation,
    PolicyLayer,
    PolicyRule,
    ReviewerDecision,
    RiskLevel,
    SecretLease,
    SecurityAuditEvent,
)
from operant.protocol import redact_public_data, redact_public_text

_SECRET_REF = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
_NETWORK_TOOLS = frozenset({"curl", "wget", "ssh", "scp", "nc"})
_SHELLS = frozenset({"bash", "dash", "fish", "ksh", "pwsh", "sh", "zsh"})
_DESTRUCTIVE = frozenset({"rm", "rmdir", "shred"})


class SecurityError(RuntimeError):
    pass


class PolicyDenied(SecurityError):
    def __init__(self, evaluation: PolicyEvaluation) -> None:
        self.evaluation = evaluation
        super().__init__(evaluation.explanation)


class CapabilityDenied(SecurityError):
    pass


class SecretUnavailable(SecurityError):
    pass


class SecurityRepository(Protocol):
    def record_security_action(self, action: ActionRequest) -> ActionRequest: ...

    def append_security_audit(self, event: SecurityAuditEvent) -> SecurityAuditEvent: ...

    def issue_capability_lease(self, lease: CapabilityLease) -> CapabilityLease: ...

    def consume_capability_lease(
        self,
        lease_id: str,
        *,
        action_hash: str,
        principal: str,
        capability: Capability,
        target_id: str,
        now: datetime,
    ) -> CapabilityLease: ...

    def record_denial_signature(self, signature: str, *, action_hash: str) -> int: ...


class ActionNormalizer:
    """Turn a proposed action into one stable, fully bound security request."""

    def normalize(
        self,
        *,
        principal: str,
        tool: str,
        operation: str,
        arguments: Mapping[str, Any],
        requested_capabilities: Sequence[Capability],
        idempotency_key: str,
        policy_version: str,
        workspace: str | Path | None = None,
        workspace_id: str | None = None,
        session_id: str | None = None,
        workflow_run_id: str | None = None,
        node_run_id: str | None = None,
        agent_instance_id: str | None = None,
        secret_refs: Sequence[str] = (),
        data_classification: str = "internal",
        sandbox_profile: str = "isolated",
        network_profile: str = "none",
        dry_run: bool = False,
    ) -> ActionRequest:
        if not requested_capabilities:
            raise ValueError("at least one capability is required")
        normalized_args = self._normalize_arguments(tool, arguments, workspace)
        target = self._normalize_target(tool, normalized_args, workspace, workspace_id)
        normalized_refs = tuple(sorted(set(secret_refs)))
        if any(_SECRET_REF.fullmatch(ref) is None for ref in normalized_refs):
            raise ValueError("secret_ref must be an environment variable name")
        idempotency_level = self._idempotency_level(tool, operation, normalized_args)
        hash_input = {
            "principal": principal,
            "tool": tool,
            "operation": operation,
            "target": target.model_dump(mode="json"),
            "arguments": normalized_args,
            "workspace_id": workspace_id,
            "requested_capabilities": sorted(item.value for item in requested_capabilities),
            "sandbox_profile": sandbox_profile,
            "network_profile": network_profile,
            "secret_refs": normalized_refs,
            "dry_run": dry_run,
            "idempotency_key": idempotency_key,
            "policy_version": policy_version,
            "idempotency_level": idempotency_level.value,
        }
        return ActionRequest(
            principal=principal,
            session_id=session_id,
            workflow_run_id=workflow_run_id,
            node_run_id=node_run_id,
            agent_instance_id=agent_instance_id,
            tool=tool,
            operation=operation,
            normalized_target=target,
            normalized_arguments=normalized_args,
            workspace_id=workspace_id,
            data_classification=data_classification,
            requested_capabilities=tuple(
                sorted(set(requested_capabilities), key=lambda item: item.value)
            ),
            sandbox_profile=sandbox_profile,
            network_profile=network_profile,
            secret_refs=normalized_refs,
            dry_run=dry_run,
            idempotency_key=idempotency_key,
            action_hash=ActionRequest.calculate_hash(hash_input),
            policy_version=policy_version,
            idempotency_level=idempotency_level,
        )

    @staticmethod
    def _normalize_arguments(
        tool: str, arguments: Mapping[str, Any], workspace: str | Path | None
    ) -> dict[str, Any]:
        normalized = dict(arguments)
        root = None if workspace is None else Path(workspace).resolve(strict=True)
        if "argv" in normalized:
            argv = normalized["argv"]
            if (
                not isinstance(argv, (list, tuple))
                or not argv
                or not all(isinstance(part, str) and "\x00" not in part for part in argv)
            ):
                raise ValueError("argv must be a non-empty string array")
            normalized["argv"] = list(argv)
        for key in ("path", "cwd"):
            if key not in normalized:
                continue
            if root is None:
                raise ValueError(f"workspace is required to normalize {key}")
            raw_path = str(normalized[key])
            candidate = (root / raw_path).resolve(strict=False)
            if candidate != root and root not in candidate.parents:
                raise ValueError(f"{key} escapes the workspace")
            normalized[key] = str(candidate)
        if "url" in normalized:
            parsed = urlsplit(str(normalized["url"]))
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
                raise ValueError("url must be an http(s) endpoint without userinfo")
            port = parsed.port
            netloc = parsed.hostname.lower()
            if port is not None:
                netloc = f"{netloc}:{port}"
            normalized["url"] = urlunsplit(
                (parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, "")
            )
        return normalized

    @staticmethod
    def _normalize_target(
        tool: str,
        arguments: Mapping[str, Any],
        workspace: str | Path | None,
        workspace_id: str | None,
    ) -> NormalizedTarget:
        if "url" in arguments:
            url = str(arguments["url"])
            parsed = urlsplit(url)
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            return NormalizedTarget(
                target_type="network_endpoint",
                target_id=f"{parsed.scheme}://{parsed.hostname}:{port}",
                endpoint=url,
                host=parsed.hostname,
                port=port,
            )
        root = None if workspace is None else str(Path(workspace).resolve(strict=True))
        path = arguments.get("path") or arguments.get("cwd") or root
        return NormalizedTarget(
            target_type="workspace",
            target_id=workspace_id or root or f"tool:{tool}",
            workspace_ref=None if path is None else str(path),
        )

    @staticmethod
    def _idempotency_level(
        tool: str, operation: str, arguments: Mapping[str, Any]
    ) -> IdempotencyLevel:
        if operation in {"read", "list", "search", "diff", "check", "explain", "test"}:
            return IdempotencyLevel.READ_ONLY
        if tool == "run_command":
            argv = arguments.get("argv", [])
            executable = Path(argv[0]).name if argv else ""
            if executable in _NETWORK_TOOLS or executable in _SHELLS:
                return IdempotencyLevel.UNKNOWN
            if executable in {"ruff", "mypy", "pytest", "git"}:
                return IdempotencyLevel.IDEMPOTENT
        if operation in {"delete", "send", "submit", "push", "publish", "pay"}:
            return IdempotencyLevel.NON_IDEMPOTENT
        return IdempotencyLevel.UNKNOWN


class PolicyEngine:
    def __init__(self, bundle: PolicyBundle) -> None:
        self.bundle = bundle

    def evaluate(self, action: ActionRequest) -> PolicyEvaluation:
        matched = tuple(rule for rule in self.bundle.rules if self._matches(rule, action))
        hard = next((rule for rule in matched if rule.hard), None)
        if hard is not None:
            return self._result(action, hard, matched, hard_deny=True)
        decisions: list[PolicyRule] = []
        for capability in action.requested_capabilities:
            candidates = [
                rule for rule in matched if not rule.capabilities or capability in rule.capabilities
            ]
            if candidates:
                denied = [rule for rule in candidates if rule.decision is PolicyDecision.DENY]
                asked = [rule for rule in candidates if rule.decision is PolicyDecision.ASK]
                allowed = [rule for rule in candidates if rule.decision is PolicyDecision.ALLOW]
                if denied:
                    decisions.append(min(denied, key=lambda rule: rule.layer))
                elif asked:
                    decisions.append(min(asked, key=lambda rule: rule.layer))
                elif allowed:
                    decisions.append(min(allowed, key=lambda rule: rule.layer))
                    continue
                else:
                    decisions.append(self._default_rule())
            else:
                decisions.append(self._default_rule())
        denied = [rule for rule in decisions if rule.decision is PolicyDecision.DENY]
        if denied:
            return self._result(action, min(denied, key=lambda rule: rule.layer), matched)
        asked = [rule for rule in decisions if rule.decision is PolicyDecision.ASK]
        if asked:
            return self._result(action, min(asked, key=lambda rule: rule.layer), matched)
        allowed = [rule for rule in decisions if rule.decision is PolicyDecision.ALLOW]
        if allowed:
            return self._result(action, min(allowed, key=lambda rule: rule.layer), matched)
        default_rule = self._default_rule()
        return self._result(action, default_rule, matched)

    def _default_rule(self) -> PolicyRule:
        default_rule = PolicyRule(
            rule_id="bundle.default",
            layer=PolicyLayer.DEFAULT,
            decision=self.bundle.default_decision,
            reason="no policy rule matched",
            risk_level=RiskLevel.HIGH,
        )
        return default_rule

    def explain(self, action: ActionRequest) -> dict[str, Any]:
        result = self.evaluate(action)
        return {
            "evaluation": result.model_dump(mode="json"),
            "layers": [
                {
                    "rule_id": rule.rule_id,
                    "layer": rule.layer.name.lower(),
                    "decision": rule.decision.value,
                    "hard": rule.hard,
                }
                for rule in self.bundle.rules
                if self._matches(rule, action)
            ],
        }

    def test(self, actions: Sequence[ActionRequest]) -> tuple[PolicyEvaluation, ...]:
        return tuple(self.evaluate(action) for action in actions)

    @staticmethod
    def _matches(rule: PolicyRule, action: ActionRequest) -> bool:
        argv = action.normalized_arguments.get("argv")
        executable = (
            Path(argv[0]).name
            if isinstance(argv, list) and argv and isinstance(argv[0], str)
            else None
        )
        return bool(
            (
                not rule.capabilities
                or set(rule.capabilities).intersection(action.requested_capabilities)
            )
            and (not rule.tools or action.tool in rule.tools or executable in rule.tools)
            and (not rule.operations or action.operation in rule.operations)
            and (not rule.target_types or action.normalized_target.target_type in rule.target_types)
        )

    def _result(
        self,
        action: ActionRequest,
        decisive: PolicyRule,
        matched: Sequence[PolicyRule],
        *,
        hard_deny: bool = False,
    ) -> PolicyEvaluation:
        return PolicyEvaluation(
            action_hash=action.action_hash,
            decision=decisive.decision,
            risk_level=decisive.risk_level,
            policy_version=self.bundle.version,
            matched_rule_ids=tuple(rule.rule_id for rule in matched),
            decisive_rule_id=decisive.rule_id,
            reason_code=decisive.rule_id,
            explanation=decisive.reason,
            hard_deny=hard_deny,
            reviewer_eligible=decisive.decision is PolicyDecision.ASK,
        )


def balanced_policy_bundle(version: str = "phase45.v1") -> PolicyBundle:
    return PolicyBundle(
        bundle_id="balanced",
        version=version,
        default_decision=PolicyDecision.DENY,
        rules=(
            PolicyRule(
                rule_id="system.hard.policy-modification",
                layer=PolicyLayer.SYSTEM,
                decision=PolicyDecision.DENY,
                capabilities=(Capability.POLICY_MODIFY,),
                reason="runtime actions cannot modify the security policy",
                risk_level=RiskLevel.CRITICAL,
                hard=True,
            ),
            PolicyRule(
                rule_id="system.hard.production-mutation",
                layer=PolicyLayer.SYSTEM,
                decision=PolicyDecision.DENY,
                capabilities=(Capability.PRODUCTION_MUTATE,),
                reason="production mutation requires a separately governed flow",
                risk_level=RiskLevel.CRITICAL,
                hard=True,
            ),
            PolicyRule(
                rule_id="system.hard.privilege-escalation",
                layer=PolicyLayer.SYSTEM,
                decision=PolicyDecision.DENY,
                tools=("sudo", "doas"),
                reason="privilege escalation is forbidden",
                risk_level=RiskLevel.CRITICAL,
                hard=True,
            ),
            PolicyRule(
                rule_id="workspace.read",
                layer=PolicyLayer.WORKSPACE,
                decision=PolicyDecision.ALLOW,
                capabilities=(Capability.WORKSPACE_READ,),
                reason="workspace-scoped reads are allowed",
                risk_level=RiskLevel.LOW,
            ),
            PolicyRule(
                rule_id="workspace.isolated-write",
                layer=PolicyLayer.WORKSPACE,
                decision=PolicyDecision.ALLOW,
                capabilities=(Capability.WORKSPACE_WRITE, Capability.PROCESS_EXEC_NO_NETWORK),
                reason="isolated workspace edits and no-network execution are allowed",
                risk_level=RiskLevel.MEDIUM,
            ),
            PolicyRule(
                rule_id="workspace.destructive",
                layer=PolicyLayer.WORKSPACE,
                decision=PolicyDecision.ASK,
                capabilities=(Capability.WORKSPACE_DELETE,),
                reason="workspace deletion requires an exact approval",
                risk_level=RiskLevel.HIGH,
            ),
            PolicyRule(
                rule_id="session.network-secret-external",
                layer=PolicyLayer.SESSION,
                decision=PolicyDecision.ASK,
                capabilities=(
                    Capability.NETWORK_EGRESS,
                    Capability.SECRET_USE,
                    Capability.GIT_PUSH,
                    Capability.EXTERNAL_MESSAGE_SEND,
                    Capability.PROCESS_EXEC,
                    Capability.GIT_COMMIT,
                ),
                reason="network, secret, push, and external send actions require review",
                risk_level=RiskLevel.HIGH,
            ),
        ),
    )


ReviewerCallable = Callable[
    [dict[str, Any]],
    ReviewerDecision | Mapping[str, Any] | Awaitable[ReviewerDecision | Mapping[str, Any]],
]


class ApprovalReviewerAdapter:
    def __init__(self, reviewer: ReviewerCallable, *, timeout_seconds: float = 30.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("reviewer timeout must be positive")
        self.reviewer = reviewer
        self.timeout_seconds = timeout_seconds

    async def review(self, action: ActionRequest, evaluation: PolicyEvaluation) -> ReviewerDecision:
        if evaluation.decision is not PolicyDecision.ASK or not evaluation.reviewer_eligible:
            raise PolicyDenied(evaluation)
        minimal = {
            "action_hash": action.action_hash,
            "principal": action.principal,
            "tool": action.tool,
            "operation": action.operation,
            "target_type": action.normalized_target.target_type,
            "target_id": action.normalized_target.target_id,
            "capabilities": [item.value for item in action.requested_capabilities],
            "data_classification": action.data_classification,
            "risk_level": evaluation.risk_level.value,
            "secret_ref_count": len(action.secret_refs),
        }
        try:
            payload = redact_public_data(minimal, max_chars=4000)

            async def invoke_reviewer() -> ReviewerDecision | Mapping[str, Any]:
                candidate = await asyncio.to_thread(self.reviewer, payload)
                if inspect.isawaitable(candidate):
                    candidate = await candidate
                return candidate

            candidate = await asyncio.wait_for(invoke_reviewer(), timeout=self.timeout_seconds)
            return ReviewerDecision.model_validate(candidate)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            return ReviewerDecision(
                decision=PolicyDecision.DENY,
                reason_code="reviewer_fail_closed",
                summary="approval reviewer failed, timed out, or returned invalid output",
            )


class CapabilityBroker:
    def __init__(self, repository: SecurityRepository, *, max_ttl_seconds: int = 300) -> None:
        if max_ttl_seconds < 1:
            raise ValueError("maximum lease TTL must be positive")
        self.repository = repository
        self.max_ttl_seconds = max_ttl_seconds

    def issue(
        self,
        action: ActionRequest,
        evaluation: PolicyEvaluation,
        capability: Capability,
        *,
        issued_by: str,
        ttl_seconds: int = 60,
        max_uses: int = 1,
    ) -> CapabilityLease:
        if (
            evaluation.action_hash != action.action_hash
            or evaluation.policy_version != action.policy_version
        ):
            raise CapabilityDenied("policy evaluation does not bind this exact action")
        if evaluation.decision is not PolicyDecision.ALLOW:
            raise CapabilityDenied("only an allowed action can receive a capability lease")
        if capability not in action.requested_capabilities:
            raise CapabilityDenied("capability was not requested by this action")
        if not 1 <= ttl_seconds <= self.max_ttl_seconds:
            raise CapabilityDenied("capability lease TTL exceeds the configured maximum")
        issued_at = datetime.now().astimezone()
        lease = CapabilityLease(
            action_hash=action.action_hash,
            principal=action.principal,
            capability=capability,
            target=action.normalized_target,
            workspace_id=action.workspace_id,
            constraints={"dry_run": action.dry_run},
            issued_at=issued_at,
            expires_at=issued_at + timedelta(seconds=ttl_seconds),
            max_uses=max_uses,
            policy_version=action.policy_version,
            issued_by=issued_by,
        )
        return self.repository.issue_capability_lease(lease)

    def consume(
        self,
        lease_id: str,
        *,
        action: ActionRequest,
        capability: Capability,
    ) -> CapabilityLease:
        return self.repository.consume_capability_lease(
            lease_id,
            action_hash=action.action_hash,
            principal=action.principal,
            capability=capability,
            target_id=action.normalized_target.target_id,
            now=datetime.now().astimezone(),
        )


@dataclass(frozen=True)
class SecretMaterial:
    lease: SecretLease
    environment: Mapping[str, str]


class SecretBroker:
    """Resolve secret_ref only at execution time and never persist secret values."""

    def __init__(
        self, environment: Mapping[str, str] | None = None, *, max_ttl_seconds: int = 60
    ) -> None:
        self.environment = os.environ if environment is None else environment
        self.max_ttl_seconds = max_ttl_seconds

    def issue(
        self,
        action: ActionRequest,
        evaluation: PolicyEvaluation,
        *,
        secret_ref: str,
        ttl_seconds: int = 30,
    ) -> SecretMaterial:
        if (
            evaluation.decision is not PolicyDecision.ALLOW
            or evaluation.action_hash != action.action_hash
        ):
            raise SecretUnavailable("secret use requires an exact allowed action")
        if (
            Capability.SECRET_USE not in action.requested_capabilities
            or secret_ref not in action.secret_refs
        ):
            raise SecretUnavailable("secret_ref was not authorized for this action")
        if (
            _SECRET_REF.fullmatch(secret_ref) is None
            or not 1 <= ttl_seconds <= self.max_ttl_seconds
        ):
            raise SecretUnavailable("secret request is invalid")
        value = self.environment.get(secret_ref)
        if not value:
            raise SecretUnavailable("secret_ref is unavailable")
        issued_at = datetime.now().astimezone()
        lease = SecretLease(
            secret_ref=secret_ref,
            action_hash=action.action_hash,
            principal=action.principal,
            target_id=action.normalized_target.target_id,
            issued_at=issued_at,
            expires_at=issued_at + timedelta(seconds=ttl_seconds),
        )
        return SecretMaterial(lease=lease, environment={secret_ref: value})

    @staticmethod
    def redact_output(value: str, material: SecretMaterial) -> str:
        redacted = redact_public_text(value)
        for secret in material.environment.values():
            if secret:
                redacted = redacted.replace(secret, "[REDACTED]")
        return redacted


class DenialRemediator:
    def __init__(self, repository: SecurityRepository, *, no_progress_threshold: int = 3) -> None:
        self.repository = repository
        self.no_progress_threshold = no_progress_threshold

    def build(self, action: ActionRequest, evaluation: PolicyEvaluation) -> DenialRemediation:
        signature = ActionRequest.calculate_hash(
            {
                "reason_code": evaluation.reason_code,
                "rule": evaluation.decisive_rule_id,
                "capabilities": sorted(item.value for item in action.requested_capabilities),
                "target_type": action.normalized_target.target_type,
            }
        )
        count = self.repository.record_denial_signature(signature, action_hash=action.action_hash)
        alternatives = (
            ()
            if evaluation.hard_deny
            else (
                "reduce the requested capability set",
                "use a workspace-scoped or no-network target",
                "request a new action with changed, lower-risk parameters",
            )
        )
        return DenialRemediation(
            denied_action_hash=action.action_hash,
            reason_code=evaluation.reason_code,
            policy_rule_id=evaluation.decisive_rule_id,
            denied_capabilities=action.requested_capabilities,
            allowed_scope={"target_type": "workspace", "network_profile": "none"},
            allowed_alternatives=alternatives,
            retry_constraints=("new action_hash required", "risk must be reduced"),
            denial_signature=signature,
            repeated_count=count,
            no_progress=count >= self.no_progress_threshold,
        )
