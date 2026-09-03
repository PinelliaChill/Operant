from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

from operant.application.scheduler import CronExpression, SchedulerConflictError, TriggerService
from operant.domain.scheduler import (
    AttemptStatus,
    DispatchIdempotency,
    MisfirePolicy,
    RunRequestStatus,
    ScheduleDefinition,
    SchedulerLease,
    ScheduleStatus,
    TriggerKind,
)
from operant.persistence.scheduler import SQLiteSchedulerStore
from operant.runtime.scheduler import DispatchError, SchedulerWorker, WorkflowDispatch

UTC = timezone.utc


def _at(hour: int = 0, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 1, 1, hour, minute, second, tzinfo=UTC)


def _schedule(
    *,
    schedule_id: str = "schedule_test",
    trigger_kind: TriggerKind = TriggerKind.CRON,
    cron_expression: str | None = "* * * * *",
    timer_at: datetime | None = None,
    misfire_policy: MisfirePolicy = MisfirePolicy.FIRE_ONCE,
    max_catch_up: int = 10,
    concurrency_limit: int = 1,
    max_attempts: int = 3,
    dispatch_idempotency: DispatchIdempotency = DispatchIdempotency.IDEMPOTENT,
) -> ScheduleDefinition:
    return ScheduleDefinition(
        id=schedule_id,
        name="test schedule",
        trigger_kind=trigger_kind,
        cron_expression=cron_expression,
        timer_at=timer_at,
        timezone_name="UTC",
        misfire_policy=misfire_policy,
        max_catch_up=max_catch_up,
        workflow_id="workflow.published",
        workflow_version=4,
        workflow_input={"task": "bounded"},
        concurrency_limit=concurrency_limit,
        max_attempts=max_attempts,
        retry_base_seconds=5,
        retry_max_seconds=20,
        dispatch_idempotency=dispatch_idempotency,
        created_at=_at(),
    )


@pytest.fixture
def store(tmp_path: Path) -> SQLiteSchedulerStore:
    value = SQLiteSchedulerStore(tmp_path / "scheduler.sqlite3")
    value.initialize()
    return value


def _leader(store: SQLiteSchedulerStore, *, now: datetime | None = None) -> SchedulerLease:
    return store.acquire_authority(
        "scheduler_leader", owner="scheduler-a", ttl_seconds=3600, now=now or _at()
    )


def _writer(store: SQLiteSchedulerStore, *, now: datetime | None = None) -> SchedulerLease:
    return store.acquire_authority(
        "runtime_writer", owner="runtime-a", ttl_seconds=3600, now=now or _at()
    )


def test_cron_uses_iana_timezone_and_handles_dst_gap_and_fold() -> None:
    expression = CronExpression("30 2 * * *")
    spring = expression.occurrences_between(
        start_exclusive=datetime(2026, 3, 8, 6, 0, tzinfo=UTC),
        end_inclusive=datetime(2026, 3, 8, 8, 0, tzinfo=UTC),
        timezone_name="America/New_York",
        limit=10,
    )
    assert spring == ()

    expression = CronExpression("30 1 * * *")
    fall = expression.occurrences_between(
        start_exclusive=datetime(2026, 11, 1, 4, 0, tzinfo=UTC),
        end_inclusive=datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
        timezone_name="America/New_York",
        limit=10,
    )
    assert fall == (
        datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
        datetime(2026, 11, 1, 6, 30, tzinfo=UTC),
    )

    with pytest.raises(ValueError, match="IANA timezone"):
        _schedule().model_copy(update={"timezone_name": "not/a-zone"}).model_validate(
            {**_schedule().model_dump(), "timezone_name": "not/a-zone"}
        )


