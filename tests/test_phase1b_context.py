from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import AsyncIterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import operant.api as api_module
import operant.application.context as context_module
import operant.application.service as service_module
from operant.api import create_app
from operant.application.context import PersistentContextComposer
from operant.application.service import ApplicationService
from operant.domain.context import (
    CompactionSourceType,
    ContextReferenceType,
    ContextSourceType,
    ContextWatermarkState,
    ReferenceIncludeMode,
    ReferenceRequest,
    deterministic_compaction_id,
)
from operant.domain.memory import Memory, MemoryKind, MemoryStatus
from operant.domain.messages import (
    Message,
    MessageRole,
    ModelResponse,
    ModelUsage,
    ProviderEvent,
    ToolCall,
    ToolDefinition,
)
from operant.domain.models import AgentStatus, Budget, ModelProfile, RolePreset, Session, ToolPolicy
from operant.domain.threads import (
    ArtifactSensitivity,
    ConversationThread,
    Item,
    Turn,
    UserMessagePayload,
)
from operant.persistence.sqlite import SQLiteStore
from operant.providers.base import ModelProvider
from operant.runtime.loop import RuntimeEvent
from operant.tools.workspace import WorkspaceTools


class CapturingProvider(ModelProvider):
    def __init__(self, responses: Sequence[ModelResponse] | None = None) -> None:
        self.responses = list(responses or [ModelResponse(content="done", finish_reason="stop")])
        self.received_messages: list[tuple[Message, ...]] = []
        self.received_tools: list[tuple[ToolDefinition, ...]] = []

    async def list_models(self, *, base_url: str, secret_ref: str) -> list[str]:
        del base_url, secret_ref
        return ["phase1b-model"]

    async def stream(
        self,
        *,
        snapshot: Any,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        del snapshot
        self.received_messages.append(tuple(messages))
        self.received_tools.append(tuple(tools))
        response = self.responses.pop(0)
        if response.usage is None:
            response = response.model_copy(
                update={
                    "usage": ModelUsage(
                        prompt_tokens=100,
                        completion_tokens=10,
                        total_tokens=110,
                    )
                }
            )
        yield ProviderEvent(event_type="model.completed", response=response)


class FailingProvider(CapturingProvider):
    async def stream(
        self,
        *,
        snapshot: Any,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        del snapshot
        self.received_messages.append(tuple(messages))
        self.received_tools.append(tuple(tools))
        raise RuntimeError("provider failed after Context persistence")
        yield  # pragma: no cover


def _service(
    tmp_path: Path,
    provider: ModelProvider,
    *,
    context_window: int | None = 16_384,
    max_turns: int = 4,
    max_output_tokens: int | None = 1_024,
    tools: tuple[str, ...] = (),
) -> tuple[ApplicationService, Session]:
    store = SQLiteStore(tmp_path / "operant.sqlite3")
    service = ApplicationService(store, provider, artifact_root=tmp_path / "artifacts")
    service.initialize()
    profile = service.add_model_profile(
        ModelProfile(
            name="phase1b-model",
            model_id="phase1b-model",
            base_url="https://example.invalid/v1",
            secret_ref="OPERANT_PHASE1B_TEST_KEY",
            context_window=context_window,
        )
    )
    role = service.create_role(
        RolePreset(
            name="Phase 1B Role",
            system_prompt="Use only authorized Context.",
            model_profile_id=profile.id,
            memory_scope="read: [project]; write: []",
            budget=Budget(max_turns=max_turns, max_output_tokens=max_output_tokens),
            tool_policy=ToolPolicy(allowed_tools=tools),
        )
    )
    return service, service.create_session(role.id)


async def _run(
    service: ApplicationService,
    session: Session,
    workspace: Path,
    **kwargs: Any,
) -> list[RuntimeEvent]:
    return [
        event
        async for event in service.run_session(
            session.id,
            user_message="Inspect the authorized Context.",
            workspace=workspace,
            **kwargs,
        )
    ]


def test_context_window_is_frozen_and_old_snapshots_remain_unknown(tmp_path: Path) -> None:
    provider = CapturingProvider()
    service, session = _service(tmp_path, provider, context_window=8_192)
    assert session.role_snapshot.context_window == 8_192

    updated = service.update_model_profile(
        session.role_snapshot.model_profile_id,
        context_window=32_768,
    )
    new_session = service.create_session(session.role_snapshot.role_id)
    assert updated.context_window == 32_768
    assert session.role_snapshot.context_window == 8_192
    assert new_session.role_snapshot.context_window == 32_768

    legacy_payload = session.model_dump(mode="json")
    legacy_payload["role_snapshot"].pop("context_window")
    legacy = Session.model_validate(legacy_payload)
    assert legacy.role_snapshot.context_window is None


@pytest.mark.asyncio
async def test_legacy_session_run_persists_exact_context_revision(tmp_path: Path) -> None:
    provider = CapturingProvider()
    service, session = _service(tmp_path, provider)

    events = await _run(service, session, tmp_path)
    revisions = service.list_context_revisions(session.id)

    assert events[-1].event_type == "agent.completed"
    assert len(revisions) == len(provider.received_messages) == 1
    revision = revisions[0]
    assert revision.thread_id is None
    assert revision.messages == provider.received_messages[0]
    assert revision.tools == provider.received_tools[0]
    assert revision.watermark.state is ContextWatermarkState.GREEN
    assert revision.watermark.input_token_estimate is not None
    model_event = next(event for event in events if event.event_type == "model.completed")
    assert model_event.payload["context_revision_id"] == revision.id


@pytest.mark.asyncio
async def test_old_snapshot_context_limit_is_unknown_not_zero(tmp_path: Path) -> None:
    provider = CapturingProvider()
    service, session = _service(tmp_path, provider, context_window=None)

    await _run(service, session, tmp_path)
    revision = service.list_context_revisions(session.id)[0]

    assert revision.watermark.state is ContextWatermarkState.UNKNOWN
    assert revision.watermark.context_window is None
    assert revision.watermark.available_input_tokens is None
    assert revision.watermark.input_token_estimate is not None
    assert revision.watermark.input_token_estimate > 0


@pytest.mark.asyncio
async def test_unknown_output_reserve_keeps_watermark_unknown(tmp_path: Path) -> None:
    provider = CapturingProvider()
    service, session = _service(
        tmp_path,
        provider,
        context_window=16_384,
        max_output_tokens=None,
    )

    await _run(service, session, tmp_path)
    watermark = service.list_context_revisions(session.id)[0].watermark

    assert watermark.state is ContextWatermarkState.UNKNOWN
    assert watermark.context_window == 16_384
    assert watermark.reserved_output_tokens is None
    assert watermark.safety_margin_tokens is None
    assert watermark.available_input_tokens is None


@pytest.mark.asyncio
async def test_first_request_emergency_does_not_invent_compaction_cursors(tmp_path: Path) -> None:
    provider = CapturingProvider()
    service, session = _service(
        tmp_path,
        provider,
        context_window=1_200,
        max_output_tokens=1_000,
    )

    events = await _run(service, session, tmp_path)

    assert events[-1].event_type == "agent.failed"
    assert events[-1].payload == {"error_type": "ContextLimitExceeded"}
    assert provider.received_messages == []
    assert service.list_context_revisions(session.id) == []


@pytest.mark.asyncio
async def test_context_snapshot_and_provider_input_share_the_same_secret_safe_payload(
    tmp_path: Path,
) -> None:
    secret = "phase1b-super-secret-value"
    provider = CapturingProvider(
        (
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="secret_args",
                        name="read_file",
                        arguments_json=json.dumps({"path": "missing.txt", "password": secret}),
                    ),
                ),
                finish_reason="tool_calls",
            ),
            ModelResponse(content="done", finish_reason="stop"),
        )
    )
    service, session = _service(tmp_path, provider, tools=("read_file",))

    events = [
        event
        async for event in service.run_session(
            session.id,
            user_message=f"DATABASE_PASSWORD={secret}",
            workspace=tmp_path,
        )
    ]
    revisions = service.list_context_revisions(session.id)

    assert events[-1].event_type == "agent.completed"
    assert len(revisions) == len(provider.received_messages) == 2
    for revision, sent in zip(revisions, provider.received_messages, strict=True):
        assert revision.messages == sent
        assert secret not in revision.model_dump_json()
    with sqlite3.connect(service.store.path) as connection:
        stored = "\n".join(
            str(row[0])
            for row in connection.execute(
                "SELECT messages_json FROM context_revisions "
                "UNION ALL SELECT content FROM prompt_blocks"
            ).fetchall()
        )
    assert secret not in stored
    assert "[REDACTED]" in stored


