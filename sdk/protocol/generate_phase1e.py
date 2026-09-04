#!/usr/bin/env python3
"""Generate the Phase 1E SDK from the public OpenAPI document.

The OpenAPI document is the only wire-contract source. In particular, client
operation metadata, paths, HTTP methods, parameters, request bodies, and
responses are all read from ``document["paths"]``. The small amount of
operation-id keyed code below is deliberately limited to runtime ergonomics
(for example, the SSE response adapter); it does not contain a second wire
contract.

Only the Python standard library is used so generation remains deterministic
and works offline.
"""

# Generated output contains long protocol declarations; generated files carry
# the same targeted exemption.
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "sdk/protocol/schema/operant-phase1e.openapi.json"
DIGEST_PATH = ROOT / "sdk/protocol/schema/operant-phase1e.openapi.sha256"
TS_PATH = ROOT / "sdk/typescript-client/phase1e.generated.ts"
PY_DIR = ROOT / "sdk/python_client"
PY_PATH = PY_DIR / "phase1e_generated.py"
PY_INIT_PATH = PY_DIR / "__init__.py"

PROTOCOL_VERSION = "phase1e.v1"
MAX_CURSOR = 2**63 - 1
EXPECTED_OPERATION_IDS = {
    "negotiateProtocol",
    "listProjects",
    "listWorkspaceFiles",
    "listThreads",
    "createSession",
    "runSessionStream",
    "listPendingApprovals",
    "submitApproval",
}
HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
_IDENTIFIER = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_PATH_PARAMETER = re.compile(r"\{([^}]+)\}")


@dataclass(frozen=True)
class ParameterSpec:
    name: str
    location: str
    required: bool
    schema: dict[str, Any]


@dataclass(frozen=True)
class OperationSpec:
    operation_id: str
    path: str
    method: str
    parameters: tuple[ParameterSpec, ...]
    request_body: dict[str, Any] | None
    responses: dict[str, dict[str, Any]]
    mutually_exclusive: tuple[tuple[str, ...], ...]

    @property
    def path_parameters(self) -> tuple[ParameterSpec, ...]:
        return tuple(parameter for parameter in self.parameters if parameter.location == "path")

    @property
    def query_parameters(self) -> tuple[ParameterSpec, ...]:
        return tuple(parameter for parameter in self.parameters if parameter.location == "query")

    @property
    def header_parameters(self) -> tuple[ParameterSpec, ...]:
        return tuple(parameter for parameter in self.parameters if parameter.location == "header")


def _read_schema() -> dict[str, Any]:
    with SCHEMA_PATH.open(encoding="utf-8") as handle:
        document = json.load(handle)
    if not isinstance(document, dict):
        raise ValueError("Phase 1E Schema must be a JSON object")
    return document


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(document: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(document)).hexdigest()


