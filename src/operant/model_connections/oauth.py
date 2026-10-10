from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Protocol, cast
from urllib.parse import urlencode, urlsplit

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from operant.model_connections.credentials import CredentialError, CredentialStore


class ConnectionRepository(Protocol):
    def get_connection(self, connection_id: str) -> dict[str, Any] | None: ...
    def list_connections(self) -> list[dict[str, Any]]: ...
    def save_connection(self, connection_id: str, record: dict[str, Any]) -> None: ...
    def delete_connection(self, connection_id: str) -> None: ...
    def get_setting(self, key: str, default: Any = None) -> Any: ...
    def set_setting(self, key: str, value: Any) -> None: ...
    def get_provider_metadata(self, key: str) -> dict[str, Any] | None: ...
    def save_provider_metadata(self, key: str, record: dict[str, Any]) -> None: ...
    def delete_provider_metadata(self, key: str) -> None: ...
    def get_command(self, key: str, fingerprint: str) -> dict[str, Any] | None: ...
    def get_command_fingerprint(self, key: str) -> str | None: ...
    def get_command_fingerprint_version(self, key: str) -> str | None: ...
    def get_connection_request_snapshot(
        self, key: str, connection_id: str
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]: ...
    def save_command(self, key: str, fingerprint: str, result: dict[str, Any]) -> None: ...


class OAuthError(RuntimeError):
    """A sanitized OAuth failure; never include callback or token contents."""


@dataclass(slots=True)
class Attempt:
    id: str
    provider: str
    connection_id: str
    state: str
    nonce: str
    verifier: str
    client_id: str
    redirect_uri: str
    started_at: float
    status: str = "pending"
    error: str | None = None
    authorization_url: str | None = None
    google_client_secret: str | None = None
    created_new: bool = False
    issued_client_id: str | None = None


_OPENAI = {
    "authorize": "https://auth.openai.com/api/accounts/authorize",
    "token": "https://auth.openai.com/api/accounts/oauth/token",
    "revoke": "https://auth.openai.com/api/accounts/oauth/revoke",
    "jwks": "https://auth.openai.com/.well-known/jwks.json",
    "issuer": "https://auth.openai.com",
}
_GOOGLE = {
    "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
    "token": "https://oauth2.googleapis.com/token",
    "revoke": "https://oauth2.googleapis.com/revoke",
    "jwks": "https://www.googleapis.com/oauth2/v3/certs",
    "issuer": "https://accounts.google.com",
}
_OPENAI_SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
_GOOGLE_SCOPES = (
    "openid email profile https://www.googleapis.com/auth/cloud-platform "
    "https://www.googleapis.com/auth/generative-language.retriever"
)
_GOOGLE_REQUIRED_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/cloud-platform",
        "https://www.googleapis.com/auth/generative-language.retriever",
    }
)


def _gemini_scopes_allowed(scopes: object) -> bool:
    return isinstance(scopes, (list, tuple)) and _GOOGLE_REQUIRED_SCOPES.issubset(scopes)


def _reauthorization_blocker(record: dict[str, Any]) -> str | None:
    error = record.get("error")
    if error in {"authentication_required", "revocation_unconfirmed"}:
        return str(error)
    if record.get("status") == "needs_auth":
        if error in {"authentication_required", "permission_denied"}:
            return str(error)
        return "OAuth connection needs sign-in"
    return None


_LOG = logging.getLogger(__name__)
_CODE_EXCHANGE_ERRORS = frozenset(
    {
        "invalid_grant",
        "invalid_client",
        "unauthorized_client",
        "invalid_request",
        "invalid_scope",
        "unsupported_grant_type",
        "access_denied",
        "server_error",
        "temporarily_unavailable",
        "3p_delegated_access_policy_denied",
        "subscription_sharing_user_not_eligible",
    }
)


def _code_exchange_failure(response: httpx.Response) -> str:
    # Error descriptions and arbitrary provider codes may echo credentials.
    # Keep the actual status and only recognized, bounded machine codes.
    code = "unknown"
    if len(response.content) <= 65_536:
        try:
            body = response.json()
        except ValueError:
            body = None
        error = body.get("error") if isinstance(body, dict) else None
        supplied = error.get("code") if isinstance(error, dict) else error
        if isinstance(supplied, str) and supplied in _CODE_EXCHANGE_ERRORS:
            code = supplied
    return f"OAuth code exchange failed (HTTP {response.status_code}; {code})"