@pytest.mark.asyncio
async def test_composer_construction_failure_is_safe_and_releases_the_run_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "composer-construction-secret-value"
    provider = CapturingProvider()
    service, session = _service(tmp_path, provider)

    class FailingComposer:
        def __init__(self, **_kwargs: Any) -> None:
            raise RuntimeError(f"DATABASE_PASSWORD={secret}")

    monkeypatch.setattr(service_module, "PersistentContextComposer", FailingComposer)
    events = await _run(service, session, tmp_path)

    assert len(events) == 1
    assert events[0].event_type == "agent.failed"
    assert events[0].payload == {"error_type": "RuntimeError"}
    assert secret not in events[0].model_dump_json()
    assert provider.received_messages == []
    assert service.admitted_session_run_lease(session.id) is None
    assert service.store.get_session_run_lease(session.id).released_at is not None
    with sqlite3.connect(service.store.path) as connection:
        agent_status, stored_event = connection.execute(
            """
            SELECT a.status, e.body
            FROM agents AS a
            JOIN events AS e ON e.agent_id = a.id
            WHERE a.session_id = ? AND e.event_type = 'agent.failed'
            """,
            (session.id,),
        ).fetchone()
    assert agent_status == AgentStatus.FAILED.value
    assert secret not in stored_event


@pytest.mark.asyncio
async def test_agent_factory_failure_releases_lease_and_records_session_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = CapturingProvider()
    service, session = _service(tmp_path, provider)

    def fail_create_agent(_session_id: str) -> Any:
        raise RuntimeError("injected AgentFactory failure")

    monkeypatch.setattr(service.factory, "create_agent", fail_create_agent)
    events = await _run(service, session, tmp_path)

    assert len(events) == 1
    assert events[0].event_type == "session.run_failed"
    assert events[0].payload == {"error_type": "RuntimeError"}
    assert provider.received_messages == []
    assert service.admitted_session_run_lease(session.id) is None
    with sqlite3.connect(service.store.path) as connection:
        event_row = connection.execute(
            "SELECT agent_id, event_type, body FROM events WHERE session_id = ?",
            (session.id,),
        ).fetchone()
        agent_count = connection.execute(
            "SELECT COUNT(*) FROM agents WHERE session_id = ?",
            (session.id,),
        ).fetchone()
    assert event_row is not None
    assert event_row[0] is None
    assert event_row[1] == "session.run_failed"
    assert json.loads(event_row[2]) == {"turn": 0, "error_type": "RuntimeError"}
    assert agent_count == (0,)
    assert service.store.get_session_run_lease(session.id).released_at is not None


