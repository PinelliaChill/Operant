from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from unittest.mock import Mock

import pytest

from operant.application.approval_review import (
    ApprovalModelReviewer,
    ReviewerConfig,
    minimal_review_facts,
)
from operant.application.phase45_gateway import Phase45ActionGateway
from operant.application.security import ActionNormalizer, PolicyEngine, balanced_policy_bundle
from operant.domain.messages import Message, ModelResponse, ProviderEvent, ToolDefinition
from operant.domain.models import ModelProfile, RoleSnapshot
from operant.domain.security import Capability, PolicyDecision


class FakeProvider:
    def __init__(self, answer: str, *, discovered: bool = True) -> None:
        self.answer = answer
        self.discovered = discovered
        self.messages: Sequence[Message] = ()
        self.snapshots: list[RoleSnapshot] = []
        self.calls = 0

    async def list_models(self, *, base_url: str, secret_ref: str) -> list[str]:
        self.calls += 1
        return ["discovered-model"] if self.discovered else []

    async def stream(
        self,
        *,
        snapshot: RoleSnapshot,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        self.messages = messages
        self.snapshots.append(snapshot)
        assert not tools
        yield ProviderEvent(
            event_type="model.completed", response=ModelResponse(content=self.answer)
        )


def _profile() -> ModelProfile:
    return ModelProfile(
        id="independent-reviewer",
        name="Approval reviewer",
        model_id="discovered-model",
        base_url="https://provider.example/v1",
        secret_ref="REVIEWER_TEST_KEY",
    )


def _asked(tmp_path: Path):
    action = ActionNormalizer().normalize(
        principal="agent:private-identity",
        tool="remote_api",
        operation="send",
        arguments={"url": "https://example.invalid/private?token=sensitive-value"},
        requested_capabilities=(Capability.NETWORK_EGRESS,),
        idempotency_key="review-1",
        policy_version="phase45.v1",
        workspace=tmp_path,
    )
    evaluation = PolicyEngine(balanced_policy_bundle()).evaluate(action)
    assert evaluation.decision is PolicyDecision.ASK
    return action, evaluation


@pytest.mark.asyncio
async def test_reviewer_uses_discovered_independent_profile_and_minimal_input(
    tmp_path: Path,
) -> None:
    action, evaluation = _asked(tmp_path)
    provider = FakeProvider(
        json.dumps({"decision": "allow", "reason_code": "bounded_action", "summary": "Allowed."})
    )
    reviewer = ApprovalModelReviewer(provider=provider, get_profile=lambda _: _profile())
    outcome = await reviewer.review(
        action,
        evaluation,
        ReviewerConfig(mode="auto", profile_id="independent-reviewer", strictness="permissive"),
    )
    assert outcome.decision is not None
    assert outcome.decision.decision is PolicyDecision.ALLOW
    assert outcome.model_id == "discovered-model"
    assert provider.snapshots[0].model_profile_id == "independent-reviewer"
    sent = " ".join(message.content for message in provider.messages)
    assert "private-identity" not in sent
    assert "sensitive-value" not in sent
    assert "example.invalid" not in sent
    assert "normalized_arguments" not in sent
    assert minimal_review_facts(action, evaluation)["target_fingerprint"] in sent


@pytest.mark.asyncio
async def test_reviewer_requires_human_for_risk_model_failure_and_hard_deny(tmp_path: Path) -> None:
    action, evaluation = _asked(tmp_path)
    provider = FakeProvider('{"decision":"allow"}')
    reviewer = ApprovalModelReviewer(provider=provider, get_profile=lambda _: _profile())
    config = ReviewerConfig(mode="auto", profile_id="independent-reviewer")
    cautious = await reviewer.review(action, evaluation, config)
    assert cautious.decision is None
    assert cautious.reason_code == "review_requires_human"
    assert provider.calls == 0

    invalid = await reviewer.review(
        action, evaluation, config.model_copy(update={"strictness": "permissive"})
    )
    assert invalid.decision is None
    assert invalid.reason_code == "review_unavailable"

    provider.discovered = False
    missing = await reviewer.review(
        action, evaluation, config.model_copy(update={"strictness": "permissive"})
    )
    assert missing.decision is None
    assert missing.reason_code == "review_model_not_discovered"

    hard = evaluation.model_copy(update={"decision": PolicyDecision.DENY, "hard_deny": True})
    denied = await reviewer.review(action, hard, config)
    assert denied.decision is None
    assert denied.reason_code == "review_not_eligible"

    off = await reviewer.review(action, evaluation, ReviewerConfig(mode="off"))
    assert off.decision is None
    assert off.reason_code == "review_disabled"


def test_reviewer_config_rejects_missing_profile_and_oversized_rules() -> None:
    with pytest.raises(ValueError, match="model profile"):
        ReviewerConfig(mode="auto")
    with pytest.raises(ValueError, match="approval rules"):
        ReviewerConfig(custom_rules=("x" * 501,))


def test_phase45_gateway_notifies_only_pending_ask(tmp_path: Path) -> None:
    requested: list[str] = []
    security_repository = Mock()
    security_repository.record_security_action.side_effect = lambda action: action
    phase_repository = Mock()
    phase_repository.ensure_phase45_approval.return_value = (
        {"approval_id": "approval-1", "status": "pending", "expires_at": "later"},
        False,
    )
    gateway = Phase45ActionGateway(
        security_repository,
        phase_repository,
        PolicyEngine(balanced_policy_bundle()),
        on_approval_requested=requested.append,
    )
    _, result, _ = gateway.guard(
        tool="remote_api",
        operation="send",
        target_id="target-1",
        arguments={"url": "https://example.invalid/path"},
        capabilities=(Capability.NETWORK_EGRESS,),
        idempotency_key="ask-1",
    )
    assert result.decision.value == "ask"
    assert requested == ["approval-1"]

    _, denied, _ = gateway.guard(
        tool="remote_api",
        operation="send",
        target_id="target-1",
        arguments={"url": "https://example.invalid/path"},
        capabilities=(Capability.PRODUCTION_MUTATE,),
        idempotency_key="deny-1",
    )
    assert denied.decision.value == "deny"
    assert requested == ["approval-1"]
