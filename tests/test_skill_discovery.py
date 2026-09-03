from __future__ import annotations

import os
from pathlib import Path

import pytest

from operant.skills import SkillDiscovery, SkillDiscoveryLimits


def _write_skill(
    directory: Path, *, frontmatter: str | None = None, body: str = "Do work."
) -> None:
    directory.mkdir(parents=True)
    header = frontmatter or "name: safe-skill\ndescription: A safe test skill"
    (directory / "SKILL.md").write_text(f"---\n{header}\n---\n{body}", encoding="utf-8")


def test_discovers_only_explicit_root_and_returns_untrusted_snapshot(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    skill = allowed / "safe"
    _write_skill(skill)
    (skill / "scripts").mkdir()
    (skill / "scripts" / "run.py").write_text("print('safe')\n", encoding="utf-8")
    (skill / "references").mkdir()
    (skill / "references" / "guide.md").write_text("guide\n", encoding="utf-8")
    _write_skill(outside / "not-visible")

    result = SkillDiscovery([allowed]).discover()

    assert result.issues == ()
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.name == "safe-skill"
    assert candidate.trust == "untrusted_candidate"
    assert [resource.relative_path for resource in candidate.resources] == [
        "scripts/run.py",
        "references/guide.md",
    ]
    assert all(resource.sha256 for resource in candidate.resources)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink is unavailable")
def test_rejects_candidate_and_resource_symlink_escape(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    _write_skill(outside)
    (allowed / "linked-skill").symlink_to(outside, target_is_directory=True)
    linked_resource = allowed / "resource-link"
    _write_skill(linked_resource)
    (linked_resource / "scripts").mkdir()
    (linked_resource / "scripts" / "escape.py").symlink_to(outside / "SKILL.md")

    result = SkillDiscovery([allowed]).discover()

    assert result.candidates == ()
    assert {issue.code for issue in result.issues} == {"symlink_rejected", "candidate_rejected"}


@pytest.mark.parametrize(
    "frontmatter",
    [
        "name: unsafe\ndescription: &anchor payload",
        "name: unsafe\ndescription: !!python/object:os.system payload",
        "name: unsafe\nname: duplicate\ndescription: duplicate",
        "name: unsafe\nunknown_key: value\ndescription: unsupported",
        "name: unsafe\n  nested: value\ndescription: nested",
        'name: unsafe\ndescription: ["ok", 1]',
    ],
)
def test_rejects_malicious_or_ambiguous_frontmatter(tmp_path: Path, frontmatter: str) -> None:
    root = tmp_path / "skills"
    _write_skill(root / "unsafe", frontmatter=frontmatter)

    result = SkillDiscovery([root]).discover()

    assert result.candidates == ()
    assert result.issues[0].code == "candidate_rejected"


def test_rejects_oversized_manifest_and_bounded_resource_set(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(root / "large", body="x" * 500)
    resource_skill = root / "resources"
    _write_skill(
        resource_skill,
        frontmatter="name: resources\ndescription: Resource limit test",
    )
    (resource_skill / "scripts").mkdir()
    (resource_skill / "scripts" / "a.py").write_text("a", encoding="utf-8")
    (resource_skill / "scripts" / "b.py").write_text("b", encoding="utf-8")

    result = SkillDiscovery(
        [root],
        limits=SkillDiscoveryLimits(max_manifest_bytes=256, max_resource_files=1),
    ).discover()

    assert result.candidates == ()
    assert len(result.issues) == 2
    assert all(issue.code == "candidate_rejected" for issue in result.issues)


def test_requires_real_absolute_allowlist_root(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    with pytest.raises(ValueError, match="absolute"):
        SkillDiscovery([Path("relative")])
    with pytest.raises(ValueError, match="not a symlink"):
        SkillDiscovery([link])
