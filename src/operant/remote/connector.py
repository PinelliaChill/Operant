from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from operant.domain.remote_execution import RemoteExecutionJob, RemoteExecutionResult


class RemoteOutcomeUnknown(RuntimeError):
    """The connector sent an action but cannot prove its outcome."""


@dataclass(frozen=True)
class ConnectorOutcome:
    result: RemoteExecutionResult
    artifact_bytes: bytes | None = None


class RemoteTargetConnector(Protocol):
    """Narrow target connector; it receives no Controller environment or filesystem."""

    target_id: str
    lease_id: str
    lease_token: str
    lease_fencing: int

    def execute(self, job: RemoteExecutionJob) -> ConnectorOutcome: ...

    def cancel(self, job_id: str) -> None: ...


@dataclass
class InMemoryRemoteTargetConnector:
    """Deterministic connector used by tests; no network, Home, SSH or Docker access."""

    target_id: str
    lease_id: str
    lease_token: str
    lease_fencing: int
    outcomes: dict[str, ConnectorOutcome | Exception] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    cancellations: list[str] = field(default_factory=list)

    def execute(self, job: RemoteExecutionJob) -> ConnectorOutcome:
        self.calls.append(job.job_id)
        candidate = self.outcomes.get(job.operation)
        if candidate is None:
            raise RemoteOutcomeUnknown("fake connector has no deterministic outcome")
        if isinstance(candidate, Exception):
            raise candidate
        return candidate

    def cancel(self, job_id: str) -> None:
        self.cancellations.append(job_id)
