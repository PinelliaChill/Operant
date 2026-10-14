"""Conservative inventory and lifecycle policy for temporary Workbench resources."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ResourcePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    completed_ttl_seconds: int = Field(default=3600, ge=3600, le=31_536_000)
    unanswered_ttl_seconds: int = Field(default=259_200, ge=3600, le=31_536_000)
    completed_at: datetime | None = None
    unanswered_since: datetime | None = None


class ResourceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: Literal[
        "context_reference_snapshot",
        "artifact",
        "context_revision",
        "compaction",
        "thread_history",
        "browser_profile",
        "memory",
        "approval_audit",
        "run_state",
        "tool_snapshot",
        "terminal",
    ]
    owner_thread_id: str | None = None
    size_bytes: int = Field(
        ge=0, description="Stored payload bytes, not the SQLite file's physical allocation."
    )
    retention_reason: str
    pinned: bool = False
    hold_reason: str | None = None
    state: str
    auto_cleanup_eligible: bool = False
    due_at: datetime | None = None


class ResourceInventory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: str
    policy: ResourcePolicy
    resources: list[ResourceItem]
    truncated: bool = False
    next_cursor: dict[str, int] = Field(default_factory=dict)


class ResourceCleanupItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_id: str
    eligible: bool
    reason: str
    size_bytes: int = Field(ge=0)


class ResourceCleanupPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ResourceCleanupItem]
    total_bytes: int = Field(ge=0)
    truncated: bool = False
    next_cursor: int = Field(default=0, ge=0)


class ResourceCleanupResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    removed_ids: list[str]
    skipped: list[ResourceCleanupItem]
