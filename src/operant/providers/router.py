from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator, Sequence
from typing import Any

from operant.domain.messages import Message, ProviderEvent, ToolDefinition
from operant.domain.models import RoleSnapshot
from operant.model_connections.credentials import CredentialStore
from operant.model_connections.oauth import ConnectionRepository, OAuthConnections
from operant.providers.base import ModelProvider
from operant.providers.chatgpt_responses import ChatGPTResponsesProvider
from operant.providers.gemini_native import GeminiNativeProvider
from operant.providers.openai_compatible import OpenAICompatibleProvider, ProviderError


class ConnectionProviderRouter:
    """Keep existing profiles working while routing connected native providers."""

    def __init__(
        self,
        repo: ConnectionRepository,
        credentials: CredentialStore,
        oauth: OAuthConnections,
        *,
        compatible_provider: ModelProvider | None = None,
    ) -> None:
        self.repo = repo
        self.credentials = credentials
        self.oauth = oauth
        self.compatible = compatible_provider or OpenAICompatibleProvider()
        self.chatgpt = ChatGPTResponsesProvider(
            self._oauth_token,
            metadata_repo=repo,
            connection_for_ref=self._connection,
            report_status=self._report_status,
        )
        self.gemini = GeminiNativeProvider(
            self._gemini_headers,
            metadata_repo=repo,
            connection_for_ref=self._connection,
            report_status=self._report_status,
        )

    def _report_status(self, secret_ref: str, http_status: int | None, reason: str) -> None:
        record = self._connection(secret_ref)
        if record is None:
            return
        if http_status == 200:
            updated = {
                **record,
                "status": "ready" if record.get("profile_ids") else "connected",
                "error": None,
            }
        else:
            if http_status == 401:
                status, error = "needs_auth", "authentication_required"
            elif http_status == 403:
                status, error = (
                    "error",
                    "user_not_eligible"
                    if reason == "subscription_sharing_user_not_eligible"
                    else "permission_denied",
                )
            elif http_status == 429:
                status, error = (
                    "error",
                    "usage_limit"
                    if reason == "subscription_sharing_usage_limit_exceeded"
                    else "rate_limited",
                )
            elif http_status == 503 and reason in {
                "subscription_sharing_usage_unavailable",
                "subscription_sharing_user_unavailable",
            }:
                status, error = "error", "usage_unavailable"
            elif http_status == 503:
                status, error = "error", "provider_unavailable"
            elif http_status == 400 and reason == "subscription_sharing_unsupported_capability":
                status, error = "error", "unsupported_capability"
            elif reason == "plan_usage_disabled":
                status, error = "error", "chatgpt_plan_usage_disabled"
            elif reason == "usage_limit":
                status, error = "error", "usage_limit"
            elif reason == "network_error":
                status, error = "error", "network_error"
            else:
                status, error = "error", "provider_unavailable"
            updated = {**record, "status": status, "error": error}
        self.repo.save_connection(str(record["connection_id"]), updated)

    @staticmethod
    def _status_from_error(exc: ProviderError) -> int | None:
        match = re.search(r"HTTP (\d{3})", str(exc))
        return int(match.group(1)) if match else None

    def _connection(self, secret_ref: str) -> dict[str, Any] | None:
        return next(
            (item for item in self.repo.list_connections() if item.get("secret_ref") == secret_ref),
            None,
        )

    async def _oauth_token(self, secret_ref: str) -> str:
        record = self._connection(secret_ref)
        if not record or record.get("provider") != "chatgpt":
            raise ProviderError("ChatGPT connection is unavailable")
        if record.get("error") == "revocation_unconfirmed":
            raise ProviderError("ChatGPT connection revocation is pending")
        if "chatgpt.tokens.use.direct" not in record.get("scopes", []):
            self._report_status(secret_ref, None, "plan_usage_disabled")
            raise ProviderError("ChatGPT plan usage is not enabled")
        return await self.oauth.access_token(record)

    async def _gemini_headers(self, secret_ref: str) -> dict[str, str]:
        record = self._connection(secret_ref)
        if record and record.get("provider") == "gemini" and record.get("auth_method") == "oauth":
            if record.get("error") == "revocation_unconfirmed":
                raise ProviderError("Gemini connection revocation is pending")
            token = await self.oauth.access_token(record)
            project_id = record.get("project_id")
            if not isinstance(project_id, str) or not project_id:
                raise ProviderError("Gemini Cloud project is missing")
            return {
                "Authorization": f"Bearer {token}",
                "x-goog-user-project": project_id,
            }
        api_key = (
            self.credentials.get(secret_ref)
            if secret_ref.startswith("OPERANT_CONNECTION_")
            else os.environ.get(secret_ref)
        )
        if not api_key:
            raise ProviderError("Gemini API key is unavailable")
        return {"x-goog-api-key": api_key}

    async def list_models(self, *, base_url: str, secret_ref: str) -> list[str]:
        record = self._connection(secret_ref)
        provider = record.get("provider") if record else None
        if provider == "chatgpt":
            return await self.chatgpt.list_models(base_url=base_url, secret_ref=secret_ref)
        if provider == "gemini":
            return await self.gemini.list_models(base_url=base_url, secret_ref=secret_ref)
        if secret_ref.startswith("OPERANT_CONNECTION_"):
            self.credentials.get(secret_ref)
        try:
            models = await self.compatible.list_models(base_url=base_url, secret_ref=secret_ref)
        except ProviderError as exc:
            self._report_status(secret_ref, self._status_from_error(exc), "discovery_failed")
            raise
        self._report_status(secret_ref, 200, "connected")
        return models

    def stream(
        self,
        *,
        snapshot: RoleSnapshot,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        if snapshot.provider == "chatgpt":
            return self.chatgpt.stream(snapshot=snapshot, messages=messages, tools=tools)
        if snapshot.provider == "gemini":
            return self.gemini.stream(snapshot=snapshot, messages=messages, tools=tools)
        if snapshot.secret_ref.startswith("OPERANT_CONNECTION_"):
            self.credentials.get(snapshot.secret_ref)
        return self._compatible_stream(snapshot=snapshot, messages=messages, tools=tools)

    async def _compatible_stream(
        self,
        *,
        snapshot: RoleSnapshot,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        try:
            async for event in self.compatible.stream(
                snapshot=snapshot, messages=messages, tools=tools
            ):
                yield event
        except ProviderError as exc:
            self._report_status(
                snapshot.secret_ref, self._status_from_error(exc), "inference_failed"
            )
            raise
        self._report_status(snapshot.secret_ref, 200, "connected")
