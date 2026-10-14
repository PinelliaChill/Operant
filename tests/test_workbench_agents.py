from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from operant.api_workbench_agents import (
    CreateChildAgentRequest,
    SendAgentMessageRequest,
    WorkbenchRuntime,
)
from operant.api_workbench_context import thread_session
from operant.application.service import ApplicationService
from operant.domain.messages import Message, ModelResponse, ProviderEvent, ToolCall, ToolDefinition
from operant.domain.models import Budget, ModelProfile, RolePreset, ToolPolicy
from operant.domain.threads import ConversationThread
from operant.persistence.sqlite import ConflictError, SQLiteStore
from operant.tools.workspace import WorkspaceTools


class CompletingProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def list_models(self, *, base_url: str, secret_ref: str) -> list[str]:
        return ["local-test"]

    async def stream(
        self,
        *,
        snapshot: Any,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        self.calls += 1
        yield ProviderEvent(
            event_type="model.completed",
            response=ModelResponse(content="child result", finish_reason="stop"),
        )


class DelegatingProvider(CompletingProvider):
    async def stream(
        self,
        *,
        snapshot: Any,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        if any(message.role.value == "tool" for message in messages):
            response = ModelResponse(content="parent complete", finish_reason="stop")
        elif any("child task" in (message.content or "") for message in messages):
            response = ModelResponse(content="child result", finish_reason="stop")
        else:
            assert "delegate_agent" in {tool.name for tool in tools}
            response = ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="delegate-once",
                        name="delegate_agent",
                        arguments_json=json.dumps({"task": "child task"}),
                    ),
                ),
                finish_reason="tool_calls",
            )
        yield ProviderEvent(event_type="model.completed", response=response)


