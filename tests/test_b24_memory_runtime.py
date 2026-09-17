"""MP-3 real installed-engine chain, cutoff and revoke boundaries on isolated data."""

import pytest
import pytest_asyncio

from operant.api import create_app
from operant.contracts.b2_3 import ManagementCommand
from operant.domain.models import ModelProfile, RolePreset
from operant.memory_plugins.manager import MemoryManager
from operant.memory_plugins.recall import begin_memory_run


@pytest_asyncio.fixture
async def setup(tmp_path):
    app = create_app(tmp_path / "core.db")
    service = app.state.operant_service
    manager = MemoryManager(service)
    service.memory_manager = manager

    async def command(**kwargs):
        return await manager.execute(ManagementCommand(**kwargs))

    project = (
        (await command(action="project_create", name="test", workspace_path=str(tmp_path)))
        .state.projects[-1]
        .project_id
    )
    install = (
        (
            await command(
                action="plugin_install", plugin_id="memory-standard", mode="trusted_in_process"
            )
        )
        .state.installations[-1]
        .installation_id
    )
    await command(action="binding_select", project_id=project, installation_id=install)
    profile = service.add_model_profile(
        ModelProfile(
            name="test", base_url="https://invalid.test/v1", model_id="test", secret_ref="TEST_KEY"
        )
    )
    role = service.create_role(
        RolePreset(
            name="test", system_prompt="test", model_profile_id=profile.id, memory_scope="project"
        )
    )
    session = service.create_session(role.id)

    async def begin(query="依赖"):
        agent = service.factory.create_agent(session.id)
        return await begin_memory_run(
            manager,
            session_id=session.id,
            agent_id=agent.id,
            run_id=session.id,
            workspace=str(tmp_path),
            snapshot=session.role_snapshot,
            query=query,
            references=(),
        )

    yield app, manager, project, session, command, begin
    await manager.close()
    service.close()


@pytest.mark.asyncio
async def test_actual_plugin_recall_frozen_and_revocation(setup):
    app, m, p, session, cmd, begin = setup
    saved = await cmd(
        action="memory_save", project_id=p, content="使用 uv 管理依赖", confirmed=True
    )
    record = saved.state.records[0]
    run = await begin()
    assert run is not None
    inspection = run.inspection(16000, "test")
    assert [e.memory.content for e in inspection.entries] == ["使用 uv 管理依赖"]
    assert inspection.pack.selected[0].version == 1
    await cmd(action="memory_save", project_id=p, content="新依赖使用其他命令", confirmed=True)
    another = await begin()
    assert [v.ref.record_id for v in another.candidates] == [record.record_id]
    await cmd(
        action="memory_deactivate",
        project_id=p,
        record_id=record.record_id,
        expected_revision=record.revision,
    )
    with pytest.raises(PermissionError, match="revoked"):
        run.inspection(16000, "test")
    with pytest.raises(PermissionError, match="revoked"):
        await begin()


@pytest.mark.asyncio
async def test_memory_budget_does_not_truncate_conditions(setup):
    _, m, p, _, cmd, begin = setup
    await cmd(
        action="memory_save", project_id=p, content="仅在测试项目中使用 uv 管理依赖", confirmed=True
    )
    run = await begin()
    assert run.inspection(10, "test").entries == []
    assert len(run.inspection(16000, "test").entries) == 1


@pytest.mark.asyncio
async def test_formal_session_persists_exact_pack_with_context(setup):
    from operant.domain.messages import ModelResponse, ProviderEvent
    from operant.providers.base import ModelProvider

    class Provider(ModelProvider):
        async def list_models(self, **kwargs):
            return ["test"]

        async def stream(self, **kwargs):
            text = "\n".join(m.content or "" for m in kwargs["messages"])
            assert "使用 uv 管理依赖" in text
            yield ProviderEvent(
                event_type="model.completed",
                response=ModelResponse(content="done", finish_reason="stop"),
            )

    app, m, p, session, cmd, _ = setup
    m.service.provider = Provider()
    await cmd(action="memory_save", project_id=p, content="使用 uv 管理依赖", confirmed=True)
    workspace = m.store.get_workspace_initialization_by_id(
        m._project(p)["workspace_id"]
    ).workspace_ref
    events = [
        e async for e in m.service.run_session(session.id, user_message="依赖", workspace=workspace)
    ]
    assert events[-1].event_type == "agent.completed", [(e.event_type, e.payload) for e in events]
    revision = m.store.list_context_revisions(session.id)[0]
    assert "使用 uv 管理依赖" in "\n".join(x.content or "" for x in revision.messages)
    with m.store._connect() as c:
        row = c.execute(
            "SELECT body FROM b24_context_memory WHERE revision_id=?", (revision.id,)
        ).fetchone()
    assert row is not None and "automatic_and_explicit" in row["body"]
    assert not m._recall_runs


