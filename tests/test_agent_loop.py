import json
import subprocess
import sys
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest

from operant.domain.messages import (
    Message,
    ModelResponse,
    ProviderEvent,
    ToolCall,
    ToolDefinition,
)
from operant.domain.models import (
    Budget,
    Effort,
    ModelProfile,
    RolePreset,
    RoleSnapshot,
    ToolPolicy,
)
from operant.providers.base import ModelProvider
from operant.runtime.loop import AgentLoop
from operant.tools.workspace import WorkspaceTools


class ScriptedProvider(ModelProvider):
    def __init__(self) -> None:
        self.turn = 0
        self.received_messages: list[list[Message]] = []

    async def stream(
        self,
        *,
        snapshot: RoleSnapshot,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        self.received_messages.append(list(messages))
        self.turn += 1
        if self.turn == 1:
            yield ProviderEvent(
                event_type="model.completed",
                response=ModelResponse(
                    tool_calls=(
                        ToolCall(
                            id="call_1",
                            name="read_file",
                            arguments_json='{"path":"hello.txt"}',
                        ),
                    ),
                    finish_reason="tool_calls",
                ),
            )
        else:
            yield ProviderEvent(event_type="model.delta", delta="Found it.")
            yield ProviderEvent(
                event_type="model.completed",
                response=ModelResponse(content="Found it.", finish_reason="stop"),
            )


class RepairingTestProvider(ModelProvider):
    def __init__(self) -> None:
        self.turn = 0
        self.received_messages: list[list[Message]] = []

    async def stream(
        self,
        *,
        snapshot: RoleSnapshot,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        del snapshot, tools
        self.turn += 1
        self.received_messages.append(list(messages))
        test_command = [sys.executable, "-m", "pytest", "-q", "test_target.py"]
        if self.turn == 1:
            response = ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="first_test",
                        name="run_command",
                        arguments_json=json.dumps({"argv": test_command}),
                    ),
                ),
                finish_reason="tool_calls",
            )
        elif self.turn == 2:
            tool_result = json.loads(messages[-1].content or "{}")
            assert tool_result["test_failure"]["exit_code"] != 0
            response = ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="fix",
                        name="apply_patch",
                        arguments_json=json.dumps(
                            {
                                "path": "target.py",
                                "old_text": "return 100",
                                "new_text": "return 2",
                            }
                        ),
                    ),
                ),
                finish_reason="tool_calls",
            )
        elif self.turn == 3:
            response = ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="second_test",
                        name="run_command",
                        arguments_json=json.dumps({"argv": test_command}),
                    ),
                ),
                finish_reason="tool_calls",
            )
        else:
            response = ModelResponse(content="Tests pass after the repair.", finish_reason="stop")
        yield ProviderEvent(event_type="model.completed", response=response)


