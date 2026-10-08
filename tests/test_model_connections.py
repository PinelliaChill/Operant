from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from operant.api_model_connections import install_model_connection_routes
from operant.domain.messages import Message, MessageRole, ToolDefinition
from operant.domain.models import ModelProfile, RoleSnapshot
from operant.model_connections.credentials import CredentialError, CredentialStore
from operant.model_connections.oauth import OAuthConnections, OAuthError, secret_ref
from operant.persistence.onboarding import UXRepository
from operant.persistence.sqlite import SQLiteStore
from operant.providers.chatgpt_responses import ChatGPTResponsesProvider
from operant.providers.gemini_native import GeminiNativeProvider
from operant.providers.openai_compatible import ProviderError
from operant.providers.router import ConnectionProviderRouter


class _Repo:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.settings: dict[str, Any] = {}
        self.metadata: dict[str, dict[str, Any]] = {}
        self.commands: dict[str, tuple[str, dict[str, Any]]] = {}

    def get_connection(self, connection_id: str) -> dict[str, Any] | None:
        return self.records.get(connection_id)

    def list_connections(self) -> list[dict[str, Any]]:
        return list(self.records.values())

    def save_connection(self, connection_id: str, record: dict[str, Any]) -> None:
        self.records[connection_id] = record

    def delete_connection(self, connection_id: str) -> None:
        self.records.pop(connection_id)

    def get_setting(self, key: str, default: Any = None) -> Any:
        return self.settings.get(key, default)

    def set_setting(self, key: str, value: Any) -> None:
        self.settings[key] = value

    def get_provider_metadata(self, key: str) -> dict[str, Any] | None:
        return self.metadata.get(key)

    def save_provider_metadata(self, key: str, record: dict[str, Any]) -> None:
        self.metadata[key] = record

    def delete_provider_metadata(self, key: str) -> None:
        self.metadata.pop(key, None)

    def get_command(self, key: str, fingerprint: str) -> dict[str, Any] | None:
        command = self.commands.get(key)
        if command is None:
            return None
        if command[0] != fingerprint:
            from operant.persistence.sqlite import ConflictError

            raise ConflictError("conflict")
        return {
            field: value
            for field, value in command[1].items()
            if field != "_credential_fingerprint_version"
        }

    def get_command_fingerprint(self, key: str) -> str | None:
        command = self.commands.get(key)
        return None if command is None else command[0]

    def get_command_fingerprint_version(self, key: str) -> str | None:
        command = self.commands.get(key)
        return None if command is None else command[1].get("_credential_fingerprint_version")

    def save_command(self, key: str, fingerprint: str, result: dict[str, Any]) -> None:
        self.commands[key] = (fingerprint, result)


