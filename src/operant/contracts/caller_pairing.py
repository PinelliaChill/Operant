"""Additive, narrowly scoped identity contract for independent local clients."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

CALLER_PAIRING_PROTOCOL: Literal["caller-pairing.v1"] = "caller-pairing.v1"
SKILL_SOURCE_SCOPE: Literal["skill_source.manage"] = "skill_source.manage"


class CallerModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


PublicKey = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{87}$")]
Signature = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{86}$")]
Nonce = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{16}$")]
Identity = Annotated[str, Field(pattern=r"^(?:core|caller|ticket)_[0-9a-f]{32}$")]
RequestId = Annotated[str, Field(min_length=1, max_length=300, pattern=r"^[\x21-\x7e]+$")]
EpochSeconds = Annotated[int, Field(strict=True, ge=1, le=2**53 - 1)]


class CallerChallengeInput(CallerModel):
    ttl_seconds: int = Field(default=120, ge=30, le=300)


class CallerPairingTicket(CallerModel):
    protocol_version: Literal["caller-pairing.v1"] = CALLER_PAIRING_PROTOCOL
    ticket_id: Identity
    core_epoch_id: Identity
    base_url: str = Field(min_length=1, max_length=100)
    core_exchange_public_key: PublicKey
    core_signing_public_key: PublicKey
    scope: Literal["skill_source.manage"] = SKILL_SOURCE_SCOPE
    expires_at: EpochSeconds
    one_time_code: str = Field(min_length=32, max_length=100)


class CallerPairRequest(CallerModel):
    protocol_version: Literal["caller-pairing.v1"] = CALLER_PAIRING_PROTOCOL
    pair_request_id: RequestId
    ticket_id: Identity
    core_epoch_id: Identity
    device_id: Identity
    device_signing_public_key: PublicKey
    device_exchange_public_key: PublicKey
    scope: Literal["skill_source.manage"] = SKILL_SOURCE_SCOPE
    issued_at: EpochSeconds
    expires_at: EpochSeconds
    nonce: Nonce
    ciphertext: str = Field(min_length=22, max_length=24_000, pattern=r"^[A-Za-z0-9_-]+$")
    signature: Signature


class CallerCommand(CallerModel):
    protocol_version: Literal["caller-pairing.v1"] = CALLER_PAIRING_PROTOCOL
    core_epoch_id: Identity
    device_id: Identity
    request_id: RequestId
    operation: Literal["list", "add", "remove", "readback", "continue"]
    issued_at: EpochSeconds
    expires_at: EpochSeconds
    nonce: Nonce
    ciphertext: str = Field(min_length=22, max_length=24_000, pattern=r"^[A-Za-z0-9_-]+$")
    signature: Signature


class CallerEncryptedReply(CallerModel):
    protocol_version: Literal["caller-pairing.v1"] = CALLER_PAIRING_PROTOCOL
    core_epoch_id: Identity
    device_id: Identity
    request_id: RequestId
    operation: Literal["pair", "list", "add", "remove", "readback", "continue"]
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    issued_at: EpochSeconds
    expires_at: EpochSeconds
    nonce: Nonce
    ciphertext: str = Field(min_length=22, max_length=1_400_000, pattern=r"^[A-Za-z0-9_-]+$")
    signature: Signature


class CallerDeviceView(CallerModel):
    device_id: Identity
    display_name: str = Field(min_length=1, max_length=100)
    signing_key_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    core_epoch_id: Identity
    scope: Literal["skill_source.manage"] = SKILL_SOURCE_SCOPE
    expires_at: EpochSeconds
    state: Literal["active", "expired", "revoked", "needs_pairing"]


class CallerDeviceList(CallerModel):
    items: list[CallerDeviceView]


class CallerReceiptView(CallerModel):
    """Plaintext only inside an authenticated reply or trusted native readback."""

    request_id: RequestId
    device_id: Identity
    operation: Literal["list", "add", "remove"]
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: Literal["in_progress", "awaiting_approval", "completed", "failed", "unconfirmed"]
    http_status: int = Field(ge=100, le=599)
    result: dict[str, object] | None = None
    approval_id: str | None = Field(default=None, max_length=200)
    action_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    error_code: str | None = Field(default=None, max_length=100)


# These values are decrypted only after peer authentication. They are not grants
# of arbitrary tool, filesystem or model access.
class CallerPairPayload(CallerModel):
    one_time_code: str = Field(min_length=32, max_length=100)
    display_name: str = Field(min_length=1, max_length=100)


class CallerListPayload(CallerModel):
    pass


class CallerAddPayload(CallerModel):
    path: str = Field(min_length=1, max_length=4096)


class CallerRemovePayload(CallerModel):
    root_ref: str = Field(min_length=1, max_length=200)


class CallerReadbackPayload(CallerModel):
    original_request_id: RequestId


class CallerContinuePayload(CallerModel):
    original_request_id: RequestId
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    approval_id: str = Field(min_length=1, max_length=200)
    action_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
