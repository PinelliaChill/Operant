"""Dependency-free package identity and manifest helpers.

The digest algorithm intentionally mirrors ``operant.plugins.protocol`` so a
manifest produced here can be passed directly to the real PluginRegistry.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from collections.abc import Sequence
from pathlib import Path
from typing import Any

PLUGIN_SDK_VERSION = "operant-memory-sdk.v1"


def canonical_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))).encode(
        "utf-8"
    )


def _regular_file(path: Path) -> os.stat_result:
    item = path.lstat()
    if stat.S_ISLNK(item.st_mode) or not stat.S_ISREG(item.st_mode):
        raise ValueError(f"unsafe package file: {path}")
    return item


def package_digest(root: Path, *, max_files: int = 4_096, max_bytes: int = 100_000_000) -> str:
    root = Path(root)
    root_stat = root.lstat()
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise ValueError("plugin package must be a real directory")
    digest = hashlib.sha256()
    total = 0
    count = 0
    entries = sorted(
        (path for path in root.rglob("*") if path != root),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    for path in entries:
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        item = path.lstat()
        if stat.S_ISLNK(item.st_mode):
            raise ValueError("plugin package cannot contain symlinks")
        if stat.S_ISDIR(item.st_mode):
            continue
        _regular_file(path)
        data = path.read_bytes()
        count += 1
        total += len(data)
        if count > max_files or total > max_bytes:
            raise ValueError("plugin package exceeds the configured limit")
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(str(len(data)).encode("ascii"))
        digest.update(b"\0")
        digest.update(data)
    return digest.hexdigest()


def metadata_digest(package_root: Path, filename: str) -> str:
    path = Path(package_root) / filename
    _regular_file(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def config_schema_digest(package_root: Path, filename: str = "config.schema.json") -> str:
    return metadata_digest(package_root, filename)


def _id(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 200:
        raise ValueError(f"{label} must be a bounded identifier")
    if not (value[0].isalnum() and all(ch.isalnum() or ch in "_.:-" for ch in value)):
        raise ValueError(f"{label} contains unsupported characters")
    return value


def build_manifest(
    package_root: Path,
    *,
    plugin_id: str,
    plugin_version: str = "1.0.0",
    entrypoint: str = "plugin.py",
    capabilities: Sequence[str] = ("extract", "recall", "maintain", "on_index_event"),
    config_schema_ref: str,
    state_schema_version: str,
    memory_mb: int = 128,
    max_rpc_bytes: int = 262_144,
    max_concurrency: int = 2,
    export_supported: bool = True,
    import_supported: bool = True,
    recoverable: bool = True,
) -> dict[str, Any]:
    package_root = Path(package_root)
    for identifier, label in (
        (plugin_id, "plugin_id"),
        (plugin_version, "plugin_version"),
        (entrypoint, "entrypoint"),
        (config_schema_ref, "config_schema_ref"),
        (state_schema_version, "state_schema_version"),
    ):
        _id(identifier, label)
    deps = metadata_digest(package_root, "dependencies.json")
    permissions = metadata_digest(package_root, "permissions.json")
    schema = config_schema_digest(package_root)
    return {
        "plugin_id": plugin_id,
        "plugin_version": plugin_version,
        "sdk_version": PLUGIN_SDK_VERSION,
        "host_api_versions": [PLUGIN_SDK_VERSION],
        "package_digest": package_digest(package_root),
        "dependencies_digest": deps,
        "permissions_digest": permissions,
        "config_schema_ref": config_schema_ref,
        "config_schema_digest": schema,
        "state_schema_version": state_schema_version,
        "entrypoint": entrypoint,
        "capabilities": list(capabilities),
        "memory_mb": memory_mb,
        "max_rpc_bytes": max_rpc_bytes,
        "max_concurrency": max_concurrency,
        "export_supported": export_supported,
        "import_supported": import_supported,
        "recoverable": recoverable,
    }


def copy_runtime(destination: Path, *, source: Path | None = None) -> Path:
    """Copy the standalone stdlib runtime and DTO helpers into a package.

    This is intentionally explicit and bounded.  It copies only the two
    modules needed under ``-I -S`` and never copies a virtualenv or Core code.
    """

    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    source_root = Path(source) if source is not None else Path(__file__).parent
    shutil.copyfile(source_root / "runtime.py", destination / "memory_plugin_sdk.py")
    shutil.copyfile(source_root / "dto.py", destination / "memory_plugin_dto.py")
    shutil.copyfile(source_root / "stdio.py", destination / "memory_plugin_stdio.py")
    return destination


__all__ = [
    "PLUGIN_SDK_VERSION",
    "build_manifest",
    "canonical_json_bytes",
    "config_schema_digest",
    "copy_runtime",
    "metadata_digest",
    "package_digest",
]
