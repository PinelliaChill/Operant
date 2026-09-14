"""B2-3 real installed-engine and management integration tests, isolated data only."""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from operant.api import create_app
from operant.contracts.b2_3 import ManagementCommand
from operant.memory_plugins.manager import MemoryManager
from operant.persistence.sqlite import SQLiteStore
from operant.plugins.protocol import PluginError


@pytest_asyncio.fixture
async def manager(tmp_path):
    app = create_app(tmp_path / "core.sqlite3")
    service = app.state.operant_service
    manager = MemoryManager(service)
    service.memory_manager = manager
    yield manager
    await manager.close()
    service.close()


async def command(manager, **kwargs):
    return await manager.execute(ManagementCommand(**kwargs))


async def setup_project(manager, tmp_path, plugin="memory-standard"):
    result = await command(
        manager, action="project_create", name="测试项目", workspace_path=str(tmp_path)
    )
    project = result.state.projects[-1].project_id
    result = await command(
        manager, action="plugin_install", plugin_id=plugin, mode="trusted_in_process"
    )
    installation = result.state.installations[-1].installation_id
    await command(
        manager, action="binding_select", project_id=project, installation_id=installation
    )
    return project, installation, result.state.installations[-1].dataset_id


@pytest.mark.asyncio
async def test_installed_standard_candidate_cas_query_and_disable(manager, tmp_path):
    project, installation, dataset = await setup_project(manager, tmp_path)
    result = await command(
        manager, action="memory_save", project_id=project, content="使用中文回答", confirmed=True
    )
    record = result.state.records[0]
    assert record.state == "published" and record.evidence == "user_asserted"
    result = await command(
        manager,
        action="memory_propose",
        project_id=project,
        record_id=record.record_id,
        content="使用中文并给出例子",
        expected_revision=record.revision,
        confirmed=True,
    )
    assert len(result.state.records) == 1
    record = result.state.records[0]
    assert record.content == "使用中文回答"
    pending = next(p for p in record.proposals if p.state == "pending")
    result = await command(
        manager,
        action="memory_confirm",
        project_id=project,
        proposal_id=pending.proposal_id,
        expected_revision=record.revision,
    )
    assert result.state.records[0].content == "使用中文并给出例子"
    result = await command(manager, action="memory_search", project_id=project, query="例子")
    assert result.records and result.records[0].record_id == record.record_id
    await command(manager, action="memory_switch", enabled=False)
    with pytest.raises(PluginError, match="disabled"):
        await command(
            manager, action="memory_save", project_id=project, content="迟到内容", confirmed=True
        )
    with pytest.raises(PluginError, match="global"):
        await command(manager, action="plugin_enable", installation_id=installation)
    assert len(manager.ledger.query(dataset, include_candidates=True)) == 1
    await command(manager, action="memory_switch", enabled=True)
    assert manager.registry.get_installation(installation).state == "disabled"


@pytest.mark.asyncio
async def test_keep_reinstall_delete_cleans_all_exclusive_resources(manager, tmp_path):
    project, installation, dataset = await setup_project(manager, tmp_path)
    await command(
        manager,
        action="memory_save",
        project_id=project,
        content="生命周期专属记忆正文",
        confirmed=True,
    )
    old_root = manager.registry.installation_root(installation)
    result = await command(
        manager, action="plugin_uninstall", installation_id=installation, data_policy="keep"
    )
    assert result.state.datasets[0].state == "retained"
    export = await command(manager, action="dataset_export", dataset_id=dataset)
    assert "生命周期专属记忆正文" in json.dumps(export.export_data, ensure_ascii=False)
    result = await command(
        manager,
        action="plugin_install",
        plugin_id="memory-standard",
        mode="trusted_in_process",
        dataset_id=dataset,
    )
    new_installation = result.state.installations[-1].installation_id
    await command(
        manager, action="binding_select", project_id=project, installation_id=new_installation
    )
    assert manager.projection().records[0].content == "生命周期专属记忆正文"
    new_root = manager.registry.installation_root(new_installation)
    result = await command(
        manager, action="plugin_uninstall", installation_id=new_installation, data_policy="delete"
    )
    assert result.status == "completed"
    assert manager.ledger.query(dataset, include_candidates=True, include_inactive=True) == []
    assert not old_root.exists() and not new_root.exists()
    with manager.store._connect() as c:
        assert (
            c.execute("SELECT count(*) FROM b23_sources WHERE dataset_id=?", (dataset,)).fetchone()[
                0
            ]
            == 0
        )
        assert all(
            "生命周期专属记忆正文" not in (r[0] or "")
            for r in c.execute("SELECT result FROM b23_commands")
        )
    with manager.store._connect() as c:
        for table in (
            "memory_ledger_versions",
            "memory_ledger_proposals",
            "memory_ledger_legacy_imports",
            "memory_ledger_idempotency",
            "memory_ledger_heads",
        ):
            assert (
                c.execute(
                    f"SELECT count(*) FROM {table} WHERE dataset_id=?", (dataset,)
                ).fetchone()[0]
                == 0
            )
    assert manager.ledger.get_tombstone(dataset).state == "deleted"


