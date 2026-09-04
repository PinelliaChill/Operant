from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_remote_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _aware_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


class RemoteScope(str, Enum):
    OBSERVE = "remote.control.observe"
    COMMAND = "remote.control.command"
    APPROVE = "remote.control.approve"
    BROWSER = "remote.control.browser"
    COMPUTER = "remote.control.computer"
    SETTINGS = "remote.control.settings"


class RemoteHostOnlineState(str, Enum):
    OFFLINE = "offline"
    ONLINE = "online"
    DEGRADED = "degraded"


class RemoteTransportMode(str, Enum):
    DIRECT = "direct"
    RELAY = "relay"


class RemoteSessionState(str, Enum):
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    CLOSED = "closed"


class RemoteCommandStatus(str, Enum):
    RECEIVED = "received"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    COMPLETED = "completed"
    OUTCOME_UNKNOWN = "outcome_unknown"


class RelayEnvelopeStatus(str, Enum):
    QUEUED = "queued"
    DELIVERED = "delivered"
    ACKNOWLEDGED = "acknowledged"
    EXPIRED = "expired"


class HostInstance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    host_id: str = Field(
        default_factory=lambda: new_remote_id("host"), min_length=1, max_length=200
    )
    display_name: str = Field(min_length=1, max_length=200)
    signing_public_key: str = Field(min_length=32, max_length=500)
    exchange_public_key: str = Field(min_length=32, max_length=500)
    core_version: str = Field(min_length=1, max_length=100)
    protocol_version: str = Field(min_length=1, max_length=100)
    capabilities: tuple[str, ...] = ()
    enabled: bool = False
    online_state: RemoteHostOnlineState = RemoteHostOnlineState.OFFLINE
    last_seen_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("last_seen_at", "created_at", "updated_at")
    @classmethod
    def validate_timestamps(cls, value: datetime | None, info: Any) -> datetime | None:
        if value is None:
            return None
        return _aware_utc(value, field_name=info.field_name)


