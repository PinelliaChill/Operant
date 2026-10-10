from __future__ import annotations

import hashlib
import hmac
from typing import Any

import pytest
from fastapi import Request

from operant.local_caller import (
    IDEMPOTENCY_HEADER,
    NONCE_HEADER,
    PROTOCOL,
    PROTOCOL_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    LocalCallerAuthority,
    canonical_proof,
)

SECRET = b"s" * 32
TIME = 1_800_000_000.0
NONCE = b"a" * 32


def _request(
    *,
    body: bytes = b"json body",
    signed_body: bytes | None = None,
    method: str = "POST",
    signed_method: bytes | None = None,
    path: bytes = b"/v1/setup/connections",
    signed_path: bytes | None = None,
    query: bytes = b"a=1&b=2",
    signed_query: bytes | None = None,
    timestamp: bytes = b"1800000000",
    nonce: bytes = NONCE,
    idempotency_key: bytes | None = b"request-1",
    client: str = "127.0.0.1",
    server: tuple[str, int] = ("127.0.0.1", 18778),
    host: bytes = b"127.0.0.1:18778",
    extra_headers: list[tuple[bytes, bytes]] | None = None,
    remove_header: bytes | None = None,
    signature: bytes | None = None,
    scope_path: str | None = None,
) -> Request:
    signing_query = query if signed_query is None else signed_query
    signing_target = (signed_path or path) + (b"?" + signing_query if signing_query else b"")
    proof = canonical_proof(
        method=signed_method or method.encode(),
        target=signing_target,
        timestamp=timestamp,
        nonce=nonce,
        idempotency_key=idempotency_key or b"",
        body_digest=hashlib.sha256(body if signed_body is None else signed_body)
        .hexdigest()
        .encode(),
    )
    mac = hmac.new(SECRET, proof, hashlib.sha256).hexdigest().encode()
    headers = [
        (b"host", host),
        (PROTOCOL_HEADER.encode(), PROTOCOL.encode()),
        (TIMESTAMP_HEADER.encode(), timestamp),
        (NONCE_HEADER.encode(), nonce),
        (SIGNATURE_HEADER.encode(), signature or mac),
    ]
    if idempotency_key is not None:
        headers.append((IDEMPOTENCY_HEADER.encode(), idempotency_key))
    headers.extend(extra_headers or [])
    if remove_header is not None:
        headers = [(key, value) for key, value in headers if key != remove_header]
    scope: dict[str, Any] = {
        "type": "http",
        "method": method,
        "path": path.decode() if scope_path is None else scope_path,
        "raw_path": path,
        "query_string": query,
        "headers": headers,
        "client": (client, 12345),
        "server": server,
        "scheme": "http",
    }
    emitted = False

    async def receive() -> dict[str, Any]:
        nonlocal emitted
        if emitted:
            return {"type": "http.disconnect"}
        emitted = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


@pytest.mark.asyncio
async def test_success_is_cached_only_for_same_request_and_preserves_body() -> None:
    authority = LocalCallerAuthority(SECRET, clock=lambda: TIME)
    request = _request()
    assert await authority.authenticate(request)
    assert request.state.local_caller_trusted is True
    assert await request.body() == b"json body"
    assert await authority.authenticate(request)
    replay = _request()
    replay.state.local_caller_trusted = True  # A plain boolean is not the proof marker.
    assert not await authority.authenticate(replay)
    assert replay.state.local_caller_trusted is False


@pytest.mark.asyncio
async def test_readonly_request_may_omit_idempotency_key_and_use_ipv6_loopback() -> None:
    authority = LocalCallerAuthority(SECRET, clock=lambda: TIME)
    request = _request(
        method="GET",
        idempotency_key=None,
        client="::1",
        server=("::1", 18778),
        host=b"[::1]:18778",
    )
    assert await authority.authenticate(request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [
        {"body": b"changed", "signed_body": b"json body"},
        {"method": "PUT", "signed_method": b"POST"},
        {"path": b"/v1/setup/other", "signed_path": b"/v1/setup/connections"},
        {"query": b"b=2&a=1", "signed_query": b"a=1&b=2"},
        {"idempotency_key": b"request-2", "signature": b"0" * 64},
        {"nonce": b"b" * 32, "signature": b"0" * 64},
        {"timestamp": b"1800000001", "signature": b"0" * 64},
    ],
)
async def test_changed_proof_fields_fail(changed: dict[str, Any]) -> None:
    authority = LocalCallerAuthority(SECRET, clock=lambda: TIME)
    assert not await authority.authenticate(_request(**changed))


