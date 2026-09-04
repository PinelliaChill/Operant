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
class _RegisteredRoot:
    path: Path
    stat_result: os.stat_result


_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | int(getattr(os, "O_DIRECTORY", 0))
    | int(getattr(os, "O_NOFOLLOW", 0))
    | int(getattr(os, "O_CLOEXEC", 0))
)
_FILE_FLAGS = (
    os.O_RDONLY
    | int(getattr(os, "O_NOFOLLOW", 0))
    | int(getattr(os, "O_NONBLOCK", 0))
    | int(getattr(os, "O_CLOEXEC", 0))
)


class SkillDiscovery:
    """Scan only explicit roots for bounded, non-symlink Skill candidates."""

    def __init__(
        self,
        allowlist_roots: tuple[Path, ...] | list[Path],
        *,
        limits: SkillDiscoveryLimits | None = None,
    ) -> None:
        self.limits = limits or SkillDiscoveryLimits()
        self._require_safe_primitives()
        if not allowlist_roots or len(allowlist_roots) > self.limits.max_roots:
            raise ValueError("skill discovery requires a bounded, non-empty root allowlist")
        roots: list[_RegisteredRoot] = []
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
            canonical = root.resolve(strict=True)
            descriptor = self._open_absolute_directory(canonical)
            try:
                opened = os.fstat(descriptor)
                if not self._same_identity(root_stat, opened):
                    raise ValueError("skill allowlist root changed while it was registered")
            finally:
                os.close(descriptor)
            roots.append(_RegisteredRoot(path=canonical, stat_result=root_stat))
        self._roots = tuple(roots)

    def discover(self) -> SkillDiscoveryResult:
        candidates: list[DiscoveredSkill] = []
        issues: list[DiscoveryIssue] = []
        visited = 0
        for root_index, root in enumerate(self._roots):
            root_candidates: list[DiscoveredSkill] = []
            root_issues: list[DiscoveryIssue] = []
            try:
                root_fd = self._open_registered_root(root)
            except (OSError, ValueError):
                issues.append(self._issue(root_index, ".", "root_unreadable"))
                continue
            root_opened = os.fstat(root_fd)
            try:
                directories: list[tuple[str | None, os.stat_result, str]] = []
                if self._entry_exists(root_fd, "SKILL.md"):
                    directories.append((None, root_opened, "."))
                try:
                    root_children = self._directory_names(root_fd)
                except OSError:
                    raise ValueError("skill root changed while it was enumerated") from None
                for child_name in root_children:
                    try:
                        child_stat = os.stat(child_name, dir_fd=root_fd, follow_symlinks=False)
                    except OSError:
                        root_issues.append(self._issue(root_index, child_name, "entry_unreadable"))
                        continue
                    if stat.S_ISLNK(child_stat.st_mode):
                        root_issues.append(self._issue(root_index, child_name, "symlink_rejected"))
                    elif stat.S_ISDIR(child_stat.st_mode) and self._directory_has_manifest(
                        root_fd, child_name, child_stat
                    ):
                        directories.append((child_name, child_stat, child_name))
                for candidate_name, expected, relative in directories:
                    visited += 1
                    if visited > self.limits.max_skill_directories:
                        issues.extend(root_issues)
                        issues.append(
                            self._issue(root_index, relative, "skill_directory_limit_exceeded")
                        )
                        return SkillDiscoveryResult(
                            candidates=tuple(candidates), issues=tuple(issues)
                        )
                    try:
                        root_candidates.append(
                            self._read_candidate(
                                root_index, root_fd, candidate_name, expected, relative
                            )
                        )
                    except (OSError, UnicodeError, ValueError) as exc:
                        root_issues.append(
                            DiscoveryIssue(
                                root_index=root_index,
                                relative_directory=relative,
                                code="candidate_rejected",
                                message=str(exc)[:300],
                            )
                        )
                if not self._same_version(root_opened, os.fstat(root_fd)):
                    raise ValueError("skill root changed during discovery")
                rebound_fd = self._open_registered_root(root)
                try:
                    if not self._same_version(root_opened, os.fstat(rebound_fd)):
                        raise ValueError("skill root was replaced during discovery")
                finally:
                    os.close(rebound_fd)
            except (OSError, ValueError):
                issues.append(self._issue(root_index, ".", "root_unreadable"))
            else:
                candidates.extend(root_candidates)
                issues.extend(root_issues)
            finally:
                os.close(root_fd)
        return SkillDiscoveryResult(candidates=tuple(candidates), issues=tuple(issues))

    def _read_candidate(
        self,
        root_index: int,
        root_fd: int,
        child_name: str | None,
        expected: os.stat_result,
        relative: str,
    ) -> DiscoveredSkill:
        directory_fd = self._open_bound_directory(root_fd, child_name, expected, relative)
        opened = os.fstat(directory_fd)
        try:
            raw = self._read_regular_file(
                directory_fd, "SKILL.md", "SKILL.md", self.limits.max_manifest_bytes
            )
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
            resources = self._list_resources(directory_fd)
            self._verify_directory_binding(root_fd, child_name, opened, directory_fd, relative)
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
        finally:
            os.close(directory_fd)

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

    def _list_resources(self, directory_fd: int) -> tuple[SkillResource, ...]:
        resources: list[SkillResource] = []
        budget = {"bytes": 0}
        for folder_name, kind in (("scripts", "script"), ("references", "reference")):
            try:
                folder_stat = os.stat(folder_name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(folder_stat.st_mode) or not stat.S_ISDIR(folder_stat.st_mode):
                raise ValueError(f"{folder_name} must be a real directory")
            folder_fd = self._open_bound_directory(
                directory_fd, folder_name, folder_stat, folder_name
            )
            opened = os.fstat(folder_fd)
            try:
                self._walk_resources(
                    folder_fd,
                    prefix=folder_name,
                    kind=kind,
                    depth=0,
                    resources=resources,
                    budget=budget,
                )
                self._verify_directory_binding(
                    directory_fd, folder_name, opened, folder_fd, folder_name
                )
            finally:
                os.close(folder_fd)
        return tuple(resources)

    def _walk_resources(
        self,
        directory_fd: int,
        *,
        prefix: str,
        kind: str,
        depth: int,
        resources: list[SkillResource],
        budget: dict[str, int],
    ) -> None:
        names = self._directory_names(directory_fd)
        directories: list[tuple[str, os.stat_result]] = []
        files: list[tuple[str, os.stat_result]] = []
        for name in names:
            entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISLNK(entry.st_mode):
                raise ValueError("symlinks in skill resources are rejected")
            if stat.S_ISDIR(entry.st_mode):
                directories.append((name, entry))
            else:
                files.append((name, entry))
        if depth >= self.limits.max_resource_depth and directories:
            raise ValueError("skill resource nesting exceeds the configured depth")
        for name, expected in files:
            relative = f"{prefix}/{name}"
            if not stat.S_ISREG(expected.st_mode):
                raise ValueError(f"{relative} must be a regular non-symlink file")
            if expected.st_size > self.limits.max_resource_bytes_each:
                raise ValueError("a skill resource exceeds the per-file byte limit")
            budget["bytes"] += expected.st_size
            if budget["bytes"] > self.limits.max_resource_bytes_total:
                raise ValueError("skill resources exceed the total byte limit")
            if len(resources) >= self.limits.max_resource_files:
                raise ValueError("skill resources exceed the file-count limit")
            content = self._read_regular_file(
                directory_fd,
                name,
                relative,
                self.limits.max_resource_bytes_each,
                expected=expected,
            )
            resources.append(
                SkillResource(
                    relative_path=relative,
                    kind=kind,
                    size_bytes=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                )
            )
        for name, expected in directories:
            relative = f"{prefix}/{name}"
            child_fd = self._open_bound_directory(directory_fd, name, expected, relative)
            opened = os.fstat(child_fd)
            try:
                self._walk_resources(
                    child_fd,
                    prefix=relative,
                    kind=kind,
                    depth=depth + 1,
                    resources=resources,
                    budget=budget,
                )
                self._verify_directory_binding(directory_fd, name, opened, child_fd, relative)
            finally:
                os.close(child_fd)

    def _read_regular_file(
        self,
        parent_fd: int,
        name: str,
        relative: str,
        max_bytes: int,
        *,
        expected: os.stat_result | None = None,
    ) -> bytes:
        before = expected or os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise ValueError(f"{relative} must be a regular non-symlink file")
        if before.st_size > max_bytes:
            raise ValueError(f"{relative} exceeds the configured byte limit")
        descriptor = os.open(name, _FILE_FLAGS, dir_fd=parent_fd)
        try:
            opened = os.fstat(descriptor)
            if not self._same_identity(before, opened) or not stat.S_ISREG(opened.st_mode):
                raise ValueError(f"{relative} changed during discovery")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, min(64 * 1024, max_bytes + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"{relative} exceeds the configured byte limit")
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        rebound = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not self._same_version(opened, after) or not self._same_version(opened, rebound):
            raise ValueError(f"{relative} changed during discovery")
        return b"".join(chunks)

    def _directory_has_manifest(self, parent_fd: int, name: str, expected: os.stat_result) -> bool:
        child_fd = self._open_bound_directory(parent_fd, name, expected, name)
        try:
            return self._entry_exists(child_fd, "SKILL.md")
        finally:
            os.close(child_fd)

    @staticmethod
    def _entry_exists(directory_fd: int, name: str) -> bool:
        try:
            os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True

    @staticmethod
    def _directory_names(directory_fd: int) -> list[str]:
        with os.scandir(directory_fd) as entries:
            return sorted(entry.name for entry in entries)

    def _open_bound_directory(
        self,
        parent_fd: int,
        name: str | None,
        expected: os.stat_result,
        relative: str,
    ) -> int:
        descriptor = (
            os.dup(parent_fd) if name is None else os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        )
        try:
            opened = os.fstat(descriptor)
            if not self._same_identity(expected, opened) or not stat.S_ISDIR(opened.st_mode):
                raise ValueError(f"{relative} changed during discovery")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _verify_directory_binding(
        self,
        parent_fd: int,
        name: str | None,
        opened: os.stat_result,
        descriptor: int,
        relative: str,
    ) -> None:
        after = os.fstat(descriptor)
        rebound = (
            os.fstat(parent_fd)
            if name is None
            else os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        )
        if not self._same_version(opened, after) or not self._same_version(opened, rebound):
            raise ValueError(f"{relative} changed during discovery")

    def _open_registered_root(self, root: _RegisteredRoot) -> int:
        descriptor = self._open_absolute_directory(root.path)
        opened = os.fstat(descriptor)
        if not self._same_identity(root.stat_result, opened):
            os.close(descriptor)
            raise ValueError("skill allowlist root was replaced")
        return descriptor

    @staticmethod
    def _open_absolute_directory(path: Path) -> int:
        parts = path.parts
        if not path.is_absolute() or not parts or not parts[0].startswith(os.sep):
            raise ValueError("skill allowlist roots must be absolute")
        descriptor = os.open(os.sep, _DIRECTORY_FLAGS)
        try:
            for component in parts[1:]:
                if component in {"", ".", ".."}:
                    raise ValueError("skill allowlist root is invalid")
                child_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child_fd
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
        return (left.st_dev, left.st_ino, stat.S_IFMT(left.st_mode)) == (
            right.st_dev,
            right.st_ino,
            stat.S_IFMT(right.st_mode),
        )

    @classmethod
    def _same_version(cls, left: os.stat_result, right: os.stat_result) -> bool:
        return cls._same_identity(left, right) and (
            left.st_size,
            left.st_mtime_ns,
            left.st_ctime_ns,
        ) == (right.st_size, right.st_mtime_ns, right.st_ctime_ns)

    @staticmethod
    def _require_safe_primitives() -> None:
        if (
            not hasattr(os, "O_NOFOLLOW")
            or not hasattr(os, "O_DIRECTORY")
            or os.open not in getattr(os, "supports_dir_fd", ())
            or os.stat not in getattr(os, "supports_dir_fd", ())
            or os.stat not in getattr(os, "supports_follow_symlinks", ())
            or os.scandir not in getattr(os, "supports_fd", ())
        ):
            raise RuntimeError("safe skill discovery is unavailable on this platform")

    @staticmethod
    def _issue(root_index: int, relative: str, code: str) -> DiscoveryIssue:
        return DiscoveryIssue(
            root_index=root_index,
            relative_directory=relative,
            code=code,
            message=code.replace("_", " "),
        )
