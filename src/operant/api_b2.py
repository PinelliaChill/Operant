"""Additive B2-2 task/history projection over committed Core facts."""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from operant.application.client_projection import WorkspaceProjectionError
from operant.application.service import ApplicationService
from operant.contracts.b2_1 import TaskAction, TaskSource
from operant.domain.models import AgentInstance, Session
from operant.domain.threads import ConversationThread, Item, LegacySourceType, ThreadLegacyRef
from operant.persistence.sqlite import NotFoundError
from operant.protocol import is_sensitive_key, redact_public_text


def _redact_typed_history(value: Any) -> Any:
    """Preserve a validated projection's shape while redacting dynamic text.

    Collection pagination belongs to the query, never the generic log truncator:
    truncating nested dictionaries would invalidate Item and Session schemas.
    """
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if is_sensitive_key(key) else _redact_typed_history(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_typed_history(item) for item in value]
    if isinstance(value, str):
        return redact_public_text(value)
    return value


class B2CreateThread(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: str = Field(min_length=1, max_length=300)


class B2Task(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: TaskSource
    thread_id: str | None
    project_id: str | None
    workspace_id: str | None
    source_status: str
    revision: int
    created_at: datetime
    title: str
    actions: tuple[TaskAction, ...]


class B2TaskPage(BaseModel):
    items: list[B2Task]
    next_offset: int | None


class B2AgentPage(BaseModel):
    items: list[AgentInstance]
    next_offset: int | None


class B2SessionHistory(BaseModel):
    session: Session
    thread_id: str | None
    agents: list[AgentInstance]
    items: list[Item]
    next_cursor: int | None


class B2Discovery(BaseModel):
    model_ids: list[str]


class B2Cancellation(BaseModel):
    accepted: bool


def install_b2_routes(app: FastAPI, service: ApplicationService) -> None:
    store = service.store

    def projected_agent(body: str) -> AgentInstance:
        agent = AgentInstance.model_validate_json(body)
        # A stream disconnect may interrupt cleanup after the durable terminal
        # event. Prefer that event over a stale running row, without mutating it.
        with store._connect() as connection:
            terminal = connection.execute(
                "SELECT event_type FROM events WHERE agent_id = ? AND event_type IN "
                "('agent.completed','agent.cancelled','agent.failed','agent.timed_out') "
                "ORDER BY sequence DESC LIMIT 1",
                (agent.id,),
            ).fetchone()
        if terminal is not None:
            return AgentInstance.model_validate(
                {
                    **agent.model_dump(),
                    "status": terminal["event_type"].removeprefix("agent."),
                }
            )
        return agent

    @app.post(
        "/v1/b2/threads",
        operation_id="createB2Thread",
        response_model=ConversationThread,
        status_code=201,
    )
    def create_thread(body: B2CreateThread) -> ConversationThread:
        try:
            workspace = store.get_workspace_initialization_by_id(body.workspace_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail="workspace not registered") from exc
        # Recheck current availability through the existing no-follow boundary.
        try:
            service.list_workspace_files(body.workspace_id, limit=1)
        except WorkspaceProjectionError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=409, detail="workspace unavailable") from exc
        return service.create_thread(ConversationThread(workspace_ref=workspace.workspace_ref))

    @app.get("/v1/protocol/b2", operation_id="negotiateB2")
    def negotiate() -> dict[str, Any]:
        configured = os.environ.get("OPERANT_B2_SCHEMA_DIGEST_PATH")
        path = (
            Path(configured)
            if configured
            else Path(__file__).resolve().parents[2]
            / "sdk/protocol/schema/operant-b2.openapi.sha256"
        )
        if not path.is_absolute():
            raise HTTPException(status_code=503, detail="B2 digest path must be absolute")
        try:
            digest = path.read_text().split()[0]
            if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                raise ValueError("invalid digest")
        except (OSError, ValueError, IndexError) as exc:
            raise HTTPException(status_code=503, detail="B2 protocol schema unavailable") from exc
        return {
            "protocol_version": "b2.v1",
            "min_client_version": "b2.v1",
            "schema_digest": digest,
            "capabilities": [
                "task_projection",
                "canonical_history",
                "model_role_configuration",
                "session_cancel",
            ],
        }

    def project_tasks(rows: list[Any]) -> list[B2Task]:
        projects = service.list_project_projections(limit=1000)
        workspace_ids = {project.workspace_ref: project.project_id for project in projects}
        tasks: list[B2Task] = []
        for row in rows:
            source_type, source_id = row["kind"], row["id"]
            try:
                thread = store.get_thread_by_legacy_ref(
                    ThreadLegacyRef(source_type=LegacySourceType(source_type), source_id=source_id)
                )
            except NotFoundError:
                thread = None
            if source_type == "session":
                session = service.get_session(source_id)
                with store._connect() as connection:
                    agent_rows = connection.execute(
                        "SELECT body FROM agents WHERE session_id = ? "
                        "ORDER BY created_at DESC, id DESC LIMIT 1",
                        (source_id,),
                    ).fetchall()
                agents = [projected_agent(r["body"]) for r in agent_rows]
                status = agents[0].status.value if agents else "created"
                with store._connect() as connection:
                    revision = int(
                        connection.execute(
                            "SELECT COALESCE(MAX(sequence), 0) FROM events WHERE session_id = ?",
                            (source_id,),
                        ).fetchone()[0]
                    )
                title = session.role_snapshot.role_name
                # Cancellation admission is an active Core lease, not a guessed UI status.
                try:
                    lease = store.get_session_run_lease(source_id)
                    cancel = lease.released_at is None and lease.expires_at > datetime.now(
                        lease.expires_at.tzinfo
                    )
                except NotFoundError:
                    cancel = False
            else:
                run = service.get_workflow_run(source_id)
                status = run.status.value
                title = run.task[:200]
                with store._connect() as connection:
                    revision = int(
                        connection.execute(
                            "SELECT COALESCE(MAX(sequence), 0) FROM workflow_run_events "
                            "WHERE workflow_run_id = ?",
                            (source_id,),
                        ).fetchone()[0]
                    )
                cancel = status == "running"
            workspace_id = (
                None
                if thread is None or thread.workspace_ref is None
                else workspace_ids.get(thread.workspace_ref)
            )
            tasks.append(
                B2Task(
                    source=TaskSource(source_type=source_type, source_id=source_id),
                    thread_id=None if thread is None else thread.id,
                    project_id=workspace_id,
                    workspace_id=workspace_id,
                    source_status=status,
                    revision=revision,
                    created_at=row["created_at"],
                    title=redact_public_text(title, max_chars=200),
                    actions=(
                        TaskAction(
                            action="inspect",
                            availability="available",
                            reason_code=None,
                            projection_revision=revision,
                        ),
                        TaskAction(
                            action="cancel",
                            availability="available" if cancel else "blocked",
                            reason_code=None if cancel else "no_active_run",
                            projection_revision=revision,
                        ),
                        TaskAction(
                            action="resume",
                            availability="unsupported",
                            reason_code="source_specific_recovery_required",
                            projection_revision=revision,
                        ),
                    ),
                )
            )
        return tasks

    @app.get("/v1/b2/tasks", operation_id="listB2Tasks", response_model=B2TaskPage)
    def list_tasks(
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=100),
    ) -> B2TaskPage:
        # Source-specific rows retain their actual identity. No TeamTask synthesis.
        with store._connect() as connection:
            rows = connection.execute(
                """SELECT 'session' AS kind, id, created_at FROM sessions
                UNION ALL SELECT 'workflow_run' AS kind, id, created_at FROM workflow_runs
                ORDER BY created_at DESC, kind, id LIMIT ? OFFSET ?""",
                (limit + 1, offset),
            ).fetchall()
        return B2TaskPage(
            items=project_tasks(rows[:limit]),
            next_offset=offset + limit if len(rows) > limit else None,
        )

    @app.get("/v1/b2/tasks/{source_id}", operation_id="getB2Task", response_model=B2Task)
    def get_task(
        source_id: str, source_type: Literal["session", "workflow_run"] | None = None
    ) -> B2Task:
        where = "id = ?"
        values: list[Any] = [source_id]
        if source_type is not None:
            where += " AND kind = ?"
            values.append(source_type)
        with store._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM (SELECT 'session' AS kind, id, created_at FROM sessions "
                "UNION ALL SELECT 'workflow_run' AS kind, id, created_at FROM workflow_runs) "
                f"WHERE {where} LIMIT 2",
                values,
            ).fetchall()
        if not rows:
            raise HTTPException(status_code=404, detail="task not found")
        if len(rows) != 1:
            raise HTTPException(
                status_code=409, detail="ambiguous task identity: provide source_type"
            )
        return project_tasks(rows)[0]

    @app.get("/v1/b2/agents", operation_id="listB2Agents", response_model=B2AgentPage)
    def list_agents(
        offset: int = Query(default=0, ge=0, le=2**31 - 1),
        limit: int = Query(default=100, ge=1, le=100),
    ) -> Any:
        with store._connect() as connection:
            rows = connection.execute(
                "SELECT body FROM agents ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (limit + 1, offset),
            ).fetchall()
        result = B2AgentPage(
            items=[projected_agent(row["body"]) for row in rows[:limit]],
            next_offset=offset + limit if len(rows) > limit else None,
        )
        return _redact_typed_history(result.model_dump(mode="json"))

    @app.get(
        "/v1/b2/sessions/{session_id}/history",
        operation_id="getB2SessionHistory",
        response_model=B2SessionHistory,
    )
    def get_history(
        session_id: str,
        after_cursor: int | None = Query(default=None, ge=0, le=2**63 - 1),
        limit: int = Query(default=100, ge=1, le=999),
    ) -> Any:
        try:
            session = service.get_session(session_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc
        try:
            thread = store.get_thread_by_legacy_ref(
                ThreadLegacyRef(source_type=LegacySourceType.SESSION, source_id=session_id)
            )
        except NotFoundError:
            thread = None
        with store._connect() as connection:
            rows = connection.execute(
                "SELECT body FROM agents WHERE session_id = ? ORDER BY created_at, id",
                (session_id,),
            ).fetchall()
        items = (
            []
            if thread is None
            else store.list_items(thread.id, after_cursor=after_cursor, limit=limit + 1)
        )
        result = B2SessionHistory(
            session=session,
            thread_id=None if thread is None else thread.id,
            agents=[projected_agent(row["body"]) for row in rows],
            items=items[:limit],
            next_cursor=items[limit - 1].cursor if len(items) > limit else None,
        )
        return _redact_typed_history(result.model_dump(mode="json"))
