"""Additive B2-5 governance protocol; publication remains Core owned."""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from operant.contracts.b2_1 import (
    MemoryHead,
    MemoryProposal,
    MemoryVersion,
    MemoryVersionRef,
    SourceRef,
)


class GovernanceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExactProposal(GovernanceModel):
    proposal_id: str = Field(min_length=1, max_length=200)
    proposal_revision: int = Field(ge=0, le=2**53 - 1)
    proposed_version: MemoryVersionRef
    base_head_revision: int = Field(ge=0, le=2**53 - 1)


class MemoryRelationship(GovernanceModel):
    relation: Literal["conflicts_with", "supersedes"]
    target: MemoryVersionRef


class GovernanceEntry(GovernanceModel):
    proposal: MemoryProposal
    version: MemoryVersion
    current_head: MemoryHead
    review_due_at: AwareDatetime | None = None
    review_expired: bool = False
    independent_evidence_count: int = Field(ge=0)
    relationships: list[MemoryRelationship] = Field(default_factory=list)
    blocked_reason: str | None = None


class GovernanceRecord(GovernanceModel):
    version: MemoryVersion
    head: MemoryHead
    independent_evidence_count: int = Field(ge=0)
    relationships: list[MemoryRelationship] = Field(default_factory=list)
    currently_usable: bool
    blocked_reason: str | None = None


class HistoryEntry(GovernanceModel):
    item_id: str
    thread_id: str
    cursor: int
    occurred_at: AwareDatetime
    kind: str
    excerpt: str
    source: SourceRef | None = None


class HistoryPage(GovernanceModel):
    project_id: str
    perspective: Literal["historical_fact"] = "historical_fact"
    items: list[HistoryEntry]
    next_cursor: int | None = None
    cutoff_cursor: int


class HistoryDetail(GovernanceModel):
    project_id: str
    entry: HistoryEntry
    text: str
    perspective: Literal["historical_fact"] = "historical_fact"


class MaintenanceJobView(GovernanceModel):
    job_id: str
    project_id: str
    workflow_id: str
    workflow_version: int
    graph_run_id: str | None = None
    run_request_id: str | None = None
    state: str
    source_cursor: int
    processed_cursor: int
    model_profile_id: str
    model_id: str
    attempts: int
    max_attempts: int
    input_tokens: int = 0
    output_tokens: int = 0
    error_code: str | None = None
    proposal_ids: list[str] = Field(default_factory=list)
    updated_at: AwareDatetime


class GovernanceState(GovernanceModel):
    project_id: str
    enabled: bool
    maintenance_enabled: bool = False
    unresolved_command_ids: list[str] = Field(default_factory=list)
    records: list[GovernanceRecord]
    proposals: list[GovernanceEntry]
    jobs: list[MaintenanceJobView]
    perspective: Literal["current_agreement"] = "current_agreement"


class B25Command(GovernanceModel):
    action: Literal[
        "propose",
        "review",
        "source_revoke",
        "maintenance_create",
        "maintenance_configure",
        "maintenance_cancel",
        "maintenance_retry",
    ]
    project_id: str = Field(min_length=1, max_length=200)
    enabled: bool | None = None
    record_id: str | None = Field(default=None, max_length=200)
    expected_head_revision: int | None = Field(default=None, ge=0, le=2**53 - 1)
    content: str | None = Field(default=None, min_length=1, max_length=100_000)
    sources: list[SourceRef] = Field(default_factory=list, max_length=100)
    relationships: list[MemoryRelationship] = Field(default_factory=list, max_length=50)
    valid_from: AwareDatetime | None = None
    valid_until: AwareDatetime | None = None
    review_due_at: AwareDatetime | None = None
    selections: list[ExactProposal] = Field(default_factory=list, max_length=50)
    decision: Literal["accept", "reject"] | None = None
    source: SourceRef | None = None
    model_profile_id: str | None = Field(default=None, max_length=200)
    job_id: str | None = Field(default=None, max_length=200)
    max_sources: int = Field(default=20, ge=1, le=50)
    max_output_tokens: int = Field(default=1024, ge=64, le=4096)
    max_attempts: int = Field(default=3, ge=1, le=3)

    @model_validator(mode="after")
    def validate_times(self) -> B25Command:
        if self.valid_from and self.valid_until and self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be after valid_from")
        return self


class B25Result(GovernanceModel):
    status: str
    message: str
    state: GovernanceState
    affected_ids: list[str] = Field(default_factory=list)


class GovernanceEvent(GovernanceModel):
    cursor: int
    project_id: str
    action: str
    affected_ids: list[str]
    occurred_at: AwareDatetime


class GovernanceEventPage(GovernanceModel):
    events: list[GovernanceEvent]
    next_cursor: int | None = None


class ContextMemoryImpact(GovernanceModel):
    ref: MemoryVersionRef
    source_and_time_valid: bool
    reason: str
    conflict_proposal_ids: list[str] = Field(default_factory=list)


class ContextGovernanceImpact(GovernanceModel):
    session_id: str
    entries: list[ContextMemoryImpact]
    limitation: str = "已发送内容无法收回；这里只反映当前来源与时效，发送前仍由 Core 复核权限。"
