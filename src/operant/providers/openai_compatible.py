from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Sequence
from typing import Any
from urllib.parse import urlsplit

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


class ProviderError(RuntimeError):
    """Sanitized provider error that never contains request credentials."""

    def __init__(
        self,
        message: str = "",
        *,
        failure_stage: str | None = None,
        failure_category: str | None = None,
    ) -> None:
        super().__init__(message)
        self.failure_stage = failure_stage
        self.failure_category = failure_category


_PROVIDER_FAILURE_MESSAGES = {
    "timeout": "模型请求超时，请检查网络连接后再试。",
    "connect_timeout": "连接模型服务超时，请检查网络连接。",
    "read_timeout": "等待模型响应超时，请稍后再试。",
    "write_timeout": "发送模型请求超时，请检查网络连接。",
    "pool_timeout": "模型连接等待超时，请稍后再试。",
    "proxy_error": "代理连接失败，请检查代理设置。",
    "connection_error": "无法连接模型服务，请检查网络连接。",
    "protocol_error": "模型连接协议异常，请检查网络或代理设置。",
    "network_error": "模型连接中断，请检查网络连接。",
    "invalid_response": "模型响应格式异常，请稍后再试。",
}
_PROVIDER_FAILURE_STAGES = frozenset({"inference_transport", "inference_response"})


def provider_failure_payload(exc: Exception) -> dict[str, Any]:
    """Return only fixed, reviewed diagnostic data for persisted failure events."""
    payload: dict[str, Any] = {"error_type": type(exc).__name__}
    if not isinstance(exc, ProviderError):
        return payload
    stage = exc.failure_stage
    category = exc.failure_category
    if (
        not isinstance(stage, str)
        or stage not in _PROVIDER_FAILURE_STAGES
        or not isinstance(category, str)
        or category not in _PROVIDER_FAILURE_MESSAGES
    ):
        return payload
    payload["message"] = _PROVIDER_FAILURE_MESSAGES[category]
    payload["provider_failure"] = {"stage": stage, "category": category}
    return payload


