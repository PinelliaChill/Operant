from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI

from operant.application.graph import GraphRuntime
from operant.application.scheduler import SchedulerConflictError, TriggerService
from operant.application.security import ActionNormalizer, CapabilityBroker, PolicyEngine
from operant.domain.graph import NodeKind, NodeSpec, WorkflowDefinition, WorkflowDefinitionStatus
from operant.domain.scheduler import (
    DispatchIdempotency,
    RunRequest,
    RunRequestStatus,
    ScheduleDefinition,
    SchedulerLease,
    TriggerKind,
)
from operant.domain.security import PolicyBundle, PolicyDecision
from operant.persistence.graph_team import SQLiteGraphRepository
from operant.persistence.scheduler import SQLiteSchedulerStore
from operant.persistence.security import SQLiteSecurityRepository
from operant.persistence.sqlite import SQLiteStore
from operant.runtime.scheduler import DispatchError, SchedulerWorker, WorkflowDispatch
from operant.runtime.scheduler_integration import (
    GraphSchedulerActionGateway,
    SchedulerCoordinator,
    SchedulerCycleResult,
    SchedulerSecurityService,
    SQLiteGraphDispatchRegistry,
    fastapi_scheduler_lifespan,
)

UTC = timezone.utc


class _NoWorkWorker:
    def __init__(self, dispatches: int = 0) -> None:
        self.remaining = dispatches
        self.calls = 0

    def run_one(self, _writer_lease: SchedulerLease) -> RunRequest | None:
        self.calls += 1
        if self.remaining == 0:
            return None
        self.remaining -= 1
        return object()  # type: ignore[return-value]


def _scheduler_store(tmp_path: Path) -> SQLiteSchedulerStore:
    store = SQLiteSchedulerStore(tmp_path / "scheduler.sqlite3")
    store.initialize()
    return store


def test_coordinator_cycle_is_bounded_and_releases_leases_for_restart(tmp_path: Path) -> None:
    store = _scheduler_store(tmp_path)
    first_worker = _NoWorkWorker(dispatches=20)
    first = SchedulerCoordinator(
        store=store,
        trigger_service=TriggerService(store),
        worker=first_worker,
        owner="core-one",
        max_dispatches_per_cycle=3,
    )

    assert first.run_cycle() == SchedulerCycleResult(materialized=0, dispatched=3)
    assert first_worker.calls == 3
    first.release_leases()

    restarted = SchedulerCoordinator(
        store=store,
        trigger_service=TriggerService(store),
        worker=_NoWorkWorker(),
        owner="core-two",
    )
    assert restarted.run_cycle() == SchedulerCycleResult(materialized=0, dispatched=0)
    restarted.release_leases()


@pytest.mark.asyncio
async def test_fastapi_lifespan_starts_and_stops_without_real_sleep(tmp_path: Path) -> None:
    store = _scheduler_store(tmp_path)
    waiter_calls = 0

    async def stop_immediately(event, _interval: float) -> None:
        nonlocal waiter_calls
        waiter_calls += 1
        event.set()

    coordinator = SchedulerCoordinator(
        store=store,
        trigger_service=TriggerService(store),
        worker=_NoWorkWorker(),
        owner="lifespan-core",
        waiter=stop_immediately,
    )
    lifespan = fastapi_scheduler_lifespan(coordinator)

    async with lifespan(FastAPI()):
        await coordinator._task

    assert waiter_calls == 1
    assert coordinator.running is False
    store.acquire_authority(
        "scheduler_leader", owner="after-stop", ttl_seconds=5, now=datetime.now(UTC)
    )


def test_lost_lease_fences_coordinator_and_preserves_new_owner(tmp_path: Path) -> None:
    store = _scheduler_store(tmp_path)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    coordinator = SchedulerCoordinator(
        store=store,
        trigger_service=TriggerService(store),
        worker=_NoWorkWorker(),
        owner="old-core",
        lease_ttl_seconds=5,
        clock=lambda: now,
    )
    coordinator.run_cycle()
    takeover_at = now + timedelta(seconds=5)
    new_leader = store.acquire_authority(
        "scheduler_leader", owner="new-core", ttl_seconds=10, now=takeover_at
    )
    new_writer = store.acquire_authority(
        "runtime_writer", owner="new-core", ttl_seconds=10, now=takeover_at
    )
    coordinator.clock = lambda: takeover_at

    with pytest.raises(SchedulerConflictError, match="lost or expired"):
        coordinator.run_cycle()
    coordinator.release_leases()

    store.renew_authority(new_leader, ttl_seconds=10, now=takeover_at)
    store.renew_authority(new_writer, ttl_seconds=10, now=takeover_at)


class _RecordingGateway:
    def __init__(self) -> None:
        self.actions: list[WorkflowDispatch] = []

    def dispatch_workflow(self, action: WorkflowDispatch) -> str:
        self.actions.append(action)
        return "graph_recovered"


