"""Focused B2-2 protocol and generated SDK tests."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from sdk.protocol import (
    generate_b2,
    generate_beta,
    generate_phase1e,
    generate_phase23,
    generate_phase45,
    generate_phase56,
)
from sdk.protocol.generate_phase1e import _canonical_json
from sdk.python_client.b2_generated import (
    B2_MAX_CURSOR,
    B2_PROTOCOL_VERSION,
    B2_SCHEMA_DIGEST,
    B2Client,
    B2Error,
    ProtocolNegotiationError,
)
from sdk.python_client.transport import TransportRequest, TransportResponse


def _response(
    payload: Any,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> TransportResponse:
    return TransportResponse(
        status=status,
        headers=headers or {},
        json=json.dumps(payload, ensure_ascii=False),
    )


def _metadata() -> dict[str, Any]:
    return {
        "protocol_version": B2_PROTOCOL_VERSION,
        "schema_digest": B2_SCHEMA_DIGEST,
        "min_client_version": B2_PROTOCOL_VERSION,
        "capabilities": ["task_projection", "canonical_history", "session_cancel"],
    }


class QueueTransport:
    def __init__(self, *responses: TransportResponse) -> None:
        self.responses = list(responses)
        self.requests: list[TransportRequest] = []

    def __call__(self, request: TransportRequest) -> TransportResponse:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("unexpected transport request")
        response = self.responses.pop(0)
        if callable(response):
            return response(request)
        if isinstance(response, Exception):
            raise response
        return response


def test_b2_client_negotiates_and_sends_typed_paths_and_idempotency() -> None:
    transport = QueueTransport(
        _response(_metadata()),
        _response(
            {"id": "model-1", "name": "relay", "model_id": "m1"},
            status=201,
            headers={"Idempotency-Key": "model-create"},
        ),
    )
    client = B2Client("http://core.test/", transport=transport)

    created = client.create_model(
        {
            "name": "relay",
            "model_id": "m1",
            "base_url": "https://provider.example.test/v1",
        },
        idempotency_key="model-create",
    )

    assert created["id"] == "model-1"
    assert len(transport.requests) == 2
    assert transport.requests[0].url == "http://core.test/v1/protocol/b2"
    assert transport.requests[0].headers["X-Operant-Client-Version"] == B2_PROTOCOL_VERSION
    create_request = transport.requests[1]
    assert create_request.method == "POST"
    assert create_request.url == "http://core.test/v1/models"
    assert create_request.headers["Idempotency-Key"] == "model-create"
    assert json.loads(create_request.body or "")["model_id"] == "m1"
    assert client.last_response is not None
    assert client.last_response.idempotency_key == "model-create"

    # Path values are URL encoded before they cross the transport boundary.
    history_transport = QueueTransport(
        _response(_metadata()),
        _response(
            {
                "session": {},
                "thread_id": None,
                "agents": [],
                "items": [],
                "next_cursor": None,
            }
        ),
    )
    B2Client("http://core.test", transport=history_transport).get_session_history(
        "session/with spaces",
        after_cursor=B2_MAX_CURSOR,
        limit=2,
    )
    history_request = history_transport.requests[1]
    assert history_request.url == (
        "http://core.test/v1/b2/sessions/session%2Fwith%20spaces/history"
        "?after_cursor=9223372036854775807&limit=2"
    )


def test_b2_client_protocol_errors_missing_fields_unknown_resource_and_disconnect() -> None:
    missing_metadata = {"protocol_version": B2_PROTOCOL_VERSION}
    with pytest.raises(ProtocolNegotiationError, match="missing required metadata"):
        B2Client(transport=QueueTransport(_response(missing_metadata))).negotiate_protocol()

    not_found = QueueTransport(
        _response(_metadata()),
        _response(
            {
                "detail": "session not found",
                "error": {
                    "code": "http_404",
                    "message": "session not found",
                    "retryable": False,
                    "recovery": "none",
                },
            },
            status=404,
        ),
    )
    with pytest.raises(B2Error) as not_found_error:
        B2Client(transport=not_found).get_session_history("missing")
    assert not_found_error.value.code == "http_404"
    assert not_found_error.value.retryable is False
    assert not_found_error.value.recovery == "none"

    disconnected = QueueTransport(_response(_metadata()))

    def fail(_request: TransportRequest) -> TransportResponse:
        raise OSError("connection dropped")

    disconnected.responses.append(fail)  # type: ignore[arg-type]
    with pytest.raises(B2Error) as disconnected_error:
        B2Client(transport=disconnected).list_models()
    assert disconnected_error.value.code == "transport_unavailable"
    assert disconnected_error.value.retryable is True
    assert disconnected_error.value.recovery == "retry_later"


def test_b2_schema_and_generated_clients_are_deterministic_and_legacy_files_frozen() -> None:
    b2_paths = (
        generate_b2.SCHEMA_PATH,
        generate_b2.DIGEST_PATH,
        generate_b2.TS_PATH,
        generate_b2.PY_PATH,
    )
    legacy_paths = (
        generate_phase1e.SCHEMA_PATH,
        generate_phase1e.DIGEST_PATH,
        generate_phase1e.TS_PATH,
        generate_phase1e.PY_PATH,
        generate_phase23.SCHEMA_PATH,
        generate_phase23.DIGEST_PATH,
        generate_phase23.TS_PATH,
        generate_phase23.PY_PATH,
        generate_phase45.SCHEMA_PATH,
        generate_phase45.DIGEST_PATH,
        generate_phase45.TS_PATH,
        generate_phase45.PY_PATH,
        generate_phase56.SCHEMA_PATH,
        generate_phase56.DIGEST_PATH,
        generate_phase56.TS_PATH,
        generate_phase56.PY_PATH,
        generate_beta.SCHEMA_PATH,
        generate_beta.DIGEST_PATH,
        generate_beta.TS_PATH,
        generate_beta.PY_PATH,
    )
    before_legacy = {path: path.read_bytes() for path in legacy_paths}
    document = json.loads(generate_b2.SCHEMA_PATH.read_text(encoding="utf-8"))
    expected_digest = hashlib.sha256(_canonical_json(document)).hexdigest()
    assert expected_digest == B2_SCHEMA_DIGEST
    assert generate_b2.DIGEST_PATH.read_text(encoding="utf-8") == (
        f"{expected_digest}  {generate_b2.SCHEMA_PATH.name}\n"
    )

    first_digest = generate_b2.generate()
    first_bytes = {path: path.read_bytes() for path in b2_paths}
    second_digest = generate_b2.generate()
    second_bytes = {path: path.read_bytes() for path in b2_paths}

    assert first_digest == expected_digest == second_digest
    assert first_bytes == second_bytes
    assert before_legacy == {path: path.read_bytes() for path in legacy_paths}


def test_b2_operation_surface_is_additive_and_excludes_old_protocol_operations() -> None:
    operation_ids = set(generate_b2.EXPECTED_OPERATION_IDS)
    assert operation_ids == {
        "updateModel",
        "getSessionHistory",
        "discoverModels",
        "createModel",
        "updateRole",
        "createRole",
        "listModels",
        "listTasks",
        "listAgents",
        "getTask",
        "createThread",
        "cancelSession",
        "listRoles",
        "negotiateProtocol",
    }
    b2_document = json.loads(generate_b2.SCHEMA_PATH.read_text(encoding="utf-8"))
    b2_operations = {
        operation["operationId"]
        for path_item in b2_document["paths"].values()
        for operation in path_item.values()
        if isinstance(operation, dict) and "operationId" in operation
    }
    assert b2_operations == operation_ids
    assert set(b2_operations).isdisjoint({"createSession", "runSessionStream", "listProjects"})