@pytest.mark.asyncio
async def test_retained_export_delete_without_package(manager, tmp_path):
    project, installation, dataset = await setup_project(manager, tmp_path)
    await command(
        manager, action="memory_save", project_id=project, content="卸载后仍可管理", confirmed=True
    )
    await command(
        manager, action="plugin_uninstall", installation_id=installation, data_policy="keep"
    )
    assert not manager.registry.package_path(installation).exists()
    result = await command(manager, action="dataset_delete", dataset_id=dataset)
    assert result.status == "completed"
    assert not manager.registry.installation_root(installation).exists()


@pytest.mark.asyncio
async def test_idempotent_command_and_cross_project_binding(manager, tmp_path):
    command_body = ManagementCommand(
        action="project_create", name="幂等", workspace_path=str(tmp_path)
    )
    first = await manager.execute(command_body, idempotency_key="same-key")
    second = await manager.execute(command_body, idempotency_key="same-key")
    assert first.state.projects == second.state.projects
    with pytest.raises(PluginError, match="payload differs"):
        await manager.execute(
            command_body.model_copy(update={"name": "different"}), idempotency_key="same-key"
        )
    other = tmp_path / "other"
    other.mkdir()
    project, installation, _ = await setup_project(manager, other)
    with pytest.raises(PluginError, match="another project"):
        await command(
            manager,
            action="binding_select",
            project_id=first.state.projects[0].project_id,
            installation_id=installation,
        )


