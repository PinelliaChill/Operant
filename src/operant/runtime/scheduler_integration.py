from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from collections.abc import Awaitable, Callable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from fastapi import FastAPI

from operant.application.graph import GraphRepository, GraphRuntime
from operant.application.scheduler import SchedulerConflictError, TriggerService
from operant.application.security import ActionNormalizer, CapabilityBroker, PolicyEngine
from operant.domain.graph import GraphRunStatus, WorkflowDefinitionStatus
from operant.domain.scheduler import RunRequest, SchedulerLease, require_aware_utc
from operant.domain.security import (
    ActionRequest,
    Capability,
    PolicyDecision,
    PolicyEvaluation,
    SecurityAuditEvent,
)
from operant.persistence.scheduler import SQLiteSchedulerStore
from operant.runtime.scheduler import (
    DispatchError,
    SchedulerActionGateway,
    WorkflowDispatch,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SecurityAuditRepository(Protocol):
    def record_security_action(self, action: ActionRequest) -> ActionRequest: ...

    def append_security_audit(self, event: SecurityAuditEvent) -> SecurityAuditEvent: ...


@dataclass(frozen=True)
class AuthorizedSchedulerAction:
    action: ActionRequest
    evaluation: PolicyEvaluation


class SchedulerSecurityService:
    """Phase 4 composition boundary for unattended Scheduler dispatch.

    Scheduler dispatch never waits for an approval reviewer. Both DENY and ASK are
    terminal for the current attempt, so a background loop cannot silently approve
    itself or create a GraphRun before authorization is complete.
    """

    def __init__(
        self,
        *,
        repository: SecurityAuditRepository,
        normalizer: ActionNormalizer,
        policy_engine: PolicyEngine,
        capability_broker: CapabilityBroker,
        principal: str = "scheduler.local",
        capability_ttl_seconds: int = 60,
    ) -> None:
        if not principal:
            raise ValueError("scheduler principal must not be empty")
        if capability_ttl_seconds < 1:
            raise ValueError("capability TTL must be positive")
        self.repository = repository
        self.normalizer = normalizer
        self.policy_engine = policy_engine
        self.capability_broker = capability_broker
        self.principal = principal
        self.capability_ttl_seconds = capability_ttl_seconds

    def authorize(self, dispatch: WorkflowDispatch) -> AuthorizedSchedulerAction:
        capability = Capability.PROCESS_EXEC_NO_NETWORK
        action = self.normalizer.normalize(
            principal=self.principal,
            tool="scheduler",
            operation="dispatch",
            arguments={
                "workflow_id": dispatch.workflow_id,
                "workflow_version": dispatch.workflow_version,
                "workflow_input": dispatch.workflow_input,
                "source_run_request_id": dispatch.source_run_request_id,
            },
            requested_capabilities=(capability,),
            idempotency_key=dispatch.idempotency_key,
            policy_version=self.policy_engine.bundle.version,
            workspace_id=f"workflow:{dispatch.workflow_id}:v{dispatch.workflow_version}",
            sandbox_profile="local-core",
            network_profile="none",
        )
        action = self.repository.record_security_action(action)
        evaluation = self.policy_engine.evaluate(action)
        self.repository.append_security_audit(
            SecurityAuditEvent(
                action_hash=action.action_hash,
                principal=action.principal,
                event_type="scheduler.dispatch.policy_evaluated",
                decision=evaluation.decision,
                rule_ids=evaluation.matched_rule_ids,
                detail={
                    "reason_code": evaluation.reason_code,
                    "source_run_request_id": dispatch.source_run_request_id,
                },
            )
        )
        if evaluation.decision is not PolicyDecision.ALLOW:
            self.repository.append_security_audit(
                SecurityAuditEvent(
                    action_hash=action.action_hash,
                    principal=action.principal,
                    event_type="scheduler.dispatch.blocked",
                    decision=evaluation.decision,
                    rule_ids=evaluation.matched_rule_ids,
                    detail={"fail_closed": True},
                )
            )
            raise DispatchError(
                f"scheduler.policy_{evaluation.decision.value}", outcome_unknown=False
            )
        lease = self.capability_broker.issue(
            action,
            evaluation,
            capability,
            issued_by=self.principal,
            ttl_seconds=self.capability_ttl_seconds,
        )
        consumed = self.capability_broker.consume(
            lease.lease_id, action=action, capability=capability
        )
        self.repository.append_security_audit(
            SecurityAuditEvent(
                action_hash=action.action_hash,
                principal=action.principal,
                event_type="scheduler.dispatch.capability_consumed",
                decision=PolicyDecision.ALLOW,
                rule_ids=evaluation.matched_rule_ids,
                detail={
                    "capability": capability.value,
                    "lease_id": consumed.lease_id,
                    "uses": consumed.uses,
                },
            )
        )
        return AuthorizedSchedulerAction(action=action, evaluation=evaluation)


class DispatchBindingStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"


@dataclass(frozen=True)
class DispatchBinding:
    source_run_request_id: str
    idempotency_key: str
    action_hash: str
    status: DispatchBindingStatus
    graph_run_id: str | None = None


class GraphDispatchRegistry(Protocol):
    def reserve(
        self, *, source_run_request_id: str, idempotency_key: str, action_hash: str
    ) -> tuple[DispatchBinding, bool]: ...

    def complete(self, binding: DispatchBinding, *, graph_run_id: str) -> DispatchBinding: ...


_DISPATCH_BINDING_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduler_graph_dispatches (
    source_run_request_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    action_hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK(status IN ('pending','completed')),
    graph_run_id TEXT UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(
      (status='pending' AND graph_run_id IS NULL)
      OR (status='completed' AND graph_run_id IS NOT NULL)
    )
);
"""


class SQLiteGraphDispatchRegistry:
    """Small durable idempotency registry; the migration owner may adopt its DDL."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)

    def initialize(self) -> None:
        with self._transaction() as connection:
            connection.execute(_DISPATCH_BINDING_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def reserve(
        self, *, source_run_request_id: str, idempotency_key: str, action_hash: str
    ) -> tuple[DispatchBinding, bool]:
        now = utc_now().isoformat()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM scheduler_graph_dispatches "
                "WHERE source_run_request_id=? OR idempotency_key=? OR action_hash=?",
                (source_run_request_id, idempotency_key, action_hash),
            ).fetchone()
            if row is not None:
                binding = self._binding(row)
                if (
                    binding.source_run_request_id != source_run_request_id
                    or binding.idempotency_key != idempotency_key
                    or binding.action_hash != action_hash
                ):
                    raise SchedulerConflictError(
                        "scheduler graph dispatch idempotency binding conflict"
                    )
                return binding, False
            connection.execute(
                "INSERT INTO scheduler_graph_dispatches VALUES (?,?,?,'pending',NULL,?,?)",
                (source_run_request_id, idempotency_key, action_hash, now, now),
            )
        return (
            DispatchBinding(
                source_run_request_id=source_run_request_id,
                idempotency_key=idempotency_key,
                action_hash=action_hash,
                status=DispatchBindingStatus.PENDING,
            ),
            True,
        )

    def complete(self, binding: DispatchBinding, *, graph_run_id: str) -> DispatchBinding:
        with self._transaction() as connection:
            changed = connection.execute(
                "UPDATE scheduler_graph_dispatches SET status='completed', graph_run_id=?, "
                "updated_at=? WHERE source_run_request_id=? AND idempotency_key=? "
                "AND action_hash=? AND status='pending'",
                (
                    graph_run_id,
                    utc_now().isoformat(),
                    binding.source_run_request_id,
                    binding.idempotency_key,
                    binding.action_hash,
                ),
            ).rowcount
            row = connection.execute(
                "SELECT * FROM scheduler_graph_dispatches WHERE source_run_request_id=?",
                (binding.source_run_request_id,),
            ).fetchone()
            if row is None:
                raise SchedulerConflictError("scheduler graph dispatch binding disappeared")
            completed = self._binding(row)
            if changed != 1 and (
                completed.status is not DispatchBindingStatus.COMPLETED
                or completed.graph_run_id != graph_run_id
            ):
                raise SchedulerConflictError("scheduler graph dispatch completion conflict")
        return completed

    @staticmethod
    def _binding(row: sqlite3.Row) -> DispatchBinding:
        return DispatchBinding(
            source_run_request_id=str(row["source_run_request_id"]),
            idempotency_key=str(row["idempotency_key"]),
            action_hash=str(row["action_hash"]),
            status=DispatchBindingStatus(str(row["status"])),
            graph_run_id=None if row["graph_run_id"] is None else str(row["graph_run_id"]),
        )


class GraphSchedulerActionGateway(SchedulerActionGateway):
    """Authorize and idempotently create/start a durable GraphRun."""

    def __init__(
        self,
        *,
        graph_repository: GraphRepository,
        graph_runtime: GraphRuntime,
        security: SchedulerSecurityService,
        dispatch_registry: GraphDispatchRegistry,
        workspace_resolver: Callable[[WorkflowDispatch], str | None] | None = None,
    ) -> None:
        if graph_runtime.repository is not graph_repository:
            raise ValueError("graph runtime and repository must share one authority")
        self.graph_repository = graph_repository
        self.graph_runtime = graph_runtime
        self.security = security
        self.dispatch_registry = dispatch_registry
        self.workspace_resolver = workspace_resolver or (lambda _dispatch: None)

    def dispatch_workflow(self, action: WorkflowDispatch) -> str:
        # Definition existence, publication, compilation/input validity and policy
        # are deterministic failures. They must not leave a pending dispatch.
        try:
            definition = self.graph_repository.get_definition(
                action.workflow_id, action.workflow_version
            )
        except KeyError as exc:
            raise DispatchError("scheduler.workflow_not_found", outcome_unknown=False) from exc
        if definition.status is not WorkflowDefinitionStatus.PUBLISHED:
            raise DispatchError("scheduler.workflow_not_published", outcome_unknown=False)
        try:
            self.graph_runtime.validate_run(definition, input=dict(action.workflow_input))
        except Exception as exc:
            raise DispatchError(
                f"scheduler.workflow_invalid.{type(exc).__name__}", outcome_unknown=False
            ) from exc
        authorized = self.security.authorize(action)
        graph_run_id = self._graph_run_id(
            action.source_run_request_id, authorized.action.action_hash
        )
        binding, created = self.dispatch_registry.reserve(
            source_run_request_id=action.source_run_request_id,
            idempotency_key=action.idempotency_key,
            action_hash=authorized.action.action_hash,
        )
        if not created:
            if binding.status is DispatchBindingStatus.COMPLETED and binding.graph_run_id:
                self.graph_repository.get_run(binding.graph_run_id)
                return binding.graph_run_id
            # A deterministic GraphRun ID makes the crash window recoverable.
            # Missing means the create transaction did not commit, so the same
            # idempotent binding can safely continue instead of becoming unknown.
            try:
                recovered = self.graph_repository.get_run(graph_run_id)
            except KeyError:
                recovered = None
            if recovered is not None:
                if (
                    recovered.workflow_definition_id != action.workflow_id
                    or recovered.workflow_definition_version != action.workflow_version
                    or recovered.input != action.workflow_input
                ):
                    raise DispatchError(
                        "scheduler.graph_dispatch_binding_conflict", outcome_unknown=True
                    )
                if recovered.status in {
                    GraphRunStatus.CREATED,
                    GraphRunStatus.QUEUED,
                    GraphRunStatus.INTERRUPTED,
                }:
                    recovered = self.graph_runtime.start_run(recovered.id)
                completed = self.dispatch_registry.complete(binding, graph_run_id=recovered.id)
                assert completed.graph_run_id is not None
                return completed.graph_run_id
        try:
            run = self.graph_runtime.create_run(
                definition,
                input=dict(action.workflow_input),
                workspace_or_target=self.workspace_resolver(action),
                run_id=graph_run_id,
            )
            started = self.graph_runtime.start_run(run.id)
            completed = self.dispatch_registry.complete(binding, graph_run_id=started.id)
        except DispatchError:
            raise
        except Exception as exc:
            self.security.repository.append_security_audit(
                SecurityAuditEvent(
                    action_hash=authorized.action.action_hash,
                    principal=authorized.action.principal,
                    event_type="scheduler.dispatch.outcome_unknown",
                    decision=PolicyDecision.ALLOW,
                    rule_ids=authorized.evaluation.matched_rule_ids,
                    detail={"error_code": f"graph.{type(exc).__name__}"},
                )
            )
            raise DispatchError(f"graph.{type(exc).__name__}", outcome_unknown=True) from exc
        self.security.repository.append_security_audit(
            SecurityAuditEvent(
                action_hash=authorized.action.action_hash,
                principal=authorized.action.principal,
                event_type="scheduler.dispatch.completed",
                decision=PolicyDecision.ALLOW,
                rule_ids=authorized.evaluation.matched_rule_ids,
                detail={"graph_run_id": completed.graph_run_id},
            )
        )
        assert completed.graph_run_id is not None
        return completed.graph_run_id

    def workflow_status(self, workflow_run_id: str) -> GraphRunStatus:
        return self.graph_repository.get_run(workflow_run_id).status

    @staticmethod
    def _graph_run_id(source_run_request_id: str, action_hash: str) -> str:
        digest = hashlib.sha256(f"{source_run_request_id}\0{action_hash}".encode()).hexdigest()
        return f"graph_run_scheduler_{digest}"


@dataclass(frozen=True)
class SchedulerCycleResult:
    materialized: int
    dispatched: int


Waiter = Callable[[asyncio.Event, float], Awaitable[None]]


class SchedulerWorkerPort(Protocol):
    def run_one(self, writer_lease: SchedulerLease) -> RunRequest | None: ...


async def _wait_for_stop(stop_event: asyncio.Event, interval_seconds: float) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
    except TimeoutError:
        return


class SchedulerCoordinator:
    """Bounded single-process loop suitable for composition into FastAPI lifespan."""

    def __init__(
        self,
        *,
        store: SQLiteSchedulerStore,
        trigger_service: TriggerService,
        worker: SchedulerWorkerPort,
        owner: str,
        interval_seconds: float = 1.0,
        lease_ttl_seconds: int = 30,
        max_dispatches_per_cycle: int = 32,
        clock: Callable[[], datetime] = utc_now,
        waiter: Waiter = _wait_for_stop,
    ) -> None:
        if not owner:
            raise ValueError("scheduler coordinator owner must not be empty")
        if interval_seconds <= 0:
            raise ValueError("scheduler interval must be positive")
        if lease_ttl_seconds < 2:
            raise ValueError("scheduler lease TTL must be at least two seconds")
        if not 1 <= max_dispatches_per_cycle <= 10_000:
            raise ValueError("max dispatches per cycle is outside its allowed range")
        self.store = store
        self.trigger_service = trigger_service
        self.worker = worker
        self.owner = owner
        self.interval_seconds = interval_seconds
        self.lease_ttl_seconds = lease_ttl_seconds
        self.max_dispatches_per_cycle = max_dispatches_per_cycle
        self.clock = clock
        self.waiter = waiter
        self.stop_event = asyncio.Event()
        self._leader_lease: SchedulerLease | None = None
        self._writer_lease: SchedulerLease | None = None
        self._task: asyncio.Task[None] | None = None
        self.last_error_code: str | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self.stop_event.clear()
        self._task = asyncio.create_task(self.run(), name=f"operant-scheduler:{self.owner}")

    async def stop(self) -> None:
        self.stop_event.set()
        task = self._task
        if task is not None:
            await task
        self._task = None
        self.release_leases()

    async def run(self) -> None:
        try:
            while not self.stop_event.is_set():
                try:
                    self.run_cycle()
                    self.last_error_code = None
                except Exception as exc:
                    self.last_error_code = f"scheduler.{type(exc).__name__}"
                    self.release_leases()
                await self.waiter(self.stop_event, self.interval_seconds)
        finally:
            self.release_leases()

    def run_cycle(self) -> SchedulerCycleResult:
        now = require_aware_utc(self.clock(), field="scheduler clock")
        leader, writer = self._ensure_leases(now)
        materialized = self.trigger_service.materialize_all_due(leader_lease=leader, now=now)
        dispatched = 0
        for _ in range(self.max_dispatches_per_cycle):
            request: RunRequest | None = self.worker.run_one(writer)
            if request is None:
                break
            dispatched += 1
        return SchedulerCycleResult(materialized=len(materialized), dispatched=dispatched)

    def _ensure_leases(self, now: datetime) -> tuple[SchedulerLease, SchedulerLease]:
        if self._leader_lease is None or self._writer_lease is None:
            leader = self.store.acquire_authority(
                "scheduler_leader",
                owner=self.owner,
                ttl_seconds=self.lease_ttl_seconds,
                now=now,
            )
            try:
                writer = self.store.acquire_authority(
                    "runtime_writer",
                    owner=self.owner,
                    ttl_seconds=self.lease_ttl_seconds,
                    now=now,
                )
            except BaseException:
                self._release_lease(leader)
                raise
            self._leader_lease, self._writer_lease = leader, writer
            return leader, writer
        self._leader_lease = self.store.renew_authority(
            self._leader_lease, ttl_seconds=self.lease_ttl_seconds, now=now
        )
        self._writer_lease = self.store.renew_authority(
            self._writer_lease, ttl_seconds=self.lease_ttl_seconds, now=now
        )
        return self._leader_lease, self._writer_lease

    def release_leases(self) -> None:
        """Release only this coordinator's exact fenced authority leases."""
        for lease in (self._writer_lease, self._leader_lease):
            if lease is not None:
                self._release_lease(lease)
        self._writer_lease = None
        self._leader_lease = None

    def _release_lease(self, lease: SchedulerLease) -> None:
        connection = sqlite3.connect(self.store.database_path, timeout=30, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM scheduler_authority_leases "
                "WHERE kind=? AND owner=? AND token=? AND fencing=?",
                (lease.kind, lease.owner, lease.token, lease.fencing),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


def fastapi_scheduler_lifespan(
    coordinator: SchedulerCoordinator,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """Return a lifespan callable for the public API owner to compose/install."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> Any:
        await coordinator.start()
        try:
            yield
        finally:
            await coordinator.stop()

    return lifespan
