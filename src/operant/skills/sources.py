"""Conventional Skill locations; listing them never installs a Skill."""

from pathlib import Path

from operant.package_resources import default_skill_root


def default_skill_roots(workspace: Path | None = None) -> dict[str, Path]:
    """Return bounded source locations without creating or scanning them."""
    home = Path.home()
    result = {
        "operant-default": default_skill_root(),
        "home-agents": home / ".agents" / "skills",
        "home-codex": home / ".codex" / "skills",
        "home-claude": home / ".claude" / "skills",
    }
    if workspace is not None:
        if not workspace.is_absolute():
            raise ValueError("workspace Skill source must be absolute")
        result.update(
            {
                "workspace-agents": workspace / ".agents" / "skills",
                "workspace-codex": workspace / ".codex" / "skills",
                "workspace-claude": workspace / ".claude" / "skills",
                "workspace-operant": workspace / ".operant" / "skills",
            }
        )
    return result
