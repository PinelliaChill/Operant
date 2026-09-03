from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from enum import Enum, IntEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from operant.domain.models import new_id, utc_now


class PolicyDecision(str, Enum):
    DENY = "deny"
    ASK = "ask"
    ALLOW = "allow"


class PolicyLayer(IntEnum):
    SYSTEM = 0
    WORKSPACE = 10
    ROLE = 20
    WORKFLOW = 30
    SESSION = 40
    APPROVAL = 50
    DEFAULT = 60


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Capability(str, Enum):
    WORKSPACE_READ = "workspace.read"
    WORKSPACE_WRITE = "workspace.write"
    WORKSPACE_DELETE = "workspace.delete"
    PROCESS_EXEC = "process.exec"
    PROCESS_EXEC_NO_NETWORK = "process.exec.no_network"
    NETWORK_EGRESS = "network.egress"
    SECRET_USE = "secret.use"
    GIT_COMMIT = "git.commit"
    GIT_PUSH = "git.push"
    EXTERNAL_MESSAGE_SEND = "external.message.send"
    PRODUCTION_MUTATE = "production.mutate"
    POLICY_MODIFY = "policy.modify"


class IdempotencyLevel(str, Enum):
    READ_ONLY = "read_only"
    IDEMPOTENT = "idempotent"
    NON_IDEMPOTENT = "non_idempotent"
    UNKNOWN = "unknown"


class NormalizedTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_type: str = Field(min_length=1, max_length=100)
    target_id: str = Field(min_length=1, max_length=500)
    workspace_ref: str | None = Field(default=None, max_length=4096)
    endpoint: str | None = Field(default=None, max_length=2048)
    host: str | None = Field(default=None, max_length=253)
    port: int | None = Field(default=None, ge=1, le=65535)


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(default_factory=lambda: new_id("security_action"))
    principal: str = Field(min_length=1, max_length=300)
    session_id: str | None = Field(default=None, max_length=300)
    workflow_run_id: str | None = Field(default=None, max_length=300)
    node_run_id: str | None = Field(default=None, max_length=300)
    agent_instance_id: str | None = Field(default=None, max_length=300)
    tool: str = Field(min_length=1, max_length=200)
    operation: str = Field(min_length=1, max_length=200)
    normalized_target: NormalizedTarget
    normalized_arguments: dict[str, Any] = Field(default_factory=dict)
    workspace_id: str | None = Field(default=None, max_length=300)
    data_classification: str = Field(default="internal", max_length=100)
    requested_capabilities: tuple[Capability, ...] = Field(min_length=1, max_length=32)
    sandbox_profile: str = Field(default="isolated", max_length=100)
    network_profile: str = Field(default="none", max_length=100)
    secret_refs: tuple[str, ...] = Field(default=(), max_length=32)
    dry_run: bool = False
    idempotency_key: str = Field(min_length=1, max_length=300)
    action_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_version: str = Field(min_length=1, max_length=100)
    idempotency_level: IdempotencyLevel = IdempotencyLevel.UNKNOWN
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_secret_capability(self) -> ActionRequest:
        if self.secret_refs and Capability.SECRET_USE not in self.requested_capabilities:
            raise ValueError("secret_refs require secret.use capability")
        return self

    @staticmethod
    def calculate_hash(payload: dict[str, Any]) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class PolicyRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str = Field(min_length=1, max_length=200)
    layer: PolicyLayer
    decision: PolicyDecision
    capabilities: tuple[Capability, ...] = ()
    tools: tuple[str, ...] = ()
    operations: tuple[str, ...] = ()
    target_types: tuple[str, ...] = ()
    risk_level: RiskLevel = RiskLevel.MEDIUM
    reason: str = Field(min_length=1, max_length=500)
    hard: bool = False

    @model_validator(mode="after")
    def validate_hard_rule(self) -> PolicyRule:
        if self.hard and (
            self.layer is not PolicyLayer.SYSTEM or self.decision is not PolicyDecision.DENY
        ):
            raise ValueError("only system DENY rules may be hard")
        return self


class PolicyBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    bundle_id: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=100)
    default_decision: PolicyDecision = PolicyDecision.DENY
    rules: tuple[PolicyRule, ...]


class PolicyEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: PolicyDecision
    risk_level: RiskLevel
    policy_version: str
    matched_rule_ids: tuple[str, ...] = ()
    decisive_rule_id: str | None = None
    reason_code: str = Field(min_length=1, max_length=200)
    explanation: str = Field(min_length=1, max_length=1000)
    hard_deny: bool = False
    reviewer_eligible: bool = False


class ReviewerDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: PolicyDecision
    reason_code: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def deny_or_allow_only(self) -> ReviewerDecision:
        if self.decision is PolicyDecision.ASK:
            raise ValueError("reviewer must resolve ASK to ALLOW or DENY")
        return self


class CapabilityLease(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    lease_id: str = Field(default_factory=lambda: new_id("capability_lease"))
    action_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    principal: str = Field(min_length=1, max_length=300)
    capability: Capability
    target: NormalizedTarget
    workspace_id: str | None = Field(default=None, max_length=300)
    constraints: dict[str, Any] = Field(default_factory=dict)
    issued_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime = Field(default_factory=lambda: utc_now() + timedelta(minutes=5))
    max_uses: int = Field(default=1, ge=1, le=100)
    uses: int = Field(default=0, ge=0)
    policy_version: str = Field(min_length=1, max_length=100)
    issued_by: str = Field(min_length=1, max_length=300)
    revoked_at: datetime | None = None

    @model_validator(mode="after")
    def validate_lease(self) -> CapabilityLease:
        if self.expires_at <= self.issued_at:
            raise ValueError("lease expiry must follow issuance")
        if self.uses > self.max_uses:
            raise ValueError("lease use count exceeds max_uses")
        return self


class DenialRemediation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    denied_action_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason_code: str = Field(min_length=1, max_length=200)
    policy_rule_id: str | None = Field(default=None, max_length=200)
    denied_capabilities: tuple[Capability, ...]
    allowed_scope: dict[str, Any] = Field(default_factory=dict)
    allowed_alternatives: tuple[str, ...] = ()
    retry_constraints: tuple[str, ...] = ()
    denial_signature: str = Field(pattern=r"^[0-9a-f]{64}$")
    repeated_count: int = Field(default=1, ge=1)
    no_progress: bool = False


class SecretLease(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    lease_id: str = Field(default_factory=lambda: new_id("secret_lease"))
    secret_ref: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,127}$")
    action_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    principal: str = Field(min_length=1, max_length=300)
    target_id: str = Field(min_length=1, max_length=500)
    issued_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime = Field(default_factory=lambda: utc_now() + timedelta(seconds=60))
    max_uses: int = Field(default=1, ge=1, le=10)

    @model_validator(mode="after")
    def validate_expiry(self) -> SecretLease:
        if self.expires_at <= self.issued_at:
            raise ValueError("secret lease expiry must follow issuance")
        return self


class SecurityAuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(default_factory=lambda: new_id("security_audit"))
    cursor: int | None = Field(default=None, ge=1)
    action_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    principal: str = Field(min_length=1, max_length=300)
    event_type: str = Field(min_length=1, max_length=200)
    decision: PolicyDecision | None = None
    rule_ids: tuple[str, ...] = ()
    detail: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
