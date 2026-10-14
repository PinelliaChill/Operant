from __future__ import annotations

import importlib.util
import plistlib
import sqlite3
import stat
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "distribution.py"
SPEC = importlib.util.spec_from_file_location("distribution", SCRIPT)
assert SPEC and SPEC.loader
distribution = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(distribution)


def _data(root: Path) -> Path:
    root.mkdir()
    with sqlite3.connect(root / "operant.sqlite3") as connection:
        connection.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO schema_migrations VALUES (23)")
        connection.execute("CREATE TABLE records (value TEXT NOT NULL)")
        connection.execute("INSERT INTO records VALUES ('kept')")
    (root / "state").mkdir()
    (root / "state" / "note.txt").write_text("retained")
    return root


def test_snapshot_upgrade_and_rollback_keep_original_rows(tmp_path: Path) -> None:
    source = _data(tmp_path / "source")
    snapshot = tmp_path / "snapshot"
    upgraded = tmp_path / "upgrade"
    rollback = tmp_path / "rollback"

    distribution.backup(source, snapshot)
    assert stat.S_IMODE((snapshot / "operant.sqlite3").stat().st_mode) == 0o600
    assert stat.S_IMODE((snapshot / "state").stat().st_mode) == 0o700
    assert stat.S_IMODE((snapshot / "state" / "note.txt").stat().st_mode) == 0o600
    distribution.restore(snapshot, upgraded)
    with sqlite3.connect(upgraded / "operant.sqlite3") as connection:
        connection.execute("INSERT INTO records VALUES ('new-version')")
    distribution.restore(snapshot, rollback)

    assert distribution.verify(snapshot)["operant_schema_version"] == 23
    assert (rollback / "state" / "note.txt").read_text() == "retained"
    with sqlite3.connect(rollback / "operant.sqlite3") as connection:
        assert connection.execute("SELECT value FROM records").fetchall() == [("kept",)]
    with sqlite3.connect(upgraded / "operant.sqlite3") as connection:
        assert connection.execute("SELECT value FROM records").fetchall() == [
            ("kept",),
            ("new-version",),
        ]


def test_restore_rejects_tampering_and_existing_destination(tmp_path: Path) -> None:
    source = _data(tmp_path / "source")
    snapshot = tmp_path / "snapshot"
    distribution.backup(source, snapshot)
    with pytest.raises(ValueError, match="already exists"):
        distribution.restore(snapshot, source)
    (snapshot / "state" / "note.txt").write_text("changed")
    with pytest.raises(ValueError, match="hashes"):
        distribution.restore(snapshot, tmp_path / "restore")
    assert not (tmp_path / "restore").exists()


def test_backup_rejects_symlink_and_nested_destination(tmp_path: Path) -> None:
    source = _data(tmp_path / "source")
    (source / "external").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic links"):
        distribution.backup(source, tmp_path / "snapshot")
    assert not (tmp_path / "snapshot").exists()
    (source / "external").unlink()
    with pytest.raises(ValueError, match="separate"):
        distribution.backup(source, source / "snapshot")


def test_desktop_launcher_rejects_bundle_path_escape(tmp_path: Path) -> None:
    app = tmp_path / "Operant.app"
    (app / "Contents").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleExecutable": "../../outside"})
    )
    with pytest.raises(ValueError, match="executable name"):
        distribution.launch_desktop(app, tmp_path / "venv", tmp_path / "data")
