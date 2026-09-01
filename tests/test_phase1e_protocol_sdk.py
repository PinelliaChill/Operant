"""Contract and offline generation tests for the Phase 1E SDK line."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

# The project wheel intentionally packages only ``src/operant``; protocol
# generation tests exercise the repository-local SDK directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sdk.protocol.generate_phase1e import (
    DIGEST_PATH,
    PY_PATH,
    SCHEMA_PATH,
    TS_PATH,
    _canonical_json,
    generate,
)
from sdk.python_client.phase1e_generated import (
    PHASE1E_MAX_CURSOR,
    PHASE1E_PROTOCOL_VERSION,
    PHASE1E_SCHEMA_DIGEST,
    Phase1EClient,
    ProtocolNegotiationError,
)
from sdk.python_client.transport import (
    Phase1EError,
    TransportRequest,
    TransportResponse,
    parse_sse,
)


def _response(
    payload: Any,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    body: str | bytes | None = None,
) -> TransportResponse:
    return TransportResponse(
        status=status,
        headers=headers or {},
        body=body,
        json=payload if body is None else None,
    )


class QueueTransport:
    def __init__(self, *responses: TransportResponse) -> None:
        self.responses = list(responses)
        self.requests: list[TransportRequest] = []

    def __call__(self, request: TransportRequest) -> TransportResponse:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("unexpected transport request")
        return self.responses.pop(0)


def _metadata() -> dict[str, Any]:
    return {
        "protocol_version": PHASE1E_PROTOCOL_VERSION,
        "schema_digest": PHASE1E_SCHEMA_DIGEST,
        "min_client_version": PHASE1E_PROTOCOL_VERSION,
        "capabilities": ["session_command", "sse_replay"],
    }


def test_schema_has_only_the_frozen_narrow_operation_surface() -> None:
    document = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    operation_ids = {
        operation["operationId"]
        for path in document["paths"].values()
        for operation in path.values()
        if "operationId" in operation
    }
    assert operation_ids == {
        "negotiateProtocol",
        "listProjects",
        "listWorkspaceFiles",
        "listThreads",
        "createSession",
        "runSessionStream",
        "listPendingApprovals",
        "submitApproval",
    }
    formal_text = _canonical_json(document).decode("utf-8").lower()
    for excluded in ("graph", "remote", "team", "scheduler", "oauth", "tui", "tauri"):
        assert excluded not in formal_text


def test_schema_digest_and_generated_outputs_are_reproducible() -> None:
    document = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    expected = hashlib.sha256(_canonical_json(document)).hexdigest()
    assert expected == PHASE1E_SCHEMA_DIGEST
    assert DIGEST_PATH.read_text(encoding="utf-8") == f"{expected}  {SCHEMA_PATH.name}\n"
    before = {path: path.read_bytes() for path in (DIGEST_PATH, TS_PATH, PY_PATH)}
    assert generate() == expected
    assert before == {path: path.read_bytes() for path in before}
    result = subprocess.run(
        [sys.executable, str(Path("sdk/protocol/generate_phase1e.py"))],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == expected
    assert before == {path: path.read_bytes() for path in before}


def test_python_client_negotiates_before_queries_and_encodes_cursor_scope() -> None:
    transport = QueueTransport(_response(_metadata()), _response([{"project_id": "w1"}]))
    client = Phase1EClient("http://core.test/", transport=transport)

    projects = client.list_projects(after_cursor=7, limit=2)

    assert projects == [{"project_id": "w1"}]
    assert len(transport.requests) == 2
    assert transport.requests[0].url == "http://core.test/v1/protocol"
    assert transport.requests[0].headers["X-Operant-Client-Version"] == PHASE1E_PROTOCOL_VERSION
    project_request = transport.requests[1]
    assert parse_qs(urlsplit(project_request.url).query) == {"after_cursor": ["7"], "limit": ["2"]}


def test_mutations_send_reusable_idempotency_key_and_read_receipt_headers() -> None:
    session = {"id": "s1", "role_snapshot": {}, "created_at": "2026-09-01T00:00:00Z"}
    transport = QueueTransport(
        _response(_metadata()),
        _response(session, status=201, headers={"Idempotency-Key": "idem-1"}),
        _response(
            {"command_kind": "stream", "accepted": True, "command_id": "cmd-1"},
            status=202,
            headers={"Idempotency-Key": "idem-1", "Idempotency-Replayed": "true"},
        ),
    )
    client = Phase1EClient("http://core.test", transport=transport)

    client.create_session({"role_id": "role-1"}, idempotency_key="idem-1")
    replay = client.run_session_stream(
        "s1",
        {"message": "retry", "workspace": "/tmp/ws"},
        idempotency_key="idem-1",
    )

    assert replay.receipt == {"command_kind": "stream", "accepted": True, "command_id": "cmd-1"}
    assert replay.metadata.idempotency_key == "idem-1"
    assert replay.metadata.idempotency_replayed is True
    assert transport.requests[1].headers["Idempotency-Key"] == "idem-1"
    assert transport.requests[2].headers["Idempotency-Key"] == "idem-1"


def test_stream_reconnect_sends_last_event_id_and_scoped_lossless_cursor() -> None:
    stream_body = (
        b": heartbeat\n"
        b"id: 9223372036854775807\n"
        b"event: agent.completed\n"
        b'data: {"cursor": 9223372036854775807,"payload": {"ok": true}}\n\n'
    )
    transport = QueueTransport(_response(_metadata()), _response(None, body=stream_body))
    client = Phase1EClient("http://core.test", transport=transport)

    stream = client.run_session_stream(
        "s1",
        {"message": "resume", "workspace": "/tmp/ws"},
        idempotency_key="idem-resume",
        last_event_id=11,
    )
    events = list(stream.events)

    assert events[0]["id"] == PHASE1E_MAX_CURSOR
    assert events[0]["resource_scope"] == "session:s1"
    assert events[0]["stream_kind"] == "session.run"
    assert transport.requests[1].headers["Last-Event-ID"] == "11"


def test_protocol_mismatch_and_error_envelope_fail_closed() -> None:
    mismatch = _metadata()
    mismatch["schema_digest"] = "0" * 64
    with pytest.raises(ProtocolNegotiationError):
        Phase1EClient(transport=QueueTransport(_response(mismatch))).negotiate_protocol()

    error = TransportResponse(
        status=409,
        headers={},
        json={
            "detail": "legacy detail",
            "error": {
                "code": "command_outcome_unknown",
                "message": "manual reconciliation required",
                "retryable": False,
                "recovery": "manual_reconcile",
            },
        },
    )
    raised = Phase1EError.from_response(error)
    assert raised.code == "command_outcome_unknown"
    assert raised.recovery == "manual_reconcile"
    assert raised.retryable is False


def test_sse_parser_handles_multiline_json_and_ignores_comments() -> None:
    frames = list(
        parse_sse('id: 4\nevent: thread.item.appended\ndata: {"a":\ndata: 1}\n\n: ignored\n\n')
    )
    assert frames == [{"id": "4", "event": "thread.item.appended", "data": {"a": 1}}]
