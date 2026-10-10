from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from starlette.requests import Request

import operant.api as api_module
from operant.api import (
    RunSessionRequest,
    _first_stream_summary,
    _parse_sse_frame,
    _session_sse_with_heartbeat,
    _sse_event,
    create_app,
)
from operant.domain.actions import CommandExecutionStatus
from operant.domain.messages import Message, ModelResponse, ProviderEvent, ToolCall, ToolDefinition
from operant.domain.models import AgentStatus, ModelProfile, RolePreset, ToolPolicy


class _ApprovalProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def stream(
        self,
        *,
        snapshot: Any,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        del snapshot, messages, tools
        self.calls += 1
        if self.calls == 1:
            yield ProviderEvent(
                event_type="model.completed",
                response=ModelResponse(
                    tool_calls=(
                        ToolCall(
                            id="heartbeat-tool-call",
                            name="run_command",
                            arguments_json='{"argv":["git","add","change.txt"]}',
                        ),
                    ),
                    finish_reason="tool_calls",
                ),
            )
        else:
            yield ProviderEvent(
                event_type="model.completed",
                response=ModelResponse(content="approved", finish_reason="stop"),
            )


class _WaitingProvider:
    async def stream(
        self,
        *,
        snapshot: Any,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        del snapshot, messages, tools
        await asyncio.Event().wait()
        if False:
            yield ProviderEvent(event_type="unreachable")


def _start_response(app: Any, session_id: str, workspace: Path) -> Any:
    route = next(
        route
        for route in app.routes
        if getattr(route, "path", "") == "/v1/sessions/{session_id}/runs"
        and "POST" in getattr(route, "methods", set())
    )
    raw_request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"/v1/sessions/{session_id}/runs",
            "query_string": b"",
            "headers": [],
        }
    )
    return route.endpoint(
        session_id,
        RunSessionRequest(message="stage change", workspace=str(workspace)),
        raw_request,
    )


def _event(name: str, cursor: int) -> str:
    return _sse_event(
        name, {"event_type": name, "turn": 0, "cursor": cursor, "payload": {}}, cursor=cursor
    )


@pytest.mark.asyncio
async def test_heartbeat_waits_for_durable_first_event_and_delayed_approval_continues() -> None:
    first_ready = asyncio.Event()
    approval_decided = asyncio.Event()
    closed = asyncio.Event()
    pulls = 0

    async def source() -> AsyncIterator[str]:
        nonlocal pulls
        try:
            pulls += 1
            await first_ready.wait()
            yield _event("agent.started", 1)
            pulls += 1
            yield _event("tool.approval_required", 2)
            pulls += 1
            await approval_decided.wait()
            yield _event("tool.approval_decided", 3)
        finally:
            closed.set()

    stream = _session_sse_with_heartbeat(source(), interval_seconds=0.01)
    first = asyncio.create_task(anext(stream))
    await asyncio.sleep(0.035)
    assert not first.done()  # A comment cannot become the Command's first frame.
    assert pulls == 1
    first_ready.set()
    assert await first == _event("agent.started", 1)
    assert await anext(stream) == _event("tool.approval_required", 2)
    for _ in range(3):
        assert await anext(stream) == ": keep-alive\n\n"
        assert pulls == 3  # Timeouts do not restart or duplicate the upstream pull.
    approval_decided.set()
    assert await anext(stream) == _event("tool.approval_decided", 3)
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert closed.is_set()


@pytest.mark.asyncio
async def test_heartbeat_stream_disconnect_cancels_upstream_pull_and_closes_generator() -> None:
    waiting = asyncio.Event()
    cancelled = asyncio.Event()
    closed = asyncio.Event()

    async def source() -> AsyncIterator[str]:
        try:
            yield _event("agent.started", 1)
            waiting.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        finally:
            closed.set()

    stream = _session_sse_with_heartbeat(source(), interval_seconds=0.01)
    assert await anext(stream) == _event("agent.started", 1)
    await waiting.wait()
    assert await anext(stream) == ": keep-alive\n\n"
    await stream.aclose()
    assert cancelled.is_set()
    assert closed.is_set()


@pytest.mark.asyncio
async def test_transport_cancellation_while_waiting_closes_upstream() -> None:
    cancelled = asyncio.Event()

    async def source() -> AsyncIterator[str]:
        yield _event("agent.started", 1)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    stream = _session_sse_with_heartbeat(source(), interval_seconds=0.1)
    assert await anext(stream) == _event("agent.started", 1)
    read = asyncio.create_task(anext(stream))
    await asyncio.sleep(0.01)
    read.cancel()
    with pytest.raises(asyncio.CancelledError):
        await read
    assert cancelled.is_set()


def test_comment_cannot_satisfy_command_first_frame(tmp_path: Path) -> None:
    comment = b": keep-alive\n\n"
    assert _parse_sse_frame(comment) is None
    app = create_app(tmp_path / "heartbeat.sqlite3", artifact_root=tmp_path / "artifacts")
    status, summary, error_code, *_ = _first_stream_summary(
        store=app.state.operant_service.store,
        command_id="command_1",
        path="/v1/sessions/session_1/runs",
        frame=comment,
    )
    assert status is CommandExecutionStatus.MANUAL_RECONCILE_REQUIRED
    assert summary is None
    assert error_code == "stream_first_frame_invalid"


