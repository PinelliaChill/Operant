from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from operant.domain.memory import Memory, MemoryKind, MemoryStatus  # noqa: F401


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class Effort(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RoleStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class AgentStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class CommandRunnerType(str, Enum):
    HOST = "host"
    DOCKER = "docker"


class CommandExecutionPolicy(BaseModel):
    """Immutable limits for the process that backs ``run_command``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    runner: CommandRunnerType = CommandRunnerType.HOST
    docker_image: str = Field(default="python:3.13-slim", min_length=1, max_length=300)
    cpu_limit: float = Field(default=1.0, gt=0, le=64)
    memory_limit_mb: int = Field(default=512, ge=64, le=262_144)
    pids_limit: int = Field(default=256, ge=16, le=65_536)


class ToolPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed_tools: tuple[str, ...] = ()
    workspace_write: bool = False
    command_execution: bool = False
    approval_required: tuple[str, ...] = (
        "git_write",
        "destructive",
        "network",
        "privileged",
        "shell",
    )
    command_execution_policy: CommandExecutionPolicy = Field(default_factory=CommandExecutionPolicy)

    @field_validator("allowed_tools")
    @classmethod
    def validate_tool_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        known = {
            "read_file",
            "search_files",
            "apply_patch",
            "run_command",
            "git_diff",
        }
        unknown = set(value).difference(known)
        if unknown:
            raise ValueError(f"unknown tools: {sorted(unknown)}")
        if len(value) != len(set(value)):
            raise ValueError("allowed_tools must not contain duplicates")
        return value


class Budget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_turns: int = Field(default=12, ge=1, le=100)
    max_consecutive_test_failures: int = Field(default=2, ge=2, le=10)
    timeout_seconds: int = Field(default=300, ge=1, le=3600)
    max_output_tokens: int | None = Field(default=None, ge=1)
    max_cost_usd: float | None = Field(default=None, gt=0)
    max_tool_calls: int | None = Field(default=None, ge=0)


class EffortMapping(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    effort: Effort
    provider_value: str = Field(min_length=1)


class ModelProfile(BaseModel):
    """Serializable model configuration that contains no credential value."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("model"))
    name: str = Field(min_length=1, max_length=100)
    provider: str = Field(default="openai-compatible", min_length=1, max_length=100)
    model_id: str = Field(min_length=1, max_length=200)
    base_url: str = Field(min_length=1, max_length=2048)
    secret_ref: str = Field(min_length=1, max_length=200)
    context_window: int | None = Field(default=None, ge=1)
    default_token_budget: int | None = Field(default=None, ge=1)
    input_usd_per_million_tokens: float | None = Field(default=None, ge=0)
    output_usd_per_million_tokens: float | None = Field(default=None, ge=0)
    supported_efforts: tuple[Effort, ...] = (Effort.LOW, Effort.MEDIUM, Effort.HIGH)
    default_effort: Effort = Effort.MEDIUM
    effort_parameter: str | None = "reasoning_effort"
    effort_mapping: tuple[EffortMapping, ...] = (
        EffortMapping(effort=Effort.LOW, provider_value="low"),
        EffortMapping(effort=Effort.MEDIUM, provider_value="medium"),
        EffortMapping(effort=Effort.HIGH, provider_value="high"),
    )
    enabled: bool = True
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("base_url")
    @classmethod
    def reject_credentials_in_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError("base_url must be an absolute HTTP(S) URL")
        if parts.username is not None or parts.password is not None:
            raise ValueError("base_url must not contain credentials")
        if parts.query or parts.fragment:
            raise ValueError("base_url must not contain query parameters or fragments")
        return value.rstrip("/")

    @field_validator("secret_ref")
    @classmethod
    def validate_secret_ref(cls, value: str) -> str:
        if not value.replace("_", "").isalnum() or not value[0].isalpha():
            raise ValueError("secret_ref must name an environment variable")
        return value

    @model_validator(mode="after")
    def validate_default_effort(self) -> ModelProfile:
        if self.default_effort not in self.supported_efforts:
            raise ValueError("default_effort must be supported by the model")
        mapped_efforts = [mapping.effort for mapping in self.effort_mapping]
        if len(mapped_efforts) != len(set(mapped_efforts)):
            raise ValueError("effort_mapping must contain unique effort entries")
        if self.effort_parameter is not None and not set(self.supported_efforts).issubset(
            mapped_efforts
        ):
            raise ValueError("effort_mapping must cover every supported effort")
        if (self.input_usd_per_million_tokens is None) != (
            self.output_usd_per_million_tokens is None
        ):
            raise ValueError("input and output token prices must be configured together")
        return self

    def provider_effort_value(self, effort: Effort) -> str | None:
        if self.effort_parameter is None:
            return None
        for mapping in self.effort_mapping:
            if mapping.effort is effort:
                return mapping.provider_value
        raise ValueError(f"no provider effort mapping for {effort.value!r}")


class RolePreset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("role"))
    version: int = Field(default=1, ge=1)
    name: str = Field(min_length=1, max_length=100)
    system_prompt: str = Field(min_length=1)
    model_profile_id: str = Field(min_length=1)
    effort: Effort = Effort.MEDIUM
    tool_policy: ToolPolicy = Field(default_factory=ToolPolicy)
    budget: Budget = Field(default_factory=Budget)
    memory_scope: str = Field(default="session", min_length=1, max_length=100)
    status: RoleStatus = RoleStatus.ACTIVE
    created_at: datetime = Field(default_factory=utc_now)


class SnapshotOverrides(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    effort_overridden: bool = False
    model_profile_overridden: bool = False
    budget_fields: tuple[str, ...] = ()


class RoleSnapshot(BaseModel):
    """Immutable execution fact captured when a session or agent is created."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role_id: str
    role_version: int
    role_name: str
    system_prompt: str
    model_profile_id: str
    model_profile_name: str
    provider: str
    model_id: str
    base_url: str
    secret_ref: str
    # Optional for backward-compatible reads of pre-Phase-1B snapshots. New
    # Sessions freeze the selected ModelProfile value at creation time.
    context_window: int | None = Field(default=None, ge=1)
    input_usd_per_million_tokens: float | None = Field(default=None, ge=0)
    output_usd_per_million_tokens: float | None = Field(default=None, ge=0)
    effort: Effort
    provider_effort_parameter: str | None
    provider_effort_value: str | None
    tool_policy: ToolPolicy
    budget: Budget
    memory_scope: str
    captured_at: datetime = Field(default_factory=utc_now)
    overrides: SnapshotOverrides = Field(default_factory=SnapshotOverrides)

    @model_validator(mode="after")
    def validate_pricing_pair(self) -> RoleSnapshot:
        if (self.input_usd_per_million_tokens is None) != (
            self.output_usd_per_million_tokens is None
        ):
            raise ValueError("input and output token prices must be configured together")
        return self


class Session(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("session"))
    role_snapshot: RoleSnapshot
    created_at: datetime = Field(default_factory=utc_now)


class AgentInstance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("agent"))
    session_id: str
    role_snapshot: RoleSnapshot
    status: AgentStatus = AgentStatus.CREATED
    created_at: datetime = Field(default_factory=utc_now)


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("event"))
    cursor: int | None = Field(default=None, ge=1)
    session_id: str
    agent_id: str | None = None
    event_type: str = Field(min_length=1, max_length=100)
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
