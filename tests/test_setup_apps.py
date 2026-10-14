from __future__ import annotations

import plistlib
from pathlib import Path

from operant.api_setup_apps import local_applications


def _bundle(root: Path, name: str, value: object) -> Path:
    path = root / f"{name}.app" / "Contents"
    path.mkdir(parents=True)
    (path / "Info.plist").write_bytes(plistlib.dumps(value))
    return path.parent


def test_application_targets_skip_bad_bundles_and_deduplicate(tmp_path: Path) -> None:
    root = tmp_path / "Applications"
    _bundle(root, "Editor", {"CFBundleIdentifier": "test.editor", "CFBundleName": "编辑器"})
    _bundle(root / "Utilities", "Duplicate", {"CFBundleIdentifier": "test.editor"})
    _bundle(root, "Invalid", ["a plist without an application dictionary"])
    _bundle(root, "Control", {"CFBundleIdentifier": "test.control", "CFBundleName": "a\nb"})
    _bundle(root, "InvalidID", {"CFBundleIdentifier": "not an id"})
    broken = _bundle(root, "Broken", {"CFBundleIdentifier": "test.broken"})
    (broken / "Contents" / "Info.plist").write_text("not a plist")
    assert [item.model_dump() for item in local_applications((root, tmp_path / "missing"))] == [
        {"bundle_id": "test.editor", "name": "编辑器"}
    ]


def test_application_targets_do_not_follow_links_or_unbounded_files(tmp_path: Path) -> None:
    outside = _bundle(tmp_path / "outside", "Private", {"CFBundleIdentifier": "test.private"})
    root = tmp_path / "Applications"
    root.mkdir()
    (root / "Linked.app").symlink_to(outside, target_is_directory=True)
    contents_link = root / "ContentsLink.app"
    contents_link.mkdir()
    (contents_link / "Contents").symlink_to(outside / "Contents", target_is_directory=True)
    file_link = root / "FileLink.app" / "Contents"
    file_link.mkdir(parents=True)
    (file_link / "Info.plist").symlink_to(outside / "Contents" / "Info.plist")
    (root / "Utilities").symlink_to(outside.parent, target_is_directory=True)
    oversized = _bundle(root, "Large", {"CFBundleIdentifier": "test.large"})
    (oversized / "Contents" / "Info.plist").write_bytes(b"x" * 1_048_577)
    assert local_applications((root,)) == []
