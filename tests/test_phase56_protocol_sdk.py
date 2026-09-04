from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from operant.api import create_app
from sdk.protocol.generate_phase56 import EXPECTED_OPERATION_IDS, generate
from sdk.python_client import PHASE56_PROTOCOL_VERSION, PHASE56_SCHEMA_DIGEST, Phase56Client
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
        "protocol_version": PHASE56_PROTOCOL_VERSION,
        "schema_digest": PHASE56_SCHEMA_DIGEST,
        "min_client_version": PHASE56_PROTOCOL_VERSION,
        "capabilities": ["remote_control", "remote_execution", "multi_writer"],
    }


def test_phase56_schema_and_clients_are_reproducible_and_additive() -> None:
    frozen_paths = tuple(
        Path(path)
        for phase in ("phase1e", "phase23", "phase45")
        for path in (
            f"sdk/protocol/schema/operant-{phase}.openapi.json",
            f"sdk/protocol/schema/operant-{phase}.openapi.sha256",
            f"sdk/typescript-client/{phase}.generated.ts",
            f"sdk/python_client/{phase}_generated.py",
        )
    )
    python_init = Path("sdk/python_client/__init__.py")
    before = {
        path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (*frozen_paths, python_init)
    }
    first = generate()
    second = generate()
    after = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in before}

    assert before == after
    assert first == second == PHASE56_SCHEMA_DIGEST
    assert PHASE56_PROTOCOL_VERSION == "phase56.v1"
    assert Phase56Client.__name__ == "Phase56Client"
    assert '"Phase56Client"' in python_init.read_text(encoding="utf-8")
    document = json.loads(
        Path("sdk/protocol/schema/operant-phase56.openapi.json").read_text(encoding="utf-8")
    )
    operations = {
        operation["operationId"]
        for path_item in document["paths"].values()
        for operation in path_item.values()
    }
    assert operations == EXPECTED_OPERATION_IDS
    serialized = json.dumps(document, sort_keys=True).lower()
    for excluded in ("oauth", "tui", "tauri"):
        assert excluded not in serialized


def test_phase56_client_reuses_mutation_key_and_lists_remote_hosts() -> None:
    listed = {"items": [{"host_id": "host-1"}]}
    transport = QueueTransport(
        _response(_metadata()),
        _response(listed, headers={"Idempotency-Key": "enable-stable"}),
        _response(listed, headers={"Idempotency-Replayed": "true"}),
    )
    client = Phase56Client("http://core.test", transport=transport)
    request = {
        "display_name": "Local Core",
        "core_version": "0.1.0",
        "protocol_version": "phase56.v1",
    }

    assert client.enable_remote_host(request, idempotency_key="enable-stable") == listed
    assert client.list_remote_hosts() == listed
    assert transport.requests[1].headers["Idempotency-Key"] == "enable-stable"
    assert transport.requests[2].url == "http://core.test/v1/remote-control/hosts"


def test_pairing_secret_is_one_time_and_never_persisted_in_command_receipt(
    tmp_path: Path,
) -> None:
    database = tmp_path / "sensitive.sqlite3"
    app = create_app(database)
    with TestClient(app) as client:
        host = client.post(
            "/v1/remote-control/hosts/enable",
            headers={"Idempotency-Key": "enable-host"},
            json={
                "display_name": "Local Core",
                "core_version": "0.1.0",
                "protocol_version": "phase56.v1",
            },
        )
        assert host.status_code == 200, host.text
        response = client.post(
            "/v1/remote-control/pairing-challenges",
            headers={"Idempotency-Key": "pairing-secret"},
            json={"host_id": host.json()["host_id"], "ttl_seconds": 60},
        )
        assert response.status_code == 200, response.text
        assert len(response.json()["one_time_code"]) >= 16
        assert "no-store" in response.headers["Cache-Control"]
        retry = client.post(
            "/v1/remote-control/pairing-challenges",
            headers={"Idempotency-Key": "pairing-secret"},
            json={"host_id": host.json()["host_id"], "ttl_seconds": 60},
        )
        assert retry.status_code == 409
        assert retry.json()["error"]["recovery"] == "manual_reconcile"

    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT status, response_json, error_code FROM command_executions "
            "WHERE idempotency_key='pairing-secret'"
        ).fetchone()
    assert row == (
        "manual_reconcile_required",
        None,
        "sensitive_response_not_replayable",
    )
