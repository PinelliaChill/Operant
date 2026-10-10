"""Pure ASGI proof boundary for routes served by a self-managed local Core.

Install this outside command/file middleware so rejected requests have no
downstream effects. It does not create or distribute the caller secret.
"""

from __future__ import annotations

import posixpath
import re
from urllib.parse import unquote

from fastapi import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from operant.local_caller import (
    NONCE_HEADER,
    PROTOCOL_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    LocalCallerAuthority,
    _canonical_request,
)

_PROOF_HEADERS = frozenset(
    name.encode("ascii")
    for name in (PROTOCOL_HEADER, TIMESTAMP_HEADER, NONCE_HEADER, SIGNATURE_HEADER)
)
_DENIED = {"error": {"code": "local_caller_required", "message": "本机调用者验证失败"}}
_MAX_BODY_MESSAGES = 4096


def _route(scope: Scope) -> tuple[str, str] | None:
    """Return a raw, unambiguous method/path pair for routing decisions."""
    request = Request(scope)
    canonical = _canonical_request(request)
    if canonical is None:
        return None
    method, _target = canonical
    raw_path = scope["raw_path"]
    return method.decode("ascii"), raw_path.decode("ascii")


def _has_proof_header(scope: Scope) -> bool | None:
    try:
        headers = scope["headers"]
        if not isinstance(headers, (list, tuple)):
            return None
        if any(
            not isinstance(item, tuple)
            or len(item) != 2
            or not isinstance(item[0], bytes)
            or not isinstance(item[1], bytes)
            for item in headers
        ):
            return None
        return any(key.lower() in _PROOF_HEADERS for key, _ in headers)
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def _may_be_protected(
    path: str, prefixes: tuple[str, ...], patterns: tuple[re.Pattern[str], ...]
) -> bool:
    """Use the routed path and conservative aliases only to decide when to verify."""
    candidates = {path}
    decoded = path
    for _ in range(2):
        decoded = unquote(decoded)
        candidates.add(decoded)
    candidates.update(posixpath.normpath(re.sub(r"/+", "/", item)) for item in tuple(candidates))
    return any(
        candidate == prefix or candidate.startswith(prefix + "/")
        for candidate in candidates
        for prefix in prefixes
    ) or any(pattern.fullmatch(candidate) for candidate in candidates for pattern in patterns)


class LocalCallerProofMiddleware:
    """Authenticate protected routes and replay their exact body to downstream ASGI."""

    def __init__(
        self,
        app: ASGIApp,
        authority: LocalCallerAuthority,
        protected_prefixes: tuple[str, ...],
        public_exact_routes: frozenset[tuple[str, str]],
        protected_path_patterns: tuple[str, ...] = (),
    ) -> None:
        self.app = app
        self.authority = authority
        if any(not prefix.startswith("/") or prefix == "/" for prefix in protected_prefixes):
            raise ValueError("protected prefixes must be non-root absolute paths")
        self.protected_prefixes = tuple(prefix.rstrip("/") for prefix in protected_prefixes)
        self.public_exact_routes = public_exact_routes
        self.protected_path_patterns = tuple(
            re.compile(pattern) for pattern in protected_path_patterns
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        denied = False
        downstream_receive = receive
        try:
            path = scope.get("path")
            has_proof = _has_proof_header(scope)
            if not isinstance(path, str) or has_proof is None:
                denied = True
            else:
                protected = _may_be_protected(
                    path, self.protected_prefixes, self.protected_path_patterns
                )
                if protected or has_proof:
                    route = _route(scope)
                    if route is None:
                        denied = True
                    else:
                        public = route in self.public_exact_routes
                        should_authenticate = has_proof or (protected and not public)
                else:
                    should_authenticate = False
                if not denied and should_authenticate:
                    captured: list[Message] = []

                    async def capturing_receive() -> Message:
                        message = await receive()
                        if message["type"] == "http.request":
                            if len(captured) >= _MAX_BODY_MESSAGES:
                                raise ValueError("too many body chunks")
                            captured.append(message)
                        return message

                    request = Request(scope, capturing_receive)
                    if not await self.authority.authenticate(request):
                        denied = True
                    else:
                        index = 0

                        async def replay_receive() -> Message:
                            nonlocal index
                            if index < len(captured):
                                message = captured[index]
                                index += 1
                                return message
                            return await receive()

                        downstream_receive = replay_receive

        except Exception:
            # Malformed request metadata/body must never disclose exception text.
            denied = True
        if denied:
            await self._deny(scope, receive, send)
            return
        await self.app(scope, downstream_receive, send)

    @staticmethod
    async def _deny(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(_DENIED, status_code=403, headers={"Cache-Control": "no-store"})
        await response(scope, receive, send)
