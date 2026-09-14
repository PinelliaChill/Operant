"""Generate offline B2-1 contracts. Does not create or advertise live endpoints."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from operant.contracts import b2_1
from sdk.protocol.generate_phase1e import _py_type, _ts_type

ROOT = Path(__file__).resolve().parents[2]


def document(version: str, interfaces: dict[str, Any]) -> dict[str, Any]:
    definitions: dict[str, Any] = {}
    for name, model in vars(b2_1).items():
        if not isinstance(model, type) or not issubclass(model, BaseModel):
            continue
        if model.__module__ != b2_1.__name__ or name == "Contract":
            continue
        schema = model.model_json_schema(ref_template="#/$defs/{model}")
        definitions.update(schema.pop("$defs", {}))
        definitions[name] = schema
    operations = {
        interface: {
            name: {"request": request.__name__, "response": response.__name__}
            for name, (request, response) in sorted(methods.items())
        }
        for interface, methods in interfaces.items()
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"urn:operant:{version}",
        "title": version,
        "description": "Offline contract definitions; no live API capability is registered.",
        "$defs": dict(sorted(definitions.items())),
        "x-interfaces": operations,
        "x-error": "ContractError",
        "x-enforcement": "Pydantic cross-field checks and current Core transaction authorization",
    }


def _type_refs(schema: Any) -> Any:
    if isinstance(schema, dict):
        return {
            key: value.replace("#/$defs/", "#/components/schemas/")
            if key == "$ref" and isinstance(value, str)
            else _type_refs(value)
            for key, value in schema.items()
        }
    if isinstance(schema, list):
        return [_type_refs(item) for item in schema]
    return schema


def _render(doc: dict[str, Any], digest: str, *, python: bool) -> str:
    if python:
        lines = [
            '"""Generated offline interfaces. No runtime transport. DO NOT EDIT."""',
            "",
            "# fmt: off",
            "# ruff: noqa: E501, I001, F401",
            "from __future__ import annotations",
            "from typing import Any, Literal, Protocol",
            "from typing_extensions import NotRequired, TypedDict",
            f"CONTRACT_VERSION = {doc['title']!r}",
            f"SCHEMA_DIGEST = {digest!r}",
        ]
    else:
        lines = [
            "// Generated offline interfaces. No runtime transport. DO NOT EDIT.",
            f"export const CONTRACT_VERSION = {json.dumps(doc['title'])} as const;",
            f"export const SCHEMA_DIGEST = {json.dumps(digest)} as const;",
        ]
    for name, raw_schema in doc["$defs"].items():
        schema = _type_refs(raw_schema)
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        lines.append("")
        lines.append(f"class {name}(TypedDict):" if python else f"export interface {name} {{")
        for field, prop in properties.items():
            value_type = _py_type(prop) if python else _ts_type(prop)
            if python:
                value_type = value_type if field in required else f"NotRequired[{value_type}]"
                lines.append(f"    {field}: {value_type}")
            else:
                optional = "" if field in required else "?"
                lines.append(f"  {field}{optional}: {value_type};")
        if python and not properties:
            lines.append("    pass")
        if not python:
            lines.append("}")
    for name, methods in doc["x-interfaces"].items():
        lines.append("")
        lines.append(f"class {name}(Protocol):" if python else f"export interface {name} {{")
        for method, types in methods.items():
            request, response = types["request"], types["response"]
            if python:
                lines.append(
                    f"    async def {method}(self, request: {request}) -> "
                    f"{response} | ContractError: ..."
                )
            else:
                lines.append(
                    f"  {method}(request: {request}): Promise<{response} | ContractError>;"
                )
        if not python:
            lines.append("}")
    return "\n".join(lines) + "\n"


def generate(root: Path = ROOT) -> dict[str, str]:
    results: dict[str, str] = {}
    groups = (
        ("b2-contract", b2_1.APP_CONTRACT_VERSION, {"B2AppContract": b2_1.APP_OPERATIONS}),
        (
            "memory-sdk",
            b2_1.PLUGIN_SDK_VERSION,
            {"MemoryEngine": b2_1.ENGINE_OPERATIONS, "MemoryHostApi": b2_1.HOST_OPERATIONS},
        ),
    )
    for stem, version, interfaces in groups:
        doc = document(version, interfaces)
        encoded = (json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
        digest = hashlib.sha256(encoded).hexdigest()
        files = {
            f"sdk/protocol/schema/operant-{stem}.json": encoded.decode(),
            f"sdk/protocol/schema/operant-{stem}.sha256": f"{digest}  operant-{stem}.json\n",
            f"sdk/typescript-client/{stem}.generated.ts": _render(doc, digest, python=False),
            f"sdk/python_client/{stem.replace('-', '_')}_generated.py": _render(
                doc, digest, python=True
            ),
        }
        for relative, content in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists() or path.read_text() != content:
                path.write_text(content)
        results[version] = digest
    return results


if __name__ == "__main__":
    print(json.dumps(generate(), sort_keys=True))