def _snapshot(provider: str, model_id: str, secret: str = "TOKEN") -> RoleSnapshot:
    return cast(
        RoleSnapshot,
        SimpleNamespace(
            provider=provider,
            model_id=model_id,
            model_profile_id=f"profile-{model_id}",
            secret_ref=secret,
            budget=SimpleNamespace(max_output_tokens=100),
            temperature=0.8,
        ),
    )


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _jwt(
    private: rsa.RSAPrivateKey,
    nonce: str,
    subject: str = "account-1",
    *,
    issuer: str = "https://auth.openai.com",
    audience: str = "oaiapp_issued",
) -> str:
    header = _b64(json.dumps({"alg": "RS256", "kid": "key-1"}).encode())
    body = _b64(
        json.dumps(
            {
                "iss": issuer,
                "aud": audience,
                "sub": subject,
                "email": "user@example.test",
                "nonce": nonce,
                "iat": int(time.time()),
                "exp": int(time.time()) + 3600,
            }
        ).encode()
    )
    data = f"{header}.{body}".encode()
    signature = private.sign(data, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{body}.{_b64(signature)}"


def test_credential_store_preserves_env_and_rejects_unsafe_file(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("UNCHANGED=old-value", encoding="utf-8")
    os.chmod(path, 0o600)
    store = CredentialStore(path)
    ref = "OPERANT_CONNECTION_ABC_API_KEY"
    try:
        store.put(ref, "secret-value")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert path.read_text().startswith("UNCHANGED=old-value\n")
        assert store.get(ref) == "secret-value"
        store.delete(ref)
        assert path.read_text() == "UNCHANGED=old-value\n"
        path.unlink()
        path.symlink_to(tmp_path / "target")
        with pytest.raises(CredentialError):
            store.put(ref, "new")
    finally:
        os.environ.pop(ref, None)


def test_credential_store_serializes_concurrent_connections(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    references = [f"OPERANT_CONNECTION_PARALLEL_{index}_API_KEY" for index in range(24)]

    def write(item: tuple[int, str]) -> None:
        index, reference = item
        CredentialStore(path).put(reference, f"value-{index}")

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, enumerate(references)))
        reader = CredentialStore(path)
        assert [reader.get(reference) for reference in references] == [
            f"value-{index}" for index in range(24)
        ]
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    finally:
        for reference in references:
            os.environ.pop(reference, None)


@pytest.mark.asyncio
async def test_chatgpt_dynamic_oauth_validates_account_and_rotates_tokens(tmp_path: Path) -> None:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private.public_key().public_numbers()
    repo = _Repo()
    forms: list[dict[str, list[str]]] = []
    token_id: list[str] = [""]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("jwks.json"):
            return httpx.Response(
                200,
                json={
                    "keys": [
                        {
                            "kid": "key-1",
                            "kty": "RSA",
                            "alg": "RS256",
                            "n": _b64(public.n.to_bytes(256, "big")),
                            "e": _b64(public.e.to_bytes(3, "big")),
                        }
                    ]
                },
            )
        form = parse_qs(request.content.decode())
        forms.append(form)
        if request.url.path.endswith("/revoke"):
            return httpx.Response(200)
        if form.get("grant_type") == ["refresh_token"]:
            return httpx.Response(
                200,
                json={
                    "access_token": "access-2",
                    "refresh_token": "refresh-2",
                    "expires_in": 3600,
                },
            )
        return httpx.Response(
            200,
            json={
                "access_token": "access-1",
                "refresh_token": "refresh-1",
                "id_token": token_id[0],
                "expires_in": 3600,
                "scope": "openid offline_access resource.invoke chatgpt.tokens.use.direct",
            },
        )

    credentials = CredentialStore(tmp_path / ".env")
    oauth = OAuthConnections(repo, credentials, transport=httpx.MockTransport(handler))
    attempt, url = oauth.start(
        provider="chatgpt",
        redirect_uri="http://127.0.0.1:4343/internal/model-auth/chatgpt/callback",
    )
    params = parse_qs(urlsplit(url).query)
    assert params["client_id"] == ["dynamic_agent_client"]
    assert params["agent_name_hint"] == ["Operant"]
    assert "id_token_hint" not in params
    with pytest.raises(OAuthError, match="state mismatch"):
        await oauth.complete(provider="chatgpt", state="wrong", code="code")
    token_id[0] = _jwt(private, attempt.nonce)
    record = await oauth.complete(
        provider="chatgpt", state=attempt.state, code="code", client_id="oaiapp_issued"
    )
    assert record["subject"] == "account-1"
    assert record["client_id"] == "oaiapp_issued"
    assert "access-1" not in json.dumps(record)
    assert forms[0]["client_id"] == ["oaiapp_issued"]
    repo.save_connection(attempt.connection_id, {**record, "expires_at": 0})
    assert await oauth.access_token(record) == "access-2"
    assert credentials.get(secret_ref(attempt.connection_id, "REFRESH_TOKEN")) == "refresh-2"
    await oauth.disconnect(record)
    assert forms[-1]["token_type_hint"] == ["refresh_token"]
    assert repo.get_connection(attempt.connection_id) is None


@pytest.mark.asyncio
async def test_chatgpt_login_without_plan_scope_keeps_identity_but_blocks_models(
    tmp_path: Path,
) -> None:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private.public_key().public_numbers()
    repo = _Repo()
    token_id = [""]
    scope = ["openid offline_access"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("jwks.json"):
            return httpx.Response(
                200,
                json={
                    "keys": [
                        {
                            "kid": "key-1",
                            "kty": "RSA",
                            "alg": "RS256",
                            "n": _b64(public.n.to_bytes(256, "big")),
                            "e": _b64(public.e.to_bytes(3, "big")),
                        }
                    ]
                },
            )
        form = parse_qs(request.content.decode())
        if form.get("grant_type") == ["refresh_token"]:
            return httpx.Response(
                200,
                json={
                    "access_token": "rotated-access",
                    "refresh_token": "rotated-refresh",
                    "expires_in": 3600,
                    "scope": scope[0],
                },
            )
        return httpx.Response(
            200,
            json={
                "access_token": "signed-in-access",
                "refresh_token": "signed-in-refresh",
                "id_token": token_id[0],
                "expires_in": 3600,
                "scope": scope[0],
            },
        )

    credentials = CredentialStore(tmp_path / ".env")
    oauth = OAuthConnections(repo, credentials, transport=httpx.MockTransport(handler))
    callback = "http://127.0.0.1:4343/internal/model-auth/chatgpt/callback"
    attempt, _ = oauth.start(provider="chatgpt", redirect_uri=callback)
    token_id[0] = _jwt(private, attempt.nonce)
    record = await oauth.complete(
        provider="chatgpt", state=attempt.state, code="code", client_id="oaiapp_issued"
    )
    assert record["subject"] == "account-1"
    assert record["status"] == "error"
    assert record["error"] == "chatgpt_plan_usage_disabled"
    assert credentials.get(secret_ref(attempt.connection_id, "ACCESS_TOKEN")) == "signed-in-access"
    router = ConnectionProviderRouter(repo, credentials, oauth)
    router.chatgpt.transport = httpx.MockTransport(
        lambda _: pytest.fail("disabled ChatGPT plan must not make a model request")
    )
    with pytest.raises(ProviderError, match="not enabled"):
        await router.list_models(base_url="", secret_ref=record["secret_ref"])

    scope[0] = "openid offline_access chatgpt.tokens.use.direct"
    renewed, _ = oauth.start(
        provider="chatgpt", connection_id=attempt.connection_id, redirect_uri=callback
    )
    token_id[0] = _jwt(private, renewed.nonce)
    active = await oauth.complete(
        provider="chatgpt", state=renewed.state, code="code", client_id="oaiapp_issued"
    )
    assert active["status"] == "connected"
    assert active["subject"] == record["subject"]
    repo.save_connection(attempt.connection_id, {**active, "expires_at": 0})
    scope[0] = "openid offline_access"
    with pytest.raises(OAuthError, match="chatgpt_plan_usage_disabled"):
        await oauth.access_token(repo.get_connection(attempt.connection_id) or {})
    disabled = repo.get_connection(attempt.connection_id)
    assert disabled is not None
    assert disabled["subject"] == "account-1"
    assert disabled["status"] == "error"
    assert disabled["error"] == "chatgpt_plan_usage_disabled"
    assert credentials.get(secret_ref(attempt.connection_id, "REFRESH_TOKEN")) == "rotated-refresh"
    with pytest.raises(ProviderError, match="not enabled"):
        await router.list_models(base_url="", secret_ref=record["secret_ref"])


@pytest.mark.asyncio
async def test_google_desktop_oauth_requires_project_and_scopes(tmp_path: Path) -> None:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private.public_key().public_numbers()
    repo = _Repo()
    token_id: list[str] = [""]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/certs"):
            return httpx.Response(
                200,
                json={
                    "keys": [
                        {
                            "kid": "key-1",
                            "kty": "RSA",
                            "alg": "RS256",
                            "n": _b64(public.n.to_bytes(256, "big")),
                            "e": _b64(public.e.to_bytes(3, "big")),
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "access_token": "google-access",
                "refresh_token": "google-refresh",
                "id_token": token_id[0],
                "expires_in": 3600,
                "scope": (
                    "openid https://www.googleapis.com/auth/cloud-platform "
                    "https://www.googleapis.com/auth/generative-language.retriever"
                ),
            },
        )

    credentials = CredentialStore(tmp_path / ".env")
    oauth = OAuthConnections(repo, credentials, transport=httpx.MockTransport(handler))
    with pytest.raises(OAuthError, match="Cloud project"):
        oauth.start(
            provider="gemini",
            redirect_uri="http://127.0.0.1:4343/internal/model-auth/gemini/callback",
            google_client_id="desktop-client",
            google_client_secret="client-secret",
        )
    attempt, url = oauth.start(
        provider="gemini",
        redirect_uri="http://127.0.0.1:4343/internal/model-auth/gemini/callback",
        google_client_id="desktop-client",
        google_client_secret="client-secret",
        project_id="own-project",
    )
    params = parse_qs(urlsplit(url).query)
    assert params["client_id"] == ["desktop-client"]
    assert params["code_challenge_method"] == ["S256"]
    assert "client-secret" not in url
    token_id[0] = _jwt(
        private,
        attempt.nonce,
        issuer="https://accounts.google.com",
        audience="desktop-client",
    )
    record = await oauth.complete(provider="gemini", state=attempt.state, code="code")
    assert record["project_id"] == "own-project"
    assert record["status"] == "connected"
    assert "google-access" not in json.dumps(record)
    old_token = credentials.get(secret_ref(attempt.connection_id, "ACCESS_TOKEN"))
    reauth, _ = oauth.start(
        provider="gemini",
        connection_id=attempt.connection_id,
        redirect_uri="http://127.0.0.1:4343/internal/model-auth/gemini/callback",
    )
    assert repo.get_connection(attempt.connection_id) == record
    token_id[0] = _jwt(
        private,
        reauth.nonce,
        subject="different-account",
        issuer="https://accounts.google.com",
        audience="desktop-client",
    )
    with pytest.raises(OAuthError, match="does not match"):
        await oauth.complete(provider="gemini", state=reauth.state, code="code-2")
    assert repo.get_connection(attempt.connection_id) == record
    assert credentials.get(secret_ref(attempt.connection_id, "ACCESS_TOKEN")) == old_token
    await oauth.disconnect(record)


def test_oauth_cancel_and_expiry_clear_pending_google_attempt(tmp_path: Path) -> None:
    repo = _Repo()
    moment = [1000.0]
    credentials = CredentialStore(tmp_path / ".env")
    oauth = OAuthConnections(repo, credentials, clock=lambda: moment[0])
    callback = "http://127.0.0.1:4343/internal/model-auth/gemini/callback"
    attempt, url = oauth.start(
        provider="gemini",
        redirect_uri=callback,
        google_client_id="desktop-client",
        google_client_secret="client-secret",
        project_id="cloud-project",
    )
    assert "client-secret" not in url
    assert not (tmp_path / ".env").exists()
    assert repo.get_connection(attempt.connection_id) is not None
    oauth.cancel(attempt.id)
    assert oauth.status(attempt.id)["status"] == "cancelled"
    assert attempt.google_client_secret is None
    assert attempt.authorization_url is None
    assert repo.get_connection(attempt.connection_id) is None

    expired, _ = oauth.start(
        provider="gemini",
        redirect_uri=callback,
        google_client_id="desktop-client",
        google_client_secret="client-secret",
        project_id="cloud-project",
    )
    moment[0] += 601
    assert oauth.status(expired.id)["status"] == "expired"
    assert expired.google_client_secret is None
    assert expired.authorization_url is None
    assert repo.get_connection(expired.connection_id) is None


def test_gemini_oauth_uses_own_private_operator_configuration(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        'OPERANT_GEMINI_OAUTH_CLIENT_ID="own-desktop-client"\n'
        'OPERANT_GEMINI_OAUTH_CLIENT_SECRET="own-client-secret"\n',
        encoding="utf-8",
    )
    os.chmod(env_path, 0o600)
    repo = _Repo()
    oauth = OAuthConnections(repo, CredentialStore(env_path))
    callback = "http://127.0.0.1:4343/internal/model-auth/gemini/callback"
    attempt, url = oauth.start(provider="gemini", redirect_uri=callback, project_id="own-project")
    assert parse_qs(urlsplit(url).query)["client_id"] == ["own-desktop-client"]
    assert "own-client-secret" not in url
    assert "own-client-secret" not in json.dumps(repo.list_connections())
    oauth.cancel(attempt.id)
    assert env_path.read_text(encoding="utf-8").startswith(
        'OPERANT_GEMINI_OAUTH_CLIENT_ID="own-desktop-client"'
    )
    explicit, url = oauth.start(
        provider="gemini",
        redirect_uri=callback,
        project_id="own-project",
        google_client_id="explicit-desktop-client",
        google_client_secret="explicit-secret",
    )
    assert parse_qs(urlsplit(url).query)["client_id"] == ["explicit-desktop-client"]
    oauth.cancel(explicit.id)


@pytest.mark.asyncio
async def test_chatgpt_response_stream_uses_supported_fields_and_maps_tools() -> None:
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        captured.append(payload)
        events = [
            {"type": "response.output_text.delta", "delta": "Hello"},
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "namespace": "operant",
                    "call_id": "call-1",
                    "name": "read_file",
                    "arguments": "{}",
                },
            },
            {
                "type": "response.completed",
                "response": {
                    "id": "resp-1",
                    "usage": {"input_tokens": 3, "output_tokens": 4},
                },
            },
        ]
        return httpx.Response(200, text="".join(f"data: {json.dumps(item)}\n\n" for item in events))

    async def token(_: str) -> str:
        return "access"

    provider = ChatGPTResponsesProvider(token, transport=httpx.MockTransport(handler))
    items = [
        item
        async for item in provider.stream(
            snapshot=_snapshot("chatgpt", "model-1"),
            messages=[
                Message(role=MessageRole.SYSTEM, content="system"),
                Message(role=MessageRole.USER, content="hello"),
            ],
            tools=[
                ToolDefinition(name="read_file", description="Read", parameters={"type": "object"})
            ],
        )
    ]
    assert captured[0]["store"] is False and captured[0]["stream"] is True
    assert captured[0]["model"] == "model-1"
    assert "temperature" not in captured[0] and "max_output_tokens" not in captured[0]
    assert captured[0]["instructions"] == "system"
    assert captured[0]["tools"][0]["type"] == "namespace"
    assert [item.event_type for item in items] == ["model.delta", "model.completed"]
    assert items[-1].response is not None
    assert items[-1].response.tool_calls[0].name == "read_file"
    assert items[-1].response.usage is not None
    assert items[-1].response.usage.total_tokens == 7


@pytest.mark.asyncio
async def test_chatgpt_http_errors_and_streamed_quota_update_connection_without_completion(
    tmp_path: Path,
) -> None:
    repo = _Repo()
    connection_id = "chatgpt-connection"
    access_ref = secret_ref(connection_id, "ACCESS_TOKEN")
    credentials = CredentialStore(tmp_path / ".env")
    credentials.put(access_ref, "fixture-access")
    repo.save_connection(
        connection_id,
        {
            "connection_id": connection_id,
            "provider": "chatgpt",
            "auth_method": "oauth",
            "secret_ref": access_ref,
            "status": "ready",
            "profile_ids": ["profile-model-1"],
            "scopes": ["chatgpt.tokens.use.direct"],
            "expires_at": time.time() + 3600,
        },
    )
    router = ConnectionProviderRouter(repo, credentials, OAuthConnections(repo, credentials))
    try:
        router.chatgpt.transport = httpx.MockTransport(
            lambda _: httpx.Response(401, json={"error": "private-error-body"})
        )
        with pytest.raises(ProviderError, match="HTTP 401"):
            await router.list_models(base_url="", secret_ref=access_ref)
        failed = repo.get_connection(connection_id)
        assert failed is not None and failed["status"] == "needs_auth"
        assert failed["error"] == "authentication_required"
        assert "private-error-body" not in json.dumps(failed)

        original = [Message(role=MessageRole.USER, content="Keep this message")]
        cases = (
            (
                403,
                {"error": {"code": "subscription_sharing_user_not_eligible", "message": "private"}},
                "user_not_eligible",
            ),
            (403, {"detail": "private region or policy detail"}, "permission_denied"),
            (
                429,
                {
                    "error": {
                        "code": "subscription_sharing_usage_limit_exceeded",
                        "message": "private",
                    }
                },
                "usage_limit",
            ),
            (429, {"detail": "private throttle detail"}, "rate_limited"),
            (
                503,
                {"error": {"code": "subscription_sharing_usage_unavailable", "message": "private"}},
                "usage_unavailable",
            ),
            (503, {"detail": "private routing detail"}, "provider_unavailable"),
        )
        for http_status, error_body, expected_error in cases:
            router.chatgpt.transport = httpx.MockTransport(
                lambda _, status=http_status, body=error_body: httpx.Response(status, json=body)
            )
            with pytest.raises(ProviderError, match=f"HTTP {http_status}"):
                _ = [
                    event
                    async for event in router.stream(
                        snapshot=_snapshot("chatgpt", "model-1", access_ref),
                        messages=original,
                        tools=[],
                    )
                ]
            state = repo.get_connection(connection_id)
            assert state is not None and state["status"] == "error"
            assert state["error"] == expected_error
            assert "private" not in json.dumps(state)
            assert credentials.get(access_ref) == "fixture-access"

        for code, expected_error in (
            ("subscription_sharing_user_not_eligible", "user_not_eligible"),
            ("subscription_sharing_usage_unavailable", "usage_unavailable"),
        ):
            failed_event = {
                "type": "response.failed",
                "response": {"error": {"code": code, "message": "private streamed detail"}},
            }
            router.chatgpt.transport = httpx.MockTransport(
                lambda _, event=failed_event: httpx.Response(
                    200, text=f"data: {json.dumps(event)}\n\n"
                )
            )
            with pytest.raises(ProviderError, match="response.failed"):
                _ = [
                    event
                    async for event in router.stream(
                        snapshot=_snapshot("chatgpt", "model-1", access_ref),
                        messages=original,
                        tools=[],
                    )
                ]
            state = repo.get_connection(connection_id)
            assert state is not None and state["status"] == "error"
            assert state["error"] == expected_error
            assert "private streamed detail" not in json.dumps(state)

        quota = {
            "type": "response.failed",
            "response": {
                "error": {
                    "code": "subscription_sharing_usage_limit_exceeded",
                    "message": "private quota details",
                }
            },
        }
        router.chatgpt.transport = httpx.MockTransport(
            lambda _: httpx.Response(200, text=f"data: {json.dumps(quota)}\n\n")
        )
        events = []
        with pytest.raises(ProviderError, match="response.failed"):
            async for event in router.stream(
                snapshot=_snapshot("chatgpt", "model-1", access_ref),
                messages=original,
                tools=[],
            ):
                events.append(event)
        assert events == []
        assert original[0].content == "Keep this message"
        failed = repo.get_connection(connection_id)
        assert failed is not None and failed["status"] == "error"
        assert failed["error"] == "usage_limit"
        assert "private quota details" not in json.dumps(failed)
        completed = {
            "type": "response.completed",
            "response": {"id": "resp-recovered", "status": "completed"},
        }
        router.chatgpt.transport = httpx.MockTransport(
            lambda _: httpx.Response(200, text=f"data: {json.dumps(completed)}\n\n")
        )
        recovered = [
            event
            async for event in router.stream(
                snapshot=_snapshot("chatgpt", "model-1", access_ref),
                messages=original,
                tools=[],
            )
        ]
        assert recovered[-1].event_type == "model.completed"
        connection = repo.get_connection(connection_id)
        assert connection is not None and connection["status"] == "ready"
        assert connection["error"] is None
    finally:
        credentials.delete(access_ref)


@pytest.mark.asyncio
async def test_chatgpt_stream_cancellation_does_not_complete_or_change_connection() -> None:
    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n'
            await asyncio.sleep(60)

    async def token(_: str) -> str:
        return "fixture-access"

    provider = ChatGPTResponsesProvider(
        token,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=SlowStream())),
    )
    stream = provider.stream(
        snapshot=_snapshot("chatgpt", "model-1"),
        messages=[Message(role=MessageRole.USER, content="preserved")],
        tools=[],
    )
    first = await anext(stream)
    assert first.event_type == "model.delta" and first.delta == "partial"
    task = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await stream.aclose()


