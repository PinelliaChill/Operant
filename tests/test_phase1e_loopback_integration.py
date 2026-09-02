from __future__ import annotations

import json
import socket
import sqlite3
import sys
import threading
import time
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import pytest
import uvicorn

from operant.api import create_app
from operant.domain.messages import Message, ModelResponse, ProviderEvent, ToolCall, ToolDefinition
from operant.domain.models import Budget, ModelProfile, RolePreset, RoleSnapshot, ToolPolicy
from operant.domain.threads import ConversationThread, LegacySourceType, ThreadLegacyRef
from operant.providers.base import ModelProvider

# The wheel intentionally packages only ``src/operant``; the generated client
# is exercised directly from this repository checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sdk.python_client.phase1e_generated import (
    OPERATION_DEFINITIONS,
    PHASE1E_MAX_CURSOR,
    PHASE1E_PROTOCOL_VERSION,
    PHASE1E_SCHEMA_DIGEST,
    Phase1EClient,
    RunSessionStream,
)
from sdk.python_client.transport import Phase1EError

_SCHEMA_PATH = Path(__file__).resolve().parents[1] / (
    "sdk/protocol/schema/operant-phase1e.openapi.json"
)


class _DeterministicProvider:
    """A local provider that asks for one shell approval per session."""

    def __init__(self) -> None:
        self.calls = 0
        self._lock = threading.Lock()

    async def list_models(self, *, base_url: str, secret_ref: str) -> list[str]:
        del base_url, secret_ref
        return ["phase1e-local-model"]

    async def stream(
        self,
        *,
        snapshot: RoleSnapshot,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        del snapshot, messages, tools
        with self._lock:
            self.calls += 1
            call_number = self.calls
        if call_number % 2:
            yield ProviderEvent(
                event_type="model.completed",
                response=ModelResponse(
                    tool_calls=(
                        ToolCall(
                            id=f"local-shell-{call_number}",
                            name="run_command",
                            arguments_json=json.dumps(
                                {
                                    "argv": [
                                        "sh",
                                        "-c",
                                        "printf 'approved\\n' >> action-marker.txt",
                                    ]
                                }
                            ),
                        ),
                    ),
                    finish_reason="tool_calls",
                ),
            )
        else:
            yield ProviderEvent(
                event_type="model.completed",
                response=ModelResponse(
                    content="deterministic complete",
                    finish_reason="stop",
                ),
            )


@contextmanager
def _running_uvicorn(app: Any) -> Iterator[str]:
    """Serve the app on an actual loopback socket for urllib transport tests."""

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = int(listener.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            loop="asyncio",
            lifespan="on",
            log_level="error",
            access_log=False,
        )
    )
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        name="phase1e-loopback-uvicorn",
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        raise RuntimeError("uvicorn loopback server did not start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        if thread.is_alive():
            raise RuntimeError("uvicorn loopback server did not stop")


def _schema() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(_SCHEMA_PATH.read_text(encoding="utf-8")))


def _assert_schema_shape(name: str, payload: object, schemas: dict[str, Any]) -> None:
    assert isinstance(payload, dict), name
    definition = schemas[name]
    required = set(definition.get("required", ()))
    properties = set(definition.get("properties", ()))
    assert required <= set(payload), (name, required - set(payload))
    if definition.get("additionalProperties") is False:
        assert set(payload) == properties, (name, set(payload) ^ properties)


def _assert_int64(value: object, *, allow_none: bool = False) -> None:
    if value is None and allow_none:
        return
    assert isinstance(value, int) and not isinstance(value, bool)
    assert 0 <= value <= PHASE1E_MAX_CURSOR


def _assert_projection_shapes(payload: dict[str, Any], schemas: dict[str, Any]) -> None:
    _assert_schema_shape("ProjectProjection", payload, schemas)
    assert isinstance(payload["project_id"], str)
    assert isinstance(payload["workspace_ref"], str)
    assert isinstance(payload["readable"], bool)
    assert isinstance(payload["writable"], bool)
    assert isinstance(payload["created_at"], str)
    assert isinstance(payload["threads"], list)
    for thread in payload["threads"]:
        _assert_schema_shape("ProjectThreadSummary", thread, schemas)
    assert isinstance(payload["workflow_runs"], list)
    for workflow_run in payload["workflow_runs"]:
        _assert_schema_shape("ProjectWorkflowRunSummary", workflow_run, schemas)