@pytest.mark.asyncio
async def test_secret_shaped_workspace_and_custom_tool_are_redacted_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_secret = "workspace-reviewer-secret"
    tool_secret = "sk-reviewertoolsecret1234567890"
    plain_password = "plain-password-value"
    plain_api_key = "plain-api-key-value"
    plain_authorization = "plain authorization value"
    nested_secret = "nested ordinary client secret"
    listed_secret = "listed ordinary refresh token"
    workspace = tmp_path / f"DATABASE_PASSWORD={workspace_secret}"
    workspace.mkdir()
    (workspace / "large.txt").write_text("x" * 2_000)
    custom_tool = ToolDefinition(
        name="read_file",
        description=f"Read a file with password={tool_secret}",
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": f"authorization={tool_secret}",
                }
            },
            "required": ["path"],
            "password": plain_password,
            "api_key": plain_api_key,
            "authorization": plain_authorization,
            f"password={tool_secret}": "ordinary value under a raw sensitive key",
            "nested": {
                "client_secret": nested_secret,
                "items": [{"refresh_token": listed_secret}],
            },
            "x_api_key": tool_secret,
        },
    )
    monkeypatch.setattr(WorkspaceTools, "definitions", lambda _self: (custom_tool,))
    responses = [
        ModelResponse(
            tool_calls=(
                ToolCall(
                    id=f"secret_read_{index}",
                    name="read_file",
                    arguments_json='{"path":"large.txt"}',
                ),
            ),
            finish_reason="tool_calls",
        )
        for index in range(7)
    ]
    responses.append(ModelResponse(content="done", finish_reason="stop"))
    provider = CapturingProvider(responses)
    service, session = _service(
        tmp_path,
        provider,
        context_window=5_000,
        max_turns=8,
        tools=("read_file",),
    )

    events = await _run(service, session, workspace)
    revisions = service.list_context_revisions(session.id)

    assert events[-1].event_type == "agent.completed"
    assert len(revisions) == len(provider.received_messages) == len(provider.received_tools)
    for revision, sent_messages, sent_tools in zip(
        revisions,
        provider.received_messages,
        provider.received_tools,
        strict=True,
    ):
        assert revision.messages == sent_messages
        assert revision.tools == sent_tools
        serialized_messages = json.dumps(
            [message.model_dump(mode="json") for message in sent_messages],
            ensure_ascii=False,
        )
        serialized_tools = json.dumps(
            [tool.model_dump(mode="json") for tool in sent_tools],
            ensure_ascii=False,
        )
        serialized_blocks = json.dumps(
            [block.model_dump(mode="json") for block in revision.blocks],
            ensure_ascii=False,
            default=str,
        )
        for value in (serialized_messages, serialized_tools, serialized_blocks):
            for secret in (
                workspace_secret,
                tool_secret,
                plain_password,
                plain_api_key,
                plain_authorization,
                nested_secret,
                listed_secret,
            ):
                assert secret not in value
        safe_parameters = sent_tools[0].parameters
        assert safe_parameters["password"] == "[REDACTED]"
        assert safe_parameters["api_key"] == "[REDACTED]"
        assert safe_parameters["authorization"] == "[REDACTED]"
        assert safe_parameters["password=[REDACTED]"] == "[REDACTED]"
        assert safe_parameters["nested"]["client_secret"] == "[REDACTED]"
        assert safe_parameters["nested"]["items"][0]["refresh_token"] == "[REDACTED]"

    compacted = next(revision for revision in revisions if revision.compaction_id is not None)
    compaction = service.store.get_compaction(compacted.compaction_id or "")
    compaction_json = compaction.model_dump_json()
    assert workspace_secret not in compaction_json
    assert tool_secret not in compaction_json
    assert "[REDACTED]" in compaction_json
    with sqlite3.connect(service.store.path) as connection:
        persisted = "\n".join(
            str(row[0])
            for row in connection.execute(
                "SELECT body FROM events "
                "UNION ALL SELECT messages_json FROM context_revisions "
                "UNION ALL SELECT tools_json FROM context_revisions "
                "UNION ALL SELECT content FROM prompt_blocks "
                "UNION ALL SELECT summary_json FROM compactions"
            ).fetchall()
        )
    for secret in (
        workspace_secret,
        tool_secret,
        plain_password,
        plain_api_key,
        plain_authorization,
        nested_secret,
        listed_secret,
    ):
        assert secret not in persisted


@pytest.mark.asyncio
async def test_tool_schema_key_collision_after_redaction_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_key_secret = "sk-collisionsecret1111111111"
    second_key_secret = "sk-collisionsecret2222222222"
    custom_tool = ToolDefinition(
        name="read_file",
        description="Collision safety probe.",
        parameters={
            "type": "object",
            "nested": {
                f"label={first_key_secret}": "first ordinary value",
                f"label={second_key_secret}": "second ordinary value",
            },
        },
    )
    monkeypatch.setattr(WorkspaceTools, "definitions", lambda _self: (custom_tool,))
    provider = CapturingProvider()
    service, session = _service(tmp_path, provider, tools=("read_file",))

    events = await _run(service, session, tmp_path)

    assert events[-1].event_type == "agent.failed"
    assert events[-1].payload == {"error_type": "ContextCompositionError"}
    assert provider.received_messages == []
    assert provider.received_tools == []
    assert service.list_context_revisions(session.id) == []
    stored_events = json.dumps(
        [event.model_dump(mode="json") for event in service.list_events(session.id)],
        ensure_ascii=False,
    )
    assert first_key_secret not in stored_events
    assert second_key_secret not in stored_events