@pytest.mark.asyncio
async def test_chatgpt_encrypted_reasoning_replays_after_restart_without_public_leak() -> None:
    payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        events = [
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "reasoning",
                    "id": "rs-1",
                    "encrypted_content": "opaque-reasoning",
                },
            },
            {"type": "response.output_text.delta", "delta": "answer"},
            {"type": "response.completed", "response": {"id": "resp-1"}},
        ]
        return httpx.Response(200, text="".join(f"data: {json.dumps(item)}\n\n" for item in events))

    async def token(_: str) -> str:
        return "access"

    repo = _Repo()
    repo.save_connection("connection", {"connection_id": "connection", "secret_ref": "TOKEN"})

    def resolve(_: str) -> dict[str, Any] | None:
        return repo.get_connection("connection")

    first = ChatGPTResponsesProvider(
        token,
        transport=httpx.MockTransport(handler),
        metadata_repo=repo,
        connection_for_ref=resolve,
    )
    user = Message(role=MessageRole.USER, content="question")
    result = [
        event
        async for event in first.stream(
            snapshot=_snapshot("chatgpt", "model-1"),
            messages=[user],
            tools=[],
        )
    ]
    assert result[-1].response is not None
    assert "opaque-reasoning" not in result[-1].model_dump_json()
    restarted = ChatGPTResponsesProvider(
        token,
        transport=httpx.MockTransport(handler),
        metadata_repo=repo,
        connection_for_ref=resolve,
    )
    _ = [
        event
        async for event in restarted.stream(
            snapshot=_snapshot("chatgpt", "model-1"),
            messages=[
                user,
                Message(role=MessageRole.ASSISTANT, content="answer"),
                Message(role=MessageRole.USER, content="next"),
            ],
            tools=[],
        )
    ]
    assert payloads[1]["input"][1] == {
        "type": "reasoning",
        "id": "rs-1",
        "encrypted_content": "opaque-reasoning",
    }


