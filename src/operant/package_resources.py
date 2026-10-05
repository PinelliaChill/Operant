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


def default_skill_root() -> Path:
    """Locate the distributable, opt-in default Skill candidates."""
    package = Path(__file__).resolve().parent
    bundled = package / "bundled_plugins" / "default-skills"
    if bundled.is_dir():
        return bundled
    return package.parents[1] / "plugins" / "default-skills"