@pytest.mark.asyncio
async def test_disabled_previous_derived_context_pauses_but_new_session_works(setup):
    _, m, p, session, cmd, begin = setup
    await cmd(action="memory_save", project_id=p, content="使用 uv 管理依赖", confirmed=True)
    run = await begin()
    run.inspection(16000, "test")
    # Release this test's artificial live lease before the authorized stop barrier.
    m.registry.release_run(run.lease.lease_id)
    await cmd(action="memory_switch", enabled=False)
    with pytest.raises(PermissionError, match="clean Context"):
        await begin()
    fresh = m.service.create_session(session.role_snapshot.role_id)
    agent = m.service.factory.create_agent(fresh.id)
    workspace = m.store.get_workspace_initialization_by_id(
        m._project(p)["workspace_id"]
    ).workspace_ref
    assert (
        await begin_memory_run(
            m,
            session_id=fresh.id,
            agent_id=agent.id,
            run_id=fresh.id,
            workspace=workspace,
            snapshot=fresh.role_snapshot,
            query="ordinary",
            references=(),
        )
        is None
    )


@pytest.mark.asyncio
async def test_refresh_and_exclude_formal_commands_are_safe_point_only(setup):
    from fastapi.testclient import TestClient

    from operant.memory_plugins.recall import load_manifest

    app, m, p, session, cmd, begin = setup
    await cmd(action="memory_save", project_id=p, content="使用 uv 管理依赖", confirmed=True)
    run = await begin()
    inspection = run.inspection(16000, "test")
    record = inspection.entries[0].memory.ref.record_id
    with TestClient(app) as client:
        # No runtime admission lease yet: completed-turn control is allowed.
        response = client.post(
            "/v1/b2-4/commands",
            json={"action": "memory_exclude", "session_id": session.id, "record_id": record},
        )
        assert response.status_code == 200, response.text
        assert load_manifest(m, session.id)["excluded"] == [record]
        response = client.post(
            "/v1/b2-4/commands", json={"action": "memory_refresh", "session_id": session.id}
        )
        assert response.status_code == 200, response.text
        assert load_manifest(m, session.id)["revision"] == 3


@pytest.mark.asyncio
async def test_parallel_manifests_preserve_members_and_newer_refresh(setup):
    from copy import deepcopy

    from operant.memory_plugins.recall import load_manifest, save_manifest

    _, m, p, session, cmd, begin = setup
    await cmd(action="memory_save", project_id=p, content="使用 uv 管理依赖", confirmed=True)
    run = await begin()
    stale = deepcopy(run.state)
    ref = run.inspection(16000, "test").entries[0].memory.ref.model_dump()
    latest = load_manifest(m, session.id)
    latest["revision"] += 1
    latest["excluded"] = [ref["record_id"]]
    save_manifest(m, session.id, session.id, run.lease.dataset_id, latest)
    stale["used_refs"] = [ref]
    stale["used_by_session"] = {"another-session": [ref]}
    save_manifest(m, session.id, "another-session", run.lease.dataset_id, stale)
    result = load_manifest(m, session.id)
    assert result["revision"] == 2 and result["excluded"] == [ref["record_id"]]
    assert result["used_by_session"] == {session.id: [ref], "another-session": [ref]}


@pytest.mark.asyncio
async def test_prior_history_is_rechecked_even_when_new_pack_omits_entry(setup):
    _, m, p, session, cmd, begin = setup
    saved = await cmd(
        action="memory_save", project_id=p, content="使用 uv 管理依赖", confirmed=True
    )
    record = saved.state.records[0]
    first = await begin()
    first.inspection(16000, "test")
    later = await begin()
    later.candidates = []
    await cmd(
        action="memory_deactivate",
        project_id=p,
        record_id=record.record_id,
        expected_revision=record.revision,
    )
    with pytest.raises(PermissionError, match="prior memory-derived history"):
        later.inspection(16000, "test")


@pytest.mark.asyncio
async def test_history_permission_is_per_session_and_live_role_rechecked(setup):
    _, m, p, session, cmd, begin = setup
    await cmd(action="memory_save", project_id=p, content="使用 uv 管理依赖", confirmed=True)
    run = await begin()
    version = run.candidates[0]
    # Immutable role-restricted provenance must also be checked for old history.
    restricted = version.model_copy(update={"role_ids": ("other-role",)})
    assert not run.validate(restricted, historical=True)
    restricted = version.model_copy(update={"agent_ids": ("other-agent",)})
    assert not run.validate(restricted, historical=True)
    run.inspection(16000, "test")
    other = await begin()
    other.session_id = "unrelated-member-session"
    other.candidates = []
    m.authorize_source = lambda *_: False
    # A member without this history does not inherit another member's private refs.
    assert other.inspection(16000, "test").entries == []