@pytest.mark.asyncio
async def test_gemini_stream_preserves_function_id_and_signature() -> None:
    payloads: list[dict[str, Any]] = []
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        payloads.append(json.loads(request.content))
        if len(payloads) == 1:
            chunk = {
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "parts": [
                                {
                                    "functionCall": {
                                        "id": "fc-1",
                                        "name": "read_file",
                                        "args": {"path": "a"},
                                    },
                                    "thoughtSignature": "opaque-signature",
                                }
                            ]
                        },
                    }
                ]
            }
        else:
            chunk = {
                "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "done"}]}}]
            }
        chunk["usageMetadata"] = {
            "promptTokenCount": 5,
            "candidatesTokenCount": 3,
            "totalTokenCount": 8,
        }
        return httpx.Response(200, text=f"data: {json.dumps(chunk)}\n\n")

    async def auth(_: str) -> dict[str, str]:
        return {"Authorization": "Bearer access", "x-goog-user-project": "project"}

    repo = _Repo()
    repo.save_connection("connection", {"connection_id": "connection", "secret_ref": "TOKEN"})

    def resolve(_: str) -> dict[str, Any] | None:
        return repo.get_connection("connection")

    provider = GeminiNativeProvider(
        auth,
        transport=httpx.MockTransport(handler),
        metadata_repo=repo,
        connection_for_ref=resolve,
    )
    user = Message(role=MessageRole.USER, content="read")
    tools = [ToolDefinition(name="read_file", description="Read", parameters={"type": "object"})]
    first = [
        item
        async for item in provider.stream(
            snapshot=_snapshot("gemini", "gemini-3-model"),
            messages=[user],
            tools=tools,
        )
    ]
    assert first[-1].response is not None
    assert requested_paths[0].endswith("/models/gemini-3-model:streamGenerateContent")
    call = first[-1].response.tool_calls[0]
    assert call.id == "fc-1"
    second = [
        item
        async for item in provider.stream(
            snapshot=_snapshot("gemini", "gemini-3-model"),
            messages=[
                user,
                Message(role=MessageRole.ASSISTANT, tool_calls=(call,)),
                Message(role=MessageRole.TOOL, name="read_file", tool_call_id="fc-1", content="ok"),
            ],
            tools=tools,
        )
    ]
    assert second[-1].response is not None and second[-1].response.content == "done"
    assert second[-1].response.usage is not None
    assert second[-1].response.usage.total_tokens == 8
    assert payloads[1]["contents"][1]["parts"][0]["thoughtSignature"] == "opaque-signature"
    assert payloads[1]["contents"][2]["parts"][0]["functionResponse"]["id"] == "fc-1"
    restarted = GeminiNativeProvider(
        auth,
        transport=httpx.MockTransport(handler),
        metadata_repo=repo,
        connection_for_ref=resolve,
    )
    _ = [
        item
        async for item in restarted.stream(
            snapshot=_snapshot("gemini", "gemini-3-model"),
            messages=[user, Message(role=MessageRole.ASSISTANT, tool_calls=(call,))],
            tools=tools,
        )
    ]
    assert payloads[2]["contents"][1]["parts"][0]["thoughtSignature"] == "opaque-signature"