def _write_if_changed(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return
    path.write_text(content, encoding="utf-8")


def _ref_name(schema: dict[str, Any]) -> str | None:
    ref = schema.get("$ref")
    if not isinstance(ref, str) or not ref.startswith("#/components/schemas/"):
        return None
    return ref.rsplit("/", 1)[-1]


def _literal(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    return str(value)


def _ts_type(schema: dict[str, Any], *, property_name: str = "") -> str:
    """Render a schema type while preserving the lossless int64 distinction."""

    ref = _ref_name(schema)
    if ref:
        return "Cursor" if ref == "Cursor" else ref
    if schema.get("format") == "int64":
        schema_type = schema.get("type")
        if isinstance(schema_type, list):
            return " | ".join("null" if item == "null" else "Int64" for item in schema_type)
        return "Int64"
    if "const" in schema:
        return _literal(schema["const"])
    enum = schema.get("enum")
    if isinstance(enum, list):
        return " | ".join(_literal(value) for value in enum)
    any_of = schema.get("anyOf")
    if isinstance(any_of, list):
        return (
            " | ".join(
                item_type
                for item_type in (_ts_type(item, property_name=property_name) for item in any_of)
                if item_type != "never"
            )
            or "never"
        )
    one_of = schema.get("oneOf")
    if isinstance(one_of, list):
        return " | ".join(_ts_type(item, property_name=property_name) for item in one_of)
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        return " | ".join(
            _ts_type({"type": item}, property_name=property_name) for item in schema_type
        )
    if schema_type == "string":
        return "string"
    if schema_type == "null":
        return "null"
    if schema_type == "integer" or schema_type == "number":
        return "number"
    if schema_type == "boolean":
        return "boolean"
    if schema_type == "array":
        items = schema.get("items", {})
        return f"Array<{_ts_type(items, property_name=property_name)}>"
    if schema_type == "object" or "properties" in schema:
        return "Record<string, unknown>"
    return "unknown"


def _py_type(schema: dict[str, Any], *, property_name: str = "") -> str:
    ref = _ref_name(schema)
    if ref:
        return "Cursor" if ref == "Cursor" else ref
    if schema.get("format") == "int64":
        schema_type = schema.get("type")
        if isinstance(schema_type, list):
            return " | ".join("None" if item == "null" else "Int64" for item in schema_type)
        return "Int64"
    if "const" in schema:
        return f"Literal[{schema['const']!r}]"
    enum = schema.get("enum")
    if isinstance(enum, list):
        values = ", ".join(repr(value) for value in enum)
        return f"Literal[{values}]"
    for combinator in ("anyOf", "oneOf"):
        options = schema.get(combinator)
        if isinstance(options, list):
            types = [_py_type(item, property_name=property_name) for item in options]
            if "Any" in types:
                return "Any"
            return " | ".join(types) or "Any"
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        types = [_py_type({"type": item}, property_name=property_name) for item in schema_type]
        return " | ".join(types) or "Any"
    if schema_type == "string":
        return "str"
    if schema_type == "null":
        return "None"
    if schema_type == "integer":
        return "int"
    if schema_type == "number":
        return "float"
    if schema_type == "boolean":
        return "bool"
    if schema_type == "array":
        return f"list[{_py_type(schema.get('items', {}), property_name=property_name)}]"
    if schema_type == "object" or "properties" in schema:
        return "dict[str, Any]"
    return "Any"


def _schema_objects(document: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    schemas = document.get("components", {}).get("schemas", {})
    if not isinstance(schemas, dict):
        raise ValueError("components.schemas must be an object")
    return [(name, schemas[name]) for name in schemas if isinstance(schemas[name], dict)]


def _components(document: dict[str, Any], kind: str) -> dict[str, Any]:
    components = document.get("components")
    if not isinstance(components, dict) or not isinstance(components.get(kind), dict):
        raise ValueError(f"components.{kind} must be an object")
    return components[kind]


def _resolve_component(
    document: dict[str, Any], value: Any, kind: str, *, label: str
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    ref = value.get("$ref")
    if ref is None:
        return value
    if not isinstance(ref, str) or not ref.startswith(f"#/components/{kind}/"):
        raise ValueError(f"{label} has an unsupported reference")
    name = ref.rsplit("/", 1)[-1]
    component = _components(document, kind).get(name)
    if not isinstance(component, dict):
        raise ValueError(f"{label} references missing components.{kind}.{name}")
    return component


def _wire_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Keep refs compact while retaining inline schemas from the operation."""

    ref = schema.get("$ref")
    if isinstance(ref, str):
        return {"$ref": ref}
    return schema


def _schema_from_ref(document: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    ref = _ref_name(schema)
    if ref is None:
        return schema
    value = _components(document, "schemas").get(ref)
    if not isinstance(value, dict):
        raise ValueError(f"schema references missing components.schemas.{ref}")
    return value


def _exclusive_groups_from_value(
    value: Any, *, properties: set[str], label: str
) -> tuple[tuple[str, ...], ...]:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        groups_value: list[Any] = [value]
    elif isinstance(value, list) and all(isinstance(item, list) for item in value):
        groups_value = value
    else:
        raise ValueError(f"{label} must be a list of property names or lists")
    groups: list[tuple[str, ...]] = []
    for index, group_value in enumerate(groups_value):
        if (
            not isinstance(group_value, list)
            or len(group_value) < 2
            or not all(isinstance(name, str) and name for name in group_value)
            or len(set(group_value)) != len(group_value)
            or not set(group_value) <= properties
        ):
            raise ValueError(f"{label}[{index}] must name two or more distinct properties")
        groups.append(tuple(group_value))
    if not groups:
        raise ValueError(f"{label} must not be empty")
    return tuple(groups)


def _mutually_exclusive_groups(
    document: dict[str, Any], schema: dict[str, Any], *, label: str
) -> tuple[tuple[str, ...], ...]:
    """Read the schema's explicit XOR metadata, failing closed when malformed."""

    resolved = _schema_from_ref(document, schema)
    properties = resolved.get("properties", {})
    property_names = set(properties) if isinstance(properties, dict) else set()
    extension = resolved.get("x-mutually-exclusive")
    if extension is not None:
        return _exclusive_groups_from_value(
            extension, properties=property_names, label=f"{label}.x-mutually-exclusive"
        )
    one_of = resolved.get("oneOf")
    if one_of is None:
        return ()
    if not isinstance(one_of, list) or not one_of:
        raise ValueError(f"{label}.oneOf must be a non-empty list")
    branch_fields: list[str] = []
    for index, branch in enumerate(one_of):
        if not isinstance(branch, dict):
            raise ValueError(f"{label}.oneOf[{index}] must be an object")
        branch_schema = _schema_from_ref(document, branch)
        required = branch_schema.get("required")
        if not isinstance(required, list) or len(required) != 1 or not isinstance(required[0], str):
            raise ValueError(
                f"{label}.oneOf[{index}] must require exactly one property for XOR generation"
            )
        branch_fields.append(required[0])
    return _exclusive_groups_from_value(
        branch_fields, properties=property_names, label=f"{label}.oneOf"
    )


def _content_descriptor(content: Any, *, label: str) -> dict[str, Any]:
    if content is None:
        return {}
    if not isinstance(content, dict) or not content:
        raise ValueError(f"{label}.content must be a non-empty object")
    result: dict[str, Any] = {}
    for media_type in sorted(content):
        media = content[media_type]
        if not isinstance(media, dict) or not isinstance(media.get("schema"), dict):
            raise ValueError(f"{label} media type {media_type} must declare a schema")
        result[media_type] = {"schema": _wire_schema(media["schema"])}
    return result


def _operation_specs(document: dict[str, Any]) -> dict[str, OperationSpec]:
    paths = document.get("paths")
    if not isinstance(paths, dict) or not paths:
        raise ValueError("Phase 1E Schema paths are missing")
    operations: dict[str, OperationSpec] = {}
    for path, path_item in paths.items():
        if not isinstance(path, str) or not isinstance(path_item, dict):
            raise ValueError("each path item must be an object")
        path_parameters = path_item.get("parameters", [])
        if not isinstance(path_parameters, list):
            raise ValueError(f"{path} path parameters must be an array")
        for raw_method, raw_operation in path_item.items():
            method = str(raw_method).lower()
            if method not in HTTP_METHODS:
                continue
            if not isinstance(raw_operation, dict):
                raise ValueError(f"{path} {method} operation must be an object")
            operation_id = raw_operation.get("operationId")
            if not isinstance(operation_id, str) or not operation_id:
                raise ValueError(f"{path} {method} must declare operationId")
            if operation_id in operations:
                raise ValueError(f"duplicate operationId: {operation_id}")
            operation_parameters = raw_operation.get("parameters", [])
            if not isinstance(operation_parameters, list):
                raise ValueError(f"{operation_id}.parameters must be an array")
            raw_parameters = path_parameters + operation_parameters
            parameters: list[ParameterSpec] = []
            seen_parameters: set[tuple[str, str]] = set()
            for index, raw_parameter in enumerate(raw_parameters):
                parameter = _resolve_component(
                    document,
                    raw_parameter,
                    "parameters",
                    label=f"{operation_id}.parameters[{index}]",
                )
                name = parameter.get("name")
                location = parameter.get("in")
                schema = parameter.get("schema")
                if not isinstance(name, str) or not name:
                    raise ValueError(f"{operation_id} parameter name is invalid")
                if location not in {"path", "query", "header"}:
                    raise ValueError(f"{operation_id} parameter {name} has unsupported location")
                if not isinstance(schema, dict):
                    raise ValueError(f"{operation_id} parameter {name} has no schema")
                key = (location, name)
                if key in seen_parameters:
                    raise ValueError(f"{operation_id} has duplicate parameter {location}:{name}")
                seen_parameters.add(key)
                required = parameter.get("required") is True or location == "path"
                parameters.append(
                    ParameterSpec(name=name, location=location, required=required, schema=schema)
                )
            placeholders = set(_PATH_PARAMETER.findall(path))
            declared_path = {
                parameter.name for parameter in parameters if parameter.location == "path"
            }
            if placeholders != declared_path:
                raise ValueError(
                    f"{operation_id} path parameters do not match template: "
                    f"expected {sorted(placeholders)}, got {sorted(declared_path)}"
                )
            raw_request = raw_operation.get("requestBody")
            request_body: dict[str, Any] | None = None
            mutually_exclusive: tuple[tuple[str, ...], ...] = ()
            if raw_request is not None:
                request = _resolve_component(
                    document, raw_request, "requestBodies", label=f"{operation_id}.requestBody"
                )
                request_content = request.get("content")
                if isinstance(request_content, dict):
                    for media_type in sorted(request_content):
                        media = request_content[media_type]
                        if not isinstance(media, dict) or not isinstance(media.get("schema"), dict):
                            continue
                        groups = _mutually_exclusive_groups(
                            document,
                            media["schema"],
                            label=f"{operation_id}.requestBody.{media_type}.schema",
                        )
                        if groups:
                            if mutually_exclusive and groups != mutually_exclusive:
                                raise ValueError(
                                    f"{operation_id} request body has inconsistent XOR metadata"
                                )
                            mutually_exclusive = groups
                request_body = {
                    "required": request.get("required") is True,
                    "content": _content_descriptor(
                        request_content, label=f"{operation_id}.requestBody"
                    ),
                }
            raw_responses = raw_operation.get("responses")
            if not isinstance(raw_responses, dict) or not raw_responses:
                raise ValueError(f"{operation_id}.responses must be a non-empty object")
            responses: dict[str, dict[str, Any]] = {}
            for status, raw_response in raw_responses.items():
                response = _resolve_component(
                    document, raw_response, "responses", label=f"{operation_id}.responses.{status}"
                )
                responses[str(status)] = {
                    "description": response.get("description", ""),
                    "content": _content_descriptor(
                        response.get("content"), label=f"{operation_id}.responses.{status}"
                    ),
                }
            operations[operation_id] = OperationSpec(
                operation_id=operation_id,
                path=path,
                method=method.upper(),
                parameters=tuple(parameters),
                request_body=request_body,
                responses=responses,
                mutually_exclusive=mutually_exclusive,
            )
    if set(operations) != EXPECTED_OPERATION_IDS:
        raise ValueError(f"unexpected Phase 1E operation ids: {sorted(operations)}")
    return operations


def _success_response(operation: OperationSpec) -> tuple[str, dict[str, Any]]:
    candidates = [
        (status, response)
        for status, response in operation.responses.items()
        if status.isdigit() and 200 <= int(status) < 300
    ]
    if not candidates:
        raise ValueError(f"{operation.operation_id} must declare a 2xx response")
    return min(candidates, key=lambda item: int(item[0]))


def _response_schema(
    response: dict[str, Any], media_type: str | None = None
) -> dict[str, Any] | None:
    content = response.get("content", {})
    if not isinstance(content, dict) or not content:
        return None
    selected = media_type if media_type in content else sorted(content)[0]
    media = content.get(selected)
    return media.get("schema") if isinstance(media, dict) else None


def _response_type(response: dict[str, Any]) -> str:
    schema = _response_schema(response)
    return _ts_type(schema) if isinstance(schema, dict) else "unknown"


def _py_response_type(response: dict[str, Any]) -> str:
    schema = _response_schema(response)
    return _py_type(schema) if isinstance(schema, dict) else "Any"


def _is_stream_operation(operation: OperationSpec) -> bool:
    return any(
        media_type == "text/event-stream"
        for response in operation.responses.values()
        for media_type in response.get("content", {})
    )


def _stream_response_status(operation: OperationSpec) -> str:
    candidates = [
        status
        for status, response in operation.responses.items()
        if status.isdigit()
        and 200 <= int(status) < 300
        and any(
            media_type.lower() == "text/event-stream" for media_type in response.get("content", {})
        )
    ]
    if candidates != ["200"]:
        raise ValueError(
            f"{operation.operation_id} must declare exactly one 200 text/event-stream response"
        )
    return "200"


def _receipt_response_status(operation: OperationSpec) -> str:
    response = operation.responses.get("202")
    content = response.get("content", {}) if isinstance(response, dict) else {}
    if not isinstance(content, dict) or not any(
        media_type.lower() == "application/json" for media_type in content
    ):
        raise ValueError(
            f"{operation.operation_id} must declare a 202 application/json receipt response"
        )
    return "202"


def _operation_metadata(operation: OperationSpec) -> dict[str, Any]:
    return {
        "method": operation.method,
        "pathTemplate": operation.path,
        "parameters": [
            {
                "name": parameter.name,
                "in": parameter.location,
                "required": parameter.required,
                "schema": _wire_schema(parameter.schema),
            }
            for parameter in operation.parameters
        ],
        "requestBody": operation.request_body,
        "mutuallyExclusive": [list(group) for group in operation.mutually_exclusive],
        "responses": operation.responses,
    }


def _camel(name: str) -> str:
    parts = re.split(r"[^A-Za-z0-9]+", name)
    if not parts:
        return name
    return parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:] if part)


def _snake(name: str) -> str:
    value = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value).replace("-", "_").lower()


def _pascal(name: str) -> str:
    value = _camel(name)
    return value[:1].upper() + value[1:]


def _ts_options_name(operation: OperationSpec) -> str:
    return {
        "listProjects": "ProjectListOptions",
        "listWorkspaceFiles": "WorkspaceFilesOptions",
        "listThreads": "ThreadListOptions",
        "runSessionStream": "RunSessionStreamOptions",
    }.get(operation.operation_id, f"{_pascal(operation.operation_id)}Options")


def _option_parameters(operation: OperationSpec) -> list[ParameterSpec]:
    return [parameter for parameter in operation.parameters if parameter.location == "query"]


def _header_parameter(operation: OperationSpec, name: str) -> ParameterSpec | None:
    return next(
        (
            parameter
            for parameter in operation.header_parameters
            if parameter.name.lower() == name.lower()
        ),
        None,
    )


def _accept_header(operation: OperationSpec) -> str:
    media_types: list[str] = []
    for response in operation.responses.values():
        for media_type in response.get("content", {}):
            if media_type not in media_types:
                media_types.append(media_type)
    return ", ".join(media_types) if media_types else "application/json"


def _request_media_type(operation: OperationSpec) -> str | None:
    if not operation.request_body:
        return None
    content = operation.request_body.get("content", {})
    return sorted(content)[0] if content else None


def _ts_param_options_lines(operation: OperationSpec) -> list[str]:
    parameters = _option_parameters(operation)
    has_idempotency = _header_parameter(operation, "Idempotency-Key") is not None
    has_last_event = _header_parameter(operation, "Last-Event-ID") is not None
    if not parameters and not has_idempotency and not has_last_event:
        return []
    name = _ts_options_name(operation)
    base = " extends Phase1ERequestOptions" if operation.operation_id == "runSessionStream" else ""
    lines = [f"export interface {name}{base} {{"]
    emitted: set[str] = set()
    for parameter in parameters:
        field = _camel(parameter.name)
        if field in emitted:
            raise ValueError(f"{operation.operation_id} option name collision: {field}")
        emitted.add(field)
        optional = "" if parameter.required else "?"
        lines.append(f"  {field}{optional}: {_ts_type(parameter.schema)};")
    if has_idempotency and "idempotencyKey" not in emitted and not base:
        lines.append("  idempotencyKey?: string;")
    if has_last_event and "lastEventId" not in emitted:
        lines.append("  lastEventId?: Cursor;")
    lines.extend(["}", ""])
    return lines


def _ts_models(document: dict[str, Any], digest: str) -> str:
    lines = [
        "/* eslint-disable */",
        "// GENERATED FILE - DO NOT EDIT. Source: sdk/protocol/schema/operant-phase1e.openapi.json",
        f"// Generated by sdk/protocol/generate_phase1e.py; schema digest: {digest}",
        "",
        "import {",
        "  Phase1EError,",
        "  Phase1ERequest,",
        "  Phase1EResponse,",
        "  Phase1ETransport,",
        "  fetchPhase1ETransport,",
        "  parseCursor,",
        "  parseSse,",
        "  readJson,",
        "  readText,",
        "  responseHeaders,",
        "  stringifyJson,",
        "} from './phase1e-transport';",
        "",
        f"export const PHASE1E_PROTOCOL_VERSION = {json.dumps(PROTOCOL_VERSION)} as const;",
        f"export const PHASE1E_SCHEMA_DIGEST = {json.dumps(digest)} as const;",
        f"export const PHASE1E_MAX_CURSOR = {MAX_CURSOR}n;",
        "",
        "/** All int64 values remain a safe number or a bigint; JSON parsing promotes large tokens. */",
        "export type Int64 = number | bigint;",
        "export type Cursor = Int64;",
        "",
    ]
    for name, schema in _schema_objects(document):
        if name == "Cursor":
            continue
        if schema.get("type") == "string" and isinstance(schema.get("enum"), list):
            values = " | ".join(_literal(value) for value in schema["enum"])
            lines.extend([f"export type {name} = {values};", ""])
            continue
        if "const" in schema and len(schema) <= 3:
            lines.extend([f"export type {name} = {_literal(schema['const'])};", ""])
            continue
        if schema.get("type") != "object" and "properties" not in schema:
            lines.extend([f"export type {name} = {_ts_type(schema)};", ""])
            continue
        lines.append(f"export interface {name} {{")
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        if isinstance(properties, dict):
            for property_name, property_schema in properties.items():
                if not isinstance(property_schema, dict):
                    continue
                optional = "" if property_name in required else "?"
                field = (
                    property_name
                    if _IDENTIFIER.fullmatch(property_name)
                    else json.dumps(property_name, ensure_ascii=False)
                )
                lines.append(
                    f"  {field}{optional}: {_ts_type(property_schema, property_name=property_name)};"
                )
        if schema.get("additionalProperties") is True:
            lines.append("  [key: string]: unknown;")
        lines.extend(["}", ""])
    return "\n".join(lines)


def _render_ts_method(operation: OperationSpec) -> list[str]:
    method_name = operation.operation_id
    path_args = [f"{_camel(parameter.name)}: string" for parameter in operation.path_parameters]
    body_schema = operation.request_body
    body_type = "unknown"
    if body_schema:
        content = body_schema.get("content", {})
        if content:
            body_schema_value = next(iter(content.values())).get("schema")
            if isinstance(body_schema_value, dict):
                body_type = _ts_type(body_schema_value)
    option_parameters = _option_parameters(operation)
    has_options = (
        bool(option_parameters)
        or bool(_header_parameter(operation, "Idempotency-Key"))
        or bool(_header_parameter(operation, "Last-Event-ID"))
    )
    option_name = _ts_options_name(operation)
    required_options = any(parameter.required for parameter in option_parameters)
    options_arg = ""
    if has_options:
        options_arg = f"options: {option_name}" + ("" if required_options else " = {}")
    args = path_args.copy()
    if body_schema:
        args.append(f"request: {body_type}")
    if options_arg:
        args.append(options_arg)
    _status, response = _success_response(operation)
    response_type = _response_type(response)
    return_type = "RunSessionStream" if _is_stream_operation(operation) else response_type
    lines = [f"  async {method_name}({', '.join(args)}): Promise<{return_type}> {{"]
    request_args: list[str] = []
    if operation.path_parameters:
        pairs = ", ".join(
            f"{json.dumps(parameter.name)}: pathPart({_camel(parameter.name)}, {json.dumps(parameter.name)})"
            for parameter in operation.path_parameters
        )
        request_args.append(f"pathParams: {{ {pairs} }}")
    if operation.query_parameters:
        query_entries = []
        for parameter in operation.query_parameters:
            field = _camel(parameter.name)
            value = f"options.{field}"
            if parameter.required:
                value = f"requireOption({value}, {parameter.name!r})"
            if _ref_name(parameter.schema) == "Cursor":
                value = (
                    f"cursorQuery({value})"
                    if parameter.required
                    else f"{value} === undefined ? undefined : cursorQuery({value})"
                )
            else:
                value = (
                    f"String({value})"
                    if parameter.required
                    else f"{value} === undefined ? undefined : String({value})"
                )
            query_entries.append(f"{json.dumps(parameter.name)}: {value}")
        request_args.append(f"query: {{ {', '.join(query_entries)} }}")
    header_entries: list[str] = [f"Accept: {json.dumps(_accept_header(operation))}"]
    if _header_parameter(operation, "Idempotency-Key"):
        header_entries.insert(
            0,
            "'Idempotency-Key': requireIdempotencyKey(options.idempotencyKey ?? newIdempotencyKey())",
        )
    if _header_parameter(operation, "Last-Event-ID"):
        header_entries.insert(
            0,
            "'Last-Event-ID': options.lastEventId === undefined ? undefined : cursorQuery(options.lastEventId)",
        )
    media_type = _request_media_type(operation)
    if media_type:
        header_entries.append(f"'Content-Type': {json.dumps(media_type)}")
    request_args.append(f"headers: {{ {', '.join(header_entries)} }}")
    if body_schema:
        request_args.append("bodyValue: request")
        request_args.append("body: stringifyJson(request)")
    lines.append(
        f"    const response = await this.requestOperation(PHASE1E_OPERATIONS[{json.dumps(operation.operation_id)}], {{ {', '.join(request_args)} }});"
    )
    if _is_stream_operation(operation):
        stream_status = _stream_response_status(operation)
        receipt_status = _receipt_response_status(operation)
        receipt_response = operation.responses[receipt_status]
        receipt_type = _response_type(receipt_response)
        lines.extend(
            [
                "    const metadata = this.responseMetadata ?? { status: response.status, idempotencyReplayed: false };",
                "    const contentType = responseHeaders(response.headers)['content-type']?.split(';', 1)[0]?.trim().toLowerCase();",
                f"    if (response.status === {receipt_status}) {{",
                "      if (contentType !== 'application/json') throw new Phase1EError('invalid_stream_response', '202 stream receipt must use application/json', false, 'none');",
                f"      return {{ receipt: await readJson<{receipt_type}>(response), metadata, events: (async function*() {{}})() }};",
                "    }",
                f"    if (response.status !== {stream_status} || contentType !== 'text/event-stream') throw new Phase1EError('invalid_stream_response', '200 stream response must use text/event-stream', false, 'none');",
                "    const source = response.body ?? await readText(response);",
                "    return { receipt: null, metadata, events: (async function*() {",
                "      for await (const frame of parseSse(source)) {",
                "        const id = frame.id === undefined ? null : parseCursor(frame.id);",
                "        yield { id, event: frame.event ?? 'message', data: frame.data, resource_scope: `session:${sessionId}`, stream_kind: 'session.run' };",
                "      }",
                "    })() };",
            ]
        )
    else:
        lines.append(f"    return readJson<{response_type}>(response);")
    lines.append("  }")
    return lines


def _render_ts_client(document: dict[str, Any], digest: str) -> str:
    # The digest is embedded in the model header; operation data is schema-derived.
    _ = digest
    operations = _operation_specs(document)
    metadata = {
        operation_id: _operation_metadata(operation)
        for operation_id, operation in operations.items()
    }
    metadata_json = json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=False)
    lines = [
        "type OperationParameter = { name: string; in: string; required: boolean; schema: Record<string, unknown> };",
        "type OperationDefinition = {",
        "  method: string; pathTemplate: string; parameters: readonly OperationParameter[];",
        "  requestBody: Record<string, unknown> | null; mutuallyExclusive: readonly (readonly string[])[];",
        "  responses: Record<string, Record<string, unknown>>;",
        "};",
        "",
        f"const PHASE1E_OPERATIONS: Record<string, OperationDefinition> = {metadata_json} as const;",
        "",
        "export interface ResponseMetadata {",
        "  status: number; idempotencyKey?: string; idempotencyReplayed: boolean;",
        "}",
        "export interface Phase1ERequestOptions { idempotencyKey?: string; }",
    ]
    for operation in operations.values():
        lines.extend(_ts_param_options_lines(operation))
    lines.extend(
        [
            "export interface RunSessionStream {",
            "  receipt: CommandReceipt | null; metadata: ResponseMetadata;",
            "  events: AsyncIterable<SseFrame>;",
            "}",
            "",
            "/** Deduplicate only a bounded recent window per resource and stream scope. */",
            "export class ScopedCursorTracker {",
            "  private readonly seen = new Map<string, Set<string>>();",
            "  private readonly order = new Map<string, string[]>();",
            "  private readonly latest = new Map<string, Cursor>();",
            "  private readonly scopeOrder: string[] = [];",
            "  constructor(private readonly maxSeenPerScope = 1024, private readonly maxScopes = 1024) {",
            "    if (!Number.isSafeInteger(maxSeenPerScope) || maxSeenPerScope < 1 || !Number.isSafeInteger(maxScopes) || maxScopes < 1) throw new RangeError('cursor tracker bounds must be positive safe integers');",
            "  }",
            "",
            "  accept(frame: SseFrame): boolean {",
            "    if (frame.id === null || frame.id === undefined) return true;",
            "    if (!frame.resource_scope || !frame.stream_kind) throw new TypeError('SSE scope is required');",
            "    const parsed = parseCursor(frame.id);",
            "    const key = `${frame.resource_scope}\\0${frame.stream_kind}`;",
            "    const cursor = BigInt(parsed).toString();",
            "    const scope = this.seen.get(key) ?? new Set<string>();",
            "    const order = this.order.get(key) ?? [];",
            "    if (!this.seen.has(key)) { this.scopeOrder.push(key); while (this.scopeOrder.length > this.maxScopes) { const evictedKey = this.scopeOrder.shift(); if (evictedKey !== undefined) { this.seen.delete(evictedKey); this.order.delete(evictedKey); this.latest.delete(evictedKey); } } }",
            "    if (scope.has(cursor)) return false;",
            "    scope.add(cursor); order.push(cursor);",
            "    while (order.length > this.maxSeenPerScope) { const evicted = order.shift(); if (evicted !== undefined) scope.delete(evicted); }",
            "    this.seen.set(key, scope); this.order.set(key, order);",
            "    const previous = this.latest.get(key);",
            "    if (previous === undefined || BigInt(parsed) > BigInt(previous)) this.latest.set(key, parsed);",
            "    return true;",
            "  }",
            "",
            "  last(scope: string, streamKind: string): Cursor | null {",
            "    if (!scope || !streamKind) throw new TypeError('SSE scope is required');",
            "    return this.latest.get(`${scope}\\0${streamKind}`) ?? null;",
            "  }",
            "}",
            "",
            "export class ProtocolNegotiationError extends Phase1EError {",
            "  constructor(message: string) { super('protocol_incompatible', message, false, 'refresh_and_retry'); this.name = 'ProtocolNegotiationError'; }",
            "}",
            "",
            "function validateProtocolNegotiation(value: unknown): ProtocolNegotiation {",
            "  if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new ProtocolNegotiationError('protocol response is not an object');",
            "  const metadata = value as Record<string, unknown>;",
            "  const required = ['protocol_version', 'schema_digest', 'min_client_version', 'capabilities'];",
            "  if (required.some(field => !Object.prototype.hasOwnProperty.call(metadata, field))) throw new ProtocolNegotiationError('protocol response is missing required metadata');",
            "  if (metadata.protocol_version !== PHASE1E_PROTOCOL_VERSION || metadata.min_client_version !== PHASE1E_PROTOCOL_VERSION) throw new ProtocolNegotiationError(`unsupported Core protocol: ${String(metadata.protocol_version)}`);",
            "  if (metadata.schema_digest !== PHASE1E_SCHEMA_DIGEST) throw new ProtocolNegotiationError(`Core Schema digest mismatch: expected ${PHASE1E_SCHEMA_DIGEST}, got ${String(metadata.schema_digest)}`);",
            "  if (!Array.isArray(metadata.capabilities) || metadata.capabilities.some(item => typeof item !== 'string' || item.length === 0)) throw new ProtocolNegotiationError('Core protocol capabilities are invalid');",
            "  return metadata as unknown as ProtocolNegotiation;",
            "}",
            "",
            "function newIdempotencyKey(): string {",
            "  const cryptoApi = globalThis.crypto;",
            "  if (typeof cryptoApi?.randomUUID === 'function') return cryptoApi.randomUUID();",
            "  if (typeof cryptoApi?.getRandomValues === 'function') {",
            "    const bytes = cryptoApi.getRandomValues(new Uint8Array(16));",
            "    bytes[6] = (bytes[6] & 0x0f) | 0x40; bytes[8] = (bytes[8] & 0x3f) | 0x80;",
            "    const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');",
            "    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;",
            "  }",
            "  throw new Phase1EError('crypto_unavailable', 'Secure idempotency key generation is unavailable', false, 'none');",
            "}",
            "",
            "function requireIdempotencyKey(value: string): string {",
            "  if (typeof value !== 'string' || !value) throw new TypeError('idempotencyKey must not be empty');",
            "  return value;",
            "}",
            "",
            "function cursorQuery(value: Cursor | undefined): string | undefined {",
            "  if (value === undefined) return undefined;",
            "  return parseCursor(value).toString();",
            "}",
            "",
            "function pathPart(value: string, label: string): string {",
            "  if (!value) throw new TypeError(`${label} must not be empty`);",
            "  return encodeURIComponent(value);",
            "}",
            "",
            "function validateMutuallyExclusive(request: unknown, groups: readonly (readonly string[])[]): void {",
            "  if (groups.length === 0) return;",
            "  if (typeof request !== 'object' || request === null || Array.isArray(request)) throw new TypeError('request body must be an object');",
            "  const record = request as Record<string, unknown>;",
            "  for (const group of groups) {",
            "    if (!Array.isArray(group) || group.length < 2 || group.some(field => typeof field !== 'string' || field.length === 0)) throw new TypeError('invalid mutually-exclusive schema metadata');",
            "    const provided = group.filter(field => record[field] !== undefined && record[field] !== null);",
            "    if (provided.length !== 1) throw new TypeError(`exactly one of ${group.join(', ')} is required`);",
            "  }",
            "}",
            "",
            "export class Phase1EClient {",
            "  readonly protocolVersion = PHASE1E_PROTOCOL_VERSION;",
            "  readonly schemaDigest = PHASE1E_SCHEMA_DIGEST;",
            "  private readonly baseUrl: string;",
            "  private readonly transport: Phase1ETransport;",
            "  private readonly clientVersion: string;",
            "  private negotiated: ProtocolNegotiation | null = null;",
            "  private responseMetadata: ResponseMetadata | null = null;",
            "",
            "  constructor(baseUrl = 'http://127.0.0.1:8000', transport: Phase1ETransport = fetchPhase1ETransport, clientVersion = PHASE1E_PROTOCOL_VERSION) {",
            "    this.baseUrl = baseUrl.replace(/\\/+$/, ''); this.transport = transport; this.clientVersion = clientVersion;",
            "  }",
            "",
            "  get lastResponse(): ResponseMetadata | null { return this.responseMetadata; }",
            "  get negotiatedProtocol(): ProtocolNegotiation | null { return this.negotiated; }",
            "",
            "  async negotiateProtocol(force = false): Promise<ProtocolNegotiation> {",
            "    if (this.negotiated && !force) return this.negotiated;",
            "    const response = await this.requestOperation(PHASE1E_OPERATIONS.negotiateProtocol, {}, true);",
            "    const metadata = validateProtocolNegotiation(await readJson<unknown>(response));",
            "    this.negotiated = metadata; return metadata;",
            "  }",
            "",
            "  private async requestOperation(operation: OperationDefinition, args: { pathParams?: Record<string, string>; query?: Record<string, string | undefined>; headers?: Record<string, string | undefined>; body?: string; bodyValue?: unknown; }, skipNegotiation = false): Promise<Phase1EResponse> {",
            "    validateMutuallyExclusive(args.bodyValue, operation.mutuallyExclusive);",
            "    const path = operation.pathTemplate.replace(/\\{([^}]+)\\}/g, (_match, name: string) => { const value = args.pathParams?.[name]; if (value === undefined) throw new TypeError(`missing path parameter: ${name}`); return value; });",
            "    const headers = Object.fromEntries(Object.entries(args.headers ?? {}).filter((entry): entry is [string, string] => entry[1] !== undefined));",
            "    return this.requestRaw({ method: operation.method, path, query: args.query, headers, body: args.body }, skipNegotiation);",
            "  }",
            "",
            "  private async requestRaw(request: Omit<Phase1ERequest, 'url'> & { path: string; query?: Record<string, string | undefined>; }, skipNegotiation = false): Promise<Phase1EResponse> {",
            "    if (!skipNegotiation) await this.negotiateProtocol();",
            "    const url = new URL(`${this.baseUrl}${request.path}`); for (const [key, value] of Object.entries(request.query ?? {})) if (value !== undefined) url.searchParams.set(key, value);",
            "    let response: Phase1EResponse;",
            "    try { response = await this.transport({ ...request, url: url.toString(), headers: { 'X-Operant-Client-Version': this.clientVersion, ...(request.headers ?? {}) } }); }",
            "    catch (error: unknown) { if (error instanceof Phase1EError) throw error; throw new Phase1EError('transport_unavailable', error instanceof Error ? error.message : 'transport request failed', true, 'retry_later'); }",
            "    const headers = responseHeaders(response.headers); this.responseMetadata = { status: response.status, idempotencyKey: headers['idempotency-key'], idempotencyReplayed: headers['idempotency-replayed']?.toLowerCase() === 'true' };",
            "    if (response.status < 200 || response.status >= 300) throw await Phase1EError.fromResponse(response); return response;",
            "  }",
            "",
        ]
    )
    if any(
        parameter.required
        for operation in operations.values()
        for parameter in operation.query_parameters
    ):
        insert_at = lines.index(
            "function cursorQuery(value: Cursor | undefined): string | undefined {"
        )
        lines[insert_at:insert_at] = [
            "function requireOption<T>(value: T | undefined, label: string): T {",
            "  if (value === undefined || value === null) throw new TypeError(`${label} is required`);",
            "  return value;",
            "}",
            "",
        ]
    for operation in operations.values():
        if operation.operation_id == "negotiateProtocol":
            continue
        lines.extend(_render_ts_method(operation))
        lines.append("")
    lines.extend(["}", "", "export { Phase1EError };", ""])
    return "\n".join(lines)


