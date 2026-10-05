"""Explicit, project-bound Skill commands in an ordinary conversation Run."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from operant.api_workbench_context import thread_session
from operant.application.phase45_gateway import Phase45ActionGateway
from operant.application.service import ApplicationService
from operant.domain.actions import CommandExecution, CommandExecutionStatus
from operant.domain.models import Event
from operant.domain.security import Capability, PolicyDecision
from operant.domain.threads import Item, SystemEventPayload, Turn
from operant.persistence.sqlite import ConflictError, IdempotencyConflictError
from operant.protocol import canonical_action_hash, redact_public_text


class SkillCommandArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=4000)


class ExecuteSkillCommandBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str = Field(pattern=r"^skill:skill_[0-9a-f]{32}$")
    arguments: SkillCommandArguments
    idempotency_key: str = Field(min_length=1, max_length=300)


class SkillCommandView(BaseModel):
    command: str
    skill_id: str
    description: str
    parameters: dict[str, Any]


class SkillCommandRegistry(BaseModel):
    commands: list[SkillCommandView]


class SkillCommandResult(BaseModel):
    command: str
    status: Literal["completed", "failed"]
    result: str
    resource_id: str


def install_skill_command_routes(
    app: FastAPI,
    service: ApplicationService,
    *,
    action_gateway: Phase45ActionGateway,
    local_authorizer: Callable[[Request], bool],
) -> None:
    def authorized(request: Request) -> None:
        if not local_authorizer(request):
            raise HTTPException(status_code=403, detail="local Skill command access denied")

    def available(thread_id: str) -> tuple[Any, str, dict[str, dict[str, str]]]:
        session = thread_session(service, thread_id)
        thread = service.get_thread(thread_id)
        if not thread.workspace_ref:
            raise ValueError("thread has no bound workspace")
        workspace = str(Path(thread.workspace_ref).resolve(strict=True))
        if session.role_snapshot.config_workspace_ref is not None and (
            workspace != session.role_snapshot.config_workspace_ref
        ):
            raise PermissionError("thread workspace differs from the frozen Session")
        manager = service.memory_manager or (
            service.memory_manager_factory() if service.memory_manager_factory else None
        )
        if manager is None:
            raise PermissionError("Skill runtime is unavailable")
        service.memory_manager = manager
        source = session.role_snapshot.config_sources.get("skill_ids")
        bounded = bool(source and source != f"role_base:{session.role_snapshot.role_id}")
        records = {
            record["skill_id"]: record
            for record in manager.list_invocable_skills(workspace)
            if not bounded or record["skill_id"] in session.role_snapshot.skill_ids
        }
        return session, workspace, records

    def replay(key: str, action_hash: str) -> SkillCommandResult | None:
        with service.store._connect() as connection:
            row = connection.execute(
                "SELECT id FROM command_executions WHERE command_type=? AND idempotency_key=?",
                ("skill.command", key),
            ).fetchone()
        if row is None:
            return None
        command = service.store.get_command_execution(str(row["id"]))
        if command.action_hash != action_hash:
            raise HTTPException(status_code=409, detail={"code": "idempotency_key_conflict"})
        if command.status is CommandExecutionStatus.COMPLETED and command.response_json:
            return SkillCommandResult.model_validate_json(command.response_json)
        raise HTTPException(
            status_code=409,
            detail={
                "code": (
                    "command_in_progress"
                    if command.status is CommandExecutionStatus.IN_PROGRESS
                    else "command_outcome_unknown"
                ),
                "command_execution_id": command.id,
            },
        )

    @app.get(
        "/v1/workbench/threads/{thread_id}/skill-commands",
        operation_id="listSkillCommands",
        response_model=SkillCommandRegistry,
    )
    def list_commands(thread_id: str, request: Request) -> SkillCommandRegistry:
        authorized(request)
        try:
            _, _, records = available(thread_id)
        except (KeyError, ValueError, PermissionError, OSError) as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return SkillCommandRegistry(
            commands=[
                SkillCommandView(
                    command=f"skill:{skill_id}",
                    skill_id=skill_id,
                    description=record["description"],
                    parameters={
                        "type": "object",
                        "properties": {
                            "prompt": {"type": "string", "minLength": 1, "maxLength": 4000}
                        },
                        "required": ["prompt"],
                        "additionalProperties": False,
                    },
                )
                for skill_id, record in sorted(records.items())
            ]
        )

    @app.post(
        "/v1/workbench/threads/{thread_id}/skill-commands",
        operation_id="executeSkillCommand",
        response_model=SkillCommandResult,
    )
    async def execute(
        thread_id: str, body: ExecuteSkillCommandBody, request: Request
    ) -> SkillCommandResult:
        authorized(request)
        skill_id = body.command.removeprefix("skill:")
        prompt = body.arguments.prompt
        action_hash = canonical_action_hash(
            {
                "thread_id": thread_id,
                "command": body.command,
                "prompt_sha256": canonical_action_hash({"prompt": prompt}),
            }
        )
        prior = replay(body.idempotency_key, action_hash)
        if prior is not None:
            return prior
        try:
            session, workspace, records = available(thread_id)
            if skill_id not in records:
                raise PermissionError("Skill is not installed and authorized for this Session")
        except (KeyError, ValueError, PermissionError, OSError) as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        try:
            admitted = service.admit_session_run(session.id)
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail={"code": "session_run_conflict"}) from exc
        if not admitted:
            raise HTTPException(status_code=409, detail={"code": "session_run_conflict"})
        lease = service.admitted_session_run_lease(session.id)
        if lease is None:
            service.release_session_run(session.id)
            raise HTTPException(status_code=409, detail={"code": "session_run_conflict"})
        command_id: str | None = None
        try:
            prior = replay(body.idempotency_key, action_hash)
            if prior is not None:
                return prior
            action, result, _ = action_gateway.guard(
                tool="skill_command",
                operation="invoke",
                target_id=skill_id,
                arguments={
                    "thread_id": thread_id,
                    "session_id": session.id,
                    "skill_digest": records[skill_id]["digest"],
                    "prompt_sha256": canonical_action_hash({"prompt": prompt}),
                },
                capabilities=(Capability.WORKSPACE_READ,),
                idempotency_key=body.idempotency_key,
            )
            if result.decision.value != PolicyDecision.ALLOW.value or result.lease is None:
                if result.decision.value == PolicyDecision.ASK.value:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "approval_required",
                            "reason_code": result.reason_code,
                            "approval_id": result.approval_id,
                            "action_hash": action.action_hash,
                            "policy_version": action.policy_version,
                        },
                    )
                raise HTTPException(status_code=403, detail=result.reason_code)
            action_gateway.consume(result.lease, action)
            try:
                command, created = service.store.reserve_command_execution(
                    CommandExecution(
                        command_type="skill.command",
                        idempotency_key=body.idempotency_key,
                        action_hash=action_hash,
                    )
                )
            except IdempotencyConflictError as exc:
                raise HTTPException(
                    status_code=409, detail={"code": "idempotency_key_conflict"}
                ) from exc
            if not created:
                previous = replay(body.idempotency_key, action_hash)
                assert previous is not None
                return previous
            command_id = command.id
            command_turn = service.create_turn(Turn(thread_id=thread_id))
            service.store.append_event(
                Event(
                    session_id=session.id,
                    event_type="skill.command.started",
                    payload={
                        "command": body.command,
                        "skill_id": skill_id,
                        "skill_digest": records[skill_id]["digest"],
                        "command_execution_id": command_id,
                        "prompt_sha256": canonical_action_hash({"prompt": prompt}),
                    },
                ),
                history_item=Item(
                    thread_id=thread_id,
                    turn_id=command_turn.id,
                    payload=SystemEventPayload(
                        event_type="skill.command.started",
                        summary=f"/{body.command} 已启动 · {skill_id}",
                        source_ref=command_id,
                    ),
                ),
            )
            terminal: Literal["completed", "failed", "unknown"] | None = None
            content = ""
            async for event in service.run_session(
                session.id,
                user_message=prompt,
                workspace=workspace,
                thread_id=thread_id,
                selected_skill_ids=(skill_id,),
                expected_skill_digests={skill_id: records[skill_id]["digest"]},
                _admission_granted=True,
            ):
                if event.event_type == "agent.completed":
                    terminal = "completed"
                    value = event.payload.get("content")
                    content = value if isinstance(value, str) else ""
                elif event.event_type in {
                    "agent.failed",
                    "agent.cancelled",
                    "agent.timed_out",
                    "budget.exhausted",
                }:
                    terminal = "failed"
                    content = event.event_type
                elif event.event_type == "agent.stream_error":
                    terminal = "unknown"
            if terminal not in {"completed", "failed"}:
                raise RuntimeError("Skill Run outcome is unknown")
            response = SkillCommandResult(
                command=body.command,
                status=terminal,
                result=redact_public_text(content, max_chars=4000),
                resource_id=session.id,
            )
            service.store.append_event(
                Event(
                    session_id=session.id,
                    event_type=f"skill.command.{terminal}",
                    payload={
                        "command": body.command,
                        "skill_id": skill_id,
                        "skill_digest": records[skill_id]["digest"],
                        "command_execution_id": command_id,
                    },
                ),
                history_item=Item(
                    thread_id=thread_id,
                    turn_id=command_turn.id,
                    payload=SystemEventPayload(
                        event_type=f"skill.command.{terminal}",
                        summary=f"/{body.command} {terminal} · {skill_id}",
                        source_ref=command_id,
                    ),
                ),
            )
            service.store.complete_command_execution(
                command_id,
                response_json=response.model_dump_json(),
                http_status=200,
                resource_type="session",
                resource_id=session.id,
            )
            return response
        except HTTPException:
            raise
        except BaseException as exc:
            if command_id is not None:
                service.store.mark_command_manual_reconcile(
                    command_id, error_code=type(exc).__name__
                )
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "command_outcome_unknown",
                    "command_execution_id": command_id,
                },
            ) from exc
        finally:
            service.release_session_run(session.id, lease)
