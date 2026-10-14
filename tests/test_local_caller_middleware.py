from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest
from fastapi import FastAPI, Request
from pydantic import BaseModel
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Scope

from operant.local_caller import LocalCallerAuthority, canonical_proof
from operant.local_caller_middleware import LocalCallerProofMiddleware

_SECRET = b"s" * 32
_TIME = 1_800_000_000.0


class _Payload(BaseModel):
    marker: str


def _proof_headers(
    *,
    method: bytes = b"POST",
    path: bytes = b"/v1/submit",
    query: bytes = b"b=2&a=1",
    body: bytes = b'{"marker":"ok"}',
    nonce: bytes = b"a" * 32,
    key: bytes = b"command-1",
) -> list[tuple[bytes, bytes]]:
    target = path + (b"?" + query if query else b"")
    proof = canonical_proof(
        method=method,
        target=target,
        timestamp=b"1800000000",
        nonce=nonce,
        idempotency_key=key,
        body_digest=hashlib.sha256(body).hexdigest().encode(),
    )
    signature = hmac.new(_SECRET, proof, hashlib.sha256).hexdigest().encode()
    return [
        (b"x-operant-caller-protocol", b"local-caller.v1"),
        (b"x-operant-caller-timestamp", b"1800000000"),
        (b"x-operant-caller-nonce", nonce),
        (b"x-operant-caller-signature", signature),
        (b"idempotency-key", key),
    ]


async def _call(
    app: ASGIApp,
    *,
    method: str = "POST",
    path: bytes = b"/v1/submit",
    scope_path: str | None = None,
    query: bytes = b"b=2&a=1",
    chunks: tuple[bytes, ...] = (b'{"marker":', b'"ok"}'),
    proof: bool = False,
    headers: list[tuple[bytes, bytes]] | None = None,
    state: dict[str, Any] | None = None,
    malformed_headers: bool = False,
) -> tuple[int, dict[str, Any], list[Message]]:
    body = b"".join(chunks)
    all_headers = [(b"host", b"127.0.0.1:18778"), (b"content-type", b"application/json")]
    if proof:
        all_headers.extend(
            _proof_headers(method=method.encode(), path=path, query=query, body=body)
        )
    all_headers.extend(headers or [])
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "scheme": "http",
        "method": method,
        "path": path.decode() if scope_path is None else scope_path,
        "raw_path": path,
        "query_string": query,
        "headers": all_headers,
        "client": ("127.0.0.1", 50123),
        "server": ("127.0.0.1", 18778),
        "root_path": "",
        "state": state or {},
    }
    if malformed_headers:
        scope["headers"] = None  # type: ignore[typeddict-item]
    received = 0
    sent: list[Message] = []

    async def receive() -> Message:
        nonlocal received
        if received < len(chunks):
            index = received
            received += 1
            return {
                "type": "http.request",
                "body": chunks[index],
                "more_body": received < len(chunks),
            }
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)
    start = next(message for message in sent if message["type"] == "http.response.start")
    response_body = b"".join(
        message.get("body", b"") for message in sent if message["type"] == "http.response.body"
    )
    return start["status"], json.loads(response_body), sent


def _application() -> tuple[ASGIApp, LocalCallerAuthority, list[str]]:
    authority = LocalCallerAuthority(_SECRET, clock=lambda: _TIME)
    effects: list[str] = []
    app = FastAPI()

    @app.post("/v1/submit")
    async def submit(payload: _Payload, request: Request) -> dict[str, str]:
        assert authority.is_trusted(request)
        assert await authority.authenticate(request)  # Same scope does not consume the nonce twice.
        effects.append(payload.marker)
        return {"marker": payload.marker, "query": request.url.query}

    @app.get("/v1/public")
    async def public() -> dict[str, str]:
        return {"status": "public"}

    @app.get("/outside")
    async def outside(request: Request) -> dict[str, bool]:
        return {"trusted": authority.is_trusted(request)}

    wrapped = LocalCallerProofMiddleware(
        app,
        authority,
        protected_prefixes=("/v1",),
        public_exact_routes=frozenset({("GET", "/v1/public")}),
    )
    return wrapped, authority, effects


