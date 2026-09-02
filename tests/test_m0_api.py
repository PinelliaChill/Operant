from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.testclient import TestClient
from starlette.requests import Request

from operant.api import MAX_FIRST_SSE_FRAME_BYTES, RunSessionRequest, create_app
from operant.application.service import ApplicationService
from operant.domain.evaluation import EvaluationRun, EvaluationRunEvent
from operant.domain.messages import ModelResponse, ProviderEvent
from operant.domain.models import Event, ModelProfile, RolePreset
from operant.domain.workflow import WorkflowRun, WorkflowRunEvent, WorkflowRunStatus
from operant.persistence.sqlite import SQLiteStore
from operant.providers.openai_compatible import OpenAICompatibleProvider

MODEL_PAYLOAD = {
    "name": "m0-api",
    "model_id": "m0-api-model",
    "base_url": "https://example.invalid/v1",
    "secret_ref": "OPERANT_TEST_KEY",
}


def _command_rows(path: Path) -> list[sqlite3.Row]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(
            "SELECT * FROM command_executions ORDER BY created_at, id"
        ).fetchall()
    finally:
        connection.close()


def _insert_evaluation_run(store: SQLiteStore) -> EvaluationRun:
    run = EvaluationRun(id="eval_run_api_cursor", suite_id="suite_api_cursor")
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "INSERT INTO evaluation_suites(id, body, experiment, status, created_at) "
            "VALUES (?, '{}', NULL, 'ready', ?)",
            (run.suite_id, run.created_at.isoformat()),
        )
        connection.execute(
            "INSERT INTO evaluation_runs(id, suite_id, body, status, execution_strategy, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run.id,
                run.suite_id,
                run.model_dump_json(),
                run.status.value,
                run.execution_strategy.value,
                run.created_at.isoformat(),
                run.updated_at.isoformat(),
            ),
        )
    return run


def test_mutating_commands_generate_persist_and_replay_idempotency_receipts(
    tmp_path: Path,
) -> None:
    database = tmp_path / "commands.sqlite3"
    with TestClient(create_app(database)) as client:
        created = client.post("/v1/models", json=MODEL_PAYLOAD)
        assert created.status_code == 201
        key = created.headers["Idempotency-Key"]
        assert key.startswith("idem_")

        replayed = client.post(
            "/v1/models",
            headers={"Idempotency-Key": key},
            json=MODEL_PAYLOAD,
        )
        assert replayed.status_code == 201
        assert replayed.json() == created.json()
        assert replayed.headers["Idempotency-Key"] == key
        assert replayed.headers["Idempotency-Replayed"] == "true"

        conflicted = client.post(
            "/v1/models",
            headers={"Idempotency-Key": key},
            json={**MODEL_PAYLOAD, "name": "different-command"},
        )
        assert conflicted.status_code == 409
        assert conflicted.headers["Idempotency-Key"] == key
        assert conflicted.json()["error"]["code"] == "idempotency_key_conflict"

    rows = _command_rows(database)
    assert len(rows) == 1
    assert rows[0]["action_hash"] and len(rows[0]["action_hash"]) == 64
    assert rows[0]["status"] == "completed"
    assert rows[0]["response_json"] is not None


def test_command_scope_hashes_full_long_path_and_preserves_workflow_alias(
    tmp_path: Path,
) -> None:
    database = tmp_path / "command-paths.sqlite3"
    app = create_app(database)
    common_prefix = "/v1/" + ("segment" * 40)
    first_path = f"{common_prefix}-first"
    second_path = f"{common_prefix}-second"
    calls: list[str] = []

    @app.post(first_path)
    async def first_long_path() -> dict[str, str]:
        calls.append("first")
        return {"path": "first"}

    @app.post(second_path)
    async def second_long_path() -> dict[str, str]:
        calls.append("second")
        return {"path": "second"}

    with TestClient(app) as client:
        key = "same-key-long-path"
        first = client.post(first_path, headers={"Idempotency-Key": key}, json={})
        assert first.status_code == 200
        conflict = client.post(second_path, headers={"Idempotency-Key": key}, json={})
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "idempotency_key_conflict"
        assert calls == ["first"]

        alias_key = "workflow-alias"
        task_error = client.post(
            "/v1/tasks",
            headers={"Idempotency-Key": alias_key},
            json={},
        )
        assert task_error.status_code == 422
        alias_replay = client.post(
            "/v1/workflows/coding/runs",
            headers={"Idempotency-Key": alias_key},
            json={},
        )
        assert alias_replay.status_code == 422
        assert alias_replay.headers["Idempotency-Replayed"] == "true"

    rows = _command_rows(database)
    assert {row["command_type"] for row in rows} == {"rest-command.v2:POST"}
    assert len(rows) == 2
    assert all(len(row["action_hash"]) == 64 for row in rows)