class RepairingDelegateProvider(CompletingProvider):
    def __init__(self, *, denied_role_id: str | None = None) -> None:
        self.denied_role_id = denied_role_id
        self.saw_tool_error = False

    async def stream(
        self,
        *,
        snapshot: Any,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        del snapshot
        if any("child task" in (message.content or "") for message in messages):
            response = ModelResponse(content="child result", finish_reason="stop")
        else:
            tool_messages = [message for message in messages if message.role.value == "tool"]
            if tool_messages:
                last = tool_messages[-1]
                if last.tool_call_id == "bad-delegate":
                    assert "ToolError" in (last.content or "")
                    self.saw_tool_error = True
                    if self.denied_role_id is not None:
                        response = ModelResponse(
                            content="permission respected", finish_reason="stop"
                        )
                    else:
                        response = ModelResponse(
                            tool_calls=(
                                ToolCall(
                                    id="delegate-with-inheritance",
                                    name="delegate_agent",
                                    arguments_json=json.dumps({"task": "child task"}),
                                ),
                            ),
                            finish_reason="tool_calls",
                        )
                else:
                    response = ModelResponse(content="parent complete", finish_reason="stop")
            else:
                definition = next(tool for tool in tools if tool.name == "delegate_agent")
                profile_ids = definition.parameters["properties"]["model_profile_id"]["enum"]
                assert profile_ids and all(
                    identity.startswith("model_") for identity in profile_ids
                )
                arguments = {"task": "child task"}
                if self.denied_role_id is None:
                    arguments["model_profile_id"] = "gpt-4.1-mini"
                else:
                    arguments["role_id"] = self.denied_role_id
                response = ModelResponse(
                    tool_calls=(
                        ToolCall(
                            id="bad-delegate",
                            name="delegate_agent",
                            arguments_json=json.dumps(arguments),
                        ),
                    ),
                    finish_reason="tool_calls",
                )
        yield ProviderEvent(event_type="model.completed", response=response)


class BarrierProvider(CompletingProvider):
    def __init__(self, *, fail_first: bool = False) -> None:
        super().__init__()
        self.fail_first = fail_first
        self.started: asyncio.Queue[int] = asyncio.Queue()
        self.permits: asyncio.Queue[None] = asyncio.Queue()

    async def stream(
        self,
        *,
        snapshot: Any,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        del snapshot, messages, tools
        self.calls += 1
        await self.started.put(self.calls)
        await self.permits.get()
        if self.fail_first and self.calls == 1:
            raise RuntimeError("injected provider failure")
        yield ProviderEvent(
            event_type="model.completed",
            response=ModelResponse(content=f"run {self.calls} complete", finish_reason="stop"),
        )


def scope(
    tmp_path: Path, provider: CompletingProvider | None = None
) -> tuple[ApplicationService, str, str, str]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = ApplicationService(
        SQLiteStore(tmp_path / "core.sqlite3"),
        provider or CompletingProvider(),
        artifact_root=tmp_path / "artifacts",
    )
    service.initialize()
    profile = service.add_model_profile(
        ModelProfile(
            name="test",
            model_id="local-test",
            base_url="https://example.invalid/v1",
            secret_ref="OPERANT_WORKBENCH_TEST_KEY",
        )
    )
    role = service.create_role(
        RolePreset(
            name="coordinator",
            system_prompt="Use bounded child work.",
            model_profile_id=profile.id,
            budget=Budget(max_turns=4),
            tool_policy=ToolPolicy(
                allowed_tools=(
                    "read_file",
                    "delegate_agent",
                    "send_agent_message",
                    "wait_for_agent",
                )
            ),
        )
    )
    parent = service.create_thread(ConversationThread(workspace_ref=str(workspace)))
    service.create_session(role.id, thread_id=parent.id)
    return service, parent.id, role.id, str(workspace)


def related_sender(service: ApplicationService, parent_id: str, workspace: str) -> str:
    sender = service.create_thread(
        ConversationThread(parent_thread_id=parent_id, workspace_ref=workspace)
    )
    parent_snapshot = thread_session(service, parent_id).role_snapshot
    service.store.create_session_from_snapshot(parent_snapshot, thread_id=sender.id)
    return sender.id


@pytest.mark.asyncio
async def test_child_executes_and_freezes_parent_boundary(tmp_path: Path) -> None:
    service, parent_id, role_id, workspace = scope(tmp_path)
    runtime = service.workbench
    child = await runtime.create_child(parent_id, CreateChildAgentRequest(task="bounded work"))
    await runtime.tasks[child.thread_id]
    finished = runtime.get_child(child.thread_id)
    assert finished.status == "completed"
    assert finished.result == "child result"
    assert finished.workspace_ref == workspace
    assert finished.role_id == role_id
    assert (
        finished.tool_policy.allowed_tools
        == service.get_session(child.session_id).role_snapshot.tool_policy.allowed_tools
    )
    assert service.get_thread(child.thread_id).parent_thread_id == parent_id
    assert service.get_thread(child.thread_id).status.value == "active"
    followup = runtime.send(
        parent_id,
        SendAgentMessageRequest(
            recipient_thread_id=child.thread_id, body="continue", idempotency_key="followup"
        ),
    )
    assert followup.delivery_status == "delivered"
    assert await runtime.wake(child.thread_id) == "scheduled"
    await runtime.tasks[child.thread_id]
    assert runtime.get_child(child.thread_id).status == "completed"
    assert runtime.list_messages(child.thread_id)[0].delivery_status == "consumed"


@pytest.mark.asyncio
async def test_directed_mailbox_dedup_and_consumption_boundary(tmp_path: Path) -> None:
    service, parent_id, _role_id, workspace = scope(tmp_path)
    runtime = service.workbench
    child = service.create_thread(
        ConversationThread(parent_thread_id=parent_id, workspace_ref=workspace)
    )
    parent_session = service.get_session(
        next(
            ref.source_id
            for ref in service.get_thread(parent_id).legacy_refs
            if ref.source_type.value == "session"
        )
    )
    service.store.create_session_from_snapshot(parent_session.role_snapshot, thread_id=child.id)
    request = SendAgentMessageRequest(
        recipient_thread_id=child.id, body="private update", idempotency_key="same"
    )
    first = runtime.send(parent_id, request)
    repeated = runtime.send(parent_id, request)
    assert repeated.message_id == first.message_id
    assert first.delivery_status == "delivered"
    assert len(runtime.list_messages(child.id)) == 1
    assert "private update" in runtime.peek_inbox(child.id)
    runtime.acknowledge_inbox(child.id)
    assert "private update" in runtime.peek_inbox(child.id)
    runtime.clear_run_inbox(child.id)
    assert runtime.peek_inbox(child.id) == ""
    assert runtime.list_messages(child.id)[0].delivery_status == "consumed"
    with pytest.raises(ConflictError, match="reused"):
        runtime.send(parent_id, request.model_copy(update={"body": "different"}))


def test_automatic_wake_limit_persists_across_restart(tmp_path: Path) -> None:
    service, parent_id, _role_id, _workspace = scope(tmp_path)
    for _ in range(4):
        assert service.store.reserve_workbench_wake(parent_id, limit=4)
    assert not service.store.reserve_workbench_wake(parent_id, limit=4)
    reopened = SQLiteStore(service.store.path)
    reopened.initialize()
    assert not reopened.reserve_workbench_wake(parent_id, limit=4)


@pytest.mark.asyncio
async def test_child_role_cannot_gain_permissions_or_use_inactive_role(tmp_path: Path) -> None:
    service, parent_id, parent_role_id, _workspace = scope(tmp_path)
    parent_session = service.get_session(
        next(
            ref.source_id
            for ref in service.get_thread(parent_id).legacy_refs
            if ref.source_type.value == "session"
        )
    )
    stronger = service.create_role(
        RolePreset(
            name="stronger",
            system_prompt="stronger",
            model_profile_id=parent_session.role_snapshot.model_profile_id,
            tool_policy=ToolPolicy(allowed_tools=("read_file", "run_command")),
        )
    )
    with pytest.raises(PermissionError, match="gain tools"):
        await service.workbench.create_child(
            parent_id, CreateChildAgentRequest(task="no", role_id=stronger.id)
        )
    service.deactivate_role(stronger.id)
    with pytest.raises(ValueError, match="inactive"):
        await service.workbench.create_child(
            parent_id, CreateChildAgentRequest(task="no", role_id=stronger.id)
        )
    assert service.get_role(parent_role_id).status.value == "active"


@pytest.mark.asyncio
async def test_delegate_tool_uses_formal_agent_loop_and_action_gateway(tmp_path: Path) -> None:
    service, parent_id, _role_id, workspace = scope(tmp_path, DelegatingProvider())
    parent = service.get_thread(parent_id)
    session_id = next(
        ref.source_id for ref in parent.legacy_refs if ref.source_type.value == "session"
    )
    events = [
        event
        async for event in service.run_session(
            session_id, user_message="start delegate", workspace=workspace, thread_id=parent_id
        )
    ]
    assert any(event.event_type == "agent.completed" for event in events)
    children = service.workbench.list_children(parent_id)
    assert len(children) == 1
    await service.workbench.tasks[children[0].thread_id]
    assert service.workbench.get_child(children[0].thread_id).result == "child result"
    with service.store._connect() as connection:
        receipt = connection.execute(
            "SELECT status FROM tool_action_receipts WHERE command_name='delegate_agent'"
        ).fetchone()
    assert receipt is not None and receipt["status"] == "completed"


@pytest.mark.asyncio
async def test_invalid_profile_tool_result_allows_inherited_retry(tmp_path: Path) -> None:
    provider = RepairingDelegateProvider()
    service, parent_id, _role_id, workspace = scope(tmp_path, provider)
    parent = service.get_thread(parent_id)
    session_id = next(
        ref.source_id for ref in parent.legacy_refs if ref.source_type.value == "session"
    )
    events = [
        event
        async for event in service.run_session(
            session_id, user_message="delegate", workspace=workspace, thread_id=parent_id
        )
    ]
    assert provider.saw_tool_error
    assert [event.event_type for event in events].count("tool.failed") == 1
    assert [event.event_type for event in events].count("tool.completed") == 1
    assert any(event.event_type == "agent.completed" for event in events)
    assert not any(event.event_type == "agent.failed" for event in events)
    children = service.workbench.list_children(parent_id)
    assert len(children) == 1
    await service.workbench.tasks[children[0].thread_id]
    assert service.workbench.get_child(children[0].thread_id).result == "child result"


@pytest.mark.asyncio
async def test_duplicate_consumed_private_message_does_not_wake_again(tmp_path: Path) -> None:
    provider = CompletingProvider()
    service, parent_id, _role_id, _workspace = scope(tmp_path, provider)
    runtime = service.workbench
    child = await runtime.create_child(parent_id, CreateChildAgentRequest(task="initial task"))
    await runtime.tasks[child.thread_id]
    tool = runtime.tools_for(parent_id)
    arguments = {
        "recipient_thread_id": child.thread_id,
        "body": "one private follow-up",
        "idempotency_key": "one-follow-up",
    }
    first = await tool.send(arguments)
    await runtime.tasks[child.thread_id]
    with service.store._connect() as connection:
        before = connection.execute(
            "SELECT wake_count FROM workbench_wake_counts WHERE thread_id=?",
            (child.thread_id,),
        ).fetchone()[0]
    calls = provider.calls
    repeated = await tool.send(arguments)
    assert repeated["message_id"] == first["message_id"]
    assert repeated["delivery_status"] == "consumed"
    assert child.thread_id not in runtime.tasks
    assert provider.calls == calls
    with service.store._connect() as connection:
        after = connection.execute(
            "SELECT wake_count FROM workbench_wake_counts WHERE thread_id=?",
            (child.thread_id,),
        ).fetchone()[0]
    assert before == after == 1


@pytest.mark.asyncio
async def test_private_message_reply_and_recipient_hints_preserve_scope(tmp_path: Path) -> None:
    service, parent_id, _role_id, workspace = scope(tmp_path)
    runtime = service.workbench
    child = await runtime.create_child(parent_id, CreateChildAgentRequest(task="initial task"))
    await runtime.tasks[child.thread_id]
    first = runtime.send(
        parent_id,
        SendAgentMessageRequest(
            recipient_thread_id=child.thread_id,
            body="new message",
            reply_to="   ",
            idempotency_key="first",
        ),
    )
    assert first.reply_to is None
    with pytest.raises(PermissionError, match="message_id"):
        runtime.send(
            parent_id,
            SendAgentMessageRequest(
                recipient_thread_id=child.thread_id,
                body="wrong reply target",
                reply_to=parent_id,
                idempotency_key="wrong-reply",
            ),
        )
    with pytest.raises(PermissionError, match="not this Thread"):
        runtime.send(
            child.thread_id,
            SendAgentMessageRequest(
                recipient_thread_id=child.thread_id,
                body="self message",
                idempotency_key="self",
            ),
        )
    child_session = service.get_session(child.session_id)
    tools = WorkspaceTools(
        workspace,
        policy=child_session.role_snapshot.tool_policy,
        collaboration=runtime.tools_for(child.thread_id),
    )
    send_tool = next(tool for tool in tools.definitions() if tool.name == "send_agent_message")
    recipients = send_tool.parameters["properties"]["recipient_thread_id"]["enum"]
    assert parent_id in recipients and child.thread_id not in recipients
    assert parent_id in send_tool.description
    assert "message_id" in send_tool.parameters["properties"]["reply_to"]["description"]
    grandchild = await runtime.create_child(
        child.thread_id, CreateChildAgentRequest(task="grandchild task")
    )
    await runtime.tasks[grandchild.thread_id]
    updated = next(tool for tool in tools.definitions() if tool.name == "send_agent_message")
    recipients = updated.parameters["properties"]["recipient_thread_id"]["enum"]
    assert parent_id in recipients and grandchild.thread_id in recipients
    assert child.thread_id not in recipients
    directed = runtime.send(
        child.thread_id,
        SendAgentMessageRequest(
            recipient_thread_id=grandchild.thread_id,
            body="child to grandchild",
            idempotency_key="grandchild-message",
        ),
    )
    assert directed.recipient_thread_id == grandchild.thread_id


@pytest.mark.asyncio
async def test_stale_child_lease_is_not_reclaimed_by_private_message(tmp_path: Path) -> None:
    service, parent_id, _role_id, _workspace = scope(tmp_path)
    original = service.workbench
    child = await original.create_child(parent_id, CreateChildAgentRequest(task="initial task"))
    await original.tasks[child.thread_id]
    uncertain_agent = service.factory.create_agent(child.session_id)
    with service.store._connect() as connection:
        connection.execute(
            "UPDATE workbench_children SET status='running' WHERE thread_id=?",
            (child.thread_id,),
        )
        connection.execute(
            "UPDATE session_run_leases SET released_at=NULL,expires_at=?,"
            "generation=generation+1,agent_id=? WHERE session_id=?",
            ("2000-01-01T00:00:00+00:00", uncertain_agent.id, child.session_id),
        )
    recovered = WorkbenchRuntime(service)
    service.workbench = recovered
    message, created = recovered.send_once(
        parent_id,
        SendAgentMessageRequest(
            recipient_thread_id=child.thread_id,
            body="do not replay unknown work",
            idempotency_key="after-restart",
        ),
    )
    assert created and message.delivery_status == "delivered"
    assert await recovered.wake(child.thread_id) == "pending"
    assert recovered.get_child(child.thread_id).status == "interrupted"
    assert recovered.get_child(child.thread_id).recovery == "manual_reconcile"
    assert child.thread_id not in recovered.tasks
    with service.store._connect() as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM workbench_wake_counts WHERE thread_id=?", (child.thread_id,)
            ).fetchone()
            is None
        )


