"""Encrypted, ephemeral response contract for the B2-6 Remote Query route."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_B26_REMOTE_QUERY_REPLY_SECONDS = 60
MAX_B26_REMOTE_QUERY_CIPHERTEXT_CHARS = 350_000


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class B26EncryptedReply(BaseModel):
    """A short-lived encrypted response; plaintext is never persisted here."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    command_id: str = Field(min_length=1, max_length=300)
    host_id: str = Field(min_length=1, max_length=200)
    device_id: str = Field(min_length=1, max_length=200)
    remote_session_id: str = Field(min_length=1, max_length=200)
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    nonce: str = Field(min_length=16, max_length=200)
    ciphertext: str = Field(min_length=1, max_length=MAX_B26_REMOTE_QUERY_CIPHERTEXT_CHARS)

    @field_validator("issued_at", "expires_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime, info: object) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{getattr(info, 'field_name', 'timestamp')} must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_expiry(self) -> B26EncryptedReply:
        if self.expires_at <= self.issued_at:
            raise ValueError("encrypted reply expiry must follow issuance")
        if self.expires_at - self.issued_at > timedelta(seconds=MAX_B26_REMOTE_QUERY_REPLY_SECONDS):
            raise ValueError("encrypted reply TTL exceeds one minute")
        return self


def reply_metadata(reply: B26EncryptedReply) -> dict[str, str]:
    """Return the fields authenticated as AEAD associated data."""

    return {
        "command_id": reply.command_id,
        "host_id": reply.host_id,
        "device_id": reply.device_id,
        "remote_session_id": reply.remote_session_id,
        "issued_at": reply.issued_at.isoformat(),
        "expires_at": reply.expires_at.isoformat(),
        "nonce": reply.nonce,
    }


__all__ = [
    "B26EncryptedReply",
    "MAX_B26_REMOTE_QUERY_CIPHERTEXT_CHARS",
    "MAX_B26_REMOTE_QUERY_REPLY_SECONDS",
    "reply_metadata",
    "utc_now",
]
