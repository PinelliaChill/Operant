#!/usr/bin/env python3
"""Isolated candidate data snapshots and macOS desktop launcher."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import shutil
import socket
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

MANIFEST = "snapshot-manifest.json"
DATABASE = "operant.sqlite3"
TRANSIENT = {DATABASE + "-wal", DATABASE + "-shm", DATABASE + "-journal"}
REPOSITORY = Path(__file__).parents[1]
BUILD_PREFIXES = (
    "src/",
    "plugins/",
    "sdk/",
    "clients/gui/",
    "clients/desktop/",
    "clients/tui/",
    "licenses/",
)
BUILD_FILES = {"pyproject.toml", "uv.lock", "README.md", "LICENSE", "COMMERCIAL.md"}


def _absolute(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError(f"absolute path required: {path}")
    return path.resolve(strict=False)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _files(root: Path) -> list[Path]:
    result: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"symbolic links are not supported in data snapshots: {path}")
        if path.is_file():
            result.append(path)
        elif not path.is_dir():
            raise ValueError(f"unsupported data entry: {path}")
    return result


def _check_database(path: Path) -> int:
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("database quick_check failed")
        row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        if row is None or row[0] is None:
            raise ValueError("database has no Operant schema version")
        return int(row[0])
    finally:
        connection.close()


def backup(source: Path, destination: Path) -> dict[str, object]:
    source, destination = _absolute(source), _absolute(destination)
    if not source.is_dir() or not (source / DATABASE).is_file():
        raise ValueError("source must contain operant.sqlite3")
    if destination == source or source in destination.parents or destination in source.parents:
        raise ValueError("source and snapshot directories must be separate")
    if destination.exists():
        raise ValueError("snapshot destination already exists")
    originals = _files(source)
    destination.mkdir(parents=True, mode=0o700)
    try:
        for path in originals:
            relative = path.relative_to(source)
            if relative.name in TRANSIENT or relative == Path(DATABASE):
                continue
            target = destination / relative
            target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            shutil.copy2(path, target)
            os.chmod(target, 0o600)
        with (
            closing(sqlite3.connect((source / DATABASE).as_uri() + "?mode=ro", uri=True)) as live,
            closing(sqlite3.connect(destination / DATABASE)) as saved,
        ):
            live.backup(saved)
        os.chmod(destination / DATABASE, 0o600)
        for directory in (path for path in destination.rglob("*") if path.is_dir()):
            os.chmod(directory, 0o700)
        version = _check_database(destination / DATABASE)
        entries = {
            str(path.relative_to(destination)): _sha256(path) for path in _files(destination)
        }
        manifest: dict[str, object] = {
            "format": "operant-data-snapshot-v1",
            "database": DATABASE,
            "operant_schema_version": version,
            "files": entries,
        }
        (destination / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        os.chmod(destination / MANIFEST, 0o600)
        verify(destination)
        return manifest
    except Exception:
        shutil.rmtree(destination)
        raise


def verify(snapshot: Path) -> dict[str, object]:
    snapshot = _absolute(snapshot)
    manifest_path = snapshot / MANIFEST
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError("snapshot manifest is missing")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("format") != "operant-data-snapshot-v1" or manifest.get("database") != DATABASE:
        raise ValueError("unsupported snapshot manifest")
    expected = manifest.get("files")
    if not isinstance(expected, dict) or DATABASE not in expected:
        raise ValueError("snapshot file list is incomplete")
    actual = {
        str(path.relative_to(snapshot)): _sha256(path)
        for path in _files(snapshot)
        if path != manifest_path
    }
    if actual != expected:
        raise ValueError("snapshot file hashes do not match")
    if _check_database(snapshot / DATABASE) != manifest.get("operant_schema_version"):
        raise ValueError("snapshot database version changed")
    return manifest


def restore(snapshot: Path, destination: Path) -> None:
    snapshot, destination = _absolute(snapshot), _absolute(destination)
    verify(snapshot)
    if (
        destination == snapshot
        or snapshot in destination.parents
        or destination in snapshot.parents
    ):
        raise ValueError("snapshot and restore directories must be separate")
    if destination.exists():
        raise ValueError("restore destination already exists")
    shutil.copytree(snapshot, destination, symlinks=False)
    (destination / MANIFEST).unlink()
    os.chmod(destination, 0o700)
    _check_database(destination / DATABASE)


def source_inputs(repository: Path) -> dict[str, object]:
    """Record the exact tracked and nonignored untracked files used by a candidate."""

    repository = _absolute(repository)

    def git(*arguments: str) -> bytes:
        return subprocess.run(
            ["git", "-c", "core.fsmonitor=false", *arguments],
            cwd=repository,
            check=True,
            capture_output=True,
        ).stdout

    revision = git("rev-parse", "HEAD").decode().strip()
    names = sorted(
        set(git("ls-files", "--cached", "--others", "--exclude-standard", "-z").split(b"\0"))
        - {b""}
    )
    files: dict[str, str] = {}
    for raw in names:
        relative = Path(os.fsdecode(raw))
        relative_text = str(relative)
        if relative_text not in BUILD_FILES and not relative_text.startswith(BUILD_PREFIXES):
            continue
        path = repository / relative
        if path.is_symlink():
            raise ValueError(f"source input is a symbolic link: {relative}")
        if path.is_file():
            files[str(relative)] = _sha256(path)
    return {
        "format": "operant-candidate-inputs-v1",
        "scope": "Core/TUI/Desktop package inputs only",
        "git_head": revision,
        "dirty": bool(git("status", "--porcelain", "--untracked-files=normal")),
        "files": files,
    }


def verify_source_inputs(
    repository: Path, manifest_path: Path, *, content_only: bool = False
) -> None:
    expected = json.loads(_absolute(manifest_path).read_text())
    actual = source_inputs(repository)
    if content_only:
        expected = expected.get("files")
        actual = actual["files"]
    if expected != actual:
        raise ValueError("candidate source inputs changed")


def launch_desktop(app: Path, venv: Path, data: Path) -> None:
    app, venv, data = _absolute(app), _absolute(venv), _absolute(data)
    info_path = app / "Contents" / "Info.plist"
    if not info_path.is_file():
        raise ValueError("candidate App metadata is missing")
    with info_path.open("rb") as stream:
        executable = plistlib.load(stream).get("CFBundleExecutable")
    if (
        not isinstance(executable, str)
        or executable in {"", ".", ".."}
        or Path(executable).name != executable
    ):
        raise ValueError("candidate App executable name is invalid")
    binary = app / "Contents" / "MacOS" / executable
    operant = venv / "bin" / "operant"
    if not binary.is_file() or not operant.is_file():
        raise ValueError("candidate App or installed Core executable is missing")
    if not data.is_dir() or not (data / DATABASE).is_file():
        raise ValueError("candidate data directory must contain an initialized database")
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", 8000))
        except OSError as exc:
            raise ValueError("port 8000 is occupied; stop the other Core first") from exc
    environment = os.environ.copy()
    environment["PATH"] = str(venv / "bin") + os.pathsep + environment.get("PATH", "")
    environment["OPERANT_DB_PATH"] = str(data / DATABASE)
    environment.pop("OPERANT_CORE_URL", None)
    subprocess.run([str(binary)], cwd=data, env=environment, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("backup", "restore"):
        command = commands.add_parser(name)
        command.add_argument("--source", type=Path, required=True)
        command.add_argument("--destination", type=Path, required=True)
    check = commands.add_parser("verify")
    check.add_argument("--snapshot", type=Path, required=True)
    inputs = commands.add_parser("record-inputs")
    inputs.add_argument("--repository", type=Path, default=REPOSITORY)
    inputs.add_argument("--output", type=Path, required=True)
    verify_inputs = commands.add_parser("verify-inputs")
    verify_inputs.add_argument("--repository", type=Path, default=REPOSITORY)
    verify_inputs.add_argument("--manifest", type=Path, required=True)
    verify_inputs.add_argument("--content-only", action="store_true")
    desktop = commands.add_parser("launch-desktop")
    desktop.add_argument("--app", type=Path, required=True)
    desktop.add_argument("--venv", type=Path, required=True)
    desktop.add_argument("--data", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "backup":
            backup(args.source, args.destination)
        elif args.command == "restore":
            restore(args.source, args.destination)
        elif args.command == "verify":
            verify(args.snapshot)
        elif args.command == "record-inputs":
            output = _absolute(args.output)
            output.write_text(
                json.dumps(source_inputs(args.repository), indent=2, sort_keys=True) + "\n"
            )
        elif args.command == "verify-inputs":
            verify_source_inputs(args.repository, args.manifest, content_only=args.content_only)
        else:
            launch_desktop(args.app, args.venv, args.data)
    except (OSError, ValueError, sqlite3.Error) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps({"command": args.command, "status": "ok"}))


if __name__ == "__main__":
    main()
