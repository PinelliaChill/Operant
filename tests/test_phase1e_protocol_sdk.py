"""Contract and offline generation tests for the Phase 1E SDK line."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, get_type_hints
from urllib.parse import parse_qs, urlsplit

import pytest

# The project wheel intentionally packages only ``src/operant``; protocol
# generation tests exercise the repository-local SDK directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sdk.python_client.phase1e_generated as generated
from sdk.protocol.generate_phase1e import (
    DIGEST_PATH,
    PY_PATH,
    SCHEMA_PATH,
    TS_PATH,
    _canonical_json,
    _operation_specs,
    _render_ts_client,
    generate,
)
from sdk.python_client.phase1e_generated import (
    PHASE1E_MAX_CURSOR,
    PHASE1E_PROTOCOL_VERSION,
    PHASE1E_SCHEMA_DIGEST,
    Phase1EClient,
    ProtocolNegotiationError,
    ScopedCursorTracker,
    _cursor_value_int,
)
from sdk.python_client.transport import (
    Phase1EError,
    TransportRequest,
    TransportResponse,
    parse_sse,
    response_json,
)


def _response(
    payload: Any,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    body: str | bytes | None = None,
) -> TransportResponse:
    raw_json = None if body is not None or payload is None else json.dumps(payload)
    return TransportResponse(
        status=status,
        headers=headers or {},
        body=body,
        json=raw_json,
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
            headers={
                "Idempotency-Key": "idem-1",
                "Idempotency-Replayed": "true",
                "Content-Type": "application/json",
            },
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
    transport = QueueTransport(
        _response(_metadata()),
        _response(None, body=stream_body, headers={"Content-Type": "text/event-stream"}),
    )
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
        body=json.dumps(
            {
                "detail": "legacy detail",
                "error": {
                    "code": "command_outcome_unknown",
                    "message": "manual reconciliation required",
                    "retryable": False,
                    "recovery": "manual_reconcile",
                },
            }
        ),
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


def test_client_rendering_is_driven_by_path_operation_metadata() -> None:
    document = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    baseline = _render_ts_client(document, "digest")

    changed_path = deepcopy(document)
    changed_path["components"]["parameters"]["AfterCursor"]["name"] = "cursor"
    changed_path_render = _render_ts_client(changed_path, "digest")
    assert changed_path_render != baseline
    assert '"name": "cursor"' in changed_path_render
    assert "options.cursor" in changed_path_render

    changed_method = deepcopy(document)
    changed_method["paths"]["/v1/projects"]["post"] = changed_method["paths"]["/v1/projects"].pop(
        "get"
    )
    changed_method["paths"]["/v1/projects"]["post"]["operationId"] = "listProjects"
    changed_method_render = _render_ts_client(changed_method, "digest")
    assert '"method": "POST"' in changed_method_render

    changed_response = deepcopy(document)
    changed_response["paths"]["/v1/projects"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["items"]["$ref"] = "#/components/schemas/ThreadProjection"
    changed_response_render = _render_ts_client(changed_response, "digest")
    assert "Array<ThreadProjection>" in changed_response_render
    assert "del document" not in Path("sdk/protocol/generate_phase1e.py").read_text(
        encoding="utf-8"
    )


def test_int64_and_required_types_are_explicit_and_strict() -> None:
    assert _cursor_value_int(0) == 0
    assert _cursor_value_int(PHASE1E_MAX_CURSOR) == PHASE1E_MAX_CURSOR
    with pytest.raises(ValueError):
        _cursor_value_int(True)
    with pytest.raises(ValueError):
        _cursor_value_int(PHASE1E_MAX_CURSOR + 1)

    from sdk.python_client.phase1e_generated import ProtocolNegotiation, WorkspaceFileEntry

    assert ProtocolNegotiation.__required_keys__ == {
        "protocol_version",
        "schema_digest",
        "min_client_version",
        "capabilities",
    }
    assert str(get_type_hints(WorkspaceFileEntry)["size_bytes"]) == "int | None"


def test_all_generated_typed_dict_required_sets_match_schema() -> None:
    document = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    for name, schema in document["components"]["schemas"].items():
        if schema.get("type") != "object" and "properties" not in schema:
            continue
        model = getattr(generated, name)
        properties = set(schema.get("properties", {}))
        required = set(schema.get("required", []))
        assert model.__required_keys__ == frozenset(required)
        assert model.__optional_keys__ == frozenset(properties - required)
        assert set(get_type_hints(model)) == properties


def test_generated_python_sdk_uses_python310_typed_dict_compatibility_import() -> None:
    source = PY_PATH.read_text(encoding="utf-8")
    assert "from typing import Any, Literal, NotRequired, TypedDict, cast" not in source
    assert "from typing_extensions import NotRequired, TypedDict" in source
    assert "try:\n    from typing import NotRequired" not in source

    python310 = shutil.which("python3.10")
    if python310 is None:
        return  # Static source check is the available Python 3.10 compatibility gate.
    probe = subprocess.run([python310, "--version"], capture_output=True, text=True)
    if probe.returncode != 0:
        return  # A pyenv shim without an activated 3.10 runtime is not executable.
    smoke = subprocess.run(
        [
            python310,
            "-c",
            (
                "import sdk.python_client.phase1e_generated as m; "
                "assert m.CreateSessionRequest.__optional_keys__"
            ),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        capture_output=True,
        text=True,
    )
    assert smoke.returncode == 0, smoke.stderr


def test_sse_line_endings_and_cursor_tracker_are_bounded() -> None:
    chunks = ['id: 1\rdata: {"ok":', "true}\r\r", "id: 2\n", "data: 2\n\n"]
    assert list(parse_sse(iter(chunks))) == [
        {"id": "1", "data": {"ok": True}},
        {"id": "2", "data": 2},
    ]

    tracker = ScopedCursorTracker(max_seen_per_scope=2, max_scopes=1)

    def frame(cursor: int, scope: str = "s1") -> dict[str, object]:
        return {"id": cursor, "resource_scope": scope, "stream_kind": "run"}

    assert tracker.accept(frame(1))
    assert tracker.accept(frame(2))
    assert tracker.accept(frame(3))
    assert not tracker.accept(frame(2))
    assert tracker.accept(frame(1))  # cursor 1 was evicted from the bounded window
    assert tracker.accept(frame(4, "s2"))
    assert tracker.last("s1", "run") is None


def test_create_session_xor_is_checked_before_transport() -> None:
    transport = QueueTransport()
    client = Phase1EClient(transport=transport)
    with pytest.raises(ValueError, match="exactly one"):
        client.create_session({})
    with pytest.raises(ValueError, match="exactly one"):
        client.create_session({"role_id": "r1", "new_role": {"name": "new"}})
    with pytest.raises(ValueError, match="exactly one"):
        client.create_session({"role_id": "r1", "new_role": {}})
    assert transport.requests == []


def test_create_session_xor_is_schema_metadata_driven_and_fails_closed() -> None:
    document = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert _operation_specs(document)["createSession"].mutually_exclusive == (
        ("role_id", "new_role"),
    )

    changed = deepcopy(document)
    changed["components"]["schemas"]["CreateSessionRequest"]["x-mutually-exclusive"] = [
        "role_id",
        "effort",
    ]
    changed_operations = _operation_specs(changed)
    assert changed_operations["createSession"].mutually_exclusive == (("role_id", "effort"),)
    assert _render_ts_client(changed, "digest") != _render_ts_client(document, "digest")

    standard_one_of = deepcopy(document)
    request_schema = standard_one_of["components"]["schemas"]["CreateSessionRequest"]
    request_schema.pop("x-mutually-exclusive")
    request_schema["oneOf"] = [
        {"type": "object", "required": ["role_id"]},
        {"type": "object", "required": ["new_role"]},
    ]
    assert _operation_specs(standard_one_of)["createSession"].mutually_exclusive == (
        ("role_id", "new_role"),
    )

    invalid = deepcopy(document)
    invalid["components"]["schemas"]["CreateSessionRequest"]["x-mutually-exclusive"] = [
        "role_id",
        "not_a_property",
    ]
    with pytest.raises(ValueError, match="mutually-exclusive"):
        _operation_specs(invalid)


def test_negotiation_requires_exact_metadata_shape_and_raw_transport_json() -> None:
    invalid_values = [
        None,
        {"protocol_version": PHASE1E_PROTOCOL_VERSION},
        {**_metadata(), "capabilities": [1]},
        {**_metadata(), "capabilities": [""]},
    ]
    for value in invalid_values:
        with pytest.raises(ProtocolNegotiationError) as raised:
            Phase1EClient(transport=QueueTransport(_response(value))).negotiate_protocol()
        assert raised.value.code == "protocol_incompatible"
        assert raised.value.recovery == "refresh_and_retry"

    with pytest.raises(TypeError, match="raw text"):
        response_json(TransportResponse(status=200, headers={}, json={"cursor": 2**63 - 1}))  # type: ignore[arg-type]


def test_python_transport_decodes_split_utf8_and_stream_content_types_strictly() -> None:
    raw = 'data: {"text": "é"}\n\n'.encode()
    split = raw.index(b"\xc3") + 1
    assert list(parse_sse(iter([raw[:split], raw[split:]]))) == [{"data": {"text": "é"}}]

    for status, content_type in ((200, "application/json"), (202, "text/event-stream")):
        response_body = (
            'data: {"ok": true}\n\n'
            if status == 200
            else json.dumps({"command_kind": "stream", "accepted": True, "command_id": "c1"})
        )
        transport = QueueTransport(
            _response(_metadata()),
            _response(
                None,
                status=status,
                headers={"Content-Type": content_type},
                body=response_body,
            ),
        )
        with pytest.raises(Phase1EError, match="stream") as raised:
            Phase1EClient(transport=transport).run_session_stream(
                "s1", {"message": "m", "workspace": "/tmp/ws"}, idempotency_key="k"
            )
        assert raised.value.code == "invalid_stream_response"
