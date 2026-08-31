from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final

from operant.domain.threads import ArtifactAccessLevel, ArtifactSensitivity

_TOKEN_PREFIX: Final = "operant-artifact-v1"
_ACCESS_RANK = {
    ArtifactAccessLevel.NORMAL: 0,
    ArtifactAccessLevel.SENSITIVE: 1,
    ArtifactAccessLevel.RESTRICTED: 2,
}
_SENSITIVITY_RANK = {
    ArtifactSensitivity.NORMAL: 0,
    ArtifactSensitivity.SENSITIVE: 1,
    ArtifactSensitivity.RESTRICTED: 2,
}


class ArtifactCapabilityError(PermissionError):
    pass


@dataclass(frozen=True, slots=True)
class ArtifactCapabilityAuthority:
    """Issue and verify short-lived, object-scoped local Core capabilities."""

    secret: bytes

    def __post_init__(self) -> None:
        if len(self.secret) < 32:
            raise ValueError("Artifact capability secret must be at least 32 bytes")

    def issue(
        self,
        *,
        artifact_id: str,
        operation: str,
        access_level: ArtifactAccessLevel,
        ttl_seconds: int = 300,
        operation_scope_hash: str | None = None,
    ) -> str:
        if operation not in {
            "read",
            "download",
            "export",
            "physical_delete",
            "repair_orphan",
            "reconcile_delete",
            "retention_pin",
            "retention_archive",
            "retention_schedule",
            "retention_trash",
            "retention_restore",
        }:
            raise ValueError("unsupported Artifact capability operation")
        if not 1 <= ttl_seconds <= 3600:
            raise ValueError("Artifact capability TTL must be between 1 and 3600 seconds")
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        payload = {
            "artifact_id": artifact_id,
            "operation": operation,
            "access_level": access_level.value,
            "expires_at": int(expires_at.timestamp()),
            "operation_scope_hash": operation_scope_hash,
        }
        encoded = _urlsafe_encode(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        signature = hmac.new(
            self.secret,
            f"{_TOKEN_PREFIX}.{encoded}".encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        return f"{_TOKEN_PREFIX}.{encoded}.{signature}"

    def verify(
        self,
        token: str,
        *,
        artifact_id: str,
        operation: str,
        sensitivity: ArtifactSensitivity,
        operation_scope_hash: str | None = None,
    ) -> None:
        try:
            prefix, encoded, signature = token.split(".", 2)
            expected = hmac.new(
                self.secret,
                f"{prefix}.{encoded}".encode("ascii"),
                hashlib.sha256,
            ).hexdigest()
            if prefix != _TOKEN_PREFIX or not hmac.compare_digest(signature, expected):
                raise ArtifactCapabilityError("Artifact capability is invalid")
            payload = json.loads(_urlsafe_decode(encoded))
            access = ArtifactAccessLevel(payload["access_level"])
            expires_at = int(payload["expires_at"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise ArtifactCapabilityError("Artifact capability is invalid") from None
        if (
            payload.get("artifact_id") != artifact_id
            or payload.get("operation") != operation
            or payload.get("operation_scope_hash") != operation_scope_hash
            or expires_at <= int(datetime.now(timezone.utc).timestamp())
            or _ACCESS_RANK[access] < _SENSITIVITY_RANK[sensitivity]
        ):
            raise ArtifactCapabilityError("Artifact capability does not authorize this operation")


def _urlsafe_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _urlsafe_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)
