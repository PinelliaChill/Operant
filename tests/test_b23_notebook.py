"""Notebook package lifecycle regression through the real MemoryManager."""

from __future__ import annotations

import json

import pytest

from operant.api import create_app
from operant.contracts.b2_3 import ManagementCommand
from operant.memory_plugins.manager import MemoryManager


async def _command(manager: MemoryManager, **kwargs):
    return await manager.execute(ManagementCommand(**kwargs))


@pytest.mark.asyncio
async def test_notebook_deactivation_removes_private_recall_entry(tmp_path) -> None:
    """A retained inactive head cannot remain in the plugin's published index."""

    app = create_app(tmp_path / "notebook-lifecycle.sqlite3")
    service = app.state.operant_service
    manager = MemoryManager(service)
    service.memory_manager = manager
    try:
        project_result = await _command(
            manager,
            action="project_create",
            name="Notebook lifecycle",
            workspace_path=str(tmp_path),
        )
        project_id = project_result.state.projects[0].project_id
        install_result = await _command(
            manager,
            action="plugin_install",
            plugin_id="memory-notebook",
            mode="trusted_in_process",
        )
        installation = install_result.state.installations[-1]
        await _command(
            manager,
            action="binding_select",
            project_id=project_id,
            installation_id=installation.installation_id,
        )

        proposed = await _command(
            manager,
            action="memory_save",
            project_id=project_id,
            content="验收标记=MAPLE-42",
            confirmed=False,
        )
        record = proposed.state.records[0]
        pending = next(item for item in record.proposals if item.state == "pending")
        assert record.state == "unpublished"

        confirmed = await _command(
            manager,
            action="memory_confirm",
            project_id=project_id,
            proposal_id=pending.proposal_id,
            expected_revision=record.revision,
        )
        published = confirmed.state.records[0]
        assert published.state == "published"

        recalled = await _command(
            manager,
            action="memory_search",
            project_id=project_id,
            query="验收标记",
        )
        assert [item.record_id for item in recalled.records or []] == [published.record_id]

        deactivated = await _command(
            manager,
            action="memory_deactivate",
            project_id=project_id,
            record_id=published.record_id,
            expected_revision=published.revision,
        )
        assert deactivated.state.records[0].state == "inactive"

        after_deactivate = await _command(
            manager,
            action="memory_search",
            project_id=project_id,
            query="验收标记",
        )
        assert after_deactivate.records == []

        index_path = (
            manager.registry.installation_root(installation.installation_id)
            / "indexes"
            / "notebook-key-value.json"
        )
        index = json.loads(index_path.read_text(encoding="utf-8"))
        assert index["published"] == {}
    finally:
        await manager.close()
        service.close()


@pytest.mark.asyncio
async def test_notebook_reinstall_rebuilds_exact_recall_after_host_restart(tmp_path) -> None:
    """A fresh package index is rebuilt from Core search and version reads."""

    app = create_app(tmp_path / "notebook-reinstall.sqlite3")
    service = app.state.operant_service
    manager = MemoryManager(service)
    service.memory_manager = manager
    try:
        project_result = await _command(
            manager,
            action="project_create",
            name="Notebook reinstall",
            workspace_path=str(tmp_path),
        )
        project_id = project_result.state.projects[0].project_id
        installed = await _command(
            manager,
            action="plugin_install",
            plugin_id="memory-notebook",
            mode="trusted_in_process",
        )
        old_installation = installed.state.installations[-1]
        dataset_id = old_installation.dataset_id
        await _command(
            manager,
            action="binding_select",
            project_id=project_id,
            installation_id=old_installation.installation_id,
        )
        saved = await _command(
            manager,
            action="memory_save",
            project_id=project_id,
            content="验收标记=MAPLE-42",
            confirmed=True,
        )
        record_id = saved.state.records[0].record_id

        await _command(
            manager,
            action="plugin_uninstall",
            installation_id=old_installation.installation_id,
            data_policy="keep",
        )
        reinstalled = await _command(
            manager,
            action="plugin_install",
            plugin_id="memory-notebook",
            mode="trusted_in_process",
            dataset_id=dataset_id,
        )
        new_installation = reinstalled.state.installations[-1]
        await _command(
            manager,
            action="binding_select",
            project_id=project_id,
            installation_id=new_installation.installation_id,
        )

        # Recreate the manager/Host against the same durable registry and
        # ledger before querying the newly-installed package.
        await manager.close()
        manager = MemoryManager(service)
        service.memory_manager = manager
        await _command(
            manager,
            action="plugin_enable",
            installation_id=new_installation.installation_id,
        )
        result = await _command(
            manager,
            action="memory_search",
            project_id=project_id,
            query="验收标记",
        )
        assert [item.record_id for item in result.records or []] == [record_id]
    finally:
        await manager.close()
        service.close()