@pytest.mark.asyncio
async def test_thread_item_artifact_and_memory_references_are_typed_and_explainable(
    tmp_path: Path,
) -> None:
    provider = CapturingProvider()
    service, session = _service(tmp_path, provider)
    thread = service.create_thread(ConversationThread(workspace_ref=str(tmp_path.resolve())))
    turn = service.create_turn(Turn(thread_id=thread.id))
    item = service.append_item(
        Item(
            thread_id=thread.id,
            turn_id=turn.id,
            payload=UserMessagePayload(text="Canonical user fact."),
        )
    )
    artifact, _created = service.create_artifact(
        content=b"metadata only",
        media_type="text/plain",
    )
    memory = service.store.create_memory(
        Memory(
            kind=MemoryKind.PROJECT,
            content="Authorized project convention.",
            project_scope=str(tmp_path.resolve()),
            status=MemoryStatus.ACTIVE,
        )
    )
    references = (
        ReferenceRequest(
            ref_type=ContextReferenceType.ITEM,
            target_id=item.id,
        ),
        ReferenceRequest(
            ref_type=ContextReferenceType.ARTIFACT,
            target_id=artifact.id,
            include_mode=ReferenceIncludeMode.METADATA,
        ),
        ReferenceRequest(
            ref_type=ContextReferenceType.MEMORY,
            target_id=memory.id,
        ),
    )

    events = await _run(
        service,
        session,
        tmp_path,
        thread_id=thread.id,
        references=references,
    )
    revision = service.list_context_revisions(session.id)[0]

    assert events[-1].event_type == "agent.completed"
    assert revision.thread_id == thread.id
    assert [binding.ref_type for binding in revision.reference_bindings] == [
        ContextReferenceType.THREAD,
        ContextReferenceType.ITEM,
        ContextReferenceType.ARTIFACT,
        ContextReferenceType.MEMORY,
    ]
    sent = json.dumps(
        [message.model_dump(mode="json") for message in provider.received_messages[0]],
        ensure_ascii=False,
    )
    assert "Canonical user fact." in sent
    assert "Authorized project convention." in sent
    assert "metadata only" not in sent
    assert artifact.content_hash in sent
    assert thread.cursor is not None and item.cursor is not None
    assert revision.source_cursor_start == item.cursor
    assert revision.source_cursor_end == item.cursor
    assert revision.source_cursor_namespace == "items.sequence"
    assert revision.agent_instance_id == revision.agent_id
    assert revision.source_item_ids == (item.id,)
    assert revision.artifact_refs == (artifact.id,)
    assert revision.memory_refs == (memory.id,)
    assert revision.compaction_refs == ()
    assert revision.token_estimate == revision.watermark.input_token_estimate
    assert len(revision.message_ids) == len(revision.messages)
    assert len(set(revision.message_ids)) == len(revision.message_ids)
    assert service.store.get_context_revision(revision.id) == revision
    assert service.list_context_revisions(session.id) == [revision]
    assert all(block.source_refs for block in revision.blocks)
    sources = {
        source.source_type: source for block in revision.blocks for source in block.source_refs
    }
    with sqlite3.connect(service.store.path) as connection:
        thread_body = connection.execute(
            "SELECT body FROM threads WHERE id = ?", (thread.id,)
        ).fetchone()[0]
        item_body = connection.execute(
            "SELECT body FROM items WHERE id = ?", (item.id,)
        ).fetchone()[0]
        memory_body = connection.execute(
            """
            SELECT mv.body
            FROM memories AS m
            JOIN memory_versions AS mv
                ON mv.memory_id = m.id AND mv.version = m.current_version
            WHERE m.id = ?
            """,
            (memory.id,),
        ).fetchone()[0]
        tools_json = connection.execute(
            "SELECT tools_json FROM context_revisions WHERE id = ?", (revision.id,)
        ).fetchone()[0]
    assert (
        sources[ContextSourceType.MEMORY].content_hash
        == revision.reference_bindings[-1].source_snapshot_hash
    )
    assert (
        sources[ContextSourceType.THREAD].content_hash
        == hashlib.sha256(thread_body.encode("utf-8")).hexdigest()
    )
    assert (
        sources[ContextSourceType.ITEM].content_hash
        == hashlib.sha256(item_body.encode("utf-8")).hexdigest()
    )
    assert (
        sources[ContextSourceType.MEMORY].content_hash
        == hashlib.sha256(memory_body.encode("utf-8")).hexdigest()
    )
    assert sources[ContextSourceType.ARTIFACT].content_hash == artifact.content_hash
    assert (
        sources[ContextSourceType.TOOL_SCHEMA].content_hash
        == hashlib.sha256(tools_json.encode("utf-8")).hexdigest()
    )
    for block in revision.blocks:
        for source in block.source_refs:
            if source.source_type in {ContextSourceType.SESSION, ContextSourceType.AGENT}:
                assert source.content_hash == block.content_hash


@pytest.mark.asyncio
async def test_unknown_thread_capacity_remains_nonzero_but_cumulative_source_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(context_module, "_THREAD_SOURCE_HARD_BYTES", 6_000)
    monkeypatch.setattr(context_module, "_THREAD_SOURCE_HARD_TOKENS", 1_500)
    provider = CapturingProvider(
        (
            ModelResponse(content="bounded", finish_reason="stop"),
            ModelResponse(content="must-not-run", finish_reason="stop"),
        )
    )
    service, session = _service(tmp_path, provider, context_window=None)
    thread = service.create_thread(ConversationThread(workspace_ref=str(tmp_path.resolve())))
    turn = service.create_turn(Turn(thread_id=thread.id))
    for index in range(4):
        service.append_item(
            Item(
                thread_id=thread.id,
                turn_id=turn.id,
                payload=UserMessagePayload(text=f"bounded-{index}-" + "x" * 160),
            )
        )

    first_events = await _run(service, session, tmp_path, thread_id=thread.id)
    first_revision = service.list_context_revisions(session.id)[0]

    assert first_events[-1].event_type == "agent.completed"
    assert first_revision.watermark.state is ContextWatermarkState.UNKNOWN
    assert first_revision.watermark.available_input_tokens is None
    assert first_revision.watermark.input_token_estimate is not None
    assert first_revision.watermark.input_token_estimate > 0
    assert len(provider.received_messages) == 1

    for index in range(24):
        service.append_item(
            Item(
                thread_id=thread.id,
                turn_id=turn.id,
                payload=UserMessagePayload(text=f"overflow-{index}-" + "y" * 320),
            )
        )
    second_events = await _run(service, session, tmp_path, thread_id=thread.id)

    assert second_events[-1].event_type == "agent.failed"
    assert second_events[-1].payload == {"error_type": "ContextLimitExceeded"}
    assert len(provider.received_messages) == 1
    assert service.list_context_revisions(session.id) == [first_revision]


@pytest.mark.asyncio
async def test_large_thread_first_request_uses_exact_thread_item_compaction_evidence(
    tmp_path: Path,
) -> None:
    provider = CapturingProvider()
    service, session = _service(
        tmp_path,
        provider,
        context_window=12_000,
        max_output_tokens=1_024,
    )
    thread = service.create_thread(ConversationThread(workspace_ref=str(tmp_path.resolve())))
    turn = service.create_turn(Turn(thread_id=thread.id))
    canonical_items = [
        service.append_item(
            Item(
                thread_id=thread.id,
                turn_id=turn.id,
                payload=UserMessagePayload(text=f"canonical-{index}-" + "z" * 800),
            )
        )
        for index in range(50)
    ]

    events = await _run(service, session, tmp_path, thread_id=thread.id)
    revision = service.list_context_revisions(session.id)[0]
    assert revision.compaction_id is not None
    compaction = service.store.get_compaction(revision.compaction_id)

    assert events[-1].event_type == "agent.completed"
    assert len(provider.received_messages) == 1
    assert compaction.source_type is CompactionSourceType.THREAD_ITEMS
    assert compaction.id == deterministic_compaction_id(compaction)
    assert compaction.thread_id == thread.id
    assert [ref.source_id for ref in compaction.covered_item_refs] == [
        item.id for item in canonical_items
    ]
    assert [ref.cursor for ref in compaction.covered_item_refs] == [
        item.cursor for item in canonical_items
    ]
    assert [ref.content_hash for ref in compaction.covered_item_refs] == [
        hashlib.sha256(
            item.model_copy(update={"cursor": None}).model_dump_json().encode("utf-8")
        ).hexdigest()
        for item in canonical_items
    ]
    assert compaction.source_cursor_start == canonical_items[0].cursor
    assert compaction.source_cursor_end == canonical_items[-1].cursor
    assert revision.source_item_ids == tuple(item.id for item in canonical_items)
    assert revision.source_cursor_start == canonical_items[0].cursor
    assert revision.source_cursor_end == canonical_items[-1].cursor
    assert revision.source_cursor_namespace == "items.sequence"
    assert service.list_items(thread.id) == canonical_items


