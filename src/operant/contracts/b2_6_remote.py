"""Typed contract for the MP-5.4 Remote Memory boundary.

These objects deliberately describe the narrow package exchange, not the
whole memory-management API.  Existing B2-3/B2-5 commands remain the only
route for proposing, reviewing and publishing knowledge.  A Target can
receive a bounded package and submit observations/candidates for Core review;
it cannot publish a memory version by sending this contract back.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from operant.contracts.b2_1 import MemoryVersionRef, SourceRef


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_remote_memory_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class RemoteMemoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


RemoteMemoryAction = Literal[
    "remote_pack_create",
    "remote_pack_revoke",
    "remote_upload_review",
]
RemoteMemoryStatus = Literal[
    "ready",
    "completed",
    "pending_review",
    "rejected",
    "revoked",
    "expired",
    "outcome_unknown",
]
RemoteMemoryContentType = Literal["fact", "preference", "episode", "procedure"]
RemoteMemorySensitivity = Literal["public", "internal"]


class RemoteMemoryRecord(RemoteMemoryModel):
    """A Core-authorized current version exposed by ``state`` or a Query."""

    ref: MemoryVersionRef
    content: str = Field(min_length=1, max_length=100_000)
    content_type: RemoteMemoryContentType
    sensitivity: RemoteMemorySensitivity
    source_refs: tuple[SourceRef, ...] = Field(default=(), max_length=32)
    head_revision: int = Field(ge=0, le=2**53 - 1)
    currently_usable: bool = True


class RemoteMemoryPackEntry(RemoteMemoryModel):
    """The minimum memory material needed by one remote execution Target."""

    ref: MemoryVersionRef
    content: str = Field(min_length=1, max_length=100_000)
    content_type: RemoteMemoryContentType
    sensitivity: RemoteMemorySensitivity
    # SourceRef contains identity, digest, scope and permission epoch, but no
    # source body.  It is therefore sufficient for a Core to re-check an
    # upload without copying source text into the Relay.
    source_refs: tuple[SourceRef, ...] = Field(default=(), max_length=32)


class RemoteMemoryPack(RemoteMemoryModel):
    """An immutable, purpose- and expiry-bound package for a Target."""

    package_id: str = Field(
        default_factory=lambda: new_remote_memory_id("remote_pack"),
        min_length=1,
        max_length=200,
    )
    package_digest: str = Field(
        default="0" * 64,
        pattern=r"^[0-9a-f]{64}$",
    )
    project_id: str = Field(min_length=1, max_length=200)
    dataset_id: str = Field(min_length=1, max_length=200)
    target_id: str = Field(min_length=1, max_length=200)
    purpose: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z][A-Za-z0-9_.:-]{0,99}$",
    )
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    binding_epoch: int = Field(ge=0, le=2**53 - 1)
    permission_epoch: int = Field(ge=0, le=2**53 - 1)
    revocation_epoch: int = Field(ge=0, le=2**53 - 1)
    entries: tuple[RemoteMemoryPackEntry, ...] = Field(default=(), max_length=50)
    token_count: int = Field(ge=0, le=1_000_000)
    byte_count: int = Field(ge=0, le=16_777_216)
    max_tokens: int = Field(ge=0, le=1_000_000)
    max_bytes: int = Field(ge=0, le=16_777_216)
    status: Literal["active", "revoked", "expired"] = "active"

    @model_validator(mode="after")
    def validate_bounds(self) -> RemoteMemoryPack:
        if self.expires_at <= self.issued_at:
            raise ValueError("remote memory package expiry must follow issuance")
        if self.expires_at - self.issued_at > timedelta(minutes=5):
            raise ValueError("remote memory package TTL exceeds five minutes")
        if self.token_count > self.max_tokens:
            raise ValueError("remote memory package exceeds token budget")
        if self.byte_count > self.max_bytes:
            raise ValueError("remote memory package exceeds byte budget")
        if any(entry.ref.dataset_id != self.dataset_id for entry in self.entries):
            raise ValueError("remote memory package contains a foreign dataset reference")
        return self

    def digest_payload(self) -> dict[str, Any]:
        """Return immutable fields used to calculate the package digest."""

        return self.model_dump(mode="json", exclude={"package_digest", "status"})

    def calculated_digest(self) -> str:
        encoded = json.dumps(
            self.digest_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def has_valid_digest(self) -> bool:
        return self.package_digest == self.calculated_digest()


class RemoteMemoryPackSummary(RemoteMemoryModel):
    package_id: str
    package_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    project_id: str
    dataset_id: str
    target_id: str
    purpose: str
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    status: Literal["active", "revoked", "expired"]
    entry_count: int = Field(ge=0)


class RemoteMemoryCandidate(RemoteMemoryModel):
    """Candidate text returned by a Target for local Core review."""

    record_id: str | None = Field(default=None, min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=100_000)
    content_type: RemoteMemoryContentType = "procedure"
    source_refs: tuple[SourceRef, ...] = Field(default=(), max_length=32)
    reason: str = Field(min_length=1, max_length=2_000)
    expected_head_revision: int | None = Field(default=None, ge=0, le=2**53 - 1)


class RemoteMemoryUpload(RemoteMemoryModel):
    """Target upload envelope; it is never a publication request."""

    upload_id: str = Field(
        default_factory=lambda: new_remote_memory_id("remote_upload"),
        min_length=1,
        max_length=200,
    )
    package_id: str = Field(min_length=1, max_length=200)
    package_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    project_id: str = Field(min_length=1, max_length=200)
    dataset_id: str = Field(min_length=1, max_length=200)
    target_id: str = Field(min_length=1, max_length=200)
    purpose: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z][A-Za-z0-9_.:-]{0,99}$",
    )
    candidates: tuple[RemoteMemoryCandidate, ...] = Field(default=(), max_length=50)
    source_refs: tuple[SourceRef, ...] = Field(default=(), max_length=64)
    source_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    submitted_at: AwareDatetime = Field(default_factory=utc_now)

    def calculated_source_digest(self) -> str:
        payload = {
            "package_id": self.package_id,
            "package_digest": self.package_digest,
            "project_id": self.project_id,
            "dataset_id": self.dataset_id,
            "target_id": self.target_id,
            "purpose": self.purpose,
            "candidates": [item.model_dump(mode="json") for item in self.candidates],
            "source_refs": [item.model_dump(mode="json") for item in self.source_refs],
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def has_valid_source_digest(self) -> bool:
        return self.source_digest is None or self.source_digest == self.calculated_source_digest()


class RemoteMemoryCommand(RemoteMemoryModel):
    """Only the three MP-5.4 remote operations cross the Remote Control link."""

    action: RemoteMemoryAction
    project_id: str = Field(min_length=1, max_length=200)
    target_id: str = Field(min_length=1, max_length=200)
    purpose: str = Field(
        default="remote_execution",
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z][A-Za-z0-9_.:-]{0,99}$",
    )
    ttl_seconds: int = Field(default=120, ge=1, le=300)
    query: str | None = Field(default=None, min_length=1, max_length=1_000)
    record_ids: tuple[str, ...] = Field(default=(), max_length=50)
    limit: int = Field(default=50, ge=1, le=50)
    max_tokens: int = Field(default=8_000, ge=1, le=1_000_000)
    max_bytes: int = Field(default=256_000, ge=1_024, le=16_777_216)
    package_id: str | None = Field(default=None, min_length=1, max_length=200)
    package_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    upload: RemoteMemoryUpload | None = None
    # Optional local execution binding.  When present, the service verifies
    # the session/Agent and applies its RoleSnapshot restrictions.
    session_id: str | None = Field(default=None, min_length=1, max_length=200)
    agent_instance_id: str | None = Field(default=None, min_length=1, max_length=200)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=300)

    @model_validator(mode="after")
    def validate_operation_fields(self) -> RemoteMemoryCommand:
        if self.action == "remote_pack_revoke":
            if self.package_id is None or self.package_digest is None:
                raise ValueError("remote_pack_revoke requires package_id and package_digest")
            if self.query is not None or self.record_ids or self.upload is not None:
                raise ValueError("remote_pack_revoke does not accept package contents")
        elif self.action == "remote_upload_review":
            if self.upload is None:
                raise ValueError("remote_upload_review requires an upload")
            if self.upload.project_id != self.project_id or self.upload.target_id != self.target_id:
                raise ValueError("remote upload identity does not match the command")
        elif self.action == "remote_pack_create":
            if (
                self.package_id is not None
                or self.package_digest is not None
                or self.upload is not None
            ):
                raise ValueError("remote_pack_create does not accept an existing package or upload")
        return self


class RemoteMemoryState(RemoteMemoryModel):
    """Core projection returned after a remote package or upload operation."""

    action: Literal[
        "state",
        "remote_pack_create",
        "remote_pack_revoke",
        "remote_upload_review",
    ]
    project_id: str
    dataset_id: str | None = None
    status: RemoteMemoryStatus
    message: str
    records: tuple[RemoteMemoryRecord, ...] = Field(default=(), max_length=50)
    packs: tuple[RemoteMemoryPackSummary, ...] = Field(default=(), max_length=50)
    pack: RemoteMemoryPack | None = None
    upload_id: str | None = None
    affected_ids: tuple[str, ...] = Field(default=(), max_length=100)
    result_ref: str | None = Field(default=None, max_length=500)
    permission_epoch: int | None = Field(default=None, ge=0, le=2**53 - 1)
    binding_epoch: int | None = Field(default=None, ge=0, le=2**53 - 1)
    expires_at: AwareDatetime | None = None


__all__ = [
    "RemoteMemoryAction",
    "RemoteMemoryCandidate",
    "RemoteMemoryCommand",
    "RemoteMemoryContentType",
    "RemoteMemoryPack",
    "RemoteMemoryPackEntry",
    "RemoteMemoryPackSummary",
    "RemoteMemoryRecord",
    "RemoteMemorySensitivity",
    "RemoteMemoryState",
    "RemoteMemoryStatus",
    "RemoteMemoryUpload",
    "new_remote_memory_id",
    "utc_now",
]