@pytest.mark.asyncio
async def test_gemini_429_and_partial_stream_do_not_commit_tool_context(tmp_path: Path) -> None:
    repo = _Repo()
    connection_id = "gemini-connection"
    key_ref = secret_ref(connection_id, "API_KEY")
    credentials = CredentialStore(tmp_path / ".env")
    credentials.put(key_ref, "fixture-key")
    repo.save_connection(
        connection_id,
        {
            "connection_id": connection_id,
            "provider": "gemini",
            "auth_method": "api_key",
            "secret_ref": key_ref,
            "status": "ready",
            "profile_ids": ["profile-gemini-3-model"],
        },
    )
    router = ConnectionProviderRouter(repo, credentials, OAuthConnections(repo, credentials))
    snapshot = _snapshot("gemini", "gemini-3-model", key_ref)
    user = Message(role=MessageRole.USER, content="preserved request")
    try:
        router.gemini.transport = httpx.MockTransport(
            lambda _: httpx.Response(429, json={"message": "private quota details"})
        )
        with pytest.raises(ProviderError, match="HTTP 429"):
            _ = [
                event
                async for event in router.stream(
                    snapshot=snapshot,
                    messages=[user],
                    tools=[],
                )
            ]
        limited = repo.get_connection(connection_id)
        assert limited is not None and limited["status"] == "error"
        assert limited["error"] == "rate_limited"
        assert "private quota details" not in json.dumps(limited)

        partial = [
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": "partial",
                                    "functionCall": {
                                        "id": "incomplete-call",
                                        "name": "read_file",
                                        "args": {},
                                    },
                                    "thoughtSignature": "uncommitted-signature",
                                }
                            ]
                        }
                    }
                ]
            },
            {"candidates": [{"finishReason": "MAX_TOKENS"}]},
        ]
        router.gemini.transport = httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                text="".join(f"data: {json.dumps(chunk)}\n\n" for chunk in partial),
            )
        )
        events = []
        with pytest.raises(ProviderError, match="did not complete"):
            async for event in router.stream(snapshot=snapshot, messages=[user], tools=[]):
                events.append(event)
        assert [event.event_type for event in events] == ["model.delta"]
        assert user.content == "preserved request"
        assert repo.metadata == {}
    finally:
        credentials.delete(key_ref)


@pytest.mark.asyncio
async def test_gemini_text_signature_survives_restart_for_same_public_prefix() -> None:
    payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        chunk = {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {
                        "parts": [{"text": "answer", "thoughtSignature": "opaque-text-signature"}]
                    },
                }
            ]
        }
        return httpx.Response(200, text=f"data: {json.dumps(chunk)}\n\n")

    async def auth(_: str) -> dict[str, str]:
        return {"Authorization": "Bearer access"}

    repo = _Repo()
    repo.save_connection("connection", {"connection_id": "connection", "secret_ref": "TOKEN"})

    def resolve(_: str) -> dict[str, Any] | None:
        return repo.get_connection("connection")

    first = GeminiNativeProvider(
        auth,
        transport=httpx.MockTransport(handler),
        metadata_repo=repo,
        connection_for_ref=resolve,
    )
    user = Message(role=MessageRole.USER, content="question")
    _ = [
        event
        async for event in first.stream(
            snapshot=_snapshot("gemini", "gemini-3-model"),
            messages=[user],
            tools=[],
        )
    ]
    assert all("answer" not in json.dumps(record) for record in repo.metadata.values())
    restarted = GeminiNativeProvider(
        auth,
        transport=httpx.MockTransport(handler),
        metadata_repo=repo,
        connection_for_ref=resolve,
    )
    _ = [
        event
        async for event in restarted.stream(
            snapshot=_snapshot("gemini", "gemini-3-model"),
            messages=[
                user,
                Message(role=MessageRole.ASSISTANT, content="answer"),
                Message(role=MessageRole.USER, content="next"),
            ],
            tools=[],
        )
    ]
    assert payloads[1]["contents"][1]["parts"][0]["thoughtSignature"] == ("opaque-text-signature")


@pytest.mark.asyncio
async def test_gemini_discovery_reads_all_pages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("pageToken") == "next":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "models/gemini-2",
                            "supportedGenerationMethods": ["generateContent"],
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "models": [
                    {"name": "models/gemini-1", "supportedGenerationMethods": ["generateContent"]}
                ],
                "nextPageToken": "next",
            },
        )

    async def auth(_: str) -> dict[str, str]:
        return {"Authorization": "Bearer access", "x-goog-user-project": "project"}

    provider = GeminiNativeProvider(auth, transport=httpx.MockTransport(handler))
    assert await provider.list_models(base_url="", secret_ref="TOKEN") == ["gemini-1", "gemini-2"]


@pytest.mark.asyncio
async def test_native_model_catalog_recovers_connection_and_keeps_safe_display_names(
    tmp_path: Path,
) -> None:
    repo = _Repo()
    credentials = CredentialStore(tmp_path / ".env")
    chat_ref = secret_ref("chatgpt-catalog", "ACCESS_TOKEN")
    gemini_ref = secret_ref("gemini-catalog", "API_KEY")
    credentials.put_many({chat_ref: "chat-access", gemini_ref: "gemini-key"})
    repo.save_connection(
        "chatgpt-catalog",
        {
            "connection_id": "chatgpt-catalog",
            "provider": "chatgpt",
            "auth_method": "oauth",
            "secret_ref": chat_ref,
            "status": "error",
            "error": "usage_unavailable",
            "profile_ids": [],
            "scopes": ["chatgpt.tokens.use.direct"],
            "expires_at": time.time() + 3600,
        },
    )
    repo.save_connection(
        "gemini-catalog",
        {
            "connection_id": "gemini-catalog",
            "provider": "gemini",
            "auth_method": "api_key",
            "secret_ref": gemini_ref,
            "status": "error",
            "error": "permission_denied",
            "profile_ids": [],
        },
    )
    router = ConnectionProviderRouter(repo, credentials, OAuthConnections(repo, credentials))
    router.chatgpt.transport = httpx.MockTransport(lambda _: httpx.Response(503, json={}))
    with pytest.raises(ProviderError, match="HTTP 503"):
        await router.list_models(base_url="", secret_ref=chat_ref)
    failed_chat = repo.get_connection("chatgpt-catalog")
    assert failed_chat is not None and failed_chat["error"] == "provider_unavailable"
    chat_models = [
        {"slug": "model-b", "display_name": "  Beta\u202e\n Name  ", "visibility": "list"},
        {"slug": "model-a", "display_name": 42, "visibility": "list"},
        {"slug": "hidden-model", "display_name": "Hidden", "visibility": "hide"},
        {"slug": "bad\nslug", "display_name": "Bad", "visibility": "list"},
        {"slug": "model-b", "display_name": "Duplicate", "visibility": "list"},
    ]
    router.chatgpt.transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"models": chat_models, "private": "do-not-store"})
    )
    assert await router.list_models(base_url="", secret_ref=chat_ref) == ["model-b", "model-a"]
    chat_record = repo.get_connection("chatgpt-catalog")
    assert chat_record is not None
    assert chat_record["status"] == "connected" and chat_record["error"] is None
    assert chat_record["model_names"] == {"model-b": "Beta Name", "model-a": "model-a"}
    assert "do-not-store" not in json.dumps(chat_record)
    assert credentials.get(chat_ref) == "chat-access"

    def gemini_handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("pageToken") == "next":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "models/gemini-b",
                            "displayName": "  Gemini B\x00  ",
                            "supportedGenerationMethods": ["generateContent"],
                        },
                        {
                            "name": "models/gemini-a",
                            "displayName": "Duplicate",
                            "supportedGenerationMethods": ["generateContent"],
                        },
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": "models/gemini-a",
                        "displayName": "Gemini A",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                    {
                        "name": "models/embedding-only",
                        "displayName": "Embedding",
                        "supportedGenerationMethods": ["embedContent"],
                    },
                    {
                        "name": "models/bad/slash",
                        "displayName": "Bad",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                ],
                "nextPageToken": "next",
            },
        )

    router.gemini.transport = httpx.MockTransport(lambda _: httpx.Response(403, json={}))
    with pytest.raises(ProviderError, match="HTTP 403"):
        await router.list_models(base_url="", secret_ref=gemini_ref)
    failed_gemini = repo.get_connection("gemini-catalog")
    assert failed_gemini is not None and failed_gemini["error"] == "permission_denied"
    router.gemini.transport = httpx.MockTransport(gemini_handler)
    assert await router.list_models(base_url="", secret_ref=gemini_ref) == [
        "gemini-a",
        "gemini-b",
    ]
    gemini_record = repo.get_connection("gemini-catalog")
    assert gemini_record is not None
    assert gemini_record["status"] == "connected" and gemini_record["error"] is None
    assert gemini_record["model_names"] == {"gemini-a": "Gemini A", "gemini-b": "Gemini B"}
    assert credentials.get(gemini_ref) == "gemini-key"


