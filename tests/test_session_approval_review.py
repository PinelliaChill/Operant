from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest

from operant.application.configuration import ConfigService
from operant.application.security import PolicyEngine
from operant.application.service import ApplicationService, _PersistentActionGateway
from operant.domain.actions import ApprovalStatus
from operant.domain.messages import Message, ModelResponse, ProviderEvent, ToolCall, ToolDefinition
from operant.domain.models import ModelProfile, RolePreset, RoleSnapshot, ToolPolicy
from operant.domain.security import (
    Capability,
    PolicyBundle,
    PolicyDecision,
    PolicyLayer,
    PolicyRule,
    RiskLevel,
)
from operant.persistence.security import SQLiteSecurityRepository
from operant.persistence.sqlite import SQLiteStore
from operant.tools.workspace import ToolError, WorkspaceTools


class SessionAndReviewerProvider:
    def __init__(self, *, review_content: str) -> None:
        self.agent_turn = 0
        self.review_content = review_content
        self.review_messages: Sequence[Message] = ()
        self.review_calls = 0

    async def list_models(self, *, base_url: str, secret_ref: str) -> list[str]:
        return ["independent-model"]

    async def stream(
        self,
        *,
        snapshot: RoleSnapshot,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        if snapshot.role_id == "system:approval-reviewer":
            self.review_calls += 1
            self.review_messages = messages
            assert snapshot.model_profile_id == "approval-profile"
            assert tools == ()
            yield ProviderEvent(
                event_type="model.completed",
                response=ModelResponse(content=self.review_content),
            )
            return
        self.agent_turn += 1
        if self.agent_turn == 1:
            yield ProviderEvent(
                event_type="model.completed",
                response=ModelResponse(
                    tool_calls=(
                        ToolCall(
                            id="git-add-call",
                            name="run_command",
                            arguments_json='{"argv":["git","add","change.txt"]}',
                        ),
                    ),
                    finish_reason="tool_calls",
                ),
            )
            return
        yield ProviderEvent(
            event_type="model.completed",
            response=ModelResponse(content="done", finish_reason="stop"),
        )


def _service(
    tmp_path: Path, provider: SessionAndReviewerProvider
) -> tuple[ApplicationService, str]:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "change.txt").write_text("change\n", encoding="utf-8")
    service = ApplicationService(SQLiteStore(tmp_path / "session-approval.sqlite3"), provider)
    service.initialize()
    main_profile = service.add_model_profile(
        ModelProfile(
            id="session-profile",
            name="Session model",
            model_id="session-model",
            base_url="https://provider.example/v1",
            secret_ref="SESSION_TEST_KEY",
        )
    )
    service.add_model_profile(
        ModelProfile(
            id="approval-profile",
            name="Approval model",
            model_id="independent-model",
            base_url="https://provider.example/v1",
            secret_ref="REVIEWER_TEST_KEY",
        )
    )
    role = service.create_role(
        RolePreset(
            id="session-role",
            name="Session role",
            system_prompt="Run a bounded command.",
            model_profile_id=main_profile.id,
            tool_policy=ToolPolicy(allowed_tools=("run_command",), command_execution=True),
        )
    )
    ConfigService(service.store).put_scope(
        "global",
        "default",
        patch={
            "approval_reviewer": {
                "mode": "auto",
                "profile_id": "approval-profile",
                "strictness": "permissive",
                "custom_rules": ["Allow bounded staging commands."],
            }
        },
        expected_revision=0,
    )
    return service, role.id


