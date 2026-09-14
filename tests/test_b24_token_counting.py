from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from operant.application.token_counting import (
    TokenizerRegistry,
    count_context_tokens,
    estimate_utf8_tokens,
)
from operant.domain.messages import Message, MessageRole, ToolCall, ToolDefinition


class CharacterTokenizer:
    """Deterministic test tokenizer whose token count is the text length."""

    def encode(self, text: str) -> list[int]:
        return list(range(len(text)))


def complete_wrapper(
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]],
) -> str:
    # The wrapper deliberately includes framing plus both message and tool
    # arrays, just like a complete provider adapter would.
    return (
        "<request>"
        + json.dumps(
            {"messages": list(messages), "tools": list(tools)},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "</request>"
    )


def sample_messages() -> tuple[Message, ...]:
    return (
        Message(role=MessageRole.SYSTEM, content="You are a careful assistant."),
        Message(
            role=MessageRole.ASSISTANT,
            content=None,
            tool_calls=(
                ToolCall(
                    id="call_1",
                    name="read_file",
                    arguments_json='{"path":"src/main.py"}',
                ),
            ),
        ),
        Message(
            role=MessageRole.TOOL,
            content="def main():\n    return 0",
            tool_call_id="call_1",
            name="read_file",
        ),
    )


def sample_tools() -> tuple[ToolDefinition, ...]:
    return (
        ToolDefinition(
            name="read_file",
            description="Read one UTF-8 file.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        ),
    )


def test_exact_registration_matches_model_id_and_counts_complete_wrapper() -> None:
    registry = TokenizerRegistry()
    tokenizer = CharacterTokenizer()
    registration = registry.register(
        "provider/model-v1",
        tokenizer,
        complete_wrapper,
        wrapper_complete=True,
    )

    result = count_context_tokens(
        "provider/model-v1",
        sample_messages(),
        sample_tools(),
        reserved_output_tokens=32,
        registry=registry,
    )

    normalized_messages = [message.model_dump(mode="json") for message in sample_messages()]
    normalized_tools = [tool.model_dump(mode="json") for tool in sample_tools()]
    expected_prompt = complete_wrapper(normalized_messages, normalized_tools)

    assert registry.get("provider/model-v1") is registration
    assert result.exact is True
    assert result.estimated is False
    assert result.method == "registered_tokenizer_complete_provider_wrapper"
    assert result.input_tokens == len(expected_prompt)
    assert result.total_tokens == len(expected_prompt) + 32
    assert result.tool_schema_tokens > 0
    assert result.tool_call_tokens > 0
    assert result.wrapper_tokens > 0


def test_unknown_model_uses_full_utf8_upper_bound_and_marks_estimate() -> None:
    messages = ({"role": "user", "content": "中文短词 🚀"},)
    tools = ({"type": "function", "function": {"name": "搜索", "parameters": {}}},)

    result = count_context_tokens(
        "provider/unknown-model",
        messages,
        tools,
        reserved_output_tokens=17,
        registry=TokenizerRegistry(),
    )
    envelope = json.dumps(
        {"messages": list(messages), "tools": list(tools)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    assert result.exact is False
    assert result.estimated is True
    assert result.method == "utf8_bytes_upper_bound"
    assert result.input_tokens == estimate_utf8_tokens(envelope)
    assert result.total_tokens == result.input_tokens + 17
    assert result.fallback_reason == "model id is not registered"
    assert result.tool_schema_tokens > 0


def test_registered_tokenizer_without_complete_wrapper_never_claims_exact() -> None:
    registry = TokenizerRegistry()
    registry.register("provider/model-v1", CharacterTokenizer())

    result = count_context_tokens(
        "provider/model-v1",
        sample_messages(),
        sample_tools(),
        registry=registry,
    )

    assert result.exact is False
    assert result.estimated is True
    assert result.method == "utf8_bytes_upper_bound_incomplete_provider_wrapper"
    assert result.fallback_reason == "registered tokenizer has no complete provider wrapper"


def test_model_switch_performs_new_exact_lookup_without_reusing_old_count() -> None:
    registry = TokenizerRegistry()
    registry.register(
        "provider/short",
        CharacterTokenizer(),
        complete_wrapper,
        wrapper_complete=True,
    )
    registry.register(
        "provider/long",
        lambda text: list(range(len(text) * 3)),
        complete_wrapper,
        wrapper_complete=True,
    )

    first = count_context_tokens(
        "provider/short", sample_messages(), sample_tools(), registry=registry
    )
    second = count_context_tokens(
        "provider/long", sample_messages(), sample_tools(), registry=registry
    )
    first_again = count_context_tokens(
        "provider/short", sample_messages(), sample_tools(), registry=registry
    )

    assert second.input_tokens == first.input_tokens * 3
    assert first_again == first


def test_exact_count_changes_when_tool_calls_or_schemas_change() -> None:
    registry = TokenizerRegistry()
    registry.register(
        "provider/model-v1",
        CharacterTokenizer(),
        complete_wrapper,
        wrapper_complete=True,
    )

    without_calls = (Message(role=MessageRole.USER, content="read the file"),)
    with_calls = (
        Message(
            role=MessageRole.ASSISTANT,
            tool_calls=(
                ToolCall(
                    id="call_2",
                    name="read_file",
                    arguments_json='{"path":"README.md"}',
                ),
            ),
        ),
    )
    base = count_context_tokens("provider/model-v1", without_calls, (), registry=registry)
    with_tool_call = count_context_tokens("provider/model-v1", with_calls, (), registry=registry)
    with_schema = count_context_tokens(
        "provider/model-v1", without_calls, sample_tools(), registry=registry
    )

    assert with_tool_call.input_tokens > base.input_tokens
    assert with_tool_call.tool_call_tokens > 0
    assert with_schema.input_tokens > base.input_tokens
    assert with_schema.tool_schema_tokens > 0


def test_registry_requires_formatter_for_explicit_complete_flag_and_matches_exactly() -> None:
    registry = TokenizerRegistry()
    with pytest.raises(ValueError, match="complete provider wrapper requires a formatter"):
        registry.register("provider/model-v1", CharacterTokenizer(), wrapper_complete=True)

    registry.register(
        "provider/model-v1",
        CharacterTokenizer(),
        formatter=complete_wrapper,
        complete=True,
    )
    assert registry.get("provider/model-v1") is not None
    assert registry.get("provider/model-v1").exact_capable is True  # type: ignore[union-attr]


def test_output_reservation_aliases_and_validation() -> None:
    registry = TokenizerRegistry()
    result = count_context_tokens(
        "provider/unknown",
        ({"role": "user", "content": "hello"},),
        output_reserve_tokens=9,
        registry=registry,
    )
    assert result.output_reserve_tokens == 9
    assert result.prompt_tokens == result.input_tokens
    assert result.total == result.input_tokens + 9

    with pytest.raises(ValueError, match="reserved output tokens"):
        count_context_tokens(
            "provider/unknown",
            (),
            reserved_output_tokens=-1,
            registry=registry,
        )
