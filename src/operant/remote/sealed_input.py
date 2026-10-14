"""Seal browser text so durable Core Jobs contain ciphertext, never input text."""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_PREFIX = "operant-browser-input.v1:"
_RETRY_SEALS: ContextVar[dict[str, tuple[bytes, str]] | None] = ContextVar(
    "operant_local_tool_retry_seals", default=None
)


@contextmanager
def bound_sealed_input_retry() -> Iterator[None]:
    """Reuse one random envelope only during this Tool's exact ASK continuation."""

    cache: dict[str, tuple[bytes, str]] = {}
    binding = _RETRY_SEALS.set(cache)
    try:
        yield
    finally:
        cache.clear()
        _RETRY_SEALS.reset(binding)


def _key(token: str, target_id: str, lease_id: str, fencing: int) -> bytes:
    if len(token) < 16:
        raise ValueError("browser input lease token is incomplete")
    salt = json.dumps(
        [target_id, lease_id, fencing], ensure_ascii=False, separators=(",", ":")
    ).encode()
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=b"operant-browser-input.v1",
    ).derive(token.encode())


def _aad(*, target_id: str, observation_hash: str, selector: str, idempotency_key: str) -> bytes:
    return json.dumps(
        [target_id, observation_hash, selector, idempotency_key],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()


def seal_browser_input(
    value: str,
    *,
    token: str,
    target_id: str,
    lease_id: str,
    fencing: int,
    observation_hash: str,
    selector: str,
    idempotency_key: str,
) -> str:
    if len(value) > 2_000:
        raise ValueError("browser input is too long")
    key = _key(token, target_id, lease_id, fencing)
    associated = _aad(
        target_id=target_id,
        observation_hash=observation_hash,
        selector=selector,
        idempotency_key=idempotency_key,
    )
    cache = _RETRY_SEALS.get()
    fingerprint: bytes | None = None
    if cache is not None:
        fingerprint = hmac.digest(key, associated + b"\0" + value.encode(), "sha256")
        if idempotency_key in cache:
            saved_fingerprint, saved_envelope = cache[idempotency_key]
            if not hmac.compare_digest(saved_fingerprint, fingerprint):
                raise ValueError("approved local input binding changed")
            return saved_envelope
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(
        nonce,
        value.encode(),
        associated,
    )
    envelope = _PREFIX + base64.urlsafe_b64encode(nonce + ciphertext).decode()
    if cache is not None:
        assert fingerprint is not None
        cache[idempotency_key] = (fingerprint, envelope)
    return envelope


def open_browser_input(
    value_sealed: str,
    *,
    token: str,
    target_id: str,
    lease_id: str,
    fencing: int,
    observation_hash: str,
    selector: str,
    idempotency_key: str,
) -> str:
    if not valid_sealed_browser_input(value_sealed):
        raise ValueError("browser input envelope is invalid")
    encoded = value_sealed.removeprefix(_PREFIX)
    raw = base64.b64decode(encoded, altchars=b"-_", validate=True)
    plaintext = AESGCM(_key(token, target_id, lease_id, fencing)).decrypt(
        raw[:12],
        raw[12:],
        _aad(
            target_id=target_id,
            observation_hash=observation_hash,
            selector=selector,
            idempotency_key=idempotency_key,
        ),
    )
    result = plaintext.decode()
    if len(result) > 2_000:
        raise ValueError("browser input is too long")
    return result


def valid_sealed_browser_input(value: Any) -> bool:
    if not isinstance(value, str) or not value.startswith(_PREFIX) or len(value) > 4_096:
        return False
    try:
        raw = base64.b64decode(value.removeprefix(_PREFIX), altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):
        return False
    return 29 <= len(raw) <= 3_000
