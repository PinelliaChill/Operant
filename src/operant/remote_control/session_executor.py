"""The small set of Session operations accepted from a paired remote device."""

from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from operant.application.service import ApplicationService
from operant.domain.security import ActionRequest, Capability
from operant.persistence.sqlite import ConflictError, NotFoundError
from operant.remote_control.runtime import RemoteActionPayload, RemoteControlError

REMOTE_SESSION_CAPABILITIES: dict[tuple[str, str], tuple[Capability, ...]] = {
    ("session", "create"): (Capability.REMOTE_CONTROL_COMMAND,),
    ("session", "run"): (
        Capability.REMOTE_CONTROL_COMMAND,
        Capability.WORKSPACE_READ,
        Capability.NETWORK_EGRESS,
    ),
    ("session", "status"): (Capability.REMOTE_CONTROL_OBSERVE,),
    ("session", "cancel"): (Capability.REMOTE_CONTROL_COMMAND,),
}


class _Create(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role_id: str = Field(min_length=1, max_length=300)
    project_id: str = Field(min_length=1, max_length=300)
    model_profile_id: str | None = Field(default=None, min_length=1, max_length=300)


class _Run(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=20_000)


def _bound_workspace(service: ApplicationService, session_id: str, principal: str) -> Path:
    if not principal.startswith("remote-device:"):
        raise RemoteControlError(
            "remote.device_invalid", "Remote device identity is invalid", status_code=403
        )
    with service.store._connect() as connection:
        owned = connection.execute(
            "SELECT 1 FROM remote_command_receipts WHERE device_id=? "
            "AND result_ref=? AND status='completed' LIMIT 1",
            (principal.removeprefix("remote-device:"), f"session:{session_id}"),
        ).fetchone()
    if owned is None:
        raise RemoteControlError(
            "remote.session_scope_denied", "Session does not belong to this device", status_code=403
        )
    session = service.get_session(session_id)
    project_id = session.role_snapshot.config_project_id
    if not project_id:
        raise RemoteControlError(
            "remote.session_project_required", "Session has no Project binding", status_code=403
        )
    project = service.store.get_workspace_initialization_by_id(project_id)
    workspace = Path(project.workspace_ref)
    if not workspace.is_absolute() or not workspace.is_dir() or not project.readable:
        raise RemoteControlError(
            "remote.workspace_unavailable", "Project workspace is unavailable", status_code=409
        )
    if session.role_snapshot.config_workspace_ref != str(workspace):
        raise RemoteControlError(
            "remote.workspace_binding_changed", "Session workspace binding changed", status_code=409
        )
    return workspace


def session_remote_executor(
    service: ApplicationService,
    payload: RemoteActionPayload,
    action: ActionRequest,
) -> str:
    """Run only a Project-bound Session; the caller already passed Action Gateway."""

    if payload.tool != "session" or payload.workspace is not None or payload.secret_refs:
        raise RemoteControlError(
            "remote.operation_unavailable",
            "Remote Session operation is unavailable",
            status_code=403,
        )
    try:
        if payload.operation == "create":
            if payload.target_id != "new":
                raise ValueError("new Session target is required")
            request = _Create.model_validate(payload.arguments)
            project = service.store.get_workspace_initialization_by_id(request.project_id)
            if not project.readable or not Path(project.workspace_ref).is_absolute():
                raise ValueError("Project workspace is unavailable")
            session = service.create_session(
                request.role_id,
                project_id=request.project_id,
                model_profile_id=request.model_profile_id,
            )
            return f"session:{session.id}"
        if payload.operation not in {"run", "status", "cancel"} or payload.target_id == "new":
            raise ValueError("Session operation is not registered")
        session_id = payload.target_id
        workspace = _bound_workspace(service, session_id, action.principal)
        if payload.operation == "status":
            if payload.arguments:
                raise ValueError("status does not accept arguments")
            events = service.list_events(session_id, limit=1_000)
            cursor = max((event.cursor or 0 for event in events), default=0)
            return f"session:{session_id}:cursor:{cursor}"
        if payload.operation == "cancel":
            if payload.arguments:
                raise ValueError("cancel does not accept arguments")
            return f"session:{session_id}:cancel:{str(service.cancel_session(session_id)).lower()}"
        run_request = _Run.model_validate(payload.arguments)

        async def run() -> None:
            async for _event in service.run_session(
                session_id,
                user_message=run_request.message,
                workspace=workspace,
            ):
                pass

        asyncio.run(run())
        return f"session:{session_id}"
    except (ValueError, NotFoundError, ConflictError) as exc:
        raise RemoteControlError(
            "remote.session_invalid", "Remote Session request was rejected", status_code=409
        ) from exc
