from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from operant.domain.models import new_id, utc_now


class ToolActionReceiptStatus(str, Enum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class CommandExecutionStatus(str, Enum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    MANUAL_RECONCILE_REQUIRED = "manual_reconcile_required"


class ToolActionReceipt(BaseModel):
    """Persistent deduplication record without the original action arguments."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("action"))
    scope: str = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=1, max_length=300)
    action_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    command_name: str = Field(min_length=1, max_length=200)
    session_id: str
    agent_id: str
    status: ToolActionReceiptStatus = ToolActionReceiptStatus.IN_PROGRESS
    result_json: str | None = None
    error_code: str | None = Field(default=None, max_length=200)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None


class CommandExecution(BaseModel):
    """REST Command idempotency state, separate from model Tool Call receipts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("command"))
    command_type: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=300)
    action_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: CommandExecutionStatus = CommandExecutionStatus.IN_PROGRESS
    resource_type: str | None = Field(default=None, max_length=100)
    resource_id: str | None = Field(default=None, max_length=300)
    response_json: str | None = None
    http_status: int | None = Field(default=None, ge=100, le=599)
    error_code: str | None = Field(default=None, max_length=200)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None


class ApprovalRequest(BaseModel):
    """Durable approval state with a deliberately sanitized preview."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("approval"))
    session_id: str
    agent_id: str
    tool_action_receipt_id: str
    tool_call_id: str = Field(min_length=1, max_length=300)
    action_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    category: str = Field(min_length=1, max_length=100)
    detail_summary: str = Field(min_length=1, max_length=500)
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime = Field(default_factory=lambda: utc_now() + timedelta(hours=24))
    updated_at: datetime = Field(default_factory=utc_now)
    decided_at: datetime | None = None


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("approval_decision"))
    approval_id: str
    approved: bool
    decided_by: str = Field(default="user", min_length=1, max_length=100)
    reason_code: str | None = Field(default=None, max_length=200)
    decided_at: datetime = Field(default_factory=utc_now)


class ApprovalAuditEvent(BaseModel):
    """Append-only, secret-safe approval audit fact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("approval_audit"))
    approval_id: str
    cursor: int | None = Field(default=None, ge=1)
    event_type: str = Field(min_length=1, max_length=100)
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
