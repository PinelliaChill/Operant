"""Loopback-only onboarding routes for model connections."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any, cast
from urllib.parse import urlsplit

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse

from operant.application.phase45_gateway import Phase45ActionGateway
from operant.application.service import ApplicationService
from operant.contracts.onboarding import (
    ConnectionCreate,
    ConnectionDeleted,
    ConnectionModels,
    ConnectionModelSelection,
    ConnectionProfile,
    OAuthAttempt,
    OAuthStart,
    ProviderConnection,
    ProviderConnectionList,
)
from operant.domain.models import ModelProfile
from operant.domain.security import Capability
from operant.model_connections.credentials import CredentialError, CredentialStore
from operant.model_connections.oauth import (
    Attempt,
    ConnectionRepository,
    OAuthConnections,
    OAuthError,
    secret_ref,
)
from operant.persistence.sqlite import ConflictError, NotFoundError
from operant.providers.base import ModelProvider
from operant.providers.openai_compatible import ProviderError
from operant.providers.router import ConnectionProviderRouter

_DESKTOP_ORIGINS = frozenset(
    {"tauri://localhost", "http://tauri.localhost", "https://tauri.localhost"}
)
_SECRET_FINGERPRINT_VERSION = "hmac-v1"


def _view(record: dict[str, Any]) -> ProviderConnection:
    return ProviderConnection.model_validate(
        {key: record.get(key) for key in ProviderConnection.model_fields if key in record}
    )


def _oauth_view(attempt: Attempt, repo: ConnectionRepository) -> OAuthAttempt:
    status = {
        "pending": "pending",
        "processing": "pending",
        "completed": "connected",
        "failed": "error",
        "cancelled": "cancelled",
        "expired": "expired",
    }.get(attempt.status, "error")
    message = attempt.error
    if attempt.status == "completed":
        record = repo.get_connection(attempt.connection_id)
        if record is None:
            status, message = "error", "connection_unavailable"
        elif record.get("status") == "ready":
            status = "ready"
        elif record.get("status") in {"error", "needs_auth"}:
            status = "error"
            safe_errors = {
                "chatgpt_plan_usage_disabled",
                "permission_denied",
                "user_not_eligible",
                "usage_unavailable",
                "provider_unavailable",
                "authentication_required",
                "rate_limited",
                "usage_limit",
                "network_error",
                "revocation_unconfirmed",
                "unsupported_capability",
            }
            error = record.get("error")
            message = (
                error
                if isinstance(error, str) and error in safe_errors
                else "connection_unavailable"
            )
    return OAuthAttempt.model_validate(
        {
            "attempt_id": attempt.id,
            "connection_id": attempt.connection_id,
            "provider": attempt.provider,
            "status": status,
            "authorization_url": attempt.authorization_url if attempt.status == "pending" else None,
            "message": message,
            "expires_at": datetime.fromtimestamp(attempt.started_at + 600, tz=timezone.utc),
        }
    )


def _trusted_local(request: Request, local_authorizer: Callable[[Request], bool] | None) -> bool:
    if local_authorizer is not None and not local_authorizer(request):
        return False
    if request.client is None or request.client.host not in {"127.0.0.1", "::1", "testclient"}:
        return False
    host = request.headers.get("host", "")
    try:
        parsed_host = urlsplit(f"http://{host}")
        host_port = parsed_host.port
    except ValueError:
        return False
    if parsed_host.hostname not in {"127.0.0.1", "localhost", "::1", "testserver"}:
        return False
    origin = request.headers.get("origin")
    if origin is None:
        return True
    if origin in _DESKTOP_ORIGINS:
        return True
    try:
        parsed = urlsplit(origin)
        origin_port = parsed.port
    except ValueError:
        return False
    local_http_origin = (
        parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        and origin_port is not None
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
        and parsed.username is None
        and parsed.password is None
    )
    if not local_http_origin:
        return False
    if origin_port == host_port:
        return True
    configured = getattr(request.app.state, "setup_allowed_origins", ())
    return isinstance(configured, (tuple, list, set, frozenset)) and origin in configured


def _digest(operation: str, target: str, payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"operation": operation, "target": target, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _legacy_credential_digest(value: str) -> str:
    """Match an existing receipt only; never create a new unkeyed secret digest."""
    return hashlib.sha256(value.encode()).hexdigest()


def install_model_connection_routes(
    app: FastAPI,
    service: ApplicationService,
    repo: ConnectionRepository,
    *,
    action_gateway: Phase45ActionGateway,
    local_authorizer: Callable[[Request], bool] | None = None,
) -> ConnectionProviderRouter:
    credentials = CredentialStore(service.store.path.parent / ".env")
    oauth = OAuthConnections(repo, credentials)
    previous_provider = getattr(service, "provider", None)
    previous_provider = getattr(previous_provider, "delegate", previous_provider)
    if not all(hasattr(previous_provider, method) for method in ("list_models", "stream")):
        previous_provider = None
    router = ConnectionProviderRouter(
        repo, credentials, oauth, compatible_provider=cast(ModelProvider | None, previous_provider)
    )

    def require_local(request: Request) -> None:
        if not _trusted_local(request, local_authorizer):
            raise HTTPException(
                status_code=403, detail="model setup requires a trusted local client"
            )

    def get_record(connection_id: str) -> dict[str, Any]:
        record = repo.get_connection(connection_id)
        if record is None:
            raise HTTPException(status_code=404, detail="model connection not found")
        return record

    def prepare_command(
        *,
        operation: str,
        target: str,
        payload: dict[str, Any],
        idempotency_key: str | None,
    ) -> tuple[str, str, dict[str, Any] | None]:
        key = idempotency_key or str(uuid.uuid4())
        fingerprint = _digest(operation, target, payload)
        try:
            existing = repo.get_command(key, fingerprint)
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail="idempotency key is already used") from exc
        if existing is not None:
            return key, fingerprint, existing
        action, result, _ = action_gateway.guard(
            tool="model_connection",
            operation=operation,
            target_id=target,
            arguments={"operation": operation, "request_digest": fingerprint},
            capabilities=(Capability.WORKSPACE_WRITE,),
            idempotency_key=key,
        )
        if result.decision.value == "ask":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "approval_required",
                    "approval_id": result.approval_id,
                    "action_hash": action.action_hash,
                },
            )
        if result.decision.value != "allow" or result.lease is None:
            raise HTTPException(status_code=403, detail=result.reason_code)
        action_gateway.consume(result.lease, action)
        return key, fingerprint, None

    def secret_payload_digest(
        key: str,
        context: str,
        value: str,
        previous_fingerprint: str | None = None,
        previous_version: str | None = None,
    ) -> tuple[str, str]:
        saved_command = repo.get_command_fingerprint(key)
        saved = saved_command or previous_fingerprint
        version = (
            repo.get_command_fingerprint_version(key)
            if saved_command is not None
            else previous_version
        )
        if version not in {None, _SECRET_FINGERPRINT_VERSION}:
            raise HTTPException(
                status_code=409, detail="request fingerprint version is unavailable"
            )
        if saved is not None and version is None:
            return _legacy_credential_digest(value), ""

        def authorize_private_key() -> None:
            prepare_command(
                operation="initialize_request_matching_key",
                target="model-connections",
                payload={"format": _SECRET_FINGERPRINT_VERSION},
                idempotency_key="model-connection-request-matching-key:v1",
            )

        try:
            return (
                credentials.request_fingerprint(
                    context,
                    value,
                    before_create=authorize_private_key,
                    allow_create=saved is None,
                ),
                _SECRET_FINGERPRINT_VERSION,
            )
        except CredentialError as exc:
            raise HTTPException(
                status_code=400, detail="credential storage is unavailable"
            ) from exc

    def save_command(
        key: str, fingerprint: str, value: Any, *, fingerprint_version: str = ""
    ) -> None:
        try:
            result = value.model_dump(mode="json")
            if fingerprint_version:
                result["_credential_fingerprint_version"] = fingerprint_version
            repo.save_command(key, fingerprint, result)
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail="idempotency result conflict") from exc

    @app.get(
        "/v1/setup/connections",
        operation_id="listModelConnections",
        response_model=ProviderConnectionList,
    )
    def list_connections(request: Request) -> ProviderConnectionList:
        require_local(request)
        return ProviderConnectionList(items=[_view(record) for record in repo.list_connections()])

    @app.post(
        "/v1/setup/connections",
        operation_id="createModelConnection",
        response_model=ProviderConnection,
        status_code=201,
    )
    def create_connection(
        request: Request,
        body: ConnectionCreate,
        idempotency_key: str | None = Header(
            default=None, alias="Idempotency-Key", min_length=1, max_length=300
        ),
    ) -> ProviderConnection:
        require_local(request)
        if body.provider == "chatgpt":
            raise HTTPException(status_code=400, detail="ChatGPT requires OAuth sign-in")
        if body.api_key is None or not body.api_key.get_secret_value():
            raise HTTPException(status_code=400, detail="API key is required")
        if (
            body.client_id is not None
            or body.client_secret is not None
            or body.project_id is not None
        ):
            raise HTTPException(status_code=400, detail="OAuth fields require OAuth sign-in")
        supplied_key = idempotency_key or str(uuid.uuid4())
        connection_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"operant:model-connection:{supplied_key}")
        )
        previous = repo.get_connection(connection_id)
        payload = body.model_dump(mode="json", exclude={"api_key", "client_secret"})
        payload["api_key_digest"], fingerprint_version = secret_payload_digest(
            supplied_key,
            "model-connection-api-key",
            body.api_key.get_secret_value(),
            previous.get("create_fingerprint") if previous is not None else None,
            previous.get("create_fingerprint_version") if previous is not None else None,
        )
        key, fingerprint, cached = prepare_command(
            operation="create",
            target="model-connections",
            payload=payload,
            idempotency_key=supplied_key,
        )
        if cached is not None:
            return ProviderConnection.model_validate(cached)
        if previous is not None:
            if previous.get("create_fingerprint") != fingerprint:
                raise HTTPException(status_code=409, detail="model connection command conflict")
            result = _view(previous)
            save_command(key, fingerprint, result, fingerprint_version=fingerprint_version)
            return result
        base_url = body.base_url or (
            "https://generativelanguage.googleapis.com/v1"
            if body.provider == "gemini"
            else "https://api.openai.com/v1"
        )
        if (
            body.provider == "gemini"
            and base_url.rstrip("/") != "https://generativelanguage.googleapis.com/v1"
        ):
            raise HTTPException(status_code=400, detail="Gemini uses its official API endpoint")
        try:
            # ModelProfile applies the same URL validation used by existing profiles.
            ModelProfile(
                name="connection validation",
                provider=body.provider,
                model_id="pending",
                base_url=base_url,
                secret_ref=secret_ref(connection_id, "API_KEY"),
            )
            credentials.put(secret_ref(connection_id, "API_KEY"), body.api_key.get_secret_value())
            record: dict[str, Any] = {
                "connection_id": connection_id,
                "name": body.name or ("Gemini" if body.provider == "gemini" else "API connection"),
                "provider": body.provider,
                "auth_method": "api_key",
                "status": "connected",
                "base_url": base_url,
                "model_ids": [],
                "profile_ids": [],
                "secret_ref": secret_ref(connection_id, "API_KEY"),
                "create_fingerprint": fingerprint,
                "create_fingerprint_version": fingerprint_version or None,
            }
            repo.save_connection(connection_id, record)
            result = _view(record)
            save_command(key, fingerprint, result, fingerprint_version=fingerprint_version)
            return result
        except (ValueError, CredentialError) as exc:
            raise HTTPException(
                status_code=400, detail="model connection configuration is invalid"
            ) from exc

    @app.post(
        "/v1/setup/connections/{connection_id}/models",
        operation_id="discoverConnectionModels",
        response_model=ConnectionModels,
    )
    async def discover_models(
        request: Request,
        connection_id: str,
        idempotency_key: str | None = Header(
            default=None, alias="Idempotency-Key", min_length=1, max_length=300
        ),
    ) -> ConnectionModels:
        require_local(request)
        key, fingerprint, cached = prepare_command(
            operation="discover",
            target=connection_id,
            payload={},
            idempotency_key=idempotency_key,
        )
        if cached is not None:
            return ConnectionModels.model_validate(cached)
        record = get_record(connection_id)
        if (
            record.get("status") not in {"connected", "ready", "error"}
            or record.get("error") == "revocation_unconfirmed"
        ):
            raise HTTPException(status_code=409, detail="model connection needs authorization")
        try:
            models = await router.list_models(
                base_url=str(record["base_url"]), secret_ref=str(record["secret_ref"])
            )
        except (ProviderError, OAuthError) as exc:
            raise HTTPException(status_code=502, detail="model discovery failed") from exc
        latest = repo.get_connection(connection_id) or record
        repo.save_connection(connection_id, {**latest, "model_ids": models})
        result = ConnectionModels(
            connection_id=connection_id, model_ids=models, model_names=latest.get("model_names", {})
        )
        save_command(key, fingerprint, result)
        return result

    @app.post(
        "/v1/setup/connections/{connection_id}/profiles",
        operation_id="selectConnectionModel",
        response_model=ConnectionProfile,
    )
    def select_model(
        request: Request,
        connection_id: str,
        body: ConnectionModelSelection,
        idempotency_key: str | None = Header(
            default=None, alias="Idempotency-Key", min_length=1, max_length=300
        ),
    ) -> ConnectionProfile:
        require_local(request)
        key, fingerprint, cached = prepare_command(
            operation="select_model",
            target=connection_id,
            payload=body.model_dump(mode="json"),
            idempotency_key=idempotency_key,
        )
        if cached is not None:
            return ConnectionProfile.model_validate(cached)
        record = get_record(connection_id)
        if record.get("status") not in {"connected", "ready"}:
            raise HTTPException(status_code=409, detail="model connection is not ready")
        if body.model_id not in record.get("model_ids", []):
            raise HTTPException(status_code=400, detail="model must be selected from discovery")
        profile_id = "model_" + str(uuid.uuid5(uuid.NAMESPACE_URL, f"operant:model-profile:{key}"))
        if profile_id in record.get("profile_ids", []):
            result = ConnectionProfile(
                connection_id=connection_id,
                model_profile_id=profile_id,
                model_id=body.model_id,
            )
            save_command(key, fingerprint, result)
            return result
        model_name = record.get("model_names", {}).get(body.model_id, body.model_id)
        profile = ModelProfile(
            id=profile_id,
            name=body.name or f"{record['name']} · {model_name}"[:100],
            provider=str(record["provider"]),
            model_id=body.model_id,
            base_url=str(record["base_url"]),
            secret_ref=str(record["secret_ref"]),
            supports_temperature=False,
            effort_parameter=None,
        )
        try:
            service.add_model_profile(profile)
        except (ConflictError, ValueError) as exc:
            raise HTTPException(
                status_code=400, detail="model profile could not be created"
            ) from exc
        ids = [*record.get("profile_ids", []), profile.id]
        repo.save_connection(connection_id, {**record, "profile_ids": ids, "status": "ready"})
        if repo.get_setting("default_model_profile_id") is None:
            repo.set_setting("default_model_profile_id", profile.id)
        result = ConnectionProfile(
            connection_id=connection_id, model_profile_id=profile.id, model_id=body.model_id
        )
        save_command(key, fingerprint, result)
        return result

    @app.delete(
        "/v1/setup/connections/{connection_id}",
        operation_id="deleteModelConnection",
        response_model=ConnectionDeleted,
    )
    async def delete_connection(
        request: Request,
        connection_id: str,
        idempotency_key: str | None = Header(
            default=None, alias="Idempotency-Key", min_length=1, max_length=300
        ),
    ) -> ConnectionDeleted:
        require_local(request)
        key, fingerprint, cached = prepare_command(
            operation="disconnect",
            target=connection_id,
            payload={},
            idempotency_key=idempotency_key,
        )
        if cached is not None:
            return ConnectionDeleted.model_validate(cached)
        record = get_record(connection_id)
        try:
            if record.get("auth_method") == "oauth":
                await oauth.disconnect(record)
            else:
                credentials.delete(str(record["secret_ref"]))
                repo.delete_connection(connection_id)
            for profile_id in record.get("profile_ids", []):
                with suppress(NotFoundError):
                    service.deactivate_model_profile(profile_id)
            if repo.get_setting("default_model_profile_id") in record.get("profile_ids", []):
                repo.set_setting("default_model_profile_id", None)
        except (OAuthError, CredentialError) as exc:
            raise HTTPException(
                status_code=502, detail="model connection could not be disconnected"
            ) from exc
        result = ConnectionDeleted(connection_id=connection_id)
        save_command(key, fingerprint, result)
        return result

    @app.post(
        "/v1/setup/oauth/start",
        operation_id="startModelOAuth",
        response_model=OAuthAttempt,
    )
    def start_oauth(
        request: Request,
        response: Response,
        body: OAuthStart,
        idempotency_key: str | None = Header(
            default=None, alias="Idempotency-Key", min_length=1, max_length=300
        ),
    ) -> OAuthAttempt:
        response.headers["Cache-Control"] = "no-store"
        require_local(request)
        supplied_key = idempotency_key or str(uuid.uuid4())
        payload = body.model_dump(mode="json", exclude={"client_secret"})
        fingerprint_version = ""
        if body.client_secret is not None:
            payload["client_secret_digest"], fingerprint_version = secret_payload_digest(
                supplied_key, "model-oauth-client-secret", body.client_secret.get_secret_value()
            )
        key, fingerprint, cached = prepare_command(
            operation="oauth_start",
            target=body.connection_id or "model-connections",
            payload=payload,
            idempotency_key=supplied_key,
        )
        if cached is not None:
            attempt_id = cached.get("attempt_id")
            attempt = oauth.attempts.get(str(attempt_id))
            if attempt is None:
                return OAuthAttempt.model_validate(
                    {
                        **cached,
                        "status": "expired",
                        "authorization_url": None,
                    }
                )
            oauth.status(attempt.id)
            return _oauth_view(attempt, repo)
        server = request.scope.get("server")
        if (
            not isinstance(server, (tuple, list))
            or len(server) != 2
            or server[0] != "127.0.0.1"
            or not isinstance(server[1], int)
        ):
            raise HTTPException(status_code=403, detail="OAuth requires a loopback Core listener")
        # OpenAI requires the literal IPv4 host throughout authorization and exchange.
        redirect_uri = f"http://127.0.0.1:{server[1]}/internal/model-auth/{body.provider}/callback"
        try:
            attempt, url = oauth.start(
                provider=body.provider,
                redirect_uri=redirect_uri,
                connection_id=body.connection_id,
                google_client_id=body.client_id,
                google_client_secret=(
                    body.client_secret.get_secret_value() if body.client_secret else None
                ),
                project_id=body.project_id,
            )
        except (OAuthError, CredentialError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        result = OAuthAttempt(
            attempt_id=attempt.id,
            connection_id=attempt.connection_id,
            provider=body.provider,
            status="pending",
            authorization_url=url,
            expires_at=datetime.fromtimestamp(attempt.started_at + 600, tz=timezone.utc),
        )
        # State and nonce belong only to the in-memory OAuth attempt, not SQLite.
        stored_result = result.model_dump(mode="json", exclude={"authorization_url"})
        if fingerprint_version:
            stored_result["_credential_fingerprint_version"] = fingerprint_version
        repo.save_command(key, fingerprint, stored_result)
        return result

    @app.get(
        "/v1/setup/oauth/{attempt_id}",
        operation_id="getModelOAuthStatus",
        response_model=OAuthAttempt,
    )
    def oauth_status(request: Request, response: Response, attempt_id: str) -> OAuthAttempt:
        response.headers["Cache-Control"] = "no-store"
        require_local(request)
        try:
            attempt = oauth._attempt(attempt_id)
            oauth.status(attempt_id)
        except OAuthError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _oauth_view(attempt, repo)

    @app.delete(
        "/v1/setup/oauth/{attempt_id}",
        operation_id="cancelModelOAuth",
        response_model=OAuthAttempt,
    )
    def cancel_oauth(
        request: Request,
        response: Response,
        attempt_id: str,
        idempotency_key: str | None = Header(
            default=None, alias="Idempotency-Key", min_length=1, max_length=300
        ),
    ) -> OAuthAttempt:
        response.headers["Cache-Control"] = "no-store"
        require_local(request)
        key, fingerprint, cached = prepare_command(
            operation="oauth_cancel",
            target=attempt_id,
            payload={},
            idempotency_key=idempotency_key,
        )
        if cached is not None:
            return OAuthAttempt.model_validate(cached)
        try:
            oauth.cancel(attempt_id)
        except OAuthError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        result = oauth_status(request, response, attempt_id)
        save_command(key, fingerprint, result)
        return result

    @app.get("/internal/model-auth/{provider}/callback", include_in_schema=False)
    async def oauth_callback(request: Request, provider: str) -> PlainTextResponse:
        params = request.query_params
        # The ASGI access log must never receive an OAuth code through the scope.
        request.scope["query_string"] = b""
        require_local(request)
        if provider not in {"chatgpt", "gemini"}:
            raise HTTPException(status_code=404, detail="OAuth provider not found")
        attempt = next(
            (
                item
                for item in oauth.attempts.values()
                if item.provider == provider and item.state == params.get("state")
            ),
            None,
        )
        if attempt is None:
            return PlainTextResponse(
                "连接失败。请返回 Operant 重试。",
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        prepare_command(
            operation="oauth_start",
            target=attempt.connection_id,
            payload={"callback_state_digest": hashlib.sha256(attempt.state.encode()).hexdigest()},
            idempotency_key=f"model-oauth-callback:{attempt.id}",
        )
        try:
            await oauth.complete(
                provider=provider,
                state=params.get("state", ""),
                code=params.get("code"),
                client_id=params.get("client_id"),
                error=params.get("error"),
            )
        except OAuthError:
            return PlainTextResponse(
                "连接失败。请返回 Operant 重试。",
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        return PlainTextResponse(
            "连接成功。现在可以返回 Operant。",
            headers={"Cache-Control": "no-store"},
        )

    return router
