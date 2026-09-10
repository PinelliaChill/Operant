"""Additive B2-2 task/history projection over committed Core facts."""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from operant.application.service import ApplicationService
from operant.contracts.b2_1 import TaskAction, TaskSource
from operant.domain.models import AgentInstance, Session
from operant.domain.threads import Item, LegacySourceType, ThreadLegacyRef
from operant.persistence.sqlite import NotFoundError
from operant.protocol import redact_public_data, redact_public_text


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
                agents = [AgentInstance.model_validate_json(r["body"]) for r in agent_rows]
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
            agents=[AgentInstance.model_validate_json(row["body"]) for row in rows],
            items=items[:limit],
            next_cursor=items[limit - 1].cursor if len(items) > limit else None,
        )
        return redact_public_data(result.model_dump(mode="json"))
