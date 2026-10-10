"""One-request, loopback-only proof for a self-managed native Core caller.

The parent process supplies a fresh 32-byte secret over a private anonymous pipe.
This module neither creates nor loads a secret. Native signs the exact ASCII bytes
below with HMAC-SHA256 and sends lowercase hexadecimal in the signature header.
The proof is these seven fields joined by one LF byte, with no trailing LF::

    local-caller.v1
    METHOD
    RAW_PATH[?RAW_QUERY]
    TIMESTAMP
    NONCE
    IDEMPOTENCY_KEY_OR_EMPTY
    SHA256_BODY_LOWERCASE_HEX

The request target is the ASGI raw path and raw query, never a normalized URL. All
signed fields are bounded and reject LF, making the proof unambiguous.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import math
import re
import threading
import time
from collections.abc import Callable, Iterable
from typing import cast
from urllib.parse import urlsplit

from fastapi import Request

PROTOCOL = "local-caller.v1"
PROTOCOL_HEADER = "x-operant-caller-protocol"
TIMESTAMP_HEADER = "x-operant-caller-timestamp"
NONCE_HEADER = "x-operant-caller-nonce"
SIGNATURE_HEADER = "x-operant-caller-signature"
IDEMPOTENCY_HEADER = "idempotency-key"
MAX_BODY_BYTES = 1024 * 1024
MAX_CLOCK_SKEW_SECONDS = 30
MAX_NONCES = 4096

_METHOD = re.compile(rb"(?:GET|POST|PUT|PATCH|DELETE)")
_PATH = re.compile(rb"/[A-Za-z0-9._~/-]*")
_QUERY = re.compile(rb"[\x21-\x7e]*")
_NONCE = re.compile(rb"[0-9a-f]{32}")
_SIGNATURE = re.compile(rb"[0-9a-f]{64}")
_IDEMPOTENCY = re.compile(rb"[\x21-\x7e]{1,300}")
_TIMESTAMP = re.compile(rb"[1-9][0-9]{0,12}")
_SIGNED_HEADERS = (
    PROTOCOL_HEADER,
    TIMESTAMP_HEADER,
    NONCE_HEADER,
    SIGNATURE_HEADER,
    IDEMPOTENCY_HEADER,
    "host",
)


def canonical_proof(
    *,
    method: bytes,
    target: bytes,
    timestamp: bytes,
    nonce: bytes,
    idempotency_key: bytes,
    body_digest: bytes,
) -> bytes:
    """Return the versioned proof bytes shared with the native signer."""
    return b"\n".join(
        (b"local-caller.v1", method, target, timestamp, nonce, idempotency_key, body_digest)
    )


def _single_header(request: Request, name: str, *, required: bool = True) -> bytes | None:
    raw_headers = cast(Iterable[tuple[bytes, bytes]], request.scope.get("headers", ()))
    matches = [value for key, value in raw_headers if key.lower() == name.encode()]
    if len(matches) == 1:
        return matches[0]
    if not matches and not required:
        return b""
    return None


def _loopback_request(request: Request) -> bool:
    if request.scope.get("type") != "http" or request.client is None:
        return False
    server = request.scope.get("server")
    if not isinstance(server, (tuple, list)) or len(server) != 2:
        return False
    server_host, server_port = server
    if not isinstance(server_host, str) or not isinstance(server_port, int):
        return False
    if not 1 <= server_port <= 65535:
        return False
    try:
        client_ip = ipaddress.ip_address(request.client.host)
        server_ip = ipaddress.ip_address(server_host)
        if not client_ip.is_loopback or not server_ip.is_loopback:
            return False
        host_header = _single_header(request, "host")
        if host_header is None or len(host_header) > 64:
            return False
        host = host_header.decode("ascii")
        parsed = urlsplit(f"http://{host}")
        return (
            parsed.hostname == server_ip.compressed
            and parsed.port == server_port
            and not parsed.username
            and not parsed.password
            and not parsed.path
            and not parsed.query
            and not parsed.fragment
            and host
            == (
                f"[{server_ip.compressed}]:{server_port}"
                if server_ip.version == 6
                else f"{server_ip.compressed}:{server_port}"
            )
        )
    except (AttributeError, TypeError, UnicodeError, ValueError):
        return False


def _canonical_request(request: Request) -> tuple[bytes, bytes] | None:
    method = request.scope.get("method")
    raw_path = request.scope.get("raw_path")
    query = request.scope.get("query_string", b"")
    if not isinstance(method, str) or not isinstance(raw_path, bytes):
        return None
    if not isinstance(query, bytes) or len(raw_path) > 2048 or len(query) > 4096:
        return None
    try:
        method_bytes = method.encode("ascii")
    except UnicodeError:
        return None
    if not _METHOD.fullmatch(method_bytes) or not _PATH.fullmatch(raw_path):
        return None
    if b"//" in raw_path or any(segment in (b".", b"..") for segment in raw_path.split(b"/")):
        return None
    if not _QUERY.fullmatch(query) or b"#" in query:
        return None
    # Refuse a discrepancy between the raw target being signed and the path routed.
    if request.scope.get("path") != raw_path.decode("ascii"):
        return None
    return method_bytes, raw_path + (b"?" + query if query else b"")


class LocalCallerAuthority:
    """Verify one native caller proof, consuming a bounded nonce only on success."""

    def __init__(
        self,
        secret: bytes,
        *,
        clock: Callable[[], float] = time.time,
        max_nonces: int = MAX_NONCES,
    ) -> None:
        if not isinstance(secret, bytes) or len(secret) != 32:
            raise ValueError("local caller secret must be 32 bytes")
        if max_nonces < 1:
            raise ValueError("max_nonces must be positive")
        self._secret = secret
        self._clock = clock
        self._max_nonces = max_nonces
        self._nonces: dict[bytes, float] = {}
        self._lock = threading.Lock()
        self._marker = object()

    async def authenticate(self, request: Request) -> bool:
        """Return only a boolean; never disclose proof material or diagnostics."""
        if not _loopback_request(request):
            request.state.local_caller_trusted = False
            return False
        if getattr(request.state, "_operant_local_caller_marker", None) is self._marker:
            return True
        request.state.local_caller_trusted = False
        try:
            headers = {
                name: _single_header(request, name, required=name != IDEMPOTENCY_HEADER)
                for name in _SIGNED_HEADERS
            }
            if any(value is None for value in headers.values()):
                return False
            protocol = headers[PROTOCOL_HEADER]
            timestamp = headers[TIMESTAMP_HEADER]
            nonce = headers[NONCE_HEADER]
            signature = headers[SIGNATURE_HEADER]
            idempotency_key = headers[IDEMPOTENCY_HEADER]
            if (
                protocol != PROTOCOL.encode()
                or timestamp is None
                or not _TIMESTAMP.fullmatch(timestamp)
                or nonce is None
                or not _NONCE.fullmatch(nonce)
                or signature is None
                or not _SIGNATURE.fullmatch(signature)
                or idempotency_key is None
            ):
                return False
            target = _canonical_request(request)
            if target is None:
                return False
            method, raw_target = target
            if method != b"GET" and not _IDEMPOTENCY.fullmatch(idempotency_key):
                return False
            if method == b"GET" and idempotency_key and not _IDEMPOTENCY.fullmatch(idempotency_key):
                return False
            now = self._clock()
            if not math.isfinite(now) or abs(now - int(timestamp)) > MAX_CLOCK_SKEW_SECONDS:
                return False
            chunks: list[bytes] = []
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_BODY_BYTES:
                    return False
                chunks.append(chunk)
            body = b"".join(chunks)
            request._body = body  # Starlette's body cache preserves it for the endpoint.
            proof = canonical_proof(
                method=method,
                target=raw_target,
                timestamp=timestamp,
                nonce=nonce,
                idempotency_key=idempotency_key,
                body_digest=hashlib.sha256(body).hexdigest().encode("ascii"),
            )
            expected = hmac.new(self._secret, proof, hashlib.sha256).hexdigest().encode("ascii")
            if not hmac.compare_digest(expected, signature):
                return False
            with self._lock:
                self._nonces = {key: until for key, until in self._nonces.items() if until >= now}
                if nonce in self._nonces or len(self._nonces) >= self._max_nonces:
                    return False
                self._nonces[nonce] = int(timestamp) + MAX_CLOCK_SKEW_SECONDS
            request.state._operant_local_caller_marker = self._marker
            request.state.local_caller_trusted = True
            return True
        except Exception:
            return False
