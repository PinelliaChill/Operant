from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

_SAFE_FRONTMATTER_KEYS = frozenset(
    {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
)
_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_DANGEROUS_YAML_RE = re.compile(r"(?:^|[\s\[{,:])(?:!!|![A-Za-z]|&[A-Za-z]|\*[A-Za-z])")


class SkillDiscoveryLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_roots: int = Field(default=16, ge=1, le=64)
    max_skill_directories: int = Field(default=256, ge=1, le=2_048)
    max_manifest_bytes: int = Field(default=128_000, ge=256, le=1_000_000)
    max_frontmatter_bytes: int = Field(default=16_000, ge=64, le=128_000)
    max_body_chars: int = Field(default=100_000, ge=1, le=1_000_000)
    max_resource_files: int = Field(default=256, ge=0, le=4_096)
    max_resource_bytes_each: int = Field(default=2_000_000, ge=1, le=20_000_000)
    max_resource_bytes_total: int = Field(default=20_000_000, ge=1, le=200_000_000)
    max_resource_depth: int = Field(default=8, ge=1, le=32)


class SkillResource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    relative_path: str
    kind: str = Field(pattern=r"^(script|reference)$")
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class DiscoveredSkill(BaseModel):
    """An untrusted discovery candidate, not a registration or permission grant."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    root_index: int = Field(ge=0)
    relative_directory: str
    name: str
    description: str
    body: str
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    frontmatter: dict[str, str | list[str]]
    resources: tuple[SkillResource, ...] = ()
    trust: str = Field(default="untrusted_candidate", pattern="^untrusted_candidate$")


class DiscoveryIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    root_index: int = Field(ge=0)
    relative_directory: str
    code: str
    message: str


class SkillDiscoveryResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidates: tuple[DiscoveredSkill, ...]
    issues: tuple[DiscoveryIssue, ...]


@dataclass(frozen=True)
class _SafeEntry:
    path: Path
    relative: str
    stat_result: os.stat_result


class SkillDiscovery:
    """Scan only explicit roots for bounded, non-symlink Skill candidates."""

    def __init__(
        self,
        allowlist_roots: tuple[Path, ...] | list[Path],
        *,
        limits: SkillDiscoveryLimits | None = None,
    ) -> None:
        self.limits = limits or SkillDiscoveryLimits()
        if not allowlist_roots or len(allowlist_roots) > self.limits.max_roots:
            raise ValueError("skill discovery requires a bounded, non-empty root allowlist")
        roots: list[Path] = []
        identities: set[tuple[int, int]] = set()
        for supplied in allowlist_roots:
            root = Path(supplied)
            if not root.is_absolute():
                raise ValueError("skill allowlist roots must be absolute")
            root_stat = root.lstat()
            if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
                raise ValueError("skill allowlist root must be a real directory, not a symlink")
            identity = (root_stat.st_dev, root_stat.st_ino)
            if identity in identities:
                raise ValueError("duplicate skill allowlist root")
            identities.add(identity)
            roots.append(root.resolve(strict=True))
        self._roots = tuple(roots)

    def discover(self) -> SkillDiscoveryResult:
        candidates: list[DiscoveredSkill] = []
        issues: list[DiscoveryIssue] = []
        visited = 0
        for root_index, root in enumerate(self._roots):
            directories: list[Path] = []
            if (root / "SKILL.md").exists():
                directories.append(root)
            try:
                root_children = sorted(root.iterdir(), key=lambda item: item.name)
            except OSError:
                issues.append(self._issue(root_index, ".", "root_unreadable"))
                continue
            for child in root_children:
                try:
                    child_stat = child.lstat()
                except OSError:
                    issues.append(self._issue(root_index, child.name, "entry_unreadable"))
                    continue
                if stat.S_ISLNK(child_stat.st_mode):
                    issues.append(self._issue(root_index, child.name, "symlink_rejected"))
                elif stat.S_ISDIR(child_stat.st_mode) and (child / "SKILL.md").exists():
                    directories.append(child)
            for directory in directories:
                visited += 1
                relative = "." if directory == root else directory.name
                if visited > self.limits.max_skill_directories:
                    issues.append(
                        self._issue(root_index, relative, "skill_directory_limit_exceeded")
                    )
                    return SkillDiscoveryResult(candidates=tuple(candidates), issues=tuple(issues))
                try:
                    candidates.append(self._read_candidate(root_index, root, directory, relative))
                except (OSError, UnicodeError, ValueError) as exc:
                    issues.append(
                        DiscoveryIssue(
                            root_index=root_index,
                            relative_directory=relative,
                            code="candidate_rejected",
                            message=str(exc)[:300],
                        )
                    )
        return SkillDiscoveryResult(candidates=tuple(candidates), issues=tuple(issues))

    def _read_candidate(
        self, root_index: int, root: Path, directory: Path, relative: str
    ) -> DiscoveredSkill:
        self._assert_contained(root, directory)
        directory_stat = directory.lstat()
        if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(directory_stat.st_mode):
            raise ValueError("skill directory must be a real directory")
        manifest = self._safe_regular_file(root, directory / "SKILL.md", "SKILL.md")
        if manifest.stat_result.st_size > self.limits.max_manifest_bytes:
            raise ValueError("SKILL.md exceeds the configured byte limit")
        raw = self._read_unchanged(manifest, self.limits.max_manifest_bytes)
        text = raw.decode("utf-8", errors="strict")
        frontmatter, body = self._parse_manifest(text)
        name_value = frontmatter.get("name")
        description_value = frontmatter.get("description")
        if not isinstance(name_value, str) or not _NAME_RE.fullmatch(name_value):
            raise ValueError("frontmatter name is missing or invalid")
        if not isinstance(description_value, str) or not description_value.strip():
            raise ValueError("frontmatter description is missing or invalid")
        if len(body) > self.limits.max_body_chars:
            raise ValueError("SKILL.md body exceeds the configured character limit")
        resources = self._list_resources(root, directory)
        return DiscoveredSkill(
            root_index=root_index,
            relative_directory=relative,
            name=name_value,
            description=description_value,
            body=body,
            manifest_sha256=hashlib.sha256(raw).hexdigest(),
            frontmatter=frontmatter,
            resources=resources,
        )

    def _parse_manifest(self, text: str) -> tuple[dict[str, str | list[str]], str]:
        if not text.startswith("---\n"):
            raise ValueError("SKILL.md must start with bounded frontmatter")
        boundary = text.find("\n---\n", 4)
        if boundary < 0:
            raise ValueError("SKILL.md frontmatter is not terminated")
        encoded_frontmatter = text[4:boundary].encode("utf-8")
        if len(encoded_frontmatter) > self.limits.max_frontmatter_bytes:
            raise ValueError("SKILL.md frontmatter exceeds the configured byte limit")
        parsed: dict[str, str | list[str]] = {}
        for raw_line in text[4:boundary].splitlines():
            if not raw_line.strip() or raw_line.lstrip().startswith("#"):
                continue
            if "\t" in raw_line or raw_line[:1].isspace() or ":" not in raw_line:
                raise ValueError("nested, tabbed, or malformed frontmatter is rejected")
            key, raw_value = raw_line.split(":", 1)
            key = key.strip()
            value = raw_value.strip()
            if not _KEY_RE.fullmatch(key) or key not in _SAFE_FRONTMATTER_KEYS:
                raise ValueError("frontmatter contains an unsupported key")
            if key in parsed:
                raise ValueError("frontmatter contains a duplicate key")
            if _DANGEROUS_YAML_RE.search(value) or "${" in value or "<(" in value:
                raise ValueError("frontmatter contains an unsafe YAML construct")
            parsed[key] = self._parse_scalar_or_list(value)
        return parsed, text[boundary + 5 :]

    @staticmethod
    def _parse_scalar_or_list(value: str) -> str | list[str]:
        if not value:
            return ""
        if value.startswith("["):
            try:
                parsed: Any = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError("frontmatter lists must use a JSON string array") from exc
            if (
                not isinstance(parsed, list)
                or len(parsed) > 64
                or any(not isinstance(item, str) or len(item) > 1_000 for item in parsed)
            ):
                raise ValueError("frontmatter list is invalid or too large")
            return parsed
        if value.startswith('"'):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError("invalid quoted frontmatter scalar") from exc
            if not isinstance(parsed, str):
                raise ValueError("frontmatter scalar must be a string")
            value = parsed
        elif value.startswith("'"):
            if not value.endswith("'") or len(value) < 2:
                raise ValueError("invalid quoted frontmatter scalar")
            value = value[1:-1].replace("''", "'")
        if any(character in value for character in ("\x00", "\r", "\n")) or len(value) > 4_000:
            raise ValueError("frontmatter scalar is invalid or too large")
        return value

    def _list_resources(self, root: Path, directory: Path) -> tuple[SkillResource, ...]:
        resources: list[SkillResource] = []
        total_bytes = 0
        for folder_name, kind in (("scripts", "script"), ("references", "reference")):
            folder = directory / folder_name
            if not folder.exists():
                continue
            folder_stat = folder.lstat()
            if stat.S_ISLNK(folder_stat.st_mode) or not stat.S_ISDIR(folder_stat.st_mode):
                raise ValueError(f"{folder_name} must be a real directory")
            for current, directory_names, file_names in os.walk(folder, followlinks=False):
                current_path = Path(current)
                depth = len(current_path.relative_to(folder).parts)
                if depth >= self.limits.max_resource_depth and directory_names:
                    raise ValueError("skill resource nesting exceeds the configured depth")
                for child_name in tuple(directory_names):
                    child = current_path / child_name
                    if stat.S_ISLNK(child.lstat().st_mode):
                        raise ValueError("symlinks in skill resources are rejected")
                directory_names[:] = sorted(directory_names)
                for file_name in sorted(file_names):
                    path = current_path / file_name
                    relative_path = path.relative_to(directory).as_posix()
                    entry = self._safe_regular_file(root, path, relative_path)
                    if entry.stat_result.st_size > self.limits.max_resource_bytes_each:
                        raise ValueError("a skill resource exceeds the per-file byte limit")
                    total_bytes += entry.stat_result.st_size
                    if total_bytes > self.limits.max_resource_bytes_total:
                        raise ValueError("skill resources exceed the total byte limit")
                    if len(resources) >= self.limits.max_resource_files:
                        raise ValueError("skill resources exceed the file-count limit")
                    content = self._read_unchanged(entry, self.limits.max_resource_bytes_each)
                    resources.append(
                        SkillResource(
                            relative_path=relative_path,
                            kind=kind,
                            size_bytes=len(content),
                            sha256=hashlib.sha256(content).hexdigest(),
                        )
                    )
        return tuple(resources)

    def _safe_regular_file(self, root: Path, path: Path, relative: str) -> _SafeEntry:
        self._assert_contained(root, path)
        path_stat = path.lstat()
        if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
            raise ValueError(f"{relative} must be a regular non-symlink file")
        return _SafeEntry(path=path, relative=relative, stat_result=path_stat)

    @staticmethod
    def _read_unchanged(entry: _SafeEntry, max_bytes: int) -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(entry.path, flags)
        try:
            before = os.fstat(descriptor)
            if (before.st_dev, before.st_ino) != (
                entry.stat_result.st_dev,
                entry.stat_result.st_ino,
            ):
                raise ValueError(f"{entry.relative} changed during discovery")
            data = os.read(descriptor, max_bytes + 1)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if len(data) > max_bytes:
            raise ValueError(f"{entry.relative} exceeds the configured byte limit")
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError(f"{entry.relative} changed during discovery")
        return data

    @staticmethod
    def _assert_contained(root: Path, path: Path) -> None:
        try:
            path.resolve(strict=True).relative_to(root)
        except (OSError, ValueError) as exc:
            raise ValueError("skill path escapes its allowlisted root") from exc

    @staticmethod
    def _issue(root_index: int, relative: str, code: str) -> DiscoveryIssue:
        return DiscoveryIssue(
            root_index=root_index,
            relative_directory=relative,
            code=code,
            message=code.replace("_", " "),
        )
