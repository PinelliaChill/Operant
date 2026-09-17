"""Additive B2-6 contracts for experience promotion and explicit sharing.

The contracts in this module describe Core-owned facts.  They deliberately do
not turn a client supplied ``merged`` flag into publication authority.  A
writer promotion is represented by the merge, target tree and verification
references that the Core checked at the promotion boundary.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import AwareDatetime, ConfigDict, Field, model_validator

from operant.contracts.b2_1 import (
    Contract,
    DatasetTransfer,
    Digest,
    Id,
    MemoryVersion,
    MemoryVersionRef,
    Scope,
    SourceRef,
)

B26_SHARING_CONTRACT_VERSION: Literal["operant-b2-6-sharing.v1"] = "operant-b2-6-sharing.v1"

BranchRef = Annotated[str, Field(min_length=1, max_length=500)]
CommitRef = Annotated[str, Field(min_length=7, max_length=128)]
Reason = Annotated[str, Field(min_length=1, max_length=2_000)]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class SharingModel(Contract):
    """Common immutable, extra-field rejecting model configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class ProjectWorktreeRegistration(SharingModel):
    """An explicit Project to worktree association.

    ``workspace_ref`` is the opaque identity used by the public contract.  A
    local path, Git remote or directory name is not a substitute for this
    registration.
    """

    registration_id: Id = Field(default_factory=lambda: _id("worktree_registration"))
    project_id: Id
    workspace_id: Id
    worktree_id: Id
    workspace_ref: Annotated[str, Field(pattern=r"^workspace:[0-9a-f]{64}$")]
    branch_ref: BranchRef
    commit_ref: CommitRef | None = None
    tree_digest: Digest | None = None
    principal_id: Id
    association_revision: int = Field(default=0, ge=0, le=2**53 - 1)
    permission_epoch: int = Field(default=0, ge=0, le=2**53 - 1)
    state: Literal["active", "revoked"] = "active"
    revoked_at: AwareDatetime | None = None
    revoke_reason: Reason | None = None
    created_at: AwareDatetime = Field(default_factory=_now)
    updated_at: AwareDatetime = Field(default_factory=_now)

    @model_validator(mode="after")
    def validate_state(self) -> ProjectWorktreeRegistration:
        if self.state == "active" and (
            self.revoked_at is not None or self.revoke_reason is not None
        ):
            raise ValueError("active worktree registration cannot carry revocation metadata")
        if self.state == "revoked" and self.revoked_at is None:
            raise ValueError("revoked worktree registration requires revoked_at")
        if self.state == "revoked" and self.revoke_reason is None:
            raise ValueError("revoked worktree registration requires revoke_reason")
        if self.commit_ref is not None and any(char.isspace() for char in self.commit_ref):
            raise ValueError("commit_ref cannot contain whitespace")
        if any(char in self.branch_ref for char in ("\x00", "\n", "\r")):
            raise ValueError("branch_ref contains a control character")
        return self


class SharingGrant(SharingModel):
    """A bounded, revocable grant between two typed memory scopes."""

    grant_id: Id = Field(default_factory=lambda: _id("sharing_grant"))
    # This is the owner project.  The target may be another project only when
    # the grant explicitly names that target scope.
    project_id: Id
    source_dataset_id: Id
    source_scope: Scope
    target_scope: Scope
    subject_id: Id
    grantor_id: Id
    purpose: Literal["recall", "source_read", "publish", "share", "transfer", "delete"]
    memory_refs: tuple[MemoryVersionRef, ...] = Field(min_length=1, max_length=500)
    policy_revision: int = Field(default=0, ge=0, le=2**53 - 1)
    permission_epoch: int = Field(default=0, ge=0, le=2**53 - 1)
    expires_at: AwareDatetime
    revision: int = Field(default=0, ge=0, le=2**53 - 1)
    state: Literal["active", "revoked", "expired"] = "active"
    revoked_at: AwareDatetime | None = None
    revoke_reason: Reason | None = None
    created_at: AwareDatetime = Field(default_factory=_now)
    updated_at: AwareDatetime = Field(default_factory=_now)

    @model_validator(mode="after")
    def validate_grant(self) -> SharingGrant:
        if any(ref.dataset_id != self.source_dataset_id for ref in self.memory_refs):
            raise ValueError("sharing grant memory_refs must belong to source_dataset_id")
        if (
            hasattr(self.source_scope, "project_id")
            and self.source_scope.project_id != self.project_id
        ):
            raise ValueError("grant project_id does not match its source scope")
        if self.state == "active" and (
            self.revoked_at is not None or self.revoke_reason is not None
        ):
            raise ValueError("active sharing grant cannot carry revocation metadata")
        if self.state == "revoked" and self.revoked_at is None:
            raise ValueError("revoked sharing grant requires revoked_at")
        if self.state == "revoked" and self.revoke_reason is None:
            raise ValueError("revoked sharing grant requires revoke_reason")
        return self


