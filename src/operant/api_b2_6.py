"""B2-6 experience and sharing boundary. All writes pass the Action Gateway."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from datetime import datetime, timezone
from typing import Any, cast
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Query, Response

from operant.api_b2_3 import _bounded_projection
from operant.application.service import ApplicationService
from operant.contracts.b2_6 import (
    B26Command,
    B26Dataset,
    B26Event,
    B26EventPage,
    B26Result,
    B26State,
)
from operant.contracts.b2_6_remote import RemoteMemoryCommand
from operant.contracts.b2_6_sharing import SharingCommand
from operant.contracts.b2_6_skills import SkillCommand
from operant.domain.security import Capability
from operant.memory_plugins.manager import MemoryManager
from operant.package_resources import protocol_schema_path
from operant.persistence.sqlite import NotFoundError
from operant.plugins.protocol import PluginError


def install_b2_6_routes(app: FastAPI, service: ApplicationService) -> None:
    def manager() -> MemoryManager:
        if service.memory_manager is None:
            if service.memory_manager_factory is None:
                raise HTTPException(503, {"code": "unavailable", "message": "记忆服务不可用"})
            return service.memory_manager_factory()
        return service.memory_manager

    class TargetRuntime:
        def validate_memory_target(self, target_id: str, **kwargs: Any) -> None:
            from operant.memory_plugins.remote_memory import (
                RemoteMemoryError,
                RemoteMemoryTargetRuntime,
            )

            connector = app.state.b26_remote_connectors.get(target_id)
            if connector is None:
                raise RemoteMemoryError(
                    "target_runtime_unavailable", "Target connector is unavailable"
                )
            RemoteMemoryTargetRuntime(
                app.state.remote_execution_controller, connector
            ).validate_memory_target(target_id, **kwargs)

    def services() -> tuple[Any, Any, Any]:
        from operant.memory_plugins.experience_skills import ExperienceSkillService
        from operant.memory_plugins.remote_memory import RemoteMemoryService
        from operant.memory_plugins.sharing import SharingService

        m = manager()
        return (
            ExperienceSkillService(m),
            SharingService(
                m,
                writer_adapter=getattr(
                    getattr(app.state, "multiwriter_runtime", None), "merge_adapter", None
                ),
                principal_resolver=lambda project_id: (
                    m._installation(m._project(project_id), enabled=False).owner.principal_id
                ),
            ),
            RemoteMemoryService(m, TargetRuntime()),
        )

    def projection(project_id: str) -> B26State:
        m = manager()
        project = m._project(project_id)
        installation_id = project.get("installation_id")
        dataset_id = (
            m.registry.get_installation(installation_id).dataset_id if installation_id else None
        )
        skills, sharing, remote = services()
        with m.store._connect() as c:
            unresolved = [
                str(r[0])
                for r in c.execute(
                    "SELECT command_id FROM b26_commands WHERE project_id=? "
                    "AND state IN ('pending','outcome_unknown') ORDER BY command_id",
                    (project_id,),
                )
            ]
        return cast(
            B26State,
            _bounded_projection(
                B26State(
                    project_id=project_id,
                    skills=skills.state(project_id),
                    sharing=sharing.state(project_id),
                    remote=remote.state(project_id),
                    datasets=[
                        B26Dataset(
                            dataset_id=d.dataset_id,
                            installation_id=d.installation_id,
                            principal_id=d.owner.principal_id,
                            revision=d.revision,
                            state=d.state,
                        )
                        for d in m.registry.state.datasets
                        if d.dataset_id == dataset_id
                    ],
                    unresolved_command_ids=unresolved,
                )
            ),
        )

    def translate(exc: Exception) -> HTTPException:
        if isinstance(exc, HTTPException):
            return exc
        if isinstance(exc, PermissionError):
            return HTTPException(
                403, {"code": "permission_denied", "message": "权限、来源或授权状态不允许此操作"}
            )
        if isinstance(exc, (LookupError, NotFoundError)):
            return HTTPException(404, {"code": "not_found", "message": "请求对象不存在"})
        code = exc.code if isinstance(exc, PluginError) else "revision_conflict"
        return HTTPException(
            409, {"code": code, "message": "对象、验证或授权状态已变化，请刷新并核对后操作"}
        )

    def recover_pending() -> None:
        with service.store._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            for row in c.execute("SELECT * FROM b26_commands WHERE state='pending'").fetchall():
                c.execute(
                    "UPDATE b26_commands SET state='outcome_unknown' WHERE command_id=?",
                    (row["command_id"],),
                )
                c.execute(
                    "INSERT INTO b26_events(project_id,action,affected_ids,occurred_at) "
                    "VALUES(?,?,?,?)",
                    (
                        row["project_id"],
                        "outcome_unknown",
                        json.dumps([row["command_id"]]),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )

    app.router.on_startup.insert(0, recover_pending)
    app.state.b26_projection = projection

    @app.get("/v1/protocol/b2-6", operation_id="negotiateB26")
    def negotiate() -> dict[str, Any]:
        path = protocol_schema_path("operant-b2-6.openapi.sha256")
        if not path.is_file():
            raise HTTPException(
                503, {"code": "schema_unavailable", "message": "经验与共享契约不可用"}
            )
        return {
            "protocol_version": "b2-6.v1",
            "min_client_version": "b2-6.v1",
            "schema_digest": path.read_text().split()[0],
            "capabilities": [
                "experience_skills",
                "writer_promotion",
                "memory_sharing",
                "remote_memory",
            ],
        }

    @app.get(
        "/v1/b2-6/projects/{project_id}/experience",
        response_model=B26State,
        operation_id="getB26Experience",
    )
    def state(project_id: str) -> B26State:
        try:
            return projection(project_id)
        except (ValueError, RuntimeError, LookupError, PermissionError) as exc:
            raise translate(exc) from exc

    @app.get(
        "/v1/b2-6/projects/{project_id}/events",
        response_model=B26EventPage,
        operation_id="getB26Events",
    )
    def events(
        project_id: str,
        after_cursor: int = Query(default=0, ge=0, le=2**53 - 1),
        limit: int = Query(default=100, ge=1, le=100),
    ) -> B26EventPage:
        try:
            m = manager()
            m._project(project_id)
            with m.store._connect() as c:
                rows = c.execute(
                    "SELECT * FROM b26_events WHERE project_id=? AND sequence>? "
                    "ORDER BY sequence LIMIT ?",
                    (project_id, after_cursor, limit + 1),
                ).fetchall()
            return B26EventPage(
                events=[
                    B26Event(
                        cursor=r["sequence"],
                        project_id=project_id,
                        action=r["action"],
                        affected_ids=json.loads(r["affected_ids"]),
                        occurred_at=r["occurred_at"],
                    )
                    for r in rows[:limit]
                ],
                next_cursor=rows[limit - 1]["sequence"] if len(rows) > limit else None,
            )
        except (ValueError, RuntimeError, LookupError, PermissionError) as exc:
            raise translate(exc) from exc

    @app.post("/v1/b2-6/commands", response_model=B26Result, operation_id="executeB26Command")
    async def execute(
        body: B26Command,
        response: Response,
        idempotency_key: str | None = Header(default=None, min_length=1, max_length=300),
    ) -> B26Result:
        command = body.root
        key = idempotency_key or uuid4().hex
        response.headers["Idempotency-Key"] = key
        request_digest = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
        gateway = app.state.phase45_action_gateway
        action, decision, _ = gateway.guard(
            tool="memory_experience",
            operation=command.action,
            target_id=command.project_id,
            arguments={"request_digest": request_digest},
            capabilities=(Capability.WORKSPACE_WRITE,),
            idempotency_key=key,
        )
        if decision.decision.value != "allow" or decision.lease is None:
            raise HTTPException(403, {"code": "policy_denied", "message": "Policy 阻止此操作"})
        gateway.consume(decision.lease, action)
        m = manager()
        async with m._lock:
            m._project(command.project_id)
            with m.store._connect() as c:
                old = c.execute("SELECT * FROM b26_commands WHERE command_id=?", (key,)).fetchone()
                if old:
                    if (
                        old["request_digest"] != request_digest
                        or old["project_id"] != command.project_id
                    ):
                        raise HTTPException(
                            409, {"code": "revision_conflict", "message": "幂等键对应另一请求"}
                        )
                    if old["state"] != "completed":
                        raise HTTPException(
                            409,
                            {
                                "code": "manual_reconcile_required",
                                "message": "上次结果待核对，不能自动重放",
                            },
                        )
                    response.headers["Idempotent-Replayed"] = "true"
                    return B26Result(
                        **json.loads(old["result"]), state=projection(command.project_id)
                    )
                c.execute(
                    "INSERT INTO b26_commands VALUES(?,?,?,?,?)",
                    (key, command.project_id, request_digest, "pending", None),
                )
            try:
                skills, sharing, remote = services()
                handler = (
                    skills
                    if isinstance(command, SkillCommand)
                    else sharing
                    if isinstance(command, SharingCommand)
                    else remote
                )
                result = handler.execute(command)
                if inspect.isawaitable(result):
                    result = await result
                affected = (
                    list(result)
                    if isinstance(result, (list, tuple))
                    else list(getattr(result, "affected_ids", ()))
                )
                result_status = getattr(result, "status", "completed")
                result_message = getattr(result, "message", "操作已由本地 Core 记录")
                if (
                    isinstance(command, RemoteMemoryCommand)
                    and command.action == "remote_pack_create"
                    and result.pack is not None
                ):
                    from operant.domain.remote_execution import (
                        RemoteActionIdempotency,
                        RemoteCapability,
                    )

                    connector = app.state.b26_remote_connectors[command.target_id]
                    job = app.state.remote_execution_controller.create_job(
                        target_id=command.target_id,
                        lease_id=connector.lease_id,
                        lease_token=connector.lease_token,
                        lease_fencing=connector.lease_fencing,
                        capability=RemoteCapability.TARGET_EXEC,
                        operation="memory.consume",
                        arguments=remote.target_payload(result.pack),
                        idempotency_key=key + ":target",
                        idempotency=RemoteActionIdempotency.IDEMPOTENT,
                        now=datetime.now(timezone.utc),
                    )
                    affected.append(job.job_id)
                    result_status = "accepted"
                    result_message = "最小记忆包已进入 Target 队列；尚未收到 Target 完成回执"
                payload: dict[str, Any] = {
                    "status": result_status,
                    "message": result_message,
                    "affected_ids": affected,
                }
                with m.store._connect() as c:
                    c.execute("BEGIN IMMEDIATE")
                    c.execute(
                        "UPDATE b26_commands SET state='completed',result=? WHERE command_id=?",
                        (json.dumps(payload), key),
                    )
                    c.execute(
                        "INSERT INTO b26_events(project_id,action,affected_ids,occurred_at) "
                        "VALUES(?,?,?,?)",
                        (
                            command.project_id,
                            command.action,
                            json.dumps(affected),
                            datetime.now(timezone.utc).isoformat(),
                        ),
                    )
                return B26Result(**payload, state=projection(command.project_id))
            except (ValueError, RuntimeError, LookupError, PermissionError) as exc:
                # Preserve a pending receipt: a multi-store write may have partly completed.
                raise translate(exc) from exc

    app.state.b26_execute = execute

    def connect_remote(remote_service: Any) -> None:
        from operant.memory_plugins.remote_query import install_remote_memory_query

        install_remote_memory_query(app, remote_service, projection)
        previous = remote_service.executor
        loop_holder: list[asyncio.AbstractEventLoop] = []

        async def capture_loop() -> None:
            loop_holder[:] = [asyncio.get_running_loop()]

        app.router.on_startup.append(capture_loop)

        def remote_executor(payload: Any, action: Any) -> str | None:
            if payload.tool != "memory":
                return cast(str | None, previous(payload, action))
            if payload.operation == "query":
                projection(payload.target_id)
                return f"b26-project:{payload.target_id}"
            if payload.operation != "command" or not loop_holder:
                raise ValueError("memory command executor is unavailable")
            command = B26Command.model_validate(payload.arguments)
            if command.root.project_id != payload.target_id:
                raise PermissionError("remote command project identity mismatch")
            try:
                current_loop = asyncio.get_running_loop()
            except RuntimeError:
                current_loop = None
            if current_loop is loop_holder[0]:
                raise RuntimeError("remote memory commands require the Remote Control worker")
            future = asyncio.run_coroutine_threadsafe(
                execute(command, Response(), idempotency_key=action.idempotency_key), loop_holder[0]
            )
            result = future.result(timeout=60)
            # Opaque references only; Relay does not receive plaintext results.
            return f"b26-command:{action.idempotency_key}:{result.status}"

        remote_service.executor = remote_executor

    app.state.b26_connect_remote = connect_remote
