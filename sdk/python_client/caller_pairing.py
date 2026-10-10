"""Independent, scope-limited local caller for skill source management.

Only an explicitly pasted native ticket can pin a Core. The generated client
owns the public protocol; this adapter owns private keys and reply validation.
"""

from __future__ import annotations

import fcntl
import http.client
import ipaddress
import json
import os
import re
import secrets
import stat
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    load_der_private_key,
)
from pydantic import ValidationError

from operant.caller_pairing.crypto import (
    COMMAND_PATH,
    PAIR_PATH,
    READBACK_PATH,
    CallerCryptoError,
    b64,
    canonical_bytes,
    decrypt,
    derive_key,
    device_id,
    encrypt,
    load_public_key,
    new_id,
    public_key_text,
    request_hash,
    sign,
    unb64,
    verify,
)
from operant.contracts.caller_pairing import (
    CALLER_PAIRING_PROTOCOL,
    SKILL_SOURCE_SCOPE,
    CallerEncryptedReply,
    CallerPairingTicket,
    CallerReceiptView,
)

from .caller_pairing_generated import CallerCommand as CallerCommandPayload
from .caller_pairing_generated import CallerPairingClient as GeneratedCallerPairingClient
from .caller_pairing_generated import CallerPairRequest as CallerPairRequestPayload
from .transport import Transport, TransportRequest, TransportResponse

TICKET_PREFIX = "operant-caller-pairing-v1:"
_SIGNING_REF = "OPERANT_CALLER_SIGNING_P256_PKCS8_B64"
_EXCHANGE_REF = "OPERANT_CALLER_EXCHANGE_P256_PKCS8_B64"
_MAX_HTTP_BODY = 1024 * 1024
_ALLOWED_HTTP_PATHS = {
    "/v1/protocol/caller-pairing": "GET",
    PAIR_PATH: "POST",
    COMMAND_PATH: "POST",
    READBACK_PATH: "POST",
}


