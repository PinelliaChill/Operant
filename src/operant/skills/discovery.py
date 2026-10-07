from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

_SAFE_FRONTMATTER_KEYS = frozenset(
    {
        "name",
        "description",
        "license",
        "compatibility",
        "metadata",
        "allowed-tools",
        "disable-model-invocation",
    }
)
_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FRONTMATTER_MAX_DEPTH = 3
_FRONTMATTER_MAX_ENTRIES = 128


class _SkillFrontmatterLoader(yaml.SafeLoader):
    """Safe YAML with duplicate keys and aliases rejected."""

    def compose_node(self, parent: Any, index: Any) -> yaml.Node:
        event = self.peek_event()  # type: ignore[no-untyped-call]
        if isinstance(event, yaml.AliasEvent):
            raise ValueError("frontmatter YAML aliases are rejected")
        if (
            isinstance(event, (yaml.ScalarEvent, yaml.SequenceStartEvent, yaml.MappingStartEvent))
            and event.anchor is not None
        ):
            raise ValueError("frontmatter YAML anchors are rejected")
        node = super().compose_node(parent, index)
        assert node is not None
        return node

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        result: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise ValueError("frontmatter keys must be unique strings")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


class SkillDiscoveryLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_roots: int = Field(default=16, ge=1, le=64)
    max_root_entries: int = Field(default=4_096, ge=1, le=100_000)
    max_skill_directories: int = Field(default=256, ge=1, le=2_048)
    max_manifest_bytes: int = Field(default=128_000, ge=256, le=1_000_000)
    max_frontmatter_bytes: int = Field(default=16_000, ge=64, le=128_000)
    max_body_chars: int = Field(default=100_000, ge=1, le=1_000_000)
    max_resource_files: int = Field(default=256, ge=0, le=4_096)
    max_resource_entries: int = Field(default=4_096, ge=1, le=100_000)
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
    frontmatter: dict[str, str | list[str] | dict[str, str]]
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
    """Scan explicit roots and links to directories inside those roots."""

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
        seen_candidate_paths: set[Path] = set()
        root_entry_budget = {"entries": 0}
        for root_index, root in enumerate(self._roots):
            root_candidates: list[DiscoveredSkill] = []
            root_issues: list[DiscoveryIssue] = []
            root_seen_paths: set[Path] = set()
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
                    root_children = self._directory_names(
                        root_fd,
                        budget=root_entry_budget,
                        limit=self.limits.max_root_entries,
                        limit_message="skill roots exceed the entry limit",
                    )
                except OSError:
                    raise ValueError("skill root changed while it was enumerated") from None
                for child_name in root_children:
                    try:
                        child_stat = os.stat(child_name, dir_fd=root_fd, follow_symlinks=False)
                    except OSError:
                        root_issues.append(self._issue(root_index, child_name, "entry_unreadable"))
                        continue
                    if stat.S_ISLNK(child_stat.st_mode):
                        try:
                            linked = self._read_linked_candidate(root_index, root, child_name)
                        except (OSError, UnicodeError, ValueError) as exc:
                            root_issues.append(
                                DiscoveryIssue(
                                    root_index=root_index,
                                    relative_directory=child_name,
                                    code="symlink_rejected",
                                    message=str(exc)[:300],
                                )
                            )
                        else:
                            if linked is not None:
                                actual = (root.path / child_name).resolve(strict=True)
                                if actual in seen_candidate_paths or actual in root_seen_paths:
                                    continue
                                visited += 1
                                if visited > self.limits.max_skill_directories:
                                    root_issues.append(
                                        self._issue(
                                            root_index,
                                            child_name,
                                            "skill_directory_limit_exceeded",
                                        )
                                    )
                                    issues.extend(root_issues)
                                    return SkillDiscoveryResult(
                                        candidates=tuple(candidates), issues=tuple(issues)
                                    )
                                root_candidates.append(linked)
                                root_seen_paths.add(actual)
                    elif stat.S_ISDIR(child_stat.st_mode) and self._directory_has_manifest(
                        root_fd, child_name, child_stat
                    ):
                        directories.append((child_name, child_stat, child_name))
                for candidate_name, expected, relative in directories:
                    actual = root.path if candidate_name is None else root.path / candidate_name
                    if actual in seen_candidate_paths or actual in root_seen_paths:
                        continue
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
                        root_seen_paths.add(actual)
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
                seen_candidate_paths.update(root_seen_paths)
            finally:
                os.close(root_fd)
        return SkillDiscoveryResult(candidates=tuple(candidates), issues=tuple(issues))

    def _read_linked_candidate(
        self, root_index: int, root: _RegisteredRoot, name: str
    ) -> DiscoveredSkill | None:
        link = root.path / name
        before = link.lstat()
        target = link.resolve(strict=True)
        if not target.is_dir():
            raise ValueError("Skill link target is not a directory")
        allowed_root = next(
            (
                allowed
                for allowed in self._roots
                if target == allowed.path or target.is_relative_to(allowed.path)
            ),
            None,
        )
        if allowed_root is None:
            raise ValueError(
                "Skill link target is outside registered roots; "
                "add its real parent directory as a Skill source"
            )
        allowed_fd = self._open_registered_root(allowed_root)
        os.close(allowed_fd)
        descriptor = self._open_absolute_directory(target)
        try:
            opened = os.fstat(descriptor)
            if not self._entry_exists(descriptor, "SKILL.md"):
                return None
            candidate = self._read_candidate(root_index, descriptor, None, opened, name)
            after = link.lstat()
            if not self._same_version(before, after) or link.resolve(strict=True) != target:
                raise ValueError("Skill link changed during discovery")
            allowed_fd = self._open_registered_root(allowed_root)
            os.close(allowed_fd)
            return candidate
        finally:
            os.close(descriptor)

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

    def _parse_manifest(self, text: str) -> tuple[dict[str, str | list[str] | dict[str, str]], str]:
        text = text.removeprefix("\ufeff").replace("\r\n", "\n")
        if not text.startswith("---\n"):
            raise ValueError("SKILL.md must start with bounded frontmatter")
        boundary = text.find("\n---\n", 4)
        if boundary < 0:
            raise ValueError("SKILL.md frontmatter is not terminated")
        encoded_frontmatter = text[4:boundary].encode("utf-8")
        if len(encoded_frontmatter) > self.limits.max_frontmatter_bytes:
            raise ValueError("SKILL.md frontmatter exceeds the configured byte limit")
        frontmatter_text = text[4:boundary]
        if "\t" in frontmatter_text or "${" in frontmatter_text or "<(" in frontmatter_text:
            raise ValueError("frontmatter contains an unsafe construct")
        try:
            loader = _SkillFrontmatterLoader(frontmatter_text)
            try:
                node = loader.get_single_node()
                if node is None:
                    raise ValueError("SKILL.md frontmatter is empty")
                self._validate_yaml_node(node)
                parsed = loader.construct_document(node)
            finally:
                loader.dispose()
        except yaml.YAMLError as exc:
            raise ValueError("SKILL.md frontmatter is malformed or unsafe YAML") from exc
        if not isinstance(parsed, dict):
            raise ValueError("SKILL.md frontmatter must be a mapping")
        result: dict[str, str | list[str] | dict[str, str]] = {}
        for key, value in parsed.items():
            if not _KEY_RE.fullmatch(key) or key not in _SAFE_FRONTMATTER_KEYS:
                raise ValueError("frontmatter contains an unsupported key")
            if key == "metadata":
                if not isinstance(value, dict) or len(value) > 64:
                    raise ValueError("metadata frontmatter must be a bounded mapping")
                metadata: dict[str, str] = {}
                for metadata_key, metadata_value in value.items():
                    if isinstance(metadata_value, bool):
                        metadata_value = "true" if metadata_value else "false"
                    elif isinstance(metadata_value, int | float):
                        metadata_value = str(metadata_value)
                    if (
                        not metadata_key
                        or len(metadata_key) > 128
                        or any(ord(character) < 32 for character in metadata_key)
                        or not isinstance(metadata_value, str)
                        or len(metadata_value) > 1_000
                    ):
                        raise ValueError("metadata frontmatter contains an invalid entry")
                    metadata[metadata_key] = metadata_value
                result[key] = metadata
            elif key == "disable-model-invocation" and isinstance(value, bool):
                result[key] = "true" if value else "false"
            elif isinstance(value, str):
                if len(value) > 4_000 or "\x00" in value:
                    raise ValueError("frontmatter scalar is invalid or too large")
                if key == "disable-model-invocation" and value not in {"true", "false"}:
                    raise ValueError("disable-model-invocation must be true or false")
                result[key] = value
            elif key == "allowed-tools" and isinstance(value, list):
                if len(value) > 64 or any(
                    not isinstance(item, str) or len(item) > 1_000 for item in value
                ):
                    raise ValueError("frontmatter list is invalid or too large")
                result[key] = value
            else:
                raise ValueError("frontmatter value has an unsupported shape")
        return result, text[boundary + 5 :]

    @staticmethod
    def _validate_yaml_node(node: yaml.Node) -> None:
        pending: list[tuple[yaml.Node, int]] = [(node, 1)]
        count = 0
        while pending:
            current, depth = pending.pop()
            count += 1
            if count > _FRONTMATTER_MAX_ENTRIES or depth > _FRONTMATTER_MAX_DEPTH:
                raise ValueError("frontmatter structure exceeds the configured limit")
            if isinstance(current, yaml.MappingNode):
                pending.extend((child, depth + 1) for pair in current.value for child in pair)
            elif isinstance(current, yaml.SequenceNode):
                pending.extend((child, depth + 1) for child in current.value)
            elif not isinstance(current, yaml.ScalarNode):
                raise ValueError("frontmatter contains an unsupported YAML node")
            if current.tag not in {
                "tag:yaml.org,2002:str",
                "tag:yaml.org,2002:bool",
                "tag:yaml.org,2002:int",
                "tag:yaml.org,2002:float",
                "tag:yaml.org,2002:null",
                "tag:yaml.org,2002:map",
                "tag:yaml.org,2002:seq",
            }:
                raise ValueError("frontmatter contains an unsafe YAML tag")

    def _list_resources(self, directory_fd: int) -> tuple[SkillResource, ...]:
        resources: list[SkillResource] = []
        budget = {"bytes": 0, "entries": 0}
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
        names = self._directory_names(
            directory_fd,
            budget=budget,
            limit=self.limits.max_resource_entries,
            limit_message="skill resources exceed the entry limit",
        )
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
    def _directory_names(
        directory_fd: int,
        *,
        budget: dict[str, int],
        limit: int,
        limit_message: str,
    ) -> list[str]:
        names: list[str] = []
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                budget["entries"] += 1
                if budget["entries"] > limit:
                    raise ValueError(limit_message)
                names.append(entry.name)
        return sorted(names)

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
