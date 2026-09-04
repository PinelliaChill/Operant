from __future__ import annotations

import os
from pathlib import Path

import pytest

import operant.skills.discovery as discovery_module
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


def test_fails_closed_without_no_follow_primitive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    monkeypatch.delattr(discovery_module.os, "O_NOFOLLOW")

    with pytest.raises(RuntimeError, match="safe skill discovery is unavailable"):
        SkillDiscovery([root])


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="O_NOFOLLOW is unavailable")
def test_candidate_parent_symlink_swap_does_not_read_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "skills"
    candidate = root / "candidate"
    _write_skill(candidate)
    outside = tmp_path / "outside"
    _write_skill(
        outside,
        frontmatter="name: outside-candidate\ndescription: Must not be read",
        body="outside candidate body",
    )
    discovery = SkillDiscovery([root])
    real_open = os.open
    swapped = False

    def racing_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == "candidate" and dir_fd is not None and not swapped:
            swapped = True
            candidate.rename(root / "candidate-original")
            candidate.symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(discovery_module.os, "open", racing_open)

    result = discovery.discover()

    assert swapped is True
    assert result.candidates == ()
    assert all(candidate.name != "outside-candidate" for candidate in result.candidates)


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="O_NOFOLLOW is unavailable")
@pytest.mark.parametrize("swapped_name", ["scripts", "nested"])
def test_resource_directory_symlink_swap_does_not_read_outside(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    swapped_name: str,
) -> None:
    root = tmp_path / "skills"
    candidate = root / "candidate"
    _write_skill(candidate)
    scripts = candidate / "scripts"
    nested = scripts / "nested"
    nested.mkdir(parents=True)
    (nested / "safe.py").write_text("safe", encoding="utf-8")
    outside = tmp_path / "outside-resources"
    outside.mkdir()
    (outside / "outside.py").write_text("outside resource body", encoding="utf-8")
    discovery = SkillDiscovery([root])
    real_open = os.open
    swapped = False

    def racing_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == swapped_name and dir_fd is not None and not swapped:
            swapped = True
            target = scripts if swapped_name == "scripts" else nested
            target.rename(target.with_name(f"{swapped_name}-original"))
            target.symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(discovery_module.os, "open", racing_open)

    result = discovery.discover()

    assert swapped is True
    assert result.candidates == ()
    assert all(
        resource.relative_path != "scripts/outside.py"
        for candidate_result in result.candidates
        for resource in candidate_result.resources
    )


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="O_NOFOLLOW is unavailable")
def test_registered_root_replacement_is_rejected_by_identity(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(root / "safe")
    discovery = SkillDiscovery([root])
    root.rename(tmp_path / "original-skills")
    _write_skill(
        root / "outside",
        frontmatter="name: replacement-root\ndescription: Must not be read",
    )

    result = discovery.discover()

    assert result.candidates == ()
    assert [issue.code for issue in result.issues] == ["root_unreadable"]


def test_root_version_change_discards_staged_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "skills"
    _write_skill(root / "safe")
    discovery = SkillDiscovery([root])
    original_read_candidate = discovery._read_candidate
    changed = False

    def changing_read_candidate(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        nonlocal changed
        result = original_read_candidate(*args, **kwargs)  # type: ignore[arg-type]
        if not changed:
            changed = True
            (root / "late-entry").write_text("changed", encoding="utf-8")
        return result

    monkeypatch.setattr(discovery, "_read_candidate", changing_read_candidate)

    result = discovery.discover()

    assert changed is True
    assert result.candidates == ()
    assert [issue.code for issue in result.issues] == ["root_unreadable"]