class RepeatingTestFailureProvider(RepairingTestProvider):
    async def stream(
        self,
        *,
        snapshot: RoleSnapshot,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        del snapshot, messages, tools
        self.turn += 1
        test_command = [sys.executable, "-m", "pytest", "-q", "test_target.py"]
        yield ProviderEvent(
            event_type="model.completed",
            response=ModelResponse(
                tool_calls=(
                    ToolCall(
                        id=f"test_{self.turn}",
                        name="run_command",
                        arguments_json=json.dumps({"argv": test_command}),
                    ),
                ),
                finish_reason="tool_calls",
            ),
        )


def snapshot() -> RoleSnapshot:
    profile = ModelProfile(
        name="test-model",
        model_id="test-model-id",
        base_url="https://relay.example.com/v1",
        secret_ref="OPERANT_API_KEY",
    )
    role = RolePreset(
        name="Explorer",
        system_prompt="Inspect the workspace.",
        model_profile_id=profile.id,
        effort=Effort.MEDIUM,
        budget=Budget(max_turns=3),
        tool_policy=ToolPolicy(allowed_tools=("read_file",)),
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
async def test_tool_result_is_returned_to_model_context(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("hello from a real file", encoding="utf-8")
    provider = ScriptedProvider()
    loop = AgentLoop(provider, WorkspaceTools(tmp_path, policy=snapshot().tool_policy))

    events = [event async for event in loop.run(snapshot=snapshot(), user_message="Read it")]

    assert events[-1].event_type == "agent.completed"
    assert len(provider.received_messages) == 2
    tool_message = provider.received_messages[1][-1]
    assert tool_message.tool_call_id == "call_1"
    assert "hello from a real file" in (tool_message.content or "")


@pytest.mark.asyncio
async def test_workspace_path_escape_is_returned_as_tool_error(tmp_path: Path) -> None:
    class EscapingProvider(ScriptedProvider):
        async def stream(
            self,
            *,
            snapshot: RoleSnapshot,
            messages: Sequence[Message],
            tools: Sequence[ToolDefinition],
        ) -> AsyncIterator[ProviderEvent]:
            self.received_messages.append(list(messages))
            self.turn += 1
            if self.turn == 1:
                yield ProviderEvent(
                    event_type="model.completed",
                    response=ModelResponse(
                        tool_calls=(
                            ToolCall(
                                id="escape",
                                name="read_file",
                                arguments_json='{"path":"../secret.txt"}',
                            ),
                        )
                    ),
                )
            else:
                payload = json.loads(messages[-1].content or "{}")
                assert payload["error"] == "ToolError"
                yield ProviderEvent(
                    event_type="model.completed",
                    response=ModelResponse(content="Access denied.", finish_reason="stop"),
                )

    events = [
        event
        async for event in AgentLoop(
            EscapingProvider(),
            WorkspaceTools(tmp_path, policy=snapshot().tool_policy),
        ).run(snapshot=snapshot(), user_message="Escape")
    ]

    assert any(event.event_type == "tool.failed" for event in events)


@pytest.mark.asyncio
async def test_failed_test_feedback_drives_a_second_patch_and_verification(tmp_path: Path) -> None:
    (tmp_path / "target.py").write_text(
        "def answer() -> int:\n    return 100\n",
        encoding="utf-8",
    )
    (tmp_path / "test_target.py").write_text(
        "from target import answer\n\n\ndef test_answer() -> None:\n    assert answer() == 2\n",
        encoding="utf-8",
    )
    writable_snapshot = snapshot().model_copy(
        update={
            "budget": Budget(max_turns=4),
            "tool_policy": ToolPolicy(
                allowed_tools=("apply_patch", "run_command"),
                workspace_write=True,
                command_execution=True,
            ),
        }
    )
    provider = RepairingTestProvider()

    events = [
        event
        async for event in AgentLoop(
            provider,
            WorkspaceTools(tmp_path, policy=writable_snapshot.tool_policy),
        ).run(snapshot=writable_snapshot, user_message="Fix the failing test")
    ]

    assert events[-1].event_type == "agent.completed"
    assert any(event.event_type == "test.failure_feedback" for event in events)
    assert "return 2" in (tmp_path / "target.py").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_repeated_test_failure_stops_the_agent_without_another_model_turn(
    tmp_path: Path,
) -> None:
    (tmp_path / "test_target.py").write_text(
        "def test_failure() -> None:\n    assert False\n",
        encoding="utf-8",
    )
    writable_snapshot = snapshot().model_copy(
        update={
            "budget": Budget(max_turns=4, max_consecutive_test_failures=2),
            "tool_policy": ToolPolicy(
                allowed_tools=("run_command",),
                command_execution=True,
            ),
        }
    )
    provider = RepeatingTestFailureProvider()

    events = [
        event
        async for event in AgentLoop(
            provider,
            WorkspaceTools(tmp_path, policy=writable_snapshot.tool_policy),
        ).run(snapshot=writable_snapshot, user_message="Keep trying")
    ]

    assert provider.turn == 2
    assert events[-1].event_type == "agent.no_progress"
    assert events[-1].payload["consecutive_failures"] == 2


@pytest.mark.asyncio
async def test_approval_pauses_and_resumes_before_git_write(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "change.txt").write_text("change\n", encoding="utf-8")

    class GitWriteProvider(ScriptedProvider):
        async def stream(
            self,
            *,
            snapshot: RoleSnapshot,
            messages: Sequence[Message],
            tools: Sequence[ToolDefinition],
        ) -> AsyncIterator[ProviderEvent]:
            self.received_messages.append(list(messages))
            self.turn += 1
            if self.turn == 1:
                yield ProviderEvent(
                    event_type="model.completed",
                    response=ModelResponse(
                        tool_calls=(
                            ToolCall(
                                id="git_add",
                                name="run_command",
                                arguments_json='{"argv":["git","add","change.txt"]}',
                            ),
                        )
                    ),
                )
            else:
                yield ProviderEvent(
                    event_type="model.completed",
                    response=ModelResponse(content="Staged.", finish_reason="stop"),
                )

    writable_snapshot = snapshot().model_copy(
        update={
            "tool_policy": ToolPolicy(
                allowed_tools=("run_command",),
                command_execution=True,
            )
        }
    )
    approvals: list[tuple[str, str, str]] = []

    async def approve(tool_call_id: str, category: str, detail: str) -> bool:
        approvals.append((tool_call_id, category, detail))
        return True

    events = [
        event
        async for event in AgentLoop(
            GitWriteProvider(),
            WorkspaceTools(tmp_path, policy=writable_snapshot.tool_policy),
        ).run(
            snapshot=writable_snapshot,
            user_message="Stage it",
            approval_callback=approve,
        )
    ]

    assert [event.event_type for event in events].count("tool.approval_required") == 1
    assert any(event.event_type == "tool.approval_decided" for event in events)
    assert any(event.event_type == "tool.completed" for event in events)
    assert approvals[0][0:2] == ("git_add", "git_write")
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert staged.stdout.strip() == "change.txt"
