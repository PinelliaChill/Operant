"""Bounded B2-3 Session/Context verification with a deterministic provider.

This test deliberately uses a recording deterministic provider.  It verifies
the formal ApplicationService and local workspace tool path; it is not a
real-model acceptance test.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest
import pytest_asyncio

from operant.application.service import ApplicationService
from operant.contracts.b2_3 import ManagementCommand
from operant.domain.messages import (
    Message,
    MessageRole,
    ModelResponse,
    ProviderEvent,
    ToolCall,
    ToolDefinition,
)
from operant.domain.models import Budget, ModelProfile, RolePreset, RoleSnapshot, ToolPolicy
from operant.domain.threads import ConversationThread, UserMessagePayload
from operant.memory_plugins.manager import MemoryManager
from operant.persistence.sqlite import SQLiteStore
from operant.providers.base import ModelProvider

SKILL_GUIDANCE_MARKER = "SKILL_GUIDANCE_MAPLE_42"
MEMORY_BODY_MARKER = "MEMORY_BODY_PRIVATE_91"
USER_MESSAGE = "Read probe.txt and summarize it."
TOOL_OUTPUT_MARKER = "TOOL_OUTPUT_CEDAR_17"


class RecordingDeterministicProvider(ModelProvider):
    """Record exact provider inputs and complete after one local tool call."""

    def __init__(self) -> None:
        self.requests: list[tuple[tuple[Message, ...], tuple[ToolDefinition, ...]]] = []

    async def list_models(self, *, base_url: str, secret_ref: str) -> list[str]:
        del base_url, secret_ref
        return ["b23-session-context-test-model"]

    async def stream(
        self,
        *,
        snapshot: RoleSnapshot,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        del snapshot
        request = (tuple(messages), tuple(tools))
        self.requests.append(request)
        if any(message.role is MessageRole.TOOL for message in messages):
            response = ModelResponse(content="Deterministic summary.", finish_reason="stop")
        else:
            response = ModelResponse(
                tool_calls=(
                    ToolCall(
                        id=f"read_probe_{len(self.requests)}",
                        name="read_file",
                        arguments_json='{"path":"probe.txt"}',
                    ),
                ),
                finish_reason="tool_calls",
            )
        yield ProviderEvent(event_type="model.completed", response=response)


async def _command(manager: MemoryManager, **kwargs: object):
    return await manager.execute(ManagementCommand(**kwargs))


@pytest_asyncio.fixture
async def installed_project(tmp_path: Path):
    provider = RecordingDeterministicProvider()
    store = SQLiteStore(tmp_path / "operant.sqlite3")
    service = ApplicationService(store, provider, artifact_root=tmp_path / "artifacts")
    service.initialize()

    profile = service.add_model_profile(
        ModelProfile(
            name="B2-3 Session Context Test",
            model_id="b23-session-context-test-model",
            base_url="https://example.invalid/v1",
            secret_ref="OPERANT_B23_SESSION_CONTEXT_TEST_KEY",
        )
    )
    role = service.create_role(
        RolePreset(
            name="B2-3 Session Context Test Role",
            system_prompt="Use the supplied workspace tool and answer the user.",
            model_profile_id=profile.id,
            memory_scope="read: [project]; write: []",
            budget=Budget(max_turns=3, timeout_seconds=10),
            tool_policy=ToolPolicy(allowed_tools=("read_file",)),
        )
    )

    source_root = tmp_path / "source-skills"
    package = source_root / "guidance"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\n"
        "name: guidance\n"
        "description: deterministic session guidance\n"
        "---\n"
        f"{SKILL_GUIDANCE_MARKER}\n",
        encoding="utf-8",
    )
    (tmp_path / "probe.txt").write_text(TOOL_OUTPUT_MARKER, encoding="utf-8")

    manager = MemoryManager(service, skill_roots={"test": source_root})
    service.memory_manager = manager
    try:
        project_result = await _command(
            manager,
            action="project_create",
            name="B2-3 Session Context Project",
            workspace_path=str(tmp_path),
        )
        project_id = project_result.state.projects[-1].project_id

        install_result = await _command(
            manager,
            action="plugin_install",
            plugin_id="memory-standard",
            mode="trusted_in_process",
        )
        installation_id = install_result.state.installations[-1].installation_id
        await _command(
            manager,
            action="binding_select",
            project_id=project_id,
            installation_id=installation_id,
        )
        memory_result = await _command(
            manager,
            action="memory_save",
            project_id=project_id,
            content=MEMORY_BODY_MARKER,
            confirmed=True,
        )
        assert memory_result.state.records

        await _command(manager, action="skill_discover")
        catalog = manager.projection().skill_catalog
        assert len(catalog) == 1
        skill_result = await _command(
            manager,
            action="skill_install",
            package_ref=catalog[0].package_ref,
        )
        skill = skill_result.state.skills[0]
        await _command(
            manager,
            action="skill_enable",
            skill_id=skill.skill_id,
            project_id=project_id,
        )
        yield service, manager, provider, role.id, project_id, skill.skill_id, tmp_path
    finally:
        await manager.close()
        service.close()


async def _run_session(
    service: ApplicationService,
    role_id: str,
    workspace: Path,
) -> tuple[list[object], str]:
    thread = service.create_thread(ConversationThread(workspace_ref=str(workspace.resolve())))
    session = service.create_session(role_id, thread_id=thread.id)
    events = [
        event
        async for event in service.run_session(
            session.id,
            user_message=USER_MESSAGE,
            workspace=workspace,
            thread_id=thread.id,
        )
    ]
    return events, session.id


@pytest.mark.asyncio
async def test_session_context_keeps_tools_and_history_when_memory_is_off(
    installed_project,
) -> None:
    service, manager, provider, role_id, project_id, skill_id, workspace = installed_project

    switched = await _command(manager, action="memory_switch", enabled=False)
    assert switched.state.global_enabled is False

    first_request_index = len(provider.requests)
    events, session_id = await _run_session(service, role_id, workspace)
    assert events[-1].event_type == "agent.completed"
    first_requests = provider.requests[first_request_index:]
    assert len(first_requests) == 2

    messages = [message for request, _tools in first_requests for message in request]
    serialized_messages = "\n".join(message.model_dump_json() for message in messages)
    assert SKILL_GUIDANCE_MARKER in serialized_messages
    assert MEMORY_BODY_MARKER not in serialized_messages
    assert any(
        message.role is MessageRole.USER and message.content == USER_MESSAGE for message in messages
    )
    assert any(
        message.role is MessageRole.TOOL and TOOL_OUTPUT_MARKER in (message.content or "")
        for message in messages
    )
    assert all(
        {tool.name for tool in tools} == {"read_file"} for _messages, tools in first_requests
    )

    thread_id = service.get_thread(
        next(
            thread.id
            for thread in service.list_threads(workspace_ref=str(workspace.resolve()))
            if any(ref.source_id == session_id for ref in thread.legacy_refs)
        )
    ).id
    history = service.list_items(thread_id)
    assert any(
        isinstance(item.payload, UserMessagePayload) and item.payload.text == USER_MESSAGE
        for item in history
    )

    revisions = service.list_context_revisions(session_id)
    assert len(revisions) == len(first_requests)
    for revision, (request_messages, request_tools) in zip(revisions, first_requests, strict=True):
        assert service.get_context_revision(session_id, revision.id) == revision
        assert revision.messages == request_messages
        assert revision.tools == request_tools
        assert revision.thread_id == thread_id
        assert revision.memory_refs == ()
        assert MEMORY_BODY_MARKER not in revision.model_dump_json()

    await _command(
        manager,
        action="skill_disable",
        skill_id=skill_id,
        project_id=project_id,
    )
    second_request_index = len(provider.requests)
    events, second_session_id = await _run_session(service, role_id, workspace)
    assert events[-1].event_type == "agent.completed"
    second_requests = provider.requests[second_request_index:]
    assert len(second_requests) == 2
    second_serialized = "\n".join(
        message.model_dump_json() for request, _tools in second_requests for message in request
    )
    assert SKILL_GUIDANCE_MARKER not in second_serialized
    assert all(
        {tool.name for tool in tools} == {"read_file"} for _messages, tools in second_requests
    )
    assert service.list_context_revisions(second_session_id)
