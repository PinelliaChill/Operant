from __future__ import annotations

from pathlib import Path

import pytest

from operant.application.default_skill_pack import (
    DEFAULT_SKILL_PACK,
    install_default_skill_pack,
)
from operant.application.service import ApplicationService
from operant.contracts.b2_3 import ManagementCommand
from operant.memory_plugins.manager import MemoryManager
from operant.persistence.sqlite import SQLiteStore


class UnusedProvider:
    pass


def _write_skills(root: Path, names: tuple[str, ...]) -> None:
    root.mkdir()
    for name in names:
        path = root / name
        path.mkdir()
        (path / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name} capability\n"
            f"{'disable-model-invocation: true' if name == 'grill-me' else ''}\n"
            f"---\nVerified {name} package.\n"
        )


@pytest.mark.asyncio
async def test_default_skill_pack_installs_enables_and_loads_real_packages(tmp_path: Path) -> None:
    root = tmp_path / "source-skills"
    _write_skills(root, tuple(entry.skill_name for entry in DEFAULT_SKILL_PACK))
    service = ApplicationService(SQLiteStore(tmp_path / "core.sqlite"), UnusedProvider())  # type: ignore[arg-type]
    service.initialize()
    manager = MemoryManager(service, skill_roots={"trusted": root})
    try:
        project = await manager.execute(
            ManagementCommand(
                action="project_create", name="pack-test", workspace_path=str(tmp_path)
            )
        )
        project_id = project.state.projects[-1].project_id
        ids = await install_default_skill_pack(manager, project_id=project_id)
        assert len(ids) == len(DEFAULT_SKILL_PACK)
        assert await install_default_skill_pack(manager, project_id=project_id) == ids
        loaded = manager.begin_skill_run(str(tmp_path.resolve()), "pack-run")
        try:
            for entry in DEFAULT_SKILL_PACK:
                assert (f"Verified {entry.skill_name} package." in loaded) == (
                    entry.skill_name != "grill-me"
                )
        finally:
            manager.release_skill_run("pack-run")
        invoked = manager.begin_skill_run(
            str(tmp_path.resolve()), "explicit-grill", selected_ids=(ids[0],)
        )
        try:
            assert "Verified grill-me package." in invoked
            assert "Verified documents package." not in invoked
        finally:
            manager.release_skill_run("explicit-grill")
    finally:
        await manager.close()
        service.close()


@pytest.mark.asyncio
async def test_default_skill_pack_preflights_missing_package(tmp_path: Path) -> None:
    root = tmp_path / "source-skills"
    _write_skills(root, (DEFAULT_SKILL_PACK[0].skill_name,))
    service = ApplicationService(SQLiteStore(tmp_path / "core.sqlite"), UnusedProvider())  # type: ignore[arg-type]
    service.initialize()
    manager = MemoryManager(service, skill_roots={"trusted": root})
    try:
        project = await manager.execute(
            ManagementCommand(
                action="project_create", name="pack-test", workspace_path=str(tmp_path)
            )
        )
        project_id = project.state.projects[-1].project_id
        with pytest.raises(ValueError, match="exactly one discovered candidate"):
            await install_default_skill_pack(manager, project_id=project_id)
        assert manager.projection().skills == []
    finally:
        await manager.close()
        service.close()
