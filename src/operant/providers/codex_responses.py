"""Codex account Responses adapter; Operant retains its own Agent Loop and tools."""

from __future__ import annotations

import hashlib
import json
import unicodedata
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any, TypeGuard

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
from operant.model_connections.codex import CodexConnections
from operant.model_connections.oauth import ConnectionRepository, OAuthError
from operant.providers.openai_compatible import ProviderError

_BASE_URL = "https://chatgpt.com/backend-api/codex"
# This is the reviewed wire identity, independent of the installed login CLI.
_CLIENT_VERSION = "0.159.0"


def _model_id(value: Any) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 200
        and not any(char.isspace() or unicodedata.category(char).startswith("C") for char in value)
    )


def _failure_reason(response: httpx.Response) -> str:
    if response.status_code != 429 or len(response.content) > 16_384:
        return "inference_failed"
    try:
        body = response.json()
    except ValueError:
        return "inference_failed"
    if not isinstance(body, dict):
        return "inference_failed"
    error = body.get("error")
    if not isinstance(error, dict):
        return "inference_failed"
    signals = [value for value in (error.get("type"), error.get("code")) if isinstance(value, str)]
    if any(value in {"usage_limit_reached", "usage_limit"} for value in signals):
        return "usage_limit"
    if "rate_limit_exceeded" in signals:
        return "rate_limit"
    return "inference_failed"


def _reasoning_key(
    secret_ref: str, profile_id: str, model_id: str, messages: Sequence[Message]
) -> str:
    body = json.dumps(
        [secret_ref, profile_id, model_id, [item.model_dump(mode="json") for item in messages]],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode()).hexdigest()