class OpenAICompatibleProvider:
    def __init__(
        self,
        *,
        timeout_seconds: float = 120,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    async def list_models(self, *, base_url: str, secret_ref: str) -> list[str]:
        api_key = self._load_secret(secret_ref)
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds, transport=self.transport
            ) as client:
                response = await client.get(
                    f"{self._api_base_url(base_url)}/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            self._raise_for_status(response)
            body: Any = response.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise ProviderError(f"provider model discovery failed: {type(exc).__name__}") from exc
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise ProviderError("provider returned an invalid model list")
        model_ids: list[str] = []
        for item in body["data"]:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                model_ids.append(item["id"])
        return model_ids

    async def stream(
        self,
        *,
        snapshot: RoleSnapshot,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        api_key = self._load_secret(snapshot.secret_ref)
        payload: dict[str, Any] = {
            "model": snapshot.model_id,
            "messages": [self._message_payload(message) for message in messages],
            "stream": True,
        }
        if snapshot.budget.max_output_tokens is not None:
            # Chat Completions uses this portable spelling for current models.
            # The runtime also enforces the cumulative value from returned
            # usage; this request limit only prevents avoidable overshoot.
            payload["max_completion_tokens"] = snapshot.budget.max_output_tokens
        if snapshot.temperature is not None:
            payload["temperature"] = snapshot.temperature
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in tools
            ]
            payload["tool_choice"] = "auto"
        payload.update(self._effort_payload(snapshot))

        content_parts: list[str] = []
        tool_buffers: dict[int, dict[str, str]] = {}
        finish_reason: str | None = None
        usage: ModelUsage | None = None
        provider_request_id: str | None = None

        try:
            async with (
                httpx.AsyncClient(timeout=self.timeout_seconds, transport=self.transport) as client,
                client.stream(
                    "POST",
                    f"{self._api_base_url(snapshot.base_url)}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                ) as response,
            ):
                self._raise_for_status(response)
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line.removeprefix("data:").strip()
                    if data == "[DONE]":
                        break
                    chunk: Any = json.loads(data)
                    raw_request_id = chunk.get("id") if isinstance(chunk, dict) else None
                    if isinstance(raw_request_id, str) and len(raw_request_id) <= 300:
                        provider_request_id = raw_request_id
                    raw_usage = chunk.get("usage") if isinstance(chunk, dict) else None
                    if isinstance(raw_usage, dict):
                        prompt_details = raw_usage.get("prompt_tokens_details")
                        details = prompt_details if isinstance(prompt_details, dict) else {}
                        usage = ModelUsage(
                            prompt_tokens=self._nonnegative_int(raw_usage.get("prompt_tokens")),
                            completion_tokens=self._nonnegative_int(
                                raw_usage.get("completion_tokens")
                            ),
                            total_tokens=self._nonnegative_int(raw_usage.get("total_tokens")),
                            cache_read_tokens=self._nonnegative_int(details.get("cached_tokens")),
                            cache_write_tokens=self._nonnegative_int(
                                details.get("cache_write_tokens")
                                if "cache_write_tokens" in details
                                else raw_usage.get("cache_creation_input_tokens")
                            ),
                        )
                    choices = chunk.get("choices") if isinstance(chunk, dict) else None
                    if not isinstance(choices, list) or not choices:
                        continue
                    choice = choices[0]
                    if not isinstance(choice, dict):
                        continue
                    if isinstance(choice.get("finish_reason"), str):
                        finish_reason = choice["finish_reason"]
                    delta = choice.get("delta")
                    if not isinstance(delta, dict):
                        continue
                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        content_parts.append(content)
                        yield ProviderEvent(event_type="model.delta", delta=content)
                    self._merge_tool_call_deltas(tool_buffers, delta.get("tool_calls"))
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise ProviderError(f"provider stream failed: {type(exc).__name__}") from exc

        tool_calls = tuple(
            ToolCall(
                id=buffer.get("id") or f"tool_call_{index}",
                name=buffer.get("name") or "",
                arguments_json=buffer.get("arguments") or "{}",
            )
            for index, buffer in sorted(tool_buffers.items())
        )
        yield ProviderEvent(
            event_type="model.completed",
            response=ModelResponse(
                content="".join(content_parts) or None,
                tool_calls=tool_calls,
                finish_reason=finish_reason,
                usage=usage,
                provider_request_id=provider_request_id,
            ),
        )

    @staticmethod
    def _load_secret(secret_ref: str) -> str:
        value = os.environ.get(secret_ref)
        if not value:
            raise ProviderError(f"required secret reference is not set: {secret_ref}")
        return value

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(f"provider returned HTTP {response.status_code}") from exc

    @staticmethod
    def _effort_payload(snapshot: RoleSnapshot) -> dict[str, str]:
        if snapshot.provider_effort_parameter is None or snapshot.provider_effort_value is None:
            return {}
        return {snapshot.provider_effort_parameter: snapshot.provider_effort_value}

    @staticmethod
    def _api_base_url(base_url: str) -> str:
        normalized = base_url.rstrip("/")
        if urlsplit(normalized).path in {"", "/"}:
            return f"{normalized}/v1"
        return normalized

    @staticmethod
    def _message_payload(message: Message) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": message.role.value}
        if message.content is not None:
            payload["content"] = message.content
        if message.role is MessageRole.ASSISTANT and message.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": call.arguments_json,
                    },
                }
                for call in message.tool_calls
            ]
        if message.role is MessageRole.TOOL:
            payload["tool_call_id"] = message.tool_call_id
            if message.name:
                payload["name"] = message.name
        return payload

    @staticmethod
    def _merge_tool_call_deltas(buffers: dict[int, dict[str, str]], raw_calls: Any) -> None:
        if not isinstance(raw_calls, list):
            return
        for raw in raw_calls:
            if not isinstance(raw, dict):
                continue
            index = raw.get("index", 0)
            if not isinstance(index, int):
                continue
            buffer = buffers.setdefault(index, {"id": "", "name": "", "arguments": ""})
            if isinstance(raw.get("id"), str):
                buffer["id"] += raw["id"]
            function = raw.get("function")
            if not isinstance(function, dict):
                continue
            if isinstance(function.get("name"), str):
                buffer["name"] += function["name"]
            if isinstance(function.get("arguments"), str):
                buffer["arguments"] += function["arguments"]

    @staticmethod
    def _nonnegative_int(value: Any) -> int | None:
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        return None
