"""Seal browser text so durable Core Jobs contain ciphertext, never input text."""

from __future__ import annotations

import base64
import binascii
import json
import os
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_PREFIX = "operant-browser-input.v1:"


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
    nonce = os.urandom(12)
    ciphertext = AESGCM(_key(token, target_id, lease_id, fencing)).encrypt(
        nonce,
        value.encode(),
        _aad(
            target_id=target_id,
            observation_hash=observation_hash,
            selector=selector,
            idempotency_key=idempotency_key,
        ),
    )
    return _PREFIX + base64.urlsafe_b64encode(nonce + ciphertext).decode()


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
