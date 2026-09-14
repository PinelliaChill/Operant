"""Stdlib-only readers for the frozen ``operant-memory-sdk.v1`` DTOs.

These DTOs are deliberately small.  The Host performs the authoritative
Pydantic validation and permission checks; the isolated package uses these
readers to reject malformed frames before doing work, without importing a
third-party package under ``-I -S``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


class DTOError(ValueError):
    """Raised when an isolated DTO is not an object with the required fields."""


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DTOError(f"{label} must be an object")
    return dict(value)


def _text(value: Any, label: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise DTOError(f"{label} must be non-empty text")
    return value


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise DTOError(f"{label} must be an integer >= {minimum}")
    return value


@dataclass(frozen=True, slots=True)
class RpcContextDTO:
    sdk_version: str
    request_id: str
    installation_id: str
    dataset_id: str
    scope: dict[str, Any]
    deadline: str
    cancel_token: str
    idempotency_key: str
    request_digest: str
    binding_epoch: int
    permission_epoch: int
    lease_fencing: int

    @classmethod
    def from_dict(cls, value: Any) -> RpcContextDTO:
        raw = _object(value, "context")
        fields = {
            key: _text(raw.get(key), key)
            for key in (
                "sdk_version",
                "request_id",
                "installation_id",
                "dataset_id",
                "deadline",
                "cancel_token",
                "idempotency_key",
                "request_digest",
            )
        }
        scope = _object(raw.get("scope"), "scope")
        epochs = {
            key: _integer(raw.get(key), key)
            for key in ("binding_epoch", "permission_epoch", "lease_fencing")
        }
        return cls(scope=scope, **fields, **epochs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sdk_version": self.sdk_version,
            "request_id": self.request_id,
            "installation_id": self.installation_id,
            "dataset_id": self.dataset_id,
            "scope": dict(self.scope),
            "deadline": self.deadline,
            "cancel_token": self.cancel_token,
            "idempotency_key": self.idempotency_key,
            "request_digest": self.request_digest,
            "binding_epoch": self.binding_epoch,
            "permission_epoch": self.permission_epoch,
            "lease_fencing": self.lease_fencing,
        }


@dataclass(frozen=True, slots=True)
class SourceRefDTO:
    source_type: str
    source_id: str
    revision: int
    content_digest: str
    scope: dict[str, Any]
    permission_epoch: int
    availability: str

    @classmethod
    def from_dict(cls, value: Any) -> SourceRefDTO:
        raw = _object(value, "source")
        return cls(
            source_type=_text(raw.get("source_type"), "source_type"),
            source_id=_text(raw.get("source_id"), "source_id"),
            revision=_integer(raw.get("revision"), "revision"),
            content_digest=_text(raw.get("content_digest"), "content_digest"),
            scope=_object(raw.get("scope"), "scope"),
            permission_epoch=_integer(raw.get("permission_epoch"), "permission_epoch"),
            availability=_text(raw.get("availability"), "availability"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "source_id": self.source_id,
            "revision": self.revision,
            "content_digest": self.content_digest,
            "scope": dict(self.scope),
            "permission_epoch": self.permission_epoch,
            "availability": self.availability,
        }


@dataclass(frozen=True, slots=True)
class MemoryVersionRefDTO:
    dataset_id: str
    record_id: str
    version: int
    content_digest: str

    @classmethod
    def from_dict(cls, value: Any) -> MemoryVersionRefDTO:
        raw = _object(value, "memory version reference")
        return cls(
            dataset_id=_text(raw.get("dataset_id"), "dataset_id"),
            record_id=_text(raw.get("record_id"), "record_id"),
            version=_integer(raw.get("version"), "version", minimum=1),
            content_digest=_text(raw.get("content_digest"), "content_digest"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "record_id": self.record_id,
            "version": self.version,
            "content_digest": self.content_digest,
        }


@dataclass(frozen=True, slots=True)
class CandidateReferenceDTO:
    ref: MemoryVersionRefDTO
    score: float

    @classmethod
    def from_dict(cls, value: Any) -> CandidateReferenceDTO:
        raw = _object(value, "candidate")
        score = raw.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 1:
            raise DTOError("candidate score must be a number between 0 and 1")
        return cls(ref=MemoryVersionRefDTO.from_dict(raw.get("ref")), score=float(score))

    def to_dict(self) -> dict[str, Any]:
        return {"ref": self.ref.to_dict(), "score": self.score}


@dataclass(frozen=True, slots=True)
class PrivateIndexResourceDTO:
    resource_id: str
    owner: dict[str, Any]
    installation_id: str
    storage: str
    category: str
    locator_ref: str
    consumer_ids: tuple[str, ...]
    retention_lock_ids: tuple[str, ...]
    reconstructible: bool

    @classmethod
    def from_dict(cls, value: Any) -> PrivateIndexResourceDTO:
        raw = _object(value, "private index resource")
        consumers = raw.get("consumer_ids", ())
        locks = raw.get("retention_lock_ids", ())
        if not isinstance(consumers, (list, tuple)) or not all(
            isinstance(item, str) and item for item in consumers
        ):
            raise DTOError("consumer_ids must be text identifiers")
        if not isinstance(locks, (list, tuple)) or not all(
            isinstance(item, str) and item for item in locks
        ):
            raise DTOError("retention_lock_ids must be text identifiers")
        reconstructible = raw.get("reconstructible")
        if not isinstance(reconstructible, bool):
            raise DTOError("reconstructible must be boolean")
        return cls(
            resource_id=_text(raw.get("resource_id"), "resource_id"),
            owner=_object(raw.get("owner"), "owner"),
            installation_id=_text(raw.get("installation_id"), "installation_id"),
            storage=_text(raw.get("storage"), "storage"),
            category=_text(raw.get("category"), "category"),
            locator_ref=_text(raw.get("locator_ref"), "locator_ref"),
            consumer_ids=tuple(consumers),
            retention_lock_ids=tuple(locks),
            reconstructible=reconstructible,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "owner": dict(self.owner),
            "installation_id": self.installation_id,
            "storage": self.storage,
            "category": self.category,
            "locator_ref": self.locator_ref,
            "consumer_ids": list(self.consumer_ids),
            "retention_lock_ids": list(self.retention_lock_ids),
            "reconstructible": self.reconstructible,
        }


@dataclass(frozen=True, slots=True)
class PrivateIndexResultDTO:
    resource_id: str
    revision: int
    content_digest: str | None
    payload: str | None

    @classmethod
    def from_dict(cls, value: Any) -> PrivateIndexResultDTO:
        raw = _object(value, "private index result")
        digest = raw.get("content_digest")
        payload = raw.get("payload")
        if digest is not None and not isinstance(digest, str):
            raise DTOError("content_digest must be text or null")
        if payload is not None and not isinstance(payload, str):
            raise DTOError("payload must be text or null")
        return cls(
            resource_id=_text(raw.get("resource_id"), "resource_id"),
            revision=_integer(raw.get("revision"), "revision"),
            content_digest=digest,
            payload=payload,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "revision": self.revision,
            "content_digest": self.content_digest,
            "payload": self.payload,
        }


RpcContext = RpcContextDTO
SourceRef = SourceRefDTO
MemoryVersionRef = MemoryVersionRefDTO
CandidateReference = CandidateReferenceDTO
PrivateIndexResource = PrivateIndexResourceDTO
PrivateIndexResult = PrivateIndexResultDTO


__all__ = [
    "CandidateReference",
    "CandidateReferenceDTO",
    "DTOError",
    "MemoryVersionRef",
    "MemoryVersionRefDTO",
    "PrivateIndexResource",
    "PrivateIndexResourceDTO",
    "PrivateIndexResult",
    "PrivateIndexResultDTO",
    "RpcContext",
    "RpcContextDTO",
    "SourceRef",
    "SourceRefDTO",
]