def test_trailing_slash_redirect_does_not_consume_idempotency_receipt(tmp_path: Path) -> None:
    database = tmp_path / "command-trailing-slash.sqlite3"
    app = create_app(database)
    calls = 0

    @app.post("/v1/test-canonical-command")
    async def canonical_command() -> dict[str, bool]:
        nonlocal calls
        calls += 1
        return {"executed": True}

    headers = {"Idempotency-Key": "trailing-slash-key"}
    with TestClient(app) as client:
        redirected = client.post(
            "/v1/test-canonical-command/",
            headers=headers,
            json={},
            follow_redirects=False,
        )
        assert redirected.status_code == 307
        assert redirected.headers["location"].endswith("/v1/test-canonical-command")
        assert calls == 0
        assert _command_rows(database) == []

        followed = client.post(
            "/v1/test-canonical-command/",
            headers=headers,
            json={},
            follow_redirects=True,
        )
        assert followed.status_code == 200
        assert followed.json() == {"executed": True}
        assert calls == 1
        assert len(_command_rows(database)) == 1

        replay = client.post(
            "/v1/test-canonical-command",
            headers=headers,
            json={},
        )
        assert replay.status_code == 200
        assert replay.json() == followed.json()
        assert replay.headers["Idempotency-Replayed"] == "true"
        assert calls == 1


def test_error_envelopes_hide_invalid_inputs_and_unknown_exception_details(
    tmp_path: Path, monkeypatch
) -> None:
    secret = "unit-test-secret-value"
    database = tmp_path / "errors.sqlite3"
    with TestClient(create_app(database)) as client:
        invalid = client.post(
            "/v1/models",
            json={**MODEL_PAYLOAD, "api_key": secret},
        )
        assert invalid.status_code == 422
        assert invalid.json()["error"] == {
            "code": "request_validation_failed",
            "message": "request validation failed",
            "retryable": False,
            "recovery": "none",
        }
        assert secret not in invalid.text
        assert '"input":' not in invalid.text

    def explode(_self: ApplicationService):
        raise RuntimeError(f"API_KEY={secret}")

    monkeypatch.setattr(ApplicationService, "list_model_profiles", explode)
    with TestClient(
        create_app(tmp_path / "unknown.sqlite3"), raise_server_exceptions=False
    ) as client:
        failed = client.get("/v1/models")
        assert failed.status_code == 500
        assert failed.json()["error"]["code"] == "internal_error"
        assert failed.json()["detail"] == "internal service error"
        assert secret not in failed.text

    http_app = create_app(tmp_path / "http-detail.sqlite3")

    @http_app.get("/v1/test-http-detail")
    async def secret_http_detail() -> None:
        raise HTTPException(
            status_code=400,
            detail=(f"DATABASE_PASSWORD={secret}; https://user:{secret}@example.invalid/path"),
        )

    with TestClient(http_app) as client:
        rejected = client.get("/v1/test-http-detail")
        assert rejected.status_code == 400
        assert secret not in rejected.text
        assert isinstance(rejected.json()["detail"], str)
        assert "[REDACTED]" in rejected.text