def _py_options_name(operation: OperationSpec) -> str:
    return {
        "listProjects": "ProjectListOptions",
        "listWorkspaceFiles": "WorkspaceFilesOptions",
        "listThreads": "ThreadListOptions",
        "runSessionStream": "RunSessionStreamOptions",
    }.get(operation.operation_id, f"{_pascal(operation.operation_id)}Options")


def _render_py_method(operation: OperationSpec) -> list[str]:
    method_name = _snake(operation.operation_id)
    args: list[str] = ["self"]
    for parameter in operation.path_parameters:
        args.append(f"{parameter.name}: str")
    body_schema = operation.request_body
    body_type = "dict[str, Any]"
    if body_schema:
        content = body_schema.get("content", {})
        if content:
            body_value = next(iter(content.values())).get("schema")
            if isinstance(body_value, dict):
                body_type = _py_type(body_value)
        args.append(f"request: {body_type}")
    query_params = operation.query_parameters
    header_idempotency = _header_parameter(operation, "Idempotency-Key")
    header_last_event = _header_parameter(operation, "Last-Event-ID")
    if query_params:
        args.append("*")
        for parameter in query_params:
            type_name = _py_type(parameter.schema)
            if not parameter.required:
                type_name = f"{type_name} | None"
            default = "" if parameter.required else " = None"
            args.append(f"{parameter.name}: {type_name}{default}")
    elif header_idempotency or header_last_event:
        args.append("*")
    if header_idempotency:
        args.append("idempotency_key: str | None = None")
    if header_last_event:
        args.append("last_event_id: int | None = None")
    _status, response = _success_response(operation)
    response_type = _py_response_type(response)
    return_type = "RunSessionStream" if _is_stream_operation(operation) else response_type
    lines = [f"    def {method_name}({', '.join(args)}) -> {return_type}:"]
    request_args: list[str] = [repr(operation.operation_id)]
    if operation.path_parameters:
        entries = ", ".join(
            f"{parameter.name!r}: _required_path({parameter.name}, {parameter.name!r})"
            for parameter in operation.path_parameters
        )
        request_args.append(f"path_params={{ {entries} }}")
    if query_params:
        entries = []
        for parameter in query_params:
            value = parameter.name
            if parameter.required:
                value = f"_required_option({value}, {parameter.name!r})"
            if _ref_name(parameter.schema) == "Cursor":
                value = (
                    f"_cursor_value({value})" if parameter.required else f"_cursor_value({value})"
                )
            elif parameter.required:
                value = f"str({value})"
            else:
                value = f"None if {value} is None else str({value})"
            entries.append(f"{parameter.name!r}: {value}")
        request_args.append(f"query={{ {', '.join(entries)} }}")
    header_entries: list[str] = []
    if header_idempotency:
        header_entries.append(
            "'Idempotency-Key': _require_idempotency_key(idempotency_key if idempotency_key is not None else _new_idempotency_key())"
        )
    if header_last_event:
        header_entries.append("'Last-Event-ID': _cursor_value(last_event_id)")
    if header_entries:
        request_args.append(f"headers={{ {', '.join(header_entries)} }}")
    request_args.append(f"accept={_accept_header(operation)!r}")
    if body_schema:
        request_args.append("body=dict(request)")
    lines.append(f"        response = self._request_operation({', '.join(request_args)})")
    if _is_stream_operation(operation):
        stream_status = _stream_response_status(operation)
        receipt_status = _receipt_response_status(operation)
        lines.extend(
            [
                "        metadata = self.last_response or ResponseMetadata(response.status)",
                "        content_type = response_headers(response.headers).get('content-type', '').split(';', 1)[0].strip().lower()",
                f"        if response.status == {receipt_status}:",
                "            if content_type != 'application/json':",
                "                raise Phase1EError('invalid_stream_response', '202 stream receipt must use application/json', recovery='none')",
                "            receipt = cast(CommandReceipt, response_json(response))",
                "            return RunSessionStream(receipt=receipt, events=iter(()), metadata=metadata)",
                f"        if response.status != {stream_status} or content_type != 'text/event-stream':",
                "            raise Phase1EError('invalid_stream_response', '200 stream response must use text/event-stream', recovery='none')",
                "        source = response.body if response.body is not None else response_text(response)",
                "        frames = parse_sse(source)",
                "        def events() -> Iterator[SseFrame]:",
                "            for frame in frames:",
                "                yield {'id': _parse_cursor_id(frame.get('id')), 'event': frame.get('event', 'message'), 'data': frame.get('data'), 'resource_scope': f'session:{session_id}', 'stream_kind': 'session.run'}",
                "        return RunSessionStream(receipt=None, events=events(), metadata=metadata)",
            ]
        )
    else:
        lines.append(f"        return cast({response_type}, response_json(response))")
    return lines


