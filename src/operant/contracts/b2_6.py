"""Single public B2-6 envelope over the Core-owned feature contracts."""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, RootModel

from operant.contracts.b2_6_remote import RemoteMemoryCommand, RemoteMemoryState
from operant.contracts.b2_6_sharing import SharingCommand, SharingState
from operant.contracts.b2_6_skills import SkillCommand, SkillState


class B26Command(RootModel[SkillCommand | SharingCommand | RemoteMemoryCommand]):
    pass


class B26Dataset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_id: str
    installation_id: str | None
    principal_id: str
    revision: int
    state: str


class B26State(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str
    skills: SkillState
    sharing: SharingState
    remote: RemoteMemoryState
    datasets: list[B26Dataset] = Field(default_factory=list)
    unresolved_command_ids: list[str] = Field(default_factory=list)


class B26Result(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str
    message: str
    affected_ids: list[str] = Field(default_factory=list)
    state: B26State


class B26Event(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cursor: int
    project_id: str
    action: str
    affected_ids: list[str]
    occurred_at: AwareDatetime


class B26EventPage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: list[B26Event]
    next_cursor: int | None = None
