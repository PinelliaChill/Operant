from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pydantic import JsonValue

from operant.application.scheduler import SchedulerConflictError
from operant.domain.scheduler import RunRequest, SchedulerLease
from operant.persistence.scheduler import SQLiteSchedulerStore


@dataclass(frozen=True)
class WorkflowDispatch:
    workflow_id: str
    workflow_version: int
    workflow_input: dict[str, JsonValue]
    idempotency_key: str
    source_run_request_id: str


class SchedulerActionGateway(Protocol):
    """Only integration route from Scheduler to a workflow side effect.

    Implementations must bind ``idempotency_key`` durably before starting a WorkflowRun.
    A repeated key must return the same WorkflowRun ID rather than dispatching twice.
    """

    def dispatch_workflow(self, action: WorkflowDispatch) -> str:
        """Return the durable WorkflowRun ID created for this exact action."""
        ...


class DispatchError(RuntimeError):
    def __init__(self, error_code: str, *, outcome_unknown: bool = False) -> None:
        self.error_code = error_code
        self.outcome_unknown = outcome_unknown
        super().__init__(error_code)


class SchedulerWorker:
    def __init__(
        self,
        *,
        store: SQLiteSchedulerStore,
        gateway: SchedulerActionGateway,
        owner: str,
        job_ttl_seconds: int = 30,
    ) -> None:
        if job_ttl_seconds < 1:
            raise ValueError("job_ttl_seconds must be positive")
        self._store = store
        self._gateway = gateway
        self._owner = owner
        self._job_ttl_seconds = job_ttl_seconds

    def run_one(self, writer_lease: SchedulerLease) -> RunRequest | None:
        claimed = self._store.claim_due(
            writer_lease, owner=self._owner, ttl_seconds=self._job_ttl_seconds
        )
        if claimed is None:
            return None
        request, lease = claimed
        current = self._store.get_request(request.id)
        if current.cancel_requested:
            return self._store.cancel_claimed_job(lease)
        schedule = self._store.get_schedule_revision(request.schedule_id, request.schedule_version)
        try:
            # This fenced transition closes cancellation-before-dispatch and records the
            # conservative side-effect boundary before the external call begins.
            self._store.mark_side_effect_started(lease)
            workflow_run_id = self._gateway.dispatch_workflow(
                WorkflowDispatch(
                    workflow_id=request.workflow_id,
                    workflow_version=request.workflow_version,
                    workflow_input=request.workflow_input,
                    idempotency_key=request.idempotency_key,
                    source_run_request_id=request.id,
                )
            )
        except SchedulerConflictError:
            # A lease/cancellation fence is not an external execution failure.
            raise
        except DispatchError as exc:
            return self._store.fail_job(
                lease,
                error_code=exc.error_code,
                outcome_unknown=exc.outcome_unknown,
                retry_base_seconds=schedule.retry_base_seconds,
                retry_max_seconds=schedule.retry_max_seconds,
            )
        except Exception as exc:
            # Do not persist exception text; the stable type is enough for audit/routing.
            return self._store.fail_job(
                lease,
                error_code=f"gateway.{type(exc).__name__}",
                outcome_unknown=True,
                retry_base_seconds=schedule.retry_base_seconds,
                retry_max_seconds=schedule.retry_max_seconds,
            )
        if not workflow_run_id:
            return self._store.fail_job(
                lease,
                error_code="gateway.missing_workflow_run_id",
                outcome_unknown=True,
                retry_base_seconds=schedule.retry_base_seconds,
                retry_max_seconds=schedule.retry_max_seconds,
            )
        return self._store.complete_job(lease, workflow_run_id=workflow_run_id)
