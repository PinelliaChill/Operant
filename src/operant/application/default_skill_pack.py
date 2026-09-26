"""Optional default capabilities built from verified, installed Skills."""

from __future__ import annotations

from dataclasses import dataclass

from operant.contracts.b2_3 import ManagementCommand
from operant.memory_plugins.manager import MemoryManager
from operant.persistence.phase45 import SQLitePhase45Repository


@dataclass(frozen=True)
class SkillPackEntry:
    capability: str
    skill_name: str


DEFAULT_SKILL_PACK: tuple[SkillPackEntry, ...] = (
    SkillPackEntry("clarifying interview", "grill-me"),
    SkillPackEntry("Word documents", "documents"),
    SkillPackEntry("presentations", "presentations"),
    SkillPackEntry("PDF documents", "pdf"),
    SkillPackEntry("create Skills", "skill-creator"),
    SkillPackEntry("find Skills", "find-skills"),
)


async def install_default_skill_pack(manager: MemoryManager, *, project_id: str) -> tuple[str, ...]:
    """Install and enable configured candidates for an explicit project.

    All names must resolve uniquely from the configured allowlisted roots
    before the first install. This never downloads packages or grants tools.
    """

    if not any(
        project.project_id == project_id and not project.archived
        for project in manager.projection().projects
    ):
        raise ValueError("an active project is required for the default Skill pack")
    if not manager.skill_roots:
        raise ValueError("trusted Skill roots are not configured")
    await manager.execute(ManagementCommand(action="skill_discover"))
    catalog = tuple(
        candidate
        for candidate in SQLitePhase45Repository(manager.store).list_skill_candidates()
        if candidate["root_ref"] in manager.skill_roots
    )
    selected = {}
    for entry in DEFAULT_SKILL_PACK:
        matches = [
            candidate
            for candidate in catalog
            if candidate["name"].casefold() == entry.skill_name.casefold()
        ]
        if len(matches) != 1:
            raise ValueError(
                f"Skill {entry.skill_name!r} must have exactly one discovered candidate"
            )
        selected[entry.skill_name] = matches[0]["candidate_id"]

    installed_ids: list[str] = []
    for entry in DEFAULT_SKILL_PACK:
        package_ref = selected[entry.skill_name]
        current = [
            item
            for item in manager.projection().skills
            if item.package_ref == package_ref and item.state != "uninstalled"
        ]
        if len(current) > 1:
            raise ValueError(f"Skill {entry.skill_name!r} has ambiguous installations")
        if not current:
            result = await manager.execute(
                ManagementCommand(action="skill_install", package_ref=package_ref)
            )
            current = [
                item
                for item in result.state.skills
                if item.package_ref == package_ref and item.state != "uninstalled"
            ]
        if len(current) != 1:
            raise RuntimeError(f"Skill {entry.skill_name!r} installation was not recorded")
        skill = current[0]
        if skill.state != "installed" or project_id not in skill.project_ids:
            await manager.execute(
                ManagementCommand(
                    action="skill_enable", skill_id=skill.skill_id, project_id=project_id
                )
            )
        installed_ids.append(skill.skill_id)

    state = manager.projection()
    for skill_id in installed_ids:
        if not any(
            item.skill_id == skill_id
            and item.state == "installed"
            and project_id in item.project_ids
            for item in state.skills
        ):
            raise RuntimeError(f"Skill {skill_id} is not enabled for the project")
    return tuple(installed_ids)
