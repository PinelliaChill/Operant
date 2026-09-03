#!/usr/bin/env python3
"""Generate additive Phase 2/3 clients without modifying Phase 1E artifacts."""

# Generated output contains intentionally long protocol declarations, and the
# repository root is inserted before importing the frozen Phase 1E generator.
# ruff: noqa: E402, E501

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sdk.protocol import generate_phase1e as base

SCHEMA_PATH = ROOT / "sdk/protocol/schema/operant-phase23.openapi.json"
DIGEST_PATH = ROOT / "sdk/protocol/schema/operant-phase23.openapi.sha256"
TS_PATH = ROOT / "sdk/typescript-client/phase23.generated.ts"
PY_PATH = ROOT / "sdk/python_client/phase23_generated.py"
PROTOCOL_VERSION = "phase23.v1"

EXPECTED_OPERATION_IDS = {
    "negotiateProtocol",
    "createWorkflowDraft",
    "compileWorkflowDraft",
    "publishWorkflow",
    "getWorkflowDefinition",
    "startGraphRun",
    "getGraphRun",
    "getGraphRunByLegacyWorkflow",
    "listNodeRuns",
    "streamGraphRunEvents",
    "resumeGraphRun",
    "cancelGraphRun",
    "provideNodeInput",
    "createTeamDefinition",
    "startTeamRun",
    "getTeamRun",
    "sendTeamMessage",
    "listTeamMessages",
    "getMailbox",
    "acknowledgeMailboxDelivery",
    "getTaskBoard",
    "updateTeamTask",
    "getArtifactBoard",
    "publishArtifactBoardItem",
    "streamTeamRunEvents",
}

_STREAM_METADATA: dict[str, tuple[str, str]] = {}


def _configure_base() -> None:
    base.SCHEMA_PATH = SCHEMA_PATH
    base.DIGEST_PATH = DIGEST_PATH
    base.TS_PATH = TS_PATH
    base.PY_PATH = PY_PATH
    base.PROTOCOL_VERSION = PROTOCOL_VERSION
    base.EXPECTED_OPERATION_IDS = EXPECTED_OPERATION_IDS


def _validate_document(document: dict[str, Any]) -> None:
    if document.get("openapi") != "3.1.0":
        raise ValueError("Phase 2/3 Schema must use OpenAPI 3.1")
    info = document.get("info")
    if not isinstance(info, dict) or info.get("version") != PROTOCOL_VERSION:
        raise ValueError("Phase 2/3 Schema must declare phase23.v1")
    operations = base._operation_specs(document)
    for operation in operations.values():
        if not operation.path.startswith("/v1/"):
            raise ValueError(f"{operation.operation_id} must use a versioned Core path")
        base._success_response(operation)
        if operation.request_body and not operation.request_body.get("content"):
            raise ValueError(f"{operation.operation_id} request body has no content")
    for path_item in document["paths"].values():
        for operation_document in path_item.values():
            if not isinstance(operation_document, dict) or "operationId" not in operation_document:
                continue
            extension = operation_document.get("x-sse")
            if extension is None:
                continue
            if not isinstance(extension, dict):
                raise ValueError("x-sse must be an object")
            resource_scope = extension.get("resource_scope")
            stream_kind = extension.get("stream_kind")
            if not isinstance(resource_scope, str) or not isinstance(stream_kind, str):
                raise ValueError("x-sse must declare resource_scope and stream_kind")
            _STREAM_METADATA[operation_document["operationId"]] = (resource_scope, stream_kind)


def _ts_stream_method(operation: base.OperationSpec) -> list[str]:
    if not base._is_stream_operation(operation):
        return _ORIGINAL_TS_METHOD(operation)
    resource_scope, stream_kind = _STREAM_METADATA[operation.operation_id]
    path_args = [
        f"{base._camel(parameter.name)}: string" for parameter in operation.path_parameters
    ]
    option_name = base._ts_options_name(operation)
    path_args.append(f"options: {option_name} = {{}}")
    pairs = ", ".join(
        f"{parameter.name!r}: pathPart({base._camel(parameter.name)}, {parameter.name!r})"
        for parameter in operation.path_parameters
    )
    scope_expression = resource_scope
    for parameter in operation.path_parameters:
        scope_expression = scope_expression.replace(
            "{" + parameter.name + "}", "${" + base._camel(parameter.name) + "}"
        )
    return [
        f"  async {operation.operation_id}({', '.join(path_args)}): Promise<RunSessionStream> {{",
        f"    const response = await this.requestOperation(PHASE1E_OPERATIONS[{operation.operation_id!r}], {{ pathParams: {{ {pairs} }}, headers: {{ 'Last-Event-ID': options.lastEventId === undefined ? undefined : cursorQuery(options.lastEventId), Accept: 'text/event-stream' }} }});",
        "    const metadata = this.responseMetadata ?? { status: response.status, idempotencyReplayed: false };",
        "    const contentType = responseHeaders(response.headers)['content-type']?.split(';', 1)[0]?.trim().toLowerCase();",
        "    if (response.status !== 200 || contentType !== 'text/event-stream') throw new Phase1EError('invalid_stream_response', '200 stream response must use text/event-stream', false, 'none');",
        "    const source = response.body ?? await readText(response);",
        "    return { receipt: null, metadata, events: (async function*() {",
        "      for await (const frame of parseSse(source)) {",
        "        const id = frame.id === undefined ? null : parseCursor(frame.id);",
        f"        yield {{ id, event: frame.event ?? 'message', data: frame.data, resource_scope: `{scope_expression}`, stream_kind: {stream_kind!r} }};",
        "      }",
        "    })() };",
        "  }",
    ]


