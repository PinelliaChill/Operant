"""B2-5 governance queries and guarded, exact-version commands."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Query, Response

from operant.api_b2_3 import _bounded_projection
from operant.application.service import ApplicationService
from operant.contracts.b2_1 import SourceRef
from operant.contracts.b2_5 import (
    B25Command,
    B25Result,
    ContextGovernanceImpact,
    ContextMemoryImpact,
    GovernanceEvent,
    GovernanceEventPage,
    GovernanceState,
    HistoryDetail,
    HistoryPage,
)
from operant.domain.security import Capability
from operant.domain.threads import ConversationThread, Item, Turn, UserMessagePayload
from operant.memory_plugins.governance import GovernanceError, GovernanceService
from operant.memory_plugins.ledger import LedgerError
from operant.memory_plugins.maintenance import MaintenanceError, install_maintenance
from operant.memory_plugins.manager import MemoryManager
from operant.persistence.sqlite import ConflictError, NotFoundError, SQLiteStore
from operant.plugins.protocol import PluginError


def _record_completion(
    store: SQLiteStore,
    *,
    key: str,
    project_id: str,
    dataset_id: str,
    action: str,
    payload: dict[str, Any],
) -> None:
    """Completion event and replay receipt share one transaction."""
    with store._connect() as c:
        if payload["affected_ids"]:
            c.execute(
                "INSERT INTO b25_events(dataset_id,project_id,action,affected_ids,occurred_at) "
                "VALUES(?,?,?,?,?)",
                (
                    dataset_id,
                    project_id,
                    action,
                    json.dumps(payload["affected_ids"]),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        c.execute(
            "UPDATE b25_commands SET state='completed',result=? WHERE command_id=?",
            (json.dumps(payload), key),
        )


def install_b2_5_routes(app: FastAPI, service: ApplicationService) -> None:
    def manager() -> MemoryManager:
        if service.memory_manager is None:
            if service.memory_manager_factory is None:
                raise HTTPException(
                    503, {"code": "unavailable", "message": "memory service unavailable"}
                )
            return service.memory_manager_factory()
        return service.memory_manager

    def governance() -> GovernanceService:
        return GovernanceService(manager())

    def recover_pending_commands() -> None:
        # Startup runs before accepting commands. A pending journal is evidence
        # of uncertainty, never authorization to repeat the business mutation.
        with service.store._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            rows = c.execute("SELECT * FROM b25_commands WHERE state='pending'").fetchall()
            for row in rows:
                metadata = json.loads(row["result"] or "{}")
                c.execute(
                    "INSERT INTO b25_events(dataset_id,project_id,action,affected_ids,occurred_at) "
                    "VALUES(?,?,?,?,?)",
                    (
                        metadata.get("dataset_id", "unresolved"),
                        row["project_id"],
                        "outcome_unknown",
                        json.dumps([row["command_id"]]),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                c.execute(
                    "UPDATE b25_commands SET state='outcome_unknown' WHERE command_id=?",
                    (row["command_id"],),
                )

    app.router.on_startup.insert(0, recover_pending_commands)

    app.state.b25_governance_factory = governance
    install_maintenance(app, service, governance)

    def projection(project_id: str) -> GovernanceState:
        state = governance().get_governance(project_id)
        m = manager()
        project = m._project(project_id)
        if project.get("installation_id"):
            installation = m.registry.get_installation(project["installation_id"])
            if installation.binding_id:
                state.maintenance_enabled = m.registry.get_binding(
                    installation.binding_id
                ).config.maintenance_enabled
        controller = getattr(app.state, "b25_maintenance", None)
        if controller is not None:
            state.jobs = controller.jobs(project_id)
        with m.store._connect() as c:
            state.unresolved_command_ids = [
                str(row[0])
                for row in c.execute(
                    "SELECT command_id FROM b25_commands WHERE project_id=? "
                    "AND state IN ('pending','outcome_unknown') ORDER BY command_id",
                    (project_id,),
                ).fetchall()
            ]
        return cast(GovernanceState, _bounded_projection(state))

    def translate(exc: Exception) -> HTTPException:
        if isinstance(exc, PermissionError):
            return HTTPException(
                403,
                {"code": "permission_denied", "message": "当前来源、权限或记忆开关不允许此操作"},
            )
        if isinstance(exc, (NotFoundError, LookupError)):
            return HTTPException(404, {"code": "not_found", "message": "请求对象不存在"})
        if isinstance(exc, PluginError):
            return HTTPException(
                409, {"code": exc.code, "message": "当前插件状态不允许此操作，请刷新后核对"}
            )
        if isinstance(exc, (LedgerError, ConflictError)):
            return HTTPException(
                409,
                {
                    "code": "revision_conflict",
                    "message": "候选、版本或来源已变化，请刷新后重新核对",
                },
            )
        if isinstance(exc, MaintenanceError):
            return HTTPException(
                409,
                {"code": exc.code, "message": "后台整理当前不能执行，请核对任务状态、配置和来源"},
            )
        if isinstance(exc, GovernanceError) and str(exc) == "conflict_resolution_required":
            return HTTPException(
                409,
                {
                    "code": "conflict_resolution_required",
                    "message": "请对原记录提出修正，或通过明确的替代关系解决冲突后再确认",
                },
            )
        if isinstance(exc, GovernanceError):
            return HTTPException(
                409,
                {"code": "governance_conflict", "message": "治理状态不允许此操作，请刷新后核对"},
            )
        return HTTPException(
            422, {"code": "invalid_request", "message": "请求参数不符合当前治理契约"}
        )

    @app.get("/v1/protocol/b2-5", operation_id="negotiateB25")
    def negotiate() -> dict[str, Any]:
        path = (
            Path(__file__).resolve().parents[2] / "sdk/protocol/schema/operant-b2-5.openapi.sha256"
        )
        if not path.is_file():
            raise HTTPException(503, "B2-5 schema unavailable")
        return {
            "protocol_version": "b2-5.v1",
            "schema_digest": path.read_text().split()[0],
            "min_client_version": "b2-5.v1",
            "capabilities": ["history_search", "memory_governance", "memory_maintenance"],
        }

    @app.get(
        "/v1/b2-5/projects/{project_id}/governance",
        response_model=GovernanceState,
        operation_id="getB25Governance",
    )
    def state(project_id: str) -> GovernanceState:
        try:
            return projection(project_id)
        except (
            GovernanceError,
            LedgerError,
            PluginError,
            NotFoundError,
            ValueError,
            KeyError,
        ) as exc:
            raise translate(exc) from exc

    @app.get(
        "/v1/b2-5/projects/{project_id}/history",
        response_model=HistoryPage,
        operation_id="searchB25History",
    )
    def history(
        project_id: str,
        query: str | None = Query(default=None, max_length=1000),
        after_cursor: int | None = Query(default=None, ge=0, le=2**53 - 1),
        cutoff_cursor: int | None = Query(default=None, ge=0, le=2**53 - 1),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> HistoryPage:
        try:
            result = governance().search_history(
                project_id,
                query,
                after_cursor=after_cursor,
                cutoff_cursor=cutoff_cursor,
                limit=limit,
            )
            return cast(HistoryPage, _bounded_projection(result))
        except (
            GovernanceError,
            LedgerError,
            PluginError,
            NotFoundError,
            ValueError,
            KeyError,
        ) as exc:
            raise translate(exc) from exc

    @app.get(
        "/v1/b2-5/projects/{project_id}/history/{item_id}",
        response_model=HistoryDetail,
        operation_id="getB25HistoryItem",
    )
    def detail(project_id: str, item_id: str) -> HistoryDetail:
        try:
            return cast(
                HistoryDetail, _bounded_projection(governance().history_detail(project_id, item_id))
            )
        except (
            GovernanceError,
            LedgerError,
            PluginError,
            NotFoundError,
            ValueError,
            KeyError,
        ) as exc:
            raise translate(exc) from exc

    @app.get(
        "/v1/b2-5/projects/{project_id}/events",
        response_model=GovernanceEventPage,
        operation_id="getB25Events",
    )
    def events(
        project_id: str,
        after_cursor: int = Query(default=0, ge=0, le=2**53 - 1),
        limit: int = Query(default=100, ge=1, le=100),
    ) -> GovernanceEventPage:
        try:
            m = manager()
            project = m._project(project_id)
            installation_id = project.get("installation_id")
            if not installation_id:
                return GovernanceEventPage(events=[])
            installation = m.registry.get_installation(installation_id)
            with m.store._connect() as c:
                rows = c.execute(
                    "SELECT * FROM b25_events WHERE project_id=? AND dataset_id=? "
                    "AND sequence>? ORDER BY sequence LIMIT ?",
                    (project_id, installation.dataset_id, after_cursor, limit + 1),
                ).fetchall()
            result = GovernanceEventPage(
                events=[
                    GovernanceEvent(
                        cursor=r["sequence"],
                        project_id=r["project_id"],
                        action=r["action"],
                        affected_ids=json.loads(r["affected_ids"]),
                        occurred_at=r["occurred_at"],
                    )
                    for r in rows[:limit]
                ],
                next_cursor=rows[limit - 1]["sequence"] if len(rows) > limit else None,
            )
            return cast(GovernanceEventPage, _bounded_projection(result))
        except (
            GovernanceError,
            LedgerError,
            PluginError,
            NotFoundError,
            ValueError,
            KeyError,
        ) as exc:
            raise translate(exc) from exc

    @app.get(
        "/v1/b2-5/sessions/{session_id}/context-impact",
        response_model=ContextGovernanceImpact,
        operation_id="getB25ContextImpact",
    )
    def context_impact(session_id: str) -> ContextGovernanceImpact:
        from operant.contracts.b2_4 import MemoryInspection
        from operant.memory_plugins.recall import history_publication_active

        try:
            service.get_session(session_id)
        except NotFoundError as exc:
            raise translate(exc) from exc
        m = manager()
        with m.store._connect() as c:
            rows = c.execute(
                "SELECT m.body FROM b24_context_memory m "
                "JOIN context_revisions r ON r.id=m.revision_id "
                "WHERE r.session_id=? ORDER BY r.sequence DESC LIMIT 50",
                (session_id,),
            ).fetchall()
        references = {}
        for row in rows:
            for entry in MemoryInspection.model_validate_json(row["body"]).entries:
                references[entry.memory.ref] = entry.memory
        results = []
        for ref in references:
            reason = "dataset_unavailable"
            valid = False
            conflict_ids = []
            for project in m._state["projects"]:
                iid = project.get("installation_id")
                if not iid:
                    continue
                installation = m.registry.get_installation(iid)
                if installation.dataset_id != ref.dataset_id:
                    continue
                try:
                    m._installation(project)
                except (PluginError, PermissionError, ValueError):
                    reason = "binding_disabled"
                    break
                g = governance()
                valid = history_publication_active(m, ref) and g.version_dependencies_valid(ref)
                reason = "usable" if valid else "source_or_time_invalid"
                conflict_ids = [
                    e.proposal.proposal_id
                    for e in g.inbox(project["project_id"])
                    if any(
                        r.relation == "conflicts_with" and r.target.record_id == ref.record_id
                        for r in e.relationships
                    )
                ]
                break
            results.append(
                ContextMemoryImpact(
                    ref=ref,
                    source_and_time_valid=valid,
                    reason=reason,
                    conflict_proposal_ids=conflict_ids,
                )
            )
        return cast(
            ContextGovernanceImpact,
            _bounded_projection(ContextGovernanceImpact(session_id=session_id, entries=results)),
        )

    def user_source(body: B25Command) -> SourceRef:
        m = manager()
        project = m._project(body.project_id)
        installation = m._installation(project)
        binding = m.registry.get_binding(installation.binding_id)
        workspace = m.store.get_workspace_initialization_by_id(project["workspace_id"])
        thread = service.create_thread(ConversationThread(workspace_ref=workspace.workspace_ref))
        turn = service.create_turn(Turn(thread_id=thread.id))
        item = service.append_item(
            Item(
                thread_id=thread.id,
                turn_id=turn.id,
                payload=UserMessagePayload(
                    text=body.content or "", author_ref="user:memory-governance"
                ),
            )
        )
        assert isinstance(item.payload, UserMessagePayload)
        return SourceRef(
            source_type="item",
            source_id=item.id,
            revision=item.cursor or 1,
            content_digest=hashlib.sha256(item.payload.text.encode()).hexdigest(),
            scope=m._scope(project),
            permission_epoch=binding.permission_epoch,
            availability="available",
        )

    @app.post("/v1/b2-5/commands", response_model=B25Result, operation_id="executeB25Command")
    async def execute(
        body: B25Command,
        response: Response,
        idempotency_key: str | None = Header(default=None, min_length=1, max_length=300),
    ) -> B25Result:
        key = idempotency_key or uuid4().hex
        response.headers["Idempotency-Key"] = key
        request_digest = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
        gateway = app.state.phase45_action_gateway
        action, decision, _ = gateway.guard(
            tool="memory_governance",
            operation=body.action,
            target_id=body.project_id,
            arguments={"request_digest": request_digest},
            capabilities=(Capability.WORKSPACE_WRITE,),
            idempotency_key=key,
        )
        if decision.decision.value != "allow" or decision.lease is None:
            raise HTTPException(403, {"code": "policy_denied", "message": "Policy 阻止此治理操作"})
        gateway.consume(decision.lease, action)
        m = manager()
        async with m._lock:
            with m.store._connect() as c:
                old = c.execute("SELECT * FROM b25_commands WHERE command_id=?", (key,)).fetchone()
                if old is not None:
                    if (
                        old["request_digest"] != request_digest
                        or old["project_id"] != body.project_id
                    ):
                        raise HTTPException(
                            409, {"code": "revision_conflict", "message": "幂等键对应另一请求"}
                        )
                    if old["state"] != "completed":
                        raise HTTPException(
                            409,
                            {
                                "code": "manual_reconcile_required",
                                "message": "上次结果未知，请刷新核对，勿自动重放",
                            },
                        )
                    response.headers["Idempotent-Replayed"] = "true"
                    return B25Result(**json.loads(old["result"]), state=projection(body.project_id))
                try:
                    project = m._project(body.project_id)
                    installation = m.registry.get_installation(project["installation_id"])
                except (NotFoundError, PluginError, ValueError, KeyError) as exc:
                    raise translate(exc) from exc
                dataset_id = installation.dataset_id
                c.execute(
                    "INSERT INTO b25_commands VALUES(?,?,?,'pending',?)",
                    (
                        key,
                        body.project_id,
                        request_digest,
                        json.dumps({"dataset_id": dataset_id, "action": body.action}),
                    ),
                )
            try:
                g = governance()
                affected: list[str]
                if body.action == "propose":
                    if not body.content or not body.content.strip():
                        raise ValueError("content required")
                    sources = body.sources or [user_source(body)]
                    entry = g.propose(
                        body.project_id,
                        record_id=body.record_id,
                        content=body.content,
                        sources=sources,
                        relationships=body.relationships,
                        valid_from=body.valid_from,
                        valid_until=body.valid_until,
                        review_due_at=body.review_due_at,
                        expected_head_revision=body.expected_head_revision,
                        idempotency_key=key,
                    )
                    affected = [entry.proposal.proposal_id]
                elif body.action == "review":
                    if not body.selections or body.decision is None:
                        raise ValueError("exact selections and decision required")
                    entries = g.review(
                        body.project_id,
                        body.selections,
                        decision=body.decision,
                        idempotency_key=key,
                    )
                    affected = [e.proposal.proposal_id for e in entries]
                elif body.action == "maintenance_configure":
                    if body.enabled is None:
                        raise ValueError("enabled required")
                    project = m._project(body.project_id)
                    installation = m._installation(project, enabled=body.enabled)
                    if not installation.binding_id:
                        raise ValueError("binding required")
                    m.registry.configure_maintenance(installation.binding_id, enabled=body.enabled)
                    affected = [installation.binding_id]
                    if not body.enabled:
                        controller = app.state.b25_maintenance
                        for job in controller.jobs(body.project_id):
                            if job.state in {"queued", "running", "retry_wait"}:
                                await controller.execute_command(
                                    B25Command(
                                        action="maintenance_cancel",
                                        project_id=body.project_id,
                                        job_id=job.job_id,
                                    ),
                                    idempotency_key=key + ":cancel",
                                )
                elif body.action == "source_revoke":
                    if body.source is None:
                        raise ValueError("source required")
                    affected = list(g.revoke_source(body.project_id, body.source))
                else:
                    controller = getattr(app.state, "b25_maintenance", None)
                    if controller is None:
                        raise HTTPException(
                            503, {"code": "unavailable", "message": "后台整理执行器不可用"}
                        )
                    affected = await controller.execute_command(body, idempotency_key=key)
                payload: dict[str, Any] = {
                    "status": "completed",
                    "message": "治理操作已由服务端记录",
                    "affected_ids": affected,
                }
                _record_completion(
                    m.store,
                    key=key,
                    project_id=body.project_id,
                    dataset_id=dataset_id,
                    action=body.action,
                    payload=payload,
                )
                return B25Result(**payload, state=projection(body.project_id))
            except (
                GovernanceError,
                MaintenanceError,
                LedgerError,
                PluginError,
                NotFoundError,
                ConflictError,
                ValueError,
                KeyError,
                PermissionError,
            ) as exc:
                raise translate(exc) from exc
