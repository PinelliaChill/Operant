from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shlex
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from operant.application.graph import GraphRuntime, coding_workflow_definition
from operant.application.service import ApplicationService
from operant.domain.graph import (
    AttemptSideEffectState,
    NodeAttempt,
    NodeRun,
    NodeRunStatus,
    WorkflowDefinitionStatus,
)
from operant.domain.memory import MemoryKind
from operant.domain.models import AgentStatus, RolePreset
from operant.domain.workflow import (
    WorkflowRun,
    WorkflowRunEvent,
    WorkflowRunStatus,
    WorkflowStage,
)
from operant.persistence.graph_team import SQLiteGraphRepository
from operant.persistence.sqlite import ConflictError


class WorkflowEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_run_id: str = ""
    cursor: int | None = Field(default=None, ge=1)
    # Internal bridge metadata. These values are intentionally excluded from
    # the frozen legacy Workflow event representation.
    role_id: str | None = Field(default=None, exclude=True)
    agent_id: str | None = Field(default=None, exclude=True)
    role: str
    session_id: str
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class WorkflowSubtaskResult(BaseModel):
    """Bounded handoff returned by every isolated role run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: str
    role_id: str
    session_id: str
    status: AgentStatus
    summary: str = ""
    completed_steps: tuple[str, ...] = ()
    failure_reason: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status is AgentStatus.COMPLETED


@dataclass
class RoleRunCapture:
    role: str
    role_id: str
    final_content: str = ""
    session_id: str = ""
    status: AgentStatus = AgentStatus.CREATED
    completed_steps: list[str] = field(default_factory=list)
    failure_reason: str | None = None

    def observe(self, event_type: str, payload: dict[str, Any]) -> None:
        if event_type != "model.delta" and event_type not in self.completed_steps:
            self.completed_steps.append(event_type)
        if event_type == "agent.started":
            self.status = AgentStatus.RUNNING
        elif event_type == "model.completed":
            content = payload.get("content")
            if content is not None:
                self.final_content = str(content)
        elif event_type == "agent.completed":
            self.status = AgentStatus.COMPLETED
            self.final_content = str(payload.get("content", self.final_content))
        elif event_type == "agent.timed_out":
            self.status = AgentStatus.TIMED_OUT
            self.failure_reason = f"timeout after {payload.get('timeout_seconds')} seconds"
        elif event_type == "agent.cancelled":
            self.status = AgentStatus.CANCELLED
            self.failure_reason = "subtask cancelled"
        elif event_type == "agent.no_progress":
            self.status = AgentStatus.FAILED
            self.failure_reason = str(payload.get("reason", "no progress"))
        elif event_type == "agent.max_turns":
            self.status = AgentStatus.FAILED
            self.failure_reason = "maximum turns reached"
        elif event_type == "agent.failed":
            self.status = AgentStatus.FAILED
            self.failure_reason = str(payload.get("error_type", "agent failure"))
        elif event_type == "budget.exhausted":
            self.status = AgentStatus.FAILED
            self.failure_reason = f"budget exhausted: {payload.get('kind', 'unknown')}"

    def result(self) -> WorkflowSubtaskResult:
        status = self.status
        reason = self.failure_reason
        if status in {AgentStatus.CREATED, AgentStatus.RUNNING}:
            status = AgentStatus.FAILED
            reason = reason or "subtask ended without agent.completed"
        return WorkflowSubtaskResult(
            role=self.role,
            role_id=self.role_id,
            session_id=self.session_id,
            status=status,
            summary=self.final_content[-12_000:],
            completed_steps=tuple(self.completed_steps),
            failure_reason=reason,
        )


class CodingWorkflowGraphBridge:
    """Project the frozen Coding Workflow coordinator into the Phase 2 graph authority."""

    def __init__(self, runtime: GraphRuntime, graph_run_id: str, main_role_id: str | None) -> None:
        self.runtime = runtime
        self.graph_run_id = graph_run_id
        self.main_role_id = main_role_id
        self._attempts: dict[str, NodeAttempt] = {}
        self._attempt_sessions: dict[str, str] = {}

    @classmethod
    def create(
        cls,
        runtime: GraphRuntime,
        *,
        legacy_workflow_run_id: str,
        task: str,
        workspace: Path,
        planner_role_id: str,
        explorer_role_ids: tuple[str, ...],
        coder_role_id: str,
        reviewer_role_id: str,
        main_role_id: str | None,
        max_parallel_explorers: int,
        max_rework_rounds: int,
        resumed_from_id: str | None,
    ) -> CodingWorkflowGraphBridge:
        identity = hashlib.sha256(
            json.dumps(
                {
                    "compatibility": "phase2.coding-workflow.v1",
                    "planner": planner_role_id,
                    "explorers": explorer_role_ids,
                    "coder": coder_role_id,
                    "reviewer": reviewer_role_id,
                    "main": main_role_id,
                    "max_parallel_explorers": max_parallel_explorers,
                    "max_rework_rounds": max_rework_rounds,
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:16]
        compatibility_definition = coding_workflow_definition(
            planner_role_id=planner_role_id,
            explorer_role_ids=explorer_role_ids,
            coder_role_id=coder_role_id,
            reviewer_role_id=reviewer_role_id,
            main_role_id=main_role_id,
            max_parallel_explorers=max_parallel_explorers,
            max_rework_rounds=max_rework_rounds,
        )
        role_id_by_node = {
            "planner": planner_role_id,
            "coder": coder_role_id,
            "reviewer": reviewer_role_id,
            **({"main": main_role_id} if main_role_id is not None else {}),
        }
        compatibility_nodes = tuple(
            spec.model_copy(
                update={
                    "metadata": {
                        **spec.metadata,
                        "legacy_role": spec.node_id,
                        "role_id": role_id_by_node[spec.node_id],
                    }
                }
            )
            if spec.node_id in role_id_by_node
            else spec
            for spec in compatibility_definition.nodes
        )
        proposed = compatibility_definition.model_copy(
            update={
                "workflow_id": f"builtin.coding-review.{identity}",
                "status": WorkflowDefinitionStatus.PUBLISHED,
                "nodes": compatibility_nodes,
            }
        )
        try:
            definition = runtime.repository.get_definition(proposed.workflow_id, proposed.version)
        except KeyError:
            definition = proposed
        graph_run = runtime.create_run(
            definition,
            input={"task": task, "resumed_from_id": resumed_from_id},
            workspace_or_target=str(workspace),
            legacy_workflow_run_id=legacy_workflow_run_id,
        )
        runtime.start_run(graph_run.id)
        return cls(runtime, graph_run.id, main_role_id)

    def before_role(
        self,
        *,
        role: str,
        role_id: str,
        checkpoint: WorkflowSubtaskResult | None = None,
    ) -> None:
        node_id = self._node_id(role, role_id)
        node = self._node(node_id)
        if node.status is not NodeRunStatus.READY:
            raise RuntimeError(f"graph node {node_id} is not ready")
        self._attempt_sessions.pop(node_id, None)
        attempt_number = len(self.runtime.repository.list_attempts(node.id)) + 1
        attempt = self.runtime.start_attempt(
            node.id,
            idempotency_key=f"{self.graph_run_id}:{node_id}:{attempt_number}",
        )
        if role == "coder" and checkpoint is None:
            attempt = self.runtime.mark_side_effect_started(attempt.id)
        self._attempts[node_id] = attempt
        if checkpoint is not None:
            self._complete_role(role=role, role_id=role_id, result=checkpoint)

    def start_explorer_stage(self) -> None:
        self._complete_synthetic("explorer_fanout", {"dispatched": True})

    def finish_explorer_stage(self) -> None:
        self._complete_synthetic("explorer_join", {"joined": True})

    def select_verdict(self, verdict: str) -> None:
        node = self._node("verdict")
        normalized = verdict.lower()
        if node.status is NodeRunStatus.SUCCEEDED:
            if node.output_refs.get("selected_port") == normalized:
                return
            raise RuntimeError("graph verdict was already selected")
        self.runtime.select_condition(
            node.id,
            selected_port=normalized,
            output_refs={
                "selected_port": normalized,
                "verdict": normalized,
                "legacy_verdict": verdict,
            },
        )

    def observe(self, event: WorkflowEvent) -> None:
        self._bind_agent_instance(event)
        if event.event_type == "workflow.subtask_result":
            raw = event.payload.get("result")
            if isinstance(raw, dict):
                result = WorkflowSubtaskResult.model_validate(raw)
                self._complete_role(role=result.role, role_id=result.role_id, result=result)
            return
        if event.event_type == "workflow.rework_started":
            self.select_verdict("REWORK")
            loop = self._node("rework_loop")
            self.runtime.advance_loop(
                loop.id,
                continue_loop=True,
                progress_signature=f"rework:{event.payload.get('round', 0)}",
                output_refs={"round": event.payload.get("round")},
            )
            return
        if event.event_type == "workflow.review_verdict_missing":
            self.select_verdict("MISSING")
            self.runtime.fail_run(self.graph_run_id)
            return
        if event.event_type in {"workflow.failed", "workflow.rework_limit_reached"}:
            self.runtime.fail_run(self.graph_run_id)
            return
        if event.event_type == "workflow.completed" and self.main_role_id is None:
            self.select_verdict("APPROVED")
            self._complete_synthetic("main", {"verdict": event.payload.get("verdict")})

    def _bind_agent_instance(self, event: WorkflowEvent) -> None:
        """Bind only an AgentInstance proven by this role's persisted Session event."""
        if event.role_id is None or event.agent_id is None or not event.session_id:
            return
        try:
            node_id = self._node_id(event.role, event.role_id)
        except KeyError:
            return
        attempt = self._attempts.get(node_id)
        if attempt is None:
            return
        known_session = self._attempt_sessions.setdefault(node_id, event.session_id)
        if known_session != event.session_id:
            return
        if attempt.agent_instance_id is not None:
            return
        bound = attempt.model_copy(update={"agent_instance_id": event.agent_id})
        self.runtime.repository.update_attempt(bound)
        self._attempts[node_id] = bound

    def interrupt(self) -> None:
        self.runtime.interrupt_run(self.graph_run_id)

    def cancel(self) -> None:
        self.runtime.cancel_run(self.graph_run_id)

    def _complete_role(self, *, role: str, role_id: str, result: WorkflowSubtaskResult) -> None:
        node_id = self._node_id(role, role_id)
        attempt = self._attempts.pop(node_id, None)
        if attempt is None:
            node = self._node(node_id)
            if node.status in {
                NodeRunStatus.SUCCEEDED,
                NodeRunStatus.SKIPPED,
                NodeRunStatus.MANUAL_RECONCILE_REQUIRED,
            }:
                return
            raise RuntimeError(f"graph node {node_id} has no active attempt")
        effect = None
        if role == "coder":
            effect = (
                AttemptSideEffectState.COMMITTED
                if result.succeeded
                else AttemptSideEffectState.UNKNOWN
            )
        self.runtime.complete_attempt(
            attempt,
            succeeded=result.succeeded,
            output_refs={"subtask": result.model_dump(mode="json")},
            failure_class=result.failure_reason,
            side_effect_state=effect,
        )

    def _complete_synthetic(self, node_id: str, output: dict[str, Any]) -> None:
        node = self._node(node_id)
        if node.status is NodeRunStatus.SUCCEEDED:
            return
        if node.status is not NodeRunStatus.READY:
            raise RuntimeError(f"graph node {node_id} is not ready")
        attempt = self.runtime.start_attempt(node.id, worker_id="legacy-workflow-coordinator")
        self.runtime.complete_attempt(attempt, succeeded=True, output_refs=output)

    def _node_id(self, role: str, role_id: str) -> str:
        if role != "explorer":
            return role
        definition = self.runtime.repository.get_definition(
            self.runtime.repository.get_run(self.graph_run_id).workflow_definition_id,
            self.runtime.repository.get_run(self.graph_run_id).workflow_definition_version,
        )
        for spec in definition.nodes:
            if (
                spec.metadata.get("legacy_role") == "explorer"
                and spec.metadata.get("role_id") == role_id
            ):
                return spec.node_id
        raise KeyError(role_id)

    def _node(self, node_id: str) -> NodeRun:
        return next(
            node
            for node in self.runtime.repository.list_node_runs(self.graph_run_id)
            if node.node_id == node_id
        )