@pytest.mark.asyncio
async def test_real_ux_repository_oauth_and_provider_metadata(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "operant.db")
    store.initialize()
    repo = UXRepository(store)
    credentials = CredentialStore(tmp_path / ".env")
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private.public_key().public_numbers()
    token_id: list[str] = [""]
    revoked: list[dict[str, list[str]]] = []
    fail_revoke = [True]
    refreshes: list[dict[str, list[str]]] = []
    fail_refresh = [True]

    def oauth_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("jwks.json"):
            return httpx.Response(
                200,
                json={
                    "keys": [
                        {
                            "kid": "key-1",
                            "kty": "RSA",
                            "alg": "RS256",
                            "n": _b64(public.n.to_bytes(256, "big")),
                            "e": _b64(public.e.to_bytes(3, "big")),
                        }
                    ]
                },
            )
        form = parse_qs(request.content.decode())
        if request.url.path.endswith("/revoke"):
            revoked.append(form)
            if fail_revoke[0]:
                return httpx.Response(503, json={"detail": "private revoke detail"})
            return httpx.Response(200)
        if form.get("grant_type") == ["refresh_token"]:
            refreshes.append(form)
            if fail_refresh[0]:
                return httpx.Response(
                    400,
                    json={
                        "error": "invalid_grant",
                        "token": "must-never-leak",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "access_token": "access-after-restart",
                    "refresh_token": "refresh-after-restart",
                    "expires_in": 3600,
                },
            )
        return httpx.Response(
            200,
            json={
                "access_token": "access-before-restart",
                "refresh_token": "refresh-before-restart",
                "id_token": token_id[0],
                "expires_in": 3600,
                "scope": "openid offline_access resource.invoke chatgpt.tokens.use.direct",
            },
        )

    transport = httpx.MockTransport(oauth_handler)
    oauth = OAuthConnections(repo, credentials, transport=transport)
    attempt, _ = oauth.start(
        provider="chatgpt",
        redirect_uri="http://127.0.0.1:4343/internal/model-auth/chatgpt/callback",
    )
    token_id[0] = _jwt(private, attempt.nonce)
    record = await oauth.complete(
        provider="chatgpt", state=attempt.state, code="code", client_id="oaiapp_issued"
    )
    assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o600
    with store._connect() as connection:
        body = connection.execute(
            "SELECT body_json FROM ux_model_connections WHERE id=?", (attempt.connection_id,)
        ).fetchone()[0]
    assert "access-before-restart" not in body
    assert "refresh-before-restart" not in body
    assert "id_token" not in body
    repo.save_connection(attempt.connection_id, {**record, "expires_at": 0})
    for suffix in ("ACCESS_TOKEN", "REFRESH_TOKEN", "ID_TOKEN"):
        os.environ.pop(secret_ref(attempt.connection_id, suffix), None)
    resumed = OAuthConnections(repo, CredentialStore(tmp_path / ".env"), transport=transport)
    with pytest.raises(OAuthError, match="OAuth token refresh failed") as failed:
        await resumed.access_token(repo.get_connection(attempt.connection_id) or {})
    assert "must-never-leak" not in str(failed.value)
    failed_record = repo.get_connection(attempt.connection_id)
    assert failed_record is not None
    assert failed_record["status"] == "error"
    assert failed_record["error"] == "OAuth token refresh failed"
    assert credentials.get(secret_ref(attempt.connection_id, "REFRESH_TOKEN")) == (
        "refresh-before-restart"
    )
    fail_refresh[0] = False
    second_runtime = OAuthConnections(repo, CredentialStore(tmp_path / ".env"), transport=transport)
    recovered = await asyncio.gather(
        resumed.access_token(repo.get_connection(attempt.connection_id) or {}),
        second_runtime.access_token(repo.get_connection(attempt.connection_id) or {}),
    )
    assert recovered == ["access-after-restart", "access-after-restart"]
    assert len(refreshes) == 2
    connected = repo.get_connection(attempt.connection_id)
    assert connected is not None and connected["status"] == "connected"
    assert connected["error"] is None
    current = repo.get_connection(attempt.connection_id)
    assert current is not None
    with pytest.raises(OAuthError, match="revocation failed"):
        await resumed.disconnect(current)
    pending = repo.get_connection(attempt.connection_id)
    assert pending is not None and pending["error"] == "revocation_unconfirmed"
    assert "private revoke detail" not in json.dumps(pending)
    router = ConnectionProviderRouter(repo, credentials, resumed)
    with pytest.raises(ProviderError, match="revocation is pending"):
        await router.list_models(base_url="", secret_ref=str(pending["secret_ref"]))
    fail_revoke[0] = False
    await resumed.disconnect(current)
    assert revoked[-1]["token"] == ["refresh-after-restart"]
    assert repo.get_connection(attempt.connection_id) is None

    repo.save_connection(
        "gemini-connection",
        {
            "connection_id": "gemini-connection",
            "secret_ref": "GEMINI_TEST_TOKEN",
        },
    )

    async def auth(_: str) -> dict[str, str]:
        return {"Authorization": "Bearer fixture"}

    def gemini_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if len(payload["contents"]) == 1:
            parts = [
                {
                    "functionCall": {"id": "tool-1", "name": "read_file", "args": {}},
                    "thoughtSignature": "opaque-signature",
                    "thought": "hidden thought body",
                }
            ]
        else:
            assert payload["contents"][1]["parts"][0]["thoughtSignature"] == ("opaque-signature")
            parts = [{"text": "done"}]
        chunk = {"candidates": [{"finishReason": "STOP", "content": {"parts": parts}}]}
        return httpx.Response(200, text=f"data: {json.dumps(chunk)}\n\n")

    def resolve(_: str) -> dict[str, Any] | None:
        return repo.get_connection("gemini-connection")

    first = GeminiNativeProvider(
        auth,
        transport=httpx.MockTransport(gemini_handler),
        metadata_repo=repo,
        connection_for_ref=resolve,
    )
    user = Message(role=MessageRole.USER, content="read")
    output = [
        event
        async for event in first.stream(
            snapshot=_snapshot("gemini", "gemini-3-model", "GEMINI_TEST_TOKEN"),
            messages=[user],
            tools=[],
        )
    ]
    assert output[-1].response is not None
    call = output[-1].response.tool_calls[0]
    with store._connect() as connection:
        metadata = connection.execute(
            "SELECT body_json FROM ux_provider_metadata WHERE connection_id=?",
            ("gemini-connection",),
        ).fetchone()[0]
    assert "opaque-signature" in metadata
    assert "hidden thought body" not in metadata
    second = GeminiNativeProvider(
        auth,
        transport=httpx.MockTransport(gemini_handler),
        metadata_repo=UXRepository(SQLiteStore(store.path)),
        connection_for_ref=resolve,
    )
    _ = [
        event
        async for event in second.stream(
            snapshot=_snapshot("gemini", "gemini-3-model", "GEMINI_TEST_TOKEN"),
            messages=[user, Message(role=MessageRole.ASSISTANT, tool_calls=(call,))],
            tools=[],
        )
    ]

    text_payloads: list[dict[str, Any]] = []

    def text_handler(request: httpx.Request) -> httpx.Response:
        text_payloads.append(json.loads(request.content))
        chunk = {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {
                        "parts": [
                            {
                                "text": "visible answer",
                                "thoughtSignature": "opaque-text-signature",
                                "thought": "private model thought",
                            }
                        ]
                    },
                }
            ]
        }
        return httpx.Response(200, text=f"data: {json.dumps(chunk)}\n\n")

    text_user = Message(role=MessageRole.USER, content="text prompt")
    text_provider = GeminiNativeProvider(
        auth,
        transport=httpx.MockTransport(text_handler),
        metadata_repo=repo,
        connection_for_ref=resolve,
    )
    _ = [
        event
        async for event in text_provider.stream(
            snapshot=_snapshot("gemini", "gemini-3-model", "GEMINI_TEST_TOKEN"),
            messages=[text_user],
            tools=[],
        )
    ]
    with store._connect() as connection:
        rows = connection.execute(
            "SELECT body_json FROM ux_provider_metadata WHERE connection_id=?",
            ("gemini-connection",),
        ).fetchall()
    assert any("opaque-text-signature" in row[0] for row in rows)
    assert all("private model thought" not in row[0] for row in rows)
    assert all("visible answer" not in row[0] for row in rows)
    text_restarted = GeminiNativeProvider(
        auth,
        transport=httpx.MockTransport(text_handler),
        metadata_repo=UXRepository(SQLiteStore(store.path)),
        connection_for_ref=resolve,
    )
    _ = [
        event
        async for event in text_restarted.stream(
            snapshot=_snapshot("gemini", "gemini-3-model", "GEMINI_TEST_TOKEN"),
            messages=[
                text_user,
                Message(role=MessageRole.ASSISTANT, content="visible answer"),
                Message(role=MessageRole.USER, content="next"),
            ],
            tools=[],
        )
    ]
    assert text_payloads[1]["contents"][1]["parts"][0]["thoughtSignature"] == (
        "opaque-text-signature"
    )
    repo.delete_connection("gemini-connection")
    with store._connect() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM ux_provider_metadata WHERE connection_id=?",
                ("gemini-connection",),
            ).fetchone()[0]
            == 0
        )


