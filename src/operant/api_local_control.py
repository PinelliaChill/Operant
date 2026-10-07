"""Local-only GUI lifecycle and control commands over Core's leased jobs."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from threading import RLock
from typing import Any, Literal, cast

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from operant.application.local_control import (
    LocalControlManager,
    LocalControlSnapshotBinding,
    conversation_local_control_tools,
    local_control_tool_names,
    snapshot_control_bindings,
)
from operant.application.phase45_gateway import Phase45ActionGateway
from operant.application.service import ApplicationService
from operant.domain.actions import CommandExecution, CommandExecutionStatus
from operant.domain.models import RoleSnapshot, ToolPolicy
from operant.domain.security import Capability, SecurityAuditEvent
from operant.plugins.capability_registry import CapabilityPluginRegistry
from operant.plugins.local_extensions import list_operations, run_operation
from operant.protocol import canonical_action_hash
from operant.remote.local_worker import COMPUTER_PLUGIN
from operant.remote.operator import CapabilityLeaseBinding
from operant.remote.tool_extensions import local_capability_tool_extensions
from operant.tools.extensions import ToolExtension


class LocalControlKeyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=1, max_length=200)


class InstallLocalCapabilityBody(LocalControlKeyBody):
    plugin_id: Literal["operant.chrome.browser", "operant.macos.computer"]
    allowed_targets: tuple[str, ...] = Field(min_length=1, max_length=100)


class SetLocalCapabilityEnabledBody(LocalControlKeyBody):
    enabled: bool


class OpenLocalControlBody(LocalControlKeyBody):
    plugin_id: Literal["operant.chrome.browser", "operant.macos.computer"]
    computer_bundle_id: str | None = Field(default=None, max_length=253)


class ActLocalControlBody(LocalControlKeyBody):
    operation: str = Field(min_length=1, max_length=100)
    observation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    arguments: dict[str, Any] = Field(default_factory=dict)


class DriveLocalControlBody(LocalControlKeyBody):
    driver_name: str = Field(min_length=1, max_length=300)
    observation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    arguments: dict[str, Any] = Field(default_factory=dict)


class ReconcileLocalControlBody(LocalControlKeyBody):
    observed_outcome: Literal["applied", "not_applied"]
    evidence_note: str = Field(min_length=5, max_length=1000)


def install_local_control_routes(
    app: FastAPI,
    service: ApplicationService,
    gateway: Phase45ActionGateway,
    local_authorizer: Callable[[Request], bool],
) -> LocalControlManager:
    registry = CapabilityPluginRegistry(service.store.path.resolve().parent / "capability-plugins")
    manager = LocalControlManager(
        registry, app.state.remote_execution_controller, app.state.local_capability_artifacts
    )
    app.state.local_control_manager = manager
    app.router.add_event_handler("startup", manager.start)
    # Must close adapters before the Service/SQLite shutdown hook.
    app.router.on_shutdown.insert(0, manager.close)
    previous_factory = service.tool_extension_factory

    class SnapshotAwareToolFactory:
        def __call__(self, path: Path, policy: ToolPolicy) -> dict[str, ToolExtension]:
            extensions = previous_factory(path, policy) if previous_factory is not None else {}
            # Legacy explicit ext roles keep their existing process-wide binding.
            if getattr(app.state, "local_control_origin", None):
                extensions.update(
                    local_capability_tool_extensions(
                        path,
                        policy,
                        bindings=manager.bindings(),
                        core_origin=app.state.local_control_origin,
                    )
                )
            return extensions

        def for_snapshot(self, path: Path, snapshot: RoleSnapshot) -> dict[str, ToolExtension]:
            selected = snapshot_control_bindings(snapshot.config_sources)
            if not selected:
                return self(path, snapshot.tool_policy)
            origin = getattr(app.state, "local_control_origin", None)
            if not isinstance(origin, str) or not origin:
                raise PermissionError("local control listener is unavailable")
            bindings: dict[str, CapabilityLeaseBinding] = {
                item.kind: manager.verified_snapshot_binding(item.session_id, expected=item)[1]
                for item in selected
            }
            local_names = local_control_tool_names()
            ordinary_policy = snapshot.tool_policy.model_copy(
                update={
                    "allowed_tools": tuple(
                        name
                        for name in snapshot.tool_policy.allowed_tools
                        if name not in local_names
                    )
                }
            )
            ordinary = (
                previous_factory(path, ordinary_policy) if previous_factory is not None else {}
            )
            extensions = {name: item for name, item in ordinary.items() if name not in local_names}
            extensions.update(
                local_capability_tool_extensions(
                    path,
                    snapshot.tool_policy,
                    bindings=bindings,
                    core_origin=origin,
                )
            )
            required = {
                name for item in selected for name in conversation_local_control_tools(item.kind)
            }
            if not required.issubset(extensions):
                raise PermissionError("bound local control tools are unavailable")
            # A Run keeps WorkspaceTools alive across human takeover. Resolve
            # its exact session again before each invocation so a resumed
            # session uses the new fenced lease; never substitute a global one.
            for marker in selected:
                for name in local_names.intersection(extensions):
                    if not name.startswith(f"ext_{marker.kind}_"):
                        continue
                    extension = extensions[name]

                    async def execute_bound(
                        arguments: dict[str, Any],
                        *,
                        bound_marker: LocalControlSnapshotBinding = marker,
                        tool_name: str = name,
                    ) -> dict[str, Any]:
                        current = manager.verified_snapshot_binding(
                            bound_marker.session_id, expected=bound_marker
                        )[1]
                        refreshed = local_capability_tool_extensions(
                            path,
                            snapshot.tool_policy,
                            bindings={bound_marker.kind: current},
                            core_origin=origin,
                        ).get(tool_name)
                        if refreshed is None:
                            raise PermissionError("bound local control tool is unavailable")
                        return await refreshed.execute(arguments)

                    extensions[name] = replace(extension, execute=execute_bound)
            return extensions

    service.tool_extension_factory = SnapshotAwareToolFactory()

    def require_local(request: Request) -> None:
        if not local_authorizer(request):
            raise HTTPException(status_code=403, detail="local control requires a local client")
        # request.base_url uses caller-controlled Host; never use it to send
        # lease credentials. The ASGI server listener address is authoritative.
        server = request.scope.get("server")
        if server and server[0] in {"127.0.0.1", "::1"} and isinstance(server[1], int):
            host = f"[{server[0]}]" if server[0] == "::1" else server[0]
            app.state.local_control_origin = f"http://{host}:{server[1]}"
            manager.core_origin = app.state.local_control_origin

    def guard(
        operation: str,
        target: str,
        arguments: dict[str, Any],
        key: str,
        capabilities: tuple[Capability, ...] = (Capability.REMOTE_TARGET_EXEC,),
    ) -> None:
        action, result, _ = gateway.guard(
            tool="local_control",
            operation=operation,
            target_id=target,
            arguments=arguments,
            capabilities=capabilities,
            idempotency_key=key,
        )
        if result.decision.value == "ask":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "approval_required",
                    "message": "approve the exact local control action before continuing",
                    "approval_id": result.approval_id,
                    "action_hash": action.action_hash,
                    "policy_version": action.policy_version,
                },
            )
        if result.decision.value != "allow" or result.lease is None:
            raise HTTPException(status_code=403, detail=result.reason_code)
        gateway.consume(result.lease, action)

    async def call(function: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(function)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="local control resource missing or evidence expired"
            ) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=type(exc).__name__) from exc

    driver_proposals: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
    mutation_lock = RLock()

    def mutation(
        operation: str,
        target: str,
        arguments: dict[str, Any],
        key: str,
        perform: Callable[[], dict[str, Any]],
        capabilities: tuple[Capability, ...] = (Capability.REMOTE_TARGET_EXEC,),
    ) -> dict[str, Any]:
        with mutation_lock:
            fingerprint = canonical_action_hash(
                {"operation": operation, "target": target, "arguments": arguments}
            )
            with service.store._connect() as connection:
                row = connection.execute(
                    "SELECT id FROM command_executions "
                    "WHERE command_type='local-control.management' AND idempotency_key=?",
                    (key,),
                ).fetchone()
            if row is not None:
                prior = service.store.get_command_execution(row["id"])
                if prior.action_hash != fingerprint:
                    raise ValueError("local management idempotency key changed request")
                if prior.status is CommandExecutionStatus.COMPLETED and prior.response_json:
                    return cast(dict[str, Any], json.loads(prior.response_json))
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "command_outcome_unknown",
                        "message": "本机操作结果尚未确认。请先核对记录，暂时不要重试。",
                        "command_execution_id": prior.id,
                    },
                )
            guard(operation, target, arguments, key, capabilities)
            command, created = service.store.reserve_command_execution(
                CommandExecution(
                    command_type="local-control.management",
                    idempotency_key=key,
                    action_hash=fingerprint,
                )
            )
            if not created:
                raise HTTPException(status_code=409, detail="local management command in progress")
            try:
                result = perform()
                service.store.complete_command_execution(
                    command.id, response_json=json.dumps(result, sort_keys=True), http_status=200
                )
            except Exception as exc:
                service.store.mark_command_manual_reconcile(
                    command.id, error_code=type(exc).__name__
                )
                raise
            return result

    # Onboarding is installed earlier; resolve these trusted closures at request time.
    app.state.local_control_prepare_request = require_local
    app.state.local_control_mutation = mutation
    app.state.local_control_call = call

    @app.get("/v1/local-control/plugins", operation_id="listLocalCapabilityPlugins")
    async def list_plugins(request: Request) -> dict[str, Any]:
        require_local(request)
        return {"items": [r.model_dump(mode="json") for r in registry.list()]}

    @app.post("/v1/local-control/plugins", operation_id="installLocalCapabilityPlugin")
    async def install_plugin(request: Request, body: InstallLocalCapabilityBody) -> dict[str, Any]:
        require_local(request)
        return await call(
            lambda: mutation(
                "install",
                body.plugin_id,
                {"allowed_targets": body.allowed_targets},
                body.idempotency_key,
                lambda: registry.install(body.plugin_id, body.allowed_targets).model_dump(
                    mode="json"
                ),
            )
        )

    @app.post(
        "/v1/local-control/plugins/{plugin_id}/enabled",
        operation_id="setLocalCapabilityPluginEnabled",
    )
    async def enable_plugin(
        request: Request, plugin_id: str, body: SetLocalCapabilityEnabledBody
    ) -> dict[str, Any]:
        require_local(request)

        def perform() -> dict[str, Any]:
            result = registry.set_enabled(plugin_id, body.enabled)
            if not body.enabled:
                for session in tuple(manager.sessions.values()):
                    if session.plugin_id == plugin_id and session.state != "closed":
                        manager.transition(session.session_id, "closed")
            return result.model_dump(mode="json")

        return await call(
            lambda: mutation(
                "set_enabled", plugin_id, {"enabled": body.enabled}, body.idempotency_key, perform
            )
        )

    @app.delete(
        "/v1/local-control/plugins/{plugin_id}", operation_id="uninstallLocalCapabilityPlugin"
    )
    async def uninstall_plugin(
        request: Request, plugin_id: str, body: LocalControlKeyBody
    ) -> dict[str, Any]:
        require_local(request)

        def perform() -> dict[str, Any]:
            registry.uninstall(plugin_id)
            return {"plugin_id": plugin_id, "state": "uninstalled"}

        return await call(
            lambda: mutation("uninstall", plugin_id, {}, body.idempotency_key, perform)
        )

    @app.get("/v1/local-control/sessions", operation_id="listLocalControlSessions")
    async def list_sessions(request: Request) -> dict[str, Any]:
        require_local(request)
        return {"items": [s.projection() for s in manager.sessions.values()]}

    @app.get("/v1/local-control/unknown-jobs", operation_id="listUnknownLocalControlJobs")
    async def list_unknown_jobs(request: Request) -> dict[str, Any]:
        require_local(request)
        return {"jobs": manager.pending_unknowns()}

    @app.post(
        "/v1/local-control/unknown-jobs/{job_id}/reconcile",
        operation_id="reconcileUnknownLocalControlJob",
    )
    async def reconcile_unknown_job(
        request: Request, job_id: str, body: ReconcileLocalControlBody
    ) -> dict[str, Any]:
        require_local(request)
        arguments = {
            "observed_outcome": body.observed_outcome,
            "evidence_sha256": hashlib.sha256(body.evidence_note.encode()).hexdigest(),
        }

        def perform() -> dict[str, Any]:
            if not any(j["job_id"] == job_id for j in manager.pending_unknowns()):
                raise ValueError("job does not require local manual reconciliation")
            job = manager.repository.get_job(job_id)
            gateway.repository.append_security_audit(
                SecurityAuditEvent(
                    action_hash=job.action_hash,
                    principal="user:local-control",
                    event_type="local_control.unknown_checked",
                    detail={"job_id": job_id, **arguments},
                )
            )
            return {"job_id": job_id, "state": "operator_checked", "status": job.status.value}

        return await call(
            lambda: mutation("reconcile_unknown", job_id, arguments, body.idempotency_key, perform)
        )

    @app.post("/v1/local-control/sessions", operation_id="openLocalControlSession")
    async def open_session(request: Request, body: OpenLocalControlBody) -> dict[str, Any]:
        require_local(request)

        def perform() -> dict[str, Any]:
            record = registry.get(body.plugin_id, require_enabled=True)
            return mutation(
                "open",
                body.plugin_id,
                {
                    "source_digest": record.source_digest,
                    "allowed_targets": record.allowed_targets,
                    "computer_bundle_id": body.computer_bundle_id,
                },
                body.idempotency_key,
                lambda: manager.open(
                    body.plugin_id, body.idempotency_key, body.computer_bundle_id
                ).projection(),
                (Capability.REMOTE_TARGET_EXEC, Capability.COMPUTER_INPUT)
                if body.plugin_id == COMPUTER_PLUGIN.plugin_id
                else (Capability.REMOTE_TARGET_EXEC,),
            )

        return await call(perform)

    @app.post(
        "/v1/local-control/sessions/{session_id}/observe", operation_id="observeLocalControlSession"
    )
    async def observe(
        request: Request, session_id: str, body: LocalControlKeyBody
    ) -> dict[str, Any]:
        require_local(request)
        return await call(lambda: manager.observe(session_id, body.idempotency_key))

    @app.post("/v1/local-control/sessions/{session_id}/act", operation_id="actLocalControlSession")
    async def act(request: Request, session_id: str, body: ActLocalControlBody) -> dict[str, Any]:
        require_local(request)
        return await call(
            lambda: manager.act(
                session_id,
                body.operation,
                body.observation_hash,
                body.arguments,
                body.idempotency_key,
            )
        )

    @app.post(
        "/v1/local-control/sessions/{session_id}/drive", operation_id="driveLocalControlSession"
    )
    async def drive(
        request: Request, session_id: str, body: DriveLocalControlBody
    ) -> dict[str, Any]:
        require_local(request)

        def perform() -> dict[str, Any]:
            with mutation_lock:
                manager.get(session_id, active=True)
                definitions = list_operations(service.extension_registry, "capability_driver")
                definition = next((d for d in definitions if d["name"] == body.driver_name), None)
                if definition is None:
                    raise KeyError(body.driver_name)
                fingerprint = canonical_action_hash(
                    {"driver": definition, **body.model_dump(mode="json")}
                )
                cache_key = session_id, body.idempotency_key
                prior = driver_proposals.get(cache_key)
                if prior and prior[0] != fingerprint:
                    raise ValueError("local driver idempotency key changed request")
                if prior:
                    proposal = prior[1]
                else:
                    if len(driver_proposals) >= 256:
                        raise ValueError("local driver operation limit reached; restart Core")
                    guard(
                        "driver_proposal",
                        session_id,
                        {
                            "driver": definition,
                            "arguments_sha256": canonical_action_hash(body.arguments),
                        },
                        body.idempotency_key + ":proposal",
                        (Capability.PROCESS_EXEC_NO_NETWORK,),
                    )
                    proposal = run_operation(
                        service.extension_registry,
                        body.driver_name,
                        "capability_driver",
                        body.arguments,
                    )
                    if (
                        set(proposal) != {"operation", "arguments"}
                        or not isinstance(proposal["operation"], str)
                        or not isinstance(proposal["arguments"], dict)
                    ):
                        raise ValueError("extension driver must produce a typed action")
                    driver_proposals[cache_key] = fingerprint, proposal
                if proposal["operation"] == "observe":
                    if proposal["arguments"]:
                        raise ValueError("observe driver proposal cannot include arguments")
                    return manager.observe(session_id, body.idempotency_key)
                return manager.act(
                    session_id,
                    proposal["operation"],
                    body.observation_hash,
                    proposal["arguments"],
                    body.idempotency_key,
                )

        return await call(perform)

    async def transition(
        request: Request, session_id: str, body: LocalControlKeyBody, state: str
    ) -> dict[str, Any]:
        require_local(request)
        return await call(
            lambda: mutation(
                state,
                session_id,
                {},
                body.idempotency_key,
                lambda: manager.transition(session_id, state).projection(),
            )
        )

    @app.post(
        "/v1/local-control/sessions/{session_id}/takeover",
        operation_id="takeoverLocalControlSession",
    )
    async def takeover(
        request: Request, session_id: str, body: LocalControlKeyBody
    ) -> dict[str, Any]:
        return await transition(request, session_id, body, "human_control")

    @app.post(
        "/v1/local-control/sessions/{session_id}/resume", operation_id="resumeLocalControlSession"
    )
    async def resume(
        request: Request, session_id: str, body: LocalControlKeyBody
    ) -> dict[str, Any]:
        return await transition(request, session_id, body, "active")

    @app.post(
        "/v1/local-control/sessions/{session_id}/close", operation_id="closeLocalControlSession"
    )
    async def close(request: Request, session_id: str, body: LocalControlKeyBody) -> dict[str, Any]:
        return await transition(request, session_id, body, "closed")

    @app.get("/v1/local-control/artifacts/{job_id}", operation_id="getLocalControlArtifact")
    async def artifact(request: Request, job_id: str, response: Response) -> dict[str, Any]:
        require_local(request)
        response.headers["Cache-Control"] = "no-store"

        def read() -> dict[str, Any]:
            job = manager.repository.get_job(job_id)
            result = manager.repository.get_result(job_id)
            item = manager.artifacts.get(job_id)
            if (
                result is None
                or result.status.value != "succeeded"
                or result.artifact_sha256 != item.sha256
                or result.artifact_ref != f"local-capability:{job_id}"
            ):
                raise PermissionError("capability artifact is not acknowledged by Core")
            read_capability = {
                "browser.screenshot": Capability.BROWSER_SCREENSHOT,
                "computer.screenshot": Capability.COMPUTER_SCREENSHOT,
                "computer.clipboard.read": Capability.COMPUTER_CLIPBOARD_READ,
            }.get(job.capability.value)
            if read_capability is None:
                raise PermissionError("unsupported capability evidence")
            guard(
                "read_artifact",
                job.target_id,
                {"job_id": job_id, "sha256": item.sha256},
                f"artifact-read:{job_id}",
                (read_capability,),
            )
            return {
                "media_type": item.media_type,
                "base64": base64.b64encode(item.content).decode(),
                "sha256": item.sha256,
            }

        return await call(read)

    return manager
