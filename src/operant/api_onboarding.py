"""Local setup and conversation initialization routes."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response

from operant.api_model_connections import _trusted_local
from operant.application.onboarding import OnboardingService
from operant.application.onboarding_collaboration import BasicTeamService, TeamApprovalRequired
from operant.application.service import ApplicationService
from operant.contracts.onboarding import (
    CollaborationStart,
    CollaborationStarted,
    CollaborationTemplateList,
    CollaborationTemplateView,
    ConversationInitialization,
    ConversationMetadata,
    ConversationMetadataList,
    ConversationRename,
    ConversationStart,
    SetupBootstrap,
    SetupState,
)
from operant.domain.security import Capability
from operant.persistence.sqlite import ConflictError, NotFoundError
from operant.protocol import canonical_action_hash


def install_onboarding_routes(
    app: FastAPI,
    service: ApplicationService,
    repo: Any,
    *,
    local_authorizer: Callable[[Request], bool] | None = None,
) -> OnboardingService:
    onboarding = OnboardingService(service, repo)
    app.state.onboarding_service = onboarding
    teams = BasicTeamService(app, service, repo)

    def require_local(request: Request) -> None:
        if not _trusted_local(request, local_authorizer):
            raise HTTPException(status_code=403, detail="setup requires a trusted local client")

    def command_key(supplied: str | None, response: Response) -> str:
        key = supplied or uuid4().hex
        response.headers["Idempotency-Key"] = key
        return key

    def refresh_workspace_sources(workspace_id: str | None) -> None:
        refresh = getattr(app.state, "refresh_skill_sources", None)
        if not callable(refresh) or not workspace_id:
            return
        workspace = service.store.get_workspace_initialization_by_id(workspace_id)
        refresh(workspace.workspace_ref)

    def guard(operation: str, target_id: str, payload: dict[str, Any], key: str) -> None:
        try:
            action, decision, _ = app.state.phase45_action_gateway.guard(
                tool="onboarding",
                operation=operation,
                target_id=target_id,
                arguments={"request_hash": canonical_action_hash(payload)},
                capabilities=(Capability.WORKSPACE_WRITE,),
                idempotency_key=key,
            )
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail="request identity changed") from exc
        if decision.decision.value != "allow" or decision.lease is None:
            if decision.decision.value == "ask":
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "approval_required",
                        "approval_id": decision.approval_id,
                        "reason_code": decision.reason_code,
                    },
                )
            raise HTTPException(status_code=403, detail=decision.reason_code)
        app.state.phase45_action_gateway.consume(decision.lease, action)

    @app.get("/v1/setup/state", operation_id="getSetupState", response_model=SetupState)
    def get_state(request: Request) -> SetupState:
        require_local(request)
        return onboarding.state()

    @app.post("/v1/setup/bootstrap", operation_id="bootstrapSetup", response_model=SetupState)
    async def bootstrap(
        request: Request,
        response: Response,
        body: SetupBootstrap,
        idempotency_key: str | None = Header(default=None, min_length=1, max_length=300),
    ) -> SetupState:
        require_local(request)
        key = command_key(idempotency_key, response)
        guard(
            "bootstrap",
            body.model_profile_id or "default",
            body.model_dump(mode="json"),
            key,
        )
        try:
            state = await onboarding.bootstrap(body)
            refresh_workspace_sources(state.default_workspace_id)
            return state
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail="selected model was not found") from exc
        except (ConflictError, ValueError, PermissionError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/v1/setup/conversations",
        operation_id="initializeConversation",
        response_model=ConversationInitialization,
    )
    async def initialize_conversation(
        request: Request,
        response: Response,
        body: ConversationStart,
        idempotency_key: str | None = Header(default=None, min_length=1, max_length=300),
    ) -> ConversationInitialization:
        require_local(request)
        key = command_key(idempotency_key, response)
        guard(
            "conversation_create",
            body.thread_id or body.workspace_id or "default",
            body.model_dump(mode="json"),
            key,
        )
        try:
            result = await onboarding.initialize_conversation(body, idempotency_key=key)
            refresh_workspace_sources(result.workspace_id)
            return result
        except NotFoundError as exc:
            raise HTTPException(
                status_code=404, detail="selected conversation setting was not found"
            ) from exc
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ValueError, PermissionError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get(
        "/v1/setup/conversations/metadata",
        operation_id="listConversationMetadata",
        response_model=ConversationMetadataList,
    )
    def list_metadata(
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
        request_id: str | None = Query(default=None, min_length=1, max_length=300),
    ) -> ConversationMetadataList:
        require_local(request)
        return ConversationMetadataList(
            items=onboarding.list_conversation_metadata(limit=limit, request_id=request_id)
        )

    @app.get(
        "/v1/setup/conversations/{thread_id}/metadata",
        operation_id="getConversationMetadata",
        response_model=ConversationMetadata,
    )
    def get_metadata(request: Request, thread_id: str) -> ConversationMetadata:
        require_local(request)
        try:
            return onboarding.conversation_metadata(thread_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail="conversation not found") from exc

    @app.patch(
        "/v1/setup/conversations/{thread_id}/metadata",
        operation_id="renameConversation",
        response_model=ConversationMetadata,
    )
    def rename(
        thread_id: str,
        request: Request,
        response: Response,
        body: ConversationRename,
        idempotency_key: str | None = Header(default=None, min_length=1, max_length=300),
    ) -> ConversationMetadata:
        require_local(request)
        key = command_key(idempotency_key, response)
        guard(
            "conversation_rename",
            thread_id,
            {"thread_id": thread_id, **body.model_dump(mode="json")},
            key,
        )
        try:
            return onboarding.rename_conversation(thread_id, body, idempotency_key=key)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail="conversation not found") from exc
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get(
        "/v1/setup/team-templates",
        operation_id="listTeamTemplates",
        response_model=CollaborationTemplateList,
    )
    def list_team_templates(request: Request) -> CollaborationTemplateList:
        require_local(request)
        return CollaborationTemplateList(
            items=[
                CollaborationTemplateView(
                    template_id="basic",
                    name="基础协作团队",
                    description="规划、执行、审查同一任务",
                    member_count=3,
                )
            ]
        )

    @app.post(
        "/v1/setup/teams/start",
        operation_id="startTemplateTeam",
        response_model=CollaborationStarted,
        status_code=202,
    )
    async def start_team(
        request: Request,
        response: Response,
        body: CollaborationStart,
        idempotency_key: str | None = Header(default=None, min_length=1, max_length=300),
    ) -> CollaborationStarted:
        require_local(request)
        key = command_key(idempotency_key, response)
        try:
            refresh_workspace_sources(body.workspace_id or repo.get_setting("default_workspace_id"))
            result = teams.start(
                template_id=body.template_id,
                task=body.task,
                workspace_id=body.workspace_id,
                idempotency_key=key,
            )
            return CollaborationStarted.model_validate(result)
        except TeamApprovalRequired as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "approval_required",
                    "approval_id": exc.approval_id,
                    "reason_code": exc.reason_code,
                },
            ) from exc
        except NotFoundError as exc:
            raise HTTPException(
                status_code=404, detail="workspace or team member not found"
            ) from exc
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return onboarding
