"""Small, read-only projections consumed by the Phase 1E clients.

These models deliberately contain only facts that already have a durable owner.
They are not a second Project/Workspace state machine: a project projection is
an immutable view of one persisted workspace registration and its exact
workspace-scoped Thread and Workflow Run facts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ProjectThreadSummary(BaseModel):
    """Metadata-only summary of a Thread bound to one exact workspace ref."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=300)
    status: str = Field(min_length=1, max_length=100)
    created_at: datetime
    updated_at: datetime


class ProjectWorkflowRunSummary(BaseModel):
    """Metadata-only summary of a legacy/current Workflow Run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=300)
    status: str = Field(min_length=1, max_length=100)
    current_stage: str = Field(min_length=1, max_length=100)
    summary: str = Field(max_length=500)
    created_at: datetime
    updated_at: datetime


class ProjectProjection(BaseModel):
    """The Phase 1E ``Project = registered Workspace`` read projection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str = Field(min_length=1, max_length=300)
    workspace_ref: str = Field(min_length=1, max_length=4_096)
    readable: bool
    writable: bool
    created_at: datetime
    threads: tuple[ProjectThreadSummary, ...] = ()
    workflow_runs: tuple[ProjectWorkflowRunSummary, ...] = ()


class WorkspaceFileEntry(BaseModel):
    """Metadata for one safe, immediate child of a browsed directory."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1, max_length=4_096)
    name: str = Field(min_length=1, max_length=255)
    type: Literal["file", "directory"]
    size_bytes: int | None = Field(default=None, ge=0, le=2**63 - 1)
    modified_at: datetime | None = None


class WorkspaceFilesPage(BaseModel):
    """A deterministic directory page using a non-event snapshot token."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: str = Field(min_length=1, max_length=300)
    path: str = Field(min_length=1, max_length=4_096)
    entries: tuple[WorkspaceFileEntry, ...] = ()
    snapshot: str = Field(pattern=r"^[0-9a-f]{64}$")
    next_page_token: str | None = None