class PairingClientError(ValueError):
    """Fixed public category; underlying network and crypto text stays private."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _base_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 100 or any(ord(ch) < 33 for ch in value):
        raise PairingClientError("invalid_ticket", "配对码中的 Core 地址无效。")
    parsed = urlsplit(value)
    try:
        address = ipaddress.ip_address(parsed.hostname or "")
        port = parsed.port
    except ValueError as exc:
        raise PairingClientError("invalid_ticket", "配对码中的 Core 地址无效。") from exc
    if (
        parsed.scheme != "http"
        or address not in {ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("::1")}
        or port is None
        or not 1 <= port <= 65535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise PairingClientError("invalid_ticket", "配对码中的 Core 地址无效。")
    host = f"[{address}]" if address.version == 6 else str(address)
    return f"http://{host}:{port}"


def decode_ticket(pasted: str) -> CallerPairingTicket:
    if not isinstance(pasted, str) or not pasted.startswith(TICKET_PREFIX) or len(pasted) > 8192:
        raise PairingClientError("invalid_ticket", "请粘贴完整的本机配对码。")
    try:
        raw = unb64(pasted[len(TICKET_PREFIX) :])
        value = json.loads(raw)
        if not isinstance(value, dict) or canonical_bytes(value) != raw:
            raise ValueError("noncanonical ticket")
        ticket = CallerPairingTicket.model_validate(value)
        _base_url(ticket.base_url)
        load_public_key(ticket.core_exchange_public_key)
        load_public_key(ticket.core_signing_public_key)
        if ticket.scope != SKILL_SOURCE_SCOPE or ticket.expires_at <= int(time.time()):
            raise ValueError("scope or expiry")
        return ticket
    except (CallerCryptoError, UnicodeError, ValueError, ValidationError) as exc:
        raise PairingClientError("invalid_ticket", "配对码无效或已过期，请重新生成。") from exc


def strict_loopback_transport(request: TransportRequest) -> TransportResponse:
    """One bounded direct HTTP request; never use proxy, redirects or fallback."""
    parsed = urlsplit(request.url)
    _base_url(f"{parsed.scheme}://{parsed.netloc}")
    if _ALLOWED_HTTP_PATHS.get(parsed.path) != request.method or parsed.query or parsed.fragment:
        raise PairingClientError("invalid_request", "配对请求格式无效。")
    body = None if request.body is None else request.body.encode("utf-8")
    if body is not None and len(body) > _MAX_HTTP_BODY:
        raise PairingClientError("invalid_request", "配对请求过大。")
    connection = http.client.HTTPConnection(
        parsed.hostname or "", parsed.port, timeout=300 if request.method == "POST" else 8
    )
    try:
        connection.request(
            request.method,
            parsed.path,
            body=body,
            headers=dict(request.headers),
        )
        response = connection.getresponse()
        payload = response.read(_MAX_HTTP_BODY + 1)
        if len(payload) > _MAX_HTTP_BODY:
            raise PairingClientError("invalid_response", "Core 响应过大。")
        if 300 <= response.status < 400:
            raise PairingClientError("redirect_rejected", "Core 返回了不允许的跳转。")
        return TransportResponse(
            status=response.status,
            headers={key.lower(): value for key, value in response.getheaders()},
            body=payload,
        )
    except PairingClientError:
        raise
    except (OSError, TimeoutError, http.client.HTTPException) as exc:
        raise PairingClientError("transport_unavailable", "本机 Core 暂时不可用。") from exc
    finally:
        connection.close()


def _file_is_private(path: Path) -> bool:
    item = path.lstat()
    return stat.S_ISREG(item.st_mode) and item.st_uid == os.getuid() and item.st_mode & 0o077 == 0


def _safe_state_directory(path: Path) -> None:
    if not path.is_absolute():
        raise PairingClientError("unsafe_storage", "配对状态目录必须是绝对路径。")
    for ancestor in (*reversed(path.parents), path):
        if not ancestor.exists() and not ancestor.is_symlink():
            continue
        info = ancestor.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise PairingClientError("unsafe_storage", "配对状态目录包含不安全路径。")
        if ancestor == path:
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise PairingClientError("unsafe_storage", "配对目录权限不安全。")
        elif info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
            raise PairingClientError("unsafe_storage", "配对状态目录的上级路径不安全。")


def _private_read(path: Path) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(fd)
        if not (
            stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_mode & 0o077 == 0
        ):
            os.close(fd)
            raise PairingClientError("unsafe_storage", "配对密钥文件权限不安全。")
        with os.fdopen(fd, "rb") as file:
            raw = file.read(8193)
            if len(raw) > 8192:
                raise PairingClientError("unsafe_storage", "配对状态文件过大。")
            return raw
    except (OSError, ValueError) as exc:
        raise PairingClientError("unsafe_storage", "无法安全读取配对状态。") from exc


def _atomic_private_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".caller-", delete=False) as file:
        temp = Path(file.name)
        try:
            os.fchmod(file.fileno(), 0o600)
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        except Exception:
            temp.unlink(missing_ok=True)
            raise
    try:
        if (path.exists() or path.is_symlink()) and not _file_is_private(path):
            raise PairingClientError("unsafe_storage", "已有配对文件权限不安全。")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


class PairedSkillSourceClient:
    """One-device client with explicit pairing, readback and approval continuation."""

    def __init__(self, state_dir: Path | None = None, transport: Transport | None = None) -> None:
        self.state_dir = state_dir or Path.home() / ".config" / "operant" / "caller-pairing"
        self.transport = transport or strict_loopback_transport
        self._pair_pending: tuple[CallerPairingTicket, dict[str, Any], str] | None = None

    @contextmanager
    def _locked(self) -> Iterator[None]:
        _safe_state_directory(self.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        _safe_state_directory(self.state_dir)
        lock_path = self.state_dir / ".lock"
        if (lock_path.exists() or lock_path.is_symlink()) and not _file_is_private(lock_path):
            raise PairingClientError("unsafe_storage", "配对锁文件权限不安全。")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            info = os.fstat(fd)
            if not (
                stat.S_ISREG(info.st_mode)
                and info.st_uid == os.getuid()
                and info.st_mode & 0o077 == 0
            ):
                raise PairingClientError("unsafe_storage", "配对锁文件权限不安全。")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _keys(self) -> tuple[ec.EllipticCurvePrivateKey, ec.EllipticCurvePrivateKey]:
        path = self.state_dir / ".env"
        with self._locked():
            if not path.exists() and not path.is_symlink():
                signing = ec.generate_private_key(ec.SECP256R1())
                exchange = ec.generate_private_key(ec.SECP256R1())
                lines = []
                for name, key in ((_SIGNING_REF, signing), (_EXCHANGE_REF, exchange)):
                    encoded = b64(
                        key.private_bytes(Encoding.DER, PrivateFormat.PKCS8, NoEncryption())
                    )
                    lines.append(f"{name}={encoded}")
                _atomic_private_write(path, ("\n".join(lines) + "\n").encode("ascii"))
                return signing, exchange
            raw = _private_read(path)
        try:
            lines = raw.decode("ascii").splitlines()
            entries = dict(line.split("=", 1) for line in lines)
            if len(lines) != 2 or set(entries) != {_SIGNING_REF, _EXCHANGE_REF}:
                raise ValueError("key entries")
            loaded_signing = load_der_private_key(unb64(entries[_SIGNING_REF]), password=None)
            loaded_exchange = load_der_private_key(unb64(entries[_EXCHANGE_REF]), password=None)
            if not all(
                isinstance(key, ec.EllipticCurvePrivateKey) and isinstance(key.curve, ec.SECP256R1)
                for key in (loaded_signing, loaded_exchange)
            ):
                raise ValueError("key curves")
            return (
                cast(ec.EllipticCurvePrivateKey, loaded_signing),
                cast(ec.EllipticCurvePrivateKey, loaded_exchange),
            )
        except (TypeError, ValueError, CallerCryptoError, UnicodeError) as exc:
            raise PairingClientError("unsafe_storage", "配对密钥文件无效。") from exc

    def _state(self) -> dict[str, Any]:
        path = self.state_dir / "state.json"
        with self._locked():
            if not path.exists() and not path.is_symlink():
                raise PairingClientError("pairing_required", "请先粘贴本机配对码。")
            try:
                value = json.loads(_private_read(path))
            except (ValueError, UnicodeError) as exc:
                raise PairingClientError("unsafe_storage", "配对状态无效。") from exc
        if not isinstance(value, dict) or set(value) != {
            "base_url",
            "core_epoch_id",
            "core_exchange_public_key",
            "core_signing_public_key",
            "device_id",
            "scope",
            "signing_key_ref",
            "exchange_key_ref",
            "expires_at",
        }:
            raise PairingClientError("unsafe_storage", "配对状态无效。")
        if (
            value["scope"] != SKILL_SOURCE_SCOPE
            or not isinstance(value["expires_at"], int)
            or isinstance(value["expires_at"], bool)
            or value["expires_at"] <= int(time.time())
        ):
            raise PairingClientError("pairing_required", "配对已失效，请重新配对。")
        if (
            value["signing_key_ref"] != _SIGNING_REF
            or value["exchange_key_ref"] != _EXCHANGE_REF
            or not isinstance(value["device_id"], str)
            or re.fullmatch(r"caller_[0-9a-f]{32}", value["device_id"]) is None
            or not isinstance(value["core_epoch_id"], str)
            or re.fullmatch(r"core_[0-9a-f]{32}", value["core_epoch_id"]) is None
        ):
            raise PairingClientError("unsafe_storage", "配对状态无效。")
        try:
            load_public_key(value["core_exchange_public_key"])
            load_public_key(value["core_signing_public_key"])
        except (CallerCryptoError, TypeError) as exc:
            raise PairingClientError("unsafe_storage", "配对公钥状态无效。") from exc
        _base_url(value["base_url"])
        return value

    def _api(self, base_url: str) -> GeneratedCallerPairingClient:
        return GeneratedCallerPairingClient(base_url, transport=self.transport)

    def _reply(
        self,
        response: Mapping[str, Any],
        envelope: Mapping[str, Any],
        *,
        signing_key: str,
        exchange_key: str,
        exchange_private: ec.EllipticCurvePrivateKey,
        path: str,
        purpose: str,
        expected_operation: str,
        expected_request_id: str,
    ) -> dict[str, Any]:
        try:
            reply = CallerEncryptedReply.model_validate(response).model_dump()
            if (
                reply["protocol_version"] != CALLER_PAIRING_PROTOCOL
                or reply["core_epoch_id"] != envelope["core_epoch_id"]
                or reply["device_id"] != envelope["device_id"]
                or reply["request_id"] != expected_request_id
                or reply["operation"] != expected_operation
                or reply["request_hash"] != request_hash(envelope, path=path)
                or abs(reply["issued_at"] - int(time.time())) > 30
                or reply["expires_at"] <= int(time.time())
                or not 0 < reply["expires_at"] - reply["issued_at"] <= 60
            ):
                raise CallerCryptoError("reply binding")
            verify(reply, signing_key, path=path)
            key = derive_key(
                exchange_private,
                exchange_key,
                epoch_id=reply["core_epoch_id"],
                device_id_value=reply["device_id"],
                purpose=purpose,
            )
            return decrypt(reply, key, path=path)
        except (CallerCryptoError, ValidationError, KeyError, TypeError, ValueError) as exc:
            raise PairingClientError("invalid_reply", "Core 配对回包验证失败。") from exc

    def pair_ticket(self, pasted_ticket: str, display_name: str) -> dict[str, Any]:
        ticket = decode_ticket(pasted_ticket)
        if not 1 <= len(display_name) <= 100 or any(ord(ch) < 32 for ch in display_name):
            raise PairingClientError("invalid_input", "请填写有效设备名称。")
        signing, exchange = self._keys()
        signing_public = public_key_text(signing.public_key())
        exchange_public = public_key_text(exchange.public_key())
        caller_id = device_id(signing_public)
        now = int(time.time())
        envelope: dict[str, Any] = {
            "protocol_version": CALLER_PAIRING_PROTOCOL,
            "pair_request_id": new_id("pair"),
            "ticket_id": ticket.ticket_id,
            "core_epoch_id": ticket.core_epoch_id,
            "device_id": caller_id,
            "device_signing_public_key": signing_public,
            "device_exchange_public_key": exchange_public,
            "scope": SKILL_SOURCE_SCOPE,
            "issued_at": now,
            "expires_at": min(now + 60, ticket.expires_at),
            "nonce": b64(secrets.token_bytes(12)),
        }
        if envelope["expires_at"] <= now:
            raise PairingClientError("invalid_ticket", "配对码已过期。")
        key = derive_key(
            exchange,
            ticket.core_exchange_public_key,
            epoch_id=ticket.core_epoch_id,
            device_id_value=caller_id,
            purpose="pair-request",
        )
        envelope["ciphertext"] = encrypt(
            {"one_time_code": ticket.one_time_code, "display_name": display_name},
            envelope,
            key,
            path=PAIR_PATH,
        )
        envelope["signature"] = sign(envelope, signing, path=PAIR_PATH)
        self._pair_pending = (ticket, envelope, envelope["pair_request_id"])
        return self.retry_pair_same_envelope()

    def retry_pair_same_envelope(self) -> dict[str, Any]:
        pending = self._pair_pending
        if pending is None:
            raise PairingClientError("pairing_required", "当前没有可核对的配对请求。")
        ticket, envelope, idem = pending
        if ticket.expires_at <= int(time.time()):
            raise PairingClientError("invalid_ticket", "配对码已过期，请重新生成。")
        _, exchange = self._keys()
        try:
            response = self._api(_base_url(ticket.base_url)).pair_local_caller(
                cast(CallerPairRequestPayload, envelope), idempotency_key=idem
            )
        except Exception as exc:
            raise PairingClientError(
                "outcome_unknown", "配对结果未知；可用同一请求显式核对。"
            ) from exc
        clear = self._reply(
            response,
            envelope,
            signing_key=ticket.core_signing_public_key,
            exchange_key=ticket.core_exchange_public_key,
            exchange_private=exchange,
            path=PAIR_PATH,
            purpose="pair-reply",
            expected_operation="pair",
            expected_request_id=envelope["pair_request_id"],
        )
        if (
            clear.get("device_id") != envelope["device_id"]
            or clear.get("core_epoch_id") != ticket.core_epoch_id
            or clear.get("scope") != SKILL_SOURCE_SCOPE
            or not isinstance(clear.get("expires_at"), int)
            or isinstance(clear.get("expires_at"), bool)
            or not int(time.time()) < clear["expires_at"] <= int(time.time()) + 8 * 3600
        ):
            raise PairingClientError("invalid_reply", "Core 配对回包验证失败。")
        state = {
            "base_url": _base_url(ticket.base_url),
            "core_epoch_id": ticket.core_epoch_id,
            "core_exchange_public_key": ticket.core_exchange_public_key,
            "core_signing_public_key": ticket.core_signing_public_key,
            "device_id": envelope["device_id"],
            "scope": SKILL_SOURCE_SCOPE,
            "expires_at": clear["expires_at"],
            "signing_key_ref": _SIGNING_REF,
            "exchange_key_ref": _EXCHANGE_REF,
        }
        with self._locked():
            _atomic_private_write(self.state_dir / "state.json", canonical_bytes(state))
        self._pair_pending = None
        return {
            "device_id": state["device_id"],
            "scope": SKILL_SOURCE_SCOPE,
            "expires_at": clear["expires_at"],
        }

    def _pending(self) -> dict[str, str] | None:
        path = self.state_dir / "pending.json"
        with self._locked():
            if not path.exists() and not path.is_symlink():
                return None
            value = json.loads(_private_read(path))
        if not isinstance(value, dict) or set(value) != {
            "device_id",
            "operation",
            "request_id",
            "payload_hash",
        }:
            raise PairingClientError("unsafe_storage", "待核对请求状态无效。")
        if (
            not isinstance(value["device_id"], str)
            or re.fullmatch(r"caller_[0-9a-f]{32}", value["device_id"]) is None
            or not isinstance(value["operation"], str)
            or value["operation"] not in {"add", "remove"}
            or not isinstance(value["request_id"], str)
            or re.fullmatch(r"[\x21-\x7e]{1,300}", value["request_id"]) is None
            or not isinstance(value["payload_hash"], str)
            or re.fullmatch(r"[0-9a-f]{64}", value["payload_hash"]) is None
        ):
            raise PairingClientError("unsafe_storage", "待核对请求状态无效。")
        return cast(dict[str, str], value)

    def _reserve_pending(self, value: dict[str, str]) -> None:
        path = self.state_dir / "pending.json"
        with self._locked():
            if path.exists() or path.is_symlink():
                raise PairingClientError("outcome_unknown", "上一笔写入仍待核对，请先回读。")
            _atomic_private_write(path, canonical_bytes(value))

    def _clear_pending(self, value: dict[str, str]) -> None:
        path = self.state_dir / "pending.json"
        with self._locked():
            if path.exists() and json.loads(_private_read(path)) == value:
                path.unlink()

    def _command(
        self, operation: str, payload: dict[str, Any], *, original: dict[str, str] | None = None
    ) -> dict[str, Any]:
        state = self._state()
        signing, exchange = self._keys()
        if device_id(public_key_text(signing.public_key())) != state["device_id"]:
            raise PairingClientError("unsafe_storage", "设备密钥与配对状态不匹配。")
        now = int(time.time())
        path = READBACK_PATH if operation == "readback" else COMMAND_PATH
        envelope: dict[str, Any] = {
            "protocol_version": CALLER_PAIRING_PROTOCOL,
            "core_epoch_id": state["core_epoch_id"],
            "device_id": state["device_id"],
            "request_id": new_id("request"),
            "operation": operation,
            "issued_at": now,
            "expires_at": now + 60,
            "nonce": b64(secrets.token_bytes(12)),
        }
        key = derive_key(
            exchange,
            state["core_exchange_public_key"],
            epoch_id=state["core_epoch_id"],
            device_id_value=state["device_id"],
            purpose="command-request",
        )
        envelope["ciphertext"] = encrypt(payload, envelope, key, path=path)
        envelope["signature"] = sign(envelope, signing, path=path)
        pending = None
        if operation in {"add", "remove"}:
            import hashlib

            pending = {
                "device_id": state["device_id"],
                "operation": operation,
                "request_id": envelope["request_id"],
                "payload_hash": hashlib.sha256(
                    canonical_bytes({"operation": operation, "args": payload})
                ).hexdigest(),
            }
            self._reserve_pending(pending)
        try:
            api = self._api(state["base_url"])
            if operation == "readback":
                response = api.read_caller_request(
                    cast(CallerCommandPayload, envelope), idempotency_key=envelope["request_id"]
                )
            else:
                response = api.execute_caller_command(
                    cast(CallerCommandPayload, envelope), idempotency_key=envelope["request_id"]
                )
            clear = self._reply(
                response,
                envelope,
                signing_key=state["core_signing_public_key"],
                exchange_key=state["core_exchange_public_key"],
                exchange_private=exchange,
                path=path,
                purpose="command-reply",
                expected_operation=operation,
                expected_request_id=envelope["request_id"],
            )
            receipt = CallerReceiptView.model_validate(clear["receipt"]).model_dump()
            expected = pending or original
            if expected and (
                receipt["device_id"] != expected["device_id"]
                or receipt["request_id"] != expected["request_id"]
                or receipt["operation"] != expected["operation"]
                or receipt["payload_hash"] != expected["payload_hash"]
            ):
                raise PairingClientError("invalid_reply", "Core 回执身份不匹配。")
            if pending and receipt["state"] in {"completed", "failed"}:
                self._clear_pending(pending)
            if original and receipt["state"] in {"completed", "failed"}:
                self._clear_pending(original)
            return receipt
        except PairingClientError:
            raise
        except Exception as exc:
            raise PairingClientError(
                "outcome_unknown", "请求结果未知，请使用原 Request ID 回读。"
            ) from exc

    def list_sources(self) -> dict[str, Any]:
        return self._command("list", {})

    def add_source(self, path: str) -> dict[str, Any]:
        if (
            not isinstance(path, str)
            or not path
            or len(path) > 4096
            or not Path(path).is_absolute()
            or "\x00" in path
        ):
            raise PairingClientError("invalid_input", "请选择有效技能目录。")
        return self._command("add", {"path": path})

    def remove_source(self, root_ref: str) -> dict[str, Any]:
        if not isinstance(root_ref, str) or not root_ref or len(root_ref) > 200:
            raise PairingClientError("invalid_input", "请输入有效的来源引用。")
        return self._command("remove", {"root_ref": root_ref})

    def readback(self, original_request_id: str | None = None) -> dict[str, Any]:
        pending = self._pending()
        if pending is None or (
            original_request_id and original_request_id != pending["request_id"]
        ):
            raise PairingClientError("outcome_unknown", "找不到该设备待核对的原请求。")
        return self._command(
            "readback", {"original_request_id": pending["request_id"]}, original=pending
        )

    def continue_approved(self, approval_id: str, action_hash: str) -> dict[str, Any]:
        pending = self._pending()
        if pending is None:
            raise PairingClientError("outcome_unknown", "找不到待继续的原请求。")
        current = self.readback(pending["request_id"])
        if (
            current["state"] != "awaiting_approval"
            or current.get("approval_id") != approval_id
            or current.get("action_hash") != action_hash
        ):
            raise PairingClientError("approval_mismatch", "审批信息与原请求不一致，请刷新回执。")
        return self._command(
            "continue",
            {
                "original_request_id": pending["request_id"],
                "payload_hash": pending["payload_hash"],
                "approval_id": approval_id,
                "action_hash": action_hash,
            },
            original=pending,
        )

    def pending_request(self) -> dict[str, str] | None:
        return self._pending()
