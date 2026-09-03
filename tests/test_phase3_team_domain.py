from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from operant.domain.team import (
    MAX_INLINE_MESSAGE_BYTES,
    ContextEffect,
    MessageEnvelope,
    MessageKind,
    ModelDelivery,
    RosterEntry,
    RosterMemberStatus,
    TeamDefinition,
    TeamMember,
)


def _definition() -> TeamDefinition:
    return TeamDefinition(
        members=(
            TeamMember(
                member_id="coordinator",
                agent_definition_id="agent_coordinator",
                role="coordinator",
                can_coordinate=True,
            ),
            TeamMember(member_id="coder", agent_definition_id="agent_coder", role="coder"),
        ),
        default_coordinator="coordinator",
        max_active_agents=2,
    )


def _message(**overrides: object) -> MessageEnvelope:
    values: dict[str, object] = {
        "workflow_run_id": "workflow_run_1",
        "team_run_id": "team_run_1",
        "sender_id": "agent_sender",
        "recipient_ids": ("agent_recipient",),
        "message_kind": MessageKind.FINDING,
        "payload": {"summary": "bounded finding"},
    }
    values.update(overrides)
    return MessageEnvelope.model_validate(values)


def test_team_definition_requires_unique_members_and_real_coordinator() -> None:
    definition = _definition()
    assert definition.default_coordinator == "coordinator"

    with pytest.raises(ValidationError, match="team member ids must be unique"):
        TeamDefinition(
            members=(definition.members[0], definition.members[0]),
            default_coordinator="coordinator",
            max_active_agents=1,
        )

    with pytest.raises(ValidationError, match="default coordinator must be a team member"):
        TeamDefinition(
            members=definition.members,
            default_coordinator="missing",
            max_active_agents=2,
        )


def test_team_definition_allows_capacity_above_initial_roster() -> None:
    definition = TeamDefinition(
        members=(TeamMember(member_id="coder", agent_definition_id="agent_coder", role="coder"),),
        default_coordinator="coder",
        max_active_agents=3,
    )
    assert definition.max_active_agents == 3


def test_message_enforces_explicit_unique_recipients() -> None:
    with pytest.raises(ValidationError, match="at least one explicit recipient"):
        _message(recipient_ids=())
    with pytest.raises(ValidationError, match="must be unique"):
        _message(recipient_ids=("agent_a", "agent_a"))


def test_message_requires_artifact_for_large_content() -> None:
    with pytest.raises(ValidationError, match="store the content as an Artifact"):
        _message(payload={"log": "x" * (MAX_INLINE_MESSAGE_BYTES + 1)})


def test_non_model_message_cannot_affect_context() -> None:
    with pytest.raises(ValidationError, match="cannot affect model context"):
        _message(model_delivery=ModelDelivery.NONE)

    message = _message(
        model_delivery=ModelDelivery.NONE,
        context_effect=ContextEffect.NONE,
    )
    assert message.context_effect is ContextEffect.NONE


def test_roster_terminal_state_has_explicit_lifecycle_timestamps() -> None:
    now = datetime.now(timezone.utc)
    with pytest.raises(ValidationError, match="require left_at"):
        RosterEntry(
            team_run_id="team_run_1",
            member_id="coder",
            agent_instance_id="agent_1",
            thread_id="thread_1",
            status=RosterMemberStatus.COMPLETED,
            joined_at=now,
        )

    entry = RosterEntry(
        team_run_id="team_run_1",
        member_id="coder",
        agent_instance_id="agent_1",
        thread_id="thread_1",
        status=RosterMemberStatus.COMPLETED,
        joined_at=now,
        left_at=now,
    )
    assert entry.left_at == now
