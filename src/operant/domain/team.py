from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

MAX_INLINE_MESSAGE_BYTES = 12_000


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class TeamRunStatus(str, Enum):
    CREATED = "created"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CollaborationMode(str, Enum):
    BOUNDED_SUBAGENT = "bounded_subagent"
    TEAM = "team_mode"


class RosterMemberStatus(str, Enum):
    INVITED = "invited"
    ACTIVE = "active"
    IDLE = "idle"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TeamMember(BaseModel):
    """One stable member slot in a versioned Team definition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    member_id: str = Field(min_length=1, max_length=200)
    agent_definition_id: str = Field(min_length=1, max_length=200)
    role: str = Field(min_length=1, max_length=100)
    can_coordinate: bool = False


class TeamDefinition(BaseModel):
    """Versioned collaboration contract; it never describes graph execution order."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    team_id: str = Field(default_factory=lambda: _new_id("team"))
    version: int = Field(default=1, ge=1)
    members: tuple[TeamMember, ...] = Field(min_length=1)
    default_coordinator: str
    mailbox_policy: dict[str, JsonValue] = Field(default_factory=dict)
    task_board_policy: dict[str, JsonValue] = Field(default_factory=dict)
    artifact_visibility: str = Field(default="team", min_length=1, max_length=100)
    message_visibility_policy: str = Field(default="owner_audit", min_length=1, max_length=100)
    max_active_agents: int = Field(default=4, ge=1, le=64)
    max_spawn_depth: int = Field(default=1, ge=0, le=16)

    @model_validator(mode="after")
    def validate_members(self) -> TeamDefinition:
        member_ids = [member.member_id for member in self.members]
        if len(member_ids) != len(set(member_ids)):
            raise ValueError("team member ids must be unique")
        if self.default_coordinator not in member_ids:
            raise ValueError("default coordinator must be a team member")
        return self


class TeamRun(BaseModel):
    """One Team instance bound to a fixed Team definition version and Workflow run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    team_run_id: str = Field(default_factory=lambda: _new_id("team_run"))
    team_id: str
    team_version: int = Field(ge=1)
    workflow_run_id: str
    status: TeamRunStatus = TeamRunStatus.CREATED
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class RosterEntry(BaseModel):
    """Runtime binding from a Team member slot to an isolated Agent instance/thread."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    team_run_id: str
    member_id: str
    agent_instance_id: str
    thread_id: str
    status: RosterMemberStatus = RosterMemberStatus.INVITED
    joined_at: datetime | None = None
    left_at: datetime | None = None

    @model_validator(mode="after")
    def validate_lifecycle(self) -> RosterEntry:
        terminal = {
            RosterMemberStatus.COMPLETED,
            RosterMemberStatus.FAILED,
            RosterMemberStatus.CANCELLED,
        }
        if self.status in terminal and self.left_at is None:
            raise ValueError("terminal roster entries require left_at")
        if self.left_at is not None and self.joined_at is None:
            raise ValueError("left_at requires joined_at")
        return self