def _request_failure(stage: str, error: Exception) -> str:
    if isinstance(error, httpx.TimeoutException):
        category = "timeout"
    elif isinstance(error, httpx.ProxyError):
        category = "proxy_error"
    elif isinstance(error, httpx.ConnectError):
        category = "connection_error"
    elif isinstance(error, httpx.ProtocolError):
        category = "protocol_error"
    elif isinstance(error, httpx.NetworkError):
        category = "network_error"
    elif isinstance(error, httpx.HTTPError):
        category = "http_error"
    elif isinstance(error, CredentialError):
        category = "credential_store_error"
    elif isinstance(error, json.JSONDecodeError):
        category = "invalid_response"
    elif isinstance(error, KeyError):
        category = "missing_data"
    else:
        category = "invalid_data"
    return f"OAuth request failed ({stage}; {category})"


def _timeout_phase(error: Exception) -> str:
    for error_type, phase in (
        (httpx.ConnectTimeout, "connect"),
        (httpx.ReadTimeout, "read"),
        (httpx.WriteTimeout, "write"),
        (httpx.PoolTimeout, "pool"),
    ):
        if isinstance(error, error_type):
            return phase
    return "none"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def secret_ref(connection_id: str, suffix: str) -> str:
    return f"OPERANT_CONNECTION_{connection_id.replace('-', '').upper()}_{suffix}"


def _connection_lock_name(connection_id: str) -> str:
    return "refresh-" + hashlib.sha256(connection_id.encode()).hexdigest()[:32]


