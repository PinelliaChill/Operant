from __future__ import annotations

import base64
import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from operant.domain.remote_control import EncryptedRemoteCommand


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


class RemoteKeyStore:
    """Small local secret store. Only opaque references are persisted in SQLite."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.is_absolute():
            raise ValueError("remote key store path must be absolute")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with self._locked(exclusive=True):
            if not self.path.exists():
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump({"version": 1, "keys": {}}, stream)
            os.chmod(self.path, 0o600)

    def put(self, reference: str, material: bytes) -> None:
        if not reference or len(reference) > 300:
            raise ValueError("invalid key reference")
        with self._locked(exclusive=True):
            payload = self._read()
            keys = payload["keys"]
            assert isinstance(keys, dict)
            encoded = _b64(material)
            existing = keys.get(reference)
            if existing is not None and existing != encoded:
                raise ValueError("key reference is already bound")
            keys[reference] = encoded
            self._write(payload)

    def get(self, reference: str) -> bytes:
        with self._locked(exclusive=False):
            keys = self._read()["keys"]
            assert isinstance(keys, dict)
            try:
                value = keys[reference]
            except KeyError as exc:
                raise KeyError("key reference is unavailable") from exc
            if not isinstance(value, str):
                raise ValueError("invalid key material")
            return _unb64(value)

    def delete(self, reference: str) -> None:
        with self._locked(exclusive=True):
            payload = self._read()
            keys = payload["keys"]
            assert isinstance(keys, dict)
            if keys.pop(reference, None) is not None:
                self._write(payload)

    @contextmanager
    def _locked(self, *, exclusive: bool) -> Iterator[None]:
        descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _read(self) -> dict[str, Any]:
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if payload.get("version") != 1 or not isinstance(payload.get("keys"), dict):
            raise ValueError("invalid remote key store")
        return cast(dict[str, Any], payload)

    def _write(self, payload: dict[str, Any]) -> None:
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)
        os.chmod(self.path, 0o600)


class RemoteCrypto:
    @staticmethod
    def create_signing_keypair() -> tuple[str, bytes]:
        private = Ed25519PrivateKey.generate()
        return (
            _b64(
                private.public_key().public_bytes(
                    serialization.Encoding.Raw, serialization.PublicFormat.Raw
                )
            ),
            private.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            ),
        )

    @staticmethod
    def create_exchange_keypair() -> tuple[str, bytes]:
        private = X25519PrivateKey.generate()
        return (
            _b64(
                private.public_key().public_bytes(
                    serialization.Encoding.Raw, serialization.PublicFormat.Raw
                )
            ),
            private.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            ),
        )

    @staticmethod
    def derive_session_key(
        private_key: bytes, peer_public_key: str, *, session_id: str, host_id: str, device_id: str
    ) -> bytes:
        shared = X25519PrivateKey.from_private_bytes(private_key).exchange(
            X25519PublicKey.from_public_bytes(_unb64(peer_public_key))
        )
        return HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=session_id.encode("utf-8"),
            info=f"operant.remote-control.v1\0{host_id}\0{device_id}".encode(),
        ).derive(shared)

    @staticmethod
    def command_signing_bytes(command: EncryptedRemoteCommand) -> bytes:
        return _canonical(command.model_dump(mode="json", exclude={"signature"}))

    @classmethod
    def verify_command(cls, command: EncryptedRemoteCommand, signing_public_key: str) -> None:
        Ed25519PublicKey.from_public_bytes(_unb64(signing_public_key)).verify(
            _unb64(command.signature), cls.command_signing_bytes(command)
        )

    @staticmethod
    def decrypt_command(command: EncryptedRemoteCommand, session_key: bytes) -> dict[str, Any]:
        plaintext = ChaCha20Poly1305(session_key).decrypt(
            _unb64(command.nonce),
            _unb64(command.ciphertext),
            RemoteCrypto._aad(command),
        )
        payload = json.loads(plaintext)
        if not isinstance(payload, dict):
            raise ValueError("remote command payload must be an object")
        return payload

    @staticmethod
    def encrypt_relay_payload(
        payload: bytes, session_key: bytes, *, route_ref: str
    ) -> tuple[str, str]:
        nonce = os.urandom(12)
        ciphertext = ChaCha20Poly1305(session_key).encrypt(
            nonce, payload, route_ref.encode("utf-8")
        )
        return _b64(ciphertext), _b64(nonce)

    @staticmethod
    def decrypt_relay_payload(
        ciphertext: str, nonce: str, session_key: bytes, *, route_ref: str
    ) -> bytes:
        return ChaCha20Poly1305(session_key).decrypt(
            _unb64(nonce), _unb64(ciphertext), route_ref.encode("utf-8")
        )

    @staticmethod
    def encrypt_command_payload(
        command: EncryptedRemoteCommand,
        payload: dict[str, Any],
        session_key: bytes,
        signing_private_key: bytes,
    ) -> EncryptedRemoteCommand:
        nonce = os.urandom(12)
        unsigned = command.model_copy(
            update={
                "nonce": _b64(nonce),
                "ciphertext": _b64(
                    ChaCha20Poly1305(session_key).encrypt(
                        nonce, _canonical(payload), RemoteCrypto._aad(command)
                    )
                ),
                "signature": "0" * 32,
            }
        )
        signature = Ed25519PrivateKey.from_private_bytes(signing_private_key).sign(
            RemoteCrypto.command_signing_bytes(unsigned)
        )
        return unsigned.model_copy(update={"signature": _b64(signature)})

    @staticmethod
    def _aad(command: EncryptedRemoteCommand) -> bytes:
        return _canonical(
            {
                "command_id": command.command_id,
                "idempotency_key": command.idempotency_key,
                "host_id": command.host_id,
                "device_id": command.device_id,
                "remote_session_id": command.remote_session_id,
                "protocol_version": command.protocol_version,
                "issued_at": command.issued_at.isoformat(),
                "expires_at": command.expires_at.isoformat(),
            }
        )