@pytest.mark.asyncio
async def test_idempotency_key_is_signed_even_with_valid_other_fields() -> None:
    authority = LocalCallerAuthority(SECRET, clock=lambda: TIME)
    signed = _request()
    headers = signed.scope["headers"]
    signed.scope["headers"] = [
        (key, b"different" if key == IDEMPOTENCY_HEADER.encode() else value)
        for key, value in headers
    ]
    assert not await authority.authenticate(signed)


@pytest.mark.asyncio
@pytest.mark.parametrize("offset", [-31, 31])
async def test_expired_or_future_timestamp_fails(offset: int) -> None:
    authority = LocalCallerAuthority(SECRET, clock=lambda: TIME + offset)
    assert not await authority.authenticate(_request())


@pytest.mark.asyncio
async def test_timestamp_boundary_still_blocks_replay() -> None:
    authority = LocalCallerAuthority(SECRET, clock=lambda: TIME + 30)
    assert await authority.authenticate(_request())
    assert not await authority.authenticate(_request())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "header",
    [
        b"host",
        PROTOCOL_HEADER.encode(),
        TIMESTAMP_HEADER.encode(),
        NONCE_HEADER.encode(),
        SIGNATURE_HEADER.encode(),
        IDEMPOTENCY_HEADER.encode(),
    ],
)
async def test_duplicate_fixed_header_fails(header: bytes) -> None:
    authority = LocalCallerAuthority(SECRET, clock=lambda: TIME)
    duplicate = _request(extra_headers=[(header.upper(), b"duplicate")])
    assert not await authority.authenticate(duplicate)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [
        {"client": "192.0.2.1"},
        {"server": ("192.0.2.1", 18778)},
        {"host": b"localhost:18778"},
        {"host": b"127.0.0.1:18779"},
        {"host": b"127.0.0.1:evil"},
        {"host": b"127.0.0.1:99999"},
    ],
)
async def test_loopback_and_host_boundary(changed: dict[str, Any]) -> None:
    authority = LocalCallerAuthority(SECRET, clock=lambda: TIME)
    assert not await authority.authenticate(_request(**changed))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [
        {"method": "post", "signed_method": b"post"},
        {"path": b"//v1"},
        {"path": b"/v1/../admin"},
        {"path": b"/v1/%61dmin"},
        {"scope_path": "/different"},
        {"query": b"a=1\nb=2"},
        {"timestamp": b"01800000000"},
        {"nonce": b"A" * 32},
        {"idempotency_key": None},
    ],
)
async def test_noncanonical_or_missing_proof_fields_fail(changed: dict[str, Any]) -> None:
    authority = LocalCallerAuthority(SECRET, clock=lambda: TIME)
    assert not await authority.authenticate(_request(**changed))


@pytest.mark.asyncio
async def test_body_limit_and_invalid_signature_do_not_consume_nonce() -> None:
    authority = LocalCallerAuthority(SECRET, clock=lambda: TIME)
    assert not await authority.authenticate(_request(body=b"x" * (1024 * 1024 + 1)))
    assert not await authority.authenticate(_request(signature=b"0" * 64))
    assert await authority.authenticate(_request(body=b"x" * (1024 * 1024)))


@pytest.mark.asyncio
async def test_nonce_capacity_fails_closed_then_expires() -> None:
    current = [TIME]

    def clock() -> float:
        return current[0]

    authority = LocalCallerAuthority(SECRET, clock=clock, max_nonces=1)
    assert await authority.authenticate(_request(nonce=b"a" * 32))
    assert not await authority.authenticate(_request(nonce=b"b" * 32))
    current[0] = TIME + 31
    assert not await authority.authenticate(_request(nonce=b"a" * 32))
    later = _request(nonce=b"c" * 32, timestamp=b"1800000031")
    assert await authority.authenticate(later)


def test_constructor_requires_32_byte_secret() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        LocalCallerAuthority(b"short")
