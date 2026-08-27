from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_workflow_id() -> str:
    return f"workflow_{uuid4().hex}"


def new_workflow_event_id() -> str:
    return f"workflow_event_{uuid4().hex}"


class WorkflowRunStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    MANUAL_RECONCILE_REQUIRED = "manual_reconcile_required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkflowStage(str, Enum):
    CREATED = "created"
    PLANNER = "planner"
    EXPLORERS = "explorers"
    CODER = "coder"
    REVIEWER = "reviewer"
    MAIN = "main"
    COMPLETED = "completed"


class WorkflowRun(BaseModel):
    """Persisted execution identity and immutable role-selection input."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=new_workflow_id)
    task: str = Field(min_length=1, max_length=100_000)
    workspace: str = Field(min_length=1, max_length=4_096)
    main_role_id: str | None = None
    planner_role_id: str
    explorer_role_ids: tuple[str, ...] = ()
    coder_role_id: str
    reviewer_role_id: str
    max_parallel_explorers: int = Field(default=2, ge=1, le=4)
    max_rework_rounds: int = Field(default=1, ge=0, le=3)
    status: WorkflowRunStatus = WorkflowRunStatus.CREATED
    current_stage: WorkflowStage = WorkflowStage.CREATED
    resumed_from_id: str | None = None
    final_verdict: str | None = None
    last_error_type: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("task")
    @classmethod
    def reject_blank_task(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("workflow task must not be blank")
        return value

    @field_validator("workspace")
    @classmethod
    def require_absolute_workspace(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute():
            raise ValueError("workflow workspace must be an absolute path")
        return str(path.resolve(strict=False))

    @model_validator(mode="after")
    def validate_role_selection(self) -> WorkflowRun:
        if len(self.explorer_role_ids) > 4:
            raise ValueError("at most 4 explorer roles are allowed")
        if len(self.explorer_role_ids) != len(set(self.explorer_role_ids)):
            raise ValueError("explorer role ids must not contain duplicates")
        return self


class WorkflowRunEvent(BaseModel):
    """Immutable workflow-level event used for replay and checkpoints."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=new_workflow_event_id)
    workflow_run_id: str
    sequence: int | None = Field(default=None, ge=1)
    role: str
    session_id: str | None = None
    event_type: str = Field(min_length=1, max_length=100)
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