@pytest.mark.asyncio
async def test_formal_session_ask_uses_auto_reviewer_and_audits(tmp_path: Path) -> None:
    provider = SessionAndReviewerProvider(
        review_content=json.dumps(
            {"decision": "allow", "reason_code": "bounded_stage", "summary": "Approved staging."}
        )
    )
    service, role_id = _service(tmp_path, provider)
    session = service.create_session(role_id)
    events = [
        event
        async for event in service.run_session(
            session.id, user_message="stage change.txt", workspace=tmp_path
        )
    ]
    assert events[-1].event_type == "agent.completed"
    approval = service.store.list_approval_requests(session.id, status=None)[0]
    assert approval.status is ApprovalStatus.APPROVED
    decision = service.store.get_approval_decision(approval.id)
    assert decision is not None and decision.decided_by == "reviewer"
    assert provider.review_calls == 1
    sent = " ".join(message.content for message in provider.review_messages)
    assert str(tmp_path) not in sent
    assert "change.txt" not in sent
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert staged.stdout.strip() == "change.txt"
    with service.store._connect() as connection:
        action_hash = connection.execute(
            "SELECT action_hash FROM security_action_requests WHERE principal = ?",
            (f"agent:{approval.agent_id}",),
        ).fetchone()["action_hash"]
    audits = SQLiteSecurityRepository(service.store).list_security_audit(action_hash)
    decided = [event for event in audits if event.event_type == "approval.decided"]
    assert len(decided) == 1
    assert decided[0].detail["model_profile_id"] == "approval-profile"
    assert decided[0].decision is PolicyDecision.ALLOW


@pytest.mark.asyncio
async def test_formal_session_model_failure_stays_pending_for_human(tmp_path: Path) -> None:
    provider = SessionAndReviewerProvider(review_content="invalid model response")
    service, role_id = _service(tmp_path, provider)
    session = service.create_session(role_id)
    stream = service.run_session(session.id, user_message="stage change.txt", workspace=tmp_path)
    while True:
        event = await anext(stream)
        if event.event_type == "tool.approval_required":
            break
    pending_next = asyncio.create_task(anext(stream))
    for _ in range(100):
        with service.store._connect() as connection:
            deferred = connection.execute(
                "SELECT 1 FROM security_audit_events "
                "WHERE event_type = 'approval.review_deferred' LIMIT 1"
            ).fetchone()
        if deferred is not None:
            break
        await asyncio.sleep(0.01)
    assert deferred is not None
    approval = service.store.list_approval_requests(session.id, status=None)[0]
    assert approval.status is ApprovalStatus.PENDING
    service.decide_approval(session.id, approval.tool_call_id, approved=True)
    assert (await pending_next).event_type == "tool.approval_decided"
    remaining = [event async for event in stream]
    assert remaining[-1].event_type == "agent.completed"
    assert provider.review_calls == 1
    decision = service.store.get_approval_decision(approval.id)
    assert decision is not None and decision.decided_by == "user"


def test_session_approval_cannot_override_later_hard_deny(tmp_path: Path) -> None:
    provider = SessionAndReviewerProvider(review_content="unused")
    service, role_id = _service(tmp_path, provider)
    session = service.create_session(role_id)
    agent = service.store.create_agent(session.id)
    tools = WorkspaceTools(tmp_path, policy=session.role_snapshot.tool_policy)
    gateway = _PersistentActionGateway(
        store=service.store,
        session_id=session.id,
        agent_id=agent.id,
        tools=tools,
    )
    arguments = {"argv": ["git", "add", "change.txt"]}
    claim = gateway.reserve_tool_action(
        tool_call_id="hard-deny-call", name="run_command", arguments=arguments
    )
    requirement = gateway.approval_requirement(claim)
    assert requirement is not None
    gateway.request_approval(
        claim,
        tool_call_id="hard-deny-call",
        category=requirement[0],
        detail=requirement[1],
    )
    service.store.decide_approval(session.id, "hard-deny-call", approved=True)
    gateway.policy_engine = PolicyEngine(
        PolicyBundle(
            bundle_id="changed-hard-deny",
            version="changed.v2",
            default_decision=PolicyDecision.DENY,
            rules=(
                PolicyRule(
                    rule_id="system.new_hard_deny",
                    layer=PolicyLayer.SYSTEM,
                    decision=PolicyDecision.DENY,
                    capabilities=(Capability.GIT_COMMIT,),
                    risk_level=RiskLevel.HIGH,
                    reason="git writes are now prohibited",
                    hard=True,
                ),
            ),
        )
    )
    with pytest.raises(ToolError, match="current security policy"):
        gateway.verify_approval(
            claim,
            tool_call_id="hard-deny-call",
            name="run_command",
            arguments=arguments,
        )
