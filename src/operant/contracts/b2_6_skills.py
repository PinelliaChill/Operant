"""Typed contracts for B2-6 experience-derived Skills.

The contracts describe a versioned, content-addressed Skill projection.  They
do not grant a Skill any tools: execution remains subject to the caller's
Role Policy and Capability checks.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from operant.contracts.b2_1 import (
    Contract,
    Cursor,
    Digest,
    Id,
    MemoryVersionRef,
    Revision,
    Scope,
    SourceRef,
)

B26_SKILL_CONTRACT_VERSION = "operant-b2-6-skills.v1"
SkillLifecycleState = Literal["draft", "published", "disabled", "revoked", "blocked"]
SkillTrustStatus = Literal["untrusted_draft", "validated", "core_published", "revoked"]
SkillValidationStatus = Literal["pending", "passed", "failed", "blocked"]
SkillResourceKind = Literal["manifest", "script", "reference"]


class SkillResourceHash(Contract):
    """One fixed resource entry in a generated Skill artifact."""

    relative_path: Annotated[str, Field(min_length=1, max_length=500)]
    kind: SkillResourceKind
    size_bytes: int = Field(ge=0, le=20_000_000)
    sha256: Digest

    @model_validator(mode="after")
    def validate_relative_path(self) -> SkillResourceHash:
        path = PurePosixPath(self.relative_path)
        if (
            path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
            or path.parts[0] not in {"SKILL.md", "scripts", "references"}
            or (path.parts[0] == "SKILL.md" and len(path.parts) != 1)
        ):
            raise ValueError("Skill resource path must stay in SKILL.md/scripts/references")
        return self


class SkillArtifact(Contract):
    """Public artifact metadata; no local storage path is exposed."""

    artifact_id: Id
    media_type: Annotated[str, Field(min_length=1, max_length=255)] = "text/markdown"
    content_hash: Digest
    size_bytes: int = Field(ge=1, le=20_000_000)
    manifest_hash: Digest
    resources: tuple[SkillResourceHash, ...] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_manifest_resource(self) -> SkillArtifact:
        manifests = [item for item in self.resources if item.relative_path == "SKILL.md"]
        if len(manifests) != 1 or manifests[0].sha256 != self.manifest_hash:
            raise ValueError("Skill artifact requires one exact SKILL.md hash")
        if len({item.relative_path for item in self.resources}) != len(self.resources):
            raise ValueError("Skill artifact resource paths must be unique")
        return self


class SkillValidationIssue(Contract):
    code: Id
    message: Annotated[str, Field(min_length=1, max_length=500)]


class SkillValidation(Contract):
    validation_id: Id
    skill_id: Id
    skill_version: Revision
    status: SkillValidationStatus
    validator_version: Id
    artifact_hash: Digest
    source_digest: Digest
    issues: tuple[SkillValidationIssue, ...] = Field(max_length=64)
    checked_at: AwareDatetime


class SkillVersion(Contract):
    """Immutable generated Skill version and its exact provenance."""

    skill_id: Id
    version: Annotated[int, Field(ge=1, le=2**53 - 1)]
    name: Annotated[
        str, Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    ]
    description: Annotated[str, Field(min_length=1, max_length=4_000)]
    procedure_ref: MemoryVersionRef
    source_refs: tuple[SourceRef, ...] = Field(max_length=100)
    source_digest: Digest
    scope: Scope
    role_ids: tuple[Id, ...] = Field(max_length=64)
    agent_ids: tuple[Id, ...] = Field(max_length=64)
    sensitivity: Literal["public", "internal", "sensitive"]
    artifact: SkillArtifact
    trust_status: SkillTrustStatus
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_identity(self) -> SkillVersion:
        if self.artifact.artifact_id == self.skill_id:
            raise ValueError("Skill artifact must have an independent artifact identity")
        identities = {
            (item.source_type, item.source_id, item.revision, item.content_digest)
            for item in self.source_refs
        }
        if len(identities) != len(self.source_refs):
            raise ValueError("Skill source references must be unique")
        return self


class SkillHead(Contract):
    """Mutable CAS publication pointer over immutable Skill versions."""

    skill_id: Id
    head_revision: Revision
    published_version: int | None = Field(default=None, ge=1, le=2**53 - 1)
    state: SkillLifecycleState
    permission_epoch: Revision
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def validate_published_pointer(self) -> SkillHead:
        if self.state in {"published", "disabled", "revoked"} and self.published_version is None:
            raise ValueError("published, disabled, and revoked Skill heads require a version")
        return self


class SkillView(Contract):
    skill_id: Id
    version: SkillVersion
    head: SkillHead
    validation: SkillValidation | None = None
    trust_status: SkillTrustStatus = "untrusted_draft"
    dependency_state: Literal["active", "blocked", "revoked"] = "active"
    rollback_versions: tuple[int, ...] = Field(default_factory=tuple, max_length=64)


class SkillRunSnapshot(Contract):
    """Run-local Skill snapshot; current source checks still apply on each use."""

    skill_id: Id
    skill_version: int = Field(ge=1, le=2**53 - 1)
    session_id: Id
    role_id: Id
    agent_id: Id
    model_profile_id: Id
    artifact_id: Id
    artifact_hash: Digest
    source_digest: Digest
    source_refs: tuple[SourceRef, ...] = Field(max_length=100)
    scope: Scope
    permission_epoch: Revision
    content: Annotated[str, Field(min_length=1, max_length=100_000)]


class SkillState(Contract):
    schema_version: Literal["operant-b2-6-skills.v1"] = "operant-b2-6-skills.v1"
    project_id: Id
    skills: tuple[SkillView, ...] = ()
    cursor: Cursor = "0"


class SkillCommand(Contract):
    """Command payload; external callers still pass through Core Gateway."""

    action: Literal[
        "procedure_propose",
        "skill_draft",
        "skill_validate",
        "skill_publish",
        "skill_disable",
        "skill_rollback",
    ]
    project_id: Id
    skill_id: Id | None = None
    record_id: Id | None = None
    procedure_ref: MemoryVersionRef | None = None
    content: Annotated[str, Field(min_length=1, max_length=100_000)] | None = None
    sources: tuple[SourceRef, ...] = Field(default_factory=tuple, max_length=100)
    name: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    description: Annotated[str, Field(min_length=1, max_length=4_000)] | None = None
    skill_version: Revision | None = None
    rollback_to_version: Revision | None = None
    expected_head_revision: Revision | None = None
    permission_epoch: Revision | None = None
    reason: Annotated[str, Field(min_length=1, max_length=500)] | None = None

    @model_validator(mode="after")
    def validate_action_shape(self) -> SkillCommand:
        if self.action == "procedure_propose":
            if self.procedure_ref is not None:
                raise ValueError("procedure_propose creates a candidate from content and sources")
            if self.content is None:
                raise ValueError("procedure_propose requires content")
            if not self.sources:
                raise ValueError("procedure_propose requires exact source references")
            if any(
                value is not None
                for value in (
                    self.skill_id,
                    self.name,
                    self.description,
                    self.skill_version,
                    self.rollback_to_version,
                    self.expected_head_revision,
                )
            ):
                raise ValueError("procedure_propose cannot carry Skill lifecycle fields")
            return self

        if self.action == "skill_draft":
            if self.procedure_ref is None:
                raise ValueError("skill_draft requires procedure_ref")
            if self.name is None or self.description is None:
                raise ValueError("skill_draft requires name and description")
            if self.content is not None or self.sources:
                raise ValueError("skill_draft reads sources from the reviewed procedure")
            if self.rollback_to_version is not None:
                raise ValueError("skill_draft cannot carry rollback_to_version")
            return self

        if self.skill_id is None:
            raise ValueError(f"{self.action} requires skill_id")
        if self.action == "skill_rollback":
            if self.rollback_to_version is None:
                raise ValueError("skill_rollback requires rollback_to_version")
        elif self.rollback_to_version is not None:
            raise ValueError("rollback_to_version is valid only for skill_rollback")

        # These transitions operate on a mutable publication head.  Requiring
        # both values in the public command prevents a client from silently
        # applying an action to whichever version became current later.
        if self.action in {"skill_publish", "skill_disable", "skill_rollback"}:
            if self.skill_version is None:
                raise ValueError(f"{self.action} requires skill_version")
            if self.expected_head_revision is None:
                raise ValueError(f"{self.action} requires expected_head_revision")
        elif self.action != "skill_validate" and self.expected_head_revision is not None:
            raise ValueError("expected_head_revision is valid only for head transitions")
        return self


__all__ = [
    "B26_SKILL_CONTRACT_VERSION",
    "SkillArtifact",
    "SkillCommand",
    "SkillHead",
    "SkillLifecycleState",
    "SkillResourceHash",
    "SkillRunSnapshot",
    "SkillState",
    "SkillTrustStatus",
    "SkillValidation",
    "SkillValidationIssue",
    "SkillValidationStatus",
    "SkillVersion",
    "SkillView",
]