@pytest.mark.asyncio
async def test_committed_child_result_is_reconciled_after_worker_exit(tmp_path: Path) -> None:
    service, parent_id, _role_id, _workspace = scope(tmp_path)
    child = await service.workbench.create_child(parent_id, CreateChildAgentRequest(task="work"))
    await service.workbench.tasks[child.thread_id]
    with service.store._connect() as connection:
        connection.execute(
            "UPDATE workbench_children SET status='running',result=NULL WHERE thread_id=?",
            (child.thread_id,),
        )
    recovered = WorkbenchRuntime(service)
    assert recovered.get_child(child.thread_id).status == "completed"
    assert recovered.get_child(child.thread_id).result == "child result"


@pytest.mark.asyncio
async def test_message_retry_returns_original_after_threads_cancelled(tmp_path: Path) -> None:
    service, parent_id, _role_id, workspace = scope(tmp_path)
    child_id = related_sender(service, parent_id, workspace)
    request = SendAgentMessageRequest(
        recipient_thread_id=child_id, body="once", idempotency_key="retry-after-cancel"
    )
    first = service.workbench.send(parent_id, request)
    service.workbench.cancel_tree(child_id)
    repeated = service.workbench.send(parent_id, request)
    assert repeated.message_id == first.message_id
    assert len(service.workbench.list_messages(child_id)) == 1
    with pytest.raises(ConflictError, match="must be active"):
        service.workbench.send(
            parent_id,
            request.model_copy(update={"idempotency_key": "new-after-cancel"}),
        )


