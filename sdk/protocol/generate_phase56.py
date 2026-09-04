#!/usr/bin/env python3
"""Generate additive Phase 5B/6 clients without changing frozen clients."""

# Generated output contains intentionally long declarations.
# ruff: noqa: E402, E501

from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path
from typing import Any

from operant.api import create_app
from sdk.protocol import generate_phase1e as base
from sdk.protocol.generate_phase23 import _python_aliases
from sdk.protocol.generate_phase45 import _trim_unused_typescript_scaffolding

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "sdk/protocol/schema/operant-phase56.openapi.json"
DIGEST_PATH = ROOT / "sdk/protocol/schema/operant-phase56.openapi.sha256"
TS_PATH = ROOT / "sdk/typescript-client/phase56.generated.ts"
PY_PATH = ROOT / "sdk/python_client/phase56_generated.py"
PROTOCOL_VERSION = "phase56.v1"
EXPECTED_OPERATION_IDS = {
    "negotiateProtocol",
    "enableRemoteHost",
    "getRemoteHost",
    "listRemoteHosts",
    "createPairingChallenge",
    "pairRemoteDevice",
    "listRemoteDevices",
    "revokeRemoteDevice",
    "createRemoteSession",
    "closeRemoteSession",
    "listRemoteSessions",
    "submitRemoteCommand",
    "getRemoteCommand",
    "reconcileRemoteCommand",
    "listRemoteControlEvents",
    "publishRelayEnvelope",
    "pullRelayEnvelopes",
    "acknowledgeRelayEnvelope",
    "getRelayHealth",
    "registerRemoteTarget",
    "listRemoteTargets",
    "getRemoteTarget",
    "heartbeatRemoteTarget",
    "acquireRemoteTargetLease",
    "renewRemoteTargetLease",
    "releaseRemoteTargetLease",
    "createRemoteTargetJob",
    "pollRemoteTargetJobs",
    "completeRemoteTargetJob",
    "cancelRemoteTargetJob",
    "listRemoteTargetJobs",
    "observeBrowser",
    "actBrowser",
    "observeComputer",
    "actComputer",
    "createWriterWorkspace",
    "listWriterWorkspaces",
    "acquireWriterLease",
    "renewWriterLease",
    "releaseWriterLease",
    "publishWriterArtifact",
    "listWriterArtifacts",
    "detectWriterConflicts",
    "listWriterConflicts",
    "createMergeRun",
    "getMergeRun",
    "listMergeRuns",
    "resolveMergeConflict",
    "finalizeMergeRun",
    "reconcileMergeRun",
}


def _document() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="operant-phase56-schema-") as directory:
        document = create_app(Path(directory) / "schema.sqlite3").openapi()
    selected: dict[str, Any] = {}
    accepted = (EXPECTED_OPERATION_IDS - {"negotiateProtocol"}) | {"negotiatePhase56"}
    for path, item in document["paths"].items():
        operations = {
            method: copy.deepcopy(operation)
            for method, operation in item.items()
            if isinstance(operation, dict) and operation.get("operationId") in accepted
        }
        if operations:
            selected[path] = operations
    selected["/v1/protocol/phase56"]["get"]["operationId"] = "negotiateProtocol"
    for path_item in selected.values():
        for method, operation in path_item.items():
            if method.upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
                continue
            parameters = operation.setdefault("parameters", [])
            if not any(
                parameter.get("in") == "header"
                and str(parameter.get("name", "")).lower() == "idempotency-key"
                for parameter in parameters
            ):
                parameters.append(
                    {
                        "name": "Idempotency-Key",
                        "in": "header",
                        "required": False,
                        "schema": {"type": "string", "minLength": 1, "maxLength": 300},
                    }
                )
    schemas = copy.deepcopy(document.get("components", {}).get("schemas", {}))
    frozen = json.loads(
        (ROOT / "sdk/protocol/schema/operant-phase1e.openapi.json").read_text(encoding="utf-8")
    )
    for name, schema in frozen["components"]["schemas"].items():
        schemas.setdefault(name, copy.deepcopy(schema))
    required: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            reference = value.get("$ref")
            prefix = "#/components/schemas/"
            if isinstance(reference, str) and reference.startswith(prefix):
                name = reference.removeprefix(prefix)
                if name not in required:
                    required.add(name)
                    collect(schemas[name])
            for nested in value.values():
                collect(nested)
        elif isinstance(value, list):
            for nested in value:
                collect(nested)

    collect(selected)
    for name in ("CommandReceipt", "ProtocolNegotiation", "ReceiptResource", "SseFrame"):
        required.add(name)
        collect(schemas[name])
    return {
        "openapi": "3.1.0",
        "info": {"title": "Operant Phase 5B/6 Protocol", "version": PROTOCOL_VERSION},
        "paths": selected,
        "components": {"schemas": {name: schemas[name] for name in sorted(required)}},
    }