@pytest.mark.parametrize(
    ("policy", "expected_minutes"),
    [
        (MisfirePolicy.SKIP, [3]),
        (MisfirePolicy.FIRE_ONCE, [3]),
        (MisfirePolicy.CATCH_UP, [1, 2, 3]),
    ],
)
def test_misfire_policy_materializes_durable_idempotent_requests(
    store: SQLiteSchedulerStore,
    policy: MisfirePolicy,
    expected_minutes: list[int],
) -> None:
    service = TriggerService(store)
    service.create_schedule(_schedule(misfire_policy=policy))
    leader = _leader(store)

    first = service.materialize_due("schedule_test", leader_lease=leader, now=_at(minute=3))
    second = service.materialize_due("schedule_test", leader_lease=leader, now=_at(minute=3))

    assert [request.occurrence_at.minute for request in first] == expected_minutes
    assert second == ()
    assert all(request.status is RunRequestStatus.QUEUED for request in first)
    assert len({request.idempotency_key for request in first}) == len(first)


def test_catch_up_keeps_latest_bounded_window_after_long_outage(
    store: SQLiteSchedulerStore,
) -> None:
    service = TriggerService(store)
    service.create_schedule(_schedule(misfire_policy=MisfirePolicy.CATCH_UP, max_catch_up=2))

    requests = service.materialize_due(
        "schedule_test", leader_lease=_leader(store), now=_at(minute=10)
    )

    assert [request.occurrence_at.minute for request in requests] == [9, 10]


def test_timer_is_one_shot_and_all_schedule_tick_requires_live_leader(
    store: SQLiteSchedulerStore,
) -> None:
    service = TriggerService(store)
    service.create_schedule(
        _schedule(
            trigger_kind=TriggerKind.TIMER,
            cron_expression=None,
            timer_at=_at(minute=1),
        )
    )
    leader = _leader(store)
    assert len(service.materialize_all_due(leader_lease=leader, now=_at(minute=2))) == 1
    assert service.materialize_all_due(leader_lease=leader, now=_at(minute=3)) == ()

    stale = leader.model_copy(update={"token": "x" * 24})
    with pytest.raises(SchedulerConflictError, match="lost or expired"):
        service.materialize_due("schedule_test", leader_lease=stale, now=_at(minute=4))


def test_authority_takeover_fences_old_owner(store: SQLiteSchedulerStore) -> None:
    first = store.acquire_authority("runtime_writer", owner="runtime-a", ttl_seconds=5, now=_at())
    with pytest.raises(SchedulerConflictError, match="live owner"):
        store.acquire_authority(
            "runtime_writer", owner="runtime-b", ttl_seconds=5, now=_at(second=1)
        )
    second = store.acquire_authority(
        "runtime_writer", owner="runtime-b", ttl_seconds=5, now=_at(second=5)
    )
    assert second.fencing == first.fencing + 1
    with pytest.raises(SchedulerConflictError, match="lost or expired"):
        store.renew_authority(first, ttl_seconds=5, now=_at(second=5))


def test_job_renewal_is_fenced_and_capped_by_runtime_writer_lease(
    store: SQLiteSchedulerStore,
) -> None:
    service = TriggerService(store)
    service.create_schedule(_schedule())
    service.manual_trigger("schedule_test", idempotency_key="renew", now=_at())
    writer = store.acquire_authority("runtime_writer", owner="runtime-a", ttl_seconds=10, now=_at())
    _, job = store.claim_due(writer, owner="runtime-a", ttl_seconds=5, now=_at()) or pytest.fail(
        "request was not claimed"
    )

    renewed = store.renew_job(job, writer, ttl_seconds=30, now=_at(second=4))
    assert renewed.expires_at == writer.expires_at
    stale_writer = writer.model_copy(update={"token": "x" * 24})
    with pytest.raises(SchedulerConflictError, match="lost or expired"):
        store.renew_job(renewed, stale_writer, ttl_seconds=5, now=_at(second=5))
    with pytest.raises(SchedulerConflictError, match="lost or expired"):
        store.complete_job(renewed, workflow_run_id="too-late", now=_at(second=10))


def test_atomic_claim_enforces_concurrency_limit(store: SQLiteSchedulerStore) -> None:
    service = TriggerService(store)
    service.create_schedule(_schedule(concurrency_limit=1))
    service.manual_trigger("schedule_test", idempotency_key="one", now=_at())
    service.manual_trigger("schedule_test", idempotency_key="two", now=_at())
    writer = _writer(store)

    def claim() -> tuple[object, object] | None:
        return store.claim_due(writer, owner="runtime-a", ttl_seconds=60, now=_at())

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _: claim(), range(2)))
    assert sum(result is not None for result in results) == 1


