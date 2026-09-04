from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from sdk.protocol.generate_phase45 import generate
from sdk.python_client import PHASE45_PROTOCOL_VERSION, PHASE45_SCHEMA_DIGEST, Phase45Client
from sdk.python_client.transport import TransportRequest, TransportResponse


class QueueTransport:
    def __init__(self, *responses: TransportResponse) -> None:
        self.responses = list(responses)
        self.requests: list[TransportRequest] = []

    def __call__(self, request: TransportRequest) -> TransportResponse:
        self.requests.append(request)
        return self.responses.pop(0)


def _response(payload: Any, *, headers: dict[str, str] | None = None) -> TransportResponse:
    return TransportResponse(status=200, headers=headers or {}, json=json.dumps(payload))


def _metadata() -> dict[str, Any]:
    return {
        "protocol_version": PHASE45_PROTOCOL_VERSION,
        "schema_digest": PHASE45_SCHEMA_DIGEST,
        "min_client_version": PHASE45_PROTOCOL_VERSION,
        "capabilities": ["scheduler"],
    }


def test_phase45_schema_and_clients_are_reproducible_and_additive() -> None:
    frozen_paths = (
        Path("sdk/protocol/schema/operant-phase1e.openapi.json"),
        Path("sdk/protocol/schema/operant-phase1e.openapi.sha256"),
        Path("sdk/typescript-client/phase1e.generated.ts"),
        Path("sdk/python_client/phase1e_generated.py"),
        Path("sdk/protocol/schema/operant-phase23.openapi.json"),
        Path("sdk/protocol/schema/operant-phase23.openapi.sha256"),
        Path("sdk/typescript-client/phase23.generated.ts"),
        Path("sdk/python_client/phase23_generated.py"),
    )
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in frozen_paths}
    digest = generate()
    after = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in frozen_paths}

    assert before == after
    assert digest == PHASE45_SCHEMA_DIGEST
    assert PHASE45_PROTOCOL_VERSION == "phase45.v1"
    assert Phase45Client.__name__ == "Phase45Client"
    document = json.loads(
        Path("sdk/protocol/schema/operant-phase45.openapi.json").read_text(encoding="utf-8")
    )
    operations = {
        operation["operationId"]
        for path_item in document["paths"].values()
        for operation in path_item.values()
    }
    assert operations == {
        "negotiateProtocol",
        "normalizeAction",
        "checkPolicy",
        "explainPolicy",
        "testPolicy",
        "getPhase45Approval",
        "decidePhase45Approval",
        "reviewPhase45Approval",
        "issueCapabilityLease",
        "consumeCapabilityLease",
        "listSecurityAudit",
        "discoverSkills",
        "listSkills",
        "createMcpServer",
        "updateMcpServer",
        "listMcpServers",
        "listMcpWorkspaceRoots",
        "deleteMcpServer",
        "listMcpTools",
        "getMcpActionReceipt",
        "startMcpServer",
        "stopMcpServer",
        "callMcpTool",
        "createSchedule",
        "updateSchedule",
        "listSchedules",
        "getSchedule",
        "setScheduleStatus",
        "triggerSchedule",
        "listSchedulerQueue",
        "listDeadLetter",
        "replayDeadLetter",
    }
    serialized = json.dumps(document, sort_keys=True).lower()
    for excluded in ("remote", "relay", "oauth", "tui", "tauri", "browser", "computer"):
        assert excluded not in serialized
    for path_item in document["paths"].values():
        for method, operation in path_item.items():
            if method.upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
                continue
            assert any(
                parameter.get("in") == "header" and parameter.get("name") == "Idempotency-Key"
                for parameter in operation.get("parameters", [])
            )


def test_phase45_generated_clients_expose_phase45_operations() -> None:
    python_source = Path("sdk/python_client/phase45_generated.py").read_text(encoding="utf-8")
    typescript_source = Path("sdk/typescript-client/phase45.generated.ts").read_text(
        encoding="utf-8"
    )
    for operation in (
        "normalizeAction",
        "checkPolicy",
        "explainPolicy",
        "testPolicy",
        "getPhase45Approval",
        "decidePhase45Approval",
        "reviewPhase45Approval",
        "issueCapabilityLease",
        "consumeCapabilityLease",
        "listSecurityAudit",
        "discoverSkills",
        "listSkills",
        "createMcpServer",
        "updateMcpServer",
        "listMcpServers",
        "listMcpWorkspaceRoots",
        "deleteMcpServer",
        "listMcpTools",
        "getMcpActionReceipt",
        "startMcpServer",
        "stopMcpServer",
        "callMcpTool",
        "createSchedule",
        "updateSchedule",
        "listSchedules",
        "getSchedule",
        "setScheduleStatus",
        "triggerSchedule",
        "listSchedulerQueue",
        "listDeadLetter",
        "replayDeadLetter",
    ):
        assert operation in python_source
        assert operation in typescript_source

    for unused_scaffolding in (
        "  parseSse,",
        "  readText,",
        "function cursorQuery(",
    ):
        assert unused_scaffolding not in typescript_source
    assert "function newIdempotencyKey(" in typescript_source
    assert "function requireIdempotencyKey(" in typescript_source


def test_phase45_client_reuses_mutation_key_and_exposes_replayed_response() -> None:
    created = {
        "id": "schedule-1",
        "version": 1,
        "name": "nightly",
        "trigger_kind": "cron",
        "cron_expression": "0 3 * * *",
        "timer_at": None,
        "timezone_name": "UTC",
        "workflow_id": "workflow-1",
        "workflow_version": 1,
    }
    transport = QueueTransport(
        _response(_metadata()),
        _response(created, headers={"Idempotency-Key": "create-stable"}),
        _response(
            created,
            headers={
                "Idempotency-Key": "create-stable",
                "Idempotency-Replayed": "true",
            },
        ),
    )
    client = Phase45Client("http://core.test", transport=transport)

    assert client.create_schedule(created, idempotency_key="create-stable") == created
    assert client.create_schedule(created, idempotency_key="create-stable") == created

    first, retry = transport.requests[1:]
    assert first.url == retry.url == "http://core.test/v1/schedules"
    assert first.headers["Idempotency-Key"] == "create-stable"
    assert retry.headers["Idempotency-Key"] == "create-stable"
    assert client.last_response is not None
    assert client.last_response.idempotency_replayed is True
