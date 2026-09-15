"""B2-4 typed collaboration directory and context control through Action Gateway."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Query, Response

from operant.api_b2_3 import _bounded_projection
from operant.application.service import ApplicationService
from operant.contracts.b2_4 import (
    B24Command,
    B24Result,
    CollaborationDirectory,
    CollaborationGraphRun,
    CollaborationRole,
    ContextInspection,
    InspectedContextRevision,
)
from operant.domain.graph import (
    GraphWorkflowRun,
    IdempotencyClass,
    NodeKind,
    NodeSpec,
    WorkflowDefinition,
)
from operant.domain.security import Capability
from operant.domain.team import (
    TeamDefinition,
    TeamMember,
)
from operant.memory_plugins.recall import load_manifest, publication_cutoff, save_manifest
from operant.persistence.sqlite import NotFoundError


def install_b2_4_routes(app: FastAPI, service: ApplicationService) -> None:
    @app.get("/v1/protocol/b2-4", operation_id="negotiateB24")
    def negotiate() -> dict[str, Any]:
        path = (
            Path(__file__).resolve().parents[2] / "sdk/protocol/schema/operant-b2-4.openapi.sha256"
        )
        if not path.is_file():
            raise HTTPException(503, "B2-4 schema unavailable")
        return {
            "protocol_version": "b2-4.v1",
            "schema_digest": path.read_text().split()[0],
            "min_client_version": "b2-4.v1",
            "capabilities": ["memory_recall", "context_control", "collaboration"],
        }

    from operant.application.graph_execution import (
        READ_ONLY_AGENT_TOOLS,
        BoundedGraphExecutor,
        GraphExecutionError,
    )

    executor = BoundedGraphExecutor(
        service, app.state.graph_repository, app.state.graph_runtime, app.state.team_repository
    )
    app.state.b24_graph_executor = executor
    tasks: dict[str, asyncio.Task[Any]] = {}

    async def drive(run_id: str) -> None:
        try:
            await executor.run(run_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            run = app.state.graph_repository.get_run(run_id)
            if run.status.value not in {
                "completed",
                "failed",
                "cancelled",
                "manual_reconcile_required",
            }:
                app.state.graph_runtime.fail_run(run_id)
        finally:
            tasks.pop(run_id, None)

    def schedule(run_id: str) -> None:
        if run_id not in tasks:
            tasks[run_id] = asyncio.create_task(drive(run_id))

    def freeze_memory(run_id: str) -> None:
        run = app.state.graph_repository.get_run(run_id)
        manager = service.memory_manager
        if manager is None and service.memory_manager_factory:
            manager = service.memory_manager_factory()
        if manager is None or not run.workspace_or_target:
            return
        workspace = str(Path(run.workspace_or_target).resolve())
        project = next(
            (
                p
                for p in manager._state["projects"]
                if manager.store.get_workspace_initialization_by_id(p["workspace_id"]).workspace_ref
                == workspace
            ),
            None,
        )
        if project is None or load_manifest(manager, run_id) is not None:
            return
        try:
            installation = manager._installation(project)
        except Exception:
            return
        save_manifest(
            manager,
            run_id,
            run_id,
            installation.dataset_id,
            {
                "cutoff": publication_cutoff(manager),
                "revision": 1,
                "excluded": [],
                "used_refs": [],
                "binding_id": installation.binding_id,
                "binding_epoch": installation.binding_epoch,
            },
        )

    app.state.b24_freeze_graph_memory = freeze_memory
    app.state.b24_schedule_graph = schedule

    async def shutdown() -> None:
        pending = list(tasks.items())
        for run_id, task in pending:
            executor.cancel(run_id)
            task.cancel()
        if pending:
            await asyncio.gather(*(task for _, task in pending), return_exceptions=True)

    app.router.on_shutdown.insert(0, shutdown)
    lock = asyncio.Lock()

    def directory(
        workspace: str | None = None, cursor: str | None = None
    ) -> CollaborationDirectory:
        with service.store._connect() as c:
            workflows = [
                WorkflowDefinition.model_validate_json(r["body"])
                for r in c.execute(
                    "SELECT body FROM workflow_definitions ORDER BY workflow_id,version"
                )
            ]
            teams = [
                TeamDefinition.model_validate_json(r["body"])
                for r in c.execute("SELECT body FROM team_definitions ORDER BY team_id,version")
            ]
            # Definitions are owner-wide, but run discovery is explicitly scoped.
            # No workspace means a definitions-only response, never global runs.
            graph_runs = []
            next_cursor = None
            if cursor is not None and workspace is None:
                raise HTTPException(status_code=422, detail="Graph cursor requires workspace")
            if workspace is not None:
                predicate = "json_extract(body, '$.workspace_or_target') = ?"
                parameters: list[Any] = [workspace]
                if cursor is not None:
                    try:
                        anchor = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
                        if (
                            not isinstance(anchor, list)
                            or len(anchor) != 3
                            or not all(isinstance(value, str) and value for value in anchor)
                            or anchor[0] != workspace
                            or len(anchor[1]) > 64
                            or len(anchor[2]) > 300
                        ):
                            raise ValueError("invalid cursor")
                    except (ValueError, UnicodeError, binascii.Error) as exc:
                        raise HTTPException(
                            status_code=422, detail="Invalid Graph directory cursor"
                        ) from exc
                    predicate += " AND (updated_at < ? OR (updated_at = ? AND id < ?))"
                    parameters.extend([anchor[1], anchor[1], anchor[2]])
                rows = c.execute(
                    "SELECT id,updated_at,body FROM graph_workflow_runs WHERE "
                    + predicate
                    + " ORDER BY updated_at DESC,id DESC LIMIT 101",
                    parameters,
                ).fetchall()
                graph_runs = [GraphWorkflowRun.model_validate_json(row["body"]) for row in rows]
                if len(rows) > 100:
                    # Capture ordering values, not a mutable row lookup on the next page.
                    anchor = [workspace, rows[99]["updated_at"], rows[99]["id"]]
                    next_cursor = base64.urlsafe_b64encode(
                        json.dumps(anchor, ensure_ascii=False).encode()
                    ).decode()
        roles = []
        for role in service.list_roles():
            profile = service.get_model_profile(role.model_profile_id)
            roles.append(
                CollaborationRole(
                    id=role.id,
                    name=role.name,
                    model_profile_id=profile.id,
                    model_id=profile.model_id,
                    effort=role.effort.value,
                )
            )
        return CollaborationDirectory(
            workflows=workflows,
            teams=teams,
            roles=roles,
            graph_runs=[
                CollaborationGraphRun(
                    id=run.id,
                    workflow_definition_id=run.workflow_definition_id,
                    workflow_definition_version=run.workflow_definition_version,
                    workspace_or_target=run.workspace_or_target,
                    team_run_id=run.team_run_id,
                    status=run.status,
                    updated_at=run.updated_at,
                )
                for run in graph_runs[:100]
            ],
            graph_runs_has_more=len(graph_runs) > 100,
            graph_runs_next_cursor=next_cursor,
        )

    @app.get(
        "/v1/b2-4/collaboration",
        response_model=CollaborationDirectory,
        operation_id="getB24Collaboration",
    )
    def get_collaboration_directory(
        workspace: str | None = Query(default=None, min_length=1, max_length=4096),
        cursor: str | None = Query(default=None, min_length=1, max_length=32768),
    ) -> Any:
        return _bounded_projection(directory(workspace, cursor))

    @app.get(
        "/v1/b2-4/context/{session_id}",
        response_model=ContextInspection,
        operation_id="getB24Context",
    )
    def inspect_context(session_id: str) -> Any:
        try:
            service.get_session(session_id)
        except NotFoundError as exc:
            raise HTTPException(404, "Session not found") from exc
        revisions = []
        with service.store._connect() as c:
            latest = c.execute(
                "SELECT id FROM context_revisions WHERE session_id=? "
                "ORDER BY sequence DESC LIMIT 50",
                (session_id,),
            ).fetchall()
        for item in reversed(latest):
            revision = service.store.get_context_revision(item["id"])
            value = revision.model_dump(mode="json")
            with service.store._connect() as c:
                row = c.execute(
                    "SELECT body FROM b24_context_memory WHERE revision_id=?", (revision.id,)
                ).fetchone()
            value["memory_inspection"] = json.loads(row["body"]) if row else None
            revisions.append(InspectedContextRevision.model_validate(value))
        return _bounded_projection(ContextInspection(session_id=session_id, revisions=revisions))

    @app.post("/v1/b2-4/commands", response_model=B24Result, operation_id="executeB24Command")
    async def execute(
        body: B24Command,
        response: Response,
        idempotency_key: str | None = Header(default=None, min_length=1, max_length=300),
    ) -> Any:
        key = idempotency_key or uuid4().hex
        response.headers["Idempotency-Key"] = key
        digest = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
        async with lock:
            with service.store._connect() as c:
                old = c.execute("SELECT * FROM b24_commands WHERE command_id=?", (key,)).fetchone()
            if old:
                if old["request_digest"] != digest:
                    raise HTTPException(409, "idempotency payload changed")
                if old["result"] == "pending":
                    raise HTTPException(
                        409, "command outcome requires reconciliation; do not replay"
                    )
                response.headers["Idempotency-Replayed"] = "true"
                stored = json.loads(old["result"])
                if stored.get("format") == "b24-public-result.v1":
                    return B24Result.model_validate(stored["projection"])
                # Pre-fix journals contain an unredacted model. Sanitize once
                # when replaying them; current journals already store public data.
                return _bounded_projection(B24Result.model_validate(stored))
            try:
                gateway = app.state.phase45_action_gateway
                action, decision, _ = gateway.guard(
                    tool="collaboration_management",
                    operation=body.action,
                    target_id=body.session_id
                    or body.workflow_id
                    or body.team_id
                    or "collaboration",
                    arguments={"request_digest": digest},
                    capabilities=(Capability.WORKSPACE_WRITE,),
                    idempotency_key=key,
                )
                if decision.decision.value != "allow" or decision.lease is None:
                    raise PermissionError(decision.reason_code)
                gateway.consume(decision.lease, action)
                with service.store._connect() as connection:
                    connection.execute(
                        "INSERT INTO b24_commands VALUES(?,?,?)", (key, digest, "pending")
                    )
                if body.action == "graph_create_from_roles":
                    if not body.role_ids or not body.name:
                        raise ValueError("name and at least one role required")
                    roles = [service.get_role(r) for r in dict.fromkeys(body.role_ids)]
                    version = 1
                    if body.workflow_id:
                        versions = [
                            w.version
                            for w in directory().workflows
                            if w.workflow_id == body.workflow_id
                        ]
                        if not versions:
                            raise ValueError("workflow not found")
                        version = max(versions) + 1
                        with service.store._connect() as c:
                            active = c.execute(
                                "SELECT 1 FROM graph_workflow_runs "
                                "WHERE json_extract(body,'$.workflow_definition_id')=? "
                                "AND status NOT IN ('completed','failed','cancelled') LIMIT 1",
                                (body.workflow_id,),
                            ).fetchone()
                        if active:
                            raise ValueError("active workflow cannot be edited")
                    definition = WorkflowDefinition(
                        workflow_id=body.workflow_id or "definition_" + uuid4().hex,
                        version=version,
                        name=body.name,
                        description=body.description or "",
                        nodes=tuple(
                            NodeSpec(
                                node_id="member_" + str(n + 1),
                                node_kind=NodeKind.AGENT,
                                writes_workspace=bool(
                                    set(r.tool_policy.allowed_tools) - READ_ONLY_AGENT_TOOLS
                                ),
                                idempotency_class=(
                                    IdempotencyClass.NON_IDEMPOTENT
                                    if set(r.tool_policy.allowed_tools) - READ_ONLY_AGENT_TOOLS
                                    else IdempotencyClass.PURE
                                ),
                                metadata={
                                    "role_id": r.id,
                                    "role_version": r.version,
                                    "task": body.task or "",
                                },
                                workspace_or_target=body.workspace_or_target,
                            )
                            for n, r in enumerate(roles)
                        ),
                        locked_role_versions={r.id: r.version for r in roles},
                    )
                    team = TeamDefinition(
                        members=tuple(
                            TeamMember(
                                member_id=n.node_id,
                                agent_definition_id=r.id,
                                role=r.name,
                                can_coordinate=i == 0,
                            )
                            for i, (n, r) in enumerate(zip(definition.nodes, roles, strict=True))
                        ),
                        default_coordinator=definition.nodes[0].node_id,
                    )
                    definition = definition.model_copy(
                        update={
                            "default_policy": {
                                "b24_executor": True,
                                "team_id": team.team_id,
                                "team_version": team.version,
                            }
                        }
                    )
                    app.state.graph_repository.put_definition(definition)
                    app.state.team_repository.put_team_definition(team)
                    result = B24Result(
                        resource_id=definition.workflow_id,
                        resource_type="workflow_definition",
                        directory=directory(),
                    )
                elif body.action == "team_start_from_roles":
                    if not body.team_id or body.team_version is None or not body.workflow_run_id:
                        raise ValueError("select a Team definition and Graph run")
                    graph, definition = executor._load_run_definition(body.workflow_run_id)
                    expected_team = executor._team_reference(graph, definition)
                    if expected_team != (body.team_id, body.team_version):
                        raise ValueError("selected Team does not match the frozen Graph definition")
                    if body.workspace_or_target is not None and (
                        not Path(body.workspace_or_target).is_absolute()
                        or Path(body.workspace_or_target).resolve()
                        != Path(graph.workspace_or_target or "").resolve()
                    ):
                        raise ValueError("selected workspace does not match the Graph run")
                    team = app.state.team_repository.get_team_definition(*expected_team)
                    if team is None:
                        raise ValueError("team not found")
                    chosen = set(body.member_ids or [m.member_id for m in team.members])
                    if chosen != {m.member_id for m in team.members}:
                        raise ValueError("this definition requires its complete roster")
                    # Use the same admission path as Graph execution: immutable
                    # role versions, terminal state, roster and budget reserves.
                    prepared = executor.prepare(graph.id)
                    result = B24Result(
                        resource_id=prepared.team_run_id,
                        resource_type="team_run",
                        directory=directory(),
                    )
                else:
                    if not body.session_id:
                        raise ValueError("session_id required")
                    session = service.get_session(body.session_id)
                    if body.session_id in service._session_run_leases:
                        raise ValueError("refresh is allowed only after a completed turn")
                    if service.memory_manager is None and service.memory_manager_factory is None:
                        raise ValueError("memory manager unavailable")
                    manager = service.memory_manager
                    if manager is None:
                        assert service.memory_manager_factory is not None
                        manager = service.memory_manager_factory()
                    manifest_run_id = session.id
                    state = load_manifest(manager, manifest_run_id)
                    if state is None:
                        with service.store._connect() as c:
                            context = c.execute(
                                """SELECT m.body FROM b24_context_memory m
                                JOIN context_revisions r ON r.id=m.revision_id
                                WHERE r.session_id=? ORDER BY r.sequence DESC LIMIT 1""",
                                (session.id,),
                            ).fetchone()
                        if context:
                            manifest_run_id = json.loads(context["body"])["pack"]["run_id"]
                            state = load_manifest(manager, manifest_run_id)
                    if state is None:
                        raise ValueError("no Memory Manifest for session")
                    if manifest_run_id != session.id:
                        with service.store._connect() as c:
                            graph = c.execute(
                                "SELECT status FROM graph_workflow_runs WHERE id=?",
                                (manifest_run_id,),
                            ).fetchone()
                        if graph is None or graph["status"] not in {
                            "completed",
                            "failed",
                            "cancelled",
                        }:
                            raise ValueError(
                                "shared memory refresh requires a completed Graph stage"
                            )
                    if body.action == "memory_exclude":
                        if not body.record_id or body.record_id not in {
                            r["record_id"]
                            for r in state.get("used_by_session", {}).get(
                                session.id,
                                state.get("used_refs", [])
                                if "used_by_session" not in state
                                else [],
                            )
                        }:
                            raise ValueError("memory is not used by this session")
                        state["excluded"] = list(
                            dict.fromkeys([*state.get("excluded", []), body.record_id])
                        )
                    else:
                        state["cutoff"] = publication_cutoff(manager)
                    state["revision"] += 1
                    with service.store._connect() as c:
                        row = c.execute(
                            "SELECT dataset_id FROM b24_manifests WHERE run_id=?",
                            (manifest_run_id,),
                        ).fetchone()
                    save_manifest(manager, manifest_run_id, session.id, row["dataset_id"], state)
                    result = B24Result(
                        resource_id=manifest_run_id,
                        resource_type="context_manifest",
                        directory=directory(),
                    )
                result = _bounded_projection(result)
                with service.store._connect() as c:
                    c.execute(
                        "UPDATE b24_commands SET result=? WHERE command_id=? AND request_digest=?",
                        (
                            json.dumps(
                                {
                                    "format": "b24-public-result.v1",
                                    "projection": result.model_dump(mode="json"),
                                }
                            ),
                            key,
                            digest,
                        ),
                    )
                return result
            except PermissionError as exc:
                raise HTTPException(403, str(exc)) from exc
            except (ValueError, KeyError, GraphExecutionError) as exc:
                raise HTTPException(409, str(exc)) from exc
