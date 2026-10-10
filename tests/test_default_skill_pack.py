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
from operant.persistence.phase45 import SQLitePhase45Repository
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
async def test_default_skill_pack_installs_enables_and_loads_real_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "bundled-skills"
    _write_skills(root, tuple(entry.skill_name for entry in DEFAULT_SKILL_PACK))
    monkeypatch.setattr("operant.application.default_skill_pack.default_skill_root", lambda: root)
    external = tmp_path / "source-skills"
    _write_skills(external, ("documents",))
    service = ApplicationService(SQLiteStore(tmp_path / "core.sqlite"), UnusedProvider())  # type: ignore[arg-type]
    service.initialize()
    manager = MemoryManager(service, skill_roots={"trusted": external})
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
        candidates = SQLitePhase45Repository(manager.store).list_skill_candidates()
        assert any(item["root_ref"] == "trusted" for item in candidates)
        assert all(
            skill.package_ref
            in {
                item["candidate_id"] for item in candidates if item["root_ref"] == "operant-default"
            }
            for skill in manager.projection().skills
        )
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
async def test_default_skill_pack_rejects_absent_bundle_without_external_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "operant.application.default_skill_pack.default_skill_root", lambda: tmp_path / "absent"
    )
    external = tmp_path / "source-skills"
    _write_skills(external, tuple(entry.skill_name for entry in DEFAULT_SKILL_PACK))
    service = ApplicationService(SQLiteStore(tmp_path / "core.sqlite"), UnusedProvider())  # type: ignore[arg-type]
    service.initialize()
    manager = MemoryManager(service, skill_roots={"trusted": external})
    try:
        project = await manager.execute(
            ManagementCommand(
                action="project_create", name="pack-test", workspace_path=str(tmp_path)
            )
        )
        with pytest.raises(ValueError, match="bundled default Skill root is unavailable"):
            await install_default_skill_pack(
                manager, project_id=project.state.projects[-1].project_id
            )
        assert manager.projection().skills == []
    finally:
        await manager.close()
        service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("broken_manifest", [False, True])
async def test_default_skill_pack_rejects_missing_or_broken_bundled_skill_without_external_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, broken_manifest: bool
) -> None:
    bundled = tmp_path / "bundled-skills"
    _write_skills(
        bundled,
        tuple(entry.skill_name for entry in DEFAULT_SKILL_PACK if entry.skill_name != "documents"),
    )
    if broken_manifest:
        broken = bundled / "documents"
        broken.mkdir()
        (broken / "SKILL.md").write_text(
            "---\nname: documents\ndescription: !!python/object:os.system unsafe\n---\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(
        "operant.application.default_skill_pack.default_skill_root", lambda: bundled
    )
    root = tmp_path / "source-skills"
    _write_skills(root, ("documents",))
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


@pytest.mark.asyncio
async def test_distributable_default_pack_installs_without_host_roots(tmp_path: Path) -> None:
    service = ApplicationService(SQLiteStore(tmp_path / "core.sqlite"), UnusedProvider())  # type: ignore[arg-type]
    service.initialize()
    manager = MemoryManager(service)
    try:
        project = await manager.execute(
            ManagementCommand(action="project_create", name="bundled", workspace_path=str(tmp_path))
        )
        ids = await install_default_skill_pack(
            manager, project_id=project.state.projects[-1].project_id
        )
        assert len(ids) == 6
        assert all(
            item["root_ref"] == "operant-default"
            for item in SQLitePhase45Repository(manager.store).list_skill_candidates()
            if item["name"] in {entry.skill_name for entry in DEFAULT_SKILL_PACK}
        )
        loaded = manager.begin_skill_run(str(tmp_path.resolve()), "bundled-test")
        try:
            assert "operant.default_skill_tools make docx" in loaded
            assert "Runtime Python:" in loaded
            assert "Clarify a plan" not in loaded
        finally:
            manager.release_skill_run("bundled-test")
    finally:
        await manager.close()
        service.close()


@pytest.mark.asyncio
async def test_default_pack_rejects_reserved_root_redirect(tmp_path: Path) -> None:
    redirected = tmp_path / "redirected"
    _write_skills(redirected, tuple(entry.skill_name for entry in DEFAULT_SKILL_PACK))
    service = ApplicationService(SQLiteStore(tmp_path / "core.sqlite"), UnusedProvider())  # type: ignore[arg-type]
    service.initialize()
    manager = MemoryManager(service, skill_roots={"operant-default": redirected})
    try:
        project = await manager.execute(
            ManagementCommand(
                action="project_create", name="redirected", workspace_path=str(tmp_path)
            )
        )
        with pytest.raises(ValueError, match="reserved operant-default Skill root"):
            await install_default_skill_pack(
                manager, project_id=project.state.projects[-1].project_id
            )
        assert manager.projection().skills == []
    finally:
        await manager.close()
        service.close()
