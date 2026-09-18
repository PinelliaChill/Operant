"""B2-7 compatibility reads must retain the plugin authorization boundary."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import pytest_asyncio

from operant.api import create_app
from operant.contracts.b2_3 import ManagementCommand
from operant.domain.memory import Memory, MemoryKind, MemoryStatus
from operant.domain.models import AgentStatus, ModelProfile, RolePreset
from operant.memory_plugins.manager import MemoryManager


@pytest_asyncio.fixture
async def compat_environment(tmp_path: Path):
    app = create_app(tmp_path / "core.sqlite3")
    service = app.state.operant_service
    manager = MemoryManager(service)
    service.memory_manager = manager

    async def command(**kwargs):
        return await manager.execute(ManagementCommand(**kwargs))

    project = (
        (await command(action="project_create", name="compat", workspace_path=str(tmp_path)))
        .state.projects[-1]
        .project_id
    )
    installation = (
        (
            await command(
                action="plugin_install", plugin_id="memory-standard", mode="trusted_in_process"
            )
        )
        .state.installations[-1]
        .installation_id
    )
    await command(action="binding_select", project_id=project, installation_id=installation)
    profile = service.add_model_profile(
        ModelProfile(
            name="compat-test",
            model_id="compat-test",
            base_url="https://invalid.test/v1",
            secret_ref="B27_COMPAT_TEST_KEY",
        )
    )
    role = service.create_role(
        RolePreset(
            name="compat-reader",
            system_prompt="Read project memory.",
            model_profile_id=profile.id,
            memory_scope="read: [project, episodic]; write: []",
        )
    )
    session = service.create_session(role.id)
    workspace = service.store.get_workspace_initialization_by_id(
        manager._project(project)["workspace_id"]
    ).workspace_ref
    yield service, manager, project, session, workspace, command
    await manager.close()
    service.close()


async def _publish_custom(manager, base, *, record_id: str, **changes):
    content = changes.get("content", base.content)
    version = base.model_copy(
        update={
            "ref": base.ref.model_copy(
                update={
                    "record_id": record_id,
                    "version": 1,
                    "content_digest": hashlib.sha256(content.encode()).hexdigest(),
                }
            ),
            **changes,
        }
    )
    manager.ledger.save_version(version)
    head = manager.ledger.get_head(version.ref.dataset_id, record_id)
    proposal = manager.ledger.propose(version, expected_head_revision=head.revision)
    manager.ledger.confirm_proposal(proposal, expected_head_revision=head.revision)
    return version


async def _publish_next(manager, current, *, expected_revision: int, **changes):
    content = changes.get("content", current.content)
    version = current.model_copy(
        update={
            "ref": current.ref.model_copy(
                update={
                    "version": current.ref.version + 1,
                    "content_digest": hashlib.sha256(content.encode()).hexdigest(),
                }
            ),
            **changes,
        }
    )
    manager.ledger.save_version(version, expected_head_revision=expected_revision)
    proposal = manager.ledger.propose(version, expected_head_revision=expected_revision)
    manager.ledger.confirm_proposal(proposal, expected_head_revision=expected_revision)
    return version


@pytest.mark.asyncio
async def test_compat_get_and_query_enforce_version_role_and_kinds(compat_environment):
    service, manager, project, session, workspace, command = compat_environment
    saved = await command(action="memory_save", project_id=project, content="role guarded")
    record = saved.state.records[0]
    base = manager.ledger.get_version(record.dataset_id, record.record_id, 1)
    await _publish_next(
        manager,
        base,
        expected_revision=record.revision,
        role_ids=("role-other",),
        content="role guarded replacement",
    )

    with pytest.raises(PermissionError, match="role restriction"):
        service.get_memory(
            record.record_id,
            snapshot=session.role_snapshot,
            project_scope=workspace,
        )
    assert (
        service.query_memories(
            "role guarded",
            snapshot=session.role_snapshot,
            project_scope=workspace,
        )
        == []
    )
    assert (
        service.query_memories(
            "role guarded",
            snapshot=session.role_snapshot,
            project_scope=workspace,
            kinds=(MemoryKind.PROJECT,),
        )
        == []
    )
    with pytest.raises(PermissionError, match="does not allow"):
        service.query_memories(
            "role guarded",
            snapshot=session.role_snapshot,
            project_scope=workspace,
            kinds=(MemoryKind.WORKING,),
        )


@pytest.mark.asyncio
async def test_compat_agent_restriction_requires_core_bound_running_agent(compat_environment):
    service, manager, project, session, workspace, command = compat_environment
    saved = await command(action="memory_save", project_id=project, content="agent guarded")
    record = saved.state.records[0]
    base = manager.ledger.get_version(record.dataset_id, record.record_id, 1)
    agent = service.factory.create_agent(session.id)
    restricted = await _publish_custom(
        manager,
        base,
        record_id="agent-guarded-record",
        agent_ids=(agent.id,),
        content="agent guarded custom",
    )

    with pytest.raises(PermissionError, match="trusted agent"):
        service.get_memory(
            restricted.ref.record_id,
            snapshot=session.role_snapshot,
            session_id=session.id,
            project_scope=workspace,
        )
    assert (
        service.query_memories(
            "agent guarded custom",
            snapshot=session.role_snapshot,
            session_id=session.id,
            project_scope=workspace,
        )
        == []
    )

    assert service.admit_session_run(session.id)
    lease = service.admitted_session_run_lease(session.id)
    assert lease is not None
    lease = service.store.bind_session_run_lease_agent(lease, agent.id)
    service._session_run_leases[session.id] = lease
    service.store.update_agent_status(agent.id, AgentStatus.RUNNING)
    try:
        assert (
            service.get_memory(
                restricted.ref.record_id,
                snapshot=session.role_snapshot,
                session_id=session.id,
                project_scope=workspace,
            ).content
            == "agent guarded custom"
        ) is True
    finally:
        service.release_session_run(session.id, lease)


@pytest.mark.asyncio
async def test_compat_history_authorizes_each_published_version(compat_environment):
    service, manager, project, session, workspace, command = compat_environment
    saved = await command(action="memory_save", project_id=project, content="history source")
    base = manager.ledger.get_version(
        saved.state.records[0].dataset_id, saved.state.records[0].record_id, 1
    )
    first = await _publish_custom(
        manager,
        base,
        record_id="per-version-history",
        role_ids=("role-other",),
        content="private old history",
    )
    current = await _publish_next(
        manager,
        first,
        expected_revision=1,
        role_ids=(),
        content="current public history",
    )

    with pytest.raises(PermissionError, match="role restriction"):
        service.list_memory_versions(
            first.ref.record_id,
            snapshot=session.role_snapshot,
            project_scope=workspace,
        )
    # The current version is still governed independently by get/query.
    assert (
        service.get_memory(
            current.ref.record_id,
            snapshot=session.role_snapshot,
            project_scope=workspace,
        ).content
        == "current public history"
    )


@pytest.mark.asyncio
async def test_compat_rejects_cross_project_and_revoked_publications(compat_environment):
    service, manager, project, session, workspace, command = compat_environment
    saved = await command(
        action="memory_save", project_id=project, content="revocation boundary", confirmed=True
    )
    record = saved.state.records[0]
    with pytest.raises(PermissionError, match="workspace"):
        service.get_memory(
            record.record_id,
            snapshot=session.role_snapshot,
            project_scope=str(Path(workspace).parent / "other-project"),
        )

    await command(
        action="memory_deactivate",
        project_id=project,
        record_id=record.record_id,
        expected_revision=record.revision,
    )
    with pytest.raises(PermissionError, match="published"):
        service.get_memory(
            record.record_id,
            snapshot=session.role_snapshot,
            project_scope=workspace,
        )
    assert (
        service.query_memories(
            "revocation boundary",
            snapshot=session.role_snapshot,
            project_scope=workspace,
        )
        == []
    )


def test_legacy_history_and_explicit_versions_are_authorized_per_version(tmp_path: Path):
    app = create_app(tmp_path / "legacy.sqlite3")
    service = app.state.operant_service
    # The application factory installs a lazy plugin manager for API routes.
    # This test intentionally exercises the migration-only legacy read path.
    service.memory_manager = None
    service.memory_manager_factory = None
    profile = service.add_model_profile(
        ModelProfile(
            name="legacy-test",
            model_id="legacy-test",
            base_url="https://invalid.test/v1",
            secret_ref="B27_LEGACY_TEST_KEY",
        )
    )
    role = service.create_role(
        RolePreset(
            name="legacy-reader",
            system_prompt="Read migrated project rows.",
            model_profile_id=profile.id,
            memory_scope="read: [project]; write: []",
        )
    )
    session = service.create_session(role.id)
    first = service.store.create_memory(
        Memory(
            id="legacy-history",
            kind=MemoryKind.PROJECT,
            content="private old legacy row",
            project_scope=str(tmp_path),
            role_scope=("role-other",),
            status=MemoryStatus.ACTIVE,
        )
    )
    current = service.store.update_memory(
        first.id,
        content="current legacy row",
        role_scope=(),
    )
    with pytest.raises(PermissionError, match="role_scope"):
        service.get_memory(
            first.id,
            snapshot=session.role_snapshot,
            project_scope=str(tmp_path),
            version=first.version,
        )
    assert (
        service.get_memory(
            current.id,
            snapshot=session.role_snapshot,
            project_scope=str(tmp_path),
        ).content
        == "current legacy row"
    )
    with pytest.raises(PermissionError, match="role_scope"):
        service.list_memory_versions(
            first.id,
            snapshot=session.role_snapshot,
            project_scope=str(tmp_path),
        )
    service.close()
