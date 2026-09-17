"""Formal Session integration with a deterministic Provider (not real-model acceptance)."""

from __future__ import annotations

import pytest
from test_b26_experience_skills import _published_procedure, _source, experience  # noqa: F401

from operant.domain.messages import ModelResponse, ProviderEvent
from operant.domain.models import ModelProfile, RolePreset, ToolPolicy
from operant.memory_plugins.experience_runtime import begin_experience_run
from operant.memory_plugins.governance import GovernanceService
from operant.providers.base import ModelProvider


@pytest.mark.asyncio
async def test_formal_session_skill_context_and_revoked_history(experience):  # noqa: F811
    skills, manager, service, project = experience
    source = _source(manager, project)
    procedure = _published_procedure(
        skills, manager, project, source, "检查步骤口令：B26_SKILL_RUNTIME。"
    )
    draft = skills.draft_from_procedure(
        project, procedure, name="runtime-check", description="Runtime check"
    )
    skills.validate(project, draft.skill_id, skill_version=1)
    skills.publish(
        project, draft.skill_id, skill_version=1, expected_head_revision=draft.head.head_revision
    )
    profile = service.add_model_profile(
        ModelProfile(
            name="unit",
            model_id="unit-test-model",
            base_url="https://invalid.test/v1",
            secret_ref="TEST_ONLY",
        )
    )
    role = service.create_role(
        RolePreset(
            name="reader",
            system_prompt="Use authorized skills.",
            model_profile_id=profile.id,
            memory_scope="read: [project]; write: []",
            tool_policy=ToolPolicy(allowed_tools=()),
        )
    )
    session = service.create_session(role.id)
    workspace = service.store.get_workspace_initialization_by_id(
        manager._project(project)["workspace_id"]
    ).workspace_ref

    class Provider(ModelProvider):
        calls = 0

        async def list_models(self, **kwargs):
            return ["unit-test-model"]

        async def stream(self, **kwargs):
            self.calls += 1
            assert "B26_SKILL_RUNTIME" in "\n".join(m.content or "" for m in kwargs["messages"])
            yield ProviderEvent(
                event_type="model.completed",
                response=ModelResponse(content="done", finish_reason="stop"),
            )

    provider = Provider()
    service.provider = provider
    service.memory_manager = manager
    events = [
        e
        async for e in service.run_session(
            session.id, user_message="执行检查步骤", workspace=workspace
        )
    ]
    assert any(e.event_type == "agent.completed" for e in events)
    assert provider.calls == 1
    revisions = service.store.list_context_revisions(session.id)
    assert any("B26_SKILL_RUNTIME" in (m.content or "") for r in revisions for m in r.messages)
    # Same-Session history is reauthorized, while each new Agent gets its own
    # immutable Skill snapshot. Unrestricted history does not break followups.
    followup = [
        e
        async for e in service.run_session(session.id, user_message="继续检查", workspace=workspace)
    ]
    assert any(e.event_type == "agent.completed" for e in followup)
    assert provider.calls == 2
    GovernanceService(manager).revoke_source(project, source)
    # Both an existing run guard and the next agent in the same Session must fail before Provider.
    events = [
        e async for e in service.run_session(session.id, user_message="继续", workspace=workspace)
    ]
    assert not any(e.event_type == "agent.completed" for e in events)
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_disabled_memory_does_not_admit_new_skill_context(experience):  # noqa: F811
    skills, manager, service, project = experience
    source = _source(manager, project)
    ref = _published_procedure(skills, manager, project, source, "检查步骤。")
    draft = skills.draft_from_procedure(
        project, ref, name="disabled-check", description="Disabled test"
    )
    skills.validate(project, draft.skill_id, skill_version=1)
    skills.publish(project, draft.skill_id, skill_version=1)
    profile = service.add_model_profile(
        ModelProfile(
            name="unit",
            model_id="unit-test-model",
            base_url="https://invalid.test/v1",
            secret_ref="TEST_ONLY",
        )
    )
    role = service.create_role(
        RolePreset(
            name="reader", system_prompt="test", model_profile_id=profile.id, memory_scope="project"
        )
    )
    session = service.create_session(role.id)
    agent = service.factory.create_agent(session.id)
    workspace = service.store.get_workspace_initialization_by_id(
        manager._project(project)["workspace_id"]
    ).workspace_ref
    assert (
        begin_experience_run(
            manager,
            workspace=workspace,
            session_id=session.id,
            agent_id=agent.id,
            snapshot=session.role_snapshot,
            enabled=False,
        )
        is None
    )
    admitted = begin_experience_run(
        manager,
        workspace=workspace,
        session_id=session.id,
        agent_id=agent.id,
        snapshot=session.role_snapshot,
        enabled=True,
    )
    assert admitted is not None and admitted.pinned["skills"]
    with manager.store._connect() as connection:
        connection.execute(
            "UPDATE b26_run_context SET agent_id=? WHERE session_id=?",
            ("different_originating_agent", session.id),
        )
    with pytest.raises(PermissionError):
        admitted.guard()
