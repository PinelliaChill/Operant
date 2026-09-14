"""Model aware context token counting.

The runtime sends provider specific message payloads, while the memory and
context code needs a count before a provider request is made.  This module
keeps that concern small and dependency free:

* a tokenizer is registered under one exact provider model id;
* an explicitly complete provider wrapper may count the complete wire prompt;
* otherwise the complete JSON payload is measured with a conservative UTF-8
  byte upper bound and the result is marked as an estimate.

The registry deliberately has no result cache.  A model id is looked up for
every call, so changing a ``ModelProfile`` cannot reuse a count produced for a
different model.

``TokenizerRegistry.register`` accepts a tokenizer and, optionally, a
provider wrapper.  A tokenizer alone is useful for diagnostics, but cannot be
called exact: it does not describe role markers, tool-call framing, or tool
schema framing.  To opt into exact counting the caller must also pass
``wrapper_complete=True`` and provide a formatter that returns the complete
provider prompt, including message wrappers, tool calls, and tool schemas.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, TypeAlias, cast


class Tokenizer(Protocol):
    """Small adapter understood by :class:`TokenizerRegistry`.

    The bundled project intentionally has no tokenizer dependency.  A caller
    can register an installed tokenizer object (with ``encode``) or a simple
    callable returning token ids.
    """

    def encode(self, text: str) -> Sequence[Any]: ...


PromptFormatter: TypeAlias = Callable[
    [Sequence[Mapping[str, Any]], Sequence[Mapping[str, Any]]], str | bytes
]
TokenizerLike: TypeAlias = Tokenizer | Callable[[str], Sequence[Any]]


class _SupportsPromptFormatter(Protocol):
    def format_prompt(
        self,
        messages: Sequence[Mapping[str, Any]],
        tool_schemas: Sequence[Mapping[str, Any]],
    ) -> str | bytes: ...


@dataclass(frozen=True)
class TokenizerRegistration:
    """One exact model-id to tokenizer mapping.

    ``wrapper_complete`` is intentionally explicit.  A formatter that merely
    serializes message content or omits the tools array must remain an
    estimate; callers should only set this flag when their provider adapter's
    formatter is the same framing used on the wire.
    """

    model_id: str
    tokenizer: TokenizerLike
    prompt_formatter: PromptFormatter | _SupportsPromptFormatter | None = None
    wrapper_complete: bool = False
    name: str | None = None

    @property
    def exact_capable(self) -> bool:
        """Whether this registration has enough information for exact use."""

        return self.prompt_formatter is not None and self.wrapper_complete


# A shorter spelling is convenient for integrations and keeps the public API
# discoverable without making callers depend on an implementation detail.
TokenizerSpec = TokenizerRegistration


@dataclass(frozen=True)
class TokenCount:
    """A prompt count plus the output capacity reserved for the call.

    ``input_tokens`` includes all supplied messages, message tool calls and
    all supplied tool schemas.  ``total_tokens`` is populated only when an
    output reservation was supplied; an absent reservation is unknown rather
    than silently treated as zero.  ``exact`` and ``estimated`` are mutually
    exclusive facts about the input count and are also reflected in
    ``method``.
    """

    model_id: str
    input_tokens: int
    reserved_output_tokens: int | None
    total_tokens: int | None
    exact: bool
    estimated: bool
    method: str
    message_tokens: int
    tool_call_tokens: int
    tool_schema_tokens: int
    wrapper_tokens: int
    fallback_reason: str | None = None

    def __post_init__(self) -> None:
        values = (
            self.input_tokens,
            self.message_tokens,
            self.tool_call_tokens,
            self.tool_schema_tokens,
            self.wrapper_tokens,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values
        ):
            raise ValueError("token counts must be non-negative integers")
        if self.reserved_output_tokens is not None and (
            isinstance(self.reserved_output_tokens, bool)
            or not isinstance(self.reserved_output_tokens, int)
            or self.reserved_output_tokens < 0
        ):
            raise ValueError("reserved output tokens must be a non-negative integer")
        if self.total_tokens is not None and (
            isinstance(self.total_tokens, bool)
            or not isinstance(self.total_tokens, int)
            or self.total_tokens < 0
        ):
            raise ValueError("total tokens must be a non-negative integer")
        if self.exact == self.estimated:
            raise ValueError("exact and estimated must be opposites")
        if self.total_tokens is not None:
            expected_total = self.input_tokens + (self.reserved_output_tokens or 0)
            if self.total_tokens != expected_total:
                raise ValueError("total tokens must include the input and output reservation")

    # Names used by ContextWatermark and by callers familiar with provider
    # usage objects.  Keeping aliases here avoids a second conversion layer in
    # Composer integration.
    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens

    @property
    def output_reserve_tokens(self) -> int | None:
        return self.reserved_output_tokens

    @property
    def total(self) -> int | None:
        return self.total_tokens

    @property
    def is_estimate(self) -> bool:
        return self.estimated

    @property
    def messages_tokens(self) -> int:
        return self.message_tokens

    @property
    def tools_tokens(self) -> int:
        return self.tool_schema_tokens

    @property
    def token_estimate(self) -> int:
        return self.input_tokens

    @property
    def input_token_estimate(self) -> int:
        return self.input_tokens

    @property
    def tool_schema_token_estimate(self) -> int:
        return self.tool_schema_tokens

    @property
    def estimation_method(self) -> str:
        return self.method


class TokenizerRegistry:
    """Exact model-id registry for explicit tokenizer adapters."""

    def __init__(self) -> None:
        self._entries: dict[str, TokenizerRegistration] = {}

    def register(
        self,
        model_id: str,
        tokenizer: TokenizerLike,
        formatter: PromptFormatter | _SupportsPromptFormatter | None = None,
        *,
        prompt_formatter: PromptFormatter | _SupportsPromptFormatter | None = None,
        wrapper: PromptFormatter | _SupportsPromptFormatter | None = None,
        wrapper_complete: bool = False,
        complete: bool | None = None,
        name: str | None = None,
        replace: bool = True,
    ) -> TokenizerRegistration:
        """Register ``tokenizer`` for one exact ``model_id``.

        ``formatter`` is the primary spelling.  ``prompt_formatter`` and
        ``wrapper`` are accepted as readable aliases for integrations that
        already use either term.  If more than one is provided, the call fails
        instead of guessing which provider wrapper is authoritative.

        ``complete`` is an alias for ``wrapper_complete``.  Exact counting
        requires an explicit complete flag in addition to a formatter; this is
        the guard against presenting a partial provider wrapper as exact.
        """

        normalized_id = _validate_model_id(model_id)
        selected = [item for item in (formatter, prompt_formatter, wrapper) if item is not None]
        if len(selected) > 1:
            raise ValueError("provide only one provider prompt formatter")
        if complete is not None:
            if wrapper_complete and complete is not wrapper_complete:
                raise ValueError("wrapper_complete and complete disagree")
            wrapper_complete = complete
        if wrapper_complete and not selected:
            raise ValueError("a complete provider wrapper requires a formatter")
        if not replace and normalized_id in self._entries:
            raise ValueError(f"tokenizer already registered for exact model id {normalized_id!r}")
        selected_formatter = selected[0] if selected else None
        registration = TokenizerRegistration(
            model_id=normalized_id,
            tokenizer=tokenizer,
            prompt_formatter=selected_formatter,
            wrapper_complete=wrapper_complete,
            name=name,
        )
        self._entries[normalized_id] = registration
        return registration

    # Explicit aliases make the intended operation clear at call sites and
    # preserve a small amount of compatibility with likely integration code.
    register_tokenizer = register
    register_model = register

    def get(self, model_id: str) -> TokenizerRegistration | None:
        """Return only an exact, case-sensitive model-id match."""

        return self._entries.get(model_id)

    resolve = get
    lookup = get

    def unregister(self, model_id: str) -> TokenizerRegistration | None:
        """Remove one exact registration and return the old value."""

        return self._entries.pop(model_id, None)

    unregister_tokenizer = unregister

    def clear(self) -> None:
        """Clear this registry; useful for process-local test or plugin setup."""

        self._entries.clear()

    def __contains__(self, model_id: object) -> bool:
        return model_id in self._entries

    def __len__(self) -> int:
        return len(self._entries)


# The process-wide registry is deliberately just a mapping, never a result
# cache.  Applications with stricter lifecycle ownership can pass their own
# registry to ``count_context_tokens``.
TOKENIZER_REGISTRY = TokenizerRegistry()
tokenizer_registry = TOKENIZER_REGISTRY


def register_tokenizer(
    model_id: str,
    tokenizer: TokenizerLike,
    formatter: PromptFormatter | _SupportsPromptFormatter | None = None,
    *,
    prompt_formatter: PromptFormatter | _SupportsPromptFormatter | None = None,
    wrapper: PromptFormatter | _SupportsPromptFormatter | None = None,
    wrapper_complete: bool = False,
    complete: bool | None = None,
    name: str | None = None,
    replace: bool = True,
) -> TokenizerRegistration:
    """Register a tokenizer in the process-wide registry."""

    return TOKENIZER_REGISTRY.register(
        model_id,
        tokenizer,
        formatter,
        prompt_formatter=prompt_formatter,
        wrapper=wrapper,
        wrapper_complete=wrapper_complete,
        complete=complete,
        name=name,
        replace=replace,
    )


def unregister_tokenizer(model_id: str) -> TokenizerRegistration | None:
    """Remove one exact process-wide registration."""

    return TOKENIZER_REGISTRY.unregister(model_id)


def count_context_tokens(
    model_id: str,
    messages: Sequence[Any],
    tool_schemas: Sequence[Any] = (),
    *,
    reserved_output_tokens: int | None = None,
    output_reserve_tokens: int | None = None,
    registry: TokenizerRegistry | None = None,
) -> TokenCount:
    """Count one provider request's context and optional output reservation.

    Model lookup is exact and happens on every invocation.  Known models are
    exact only when their registration has both a tokenizer and a complete
    provider wrapper.  Unknown models, wrapper-incomplete registrations and
    tokenizer failures use the full serialized payload's UTF-8 byte count as
    a safe upper bound and are marked ``estimated=True``.
    """

    normalized_id = _validate_model_id(model_id)
    if (
        reserved_output_tokens is not None
        and output_reserve_tokens is not None
        and reserved_output_tokens != output_reserve_tokens
    ):
        raise ValueError("reserved_output_tokens and output_reserve_tokens disagree")
    if reserved_output_tokens is None:
        reserved_output_tokens = output_reserve_tokens
    _validate_output_reservation(reserved_output_tokens)

    normalized_messages = tuple(_json_mapping(item, kind="message") for item in messages)
    normalized_tools = tuple(_json_mapping(item, kind="tool schema") for item in tool_schemas)

    active_registry = registry or TOKENIZER_REGISTRY
    registration = active_registry.get(normalized_id)
    if registration is not None and registration.exact_capable:
        exact = _count_with_complete_wrapper(
            normalized_id,
            normalized_messages,
            normalized_tools,
            registration,
            reserved_output_tokens,
        )
        if exact is not None:
            return exact
        fallback_reason = "registered tokenizer or complete provider wrapper failed"
    elif registration is not None:
        fallback_reason = "registered tokenizer has no complete provider wrapper"
    else:
        fallback_reason = "model id is not registered"

    return _count_with_utf8_upper_bound(
        normalized_id,
        normalized_messages,
        normalized_tools,
        reserved_output_tokens,
        registration=registration,
        fallback_reason=fallback_reason,
    )


# A compact spelling for code that treats this as a generic counting helper.
count_tokens = count_context_tokens


def estimate_utf8_tokens(value: str | bytes) -> int:
    """Return a conservative byte-level token upper bound for ``value``.

    A tokenizer with byte fallback cannot require more than one token per
    UTF-8 byte.  This is intentionally different from the historic
    ``ceil(bytes / 4)`` heuristic: the latter is a useful rough estimate but
    is not an upper bound for arbitrary tokenizers.
    """

    encoded = value if isinstance(value, bytes) else value.encode("utf-8")
    return len(encoded)


def _count_with_complete_wrapper(
    model_id: str,
    messages: tuple[Mapping[str, Any], ...],
    tool_schemas: tuple[Mapping[str, Any], ...],
    registration: TokenizerRegistration,
    reserved_output_tokens: int | None,
) -> TokenCount | None:
    formatter = registration.prompt_formatter
    if formatter is None:
        return None
    try:
        formatted = _format_prompt(formatter, messages, tool_schemas)
        text = formatted.decode("utf-8") if isinstance(formatted, bytes) else formatted
        total_encoded = _encode_length(registration.tokenizer, text)
        message_encoded = (
            _encode_length(registration.tokenizer, _canonical_json(list(messages)))
            if messages
            else 0
        )
        tool_encoded = (
            _encode_length(registration.tokenizer, _canonical_json(list(tool_schemas)))
            if tool_schemas
            else 0
        )
        tool_call_payload = [
            message.get("tool_calls", ()) for message in messages if message.get("tool_calls")
        ]
        message_tool_encoded = (
            _encode_length(registration.tokenizer, _canonical_json(tool_call_payload))
            if tool_call_payload
            else 0
        )
    except Exception:  # noqa: BLE001 - tokenizer/provider adapters are optional process extensions.
        return None
    # Component values are diagnostic counts of their canonical payloads. The
    # complete wrapper count is authoritative; wrapper_tokens is its residual
    # after those components and is never allowed to go negative.
    component_tokens = message_encoded + tool_encoded
    wrapper_tokens = max(0, total_encoded - component_tokens)
    total = _with_output_reservation(total_encoded, reserved_output_tokens)
    return TokenCount(
        model_id=model_id,
        input_tokens=total_encoded,
        reserved_output_tokens=reserved_output_tokens,
        total_tokens=total,
        exact=True,
        estimated=False,
        method="registered_tokenizer_complete_provider_wrapper",
        message_tokens=message_encoded,
        tool_call_tokens=message_tool_encoded,
        tool_schema_tokens=tool_encoded,
        wrapper_tokens=wrapper_tokens,
    )


def _count_with_utf8_upper_bound(
    model_id: str,
    messages: tuple[Mapping[str, Any], ...],
    tool_schemas: tuple[Mapping[str, Any], ...],
    reserved_output_tokens: int | None,
    *,
    registration: TokenizerRegistration | None,
    fallback_reason: str,
) -> TokenCount:
    # The envelope explicitly includes message wrappers and tool schemas. A
    # JSON payload is not assumed to be the provider's wire format; counting
    # its UTF-8 bytes is therefore conservative and must remain estimated.
    envelope = _canonical_json({"messages": list(messages), "tools": list(tool_schemas)})
    input_tokens = estimate_utf8_tokens(envelope)
    message_payload = _canonical_json(list(messages))
    tool_payload = _canonical_json(list(tool_schemas))
    message_tokens = estimate_utf8_tokens(message_payload) if messages else 0
    tool_schema_tokens = estimate_utf8_tokens(tool_payload) if tool_schemas else 0
    tool_call_values = [
        message.get("tool_calls", ()) for message in messages if message.get("tool_calls")
    ]
    tool_call_payload = _canonical_json(tool_call_values)
    tool_call_tokens = estimate_utf8_tokens(tool_call_payload) if tool_call_values else 0
    component_bytes = estimate_utf8_tokens(message_payload) + estimate_utf8_tokens(tool_payload)
    wrapper_tokens = max(0, input_tokens - component_bytes)
    method = "utf8_bytes_upper_bound"
    if registration is not None:
        method = "utf8_bytes_upper_bound_incomplete_provider_wrapper"
    total = _with_output_reservation(input_tokens, reserved_output_tokens)
    return TokenCount(
        model_id=model_id,
        input_tokens=input_tokens,
        reserved_output_tokens=reserved_output_tokens,
        total_tokens=total,
        exact=False,
        estimated=True,
        method=method,
        message_tokens=message_tokens,
        tool_call_tokens=tool_call_tokens,
        tool_schema_tokens=tool_schema_tokens,
        wrapper_tokens=wrapper_tokens,
        fallback_reason=fallback_reason,
    )


def _format_prompt(
    formatter: PromptFormatter | _SupportsPromptFormatter,
    messages: Sequence[Mapping[str, Any]],
    tool_schemas: Sequence[Mapping[str, Any]],
) -> str | bytes:
    if hasattr(formatter, "format_prompt"):
        return formatter.format_prompt(messages, tool_schemas)
    return formatter(messages, tool_schemas)


def _encode_length(tokenizer: TokenizerLike, text: str) -> int:
    encoded = tokenizer.encode(text) if hasattr(tokenizer, "encode") else tokenizer(text)
    if isinstance(encoded, bool):
        raise TypeError("tokenizer returned a boolean")
    if isinstance(encoded, int):
        if encoded < 0:
            raise ValueError("tokenizer returned a negative count")
        return encoded
    if isinstance(encoded, (str, bytes)):
        raise TypeError("tokenizer must return token ids, not text")
    if not isinstance(encoded, Iterable):
        raise TypeError("tokenizer result is not iterable")
    return sum(1 for _ in encoded)


def _json_mapping(value: Any, *, kind: str) -> Mapping[str, Any]:
    converted = _json_value(value)
    if not isinstance(converted, Mapping):
        raise TypeError(f"{kind} must be a mapping")
    return cast(Mapping[str, Any], converted)


def _json_value(value: Any) -> Any:
    # Pydantic BaseModel is intentionally duck typed so this module remains a
    # lightweight Application utility and does not create a Domain import.
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _json_value(model_dump(mode="json"))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _validate_model_id(model_id: str) -> str:
    if not isinstance(model_id, str) or not model_id.strip():
        raise ValueError("model_id must be a non-empty exact string")
    return model_id


def _validate_output_reservation(value: int | None) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
        raise ValueError("reserved output tokens must be a non-negative integer")


def _with_output_reservation(input_tokens: int, reserved_output_tokens: int | None) -> int | None:
    if reserved_output_tokens is None:
        return None
    return input_tokens + reserved_output_tokens


__all__ = [
    "PromptFormatter",
    "TOKENIZER_REGISTRY",
    "Tokenizer",
    "TokenizerLike",
    "TokenizerRegistration",
    "TokenizerRegistry",
    "TokenizerSpec",
    "TokenCount",
    "count_context_tokens",
    "count_tokens",
    "estimate_utf8_tokens",
    "register_tokenizer",
    "tokenizer_registry",
    "unregister_tokenizer",
]
