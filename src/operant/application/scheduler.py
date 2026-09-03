from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

from operant.domain.scheduler import (
    MisfirePolicy,
    RunRequest,
    ScheduleDefinition,
    SchedulerLease,
    ScheduleStatus,
    TriggerKind,
    require_aware_utc,
)


class SchedulerValidationError(ValueError):
    pass


class SchedulerConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class _CronField:
    values: frozenset[int]
    wildcard: bool


class CronExpression:
    """Bounded five-field cron evaluated against aware local wall time."""

    _RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))

    def __init__(self, expression: str) -> None:
        pieces = expression.split()
        if len(pieces) != 5:
            raise SchedulerValidationError("cron_expression must contain five fields")
        self.expression = " ".join(pieces)
        self.fields = tuple(
            self._parse_field(piece, minimum, maximum, day_of_week=index == 4)
            for index, (piece, (minimum, maximum)) in enumerate(
                zip(pieces, self._RANGES, strict=True)
            )
        )

    @staticmethod
    def _parse_field(text: str, minimum: int, maximum: int, *, day_of_week: bool) -> _CronField:
        values: set[int] = set()
        wildcard = text == "*"
        for component in text.split(","):
            if not component:
                raise SchedulerValidationError("cron field contains an empty component")
            base, separator, step_text = component.partition("/")
            if separator:
                if not step_text.isdigit() or int(step_text) < 1:
                    raise SchedulerValidationError("cron step must be a positive integer")
                step = int(step_text)
            else:
                step = 1
            if base == "*":
                start, end = minimum, maximum
            elif "-" in base:
                start_text, end_text = base.split("-", 1)
                if not start_text.isdigit() or not end_text.isdigit():
                    raise SchedulerValidationError("cron ranges must be numeric")
                start, end = int(start_text), int(end_text)
            elif base.isdigit():
                start = end = int(base)
            else:
                raise SchedulerValidationError("cron fields support numeric values only")
            if start < minimum or end > maximum or start > end:
                raise SchedulerValidationError("cron value is outside its allowed range")
            values.update(range(start, end + 1, step))
        if day_of_week and 7 in values:
            values.remove(7)
            values.add(0)
        return _CronField(frozenset(values), wildcard)

    def matches(self, local: datetime) -> bool:
        minute, hour, day_of_month, month, day_of_week = self.fields
        cron_weekday = (local.weekday() + 1) % 7
        if local.minute not in minute.values or local.hour not in hour.values:
            return False
        if local.month not in month.values:
            return False
        dom_match = local.day in day_of_month.values
        dow_match = cron_weekday in day_of_week.values
        if day_of_month.wildcard or day_of_week.wildcard:
            return dom_match and dow_match
        return dom_match or dow_match

    def occurrences_between(
        self,
        *,
        start_exclusive: datetime,
        end_inclusive: datetime,
        timezone_name: str,
        limit: int,
    ) -> tuple[datetime, ...]:
        start = require_aware_utc(start_exclusive, field="start_exclusive")
        end = require_aware_utc(end_inclusive, field="end_inclusive")
        if end <= start or limit < 1:
            return ()
        zone = ZoneInfo(timezone_name)
        # Walk backwards so a bounded result is the most recent catch-up window,
        # not the oldest occurrences after a long scheduler outage.
        cursor = end.replace(second=0, microsecond=0)
        occurrences: list[datetime] = []
        while cursor > start:
            if self.matches(cursor.astimezone(zone)):
                occurrences.append(cursor)
                if len(occurrences) >= limit:
                    break
            cursor -= timedelta(minutes=1)
        occurrences.reverse()
        return tuple(occurrences)


class SchedulerRepository(Protocol):
    def put_schedule(self, schedule: ScheduleDefinition) -> None: ...

    def get_schedule(self, schedule_id: str) -> ScheduleDefinition: ...

    def update_schedule_status(
        self, schedule_id: str, status: ScheduleStatus, *, expected_version: int
    ) -> ScheduleDefinition: ...

    def get_schedule_cursor(self, schedule_id: str) -> datetime: ...

    def list_enabled_schedule_ids(self) -> tuple[str, ...]: ...

    def enqueue_occurrences(
        self,
        schedule: ScheduleDefinition,
        occurrences: tuple[datetime, ...],
        *,
        leader_lease: SchedulerLease,
        advance_cursor_to: datetime,
        available_at: datetime,
    ) -> tuple[RunRequest, ...]: ...

    def enqueue_manual(
        self,
        schedule: ScheduleDefinition,
        *,
        idempotency_key: str,
        requested_at: datetime,
    ) -> RunRequest: ...

    def replay_dead_letter(
        self,
        request_id: str,
        *,
        idempotency_key: str,
        requested_at: datetime,
    ) -> RunRequest: ...


