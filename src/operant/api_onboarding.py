"""Local setup and conversation initialization routes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any, cast
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
    ConversationLocalControlOpen,
    ConversationLocalControlSession,
    ConversationLocalControlSessionList,
    ConversationMetadata,
    ConversationMetadataList,
    ConversationRename,
    ConversationStart,
    SetupBootstrap,
    SetupState,
)
from operant.domain.actions import CommandExecutionStatus
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
    onboarding = OnboardingService(
        service,
        repo,
        local_control_manager=lambda: getattr(app.state, "local_control_manager", None),
    )
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

    def local_control_runtime(request: Request) -> tuple[Any, Any, Any]:
        require_local(request)
        prepare = getattr(app.state, "local_control_prepare_request", None)
        manager = getattr(app.state, "local_control_manager", None)
        mutation = getattr(app.state, "local_control_mutation", None)
        call = getattr(app.state, "local_control_call", None)
        if not all(callable(item) for item in (prepare, mutation, call)) or manager is None:
            raise HTTPException(status_code=503, detail="local control is unavailable")
        cast(Callable[[Request], None], prepare)(request)
        return manager, mutation, call

    def local_control_view(session: Any) -> ConversationLocalControlSession:
        state = session.state
        error = session.last_error
        if state == "active":
            try:
                app.state.local_control_manager.verified_snapshot_binding(session.session_id)
            except (KeyError, ValueError, PermissionError, ConflictError):
                state = "failed"
                error = "本机控制会话不可用，请重新开启"
        return ConversationLocalControlSession(
            session_id=session.session_id,
            plugin_id=session.plugin_id,
            target_id=session.target_id,
            computer_bundle_id=session.computer_bundle_id,
            allowed_targets=session.authorized_targets,
            state=state,
            last_error=error,
        )

    def receipt_view(raw: Any, manager: Any, request_id: str) -> ConversationLocalControlSession:
        if not isinstance(raw, dict):
            raise HTTPException(status_code=409, detail="local control receipt is invalid")
        if raw.get("operation") != "conversation_open":
            raise HTTPException(status_code=409, detail="request belongs to another operation")
        target = raw.get("target")
        arguments = raw.get("arguments")
        session_body = raw.get("session")
        if not isinstance(target, str) or not isinstance(arguments, dict):
            raise HTTPException(status_code=409, detail="local control receipt is incomplete")
        try:
            saved = ConversationLocalControlSession.model_validate(session_body)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail="local control receipt is invalid") from exc
        expected_session_id = (
            "local-control-" + hashlib.sha256(request_id.encode()).hexdigest()[:24]
        )
        if saved.session_id != expected_session_id or saved.target_id != expected_session_id:
            raise HTTPException(status_code=409, detail="local control receipt identity changed")
        allowed_targets = arguments.get("allowed_targets")
        if not isinstance(allowed_targets, list | tuple) or any(
            not isinstance(item, str) for item in allowed_targets
        ):
            raise HTTPException(status_code=409, detail="local control receipt scope is invalid")
        expected_targets = tuple(allowed_targets)
        selected_targets = (
            (saved.computer_bundle_id,)
            if saved.computer_bundle_id is not None
            else expected_targets
        )
        if (
            saved.plugin_id != target
            or saved.allowed_targets != selected_targets
            or not arguments.get("source_digest")
            or not arguments.get("generation")
        ):
            raise HTTPException(status_code=409, detail="local control receipt scope changed")
        current = manager.sessions.get(saved.session_id)
        if current is None:
            return saved.model_copy(
                update={"state": "closed", "last_error": "Core 重启后控制会话已结束"}
            )
        if (
            not current.conversation_only
            or current.plugin_id != saved.plugin_id
            or current.target_id != saved.target_id
            or current.authorized_targets != saved.allowed_targets
        ):
            return saved.model_copy(update={"state": "failed", "last_error": "控制会话目标已变化"})
        return local_control_view(current)

    @app.post(
        "/v1/setup/local-control/sessions",
        operation_id="openConversationLocalControl",
        response_model=ConversationLocalControlSession,
    )
    async def open_conversation_local_control(
        request: Request,
        response: Response,
        body: ConversationLocalControlOpen,
        idempotency_key: str | None = Header(default=None, min_length=1, max_length=300),
    ) -> ConversationLocalControlSession:
        manager, mutation, call = local_control_runtime(request)
        key = command_key(idempotency_key, response)

        def perform_command() -> dict[str, Any]:
            record = manager.registry.get(body.plugin_id, require_enabled=True)
            arguments = {
                "source_digest": record.source_digest,
                "generation": record.generation,
                "allowed_targets": record.allowed_targets,
                "computer_bundle_id": body.computer_bundle_id,
            }

            def perform() -> dict[str, Any]:
                session = manager.open(
                    body.plugin_id,
                    key,
                    body.computer_bundle_id,
                    conversation_only=True,
                )
                return {
                    "operation": "conversation_open",
                    "target": body.plugin_id,
                    "arguments": arguments,
                    "session": local_control_view(session).model_dump(mode="json"),
                }

            return cast(
                dict[str, Any],
                mutation("conversation_open", body.plugin_id, arguments, key, perform),
            )

        raw = await call(perform_command)
        return receipt_view(raw, manager, key)

    @app.get(
        "/v1/setup/local-control/sessions",
        operation_id="listConversationLocalControlSessions",
        response_model=ConversationLocalControlSessionList,
    )
    def list_conversation_local_control(
        request: Request, request_id: str | None = Query(default=None, min_length=1, max_length=300)
    ) -> ConversationLocalControlSessionList:
        manager, _, _ = local_control_runtime(request)
        if request_id is not None:
            with service.store._connect() as connection:
                row = connection.execute(
                    "SELECT id FROM command_executions "
                    "WHERE command_type='local-control.management' AND idempotency_key=?",
                    (request_id,),
                ).fetchone()
            if row is None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "command_outcome_unknown",
                        "message": "开启结果尚未确认。请稍后刷新核对，暂时不要重试。",
                    },
                )
            command = service.store.get_command_execution(str(row["id"]))
            if command.status is not CommandExecutionStatus.COMPLETED or not command.response_json:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "command_outcome_unknown",
                        "message": "开启结果尚未确认。请稍后刷新核对，暂时不要重试。",
                    },
                )
            try:
                raw = json.loads(command.response_json)
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=409, detail="local control receipt is unreadable"
                ) from exc
            if not isinstance(raw, dict):
                raise HTTPException(status_code=409, detail="local control receipt is invalid")
            expected = canonical_action_hash(
                {
                    "operation": raw.get("operation"),
                    "target": raw.get("target"),
                    "arguments": raw.get("arguments"),
                }
            )
            if command.action_hash != expected:
                raise HTTPException(status_code=409, detail="local control receipt changed")
            return ConversationLocalControlSessionList(
                items=[receipt_view(raw, manager, request_id)]
            )
        return ConversationLocalControlSessionList(
            items=[
                local_control_view(session)
                for session in sorted(manager.sessions.values(), key=lambda item: item.session_id)
                if session.conversation_only
            ]
        )

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
            body.command_payload(),
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
