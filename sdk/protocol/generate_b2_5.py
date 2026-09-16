#!/usr/bin/env python3
"""Generate the additive Operant 2.0 B2 protocol clients."""

# Generated output contains intentionally long declarations.
# ruff: noqa: E402, E501

from __future__ import annotations

import copy
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sdk.protocol import generate_phase1e as base
from sdk.protocol.generate_phase23 import _python_aliases

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "sdk/protocol/schema/operant-b2-5.openapi.json"
DIGEST_PATH = ROOT / "sdk/protocol/schema/operant-b2-5.openapi.sha256"
TS_PATH = ROOT / "sdk/typescript-client/b2_5.generated.ts"
PY_PATH = ROOT / "sdk/python_client/b2_5_generated.py"
PROTOCOL_VERSION = "b2-5.v1"
EXPECTED_OPERATION_IDS = {
    "negotiateProtocol",
    "getGovernance",
    "searchHistory",
    "getHistoryItem",
    "getEvents",
    "getContextImpact",
    "execute",
}
OPERATION_MAP = {
    "negotiateB25": "negotiateProtocol",
    "getB25Governance": "getGovernance",
    "searchB25History": "searchHistory",
    "getB25HistoryItem": "getHistoryItem",
    "getB25Events": "getEvents",
    "getB25ContextImpact": "getContextImpact",
    "executeB25Command": "execute",
}


def _document() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="operant-b2-schema-") as directory:
        previous_path = os.environ.get("OPERANT_DB_PATH")
        try:
            os.environ["OPERANT_DB_PATH"] = str(Path(directory) / "import.sqlite3")
            from operant.api import create_app

            document = create_app(Path(directory) / "schema.sqlite3").openapi()
        finally:
            if previous_path is None:
                os.environ.pop("OPERANT_DB_PATH", None)
            else:
                os.environ["OPERANT_DB_PATH"] = previous_path
    selected: dict[str, Any] = {}
    accepted = set(OPERATION_MAP)
    for path, item in document["paths"].items():
        operations = {
            method: copy.deepcopy(operation)
            for method, operation in item.items()
            if isinstance(operation, dict) and operation.get("operationId") in accepted
        }
        if operations:
            selected[path] = operations
    for item in selected.values():
        for operation in item.values():
            operation["operationId"] = OPERATION_MAP[operation["operationId"]]
    selected["/v1/protocol/b2-5"]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ] = {"$ref": "#/components/schemas/ProtocolNegotiation"}
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
    protocol_properties = schemas["ProtocolNegotiation"]["properties"]
    protocol_properties["protocol_version"] = {"const": PROTOCOL_VERSION}
    protocol_properties["min_client_version"] = {"const": PROTOCOL_VERSION}
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
    for name in ("CommandReceipt", "ProtocolNegotiation", "SseFrame"):
        required.add(name)
        collect(schemas[name])
    return {
        "openapi": "3.1.0",
        "info": {"title": "Operant 2.0 B2 Protocol", "version": PROTOCOL_VERSION},
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
        raise ValueError(f"unexpected B2 operations: {sorted(operations)}")
    for operation in operations.values():
        if not operation.path.startswith("/v1/"):
            raise ValueError("B2 operation is not versioned")
        base._success_response(operation)


def _b2_names(source: str) -> str:
    renamed = (
        source.replace("PHASE1E", "B25")
        .replace("Phase1E", "B25")
        .replace("phase1e", "b25")
        .replace("Phase 1E", "B25")
    )
    if renamed.startswith('"""Generated B2'):
        marker = '"""\n\n'
        renamed = renamed.replace(marker, '"""\n\nfrom __future__ import annotations\n\n', 1)
        renamed = renamed.replace("# ruff: noqa: E501", "# ruff: noqa: E501, F401, I001", 1)
    return renamed.replace("operant-b25.openapi", "operant-b2-5.openapi").replace(
        "generate_b25.py", "generate_b2_5.py"
    )


def generate() -> str:
    document = _document()
    from sdk.protocol.generate_phase45 import _trim_unused_typescript_scaffolding

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
        ts_source = _trim_unused_typescript_scaffolding(_b2_names(ts_source))
        if ts_source.count("pathPart(") == 1:
            ts_source = re.sub(
                r"function pathPart\([^\n]+\n.*?\n}\n", "", ts_source, flags=re.DOTALL
            )
        base._write_if_changed(TS_PATH, ts_source)
        python_source = _b2_names(base._render_py_models(document, digest))
        base._write_if_changed(
            PY_PATH, _python_aliases(python_source, base._operation_specs(document))
        )
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