class PairingChallenge(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    challenge_id: str = Field(default_factory=lambda: new_remote_id("pairing"))
    host_id: str = Field(min_length=1, max_length=200)
    code_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    allowed_scopes: tuple[RemoteScope, ...] = (RemoteScope.OBSERVE,)
    expires_at: datetime
    max_uses: int = Field(default=1, ge=1, le=5)
    uses: int = Field(default=0, ge=0, le=5)
    consumed_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("expires_at", "consumed_at", "created_at")
    @classmethod
    def validate_timestamps(cls, value: datetime | None, info: Any) -> datetime | None:
        if value is None:
            return None
        return _aware_utc(value, field_name=info.field_name)

    @model_validator(mode="after")
    def validate_usage(self) -> PairingChallenge:
        if self.uses > self.max_uses:
            raise ValueError("pairing challenge uses exceed max_uses")
        if self.expires_at <= self.created_at:
            raise ValueError("pairing challenge expiry must follow creation")
        if not self.allowed_scopes or RemoteScope.OBSERVE not in self.allowed_scopes:
            raise ValueError("pairing challenge must allow the observe scope")
        if len(set(self.allowed_scopes)) != len(self.allowed_scopes):
            raise ValueError("pairing challenge scopes must be unique")
        return self


class PairingTicket(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    challenge_id: str
    host_id: str
    one_time_code: str = Field(min_length=16, max_length=200)
    expires_at: datetime
    allowed_scopes: tuple[RemoteScope, ...]
    relay_url: str | None = Field(default=None, max_length=2_000)
    host_signing_public_key: str = Field(min_length=32, max_length=500)
    host_exchange_public_key: str = Field(min_length=32, max_length=500)


class RemoteDevice(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    device_id: str = Field(default_factory=lambda: new_remote_id("device"))
    host_id: str = Field(min_length=1, max_length=200)
    display_name: str = Field(min_length=1, max_length=200)
    signing_public_key: str = Field(min_length=32, max_length=500)
    exchange_public_key: str = Field(min_length=32, max_length=500)
    scopes: tuple[RemoteScope, ...] = (RemoteScope.OBSERVE,)
    version: int = Field(default=1, ge=1)
    paired_at: datetime = Field(default_factory=utc_now)
    last_seen_at: datetime | None = None
    revoked_at: datetime | None = None

    @field_validator("paired_at", "last_seen_at", "revoked_at")
    @classmethod
    def validate_timestamps(cls, value: datetime | None, info: Any) -> datetime | None:
        if value is None:
            return None
        return _aware_utc(value, field_name=info.field_name)


class RemoteSession(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    remote_session_id: str = Field(default_factory=lambda: new_remote_id("remote_session"))
    host_id: str = Field(min_length=1, max_length=200)
    device_id: str = Field(min_length=1, max_length=200)
    transport_mode: RemoteTransportMode
    protocol_version: str = Field(min_length=1, max_length=100)
    event_cursor: int = Field(default=0, ge=0, le=2**63 - 1)
    connection_state: RemoteSessionState = RemoteSessionState.CONNECTING
    session_key_ref: str = Field(min_length=1, max_length=300)
    expires_at: datetime
    connected_at: datetime | None = None
    disconnected_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("expires_at", "connected_at", "disconnected_at", "created_at")
    @classmethod
    def validate_timestamps(cls, value: datetime | None, info: Any) -> datetime | None:
        if value is None:
            return None
        return _aware_utc(value, field_name=info.field_name)

    @model_validator(mode="after")
    def validate_expiry(self) -> RemoteSession:
        if self.expires_at <= self.created_at:
            raise ValueError("remote session expiry must follow creation")
        return self


class EncryptedRemoteCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    command_id: str = Field(default_factory=lambda: new_remote_id("remote_command"))
    idempotency_key: str = Field(min_length=1, max_length=300)
    host_id: str = Field(min_length=1, max_length=200)
    device_id: str = Field(min_length=1, max_length=200)
    remote_session_id: str = Field(min_length=1, max_length=200)
    protocol_version: str = Field(min_length=1, max_length=100)
    issued_at: datetime
    expires_at: datetime
    nonce: str = Field(min_length=16, max_length=200)
    ciphertext: str = Field(min_length=1, max_length=1_400_000)
    signature: str = Field(min_length=32, max_length=500)

    @field_validator("issued_at", "expires_at")
    @classmethod
    def validate_timestamps(cls, value: datetime, info: Any) -> datetime:
        return _aware_utc(value, field_name=info.field_name)

    @model_validator(mode="after")
    def validate_expiry(self) -> EncryptedRemoteCommand:
        if self.expires_at <= self.issued_at:
            raise ValueError("remote command expiry must follow issuance")
        return self


class RemoteCommandReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    command_id: str
    idempotency_key: str
    action_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: RemoteCommandStatus
    host_acknowledged_at: datetime | None = None
    result_ref: str | None = Field(default=None, max_length=500)
    error_code: str | None = Field(default=None, max_length=200)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class RelayEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    envelope_id: str = Field(default_factory=lambda: new_remote_id("relay_envelope"))
    route_ref: str = Field(min_length=1, max_length=300)
    sender_ref: str = Field(min_length=1, max_length=300)
    recipient_ref: str = Field(min_length=1, max_length=300)
    protocol_version: str = Field(min_length=1, max_length=100)
    ciphertext: str = Field(min_length=1, max_length=1_400_000)
    nonce: str = Field(min_length=16, max_length=200)
    expires_at: datetime
    status: RelayEnvelopeStatus = RelayEnvelopeStatus.QUEUED
    created_at: datetime = Field(default_factory=utc_now)
    delivered_at: datetime | None = None
    acknowledged_at: datetime | None = None

    @field_validator("expires_at", "created_at", "delivered_at", "acknowledged_at")
    @classmethod
    def validate_timestamps(cls, value: datetime | None, info: Any) -> datetime | None:
        if value is None:
            return None
        return _aware_utc(value, field_name=info.field_name)

    @model_validator(mode="after")
    def validate_expiry(self) -> RelayEnvelope:
        if self.expires_at <= self.created_at:
            raise ValueError("relay envelope expiry must follow creation")
        return self
