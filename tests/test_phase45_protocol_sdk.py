from __future__ import annotations

import hashlib
import json
from pathlib import Path

from sdk.protocol.generate_phase45 import generate
from sdk.python_client import PHASE45_PROTOCOL_VERSION, PHASE45_SCHEMA_DIGEST, Phase45Client


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
        "issueCapabilityLease",
        "consumeCapabilityLease",
        "listSecurityAudit",
    }
    serialized = json.dumps(document, sort_keys=True).lower()
    for excluded in ("remote", "relay", "oauth", "tui", "tauri", "browser", "computer"):
        assert excluded not in serialized


def test_phase45_generated_clients_expose_security_operations() -> None:
    python_source = Path("sdk/python_client/phase45_generated.py").read_text(encoding="utf-8")
    typescript_source = Path("sdk/typescript-client/phase45.generated.ts").read_text(
        encoding="utf-8"
    )
    for operation in (
        "normalizeAction",
        "checkPolicy",
        "explainPolicy",
        "testPolicy",
        "issueCapabilityLease",
        "consumeCapabilityLease",
        "listSecurityAudit",
    ):
        assert operation in python_source
        assert operation in typescript_source