@pytest.mark.asyncio
async def test_session_route_approval_survives_multiple_heartbeats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "change.txt").write_text("change\n", encoding="utf-8")
    app = create_app(tmp_path / "session.sqlite3", artifact_root=tmp_path / "artifacts")
    service = app.state.operant_service
    provider = _ApprovalProvider()
    service.provider = provider
    profile = service.add_model_profile(
        ModelProfile(
            name="heartbeat",
            model_id="heartbeat",
            base_url="https://example.invalid/v1",
            secret_ref="OPERANT_TEST_UNUSED_KEY",
        )
    )
    role = service.create_role(
        RolePreset(
            name="Heartbeat",
            system_prompt="Test approval.",
            model_profile_id=profile.id,
            tool_policy=ToolPolicy(allowed_tools=("run_command",), command_execution=True),
        )
    )
    session = service.create_session(role.id)
    original_heartbeat = api_module._session_sse_with_heartbeat
    monkeypatch.setattr(
        api_module,
        "_session_sse_with_heartbeat",
        lambda source: original_heartbeat(source, interval_seconds=0.01),
    )
    response = await _start_response(app, session.id, tmp_path)
    stream = response.body_iterator
    for _ in range(30):
        frame = await asyncio.wait_for(anext(stream), timeout=5)
        parsed = _parse_sse_frame(frame.encode("utf-8"))
        if parsed is not None and parsed[0] == "tool.approval_required":
            break
    else:
        pytest.fail("durable approval was not requested")
    event_count_at_approval = len(service.list_events(session.id))
    for _ in range(3):
        assert await asyncio.wait_for(anext(stream), timeout=1) == ": keep-alive\n\n"
        assert provider.calls == 1
        assert len(service.list_events(session.id)) == event_count_at_approval
        assert (
            service.store.list_approval_requests(session.id, status=None)[0].status.value
            == "pending"
        )
    tool_call_id = parsed[1]["payload"]["tool_call_id"]
    assert service.submit_approval(session.id, tool_call_id, approved=True)
    for _ in range(30):
        frame = await asyncio.wait_for(anext(stream), timeout=5)
        parsed = _parse_sse_frame(frame.encode("utf-8"))
        if parsed is not None and parsed[0] == "agent.completed":
            break
    else:
        pytest.fail("approved Session did not complete")
    await stream.aclose()
    assert provider.calls == 2
    assert service.admitted_session_run_lease(session.id) is None
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert staged.stdout.strip() == "change.txt"


@pytest.mark.asyncio
async def test_session_route_disconnect_cancels_agent_and_releases_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(tmp_path / "disconnect.sqlite3", artifact_root=tmp_path / "artifacts")
    service = app.state.operant_service
    service.provider = _WaitingProvider()
    profile = service.add_model_profile(
        ModelProfile(
            name="heartbeat-disconnect",
            model_id="heartbeat-disconnect",
            base_url="https://example.invalid/v1",
            secret_ref="OPERANT_TEST_UNUSED_KEY",
        )
    )
    role = service.create_role(
        RolePreset(
            name="Heartbeat disconnect",
            system_prompt="Test disconnect.",
            model_profile_id=profile.id,
        )
    )
    session = service.create_session(role.id)
    original_heartbeat = api_module._session_sse_with_heartbeat
    monkeypatch.setattr(
        api_module,
        "_session_sse_with_heartbeat",
        lambda source: original_heartbeat(source, interval_seconds=0.01),
    )
    response = await _start_response(app, session.id, tmp_path)
    stream = response.body_iterator
    first = await asyncio.wait_for(anext(stream), timeout=5)
    assert _parse_sse_frame(first.encode("utf-8"))[0] == "agent.started"
    lease = service.admitted_session_run_lease(session.id)
    assert lease is not None and lease.agent_id is not None
    assert await asyncio.wait_for(anext(stream), timeout=1) == ": keep-alive\n\n"
    await stream.aclose()
    assert service.admitted_session_run_lease(session.id) is None
    assert service.store.get_agent(lease.agent_id).status is AgentStatus.CANCELLED
    assert service.list_events(session.id)[-1].payload["reason"] == "stream_cancelled"


@pytest.mark.asyncio
async def test_asgi_disconnect_after_first_receipt_frame_closes_session(
    tmp_path: Path,
) -> None:
    app = create_app(tmp_path / "asgi-disconnect.sqlite3", artifact_root=tmp_path / "artifacts")
    service = app.state.operant_service
    service.provider = _WaitingProvider()
    profile = service.add_model_profile(
        ModelProfile(
            name="asgi-disconnect",
            model_id="asgi-disconnect",
            base_url="https://example.invalid/v1",
            secret_ref="OPERANT_TEST_UNUSED_KEY",
        )
    )
    role = service.create_role(
        RolePreset(
            name="ASGI disconnect",
            system_prompt="Test transport disconnect.",
            model_profile_id=profile.id,
        )
    )
    session = service.create_session(role.id)
    body = json.dumps({"message": "wait", "workspace": str(tmp_path)}).encode("utf-8")
    disconnect = asyncio.Event()
    body_received = False
    first_frame_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal body_received
        if not body_received:
            body_received = True
            return {"type": "http.request", "body": body, "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        nonlocal first_frame_sent
        if message["type"] == "http.response.body" and b"event: agent.started" in message.get(
            "body", b""
        ):
            first_frame_sent = True
            disconnect.set()

    path = f"/v1/sessions/{session.id}/runs"
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "root_path": "",
        "query_string": b"",
        "server": ("127.0.0.1", 8000),
        "client": ("127.0.0.1", 12345),
        "headers": [
            (b"host", b"127.0.0.1:8000"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"idempotency-key", b"asgi-disconnect"),
        ],
    }
    await asyncio.wait_for(app(scope, receive, send), timeout=5)
    assert first_frame_sent
    assert service.admitted_session_run_lease(session.id) is None
    events = service.list_events(session.id)
    assert events[-1].event_type == "agent.cancelled"
    assert events[-1].payload["reason"] == "stream_cancelled"
