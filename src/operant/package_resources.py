"""Locate immutable distribution resources in a wheel or source checkout."""

from pathlib import Path


def protocol_schema_path(filename: str) -> Path:
    """Use the package-owned schema directory, retaining checkout development."""
    if Path(filename).name != filename:
        raise ValueError("schema resource must be a file name")
    package = Path(__file__).resolve().parent
    bundled = package / "protocol_schema"
    root = bundled if bundled.is_dir() else package.parents[1] / "sdk/protocol/schema"
    return root / filename
