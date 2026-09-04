from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_writer_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _aware_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


class WriterIsolationKind(str, Enum):
    WORKTREE = "worktree"
    CONTAINER = "container"


class WriterArtifactKind(str, Enum):
    PATCH = "patch"
    COMMIT = "commit"


class WriterConflictStatus(str, Enum):
    OPEN = "open"
    RESOLVED = "resolved"
    REJECTED = "rejected"


class MergeStrategy(str, Enum):
    THREE_WAY = "three_way"
    CHERRY_PICK = "cherry_pick"
    APPLY_PATCH = "apply_patch"


class MergeRunStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    CONFLICTED = "conflicted"
    REVIEW_REQUIRED = "review_required"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class WriterNodePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    writer_key: str = Field(min_length=1, max_length=200)
    isolation_kind: WriterIsolationKind
    isolation_ref: str = Field(min_length=1, max_length=500)
    ownership_paths: tuple[str, ...] = Field(min_length=1, max_length=256)

    @field_validator("ownership_paths")
    @classmethod
    def validate_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("ownership_paths must be unique")
        if any(path.startswith("/") or ".." in path.split("/") for path in value):
            raise ValueError("ownership_paths must be normalized repository-relative paths")
        return value


class MergeNodePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy: MergeStrategy = MergeStrategy.THREE_WAY
    source_writer_keys: tuple[str, ...] = Field(min_length=2, max_length=64)
    require_review: bool = True
    rollback_on_failure: bool = True


class WriterWorkspace(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    writer_workspace_id: str = Field(default_factory=lambda: new_writer_id("writer_workspace"))
    graph_run_id: str = Field(min_length=1, max_length=300)
    node_run_id: str = Field(min_length=1, max_length=300)
    writer_key: str = Field(min_length=1, max_length=200)
    isolation_kind: WriterIsolationKind
    isolation_ref: str = Field(min_length=1, max_length=500)
    base_revision: str = Field(min_length=7, max_length=128)
    ownership_paths: tuple[str, ...] = Field(min_length=1, max_length=256)
    created_at: datetime = Field(default_factory=utc_now)
    closed_at: datetime | None = None

    @field_validator("created_at", "closed_at")
    @classmethod
    def validate_timestamps(cls, value: datetime | None, info: Any) -> datetime | None:
        if value is None:
            return None
        return _aware_utc(value, field_name=info.field_name)


class WriterLease(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    writer_workspace_id: str = Field(min_length=1, max_length=300)
    owner: str = Field(min_length=1, max_length=300)
    token: str = Field(min_length=16, max_length=300)
    fencing: int = Field(ge=1)
    expires_at: datetime
    released_at: datetime | None = None


class PatchCommitArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    writer_artifact_id: str = Field(default_factory=lambda: new_writer_id("writer_artifact"))
    writer_workspace_id: str = Field(min_length=1, max_length=300)
    artifact_kind: WriterArtifactKind
    artifact_ref: str = Field(min_length=1, max_length=500)
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_revision: str = Field(min_length=7, max_length=128)
    result_revision: str | None = Field(default=None, min_length=7, max_length=128)
    changed_paths: tuple[str, ...] = Field(max_length=5_000)
    test_evidence_refs: tuple[str, ...] = Field(default=(), max_length=200)
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_commit_revision(self) -> PatchCommitArtifact:
        if self.artifact_kind is WriterArtifactKind.COMMIT and self.result_revision is None:
            raise ValueError("commit artifacts require result_revision")
        return self


class WriterConflict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    conflict_id: str = Field(default_factory=lambda: new_writer_id("writer_conflict"))
    graph_run_id: str = Field(min_length=1, max_length=300)
    left_artifact_id: str = Field(min_length=1, max_length=300)
    right_artifact_id: str = Field(min_length=1, max_length=300)
    conflict_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    paths: tuple[str, ...] = Field(min_length=1, max_length=5_000)
    status: WriterConflictStatus = WriterConflictStatus.OPEN
    resolution_artifact_ref: str | None = Field(default=None, max_length=500)
    created_at: datetime = Field(default_factory=utc_now)
    resolved_at: datetime | None = None


class MergeRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    merge_run_id: str = Field(default_factory=lambda: new_writer_id("merge_run"))
    graph_run_id: str = Field(min_length=1, max_length=300)
    merge_node_id: str = Field(min_length=1, max_length=300)
    artifact_ids: tuple[str, ...] = Field(min_length=2, max_length=64)
    strategy: MergeStrategy = MergeStrategy.THREE_WAY
    target_isolation_ref: str = Field(min_length=1, max_length=500)
    base_revision: str = Field(min_length=7, max_length=128)
    status: MergeRunStatus = MergeRunStatus.CREATED
    expected_revision: int = Field(default=0, ge=0)
    result_artifact_ref: str | None = Field(default=None, max_length=500)
    error_code: str | None = Field(default=None, max_length=200)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
