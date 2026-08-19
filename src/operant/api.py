from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from operant.application.service import ApplicationService
from operant.application.workflow import SequentialCodingWorkflow
from operant.domain.models import (
    Budget,
    Effort,
    EffortMapping,
    ModelProfile,
    RolePreset,
    RoleStatus,
    ToolPolicy,
)
from operant.persistence.sqlite import (
    ConflictError,
    NotFoundError,
    SQLiteStore,
)
from operant.providers.openai_compatible import (
    OpenAICompatibleProvider,
    ProviderError,
)
from operant.settings import database_path, load_local_env


class CreateModelProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    model_id: str
    base_url: str
    secret_ref: str = "OPERANT_API_KEY"
    context_window: int | None = None
    default_token_budget: int | None = None
    supported_efforts: tuple[Effort, ...] = (
        Effort.LOW,
        Effort.MEDIUM,
        Effort.HIGH,
    )
    default_effort: Effort = Effort.MEDIUM
    effort_parameter: str | None = "reasoning_effort"
    effort_mapping: tuple[EffortMapping, ...] = (
        EffortMapping(effort=Effort.LOW, provider_value="low"),
        EffortMapping(effort=Effort.MEDIUM, provider_value="medium"),
        EffortMapping(effort=Effort.HIGH, provider_value="high"),
    )

    def to_domain(self) -> ModelProfile:
        return ModelProfile(**self.model_dump())


class UpdateModelProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    model_id: str | None = None
    base_url: str | None = None
    secret_ref: str | None = None
    context_window: int | None = None
    default_token_budget: int | None = None
    supported_efforts: tuple[Effort, ...] | None = None
    default_effort: Effort | None = None
    effort_parameter: str | None = None
    effort_mapping: tuple[EffortMapping, ...] | None = None
    enabled: bool | None = None


class DiscoverModelsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str
    secret_ref: str = "OPERANT_API_KEY"


class CreateRoleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    system_prompt: str
    model_profile_id: str
    effort: Effort = Effort.MEDIUM
    tool_policy: ToolPolicy = Field(default_factory=ToolPolicy)
    budget: Budget = Field(default_factory=Budget)
    memory_scope: str = "session"

    def to_domain(self) -> RolePreset:
        return RolePreset(**self.model_dump())


class UpdateRoleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    system_prompt: str | None = None
    model_profile_id: str | None = None
    effort: Effort | None = None
    tool_policy: ToolPolicy | None = None
    budget: Budget | None = None
    memory_scope: str | None = None
    status: RoleStatus | None = None


class CopyRoleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str


class SeedRolesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    planner_model_profile_id: str
    coder_model_profile_id: str
    reviewer_model_profile_id: str


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role_id: str | None = None
    new_role: CreateRoleRequest | None = None
    model_profile_id: str | None = None
    effort: Effort | None = None
    budget_overrides: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_role_source(self) -> CreateSessionRequest:
        if (self.role_id is None) == (self.new_role is None):
            raise ValueError("provide exactly one of role_id or new_role")
        return self


class RunSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1)
    workspace: str = Field(min_length=1)


class ApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool


class WorkflowRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1)
    workspace: str = Field(min_length=1)
    planner_role_id: str = "role_planner"
    coder_role_id: str = "role_coder"
    reviewer_role_id: str = "role_reviewer"
    max_rework_rounds: int = Field(default=1, ge=0, le=3)