class TriggerService:
    def __init__(self, repository: SchedulerRepository) -> None:
        self._repository = repository

    def create_schedule(self, schedule: ScheduleDefinition) -> None:
        if schedule.trigger_kind is TriggerKind.CRON:
            CronExpression(schedule.cron_expression or "")
        self._repository.put_schedule(schedule)

    def set_status(
        self,
        schedule_id: str,
        status: ScheduleStatus,
        *,
        expected_version: int,
    ) -> ScheduleDefinition:
        return self._repository.update_schedule_status(
            schedule_id, status, expected_version=expected_version
        )

    def materialize_all_due(
        self,
        *,
        leader_lease: SchedulerLease,
        now: datetime | None = None,
    ) -> tuple[RunRequest, ...]:
        current = require_aware_utc(now or datetime.now(timezone.utc), field="now")
        requests: list[RunRequest] = []
        for schedule_id in self._repository.list_enabled_schedule_ids():
            requests.extend(
                self.materialize_due(schedule_id, leader_lease=leader_lease, now=current)
            )
        return tuple(requests)

    def materialize_due(
        self,
        schedule_id: str,
        *,
        leader_lease: SchedulerLease,
        now: datetime | None = None,
    ) -> tuple[RunRequest, ...]:
        current = require_aware_utc(now or datetime.now(timezone.utc), field="now")
        schedule = self._repository.get_schedule(schedule_id)
        if schedule.status is not ScheduleStatus.ENABLED:
            return ()
        cursor = self._repository.get_schedule_cursor(schedule.id)
        due = self._due_occurrences(schedule, cursor=cursor, now=current)
        selected = self._apply_misfire(schedule, due, now=current)
        return self._repository.enqueue_occurrences(
            schedule,
            selected,
            leader_lease=leader_lease,
            advance_cursor_to=current,
            available_at=current,
        )

    def manual_trigger(
        self,
        schedule_id: str,
        *,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> RunRequest:
        current = require_aware_utc(now or datetime.now(timezone.utc), field="now")
        schedule = self._repository.get_schedule(schedule_id)
        if schedule.status is ScheduleStatus.CANCELLED:
            raise SchedulerConflictError("cancelled schedules cannot be triggered")
        return self._repository.enqueue_manual(
            schedule, idempotency_key=idempotency_key, requested_at=current
        )

    def replay_dead_letter(
        self,
        request_id: str,
        *,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> RunRequest:
        current = require_aware_utc(now or datetime.now(timezone.utc), field="now")
        return self._repository.replay_dead_letter(
            request_id,
            idempotency_key=idempotency_key,
            requested_at=current,
        )

    @staticmethod
    def _due_occurrences(
        schedule: ScheduleDefinition, *, cursor: datetime, now: datetime
    ) -> tuple[datetime, ...]:
        if schedule.trigger_kind is TriggerKind.TIMER:
            timer_at = schedule.timer_at
            assert timer_at is not None
            return (timer_at,) if cursor < timer_at <= now else ()
        cron = CronExpression(schedule.cron_expression or "")
        # Read at most max_catch_up + 1 so overflow is detectable without an unbounded result.
        return cron.occurrences_between(
            start_exclusive=cursor,
            end_inclusive=now,
            timezone_name=schedule.timezone_name,
            limit=schedule.max_catch_up + 1,
        )

    @staticmethod
    def _apply_misfire(
        schedule: ScheduleDefinition,
        due: tuple[datetime, ...],
        *,
        now: datetime,
    ) -> tuple[datetime, ...]:
        if not due:
            return ()
        if schedule.misfire_policy is MisfirePolicy.FIRE_ONCE:
            return (due[-1],)
        if schedule.misfire_policy is MisfirePolicy.CATCH_UP:
            return due[-schedule.max_catch_up :]
        if schedule.trigger_kind is TriggerKind.TIMER:
            return tuple(value for value in due if value >= now)
        current_minute = now.replace(second=0, microsecond=0)
        return tuple(value for value in due if value == current_minute)