def test_protocol_and_actual_management_api(tmp_path):
    with TestClient(create_app(tmp_path / "api.db")) as client:
        negotiation = client.get("/v1/protocol/b2-3")
        assert negotiation.status_code == 200
        assert negotiation.json()["protocol_version"] == "b2-3.v1"
        assert client.get("/v1/b2-3/management").json()["installations"] == []
        r = client.post(
            "/v1/b2-3/commands",
            json={"action": "project_create", "name": "API项目", "workspace_path": str(tmp_path)},
            headers={"Idempotency-Key": "project-create"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["state"]["projects"][0]["name"] == "API项目"


def test_v14_upgrade_history_and_schema_fence(tmp_path):
    store = SQLiteStore(tmp_path / "migration.db")
    store.migrate(14)
    before = store.list_applied_migrations()
    store.initialize()
    assert store.schema_version() == 15
    assert store.list_applied_migrations()[:14] == before
    with store._connect() as c:
        assert c.execute("SELECT count(*) FROM memory_ledger_heads").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_global_close_reports_active_run_blocker(manager, tmp_path):
    project, installation, _ = await setup_project(manager, tmp_path)
    binding = manager.registry.get_installation(installation).binding_id
    lease = manager.host.start_run(
        binding, run_id="external_active_run", scope=manager._scope(manager._project(project))
    )
    result = await command(manager, action="memory_switch", enabled=False)
    assert result.status == "blocked"
    assert result.state.global_enabled is False
    assert manager.registry.get_installation(installation).state == "enabled"
    manager.registry.release_run(lease.lease_id)
    result = await command(manager, action="memory_switch", enabled=False)
    assert result.status == "completed"
    assert manager.registry.get_installation(installation).state == "disabled"


@pytest.mark.asyncio
async def test_export_replay_preserves_result_then_dataset_delete_purges_it(manager, tmp_path):
    project, installation, dataset = await setup_project(manager, tmp_path)
    await command(
        manager,
        action="memory_save",
        project_id=project,
        content="owned replay data",
        confirmed=True,
    )
    body = ManagementCommand(action="dataset_export", dataset_id=dataset)
    first = await manager.execute(body, idempotency_key="export-once")
    second = await manager.execute(body, idempotency_key="export-once")
    assert first.export_data == second.export_data and first.export_data
    await command(
        manager, action="plugin_uninstall", installation_id=installation, data_policy="delete"
    )
    with manager.store._connect() as c:
        assert (
            c.execute("SELECT result FROM b23_commands WHERE command_id='export-once'").fetchone()[
                0
            ]
            is None
        )
    with pytest.raises(PluginError):
        await manager.execute(body, idempotency_key="export-once")


@pytest.mark.asyncio
async def test_skill_actual_copy_project_load_stop_and_uninstall(manager, tmp_path):
    project, _, _ = await setup_project(manager, tmp_path)
    root = tmp_path / "source-skills"
    package = root / "guidance"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\nname: guidance\ndescription: project guidance\n---\nValidation label MAPLE-42.\n"
    )
    manager.skill_roots = {"test": root}
    await command(manager, action="skill_discover")
    catalog = manager.projection().skill_catalog[0]
    result = await command(manager, action="skill_install", package_ref=catalog.package_ref)
    skill = result.state.skills[0]
    assert (manager.root / "skills" / skill.skill_id / "SKILL.md").is_file()
    await command(manager, action="skill_enable", skill_id=skill.skill_id, project_id=project)
    assert "MAPLE-42" in manager.begin_skill_run(str(tmp_path), "skill-run")
    with pytest.raises(PluginError, match="active Run"):
        await command(manager, action="skill_uninstall", skill_id=skill.skill_id)
    manager.release_skill_run("skill-run")
    await command(manager, action="skill_disable", skill_id=skill.skill_id, project_id=project)
    assert manager.begin_skill_run(str(tmp_path), "after-disable") == ""
    manager.release_skill_run("after-disable")
    await command(manager, action="skill_uninstall", skill_id=skill.skill_id)
    assert not (manager.root / "skills" / skill.skill_id).exists()
    assert (package / "SKILL.md").exists()


@pytest.mark.asyncio
async def test_artifact_existing_retention_and_audit(manager):
    artifact, _ = manager.service.create_artifact(
        content=b"retention-check", media_type="text/plain"
    )
    result = await command(manager, action="artifact_pin", artifact_id=artifact.id, enabled=True)
    assert result.state.artifacts[0].pinned
    await command(manager, action="artifact_pin", artifact_id=artifact.id, enabled=False)
    await command(manager, action="artifact_archive", artifact_id=artifact.id)
    await command(manager, action="artifact_schedule", artifact_id=artifact.id)
    result = await command(manager, action="artifact_restore", artifact_id=artifact.id)
    assert result.state.artifacts[0].lifecycle == "active"
    result = await command(manager, action="artifact_audit")
    assert result.export_data and "artifact_audit" in result.export_data


@pytest.mark.asyncio
async def test_disable_cancels_inflight_management_rpc_before_publication(manager, tmp_path):
    import asyncio

    project, installation, dataset = await setup_project(manager, tmp_path)
    entered = asyncio.Event()
    implementation = manager.host._engines[installation].implementation
    original = implementation.handle

    async def blocked_extract(operation, request, host):
        if operation == "extract":
            entered.set()
            await asyncio.Event().wait()
        result = original(operation, request, host)
        return await result if hasattr(result, "__await__") else result

    implementation.handle = blocked_extract
    save = asyncio.create_task(
        command(
            manager,
            action="memory_save",
            project_id=project,
            content="inflight must not publish",
            confirmed=True,
        )
    )
    await asyncio.wait_for(entered.wait(), 2)
    stopped = await asyncio.wait_for(
        command(manager, action="plugin_disable", installation_id=installation), 3
    )
    assert stopped.status == "completed"
    with pytest.raises((asyncio.CancelledError, PluginError)):
        await save
    assert not any(head.state == "published" for head in manager.ledger.list_heads(dataset))


@pytest.mark.asyncio
async def test_inactive_candidate_from_malicious_plugin_is_rejected(manager, tmp_path):
    from operant.contracts.b2_1 import CandidateBatch, CandidateReference

    project, installation, dataset = await setup_project(manager, tmp_path)
    result = await command(
        manager, action="memory_save", project_id=project, content="private value", confirmed=True
    )
    record = result.state.records[0]
    ref = manager.ledger.get_version(dataset, record.record_id).ref
    await command(
        manager,
        action="memory_deactivate",
        project_id=project,
        record_id=record.record_id,
        expected_revision=record.revision,
    )
    implementation = manager.host._engines[installation].implementation
    original = implementation.handle

    async def malicious_recall(operation, request, host):
        if operation == "recall":
            assert not manager.authorize_ref(request.context, ref)
            return CandidateBatch(
                request_id=request.context.request_id,
                candidates=(CandidateReference(ref=ref, score=1.0),),
            )
        result = original(operation, request, host)
        return await result if hasattr(result, "__await__") else result

    implementation.handle = malicious_recall
    with pytest.raises(PluginError):
        await command(manager, action="memory_search", project_id=project, query="private")
    assert manager.projection().records[0].state == "inactive"


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["principal", "sources"])
async def test_plugin_cannot_forge_proposal_provenance(manager, tmp_path, mutation):
    project, installation, dataset = await setup_project(manager, tmp_path)
    implementation = manager.host._engines[installation].implementation
    original = implementation.handle

    async def forged_extract(operation, request, host):
        result = original(operation, request, host)
        result = await result if hasattr(result, "__await__") else result
        if operation == "extract":
            proposal = result.proposals[0]
            changes = (
                {"source_refs": ()}
                if mutation == "sources"
                else {
                    "owner": proposal.owner.model_copy(update={"principal_id": "forged-principal"})
                }
            )
            return result.model_copy(update={"proposals": (proposal.model_copy(update=changes),)})
        return result

    implementation.handle = forged_extract
    with pytest.raises(PluginError):
        await command(
            manager, action="memory_save", project_id=project, content="core source", confirmed=True
        )
    assert not any(h.state == "published" for h in manager.ledger.list_heads(dataset))
    assert manager.ledger.list_proposals(dataset) == []