@pytest.mark.asyncio
async def test_multichunk_signed_body_replays_to_pydantic_route_once() -> None:
    app, _authority, effects = _application()
    status, body, _ = await _call(app, proof=True)
    assert status == 200
    assert body == {"marker": "ok", "query": "b=2&a=1"}
    assert effects == ["ok"]
    replay_status, replay_body, _ = await _call(app, proof=True)
    assert replay_status == 403
    assert replay_body["error"]["code"] == "local_caller_required"
    assert effects == ["ok"]


@pytest.mark.asyncio
async def test_replay_preserves_original_chunk_messages() -> None:
    authority = LocalCallerAuthority(_SECRET, clock=lambda: _TIME)
    observed: list[Message] = []

    async def downstream(scope: Scope, receive: Any, send: Any) -> None:
        assert authority.is_trusted(Request(scope))
        first = await receive()
        second = await receive()
        observed.extend((first, second))
        response = JSONResponse({"received": True})
        await response(scope, receive, send)

    middleware = LocalCallerProofMiddleware(
        downstream, authority, protected_prefixes=("/v1",), public_exact_routes=frozenset()
    )
    status, _, _ = await _call(middleware, proof=True)
    assert status == 200
    assert [(message["body"], message["more_body"]) for message in observed] == [
        (b'{"marker":', True),
        (b'"ok"}', False),
    ]


@pytest.mark.asyncio
async def test_missing_tampered_and_oversize_proof_never_reach_effect() -> None:
    app, _authority, effects = _application()
    status, body, sent = await _call(app)
    assert status == 403 and body["error"]["code"] == "local_caller_required"
    assert any(
        (b"cache-control", b"no-store") in message.get("headers", [])
        for message in sent
        if message["type"] == "http.response.start"
    )
    signed = _proof_headers()
    tampered = [
        (name, b"bad" if name == b"x-operant-caller-signature" else value) for name, value in signed
    ]
    assert (await _call(app, headers=tampered))[0] == 403
    assert (
        await _call(
            app,
            headers=signed + [(b"X-OPERANT-CALLER-NONCE", b"b" * 32)],
        )
    )[0] == 403
    assert (await _call(app, proof=True, chunks=(b"x" * (1024 * 1024), b"x")))[0] == 403
    assert effects == []


@pytest.mark.asyncio
async def test_public_exact_exception_does_not_cover_other_method_or_path() -> None:
    app, _authority, effects = _application()
    assert (await _call(app, method="GET", path=b"/v1/public", query=b"code=one"))[0] == 200
    for method, path, query in (
        ("POST", b"/v1/public", b""),
        ("GET", b"/v1/public/extra", b""),
        ("GET", b"/v1/submit", b"next=/v1/public"),
        ("GET", b"//v1/public", b""),
        ("GET", b"/v1/%70ublic", b""),
    ):
        status, body, _ = await _call(app, method=method, path=path, query=query)
        assert status == 403 and body["error"]["code"] == "local_caller_required"
    assert effects == []