def test_coordinator_recovers_expired_job_after_crash_without_duplicate_dispatch(
    tmp_path: Path,
) -> None:
    store = _scheduler_store(tmp_path)
    before_crash = datetime.now(UTC) - timedelta(minutes=1)
    schedule = ScheduleDefinition(
        id="future-timer",
        name="future timer",
        trigger_kind=TriggerKind.TIMER,
        timer_at=before_crash + timedelta(days=1),
        timezone_name="UTC",
        workflow_id="scheduled.graph",
        workflow_version=2,
        dispatch_idempotency=DispatchIdempotency.IDEMPOTENT,
        created_at=before_crash,
    )
    service = TriggerService(store)
    service.create_schedule(schedule)
    queued = service.manual_trigger("future-timer", idempotency_key="crash", now=before_crash)
    crashed_writer = store.acquire_authority(
        "runtime_writer", owner="crashed", ttl_seconds=5, now=before_crash
    )
    store.claim_due(
        crashed_writer, owner="crashed", ttl_seconds=5, now=before_crash
    ) or pytest.fail("crashed worker did not claim request")
    gateway = _RecordingGateway()
    coordinator = SchedulerCoordinator(
        store=store,
        trigger_service=service,
        worker=SchedulerWorker(store=store, gateway=gateway, owner="restarted"),
        owner="restarted",
    )

    cycle = coordinator.run_cycle()
    coordinator.release_leases()

    assert cycle == SchedulerCycleResult(materialized=0, dispatched=1)
    assert len(gateway.actions) == 1
    recovered = store.get_request(queued.id)
    assert recovered.status is RunRequestStatus.SUCCEEDED
    assert recovered.attempt_count == 2


def _definition(*, status: WorkflowDefinitionStatus = WorkflowDefinitionStatus.PUBLISHED):
    return WorkflowDefinition(
        workflow_id="scheduled.graph",
        version=2,
        name="scheduled graph",
        nodes=(NodeSpec(node_id="artifact", node_kind=NodeKind.ARTIFACT),),
        status=status,
    )


def _graph_gateway(
    tmp_path: Path,
    *,
    decision: PolicyDecision,
    definition_status: WorkflowDefinitionStatus = WorkflowDefinitionStatus.PUBLISHED,
) -> tuple[GraphSchedulerActionGateway, SQLiteGraphRepository, SQLiteSecurityRepository]:
    core_store = SQLiteStore(tmp_path / "core.sqlite3")
    core_store.initialize()
    graph_repository = SQLiteGraphRepository(core_store)
    graph_repository.put_definition(_definition(status=definition_status))
    security_repository = SQLiteSecurityRepository(core_store)
    security = SchedulerSecurityService(
        repository=security_repository,
        normalizer=ActionNormalizer(),
        policy_engine=PolicyEngine(
            PolicyBundle(
                bundle_id="scheduler-test",
                version=f"scheduler-test.{decision.value}",
                default_decision=decision,
                rules=(),
            )
        ),
        capability_broker=CapabilityBroker(security_repository),
    )
    registry = SQLiteGraphDispatchRegistry(tmp_path / "dispatch.sqlite3")
    registry.initialize()
    runtime = GraphRuntime(graph_repository)
    return (
        GraphSchedulerActionGateway(
            graph_repository=graph_repository,
            graph_runtime=runtime,
            security=security,
            dispatch_registry=registry,
        ),
        graph_repository,
        security_repository,
    )


def _dispatch() -> WorkflowDispatch:
    return WorkflowDispatch(
        workflow_id="scheduled.graph",
        workflow_version=2,
        workflow_input={},
        idempotency_key="schedule:one:v1:2026-01-01T00:00:00Z",
        source_run_request_id="request_one",
    )


def test_graph_gateway_authorizes_persists_starts_and_deduplicates(tmp_path: Path) -> None:
    gateway, graph_repository, security_repository = _graph_gateway(
        tmp_path, decision=PolicyDecision.ALLOW
    )

    first = gateway.dispatch_workflow(_dispatch())
    restarted_gateway = GraphSchedulerActionGateway(
        graph_repository=graph_repository,
        graph_runtime=gateway.graph_runtime,
        security=gateway.security,
        dispatch_registry=SQLiteGraphDispatchRegistry(tmp_path / "dispatch.sqlite3"),
    )
    second = restarted_gateway.dispatch_workflow(_dispatch())

    assert second == first
    assert graph_repository.get_run(first).status.value == "running"
    with graph_repository.store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM graph_workflow_runs").fetchone()[0] == 1
        action_hash = connection.execute(
            "SELECT action_hash FROM security_action_requests WHERE idempotency_key=?",
            (_dispatch().idempotency_key,),
        ).fetchone()[0]
    event_types = {
        event.event_type for event in security_repository.list_security_audit(action_hash)
    }
    assert {
        "scheduler.dispatch.policy_evaluated",
        "scheduler.dispatch.capability_consumed",
        "scheduler.dispatch.completed",
    }.issubset(event_types)


@pytest.mark.parametrize("decision", [PolicyDecision.DENY, PolicyDecision.ASK])
def test_policy_deny_and_ask_fail_closed_before_graph_creation(
    tmp_path: Path, decision: PolicyDecision
) -> None:
    gateway, graph_repository, _security_repository = _graph_gateway(tmp_path, decision=decision)

    with pytest.raises(DispatchError) as raised:
        gateway.dispatch_workflow(_dispatch())

    assert raised.value.error_code == f"scheduler.policy_{decision.value}"
    assert raised.value.outcome_unknown is False
    with graph_repository.store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM graph_workflow_runs").fetchone()[0] == 0


def test_unknown_graph_creation_is_not_dispatched_twice(tmp_path: Path) -> None:
    gateway, graph_repository, _security_repository = _graph_gateway(
        tmp_path,
        decision=PolicyDecision.ALLOW,
        definition_status=WorkflowDefinitionStatus.DRAFT,
    )

    with pytest.raises(DispatchError) as first:
        gateway.dispatch_workflow(_dispatch())
    with pytest.raises(DispatchError) as replay:
        gateway.dispatch_workflow(_dispatch())

    assert first.value.outcome_unknown is True
    assert replay.value.error_code == "scheduler.graph_dispatch_outcome_unknown"
    with graph_repository.store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM graph_workflow_runs").fetchone()[0] == 0
