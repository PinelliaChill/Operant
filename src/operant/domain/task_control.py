"""Durable Goal, Plan, and execution-checklist command state."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from operant.domain.models import new_id, utc_now


class GoalStatus(str, Enum):
    ACTIVE = "active"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class PlanStatus(str, Enum):
    DRAFT = "draft"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    SUPERSEDED = "superseded"


class ChecklistStatus(str, Enum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"
    SKIPPED = "skipped"


class Goal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("goal"), min_length=1, max_length=300)
    owner_thread_id: str = Field(min_length=1, max_length=300)
    objective: str = Field(min_length=1, max_length=12000)
    status: GoalStatus = GoalStatus.ACTIVE
    token_budget: int | None = Field(default=None, ge=1)
    cost_budget: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    time_budget_seconds: int | None = Field(default=None, ge=1)
    completion_criteria: tuple[str, ...] = ()
    linked_thread_ids: tuple[str, ...] = ()
    linked_workflow_run_ids: tuple[str, ...] = ()
    blocked_reason: str | None = Field(default=None, max_length=4000)
    result_ref: str | None = Field(default=None, max_length=2048)
    revision: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_state(self) -> Goal:
        if not self.objective.strip():
            raise ValueError("goal objective must not be blank")
        if any(not item.strip() for item in self.completion_criteria):
            raise ValueError("completion criteria must not be blank")
        for name in ("linked_thread_ids", "linked_workflow_run_ids"):
            values = getattr(self, name)
            if any(not item or len(item) > 300 for item in values) or len(values) != len(
                set(values)
            ):
                raise ValueError(f"{name} must contain unique nonempty ids")
        if self.status is GoalStatus.BLOCKED and not (self.blocked_reason or "").strip():
            raise ValueError("blocked goal requires a reason")
        if self.status is not GoalStatus.BLOCKED and self.blocked_reason is not None:
            raise ValueError("only a blocked goal may have a blocked reason")
        if self.status is GoalStatus.COMPLETED and not (self.result_ref or "").strip():
            raise ValueError("completed goal requires a result reference")
        return self


class PlanArtifact(BaseModel):
    """Editable plan record. Creation records a read-only planning boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("plan"), min_length=1, max_length=300)
    goal_id: str = Field(min_length=1, max_length=300)
    scope: str = Field(default="", max_length=12000)
    assumptions: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    proposed_changes: tuple[str, ...] = ()
    risk_items: tuple[str, ...] = ()
    approval_requirements: tuple[str, ...] = ()
    verification_plan: tuple[str, ...] = ()
    created_from_context_revision: str | None = Field(default=None, max_length=300)
    source_mode: Literal["read_only"] = "read_only"
    status: PlanStatus = PlanStatus.DRAFT
    revision: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_content(self) -> PlanArtifact:
        for name in (
            "assumptions",
            "constraints",
            "open_questions",
            "proposed_changes",
            "risk_items",
            "approval_requirements",
            "verification_plan",
        ):
            if any(not item.strip() for item in getattr(self, name)):
                raise ValueError(f"{name} must not contain blank items")
        return self


class ExecutionChecklistItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_id("check"), min_length=1, max_length=300)
    plan_id: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=12000)
    dependencies: tuple[str, ...] = ()
    status: ChecklistStatus = ChecklistStatus.TODO
    assigned_agent: str | None = Field(default=None, max_length=300)
    node_run_id: str | None = Field(default=None, max_length=300)
    evidence_refs: tuple[str, ...] = ()
    blocker: str | None = Field(default=None, max_length=4000)
    revision: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_state(self) -> ExecutionChecklistItem:
        if not self.description.strip():
            raise ValueError("checklist description must not be blank")
        if any(not item or len(item) > 300 for item in self.dependencies):
            raise ValueError("checklist dependencies must contain ids")
        if len(self.dependencies) != len(set(self.dependencies)) or self.id in self.dependencies:
            raise ValueError("checklist dependencies must be unique and cannot include itself")
        if any(not item.strip() or len(item) > 2048 for item in self.evidence_refs):
            raise ValueError("checklist evidence references must not be blank")
        if self.status is ChecklistStatus.BLOCKED and not (self.blocker or "").strip():
            raise ValueError("blocked checklist item requires a blocker")
        if self.status is not ChecklistStatus.BLOCKED and self.blocker is not None:
            raise ValueError("only a blocked checklist item may have a blocker")
        if self.status is ChecklistStatus.DONE and not self.evidence_refs:
            raise ValueError("completed checklist item requires evidence")
        return self