class OAuthConnections:
    def __init__(
        self,
        repo: ConnectionRepository,
        credentials: CredentialStore,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Any = time.time,
    ) -> None:
        self.repo = repo
        self.credentials = credentials
        self.transport = transport
        self.clock = clock
        self.attempts: dict[str, Attempt] = {}
        self._registration_lock = threading.Lock()
        self._refresh_locks: dict[str, asyncio.Lock] = {}

    async def _acquire_connection_lock(self, connection_id: str) -> int:
        lock_name = _connection_lock_name(connection_id)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            fd = self.credentials.try_acquire_lock(lock_name)
            if fd is not None:
                return fd
            await asyncio.sleep(0.05)
        raise OAuthError("credential update is busy")

    def start(
        self,
        *,
        provider: str,
        redirect_uri: str,
        connection_id: str | None = None,
        google_client_id: str | None = None,
        google_client_secret: str | None = None,
        project_id: str | None = None,
    ) -> tuple[Attempt, str]:
        with self._registration_lock:
            return self._start(
                provider=provider,
                redirect_uri=redirect_uri,
                connection_id=connection_id,
                google_client_id=google_client_id,
                google_client_secret=google_client_secret,
                project_id=project_id,
            )

    def _start(
        self,
        *,
        provider: str,
        redirect_uri: str,
        connection_id: str | None = None,
        google_client_id: str | None = None,
        google_client_secret: str | None = None,
        project_id: str | None = None,
    ) -> tuple[Attempt, str]:
        if provider not in {"chatgpt", "gemini"}:
            raise OAuthError("unsupported OAuth provider")
        uri = urlsplit(redirect_uri)
        if (
            uri.scheme != "http"
            or uri.hostname != "127.0.0.1"
            or uri.port is None
            or uri.path != f"/internal/model-auth/{provider}/callback"
            or uri.query
            or uri.fragment
        ):
            raise OAuthError("OAuth callback must use the local loopback listener")
        existing = self.repo.get_connection(connection_id) if connection_id else None
        registration = next(
            (
                item
                for item in reversed(self.attempts.values())
                if provider == "chatgpt"
                and item.provider == provider
                and item.connection_id == connection_id
                and item.created_new
                and item.status == "failed"
                and item.error == "OAuth code exchange failed (HTTP 400; invalid_grant)"
                and item.issued_client_id
            ),
            None,
        )
        if connection_id and existing is None and registration is None:
            raise OAuthError("connection does not exist")
        if existing and existing.get("provider") != provider:
            raise OAuthError("connection provider does not match")
        if existing and existing.get("auth_method") != "oauth":
            raise OAuthError("connection authorization method does not match")
        connection_id = connection_id or str(uuid.uuid4())
        if provider == "chatgpt":
            client_id = (
                str(existing.get("client_id"))
                if existing
                else str(registration.issued_client_id)
                if registration
                else "dynamic_agent_client"
            )
            host_id = self.repo.get_setting("chatgpt_host_id")
            if not isinstance(host_id, str) or not host_id:
                host_id = f"urn:uuid:{uuid.uuid4()}"
                self.repo.set_setting("chatgpt_host_id", host_id)
        else:
            if existing and google_client_id and google_client_id != existing.get("client_id"):
                raise OAuthError("Google client ID does not match this connection")
            if existing and project_id and project_id != existing.get("project_id"):
                raise OAuthError("Google Cloud project does not match this connection")
            saved_client_id = existing.get("client_id") if existing else None
            client_id = (
                google_client_id
                or (saved_client_id if isinstance(saved_client_id, str) else None)
                or self.credentials.get_operator_configuration("OPERANT_GEMINI_OAUTH_CLIENT_ID")
                or ""
            )
            project_id = project_id or (str(existing.get("project_id")) if existing else "")
            if not client_id or not project_id:
                raise OAuthError("Google desktop client ID and Cloud project ID are required")
            if len(client_id) > 500 or len(project_id) > 200:
                raise OAuthError("Google OAuth configuration is invalid")
            google_client_secret = (
                google_client_secret
                or self.credentials.get(secret_ref(connection_id, "CLIENT_SECRET"))
                or self.credentials.get_operator_configuration("OPERANT_GEMINI_OAUTH_CLIENT_SECRET")
            )
            if not google_client_secret or len(google_client_secret) > 4096:
                raise OAuthError("Google desktop client secret is required")
        attempt = Attempt(
            id=str(uuid.uuid4()),
            provider=provider,
            connection_id=connection_id,
            state=secrets.token_urlsafe(32),
            nonce=secrets.token_urlsafe(32),
            verifier=secrets.token_urlsafe(64),
            client_id=client_id,
            redirect_uri=redirect_uri,
            started_at=float(self.clock()),
            google_client_secret=google_client_secret if provider == "gemini" else None,
            created_new=existing is None,
        )
        self.attempts[attempt.id] = attempt
        params = {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": _OPENAI_SCOPES if provider == "chatgpt" else _GOOGLE_SCOPES,
            "state": attempt.state,
            "nonce": attempt.nonce,
            "code_challenge_method": "S256",
            "code_challenge": _b64url(hashlib.sha256(attempt.verifier.encode()).digest()),
        }
        if provider == "chatgpt":
            params["resource"] = "https://api.openai.com/v1"
            params["ext_agent_host_id"] = host_id
            if client_id == "dynamic_agent_client":
                params["agent_name_hint"] = "Operant"
        else:
            params["access_type"] = "offline"
            params["prompt"] = "consent"
            assert project_id is not None
            # A pending connection contains metadata only; credentials are private.
            if existing is None:
                self.repo.save_connection(
                    connection_id,
                    {
                        "connection_id": connection_id,
                        "name": "Gemini",
                        "provider": provider,
                        "auth_method": "oauth",
                        "status": "needs_auth",
                        "base_url": "https://generativelanguage.googleapis.com/v1beta",
                        "model_ids": [],
                        "profile_ids": [],
                        "project_id": project_id,
                        "client_id": client_id,
                    },
                )
        endpoint = _OPENAI if provider == "chatgpt" else _GOOGLE
        attempt.authorization_url = f"{endpoint['authorize']}?{urlencode(params)}"
        if registration:
            # One explicit continuation, with fresh PKCE/state/nonce. No account
            # is activated until its signed identity and grant are validated.
            registration.issued_client_id = None
        return attempt, attempt.authorization_url

    def status(self, attempt_id: str) -> dict[str, str | None]:
        attempt = self._attempt(attempt_id)
        self._expire(attempt)
        return {"attempt_id": attempt.id, "status": attempt.status, "error": attempt.error}

    def cancel(self, attempt_id: str) -> None:
        attempt = self._attempt(attempt_id)
        self._expire(attempt)
        if attempt.status == "pending":
            attempt.status = "cancelled"
            attempt.verifier = ""
            attempt.google_client_secret = None
            attempt.authorization_url = None
            if attempt.created_new and attempt.provider == "gemini":
                self.repo.delete_connection(attempt.connection_id)

    def _expire(self, attempt: Attempt) -> None:
        if attempt.status != "pending" or float(self.clock()) - attempt.started_at <= 600:
            return
        attempt.status = "expired"
        attempt.verifier = ""
        attempt.google_client_secret = None
        attempt.authorization_url = None
        if attempt.created_new and attempt.provider == "gemini":
            self.repo.delete_connection(attempt.connection_id)

    async def complete(
        self,
        *,
        provider: str,
        state: str,
        code: str | None,
        client_id: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        candidates = [
            item
            for item in self.attempts.values()
            if item.provider == provider and secrets.compare_digest(item.state, state)
        ]
        if len(candidates) != 1:
            raise OAuthError("OAuth state mismatch")
        attempt = candidates[0]
        self._expire(attempt)
        if attempt.status != "pending":
            raise OAuthError("OAuth attempt is no longer pending")
        attempt.status = "processing"
        stage = "callback_validation"
        try:
            if error:
                raise OAuthError("authorization was declined")
            if not code or len(code) > 8192:
                raise OAuthError("authorization code is missing")
            if provider == "chatgpt" and attempt.client_id == "dynamic_agent_client":
                if not client_id or client_id == "dynamic_agent_client":
                    raise OAuthError("issued ChatGPT client ID is missing")
                actual_client_id = client_id
            else:
                if client_id and client_id != attempt.client_id:
                    raise OAuthError("OAuth client ID mismatch")
                actual_client_id = attempt.client_id
            if provider == "chatgpt":
                attempt.issued_client_id = actual_client_id
            stage = "token_exchange"
            token = await self._exchange(attempt, code, actual_client_id)
            stage = "identity_validation"
            claims = await self._verify_id_token(
                token.get("id_token"), provider, actual_client_id, attempt.nonce
            )
            scopes = str(token.get("scope", "")).split()
            if provider == "gemini" and not _gemini_scopes_allowed(scopes):
                raise OAuthError("Gemini API permission was not granted")
            existing = self.repo.get_connection(attempt.connection_id)
            if existing and existing.get("subject") and existing["subject"] != claims["sub"]:
                raise OAuthError("OAuth account does not match this connection")
            access = token.get("access_token")
            refresh = token.get("refresh_token")
            id_token = token.get("id_token")
            if not all(isinstance(item, str) and item for item in (access, refresh, id_token)):
                raise OAuthError("OAuth token response is incomplete")
            expires_in = token.get("expires_in")
            if not isinstance(expires_in, (int, float)) or not 1 <= expires_in <= 86400:
                raise OAuthError("invalid OAuth token lifetime")
            secret_values = {
                secret_ref(attempt.connection_id, "ACCESS_TOKEN"): str(access),
                secret_ref(attempt.connection_id, "REFRESH_TOKEN"): str(refresh),
                secret_ref(attempt.connection_id, "ID_TOKEN"): str(id_token),
            }
            if provider == "gemini" and attempt.google_client_secret:
                secret_values[secret_ref(attempt.connection_id, "CLIENT_SECRET")] = (
                    attempt.google_client_secret
                )
            stage = "credential_storage"
            fd = await self._acquire_connection_lock(attempt.connection_id)
            try:
                existing = self.repo.get_connection(attempt.connection_id)
                if existing and existing.get("subject") and existing["subject"] != claims["sub"]:
                    raise OAuthError("OAuth account does not match this connection")
                old_values = {
                    reference: self.credentials.get(reference) for reference in secret_values
                }
                self.credentials.put_many(secret_values)
                record = {
                    **(existing or {}),
                    "connection_id": attempt.connection_id,
                    "name": "ChatGPT" if provider == "chatgpt" else "Gemini",
                    "provider": provider,
                    "auth_method": "oauth",
                    "status": (
                        "error"
                        if provider == "chatgpt" and "chatgpt.tokens.use.direct" not in scopes
                        else "ready"
                        if (existing or {}).get("profile_ids")
                        else "connected"
                    ),
                    "base_url": (
                        "https://api.openai.com/v1"
                        if provider == "chatgpt"
                        else "https://generativelanguage.googleapis.com/v1beta"
                    ),
                    "model_ids": (existing or {}).get("model_ids", []),
                    "profile_ids": (existing or {}).get("profile_ids", []),
                    "client_id": actual_client_id,
                    "subject": claims["sub"],
                    "account_label": claims.get("email") or None,
                    "secret_ref": secret_ref(attempt.connection_id, "ACCESS_TOKEN"),
                    "expires_at": float(self.clock()) + float(expires_in),
                    "scopes": scopes,
                    "error": (
                        "chatgpt_plan_usage_disabled"
                        if provider == "chatgpt" and "chatgpt.tokens.use.direct" not in scopes
                        else None
                    ),
                }
                try:
                    self.repo.save_connection(attempt.connection_id, record)
                except Exception as exc:
                    previous = {key: value for key, value in old_values.items() if value}
                    if previous:
                        self.credentials.put_many(previous)
                    absent = tuple(key for key, value in old_values.items() if value is None)
                    if absent:
                        self.credentials.delete_many(absent)
                    raise OAuthError("connection metadata could not be saved") from exc
            finally:
                self.credentials.release_lock(fd)
            attempt.status = "completed"
            return record
        except (OAuthError, CredentialError, httpx.HTTPError, ValueError, KeyError) as exc:
            attempt.status = "failed"
            attempt.error = (
                str(exc) if isinstance(exc, OAuthError) else _request_failure(stage, exc)
            )
            # No exception text, callback URL, code, token or account data.
            _LOG.warning(
                "OAuth failure: provider=%s stage=%s category=%s timeout_phase=%s",
                provider,
                stage,
                "validation_rejected"
                if isinstance(exc, OAuthError)
                else _request_failure(stage, exc),
                _timeout_phase(exc),
            )
            if attempt.created_new and provider == "gemini":
                self.repo.delete_connection(attempt.connection_id)
            raise OAuthError(attempt.error) from exc
        finally:
            attempt.verifier = ""
            attempt.google_client_secret = None
            attempt.authorization_url = None

    async def access_token(self, record: dict[str, Any]) -> str:
        connection_id = str(record["connection_id"])
        lock = self._refresh_locks.setdefault(connection_id, asyncio.Lock())
        async with lock:
            fd = await self._acquire_connection_lock(connection_id)
            try:
                current = self.repo.get_connection(connection_id) or record
                blocked = _reauthorization_blocker(current)
                if blocked is not None:
                    # A prior 401, revoked grant or uncertain revocation cannot
                    # become connected again merely because an old token remains.
                    raise OAuthError(blocked)
                try:
                    token = await self._access_token_unlocked(current)
                except (OAuthError, CredentialError, httpx.HTTPError, ValueError) as exc:
                    failure = (
                        str(exc) if isinstance(exc, OAuthError) else "OAuth credential unavailable"
                    )
                    latest = self.repo.get_connection(connection_id) or current
                    if not (
                        latest.get("status") == "needs_auth"
                        and latest.get("error") == "permission_denied"
                    ):
                        self.repo.save_connection(
                            connection_id, {**latest, "status": "error", "error": failure}
                        )
                    raise OAuthError(failure) from exc
                latest = self.repo.get_connection(connection_id) or current
                if latest.get("status") == "error" or latest.get("error"):
                    self.repo.save_connection(
                        connection_id,
                        {
                            **latest,
                            "status": "ready" if latest.get("profile_ids") else "connected",
                            "error": None,
                        },
                    )
                return token
            finally:
                self.credentials.release_lock(fd)

    async def _access_token_unlocked(self, record: dict[str, Any]) -> str:
        connection_id = str(record["connection_id"])
        blocked = _reauthorization_blocker(record)
        if blocked is not None:
            raise OAuthError(blocked)
        if record.get("provider") == "chatgpt" and "chatgpt.tokens.use.direct" not in record.get(
            "scopes", []
        ):
            raise OAuthError("chatgpt_plan_usage_disabled")
        if record.get("provider") == "gemini" and (
            record.get("error") == "permission_denied"
            or not _gemini_scopes_allowed(record.get("scopes"))
        ):
            raise OAuthError("permission_denied")
        ref = secret_ref(connection_id, "ACCESS_TOKEN")
        token = self.credentials.get(ref)
        if not token:
            raise OAuthError("OAuth connection needs sign-in")
        if float(record.get("expires_at") or 0) > float(self.clock()) + 90:
            return token
        refresh = self.credentials.get(secret_ref(connection_id, "REFRESH_TOKEN"))
        if not refresh:
            raise OAuthError("OAuth connection needs sign-in")
        endpoint = _OPENAI if record["provider"] == "chatgpt" else _GOOGLE
        form = {
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": str(record["client_id"]),
        }
        if record["provider"] == "chatgpt":
            form["resource"] = "https://api.openai.com/v1"
        else:
            client_secret = self.credentials.get(secret_ref(connection_id, "CLIENT_SECRET"))
            if not client_secret:
                raise OAuthError("Google desktop client secret is missing")
            form["client_secret"] = client_secret
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=20) as client:
                response = await client.post(endpoint["token"], data=form)
            if response.status_code != 200:
                raise OAuthError("OAuth token refresh failed")
            body = response.json()
            if not isinstance(body, dict):
                raise OAuthError("OAuth refresh response is invalid")
            access = body.get("access_token")
            replacement = body.get("refresh_token", refresh)
            expires_in = body.get("expires_in")
            if (
                not isinstance(access, str)
                or not access
                or not isinstance(replacement, str)
                or not replacement
                or not isinstance(expires_in, (int, float))
                or not 1 <= expires_in <= 86400
            ):
                raise OAuthError("OAuth refresh response is invalid")
            scopes = str(body["scope"]).split() if "scope" in body else record.get("scopes", [])
            if record["provider"] == "gemini" and not _gemini_scopes_allowed(scopes):
                self.repo.save_connection(
                    connection_id,
                    {
                        **record,
                        "status": "needs_auth",
                        "error": "permission_denied",
                        "scopes": scopes,
                    },
                )
                raise OAuthError("permission_denied")
            self.credentials.put_many(
                {
                    ref: access,
                    secret_ref(connection_id, "REFRESH_TOKEN"): replacement,
                }
            )
            plan_disabled = (
                record["provider"] == "chatgpt" and "chatgpt.tokens.use.direct" not in scopes
            )
            updated = {
                **record,
                "expires_at": float(self.clock()) + float(expires_in),
                "status": "error"
                if plan_disabled
                else "ready"
                if record.get("profile_ids")
                else "connected",
                "error": "chatgpt_plan_usage_disabled" if plan_disabled else None,
                "scopes": scopes,
            }
            self.repo.save_connection(connection_id, updated)
            if plan_disabled:
                raise OAuthError("chatgpt_plan_usage_disabled")
            return access
        except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
            raise OAuthError("OAuth token refresh failed") from exc

    async def disconnect(self, record: dict[str, Any]) -> None:
        connection_id = str(record["connection_id"])
        fd = await self._acquire_connection_lock(connection_id)
        try:
            current = self.repo.get_connection(connection_id) or record
            refresh = self.credentials.get(secret_ref(connection_id, "REFRESH_TOKEN"))
            if refresh and current.get("provider") in {"chatgpt", "gemini"}:
                endpoint = _OPENAI if current["provider"] == "chatgpt" else _GOOGLE
                form = {"token": refresh}
                if current["provider"] == "chatgpt":
                    form["token_type_hint"] = "refresh_token"
                    form["client_id"] = str(current["client_id"])
                try:
                    async with httpx.AsyncClient(transport=self.transport, timeout=20) as client:
                        response = await client.post(endpoint["revoke"], data=form)
                    if response.status_code not in {200, 204}:
                        raise OAuthError("OAuth revocation failed")
                except (httpx.HTTPError, OAuthError) as exc:
                    self.repo.save_connection(
                        connection_id,
                        {
                            **current,
                            "status": "needs_auth",
                            "error": "revocation_unconfirmed",
                        },
                    )
                    raise OAuthError("OAuth revocation failed") from exc
            self.credentials.delete_many(
                tuple(
                    secret_ref(connection_id, suffix)
                    for suffix in ("ACCESS_TOKEN", "REFRESH_TOKEN", "ID_TOKEN", "CLIENT_SECRET")
                )
            )
            self.repo.delete_connection(connection_id)
        finally:
            self.credentials.release_lock(fd)

    async def _exchange(self, attempt: Attempt, code: str, client_id: str) -> dict[str, Any]:
        endpoint = _OPENAI if attempt.provider == "chatgpt" else _GOOGLE
        form = {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "code_verifier": attempt.verifier,
            "redirect_uri": attempt.redirect_uri,
        }
        if attempt.provider == "chatgpt":
            form["resource"] = "https://api.openai.com/v1"
        else:
            client_secret = attempt.google_client_secret
            if not client_secret:
                raise OAuthError("Google desktop client secret is missing")
            form["client_secret"] = client_secret
        # Bound each network phase and never retry a one-use authorization code.
        async with httpx.AsyncClient(
            transport=self.transport, timeout=httpx.Timeout(60, connect=15, write=15, pool=15)
        ) as client:
            response = await client.post(endpoint["token"], data=form)
        if response.status_code != 200:
            raise OAuthError(_code_exchange_failure(response))
        if len(response.content) > 1_048_576:
            raise OAuthError("OAuth token response is too large")
        body = response.json()
        if not isinstance(body, dict):
            raise OAuthError("OAuth token response is invalid")
        return body

    async def _verify_id_token(
        self, token: Any, provider: str, client_id: str, nonce: str
    ) -> dict[str, Any]:
        if not isinstance(token, str) or len(token) > 65536:
            raise OAuthError("signed ID token is required")
        try:
            head, body, signature = token.split(".")
            header = json.loads(_decode(head))
            claims = json.loads(_decode(body))
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
                raise OAuthError("unsupported ID token signature")
            endpoint = _OPENAI if provider == "chatgpt" else _GOOGLE
            async with httpx.AsyncClient(transport=self.transport, timeout=20) as client:
                response = await client.get(endpoint["jwks"])
            if response.status_code != 200 or len(response.content) > 1048576:
                raise OAuthError("ID token signing keys are unavailable")
            jwks = response.json()
            keys = jwks.get("keys") if isinstance(jwks, dict) else None
            if not isinstance(keys, list):
                raise OAuthError("invalid ID token signing keys")
            matches = [
                item for item in keys if isinstance(item, dict) and item.get("kid") == header["kid"]
            ]
            if len(matches) != 1 or matches[0].get("kty") != "RSA":
                raise OAuthError("ID token signing key is missing")
            key = matches[0]
            exponent = int.from_bytes(_decode(key["e"]), "big")
            modulus = int.from_bytes(_decode(key["n"]), "big")
            if modulus.bit_length() < 2048 or exponent < 3 or exponent % 2 == 0:
                raise OAuthError("ID token signing key is invalid")
            rsa.RSAPublicNumbers(exponent, modulus).public_key().verify(
                _decode(signature),
                f"{head}.{body}".encode("ascii"),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
            now = float(self.clock())
            audience = claims.get("aud")
            valid_issuers = {endpoint["issuer"]}
            if provider == "gemini":
                valid_issuers.add("accounts.google.com")
            if (
                claims.get("iss") not in valid_issuers
                or not (
                    audience == client_id or isinstance(audience, list) and client_id in audience
                )
                or claims.get("azp", client_id) != client_id
                or claims.get("nonce") != nonce
                or not isinstance(claims.get("sub"), str)
                or not claims["sub"]
                or not isinstance(claims.get("exp"), (int, float))
                or claims["exp"] <= now - 60
                or not isinstance(claims.get("iat"), (int, float))
                or claims["iat"] > now + 60
            ):
                raise OAuthError("ID token claims are invalid")
            return cast(dict[str, Any], claims)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError, InvalidSignature) as exc:
            raise OAuthError("ID token validation failed") from exc

    def _attempt(self, attempt_id: str) -> Attempt:
        attempt = self.attempts.get(attempt_id)
        if attempt is None:
            raise OAuthError("OAuth attempt does not exist")
        return attempt