def test_command_receipt_and_replay_redact_success_and_normalize_failure(
    tmp_path: Path,
) -> None:
    database = tmp_path / "command-redaction.sqlite3"
    app = create_app(database)
    secret = "receipt-secret-value"

    @app.post("/v1/test-secret-response")
    async def secret_response() -> dict[str, str]:
        return {
            "DATABASE_PASSWORD": secret,
            "authorization": f"Bearer {secret}",
            "url": f"https://user:{secret}@example.invalid/path",
            "secret_ref": "OPERANT_DATABASE_PASSWORD",
        }

    @app.post("/v1/test-plain-failure")
    async def plain_failure() -> JSONResponse:
        return JSONResponse({"client_secret": secret}, status_code=418)

    @app.post("/v1/test-secret-text")
    async def secret_text() -> Response:
        return Response(
            "\n".join(
                (
                    f"DATABASE_PASSWORD={secret}",
                    "Bearer abc123",
                    "-----BEGIN ENCRYPTED PRIVATE KEY-----",
                    secret,
                    "-----END ENCRYPTED PRIVATE KEY-----",
                )
            ),
            media_type="text/plain",
        )

    @app.post("/v1/test-empty-response")
    async def empty_response() -> Response:
        return Response(status_code=200)

    @app.post("/v1/test-other-non-json")
    async def other_non_json() -> Response:
        return Response(
            f"<result>Bearer z; DATABASE_PASSWORD={secret}</result>",
            status_code=207,
            media_type="application/xml",
        )

    with TestClient(app) as client:
        live = client.post(
            "/v1/test-secret-response",
            headers={"Idempotency-Key": "secret-success"},
            json={},
        )
        assert secret not in live.text
        assert live.json()["DATABASE_PASSWORD"] == "[REDACTED]"
        assert live.json()["authorization"] == "[REDACTED]"
        assert live.json()["secret_ref"] == "OPERANT_DATABASE_PASSWORD"
        replay = client.post(
            "/v1/test-secret-response",
            headers={"Idempotency-Key": "secret-success"},
            json={},
        )
        assert replay.status_code == 200
        assert replay.headers["Idempotency-Replayed"] == "true"
        assert replay.json() == live.json()
        assert secret not in replay.text
        assert replay.json()["DATABASE_PASSWORD"] == "[REDACTED]"
        assert replay.json()["secret_ref"] == "OPERANT_DATABASE_PASSWORD"

        failed = client.post(
            "/v1/test-plain-failure",
            headers={"Idempotency-Key": "plain-failure"},
            json={},
        )
        assert failed.status_code == 418
        assert secret not in failed.text
        assert failed.json()["detail"] == "command request failed"
        failed_replay = client.post(
            "/v1/test-plain-failure",
            headers={"Idempotency-Key": "plain-failure"},
            json={},
        )
        assert failed_replay.status_code == 418
        assert failed_replay.headers["Idempotency-Replayed"] == "true"
        assert failed_replay.json() == failed.json()
        assert failed_replay.json()["detail"] == "command request failed"
        assert failed_replay.json()["error"] == {
            "code": "http_418",
            "message": "command request failed",
            "retryable": False,
            "recovery": "none",
        }
        assert secret not in failed_replay.text

        text_live = client.post(
            "/v1/test-secret-text",
            headers={"Idempotency-Key": "secret-text"},
            json={},
        )
        assert text_live.status_code == 200
        assert secret not in text_live.text
        assert "abc123" not in text_live.text
        assert "[REDACTED PRIVATE KEY]" in text_live.text
        text_replay = client.post(
            "/v1/test-secret-text",
            headers={"Idempotency-Key": "secret-text"},
            json={},
        )
        assert text_replay.json() == text_live.json()
        assert text_replay.headers["Idempotency-Replayed"] == "true"

        empty_live = client.post(
            "/v1/test-empty-response",
            headers={"Idempotency-Key": "empty-response"},
            json={},
        )
        assert empty_live.status_code == 200
        assert empty_live.json() is None
        empty_replay = client.post(
            "/v1/test-empty-response",
            headers={"Idempotency-Key": "empty-response"},
            json={},
        )
        assert empty_replay.status_code == 200
        assert empty_replay.json() is None
        assert empty_replay.headers["Idempotency-Replayed"] == "true"

        other_live = client.post(
            "/v1/test-other-non-json",
            headers={"Idempotency-Key": "other-non-json"},
            json={},
        )
        assert other_live.status_code == 207
        assert other_live.headers["Idempotency-Key"] == "other-non-json"
        assert secret not in other_live.text
        assert "Bearer z" not in other_live.text
        other_replay = client.post(
            "/v1/test-other-non-json",
            headers={"Idempotency-Key": "other-non-json"},
            json={},
        )
        assert other_replay.status_code == 207
        assert other_replay.headers["Idempotency-Replayed"] == "true"
        assert other_replay.json() == other_live.json()

    rows = {row["idempotency_key"]: row for row in _command_rows(database)}
    assert secret not in rows["secret-success"]["response_json"]
    assert secret not in rows["plain-failure"]["response_json"]
    assert secret not in rows["secret-text"]["response_json"]
    assert "abc123" not in rows["secret-text"]["response_json"]
    assert secret not in rows["other-non-json"]["response_json"]
    assert "Bearer z" not in rows["other-non-json"]["response_json"]