@pytest.mark.asyncio
async def test_thread_item_compaction_is_reused_across_requests(
    tmp_path: Path,
) -> None:
    (tmp_path / "probe.txt").write_text("probe", encoding="utf-8")
    provider = CapturingProvider(
        (
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="reuse_probe",
                        name="read_file",
                        arguments_json=json.dumps({"path": "probe.txt"}),
                    ),
                ),
                finish_reason="tool_calls",
            ),
            ModelResponse(content="done", finish_reason="stop"),
        )
    )
    service, session = _service(
        tmp_path,
        provider,
        context_window=12_000,
        max_turns=2,
        tools=("read_file",),
    )
    thread = service.create_thread(ConversationThread(workspace_ref=str(tmp_path.resolve())))
    turn = service.create_turn(Turn(thread_id=thread.id))
    for index in range(50):
        service.append_item(
            Item(
                thread_id=thread.id,
                turn_id=turn.id,
                payload=UserMessagePayload(text=f"canonical-{index}-" + "z" * 800),
            )
        )

    events = await _run(service, session, tmp_path, thread_id=thread.id)
    revisions = service.list_context_revisions(session.id)

    assert events[-1].event_type == "agent.completed"
    assert len(revisions) == 2
    assert revisions[0].compaction_id is not None
    assert revisions[1].compaction_id == revisions[0].compaction_id
    with sqlite3.connect(service.store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM compactions").fetchone() == (1,)


def test_concurrent_thread_item_compaction_is_idempotent_across_revisions(
    tmp_path: Path,
) -> None:
    provider = CapturingProvider()
    service, session = _service(
        tmp_path,
        provider,
        context_window=12_000,
        max_output_tokens=1_024,
    )
    agent = service.store.create_agent(session.id)
    thread = service.create_thread(ConversationThread(workspace_ref=str(tmp_path.resolve())))
    turn = service.create_turn(Turn(thread_id=thread.id))
    for index in range(50):
        service.append_item(
            Item(
                thread_id=thread.id,
                turn_id=turn.id,
                payload=UserMessagePayload(text=f"parallel-{index}-" + "z" * 800),
            )
        )

    messages = (
        Message(role=MessageRole.SYSTEM, content=session.role_snapshot.system_prompt),
        Message(role=MessageRole.USER, content="compose concurrently"),
    )

    def compose(request_ordinal: int) -> str:
        composer = PersistentContextComposer(
            store=service.store,
            session=session,
            agent_id=agent.id,
            workspace=tmp_path,
            thread_id=thread.id,
            memory_resolver=lambda _memory_id: (_ for _ in ()).throw(AssertionError()),
            artifact_reader=lambda _artifact_id: (_ for _ in ()).throw(AssertionError()),
            artifact_writer=lambda content: service._write_tool_result_artifact(content=content),
        )
        return (
            composer.compose(
                snapshot=session.role_snapshot,
                messages=messages,
                tools=(),
                request_ordinal=request_ordinal,
            ).revision.compaction_id
            or ""
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        compaction_ids = list(executor.map(compose, (1, 2)))

    assert compaction_ids[0]
    assert compaction_ids[0] == compaction_ids[1]
    assert len(service.list_context_revisions(session.id)) == 2
    with sqlite3.connect(service.store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM compactions").fetchone() == (1,)


@pytest.mark.asyncio
async def test_empty_child_thread_does_not_inherit_parent_or_mix_cursor_namespaces(
    tmp_path: Path,
) -> None:
    provider = CapturingProvider()
    service, session = _service(tmp_path, provider)
    workspace = str(tmp_path.resolve())
    parent = service.create_thread(ConversationThread(workspace_ref=workspace))
    parent_turn = service.create_turn(Turn(thread_id=parent.id))
    service.append_item(
        Item(
            thread_id=parent.id,
            turn_id=parent_turn.id,
            payload=UserMessagePayload(text="parent-only canonical fact"),
        )
    )
    child = service.create_thread(
        ConversationThread(parent_thread_id=parent.id, workspace_ref=workspace)
    )

    events = await _run(service, session, tmp_path, thread_id=child.id)
    revision = service.list_context_revisions(session.id)[0]

    assert events[-1].event_type == "agent.completed"
    assert "parent-only canonical fact" not in json.dumps(
        [message.model_dump(mode="json") for message in provider.received_messages[0]],
        ensure_ascii=False,
    )
    assert revision.source_cursor_start is None
    assert revision.source_cursor_end is None
    thread_sources = [
        source
        for block in revision.blocks
        for source in block.source_refs
        if source.source_type.value == "thread"
    ]
    assert len(thread_sources) == 1
    assert thread_sources[0].cursor == child.cursor


@pytest.mark.asyncio
async def test_restricted_artifact_and_nonactive_memory_references_fail_closed(
    tmp_path: Path,
) -> None:
    provider = CapturingProvider()
    service, session = _service(tmp_path, provider)
    restricted, _created = service.create_artifact(
        content=b"restricted",
        media_type="text/plain",
        sensitivity=ArtifactSensitivity.RESTRICTED,
    )
    sensitive, _created = service.create_artifact(
        content=b"sensitive",
        media_type="text/plain",
        sensitivity=ArtifactSensitivity.SENSITIVE,
    )
    candidate = service.store.create_memory(
        Memory(
            kind=MemoryKind.PROJECT,
            content="not confirmed for model use",
            project_scope=str(tmp_path.resolve()),
        )
    )
    other_project = service.store.create_memory(
        Memory(
            kind=MemoryKind.PROJECT,
            content="active but belongs to another project",
            project_scope=str((tmp_path / "other").resolve()),
            status=MemoryStatus.ACTIVE,
        )
    )

    for reference in (
        ReferenceRequest(
            ref_type=ContextReferenceType.ARTIFACT,
            target_id=restricted.id,
            include_mode=ReferenceIncludeMode.METADATA,
        ),
        ReferenceRequest(
            ref_type=ContextReferenceType.ARTIFACT,
            target_id=sensitive.id,
            include_mode=ReferenceIncludeMode.INLINE,
        ),
        ReferenceRequest(
            ref_type=ContextReferenceType.MEMORY,
            target_id=candidate.id,
        ),
        ReferenceRequest(
            ref_type=ContextReferenceType.MEMORY,
            target_id=other_project.id,
        ),
    ):
        events = await _run(service, session, tmp_path, references=(reference,))
        assert events[-1].event_type == "agent.failed"
        assert events[-1].payload == {"error_type": "PermissionError"}

    assert provider.received_messages == []
    assert service.list_context_revisions(session.id) == []


@pytest.mark.asyncio
async def test_reference_user_text_is_redacted_before_binding_persistence(tmp_path: Path) -> None:
    provider = CapturingProvider()
    service, session = _service(tmp_path, provider)
    artifact, _created = service.create_artifact(
        content=b"safe metadata",
        media_type="text/plain",
    )
    secret = "binding-user-text-secret"
    reference = ReferenceRequest(
        ref_type=ContextReferenceType.ARTIFACT,
        target_id=artifact.id,
        user_text=f"password={secret}",
        include_mode=ReferenceIncludeMode.METADATA,
    )

    await _run(service, session, tmp_path, references=(reference,))
    revision = service.list_context_revisions(session.id)[0]

    assert revision.reference_bindings[0].user_text == "password=[REDACTED]"
    assert secret not in revision.model_dump_json()
    with sqlite3.connect(service.store.path) as connection:
        stored = connection.execute(
            "SELECT user_text FROM reference_bindings WHERE revision_id = ?",
            (revision.id,),
        ).fetchone()
    assert stored == ("password=[REDACTED]",)


@pytest.mark.asyncio
async def test_reference_permission_failure_is_safe_and_calls_no_provider(tmp_path: Path) -> None:
    provider = CapturingProvider()
    service, session = _service(tmp_path, provider)
    thread = service.create_thread(
        ConversationThread(workspace_ref=str((tmp_path / "other").resolve()))
    )

    events = await _run(service, session, tmp_path, thread_id=thread.id)

    assert events[-1].event_type == "agent.failed"
    assert events[-1].payload == {"error_type": "PermissionError"}
    assert provider.received_messages == []
    assert service.list_context_revisions(session.id) == []


@pytest.mark.asyncio
async def test_context_revision_survives_provider_failure_for_recovery_audit(
    tmp_path: Path,
) -> None:
    provider = FailingProvider()
    service, session = _service(tmp_path, provider)

    events = await _run(service, session, tmp_path)

    assert events[-1].event_type == "agent.failed"
    revisions = service.list_context_revisions(session.id)
    assert len(revisions) == 1
    assert revisions[0].messages == provider.received_messages[0]


@pytest.mark.asyncio
async def test_dynamic_watermark_compacts_append_only_without_changing_thread_history(
    tmp_path: Path,
) -> None:
    large_file = tmp_path / "large.txt"
    large_file.write_text("x" * 2_000)
    responses = [
        ModelResponse(
            tool_calls=(
                ToolCall(
                    id=f"read_{index}",
                    name="read_file",
                    arguments_json='{"path":"large.txt"}',
                ),
            ),
            finish_reason="tool_calls",
        )
        for index in range(7)
    ]
    responses.append(ModelResponse(content="done", finish_reason="stop"))
    provider = CapturingProvider(responses)
    service, session = _service(
        tmp_path,
        provider,
        context_window=5_000,
        max_turns=8,
        tools=("read_file",),
    )
    thread = service.create_thread(ConversationThread(workspace_ref=str(tmp_path.resolve())))
    turn = service.create_turn(Turn(thread_id=thread.id))
    item = service.append_item(
        Item(
            thread_id=thread.id,
            turn_id=turn.id,
            payload=UserMessagePayload(text="Canonical history must remain."),
        )
    )

    events = await _run(service, session, tmp_path, thread_id=thread.id)
    revisions = service.list_context_revisions(session.id)

    assert events[-1].event_type == "agent.completed"
    compacted = [revision for revision in revisions if revision.compaction_id is not None]
    assert compacted
    compacted_revision = compacted[0]
    compaction = service.store.get_compaction(compacted_revision.compaction_id or "")
    prior = [
        revision
        for revision in revisions
        if revision.request_ordinal < compacted_revision.request_ordinal
    ]
    prior_cursors = [revision.cursor for revision in prior if revision.cursor is not None]
    assert compaction.source_type is CompactionSourceType.CONTEXT_REVISIONS
    assert compaction.source_cursor_start == min(prior_cursors)
    assert compaction.source_cursor_end == max(prior_cursors)
    assert compaction.summary.active_goal
    assert service.list_items(thread.id) == [item]


@pytest.mark.asyncio
async def test_yellow_large_tool_result_becomes_recoverable_artifact_stub(
    tmp_path: Path,
) -> None:
    file_content = "x" * 19_000 + "RECOVERABLE_TOOL_RESULT_TAIL"
    (tmp_path / "large.txt").write_text(file_content)
    provider = CapturingProvider(
        (
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="large_read",
                        name="read_file",
                        arguments_json='{"path":"large.txt"}',
                    ),
                ),
                finish_reason="tool_calls",
            ),
            ModelResponse(content="done", finish_reason="stop"),
            ModelResponse(content="recovered", finish_reason="stop"),
        )
    )
    service, session = _service(
        tmp_path,
        provider,
        context_window=8_000,
        max_turns=2,
        tools=("read_file",),
    )

    events = await _run(service, session, tmp_path)
    revisions = service.list_context_revisions(session.id)
    folded = next(revision for revision in revisions if revision.tool_result_stubs)
    stub = folded.tool_result_stubs[0]
    artifact = service.get_artifact(stub.artifact_id)
    stored = service._read_artifact_for_context(stub.artifact_id).decode("utf-8")

    assert events[-1].event_type == "agent.completed"
    assert artifact.sensitivity is ArtifactSensitivity.NORMAL
    assert artifact.content_hash == stub.content_hash
    assert artifact.size_bytes == stub.stored_size
    assert "RECOVERABLE_TOOL_RESULT_TAIL" in stored
    folded_provider_input = json.dumps(
        [message.model_dump(mode="json") for message in provider.received_messages[1]],
        ensure_ascii=False,
    )
    assert stub.artifact_id in folded_provider_input
    assert "RECOVERABLE_TOOL_RESULT_TAIL" not in folded_provider_input
    assert any(
        source.source_type.value == "artifact" and source.source_id == stub.artifact_id
        for block in folded.blocks
        for source in block.source_refs
    )

    recovery_events = await _run(
        service,
        session,
        tmp_path,
        references=(
            ReferenceRequest(
                ref_type=ContextReferenceType.ARTIFACT,
                target_id=stub.artifact_id,
                include_mode=ReferenceIncludeMode.INLINE,
            ),
        ),
    )
    recovered_input = json.dumps(
        [message.model_dump(mode="json") for message in provider.received_messages[2]],
        ensure_ascii=False,
    )
    assert recovery_events[-1].event_type == "agent.completed"
    assert "RECOVERABLE_TOOL_RESULT_TAIL" in recovered_input


