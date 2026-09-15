"""AC-03 acceptance drill using a synthetic SQLite v14 database.

The fixture deliberately follows the pre-B2-3 Core schema through
``SQLiteStore.migrate(14)`` and then uses the public Manager migration command
after the v15 upgrade.  It never opens a configured or user database.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from operant.api import create_app
from operant.contracts.b2_3 import ManagementCommand
from operant.domain.commands import WorkspaceInitialization
from operant.domain.memory import Memory, MemoryKind, MemoryStatus
from operant.domain.models import ModelProfile, RolePreset
from operant.domain.threads import (
    ConversationThread,
    Item,
    Turn,
    UserMessagePayload,
)
from operant.memory_plugins.manager import MemoryManager
from operant.persistence.sqlite import MigrationError, SQLiteStore


def _workspace(path: Path) -> WorkspaceInitialization:
    path.mkdir(parents=True, exist_ok=True)
    workspace_ref = str(path.resolve())
    return WorkspaceInitialization(
        workspace_ref=workspace_ref,
        workspace_hash=hashlib.sha256(workspace_ref.encode("utf-8")).hexdigest(),
        readable=True,
        writable=True,
    )


def _seed_history(store: SQLiteStore, workspace_ref: str) -> list[Item]:
    thread = store.create_thread(ConversationThread(workspace_ref=workspace_ref))
    items: list[Item] = []
    for text in ("旧历史第一条", "旧历史第二条"):
        turn = store.create_turn(Turn(thread_id=thread.id))
        items.append(
            store.append_item(
                Item(
                    thread_id=thread.id,
                    turn_id=turn.id,
                    payload=UserMessagePayload(text=text, author_ref="synthetic-v14"),
                )
            )
        )
    return items


def _seed_memory(
    store: SQLiteStore,
    *,
    memory_id: str,
    project_scope: str,
    content: str,
) -> list[Memory]:
    first = store.create_memory(
        Memory(
            id=memory_id,
            kind=MemoryKind.PROJECT,
            content=content,
            project_scope=project_scope,
            source_task="synthetic v14 migration",
            confidence=0.91,
            status=MemoryStatus.ACTIVE,
        )
    )
    second = store.update_memory(
        memory_id,
        content=content + "（历史修订）",
        status=MemoryStatus.ACTIVE,
    )
    return [first, second]


def _fresh_source_database(
    base_path: Path,
) -> tuple[SQLiteStore, Path, str, list[Item], list[Memory]]:
    run_root = base_path / f"run-{uuid4().hex}"
    run_root.mkdir()
    source_db = run_root / "legacy-v14.sqlite3"
    project_workspace = run_root / "project-a-workspace"
    other_workspace = run_root / "project-b-workspace"

    store = SQLiteStore(source_db)
    assert store.migrate(target_version=14) == 14
    project = store.register_workspace(_workspace(project_workspace))[0]
    store.register_workspace(_workspace(other_workspace))
    items = _seed_history(store, project.workspace_ref)
    project_memories = _seed_memory(
        store,
        memory_id="legacy-project-a",
        project_scope=project.workspace_ref,
        content="旧项目 A 初始事实",
    )
    _seed_memory(
        store,
        memory_id="legacy-project-b",
        project_scope=str(other_workspace.resolve()),
        content="另一个项目的私有事实",
    )
    return store, source_db, project.workspace_ref, items, project_memories


@pytest.mark.asyncio
async def test_v14_to_v15_project_memory_migration_is_quarantined_and_scoped(
    tmp_path: Path,
) -> None:
    """Migrate only the registered project and preserve Core history."""

    old_store, source_db, project_scope, old_items, project_memories = _fresh_source_database(
        tmp_path
    )
    old_version_rows = len(old_store.list_memory_versions("legacy-project-a"))
    with old_store._connect() as connection:  # noqa: SLF001 - acceptance readback
        old_item_count = int(connection.execute("SELECT count(*) FROM items").fetchone()[0])
        old_project_count = int(
            connection.execute(
                "SELECT count(*) FROM memory_versions WHERE project_scope = ?",
                (project_scope,),
            ).fetchone()[0]
        )
    assert old_version_rows == 2
    assert old_item_count == len(old_items) == 2
    assert old_project_count == len(project_memories) == 2

    # This is the only schema upgrade in the drill.  It operates on the
    # synthetic copy above, never on a configured/user database.
    assert old_store.initialize() is None
    assert old_store.schema_version() == 16
    assert [row["version"] for row in old_store.list_applied_migrations()] == list(range(1, 17))

    app = create_app(source_db)
    service = app.state.operant_service
    manager = MemoryManager(service)
    app.state.memory_manager = manager
    service.memory_manager = manager
    try:
        project_result = await manager.execute(
            ManagementCommand(
                action="project_create",
                name="合成迁移项目",
                workspace_path=project_scope,
            )
        )
        project_id = project_result.state.projects[0].project_id
        install_result = await manager.execute(
            ManagementCommand(
                action="plugin_install",
                plugin_id="memory-standard",
                mode="trusted_in_process",
            )
        )
        installation_id = install_result.state.installations[-1].installation_id
        dataset_id = install_result.state.installations[-1].dataset_id
        await manager.execute(
            ManagementCommand(
                action="binding_select",
                project_id=project_id,
                installation_id=installation_id,
            )
        )
        migrated = await manager.execute(
            ManagementCommand(action="memory_migrate", project_id=project_id),
            idempotency_key="synthetic-v14-memory-migrate",
        )

        # Two old versions for one record become two immutable versions.  The
        # other project's row is excluded by the registered workspace scope.
        assert migrated.status == "completed"
        assert "迁移 2 条" in migrated.message
        assert migrated.state.datasets[-1].record_count == 1
        ledger = manager.ledger
        versions = ledger.list_versions(dataset_id, "legacy-project-a")
        assert len(versions) == 2
        assert [version.ref.version for version in versions] == [1, 2]
        assert all(version.evidence == "legacy_unverified" for version in versions)
        assert all(version.owner.dataset_id == dataset_id for version in versions)
        assert all(version.owner.owner_namespace == f"dataset:{dataset_id}" for version in versions)
        assert all(version.scope.project_id == project_id for version in versions)
        candidate_versions = ledger.query(
            dataset_id,
            scope=versions[0].scope,
            include_candidates=True,
            include_legacy=True,
        )
        assert {version.ref.record_id for version in candidate_versions} == {"legacy-project-a"}
        assert {version.ref.version for version in candidate_versions} == {1, 2}
        assert (
            ledger.count(
                dataset_id,
                scope=versions[0].scope,
                include_candidates=True,
                include_legacy=True,
            )
            == len(candidate_versions)
            == 2
        )
        assert ledger.count(dataset_id, scope=versions[0].scope) == 0
        assert ledger.get_head(dataset_id, "legacy-project-a").published_version is None
        assert ledger.list_proposals(dataset_id, record_id="legacy-project-a")[0].state == "pending"
        assert (
            ledger.query(
                dataset_id,
                text="另一个项目",
                scope=versions[0].scope,
                include_candidates=True,
                include_legacy=True,
            )
            == []
        )

        # The source Core rows and canonical Items remain intact after import.
        with old_store._connect() as connection:  # noqa: SLF001 - acceptance readback
            assert (
                int(connection.execute("SELECT count(*) FROM items").fetchone()[0])
                == old_item_count
            )
            assert (
                int(
                    connection.execute(
                        "SELECT count(*) FROM memory_versions WHERE project_scope = ?",
                        (project_scope,),
                    ).fetchone()[0]
                )
                == old_project_count
            )
        assert old_store.list_items(old_items[0].thread_id) == old_items

        # The pre-B2-3 REST writer is explicitly rejected once plugin mode is
        # enabled, so an old client cannot bypass Proposal/CAS.
        profile = service.add_model_profile(
            ModelProfile(
                id="synthetic-v14-profile",
                name="synthetic-v14-profile",
                model_id="synthetic-v14-model",
                base_url="https://provider.invalid/v1",
                secret_ref="OPERANT_SYNTHETIC_KEY",
            )
        )
        role = service.create_role(
            RolePreset(
                id="synthetic-v14-role",
                name="synthetic-v14-role",
                system_prompt="synthetic migration acceptance",
                model_profile_id=profile.id,
            )
        )
        session = service.create_session(role.id)
        with TestClient(app) as client:
            old_write = client.post(
                "/v1/memories",
                json={
                    "session_id": session.id,
                    "kind": "project",
                    "content": "old writer must be rejected",
                    "project_scope": project_scope,
                    "confirmed": True,
                },
            )
        assert old_write.status_code == 400
        assert old_write.json()["detail"].startswith(
            "schema_upgrade_required: use B2-3 dataset Proposal/CAS commands"
        )

        # A v15 database cannot be silently opened as v14 by an old binary.
        with pytest.raises(MigrationError, match="downgrades require rollback"):
            SQLiteStore(source_db).migrate(target_version=14)
    finally:
        await manager.close()
        service.close()
