"""Contract and offline generation tests for the additive Phase 2/3 SDK."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sdk.protocol.generate_phase1e import (
    PY_INIT_PATH,
    _canonical_json,
)
from sdk.protocol.generate_phase1e import (
    generate as generate_phase1e,
)
from sdk.protocol.generate_phase23 import (
    DIGEST_PATH,
    EXPECTED_OPERATION_IDS,
    PY_PATH,
    SCHEMA_PATH,
    TS_PATH,
    generate,
)
from sdk.python_client import (
    PHASE23_SCHEMA_DIGEST as EXPORTED_PHASE23_SCHEMA_DIGEST,
)
from sdk.python_client import Phase23Client as ExportedPhase23Client
from sdk.python_client.phase23_generated import (
    PHASE23_PROTOCOL_VERSION,
    PHASE23_SCHEMA_DIGEST,
    Phase23Client,
    ProtocolNegotiationError,
)
from sdk.python_client.transport import TransportRequest, TransportResponse


def _response(payload: Any = None, *, body: str | None = None) -> TransportResponse:
    return TransportResponse(
        status=200,
        headers={"content-type": "text/event-stream"} if body else {},
        body=body,
        json=None if body is not None else json.dumps(payload),
    )


class QueueTransport:
    def __init__(self, *responses: TransportResponse) -> None:
        self.responses = list(responses)
        self.requests: list[TransportRequest] = []

    def __call__(self, request: TransportRequest) -> TransportResponse:
        self.requests.append(request)
        return self.responses.pop(0)


def _metadata(*, digest: str = PHASE23_SCHEMA_DIGEST) -> dict[str, Any]:
    return {
        "protocol_version": PHASE23_PROTOCOL_VERSION,
        "schema_digest": digest,
        "min_client_version": PHASE23_PROTOCOL_VERSION,
        "capabilities": ["graph_runtime", "team_runtime", "sse_replay"],
    }


def test_schema_surface_and_generation_are_reproducible() -> None:
    document = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    operation_ids = {
        operation["operationId"]
        for path_item in document["paths"].values()
        for operation in path_item.values()
        if isinstance(operation, dict) and "operationId" in operation
    }
    assert operation_ids == EXPECTED_OPERATION_IDS
    assert all(path.startswith("/v1/") for path in document["paths"])
    for event_name in ("GraphEvent", "TeamEvent"):
        event_schema = document["components"]["schemas"][event_name]
        assert {
            "event_id",
            "cursor",
            "run_sequence",
            "schema_version",
            "event_type",
            "resource_scope",
            "stream_kind",
            "payload",
            "occurred_at",
        }.issubset(event_schema["required"])
        assert event_schema["properties"]["schema_version"] == {"const": "phase23.v1"}
    start_graph = document["components"]["schemas"]["StartGraphRunRequest"]
    assert "team_run_id" not in start_graph["properties"]
    assert start_graph["additionalProperties"] is False
    expected = hashlib.sha256(_canonical_json(document)).hexdigest()
    assert expected == PHASE23_SCHEMA_DIGEST
    assert DIGEST_PATH.read_text(encoding="utf-8") == f"{expected}  {SCHEMA_PATH.name}\n"
    frozen = (
        Path("sdk/protocol/schema/operant-phase1e.openapi.json"),
        Path("sdk/protocol/schema/operant-phase1e.openapi.sha256"),
        Path("sdk/typescript-client/phase1e.generated.ts"),
        Path("sdk/python_client/phase1e_generated.py"),
    )
    before = {
        path: path.read_bytes() for path in (*frozen, PY_INIT_PATH, DIGEST_PATH, TS_PATH, PY_PATH)
    }
    generate_phase1e()
    assert generate() == generate() == expected
    assert before == {path: path.read_bytes() for path in before}
    assert ExportedPhase23Client is Phase23Client
    assert EXPORTED_PHASE23_SCHEMA_DIGEST == PHASE23_SCHEMA_DIGEST


def test_client_fails_closed_on_schema_digest_mismatch() -> None:
    client = Phase23Client(
        "http://core.test",
        transport=QueueTransport(_response(_metadata(digest="0" * 64))),
    )
    with pytest.raises(ProtocolNegotiationError, match="Schema digest mismatch"):
        client.negotiate_protocol()


def test_client_preserves_cursor_scope_and_unknown_side_effect_boundary() -> None:
    receipt = {
        "command_kind": "command",
        "accepted": True,
        "command_id": "command-1",
        "resource_type": "graph_run",
        "resource_id": "run-1",
    }
    transport = QueueTransport(
        _response(_metadata()),
        _response(body='id: 8\nevent: node.succeeded\ndata: {"ok":true}\n\n'),
        TransportResponse(status=202, headers={}, json=json.dumps(receipt)),
    )
    client = Phase23Client("http://core.test", transport=transport)

    stream = client.stream_graph_run_events("run-1", last_event_id=7)
    assert list(stream.events) == [
        {
            "id": 8,
            "event": "node.succeeded",
            "data": {"ok": True},
            "resource_scope": "graph_run:run-1",
            "stream_kind": "graph.run",
        }
    ]
    client.resume_graph_run(
        "run-1",
        {"allow_unknown_side_effect_replay": False},
        idempotency_key="resume-1",
    )

    stream_request, resume_request = transport.requests[1:]
    assert stream_request.headers["Last-Event-ID"] == "7"
    assert json.loads(resume_request.body or "{}") == {"allow_unknown_side_effect_replay": False}
    assert resume_request.headers["Idempotency-Key"] == "resume-1"


def test_client_encodes_team_and_viewer_scope() -> None:
    transport = QueueTransport(
        _response(_metadata()),
        _response({"team_run_id": "team/run", "artifacts": []}),
        _response([]),
    )
    client = Phase23Client("http://core.test/", transport=transport)

    assert client.get_artifact_board("team/run", viewer_id="agent/one") == {
        "team_run_id": "team/run",
        "artifacts": [],
    }
    assert transport.requests[1].url == (
        "http://core.test/v1/teams/runs/team%2Frun/artifacts?viewer_id=agent%2Fone"
    )
    assert client.list_team_messages("team/run", viewer_id="agent/one") == []
    assert transport.requests[2].url == (
        "http://core.test/v1/teams/runs/team%2Frun/messages?viewer_id=agent%2Fone"
    )