def test_retry_backoff_reaches_dead_letter_and_explicit_replay_is_idempotent(
    store: SQLiteSchedulerStore,
) -> None:
    service = TriggerService(store)
    service.create_schedule(_schedule(max_attempts=2))
    request = service.manual_trigger("schedule_test", idempotency_key="initial", now=_at())
    writer = _writer(store)

    first_request, first_lease = store.claim_due(
        writer, owner="runtime-a", ttl_seconds=60, now=_at()
    ) or pytest.fail("first request was not claimed")
    assert first_request.id == request.id
    first = store.fail_job(
        first_lease,
        error_code="provider.busy",
        outcome_unknown=False,
        retry_base_seconds=5,
        retry_max_seconds=20,
        now=_at(),
    )
    assert first.status is RunRequestStatus.RETRY_WAIT
    assert first.available_at == _at(second=5)
    assert store.claim_due(writer, owner="runtime-a", ttl_seconds=60, now=_at(second=4)) is None

    _, second_lease = store.claim_due(
        writer, owner="runtime-a", ttl_seconds=60, now=_at(second=5)
    ) or pytest.fail("retry was not claimed")
    dead = store.fail_job(
        second_lease,
        error_code="provider.busy",
        outcome_unknown=False,
        retry_base_seconds=5,
        retry_max_seconds=20,
        now=_at(second=5),
    )
    assert dead.status is RunRequestStatus.DEAD_LETTER
    assert [attempt.status for attempt in store.list_attempts(dead.id)] == [
        AttemptStatus.FAILED,
        AttemptStatus.FAILED,
    ]

    replay = service.replay_dead_letter(
        dead.id, idempotency_key="operator-ticket-7", now=_at(second=10)
    )
    duplicate = service.replay_dead_letter(
        dead.id, idempotency_key="operator-ticket-7", now=_at(second=11)
    )
    assert replay.id == duplicate.id
    assert replay.replay_of_request_id == dead.id
    assert replay.attempt_count == 0


def test_request_keeps_retry_policy_from_pinned_schedule_revision(
    store: SQLiteSchedulerStore,
) -> None:
    service = TriggerService(store)
    original = _schedule(max_attempts=2)
    service.create_schedule(original)
    service.manual_trigger("schedule_test", idempotency_key="pinned-v1", now=_at())
    service.create_schedule(
        original.model_copy(
            update={
                "version": 2,
                "retry_base_seconds": 20,
                "retry_max_seconds": 20,
                "created_at": _at(second=1),
            }
        )
    )
    writer = _writer(store)
    _, lease = store.claim_due(
        writer, owner="runtime-a", ttl_seconds=60, now=_at(second=1)
    ) or pytest.fail("request was not claimed")
    pinned = store.get_schedule_revision("schedule_test", 1)

    result = store.fail_job(
        lease,
        error_code="temporary",
        outcome_unknown=False,
        retry_base_seconds=pinned.retry_base_seconds,
        retry_max_seconds=pinned.retry_max_seconds,
        now=_at(second=1),
    )

    assert result.schedule_version == 1
    assert result.available_at == _at(second=6)


@pytest.mark.parametrize(
    ("idempotency", "expected_status", "expect_reclaim"),
    [
        (DispatchIdempotency.IDEMPOTENT, RunRequestStatus.LEASED, True),
        (
            DispatchIdempotency.NON_IDEMPOTENT,
            RunRequestStatus.MANUAL_RECONCILE_REQUIRED,
            False,
        ),
    ],
)
def test_expired_job_recovery_only_replays_safe_dispatch(
    store: SQLiteSchedulerStore,
    idempotency: DispatchIdempotency,
    expected_status: RunRequestStatus,
    expect_reclaim: bool,
) -> None:
    service = TriggerService(store)
    service.create_schedule(_schedule(dispatch_idempotency=idempotency))
    request = service.manual_trigger("schedule_test", idempotency_key="crash", now=_at())
    writer = _writer(store)
    _, stale_lease = store.claim_due(
        writer, owner="runtime-a", ttl_seconds=5, now=_at()
    ) or pytest.fail("request was not claimed")
    store.mark_side_effect_started(stale_lease, now=_at(second=1))

    reclaimed = store.claim_due(writer, owner="runtime-a", ttl_seconds=5, now=_at(second=5))
    assert (reclaimed is not None) is expect_reclaim
    assert store.get_request(request.id).status is expected_status
    with pytest.raises(SchedulerConflictError, match="lost or expired"):
        store.complete_job(stale_lease, workflow_run_id="run_stale", now=_at(second=5))


