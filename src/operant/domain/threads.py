from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from operant.domain.models import new_id, utc_now


class ThreadStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ARCHIVED = "archived"


class LegacySourceType(str, Enum):
    SESSION = "session"
    WORKFLOW_RUN = "workflow_run"


class ArtifactSensitivity(str, Enum):
    NORMAL = "normal"
    SENSITIVE = "sensitive"
    RESTRICTED = "restricted"


class ArtifactSourceType(str, Enum):
    THREAD = "thread"
    TURN = "turn"
    ITEM = "item"
    SESSION = "session"
    AGENT = "agent"
    WORKFLOW_RUN = "workflow_run"
    TOOL_CALL = "tool_call"
    TOOL_ACTION_RECEIPT = "tool_action_receipt"
    APPROVAL = "approval"
    EVALUATION_RUN = "evaluation_run"


class ArtifactAccessLevel(str, Enum):
    """Explicit caller capability used for Artifact content operations."""

    NORMAL = "normal"
    SENSITIVE = "sensitive"
    RESTRICTED = "restricted"


class RetentionLifecycle(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    DELETION_SCHEDULED = "deletion_scheduled"
    TRASHED = "trashed"
    DELETED = "deleted"


class CacheHitStatus(str, Enum):
    HIT = "hit"
    MISS = "miss"
    UNKNOWN = "unknown"


class ItemType(str, Enum):
    USER_MESSAGE = "user_message"
    AGENT_MESSAGE = "agent_message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT_REF = "tool_result_ref"
    ARTIFACT_REF = "artifact_ref"
    APPROVAL_LINK = "approval_link"
    STEERING = "steering"
    SYSTEM_EVENT = "system_event"


class ThreadLegacyRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_type: LegacySourceType
    source_id: str = Field(min_length=1, max_length=300)


class ConversationThread(BaseModel):
    """Persistent conversation identity; canonical turns/items live separately."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("thread"))
    cursor: int | None = Field(default=None, ge=1)
    parent_thread_id: str | None = None
    workspace_ref: str | None = Field(default=None, min_length=1, max_length=2048)
    status: ThreadStatus = ThreadStatus.ACTIVE
    legacy_refs: tuple[ThreadLegacyRef, ...] = ()
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    archived_at: datetime | None = None

    @model_validator(mode="after")
    def validate_identity_and_archive_state(self) -> ConversationThread:
        if self.parent_thread_id == self.id:
            raise ValueError("a thread cannot be its own parent")
        if self.status is ThreadStatus.ARCHIVED and self.archived_at is None:
            raise ValueError("an archived thread must have archived_at")
        if self.status is not ThreadStatus.ARCHIVED and self.archived_at is not None:
            raise ValueError("only an archived thread may have archived_at")
        if len(self.legacy_refs) != len(
            {(ref.source_type, ref.source_id) for ref in self.legacy_refs}
        ):
            raise ValueError("thread legacy refs must be unique")
        return self


class Turn(BaseModel):
    """Immutable ordered unit in the canonical history of one Thread."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("turn"))
    cursor: int | None = Field(default=None, ge=1)
    thread_id: str
    position: int | None = Field(default=None, ge=1)
    created_at: datetime = Field(default_factory=utc_now)


class UserMessagePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["user_message"] = "user_message"
    text: str = Field(min_length=1, max_length=100_000)
    author_ref: str | None = Field(default=None, max_length=300)


class AgentMessagePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["agent_message"] = "agent_message"
    text: str = Field(min_length=1, max_length=100_000)
    agent_id: str = Field(min_length=1, max_length=300)


class ToolCallPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["tool_call"] = "tool_call"
    tool_call_id: str = Field(min_length=1, max_length=300)
    tool_name: str = Field(min_length=1, max_length=200)
    action_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    detail_summary: str | None = Field(default=None, max_length=500)


class ToolResultRefPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["tool_result_ref"] = "tool_result_ref"
    tool_call_id: str = Field(min_length=1, max_length=300)
    artifact_id: str = Field(min_length=1, max_length=300)
    outcome: Literal["completed", "failed", "outcome_unknown"]
    summary: str | None = Field(default=None, max_length=500)


class ArtifactRefPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["artifact_ref"] = "artifact_ref"
    artifact_id: str = Field(min_length=1, max_length=300)
    label: str | None = Field(default=None, max_length=300)


class ApprovalLinkPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["approval_link"] = "approval_link"
    approval_id: str = Field(min_length=1, max_length=300)


class SteeringPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["steering"] = "steering"
    text: str = Field(min_length=1, max_length=100_000)
    mode: Literal["queue", "steer"] = "steer"


class SystemEventPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["system_event"] = "system_event"
    event_type: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=1000)
    source_ref: str | None = Field(default=None, max_length=300)


ItemPayload = Annotated[
    UserMessagePayload
    | AgentMessagePayload
    | ToolCallPayload
    | ToolResultRefPayload
    | ArtifactRefPayload
    | ApprovalLinkPayload
    | SteeringPayload
    | SystemEventPayload,
    Field(discriminator="type"),
]