@pytest.mark.asyncio
async def test_tool_result_artifact_dedupes_across_agents_and_sessions(tmp_path: Path) -> None:
    (tmp_path / "same.txt").write_text("d" * 19_000)
    responses: list[ModelResponse] = []
    for call_id in ("first_read", "second_read"):
        responses.extend(
            (
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            id=call_id,
                            name="read_file",
                            arguments_json='{"path":"same.txt"}',
                        ),
                    ),
                    finish_reason="tool_calls",
                ),
                ModelResponse(content="done", finish_reason="stop"),
            )
        )
    provider = CapturingProvider(responses)
    service, first_session = _service(
        tmp_path,
        provider,
        context_window=8_000,
        max_turns=2,
        tools=("read_file",),
    )
    second_session = service.create_session(first_session.role_snapshot.role_id)

    await _run(service, first_session, tmp_path)
    await _run(service, second_session, tmp_path)
    first_stub = next(
        revision.tool_result_stubs[0]
        for revision in service.list_context_revisions(first_session.id)
        if revision.tool_result_stubs
    )
    second_stub = next(
        revision.tool_result_stubs[0]
        for revision in service.list_context_revisions(second_session.id)
        if revision.tool_result_stubs
    )

    assert first_stub.artifact_id == second_stub.artifact_id
    assert len(service.list_artifacts()) == 1
    assert service._read_artifact_for_context(first_stub.artifact_id)