def test_cancel_pause_resume_and_manual_trigger_deduplicate(store: SQLiteSchedulerStore) -> None:
    service = TriggerService(store)
    service.create_schedule(_schedule())
    paused = service.set_status("schedule_test", ScheduleStatus.PAUSED, expected_version=1)
    assert paused.status is ScheduleStatus.PAUSED
    assert service.materialize_all_due(leader_lease=_leader(store), now=_at(minute=2)) == ()

    resumed = service.set_status("schedule_test", ScheduleStatus.ENABLED, expected_version=1)
    assert resumed.status is ScheduleStatus.ENABLED
    first = service.manual_trigger("schedule_test", idempotency_key="button-1", now=_at())
    second = service.manual_trigger("schedule_test", idempotency_key="button-1", now=_at(second=1))
    assert first.id == second.id
    assert store.request_cancel(first.id).status is RunRequestStatus.CANCELLED

    service.set_status("schedule_test", ScheduleStatus.CANCELLED, expected_version=1)
    with pytest.raises(SchedulerConflictError, match="cancelled"):
        service.manual_trigger("schedule_test", idempotency_key="after-cancel", now=_at())


class _Gateway:
    def __init__(self, *, failure: DispatchError | None = None) -> None:
        self.failure = failure
        self.actions: list[WorkflowDispatch] = []

    def dispatch_workflow(self, action: WorkflowDispatch) -> str:
        self.actions.append(action)
        if self.failure is not None:
            raise self.failure
        return "workflow_run_1"


def test_worker_dispatches_only_through_gateway_and_records_workflow_run(
    store: SQLiteSchedulerStore,
) -> None:
    now = datetime.now(UTC)
    schedule = _schedule().model_copy(update={"created_at": now})
    service = TriggerService(store)
    service.create_schedule(schedule)
    request = service.manual_trigger("schedule_test", idempotency_key="gateway", now=now)
    writer = store.acquire_authority(
        "runtime_writer", owner="runtime-live", ttl_seconds=60, now=now
    )
    gateway = _Gateway()

    result = SchedulerWorker(
        store=store, gateway=gateway, owner="runtime-live", job_ttl_seconds=30
    ).run_one(writer)

    assert result is not None and result.status is RunRequestStatus.SUCCEEDED
    assert result.workflow_run_id == "workflow_run_1"
    assert gateway.actions == [
        WorkflowDispatch(
            workflow_id="workflow.published",
            workflow_version=4,
            workflow_input={"task": "bounded"},
            idempotency_key=request.idempotency_key,
            source_run_request_id=request.id,
        )
    ]


def test_unknown_non_idempotent_gateway_outcome_requires_manual_reconciliation(
    store: SQLiteSchedulerStore,
) -> None:
    now = datetime.now(UTC)
    schedule = _schedule(dispatch_idempotency=DispatchIdempotency.NON_IDEMPOTENT).model_copy(
        update={"created_at": now}
    )
    service = TriggerService(store)
    service.create_schedule(schedule)
    service.manual_trigger("schedule_test", idempotency_key="unsafe", now=now)
    writer = store.acquire_authority(
        "runtime_writer", owner="runtime-live", ttl_seconds=60, now=now
    )
    gateway = _Gateway(failure=DispatchError("gateway.timeout", outcome_unknown=True))

    result = SchedulerWorker(store=store, gateway=gateway, owner="runtime-live").run_one(writer)

    assert result is not None
    assert result.status is RunRequestStatus.MANUAL_RECONCILE_REQUIRED
    assert len(gateway.actions) == 1
