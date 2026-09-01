from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from operant.domain.models import new_id, utc_now

PHASE1D_SLASH_REGISTRY_VERSION: Literal["phase1d.v1"] = "phase1d.v1"


class SlashCommandKind(str, Enum):
    WORKSPACE_INIT = "workspace.initialize"
    REVIEW = "review.run"
    CONTEXT_CLEAR = "context.clear"
    CONTEXT_COMPACT = "context.compact"


class SlashCommandDefinition(BaseModel):
    """Versioned public routing metadata; it never grants an execution capability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    registry_version: Literal["phase1d.v1"] = PHASE1D_SLASH_REGISTRY_VERSION
    canonical_name: str = Field(pattern=r"^/[^\s/]+$")
    aliases: tuple[str, ...] = ()
    command_kind: SlashCommandKind
    endpoint: str = Field(pattern=r"^/v1/")
    execution_mode: Literal["json", "sse"] = "json"

    @model_validator(mode="after")
    def validate_aliases(self) -> SlashCommandDefinition:
        names = (self.canonical_name, *self.aliases)
        if any(not name.startswith("/") or any(char.isspace() for char in name) for name in names):
            raise ValueError("slash command aliases must be single slash-prefixed tokens")
        if len(names) != len({name.casefold() for name in names}):
            raise ValueError("slash command aliases must be unique")
        return self


class SlashCommandResolution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    registry_version: Literal["phase1d.v1"] = PHASE1D_SLASH_REGISTRY_VERSION
    canonical_name: str
    command_kind: SlashCommandKind
    arguments: str = Field(default="", max_length=2_000)


class ContextBaselineOperation(str, Enum):
    CLEAR = "clear"
    COMPACT = "compact"


class ContextBaseline(BaseModel):
    """Append-only active-context boundary; Canonical History remains untouched."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("context_baseline"))
    cursor: int | None = Field(default=None, ge=1)
    session_id: str = Field(min_length=1, max_length=300)
    thread_id: str = Field(min_length=1, max_length=300)
    item_cursor_end: int = Field(ge=0)
    operation: ContextBaselineOperation
    compaction_id: str | None = Field(default=None, min_length=1, max_length=300)
    previous_baseline_id: str | None = Field(default=None, min_length=1, max_length=300)
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_operation(self) -> ContextBaseline:
        if self.operation is ContextBaselineOperation.COMPACT and self.compaction_id is None:
            raise ValueError("compact baseline requires a Compaction")
        if self.operation is ContextBaselineOperation.CLEAR and self.compaction_id is not None:
            raise ValueError("clear baseline cannot claim a Compaction")
        return self


class WorkspaceInitialization(BaseModel):
    """Core-side workspace registration; it never stores credentials."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("workspace_init"))
    cursor: int | None = Field(default=None, ge=1)
    workspace_ref: str = Field(min_length=1, max_length=4_096)
    workspace_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    readable: bool
    writable: bool
    created_at: datetime = Field(default_factory=utc_now)


class ReviewRunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ReviewRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("review"))
    cursor: int | None = Field(default=None, ge=1)
    session_id: str = Field(min_length=1, max_length=300)
    thread_id: str | None = Field(default=None, max_length=300)
    workspace_ref: str = Field(min_length=1, max_length=4_096)
    scope: str = Field(default="working tree", min_length=1, max_length=2_000)
    status: ReviewRunStatus = ReviewRunStatus.RUNNING
    artifact_id: str | None = Field(default=None, max_length=300)
    error_code: str | None = Field(default=None, max_length=200)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class BTWSidecarStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PROMOTED = "promoted"


class BTWSidecarRun(BaseModel):
    """A model call isolated from the main Thread's Canonical History."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("btw"))
    cursor: int | None = Field(default=None, ge=1)
    session_id: str = Field(min_length=1, max_length=300)
    agent_id: str = Field(min_length=1, max_length=300)
    thread_id: str = Field(min_length=1, max_length=300)
    workspace_ref: str = Field(min_length=1, max_length=4_096)
    source_item_cursor_end: int = Field(ge=0)
    prompt: str = Field(min_length=1, max_length=100_000)
    prompt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: BTWSidecarStatus = BTWSidecarStatus.RUNNING
    response: str | None = Field(default=None, max_length=100_000)
    response_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    context_revision_id: str | None = Field(default=None, max_length=300)
    promoted_turn_id: str | None = Field(default=None, max_length=300)
    promoted_item_id: str | None = Field(default=None, max_length=300)
    error_code: str | None = Field(default=None, max_length=200)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_terminal_state(self) -> BTWSidecarRun:
        if self.status in {
            BTWSidecarStatus.COMPLETED,
            BTWSidecarStatus.PROMOTED,
        } and (
            self.response is None or self.response_hash is None or self.context_revision_id is None
        ):
            raise ValueError("completed BTW Sidecar requires response and ContextRevision")
        if self.status is BTWSidecarStatus.PROMOTED and (
            self.promoted_turn_id is None or self.promoted_item_id is None
        ):
            raise ValueError("promoted BTW Sidecar requires Steering Item identity")
        return self


class BTWSidecarEvent(BaseModel):
    """Append-only, resource-scoped Sidecar event suitable for SSE replay."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("btw_event"))
    cursor: int | None = Field(default=None, ge=1)
    sidecar_run_id: str = Field(min_length=1, max_length=300)
    event_type: Literal[
        "btw.started",
        "btw.model_completed",
        "btw.failed",
        "btw.promoted",
    ]
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class Phase1DCommandAuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("command_audit"))
    cursor: int | None = Field(default=None, ge=1)
    command_execution_id: str = Field(min_length=1, max_length=300)
    command_kind: SlashCommandKind
    event_type: str = Field(min_length=1, max_length=200)
    resource_type: str | None = Field(default=None, max_length=100)
    resource_id: str | None = Field(default=None, max_length=300)
    detail: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