def test_last_event_id_is_route_scoped_and_new_workflow_never_starts(tmp_path: Path) -> None:
    database = tmp_path / "last-event-id.sqlite3"
    with TestClient(create_app(database)) as client:
        assert client.get("/healthz", headers={"Last-Event-ID": "not-an-int"}).status_code == 200
        rejected = client.post(
            "/v1/tasks",
            headers={"Last-Event-ID": "not-an-int"},
            json={"task": "must not start", "workspace": str(tmp_path)},
        )
        assert rejected.status_code == 400
        assert rejected.json()["error"]["code"] == "invalid_event_cursor"
        assert rejected.headers["Idempotency-Key"].startswith("idem_")
        assert client.get("/v1/tasks").json() == []
    assert _command_rows(database)[0]["status"] == "failed"


def test_event_cursors_reject_values_above_sqlite_integer_range(tmp_path: Path) -> None:
    huge = str(2**63)
    with TestClient(create_app(tmp_path / "cursor-bounds.sqlite3")) as client:
        header_responses = (
            client.post(
                "/v1/sessions/session_missing/runs",
                headers={"Last-Event-ID": huge},
                json={"message": "must not query", "workspace": str(tmp_path)},
            ),
            client.post(
                "/v1/tasks/workflow_missing/resume",
                headers={"Last-Event-ID": huge},
                json={"allow_coder_replay": False},
            ),
            client.get(
                "/v1/evaluations/runs/evaluation_missing/events/stream",
                headers={"Last-Event-ID": huge},
            ),
        )
        query_responses = tuple(
            client.get(path, params={"after_cursor": huge})
            for path in (
                "/v1/sessions/session_missing/events",
                "/v1/tasks/workflow_missing/events",
                "/v1/evaluations/runs/evaluation_missing/events",
                "/v1/evaluations/runs/evaluation_missing/events/stream",
            )
        )

    for response in (*header_responses, *query_responses):
        assert response.status_code == 400
        payload = response.json()
        error = payload["error"]
        assert payload["detail"] == error["message"]
        assert error["code"] == "invalid_event_cursor"
        assert "between 0" in error["message"]
        assert error["retryable"] is False
        assert error["recovery"] == "refresh_and_retry"


def test_session_and_evaluation_sse_ids_match_persisted_open_interval_cursor(
    tmp_path: Path,
) -> None:
    database = tmp_path / "cursors.sqlite3"
    store = SQLiteStore(database)
    store.initialize()
    profile = store.add_model_profile(
        ModelProfile(
            name="cursor-model",
            model_id="cursor-model",
            base_url="https://example.invalid/v1",
            secret_ref="OPERANT_TEST_KEY",
        )
    )
    role = store.create_role(
        RolePreset(
            name="Cursor Role",
            system_prompt="Replay committed events only.",
            model_profile_id=profile.id,
        )
    )
    session = store.create_session(role.id)
    session_event = store.append_event(
        Event(
            session_id=session.id,
            event_type="agent.completed",
            payload={"turn": 1, "content": "done"},
        )
    )
    run = _insert_evaluation_run(store)
    first = store.append_evaluation_event(
        EvaluationRunEvent(evaluation_run_id=run.id, event_type="evaluation.run_started")
    )
    second = store.append_evaluation_event(
        EvaluationRunEvent(evaluation_run_id=run.id, event_type="evaluation.run_finished")
    )
    workflow = store.create_workflow_run(
        WorkflowRun(
            id="workflow_cursor",
            task="Replay persisted workflow events",
            workspace=str(tmp_path),
            planner_role_id="planner",
            coder_role_id="coder",
            reviewer_role_id="reviewer",
            status=WorkflowRunStatus.INTERRUPTED,
        )
    )
    workflow_event = store.append_workflow_event(
        WorkflowRunEvent(
            workflow_run_id=workflow.id,
            role="workflow",
            event_type="workflow.started",
        )
    )
    assert (
        session_event.cursor is not None and first.cursor is not None and second.cursor is not None
    )

    with TestClient(create_app(database)) as client:
        session_replay = client.post(
            f"/v1/sessions/{session.id}/runs",
            headers={"Last-Event-ID": "0", "Idempotency-Key": "must-be-ignored"},
            json={"message": "do not execute", "workspace": str(tmp_path)},
        )
        assert f"id: {session_event.cursor}" in session_replay.text
        assert f'"cursor": {session_event.cursor}' in session_replay.text

        queried = client.get(
            f"/v1/evaluations/runs/{run.id}/events",
            params={"after_cursor": first.cursor, "limit": 1},
        )
        assert [event["id"] for event in queried.json()] == [second.id]
        replay = client.get(
            f"/v1/evaluations/runs/{run.id}/events/stream",
            headers={"Last-Event-ID": str(first.cursor)},
        )
        assert f"id: {second.cursor}" in replay.text
        assert f'"cursor": {second.cursor}' in replay.text
        assert first.id not in replay.text

        workflow_replay = client.post(
            f"/v1/tasks/{workflow.id}/resume",
            headers={"Last-Event-ID": "0", "Idempotency-Key": "also-ignored"},
            json={"allow_coder_replay": False},
        )
        assert f"id: {workflow_event.cursor}" in workflow_replay.text
        assert f'"cursor": {workflow_event.cursor}' in workflow_replay.text
        assert [item["id"] for item in client.get("/v1/tasks").json()] == [workflow.id]

    assert _command_rows(database) == []