class CodexResponsesProvider:
    def __init__(
        self,
        connections: CodexConnections,
        repo: ConnectionRepository,
        connection_for_ref: Callable[[str], dict[str, Any] | None],
        report_status: Callable[[str, int | None, str], None],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.connections = connections
        self.repo = repo
        self.connection_for_ref = connection_for_ref
        self.report_status = report_status
        self.transport = transport
        # Only encrypted reasoning is retained in private provider metadata;
        # it never appears in messages or public connection projections.
        self._reasoning: dict[str, list[dict[str, str]]] = {}

    def _report(
        self, record: dict[str, Any], secret_ref: str, status: int | None, reason: str
    ) -> None:
        connection_id = str(record["connection_id"])
        fd = self.connections.credentials.try_acquire_lock(
            "codex-refresh-" + connection_id.replace("-", "")
        )
        if fd is None:
            return
        try:
            current = self.repo.get_connection(connection_id)
            if (
                current is None
                or current.get("provider") != "codex"
                or current.get("secret_ref") != secret_ref
                or current.get("auth_generation") != record.get("auth_generation")
                or current.get("error") == "revocation_unconfirmed"
            ):
                return
            self.report_status(secret_ref, status, reason)
        finally:
            self.connections.credentials.release_lock(fd)

    async def _authorization(self, secret_ref: str) -> tuple[str, str, dict[str, Any]]:
        record = self.connection_for_ref(secret_ref)
        if record is None or record.get("provider") != "codex":
            raise ProviderError("Codex connection is unavailable")
        try:
            token, account_id = await self.connections.access_token(record)
        except OAuthError as exc:
            if str(exc) in {"Codex 连接需要重新登录", "Codex 登录已过期，请重新登录"}:
                raise ProviderError("Codex connection needs sign-in") from exc
            raise ProviderError("Codex authorization could not be refreshed; try again") from exc
        return token, account_id, record

    @staticmethod
    def _headers(token: str, account_id: str, *, lite: bool = False) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {token}",
            "ChatGPT-Account-Id": account_id,
            "Originator": "codex_cli_rs",
            "Version": _CLIENT_VERSION,
            "User-Agent": f"codex_cli_rs/{_CLIENT_VERSION}",
            "Accept": "text/event-stream",
            "session-id": str(uuid.uuid4()),
        }
        if lite:
            headers["x-openai-internal-codex-responses-lite"] = "true"
        return headers

    async def list_models(self, *, base_url: str, secret_ref: str) -> list[str]:
        token, account_id, record = await self._authorization(secret_ref)
        if base_url.rstrip("/") != _BASE_URL:
            raise ProviderError("Codex connection uses the official upstream")
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=30) as client:
                response = await client.get(
                    f"{_BASE_URL}/models",
                    params={"client_version": _CLIENT_VERSION},
                    headers=self._headers(token, account_id),
                )
            if response.status_code != 200:
                self._report(record, secret_ref, response.status_code, _failure_reason(response))
                raise ProviderError(f"Codex model discovery returned HTTP {response.status_code}")
            body = response.json()
            models = body.get("models") if isinstance(body, dict) else None
            if not isinstance(models, list):
                raise ProviderError("Codex model catalog is invalid")
            names: dict[str, str] = {}
            capabilities: dict[str, dict[str, Any]] = {}
            for item in models:
                if (
                    not isinstance(item, dict)
                    or item.get("visibility") != "list"
                    or item.get("supported_in_api") is False
                ):
                    continue
                slug = item.get("slug")
                if not _model_id(slug):
                    continue
                modalities = item.get("input_modalities")
                if not isinstance(modalities, list) or "text" not in modalities:
                    continue
                name = item.get("display_name")
                clean_name = (
                    "".join(
                        char for char in name if not unicodedata.category(char).startswith("C")
                    ).strip()[:128]
                    if isinstance(name, str)
                    else ""
                )
                levels = item.get("supported_reasoning_levels")
                effort_values: list[str] = []
                if isinstance(levels, list):
                    for level in levels:
                        value = level.get("effort") if isinstance(level, dict) else level
                        if (
                            isinstance(value, str)
                            and value
                            in {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
                            and value not in effort_values
                        ):
                            effort_values.append(value)
                if not any(level in effort_values for level in ("low", "medium", "high")):
                    continue
                names[slug] = clean_name or slug
                context_window = item.get("context_window")
                capabilities[slug] = {
                    "reasoning_levels": effort_values,
                    "default_reasoning_level": item.get("default_reasoning_level")
                    if item.get("default_reasoning_level") in effort_values
                    else None,
                    "responses_lite": item.get("use_responses_lite") is True,
                    "context_window": context_window
                    if isinstance(context_window, int) and 0 < context_window <= 2_000_000
                    else None,
                }
            if not names:
                raise ProviderError("Codex model catalog has no selectable models")
            connection_id = str(record["connection_id"])
            fd = self.connections.credentials.try_acquire_lock(
                "codex-refresh-" + connection_id.replace("-", "")
            )
            if fd is None:
                raise ProviderError("Codex connection is being updated")
            try:
                current = self.repo.get_connection(connection_id)
                if (
                    current is None
                    or current.get("auth_generation") != record.get("auth_generation")
                    or current.get("error") == "revocation_unconfirmed"
                ):
                    raise ProviderError("Codex connection changed during discovery")
                self.repo.save_connection(
                    connection_id,
                    {**current, "model_names": names, "model_capabilities": capabilities},
                )
            finally:
                self.connections.credentials.release_lock(fd)
            self._report(record, secret_ref, 200, "connected")
            return list(names)
        except (httpx.HTTPError, ValueError) as exc:
            self._report(record, secret_ref, None, "network_error")
            raise ProviderError("Codex model discovery failed") from exc

    def _input(self, snapshot: RoleSnapshot, messages: Sequence[Message]) -> list[dict[str, Any]]:
        input_items: list[dict[str, Any]] = []
        for index, message in enumerate(messages):
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
            elif message.role is MessageRole.ASSISTANT:
                key = _reasoning_key(
                    snapshot.secret_ref,
                    snapshot.model_profile_id,
                    snapshot.model_id,
                    messages[: index + 1],
                )
                saved = self._reasoning.get(key)
                if saved is None:
                    metadata = self.repo.get_provider_metadata("codex-reasoning:" + key)
                    items = metadata.get("items") if metadata else None
                    saved = (
                        [
                            item
                            for item in items
                            if isinstance(item, dict)
                            and item.get("type") == "reasoning"
                            and isinstance(item.get("encrypted_content"), str)
                        ]
                        if isinstance(items, list)
                        else []
                    )
                input_items.extend(saved)
                if message.content:
                    input_items.append(
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": message.content}],
                        }
                    )
                for call in message.tool_calls:
                    input_items.append(
                        {
                            "type": "function_call",
                            "call_id": call.id,
                            "name": call.name,
                            "arguments": call.arguments_json,
                        }
                    )
            else:
                input_items.append(
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": message.content or ""}],
                    }
                )
        return input_items

    async def stream(
        self,
        *,
        snapshot: RoleSnapshot,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        token, account_id, record = await self._authorization(snapshot.secret_ref)
        if snapshot.base_url.rstrip("/") != _BASE_URL:
            raise ProviderError("Codex profile uses an unexpected upstream")
        if snapshot.model_id not in record.get("model_ids", []):
            raise ProviderError("Codex model must be selected from discovery")
        capabilities = record.get("model_capabilities", {}).get(snapshot.model_id, {})
        effort = snapshot.provider_effort_value or snapshot.effort.value
        levels = capabilities.get("reasoning_levels", [])
        if levels and effort not in levels:
            raise ProviderError("Codex model does not support this reasoning level")
        lite = bool(capabilities.get("responses_lite"))
        instructions = "\n\n".join(
            item.content or "" for item in messages if item.role is MessageRole.SYSTEM
        )
        input_items = self._input(snapshot, messages)
        tool_defs = [
            {
                "type": "function",
                "name": item.name,
                "description": item.description,
                "parameters": item.parameters,
            }
            for item in tools
        ]
        payload: dict[str, Any] = {
            "model": snapshot.model_id,
            "input": input_items,
            "stream": True,
            "store": False,
            "parallel_tool_calls": False,
            "tool_choice": "auto",
            "reasoning": {"effort": effort, "summary": "auto"},
            "include": ["reasoning.encrypted_content"],
        }
        if lite:
            payload["input"] = [
                {"type": "additional_tools", "role": "developer", "tools": tool_defs},
                *(
                    [
                        {
                            "type": "message",
                            "role": "developer",
                            "content": [{"type": "input_text", "text": instructions}],
                        }
                    ]
                    if instructions
                    else []
                ),
                *input_items,
            ]
            payload["reasoning"]["context"] = "all_turns"
        else:
            payload["instructions"] = instructions
            payload["tools"] = tool_defs
        text_parts: list[str] = []
        calls: dict[str, ToolCall] = {}
        reasoning_items: list[dict[str, str]] = []
        completed: dict[str, Any] | None = None
        try:
            async with (
                httpx.AsyncClient(transport=self.transport, timeout=120) as client,
                client.stream(
                    "POST",
                    f"{_BASE_URL}/responses",
                    headers=self._headers(token, account_id, lite=lite),
                    json=payload,
                ) as response,
            ):
                if response.status_code != 200:
                    self._report(
                        record,
                        snapshot.secret_ref,
                        response.status_code,
                        _failure_reason(response),
                    )
                    raise ProviderError(f"Codex inference returned HTTP {response.status_code}")
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    event = json.loads(data)
                    if not isinstance(event, dict):
                        raise ProviderError("Codex response event is invalid")
                    event_type = event.get("type")
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
                        elif isinstance(item, dict) and item.get("type") == "function_call":
                            call_id, name, arguments = (
                                item.get("call_id"),
                                item.get("name"),
                                item.get("arguments"),
                            )
                            if (
                                not isinstance(call_id, str)
                                or not call_id
                                or not isinstance(name, str)
                                or not name
                                or not isinstance(arguments, str)
                                or not arguments
                            ):
                                raise ProviderError("Codex tool call is invalid")
                            if name not in {tool.name for tool in tools}:
                                raise ProviderError("Codex returned an unknown tool")
                            calls[call_id] = ToolCall(
                                id=call_id, name=name, arguments_json=arguments
                            )
                    elif event_type == "response.completed":
                        value = event.get("response")
                        if isinstance(value, dict):
                            completed = value
                    elif event_type in {"response.failed", "response.incomplete", "error"}:
                        self._report(record, snapshot.secret_ref, None, "inference_failed")
                        raise ProviderError(f"Codex inference ended with {event_type}")
        except (httpx.HTTPError, ValueError) as exc:
            self._report(record, snapshot.secret_ref, None, "network_error")
            raise ProviderError("Codex inference stream failed") from exc
        if completed is None or completed.get("status", "completed") != "completed":
            self._report(record, snapshot.secret_ref, None, "stream_incomplete")
            raise ProviderError("Codex inference ended without response.completed")
        for item in completed.get("output", []):
            if not isinstance(item, dict):
                continue
            if item.get("type") == "reasoning" and not reasoning_items:
                encrypted = item.get("encrypted_content")
                if isinstance(encrypted, str) and encrypted:
                    reasoning_item = {"type": "reasoning", "encrypted_content": encrypted}
                    if isinstance(item.get("id"), str):
                        reasoning_item["id"] = item["id"]
                    reasoning_items.append(reasoning_item)
            if item.get("type") == "message" and not text_parts:
                for part in item.get("content", []):
                    if isinstance(part, dict) and part.get("type") == "output_text":
                        value = part.get("text")
                        if isinstance(value, str) and value:
                            text_parts.append(value)
                            yield ProviderEvent(event_type="model.delta", delta=value)
            if item.get("type") == "function_call" and item.get("call_id") not in calls:
                call_id, name, arguments = (
                    item.get("call_id"),
                    item.get("name"),
                    item.get("arguments"),
                )
                if (
                    not isinstance(call_id, str)
                    or not call_id
                    or not isinstance(name, str)
                    or not name
                    or not isinstance(arguments, str)
                    or not arguments
                ):
                    raise ProviderError("Codex tool call is invalid")
                if name not in {tool.name for tool in tools}:
                    raise ProviderError("Codex returned an unknown tool")
                calls[call_id] = ToolCall(id=call_id, name=name, arguments_json=arguments)
        if reasoning_items:
            if len(json.dumps(reasoning_items)) > 262_144:
                raise ProviderError("Codex encrypted reasoning exceeds the accepted size")
            key = _reasoning_key(
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
            )
            self._reasoning[key] = reasoning_items
            connection_id = str(record["connection_id"])
            fd = self.connections.credentials.try_acquire_lock(
                "codex-refresh-" + connection_id.replace("-", "")
            )
            if fd is not None:
                try:
                    current = self.repo.get_connection(connection_id)
                    if (
                        current is not None
                        and current.get("auth_generation") == record.get("auth_generation")
                        and current.get("error") != "revocation_unconfirmed"
                    ):
                        self.repo.save_provider_metadata(
                            "codex-reasoning:" + key,
                            {
                                "provider": "codex",
                                "connection_id": connection_id,
                                "items": reasoning_items,
                            },
                        )
                finally:
                    self.connections.credentials.release_lock(fd)
        usage_body = completed.get("usage")
        usage = None
        if isinstance(usage_body, dict):
            input_tokens, output_tokens = (
                usage_body.get("input_tokens"),
                usage_body.get("output_tokens"),
            )
            details = usage_body.get("input_tokens_details")
            usage = ModelUsage(
                prompt_tokens=input_tokens if isinstance(input_tokens, int) else None,
                completion_tokens=output_tokens if isinstance(output_tokens, int) else None,
                total_tokens=usage_body["total_tokens"]
                if isinstance(usage_body.get("total_tokens"), int)
                else (
                    input_tokens + output_tokens
                    if isinstance(input_tokens, int) and isinstance(output_tokens, int)
                    else None
                ),
                cache_read_tokens=details.get("cached_tokens")
                if isinstance(details, dict) and isinstance(details.get("cached_tokens"), int)
                else None,
            )
        self._report(record, snapshot.secret_ref, 200, "connected")
        response_id = completed.get("id")
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