def test_connection_routes_restrict_origin_and_keep_secret_out_of_responses(tmp_path: Path) -> None:
    repo = _Repo()

    class Service:
        def __init__(self) -> None:
            self.store = SimpleNamespace(path=tmp_path / "store.db")
            self.profiles: dict[str, ModelProfile] = {}

        def add_model_profile(self, profile: ModelProfile) -> ModelProfile:
            self.profiles[profile.id] = profile
            return profile

        def deactivate_model_profile(self, profile_id: str) -> ModelProfile:
            return self.profiles.pop(profile_id)

    app = FastAPI()
    service = Service()

    class Gateway:
        def __init__(self) -> None:
            self.decision = "allow"
            self.guards = 0

        def guard(self, **kwargs: Any) -> tuple[Any, Any, None]:
            self.guards += 1
            return (
                SimpleNamespace(action_hash="hash"),
                SimpleNamespace(
                    decision=SimpleNamespace(value=self.decision),
                    lease=object() if self.decision == "allow" else None,
                    reason_code="policy.denied",
                    approval_id=None,
                ),
                None,
            )

        def consume(self, lease: Any, action: Any) -> None:
            pass

    gateway = Gateway()
    provider = install_model_connection_routes(
        app, cast(Any, service), repo, action_gateway=cast(Any, gateway)
    )

    async def models(*, base_url: str, secret_ref: str) -> list[str]:
        return ["model-1"]

    provider.list_models = models  # type: ignore[method-assign]
    with TestClient(app, base_url="http://127.0.0.1:4343") as client:
        body = {"provider": "openai-compatible", "name": "Test", "api_key": "secret-value"}
        blocked = client.post(
            "/v1/setup/connections", json=body, headers={"Origin": "https://evil.example"}
        )
        assert blocked.status_code == 403
        assert (
            client.post(
                "/v1/setup/connections",
                json=body,
                headers={"Origin": "http://127.0.0.1:not-a-port"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/v1/setup/connections",
                json=body,
                headers={"Host": "127.0.0.1:not-a-port"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/v1/setup/connections",
                json=body,
                headers={"Origin": "http://127.0.0.1:3000"},
            ).status_code
            == 403
        )
        app.state.setup_allowed_origins = frozenset(
            {"http://127.0.0.1:3000", "https://evil.example"}
        )
        assert (
            client.post(
                "/v1/setup/connections",
                json=body,
                headers={"Origin": "http://127.0.0.1:3018"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/v1/setup/connections",
                json=body,
                headers={"Origin": "https://evil.example"},
            ).status_code
            == 403
        )
        gateway.decision = "deny"
        assert (
            client.post(
                "/v1/setup/connections",
                json=body,
                headers={"Origin": "http://127.0.0.1:3000"},
            ).status_code
            == 403
        )
        assert repo.records == {}
        assert not (tmp_path / ".env").exists()
        gateway.decision = "allow"
        headers = {
            "Idempotency-Key": "create-test",
            "Origin": "http://127.0.0.1:3000",
        }
        created = client.post("/v1/setup/connections", json=body, headers=headers)
        assert created.status_code == 201
        assert repo.get_command_fingerprint_version("create-test") == "hmac-v1"
        before = gateway.guards
        assert client.post("/v1/setup/connections", json=body, headers=headers).json() == (
            created.json()
        )
        assert gateway.guards == before
        assert (
            client.post(
                "/v1/setup/connections", json={**body, "api_key": "different"}, headers=headers
            ).status_code
            == 409
        )

        # Historical create receipt for the fixed synthetic input above. Keep
        # this golden value independent of today's DTO and hashing code so a
        # compatibility regression cannot update both sides of the assertion.
        legacy_fingerprint = "260df06c7dabada1f82d6aa316ec51f04bdfe4768e0bc3b65d80c353fc37aa7c"
        saved_result = dict(repo.commands["create-test"][1])
        saved_result.pop("_credential_fingerprint_version", None)
        repo.commands["create-test"] = (legacy_fingerprint, saved_result)
        repo.records[created.json()["connection_id"]]["create_fingerprint"] = legacy_fingerprint
        repo.records[created.json()["connection_id"]].pop("create_fingerprint_version")
        CredentialStore(tmp_path / ".env").delete("OPERANT_CONNECTION_REQUEST_FINGERPRINT_KEY")
        guards_before_legacy = gateway.guards
        assert (
            client.post("/v1/setup/connections", json=body, headers=headers).json()
            == created.json()
        )
        assert gateway.guards == guards_before_legacy
        assert (
            CredentialStore(tmp_path / ".env").get("OPERANT_CONNECTION_REQUEST_FINGERPRINT_KEY")
            is None
        )
        assert repo.commands["create-test"] == (legacy_fingerprint, saved_result)
        assert (
            client.post(
                "/v1/setup/connections", json={**body, "api_key": "different"}, headers=headers
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/v1/setup/connections", json={**body, "name": "Changed name"}, headers=headers
            ).status_code
            == 409
        )

        # A historical write may have committed the connection before its receipt.
        repo.commands.pop("create-test")
        assert (
            client.post("/v1/setup/connections", json=body, headers=headers).json()
            == created.json()
        )
        assert len(repo.records) == 1
        assert repo.commands["create-test"] == (legacy_fingerprint, saved_result)
        oauth_headers = {"Idempotency-Key": "oauth-start-test"}
        started = client.post(
            "/v1/setup/oauth/start", json={"provider": "chatgpt"}, headers=oauth_headers
        )
        assert started.status_code == 200
        assert started.json()["authorization_url"].startswith(
            "https://auth.openai.com/api/accounts/authorize?"
        )
        assert started.headers["cache-control"] == "no-store"
        assert "authorization_url" not in repo.commands["oauth-start-test"][1]
        attempt_id = started.json()["attempt_id"]
        pending = client.get(f"/v1/setup/oauth/{attempt_id}")
        assert pending.status_code == 200
        assert pending.headers["cache-control"] == "no-store"
        assert pending.json()["authorization_url"] == started.json()["authorization_url"]
        assert "authorization_url" not in repo.commands["oauth-start-test"][1]
        assert (
            client.post(
                "/v1/setup/oauth/start", json={"provider": "chatgpt"}, headers=oauth_headers
            ).json()["authorization_url"]
            == started.json()["authorization_url"]
        )
        invalid_callback = client.get(
            "/internal/model-auth/chatgpt/callback?code=secret-code&state=invalid-state"
        )
        assert invalid_callback.status_code == 400
        assert invalid_callback.headers["cache-control"] == "no-store"
        assert "secret-code" not in invalid_callback.text
        cancelled = client.delete(f"/v1/setup/oauth/{attempt_id}")
        assert cancelled.status_code == 200
        assert cancelled.headers["cache-control"] == "no-store"
        assert cancelled.json()["authorization_url"] is None
        after_cancel = client.get(f"/v1/setup/oauth/{attempt_id}")
        assert after_cancel.json()["status"] == "cancelled"
        assert after_cancel.json()["authorization_url"] is None
        connection_id = created.json()["connection_id"]
        assert "secret-value" not in created.text
        assert "secret-value" not in client.get("/v1/setup/connections").text
        discovered = client.post(f"/v1/setup/connections/{connection_id}/models")
        assert discovered.json()["model_ids"] == ["model-1"]
        selected = client.post(
            f"/v1/setup/connections/{connection_id}/profiles", json={"model_id": "model-1"}
        )
        assert selected.status_code == 200
        assert repo.get_setting("default_model_profile_id") == selected.json()["model_profile_id"]
        assert client.delete(f"/v1/setup/connections/{connection_id}").status_code == 200
    assert repo.records == {}


def test_oauth_polling_keeps_url_in_memory_only_with_real_repository(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "operant.db")
    store.initialize()
    repo = UXRepository(store)

    class Gateway:
        def guard(self, **kwargs: Any) -> tuple[Any, Any, None]:
            return (
                SimpleNamespace(action_hash="hash"),
                SimpleNamespace(
                    decision=SimpleNamespace(value="allow"),
                    lease=object(),
                    reason_code=None,
                    approval_id=None,
                ),
                None,
            )

        def consume(self, lease: Any, action: Any) -> None:
            pass

    app = FastAPI()
    service = SimpleNamespace(store=store)
    install_model_connection_routes(
        app, cast(Any, service), repo, action_gateway=cast(Any, Gateway())
    )
    with TestClient(app, base_url="http://127.0.0.1:4343") as client:
        started = client.post(
            "/v1/setup/oauth/start",
            json={"provider": "chatgpt"},
            headers={"Idempotency-Key": "pending-oauth"},
        )
        assert started.status_code == 200
        pending = client.get(f"/v1/setup/oauth/{started.json()['attempt_id']}")
        assert pending.json()["authorization_url"] == started.json()["authorization_url"]
        assert pending.headers["cache-control"] == "no-store"
    with store._connect() as connection:
        rows = connection.execute("SELECT body_json FROM ux_commands").fetchall()
    assert len(rows) == 1
    assert "authorization_url" not in rows[0]["body_json"]
    assert "code_challenge" not in rows[0]["body_json"]


def test_request_fingerprint_is_private_stable_and_serializes_creation(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / ".env")
    grants: list[bool] = []
    value = "synthetic-key-for-request-matching"

    def digest(_: int) -> str:
        return store.request_fingerprint(
            "api-key", value, before_create=lambda: grants.append(True), allow_create=True
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(digest, range(8)))
    assert len(set(results)) == 1
    assert grants == [True]
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert value not in store.path.read_text()
    assert results[0] != hashlib.sha256(value.encode()).hexdigest()
    reopened = CredentialStore(store.path)
    assert (
        reopened.request_fingerprint(
            "api-key",
            value,
            before_create=lambda: pytest.fail("unexpected key creation"),
            allow_create=False,
        )
        == results[0]
    )
    assert (
        reopened.request_fingerprint(
            "oauth-secret",
            value,
            before_create=lambda: pytest.fail("unexpected key creation"),
            allow_create=False,
        )
        != results[0]
    )


def test_request_fingerprint_rejects_missing_key_for_existing_receipt(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / ".env")
    with pytest.raises(CredentialError, match="fingerprint key is missing"):
        store.request_fingerprint(
            "api-key",
            "synthetic-key",
            before_create=lambda: pytest.fail("must not replace lost key"),
            allow_create=False,
        )
    assert not store.path.exists()


def test_full_core_model_setup_has_one_private_receipt_and_no_oauth_url_cache(
    tmp_path: Path,
) -> None:
    from operant.api import create_app

    app = create_app(tmp_path / "core.sqlite")
    with TestClient(app, base_url="http://127.0.0.1:4343") as client:
        headers = {"Idempotency-Key": "full-core-api-connect"}
        payload = {"provider": "openai-compatible", "api_key": "synthetic-api-key-for-core-receipt"}
        first = client.post("/v1/setup/connections", json=payload, headers=headers)
        assert first.status_code == 201
        assert first.headers["idempotency-key"] == headers["Idempotency-Key"]
        assert (
            client.post("/v1/setup/connections", json=payload, headers=headers).json()
            == first.json()
        )
        oauth_headers = {"Idempotency-Key": "full-core-oauth-start"}
        started = client.post(
            "/v1/setup/oauth/start", json={"provider": "chatgpt"}, headers=oauth_headers
        )
        assert started.status_code == 200
        assert started.json()["authorization_url"]
        replayed = client.post(
            "/v1/setup/oauth/start", json={"provider": "chatgpt"}, headers=oauth_headers
        )
        assert replayed.json()["authorization_url"] == started.json()["authorization_url"]
        assert replayed.headers["cache-control"] == "no-store"
        assert (
            client.post(
                "/v1/setup/connections", json=payload, headers={"Idempotency-Key": ""}
            ).status_code
            == 400
        )
        store = app.state.operant_service.store
        with store._connect() as connection:
            private = connection.execute(
                "SELECT fingerprint,body_json FROM ux_commands ORDER BY key"
            ).fetchall()
            generic = connection.execute(
                "SELECT COUNT(*) FROM command_executions WHERE idempotency_key IN (?,?)",
                (headers["Idempotency-Key"], oauth_headers["Idempotency-Key"]),
            ).fetchone()[0]
        assert generic == 0
        assert len(private) == 2
        assert all(len(row["fingerprint"]) == 64 for row in private)
        assert any(
            json.loads(row["body_json"]).get("_credential_fingerprint_version") == "hmac-v1"
            for row in private
        )
        serialized = json.dumps([dict(row) for row in private])
        assert payload["api_key"] not in serialized
        assert "authorization_url" not in serialized
        assert "code_challenge" not in serialized
