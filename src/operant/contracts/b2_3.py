"""Additive management protocol; public types owned by Core, not plugin schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ManagementModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ManagedProject(ManagementModel):
    project_id: str
    name: str
    workspace_id: str
    archived: bool = False
    memory_enabled: bool = True
    installation_id: str | None = None


class PluginCatalogEntry(ManagementModel):
    plugin_id: str
    name: str
    description: str
    config_schema: dict[str, Any]


class ManagedInstallation(ManagementModel):
    installation_id: str
    plugin_id: str
    dataset_id: str
    binding_id: str | None
    state: str
    mode: str
    certification_status: str
    config: dict[str, Any]


class ManagedDataset(ManagementModel):
    dataset_id: str
    plugin_id: str
    installation_id: str | None
    state: str
    record_count: int
    exceptions: list[str]


class ManagedSetting(ManagementModel):
    key: str
    value: Any
    source: str
    scope: str
    effective_at: str


class ManagedSkill(ManagementModel):
    skill_id: str
    name: str
    state: str
    package_ref: str
    project_ids: list[str] = Field(default_factory=list)
    trust_status: str = "user_installed"


class SkillCatalogEntry(ManagementModel):
    package_ref: str
    name: str


class ManagedProposal(ManagementModel):
    proposal_id: str
    content: str
    state: str
    expected_revision: int


class ManagedMemory(ManagementModel):
    record_id: str
    dataset_id: str
    project_id: str
    content: str
    state: str
    revision: int
    evidence: str
    version: int
    sources: list[dict[str, Any]]
    proposals: list[ManagedProposal]


class ManagedArtifact(ManagementModel):
    artifact_id: str
    content_hash: str
    size_bytes: int
    lifecycle: str
    pinned: bool
    blocked: bool


class ManagementState(ManagementModel):
    projects: list[ManagedProject]
    global_enabled: bool
    catalog: list[PluginCatalogEntry]
    installations: list[ManagedInstallation]
    datasets: list[ManagedDataset]
    settings: list[ManagedSetting]
    skills: list[ManagedSkill]
    skill_catalog: list[SkillCatalogEntry]
    records: list[ManagedMemory]
    artifacts: list[ManagedArtifact] = Field(default_factory=list)


class ManagementCommand(ManagementModel):
    action: Literal[
        "project_create",
        "project_update",
        "project_archive",
        "project_detach",
        "plugin_install",
        "plugin_enable",
        "plugin_disable",
        "plugin_uninstall",
        "binding_select",
        "memory_switch",
        "memory_save",
        "memory_propose",
        "memory_confirm",
        "memory_deactivate",
        "memory_search",
        "memory_migrate",
        "dataset_export",
        "dataset_delete",
        "cleanup_resume",
        "plugin_configure",
        "skill_discover",
        "skill_install",
        "skill_disable",
        "skill_enable",
        "skill_uninstall",
        "artifact_pin",
        "artifact_archive",
        "artifact_schedule",
        "artifact_trash",
        "artifact_restore",
        "artifact_audit",
    ]
    project_id: str | None = Field(default=None, max_length=200)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    workspace_path: str | None = Field(default=None, min_length=1, max_length=4096)
    installation_id: str | None = Field(default=None, max_length=200)
    plugin_id: str | None = Field(default=None, max_length=200)
    dataset_id: str | None = Field(default=None, max_length=200)
    enabled: bool | None = None
    mode: Literal["isolated", "trusted_in_process"] | None = None
    data_policy: Literal["keep", "delete"] | None = None
    content: str | None = Field(default=None, min_length=1, max_length=100_000)
    record_id: str | None = Field(default=None, max_length=200)
    proposal_id: str | None = Field(default=None, max_length=200)
    expected_revision: int | None = Field(default=None, ge=0, le=2**53 - 1)
    confirmed: bool = False
    query: str | None = Field(default=None, min_length=1, max_length=10_000)
    config: dict[str, Any] | None = None
    skill_id: str | None = Field(default=None, max_length=200)
    package_ref: str | None = Field(default=None, max_length=200)
    artifact_id: str | None = Field(default=None, max_length=200)


class ManagementResult(ManagementModel):
    status: str
    message: str
    state: ManagementState
    export_data: dict[str, Any] | None = None
    records: list[ManagedMemory] | None = None
