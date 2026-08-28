import json

import httpx
import pytest

from operant.domain.messages import Message, MessageRole, ToolDefinition
from operant.domain.models import (
    Budget,
    Effort,
    ModelProfile,
    RolePreset,
    RoleSnapshot,
)
from operant.providers.openai_compatible import (
    OpenAICompatibleProvider,
    ProviderError,
)


def provider_snapshot() -> RoleSnapshot:
    profile = ModelProfile(
        name="relay-model",
        model_id="exact-relay-id",
        base_url="https://relay.example.com/v1",
        secret_ref="OPERANT_TEST_API_KEY",
    )
    role = RolePreset(
        name="Coder",
        system_prompt="Use tools.",
        model_profile_id=profile.id,
        effort=Effort.HIGH,
        budget=Budget(max_turns=2),
    )
    return RoleSnapshot(
        role_id=role.id,
        role_version=role.version,
        role_name=role.name,
        system_prompt=role.system_prompt,
        model_profile_id=profile.id,
        model_profile_name=profile.name,
        provider=profile.provider,
        model_id=profile.model_id,
        base_url=profile.base_url,
        secret_ref=profile.secret_ref,
        effort=role.effort,
        provider_effort_parameter=profile.effort_parameter,
        provider_effort_value=profile.provider_effort_value(role.effort),
        tool_policy=role.tool_policy,
        budget=role.budget,
        memory_scope=role.memory_scope,
    )


@pytest.mark.asyncio
async def test_model_discovery_and_streamed_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPERANT_TEST_API_KEY", "test-only-secret")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-only-secret"
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={"data": [{"id": "kimi-exact"}, {"id": "glm-exact"}]},
            )
        payload = json.loads(request.content)
        assert payload["model"] == "exact-relay-id"
        assert payload["reasoning_effort"] == "high"
        body = "\n".join(
            (
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
                '"id":"call_1","function":{"name":"read_","arguments":"{\\"pa"}}]}}]}',
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
                '"function":{"name":"file","arguments":"th\\":\\"x.txt\\"}"}}]},'
                '"finish_reason":"tool_calls"}]}',
                'data: {"choices":[],"usage":{"prompt_tokens":11,'
                '"completion_tokens":7,"total_tokens":18}}',
                "data: [DONE]",
                "",
            )
        )
        return httpx.Response(
            200,
            text=body,
            headers={"content-type": "text/event-stream"},
        )

    provider = OpenAICompatibleProvider(transport=httpx.MockTransport(handler))
    assert await provider.list_models(
        base_url="https://relay.example.com/v1",
        secret_ref="OPERANT_TEST_API_KEY",
    ) == ["kimi-exact", "glm-exact"]
    assert await provider.list_models(
        base_url="https://relay.example.com",
        secret_ref="OPERANT_TEST_API_KEY",
    ) == ["kimi-exact", "glm-exact"]

    events = [
        event
        async for event in provider.stream(
            snapshot=provider_snapshot(),
            messages=(Message(role=MessageRole.USER, content="Read x.txt"),),
            tools=(
                ToolDefinition(
                    name="read_file",
                    description="Read a file.",
                    parameters={"type": "object"},
                ),
            ),
        )
    ]
    response = events[-1].response
    assert response is not None
    assert response.finish_reason == "tool_calls"
    assert response.tool_calls[0].id == "call_1"
    assert response.tool_calls[0].name == "read_file"
    assert response.tool_calls[0].arguments() == {"path": "x.txt"}
    assert response.usage is not None
    assert response.usage.total_tokens == 18


@pytest.mark.asyncio
async def test_output_limit_is_forwarded_and_missing_usage_fields_remain_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPERANT_TEST_API_KEY", "test-only-secret")

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["max_completion_tokens"] == 17
        return httpx.Response(
            200,
            text=('data: {"choices":[],"usage":{"prompt_tokens":4}}\ndata: [DONE]\n'),
            headers={"content-type": "text/event-stream"},
        )

    snapshot = provider_snapshot().model_copy(
        update={"budget": Budget(max_turns=2, max_output_tokens=17)}
    )
    events = [
        event
        async for event in OpenAICompatibleProvider(transport=httpx.MockTransport(handler)).stream(
            snapshot=snapshot, messages=(), tools=()
        )
    ]
    usage = events[-1].response.usage if events[-1].response is not None else None
    assert usage is not None
    assert usage.prompt_tokens == 4
    assert usage.completion_tokens is None
    assert usage.total_tokens is None


@pytest.mark.asyncio
async def test_missing_secret_reports_reference_not_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPERANT_TEST_API_KEY", raising=False)
    provider = OpenAICompatibleProvider()

    with pytest.raises(ProviderError, match="OPERANT_TEST_API_KEY"):
        await provider.list_models(
            base_url="https://relay.example.com/v1",
            secret_ref="OPERANT_TEST_API_KEY",
        )
