from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from operant.domain.team import (
    ArtifactBoardUpdate,
    ContextEffect,
    MailboxDelivery,
    MessageAck,
    MessageEnvelope,
    ModelDelivery,
    OutboxItem,
    RosterEntry,
    TaskBoardUpdate,
    TeamArtifact,
    TeamDefinition,
    TeamRun,
    TeamRunStatus,
    TeamTask,
)


class MessageProjection(BaseModel):
    """Pure projection plan; persistence applies the whole plan atomically."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    message: MessageEnvelope
    outbox: OutboxItem
    deliveries: tuple[MailboxDelivery, ...]


class ProjectionApplyResult(BaseModel):
    """Result of an atomic, idempotent message/outbox projection write."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    message: MessageEnvelope
    outbox: OutboxItem
    deliveries: tuple[MailboxDelivery, ...]
    created: bool


class BoardApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: int
    created: bool


class AgentContextProjection(BaseModel):
    """Recipient-only model input plus the monotonic Inbox cursor that was scanned."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    team_run_id: str
    recipient_id: str
    messages: tuple[MessageEnvelope, ...]
    cursor: int


@runtime_checkable
class TeamRepository(Protocol):
    """Persistence boundary for the single-writer local Team runtime.

    Implementations must enforce uniqueness for all idempotency keys and apply
    ``project_message`` atomically. ``list_inbox`` must filter by both Team run
    and recipient before returning data. ``ack_delivery`` must verify that the
    delivery, message, Team run, recipient, and cursor match; a reused key with
    different input is a conflict, while an exact retry returns the first Ack.
    Board writes must enforce ``expected_revision`` and the same conflict rule.
    SQLite remains the recovery authority.
    """

    def put_team_definition(self, definition: TeamDefinition) -> TeamDefinition: ...

    def get_team_definition(self, team_id: str, version: int) -> TeamDefinition | None: ...

    def put_team_run(self, run: TeamRun) -> TeamRun: ...

    def finish_team_run(self, team_run_id: str, status: TeamRunStatus) -> TeamRun: ...

    def put_team_run_with_roster(
        self, run: TeamRun, entries: tuple[RosterEntry, ...]
    ) -> TeamRun: ...

    def get_team_run(self, team_run_id: str) -> TeamRun | None: ...

    def put_roster_entry(self, entry: RosterEntry) -> RosterEntry: ...

    def replace_roster_agent(self, old: RosterEntry, new: RosterEntry) -> RosterEntry: ...

    def list_roster(self, team_run_id: str) -> tuple[RosterEntry, ...]: ...

    def project_message(
        self, projection: MessageProjection, *, idempotency_key: str
    ) -> ProjectionApplyResult: ...

    def get_message(self, message_id: str) -> MessageEnvelope | None: ...

    def list_messages_for_viewer(
        self,
        *,
        team_run_id: str,
        viewer_id: str,
        after_cursor: int = 0,
        limit: int = 100,
    ) -> tuple[tuple[int, MessageEnvelope], ...]: ...

    def list_inbox(
        self,
        *,
        team_run_id: str,
        recipient_id: str,
        after_cursor: int = 0,
        limit: int = 100,
    ) -> tuple[tuple[MailboxDelivery, MessageEnvelope], ...]: ...

    def list_outbox(
        self,
        *,
        team_run_id: str,
        sender_id: str,
        after_cursor: int = 0,
        limit: int = 100,
    ) -> tuple[MessageEnvelope, ...]: ...

    def ack_delivery(self, ack: MessageAck) -> MessageAck: ...

    def apply_task_update(self, update: TaskBoardUpdate) -> BoardApplyResult: ...

    def apply_artifact_update(self, update: ArtifactBoardUpdate) -> BoardApplyResult: ...

    def list_tasks(self, team_run_id: str) -> tuple[TeamTask, ...]: ...

    def list_artifacts(
        self, *, team_run_id: str, viewer_id: str, owner_audit: bool = False
    ) -> tuple[TeamArtifact, ...]: ...


class TeamRuntime:
    """Application coordinator for Team collaboration, not Workflow transitions."""

    def __init__(self, repository: TeamRepository) -> None:
        self.repository = repository

    @staticmethod
    def build_message_projection(message: MessageEnvelope) -> MessageProjection:
        deliveries = tuple(
            MailboxDelivery(
                message_id=message.message_id,
                team_run_id=message.team_run_id,
                recipient_id=recipient_id,
                idempotency_key=f"message:{message.message_id}:recipient:{recipient_id}",
            )
            for recipient_id in message.recipient_ids
        )
        outbox = OutboxItem(
            message_id=message.message_id,
            team_run_id=message.team_run_id,
            sender_id=message.sender_id,
            idempotency_key=f"message:{message.message_id}:outbox",
        )
        return MessageProjection(message=message, outbox=outbox, deliveries=deliveries)

    def send_message(
        self, message: MessageEnvelope, *, idempotency_key: str
    ) -> ProjectionApplyResult:
        """Atomically persist the message and one delivery per explicit recipient."""

        if not idempotency_key.strip():
            raise ValueError("message idempotency key must not be blank")
        return self.repository.project_message(
            self.build_message_projection(message), idempotency_key=idempotency_key
        )

    def context_messages(
        self,
        *,
        team_run_id: str,
        recipient_id: str,
        after_cursor: int = 0,
        limit: int = 100,
    ) -> tuple[MessageEnvelope, ...]:
        """Return only messages that may consume this recipient's next-turn tokens."""

        return self.project_context(
            team_run_id=team_run_id,
            recipient_id=recipient_id,
            after_cursor=after_cursor,
            limit=limit,
        ).messages

    def project_context(
        self,
        *,
        team_run_id: str,
        recipient_id: str,
        after_cursor: int = 0,
        limit: int = 100,
    ) -> AgentContextProjection:
        """Project model input and advance past UI-only deliveries without charging tokens."""

        inbox = self.repository.list_inbox(
            team_run_id=team_run_id,
            recipient_id=recipient_id,
            after_cursor=after_cursor,
            limit=limit,
        )
        messages = tuple(
            message
            for delivery, message in inbox
            if delivery.recipient_id == recipient_id
            and message.is_recipient(recipient_id)
            and message.model_delivery is ModelDelivery.NEXT_TURN
            and message.context_effect is ContextEffect.APPEND_UNTRUSTED
        )
        cursor = max(
            (delivery.cursor or after_cursor for delivery, _message in inbox),
            default=after_cursor,
        )
        return AgentContextProjection(
            team_run_id=team_run_id,
            recipient_id=recipient_id,
            messages=messages,
            cursor=cursor,
        )

    def acknowledge(self, ack: MessageAck) -> MessageAck:
        """Persist an Ack; repository uniqueness makes retries return the first fact."""

        return self.repository.ack_delivery(ack)

    def update_task(self, update: TaskBoardUpdate) -> BoardApplyResult:
        return self.repository.apply_task_update(update)

    def publish_artifact(self, update: ArtifactBoardUpdate) -> BoardApplyResult:
        return self.repository.apply_artifact_update(update)
