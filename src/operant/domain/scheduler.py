from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_scheduler_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def require_aware_utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


class TriggerKind(str, Enum):
    CRON = "cron"
    TIMER = "timer"


class MisfirePolicy(str, Enum):
    SKIP = "skip"
    FIRE_ONCE = "fire_once"
    CATCH_UP = "catch_up"


class ScheduleStatus(str, Enum):
    ENABLED = "enabled"
    PAUSED = "paused"
    CANCELLED = "cancelled"


class RunRequestStatus(str, Enum):
    QUEUED = "queued"
    LEASED = "leased"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    CANCELLED = "cancelled"
    DEAD_LETTER = "dead_letter"
    MANUAL_RECONCILE_REQUIRED = "manual_reconcile_required"


class AttemptStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


class DispatchIdempotency(str, Enum):
    IDEMPOTENT = "idempotent"
    NON_IDEMPOTENT = "non_idempotent"


class ScheduleDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(
        default_factory=lambda: new_scheduler_id("schedule"), min_length=1, max_length=200
    )
    version: int = Field(default=1, ge=1)
    name: str = Field(min_length=1, max_length=200)
    trigger_kind: TriggerKind
    cron_expression: str | None = Field(default=None, min_length=1, max_length=200)
    timer_at: datetime | None = None
    timezone_name: str = Field(min_length=1, max_length=100)
    misfire_policy: MisfirePolicy = MisfirePolicy.FIRE_ONCE
    max_catch_up: int = Field(default=10, ge=1, le=1_000)
    workflow_id: str = Field(min_length=1, max_length=200)
    workflow_version: int = Field(ge=1)
    workflow_input: dict[str, JsonValue] = Field(default_factory=dict)
    concurrency_limit: int = Field(default=1, ge=1, le=256)
    max_attempts: int = Field(default=3, ge=1, le=20)
    retry_base_seconds: int = Field(default=5, ge=1, le=86_400)
    retry_max_seconds: int = Field(default=300, ge=1, le=604_800)
    dispatch_idempotency: DispatchIdempotency = DispatchIdempotency.IDEMPOTENT
    status: ScheduleStatus = ScheduleStatus.ENABLED
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("timezone_name")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone_name must be an installed IANA timezone") from exc
        return value

    @field_validator("timer_at", "created_at")
    @classmethod
    def validate_timestamps(cls, value: datetime | None, info: object) -> datetime | None:
        if value is None:
            return None
        field_name = getattr(info, "field_name", "timestamp")
        return require_aware_utc(value, field=field_name)

    @model_validator(mode="after")
    def validate_trigger(self) -> ScheduleDefinition:
        if self.trigger_kind is TriggerKind.CRON:
            if self.cron_expression is None or self.timer_at is not None:
                raise ValueError("cron schedules require cron_expression only")
        elif self.timer_at is None or self.cron_expression is not None:
            raise ValueError("timer schedules require timer_at only")
        if self.retry_base_seconds > self.retry_max_seconds:
            raise ValueError("retry_base_seconds cannot exceed retry_max_seconds")
        return self


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_scheduler_id("request"))
    schedule_id: str = Field(min_length=1, max_length=200)
    schedule_version: int = Field(ge=1)
    occurrence_at: datetime
    available_at: datetime
    idempotency_key: str = Field(min_length=1, max_length=300)
    workflow_id: str = Field(min_length=1, max_length=200)
    workflow_version: int = Field(ge=1)
    workflow_input: dict[str, JsonValue] = Field(default_factory=dict)
    replay_of_request_id: str | None = None
    dispatch_idempotency: DispatchIdempotency
    max_attempts: int = Field(ge=1, le=20)
    attempt_count: int = Field(default=0, ge=0, le=20)
    status: RunRequestStatus = RunRequestStatus.QUEUED
    workflow_run_id: str | None = None
    cancel_requested: bool = False
    last_error_code: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("occurrence_at", "available_at", "created_at", "updated_at")
    @classmethod
    def validate_timestamps(cls, value: datetime, info: object) -> datetime:
        return require_aware_utc(value, field=getattr(info, "field_name", "timestamp"))


class SchedulerLease(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["scheduler_leader", "runtime_writer"]
    owner: str = Field(min_length=1, max_length=200)
    token: str = Field(min_length=16, max_length=200)
    fencing: int = Field(ge=1)
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def validate_expires_at(cls, value: datetime) -> datetime:
        return require_aware_utc(value, field="expires_at")


class JobLease(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_request_id: str = Field(min_length=1, max_length=200)
    owner: str = Field(min_length=1, max_length=200)
    token: str = Field(min_length=16, max_length=200)
    fencing: int = Field(ge=1)
    expires_at: datetime
    attempt_number: int = Field(ge=1, le=20)

    @field_validator("expires_at")
    @classmethod
    def validate_expires_at(cls, value: datetime) -> datetime:
        return require_aware_utc(value, field="expires_at")


class JobAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_request_id: str = Field(min_length=1, max_length=200)
    attempt_number: int = Field(ge=1, le=20)
    lease_owner: str
    lease_token: str
    lease_fencing: int = Field(ge=1)
    status: AttemptStatus
    side_effect_started: bool = False
    workflow_run_id: str | None = None
    error_code: str | None = None
    started_at: datetime
    finished_at: datetime | None = None

    @field_validator("started_at", "finished_at")
    @classmethod
    def validate_timestamps(cls, value: datetime | None, info: object) -> datetime | None:
        if value is None:
            return None
        return require_aware_utc(value, field=getattr(info, "field_name", "timestamp"))
