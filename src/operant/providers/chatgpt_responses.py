from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any, Protocol

import httpx

from operant.domain.messages import (
    Message,
    MessageRole,
    ModelResponse,
    ModelUsage,
    ProviderEvent,
    ToolCall,
    ToolDefinition,
)
from operant.domain.models import RoleSnapshot
from operant.providers.openai_compatible import ProviderError

_KNOWN_ERROR_CODES = frozenset(
    {
        "subscription_sharing_user_not_eligible",
        "subscription_sharing_usage_limit_exceeded",
        "subscription_sharing_usage_unavailable",
        "subscription_sharing_unsupported_capability",
        "subscription_sharing_route_not_supported",
        "subscription_sharing_invalid_user",
        "chatpass_v2_scope_not_authorized",
        "chatpass_v2_invalid_authorization_context",
        "subscription_sharing_user_unavailable",
    }
)


def _safe_error_code(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) and code in _KNOWN_ERROR_CODES else None


def _http_error_code(body: bytes) -> str | None:
    if len(body) > 65536:
        return None
    try:
        return _safe_error_code(json.loads(body))
    except (ValueError, UnicodeDecodeError):
        return None


def _display_name(value: Any, model_id: str) -> str:
    if not isinstance(value, str):
        return model_id
    name = "".join(char for char in value if not unicodedata.category(char).startswith("C"))
    return name.strip()[:128].strip() or model_id


class ProviderMetadataRepository(Protocol):
    def get_provider_metadata(self, key: str) -> dict[str, Any] | None: ...
    def save_provider_metadata(self, key: str, record: dict[str, Any]) -> None: ...
    def save_connection(self, connection_id: str, record: dict[str, Any]) -> None: ...