def _py_stream_method(operation: base.OperationSpec) -> list[str]:
    if not base._is_stream_operation(operation):
        return _ORIGINAL_PY_METHOD(operation)
    resource_scope, stream_kind = _STREAM_METADATA[operation.operation_id]
    args = ["self"] + [f"{parameter.name}: str" for parameter in operation.path_parameters]
    args.extend(["*", "last_event_id: int | None = None"])
    path_entries = ", ".join(
        f"{parameter.name!r}: _required_path({parameter.name}, {parameter.name!r})"
        for parameter in operation.path_parameters
    )
    scope_expression = resource_scope
    for parameter in operation.path_parameters:
        scope_expression = scope_expression.replace(
            "{" + parameter.name + "}", "{" + parameter.name + "}"
        )
    return [
        f"    def {base._snake(operation.operation_id)}({', '.join(args)}) -> RunSessionStream:",
        f"        response = self._request_operation({operation.operation_id!r}, path_params={{ {path_entries} }}, headers={{ 'Last-Event-ID': _cursor_value(last_event_id) }}, accept='text/event-stream')",
        "        metadata = self.last_response or ResponseMetadata(response.status)",
        "        content_type = response_headers(response.headers).get('content-type', '').split(';', 1)[0].strip().lower()",
        "        if response.status != 200 or content_type != 'text/event-stream':",
        "            raise Phase1EError('invalid_stream_response', '200 stream response must use text/event-stream', recovery='none')",
        "        source = response.body if response.body is not None else response_text(response)",
        "        frames = parse_sse(source)",
        "        def events() -> Iterator[SseFrame]:",
        "            for frame in frames:",
        f"                yield {{'id': _parse_cursor_id(frame.get('id')), 'event': frame.get('event', 'message'), 'data': frame.get('data'), 'resource_scope': f'{scope_expression}', 'stream_kind': {stream_kind!r}}}",
        "        return RunSessionStream(receipt=None, events=events(), metadata=metadata)",
    ]


_ORIGINAL_TS_METHOD = base._render_ts_method
_ORIGINAL_PY_METHOD = base._render_py_method


def _phase23_names(source: str) -> str:
    renamed = (
        source.replace("PHASE1E", "PHASE23")
        .replace("Phase1E", "Phase23")
        .replace("phase1e", "phase23")
        .replace("Phase 1E", "Phase 2/3")
    )
    if "function pathPart" in renamed:
        renamed = renamed.replace("version: string", "version: number")
        renamed = renamed.replace(
            "function pathPart(value: string, label: string): string {\n"
            "  if (!value) throw new TypeError(`${label} must not be empty`);\n"
            "  return encodeURIComponent(value);",
            "function pathPart(value: string | number, label: string): string {\n"
            "  if (value === '' || !Number.isFinite(typeof value === 'number' ? value : 0)) "
            "throw new TypeError(`${label} must be a valid path value`);\n"
            "  return encodeURIComponent(String(value));",
        )
    if "def _required_path" in renamed:
        renamed = renamed.replace("version: str", "version: int")
        renamed = renamed.replace("client_version: int", "client_version: str")
        renamed = renamed.replace("client_version: int", "client_version: str")
        renamed = renamed.replace(
            "def _required_path(value: str, label: str) -> str:\n"
            "    if not isinstance(value, str) or not value:\n"
            "        raise ValueError(f'{label} must not be empty')\n"
            "    from urllib.parse import quote\n"
            "    return quote(value, safe='')",
            "def _required_path(value: str | int, label: str) -> str:\n"
            "    if isinstance(value, bool) or not isinstance(value, (str, int)) or value == '':\n"
            "        raise ValueError(f'{label} must be a valid path value')\n"
            "    from urllib.parse import quote\n"
            "    return quote(str(value), safe='')",
        )
    return renamed


def _python_aliases(source: str, operations: dict[str, base.OperationSpec]) -> str:
    marker = "    # The camelCase spellings mirror the OpenAPI operationIds used by the TS client."
    start = source.index(marker)
    end = source.index("# fmt: on", start)
    aliases = [marker]
    for operation_id in operations:
        aliases.append(f"    {operation_id} = {base._snake(operation_id)}")
    aliases.extend(["", ""])
    return source[:start] + "\n".join(aliases) + source[end:]


def generate() -> str:
    previous = (
        base.SCHEMA_PATH,
        base.DIGEST_PATH,
        base.TS_PATH,
        base.PY_PATH,
        base.PROTOCOL_VERSION,
        base.EXPECTED_OPERATION_IDS,
        base._render_ts_method,
        base._render_py_method,
    )
    _configure_base()
    _STREAM_METADATA.clear()
    try:
        document = base._read_schema()
        _validate_document(document)
        operations = base._operation_specs(document)
        base._render_ts_method = _ts_stream_method
        base._render_py_method = _py_stream_method
        digest = base._digest(document)
        base._write_if_changed(DIGEST_PATH, f"{digest}  {SCHEMA_PATH.name}\n")
        ts_source = (
            base._ts_models(document, digest) + "\n" + base._render_ts_client(document, digest)
        )
        base._write_if_changed(TS_PATH, _phase23_names(ts_source))
        py_source = _phase23_names(base._render_py_models(document, digest))
        base._write_if_changed(PY_PATH, _python_aliases(py_source, operations))
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
            base._render_ts_method,
            base._render_py_method,
        ) = previous


if __name__ == "__main__":
    print(generate())
