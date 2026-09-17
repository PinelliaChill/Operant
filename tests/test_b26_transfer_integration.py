"""Dataset handoff through the actual installed MemoryManager lifecycle."""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone

import pytest
from test_b23_management import command, manager, setup_project  # noqa: F401

from operant.contracts.b2_1 import DatasetTransfer
from operant.contracts.b2_6_sharing import SharingCommand, SharingGrant
from operant.memory_plugins.sharing import SharingService
from operant.plugins.protocol import PluginError


@pytest.mark.asyncio
async def test_manager_transfer_then_uninstall_preserves_new_owner(manager, tmp_path):  # noqa: F811
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for args in (
        ("init", "-b", "main"),
        ("config", "user.name", "B26"),
        ("config", "user.email", "b26@example.invalid"),
    ):
        subprocess.run(["git", *args], cwd=workspace, check=True, capture_output=True)
    (workspace / "README.md").write_text("Synthetic transfer workspace.\n")
    for args in (("add", "README.md"), ("commit", "-m", "baseline")):
        subprocess.run(["git", *args], cwd=workspace, check=True, capture_output=True)
    project_id, source_id, dataset_id = await setup_project(manager, workspace)
    saved = await command(
        manager,
        action="memory_save",
        project_id=project_id,
        content="TRANSFER_OWNER_RETAINS_THIS",
        confirmed=True,
    )
    record_id = saved.state.records[0].record_id
    version = manager.ledger.get_version(dataset_id, record_id)
    destination_result = await command(
        manager, action="plugin_install", plugin_id="memory-standard", mode="trusted_in_process"
    )
    destination_id = destination_result.state.installations[-1].installation_id
    assert manager.registry.get_installation(destination_id).binding_id is not None
    await command(manager, action="plugin_disable", installation_id=destination_id, confirmed=True)
    sharing = SharingService(manager)
    registration = await sharing.execute(
        SharingCommand(
            action="worktree_register",
            project_id=project_id,
            workspace_id=manager._project(project_id)["workspace_id"],
        )
    )
    assert registration.status == "completed", registration.message
    grant = sharing.create_grant(
        SharingGrant(
            project_id=project_id,
            source_dataset_id=dataset_id,
            source_scope=version.scope,
            target_scope=version.scope,
            subject_id=destination_id,
            grantor_id=version.owner.principal_id,
            purpose="transfer",
            memory_refs=(version.ref,),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
    )
    pending = sharing.begin_transfer(
        DatasetTransfer(
            dataset_id=dataset_id,
            destination_dataset_id=dataset_id,
            expected_revision=manager.registry.get_dataset(dataset_id).revision,
            from_namespace=f"dataset:{dataset_id}",
            to_namespace=f"dataset:{dataset_id}",
            destination_installation_id=destination_id,
            authorization_grant_id=grant.grant_id,
            mode="transfer",
            idempotency_key="manager-transfer",
        ),
        project_id=project_id,
    )
    checked = sharing.validate_transfer(pending.transfer_id, expected_revision=pending.revision)
    transferred = sharing.commit_transfer(checked.transfer_id, expected_revision=checked.revision)
    assert transferred.state == "committed"
    assert manager.registry.get_dataset(dataset_id).installation_id == destination_id
    with pytest.raises(PluginError, match="no longer owns"):
        manager._installation(manager._project(project_id))
    await command(
        manager, action="binding_select", project_id=project_id, installation_id=destination_id
    )
    await command(manager, action="plugin_enable", installation_id=destination_id, confirmed=True)
    assert manager._installation(manager._project(project_id)).installation_id == destination_id
    result = await command(
        manager, action="plugin_uninstall", installation_id=source_id, data_policy="delete"
    )
    assert result.status == "completed"
    assert "保留新所有者" in result.message
    assert manager.registry.get_dataset(dataset_id).installation_id == destination_id
    assert (
        manager.ledger.get_version(dataset_id, record_id).content == "TRANSFER_OWNER_RETAINS_THIS"
    )
    # New owner's normal write/read path remains functional after old owner cleanup.
    fresh = await command(
        manager,
        action="memory_save",
        project_id=project_id,
        content="NEW_OWNER_CAN_WRITE",
        confirmed=True,
    )
    assert any(r.content == "NEW_OWNER_CAN_WRITE" for r in fresh.state.records)