class Item(BaseModel):
    """Immutable append-only canonical history fact within one Turn."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("item"))
    cursor: int | None = Field(default=None, ge=1)
    thread_id: str
    turn_id: str
    position: int | None = Field(default=None, ge=1)
    payload: ItemPayload
    created_at: datetime = Field(default_factory=utc_now)

    @property
    def item_type(self) -> ItemType:
        return ItemType(self.payload.type)


class ArtifactSourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_type: ArtifactSourceType
    source_id: str = Field(min_length=1, max_length=300)


class Artifact(BaseModel):
    """Public artifact metadata; physical storage paths are intentionally absent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("artifact"))
    cursor: int | None = Field(default=None, ge=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=0)
    sensitivity: ArtifactSensitivity = ArtifactSensitivity.NORMAL
    source_refs: tuple[ArtifactSourceRef, ...] = ()
    retention_policy_ref: str = Field(min_length=1, max_length=300)
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_source_refs(self) -> Artifact:
        if len(self.source_refs) != len(
            {(ref.source_type, ref.source_id) for ref in self.source_refs}
        ):
            raise ValueError("artifact source refs must be unique")
        return self


class RetentionPolicy(BaseModel):
    """Named object-level policy. It never performs deletion by itself."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=300)
    object_type: Literal["artifact"] = "artifact"
    grace_period_seconds: int = Field(default=86_400, ge=0, le=31_536_000)
    allow_physical_delete: bool = False
    created_at: datetime = Field(default_factory=utc_now)


class ArtifactRetentionState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str = Field(min_length=1, max_length=300)
    policy_ref: str = Field(min_length=1, max_length=300)
    lifecycle: RetentionLifecycle = RetentionLifecycle.ACTIVE
    pinned: bool = False
    scheduled_deletion_at: datetime | None = None
    trashed_at: datetime | None = None
    deleted_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_lifecycle_timestamps(self) -> ArtifactRetentionState:
        if self.lifecycle is RetentionLifecycle.DELETION_SCHEDULED:
            if self.scheduled_deletion_at is None:
                raise ValueError("scheduled deletion requires a due time")
        elif self.lifecycle is RetentionLifecycle.TRASHED:
            if self.scheduled_deletion_at is None or self.trashed_at is None:
                raise ValueError("trashed Artifact requires schedule and trash timestamps")
        elif self.lifecycle is RetentionLifecycle.DELETED:
            if (
                self.scheduled_deletion_at is None
                or self.trashed_at is None
                or self.deleted_at is None
            ):
                raise ValueError(
                    "deleted Artifact requires schedule, trash, and deletion timestamps"
                )
        elif any(
            timestamp is not None
            for timestamp in (self.scheduled_deletion_at, self.trashed_at, self.deleted_at)
        ):
            raise ValueError("active or archived Artifact cannot carry deletion timestamps")
        if self.pinned and self.lifecycle in {
            RetentionLifecycle.DELETION_SCHEDULED,
            RetentionLifecycle.TRASHED,
            RetentionLifecycle.DELETED,
        }:
            raise ValueError("a pinned Artifact cannot be in a deletion lifecycle")
        return self


class ArtifactAuditFinding(BaseModel):
    """Path-free immutable fact found by a read-only blob/database audit."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    finding_type: Literal[
        "orphan_blob",
        "missing_blob",
        "corrupt_blob",
        "unsafe_entry",
        "purged_blob_present",
    ]
    content_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    artifact_id: str | None = Field(default=None, max_length=300)
    expected_size_bytes: int | None = Field(default=None, ge=0)
    observed_size_bytes: int | None = Field(default=None, ge=0)
    finding_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    repairable: bool = False


class ArtifactAuditReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("artifact_audit"))
    findings: tuple[ArtifactAuditFinding, ...] = ()
    scanned_database_references: int = Field(ge=0)
    scanned_blobs: int = Field(ge=0)
    created_at: datetime = Field(default_factory=utc_now)


class ArtifactRepairResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("artifact_repair"))
    finding_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    action: Literal["delete_orphan_blob"]
    repaired: bool
    outcome: Literal["completed", "refused", "outcome_unknown"]
    created_at: datetime = Field(default_factory=utc_now)


class CacheObservation(BaseModel):
    """Provider cache facts only; no prompt, response, key, or secret value."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("cache_observation"))
    cursor: int | None = Field(default=None, ge=1)
    provider: str = Field(min_length=1, max_length=200)
    model: str = Field(min_length=1, max_length=300)
    request_id: str | None = Field(default=None, max_length=300)
    context_revision_id: str | None = Field(default=None, max_length=300)
    cache_scope: str | None = Field(default=None, max_length=300)
    breakpoint_id: str | None = Field(default=None, max_length=300)
    hit_status: CacheHitStatus = CacheHitStatus.UNKNOWN
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    cache_read_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    cache_key_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    stable_prefix_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    invalidation_reason: (
        Literal[
            "provider_reported",
            "prefix_changed",
            "ttl_expired",
            "model_changed",
            "scope_changed",
            "unknown",
        ]
        | None
    ) = None
    created_at: datetime = Field(default_factory=utc_now)