def _validate(document: dict[str, Any]) -> None:
    previous = base.EXPECTED_OPERATION_IDS
    base.EXPECTED_OPERATION_IDS = EXPECTED_OPERATION_IDS
    try:
        operations = base._operation_specs(document)
    finally:
        base.EXPECTED_OPERATION_IDS = previous
    if set(operations) != EXPECTED_OPERATION_IDS:
        raise ValueError(f"unexpected Phase 5B/6 operations: {sorted(operations)}")
    for operation in operations.values():
        if not operation.path.startswith("/v1/"):
            raise ValueError("Phase 5B/6 operation is not versioned")
        base._success_response(operation)


def _phase56_names(source: str) -> str:
    renamed = (
        source.replace("PHASE1E", "PHASE56")
        .replace("Phase1E", "Phase56")
        .replace("phase1e", "phase56")
        .replace("Phase 1E", "Phase 5B/6")
    )
    if renamed.startswith('"""Generated Phase 5B/6'):
        marker = '"""\n\n'
        renamed = renamed.replace(marker, '"""\n\nfrom __future__ import annotations\n\n', 1)
        renamed = renamed.replace("# ruff: noqa: E501", "# ruff: noqa: E501, F401, I001", 1)
    return renamed


def generate() -> str:
    document = _document()
    _validate(document)
    base._write_if_changed(SCHEMA_PATH, base._canonical_json(document).decode("utf-8") + "\n")
    previous = (
        base.SCHEMA_PATH,
        base.DIGEST_PATH,
        base.TS_PATH,
        base.PY_PATH,
        base.PROTOCOL_VERSION,
        base.EXPECTED_OPERATION_IDS,
    )
    try:
        base.SCHEMA_PATH = SCHEMA_PATH
        base.DIGEST_PATH = DIGEST_PATH
        base.TS_PATH = TS_PATH
        base.PY_PATH = PY_PATH
        base.PROTOCOL_VERSION = PROTOCOL_VERSION
        base.EXPECTED_OPERATION_IDS = EXPECTED_OPERATION_IDS
        digest = base._digest(document)
        base._write_if_changed(DIGEST_PATH, f"{digest}  {SCHEMA_PATH.name}\n")
        ts_source = (
            base._ts_models(document, digest) + "\n" + base._render_ts_client(document, digest)
        )
        base._write_if_changed(
            TS_PATH, _trim_unused_typescript_scaffolding(_phase56_names(ts_source))
        )
        python_source = _phase56_names(base._render_py_models(document, digest))
        base._write_if_changed(
            PY_PATH, _python_aliases(python_source, base._operation_specs(document))
        )
        base._write_if_changed(base.PY_INIT_PATH, base._python_package_init())
        return digest
    finally:
        (
            base.SCHEMA_PATH,
            base.DIGEST_PATH,
            base.TS_PATH,
            base.PY_PATH,
            base.PROTOCOL_VERSION,
            base.EXPECTED_OPERATION_IDS,
        ) = previous


if __name__ == "__main__":
    print(generate())
