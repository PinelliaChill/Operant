from __future__ import annotations

from collections import defaultdict

from operant.application.team import (
    BoardApplyResult,
    MessageProjection,
    ProjectionApplyResult,
    TeamRuntime,
)
from operant.domain.team import (
    ArtifactBoardUpdate,
    ContextEffect,
    MailboxDelivery,
    MessageAck,
    MessageEnvelope,
    MessageKind,
    ModelDelivery,
    RosterEntry,
    TaskBoardUpdate,
    TeamArtifact,
    TeamDefinition,
    TeamRun,
    TeamTask,
)


class FakeTeamRepository:
    """Small contract fake that models required persistence idempotency semantics."""

    def __init__(self) -> None:
        self.messages: dict[str, MessageEnvelope] = {}
        self.projections: dict[str, ProjectionApplyResult] = {}
        self.inboxes: dict[str, list[tuple[MailboxDelivery, MessageEnvelope]]] = defaultdict(list)
        self.acks: dict[str, MessageAck] = {}
        self.task_updates: dict[str, BoardApplyResult] = {}
        self.artifact_updates: dict[str, BoardApplyResult] = {}

    def project_message(
        self, projection: MessageProjection, *, idempotency_key: str
    ) -> ProjectionApplyResult:
        if idempotency_key in self.projections:
            previous = self.projections[idempotency_key]
            return previous.model_copy(update={"created": False})
        result = ProjectionApplyResult(
            message=projection.message,
            outbox=projection.outbox,
            deliveries=projection.deliveries,
            created=True,
        )
        self.projections[idempotency_key] = result
        self.messages[projection.message.message_id] = projection.message
        for delivery in projection.deliveries:
            self.inboxes[delivery.recipient_id].append((delivery, projection.message))
        return result

    def list_inbox(
        self,
        *,
        team_run_id: str,
        recipient_id: str,
        after_cursor: int = 0,
        limit: int = 100,
    ) -> tuple[tuple[MailboxDelivery, MessageEnvelope], ...]:
        del after_cursor
        return tuple(
            item for item in self.inboxes[recipient_id] if item[1].team_run_id == team_run_id
        )[:limit]

    def ack_delivery(self, ack: MessageAck) -> MessageAck:
        return self.acks.setdefault(ack.idempotency_key, ack)

    def apply_task_update(self, update: TaskBoardUpdate) -> BoardApplyResult:
        return self.task_updates.setdefault(
            update.idempotency_key,
            BoardApplyResult(revision=update.task.revision, created=True),
        )

    def apply_artifact_update(self, update: ArtifactBoardUpdate) -> BoardApplyResult:
        return self.artifact_updates.setdefault(
            update.idempotency_key,
            BoardApplyResult(revision=update.artifact.revision, created=True),
        )

    # Unused methods retain the repository protocol shape for runtime checking.
    def put_team_definition(self, definition: TeamDefinition) -> TeamDefinition:
        return definition

    def get_team_definition(self, team_id: str, version: int) -> TeamDefinition | None:
        del team_id, version
        return None

    def put_team_run(self, run: TeamRun) -> TeamRun:
        return run

    def put_team_run_with_roster(self, run: TeamRun, entries: tuple[RosterEntry, ...]) -> TeamRun:
        del entries
        return run

    def get_team_run(self, team_run_id: str) -> TeamRun | None:
        del team_run_id
        return None

    def put_roster_entry(self, entry: RosterEntry) -> RosterEntry:
        return entry

    def list_roster(self, team_run_id: str) -> tuple[RosterEntry, ...]:
        del team_run_id
        return ()

    def get_message(self, message_id: str) -> MessageEnvelope | None:
        return self.messages.get(message_id)

    def list_messages_for_viewer(
        self,
        *,
        team_run_id: str,
        viewer_id: str,
        after_cursor: int = 0,
        limit: int = 100,
    ) -> tuple[tuple[int, MessageEnvelope], ...]:
        del viewer_id, after_cursor
        return tuple(
            (index, message)
            for index, message in enumerate(self.messages.values(), start=1)
            if message.team_run_id == team_run_id
        )[:limit]

    def list_outbox(
        self,
        *,
        team_run_id: str,
        sender_id: str,
        after_cursor: int = 0,
        limit: int = 100,
    ) -> tuple[MessageEnvelope, ...]:
        del after_cursor
        return tuple(
            message
            for message in self.messages.values()
            if message.team_run_id == team_run_id and message.sender_id == sender_id
        )[:limit]

    def list_tasks(self, team_run_id: str) -> tuple[TeamTask, ...]:
        del team_run_id
        return ()

    def list_artifacts(
        self, *, team_run_id: str, viewer_id: str, owner_audit: bool = False
    ) -> tuple[TeamArtifact, ...]:
        del team_run_id, viewer_id, owner_audit
        return ()