@pytest.mark.asyncio
async def test_other_core_keeps_live_child_owner_and_cancels_it(tmp_path: Path) -> None:
    provider = BarrierProvider()
    owner, parent_id, _role_id, _workspace = scope(tmp_path, provider)
    owner._session_lease_heartbeat_seconds = 0.05
    child = await owner.workbench.create_child(parent_id, CreateChildAgentRequest(task="wait"))
    assert await asyncio.wait_for(provider.started.get(), timeout=10) == 1
    observer = ApplicationService(
        SQLiteStore(owner.store.path),
        CompletingProvider(),
        artifact_root=tmp_path / "other-artifacts",
    )
    observer.initialize()
    assert observer.workbench.get_child(child.thread_id).status == "running"
    assert observer.workbench.cancel_tree(child.thread_id).status == "cancelled"
    await asyncio.wait_for(owner.workbench.tasks[child.thread_id], timeout=10)
    assert provider.calls == 1
    assert owner.workbench.get_child(child.thread_id).status == "cancelled"
    assert owner.store.get_session_run_lease(child.session_id).released_at is not None
    assert any(
        event.event_type == "agent.cancelled" for event in owner.store.list_events(child.session_id)
    ), [event.event_type for event in owner.store.list_events(child.session_id)]


@pytest.mark.asyncio
async def test_two_cores_reserve_only_one_private_message_wake(tmp_path: Path) -> None:
    provider = CompletingProvider()
    first, parent_id, _role_id, _workspace = scope(tmp_path, provider)
    child = await first.workbench.create_child(parent_id, CreateChildAgentRequest(task="initial"))
    await first.workbench.tasks[child.thread_id]
    second = ApplicationService(
        SQLiteStore(first.store.path),
        provider,
        artifact_root=tmp_path / "other-artifacts",
    )
    second.initialize()
    first.workbench.send(
        parent_id,
        SendAgentMessageRequest(
            recipient_thread_id=child.thread_id,
            body="one wake",
            idempotency_key="one-wake",
        ),
    )
    outcomes = await asyncio.gather(
        first.workbench.wake(child.thread_id),
        second.workbench.wake(child.thread_id),
    )
    assert sorted(outcomes) == ["active_run", "scheduled"]
    for runtime in (first.workbench, second.workbench):
        if child.thread_id in runtime.tasks:
            await runtime.tasks[child.thread_id]
    assert provider.calls == 2
    assert first.workbench.list_messages(child.thread_id)[0].delivery_status == "consumed"
    with first.store._connect() as connection:
        assert (
            connection.execute(
                "SELECT wake_count FROM workbench_wake_counts WHERE thread_id=?",
                (child.thread_id,),
            ).fetchone()[0]
            == 1
        )


