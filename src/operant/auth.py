from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import math
import os
import secrets
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlencode, urlsplit

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

_AUTH_PREFIX = "/internal/auth"
_SESSION_COOKIE = "__Host-operant_session"
_TRANSACTION_COOKIE = "__Host-operant_oauth_transaction"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_MAX_TRANSACTIONS = 32
_MAX_SESSIONS = 8
_MAX_TOKEN_RESPONSE_BYTES = 256 * 1024
_MAX_JWKS_RESPONSE_BYTES = 1024 * 1024
_MAX_LOGIN_ATTEMPTS_PER_MINUTE = 10
_CLOCK_SKEW_SECONDS = 60


class AuthConfigurationError(ValueError):
    pass


class AuthFlowError(RuntimeError):
    pass


class AuthRateLimitError(AuthFlowError):
    def __init__(self, retry_after: int) -> None:
        super().__init__("too many authorization attempts")
        self.retry_after = retry_after


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode_b64url(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, binascii.Error) as exc:
        raise AuthFlowError("invalid base64url value") from exc


def _https_url(value: str, *, field: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise AuthConfigurationError(f"{field} must be an HTTPS URL without userinfo")
    if parsed.fragment:
        raise AuthConfigurationError(f"{field} must not contain a fragment")
    if parsed.query:
        raise AuthConfigurationError(f"{field} must not contain a query")
    return value


@dataclass(frozen=True, slots=True)
class OAuthConfig:
    issuer: str
    client_id: str
    subject: str
    redirect_uri: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    client_secret_ref: str | None = None
    revocation_endpoint: str | None = None
    scopes: tuple[str, ...] = ("openid", "profile")
    transaction_ttl_seconds: int = 300
    session_ttl_seconds: int = 28_800

    def __post_init__(self) -> None:
        _https_url(self.issuer, field="issuer")
        redirect = _https_url(self.redirect_uri, field="redirect_uri")
        _https_url(self.authorization_endpoint, field="authorization_endpoint")
        for field in ("token_endpoint", "jwks_uri"):
            _https_url(cast(str, getattr(self, field)), field=field)
        if self.revocation_endpoint is not None:
            _https_url(self.revocation_endpoint, field="revocation_endpoint")
        if not self.client_id or len(self.client_id) > 500:
            raise AuthConfigurationError("client_id is required")
        if not self.subject or len(self.subject) > 500:
            raise AuthConfigurationError("subject is required for single-user authentication")
        if urlsplit(redirect).path != f"{_AUTH_PREFIX}/callback":
            raise AuthConfigurationError(f"redirect_uri path must be {_AUTH_PREFIX}/callback")
        if "openid" not in self.scopes:
            raise AuthConfigurationError("openid scope is required for nonce verification")
        if not 30 <= self.transaction_ttl_seconds <= 600:
            raise AuthConfigurationError("transaction TTL must be between 30 and 600 seconds")
        if not 60 <= self.session_ttl_seconds <= 86_400:
            raise AuthConfigurationError("session TTL must be between 60 and 86400 seconds")

    @property
    def callback_path(self) -> str:
        return urlsplit(self.redirect_uri).path

    @property
    def browser_origin(self) -> str:
        parsed = urlsplit(self.redirect_uri)
        host = cast(str, parsed.hostname)
        if ":" in host:
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port not in {None, 443} else ""
        return f"{parsed.scheme}://{host}{port}"


def oauth_config_from_env(environment: Mapping[str, str] | None = None) -> OAuthConfig | None:
    values = os.environ if environment is None else environment
    required = {
        "issuer": values.get("OPERANT_OAUTH_ISSUER"),
        "client_id": values.get("OPERANT_OAUTH_CLIENT_ID"),
        "subject": values.get("OPERANT_OAUTH_SUBJECT"),
        "redirect_uri": values.get("OPERANT_OAUTH_REDIRECT_URI"),
        "authorization_endpoint": values.get("OPERANT_OAUTH_AUTHORIZATION_ENDPOINT"),
        "token_endpoint": values.get("OPERANT_OAUTH_TOKEN_ENDPOINT"),
        "jwks_uri": values.get("OPERANT_OAUTH_JWKS_URI"),
    }
    configured = {key for key, value in required.items() if value}
    if not configured:
        return None
    if len(configured) != len(required):
        missing = sorted(set(required) - configured)
        raise AuthConfigurationError(f"incomplete OAuth configuration: {', '.join(missing)}")
    scopes = tuple(filter(None, values.get("OPERANT_OAUTH_SCOPES", "openid profile").split()))
    return OAuthConfig(
        issuer=cast(str, required["issuer"]),
        client_id=cast(str, required["client_id"]),
        subject=cast(str, required["subject"]),
        redirect_uri=cast(str, required["redirect_uri"]),
        authorization_endpoint=cast(str, required["authorization_endpoint"]),
        token_endpoint=cast(str, required["token_endpoint"]),
        jwks_uri=cast(str, required["jwks_uri"]),
        client_secret_ref=values.get("OPERANT_OAUTH_CLIENT_SECRET_REF") or None,
        revocation_endpoint=values.get("OPERANT_OAUTH_REVOCATION_ENDPOINT") or None,
        scopes=scopes,
    )


@dataclass(slots=True)
class _Transaction:
    nonce: str
    verifier: str
    browser_binding_hash: str
    expires_at: float


@dataclass(slots=True)
class _Session:
    subject_hash: str
    tokens: dict[str, str]
    expires_at: float


class OAuthControl:
    """Single-user OAuth/OIDC control plane with process-local session state."""

    def __init__(
        self,
        config: OAuthConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
        clock: Any = time.time,
    ) -> None:
        self.config = config
        self._http_client = http_client
        self._owns_http_client = http_client is None
        self._clock = clock
        self._transactions: dict[str, _Transaction] = {}
        self._sessions: dict[str, _Session] = {}
        self._login_attempts: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()

    async def begin(
        self, *, browser_binding: str | None = None, client_key: str = "unknown"
    ) -> tuple[str, str]:
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        if not browser_binding or len(browser_binding) > 500:
            browser_binding = secrets.token_urlsafe(32)
        challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        now = float(self._clock())
        async with self._lock:
            self._prune(now)
            attempts = self._login_attempts.setdefault(client_key, deque())
            while attempts and attempts[0] <= now - 60:
                attempts.popleft()
            if len(attempts) >= _MAX_LOGIN_ATTEMPTS_PER_MINUTE:
                retry_after = max(1, int(60 - (now - attempts[0])))
                raise AuthRateLimitError(retry_after)
            attempts.append(now)
            binding_hash = hashlib.sha256(browser_binding.encode("utf-8")).hexdigest()
            self._transactions = {
                existing_state: existing
                for existing_state, existing in self._transactions.items()
                if existing.browser_binding_hash != binding_hash
            }
            if len(self._transactions) >= _MAX_TRANSACTIONS:
                raise AuthRateLimitError(self.config.transaction_ttl_seconds)
            self._transactions[state] = _Transaction(
                nonce=nonce,
                verifier=verifier,
                browser_binding_hash=binding_hash,
                expires_at=now + self.config.transaction_ttl_seconds,
            )
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.config.client_id,
                "redirect_uri": self.config.redirect_uri,
                "scope": " ".join(self.config.scopes),
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{self.config.authorization_endpoint}?{query}", browser_binding

    async def complete(
        self, *, state: str, code: str, browser_binding: str | None
    ) -> tuple[str, float]:
        if (
            not state
            or not code
            or not browser_binding
            or len(state) > 500
            or len(code) > 4_096
            or len(browser_binding) > 500
        ):
            raise AuthFlowError("invalid authorization callback")
        now = float(self._clock())
        async with self._lock:
            self._prune(now)
            transaction = self._transactions.get(state)
            if transaction is None or transaction.expires_at <= now:
                raise AuthFlowError("authorization transaction is invalid or expired")
            actual_binding_hash = hashlib.sha256(browser_binding.encode("utf-8")).hexdigest()
            if not hmac.compare_digest(transaction.browser_binding_hash, actual_binding_hash):
                raise AuthFlowError("authorization transaction browser binding failed")
            # Consume state only after the initiating browser binding is proven. This keeps a
            # leaked state value from becoming a one-request denial of service while retaining
            # atomic one-time use for competing callbacks.
            self._transactions.pop(state)

        token_response = await self._exchange_code(code, transaction.verifier)
        claims = await self._verify_id_token(token_response.get("id_token"), transaction.nonce)
        token_expires = token_response.get("expires_in", self.config.session_ttl_seconds)
        if (
            isinstance(token_expires, bool)
            or not isinstance(token_expires, (int, float))
            or not 1 <= token_expires <= 86_400
        ):
            raise AuthFlowError("invalid token expiration")
        expires_at = min(
            now + self.config.session_ttl_seconds,
            now + float(token_expires),
            float(claims["exp"]),
        )
        session_id = secrets.token_urlsafe(48)
        tokens: dict[str, str] = {}
        for token_name in ("access_token", "refresh_token", "id_token"):
            token = token_response.get(token_name)
            if token is None:
                continue
            if not isinstance(token, str) or not token or len(token) > 64 * 1024:
                raise AuthFlowError("invalid token response")
            tokens[token_name] = token
        if "access_token" not in tokens:
            raise AuthFlowError("token response did not contain an access token")
        subject = cast(str, claims["sub"])
        session = _Session(
            subject_hash=hashlib.sha256(subject.encode("utf-8")).hexdigest(),
            tokens=tokens,
            expires_at=expires_at,
        )
        async with self._lock:
            self._prune(now)
            if len(self._sessions) >= _MAX_SESSIONS:
                self._clear_tokens(session)
                raise AuthFlowError("too many active authorization sessions")
            self._sessions[session_id] = session
        return session_id, expires_at

    async def authenticated(self, session_id: str | None) -> bool:
        if not session_id:
            return False
        now = float(self._clock())
        expired: _Session | None = None
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is not None and session.expires_at <= now:
                expired = self._sessions.pop(session_id)
                session = None
        if expired is not None:
            self._clear_tokens(expired)
        return session is not None

    async def logout(self, session_id: str | None) -> None:
        if not session_id:
            return
        async with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            return
        revocation_tokens: list[bytes] = []
        if self.config.revocation_endpoint:
            for name in ("refresh_token", "access_token"):
                token = session.tokens.get(name)
                if token is not None:
                    revocation_tokens.append(token.encode("utf-8"))
        try:
            for revocation_token in revocation_tokens:
                await self._revoke(revocation_token.decode("utf-8"))
        finally:
            revocation_tokens.clear()
            self._clear_tokens(session)

    async def close(self) -> None:
        async with self._lock:
            sessions = tuple(self._sessions.values())
            self._sessions.clear()
            self._transactions.clear()
        for session in sessions:
            self._clear_tokens(session)
        if self._http_client is not None and self._owns_http_client:
            await self._http_client.aclose()

    def _prune(self, now: float) -> None:
        self._transactions = {
            state: transaction
            for state, transaction in self._transactions.items()
            if transaction.expires_at > now
        }
        expired_sessions = [
            session for session in self._sessions.values() if session.expires_at <= now
        ]
        self._sessions = {
            session_id: session
            for session_id, session in self._sessions.items()
            if session.expires_at > now
        }
        for session in expired_sessions:
            self._clear_tokens(session)
        for client_key, attempts in tuple(self._login_attempts.items()):
            while attempts and attempts[0] <= now - 60:
                attempts.popleft()
            if not attempts:
                self._login_attempts.pop(client_key, None)

    @staticmethod
    def _clear_tokens(session: _Session) -> None:
        session.tokens.clear()

    def _client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=10, follow_redirects=False)
        return self._http_client

    def _client_secret(self) -> str | None:
        reference = self.config.client_secret_ref
        if reference is None:
            return None
        if not reference.isidentifier():
            raise AuthConfigurationError("client secret reference is invalid")
        secret = os.getenv(reference)
        if not secret:
            raise AuthFlowError("configured client secret is unavailable")
        return secret

    async def _exchange_code(self, code: str, verifier: str) -> dict[str, Any]:
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_uri,
            "code_verifier": verifier,
        }
        client_secret = self._client_secret()
        if client_secret is not None:
            payload["client_secret"] = client_secret
        async with self._client().stream(
            "POST",
            self.config.token_endpoint,
            data=payload,
            headers={"Accept": "application/json"},
        ) as response:
            if response.status_code != 200:
                raise AuthFlowError("authorization server rejected the code exchange")
            result = await self._bounded_json(response, _MAX_TOKEN_RESPONSE_BYTES, "token endpoint")
        if not isinstance(result, dict) or result.get("token_type", "").lower() != "bearer":
            raise AuthFlowError("token endpoint returned an invalid response")
        return cast(dict[str, Any], result)

    async def _verify_id_token(self, token: object, expected_nonce: str) -> dict[str, Any]:
        if not isinstance(token, str) or len(token) > 64 * 1024:
            raise AuthFlowError("a signed ID token is required")
        segments = token.split(".")
        if len(segments) != 3:
            raise AuthFlowError("invalid ID token")
        try:
            header = json.loads(_decode_b64url(segments[0]))
            claims = json.loads(_decode_b64url(segments[1]))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AuthFlowError("invalid ID token") from exc
        if not isinstance(header, dict) or not isinstance(claims, dict):
            raise AuthFlowError("invalid ID token")
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            raise AuthFlowError("unsupported ID token signature")
        async with self._client().stream(
            "GET", self.config.jwks_uri, headers={"Accept": "application/json"}
        ) as response:
            if response.status_code != 200:
                raise AuthFlowError("unable to validate ID token")
            jwks = await self._bounded_json(response, _MAX_JWKS_RESPONSE_BYTES, "JWKS endpoint")
        keys = jwks.get("keys") if isinstance(jwks, dict) else None
        if not isinstance(keys, list) or len(keys) > 128:
            raise AuthFlowError("invalid JWKS response")
        matching = [
            key for key in keys if isinstance(key, dict) and key.get("kid") == header["kid"]
        ]
        if len(matching) != 1:
            raise AuthFlowError("ID token signing key is unavailable or ambiguous")
        key = matching[0]
        if (
            key.get("kty") != "RSA"
            or key.get("use", "sig") != "sig"
            or key.get("alg", "RS256") != "RS256"
            or not isinstance(key.get("e"), str)
            or not isinstance(key.get("n"), str)
        ):
            raise AuthFlowError("invalid ID token signing key")
        try:
            exponent = int.from_bytes(_decode_b64url(cast(str, key["e"])), "big")
            modulus = int.from_bytes(_decode_b64url(cast(str, key["n"])), "big")
            if modulus.bit_length() < 2048 or exponent < 3 or exponent % 2 == 0:
                raise ValueError("RSA signing key is too weak")
            public_key = rsa.RSAPublicNumbers(exponent, modulus).public_key()
            public_key.verify(
                _decode_b64url(segments[2]),
                f"{segments[0]}.{segments[1]}".encode("ascii"),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except (InvalidSignature, KeyError, TypeError, ValueError) as exc:
            raise AuthFlowError("invalid ID token signing key") from exc
        now = float(self._clock())
        audience = claims.get("aud")
        audience_valid = audience == self.config.client_id or (
            isinstance(audience, list)
            and all(isinstance(value, str) for value in audience)
            and self.config.client_id in audience
        )
        authorized_party = claims.get("azp")
        authorized_party_valid = (
            authorized_party is None or authorized_party == self.config.client_id
        )
        if isinstance(audience, list) and len(audience) > 1:
            authorized_party_valid = authorized_party == self.config.client_id
        expiration = claims.get("exp")
        not_before = claims.get("nbf")
        issued_at = claims.get("iat")
        expiration_valid = (
            not isinstance(expiration, bool)
            and isinstance(expiration, (int, float))
            and math.isfinite(float(expiration))
            and float(expiration) > now
        )
        not_before_valid = not_before is None or (
            not isinstance(not_before, bool)
            and isinstance(not_before, (int, float))
            and math.isfinite(float(not_before))
            and float(not_before) <= now + _CLOCK_SKEW_SECONDS
        )
        issued_at_valid = issued_at is None or (
            not isinstance(issued_at, bool)
            and isinstance(issued_at, (int, float))
            and math.isfinite(float(issued_at))
            and float(issued_at) <= now + 60
        )
        if (
            claims.get("iss") != self.config.issuer
            or not audience_valid
            or not authorized_party_valid
            or not isinstance(claims.get("sub"), str)
            or not hmac.compare_digest(cast(str, claims["sub"]), self.config.subject)
            or not expiration_valid
            or not not_before_valid
            or not issued_at_valid
            or not isinstance(claims.get("nonce"), str)
            or not hmac.compare_digest(cast(str, claims["nonce"]), expected_nonce)
        ):
            raise AuthFlowError("ID token claims validation failed")
        return cast(dict[str, Any], claims)

    @staticmethod
    async def _bounded_json(response: httpx.Response, limit: int, label: str) -> Any:
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise AuthFlowError(f"{label} returned an invalid content type")
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError as exc:
                raise AuthFlowError(f"{label} returned an invalid content length") from exc
            if declared < 0 or declared > limit:
                raise AuthFlowError(f"{label} response is too large")
        payload = bytearray()
        async for chunk in response.aiter_bytes():
            if len(payload) + len(chunk) > limit:
                raise AuthFlowError(f"{label} response is too large")
            payload.extend(chunk)
        try:
            return json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AuthFlowError(f"{label} returned invalid JSON") from exc

    async def _revoke(self, token: str) -> None:
        assert self.config.revocation_endpoint is not None
        payload = {"token": token, "client_id": self.config.client_id}
        client_secret = self._client_secret()
        if client_secret is not None:
            payload["client_secret"] = client_secret
        async with self._client().stream(
            "POST", self.config.revocation_endpoint, data=payload
        ) as response:
            if response.status_code not in {200, 204}:
                raise AuthFlowError("authorization server rejected token revocation")


class AuthMiddleware:
    def __init__(self, app: ASGIApp, *, control: OAuthControl) -> None:
        self.app = app
        self.control = control

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive=receive)
        path = request.url.path
        public = path in {
            "/healthz",
            f"{_AUTH_PREFIX}/login",
            f"{_AUTH_PREFIX}/status",
            self.control.config.callback_path,
        }
        if public:
            await self.app(scope, receive, send)
            return
        if request.method not in _SAFE_METHODS:
            origin = request.headers.get("origin")
            if origin != self.control.config.browser_origin:
                await JSONResponse(
                    {
                        "error": {
                            "code": "auth_origin_rejected",
                            "message": "request origin rejected",
                        }
                    },
                    status_code=403,
                    headers={"Cache-Control": "no-store"},
                )(scope, receive, send)
                return
        if not await self.control.authenticated(request.cookies.get(_SESSION_COOKIE)):
            await JSONResponse(
                {
                    "error": {
                        "code": "authentication_required",
                        "message": "authentication required",
                    }
                },
                status_code=401,
                headers={"Cache-Control": "no-store"},
            )(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _request_matches_redirect(request: Request, redirect_uri: str) -> bool:
    expected = urlsplit(redirect_uri)
    actual = request.url
    expected_port = expected.port or 443
    actual_port = actual.port or (443 if actual.scheme == "https" else 80)
    return (
        actual.scheme == expected.scheme
        and actual.hostname == expected.hostname
        and actual_port == expected_port
        and actual.path == expected.path
    )


def install_oauth_control(app: FastAPI, control: OAuthControl) -> None:
    app.state.oauth_control = control

    @app.get(f"{_AUTH_PREFIX}/login", include_in_schema=False)
    async def login(request: Request) -> Response:
        client_key = "unknown" if request.client is None else request.client.host
        try:
            authorization_url, browser_binding = await control.begin(
                browser_binding=request.cookies.get(_TRANSACTION_COOKIE),
                client_key=client_key,
            )
        except AuthRateLimitError as exc:
            return JSONResponse(
                {"error": "authorization_rate_limited"},
                status_code=429,
                headers={"Cache-Control": "no-store", "Retry-After": str(exc.retry_after)},
            )
        response = RedirectResponse(
            authorization_url,
            status_code=302,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
        response.set_cookie(
            _TRANSACTION_COOKIE,
            browser_binding,
            max_age=control.config.transaction_ttl_seconds,
            secure=True,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return response

    @app.get(control.config.callback_path, include_in_schema=False)
    async def callback(request: Request) -> Response:
        query_items = request.query_params.multi_items()
        query_names = [name for name, _value in query_items]
        if not _request_matches_redirect(request, control.config.redirect_uri) or sorted(
            query_names
        ) != ["code", "state"]:
            return JSONResponse(
                {"error": "invalid_callback"},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        try:
            session_id, expires_at = await control.complete(
                state=request.query_params.get("state", ""),
                code=request.query_params.get("code", ""),
                browser_binding=request.cookies.get(_TRANSACTION_COOKIE),
            )
        except (AuthFlowError, httpx.HTTPError, json.JSONDecodeError):
            failure_response = JSONResponse(
                {"error": "authorization_failed"},
                status_code=401,
                headers={"Cache-Control": "no-store"},
            )
            failure_response.delete_cookie(
                _TRANSACTION_COOKIE, path="/", secure=True, httponly=True, samesite="lax"
            )
            return failure_response
        response = RedirectResponse("/web", status_code=303, headers={"Cache-Control": "no-store"})
        response.delete_cookie(
            _TRANSACTION_COOKIE, path="/", secure=True, httponly=True, samesite="lax"
        )
        response.set_cookie(
            _SESSION_COOKIE,
            session_id,
            max_age=max(0, int(expires_at - time.time())),
            secure=True,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return response

    @app.get(f"{_AUTH_PREFIX}/status", include_in_schema=False)
    async def status(request: Request) -> Response:
        authenticated = await control.authenticated(request.cookies.get(_SESSION_COOKIE))
        return JSONResponse({"authenticated": authenticated}, headers={"Cache-Control": "no-store"})

    @app.post(f"{_AUTH_PREFIX}/logout", include_in_schema=False)
    async def logout(request: Request) -> Response:
        try:
            await control.logout(request.cookies.get(_SESSION_COOKIE))
        except (AuthFlowError, httpx.HTTPError):
            failure_response = JSONResponse(
                {"error": "revocation_failed"},
                status_code=502,
                headers={"Cache-Control": "no-store"},
            )
            failure_response.delete_cookie(
                _SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax"
            )
            return failure_response
        response = Response(status_code=204, headers={"Cache-Control": "no-store"})
        response.delete_cookie(
            _SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax"
        )
        return response

    app.add_middleware(AuthMiddleware, control=control)