@pytest.mark.asyncio
async def test_unprotected_head_options_and_encoded_file_paths_pass_unchanged() -> None:
    authority = LocalCallerAuthority(_SECRET, clock=lambda: _TIME)
    observed: list[tuple[str, str, bytes]] = []

    async def downstream(scope: Scope, receive: Any, send: Any) -> None:
        observed.append((scope["method"], scope["path"], scope["raw_path"]))
        response = JSONResponse({"routed": True})
        await response(scope, receive, send)

    middleware = LocalCallerProofMiddleware(
        downstream, authority, protected_prefixes=("/v1",), public_exact_routes=frozenset()
    )
    for method in ("HEAD", "OPTIONS"):
        status, body, _ = await _call(middleware, method=method, path=b"/outside", query=b"")
        assert status == 200 and body == {"routed": True}
    status, body, _ = await _call(
        middleware,
        method="GET",
        path=b"/files/a%20b",
        scope_path="/files/a b",
        query=b"",
    )
    assert status == 200 and body == {"routed": True}
    assert observed == [
        ("HEAD", "/outside", b"/outside"),
        ("OPTIONS", "/outside", b"/outside"),
        ("GET", "/files/a b", b"/files/a%20b"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_path", "decoded_path"),
    [
        (b"/v%31/submit", "/v1/submit"),
        (b"/v1/%70ublic", "/v1/public"),
        (b"//v1/public", "//v1/public"),
        (b"/outside/../v1/submit", "/outside/../v1/submit"),
    ],
)
async def test_decoded_protected_aliases_cannot_bypass_proof(
    raw_path: bytes, decoded_path: str
) -> None:
    app, _authority, effects = _application()
    status, body, _ = await _call(
        app, method="GET", path=raw_path, scope_path=decoded_path, query=b""
    )
    assert status == 403 and body["error"]["code"] == "local_caller_required"
    assert effects == []


@pytest.mark.asyncio
async def test_optional_proof_outside_prefix_must_verify_before_trust() -> None:
    app, _authority, _effects = _application()
    assert (await _call(app, method="GET", path=b"/outside", query=b""))[1] == {"trusted": False}
    body = b"".join((b'{"marker":', b'"ok"}'))
    headers = _proof_headers(method=b"GET", path=b"/outside", query=b"", body=body)
    status, result, _ = await _call(app, method="GET", path=b"/outside", query=b"", headers=headers)
    assert status == 200 and result == {"trusted": True}
    assert (await _call(app, method="GET", path=b"/outside", query=b"", headers=headers))[0] == 403
    bad = [
        (name, b"0" * 64 if name == b"x-operant-caller-signature" else value)
        for name, value in headers
    ]
    assert (await _call(app, method="GET", path=b"/outside", query=b"", headers=bad))[0] == 403


@pytest.mark.asyncio
async def test_state_boolean_and_malformed_headers_cannot_grant_trust() -> None:
    app, authority, effects = _application()
    status, _, _ = await _call(app, state={"local_caller_trusted": True})
    assert status == 403 and not effects
    forged_scope: Scope = {"type": "http", "state": {"local_caller_trusted": True}}
    assert not authority.is_trusted(Request(forged_scope))
    status, body, _ = await _call(
        app, headers=[(b"x-operant-caller-protocol", b"local-caller.v1"), (b"broken", b"x")]
    )
    assert status == 403 and body["error"]["code"] == "local_caller_required"
    malformed_status, malformed_body, _ = await _call(app, malformed_headers=True)
    assert malformed_status == 403 and malformed_body == {
        "error": {"code": "local_caller_required", "message": "本机调用者验证失败"}
    }


@pytest.mark.asyncio
async def test_downstream_error_is_not_rewritten_as_auth_failure() -> None:
    authority = LocalCallerAuthority(_SECRET, clock=lambda: _TIME)

    async def broken(_scope: Scope, _receive: Any, _send: Any) -> None:
        raise RuntimeError("downstream failure")

    middleware = LocalCallerProofMiddleware(
        broken, authority, protected_prefixes=("/v1",), public_exact_routes=frozenset()
    )
    with pytest.raises(RuntimeError, match="downstream failure"):
        await _call(middleware, proof=True)


@pytest.mark.asyncio
async def test_non_http_scope_passes_through() -> None:
    observed: list[str] = []

    async def downstream(scope: Scope, _receive: Any, _send: Any) -> None:
        observed.append(scope["type"])

    authority = LocalCallerAuthority(_SECRET, clock=lambda: _TIME)
    middleware = LocalCallerProofMiddleware(
        downstream, authority, protected_prefixes=("/v1",), public_exact_routes=frozenset()
    )

    async def receive() -> Message:
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(_message: Message) -> None:
        pass

    await middleware({"type": "websocket", "path": "/v1/submit"}, receive, send)
    assert observed == ["websocket"]