@pytest.mark.asyncio
async def test_cancel_wins_before_agent_completion_is_committed(tmp_path: Path) -> None:
    service, parent_id, _role_id, _workspace = scope(tmp_path)
    original = service._persist_runtime_event
    child_id: str | None = None
    child_session_id: str | None = None

    def persist_with_cancel(session_id, agent_id, event, **kwargs):  # type: ignore[no-untyped-def]
        if event.event_type == "agent.completed" and session_id == child_session_id:
            assert child_id is not None
            service.workbench.cancel_tree(child_id)
        return original(session_id, agent_id, event, **kwargs)

    service._persist_runtime_event = persist_with_cancel  # type: ignore[method-assign]
    child = await service.workbench.create_child(parent_id, CreateChildAgentRequest(task="race"))
    child_id, child_session_id = child.thread_id, child.session_id
    await service.workbench.tasks[child.thread_id]
    assert service.workbench.get_child(child.thread_id).status == "cancelled"
    assert service.get_thread(child.thread_id).status.value == "cancelled"
    assert service.store.get_session_run_lease(child.session_id).released_at is not None
    events = service.store.list_events(child.session_id)
    assert sum(event.event_type == "agent.cancelled" for event in events) == 1
    assert not any(event.event_type == "agent.completed" for event in events)
    assert service.store.get_agent(events[-1].agent_id).status.value == "cancelled"