class SharingConsumer(SharingModel):
    """A dataset consumer retained independently from an installation row."""

    consumer_id: Id = Field(default_factory=lambda: _id("dataset_consumer"))
    project_id: Id
    dataset_id: Id
    installation_id: Id
    grant_id: Id
    purpose: Literal["primary_engine", "shared_read", "export", "migration"]
    state: Literal["active", "revoked", "transferred"] = "active"
    revision: int = Field(default=0, ge=0, le=2**53 - 1)
    revoked_at: AwareDatetime | None = None
    reason: Reason | None = None
    created_at: AwareDatetime = Field(default_factory=_now)
    updated_at: AwareDatetime = Field(default_factory=_now)

    @model_validator(mode="after")
    def validate_consumer_state(self) -> SharingConsumer:
        if self.state == "active" and self.revoked_at is not None:
            raise ValueError("active consumer cannot carry revoked_at")
        if self.state != "active" and self.revoked_at is None:
            raise ValueError("non-active consumer requires revoked_at")
        return self


class DatasetTransferState(SharingModel):
    """Durable transfer state; a pending transfer does not change ownership."""

    transfer_id: Id = Field(default_factory=lambda: _id("dataset_transfer"))
    project_id: Id
    request: DatasetTransfer
    source_installation_id: Id | None = None
    source_consumer_id: Id | None = None
    destination_consumer_id: Id
    state: Literal["pending", "validated", "committed", "revoked", "blocked"] = "pending"
    revision: int = Field(default=0, ge=0, le=2**53 - 1)
    evidence_refs: tuple[Id, ...] = Field(default=(), max_length=200)
    reason: Reason | None = None
    created_at: AwareDatetime = Field(default_factory=_now)
    updated_at: AwareDatetime = Field(default_factory=_now)

    @model_validator(mode="after")
    def validate_transfer_state(self) -> DatasetTransferState:
        if self.state == "validated" and not self.evidence_refs:
            raise ValueError("validated transfer requires evidence_refs")
        if self.state == "blocked" and self.reason is None:
            raise ValueError("blocked transfer requires a reason")
        return self