class ChatGPTResponsesProvider:
    """Responses HTTP adapter for an authorized ChatGPT plan connection."""

    def __init__(
        self,
        token_for_ref: Any,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        metadata_repo: ProviderMetadataRepository | None = None,
        connection_for_ref: Callable[[str], dict[str, Any] | None] | None = None,
        report_status: Callable[[str, int | None, str], None] | None = None,
    ) -> None:
        self.token_for_ref = token_for_ref
        self.transport = transport
        self.metadata_repo = metadata_repo
        self.connection_for_ref = connection_for_ref
        self.report_status = report_status

    def _report(self, secret_ref: str, status: int | None, reason: str) -> None:
        if self.report_status:
            self.report_status(secret_ref, status, reason)

    @staticmethod
    def _prefix_key(
        secret_ref: str, profile_id: str, model_id: str, messages: Sequence[Message]
    ) -> str:
        canonical = json.dumps(
            {
                "secret_ref": secret_ref,
                "profile_id": profile_id,
                "model_id": model_id,
                "messages": [message.model_dump(mode="json") for message in messages],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return f"chatgpt-reasoning:{hashlib.sha256(canonical.encode()).hexdigest()}"

    def _reasoning_for_prefix(
        self, secret_ref: str, profile_id: str, model_id: str, prefix: Sequence[Message]
    ) -> list[dict[str, str]]:
        if self.metadata_repo is None:
            return []
        record = self.metadata_repo.get_provider_metadata(
            self._prefix_key(secret_ref, profile_id, model_id, prefix)
        )
        items = record.get("items") if record else None
        if not isinstance(items, list):
            return []
        return [
            item
            for item in items
            if isinstance(item, dict)
            and item.get("type") == "reasoning"
            and isinstance(item.get("encrypted_content"), str)
        ]

    def _save_reasoning(
        self,
        secret_ref: str,
        profile_id: str,
        model_id: str,
        prefix: Sequence[Message],
        items: list[dict[str, str]],
    ) -> None:
        if not items or self.metadata_repo is None or self.connection_for_ref is None:
            return
        if len(json.dumps(items)) > 262144:
            raise ProviderError("ChatGPT encrypted reasoning metadata exceeds the accepted size")
        connection = self.connection_for_ref(secret_ref)
        if not connection:
            return
        self.metadata_repo.save_provider_metadata(
            self._prefix_key(secret_ref, profile_id, model_id, prefix),
            {"provider": "chatgpt", "connection_id": connection["connection_id"], "items": items},
        )

    async def list_models(self, *, base_url: str, secret_ref: str) -> list[str]:
        token = await self.token_for_ref(secret_ref)
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=30) as client:
                response = await client.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {token}"},
                )
            if response.status_code != 200:
                self._report(
                    secret_ref,
                    response.status_code,
                    _http_error_code(response.content) or "discovery_failed",
                )
                raise ProviderError(f"ChatGPT model discovery returned HTTP {response.status_code}")
            body = response.json()
            models = body.get("models") if isinstance(body, dict) else None
            if not isinstance(models, list):
                self._report(secret_ref, None, "discovery_failed")
                raise ProviderError("ChatGPT model catalog is invalid")
            names: dict[str, str] = {}
            for item in models:
                if not isinstance(item, dict) or item.get("visibility") != "list":
                    continue
                slug = item.get("slug")
                if (
                    not isinstance(slug, str)
                    or not slug
                    or len(slug) > 200
                    or any(
                        char.isspace() or unicodedata.category(char).startswith("C")
                        for char in slug
                    )
                ):
                    continue
                names.setdefault(slug, _display_name(item.get("display_name"), slug))
            if self.metadata_repo is not None and self.connection_for_ref is not None:
                connection = self.connection_for_ref(secret_ref)
                if connection is not None:
                    self.metadata_repo.save_connection(
                        str(connection["connection_id"]), {**connection, "model_names": names}
                    )
            self._report(secret_ref, 200, "connected")
            return list(names)
        except (httpx.HTTPError, ValueError) as exc:
            self._report(secret_ref, None, "network_error")
            raise ProviderError("ChatGPT model discovery failed") from exc

    async def stream(
        self,
        *,
        snapshot: RoleSnapshot,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        token = await self.token_for_ref(snapshot.secret_ref)
        instructions = "\n\n".join(
            message.content or "" for message in messages if message.role is MessageRole.SYSTEM
        )
        input_items: list[dict[str, Any]] = []
        for message_index, message in enumerate(messages):
            if message.role is MessageRole.SYSTEM:
                continue
            if message.role is MessageRole.TOOL:
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": message.tool_call_id,
                        "output": message.content or "",
                    }
                )
                continue
            if message.role is MessageRole.ASSISTANT:
                input_items.extend(
                    self._reasoning_for_prefix(
                        snapshot.secret_ref,
                        snapshot.model_profile_id,
                        snapshot.model_id,
                        messages[: message_index + 1],
                    )
                )
                if message.content:
                    input_items.append({"role": "assistant", "content": message.content})
                for call in message.tool_calls:
                    input_items.append(
                        {
                            "type": "function_call",
                            "call_id": call.id,
                            "name": call.name,
                            "namespace": "operant",
                            "arguments": call.arguments_json,
                        }
                    )
                continue
            input_items.append({"role": "user", "content": message.content or ""})
        payload: dict[str, Any] = {
            "model": snapshot.model_id,
            "input": input_items,
            "store": False,
            "stream": True,
        }
        if instructions:
            payload["instructions"] = instructions
        if tools:
            payload["tools"] = [
                {
                    "type": "namespace",
                    "name": "operant",
                    "description": "Tools available through the Operant Action Gateway",
                    "tools": [
                        {
                            "type": "function",
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": tool.parameters,
                        }
                        for tool in tools
                    ],
                }
            ]
        text_parts: list[str] = []
        calls: dict[str, ToolCall] = {}
        reasoning_items: list[dict[str, str]] = []
        completed: dict[str, Any] | None = None
        try:
            async with (
                httpx.AsyncClient(transport=self.transport, timeout=120) as client,
                client.stream(
                    "POST",
                    "https://api.openai.com/v1/responses",
                    headers={"Authorization": f"Bearer {token}"},
                    json=payload,
                ) as response,
            ):
                if response.status_code != 200:
                    self._report(
                        snapshot.secret_ref,
                        response.status_code,
                        _http_error_code(await response.aread()) or "inference_failed",
                    )
                    raise ProviderError(f"ChatGPT inference returned HTTP {response.status_code}")
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    event = json.loads(data)
                    event_type = event.get("type") if isinstance(event, dict) else None
                    if event_type == "response.output_text.delta":
                        delta = event.get("delta")
                        if isinstance(delta, str) and delta:
                            text_parts.append(delta)
                            yield ProviderEvent(event_type="model.delta", delta=delta)
                    elif event_type == "response.output_item.done":
                        item = event.get("item")
                        if isinstance(item, dict) and item.get("type") == "reasoning":
                            encrypted = item.get("encrypted_content")
                            if isinstance(encrypted, str) and encrypted:
                                reasoning_item = {
                                    "type": "reasoning",
                                    "encrypted_content": encrypted,
                                }
                                if isinstance(item.get("id"), str):
                                    reasoning_item["id"] = item["id"]
                                reasoning_items.append(reasoning_item)
                        if isinstance(item, dict) and item.get("type") == "function_call":
                            if item.get("namespace", "operant") != "operant":
                                raise ProviderError(
                                    "ChatGPT returned an unrecognized tool namespace"
                                )
                            call_id = item.get("call_id")
                            name = item.get("name")
                            arguments = item.get("arguments")
                            if all(isinstance(v, str) for v in (call_id, name, arguments)):
                                assert isinstance(call_id, str)
                                calls[call_id] = ToolCall(
                                    id=call_id,
                                    name=str(name),
                                    arguments_json=str(arguments),
                                )
                    elif event_type == "response.completed":
                        value = event.get("response")
                        if isinstance(value, dict):
                            completed = value
                    elif event_type in {"response.failed", "response.incomplete", "error"}:
                        response_body = event.get("response") if isinstance(event, dict) else None
                        code = _safe_error_code(response_body) or _safe_error_code(event)
                        code_status = {
                            "subscription_sharing_user_not_eligible": 403,
                            "subscription_sharing_usage_limit_exceeded": 429,
                            "subscription_sharing_usage_unavailable": 503,
                            "subscription_sharing_user_unavailable": 503,
                            "subscription_sharing_invalid_user": 401,
                            "subscription_sharing_unsupported_capability": 400,
                            "subscription_sharing_route_not_supported": 403,
                            "chatpass_v2_scope_not_authorized": 403,
                            "chatpass_v2_invalid_authorization_context": 403,
                        }
                        self._report(
                            snapshot.secret_ref,
                            code_status.get(code) if code is not None else None,
                            code or "inference_failed",
                        )
                        raise ProviderError(f"ChatGPT inference ended with {event_type}")
        except (httpx.HTTPError, ValueError) as exc:
            self._report(snapshot.secret_ref, None, "network_error")
            raise ProviderError("ChatGPT inference stream failed") from exc
        if completed is None:
            self._report(snapshot.secret_ref, None, "stream_incomplete")
            raise ProviderError("ChatGPT inference ended without response.completed")
        if completed.get("status", "completed") != "completed":
            self._report(snapshot.secret_ref, None, "stream_incomplete")
            raise ProviderError("ChatGPT inference did not complete")
        if not reasoning_items:
            for item in completed.get("output", []):
                if isinstance(item, dict) and item.get("type") == "reasoning":
                    encrypted = item.get("encrypted_content")
                    if isinstance(encrypted, str) and encrypted:
                        reasoning_item = {"type": "reasoning", "encrypted_content": encrypted}
                        if isinstance(item.get("id"), str):
                            reasoning_item["id"] = item["id"]
                        reasoning_items.append(reasoning_item)
        if not text_parts:
            for item in completed.get("output", []):
                if not isinstance(item, dict) or item.get("type") != "message":
                    continue
                for part in item.get("content", []):
                    if isinstance(part, dict) and part.get("type") == "output_text":
                        value = part.get("text")
                        if isinstance(value, str) and value:
                            text_parts.append(value)
                            yield ProviderEvent(event_type="model.delta", delta=value)
        usage_body = completed.get("usage")
        usage = None
        if isinstance(usage_body, dict):
            input_tokens = usage_body.get("input_tokens")
            output_tokens = usage_body.get("output_tokens")
            usage = ModelUsage(
                prompt_tokens=input_tokens if isinstance(input_tokens, int) else None,
                completion_tokens=output_tokens if isinstance(output_tokens, int) else None,
                total_tokens=(input_tokens + output_tokens)
                if isinstance(input_tokens, int) and isinstance(output_tokens, int)
                else None,
            )
        response_id = completed.get("id")
        self._save_reasoning(
            snapshot.secret_ref,
            snapshot.model_profile_id,
            snapshot.model_id,
            [
                *messages,
                Message(
                    role=MessageRole.ASSISTANT,
                    content="".join(text_parts) or None,
                    tool_calls=tuple(calls.values()),
                ),
            ],
            reasoning_items,
        )
        self._report(snapshot.secret_ref, 200, "connected")
        yield ProviderEvent(
            event_type="model.completed",
            response=ModelResponse(
                content="".join(text_parts) or None,
                tool_calls=tuple(calls.values()),
                finish_reason="tool_calls" if calls else "stop",
                usage=usage,
                provider_request_id=response_id if isinstance(response_id, str) else None,
            ),
        )