class TeamRoster(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    team_run_id: str
    entries: tuple[RosterEntry, ...] = ()

    @model_validator(mode="after")
    def validate_entries(self) -> TeamRoster:
        member_ids = [entry.member_id for entry in self.entries]
        agent_ids = [entry.agent_instance_id for entry in self.entries]
        if len(member_ids) != len(set(member_ids)):
            raise ValueError("roster member ids must be unique")
        if len(agent_ids) != len(set(agent_ids)):
            raise ValueError("roster agent instance ids must be unique")
        if any(entry.team_run_id != self.team_run_id for entry in self.entries):
            raise ValueError("all roster entries must belong to the same Team run")
        return self


class MessageAudience(str, Enum):
    DIRECT = "direct"
    TEAM = "team"


class MessageKind(str, Enum):
    TASK_ASSIGNMENT = "TaskAssignment"
    FINDING = "Finding"
    ARTIFACT_PUBLISHED = "ArtifactPublished"
    DECISION = "Decision"
    BLOCKER = "Blocker"
    QUESTION = "Question"
    APPROVAL_REQUESTED = "ApprovalRequested"
    STATUS_UPDATE = "StatusUpdate"
    COMPLETION = "Completion"


class MessageDeliveryMode(str, Enum):
    DURABLE = "durable"
    BEST_EFFORT = "best_effort"


class MessageVisibility(str, Enum):
    HIDDEN = "hidden"
    RECIPIENTS = "recipients"
    TEAM = "team"
    OWNER_AUDIT = "owner_audit"


class ModelDelivery(str, Enum):
    NONE = "none"
    NEXT_TURN = "next_turn"


class ContextEffect(str, Enum):
    NONE = "none"
    APPEND_UNTRUSTED = "append_untrusted"


class MessageTrustLevel(str, Enum):
    UNTRUSTED = "untrusted"
    SYSTEM = "system"


class MessageStatus(str, Enum):
    PENDING = "pending"
    DELIVERED = "delivered"
    EXPIRED = "expired"


class MessageEnvelope(BaseModel):
    """Authoritative, immutable Team message envelope.

    Graph transitions and Approval decisions intentionally have no fields here.
    An ApprovalRequested message is only a notification; it is not an approval.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: str = Field(default_factory=lambda: _new_id("message"))
    workflow_run_id: str
    team_run_id: str
    sender_id: str
    recipient_ids: tuple[str, ...]
    audience: MessageAudience = MessageAudience.DIRECT
    message_kind: MessageKind
    schema_version: int = Field(default=1, ge=1)
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    artifact_refs: tuple[str, ...] = ()
    reply_to: str | None = None
    causation_id: str | None = None
    correlation_id: str | None = None
    delivery_mode: MessageDeliveryMode = MessageDeliveryMode.DURABLE
    ui_visibility: MessageVisibility = MessageVisibility.RECIPIENTS
    model_delivery: ModelDelivery = ModelDelivery.NEXT_TURN
    audit_visibility: MessageVisibility = MessageVisibility.OWNER_AUDIT
    context_effect: ContextEffect = ContextEffect.APPEND_UNTRUSTED
    trust_level: MessageTrustLevel = MessageTrustLevel.UNTRUSTED
    requires_ack: bool = True
    ttl: int | None = Field(default=None, ge=1)
    status: MessageStatus = MessageStatus.PENDING
    created_at: datetime = Field(default_factory=utc_now)
    delivered_at: datetime | None = None

    @field_validator("recipient_ids")
    @classmethod
    def validate_recipients(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("messages require at least one explicit recipient")
        if any(not recipient.strip() for recipient in value):
            raise ValueError("message recipient ids must not be blank")
        if len(value) != len(set(value)):
            raise ValueError("message recipient ids must be unique")
        return value

    @model_validator(mode="after")
    def validate_delivery_boundary(self) -> MessageEnvelope:
        payload_size = len(
            json.dumps(self.payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        if payload_size > MAX_INLINE_MESSAGE_BYTES:
            raise ValueError(
                "message payload is too large; store the content as an Artifact and send its ref"
            )
        if (
            self.model_delivery is ModelDelivery.NONE
            and self.context_effect is not ContextEffect.NONE
        ):
            raise ValueError("a message excluded from model delivery cannot affect model context")
        if self.trust_level is MessageTrustLevel.SYSTEM and self.sender_id != "system":
            raise ValueError("only the system sender may create trusted system messages")
        if self.status is MessageStatus.DELIVERED and self.delivered_at is None:
            raise ValueError("delivered messages require delivered_at")
        return self

    def is_recipient(self, agent_instance_id: str) -> bool:
        return agent_instance_id in self.recipient_ids


class DeliveryStatus(str, Enum):
    PENDING = "pending"
    DELIVERED = "delivered"
    ACKED = "acked"
    EXPIRED = "expired"


class MailboxDelivery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    delivery_id: str = Field(default_factory=lambda: _new_id("delivery"))
    message_id: str
    team_run_id: str
    recipient_id: str
    idempotency_key: str = Field(min_length=1, max_length=500)
    status: DeliveryStatus = DeliveryStatus.PENDING
    cursor: int | None = Field(default=None, ge=1)
    delivery_attempts: int = Field(default=0, ge=0)
    delivered_at: datetime | None = None
    acked_at: datetime | None = None

    @model_validator(mode="after")
    def validate_timestamps(self) -> MailboxDelivery:
        if (
            self.status in {DeliveryStatus.DELIVERED, DeliveryStatus.ACKED}
            and self.delivered_at is None
        ):
            raise ValueError("delivered mailbox entries require delivered_at")
        if self.status is DeliveryStatus.ACKED and self.acked_at is None:
            raise ValueError("acked mailbox entries require acked_at")
        return self


class MessageAck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ack_id: str = Field(default_factory=lambda: _new_id("ack"))
    delivery_id: str
    message_id: str
    team_run_id: str
    recipient_id: str
    idempotency_key: str = Field(min_length=1, max_length=500)
    cursor: int = Field(ge=1)
    acked_at: datetime = Field(default_factory=utc_now)


class OutboxStatus(str, Enum):
    PENDING = "pending"
    PUBLISHED = "published"


class OutboxItem(BaseModel):
    """Durable publication intent written atomically with its authoritative message."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outbox_id: str = Field(default_factory=lambda: _new_id("outbox"))
    message_id: str
    team_run_id: str
    sender_id: str
    idempotency_key: str = Field(min_length=1, max_length=500)
    status: OutboxStatus = OutboxStatus.PENDING
    cursor: int | None = Field(default=None, ge=1)
    created_at: datetime = Field(default_factory=utc_now)
    published_at: datetime | None = None

    @model_validator(mode="after")
    def validate_publication(self) -> OutboxItem:
        if self.status is OutboxStatus.PUBLISHED and self.published_at is None:
            raise ValueError("published outbox items require published_at")
        return self


class TeamTaskStatus(str, Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class TeamTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(default_factory=lambda: _new_id("team_task"))
    team_run_id: str
    title: str = Field(min_length=1, max_length=500)
    summary: str = Field(default="", max_length=4_000)
    assignee_ids: tuple[str, ...] = ()
    status: TeamTaskStatus = TeamTaskStatus.OPEN
    artifact_refs: tuple[str, ...] = ()
    source_message_id: str | None = None
    revision: int = Field(default=1, ge=1)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("assignee_ids")
    @classmethod
    def unique_assignees(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("task assignee ids must be unique")
        return value


class TeamArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str
    team_run_id: str
    title: str = Field(min_length=1, max_length=500)
    media_type: str = Field(min_length=1, max_length=200)
    publisher_id: str
    visibility: MessageVisibility = MessageVisibility.TEAM
    recipient_ids: tuple[str, ...] = ()
    source_message_id: str | None = None
    revision: int = Field(default=1, ge=1)
    published_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_visibility(self) -> TeamArtifact:
        if self.visibility is MessageVisibility.RECIPIENTS and not self.recipient_ids:
            raise ValueError("recipient-visible artifacts require explicit recipients")
        if len(self.recipient_ids) != len(set(self.recipient_ids)):
            raise ValueError("artifact recipient ids must be unique")
        return self


class TaskBoardUpdate(BaseModel):
    """Idempotent Task Board mutation requested through a persisted command handler."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    idempotency_key: str = Field(min_length=1, max_length=500)
    task: TeamTask
    expected_revision: int | None = Field(default=None, ge=0)


class ArtifactBoardUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    idempotency_key: str = Field(min_length=1, max_length=500)
    artifact: TeamArtifact
    expected_revision: int | None = Field(default=None, ge=0)


class MailboxProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    team_run_id: str
    recipient_id: str
    inbox: tuple[MailboxDelivery, ...] = ()
    cursor: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_inbox(self) -> MailboxProjection:
        if any(delivery.team_run_id != self.team_run_id for delivery in self.inbox):
            raise ValueError("all inbox deliveries must belong to the same Team run")
        if any(delivery.recipient_id != self.recipient_id for delivery in self.inbox):
            raise ValueError("mailbox may only contain its recipient's deliveries")
        return self


class TaskBoardProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    team_run_id: str
    tasks: tuple[TeamTask, ...] = ()

    @model_validator(mode="after")
    def validate_tasks(self) -> TaskBoardProjection:
        if any(task.team_run_id != self.team_run_id for task in self.tasks):
            raise ValueError("all tasks must belong to the same Team run")
        return self


class ArtifactBoardProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    team_run_id: str
    artifacts: tuple[TeamArtifact, ...] = ()

    @model_validator(mode="after")
    def validate_artifacts(self) -> ArtifactBoardProjection:
        if any(artifact.team_run_id != self.team_run_id for artifact in self.artifacts):
            raise ValueError("all artifacts must belong to the same Team run")
        return self
