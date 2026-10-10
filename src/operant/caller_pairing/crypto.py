"""P-256 authenticated transport for the additive caller-pairing.v1 domain.

The byte format is intentionally small enough to reproduce with WebCrypto.
All JSON below is UTF-8, sorted by key, with `,`/`:` separators and no NaN.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import secrets
from collections.abc import Mapping
from typing import Any

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from operant.contracts.caller_pairing import CALLER_PAIRING_PROTOCOL

PAIR_PATH = "/v1/local-callers/pair"
COMMAND_PATH = "/v1/local-callers/commands"
READBACK_PATH = "/v1/local-callers/requests/readback"
_DOMAIN = b"caller-pairing.v1\x00"


class CallerCryptoError(ValueError):
    """Fixed, non-sensitive authentication failure."""


def canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def unb64(value: str, *, length: int | None = None) -> bytes:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, binascii.Error) as exc:
        raise CallerCryptoError("invalid encoding") from exc
    if b64(raw) != value or (length is not None and len(raw) != length):
        raise CallerCryptoError("invalid encoding")
    return raw


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(16)}"


def public_key_text(key: ec.EllipticCurvePublicKey) -> str:
    return b64(key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint))


def load_public_key(value: str) -> ec.EllipticCurvePublicKey:
    raw = unb64(value, length=65)
    try:
        return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
    except ValueError as exc:
        raise CallerCryptoError("invalid public key") from exc


def device_id(signing_key: str) -> str:
    return "caller_" + hashlib.sha256(unb64(signing_key, length=65)).hexdigest()[:32]


def request_hash(envelope: Mapping[str, Any], *, path: str) -> str:
    unsigned = {key: value for key, value in envelope.items() if key != "signature"}
    return hashlib.sha256(
        _DOMAIN + b"POST\x00" + path.encode("ascii") + b"\x00" + canonical_bytes(unsigned)
    ).hexdigest()


def _signed_bytes(envelope: Mapping[str, Any], *, path: str) -> bytes:
    unsigned = {key: value for key, value in envelope.items() if key != "signature"}
    return (
        _DOMAIN
        + b"signature\x00POST\x00"
        + path.encode("ascii")
        + b"\x00"
        + canonical_bytes(unsigned)
    )


def sign(envelope: Mapping[str, Any], private_key: ec.EllipticCurvePrivateKey, *, path: str) -> str:
    der = private_key.sign(_signed_bytes(envelope, path=path), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    return b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


def verify(envelope: Mapping[str, Any], signing_key: str, *, path: str) -> None:
    raw = unb64(str(envelope.get("signature", "")), length=64)
    public = load_public_key(signing_key)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    try:
        public.verify(der, _signed_bytes(envelope, path=path), ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as exc:
        raise CallerCryptoError("invalid signature") from exc


def derive_key(
    private_key: ec.EllipticCurvePrivateKey,
    peer_public: str,
    *,
    epoch_id: str,
    device_id_value: str,
    purpose: str,
) -> bytes:
    if purpose not in {"pair-request", "pair-reply", "command-request", "command-reply"}:
        raise ValueError("invalid key purpose")
    shared = private_key.exchange(ec.ECDH(), load_public_key(peer_public))
    salt = hashlib.sha256(
        _DOMAIN + epoch_id.encode("ascii") + b"\x00" + device_id_value.encode("ascii")
    ).digest()
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=salt, info=_DOMAIN + purpose.encode("ascii")
    ).derive(shared)


def aad(envelope: Mapping[str, Any], *, path: str) -> bytes:
    # Only metadata precedes encryption; ciphertext and signature are excluded.
    metadata = {
        key: value for key, value in envelope.items() if key not in {"ciphertext", "signature"}
    }
    return _DOMAIN + b"aad\x00POST\x00" + path.encode("ascii") + b"\x00" + canonical_bytes(metadata)


def encrypt(
    plaintext: Mapping[str, Any], envelope: Mapping[str, Any], key: bytes, *, path: str
) -> str:
    if envelope.get("protocol_version") != CALLER_PAIRING_PROTOCOL:
        raise CallerCryptoError("invalid protocol")
    nonce = unb64(str(envelope["nonce"]), length=12)
    return b64(AESGCM(key).encrypt(nonce, canonical_bytes(plaintext), aad(envelope, path=path)))


def decrypt(envelope: Mapping[str, Any], key: bytes, *, path: str) -> dict[str, Any]:
    if envelope.get("protocol_version") != CALLER_PAIRING_PROTOCOL:
        raise CallerCryptoError("invalid protocol")
    try:
        raw = AESGCM(key).decrypt(
            unb64(str(envelope["nonce"]), length=12),
            unb64(str(envelope["ciphertext"])),
            aad(envelope, path=path),
        )
        result = json.loads(raw, parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
    except (ValueError, KeyError, TypeError, InvalidTag) as exc:
        raise CallerCryptoError("invalid encrypted request") from exc
    if not isinstance(result, dict):
        raise CallerCryptoError("invalid encrypted request")
    return result


class CoreKeys:
    def __init__(self) -> None:
        self.epoch_id = new_id("core")
        self.exchange = ec.generate_private_key(ec.SECP256R1())
        self.signing = ec.generate_private_key(ec.SECP256R1())

    @property
    def exchange_public(self) -> str:
        return public_key_text(self.exchange.public_key())

    @property
    def signing_public(self) -> str:
        return public_key_text(self.signing.public_key())
