from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from operant.api import create_app
from operant.application.context import PersistentContextComposer
from operant.application.service import ApplicationService
from operant.application.slash_commands import SlashCommandRegistry
from operant.domain.actions import CommandExecution, CommandExecutionStatus
from operant.domain.commands import ContextBaselineOperation, ReviewRunStatus
from operant.domain.messages import (
    Message,
    MessageRole,
    ModelResponse,
    ModelUsage,
    ProviderEvent,
    ToolCall,
    ToolDefinition,
)
from operant.domain.models import AgentStatus, Budget, ModelProfile, RolePreset, ToolPolicy
from operant.domain.threads import (
    ArtifactSensitivity,
    ConversationThread,
    Item,
    Turn,
    UserMessagePayload,
)
from operant.persistence.sqlite import SQLiteStore


class SidecarProvider:
    def __init__(
        self,
        response: ModelResponse | None = None,
        *,
        blocker: asyncio.Event | None = None,
    ) -> None:
        self.response = response or ModelResponse(
            content="done",
            usage=ModelUsage(prompt_tokens=2, completion_tokens=3, total_tokens=5),
        )
        self.blocker = blocker
        self.calls = 0
        self.received_tools: list[tuple[ToolDefinition, ...]] = []

    async def list_models(self, *, base_url: str, secret_ref: str) -> list[str]:
        del base_url, secret_ref
        return []

    async def stream(
        self,
        *,
        snapshot: Any,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        del snapshot, messages
        self.calls += 1
        self.received_tools.append(tuple(tools))
        if self.blocker is not None:
            await self.blocker.wait()
        yield ProviderEvent(event_type="model.completed", response=self.response)


def _sidecar_scope(
    tmp_path: Path,
    provider: SidecarProvider,
    *,
    budget: Budget | None = None,
    with_prices: bool = False,
) -> tuple[ApplicationService, Any, ConversationThread, Item]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(tmp_path / "sidecar.sqlite3")
    service = ApplicationService(store, provider, artifact_root=tmp_path / "artifacts")
    service.initialize()
    profile = service.add_model_profile(
        ModelProfile(
            name="sidecar",
            model_id="sidecar",
            base_url="https://example.invalid/v1",
            secret_ref="OPERANT_PHASE1D_SIDECAR_KEY",
            context_window=16_384,
            input_usd_per_million_tokens=1.0 if with_prices else None,
            output_usd_per_million_tokens=1.0 if with_prices else None,
        )
    )
    role = service.create_role(
        RolePreset(
            name="Sidecar",
            system_prompt="Answer safely.",
            model_profile_id=profile.id,
            budget=budget or Budget(max_output_tokens=128),
            tool_policy=ToolPolicy(
                allowed_tools=("apply_patch", "run_command"),
                workspace_write=True,
            ),
        )
    )
    session = service.create_session(role.id)
    workspace_ref = str(tmp_path.resolve())
    thread = service.create_thread(ConversationThread(workspace_ref=workspace_ref))
    turn = service.create_turn(Turn(thread_id=thread.id))
    item = service.append_item(
        Item(
            thread_id=thread.id,
            turn_id=turn.id,
            payload=UserMessagePayload(text="canonical main-thread fact"),
        )
    )
    return service, session, thread, item


def _review_scope(
    tmp_path: Path,
    provider: SidecarProvider,
) -> tuple[ApplicationService, RolePreset, CommandExecution]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(tmp_path / "review-lifecycle.sqlite3")
    service = ApplicationService(store, provider, artifact_root=tmp_path / "review-artifacts")
    service.initialize()
    profile = service.add_model_profile(
        ModelProfile(
            name="review-lifecycle",
            model_id="review-lifecycle",
            base_url="https://example.invalid/v1",
            secret_ref="OPERANT_PHASE1D_REVIEW_LIFECYCLE_KEY",
            context_window=16_384,
        )
    )
    role = service.create_role(
        RolePreset(
            name="Lifecycle Reviewer",
            system_prompt="Review without modifying files.",
            model_profile_id=profile.id,
            tool_policy=ToolPolicy(allowed_tools=("read_file", "search_files", "git_diff")),
            budget=Budget(max_output_tokens=128),
        )
    )
    command, claimed = store.reserve_command_execution(
        CommandExecution(
            command_type="phase1d.review",
            idempotency_key="review-lifecycle",
            action_hash="b" * 64,
        )
    )
    assert claimed
    return service, role, command


def test_slash_registry_lists_and_resolves_aliases_without_execution() -> None:
    registry = SlashCommandRegistry()

    definitions = registry.list_commands()
    assert {definition.canonical_name for definition in definitions} == {
        "/init",
        "/review",
        "/clear-context",
        "/compact-context",
    }
    resolved = registry.resolve("/清空上下文")
    assert resolved.canonical_name == "/clear-context"
    assert resolved.arguments == ""
    assert registry.resolve("/review src/operant").arguments == "src/operant"


def test_registry_query_and_workspace_registration_are_idempotent_and_path_safe(
    tmp_path: Path,
) -> None:
    database = tmp_path / "phase1d.sqlite3"
    workspace = tmp_path / "private-workspace"
    workspace.mkdir()

    with TestClient(create_app(database)) as client:
        listed = client.get("/v1/slash-commands")
        assert listed.status_code == 200
        assert listed.json()["registry_version"] == "phase1d.v1"

        resolved = client.get(
            "/v1/slash-commands/resolve",
            params={"text": "/初始化"},
        )
        assert resolved.status_code == 200
        assert resolved.json()["canonical_name"] == "/init"

        key = "phase1d-workspace-register"
        first = client.post(
            "/v1/commands/workspace/init",
            headers={"Idempotency-Key": key},
            json={"workspace": str(workspace)},
        )
        assert first.status_code == 200
        assert first.json()["created"] is True
        assert str(workspace) not in first.text

        replay = client.post(
            "/v1/commands/workspace/init",
            headers={"Idempotency-Key": key},
            json={"workspace": str(workspace)},
        )
        assert replay.status_code == 200
        assert replay.headers["Idempotency-Replayed"] == "true"
        assert replay.json() == first.json()

        conflict = client.post(
            "/v1/commands/workspace/init",
            headers={"Idempotency-Key": key},
            json={"workspace": str(tmp_path)},
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "idempotency_key_conflict"

    with sqlite3.connect(database) as connection:
        receipt_json = connection.execute(
            "SELECT response_json FROM command_executions WHERE idempotency_key = ?",
            (key,),
        ).fetchone()[0]
    assert str(workspace) not in receipt_json
    # The path is intentionally present only in the private workspace registry.
    assert json.loads(first.text)["workspace_hash"]


def test_bad_review_start_is_persisted_failed_and_replayed_without_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "bad-review.sqlite3"
    app = create_app(database, artifact_root=tmp_path / "artifacts")
    service: ApplicationService = app.state.operant_service
    original = service.run_review
    calls = 0

    async def counted_run_review(**kwargs: Any) -> AsyncIterator[Any]:
        nonlocal calls
        calls += 1
        async for event in original(**kwargs):
            yield event

    monkeypatch.setattr(service, "run_review", counted_run_review)
    key = "bad-review-start"
    missing_role = "missing-reviewer-private-value"
    request = {
        "workspace": str(tmp_path),
        "reviewer_role_id": missing_role,
        "scope": "working tree",
    }

    with TestClient(app) as client:
        first = client.post(
            "/v1/commands/review",
            headers={"Idempotency-Key": key},
            json=request,
        )
        assert first.status_code == 200
        assert "event: review.stream_error" in first.text
        assert "review_start_not_found" in first.text
        assert '"recovery": "none"' in first.text
        assert missing_role not in first.text

        replay = client.post(
            "/v1/commands/review",
            headers={"Idempotency-Key": key},
            json=request,
        )
        assert replay.status_code == 404
        assert replay.headers["Idempotency-Replayed"] == "true"
        assert replay.json()["error"] == {
            "code": "review_start_not_found",
            "message": "Review stream dependencies were not found",
            "retryable": False,
            "recovery": "none",
        }
        assert replay.json()["recovery"] == "none"

    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        receipt = connection.execute(
            "SELECT * FROM command_executions WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        assert receipt is not None
        assert receipt["status"] == CommandExecutionStatus.FAILED.value
        assert receipt["http_status"] == 404
        assert receipt["error_code"] == "review_start_not_found"
        assert missing_role not in receipt["response_json"]
        assert connection.execute("SELECT COUNT(*) FROM review_runs").fetchone()[0] == 0
        assert (
            connection.execute("SELECT COUNT(*) FROM phase1d_command_audit_events").fetchone()[0]
            == 0
        )
    assert calls == 1


def test_bad_btw_start_is_persisted_failed_and_replayed_without_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "bad-btw.sqlite3"
    app = create_app(database, artifact_root=tmp_path / "artifacts")
    service: ApplicationService = app.state.operant_service
    provider = SidecarProvider()
    service.provider = provider
    profile = service.add_model_profile(
        ModelProfile(
            name="bad-btw",
            model_id="bad-btw",
            base_url="https://example.invalid/v1",
            secret_ref="OPERANT_PHASE1D_BAD_BTW_KEY",
        )
    )
    role = service.create_role(
        RolePreset(
            name="Bad BTW",
            system_prompt="Answer safely.",
            model_profile_id=profile.id,
        )
    )
    session = service.create_session(role.id)
    thread = service.create_thread(ConversationThread(workspace_ref=str(tmp_path.resolve())))
    denied_workspace = tmp_path / "private-denied-workspace"
    denied_workspace.mkdir()
    original = service.run_btw_sidecar
    calls = 0

    async def counted_run_btw_sidecar(**kwargs: Any) -> AsyncIterator[Any]:
        nonlocal calls
        calls += 1
        async for event in original(**kwargs):
            yield event

    monkeypatch.setattr(service, "run_btw_sidecar", counted_run_btw_sidecar)
    key = "bad-btw-start"
    request = {
        "session_id": session.id,
        "thread_id": thread.id,
        "workspace": str(denied_workspace),
        "prompt": "do not run",
    }

    with TestClient(app) as client:
        first = client.post(
            "/v1/sidecars/btw",
            headers={"Idempotency-Key": key},
            json=request,
        )
        assert first.status_code == 200
        assert "event: btw.stream_error" in first.text
        assert "btw_start_policy_denied" in first.text
        assert '"recovery": "none"' in first.text
        assert str(denied_workspace) not in first.text

        replay = client.post(
            "/v1/sidecars/btw",
            headers={"Idempotency-Key": key},
            json=request,
        )
        assert replay.status_code == 403
        assert replay.headers["Idempotency-Replayed"] == "true"
        assert replay.json()["error"] == {
            "code": "btw_start_policy_denied",
            "message": "BTW Sidecar stream request is not allowed",
            "retryable": False,
            "recovery": "none",
        }
        assert replay.json()["recovery"] == "none"

    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        receipt = connection.execute(
            "SELECT * FROM command_executions WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        assert receipt is not None
        assert receipt["status"] == CommandExecutionStatus.FAILED.value
        assert receipt["http_status"] == 403
        assert receipt["error_code"] == "btw_start_policy_denied"
        assert str(denied_workspace) not in receipt["response_json"]
        assert connection.execute("SELECT COUNT(*) FROM btw_sidecar_runs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM btw_sidecar_events").fetchone()[0] == 0
    assert calls == 1
    assert provider.calls == 0


def _compose_thread_context(
    service: ApplicationService,
    session: Any,
    thread: ConversationThread,
    workspace: Path,
):
    agent = service.factory.create_agent(session.id)
    return PersistentContextComposer(
        store=service.store,
        session=session,
        agent_id=agent.id,
        workspace=workspace,
        thread_id=thread.id,
        memory_resolver=lambda _memory_id: (_ for _ in ()).throw(AssertionError()),
        artifact_reader=lambda _artifact_id: b"",
        artifact_writer=lambda _content: (_ for _ in ()).throw(AssertionError()),
    ).compose(
        snapshot=session.role_snapshot,
        messages=(
            Message(role=MessageRole.SYSTEM, content=session.role_snapshot.system_prompt),
            Message(role=MessageRole.USER, content="continue"),
        ),
        tools=(),
        request_ordinal=1,
    )


def test_clear_and_compact_commands_change_future_context_without_rewriting_history(
    tmp_path: Path,
) -> None:
    provider = SidecarProvider()
    service, session, thread, old_item = _sidecar_scope(tmp_path, provider)
    before = _compose_thread_context(service, session, thread, tmp_path)
    assert "canonical main-thread fact" in json.dumps(
        [message.model_dump(mode="json") for message in before.messages]
    )
    revision_count = len(service.store.list_context_revisions(session.id))

    clear = service.append_context_baseline(
        session_id=session.id,
        thread_id=thread.id,
        operation=ContextBaselineOperation.CLEAR,
    )
    assert len(service.store.list_context_revisions(session.id)) == revision_count
    after_clear = _compose_thread_context(service, session, thread, tmp_path)
    assert "canonical main-thread fact" not in json.dumps(
        [message.model_dump(mode="json") for message in after_clear.messages]
    )

    new_turn = service.create_turn(Turn(thread_id=thread.id))
    new_item = service.append_item(
        Item(
            thread_id=thread.id,
            turn_id=new_turn.id,
            payload=UserMessagePayload(text="new active fact"),
        )
    )
    compact_agent = service.factory.create_agent(session.id)
    compact = service.append_context_baseline(
        session_id=session.id,
        thread_id=thread.id,
        operation=ContextBaselineOperation.COMPACT,
        agent_id=compact_agent.id,
    )
    assert compact.previous_baseline_id == clear.id
    assert compact.compaction_id is not None
    assert len(service.store.list_context_revisions(session.id)) == revision_count + 1
    after_compact = _compose_thread_context(service, session, thread, tmp_path)
    assert after_compact.revision.compaction_id == compact.compaction_id
    assert service.store.list_items(thread.id) == [old_item, new_item]


@pytest.mark.asyncio
async def test_btw_sidecar_uses_no_tools_or_main_lease_and_keeps_thread_unchanged(
    tmp_path: Path,
) -> None:
    secret = "sk-1234567890abcdefghijklmnop"
    provider = SidecarProvider(
        ModelResponse(
            content=f"Use {tmp_path.resolve()} with {secret}",
            usage=ModelUsage(prompt_tokens=2, completion_tokens=3, total_tokens=5),
        )
    )
    service, session, thread, item = _sidecar_scope(tmp_path, provider)

    events = [
        event
        async for event in service.run_btw_sidecar(
            session_id=session.id,
            thread_id=thread.id,
            workspace=tmp_path,
            prompt="side question",
        )
    ]

    assert [event.event_type for event in events] == [
        "btw.started",
        "btw.model_completed",
    ]
    run = service.get_btw_sidecar_run(events[0].sidecar_run_id)
    assert run.response is not None
    assert str(tmp_path.resolve()) not in run.response
    assert secret not in run.response
    assert "[WORKSPACE]" in run.response
    assert provider.received_tools == [()]
    assert service.store.list_items(thread.id) == [item]
    assert service.store.list_events(session.id) == []
    with sqlite3.connect(service.store.path) as connection:
        active_lease_count = connection.execute(
            "SELECT COUNT(*) FROM session_run_leases WHERE released_at IS NULL"
        ).fetchone()[0]
    assert active_lease_count == 0


@pytest.mark.asyncio
async def test_btw_sidecar_unknown_pricing_fails_before_provider_or_context_revision(
    tmp_path: Path,
) -> None:
    provider = SidecarProvider()
    service, session, thread, _item = _sidecar_scope(
        tmp_path,
        provider,
        budget=Budget(max_cost_usd=0.01),
    )

    events = [
        event
        async for event in service.run_btw_sidecar(
            session_id=session.id,
            thread_id=thread.id,
            workspace=tmp_path,
            prompt="must fail closed",
        )
    ]

    assert provider.calls == 0
    assert events[-1].event_type == "btw.failed"
    assert events[-1].payload["error_code"] == "budget_cost_pricing_unknown"
    assert events[-1].payload["budget"]["usage_state"] == "unknown"
    assert service.store.list_context_revisions(session.id) == []


@pytest.mark.asyncio
async def test_btw_sidecar_unknown_usage_and_tool_call_fail_closed(tmp_path: Path) -> None:
    unknown = SidecarProvider(ModelResponse(content="unknown usage", usage=None))
    service, session, thread, item = _sidecar_scope(
        tmp_path / "unknown",
        unknown,
        budget=Budget(max_output_tokens=32),
    )
    unknown_events = [
        event
        async for event in service.run_btw_sidecar(
            session_id=session.id,
            thread_id=thread.id,
            workspace=tmp_path / "unknown",
            prompt="usage must be known",
        )
    ]
    assert unknown_events[-1].payload["error_code"] == "budget_output_tokens_usage_unknown"
    assert service.store.list_items(thread.id) == [item]

    tool_provider = SidecarProvider(
        ModelResponse(
            tool_calls=(
                ToolCall(
                    id="forbidden",
                    name="apply_patch",
                    arguments_json='{"path":"x","old_text":"a","new_text":"b"}',
                ),
            ),
            usage=ModelUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )
    )
    tool_service, tool_session, tool_thread, tool_item = _sidecar_scope(
        tmp_path / "tool-call",
        tool_provider,
    )
    tool_events = [
        event
        async for event in tool_service.run_btw_sidecar(
            session_id=tool_session.id,
            thread_id=tool_thread.id,
            workspace=tmp_path / "tool-call",
            prompt="never use tools",
        )
    ]
    assert tool_events[-1].payload["error_code"] == "provider_tool_call_rejected"
    assert tool_provider.received_tools == [()]
    assert tool_service.store.list_items(tool_thread.id) == [tool_item]


@pytest.mark.asyncio
async def test_btw_sidecar_explicit_cancel_isolated_from_main_session(tmp_path: Path) -> None:
    blocker = asyncio.Event()
    provider = SidecarProvider(blocker=blocker)
    service, session, thread, _item = _sidecar_scope(tmp_path, provider)
    stream = service.run_btw_sidecar(
        session_id=session.id,
        thread_id=thread.id,
        workspace=tmp_path,
        prompt="wait",
    )
    started = await anext(stream)
    pending = asyncio.create_task(anext(stream))
    while provider.calls == 0:
        await asyncio.sleep(0)
    assert service.cancel_btw_sidecar(started.sidecar_run_id) is True
    failed = await asyncio.wait_for(pending, timeout=1)
    assert failed.event_type == "btw.failed"
    assert failed.payload["error_code"] == "cancelled"
    assert service.cancel_btw_sidecar(started.sidecar_run_id) is False
    assert service.cancel_session(session.id) is False
    await stream.aclose()


@pytest.mark.asyncio
async def test_btw_sidecar_close_after_started_fails_run_and_agent(tmp_path: Path) -> None:
    provider = SidecarProvider()
    service, session, thread, _item = _sidecar_scope(tmp_path, provider)
    stream = service.run_btw_sidecar(
        session_id=session.id,
        thread_id=thread.id,
        workspace=tmp_path,
        prompt="close immediately",
    )

    started = await anext(stream)
    run = service.get_btw_sidecar_run(started.sidecar_run_id)
    assert run.id in service._sidecar_cancellations
    await stream.aclose()

    closed_run = service.get_btw_sidecar_run(run.id)
    assert closed_run.status.value == "failed"
    assert closed_run.error_code == "stream_closed"
    assert service.store.get_agent(run.agent_id).status is AgentStatus.CANCELLED
    assert run.id not in service._sidecar_cancellations
    assert [event.event_type for event in service.list_btw_sidecar_events(run.id)] == [
        "btw.started",
        "btw.failed",
    ]
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_review_close_and_cancel_after_started_persist_one_failure(
    tmp_path: Path,
) -> None:
    close_provider = SidecarProvider()
    close_service, close_role, close_command = _review_scope(
        tmp_path / "close",
        close_provider,
    )
    close_stream = close_service.run_review(
        command_execution_id=close_command.id,
        reviewer_role_id=close_role.id,
        workspace=tmp_path / "close",
        scope="close immediately",
    )

    started = await anext(close_stream)
    close_run = close_service.store.get_review_run(started.resource_id or "")
    await close_stream.aclose()

    closed_run = close_service.store.get_review_run(close_run.id)
    assert closed_run.status is ReviewRunStatus.FAILED
    assert closed_run.error_code == "stream_closed"
    assert [
        event.event_type for event in close_service.list_phase1d_audit_events(close_command.id)
    ] == ["review.started", "review.failed"]
    assert close_provider.calls == 0

    blocker = asyncio.Event()
    cancel_provider = SidecarProvider(blocker=blocker)
    cancel_service, cancel_role, cancel_command = _review_scope(
        tmp_path / "cancel",
        cancel_provider,
    )
    cancel_stream = cancel_service.run_review(
        command_execution_id=cancel_command.id,
        reviewer_role_id=cancel_role.id,
        workspace=tmp_path / "cancel",
        scope="cancel during provider call",
    )
    cancel_started = await anext(cancel_stream)
    pending = asyncio.create_task(anext(cancel_stream))
    while cancel_provider.calls == 0:
        await asyncio.sleep(0)
    active_run = cancel_service.store.get_review_run(cancel_started.resource_id or "")
    active_lease = cancel_service.admitted_session_run_lease(active_run.session_id)
    assert active_lease is not None and active_lease.agent_id is not None
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    cancelled_run = cancel_service.store.get_review_run(cancel_started.resource_id or "")
    assert cancelled_run.status is ReviewRunStatus.FAILED
    assert cancelled_run.error_code == "stream_cancelled"
    assert cancel_service.store.get_agent(active_lease.agent_id).status is AgentStatus.CANCELLED
    assert active_run.session_id not in cancel_service._cancellations
    assert cancel_service.admitted_session_run_lease(active_run.session_id) is None
    assert [
        event.event_type for event in cancel_service.list_phase1d_audit_events(cancel_command.id)
    ] == ["review.started", "review.failed"]
    await cancel_stream.aclose()
    assert [
        event.event_type for event in cancel_service.list_phase1d_audit_events(cancel_command.id)
    ] == ["review.started", "review.failed"]


def test_review_command_is_read_only_creates_sensitive_artifact_and_replays_sse(
    tmp_path: Path,
) -> None:
    database = tmp_path / "review.sqlite3"
    app = create_app(database, artifact_root=tmp_path / "artifacts")
    service: ApplicationService = app.state.operant_service
    provider = SidecarProvider(
        ModelResponse(
            content="VERDICT: PASS\nNo blocking findings.",
            usage=ModelUsage(prompt_tokens=4, completion_tokens=5, total_tokens=9),
        )
    )
    service.provider = provider
    profile = service.add_model_profile(
        ModelProfile(
            name="review",
            model_id="review",
            base_url="https://example.invalid/v1",
            secret_ref="OPERANT_PHASE1D_REVIEW_KEY",
            context_window=16_384,
        )
    )
    role = service.create_role(
        RolePreset(
            name="Strict Reviewer",
            system_prompt="Review without modifying files.",
            model_profile_id=profile.id,
            tool_policy=ToolPolicy(allowed_tools=("read_file", "search_files", "git_diff")),
            budget=Budget(max_output_tokens=128),
        )
    )

    with TestClient(app) as client:
        first = client.post(
            "/v1/commands/review",
            headers={"Idempotency-Key": "review-command"},
            json={
                "workspace": str(tmp_path),
                "reviewer_role_id": role.id,
                "scope": str(tmp_path / "private-scope"),
            },
        )
        assert first.status_code == 200
        assert "event: review.started" in first.text
        assert "event: review.completed" in first.text
        assert str(tmp_path) not in first.text
        replay = client.post(
            "/v1/commands/review",
            headers={"Idempotency-Key": "review-command"},
            json={
                "workspace": str(tmp_path),
                "reviewer_role_id": role.id,
                "scope": str(tmp_path / "private-scope"),
            },
        )
        assert replay.status_code == 202
        assert replay.headers["Idempotency-Replayed"] == "true"

        with sqlite3.connect(database) as connection:
            command_id = connection.execute(
                "SELECT id FROM command_executions WHERE idempotency_key = ?",
                ("review-command",),
            ).fetchone()[0]
        audit_events = service.list_phase1d_audit_events(command_id)
        assert [event.event_type for event in audit_events] == [
            "review.started",
            "review.completed",
        ]
        replay_stream = client.get(
            f"/v1/command-executions/{command_id}/events/stream",
            headers={"Last-Event-ID": str(audit_events[0].cursor)},
        )
        assert "event: review.started" not in replay_stream.text
        assert "event: review.completed" in replay_stream.text

        review = service.store.list_review_runs()[0]
        public_review = client.get(f"/v1/reviews/{review.id}")
        assert public_review.status_code == 200
        assert "scope" not in public_review.json()
        assert str(tmp_path) not in public_review.text

    assert provider.received_tools
    assert {tool.name for tool in provider.received_tools[0]} == {
        "read_file",
        "search_files",
        "git_diff",
    }
    review = service.store.list_review_runs()[0]
    assert review.artifact_id is not None
    artifact = service.get_artifact(review.artifact_id)
    assert artifact.sensitivity is ArtifactSensitivity.SENSITIVE
    with (
        sqlite3.connect(database) as connection,
        pytest.raises(
            sqlite3.IntegrityError,
            match="immutable",
        ),
    ):
        connection.execute(
            "UPDATE artifacts SET media_type = 'text/plain' WHERE id = ?",
            (artifact.id,),
        )


@pytest.mark.asyncio
async def test_review_rejects_any_writable_role_policy(tmp_path: Path) -> None:
    provider = SidecarProvider()
    store = SQLiteStore(tmp_path / "review-rejected.sqlite3")
    service = ApplicationService(store, provider, artifact_root=tmp_path / "artifacts")
    service.initialize()
    profile = service.add_model_profile(
        ModelProfile(
            name="unsafe-review",
            model_id="unsafe-review",
            base_url="https://example.invalid/v1",
            secret_ref="OPERANT_PHASE1D_UNSAFE_REVIEW_KEY",
        )
    )
    role = service.create_role(
        RolePreset(
            name="Unsafe Reviewer",
            system_prompt="May write.",
            model_profile_id=profile.id,
            tool_policy=ToolPolicy(
                allowed_tools=("read_file", "search_files", "git_diff"),
                workspace_write=True,
            ),
        )
    )
    command, claimed = store.reserve_command_execution(
        CommandExecution(
            command_type="phase1d.review",
            idempotency_key="unsafe-review",
            action_hash="a" * 64,
        )
    )
    assert claimed
    stream = service.run_review(
        command_execution_id=command.id,
        reviewer_role_id=role.id,
        workspace=tmp_path,
        scope="working tree",
    )
    with pytest.raises(PermissionError, match="strictly read-only"):
        await anext(stream)
    assert provider.calls == 0
    assert store.list_review_runs() == []
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
