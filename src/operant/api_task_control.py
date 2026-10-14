"""Typed Goal, Plan and checklist commands for the live task view."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from operant.application.plan_generation import PlanGenerationResult, generate_plan_draft
from operant.application.service import ApplicationService
from operant.application.task_control import TaskControlService
from operant.domain.task_control import (
    ChecklistStatus,
    ExecutionChecklistItem,
    Goal,
    PlanArtifact,
)
from operant.persistence.sqlite import ConflictError, NotFoundError


class CreateGoalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner_thread_id: str = Field(min_length=1, max_length=300)
    objective: str = Field(min_length=1, max_length=12000)
    token_budget: int | None = Field(default=None, ge=1)
    cost_budget: float | None = Field(default=None, gt=0)
    time_budget_seconds: int | None = Field(default=None, ge=1)
    completion_criteria: tuple[str, ...] = ()
    linked_thread_ids: tuple[str, ...] = ()
    linked_workflow_run_ids: tuple[str, ...] = ()


class UpdateRecordBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    changes: dict[str, Any]


class CreatePlanBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal_id: str = Field(min_length=1, max_length=300)
    scope: str = Field(min_length=1, max_length=12000)
    assumptions: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    proposed_changes: tuple[str, ...] = ()
    risk_items: tuple[str, ...] = ()
    approval_requirements: tuple[str, ...] = ()
    verification_plan: tuple[str, ...] = ()
    created_from_context_revision: str | None = None


class GeneratePlanBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_session_id: str = Field(min_length=1, max_length=300)
    planner_role_id: str = Field(default="role_planner", min_length=1, max_length=300)


class CreateChecklistBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=12000)
    dependencies: tuple[str, ...] = ()
    assigned_agent: str | None = None


class ChecklistStatusBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    status: ChecklistStatus
    evidence_refs: tuple[str, ...] = ()
    blocker: str | None = None
    node_run_id: str | None = None


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, NotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def install_task_control_routes(app: FastAPI, service: ApplicationService) -> None:
    control = TaskControlService(service.store)

    @app.post("/v1/goals", status_code=201, response_model=Goal, operation_id="createGoal")
    def create_goal(body: CreateGoalBody) -> Goal:
        try:
            return control.create_goal(Goal(**body.model_dump()))
        except (NotFoundError, ConflictError, ValueError) as exc:
            raise _http_error(exc) from exc

    @app.get("/v1/goals", response_model=list[Goal], operation_id="listGoals")
    def list_goals(owner_thread_id: str = Query(min_length=1)) -> list[Goal]:
        try:
            return control.list_goals(owner_thread_id)
        except (NotFoundError, ValueError) as exc:
            raise _http_error(exc) from exc

    @app.get("/v1/goals/{goal_id}", response_model=Goal, operation_id="getGoal")
    def get_goal(goal_id: str) -> Goal:
        try:
            return control.get_goal(goal_id)
        except NotFoundError as exc:
            raise _http_error(exc) from exc

    @app.patch("/v1/goals/{goal_id}", response_model=Goal, operation_id="updateGoal")
    def update_goal(goal_id: str, body: UpdateRecordBody) -> Goal:
        try:
            return control.update_goal(
                goal_id, expected_revision=body.expected_revision, **body.changes
            )
        except (NotFoundError, ConflictError, ValueError) as exc:
            raise _http_error(exc) from exc

    @app.post(
        "/v1/goals/{goal_id}/plans",
        status_code=201,
        response_model=PlanArtifact,
        operation_id="createPlan",
    )
    def create_plan(goal_id: str, body: CreatePlanBody) -> PlanArtifact:
        if body.goal_id != goal_id:
            raise HTTPException(status_code=400, detail="goal path and body differ")
        try:
            return control.create_plan(PlanArtifact(**body.model_dump()))
        except (NotFoundError, ConflictError, ValueError) as exc:
            raise _http_error(exc) from exc

    @app.post(
        "/v1/goals/{goal_id}/plans/generate",
        status_code=201,
        response_model=PlanGenerationResult,
        operation_id="generatePlanDraft",
    )
    async def generate_plan(goal_id: str, body: GeneratePlanBody) -> PlanGenerationResult:
        try:
            goal = control.get_goal(goal_id)
            thread = service.get_thread(goal.owner_thread_id)
            if not thread.workspace_ref:
                raise ValueError("Goal owner Thread has no bound workspace")
            return await generate_plan_draft(
                service,
                goal_id=goal_id,
                source_session_id=body.source_session_id,
                thread_id=thread.id,
                workspace=thread.workspace_ref,
                planner_role_id=body.planner_role_id,
            )
        except (NotFoundError, ConflictError, ValueError, PermissionError) as exc:
            if isinstance(exc, PermissionError):
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            raise _http_error(exc) from exc

    @app.get(
        "/v1/goals/{goal_id}/plans", response_model=list[PlanArtifact], operation_id="listPlans"
    )
    def list_plans(goal_id: str) -> list[PlanArtifact]:
        try:
            return control.list_plans(goal_id)
        except NotFoundError as exc:
            raise _http_error(exc) from exc

    @app.get("/v1/plans/{plan_id}", response_model=PlanArtifact, operation_id="getPlan")
    def get_plan(plan_id: str) -> PlanArtifact:
        try:
            return control.get_plan(plan_id)
        except NotFoundError as exc:
            raise _http_error(exc) from exc

    @app.patch("/v1/plans/{plan_id}", response_model=PlanArtifact, operation_id="updatePlan")
    def update_plan(plan_id: str, body: UpdateRecordBody) -> PlanArtifact:
        try:
            return control.update_plan(
                plan_id, expected_revision=body.expected_revision, **body.changes
            )
        except (NotFoundError, ConflictError, ValueError) as exc:
            raise _http_error(exc) from exc

    @app.post(
        "/v1/plans/{plan_id}/checklist",
        status_code=201,
        response_model=ExecutionChecklistItem,
        operation_id="createChecklistItem",
    )
    def create_checklist_item(plan_id: str, body: CreateChecklistBody) -> ExecutionChecklistItem:
        if body.plan_id != plan_id:
            raise HTTPException(status_code=400, detail="plan path and body differ")
        try:
            return control.add_checklist_item(ExecutionChecklistItem(**body.model_dump()))
        except (NotFoundError, ConflictError, ValueError) as exc:
            raise _http_error(exc) from exc

    @app.get(
        "/v1/plans/{plan_id}/checklist",
        response_model=list[ExecutionChecklistItem],
        operation_id="listChecklistItems",
    )
    def list_checklist_items(plan_id: str) -> list[ExecutionChecklistItem]:
        try:
            return control.list_checklist_items(plan_id)
        except NotFoundError as exc:
            raise _http_error(exc) from exc

    @app.post(
        "/v1/checklist/{item_id}/status",
        response_model=ExecutionChecklistItem,
        operation_id="commandChecklistStatus",
    )
    def command_checklist_status(item_id: str, body: ChecklistStatusBody) -> ExecutionChecklistItem:
        try:
            return control.command_checklist_status(
                item_id,
                expected_revision=body.expected_revision,
                status=body.status,
                evidence_refs=body.evidence_refs,
                blocker=body.blocker,
                node_run_id=body.node_run_id,
            )
        except (NotFoundError, ConflictError, ValueError) as exc:
            raise _http_error(exc) from exc
