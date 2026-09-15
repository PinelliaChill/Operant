"""Additive B2-4 contracts; historical protocols remain frozen."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from operant.contracts.b2_1 import MemoryPack, MemoryVersion
from operant.domain.context import ContextRevision
from operant.domain.graph import GraphRunStatus, WorkflowDefinition
from operant.domain.team import TeamDefinition


class B24Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MemorySelection(B24Model):
    memory: MemoryVersion
    reason: Literal["explicit", "task_relevance", "resident_preference"]
    token_count: int = Field(ge=0)


class MemoryInspection(B24Model):
    pack: MemoryPack
    entries: list[MemorySelection]
    counting_method: str
    refresh: Literal["frozen", "explicit_refresh"] = "frozen"
    excluded_record_ids: list[str] = Field(default_factory=list)
    limitation: str = "Already sent content cannot be recalled; revoked derived context pauses."


class CollaborationRole(B24Model):
    id: str
    name: str
    model_profile_id: str
    model_id: str
    effort: str


class CollaborationGraphRun(B24Model):
    id: str
    workflow_definition_id: str
    workflow_definition_version: int = Field(ge=1)
    workspace_or_target: str | None
    team_run_id: str | None
    status: GraphRunStatus
    updated_at: datetime


class CollaborationDirectory(B24Model):
    workflows: list[WorkflowDefinition]
    teams: list[TeamDefinition]
    roles: list[CollaborationRole]
    graph_runs: list[CollaborationGraphRun] = Field(default_factory=list, max_length=100)
    graph_runs_has_more: bool = False
    graph_runs_next_cursor: str | None = None


class B24Command(B24Model):
    action: Literal[
        "graph_create_from_roles", "team_start_from_roles", "memory_refresh", "memory_exclude"
    ]
    name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    role_ids: list[str] = Field(default_factory=list, max_length=16)
    workflow_id: str | None = None
    team_id: str | None = None
    team_version: int | None = None
    workflow_run_id: str | None = None
    workspace_or_target: str | None = None
    member_ids: list[str] = Field(default_factory=list, max_length=16)
    task: str | None = Field(default=None, max_length=10000)
    session_id: str | None = None
    record_id: str | None = None


class B24Result(B24Model):
    status: Literal["completed"] = "completed"
    resource_id: str
    resource_type: str
    directory: CollaborationDirectory


class InspectedContextRevision(ContextRevision):
    memory_inspection: MemoryInspection | None = None


class ContextInspection(B24Model):
    session_id: str
    revisions: list[InspectedContextRevision]