def _render_py_models(document: dict[str, Any], digest: str) -> str:
    operations = _operation_specs(document)
    metadata = {
        operation_id: _operation_metadata(operation)
        for operation_id, operation in operations.items()
    }
    lines = [
        '"""Generated Phase 1E models and synchronous client. DO NOT EDIT."""',
        "",
        "# fmt: off",
        "# Source: sdk/protocol/schema/operant-phase1e.openapi.json",
        "# ruff: noqa: E501",
        f"# Generated by sdk/protocol/generate_phase1e.py; schema digest: {digest}",
        "",
        "import json",
        "import re",
        "import uuid",
        "from collections import deque",
        "from collections.abc import Iterator",
        "from dataclasses import dataclass",
        "from typing import Any, Literal, cast",
        "",
        "from typing_extensions import NotRequired, TypedDict",
        "",
        "from .transport import (",
        "    Phase1EError,",
        "    Transport,",
        "    TransportRequest,",
        "    default_transport,",
        "    parse_sse,",
        "    response_headers,",
        "    response_json,",
        "    response_text,",
        ")",
        "",
        f"PHASE1E_PROTOCOL_VERSION = {PROTOCOL_VERSION!r}",
        f"PHASE1E_SCHEMA_DIGEST = {digest!r}",
        f"PHASE1E_MAX_CURSOR = {MAX_CURSOR}",
        "Int64 = int",
        "Cursor = Int64",
        "",
    ]
    for name, schema in _schema_objects(document):
        if name == "Cursor":
            continue
        if schema.get("type") == "string" and isinstance(schema.get("enum"), list):
            values = ", ".join(repr(value) for value in schema["enum"])
            lines.extend([f"{name} = Literal[{values}]", ""])
            continue
        if "const" in schema and len(schema) <= 3:
            lines.extend([f"{name} = Literal[{schema['const']!r}]", ""])
            continue
        if schema.get("type") != "object" and "properties" not in schema:
            lines.extend([f"{name} = {_py_type(schema)}", ""])
            continue
        lines.append(f"class {name}(TypedDict):")
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        if isinstance(properties, dict) and properties:
            for property_name, property_schema in properties.items():
                if not isinstance(property_schema, dict):
                    continue
                rendered = _py_type(property_schema, property_name=property_name)
                if property_name not in required:
                    rendered = f"NotRequired[{rendered}]"
                field = (
                    property_name if _IDENTIFIER.fullmatch(property_name) else repr(property_name)
                )
                lines.append(f"    {field}: {rendered}")
        else:
            lines.append("    _empty: NotRequired[Any]")
        lines.append("")
    lines.extend(
        [
            "@dataclass(frozen=True)",
            "class ResponseMetadata:",
            "    status: int",
            "    idempotency_key: str | None = None",
            "    idempotency_replayed: bool = False",
            "",
            "@dataclass(frozen=True)",
            "class RunSessionStream:",
            "    receipt: CommandReceipt | None",
            "    events: Iterator[SseFrame]",
            "    metadata: ResponseMetadata",
            "",
            f"OPERATION_DEFINITIONS: dict[str, dict[str, Any]] = {repr(metadata)}",
            "",
            "_PATH_PARAMETER = re.compile(r'\\{([^}]+)\\}')",
            "",
            "class ProtocolNegotiationError(Phase1EError):",
            "    def __init__(self, message: str) -> None:",
            "        super().__init__('protocol_incompatible', message, recovery='refresh_and_retry')",
            "",
            "class ScopedCursorTracker:",
            '    """Deduplicate a bounded recent window per resource and stream scope."""',
            "",
            "    def __init__(self, max_seen_per_scope: int = 1024, max_scopes: int = 1024) -> None:",
            "        if isinstance(max_seen_per_scope, bool) or not isinstance(max_seen_per_scope, int) or max_seen_per_scope < 1 or isinstance(max_scopes, bool) or not isinstance(max_scopes, int) or max_scopes < 1:",
            "            raise ValueError('cursor tracker bounds must be positive integers')",
            "        self._max_seen_per_scope = max_seen_per_scope",
            "        self._max_scopes = max_scopes",
            "        self._seen: dict[tuple[str, str], set[int]] = {}",
            "        self._order: dict[tuple[str, str], deque[int]] = {}",
            "        self._latest: dict[tuple[str, str], int] = {}",
            "        self._scope_order: deque[tuple[str, str]] = deque()",
            "",
            "    def accept(self, frame: SseFrame) -> bool:",
            "        cursor = frame.get('id')",
            "        if cursor is None:",
            "            return True",
            "        parsed = _cursor_value_int(cursor)",
            "        scope = frame.get('resource_scope')",
            "        stream_kind = frame.get('stream_kind')",
            "        if not isinstance(scope, str) or not isinstance(stream_kind, str) or not scope or not stream_kind:",
            "            raise TypeError('SSE scope is required')",
            "        key = (scope, stream_kind)",
            "        seen = self._seen.setdefault(key, set())",
            "        order = self._order.setdefault(key, deque())",
            "        if key not in self._scope_order:",
            "            self._scope_order.append(key)",
            "            while len(self._scope_order) > self._max_scopes:",
            "                evicted_key = self._scope_order.popleft()",
            "                self._seen.pop(evicted_key, None)",
            "                self._order.pop(evicted_key, None)",
            "                self._latest.pop(evicted_key, None)",
            "        if parsed in seen:",
            "            return False",
            "        seen.add(parsed)",
            "        order.append(parsed)",
            "        while len(order) > self._max_seen_per_scope:",
            "            seen.discard(order.popleft())",
            "        latest = self._latest.get(key)",
            "        if latest is None or parsed > latest:",
            "            self._latest[key] = parsed",
            "        return True",
            "",
            "    def last(self, scope: str, stream_kind: str) -> int | None:",
            "        if not scope or not stream_kind:",
            "            raise TypeError('SSE scope is required')",
            "        return self._latest.get((scope, stream_kind))",
            "",
            "def _new_idempotency_key() -> str:",
            "    return f'phase1e-{uuid.uuid4()}'",
            "",
            "def _require_idempotency_key(value: str) -> str:",
            "    if not isinstance(value, str) or not value:",
            "        raise ValueError('idempotency_key must not be empty')",
            "    return value",
            "",
            "def _cursor_value_int(value: object) -> int:",
            "    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= PHASE1E_MAX_CURSOR:",
            "        raise ValueError('cursor must be a non-bool integer between 0 and 2^63-1')",
            "    return value",
            "",
            "def _cursor_value(value: int | None) -> str | None:",
            "    if value is None:",
            "        return None",
            "    return str(_cursor_value_int(value))",
            "",
            "def _required_option(value: object, label: str) -> object:",
            "    if value is None:",
            "        raise ValueError(f'{label} is required')",
            "    return value",
            "",
            "def _required_path(value: str, label: str) -> str:",
            "    if not isinstance(value, str) or not value:",
            "        raise ValueError(f'{label} must not be empty')",
            "    from urllib.parse import quote",
            "    return quote(value, safe='')",
            "",
            "def _parse_cursor_id(value: object) -> int | None:",
            "    if value is None:",
            "        return None",
            "    if not isinstance(value, str) or not re.fullmatch(r'[0-9]+', value):",
            "        raise ValueError('SSE id must be a decimal cursor')",
            "    return _cursor_value_int(int(value))",
            "",
            "def _validate_mutually_exclusive(request: object, groups: object) -> None:",
            "    if not groups:",
            "        return",
            "    if not isinstance(request, dict):",
            "        raise TypeError('request body must be an object')",
            "    if not isinstance(groups, list):",
            "        raise TypeError('invalid mutually-exclusive schema metadata')",
            "    for group in groups:",
            "        if (not isinstance(group, list) or len(group) < 2 or",
            "                any(not isinstance(field, str) or not field for field in group)):",
            "            raise TypeError('invalid mutually-exclusive schema metadata')",
            "        provided = [field for field in group if request.get(field) is not None]",
            "        if len(provided) != 1:",
            "            names = ', '.join(group)",
            "            raise ValueError(f'exactly one of {names} is required')",
            "",
            "def _validate_protocol_negotiation(value: object) -> ProtocolNegotiation:",
            "    if not isinstance(value, dict):",
            "        raise ProtocolNegotiationError('protocol response is not an object')",
            "    required = ('protocol_version', 'schema_digest', 'min_client_version', 'capabilities')",
            "    if any(field not in value for field in required):",
            "        raise ProtocolNegotiationError('protocol response is missing required metadata')",
            "    if value.get('protocol_version') != PHASE1E_PROTOCOL_VERSION or value.get('min_client_version') != PHASE1E_PROTOCOL_VERSION:",
            "        raise ProtocolNegotiationError(f'unsupported Core protocol: {value.get(\"protocol_version\")}')",
            "    if value.get('schema_digest') != PHASE1E_SCHEMA_DIGEST:",
            "        raise ProtocolNegotiationError(f'Core Schema digest mismatch: expected {PHASE1E_SCHEMA_DIGEST}, got {value.get(\"schema_digest\")}')",
            "    capabilities = value.get('capabilities')",
            "    if (not isinstance(capabilities, list) or",
            "            any(not isinstance(item, str) or not item for item in capabilities)):",
            "        raise ProtocolNegotiationError('Core protocol capabilities are invalid')",
            "    return cast(ProtocolNegotiation, value)",
            "",
            "class Phase1EClient:",
            "    protocol_version = PHASE1E_PROTOCOL_VERSION",
            "    schema_digest = PHASE1E_SCHEMA_DIGEST",
            "",
            "    def __init__(self, base_url: str = 'http://127.0.0.1:8000', transport: Transport = default_transport, client_version: str = PHASE1E_PROTOCOL_VERSION) -> None:",
            "        self._base_url = base_url.rstrip('/')",
            "        self._transport = transport",
            "        self._client_version = client_version",
            "        self._negotiated: ProtocolNegotiation | None = None",
            "        self.last_response: ResponseMetadata | None = None",
            "",
            "    @property",
            "    def negotiated_protocol(self) -> ProtocolNegotiation | None:",
            "        return self._negotiated",
            "",
            "    def _request_operation(self, operation_id: str, *, path_params: dict[str, str] | None = None, query: dict[str, str | None] | None = None, headers: dict[str, str | None] | None = None, body: dict[str, Any] | None = None, accept: str = 'application/json', skip_negotiation: bool = False) -> Any:",
            "        operation = OPERATION_DEFINITIONS.get(operation_id)",
            "        if operation is None:",
            "            raise ValueError(f'unknown protocol operation: {operation_id}')",
            "        _validate_mutually_exclusive(body, operation.get('mutuallyExclusive', []))",
            "        values = path_params or {}",
            "        def replace(match: re.Match[str]) -> str:",
            "            name = match.group(1)",
            "            value = values.get(name)",
            "            if value is None:",
            "                raise ValueError(f'missing path parameter: {name}')",
            "            return value",
            "        path = _PATH_PARAMETER.sub(replace, str(operation['pathTemplate']))",
            "        if not skip_negotiation:",
            "            self.negotiate_protocol()",
            "        outgoing_headers = {'Accept': accept, 'X-Operant-Client-Version': self._client_version}",
            "        outgoing_headers.update({key: value for key, value in (headers or {}).items() if value is not None})",
            "        payload = None",
            "        if body is not None:",
            "            outgoing_headers.setdefault('Content-Type', 'application/json')",
            "            payload = json.dumps(body, ensure_ascii=False, separators=(',', ':'))",
            "        from urllib.parse import urlencode",
            "        query_values = {key: value for key, value in (query or {}).items() if value is not None}",
            "        suffix = f'?{urlencode(query_values)}' if query_values else ''",
            "        request = TransportRequest(method=str(operation['method']), url=f'{self._base_url}{path}{suffix}', headers=outgoing_headers, body=payload)",
            "        try:",
            "            response = self._transport(request)",
            "        except Phase1EError:",
            "            raise",
            "        except Exception as error:",
            "            raise Phase1EError('transport_unavailable', str(error) or 'transport request failed', retryable=True, recovery='retry_later') from error",
            "        values = response_headers(response.headers)",
            "        self.last_response = ResponseMetadata(status=response.status, idempotency_key=values.get('idempotency-key'), idempotency_replayed=values.get('idempotency-replayed', '').lower() == 'true')",
            "        if not 200 <= response.status < 300:",
            "            raise Phase1EError.from_response(response)",
            "        return response",
            "",
            "    def negotiate_protocol(self, *, force: bool = False) -> ProtocolNegotiation:",
            "        if self._negotiated is not None and not force:",
            "            return self._negotiated",
            "        response = self._request_operation('negotiateProtocol', skip_negotiation=True)",
            "        metadata = _validate_protocol_negotiation(response_json(response))",
            "        self._negotiated = metadata",
            "        return self._negotiated",
            "",
        ]
    )
    for operation in operations.values():
        if operation.operation_id == "negotiateProtocol":
            continue
        lines.extend(_render_py_method(operation))
        lines.append("")
    lines.extend(
        [
            "    # The camelCase spellings mirror the OpenAPI operationIds used by the TS client.",
            "    negotiateProtocol = negotiate_protocol",
            "    listProjects = list_projects",
            "    listWorkspaceFiles = list_workspace_files",
            "    listThreads = list_threads",
            "    createSession = create_session",
            "    runSessionStream = run_session_stream",
            "    listPendingApprovals = list_pending_approvals",
            "    submitApproval = submit_approval",
            "",
            "# fmt: on",
            "",
        ]
    )
    return "\n".join(lines)