def test_real_session_run_first_frame_creates_replayable_202_receipt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database = tmp_path / "real-session-stream.sqlite3"
    store = SQLiteStore(database)
    store.initialize()
    profile = store.add_model_profile(
        ModelProfile(
            name="real-stream",
            model_id="real-stream",
            base_url="https://example.invalid/v1",
            secret_ref="OPERANT_TEST_KEY",
        )
    )
    role = store.create_role(
        RolePreset(
            name="Real Stream",
            system_prompt="Return immediately.",
            model_profile_id=profile.id,
        )
    )
    session = store.create_session(role.id)

    sse_secret = "sse-private-value"

    async def immediate_stream(self, **_kwargs):
        del self
        yield ProviderEvent(
            event_type="model.completed",
            response=ModelResponse(
                content=(
                    "Bearer abc123\n"
                    "-----BEGIN DSA PRIVATE KEY-----\n"
                    f"{sse_secret}\n"
                    "-----END DSA PRIVATE KEY-----"
                ),
                finish_reason="stop",
            ),
        )

    monkeypatch.setattr(OpenAICompatibleProvider, "stream", immediate_stream)
    with TestClient(create_app(database)) as client:
        first = client.post(
            f"/v1/sessions/{session.id}/runs",
            headers={"Idempotency-Key": "real-session-run"},
            json={"message": "run", "workspace": str(tmp_path)},
        )
        assert first.status_code == 200
        assert "event: agent.started" in first.text
        assert "abc123" not in first.text
        assert sse_secret not in first.text
        assert "[REDACTED PRIVATE KEY]" in first.text
        replay = client.post(
            f"/v1/sessions/{session.id}/runs",
            headers={"Idempotency-Key": "real-session-run"},
            json={"message": "run", "workspace": str(tmp_path)},
        )
        assert replay.status_code == 202
        assert replay.headers["Idempotency-Replayed"] == "true"
        assert replay.headers["content-type"].startswith("application/json")
        payload = replay.json()
        assert payload["command_kind"] == "stream"
        assert payload["accepted"] is True
        assert payload["stream_replay_available"] is True
        assert payload["resource"] == {"type": "session", "id": session.id}
    assert "abc123" not in json.dumps(
        [event.model_dump(mode="json") for event in store.list_events(session.id)]
    )
    assert sse_secret not in json.dumps(
        [event.model_dump(mode="json") for event in store.list_events(session.id)]
    )