class WriterMemoryEvidence(SharingModel):
    """Evidence binding a Run-scoped memory candidate to a verified merge.

    The status is derived from the evidence checks.  It is not a client
    assertion and cannot be set to ``eligible`` without target verification.
    """

    evidence_id: Id = Field(default_factory=lambda: _id("writer_memory_evidence"))
    memory_ref: MemoryVersionRef
    candidate_head_revision: int = Field(default=0, ge=0, le=2**53 - 1)
    project_id: Id
    workspace_id: Id
    worktree_id: Id
    writer_workspace_id: Id
    graph_run_id: Id
    run_id: Id
    branch_ref: BranchRef
    base_revision: CommitRef
    base_tree_digest: Digest | None = None
    merge_run_id: Id | None = None
    target_isolation_ref: str | None = Field(default=None, min_length=1, max_length=500)
    target_commit_ref: CommitRef | None = None
    target_tree_digest: Digest | None = None
    verification_artifact_refs: tuple[Id, ...] = Field(default=(), max_length=200)
    verification_digest: Digest | None = None
    verification_status: Literal["pending", "passed", "failed", "unknown"] = "pending"
    state: Literal["candidate", "eligible", "published", "blocked", "revoked"] = "candidate"
    published_memory_ref: MemoryVersionRef | None = None
    permission_epoch: int = Field(default=0, ge=0, le=2**53 - 1)
    revision: int = Field(default=0, ge=0, le=2**53 - 1)
    reason: Reason | None = None
    created_at: AwareDatetime = Field(default_factory=_now)
    updated_at: AwareDatetime = Field(default_factory=_now)

    @model_validator(mode="after")
    def validate_evidence_state(self) -> WriterMemoryEvidence:
        if self.verification_status == "passed":
            if self.merge_run_id is None:
                raise ValueError("passed writer evidence requires merge_run_id")
            if self.target_commit_ref is None or self.target_tree_digest is None:
                raise ValueError("passed writer evidence requires target commit and tree")
            if not self.verification_artifact_refs or self.verification_digest is None:
                raise ValueError(
                    "passed writer evidence requires verification artifacts and digest"
                )
        if self.state == "eligible" and self.verification_status != "passed":
            raise ValueError("eligible writer evidence requires passed verification")
        if self.state == "published" and self.published_memory_ref is None:
            raise ValueError("published writer evidence requires published_memory_ref")
        if self.published_memory_ref is not None and self.published_memory_ref == self.memory_ref:
            raise ValueError("published memory ref must identify the promoted project version")
        if any(char in self.branch_ref for char in ("\x00", "\n", "\r")):
            raise ValueError("branch_ref contains a control character")
        return self


class WriterPromotionDecision(SharingModel):
    """A read projection of the current promotion gate."""

    evidence_id: Id
    status: Literal["candidate", "eligible", "published", "blocked", "revoked"]
    verification_status: Literal["pending", "passed", "failed", "unknown"]
    reason: Reason | None = None
    merge_run_id: Id | None = None
    target_commit_ref: CommitRef | None = None
    target_tree_digest: Digest | None = None
    verification_artifact_refs: tuple[Id, ...] = ()
    checked_at: AwareDatetime = Field(default_factory=_now)

    @property
    def can_publish(self) -> bool:
        """Convenience projection; the persisted authority remains the evidence."""

        return self.status == "eligible" and self.verification_status == "passed"


class WriterMemoryCandidate(SharingModel):
    """A filtered Run candidate that a project UI may review before binding."""

    evidence_id: Id
    memory: MemoryVersion
    project_id: Id
    workspace_id: Id
    worktree_id: Id
    writer_workspace_id: Id
    graph_run_id: Id
    run_id: Id
    branch_ref: BranchRef
    state: Literal["candidate", "eligible", "published", "blocked", "revoked"]
    revision: int = Field(default=0, ge=0, le=2**53 - 1)


class VisibilityDecision(SharingModel):
    """Explain one current authorization result without exposing hidden data."""

    status: Literal[
        "allowed",
        "denied",
        "expired",
        "revoked",
        "scope_mismatch",
        "permission_epoch_mismatch",
        "registration_missing",
        "not_selected",
    ]
    allowed: bool
    reason_code: Id
    source_ref: MemoryVersionRef | None = None
    grant_id: Id | None = None
    permission_epoch: int | None = None
    checked_at: AwareDatetime = Field(default_factory=_now)

    @model_validator(mode="after")
    def validate_allowed_status(self) -> VisibilityDecision:
        if self.allowed != (self.status == "allowed"):
            raise ValueError("visibility allowed flag does not match status")
        return self


class CleanupBlocker(SharingModel):
    """A precise reason a dataset cannot be removed by one installation."""

    dataset_id: Id
    installation_id: Id
    blocker_id: Id
    reason: Literal["shared_consumer", "active_grant", "pending_transfer", "retention_lock"]


