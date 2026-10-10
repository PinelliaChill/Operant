"""The independent caller domain must not rewrite frozen legacy contracts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient

from operant.api import create_app
from sdk.protocol import generate_caller_pairing
from sdk.python_client.caller_pairing_generated import CALLER_PAIRING_SCHEMA_DIGEST


def test_generated_pairing_is_deterministic_and_preserves_legacy_protocols(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    paths = (
        tuple((root / "sdk/protocol/schema").glob("operant-*.openapi.*"))
        + tuple((root / "sdk/python_client").glob("*_generated.py"))
        + tuple((root / "sdk/typescript-client").glob("*.generated.ts"))
    )
    before = {path: path.read_bytes() for path in paths}
    assert generate_caller_pairing.generate() == CALLER_PAIRING_SCHEMA_DIGEST
    assert generate_caller_pairing.generate() == CALLER_PAIRING_SCHEMA_DIGEST
    assert before == {path: path.read_bytes() for path in paths}
    schema = json.loads(generate_caller_pairing.SCHEMA_PATH.read_bytes())
    assert {
        operation["operationId"] for item in schema["paths"].values() for operation in item.values()
    } == generate_caller_pairing.EXPECTED_OPERATION_IDS
    assert (
        hashlib.sha256(generate_caller_pairing.SCHEMA_PATH.read_bytes().rstrip()).hexdigest()
        == CALLER_PAIRING_SCHEMA_DIGEST
    )
    with TestClient(create_app(tmp_path / "protocol.db", phase45_skill_roots={})) as client:
        response = client.get("/v1/protocol/caller-pairing")
    assert response.status_code == 200
    assert response.json()["schema_digest"] == CALLER_PAIRING_SCHEMA_DIGEST
    assert response.json()["protocol_version"] == "caller-pairing.v1"