@pytest.mark.asyncio
async def test_composer_setup_failure_releases_plugin_run(setup, monkeypatch):
    _, m, p, session, cmd, _ = setup
    await cmd(action="memory_save", project_id=p, content="使用 uv 管理依赖", confirmed=True)

    def fail_composer(**kwargs):
        assert kwargs["memory_run"] is not None
        raise ValueError("setup failed after recall")

    monkeypatch.setattr("operant.application.service.PersistentContextComposer", fail_composer)
    workspace = m.store.get_workspace_initialization_by_id(
        m._project(p)["workspace_id"]
    ).workspace_ref
    events = [
        e async for e in m.service.run_session(session.id, user_message="依赖", workspace=workspace)
    ]
    assert events[-1].event_type == "agent.failed"
    assert not m._recall_runs
    assert session.id not in m.service._session_run_leases


@pytest.mark.asyncio
async def test_revoked_shared_graph_evidence_stops_other_members(setup):
    _, m, p, session, cmd, begin = setup
    saved = await cmd(
        action="memory_save", project_id=p, content="使用 uv 管理依赖", confirmed=True
    )
    record = saved.state.records[0]
    first = await begin()
    first.inspection(16000, "test")
    other = await begin()
    other.session_id = "another-graph-member"
    other.candidates = []
    await cmd(
        action="memory_deactivate",
        project_id=p,
        record_id=record.record_id,
        expected_revision=record.revision,
    )
    with pytest.raises(PermissionError, match="prior memory-derived history"):
        other.inspection(16000, "test")


@pytest.mark.asyncio
async def test_confirmed_resident_preference_uses_same_plugin_pack_and_budget(setup):
    _, m, p, session, cmd, begin = setup
    saved = await cmd(
        action="memory_save", project_id=p, content="请用简短中文回复。", confirmed=True
    )
    record = saved.state.records[0]
    old = m.ledger.get_version(record.dataset_id, record.record_id, 1)
    preference = old.model_copy(
        update={
            "ref": old.ref.model_copy(update={"version": 2}),
            "content_type": "preference",
        }
    )
    proposal = m.ledger.propose(preference, expected_head_revision=record.revision)
    m.ledger.confirm_proposal(proposal, expected_head_revision=record.revision)
    run = await begin("unrelated arithmetic task")
    pack = run.inspection(16000, "test")
    assert len(pack.entries) == 1
    assert pack.entries[0].reason == "resident_preference"
    assert pack.entries[0].memory.ref == preference.ref
    assert run.inspection(10, "test").entries == []


@pytest.mark.asyncio
async def test_sensitive_resident_and_explicit_refs_are_denied(setup):
    from operant.domain.context import ContextReferenceType, ReferenceRequest

    _, m, p, session, cmd, begin = setup
    saved = await cmd(
        action="memory_save", project_id=p, content="请用简短中文回复。", confirmed=True
    )
    record = saved.state.records[0]
    old = m.ledger.get_version(record.dataset_id, record.record_id, 1)
    preference = old.model_copy(
        update={
            "ref": old.ref.model_copy(update={"version": 2}),
            "content_type": "preference",
            "sensitivity": "sensitive",
        }
    )
    proposal = m.ledger.propose(preference, expected_head_revision=record.revision)
    m.ledger.confirm_proposal(proposal, expected_head_revision=record.revision)
    run = await begin("unrelated task")
    assert run.inspection(16000, "test").entries == []
    with pytest.raises(PermissionError, match="explicit memory"):
        await begin_memory_run(
            m,
            session_id=session.id,
            agent_id=m.service.factory.create_agent(session.id).id,
            run_id=session.id,
            workspace=m.store.get_workspace_initialization_by_id(
                m._project(p)["workspace_id"]
            ).workspace_ref,
            snapshot=session.role_snapshot,
            query="unrelated task",
            references=(
                ReferenceRequest(ref_type=ContextReferenceType.MEMORY, target_id=record.record_id),
            ),
        )


@pytest.mark.asyncio
async def test_explicit_excluded_memory_fails_instead_of_disappearing(setup):
    _, _, p, _, cmd, begin = setup
    saved = await cmd(
        action="memory_save", project_id=p, content="使用 uv 管理依赖", confirmed=True
    )
    record = saved.state.records[0]
    run = await begin()
    run.inspection(16000, "test")
    run.state["excluded"] = [record.record_id]
    run.explicit_ids.add(record.record_id)
    with pytest.raises(ValueError, match="explicit Memory is excluded"):
        run.inspection(16000, "test")


@pytest.mark.asyncio
async def test_current_record_role_restriction_beats_frozen_version(setup):
    _, m, p, session, cmd, begin = setup
    saved = await cmd(
        action="memory_save", project_id=p, content="使用 uv 管理依赖", confirmed=True
    )
    record = saved.state.records[0]
    run = await begin()
    run.inspection(16000, "test")
    old = m.ledger.get_version(record.dataset_id, record.record_id, 1)
    changed = old.model_copy(
        update={"ref": old.ref.model_copy(update={"version": 2}), "role_ids": ("other-role",)}
    )
    proposal = m.ledger.propose(changed, expected_head_revision=record.revision)
    m.ledger.confirm_proposal(proposal, expected_head_revision=record.revision)
    with pytest.raises(PermissionError):
        run.inspection(16000, "test")
