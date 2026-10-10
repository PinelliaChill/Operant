from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any, Protocol
from urllib.parse import quote

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


class ProviderMetadataRepository(Protocol):
    def get_provider_metadata(self, key: str) -> dict[str, Any] | None: ...
    def save_provider_metadata(self, key: str, record: dict[str, Any]) -> None: ...
    def save_connection(self, connection_id: str, record: dict[str, Any]) -> None: ...


def _inference_failure_diagnostic(exc: httpx.HTTPError | ValueError) -> tuple[str, str]:
    if isinstance(exc, httpx.ConnectTimeout):
        category = "connect_timeout"
    elif isinstance(exc, httpx.ReadTimeout):
        category = "read_timeout"
    elif isinstance(exc, httpx.WriteTimeout):
        category = "write_timeout"
    elif isinstance(exc, httpx.PoolTimeout):
        category = "pool_timeout"
    elif isinstance(exc, httpx.TimeoutException):
        category = "timeout"
    elif isinstance(exc, httpx.ProxyError):
        category = "proxy_error"
    elif isinstance(exc, httpx.ConnectError):
        category = "connection_error"
    elif isinstance(exc, httpx.ProtocolError):
        category = "protocol_error"
    elif isinstance(exc, (httpx.DecodingError, ValueError)):
        return "inference_response", "invalid_response"
    else:
        category = "network_error"
    return "inference_transport", category


def _display_name(value: Any, model_id: str) -> str:
    if not isinstance(value, str):
        return model_id
    name = "".join(char for char in value if not unicodedata.category(char).startswith("C"))
    return name.strip()[:128].strip() or model_id


