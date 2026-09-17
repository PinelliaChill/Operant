"""Focused MP-5.1 checks for procedure-derived Skill lifecycle and guards."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import pytest_asyncio

from operant.api import create_app
from operant.contracts.b2_1 import SourceRef
from operant.contracts.b2_3 import ManagementCommand
from operant.contracts.b2_5 import ExactProposal
from operant.contracts.b2_6_skills import SkillCommand
from operant.domain.threads import ConversationThread, Item, Turn, UserMessagePayload
from operant.memory_plugins.experience_skills import (
    ExperienceSkillService,
    SkillPermissionError,
)
from operant.memory_plugins.governance import GovernanceService, source_key
from operant.memory_plugins.manager import MemoryManager


@pytest_asyncio.fixture
async def experience(tmp_path: Path):
    app = create_app(tmp_path / "core.sqlite3", artifact_root=tmp_path / "artifacts")
    service = app.state.operant_service
    manager = MemoryManager(service)
    project = await manager.execute(
        ManagementCommand(
            action="project_create",
            name="B2-6 experience test",
            workspace_path=str(tmp_path),
        )
    )
    project_id = project.state.projects[-1].project_id
    installed = await manager.execute(
        ManagementCommand(
            action="plugin_install",
            plugin_id="memory-standard",
            mode="trusted_in_process",
        )
    )
    installation_id = installed.state.installations[-1].installation_id
    await manager.execute(
        ManagementCommand(
            action="binding_select",
            project_id=project_id,
            installation_id=installation_id,
        )
    )
    skills = ExperienceSkillService(manager)
    yield skills, manager, service, project_id
    await manager.close()
    service.close()


def _source(manager: MemoryManager, project_id: str) -> SourceRef:
    project = manager._project(project_id)  # noqa: SLF001 - focused Core fixture
    workspace = manager.service.store.get_workspace_initialization_by_id(project["workspace_id"])
    thread = manager.service.create_thread(
        ConversationThread(workspace_ref=workspace.workspace_ref)
    )
    turn = manager.service.create_turn(Turn(thread_id=thread.id))
    item = manager.service.append_item(
        Item(
            thread_id=thread.id,
            turn_id=turn.id,
            payload=UserMessagePayload(text="canonical procedure source"),
        )
    )
    installation = manager.registry.get_installation(project["installation_id"])
    binding = manager.registry.get_binding(installation.binding_id)
    scope = manager._scope(project)  # noqa: SLF001
    return SourceRef(
        source_type="item",
        source_id=item.id,
        revision=item.cursor or 0,
        content_digest=hashlib.sha256(b"canonical procedure source").hexdigest(),
        scope=scope,
        permission_epoch=binding.permission_epoch,
        availability="available",
    )


def _exact(entry: object) -> ExactProposal:
    proposal = entry.proposal  # type: ignore[union-attr]
    return ExactProposal(
        proposal_id=proposal.proposal_id,
        proposal_revision=proposal.proposal_revision,
        proposed_version=proposal.proposed_version,
        base_head_revision=proposal.base_head.revision,
    )


def _published_procedure(
    skills: ExperienceSkillService,
    manager: MemoryManager,
    project_id: str,
    source: SourceRef,
    content: str,
):
    entry = skills.propose_procedure(
        project_id,
        content=content,
        sources=(source,),
    )
    governance = GovernanceService(manager)
    (published,) = governance.review(project_id, _exact(entry), decision="accept")
    return published.version.ref


@pytest.mark.asyncio
async def test_procedure_proposal_and_skill_lifecycle(experience) -> None:
    skills, manager, _service, project_id = experience
    source = _source(manager, project_id)
    proposal_ids = await skills.execute(
        SkillCommand(
            action="procedure_propose",
            project_id=project_id,
            content="Run the migration, then verify the result.",
            sources=(source,),
        )
    )
    proposal = manager.ledger.get_proposal(proposal_ids[0])
    assert proposal.state == "pending"
    governance = GovernanceService(manager)
    entry = next(
        item
        for item in governance.inbox(project_id)
        if item.proposal.proposal_id == proposal_ids[0]
    )
    (published,) = governance.review(project_id, _exact(entry), decision="accept")
    procedure_ref = published.version.ref

    draft = skills.draft_from_procedure(
        project_id,
        procedure_ref,
        name="migration-check",
        description="Run and verify a migration",
    )
    assert draft.head.state == "draft"
    assert draft.version.source_refs == (source,)
    assert draft.version.artifact.resources[0].relative_path == "SKILL.md"
    assert b"allowed-tools" not in skills.artifact_store.read(
        draft.version.artifact.content_hash,
        draft.version.artifact.size_bytes,
    )

    validated = skills.validate(
        project_id,
        draft.skill_id,
        skill_version=draft.version.version,
        expected_head_revision=draft.head.head_revision,
    )
    assert validated.validation is not None
    assert validated.validation.status == "passed"
    published = skills.publish(
        project_id,
        draft.skill_id,
        skill_version=draft.version.version,
        expected_head_revision=validated.head.head_revision,
    )
    assert published.head.state == "published"
    assert published.trust_status == "core_published"

    snapshots = skills.snapshot_for_run(
        project_id,
        session_id="session_1",
        role_id="role_1",
        agent_id="agent_1",
        model_profile_id="model_1",
    )
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.skill_id == draft.skill_id
    skills.assert_snapshot_usable(
        project_id,
        snapshot,
        session_id="session_1",
        role_id="role_1",
        agent_id="agent_1",
        model_profile_id="model_1",
    )
    with pytest.raises(SkillPermissionError, match="stale"):
        skills.assert_snapshot_usable(project_id, snapshot, model_profile_id="other-model")


@pytest.mark.asyncio
async def test_skill_v2_cas_rollback_and_source_revocation(experience) -> None:
    skills, manager, _service, project_id = experience
    source = _source(manager, project_id)
    first_procedure = _published_procedure(
        skills,
        manager,
        project_id,
        source,
        "Use the first procedure and verify A.",
    )
    first = skills.draft_from_procedure(
        project_id,
        first_procedure,
        name="bounded-flow",
        description="Use a bounded flow",
    )
    first = skills.validate(
        project_id,
        first.skill_id,
        skill_version=1,
        expected_head_revision=0,
    )
    first = skills.publish(
        project_id,
        first.skill_id,
        skill_version=1,
        expected_head_revision=0,
    )

    second_procedure = _published_procedure(
        skills,
        manager,
        project_id,
        source,
        "Use the second procedure and verify B.",
    )
    second = skills.draft_from_procedure(
        project_id,
        second_procedure,
        skill_id=first.skill_id,
        expected_head_revision=first.head.head_revision,
        expected_published_version=1,
        name="bounded-flow",
        description="Use a bounded flow",
    )
    assert second.version.version == 2
    draft_state = skills.state(project_id).skills[0]
    assert draft_state.version.version == 2
    assert draft_state.head.published_version == 1
    second = skills.validate(
        project_id,
        second.skill_id,
        skill_version=2,
        expected_head_revision=first.head.head_revision,
    )
    second = skills.publish(
        project_id,
        second.skill_id,
        skill_version=2,
        expected_head_revision=first.head.head_revision,
    )
    assert second.head.published_version == 2
    rolled_back = skills.rollback(
        project_id,
        first.skill_id,
        1,
        skill_version=2,
        expected_head_revision=second.head.head_revision,
    )
    assert rolled_back.head.published_version == 1
    assert rolled_back.rollback_versions == (1, 2)

    snapshot = skills.snapshot_for_run(
        project_id,
        session_id="session_2",
        role_id="role_1",
        agent_id="agent_1",
        model_profile_id="model_1",
    )[0]
    governance = GovernanceService(manager)
    affected = governance.revoke_source(project_id, source, reason="source correction")
    repeated = skills.propagate_source_revocation(
        project_id,
        source_key(source),
        reason="source correction",
    )
    assert f"{first.skill_id}@1" in affected
    assert repeated == ()
    with pytest.raises(SkillPermissionError):
        skills.assert_snapshot_usable(project_id, snapshot)
    assert skills.state(project_id).skills[0].head.state == "revoked"


@pytest.mark.asyncio
async def test_skill_command_cas_fields_and_empty_uninstalled_projection(tmp_path: Path) -> None:
    app = create_app(tmp_path / "empty.sqlite3", artifact_root=tmp_path / "empty-artifacts")
    service = app.state.operant_service
    manager = MemoryManager(service)
    result = await manager.execute(
        ManagementCommand(
            action="project_create",
            name="without memory",
            workspace_path=str(tmp_path),
        )
    )
    project_id = result.state.projects[-1].project_id
    skills = ExperienceSkillService(manager)
    assert skills.state(project_id).skills == ()
    with pytest.raises(ValueError, match="expected_head_revision"):
        SkillCommand(
            action="skill_publish",
            project_id=project_id,
            skill_id="skill_1",
            skill_version=1,
        )
    with pytest.raises(ValueError, match="skill_version"):
        SkillCommand(
            action="skill_disable",
            project_id=project_id,
            skill_id="skill_1",
            expected_head_revision=1,
        )
    await manager.close()
    service.close()
