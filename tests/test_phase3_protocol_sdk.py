from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient

from operant.api import create_app
from sdk.protocol import generate_phase3
from sdk.python_client.phase3_generated import PHASE3_SCHEMA_DIGEST


def test_phase3_schema_and_clients_are_deterministic(tmp_path: Path) -> None:
    paths = (
        generate_phase3.SCHEMA_PATH,
        generate_phase3.DIGEST_PATH,
        generate_phase3.TS_PATH,
        generate_phase3.PY_PATH,
    )
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    digest = generate_phase3.generate()
    after = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    assert before == after
    assert digest == PHASE3_SCHEMA_DIGEST

    schema = json.loads(generate_phase3.SCHEMA_PATH.read_text(encoding="utf-8"))
    operation_ids = {
        value["operationId"] for path in schema["paths"].values() for value in path.values()
    }
    assert operation_ids == generate_phase3.EXPECTED_OPERATION_IDS

    with TestClient(create_app(tmp_path / "phase3-protocol.sqlite3")) as client:
        result = client.get("/v1/protocol/phase3")
    assert result.status_code == 200
    assert result.json()["schema_digest"] == digest