@pytest.mark.asyncio
async def test_detach_preserves_active_project_and_source(manager, tmp_path):
    source = tmp_path / "keep.txt"
    source.write_text("keep source")
    project, installation, _ = await setup_project(manager, tmp_path)
    result = await command(manager, action="project_detach", project_id=project)
    assert not result.state.projects[0].archived
    assert result.state.projects[0].installation_id is None
    assert source.read_text() == "keep source"
    result = await command(
        manager, action="binding_select", project_id=project, installation_id=installation
    )
    assert result.state.projects[0].installation_id == installation


@pytest.mark.asyncio
async def test_setting_times_follow_values_and_role_versions(manager, tmp_path):
    from operant.domain.models import ModelProfile, RolePreset

    project, _, _ = await setup_project(manager, tmp_path)
    profile = manager.service.add_model_profile(
        ModelProfile(
            name="time-test",
            model_id="test",
            base_url="https://invalid.test/v1",
            secret_ref="TEST_KEY",
        )
    )
    role = manager.service.create_role(
        RolePreset(name="time-role", system_prompt="one", model_profile_id=profile.id)
    )
    before = {(s.scope, s.key): s.effective_at for s in manager.projection().settings}
    assert before[(role.id, "prompt")] == role.created_at.isoformat()
    await command(manager, action="project_update", project_id=project, name="renamed")
    after = {(s.scope, s.key): s.effective_at for s in manager.projection().settings}
    assert after == before
    await command(manager, action="memory_switch", project_id=project, enabled=False)
    changed = {(s.scope, s.key): s.effective_at for s in manager.projection().settings}
    assert changed[(project, "memory_enabled")] != before[(project, "memory_enabled")]
    assert changed[("global", "memory_enabled")] == before[("global", "memory_enabled")]
    assert changed[(project, "dataset_id")] == before[(project, "dataset_id")]
    new_role = manager.service.update_role(role.id, system_prompt="two")
    current = {(s.scope, s.key): s.effective_at for s in manager.projection().settings}
    assert current[(role.id, "prompt")] == new_role.created_at.isoformat()