def _assert_command_rows(database: Path) -> list[tuple[Any, ...]]:
    with sqlite3.connect(database) as connection:
        return connection.execute(
            "SELECT id, idempotency_key, status, http_status FROM command_executions "
            "ORDER BY created_at, id"
        ).fetchall()


def _wait_for_approval(
    client: Phase1EClient,
    session_id: str,
    *,
    timeout: float = 8,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        approvals = client.list_pending_approvals(session_id)
        if approvals:
            return cast(dict[str, Any], approvals[0])
        time.sleep(0.02)
    raise AssertionError(f"approval was not requested for {session_id}")


def _run_in_thread(
    client: Phase1EClient,
    session_id: str,
    *,
    idempotency_key: str,
    workspace: Path,
    thread_id: str | None = None,
) -> tuple[threading.Thread, dict[str, Any]]:
    result: dict[str, Any] = {}

    def consume() -> None:
        try:
            stream = client.run_session_stream(
                session_id,
                {
                    "message": "run deterministic local provider",
                    "workspace": str(workspace),
                    "thread_id": thread_id,
                },
                idempotency_key=idempotency_key,
            )
            result["stream"] = stream
            result["events"] = list(stream.events)
        except BaseException as exc:  # surface the worker failure in the test thread
            result["error"] = exc

    thread = threading.Thread(target=consume, name=f"phase1e-run-{session_id}", daemon=True)
    thread.start()
    return thread, result


def _assert_run_result(result: dict[str, Any]) -> tuple[RunSessionStream, list[dict[str, Any]]]:
    assert "error" not in result, result.get("error")
    stream = result.get("stream")
    events = result.get("events")
    assert isinstance(stream, RunSessionStream)
    assert isinstance(events, list) and events
    return stream, cast(list[dict[str, Any]], events)


def test_phase1e_core_loopback_urllib_closed_loop(tmp_path: Path) -> None:
    """Exercise all eight frozen operations against Core over a real socket."""

    frozen = _schema()
    schemas = cast(dict[str, Any], frozen["components"]["schemas"])
    operation_ids = [
        operation["operationId"]
        for path in cast(dict[str, Any], frozen["paths"]).values()
        for operation in cast(dict[str, Any], path).values()
    ]
    assert operation_ids == [
        "negotiateProtocol",
        "listProjects",
        "listWorkspaceFiles",
        "listThreads",
        "createSession",
        "runSessionStream",
        "listPendingApprovals",
        "submitApproval",
    ]
    assert set(OPERATION_DEFINITIONS) == set(operation_ids)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.txt").write_text("local integration fixture\n", encoding="utf-8")
    (workspace / "nested").mkdir()
    (workspace / "nested" / "note.txt").write_text("metadata only\n", encoding="utf-8")
    database = tmp_path / "phase1e-loopback.sqlite3"
    provider = _DeterministicProvider()
    app = create_app(database, artifact_root=tmp_path / "artifacts")
    service = app.state.operant_service
    service.provider = cast(ModelProvider, provider)
    profile = service.add_model_profile(
        ModelProfile(
            id="phase1e-local-profile",
            name="Phase 1E local fixture",
            model_id="phase1e-local-model",
            base_url="http://127.0.0.1:9",
            secret_ref="OPERANT_PHASE1E_UNUSED_KEY",
        )
    )
    role = service.create_role(
        RolePreset(
            id="phase1e-local-role",
            name="Phase 1E local role",
            system_prompt="Use the deterministic local provider.",
            model_profile_id=profile.id,
            tool_policy=ToolPolicy(
                allowed_tools=("run_command",),
                workspace_write=True,
                command_execution=True,
            ),
            budget=Budget(max_turns=3, timeout_seconds=20),
        )
    )
    initialization, created = service.initialize_workspace(workspace)
    assert created is True
    with _running_uvicorn(app) as base_url:
        client = Phase1EClient(base_url)
        protocol = client.negotiate_protocol()
        _assert_schema_shape("ProtocolNegotiation", protocol, schemas)
        assert protocol["protocol_version"] == PHASE1E_PROTOCOL_VERSION
        assert protocol["min_client_version"] == PHASE1E_PROTOCOL_VERSION
        assert protocol["schema_digest"] == PHASE1E_SCHEMA_DIGEST
        assert client.last_response is not None and client.last_response.status == 200

        session = client.create_session({"role_id": role.id}, idempotency_key="session-create")
        _assert_schema_shape("Session", session, schemas)
        _assert_schema_shape("RoleSnapshot", session["role_snapshot"], schemas)
        assert session["role_snapshot"]["role_id"] == role.id
        assert client.last_response is not None and client.last_response.status == 201
        assert client.last_response.idempotency_key == "session-create"

        thread = service.create_thread(
            ConversationThread(
                workspace_ref=str(workspace.resolve()),
                legacy_refs=(
                    ThreadLegacyRef(
                        source_type=LegacySourceType.SESSION,
                        source_id=session["id"],
                    ),
                ),
            )
        )

        projects = client.list_projects(limit=100)
        assert len(projects) == 1
        _assert_projection_shapes(projects[0], schemas)
        assert projects[0]["project_id"] == initialization.id
        assert projects[0]["workspace_ref"] == str(workspace.resolve())
        assert projects[0]["threads"][0]["id"] == thread.id

        files = client.list_workspace_files(initialization.id, limit=100)
        _assert_schema_shape("WorkspaceFilesPage", files, schemas)
        assert files["workspace_id"] == initialization.id
        assert files["path"] == "."
        assert isinstance(files["snapshot"], str) and len(files["snapshot"]) == 64
        for entry in files["entries"]:
            _assert_schema_shape("WorkspaceFileEntry", entry, schemas)
            assert isinstance(entry["path"], str)
            assert isinstance(entry["name"], str)
            assert entry["type"] in {"file", "directory"}
            _assert_int64(entry["size_bytes"], allow_none=True)

        threads = client.list_threads(workspace_ref=str(workspace.resolve()), limit=100)
        assert len(threads) == 1
        _assert_schema_shape("ThreadProjection", threads[0], schemas)
        _assert_int64(threads[0]["cursor"], allow_none=True)
        assert threads[0]["id"] == thread.id
        assert threads[0]["workspace_ref"] == str(workspace.resolve())
        assert threads[0]["legacy_refs"] == [{"source_type": "session", "source_id": session["id"]}]
        assert client.list_projects(after_cursor=PHASE1E_MAX_CURSOR) == []
        assert client.list_threads(after_cursor=PHASE1E_MAX_CURSOR) == []

        assert client.list_pending_approvals(session["id"]) == []

        run_client = Phase1EClient(base_url)
        control_client = Phase1EClient(base_url)
        run_thread, run_result = _run_in_thread(
            run_client,
            session["id"],
            idempotency_key="run-once",
            workspace=workspace,
            thread_id=thread.id,
        )
        pending = _wait_for_approval(control_client, session["id"])
        _assert_schema_shape("ApprovalProjection", pending, schemas)
        assert pending["status"] == "pending"
        assert pending["category"] == "shell"
        assert len(pending["action_hash"]) == 64
        assert "action-marker.txt" not in pending["detail"]
        _assert_int64(pending.get("cursor"), allow_none=True)

        approved = control_client.submit_approval(
            session["id"],
            pending["tool_call_id"],
            {"approved": True},
            idempotency_key="approval-once",
        )
        _assert_schema_shape("ApprovalDecisionResult", approved, schemas)
        assert approved == {
            "accepted": True,
            "changed": True,
            "approved": True,
            "status": "approved",
            "approval_id": pending["approval_id"],
            "continuation_available": True,
        }
        approved_replay = control_client.submit_approval(
            session["id"],
            pending["tool_call_id"],
            {"approved": True},
            idempotency_key="approval-once",
        )
        assert approved_replay == approved
        assert control_client.last_response is not None
        assert control_client.last_response.idempotency_replayed is True

        run_thread.join(timeout=15)
        assert not run_thread.is_alive()
        run_stream, run_events = _assert_run_result(run_result)
        assert run_stream.metadata.status == 200
        assert run_stream.receipt is None
        expected_events = {
            "agent.started",
            "tool.approval_required",
            "tool.approval_decided",
            "tool.completed",
            "agent.completed",
        }
        assert expected_events <= {str(frame["event"]) for frame in run_events}
        for frame in run_events:
            _assert_schema_shape("SseFrame", frame, schemas)
            _assert_int64(frame["id"], allow_none=True)
            assert isinstance(frame["event"], str)
            assert isinstance(frame["data"], dict)
        marker = workspace / "action-marker.txt"
        assert marker.read_text(encoding="utf-8") == "approved\n"
        assert provider.calls == 2

        with sqlite3.connect(database) as connection:
            action_rows = connection.execute(
                "SELECT status, result_json, error_code FROM tool_action_receipts "
                "WHERE session_id = ?",
                (session["id"],),
            ).fetchall()
        assert len(action_rows) == 1
        assert action_rows[0][0] == "completed"
        assert action_rows[0][1] is not None
        assert action_rows[0][2] is None

        normal_retry = client.run_session_stream(
            session["id"],
            {
                "message": "run deterministic local provider",
                "workspace": str(workspace),
                "thread_id": thread.id,
            },
            idempotency_key="run-once",
        )
        assert normal_retry.receipt is not None
        _assert_schema_shape("CommandReceipt", normal_retry.receipt, schemas)
        assert normal_retry.metadata.status == 202
        assert normal_retry.metadata.idempotency_replayed is True
        assert list(normal_retry.events) == []
        command_rows_before_replay = _assert_command_rows(database)

        reconnect = client.run_session_stream(
            session["id"],
            {"message": "read-only replay", "workspace": str(workspace)},
            idempotency_key="run-once",
            last_event_id=0,
        )
        replay_events = list(reconnect.events)
        assert reconnect.receipt is None
        assert reconnect.metadata.status == 200
        assert replay_events
        assert provider.calls == 2
        assert _assert_command_rows(database) == command_rows_before_replay

        with pytest.raises(Phase1EError) as missing_workspace:
            client.list_workspace_files("workspace-does-not-exist")
        error = missing_workspace.value
        assert error.code == "workspace_not_registered"
        assert error.retryable is False
        assert error.recovery == "none"
        _assert_schema_shape(
            "ErrorEnvelope",
            {
                "detail": error.detail,
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "retryable": error.retryable,
                    "recovery": error.recovery,
                },
            },
            schemas,
        )
        _assert_schema_shape(
            "ErrorDescriptor",
            {
                "code": error.code,
                "message": error.message,
                "retryable": error.retryable,
                "recovery": error.recovery,
            },
            schemas,
        )

        denied_session = client.create_session(
            {"role_id": role.id}, idempotency_key="session-denied"
        )
        denied_thread, denied_result = _run_in_thread(
            Phase1EClient(base_url),
            denied_session["id"],
            idempotency_key="run-denied",
            workspace=workspace,
        )
        denied_pending = _wait_for_approval(
            control_client,
            denied_session["id"],
        )
        denied = control_client.submit_approval(
            denied_session["id"],
            denied_pending["tool_call_id"],
            {"approved": False},
            idempotency_key="approval-denied",
        )
        _assert_schema_shape("ApprovalDecisionResult", denied, schemas)
        assert denied["accepted"] is True
        assert denied["changed"] is True
        assert denied["approved"] is False
        assert denied["status"] == "denied"
        assert denied["approval_id"] == denied_pending["approval_id"]
        denied_replay = control_client.submit_approval(
            denied_session["id"],
            denied_pending["tool_call_id"],
            {"approved": False},
            idempotency_key="approval-denied",
        )
        assert denied_replay == denied
        assert control_client.last_response is not None
        assert control_client.last_response.idempotency_replayed is True
        denied_thread.join(timeout=15)
        assert not denied_thread.is_alive()
        _, denied_events = _assert_run_result(denied_result)
        assert any(
            frame["event"] == "tool.failed"
            and json.loads(str(frame["data"]["payload"]["result"]))["error"] == "approval_denied"
            for frame in denied_events
        )
        assert marker.read_text(encoding="utf-8") == "approved\n"
        assert provider.calls == 4
        with sqlite3.connect(database) as connection:
            denied_action_rows = connection.execute(
                "SELECT status, error_code FROM tool_action_receipts WHERE session_id = ?",
                (denied_session["id"],),
            ).fetchall()
        assert denied_action_rows == [("failed", "approval_denied")]

    approval_statuses = set(cast(dict[str, Any], schemas["ApprovalStatus"])["enum"])
    assert {"pending", "approved", "denied", "expired", "cancelled"} <= approval_statuses
