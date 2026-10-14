"""Local-only management and explicit conversation invocation of extensions."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from operant.api_workbench_context import thread_session
from operant.application.phase45_gateway import Phase45ActionGateway
from operant.application.service import ApplicationService
from operant.domain.actions import CommandExecution, CommandExecutionStatus
from operant.domain.models import Event
from operant.domain.security import Capability, PolicyDecision
from operant.domain.threads import Item, SystemEventPayload, Turn
from operant.persistence.sqlite import IdempotencyConflictError, NotFoundError
from operant.plugins.external_tool import ExtensionCategory, ExternalToolRegistry
from operant.plugins.local_extensions import (
    list_operations,
    public_record,
    run_operation,
    validate_operation_arguments,
)
from operant.protocol import canonical_action_hash, redact_public_data


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InspectExtensionBody(_Body):
    source: str = Field(min_length=1, max_length=4096)


class InstallExtensionBody(InspectExtensionBody):
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=300)


class SetExtensionEnabledBody(_Body):
    idempotency_key: str = Field(min_length=1, max_length=300)
    granted_categories: tuple[ExtensionCategory, ...] = ()


class UninstallExtensionBody(_Body):
    idempotency_key: str = Field(min_length=1, max_length=300)


class InvokeExtensionCommandBody(_Body):
    command: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=300)


def install_extension_routes(
    app: FastAPI,
    service: ApplicationService,
    *,
    action_gateway: Phase45ActionGateway,
    local_authorizer: Callable[[Request], bool],
) -> None:
    registry = service.extension_registry

    def local(request: Request) -> None:
        if not local_authorizer(request):
            raise HTTPException(status_code=403, detail="local extension access denied")

    def guard(
        request: Request,
        *,
        operation: str,
        target: str,
        arguments: dict[str, Any],
        idempotency_key: str,
        capability: Capability = Capability.PROCESS_EXEC_NO_NETWORK,
    ) -> None:
        local(request)
        action, result, _ = action_gateway.guard(
            tool="local_extension",
            operation=operation,
            target_id=target,
            arguments=arguments,
            capabilities=(capability,),
            idempotency_key=idempotency_key,
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

    def error(exc: Exception) -> HTTPException:
        if isinstance(exc, (KeyError, LookupError, NotFoundError)):
            return HTTPException(status_code=404, detail="extension operation not found")
        if isinstance(exc, PermissionError):
            return HTTPException(status_code=403, detail=str(exc))
        if isinstance(exc, ValueError):
            return HTTPException(status_code=400, detail=str(exc))
        return HTTPException(status_code=500, detail=type(exc).__name__)

    def _replay(scope: str, key: str, action_hash: str) -> dict[str, Any] | None:
        with service.store._connect() as connection:
            row = connection.execute(
                "SELECT id FROM command_executions WHERE command_type=? AND idempotency_key=?",
                (scope, key),
            ).fetchone()
        if row is None:
            return None
        command = service.store.get_command_execution(str(row["id"]))
        if command.action_hash != action_hash:
            raise HTTPException(status_code=409, detail={"code": "idempotency_key_conflict"})
        if command.status is CommandExecutionStatus.COMPLETED and command.response_json:
            return cast(dict[str, Any], json.loads(command.response_json))
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

    def _claim(scope: str, key: str, action_hash: str) -> tuple[str | None, dict[str, Any] | None]:
        try:
            command, created = service.store.reserve_command_execution(
                CommandExecution(
                    command_type=scope,
                    idempotency_key=key,
                    action_hash=action_hash,
                )
            )
        except IdempotencyConflictError as exc:
            raise HTTPException(
                status_code=409, detail={"code": "idempotency_key_conflict"}
            ) from exc
        if not created:
            return None, _replay(scope, key, action_hash)
        return command.id, None

    def _complete(
        command_id: str, payload: dict[str, Any], *, resource_id: str | None = None
    ) -> None:
        service.store.complete_command_execution(
            command_id,
            response_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            http_status=200,
            resource_type="turn" if resource_id is not None else None,
            resource_id=resource_id,
        )

    def _unknown(command_id: str, exc: Exception) -> HTTPException:
        service.store.mark_command_manual_reconcile(command_id, error_code=type(exc).__name__)
        return HTTPException(
            status_code=409,
            detail={
                "code": "command_outcome_unknown",
                "command_execution_id": command_id,
                "error_type": type(exc).__name__,
                "recovery": "manual_reconcile",
            },
        )

    @app.get("/v1/extensions", operation_id="listExtensions")
    def list_extensions(request: Request) -> dict[str, Any]:
        local(request)
        return {"items": [public_record(record) for record in registry.list()]}

    @app.post("/v1/extensions/inspect", operation_id="inspectExtension")
    def inspect_extension(body: InspectExtensionBody, request: Request) -> dict[str, Any]:
        local(request)
        try:
            manifest, digest = ExternalToolRegistry.inspect(Path(body.source))
            return {"manifest": manifest.model_dump(mode="json"), "package_digest": digest}
        except (ValueError, OSError) as exc:
            raise error(exc) from exc

    @app.post("/v1/extensions/install", operation_id="installExtension")
    def install_extension(body: InstallExtensionBody, request: Request) -> dict[str, Any]:
        local(request)
        scope = "extension.install"
        action_hash = canonical_action_hash(
            {"source": body.source, "expected_sha256": body.expected_sha256}
        )
        replay = _replay(scope, body.idempotency_key, action_hash)
        if replay is not None:
            return replay
        try:
            manifest, digest = ExternalToolRegistry.inspect(Path(body.source))
            if manifest.host_api_version != "operant-local-extension.v1":
                raise ValueError("use the legacy Tool installer for legacy packages")
            if digest != body.expected_sha256:
                raise ValueError("extension package digest does not match expected_sha256")
        except (ValueError, OSError, PermissionError) as exc:
            raise error(exc) from exc
        guard(
            request,
            operation="install",
            target="local-extension-registry",
            arguments={"expected_sha256": body.expected_sha256},
            idempotency_key=body.idempotency_key,
        )
        command_id, replay = _claim(scope, body.idempotency_key, action_hash)
        if replay is not None:
            return replay
        assert command_id is not None
        try:
            record = registry.install(Path(body.source), expected_digest=body.expected_sha256)
            payload = public_record(record)
            _complete(command_id, payload)
            return payload
        except Exception as exc:
            raise _unknown(command_id, exc) from exc

    @app.post("/v1/extensions/{plugin_id}/enable", operation_id="enableExtension")
    def enable_extension(
        plugin_id: str, body: SetExtensionEnabledBody, request: Request
    ) -> dict[str, Any]:
        local(request)
        scope = "extension.enable"
        action_hash = canonical_action_hash(
            {"plugin_id": plugin_id, "granted_categories": list(body.granted_categories)}
        )
        replay = _replay(scope, body.idempotency_key, action_hash)
        if replay is not None:
            return replay
        guard(
            request,
            operation="enable",
            target=plugin_id,
            arguments={"granted_categories": list(body.granted_categories)},
            idempotency_key=body.idempotency_key,
        )
        command_id, replay = _claim(scope, body.idempotency_key, action_hash)
        if replay is not None:
            return replay
        assert command_id is not None
        try:
            record = registry.set_enabled(
                plugin_id, True, granted_categories=body.granted_categories
            )
            payload = public_record(record)
            _complete(command_id, payload)
            return payload
        except Exception as exc:
            raise _unknown(command_id, exc) from exc

    @app.post("/v1/extensions/{plugin_id}/disable", operation_id="disableExtension")
    def disable_extension(
        plugin_id: str, body: SetExtensionEnabledBody, request: Request
    ) -> dict[str, Any]:
        local(request)
        scope = "extension.disable"
        action_hash = canonical_action_hash({"plugin_id": plugin_id})
        replay = _replay(scope, body.idempotency_key, action_hash)
        if replay is not None:
            return replay
        guard(
            request,
            operation="disable",
            target=plugin_id,
            arguments={},
            idempotency_key=body.idempotency_key,
        )
        command_id, replay = _claim(scope, body.idempotency_key, action_hash)
        if replay is not None:
            return replay
        assert command_id is not None
        try:
            payload = public_record(registry.set_enabled(plugin_id, False))
            _complete(command_id, payload)
            return payload
        except Exception as exc:
            raise _unknown(command_id, exc) from exc

    @app.delete("/v1/extensions/{plugin_id}", operation_id="uninstallExtension")
    def uninstall_extension(
        plugin_id: str, body: UninstallExtensionBody, request: Request
    ) -> dict[str, str]:
        local(request)
        scope = "extension.uninstall"
        action_hash = canonical_action_hash({"plugin_id": plugin_id})
        replay = _replay(scope, body.idempotency_key, action_hash)
        if replay is not None:
            return replay
        guard(
            request,
            operation="uninstall",
            target=plugin_id,
            arguments={},
            idempotency_key=body.idempotency_key,
            capability=Capability.WORKSPACE_DELETE,
        )
        command_id, replay = _claim(scope, body.idempotency_key, action_hash)
        if replay is not None:
            return replay
        assert command_id is not None
        try:
            registry.uninstall(plugin_id)
            payload = {"plugin_id": plugin_id, "status": "uninstalled"}
            _complete(command_id, payload)
            return payload
        except Exception as exc:
            raise _unknown(command_id, exc) from exc

    @app.get("/v1/workbench/extensions/commands", operation_id="listExtensionCommands")
    def commands(request: Request) -> dict[str, Any]:
        local(request)
        return {
            "registry_version": "operant-local-extension.v1",
            "commands": list_operations(registry, "command"),
        }

    @app.get("/v1/extensions/drivers", operation_id="listExtensionDrivers")
    def drivers(request: Request) -> dict[str, Any]:
        local(request)
        return {"drivers": list_operations(registry, "capability_driver")}

    @app.post(
        "/v1/workbench/threads/{thread_id}/extension-commands",
        operation_id="executeExtensionCommand",
    )
    async def invoke_command(
        thread_id: str, body: InvokeExtensionCommandBody, request: Request
    ) -> dict[str, Any]:
        local(request)
        try:
            session = thread_session(service, thread_id)
        except Exception as exc:
            raise error(exc) from exc
        arguments_hash = canonical_action_hash(json.loads(json.dumps(body.arguments)))
        scope = "extension.command"
        action_hash = canonical_action_hash(
            {
                "thread_id": thread_id,
                "session_id": session.id,
                "command": body.command,
                "arguments_sha256": arguments_hash,
            }
        )
        replay = _replay(scope, body.idempotency_key, action_hash)
        if replay is not None:
            return replay
        try:
            validate_operation_arguments(registry, body.command, "command", body.arguments)
        except (LookupError, ValueError, PermissionError) as exc:
            raise error(exc) from exc
        guard(
            request,
            operation="command.invoke",
            target=body.command,
            arguments={
                "thread_id": thread_id,
                "session_id": session.id,
                "arguments_sha256": arguments_hash,
            },
            idempotency_key=body.idempotency_key,
        )
        command_id, replay = _claim(scope, body.idempotency_key, action_hash)
        if replay is not None:
            return replay
        assert command_id is not None
        try:
            result = await asyncio.to_thread(
                run_operation, registry, body.command, "command", body.arguments
            )
            safe_result = redact_public_data(result, max_chars=4_000)
            turn = service.create_turn(Turn(thread_id=thread_id))
            summary = (
                f"/{body.command} 已完成 · "
                + json.dumps(safe_result, ensure_ascii=False, sort_keys=True)[:750]
            )
            history_item = Item(
                thread_id=thread_id,
                turn_id=turn.id,
                payload=SystemEventPayload(
                    event_type="extension.command.completed",
                    summary=summary,
                    source_ref=command_id,
                ),
            )
            service.store.append_event(
                Event(
                    session_id=session.id,
                    event_type="extension.command.completed",
                    payload={
                        "command": body.command,
                        "command_execution_id": command_id,
                        "result_sha256": canonical_action_hash(safe_result),
                    },
                ),
                history_item=history_item,
            )
            payload = {
                "command": body.command,
                "status": "completed",
                "result": safe_result,
                "resource_id": turn.id,
            }
            _complete(command_id, payload, resource_id=turn.id)
            return payload
        except Exception as exc:
            raise _unknown(command_id, exc) from exc
