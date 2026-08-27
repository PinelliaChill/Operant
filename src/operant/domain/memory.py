from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_memory_id() -> str:
    return f"memory_{uuid4().hex}"


class MemoryKind(str, Enum):
    """The retention boundary of a piece of agent knowledge."""

    WORKING = "working"
    EPISODIC = "episodic"
    PROJECT = "project"


class MemoryStatus(str, Enum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    INACTIVE = "inactive"


class MemorySource(BaseModel):
    """The traceable origin of a Memory version."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_session_id: str | None = None
    source_task: str | None = None


class Memory(BaseModel):
    """One immutable, versioned piece of working, episodic, or project memory.

    ``kind`` accepts ``memory_type`` as an input alias so callers can use the
    terminology used by their storage or API layer without creating a second
    domain concept.  A versioned update creates a new instance instead of
    mutating an existing one.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )

    id: str = Field(default_factory=new_memory_id, min_length=1, max_length=200)
    kind: MemoryKind = Field(
        validation_alias=AliasChoices("kind", "memory_type", "type"),
    )
    content: str = Field(min_length=1, max_length=100_000)
    project_scope: str | None = Field(default=None, max_length=500)
    role_scope: tuple[str, ...] = ()
    source_session_id: str | None = Field(default=None, max_length=200)
    source_task: str | None = Field(default=None, max_length=100_000)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    version: int = Field(default=1, ge=1)
    status: MemoryStatus = MemoryStatus.CANDIDATE
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("memory content must not be blank")
        return value

    @field_validator("project_scope", "source_session_id", "source_task")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("role_scope", mode="before")
    @classmethod
    def normalize_role_scope(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        values = value.split(",") if isinstance(value, str) else value
        if not isinstance(values, (list, tuple, set, frozenset)):
            raise ValueError("role_scope must be a string or a sequence of role ids")
        normalized = tuple(
            item.strip() for item in values if isinstance(item, str) and item.strip()
        )
        if len(normalized) != len(set(normalized)):
            raise ValueError("role_scope must not contain duplicates")
        return normalized

    @model_validator(mode="after")
    def validate_source_shape(self) -> Memory:
        if self.kind is MemoryKind.WORKING and self.source_session_id is None:
            raise ValueError("working memory requires source_session_id")
        if self.kind is MemoryKind.PROJECT and self.project_scope is None:
            raise ValueError("project memory requires project_scope")
        return self

    @property
    def memory_type(self) -> MemoryKind:
        return self.kind

    @property
    def source(self) -> MemorySource:
        return MemorySource(
            source_session_id=self.source_session_id,
            source_task=self.source_task,
        )


@dataclass(frozen=True)
class ParsedMemoryScope:
    """Normalized read/write permissions derived from RoleSnapshot.memory_scope."""

    read: frozenset[MemoryKind]
    write: frozenset[MemoryKind]
    session_bound: bool = False

    def can_read(self, kind: MemoryKind) -> bool:
        return kind in self.read

    def can_write(self, kind: MemoryKind) -> bool:
        return kind in self.write


_ALL_KINDS = frozenset(MemoryKind)
_SCOPE_ALIASES = {
    "session": MemoryKind.WORKING,
    "working": MemoryKind.WORKING,
    "episodic": MemoryKind.EPISODIC,
    "episode": MemoryKind.EPISODIC,
    "project": MemoryKind.PROJECT,
    "project_knowledge": MemoryKind.PROJECT,
}


def _parse_kind_tokens(value: str) -> frozenset[MemoryKind]:
    lowered = value.lower().replace("'", "").replace('"', "")
    tokens = [token for token in re.split(r"[\s,|]+", lowered) if token]
    result: set[MemoryKind] = set()
    for token in tokens:
        if token in {"none", "null", "disabled", "off", "[]"}:
            continue
        if token in {"all", "*"}:
            result.update(_ALL_KINDS)
            continue
        kind = _SCOPE_ALIASES.get(token)
        if kind is not None:
            result.add(kind)
            continue
        # Unknown scope labels are ignored here.  Role validation remains
        # backwards-compatible while the service will never grant a new kind
        # accidentally just because a role contains an unknown word.
    return frozenset(result)


def _extract_explicit_scope(scope: str, label: str) -> frozenset[MemoryKind] | None:
    # Supports both ``read: [project, episodic]`` and
    # ``read=project,episodic;write=project`` forms.
    pattern = rf"\b{label}\s*[:=]\s*(?:\[([^\]]*)\]|([^;\n]+))"
    match = re.search(pattern, scope, flags=re.IGNORECASE)
    if match is None:
        return None
    value = match.group(1) if match.group(1) is not None else match.group(2)
    assert value is not None
    return _parse_kind_tokens(value)


def parse_memory_scope(scope: str) -> ParsedMemoryScope:
    """Parse the compact role scope syntax used by the current RoleSnapshot.

    Existing roles use ``session``; it means read/write access to working
    memory bound to the current session.  A plain comma-separated scope grants
    both read and write access.  Explicit ``read: [...]``/``write: [...]``
    syntax lets read-only roles opt into project or episodic knowledge.
    """

    normalized = scope.strip()
    if not normalized:
        return ParsedMemoryScope(read=frozenset(), write=frozenset())

    explicit_read = _extract_explicit_scope(normalized, "read")
    explicit_write = _extract_explicit_scope(normalized, "write")
    if explicit_read is not None or explicit_write is not None:
        read = explicit_read or frozenset()
        write = explicit_write or frozenset()
        return ParsedMemoryScope(
            read=read, write=write, session_bound="session" in normalized.lower()
        )

    kinds = _parse_kind_tokens(normalized.strip("[](){}"))
    return ParsedMemoryScope(
        read=kinds,
        write=kinds,
        session_bound="session" in normalized.lower() and MemoryKind.WORKING in kinds,
    )


_VERIFICATION_MARKERS = (
    "pytest",
    "unittest",
    "ruff",
    "mypy",
    "git diff",
    "uv run",
    "npm test",
    "cargo test",
    "go test",
    "make test",
    "验证",
    "测试",
)


def passes_conservative_activation(memory: Memory) -> bool:
    """Return whether a candidate has enough provenance to auto-activate.

    Conservative activation is deliberately narrow: only a short, highly
    confident episodic/project item with both task and session provenance and
    an explicit verification signal can bypass manual confirmation.  Working
    memory is session-local and is therefore safe to activate immediately.
    """

    if memory.kind is MemoryKind.WORKING:
        return memory.source_session_id is not None
    if memory.kind not in {MemoryKind.EPISODIC, MemoryKind.PROJECT}:
        return False
    if memory.confidence < 0.9:
        return False
    if not memory.source_session_id or not memory.source_task:
        return False
    if len(memory.content) > 4_000:
        return False
    haystack = f"{memory.content}\n{memory.source_task}".lower()
    return any(marker in haystack for marker in _VERIFICATION_MARKERS)


def default_memory_status(kind: MemoryKind) -> MemoryStatus:
    """Working memory is immediately usable; durable knowledge starts pending."""

    return MemoryStatus.ACTIVE if kind is MemoryKind.WORKING else MemoryStatus.CANDIDATE
