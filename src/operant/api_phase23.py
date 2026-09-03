from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from operant.application.graph import (
    GraphCompilationError,
    GraphCompiler,
    GraphConflictError,
    GraphRuntime,
    GraphStateError,
)
from operant.application.team import TeamRuntime
from operant.domain.graph import (
    BoundaryResolution,
    GraphRunStatus,
    NodeKind,
    NodeRunStatus,
    WorkflowDefinition,
    WorkflowDefinitionStatus,
)
from operant.domain.team import (
    ArtifactBoardUpdate,
    MessageAck,
    MessageAudience,
    MessageEnvelope,
    MessageKind,
    RosterEntry,
    RosterMemberStatus,
    TaskBoardUpdate,
    TeamArtifact,
    TeamDefinition,
    TeamRoster,
    TeamRun,
    TeamRunStatus,
    TeamTask,
    TeamTaskStatus,
)
from operant.persistence.graph_team import SQLiteGraphRepository, SQLiteTeamRepository
from operant.persistence.sqlite import NotFoundError, SQLiteStore

MAX_CURSOR = 2**63 - 1


class CompileWorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    definition_version: int = Field(ge=1)


class PublishWorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    draft_version: int = Field(ge=1)


class StartGraphRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow_id: str = Field(min_length=1, max_length=300)
    definition_version: int = Field(ge=1)
    input: dict[str, Any] = Field(default_factory=dict)
    workspace_or_target: str | None = Field(default=None, max_length=4096)


class ResumeGraphRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allow_unknown_side_effect_replay: bool = False


class NodeInputRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: Any
    approval_id: str | None = Field(default=None, max_length=300)


class RosterBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    member_id: str = Field(min_length=1, max_length=200)
    agent_instance_id: str = Field(min_length=1, max_length=300)
    thread_id: str = Field(min_length=1, max_length=300)
    status: RosterMemberStatus = RosterMemberStatus.ACTIVE


class StartTeamRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    team_id: str = Field(min_length=1, max_length=300)
    team_version: int = Field(ge=1)
    workflow_run_id: str = Field(min_length=1, max_length=300)
    roster: tuple[RosterBinding, ...] = Field(min_length=1, max_length=64)


class SendTeamMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sender_id: str = Field(min_length=1, max_length=300)
    recipient_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    audience: MessageAudience = MessageAudience.DIRECT
    message_kind: MessageKind
    payload: dict[str, Any] = Field(default_factory=dict)
    artifact_refs: tuple[str, ...] = Field(default=(), max_length=100)
    reply_to: str | None = Field(default=None, max_length=300)
    causation_id: str | None = Field(default=None, max_length=300)
    correlation_id: str | None = Field(default=None, max_length=300)
    requires_ack: bool = True


class UpdateTeamTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=500)
    summary: str = Field(default="", max_length=4000)
    assignee_ids: tuple[str, ...] = Field(default=(), max_length=64)
    status: TeamTaskStatus = TeamTaskStatus.OPEN
    artifact_refs: tuple[str, ...] = Field(default=(), max_length=100)
    source_message_id: str | None = Field(default=None, max_length=300)
    expected_revision: int = Field(ge=0)


class PublishArtifactBoardItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_id: str = Field(min_length=1, max_length=300)
    title: str = Field(min_length=1, max_length=500)
    publisher_id: str = Field(min_length=1, max_length=300)
    recipient_ids: tuple[str, ...] = Field(default=(), max_length=64)
    source_message_id: str | None = Field(default=None, max_length=300)
    expected_revision: int = Field(default=0, ge=0)


def _receipt(
    request: Request, resource_type: str, resource_id: str, *, recovery: str = "none"
) -> dict[str, Any]:
    return {
        "command_kind": "command",
        "accepted": True,
        "command_id": request.state.command_execution_id,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "resource": {"type": resource_type, "id": resource_id},
        "recovery": recovery,
    }