@pytest.mark.asyncio
async def test_cancel_after_committed_completion_preserves_completed_child(tmp_path: Path) -> None:
    service, parent_id, _role_id, _workspace = scope(tmp_path)
    child = await service.workbench.create_child(parent_id, CreateChildAgentRequest(task="done"))
    await service.workbench.tasks[child.thread_id]
    assert service.workbench.cancel_tree(child.thread_id).status == "completed"
    assert service.get_thread(child.thread_id).status.value == "active"
    assert service.store.get_session_run_lease(child.session_id).cancel_requested is False
    assert [event.event_type for event in service.store.list_events(child.session_id)].count(
        "agent.completed"
    ) == 1


@pytest.mark.asyncio
async def test_parent_cancel_before_completion_is_committed(tmp_path: Path) -> None:
    service, parent_id, _role_id, workspace = scope(tmp_path)
    session = thread_session(service, parent_id)
    original = service._persist_runtime_event

    def persist_with_cancel(session_id, agent_id, event, **kwargs):  # type: ignore[no-untyped-def]
        if event.event_type == "agent.completed" and session_id == session.id:
            service.workbench.cancel_tree(parent_id)
        return original(session_id, agent_id, event, **kwargs)

    service._persist_runtime_event = persist_with_cancel  # type: ignore[method-assign]
    events = [
        event
        async for event in service.run_session(
            session.id,
            user_message="finish",
            workspace=workspace,
            thread_id=parent_id,
        )
    ]
    assert events[-1].event_type == "agent.cancelled"
    assert service.get_thread(parent_id).status.value == "cancelled"
    assert service.store.get_session_run_lease(session.id).released_at is not None
    assert not any(
        event.event_type == "agent.completed" for event in service.store.list_events(session.id)
    )


