from __future__ import annotations

import pytest
from pydantic import ValidationError

from operant.contracts.onboarding import ConversationStart
from operant.protocol import canonical_action_hash


def test_empty_control_permissions_preserve_existing_command_identity() -> None:
    legacy_payload = {
        "workspace_id": "workspace-test",
        "thread_id": None,
        "role_id": None,
        "model_profile_id": None,
        "title": None,
    }
    plain = ConversationStart(workspace_id="workspace-test")
    explicit_empty = ConversationStart(workspace_id="workspace-test", local_control_session_ids=())
    assert plain.command_payload() == explicit_empty.command_payload() == legacy_payload
    assert canonical_action_hash(plain.command_payload()) == canonical_action_hash(legacy_payload)


def test_explicit_control_permissions_change_identity_and_have_no_order_dependency() -> None:
    first = ConversationStart(
        local_control_session_ids=("local-control-browser", "local-control-app")
    )
    reverse = ConversationStart(
        local_control_session_ids=tuple(reversed(first.local_control_session_ids))
    )
    assert first.command_payload() == reverse.command_payload()
    assert canonical_action_hash(first.command_payload()) != canonical_action_hash(
        ConversationStart().command_payload()
    )


@pytest.mark.parametrize(
    "ids",
    [
        ("control-one", "control-one"),
        ("one", "two", "three"),
        ("",),
        ("not a control ID",),
        ("line\nbreak",),
        ("x" * 201,),
    ],
)
def test_control_session_identifiers_are_bounded_and_unambiguous(ids: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError):
        ConversationStart(local_control_session_ids=ids)