def _message(**overrides: object) -> MessageEnvelope:
    values: dict[str, object] = {
        "message_id": "message_1",
        "workflow_run_id": "workflow_1",
        "team_run_id": "team_run_1",
        "sender_id": "agent_sender",
        "recipient_ids": ("agent_a",),
        "message_kind": MessageKind.FINDING,
        "payload": {"summary": "finding"},
    }
    values.update(overrides)
    return MessageEnvelope.model_validate(values)


def test_directed_projection_creates_only_recipient_deliveries() -> None:
    repository = FakeTeamRepository()
    runtime = TeamRuntime(repository)
    message = _message(recipient_ids=("agent_a", "agent_b"))

    result = runtime.send_message(message, idempotency_key="send-1")

    assert result.created is True
    assert result.outbox.message_id == message.message_id
    assert {delivery.recipient_id for delivery in result.deliveries} == {
        "agent_a",
        "agent_b",
    }
    assert repository.inboxes["agent_c"] == []


def test_duplicate_delivery_is_idempotent_and_does_not_duplicate_inbox() -> None:
    repository = FakeTeamRepository()
    runtime = TeamRuntime(repository)
    message = _message()

    first = runtime.send_message(message, idempotency_key="send-1")
    second = runtime.send_message(message, idempotency_key="send-1")

    assert first.created is True
    assert second.created is False
    assert len(repository.inboxes["agent_a"]) == 1


def test_non_recipient_message_never_enters_context_or_consumes_tokens() -> None:
    repository = FakeTeamRepository()
    runtime = TeamRuntime(repository)
    runtime.send_message(_message(), idempotency_key="send-1")

    assert runtime.context_messages(team_run_id="team_run_1", recipient_id="agent_a")
    assert runtime.context_messages(team_run_id="team_run_1", recipient_id="agent_b") == ()


def test_ui_only_message_does_not_enter_model_context() -> None:
    repository = FakeTeamRepository()
    runtime = TeamRuntime(repository)
    message = _message(
        model_delivery=ModelDelivery.NONE,
        context_effect=ContextEffect.NONE,
    )
    runtime.send_message(message, idempotency_key="send-1")

    assert runtime.context_messages(team_run_id="team_run_1", recipient_id="agent_a") == ()


def test_context_projection_keeps_monotonic_inbox_cursor_for_ui_only_message() -> None:
    repository = FakeTeamRepository()
    runtime = TeamRuntime(repository)
    message = _message(
        model_delivery=ModelDelivery.NONE,
        context_effect=ContextEffect.NONE,
    )
    runtime.send_message(message, idempotency_key="send-1")
    delivery, stored = repository.inboxes["agent_a"][0]
    repository.inboxes["agent_a"][0] = (delivery.model_copy(update={"cursor": 9}), stored)

    projection = runtime.project_context(
        team_run_id="team_run_1", recipient_id="agent_a", after_cursor=3
    )

    assert projection.messages == ()
    assert projection.cursor == 9


def test_ack_retry_returns_original_ack_fact() -> None:
    repository = FakeTeamRepository()
    runtime = TeamRuntime(repository)
    first = MessageAck(
        ack_id="ack_1",
        delivery_id="delivery_1",
        message_id="message_1",
        team_run_id="team_run_1",
        recipient_id="agent_a",
        idempotency_key="ack-retry-1",
        cursor=1,
    )
    retry = first.model_copy(update={"ack_id": "ack_2"})

    assert runtime.acknowledge(first).ack_id == "ack_1"
    assert runtime.acknowledge(retry).ack_id == "ack_1"


def test_board_update_retries_are_idempotent() -> None:
    repository = FakeTeamRepository()
    runtime = TeamRuntime(repository)
    task_update = TaskBoardUpdate(
        idempotency_key="task-1",
        task=TeamTask(team_run_id="team_run_1", title="Implement Team"),
    )
    artifact_update = ArtifactBoardUpdate(
        idempotency_key="artifact-1",
        artifact=TeamArtifact(
            artifact_id="artifact_ref_1",
            team_run_id="team_run_1",
            title="Test report",
            media_type="text/plain",
            publisher_id="agent_a",
        ),
    )

    assert runtime.update_task(task_update) == runtime.update_task(task_update)
    assert runtime.publish_artifact(artifact_update) == runtime.publish_artifact(artifact_update)
    assert len(repository.task_updates) == 1
    assert len(repository.artifact_updates) == 1