class SequentialCodingWorkflow:
    """Role-driven coding workflow with isolated sessions and read-only parallelism.

    The application workflow is the deterministic coordinator. It selects
    explicit Role Presets, keeps every child context isolated, and can ask a
    configured Main role for the final user-facing summary. Dynamic model-based
    routing is intentionally out of scope for the v1 workflow.
    """

    _MAX_EXPLORERS = 4

    def __init__(
        self, service: ApplicationService, graph_runtime: GraphRuntime | None = None
    ) -> None:
        self.service = service
        self.graph_runtime = graph_runtime or GraphRuntime(SQLiteGraphRepository(service.store))

    def validate_configuration(
        self,
        *,
        planner_role_id: str,
        explorer_role_ids: tuple[str, ...],
        coder_role_id: str,
        reviewer_role_id: str,
        max_parallel_explorers: int,
        main_role_id: str | None = None,
    ) -> None:
        if not 1 <= max_parallel_explorers <= self._MAX_EXPLORERS:
            raise ValueError(f"max_parallel_explorers must be between 1 and {self._MAX_EXPLORERS}")
        if len(explorer_role_ids) > self._MAX_EXPLORERS:
            raise ValueError(f"at most {self._MAX_EXPLORERS} explorer roles are allowed")
        if len(explorer_role_ids) != len(set(explorer_role_ids)):
            raise ValueError("explorer_role_ids must not contain duplicates")

        readonly_slots = {
            "planner": self.service.get_role(planner_role_id),
            "reviewer": self.service.get_role(reviewer_role_id),
        }
        if main_role_id is not None:
            readonly_slots["main"] = self.service.get_role(main_role_id)
        self.service.get_role(coder_role_id)
        for index, role_id in enumerate(explorer_role_ids, start=1):
            readonly_slots[f"explorer[{index}]"] = self.service.get_role(role_id)
        for slot, role in readonly_slots.items():
            self._require_readonly_role(slot, role)

    async def run(
        self,
        *,
        task: str,
        workspace: str | Path,
        planner_role_id: str,
        coder_role_id: str,
        reviewer_role_id: str,
        explorer_role_ids: tuple[str, ...] = (),
        max_parallel_explorers: int = 2,
        max_rework_rounds: int = 1,
        main_role_id: str | None = None,
        resumed_from_id: str | None = None,
        memory_enabled: bool = True,
        memory_project_scope: str | Path | None = None,
        persist_memory_candidates: bool = True,
        _checkpoint_results: tuple[WorkflowSubtaskResult, ...] = (),
    ) -> AsyncIterator[WorkflowEvent]:
        """Create, persist, and stream one workflow execution.

        SQLite is the source of truth. Every event is committed before it is
        yielded, so an SSE disconnect leaves a resumable stage checkpoint.
        """

        workspace_path = Path(workspace).resolve(strict=False)
        memory_scope_path = (
            workspace_path
            if memory_project_scope is None
            else Path(memory_project_scope).resolve(strict=False)
        )
        if not 0 <= max_rework_rounds <= 3:
            raise ValueError("max_rework_rounds must be between 0 and 3")
        self.validate_configuration(
            planner_role_id=planner_role_id,
            explorer_role_ids=explorer_role_ids,
            coder_role_id=coder_role_id,
            reviewer_role_id=reviewer_role_id,
            max_parallel_explorers=max_parallel_explorers,
            main_role_id=main_role_id,
        )
        run = self.service.create_workflow_run(
            WorkflowRun(
                task=task,
                workspace=str(workspace_path),
                main_role_id=main_role_id,
                planner_role_id=planner_role_id,
                explorer_role_ids=explorer_role_ids,
                coder_role_id=coder_role_id,
                reviewer_role_id=reviewer_role_id,
                max_parallel_explorers=max_parallel_explorers,
                max_rework_rounds=max_rework_rounds,
                resumed_from_id=resumed_from_id,
            )
        )
        execution_lease = self.service.acquire_workflow_execution_lease(run.id)
        if execution_lease is None:
            raise ConflictError("workflow already has an active coordinator")
        try:
            self.service.activate_workflow_run(execution_lease)
        except BaseException:
            self.service.release_workflow_execution_lease(execution_lease)
            raise

        try:
            graph_bridge = CodingWorkflowGraphBridge.create(
                self.graph_runtime,
                legacy_workflow_run_id=run.id,
                task=task,
                workspace=workspace_path,
                planner_role_id=planner_role_id,
                explorer_role_ids=explorer_role_ids,
                coder_role_id=coder_role_id,
                reviewer_role_id=reviewer_role_id,
                main_role_id=main_role_id,
                max_parallel_explorers=max_parallel_explorers,
                max_rework_rounds=max_rework_rounds,
                resumed_from_id=resumed_from_id,
            )
        except BaseException:
            self.service.update_workflow_run_if_status(
                run.id,
                expected_status=WorkflowRunStatus.RUNNING,
                status=WorkflowRunStatus.INTERRUPTED,
                last_error_type="graph_projection_initialization_failed",
            )
            self.service.release_workflow_execution_lease(execution_lease)
            raise

        terminal = False
        guard_lost = asyncio.Event()
        steps = self._run_steps(
            workflow_run_id=run.id,
            task=task,
            workspace=workspace_path,
            planner_role_id=planner_role_id,
            coder_role_id=coder_role_id,
            reviewer_role_id=reviewer_role_id,
            explorer_role_ids=explorer_role_ids,
            max_parallel_explorers=max_parallel_explorers,
            max_rework_rounds=max_rework_rounds,
            main_role_id=main_role_id,
            checkpoint_results=_checkpoint_results,
            resumed_from_id=resumed_from_id,
            memory_enabled=memory_enabled,
            memory_project_scope=memory_scope_path,
            graph_bridge=graph_bridge,
        )

        async def heartbeat_execution_lease() -> None:
            current = execution_lease
            while True:
                await asyncio.sleep(self.service.workflow_execution_heartbeat_seconds)
                renewed = self.service.renew_workflow_execution_lease(current)
                if renewed is None:
                    guard_lost.set()
                    return
                current = renewed

        heartbeat = asyncio.create_task(heartbeat_execution_lease())
        next_event: asyncio.Task[WorkflowEvent] | None = None
        lost_waiter: asyncio.Task[bool] | None = None
        try:
            while True:
                next_event = asyncio.create_task(anext(steps))
                lost_waiter = asyncio.create_task(guard_lost.wait())
                done, _ = await asyncio.wait(
                    {next_event, lost_waiter},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if lost_waiter in done and lost_waiter.result():
                    if not next_event.done():
                        next_event.cancel()
                    await asyncio.gather(next_event, return_exceptions=True)
                    self.service.cancel_workflow_children(run.id)
                    break
                lost_waiter.cancel()
                await asyncio.gather(lost_waiter, return_exceptions=True)
                try:
                    raw_event = next_event.result()
                except StopAsyncIteration:
                    break
                if self.service.get_workflow_run(run.id).status is WorkflowRunStatus.CANCELLED:
                    terminal = True
                    graph_bridge.cancel()
                    break
                if not self.service.verify_workflow_execution_lease(execution_lease):
                    guard_lost.set()
                    self.service.cancel_workflow_children(run.id)
                    break
                if raw_event.event_type == "workflow.completed" and persist_memory_candidates:
                    for memory_event in self._knowledge_candidate_events(run, raw_event):
                        persisted_memory_event = memory_event.model_copy(
                            update={"workflow_run_id": run.id}
                        )
                        persisted_memory_event = self._persist_workflow_event(
                            run.id, persisted_memory_event
                        )
                        yield persisted_memory_event
                event = raw_event.model_copy(update={"workflow_run_id": run.id})
                event = self._persist_workflow_event(run.id, event)
                graph_bridge.observe(event)
                transition_terminal = self._advance_workflow_run(run.id, event)
                terminal = transition_terminal or terminal
                yield event
                if transition_terminal:
                    break
        finally:
            for waiter in (next_event, lost_waiter):
                if waiter is not None and not waiter.done():
                    waiter.cancel()
            await asyncio.gather(
                *(waiter for waiter in (next_event, lost_waiter) if waiter is not None),
                return_exceptions=True,
            )
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            await steps.aclose()
            if guard_lost.is_set():
                self.service.cancel_workflow_children(run.id)
                self.service.interrupt_workflow_after_guard_loss(execution_lease)
            current = self.service.get_workflow_run(run.id)
            if (
                not terminal
                and current.status is WorkflowRunStatus.RUNNING
                and self.service.verify_workflow_execution_lease(execution_lease)
            ):
                self.service.update_workflow_run_if_status(
                    run.id,
                    expected_status=WorkflowRunStatus.RUNNING,
                    status=WorkflowRunStatus.INTERRUPTED,
                    last_error_type="stream_interrupted",
                )
                current = self.service.get_workflow_run(run.id)
            if current.status in {
                WorkflowRunStatus.INTERRUPTED,
                WorkflowRunStatus.MANUAL_RECONCILE_REQUIRED,
            }:
                graph_bridge.interrupt()
            self.service.release_workflow_execution_lease(execution_lease)

    async def resume(
        self,
        workflow_run_id: str,
        *,
        allow_coder_replay: bool = False,
    ) -> AsyncIterator[WorkflowEvent]:
        """Resume from committed role boundaries without replaying unknown writes."""

        original = self.service.get_workflow_run(workflow_run_id)
        if original.status not in {
            WorkflowRunStatus.INTERRUPTED,
            WorkflowRunStatus.MANUAL_RECONCILE_REQUIRED,
        }:
            raise ValueError("only interrupted workflows can be resumed")
        persisted = self._workflow_history(original)
        if self._unknown_coder_outcome(original, persisted) and not allow_coder_replay:
            updated = self.service.update_workflow_run_if_status(
                original.id,
                expected_status=original.status,
                status=WorkflowRunStatus.MANUAL_RECONCILE_REQUIRED,
                last_error_type="coder_outcome_unknown",
            )
            if updated is None:
                raise ConflictError("workflow status changed while preparing resume")
            raise ValueError(
                "coder outcome is unknown; inspect the workspace and resume with "
                "allow_coder_replay=True only after reconciliation"
            )
        checkpoints = self._checkpoint_results(persisted)
        if allow_coder_replay and original.current_stage is WorkflowStage.CODER:
            checkpoints = self._before_unknown_coder(checkpoints)
        async for event in self.run(
            task=original.task,
            workspace=original.workspace,
            main_role_id=original.main_role_id,
            planner_role_id=original.planner_role_id,
            explorer_role_ids=original.explorer_role_ids,
            coder_role_id=original.coder_role_id,
            reviewer_role_id=original.reviewer_role_id,
            max_parallel_explorers=original.max_parallel_explorers,
            max_rework_rounds=original.max_rework_rounds,
            resumed_from_id=original.id,
            _checkpoint_results=checkpoints,
        ):
            yield event

    async def _run_steps(
        self,
        *,
        workflow_run_id: str,
        task: str,
        workspace: str | Path,
        planner_role_id: str,
        coder_role_id: str,
        reviewer_role_id: str,
        explorer_role_ids: tuple[str, ...] = (),
        max_parallel_explorers: int = 2,
        max_rework_rounds: int = 1,
        main_role_id: str | None = None,
        checkpoint_results: tuple[WorkflowSubtaskResult, ...] = (),
        resumed_from_id: str | None = None,
        memory_enabled: bool = True,
        memory_project_scope: str | Path | None = None,
        graph_bridge: CodingWorkflowGraphBridge,
    ) -> AsyncGenerator[WorkflowEvent, None]:
        if not 0 <= max_rework_rounds <= 3:
            raise ValueError("max_rework_rounds must be between 0 and 3")
        self.validate_configuration(
            planner_role_id=planner_role_id,
            explorer_role_ids=explorer_role_ids,
            coder_role_id=coder_role_id,
            reviewer_role_id=reviewer_role_id,
            max_parallel_explorers=max_parallel_explorers,
            main_role_id=main_role_id,
        )
        yield WorkflowEvent(
            role="workflow",
            session_id="",
            event_type="workflow.started",
            payload={
                "planner_role_id": planner_role_id,
                "explorer_role_ids": list(explorer_role_ids),
                "coder_role_id": coder_role_id,
                "reviewer_role_id": reviewer_role_id,
                "main_role_id": main_role_id,
                "max_parallel_explorers": max_parallel_explorers,
                "resumed_from_id": resumed_from_id,
            },
        )

        results: list[WorkflowSubtaskResult] = []
        planner_result = self._checkpoint_for(checkpoint_results, "planner", planner_role_id)
        planner = self._capture_from_checkpoint(
            planner_result,
            role="planner",
            role_id=planner_role_id,
        )
        graph_bridge.before_role(role="planner", role_id=planner_role_id, checkpoint=planner_result)
        if planner_result is None:
            async for event in self._stream_role(
                workflow_run_id=workflow_run_id,
                message=task,
                workspace=workspace,
                capture=planner,
                memory_enabled=memory_enabled,
                memory_project_scope=memory_project_scope,
            ):
                yield event
            planner_result = planner.result()
        results.append(planner_result)
        if not planner_result.succeeded:
            yield self._workflow_failed(planner_result)
            return

        explorer_checkpoints = {
            result.role_id: result
            for result in checkpoint_results
            if result.role == "explorer" and result.role_id in explorer_role_ids
        }
        explorers = [
            self._capture_from_checkpoint(
                explorer_checkpoints.get(role_id),
                role="explorer",
                role_id=role_id,
            )
            for role_id in explorer_role_ids
        ]
        graph_bridge.start_explorer_stage()
        for capture in explorers:
            graph_bridge.before_role(
                role="explorer",
                role_id=capture.role_id,
                checkpoint=explorer_checkpoints.get(capture.role_id),
            )
        pending_explorers = [
            capture for capture in explorers if capture.role_id not in explorer_checkpoints
        ]
        if pending_explorers:
            explorer_message = self._explorer_message(
                task=task,
                planner_output=planner.final_content,
            )
            async with aclosing(
                self._stream_parallel_readonly_roles(
                    workflow_run_id=workflow_run_id,
                    captures=pending_explorers,
                    message=explorer_message,
                    workspace=workspace,
                    max_parallel=max_parallel_explorers,
                    memory_enabled=memory_enabled,
                    memory_project_scope=memory_project_scope,
                )
            ) as explorer_stream:
                async for event in explorer_stream:
                    yield event
        explorer_results = [capture.result() for capture in explorers]
        results.extend(explorer_results)
        graph_bridge.finish_explorer_stage()

        coder_result = self._checkpoint_for(checkpoint_results, "coder", coder_role_id)
        coder = self._capture_from_checkpoint(
            coder_result,
            role="coder",
            role_id=coder_role_id,
        )
        graph_bridge.before_role(role="coder", role_id=coder_role_id, checkpoint=coder_result)
        if coder_result is None:
            coder_message = self._coder_message(
                task=task,
                planner_output=planner.final_content,
                explorer_results=explorer_results,
            )
            async for event in self._stream_role(
                workflow_run_id=workflow_run_id,
                message=coder_message,
                workspace=workspace,
                capture=coder,
                memory_enabled=memory_enabled,
                memory_project_scope=memory_project_scope,
            ):
                yield event
            coder_result = coder.result()
        results.append(coder_result)
        if not coder_result.succeeded:
            yield self._workflow_failed(coder_result)
            return

        reviewer_result = self._checkpoint_after_latest(
            checkpoint_results,
            role="reviewer",
            role_id=reviewer_role_id,
            after_role="coder",
        )
        reviewer = self._capture_from_checkpoint(
            reviewer_result,
            role="reviewer",
            role_id=reviewer_role_id,
        )
        graph_bridge.before_role(
            role="reviewer", role_id=reviewer_role_id, checkpoint=reviewer_result
        )
        if reviewer_result is None:
            async for event in self._stream_role(
                workflow_run_id=workflow_run_id,
                message=self._reviewer_message(
                    task=task,
                    planner_output=planner.final_content,
                    explorer_results=explorer_results,
                    coder_output=coder.final_content,
                ),
                workspace=workspace,
                capture=reviewer,
                memory_enabled=memory_enabled,
                memory_project_scope=memory_project_scope,
            ):
                yield event
            reviewer_result = reviewer.result()
        results.append(reviewer_result)
        if not reviewer_result.succeeded:
            yield self._workflow_failed(reviewer_result)
            return

        verdict = self._review_verdict(reviewer.final_content)
        if verdict is None:
            yield WorkflowEvent(
                role="workflow",
                session_id=reviewer.session_id,
                event_type="workflow.review_verdict_missing",
                payload={"expected": "VERDICT: APPROVED or VERDICT: REWORK"},
            )
            return

        for round_number in range(1, max_rework_rounds + 1):
            if verdict != "REWORK":
                async for event in self._stream_completion(
                    workflow_run_id=workflow_run_id,
                    task=task,
                    verdict=verdict,
                    results=results,
                    main_role_id=main_role_id,
                    workspace=workspace,
                    memory_enabled=memory_enabled,
                    memory_project_scope=memory_project_scope,
                    main_checkpoint=self._checkpoint_after_latest(
                        checkpoint_results,
                        role="main",
                        role_id=main_role_id,
                        after_role="reviewer",
                    ),
                    graph_bridge=graph_bridge,
                ):
                    yield event
                return

            yield WorkflowEvent(
                role="workflow",
                session_id=reviewer.session_id,
                event_type="workflow.rework_started",
                payload={"round": round_number, "max_rework_rounds": max_rework_rounds},
            )
            previous_coder_output = coder.final_content
            coder = RoleRunCapture(role="coder", role_id=coder_role_id)
            graph_bridge.before_role(role="coder", role_id=coder_role_id)
            rework_message = (
                f"原始任务：\n{task}\n\nPlanner 输出：\n{planner.final_content}\n\n"
                f"Explorer 结构化结果：\n{self._results_json(explorer_results)}\n\n"
                f"上一轮 Coder 输出：\n{previous_coder_output}\n\n"
                f"Reviewer 反馈：\n{reviewer.final_content}\n\n"
                "请只处理 Reviewer 明确指出的问题，在 workspace 中完成返工，"
                "运行相关测试并检查 Git diff。"
            )
            async for event in self._stream_role(
                workflow_run_id=workflow_run_id,
                message=rework_message,
                workspace=workspace,
                capture=coder,
                memory_enabled=memory_enabled,
                memory_project_scope=memory_project_scope,
            ):
                yield event
            coder_result = coder.result()
            results.append(coder_result)
            if not coder_result.succeeded:
                yield self._workflow_failed(coder_result)
                return

            reviewer = RoleRunCapture(role="reviewer", role_id=reviewer_role_id)
            graph_bridge.before_role(role="reviewer", role_id=reviewer_role_id)
            async for event in self._stream_role(
                workflow_run_id=workflow_run_id,
                message=self._reviewer_message(
                    task=task,
                    planner_output=planner.final_content,
                    explorer_results=explorer_results,
                    coder_output=coder.final_content,
                ),
                workspace=workspace,
                capture=reviewer,
                memory_enabled=memory_enabled,
                memory_project_scope=memory_project_scope,
            ):
                yield event
            reviewer_result = reviewer.result()
            results.append(reviewer_result)
            if not reviewer_result.succeeded:
                yield self._workflow_failed(reviewer_result)
                return
            verdict = self._review_verdict(reviewer.final_content)
            if verdict is None:
                yield WorkflowEvent(
                    role="workflow",
                    session_id=reviewer.session_id,
                    event_type="workflow.review_verdict_missing",
                    payload={"expected": "VERDICT: APPROVED or VERDICT: REWORK"},
                )
                return

        if verdict == "REWORK":
            yield WorkflowEvent(
                role="workflow",
                session_id=reviewer.session_id,
                event_type="workflow.rework_limit_reached",
                payload={"max_rework_rounds": max_rework_rounds},
            )
            return
        async for event in self._stream_completion(
            workflow_run_id=workflow_run_id,
            task=task,
            verdict=verdict,
            results=results,
            main_role_id=main_role_id,
            workspace=workspace,
            memory_enabled=memory_enabled,
            memory_project_scope=memory_project_scope,
            main_checkpoint=None,
            graph_bridge=graph_bridge,
        ):
            yield event

    @staticmethod
    def _explorer_message(*, task: str, planner_output: str) -> str:
        return (
            f"原始任务：\n{task}\n\nPlanner 输出：\n{planner_output}\n\n"
            "请只读检索 workspace。依据你的角色专长，返回与任务直接相关的文件、符号、"
            "调用链、编码约定、风险和可核对证据；不要修改文件，也不要重复 Planner 的一般性描述。"
        )

    @classmethod
    def _coder_message(
        cls,
        *,
        task: str,
        planner_output: str,
        explorer_results: list[WorkflowSubtaskResult],
    ) -> str:
        return (
            f"原始任务：\n{task}\n\nPlanner 输出：\n{planner_output}\n\n"
            f"Explorer 结构化结果：\n{cls._results_json(explorer_results)}\n\n"
            "请在 workspace 中完成任务并运行相关测试；如果这是 Git workspace，完成前必须执行 "
            "`git diff --check` 并修复它报告的格式问题。"
        )

    @classmethod
    def _reviewer_message(
        cls,
        *,
        task: str,
        planner_output: str,
        explorer_results: list[WorkflowSubtaskResult],
        coder_output: str,
    ) -> str:
        return (
            f"原始任务：\n{task}\n\nPlanner 输出：\n{planner_output}\n\n"
            f"Explorer 结构化结果：\n{cls._results_json(explorer_results)}\n\n"
            f"Coder 输出：\n{coder_output}\n\n"
            "请只读检查 workspace 的 Git diff 和测试结果，不要修改文件。"
            "最终必须单独以 `VERDICT: APPROVED` 或 `VERDICT: REWORK` 结束；"
            "只有确实需要 Coder 继续修改时才使用 REWORK。"
        )

    @staticmethod
    def _review_verdict(content: str) -> str | None:
        for line in reversed(content.splitlines()):
            normalized = line.strip().upper()
            if normalized == "VERDICT: APPROVED":
                return "APPROVED"
            if normalized == "VERDICT: REWORK":
                return "REWORK"
        return None

    async def _stream_parallel_readonly_roles(
        self,
        *,
        workflow_run_id: str,
        captures: list[RoleRunCapture],
        message: str,
        workspace: str | Path,
        max_parallel: int,
        memory_enabled: bool,
        memory_project_scope: str | Path | None,
    ) -> AsyncGenerator[WorkflowEvent, None]:
        queue: asyncio.Queue[tuple[int, WorkflowEvent | None]] = asyncio.Queue()
        semaphore = asyncio.Semaphore(max_parallel)

        async def consume(index: int, capture: RoleRunCapture) -> None:
            try:
                async with semaphore:
                    async for event in self._stream_role(
                        workflow_run_id=workflow_run_id,
                        message=message,
                        workspace=workspace,
                        capture=capture,
                        memory_enabled=memory_enabled,
                        memory_project_scope=memory_project_scope,
                    ):
                        await queue.put((index, event))
            finally:
                await queue.put((index, None))

        tasks = [
            asyncio.create_task(consume(index, capture)) for index, capture in enumerate(captures)
        ]
        remaining = len(tasks)
        try:
            while remaining:
                _, event = await queue.get()
                if event is None:
                    remaining -= 1
                else:
                    yield event
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _stream_completion(
        self,
        *,
        workflow_run_id: str,
        task: str,
        verdict: str,
        results: list[WorkflowSubtaskResult],
        main_role_id: str | None,
        workspace: str | Path,
        memory_enabled: bool,
        memory_project_scope: str | Path | None,
        main_checkpoint: WorkflowSubtaskResult | None,
        graph_bridge: CodingWorkflowGraphBridge,
    ) -> AsyncGenerator[WorkflowEvent, None]:
        graph_bridge.select_verdict("APPROVED")
        if main_role_id is not None:
            main = self._capture_from_checkpoint(
                main_checkpoint,
                role="main",
                role_id=main_role_id,
            )
            graph_bridge.before_role(role="main", role_id=main_role_id, checkpoint=main_checkpoint)
            if main_checkpoint is None:
                message = (
                    f"原始任务：\n{task}\n\nReviewer 结论：{verdict}\n\n"
                    f"全部子任务结构化结果：\n{self._results_json(results)}\n\n"
                    "请面向用户汇总实际完成内容、验证证据、失败或限制和下一步。"
                    "不要修改 workspace，也不要把未验证事项描述为成功。"
                )
                async for event in self._stream_role(
                    workflow_run_id=workflow_run_id,
                    message=message,
                    workspace=workspace,
                    capture=main,
                    memory_enabled=memory_enabled,
                    memory_project_scope=memory_project_scope,
                ):
                    yield event
                main_result = main.result()
            else:
                main_result = main_checkpoint
            results.append(main_result)
            if not main_result.succeeded:
                yield self._workflow_failed(main_result)
                return
        yield self._workflow_completed(results, verdict=verdict)

    async def _stream_role(
        self,
        *,
        workflow_run_id: str,
        message: str,
        workspace: str | Path,
        capture: RoleRunCapture,
        memory_enabled: bool = True,
        memory_project_scope: str | Path | None = None,
    ) -> AsyncGenerator[WorkflowEvent, None]:
        try:
            if self.service.get_workflow_run(workflow_run_id).status is WorkflowRunStatus.CANCELLED:
                capture.status = AgentStatus.CANCELLED
                capture.failure_reason = "workflow cancelled"
                return
            execution_lease = self.service.admitted_workflow_execution_lease(workflow_run_id)
            if execution_lease is None or not self.service.verify_workflow_execution_lease(
                execution_lease
            ):
                raise ConflictError("workflow execution lease is expired, cancelled, or fenced")
            session = self.service.create_session(capture.role_id)
            capture.session_id = session.id
            memory_context = (
                self._memory_context(
                    session_id=session.id,
                    workspace=(workspace if memory_project_scope is None else memory_project_scope),
                )
                if memory_enabled
                else ""
            )
            if memory_context:
                message = f"{message}\n\n可复用的已确认项目知识：\n{memory_context}"
            async for event in self.service.run_session(
                session.id,
                user_message=message,
                workspace=workspace,
                workflow_run_id=workflow_run_id,
                workflow_execution_lease=execution_lease,
            ):
                capture.observe(event.event_type, event.payload)
                agent_id = self._persisted_runtime_agent_id(session.id, event.cursor)
                yield WorkflowEvent(
                    role=capture.role,
                    role_id=capture.role_id,
                    agent_id=agent_id,
                    session_id=session.id,
                    event_type=event.event_type,
                    payload=event.payload,
                )
        except Exception as exc:
            capture.status = AgentStatus.FAILED
            capture.failure_reason = type(exc).__name__
        yield WorkflowEvent(
            role=capture.role,
            role_id=capture.role_id,
            session_id=capture.session_id,
            event_type="workflow.subtask_result",
            payload={"result": capture.result().model_dump(mode="json")},
        )

    def _persisted_runtime_agent_id(self, session_id: str, cursor: int | None) -> str | None:
        if cursor is None:
            return None
        events = self.service.list_events(session_id, after_cursor=cursor - 1, limit=1)
        if not events or events[0].cursor != cursor:
            return None
        return events[0].agent_id

    @staticmethod
    def _require_readonly_role(slot: str, role: RolePreset) -> None:
        policy = role.tool_policy
        write_tools = {"apply_patch", "run_command"}.intersection(policy.allowed_tools)
        if policy.workspace_write or policy.command_execution or write_tools:
            raise ValueError(f"{slot} role must be read-only: {role.id}")

    @staticmethod
    def _results_json(results: list[WorkflowSubtaskResult]) -> str:
        if not results:
            return "[]"
        return "[\n" + ",\n".join(result.model_dump_json(indent=2) for result in results) + "\n]"

    @staticmethod
    def _workflow_failed(result: WorkflowSubtaskResult) -> WorkflowEvent:
        return WorkflowEvent(
            role="workflow",
            session_id=result.session_id,
            event_type="workflow.failed",
            payload={"failed_subtask": result.model_dump(mode="json")},
        )

    @staticmethod
    def _workflow_completed(results: list[WorkflowSubtaskResult], *, verdict: str) -> WorkflowEvent:
        return WorkflowEvent(
            role="workflow",
            session_id=results[-1].session_id,
            event_type="workflow.completed",
            payload={
                "verdict": verdict,
                "subtasks": [result.model_dump(mode="json") for result in results],
            },
        )

    def _persist_workflow_event(self, workflow_run_id: str, event: WorkflowEvent) -> WorkflowEvent:
        persisted = self.service.append_workflow_event(
            WorkflowRunEvent(
                workflow_run_id=workflow_run_id,
                role=event.role,
                session_id=event.session_id or None,
                event_type=event.event_type,
                payload=event.payload,
            )
        )
        return event.model_copy(update={"cursor": persisted.cursor})

    def _advance_workflow_run(self, workflow_run_id: str, event: WorkflowEvent) -> bool:
        if event.event_type == "workflow.completed":
            self.service.update_workflow_run_if_status(
                workflow_run_id,
                expected_status=WorkflowRunStatus.RUNNING,
                status=WorkflowRunStatus.COMPLETED,
                current_stage=WorkflowStage.COMPLETED,
                final_verdict=str(event.payload.get("verdict", "")) or None,
                last_error_type=None,
            )
            return True
        if event.event_type in {
            "workflow.failed",
            "workflow.review_verdict_missing",
            "workflow.rework_limit_reached",
        }:
            self.service.update_workflow_run_if_status(
                workflow_run_id,
                expected_status=WorkflowRunStatus.RUNNING,
                status=WorkflowRunStatus.FAILED,
                last_error_type=event.event_type.removeprefix("workflow."),
            )
            return True
        stage_by_role = {
            "planner": WorkflowStage.PLANNER,
            "explorer": WorkflowStage.EXPLORERS,
            "coder": WorkflowStage.CODER,
            "reviewer": WorkflowStage.REVIEWER,
            "main": WorkflowStage.MAIN,
        }
        stage = stage_by_role.get(event.role)
        if stage is not None:
            updated = self.service.update_workflow_run_if_status(
                workflow_run_id,
                expected_status=WorkflowRunStatus.RUNNING,
                status=WorkflowRunStatus.RUNNING,
                current_stage=stage,
                last_error_type=None,
            )
            return updated is None
        return False

    def _workflow_history(self, run: WorkflowRun) -> list[WorkflowRunEvent]:
        chain: list[WorkflowRun] = []
        current: WorkflowRun | None = run
        seen: set[str] = set()
        while current is not None and current.id not in seen:
            seen.add(current.id)
            chain.append(current)
            current = (
                None
                if current.resumed_from_id is None
                else self.service.get_workflow_run(current.resumed_from_id)
            )
        history: list[WorkflowRunEvent] = []
        for item in reversed(chain):
            history.extend(self.service.list_workflow_events(item.id))
        return history

    @staticmethod
    def _checkpoint_results(
        events: list[WorkflowRunEvent],
    ) -> tuple[WorkflowSubtaskResult, ...]:
        results: list[WorkflowSubtaskResult] = []
        for event in events:
            if event.event_type != "workflow.subtask_result":
                continue
            raw = event.payload.get("result")
            if not isinstance(raw, dict):
                continue
            try:
                result = WorkflowSubtaskResult.model_validate(raw)
            except ValueError:
                continue
            if result.succeeded or result.role == "explorer":
                results.append(result)
        return tuple(results)

    @staticmethod
    def _checkpoint_for(
        checkpoints: tuple[WorkflowSubtaskResult, ...],
        role: str,
        role_id: str | None,
    ) -> WorkflowSubtaskResult | None:
        if role_id is None:
            return None
        for result in reversed(checkpoints):
            if result.role == role and result.role_id == role_id:
                return result
        return None

    @staticmethod
    def _checkpoint_after_latest(
        checkpoints: tuple[WorkflowSubtaskResult, ...],
        *,
        role: str,
        role_id: str | None,
        after_role: str,
    ) -> WorkflowSubtaskResult | None:
        if role_id is None:
            return None
        latest_boundary = -1
        for index, result in enumerate(checkpoints):
            if result.role == after_role:
                latest_boundary = index
        for result in reversed(checkpoints[latest_boundary + 1 :]):
            if result.role == role and result.role_id == role_id:
                return result
        return None

    @staticmethod
    def _capture_from_checkpoint(
        checkpoint: WorkflowSubtaskResult | None,
        *,
        role: str,
        role_id: str,
    ) -> RoleRunCapture:
        if checkpoint is None:
            return RoleRunCapture(role=role, role_id=role_id)
        return RoleRunCapture(
            role=role,
            role_id=role_id,
            final_content=checkpoint.summary,
            session_id=checkpoint.session_id,
            status=checkpoint.status,
            completed_steps=list(checkpoint.completed_steps),
            failure_reason=checkpoint.failure_reason,
        )

    @staticmethod
    def _unknown_coder_outcome(
        run: WorkflowRun,
        events: list[WorkflowRunEvent],
    ) -> bool:
        if run.current_stage is not WorkflowStage.CODER:
            return False
        for event in reversed(events):
            if event.role != "coder":
                continue
            if event.event_type != "workflow.subtask_result":
                return True
            raw = event.payload.get("result")
            return not (
                isinstance(raw, dict)
                and raw.get("status") in {AgentStatus.COMPLETED.value, AgentStatus.FAILED.value}
            )
        return False

    @staticmethod
    def _before_unknown_coder(
        checkpoints: tuple[WorkflowSubtaskResult, ...],
    ) -> tuple[WorkflowSubtaskResult, ...]:
        last_reviewer = -1
        for index, result in enumerate(checkpoints):
            if result.role == "reviewer":
                last_reviewer = index
        if last_reviewer >= 0:
            return checkpoints[: last_reviewer + 1]
        return tuple(result for result in checkpoints if result.role != "coder")

    def _memory_context(
        self,
        *,
        session_id: str,
        workspace: str | Path,
    ) -> str:
        if self.service.memory_plugin_mode:
            # MP-2 explicit management does not activate the old automatic policy.
            return ""
        session = self.service.get_session(session_id)
        try:
            memories = self.service.query_memories(
                "",
                snapshot=session.role_snapshot,
                session_id=session_id,
                project_scope=str(Path(workspace).resolve(strict=False)),
                kinds=(MemoryKind.PROJECT,),
                limit=5,
            )
        except (PermissionError, ValueError):
            return ""
        return "\n".join(
            f"- [{memory.kind.value}] {memory.content[:1_500]}" for memory in memories
        )[:6_000]

    def _knowledge_candidate_events(
        self,
        run: WorkflowRun,
        completed_event: WorkflowEvent,
    ) -> list[WorkflowEvent]:
        if self.service.memory_plugin_mode:
            # MP-2 explicit management does not activate the old automatic policy.
            return []
        raw_subtasks = completed_event.payload.get("subtasks")
        if not isinstance(raw_subtasks, list):
            return []
        coder_result: WorkflowSubtaskResult | None = None
        for raw in reversed(raw_subtasks):
            if not isinstance(raw, dict) or raw.get("role") != "coder":
                continue
            try:
                candidate = WorkflowSubtaskResult.model_validate(raw)
            except ValueError:
                continue
            if candidate.succeeded:
                coder_result = candidate
                break
        if coder_result is None or not coder_result.session_id:
            return []

        session = self.service.get_session(coder_result.session_id)
        events: list[WorkflowEvent] = []
        for raw in raw_subtasks:
            if not isinstance(raw, dict) or raw.get("role") != "explorer":
                continue
            try:
                explorer_result = WorkflowSubtaskResult.model_validate(raw)
            except ValueError:
                continue
            summary = explorer_result.summary.strip()
            if not explorer_result.succeeded or not explorer_result.session_id or not summary:
                continue
            try:
                memory = self.service.save_memory(
                    snapshot=session.role_snapshot,
                    session_id=session.id,
                    kind=MemoryKind.PROJECT,
                    content=(
                        f"项目结构与编码约定候选（{explorer_result.role_id}）：{summary[-2_000:]}"
                    ),
                    project_scope=run.workspace,
                    source_session_id=explorer_result.session_id,
                    source_task=run.task,
                    confidence=0.5,
                )
            except (PermissionError, ValueError):
                continue
            events.append(
                WorkflowEvent(
                    role="workflow",
                    session_id=explorer_result.session_id,
                    event_type="workflow.memory_candidate",
                    payload={
                        "memory_id": memory.id,
                        "kind": memory.kind.value,
                        "status": memory.status.value,
                        "source_session_id": explorer_result.session_id,
                    },
                )
            )
        for command in self._verified_commands(coder_result.session_id):
            try:
                memory = self.service.save_memory(
                    snapshot=session.role_snapshot,
                    session_id=session.id,
                    kind=MemoryKind.PROJECT,
                    content=f"验证命令：{command}",
                    project_scope=run.workspace,
                    source_session_id=session.id,
                    source_task=run.task,
                    confidence=0.98,
                    allow_conservative_activation=True,
                )
            except (PermissionError, ValueError):
                continue
            events.append(
                WorkflowEvent(
                    role="workflow",
                    session_id=session.id,
                    event_type="workflow.memory_candidate",
                    payload={
                        "memory_id": memory.id,
                        "kind": memory.kind.value,
                        "status": memory.status.value,
                        "source_session_id": session.id,
                    },
                )
            )

        summary = coder_result.summary.strip()
        if summary:
            try:
                memory = self.service.save_memory(
                    snapshot=session.role_snapshot,
                    session_id=session.id,
                    kind=MemoryKind.EPISODIC,
                    content=f"任务实现摘要：{summary[-2_000:]}",
                    source_session_id=session.id,
                    source_task=run.task,
                    confidence=0.5,
                )
            except (PermissionError, ValueError):
                pass
            else:
                events.append(
                    WorkflowEvent(
                        role="workflow",
                        session_id=session.id,
                        event_type="workflow.memory_candidate",
                        payload={
                            "memory_id": memory.id,
                            "kind": memory.kind.value,
                            "status": memory.status.value,
                            "source_session_id": session.id,
                        },
                    )
                )
        return events

    def _verified_commands(self, session_id: str) -> tuple[str, ...]:
        pending: dict[str, str] = {}
        verified: list[str] = []
        for event in self.service.list_events(session_id):
            if event.event_type == "model.completed":
                tool_calls = event.payload.get("tool_calls")
                if not isinstance(tool_calls, list):
                    continue
                for raw in tool_calls:
                    if not isinstance(raw, dict) or raw.get("name") != "run_command":
                        continue
                    call_id = raw.get("id")
                    arguments_json = raw.get("arguments_json")
                    if not isinstance(call_id, str) or not isinstance(arguments_json, str):
                        continue
                    try:
                        arguments = json.loads(arguments_json)
                    except json.JSONDecodeError:
                        continue
                    argv = arguments.get("argv") if isinstance(arguments, dict) else None
                    if self._is_safe_verification_argv(argv):
                        assert isinstance(argv, list)
                        pending[call_id] = shlex.join(str(token) for token in argv)
            elif event.event_type == "tool.completed":
                call_id = event.payload.get("tool_call_id")
                if not isinstance(call_id, str) or call_id not in pending:
                    continue
                try:
                    result = json.loads(str(event.payload.get("result", "")))
                except json.JSONDecodeError:
                    continue
                if isinstance(result, dict) and result.get("exit_code") == 0:
                    command = pending[call_id]
                    if command not in verified:
                        verified.append(command)
        return tuple(verified)

    @staticmethod
    def _is_safe_verification_argv(value: Any) -> bool:
        if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
            return False
        if any(
            len(token) > 200
            or "\n" in token
            or token.startswith(("http://", "https://"))
            or re.search(r"(?i)(?:key|token|password|secret)=", token)
            for token in value
        ):
            return False
        prefixes = (
            ("pytest",),
            ("python", "-m", "unittest"),
            ("python3", "-m", "unittest"),
            ("uv", "run", "pytest"),
            ("uv", "run", "ruff"),
            ("uv", "run", "mypy"),
            ("npm", "test"),
            ("cargo", "test"),
            ("go", "test"),
        )
        return any(tuple(value[: len(prefix)]) == prefix for prefix in prefixes)