@pytest.mark.asyncio
async def test_late_mail_after_parent_model_request_wakes_once(tmp_path: Path) -> None:
    provider = BarrierProvider()
    service, parent_id, _role_id, workspace = scope(tmp_path, provider)
    sender_id = related_sender(service, parent_id, workspace)
    parent_session = thread_session(service, parent_id)

    async def run_parent() -> list[Any]:
        return [
            event
            async for event in service.run_session(
                parent_session.id,
                user_message="initial work",
                workspace=workspace,
                thread_id=parent_id,
            )
        ]

    first_run = asyncio.create_task(run_parent())
    assert await asyncio.wait_for(provider.started.get(), timeout=10) == 1
    message = service.workbench.send(
        sender_id,
        SendAgentMessageRequest(
            recipient_thread_id=parent_id, body="late private fact", idempotency_key="late-parent"
        ),
    )
    assert await service.workbench.wake(parent_id) == "active_run"
    provider.permits.put_nowait(None)
    first_events = await asyncio.wait_for(first_run, timeout=10)
    assert any(event.event_type == "agent.completed" for event in first_events)
    assert await asyncio.wait_for(provider.started.get(), timeout=10) == 2
    provider.permits.put_nowait(None)
    followup = service.workbench.wake_tasks[parent_id]
    await asyncio.wait_for(followup, timeout=10)
    await asyncio.gather(*tuple(service.workbench.completion_checks))
    assert provider.calls == 2
    assert service.workbench.list_messages(parent_id)[0].message_id == message.message_id
    assert service.workbench.list_messages(parent_id)[0].delivery_status == "consumed"
    with service.store._connect() as connection:
        assert (
            connection.execute(
                "SELECT wake_count FROM workbench_wake_counts WHERE thread_id=?", (parent_id,)
            ).fetchone()[0]
            == 1
        )


