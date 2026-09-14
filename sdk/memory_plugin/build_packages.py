"""Build or refresh the two checked-in stdlib memory package directories.

The builder intentionally copies a tiny runtime only.  It never copies the
project virtualenv, Core source, credentials, or a user database.  A manifest
is returned as a dictionary so the caller can validate it with the frozen
``PluginManifest`` model before installation.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

try:
    from .package import build_manifest
except ImportError:  # Allows ``python sdk/memory_plugin/build_packages.py``.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from sdk.memory_plugin.package import build_manifest

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_SPECS: dict[str, dict[str, Any]] = {
    "memory-standard": {
        "plugin_id": "memory-standard",
        "config_schema_ref": "memory-standard-config.v1",
        "state_schema_version": "memory-standard-state.v1",
    },
    "memory-notebook": {
        "plugin_id": "memory-notebook",
        "config_schema_ref": "memory-notebook-config.v1",
        "state_schema_version": "memory-notebook-state.v1",
    },
}


def prepare_package(package_root: Path, *, destination: Path | None = None) -> Path:
    """Copy one package and the standalone runtime to ``destination``."""

    source = Path(package_root)
    target = source if destination is None else Path(destination) / source.name
    target.mkdir(parents=True, exist_ok=True)
    if target.resolve() != source.resolve():
        for current in sorted(
            source.rglob("*"), key=lambda path: path.relative_to(source).as_posix()
        ):
            if "__pycache__" in current.parts or current.suffix in {".pyc", ".pyo"}:
                continue
            relative = current.relative_to(source)
            destination_path = target / relative
            if current.is_dir():
                destination_path.mkdir(parents=True, exist_ok=True)
            elif current.is_file():
                destination_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(current, destination_path)
    source_root = Path(__file__).parent
    for source_name, target_name in (
        ("runtime.py", "memory_plugin_sdk.py"),
        ("dto.py", "memory_plugin_dto.py"),
        ("stdio.py", "memory_plugin_stdio.py"),
    ):
        runtime_source = source_root / source_name
        runtime_target = target / target_name
        if runtime_target.resolve() != runtime_source.resolve():
            shutil.copyfile(runtime_source, runtime_target)
    return target


def build_all(
    *,
    root: Path = ROOT,
    destination: Path | None = None,
    write_manifests_to: Path | None = None,
) -> dict[str, dict[str, Any]]:
    packages: dict[str, dict[str, Any]] = {}
    for name, spec in PACKAGE_SPECS.items():
        source = Path(root) / "plugins" / name
        target = prepare_package(source, destination=destination)
        packages[name] = build_manifest(target, **spec)
    if write_manifests_to is not None:
        output = Path(write_manifests_to)
        output.mkdir(parents=True, exist_ok=True)
        for name, manifest in packages.items():
            (output / f"{name}.manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
    return packages


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--manifests", type=Path)
    args = parser.parse_args()
    result = build_all(
        root=args.root,
        destination=args.destination,
        write_manifests_to=args.manifests,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