def _definition_hash(definition: WorkflowDefinition) -> str:
    body = json.dumps(
        definition.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _sse(event: dict[str, Any]) -> str:
    return (
        f"id: {event['cursor']}\n"
        f"event: {event['event_type']}\n"
        f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
    )


def install_phase23_routes(app: FastAPI, store: SQLiteStore) -> None:
    graph_repository = SQLiteGraphRepository(store)
    graph_runtime = GraphRuntime(graph_repository)
    team_repository = SQLiteTeamRepository(store)
    team_runtime = TeamRuntime(team_repository)
    compiler = GraphCompiler()
    for run_id in graph_repository.list_recoverable_run_ids():
        graph_runtime.recover(run_id)

    app.state.graph_repository = graph_repository
    app.state.graph_runtime = graph_runtime
    app.state.team_repository = team_repository
    app.state.team_runtime = team_runtime

    def require_team_run(team_run_id: str) -> TeamRun:
        run = team_repository.get_team_run(team_run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Team Run not found")
        return run

    def require_roster_agents(team_run_id: str, *agent_ids: str) -> set[str]:
        require_team_run(team_run_id)
        roster_ids = {entry.agent_instance_id for entry in team_repository.list_roster(team_run_id)}
        if any(agent_id not in roster_ids for agent_id in agent_ids):
            raise HTTPException(status_code=422, detail="Agent is not in the Team Roster")
        return roster_ids

    @app.post("/v1/graph/workflows/drafts", status_code=202)
    async def create_workflow_draft(
        definition: WorkflowDefinition, request: Request
    ) -> dict[str, Any]:
        draft = definition.model_copy(update={"status": WorkflowDefinitionStatus.DRAFT})
        try:
            graph_repository.put_definition(draft)
        except GraphConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _receipt(
            request,
            "workflow_definition",
            f"{draft.workflow_id}:v{draft.version}",
        )

    @app.post("/v1/graph/workflows/{workflow_id}/compile")
    async def compile_workflow_draft(
        workflow_id: str, body: CompileWorkflowRequest
    ) -> dict[str, Any]:
        try:
            definition = graph_repository.get_definition(workflow_id, body.definition_version)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Workflow Definition not found") from exc
        try:
            compiler.compile(definition)
            issues: list[dict[str, Any]] = []
            valid = True
        except GraphCompilationError as exc:
            issues = [issue.model_dump(mode="json") for issue in exc.issues]
            valid = False
        return {
            "workflow_id": workflow_id,
            "definition_version": definition.version,
            "valid": valid,
            "diagnostics": issues,
            "definition_hash": _definition_hash(definition),
        }

    @app.post("/v1/graph/workflows/{workflow_id}/publish", status_code=202)
    async def publish_workflow(
        workflow_id: str, body: PublishWorkflowRequest, request: Request
    ) -> dict[str, Any]:
        try:
            draft = graph_repository.get_definition(workflow_id, body.draft_version)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Workflow draft not found") from exc
        try:
            compiler.compile(draft)
        except GraphCompilationError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "graph_compile_failed",
                    "diagnostics": [item.model_dump(mode="json") for item in exc.issues],
                },
            ) from exc
        published = draft.model_copy(
            update={
                "version": draft.version + 1,
                "status": WorkflowDefinitionStatus.PUBLISHED,
                "created_at": datetime.now(timezone.utc),
            }
        )
        try:
            graph_repository.put_definition(published)
        except GraphConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _receipt(
            request,
            "workflow_definition",
            f"{published.workflow_id}:v{published.version}",
        )

    @app.get("/v1/graph/workflows/{workflow_id}/definitions/{version}")
    async def get_workflow_definition(workflow_id: str, version: int) -> dict[str, Any]:
        try:
            return graph_repository.get_definition(workflow_id, version).model_dump(mode="json")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Workflow Definition not found") from exc

    @app.post("/v1/graph/runs", status_code=202)
    async def start_graph_run(body: StartGraphRunRequest, request: Request) -> dict[str, Any]:
        try:
            definition = graph_repository.get_definition(body.workflow_id, body.definition_version)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Workflow Definition not found") from exc
        try:
            run = graph_runtime.create_run(
                definition,
                input=body.input,
                workspace_or_target=body.workspace_or_target,
            )
            graph_runtime.start_run(run.id)
        except (GraphConflictError, GraphStateError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _receipt(request, "graph_run", run.id, recovery="replay_events")

    def graph_run_projection(run_id: str) -> dict[str, Any]:
        run = graph_repository.get_run(run_id)
        result = run.model_dump(mode="json")
        result["current_node_ids"] = [
            node.node_id
            for node in graph_repository.list_node_runs(run_id)
            if node.status
            in {
                NodeRunStatus.READY,
                NodeRunStatus.RUNNING,
                NodeRunStatus.WAITING_APPROVAL,
                NodeRunStatus.WAITING_INPUT,
                NodeRunStatus.WAITING,
                NodeRunStatus.RETRY_WAIT,
            }
        ]
        return result

    @app.get("/v1/graph/runs/by-legacy/{legacy_workflow_run_id}")
    async def get_graph_run_by_legacy_workflow(
        legacy_workflow_run_id: str,
    ) -> dict[str, Any]:
        try:
            run = graph_repository.get_run_by_legacy_workflow_run_id(legacy_workflow_run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Graph Run not found") from exc
        return graph_run_projection(run.id)

    @app.get("/v1/graph/runs/{run_id}")
    async def get_graph_run(run_id: str) -> dict[str, Any]:
        try:
            return graph_run_projection(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Graph Run not found") from exc

    @app.get("/v1/graph/runs/{run_id}/nodes")
    async def list_node_runs(run_id: str) -> list[dict[str, Any]]:
        try:
            graph_repository.get_run(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Graph Run not found") from exc
        result = []
        for node in graph_repository.list_node_runs(run_id):
            item = node.model_dump(mode="json")
            item["attempts"] = [
                attempt.model_dump(mode="json")
                for attempt in graph_repository.list_attempts(node.id)
            ]
            result.append(item)
        return result

    @app.get("/v1/graph/runs/{run_id}/events/stream")
    async def stream_graph_run_events(run_id: str, request: Request) -> StreamingResponse:
        try:
            graph_repository.get_run(run_id)
            cursor = int(request.headers.get("Last-Event-ID", "0"))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Graph Run not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid Last-Event-ID") from exc

        async def replay() -> AsyncIterator[str]:
            current = cursor
            while not await request.is_disconnected():
                events = graph_repository.list_events(run_id, after_cursor=current)
                for event in events:
                    current = int(event["cursor"])
                    yield _sse(event)
                run = graph_repository.get_run(run_id)
                if run.status in {
                    GraphRunStatus.COMPLETED,
                    GraphRunStatus.FAILED,
                    GraphRunStatus.CANCELLED,
                    GraphRunStatus.INTERRUPTED,
                    GraphRunStatus.MANUAL_RECONCILE_REQUIRED,
                }:
                    return
                await asyncio.sleep(0.1)

        return StreamingResponse(replay(), media_type="text/event-stream")

    @app.post("/v1/graph/runs/{run_id}/resume", status_code=202)
    async def resume_graph_run(
        run_id: str, body: ResumeGraphRunRequest, request: Request
    ) -> dict[str, Any]:
        if body.allow_unknown_side_effect_replay:
            raise HTTPException(
                status_code=409,
                detail="unknown side effects cannot be replayed through this command",
            )
        try:
            run = graph_runtime.recover(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Graph Run not found") from exc
        except (GraphConflictError, GraphStateError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        recovery = (
            "manual_reconcile"
            if run.status is GraphRunStatus.MANUAL_RECONCILE_REQUIRED
            else "replay_events"
        )
        return _receipt(request, "graph_run", run_id, recovery=recovery)

    @app.post("/v1/graph/runs/{run_id}/cancel", status_code=202)
    async def cancel_graph_run(run_id: str, request: Request) -> dict[str, Any]:
        try:
            run = graph_runtime.cancel_run(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Graph Run not found") from exc
        except (GraphConflictError, GraphStateError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        recovery = (
            "manual_reconcile"
            if run.status is GraphRunStatus.MANUAL_RECONCILE_REQUIRED
            else "replay_events"
        )
        return _receipt(request, "graph_run", run_id, recovery=recovery)

    @app.post("/v1/graph/runs/{run_id}/nodes/{node_id}/input", status_code=202)
    async def provide_node_input(
        run_id: str, node_id: str, body: NodeInputRequest, request: Request
    ) -> dict[str, Any]:
        try:
            run = graph_repository.get_run(run_id)
            definition = graph_repository.get_definition(
                run.workflow_definition_id, run.workflow_definition_version
            )
            node = next(
                item for item in graph_repository.list_node_runs(run_id) if item.node_id == node_id
            )
            spec = next(item for item in definition.nodes if item.node_id == node_id)
        except (KeyError, StopIteration) as exc:
            raise HTTPException(status_code=404, detail="Graph node not found") from exc
        if spec.node_kind is NodeKind.APPROVAL:
            raise HTTPException(
                status_code=409,
                detail="Approval nodes must be resolved by the durable Approval command",
            )
        if spec.node_kind is not NodeKind.HUMAN_INPUT or node.wait_token is None:
            raise HTTPException(status_code=409, detail="Graph node is not waiting for input")
        try:
            graph_runtime.resolve_boundary(
                node.id,
                BoundaryResolution(wait_token=node.wait_token, payload={"value": body.value}),
            )
        except (GraphConflictError, GraphStateError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _receipt(request, "node_run", node.id, recovery="replay_events")

    @app.post("/v1/teams/definitions", status_code=202)
    async def create_team_definition(
        definition: TeamDefinition, request: Request
    ) -> dict[str, Any]:
        try:
            team_repository.put_team_definition(definition)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _receipt(
            request,
            "team_definition",
            f"{definition.team_id}:v{definition.version}",
        )

    @app.post("/v1/teams/runs", status_code=202)
    async def start_team_run(body: StartTeamRunRequest, request: Request) -> dict[str, Any]:
        definition = team_repository.get_team_definition(body.team_id, body.team_version)
        if definition is None:
            raise HTTPException(status_code=404, detail="Team Definition not found")
        try:
            graph_run = graph_repository.get_run(body.workflow_run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Graph Run not found") from exc
        member_ids = {member.member_id for member in definition.members}
        if any(binding.member_id not in member_ids for binding in body.roster):
            raise HTTPException(status_code=422, detail="Roster member is not in Team Definition")
        if len(body.roster) > definition.max_active_agents:
            raise HTTPException(status_code=422, detail="Roster exceeds Team active-agent limit")
        run = TeamRun(
            team_id=body.team_id,
            team_version=body.team_version,
            workflow_run_id=body.workflow_run_id,
            status=TeamRunStatus.ACTIVE,
        )
        if graph_run.team_run_id is not None and graph_run.team_run_id != run.team_run_id:
            raise HTTPException(
                status_code=409,
                detail="Graph Run is already bound to a different Team Run",
            )
        try:
            roster = TeamRoster(
                team_run_id=run.team_run_id,
                entries=tuple(
                    RosterEntry(
                        team_run_id=run.team_run_id,
                        member_id=binding.member_id,
                        agent_instance_id=binding.agent_instance_id,
                        thread_id=binding.thread_id,
                        status=binding.status,
                        joined_at=datetime.now(timezone.utc),
                    )
                    for binding in body.roster
                ),
            )
            team_repository.bind_graph_and_put_team_run_with_roster(run, roster.entries)
        except GraphConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _receipt(request, "team_run", run.team_run_id, recovery="replay_events")

    @app.get("/v1/teams/runs/{team_run_id}")
    async def get_team_run(team_run_id: str) -> dict[str, Any]:
        run = require_team_run(team_run_id)
        result = run.model_dump(mode="json")
        result["roster"] = [
            entry.model_dump(mode="json") for entry in team_repository.list_roster(team_run_id)
        ]
        return result

    @app.post("/v1/teams/runs/{team_run_id}/messages", status_code=202)
    async def send_team_message(
        team_run_id: str, body: SendTeamMessageRequest, request: Request
    ) -> dict[str, Any]:
        run = require_team_run(team_run_id)
        require_roster_agents(team_run_id, body.sender_id, *body.recipient_ids)
        for artifact_id in body.artifact_refs:
            try:
                store.get_artifact(artifact_id)
            except NotFoundError as exc:
                raise HTTPException(
                    status_code=422, detail="Message Artifact ref is invalid"
                ) from exc
        if body.reply_to is not None:
            parent = team_repository.get_message(body.reply_to)
            if parent is None or parent.team_run_id != team_run_id:
                raise HTTPException(status_code=422, detail="Message reply target is invalid")
        message = MessageEnvelope(
            workflow_run_id=run.workflow_run_id,
            team_run_id=team_run_id,
            **body.model_dump(),
        )
        try:
            team_runtime.send_message(
                message,
                idempotency_key=request.headers.get(
                    "Idempotency-Key", request.state.command_execution_id
                ),
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _receipt(request, "team_message", message.message_id, recovery="replay_events")

    @app.get("/v1/teams/runs/{team_run_id}/messages")
    async def list_team_messages(
        team_run_id: str,
        viewer_id: str = Query(min_length=1, max_length=300),
        after_cursor: int = Query(default=0, ge=0, le=MAX_CURSOR),
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> list[dict[str, Any]]:
        require_roster_agents(team_run_id, viewer_id)

        return [
            {"cursor": cursor, **message.model_dump(mode="json")}
            for cursor, message in team_repository.list_messages_for_viewer(
                team_run_id=team_run_id,
                viewer_id=viewer_id,
                after_cursor=after_cursor,
                limit=limit,
            )
        ]

    @app.get("/v1/teams/runs/{team_run_id}/mailbox/{agent_id}")
    async def get_mailbox(
        team_run_id: str,
        agent_id: str,
        after_cursor: int = Query(default=0, ge=0, le=MAX_CURSOR),
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> dict[str, Any]:
        require_roster_agents(team_run_id, agent_id)
        deliveries = team_repository.list_inbox(
            team_run_id=team_run_id,
            recipient_id=agent_id,
            after_cursor=after_cursor,
            limit=limit,
        )
        return {
            "team_run_id": team_run_id,
            "recipient_id": agent_id,
            "deliveries": [
                {
                    **delivery.model_dump(mode="json"),
                    "message": message.model_dump(mode="json"),
                }
                for delivery, message in deliveries
            ],
        }

    @app.post(
        "/v1/teams/runs/{team_run_id}/mailbox/{agent_id}/{delivery_id}/ack",
        status_code=202,
    )
    async def acknowledge_mailbox_delivery(
        team_run_id: str, agent_id: str, delivery_id: str, request: Request
    ) -> dict[str, Any]:
        delivery = team_repository.get_delivery(delivery_id)
        if delivery is None:
            raise HTTPException(status_code=404, detail="Mailbox delivery not found")
        if delivery.team_run_id != team_run_id or delivery.recipient_id != agent_id:
            raise HTTPException(status_code=404, detail="Mailbox delivery not found")
        if delivery.cursor is None:
            raise HTTPException(status_code=409, detail="Mailbox delivery has no durable cursor")
        try:
            team_runtime.acknowledge(
                MessageAck(
                    delivery_id=delivery.delivery_id,
                    message_id=delivery.message_id,
                    team_run_id=team_run_id,
                    recipient_id=agent_id,
                    idempotency_key=request.headers.get(
                        "Idempotency-Key", request.state.command_execution_id
                    ),
                    cursor=delivery.cursor,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _receipt(request, "mailbox_delivery", delivery_id, recovery="replay_events")

    @app.get("/v1/teams/runs/{team_run_id}/tasks")
    async def get_task_board(team_run_id: str) -> dict[str, Any]:
        require_team_run(team_run_id)
        return {
            "team_run_id": team_run_id,
            "tasks": [
                task.model_dump(mode="json") for task in team_repository.list_tasks(team_run_id)
            ],
        }

    @app.post("/v1/teams/runs/{team_run_id}/tasks/{task_id}", status_code=202)
    async def update_team_task(
        team_run_id: str, task_id: str, body: UpdateTeamTaskRequest, request: Request
    ) -> dict[str, Any]:
        require_roster_agents(team_run_id, *body.assignee_ids)
        if body.source_message_id is not None:
            source_message = team_repository.get_message(body.source_message_id)
            if source_message is None or source_message.team_run_id != team_run_id:
                raise HTTPException(status_code=422, detail="Task source message is invalid")
        for artifact_id in body.artifact_refs:
            try:
                store.get_artifact(artifact_id)
            except NotFoundError as exc:
                raise HTTPException(status_code=422, detail="Task Artifact ref is invalid") from exc
        existing = next(
            (item for item in team_repository.list_tasks(team_run_id) if item.task_id == task_id),
            None,
        )
        revision = 1 if existing is None else existing.revision + 1
        task = TeamTask(
            task_id=task_id,
            team_run_id=team_run_id,
            revision=revision,
            **body.model_dump(exclude={"expected_revision"}),
        )
        try:
            team_runtime.update_task(
                TaskBoardUpdate(
                    idempotency_key=request.headers.get(
                        "Idempotency-Key", request.state.command_execution_id
                    ),
                    task=task,
                    expected_revision=body.expected_revision,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _receipt(request, "team_task", task_id, recovery="replay_events")

    @app.get("/v1/teams/runs/{team_run_id}/artifacts")
    async def get_artifact_board(
        team_run_id: str, viewer_id: str = Query(min_length=1, max_length=300)
    ) -> dict[str, Any]:
        require_roster_agents(team_run_id, viewer_id)
        return {
            "team_run_id": team_run_id,
            "artifacts": [
                item.model_dump(mode="json")
                for item in team_repository.list_artifacts(
                    team_run_id=team_run_id, viewer_id=viewer_id
                )
            ],
        }

    @app.post("/v1/teams/runs/{team_run_id}/artifacts", status_code=202)
    async def publish_artifact_board_item(
        team_run_id: str, body: PublishArtifactBoardItemRequest, request: Request
    ) -> dict[str, Any]:
        require_roster_agents(team_run_id, body.publisher_id, *body.recipient_ids)
        try:
            artifact_record = store.get_artifact(body.artifact_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail="Artifact not found") from exc
        if body.source_message_id is not None:
            source_message = team_repository.get_message(body.source_message_id)
            if source_message is None or source_message.team_run_id != team_run_id:
                raise HTTPException(status_code=422, detail="Artifact source message is invalid")
        existing = next(
            (
                item
                for item in team_repository.list_artifacts(
                    team_run_id=team_run_id,
                    viewer_id=body.publisher_id,
                    owner_audit=True,
                )
                if item.artifact_id == body.artifact_id
            ),
            None,
        )
        team_artifact = TeamArtifact(
            artifact_id=body.artifact_id,
            team_run_id=team_run_id,
            title=body.title,
            media_type=artifact_record.media_type,
            publisher_id=body.publisher_id,
            recipient_ids=body.recipient_ids,
            source_message_id=body.source_message_id,
            revision=1 if existing is None else existing.revision + 1,
        )
        try:
            team_runtime.publish_artifact(
                ArtifactBoardUpdate(
                    idempotency_key=request.headers.get(
                        "Idempotency-Key", request.state.command_execution_id
                    ),
                    artifact=team_artifact,
                    expected_revision=body.expected_revision,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _receipt(request, "artifact_board_item", body.artifact_id, recovery="replay_events")

    @app.get("/v1/teams/runs/{team_run_id}/events/stream")
    async def stream_team_run_events(team_run_id: str, request: Request) -> StreamingResponse:
        if team_repository.get_team_run(team_run_id) is None:
            raise HTTPException(status_code=404, detail="Team Run not found")
        try:
            cursor = int(request.headers.get("Last-Event-ID", "0"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid Last-Event-ID") from exc

        async def replay() -> AsyncIterator[str]:
            current = cursor
            while not await request.is_disconnected():
                events = team_repository.list_events(team_run_id, after_cursor=current)
                for event in events:
                    current = int(event["cursor"])
                    yield _sse(event)
                run = team_repository.get_team_run(team_run_id)
                if run is None or run.status in {
                    TeamRunStatus.COMPLETED,
                    TeamRunStatus.FAILED,
                    TeamRunStatus.CANCELLED,
                }:
                    return
                await asyncio.sleep(0.1)

        return StreamingResponse(replay(), media_type="text/event-stream")