@pytest.mark.asyncio
async def test_late_mail_after_child_model_request_wakes_once(tmp_path: Path) -> None:
    provider = BarrierProvider()
    service, parent_id, _role_id, _workspace = scope(tmp_path, provider)
    runtime = service.workbench
    child = await runtime.create_child(parent_id, CreateChildAgentRequest(task="initial work"))
    first_run = runtime.tasks[child.thread_id]
    assert await asyncio.wait_for(provider.started.get(), timeout=10) == 1
    message = runtime.send(
        parent_id,
        SendAgentMessageRequest(
            recipient_thread_id=child.thread_id,
            body="late instruction",
            idempotency_key="late-child",
        ),
    )
    assert await runtime.wake(child.thread_id) == "active_run"
    provider.permits.put_nowait(None)
    await asyncio.wait_for(first_run, timeout=10)
    assert await asyncio.wait_for(provider.started.get(), timeout=10) == 2
    provider.permits.put_nowait(None)
    followup = runtime.tasks[child.thread_id]
    assert followup is not first_run
    await asyncio.wait_for(followup, timeout=10)
    await asyncio.gather(*tuple(runtime.completion_checks))
    assert provider.calls == 2
    assert runtime.list_messages(child.thread_id)[0].message_id == message.message_id
    assert runtime.list_messages(child.thread_id)[0].delivery_status == "consumed"
    with service.store._connect() as connection:
        assert (
            connection.execute(
                "SELECT wake_count FROM workbench_wake_counts WHERE thread_id=?",
                (child.thread_id,),
            ).fetchone()[0]
            == 1
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["failed", "cancelled", "quota"])
async def test_late_mail_is_pending_without_safe_followup(tmp_path: Path, outcome: str) -> None:
    provider = BarrierProvider(fail_first=outcome == "failed")
    service, parent_id, _role_id, workspace = scope(tmp_path, provider)
    sender_id = related_sender(service, parent_id, workspace)
    parent_session = thread_session(service, parent_id)
    if outcome == "quota":
        for _ in range(4):
            assert service.store.reserve_workbench_wake(parent_id, limit=4)

    async def run_parent() -> list[Any]:
        return [
            event
            async for event in service.run_session(
                parent_session.id,
                user_message="initial work",
                workspace=workspace,
                thread_id=parent_id,
            )
        ]

    first_run = asyncio.create_task(run_parent())
    assert await asyncio.wait_for(provider.started.get(), timeout=10) == 1
    message = service.workbench.send(
        sender_id,
        SendAgentMessageRequest(
            recipient_thread_id=parent_id,
            body="keep pending if unsafe",
            idempotency_key=f"late-{outcome}",
        ),
    )
    assert await service.workbench.wake(parent_id) == "active_run"
    if outcome == "cancelled":
        service.cancel_session(parent_session.id)
    else:
        provider.permits.put_nowait(None)
    events = await asyncio.wait_for(first_run, timeout=10)
    if outcome == "quota":
        await asyncio.gather(*tuple(service.workbench.completion_checks))
    assert provider.calls == 1
    assert any(
        event.event_type
        == (
            "agent.failed"
            if outcome == "failed"
            else "agent.cancelled"
            if outcome == "cancelled"
            else "agent.completed"
        )
        for event in events
    )
    assert service.workbench.list_messages(parent_id)[0].message_id == message.message_id
    assert service.workbench.list_messages(parent_id)[0].delivery_status == "delivered"
    with service.store._connect() as connection:
        row = connection.execute(
            "SELECT wake_count FROM workbench_wake_counts WHERE thread_id=?", (parent_id,)
        ).fetchone()
    assert (None if row is None else row["wake_count"]) == (4 if outcome == "quota" else None)


@pytest.mark.asyncio
async def test_forbidden_child_role_returns_tool_error_without_child(tmp_path: Path) -> None:
    provider = RepairingDelegateProvider()
    service, parent_id, _role_id, workspace = scope(tmp_path, provider)
    profile_id = thread_session(service, parent_id).role_snapshot.model_profile_id
    stronger = service.create_role(
        RolePreset(
            name="stronger",
            system_prompt="stronger",
            model_profile_id=profile_id,
            tool_policy=ToolPolicy(allowed_tools=("read_file", "run_command")),
        )
    )
    provider.denied_role_id = stronger.id
    session_id = thread_session(service, parent_id).id
    events = [
        event
        async for event in service.run_session(
            session_id, user_message="delegate", workspace=workspace, thread_id=parent_id
        )
    ]
    assert provider.saw_tool_error
    assert any(event.event_type == "tool.failed" for event in events)
    assert any(event.event_type == "agent.completed" for event in events)
    assert service.workbench.list_children(parent_id) == []
