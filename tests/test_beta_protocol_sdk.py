from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from sdk.protocol.generate_beta import EXPECTED_OPERATION_IDS, generate
from sdk.python_client import BETA_PROTOCOL_VERSION, BETA_SCHEMA_DIGEST, BetaClient
from sdk.python_client.transport import TransportRequest, TransportResponse


class QueueTransport:
    def __init__(self, *responses: TransportResponse) -> None:
        self.responses = list(responses)
        self.requests: list[TransportRequest] = []

    def __call__(self, request: TransportRequest) -> TransportResponse:
        self.requests.append(request)
        return self.responses.pop(0)


def _response(payload: Any) -> TransportResponse:
    return TransportResponse(status=200, headers={}, json=json.dumps(payload))


def _metadata() -> dict[str, Any]:
    return {
        "protocol_version": BETA_PROTOCOL_VERSION,
        "schema_digest": BETA_SCHEMA_DIGEST,
        "min_client_version": BETA_PROTOCOL_VERSION,
        "capabilities": [
            "remote_gateway_wss",
            "https_target_connector",
            "container_writer_lifecycle",
        ],
    }


def test_beta_schema_and_clients_are_reproducible_and_additive() -> None:
    frozen_paths = tuple(
        Path(path)
        for phase in ("phase1e", "phase23", "phase45", "phase56")
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
    assert first == second == BETA_SCHEMA_DIGEST
    assert BETA_PROTOCOL_VERSION == "beta.v1"
    assert BetaClient.__name__ == "BetaClient"
    document = json.loads(
        Path("sdk/protocol/schema/operant-beta.openapi.json").read_text(encoding="utf-8")
    )
    operations = {
        operation["operationId"]
        for path_item in document["paths"].values()
        for operation in path_item.values()
    }
    assert operations == EXPECTED_OPERATION_IDS
    serialized = json.dumps(document, sort_keys=True).lower()
    assert "/internal/" not in serialized
    assert "oauth" not in serialized


def test_beta_client_negotiates_and_reuses_container_idempotency_key() -> None:
    projection = {
        "writer_workspace_id": "workspace-1",
        "container_name": "operant-writer-workspace-1",
        "image_ref": f"sha256:{'a' * 64}",
        "mount_ref": "container:workspace-1",
        "status": "created",
        "revision": 2,
        "owner_id": None,
        "fencing": 1,
        "lease_expires_at": None,
        "action_hash": "b" * 64,
        "resource_limits": {},
        "last_error_code": None,
        "created_at": "2026-09-04T00:00:00Z",
        "updated_at": "2026-09-04T00:00:00Z",
        "stopped_at": None,
        "removed_at": None,
    }
    transport = QueueTransport(_response(_metadata()), _response(projection))
    client = BetaClient("http://core.test", transport=transport)
    request = {
        "image": f"sha256:{'a' * 64}",
        "command": ["true"],
        "user_uid": 1000,
        "user_gid": 1000,
        "lease": {
            "writer_workspace_id": "workspace-1",
            "owner": "coder",
            "token": "lease-token-long-enough",
            "fencing": 1,
            "expires_at": "2026-09-04T01:00:00Z",
            "released_at": None,
        },
    }

    assert (
        client.create_container_writer("workspace-1", request, idempotency_key="container-stable")[
            "status"
        ]
        == "created"
    )
    assert transport.requests[0].url == "http://core.test/v1/protocol/beta"
    assert transport.requests[1].headers["Idempotency-Key"] == "container-stable"