def test_stream_before_first_frame_and_handler_crash_require_manual_reconcile(
    tmp_path: Path,
) -> None:
    database = tmp_path / "stream-terminal.sqlite3"
    app: FastAPI = create_app(database)

    @app.post("/v1/test-stream-failure")
    async def failing_stream() -> StreamingResponse:
        async def body() -> AsyncIterator[str]:
            if False:
                yield "unused"
            raise RuntimeError("private stream failure")

        return StreamingResponse(body(), media_type="text/event-stream")

    @app.post("/v1/test-stream-cancel")
    async def cancelled_stream() -> StreamingResponse:
        async def body() -> AsyncIterator[str]:
            if False:
                yield "unused"
            raise asyncio.CancelledError

        return StreamingResponse(body(), media_type="text/event-stream")

    handler_calls = 0

    @app.post("/v1/test-handler-failure")
    async def failing_handler() -> None:
        nonlocal handler_calls
        handler_calls += 1
        raise RuntimeError("private handler failure")

    with TestClient(app, raise_server_exceptions=False) as client:
        client.post("/v1/test-stream-failure", json={})
        client.post("/v1/test-stream-cancel", json={})
        failed = client.post(
            "/v1/test-handler-failure",
            headers={"Idempotency-Key": "handler-crash"},
            json={},
        )
        assert failed.status_code == 500
        assert "private handler failure" not in failed.text
        retried = client.post(
            "/v1/test-handler-failure",
            headers={"Idempotency-Key": "handler-crash"},
            json={},
        )
        assert retried.status_code == 409
        assert retried.json()["error"]["recovery"] == "manual_reconcile"
        assert handler_calls == 1

    rows = _command_rows(database)
    assert {row["command_type"] for row in rows} == {"rest-command.v2:POST"}
    assert {row["status"] for row in rows} == {"manual_reconcile_required"}


def test_stream_acceptance_requires_committed_resource_cursor_and_bounds_first_frame(
    tmp_path: Path,
) -> None:
    database = tmp_path / "stream-acceptance.sqlite3"
    store = SQLiteStore(database)
    store.initialize()
    profile = store.add_model_profile(
        ModelProfile(
            name="stream-acceptance",
            model_id="stream-acceptance",
            base_url="https://example.invalid/v1",
            secret_ref="OPERANT_TEST_KEY",
        )
    )
    role = store.create_role(
        RolePreset(
            name="Stream Acceptance",
            system_prompt="Test stream receipts.",
            model_profile_id=profile.id,
        )
    )
    session = store.create_session(role.id)
    persisted = store.append_event(
        Event(session_id=session.id, event_type="agent.started", payload={"turn": 0})
    )
    assert persisted.cursor is not None
    app = create_app(database)

    @app.post("/v1/test-stream-committed")
    async def committed_stream() -> StreamingResponse:
        first_frame = (
            f"id: {persisted.cursor}\n"
            "event: agent.started\n"
            + "data: "
            + __import__("json").dumps({"session_id": session.id, "cursor": persisted.cursor})
            + "\n\n"
        )

        async def body() -> AsyncIterator[str]:
            split = len(first_frame) // 2
            yield first_frame[:split]
            yield first_frame[split:] + ("x" * (MAX_FIRST_SSE_FRAME_BYTES + 1))

        return StreamingResponse(body(), media_type="text/event-stream")

    @app.post("/v1/test-stream-unverified")
    async def unverified_stream() -> StreamingResponse:
        async def body() -> AsyncIterator[str]:
            yield (
                f"id: {persisted.cursor + 1}\n"
                "event: agent.started\n"
                + "data: "
                + __import__("json").dumps(
                    {"session_id": session.id, "cursor": persisted.cursor + 1}
                )
                + "\n\n"
            )

        return StreamingResponse(body(), media_type="text/event-stream")

    @app.post("/v1/test-stream-missing-id")
    async def missing_id_stream() -> StreamingResponse:
        async def body() -> AsyncIterator[str]:
            yield (
                "event: agent.started\n"
                + "data: "
                + __import__("json").dumps({"session_id": session.id, "cursor": persisted.cursor})
                + "\n\n"
            )

        return StreamingResponse(body(), media_type="text/event-stream")

    @app.post("/v1/test-stream-invalid-id")
    async def invalid_id_stream() -> StreamingResponse:
        async def body() -> AsyncIterator[str]:
            yield (
                "id: not-a-cursor\n"
                "event: agent.started\n"
                + "data: "
                + __import__("json").dumps({"session_id": session.id, "cursor": persisted.cursor})
                + "\n\n"
            )

        return StreamingResponse(body(), media_type="text/event-stream")

    @app.post("/v1/test-stream-oversized")
    async def oversized_stream() -> StreamingResponse:
        async def body() -> AsyncIterator[str]:
            yield "x" * (MAX_FIRST_SSE_FRAME_BYTES + 1)

        return StreamingResponse(body(), media_type="text/event-stream")

    with TestClient(app) as client:
        accepted = client.post(
            "/v1/test-stream-committed",
            headers={"Idempotency-Key": "committed"},
            json={},
        )
        assert accepted.status_code == 200
        replay = client.post(
            "/v1/test-stream-committed",
            headers={"Idempotency-Key": "committed"},
            json={},
        )
        assert replay.status_code == 202
        assert replay.headers["content-type"].startswith("application/json")
        assert replay.headers["Idempotency-Replayed"] == "true"
        assert replay.json()["stream_replay_available"] is True
        assert replay.json()["resource"] == {"type": "session", "id": session.id}
        assert replay.json()["replay_url"].endswith(f"/{session.id}/events")

        unverified = client.post(
            "/v1/test-stream-unverified",
            headers={"Idempotency-Key": "unverified"},
            json={},
        )
        assert unverified.status_code == 200
        unverified_replay = client.post(
            "/v1/test-stream-unverified",
            headers={"Idempotency-Key": "unverified"},
            json={},
        )
        assert unverified_replay.status_code == 409
        assert unverified_replay.json()["error"]["recovery"] == "manual_reconcile"

        for path, key in (
            ("/v1/test-stream-missing-id", "missing-id"),
            ("/v1/test-stream-invalid-id", "invalid-id"),
        ):
            first = client.post(path, headers={"Idempotency-Key": key}, json={})
            assert first.status_code == 200
            retry = client.post(path, headers={"Idempotency-Key": key}, json={})
            assert retry.status_code == 409
            assert retry.json()["error"]["recovery"] == "manual_reconcile"

        oversized = client.post(
            "/v1/test-stream-oversized",
            headers={"Idempotency-Key": "oversized"},
            json={},
        )
        assert "command.stream_error" in oversized.text
        oversized_replay = client.post(
            "/v1/test-stream-oversized",
            headers={"Idempotency-Key": "oversized"},
            json={},
        )
        assert oversized_replay.status_code == 409

    statuses = {row["idempotency_key"]: row["status"] for row in _command_rows(database)}
    assert statuses == {
        "committed": "completed",
        "unverified": "manual_reconcile_required",
        "missing-id": "manual_reconcile_required",
        "invalid-id": "manual_reconcile_required",
        "oversized": "manual_reconcile_required",
    }