@pytest.mark.asyncio
async def test_tool_result_writer_reuses_normal_hash_but_rejects_sensitive_hash(
    tmp_path: Path,
) -> None:
    file_content = "u" * 19_000
    (tmp_path / "existing.txt").write_text(file_content)
    expected_result = json.dumps(
        {"path": "existing.txt", "content": file_content, "truncated": False},
        ensure_ascii=False,
    ).encode("utf-8")

    normal_provider = CapturingProvider(
        (
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="normal_existing",
                        name="read_file",
                        arguments_json='{"path":"existing.txt"}',
                    ),
                ),
                finish_reason="tool_calls",
            ),
            ModelResponse(content="done", finish_reason="stop"),
        )
    )
    normal_service, normal_session = _service(
        tmp_path,
        normal_provider,
        context_window=8_000,
        max_turns=2,
        tools=("read_file",),
    )
    existing_normal, _created = normal_service.create_artifact(
        content=expected_result,
        media_type="application/x-user-tool-result",
        retention_policy_ref="user-retention",
    )
    await _run(normal_service, normal_session, tmp_path)
    normal_stub = next(
        revision.tool_result_stubs[0]
        for revision in normal_service.list_context_revisions(normal_session.id)
        if revision.tool_result_stubs
    )
    assert normal_stub.artifact_id == existing_normal.id

    sensitive_root = tmp_path / "sensitive-case"
    sensitive_root.mkdir()
    (sensitive_root / "existing.txt").write_text(file_content)
    sensitive_provider = CapturingProvider(
        (
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="sensitive_existing",
                        name="read_file",
                        arguments_json='{"path":"existing.txt"}',
                    ),
                ),
                finish_reason="tool_calls",
            ),
            ModelResponse(content="must-not-run", finish_reason="stop"),
        )
    )
    sensitive_service, sensitive_session = _service(
        sensitive_root,
        sensitive_provider,
        context_window=8_000,
        max_turns=2,
        tools=("read_file",),
    )
    sensitive_service.create_artifact(
        content=expected_result,
        media_type="text/plain",
        sensitivity=ArtifactSensitivity.SENSITIVE,
    )

    sensitive_events = await _run(sensitive_service, sensitive_session, sensitive_root)

    assert sensitive_events[-1].event_type == "agent.failed"
    assert sensitive_events[-1].payload == {"error_type": "PermissionError"}
    assert len(sensitive_provider.received_messages) == 1
    assert len(sensitive_service.list_context_revisions(sensitive_session.id)) == 1


@pytest.mark.asyncio
async def test_tool_result_artifact_failure_persists_no_partial_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "large.txt").write_text("f" * 19_000)
    provider = CapturingProvider(
        (
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="failing_artifact",
                        name="read_file",
                        arguments_json='{"path":"large.txt"}',
                    ),
                ),
                finish_reason="tool_calls",
            ),
            ModelResponse(content="must-not-run", finish_reason="stop"),
        )
    )
    service, session = _service(
        tmp_path,
        provider,
        context_window=8_000,
        max_turns=2,
        tools=("read_file",),
    )

    def fail_writer(*, content: bytes) -> Any:
        del content
        raise RuntimeError("injected Artifact writer failure")

    monkeypatch.setattr(service, "_write_tool_result_artifact", fail_writer)
    events = await _run(service, session, tmp_path)

    assert events[-1].event_type == "agent.failed"
    assert events[-1].payload == {"error_type": "RuntimeError"}
    assert len(provider.received_messages) == 1
    assert len(service.list_context_revisions(session.id)) == 1
    assert service.list_artifacts() == []


def test_concurrent_compose_of_same_request_is_idempotent(tmp_path: Path) -> None:
    provider = CapturingProvider()
    service, session = _service(tmp_path, provider)
    agent = service.store.create_agent(session.id)
    composer = PersistentContextComposer(
        store=service.store,
        session=session,
        agent_id=agent.id,
        workspace=tmp_path,
        memory_resolver=lambda _memory_id: (_ for _ in ()).throw(AssertionError()),
        artifact_reader=lambda _artifact_id: (_ for _ in ()).throw(AssertionError()),
        artifact_writer=lambda content: service._write_tool_result_artifact(content=content),
    )
    messages = (
        Message(role="system", content=session.role_snapshot.system_prompt),
        Message(role="user", content="compose exactly once"),
    )

    def compose() -> str:
        return composer.compose(
            snapshot=session.role_snapshot,
            messages=messages,
            tools=(),
            request_ordinal=1,
        ).revision.id

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _value: compose(), range(2)))

    assert outcomes[0] == outcomes[1]
    revisions = service.list_context_revisions(session.id)
    assert len(revisions) == 1
    assert revisions[0].id == outcomes[0]