def create_app(db_path: str | Path | None = None) -> FastAPI:
    load_local_env()
    store = SQLiteStore(db_path or database_path())
    service = ApplicationService(store, OpenAICompatibleProvider())
    service.initialize()
    workflow = SequentialCodingWorkflow(service)
    app = FastAPI(
        title="Operant API",
        description="由角色预设驱动的多模型 Coding Agent Runtime。",
        version="0.1.0",
    )

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/models")
    async def list_models() -> list[dict[str, object]]:
        return [profile.model_dump(mode="json") for profile in service.list_model_profiles()]

    @app.post("/v1/models", status_code=201)
    async def create_model(
        request: CreateModelProfileRequest,
    ) -> dict[str, object]:
        try:
            profile = service.add_model_profile(request.to_domain())
        except (ConflictError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return profile.model_dump(mode="json")

    @app.get("/v1/models/{profile_id}")
    async def get_model(profile_id: str) -> dict[str, object]:
        try:
            profile = service.get_model_profile(profile_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return profile.model_dump(mode="json")

    @app.patch("/v1/models/{profile_id}")
    async def update_model(
        profile_id: str, request: UpdateModelProfileRequest
    ) -> dict[str, object]:
        try:
            profile = service.update_model_profile(
                profile_id, **request.model_dump(exclude_unset=True)
            )
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return profile.model_dump(mode="json")

    @app.delete("/v1/models/{profile_id}")
    async def deactivate_model(profile_id: str) -> dict[str, object]:
        try:
            profile = service.deactivate_model_profile(profile_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return profile.model_dump(mode="json")

    @app.post("/v1/models/discover")
    async def discover_models(
        request: DiscoverModelsRequest,
    ) -> dict[str, list[str]]:
        try:
            model_ids = await service.discover_models(
                base_url=request.base_url,
                secret_ref=request.secret_ref,
            )
        except ProviderError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"model_ids": model_ids}

    @app.post("/v1/models/{profile_id}/health")
    async def check_model(profile_id: str) -> dict[str, object]:
        try:
            return await service.check_model_profile(profile_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ProviderError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/v1/roles")
    async def list_roles(
        include_inactive: bool = False,
    ) -> list[dict[str, object]]:
        return [
            role.model_dump(mode="json")
            for role in service.list_roles(include_inactive=include_inactive)
        ]

    @app.post("/v1/roles", status_code=201)
    async def create_role(
        request: CreateRoleRequest,
    ) -> dict[str, object]:
        try:
            role = service.create_role(request.to_domain())
        except (ConflictError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return role.model_dump(mode="json")

    @app.post("/v1/roles/seed-defaults")
    async def seed_default_roles(
        request: SeedRolesRequest,
    ) -> list[dict[str, object]]:
        try:
            roles = service.seed_default_roles(**request.model_dump())
        except (NotFoundError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return [role.model_dump(mode="json") for role in roles]

    @app.get("/v1/roles/{role_id}")
    async def get_role(role_id: str, version: int | None = None) -> dict[str, object]:
        try:
            role = service.get_role(role_id, version)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return role.model_dump(mode="json")

    @app.get("/v1/roles/{role_id}/versions")
    async def list_role_versions(
        role_id: str,
    ) -> list[dict[str, object]]:
        try:
            roles = service.list_role_versions(role_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return [role.model_dump(mode="json") for role in roles]

    @app.patch("/v1/roles/{role_id}")
    async def update_role(role_id: str, request: UpdateRoleRequest) -> dict[str, object]:
        try:
            role = service.update_role(role_id, **request.model_dump(exclude_unset=True))
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return role.model_dump(mode="json")

    @app.post("/v1/roles/{role_id}/copy", status_code=201)
    async def copy_role(role_id: str, request: CopyRoleRequest) -> dict[str, object]:
        try:
            role = service.copy_role(role_id, name=request.name)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return role.model_dump(mode="json")

    @app.delete("/v1/roles/{role_id}")
    async def deactivate_role(role_id: str) -> dict[str, object]:
        try:
            role = service.deactivate_role(role_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return role.model_dump(mode="json")

    @app.post("/v1/sessions", status_code=201)
    async def create_session(
        request: CreateSessionRequest,
    ) -> dict[str, object]:
        try:
            session = service.create_session(
                request.role_id,
                new_role=(None if request.new_role is None else request.new_role.to_domain()),
                model_profile_id=request.model_profile_id,
                effort=(None if request.effort is None else request.effort.value),
                budget_overrides=request.budget_overrides,
            )
        except (NotFoundError, ConflictError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return session.model_dump(mode="json")

    @app.get("/v1/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, object]:
        try:
            session = service.get_session(session_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return session.model_dump(mode="json")

    @app.get("/v1/sessions/{session_id}/events")
    async def list_events(
        session_id: str,
    ) -> list[dict[str, object]]:
        try:
            events = service.list_events(session_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return [event.model_dump(mode="json") for event in events]

    @app.post("/v1/sessions/{session_id}/runs")
    async def run_session(session_id: str, request: RunSessionRequest) -> StreamingResponse:
        try:
            service.get_session(session_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        async def stream_events() -> AsyncIterator[str]:
            async for event in service.run_session(
                session_id,
                user_message=request.message,
                workspace=request.workspace,
            ):
                yield (f"event: {event.event_type}\ndata: {event.model_dump_json()}\n\n")

        return StreamingResponse(stream_events(), media_type="text/event-stream")

    @app.post("/v1/sessions/{session_id}/cancel")
    async def cancel_session(session_id: str) -> dict[str, bool]:
        try:
            accepted = service.cancel_session(session_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"accepted": accepted}

    @app.get("/v1/sessions/{session_id}/approvals")
    async def list_approvals(
        session_id: str,
    ) -> list[dict[str, str]]:
        try:
            return service.list_pending_approvals(session_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/v1/sessions/{session_id}/approvals/{tool_call_id}")
    async def decide_approval(
        session_id: str,
        tool_call_id: str,
        request: ApprovalDecisionRequest,
    ) -> dict[str, bool]:
        try:
            accepted = service.submit_approval(
                session_id,
                tool_call_id,
                approved=request.approved,
            )
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if not accepted:
            raise HTTPException(
                status_code=409,
                detail="approval is not pending",
            )
        return {"accepted": True}

    @app.post("/v1/workflows/coding/runs")
    async def run_coding_workflow(
        request: WorkflowRunRequest,
    ) -> StreamingResponse:
        for role_id in (
            request.planner_role_id,
            request.coder_role_id,
            request.reviewer_role_id,
        ):
            try:
                service.get_role(role_id)
            except NotFoundError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        async def stream_events() -> AsyncIterator[str]:
            async for event in workflow.run(
                task=request.task,
                workspace=request.workspace,
                planner_role_id=request.planner_role_id,
                coder_role_id=request.coder_role_id,
                reviewer_role_id=request.reviewer_role_id,
                max_rework_rounds=request.max_rework_rounds,
            ):
                yield (f"event: {event.event_type}\ndata: {event.model_dump_json()}\n\n")

        return StreamingResponse(stream_events(), media_type="text/event-stream")

    return app


app = create_app()