def test_api_session_run_admission_returns_json_conflict_and_releases_unentered_stream(
    tmp_path: Path,
) -> None:
    database = tmp_path / "api-single-flight.sqlite3"
    store = SQLiteStore(database)
    store.initialize()
    profile = store.add_model_profile(
        ModelProfile(
            name="api-single-flight",
            model_id="api-single-flight",
            base_url="https://example.invalid/v1",
            secret_ref="OPERANT_TEST_KEY",
        )
    )
    role = store.create_role(
        RolePreset(
            name="API Single Flight",
            system_prompt="Test atomic API admission.",
            model_profile_id=profile.id,
        )
    )
    session = store.create_session(role.id)
    app = create_app(database)
    endpoint = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == "/v1/sessions/{session_id}/runs"
    )

    def raw_request() -> Request:
        return Request(
            {
                "type": "http",
                "method": "POST",
                "path": f"/v1/sessions/{session.id}/runs",
                "query_string": b"",
                "headers": [],
                "scheme": "http",
                "server": ("testserver", 80),
                "client": ("testclient", 1),
            }
        )

    request = RunSessionRequest(message="run once", workspace=str(tmp_path))

    async def exercise() -> None:
        first = await endpoint(session.id, request, raw_request())
        assert isinstance(first, StreamingResponse)
        second = await endpoint(session.id, request, raw_request())
        assert isinstance(second, JSONResponse)
        assert second.status_code == 409
        payload = json.loads(second.body)
        assert payload["detail"] == "session already has an active run"
        assert payload["error"] == {
            "code": "session_run_conflict",
            "message": "session already has an active run",
            "retryable": True,
            "recovery": "retry_later",
        }
        with sqlite3.connect(database) as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM agents WHERE session_id = ?",
                    (session.id,),
                ).fetchone()[0]
                == 0
            )

        assert first.background is not None
        await first.background()
        entered = await endpoint(session.id, request, raw_request())
        assert isinstance(entered, StreamingResponse)
        first_chunk = await anext(entered.body_iterator)
        assert "agent.started" in str(first_chunk)
        await entered.body_iterator.aclose()

        after_close = await endpoint(session.id, request, raw_request())
        assert isinstance(after_close, StreamingResponse)
        assert after_close.background is not None
        await after_close.background()

    asyncio.run(exercise())