def _validate_document(document: dict[str, Any]) -> None:
    if document.get("openapi") != "3.1.0":
        raise ValueError("Phase 1E Schema must use OpenAPI 3.1")
    info = document.get("info")
    if not isinstance(info, dict) or info.get("version") != PROTOCOL_VERSION:
        raise ValueError("Phase 1E Schema must declare phase1e.v1")
    operations = _operation_specs(document)
    serialized = _canonical_json(document).decode("utf-8").lower()
    for excluded in ("graph", "remote", "team", "scheduler", "oauth", "tui", "tauri"):
        if excluded in serialized:
            raise ValueError(f"excluded capability appears in formal Schema: {excluded}")
    for operation in operations.values():
        if not operation.path.startswith("/"):
            raise ValueError(f"{operation.operation_id} path must be absolute")
        _success_response(operation)
        if operation.request_body and not operation.request_body.get("content"):
            raise ValueError(f"{operation.operation_id} request body has no content")


def _python_package_init_before_phase56() -> str:
    """Render the package entry point shared by the frozen protocol lines."""

    return '''"""Generated Operant Python SDK entry points."""\n\nfrom .phase1e_generated import (\n    PHASE1E_MAX_CURSOR,\n    PHASE1E_PROTOCOL_VERSION,\n    PHASE1E_SCHEMA_DIGEST,\n    Phase1EClient,\n    ProtocolNegotiationError,\n)\nfrom .phase23_generated import (\n    PHASE23_MAX_CURSOR,\n    PHASE23_PROTOCOL_VERSION,\n    PHASE23_SCHEMA_DIGEST,\n    Phase23Client,\n)\nfrom .phase23_generated import (\n    ProtocolNegotiationError as Phase23ProtocolNegotiationError,\n)\nfrom .phase45_generated import (\n    PHASE45_MAX_CURSOR,\n    PHASE45_PROTOCOL_VERSION,\n    PHASE45_SCHEMA_DIGEST,\n    Phase45Client,\n)\nfrom .phase45_generated import (\n    ProtocolNegotiationError as Phase45ProtocolNegotiationError,\n)\nfrom .transport import Phase23Error\n\n__all__ = [\n    "PHASE1E_MAX_CURSOR",\n    "PHASE1E_PROTOCOL_VERSION",\n    "PHASE1E_SCHEMA_DIGEST",\n    "PHASE23_MAX_CURSOR",\n    "PHASE23_PROTOCOL_VERSION",\n    "PHASE23_SCHEMA_DIGEST",\n    "PHASE45_MAX_CURSOR",\n    "PHASE45_PROTOCOL_VERSION",\n    "PHASE45_SCHEMA_DIGEST",\n    "Phase1EClient",\n    "Phase23Client",\n    "Phase23Error",\n    "Phase23ProtocolNegotiationError",\n    "Phase45Client",\n    "Phase45ProtocolNegotiationError",\n    "ProtocolNegotiationError",\n]\n'''


