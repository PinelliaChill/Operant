from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_target_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _aware_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


class RemoteTargetStatus(str, Enum):
    REGISTERED = "registered"
    ONLINE = "online"
    OFFLINE = "offline"
    DRAINING = "draining"
    REVOKED = "revoked"


class RemoteJobStatus(str, Enum):
    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    MANUAL_RECONCILE_REQUIRED = "manual_reconcile_required"


class RemoteCapability(str, Enum):
    TARGET_READ = "remote.target.read"
    TARGET_EXEC = "remote.target.exec"
    BROWSER_OBSERVE = "browser.observe"
    BROWSER_NAVIGATE = "browser.navigate"
    BROWSER_SUBMIT = "browser.submit"
    COMPUTER_OBSERVE = "computer.observe"
    COMPUTER_INPUT = "computer.input"
    CLIPBOARD_READ = "computer.clipboard.read"
    CLIPBOARD_WRITE = "computer.clipboard.write"


class RemoteActionIdempotency(str, Enum):
    IDEMPOTENT = "idempotent"
    NON_IDEMPOTENT = "non_idempotent"


class CapabilityManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(min_length=1, max_length=100)
    capabilities: tuple[RemoteCapability, ...]
    supported_operations: tuple[str, ...]
    max_concurrent_jobs: int = Field(default=1, ge=1, le=256)
    max_payload_bytes: int = Field(default=1_048_576, ge=1, le=16_777_216)
    platform: str = Field(min_length=1, max_length=200)


class RemoteTargetRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_id: str = Field(default_factory=lambda: new_target_id("remote_target"))
    display_name: str = Field(min_length=1, max_length=200)
    endpoint_ref: str = Field(min_length=1, max_length=2_000)
    identity_public_key: str = Field(min_length=32, max_length=500)
    credential_ref: str = Field(min_length=1, max_length=300)
    policy_ref: str = Field(min_length=1, max_length=300)
    artifact_namespace: str = Field(min_length=1, max_length=300)
    capability_manifest: CapabilityManifest
    status: RemoteTargetStatus = RemoteTargetStatus.REGISTERED
    fencing: int = Field(default=0, ge=0)
    last_seen_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("last_seen_at", "created_at", "updated_at")
    @classmethod
    def validate_timestamps(cls, value: datetime | None, info: Any) -> datetime | None:
        if value is None:
            return None
        return _aware_utc(value, field_name=info.field_name)


class RemoteTargetLease(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    lease_id: str = Field(default_factory=lambda: new_target_id("remote_target_lease"))
    target_id: str = Field(min_length=1, max_length=200)
    owner: str = Field(min_length=1, max_length=300)
    token: str = Field(min_length=16, max_length=300)
    fencing: int = Field(ge=1)
    workspace_ref: str = Field(min_length=1, max_length=500)
    expires_at: datetime
    released_at: datetime | None = None

    @field_validator("expires_at", "released_at")
    @classmethod
    def validate_timestamps(cls, value: datetime | None, info: Any) -> datetime | None:
        if value is None:
            return None
        return _aware_utc(value, field_name=info.field_name)


class RemoteExecutionJob(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str = Field(default_factory=lambda: new_target_id("remote_job"))
    target_id: str = Field(min_length=1, max_length=200)
    lease_id: str = Field(min_length=1, max_length=200)
    lease_fencing: int = Field(ge=1)
    capability: RemoteCapability
    operation: str = Field(min_length=1, max_length=300)
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    action_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=300)
    idempotency: RemoteActionIdempotency
    status: RemoteJobStatus = RemoteJobStatus.QUEUED
    cancellation_requested: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @field_validator("created_at", "started_at", "finished_at")
    @classmethod
    def validate_timestamps(cls, value: datetime | None, info: Any) -> datetime | None:
        if value is None:
            return None
        return _aware_utc(value, field_name=info.field_name)


class RemoteExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    result_id: str = Field(default_factory=lambda: new_target_id("remote_result"))
    job_id: str = Field(min_length=1, max_length=200)
    result_idempotency_key: str = Field(min_length=1, max_length=300)
    status: RemoteJobStatus
    artifact_ref: str | None = Field(default=None, max_length=500)
    artifact_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    postcondition: dict[str, JsonValue] = Field(default_factory=dict)
    error_code: str | None = Field(default=None, max_length=200)
    completed_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_terminal_status(self) -> RemoteExecutionResult:
        if self.status not in {
            RemoteJobStatus.SUCCEEDED,
            RemoteJobStatus.FAILED,
            RemoteJobStatus.CANCELLED,
            RemoteJobStatus.MANUAL_RECONCILE_REQUIRED,
        }:
            raise ValueError("remote result must use a terminal status")
        if (self.artifact_ref is None) != (self.artifact_sha256 is None):
            raise ValueError("artifact_ref and artifact_sha256 must be supplied together")
        return self


class CapabilityObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    observation_id: str = Field(default_factory=lambda: new_target_id("observation"))
    target_id: str = Field(min_length=1, max_length=200)
    capability: RemoteCapability
    target_ref: str = Field(min_length=1, max_length=500)
    observation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    body: dict[str, JsonValue]
    artifact_ref: str | None = Field(default=None, max_length=500)
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime


class CapabilityActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_id: str = Field(min_length=1, max_length=200)
    capability: RemoteCapability
    operation: str = Field(min_length=1, max_length=300)
    target_ref: str = Field(min_length=1, max_length=500)
    observation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    precondition: dict[str, JsonValue] = Field(default_factory=dict)
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    postcondition: dict[str, JsonValue] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=300)
    idempotency: RemoteActionIdempotency


class CapabilityActionReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    receipt_id: str = Field(default_factory=lambda: new_target_id("capability_receipt"))
    target_id: str
    capability: RemoteCapability
    action_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    observation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: RemoteJobStatus
    result: dict[str, JsonValue] = Field(default_factory=dict)
    error_code: str | None = Field(default=None, max_length=200)
    created_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