class GeminiNativeProvider:
    """Gemini listModels and streamGenerateContent adapter."""

    def __init__(
        self,
        auth_for_ref: Any,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        metadata_repo: ProviderMetadataRepository | None = None,
        connection_for_ref: Callable[[str], dict[str, Any] | None] | None = None,
        report_status: Callable[[str, int | None, str], None] | None = None,
    ) -> None:
        self.auth_for_ref = auth_for_ref
        self.transport = transport
        self.metadata_repo = metadata_repo
        self.connection_for_ref = connection_for_ref
        self.report_status = report_status
        self._thought_signatures: dict[str, str] = {}
        self._text_segments: dict[str, list[dict[str, Any]]] = {}

    def _report(self, secret_ref: str, status: int | None, reason: str) -> None:
        if self.report_status:
            self.report_status(secret_ref, status, reason)

    @staticmethod
    def _counter(value: Any) -> int | None:
        return (
            value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
        )

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
        return f"gemini-text:{hashlib.sha256(canonical.encode()).hexdigest()}"

    def _stored_text_parts(
        self,
        secret_ref: str,
        profile_id: str,
        model_id: str,
        prefix: Sequence[Message],
        text: str,
    ) -> list[dict[str, Any]]:
        key = self._prefix_key(secret_ref, profile_id, model_id, prefix)
        record = self.metadata_repo.get_provider_metadata(key) if self.metadata_repo else None
        segments = record.get("segments") if record else self._text_segments.get(key)
        if not isinstance(segments, list) or sum(
            part.get("length", -1) for part in segments if isinstance(part, dict)
        ) != len(text):
            return [{"text": text}] if text else []
        parts: list[dict[str, Any]] = []
        offset = 0
        for segment in segments:
            if not isinstance(segment, dict) or not isinstance(segment.get("length"), int):
                return [{"text": text}]
            length = segment["length"]
            if length < 0:
                return [{"text": text}]
            part: dict[str, Any] = {"text": text[offset : offset + length]}
            signature = segment.get("thought_signature")
            if isinstance(signature, str) and signature:
                if len(signature) > 65536:
                    raise ProviderError("Gemini text signature metadata is too large")
                part["thoughtSignature"] = signature
            parts.append(part)
            offset += length
        return parts

    def _save_text_parts(
        self,
        secret_ref: str,
        profile_id: str,
        model_id: str,
        prefix: Sequence[Message],
        segments: list[dict[str, Any]],
    ) -> None:
        if not any(segment.get("thought_signature") for segment in segments):
            return
        if len(json.dumps(segments)) > 131072:
            raise ProviderError("Gemini text signature metadata exceeds the accepted size")
        key = self._prefix_key(secret_ref, profile_id, model_id, prefix)
        connection = self.connection_for_ref(secret_ref) if self.connection_for_ref else None
        if self.metadata_repo and connection:
            self.metadata_repo.save_provider_metadata(
                key,
                {
                    "provider": "gemini",
                    "connection_id": connection["connection_id"],
                    "segments": segments,
                },
            )
        else:
            self._text_segments[key] = segments

    def _metadata_key(self, secret_ref: str, profile_id: str, call_id: str) -> str | None:
        if self.connection_for_ref is None:
            return None
        connection = self.connection_for_ref(secret_ref)
        if not connection:
            return None
        connection_id = connection.get("connection_id")
        if not isinstance(connection_id, str):
            return None
        digest = hashlib.sha256(f"{connection_id}\0{profile_id}\0{call_id}".encode()).hexdigest()
        return f"gemini:{digest}"

    def _signature(self, secret_ref: str, profile_id: str, call_id: str) -> str | None:
        key = self._metadata_key(secret_ref, profile_id, call_id)
        if key and self.metadata_repo is not None:
            record = self.metadata_repo.get_provider_metadata(key)
            if record and isinstance(record.get("thought_signature"), str):
                return str(record["thought_signature"])
        return self._thought_signatures.get(f"{profile_id}\0{call_id}")

    def _save_signature(
        self, secret_ref: str, profile_id: str, call_id: str, signature: str
    ) -> None:
        if len(signature) > 65536:
            raise ProviderError("Gemini tool signature exceeds the accepted size")
        key = self._metadata_key(secret_ref, profile_id, call_id)
        if key and self.metadata_repo is not None:
            connection = self.connection_for_ref(secret_ref) if self.connection_for_ref else None
            assert connection is not None
            self.metadata_repo.save_provider_metadata(
                key,
                {
                    "provider": "gemini",
                    "connection_id": connection["connection_id"],
                    "tool_call_id": call_id,
                    "thought_signature": signature,
                },
            )
        else:
            self._thought_signatures[f"{profile_id}\0{call_id}"] = signature

    async def list_models(self, *, base_url: str, secret_ref: str) -> list[str]:
        headers = await self.auth_for_ref(secret_ref)
        try:
            names: dict[str, str] = {}
            page_token: str | None = None
            async with httpx.AsyncClient(transport=self.transport, timeout=30) as client:
                for _ in range(20):
                    response = await client.get(
                        "https://generativelanguage.googleapis.com/v1/models",
                        headers=headers,
                        params={"pageToken": page_token} if page_token else None,
                    )
                    if response.status_code != 200:
                        self._report(secret_ref, response.status_code, "discovery_failed")
                        raise ProviderError(
                            f"Gemini model discovery returned HTTP {response.status_code}"
                        )
                    body = response.json()
                    models = body.get("models") if isinstance(body, dict) else None
                    if not isinstance(models, list):
                        self._report(secret_ref, None, "discovery_failed")
                        raise ProviderError("Gemini model catalog is invalid")
                    for item in models:
                        if not isinstance(item, dict):
                            continue
                        name = item.get("name")
                        methods = item.get("supportedGenerationMethods")
                        if (
                            not isinstance(name, str)
                            or not name.startswith("models/")
                            or not isinstance(methods, list)
                            or "generateContent" not in methods
                        ):
                            continue
                        model_id = name.removeprefix("models/")
                        if (
                            not model_id
                            or len(model_id) > 200
                            or "/" in model_id
                            or any(
                                char.isspace() or unicodedata.category(char).startswith("C")
                                for char in model_id
                            )
                        ):
                            continue
                        names.setdefault(model_id, _display_name(item.get("displayName"), model_id))
                    page_token = body.get("nextPageToken")
                    if not page_token:
                        if self.metadata_repo is not None and self.connection_for_ref is not None:
                            connection = self.connection_for_ref(secret_ref)
                            if connection is not None:
                                self.metadata_repo.save_connection(
                                    str(connection["connection_id"]),
                                    {**connection, "model_names": names},
                                )
                        self._report(secret_ref, 200, "connected")
                        return list(names)
                    if not isinstance(page_token, str):
                        raise ProviderError("Gemini model pagination is invalid")
            raise ProviderError("Gemini model catalog has too many pages")
        except (httpx.HTTPError, ValueError) as exc:
            self._report(secret_ref, None, "network_error")
            raise ProviderError("Gemini model discovery failed") from exc

    async def stream(
        self,
        *,
        snapshot: RoleSnapshot,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        headers = await self.auth_for_ref(snapshot.secret_ref)
        system_text = "\n\n".join(
            message.content or "" for message in messages if message.role is MessageRole.SYSTEM
        )
        contents: list[dict[str, Any]] = []
        for message_index, message in enumerate(messages):
            if message.role is MessageRole.SYSTEM:
                continue
            if message.role is MessageRole.TOOL:
                try:
                    result = json.loads(message.content or "null")
                except json.JSONDecodeError:
                    result = {"text": message.content or ""}
                if not isinstance(result, dict):
                    result = {"result": result}
                contents.append(
                    {
                        "role": "user",
                        "parts": [
                            {
                                "functionResponse": {
                                    "name": message.name or "tool",
                                    "response": result,
                                    "id": message.tool_call_id,
                                }
                            }
                        ],
                    }
                )
                continue
            parts: list[dict[str, Any]] = []
            if message.role is MessageRole.ASSISTANT:
                parts.extend(
                    self._stored_text_parts(
                        snapshot.secret_ref,
                        snapshot.model_profile_id,
                        snapshot.model_id,
                        messages[: message_index + 1],
                        message.content or "",
                    )
                )
            elif message.content:
                parts.append({"text": message.content})
            for index, call in enumerate(message.tool_calls):
                function_part: dict[str, Any] = {
                    "functionCall": {
                        "name": call.name,
                        "args": json.loads(call.arguments_json),
                        "id": call.id,
                    }
                }
                signature = self._signature(snapshot.secret_ref, snapshot.model_profile_id, call.id)
                if signature:
                    function_part["thoughtSignature"] = signature
                elif index == 0 and snapshot.model_id.startswith("gemini-3"):
                    raise ProviderError(
                        "Gemini tool context cannot be resumed without its signature"
                    )
                parts.append(function_part)
            if parts:
                contents.append(
                    {
                        "role": "model" if message.role is MessageRole.ASSISTANT else "user",
                        "parts": parts,
                    }
                )
        payload: dict[str, Any] = {"contents": contents}
        if system_text:
            payload["systemInstruction"] = {"parts": [{"text": system_text}]}
        if tools:
            payload["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": tool.parameters,
                        }
                        for tool in tools
                    ]
                }
            ]
        if snapshot.budget.max_output_tokens is not None or snapshot.temperature is not None:
            config: dict[str, Any] = {}
            if snapshot.budget.max_output_tokens is not None:
                config["maxOutputTokens"] = snapshot.budget.max_output_tokens
            if snapshot.temperature is not None:
                config["temperature"] = snapshot.temperature
            payload["generationConfig"] = config
        model_id = snapshot.model_id.removeprefix("models/")
        url = (
            "https://generativelanguage.googleapis.com/v1/models/"
            f"{quote(model_id, safe='')}:streamGenerateContent?alt=sse"
        )
        parts_seen: list[str] = []
        text_segments: list[dict[str, Any]] = []
        calls: list[ToolCall] = []
        staged_signatures: list[tuple[str, str]] = []
        usage: ModelUsage | None = None
        finish_reason: str | None = None
        seen_chunk = False
        try:
            async with (
                httpx.AsyncClient(transport=self.transport, timeout=120) as client,
                client.stream("POST", url, headers=headers, json=payload) as response,
            ):
                if response.status_code != 200:
                    self._report(snapshot.secret_ref, response.status_code, "inference_failed")
                    raise ProviderError(f"Gemini inference returned HTTP {response.status_code}")
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    chunk = json.loads(line[5:].strip())
                    if not isinstance(chunk, dict):
                        raise ValueError("Gemini inference chunk must be an object")
                    if "error" in chunk:
                        error_body = chunk.get("error")
                        code = error_body.get("code") if isinstance(error_body, dict) else None
                        reason = "usage_limit" if code == 429 else "inference_failed"
                        self._report(
                            snapshot.secret_ref, code if isinstance(code, int) else None, reason
                        )
                        raise ProviderError("Gemini inference failed")
                    seen_chunk = True
                    candidates = chunk.get("candidates")
                    if isinstance(candidates, list) and candidates:
                        first = candidates[0]
                        if isinstance(first, dict):
                            if isinstance(first.get("finishReason"), str):
                                finish_reason = first["finishReason"]
                            content = first.get("content")
                            for part in (
                                content.get("parts", []) if isinstance(content, dict) else []
                            ):
                                if not isinstance(part, dict):
                                    continue
                                value = part.get("text")
                                signature = part.get("thoughtSignature")
                                if isinstance(signature, str) and len(signature) > 65536:
                                    raise ProviderError("Gemini thought signature is too large")
                                thought = part.get("thought", False)
                                if type(thought) is not bool:
                                    raise ProviderError("Gemini thought marker is invalid")
                                if thought:
                                    if any(
                                        field in part
                                        for field in (
                                            "functionCall",
                                            "functionResponse",
                                            "executableCode",
                                            "codeExecutionResult",
                                            "toolCall",
                                            "toolResponse",
                                        )
                                    ):
                                        raise ProviderError(
                                            "Gemini thought part contains an executable field"
                                        )
                                    # A thought body cannot enter public events or
                                    # persisted history. A signed thought part needs
                                    # its original body for exact replay, so fail
                                    # rather than inventing replacement content.
                                    if isinstance(signature, str) and signature:
                                        raise ProviderError(
                                            "Gemini signed thought part cannot be safely resumed"
                                        )
                                    continue
                                if isinstance(value, str):
                                    if value:
                                        parts_seen.append(value)
                                    segment = {
                                        "length": len(value),
                                        "thought_signature": signature
                                        if isinstance(signature, str) and signature
                                        else None,
                                    }
                                    if value and (
                                        text_segments
                                        and not text_segments[-1]["thought_signature"]
                                        and not segment["thought_signature"]
                                    ):
                                        text_segments[-1]["length"] += segment["length"]
                                    elif value or segment["thought_signature"]:
                                        text_segments.append(segment)
                                    if value:
                                        yield ProviderEvent(event_type="model.delta", delta=value)
                                function = part.get("functionCall")
                                if isinstance(function, dict) and isinstance(
                                    function.get("name"), str
                                ):
                                    call_id = function.get("id")
                                    if not isinstance(call_id, str) or not call_id:
                                        call_id = f"gemini_call_{len(calls)}"
                                    signature = part.get("thoughtSignature")
                                    if isinstance(signature, str) and signature:
                                        staged_signatures.append((call_id, signature))
                                    elif not calls and snapshot.model_id.startswith("gemini-3"):
                                        raise ProviderError(
                                            "Gemini returned a tool call without a signature"
                                        )
                                    calls.append(
                                        ToolCall(
                                            id=call_id,
                                            name=function["name"],
                                            arguments_json=json.dumps(function.get("args", {})),
                                        )
                                    )
                    raw_usage = chunk.get("usageMetadata")
                    if isinstance(raw_usage, dict):
                        usage = ModelUsage(
                            prompt_tokens=self._counter(raw_usage.get("promptTokenCount")),
                            completion_tokens=self._counter(raw_usage.get("candidatesTokenCount")),
                            total_tokens=self._counter(raw_usage.get("totalTokenCount")),
                        )
        except (httpx.HTTPError, ValueError) as exc:
            self._report(snapshot.secret_ref, None, "network_error")
            stage, category = _inference_failure_diagnostic(exc)
            raise ProviderError(
                "Gemini inference stream failed",
                failure_stage=stage,
                failure_category=category,
            ) from exc
        if not seen_chunk or finish_reason is None:
            self._report(snapshot.secret_ref, None, "stream_incomplete")
            raise ProviderError("Gemini inference ended without a terminal candidate")
        if finish_reason != "STOP":
            self._report(snapshot.secret_ref, None, "stream_incomplete")
            raise ProviderError("Gemini inference did not complete successfully")
        if (
            any(segment.get("thought_signature") for segment in text_segments)
            and len(json.dumps(text_segments)) > 131072
        ):
            raise ProviderError("Gemini text signature metadata exceeds the accepted size")
        for call_id, signature in staged_signatures:
            self._save_signature(snapshot.secret_ref, snapshot.model_profile_id, call_id, signature)
        if text_segments:
            self._save_text_parts(
                snapshot.secret_ref,
                snapshot.model_profile_id,
                snapshot.model_id,
                [
                    *messages,
                    Message(
                        role=MessageRole.ASSISTANT,
                        content="".join(parts_seen) or None,
                        tool_calls=tuple(calls),
                    ),
                ],
                text_segments,
            )
        self._report(snapshot.secret_ref, 200, "connected")
        yield ProviderEvent(
            event_type="model.completed",
            response=ModelResponse(
                content="".join(parts_seen) or None,
                tool_calls=tuple(calls),
                finish_reason=finish_reason,
                usage=usage,
            ),
        )