def _python_package_init() -> str:
    """Keep frozen clients while exposing every additive protocol line."""

    source = _python_package_init_before_phase56()
    source = source.replace(
        "from .transport import Phase23Error\n",
        "from .phase56_generated import (\n"
        "    PHASE56_MAX_CURSOR,\n"
        "    PHASE56_PROTOCOL_VERSION,\n"
        "    PHASE56_SCHEMA_DIGEST,\n"
        "    Phase56Client,\n"
        ")\n"
        "from .phase56_generated import (\n"
        "    ProtocolNegotiationError as Phase56ProtocolNegotiationError,\n"
        ")\n"
        "from .transport import Phase23Error\n",
        1,
    )
    source = source.replace(
        '    "PHASE45_SCHEMA_DIGEST",\n',
        '    "PHASE45_SCHEMA_DIGEST",\n'
        '    "PHASE56_MAX_CURSOR",\n'
        '    "PHASE56_PROTOCOL_VERSION",\n'
        '    "PHASE56_SCHEMA_DIGEST",\n',
        1,
    )
    return source.replace(
        '    "Phase45ProtocolNegotiationError",\n',
        '    "Phase45ProtocolNegotiationError",\n'
        '    "Phase56Client",\n'
        '    "Phase56ProtocolNegotiationError",\n',
        1,
    )


def generate() -> str:
    document = _read_schema()
    _validate_document(document)
    digest = _digest(document)
    _write_if_changed(DIGEST_PATH, f"{digest}  {SCHEMA_PATH.name}\n")
    _write_if_changed(
        TS_PATH, _ts_models(document, digest) + "\n" + _render_ts_client(document, digest)
    )
    _write_if_changed(PY_PATH, _render_py_models(document, digest))
    _write_if_changed(PY_INIT_PATH, _python_package_init())
    return digest


if __name__ == "__main__":
    print(generate())