def test_api_run_remains_optional_and_context_query_hides_prompt_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = CapturingProvider()
    monkeypatch.setattr(api_module, "OpenAICompatibleProvider", lambda: provider)
    database = tmp_path / "api.sqlite3"
    with TestClient(create_app(database, artifact_root=tmp_path / "api-artifacts")) as client:
        profile = client.post(
            "/v1/models",
            json={
                "name": "api-model",
                "model_id": "phase1b-model",
                "base_url": "https://example.invalid/v1",
                "secret_ref": "OPERANT_PHASE1B_TEST_KEY",
                "context_window": 16_384,
            },
        ).json()
        role = client.post(
            "/v1/roles",
            json={
                "name": "api-role",
                "system_prompt": "private role instructions",
                "model_profile_id": profile["id"],
                "budget": {"max_output_tokens": 1_024},
            },
        ).json()
        session = client.post("/v1/sessions", json={"role_id": role["id"]}).json()
        response = client.post(
            f"/v1/sessions/{session['id']}/runs",
            json={"message": "legacy request body", "workspace": str(tmp_path)},
        )
        assert response.status_code == 200
        assert "event: agent.completed" in response.text
        event_types = [
            line.removeprefix("event: ")
            for line in response.text.splitlines()
            if line.startswith("event: ")
        ]
        assert event_types == [
            "agent.started",
            "model.completed",
            "agent.completed",
        ]

        listed = client.get(f"/v1/sessions/{session['id']}/context-revisions")
        assert listed.status_code == 200
        evidence = listed.json()[0]
        assert evidence["thread_id"] is None
        assert evidence["agent_instance_id"] == evidence["agent_id"]
        assert evidence["prompt_layout_version"] == "phase1b.v1"
        assert evidence["message_count"] == 2
        assert len(evidence["message_ids"]) == 2
        assert len(set(evidence["message_ids"])) == 2
        assert evidence["source_item_ids"] == []
        assert evidence["artifact_refs"] == []
        assert evidence["memory_refs"] == []
        assert evidence["compaction_refs"] == []
        assert evidence["source_cursor_start"] is None
        assert evidence["source_cursor_end"] is None
        assert evidence["source_cursor_namespace"] is None
        assert evidence["token_estimate"] == evidence["watermark"]["input_token_estimate"]
        assert "messages" not in evidence
        assert "tools" not in evidence
        assert "content" not in evidence["blocks"][0]
        assert "user_text" not in json.dumps(evidence["reference_bindings"])
        assert evidence["blocks"][0]["content_hash"]
        evidence_json = json.dumps(evidence, ensure_ascii=False)
        assert "private role instructions" not in evidence_json
        assert "legacy request body" not in evidence_json

        fetched = client.get(f"/v1/sessions/{session['id']}/context-revisions/{evidence['id']}")
        assert fetched.status_code == 200
        assert fetched.json() == evidence

        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT COUNT(*) FROM context_revisions").fetchone() == (1,)


def test_context_revision_api_omits_tool_result_stub_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ordinary_secret = "phoenix-private-notes-20260829"
    (tmp_path / "large.txt").write_text(ordinary_secret + "x" * 19_000)
    provider = CapturingProvider(
        (
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="api_large_read",
                        name="read_file",
                        arguments_json='{"path":"large.txt"}',
                    ),
                ),
                finish_reason="tool_calls",
            ),
            ModelResponse(content="done", finish_reason="stop"),
        )
    )
    monkeypatch.setattr(api_module, "OpenAICompatibleProvider", lambda: provider)
    database = tmp_path / "api-tool-result.sqlite3"
    with TestClient(create_app(database, artifact_root=tmp_path / "api-artifacts")) as client:
        profile = client.post(
            "/v1/models",
            json={
                "name": "api-model",
                "model_id": "phase1b-model",
                "base_url": "https://example.invalid/v1",
                "secret_ref": "OPERANT_PHASE1B_TEST_KEY",
                "context_window": 8_000,
            },
        ).json()
        role = client.post(
            "/v1/roles",
            json={
                "name": "api-role",
                "system_prompt": "Use only authorized Context.",
                "model_profile_id": profile["id"],
                "tool_policy": {"allowed_tools": ["read_file"]},
                "budget": {"max_turns": 2, "max_output_tokens": 1_024},
            },
        ).json()
        session = client.post("/v1/sessions", json={"role_id": role["id"]}).json()
        run = client.post(
            f"/v1/sessions/{session['id']}/runs",
            json={"message": "read the large file", "workspace": str(tmp_path)},
        )
        assert run.status_code == 200
        assert "event: agent.completed" in run.text

        with sqlite3.connect(database) as connection:
            stored_stubs = [
                json.loads(row[0])
                for row in connection.execute(
                    "SELECT tool_result_stubs_json FROM context_revisions ORDER BY sequence"
                ).fetchall()
                if json.loads(row[0])
            ]
        assert len(stored_stubs) == 1
        internal_stub = stored_stubs[0][0]
        assert ordinary_secret in internal_stub["summary"]

        listed = client.get(f"/v1/sessions/{session['id']}/context-revisions")
        assert listed.status_code == 200
        evidence = next(item for item in listed.json() if item["tool_result_stubs"])
        public_stub = evidence["tool_result_stubs"][0]
        assert set(public_stub) == {
            "artifact_id",
            "tool_call_id",
            "content_hash",
            "original_size",
            "stored_size",
            "fetch_capability",
        }
        assert public_stub == {
            key: internal_stub[key]
            for key in (
                "artifact_id",
                "tool_call_id",
                "content_hash",
                "original_size",
                "stored_size",
                "fetch_capability",
            )
        }
        assert ordinary_secret not in listed.text

        fetched = client.get(f"/v1/sessions/{session['id']}/context-revisions/{evidence['id']}")
        assert fetched.status_code == 200
        assert fetched.json() == evidence
        assert ordinary_secret not in fetched.text