class SharingState(SharingModel):
    contract_version: Literal["operant-b2-6-sharing.v1"] = B26_SHARING_CONTRACT_VERSION
    project_id: Id
    projection_revision: int = Field(default=0, ge=0, le=2**53 - 1)
    worktrees: tuple[ProjectWorktreeRegistration, ...] = ()
    grants: tuple[SharingGrant, ...] = ()
    consumers: tuple[SharingConsumer, ...] = ()
    transfers: tuple[DatasetTransferState, ...] = ()
    writer_evidence: tuple[WriterMemoryEvidence, ...] = ()
    writer_candidates: tuple[WriterMemoryCandidate, ...] = ()
    # Exact published refs are enough for a UI to select a preference for an
    # explicit grant.  The preference body stays in the Core ledger and is
    # never copied into the sharing projection.
    personal_preferences: tuple[MemoryVersion, ...] = ()


class SharingCommand(SharingModel):
    """Core command envelope; feature payloads remain strongly typed."""

    action: Literal[
        "worktree_register",
        "worktree_revoke",
        "grant_create",
        "grant_revoke",
        "dataset_transfer_begin",
        "dataset_transfer_validate",
        "dataset_transfer_commit",
        "dataset_transfer_revoke",
        "consumer_attach",
        "consumer_revoke",
        "personal_preference_create",
        "writer_memory_bind",
        "writer_memory_propose",
        "writer_memory_verify",
        "writer_memory_promote",
        "writer_memory_revoke",
    ]
    project_id: Id
    expected_revision: int | None = Field(default=None, ge=0, le=2**53 - 1)
    reason: Reason | None = None

    registration: ProjectWorktreeRegistration | None = None
    # Public clients may identify an existing Core workspace or provide its
    # absolute path.  The service derives the opaque registration projection
    # and local Git identity from these fields.
    workspace_id: Id | None = None
    workspace_path: Annotated[str, Field(min_length=1, max_length=4_096)] | None = None
    registration_id: Id | None = None
    grant: SharingGrant | None = None
    grant_id: Id | None = None
    transfer: DatasetTransfer | None = None
    transfer_id: Id | None = None
    evidence_id: Id | None = None
    memory_ref: MemoryVersionRef | None = None
    content: Annotated[str, Field(min_length=1, max_length=100_000)] | None = None
    source_refs: tuple[SourceRef, ...] = Field(default=(), max_length=200)
    content_type: Literal["fact", "preference", "episode", "procedure"] | None = None
    record_id: Id | None = None
    sensitivity: Literal["public", "internal", "sensitive"] | None = None
    worktree_id: Id | None = None
    writer_workspace_id: Id | None = None
    graph_run_id: Id | None = None
    run_id: Id | None = None
    branch_ref: BranchRef | None = None
    merge_run_id: Id | None = None
    verification_artifact_refs: tuple[Id, ...] = Field(default=(), max_length=200)
    dataset_id: Id | None = None
    installation_id: Id | None = None
    consumer_id: Id | None = None
    consumer_grant_id: Id | None = None
    consumer_purpose: Literal["primary_engine", "shared_read", "export", "migration"] | None = None

    @model_validator(mode="after")
    def validate_worktree_input(self) -> SharingCommand:
        if self.action != "worktree_register":
            return self
        if self.workspace_path is not None and not self.workspace_path.startswith("/"):
            raise ValueError("workspace_path must be absolute")
        if self.workspace_id is not None and self.workspace_path is not None:
            raise ValueError("worktree registration accepts workspace_id or workspace_path")
        if self.registration is None and self.workspace_id is None and self.workspace_path is None:
            raise ValueError(
                "worktree_register requires workspace_id, workspace_path or registration"
            )
        if (
            self.registration is None
            and self.workspace_path is not None
            and self.branch_ref is None
        ):
            raise ValueError("workspace_path registration requires branch_ref")
        return self


class SharingResult(SharingModel):
    status: Literal["completed", "blocked", "revoked", "failed"]
    message: str = Field(min_length=1, max_length=2_000)
    affected_ids: tuple[Id, ...] = ()
    state: SharingState | None = None


__all__ = [
    "B26_SHARING_CONTRACT_VERSION",
    "CleanupBlocker",
    "DatasetTransferState",
    "ProjectWorktreeRegistration",
    "SharingCommand",
    "SharingConsumer",
    "SharingGrant",
    "SharingResult",
    "SharingState",
    "VisibilityDecision",
    "WriterMemoryCandidate",
    "WriterMemoryEvidence",
    "WriterPromotionDecision",
]
