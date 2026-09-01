#!/usr/bin/env python3
"""Generate the Phase 1E SDK from the one public OpenAPI document.

The generator intentionally uses only the Python standard library.  That keeps
the checked-in SDK reproducible when the Core or a client is built offline.
The generated files contain no hand-maintained request/response models; the
small transport and SSE adapters are kept separately because they are runtime
plumbing rather than protocol definitions.
"""

# Generated output contains long protocol declarations; the generated files
# carry the same targeted exemption.
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import re
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
_IDENTIFIER = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")


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
    ref = _ref_name(schema)
    if ref:
        return "Cursor" if ref == "Cursor" else ref
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


def _render_ts_models(document: dict[str, Any], digest: str) -> str:
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
        "} from './phase1e-transport';",
        "",
        "export const PHASE1E_PROTOCOL_VERSION = 'phase1e.v1' as const;",
        f"export const PHASE1E_SCHEMA_DIGEST = '{digest}' as const;",
        "export const PHASE1E_MAX_CURSOR = 9223372036854775807n;",
        "",
        "/** Cursor is kept lossless for SSE ids beyond Number.MAX_SAFE_INTEGER. */",
        "export type Cursor = number | bigint;",
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
                    else json.dumps(property_name)
                )
                lines.append(
                    f"  {field}{optional}: {_ts_type(property_schema, property_name=property_name)};"
                )
        if schema.get("additionalProperties") is True:
            lines.append("  [key: string]: unknown;")
        lines.extend(["}", ""])
    return "\n".join(lines)


def _render_ts_client(document: dict[str, Any], digest: str) -> str:
    del document  # Operation names and wire shapes are frozen in the Schema above.
    return f"""/* eslint-disable */
// GENERATED FILE - DO NOT EDIT. Source: sdk/protocol/schema/operant-phase1e.openapi.json
// Generated by sdk/protocol/generate_phase1e.py; schema digest: {digest}

import {{
  Phase1EError,
  Phase1ERequest,
  Phase1EResponse,
  Phase1ETransport,
  fetchPhase1ETransport,
  parseCursor,
  parseSse,
  readJson,
  readText,
  responseHeaders,
}} from './phase1e-transport';

export const PHASE1E_PROTOCOL_VERSION = 'phase1e.v1' as const;
export const PHASE1E_SCHEMA_DIGEST = '{digest}' as const;
export const PHASE1E_MAX_CURSOR = 9223372036854775807n;

/** Cursor is kept lossless for SSE ids beyond Number.MAX_SAFE_INTEGER. */
export type Cursor = number | bigint;

export interface ProtocolNegotiation {{
  protocol_version: 'phase1e.v1';
  schema_digest: string;
  min_client_version: 'phase1e.v1';
  capabilities: string[];
}}

export interface ReceiptResource {{ type: string; id: string; }}
export interface CommandReceipt {{
  command_kind: 'command' | 'stream';
  accepted: boolean;
  command_id: string;
  stream_replay_available?: boolean;
  first_event_type?: string | null;
  resource_type?: string | null;
  resource_id?: string | null;
  replay_url?: string | null;
  replay_after_cursor?: Cursor | null;
  recovery?: 'replay_events' | 'not_available' | 'none' | 'manual_reconcile';
  resource?: ReceiptResource | null;
  [key: string]: unknown;
}}

export interface ProjectThreadSummary {{
  id: string; status: string; created_at: string; updated_at: string;
}}
export interface ProjectWorkflowRunSummary {{
  id: string; status: string; current_stage: string; summary: string;
  created_at: string; updated_at: string;
}}
export interface ProjectProjection {{
  project_id: string; workspace_ref: string; readable: boolean; writable: boolean;
  created_at: string; threads: ProjectThreadSummary[];
  workflow_runs: ProjectWorkflowRunSummary[];
}}
export interface WorkspaceFileEntry {{
  path: string; name: string; type: 'file' | 'directory';
  size_bytes: number | bigint | null; modified_at: string | null;
}}
export interface WorkspaceFilesPage {{
  workspace_id: string; path: string; entries: WorkspaceFileEntry[];
  snapshot: string; next_page_token: string | null;
}}
export type ThreadStatus = 'active' | 'completed' | 'cancelled' | 'archived';
export interface ThreadLegacyRef {{ source_type: 'session' | 'workflow_run'; source_id: string; }}
export interface ThreadProjection {{
  id: string; cursor: Cursor | null; parent_thread_id: string | null;
  workspace_ref: string | null; status: ThreadStatus; legacy_refs: ThreadLegacyRef[];
  created_at: string; updated_at: string; archived_at: string | null;
}}
export interface RoleSnapshot {{
  [key: string]: unknown;
  role_id: string; role_version: number; role_name: string; system_prompt: string;
  model_profile_id: string; model_profile_name: string; provider: string; model_id: string;
  base_url: string; secret_ref: string; context_window?: number | null; effort: string;
  provider_effort_parameter?: string | null; provider_effort_value?: string | null;
  tool_policy: Record<string, unknown>; budget: Record<string, unknown>;
  memory_scope: string; captured_at: string; overrides: Record<string, unknown>;
}}
export interface Session {{ id: string; role_snapshot: RoleSnapshot; created_at: string; }}
export interface CreateRole {{
  name: string; system_prompt: string; model_profile_id: string; [key: string]: unknown;
}}
export interface CreateSessionRequest {{
  role_id?: string | null; new_role?: CreateRole | null;
  model_profile_id?: string | null; effort?: string | null;
  budget_overrides?: Record<string, unknown> | null;
}}
export interface ReferenceRequest {{ [key: string]: unknown; path?: string; start_line?: number; end_line?: number; }}
export interface RunSessionRequest {{
  message: string; workspace: string; thread_id?: string | null; references?: ReferenceRequest[];
}}
export interface RuntimeEvent {{
  [key: string]: unknown; id?: string; cursor?: Cursor | null; session_id?: string;
  agent_id?: string | null; event_type?: string; payload?: Record<string, unknown>;
  created_at?: string;
}}
export interface SseFrame {{
  id: Cursor | null; event: string; data: unknown; resource_scope: string; stream_kind: string;
}}
export type ApprovalStatus = 'pending' | 'approved' | 'rejected' | 'expired';
export interface ApprovalProjection {{
  approval_id: string; tool_call_id: string; category: string; detail: string;
  action_hash: string; status: ApprovalStatus; requested_at: string; expires_at: string;
  continuation_available: boolean;
}}
export interface ApprovalDecisionRequest {{ approved: boolean; }}
export interface ApprovalDecisionResult {{
  accepted: boolean; changed: boolean; approved: boolean; status: ApprovalStatus;
  approval_id: string; continuation_available: boolean; [key: string]: unknown;
}}
export interface ResponseMetadata {{
  status: number; idempotencyKey?: string; idempotencyReplayed: boolean;
}}
export interface Phase1ERequestOptions {{ idempotencyKey?: string; }}
export interface WorkspaceFilesOptions {{
  path?: string; limit?: number; pageToken?: string; snapshot?: string; afterName?: string;
}}
export interface ProjectListOptions {{ afterCursor?: Cursor; limit?: number; }}
export interface ThreadListOptions {{
  afterCursor?: Cursor; limit?: number; parentThreadId?: string;
  workspaceRef?: string; status?: ThreadStatus;
}}
export interface RunSessionStreamOptions extends Phase1ERequestOptions {{
  lastEventId?: Cursor;
}}
export interface RunSessionStream {{
  receipt: CommandReceipt | null; metadata: ResponseMetadata;
  events: AsyncIterable<SseFrame>;
}}

/** Deduplicate only within the same resource and stream scope. Cursor gaps are valid. */
export class ScopedCursorTracker {{
  private readonly seen = new Map<string, Set<string>>();
  private readonly latest = new Map<string, Cursor>();

  accept(frame: SseFrame): boolean {{
    if (frame.id === null || frame.id === undefined) return true;
    if (!frame.resource_scope || !frame.stream_kind) throw new TypeError('SSE scope is required');
    const key = `${{frame.resource_scope}}\\0${{frame.stream_kind}}`;
    const cursor = String(frame.id);
    const scope = this.seen.get(key) ?? new Set<string>();
    if (scope.has(cursor)) return false;
    scope.add(cursor);
    this.seen.set(key, scope);
    const previous = this.latest.get(key);
    if (previous === undefined || BigInt(frame.id) > BigInt(previous)) this.latest.set(key, frame.id);
    return true;
  }}

  last(scope: string, streamKind: string): Cursor | null {{
    return this.latest.get(`${{scope}}\\0${{streamKind}}`) ?? null;
  }}
}}

export class ProtocolNegotiationError extends Error {{
  constructor(message: string) {{ super(message); this.name = 'ProtocolNegotiationError'; }}
}}

function newIdempotencyKey(): string {{
  const cryptoApi = globalThis.crypto;
  if (cryptoApi?.randomUUID) return cryptoApi.randomUUID();
  return `phase1e-${{Date.now().toString(36)}}-${{Math.random().toString(36).slice(2)}}`;
}}

function cursorQuery(value: Cursor | undefined): string | undefined {{
  if (value === undefined) return undefined;
  return typeof value === 'bigint' ? value.toString() : String(value);
}}

function pathPart(value: string, label: string): string {{
  if (!value) throw new TypeError(`${{label}} must not be empty`);
  return encodeURIComponent(value);
}}

export class Phase1EClient {{
  readonly protocolVersion = PHASE1E_PROTOCOL_VERSION;
  readonly schemaDigest = PHASE1E_SCHEMA_DIGEST;
  private readonly baseUrl: string;
  private readonly transport: Phase1ETransport;
  private readonly clientVersion: string;
  private negotiated: ProtocolNegotiation | null = null;
  private responseMetadata: ResponseMetadata | null = null;

  constructor(
    baseUrl = 'http://127.0.0.1:8000',
    transport: Phase1ETransport = fetchPhase1ETransport,
    clientVersion = PHASE1E_PROTOCOL_VERSION,
  ) {{
    this.baseUrl = baseUrl.replace(/\\/+$/, '');
    this.transport = transport;
    this.clientVersion = clientVersion;
  }}

  get lastResponse(): ResponseMetadata | null {{ return this.responseMetadata; }}
  get negotiatedProtocol(): ProtocolNegotiation | null {{ return this.negotiated; }}

  async negotiateProtocol(force = false): Promise<ProtocolNegotiation> {{
    if (this.negotiated && !force) return this.negotiated;
    const response = await this.requestRaw({{ method: 'GET', path: '/v1/protocol' }}, true);
    const metadata = await readJson<ProtocolNegotiation>(response);
    if (
      metadata.protocol_version !== PHASE1E_PROTOCOL_VERSION ||
      metadata.min_client_version !== PHASE1E_PROTOCOL_VERSION
    ) {{
      throw new ProtocolNegotiationError(
        `unsupported Core protocol: ${{String(metadata.protocol_version)}}`,
      );
    }}
    if (metadata.schema_digest !== PHASE1E_SCHEMA_DIGEST) {{
      throw new ProtocolNegotiationError(
        `Core Schema digest mismatch: expected ${{PHASE1E_SCHEMA_DIGEST}}, got ${{metadata.schema_digest}}`,
      );
    }}
    if (!Array.isArray(metadata.capabilities)) {{
      throw new ProtocolNegotiationError('Core protocol capabilities are invalid');
    }}
    this.negotiated = metadata;
    return metadata;
  }}

  private async ensureNegotiated(): Promise<void> {{ await this.negotiateProtocol(); }}

  private async requestRaw(
    request: Omit<Phase1ERequest, 'url'> & {{ path: string; query?: Record<string, string | undefined>; }},
    skipNegotiation = false,
  ): Promise<Phase1EResponse> {{
    if (!skipNegotiation) await this.ensureNegotiated();
    const url = new URL(`${{this.baseUrl}}${{request.path}}`);
    for (const [key, value] of Object.entries(request.query ?? {{}})) {{
      if (value !== undefined) url.searchParams.set(key, value);
    }}
    let response: Phase1EResponse;
    try {{
      response = await this.transport({{
        ...request,
        url: url.toString(),
        headers: {{ 'X-Operant-Client-Version': this.clientVersion, ...(request.headers ?? {{}}) }},
      }});
    }} catch (error: unknown) {{
      if (error instanceof Phase1EError) throw error;
      throw new Phase1EError(
        'transport_unavailable',
        error instanceof Error ? error.message : 'transport request failed',
        true,
        'retry_later',
      );
    }}
    const headers = responseHeaders(response.headers);
    this.responseMetadata = {{
      status: response.status,
      idempotencyKey: headers['idempotency-key'],
      idempotencyReplayed: headers['idempotency-replayed']?.toLowerCase() === 'true',
    }};
    if (response.status < 200 || response.status >= 300) throw await Phase1EError.fromResponse(response);
    return response;
  }}

  async listProjects(options: ProjectListOptions = {{}}): Promise<ProjectProjection[]> {{
    const response = await this.requestRaw({{
      method: 'GET', path: '/v1/projects', headers: {{ Accept: 'application/json' }},
      query: {{ after_cursor: cursorQuery(options.afterCursor), limit: options.limit === undefined ? undefined : String(options.limit) }},
    }});
    return readJson<ProjectProjection[]>(response);
  }}

  async listWorkspaceFiles(workspaceId: string, options: WorkspaceFilesOptions = {{}}): Promise<WorkspaceFilesPage> {{
    const response = await this.requestRaw({{
      method: 'GET', path: `/v1/workspaces/${{pathPart(workspaceId, 'workspaceId')}}/files`,
      headers: {{ Accept: 'application/json' }},
      query: {{
        path: options.path,
        limit: options.limit === undefined ? undefined : String(options.limit),
        page_token: options.pageToken,
        snapshot: options.snapshot,
        after_name: options.afterName,
      }},
    }});
    return readJson<WorkspaceFilesPage>(response);
  }}

  async listThreads(options: ThreadListOptions = {{}}): Promise<ThreadProjection[]> {{
    const response = await this.requestRaw({{
      method: 'GET', path: '/v1/threads', headers: {{ Accept: 'application/json' }},
      query: {{
        after_cursor: cursorQuery(options.afterCursor),
        limit: options.limit === undefined ? undefined : String(options.limit),
        parent_thread_id: options.parentThreadId,
        workspace_ref: options.workspaceRef,
        status: options.status,
      }},
    }});
    return readJson<ThreadProjection[]>(response);
  }}

  async createSession(
    request: CreateSessionRequest,
    options: Phase1ERequestOptions = {{}},
  ): Promise<Session> {{
    const response = await this.requestRaw({{
      method: 'POST', path: '/v1/sessions',
      headers: {{
        Accept: 'application/json', 'Content-Type': 'application/json',
        'Idempotency-Key': options.idempotencyKey ?? newIdempotencyKey(),
      }},
      body: JSON.stringify(request),
    }});
    return readJson<Session>(response);
  }}

  async runSessionStream(
    sessionId: string,
    request: RunSessionRequest,
    options: RunSessionStreamOptions = {{}},
  ): Promise<RunSessionStream> {{
    const response = await this.requestRaw({{
      method: 'POST', path: `/v1/sessions/${{pathPart(sessionId, 'sessionId')}}/runs`,
      headers: {{
        Accept: 'text/event-stream, application/json', 'Content-Type': 'application/json',
        'Idempotency-Key': options.idempotencyKey ?? newIdempotencyKey(),
        ...(options.lastEventId === undefined ? {{}} : {{ 'Last-Event-ID': String(options.lastEventId) }}),
      }},
      body: JSON.stringify(request),
    }});
    const metadata = this.responseMetadata ?? {{ status: response.status, idempotencyReplayed: false }};
    if (response.status === 202) {{
      return {{ receipt: await readJson<CommandReceipt>(response), metadata, events: (async function*() {{}})() }};
    }}
    const source = response.body ?? await readText(response);
    return {{
      receipt: null,
      metadata,
      events: (async function*() {{
        for await (const frame of parseSse(source)) {{
          const id = frame.id === undefined ? null : parseCursor(frame.id);
          yield {{
            id,
            event: frame.event ?? 'message',
            data: frame.data,
            resource_scope: `session:${{sessionId}}`,
            stream_kind: 'session.run',
          }};
        }}
      }})(),
    }};
  }}

  async listPendingApprovals(sessionId: string): Promise<ApprovalProjection[]> {{
    const response = await this.requestRaw({{
      method: 'GET', path: `/v1/sessions/${{pathPart(sessionId, 'sessionId')}}/approvals`,
      headers: {{ Accept: 'application/json' }},
    }});
    return readJson<ApprovalProjection[]>(response);
  }}

  async submitApproval(
    sessionId: string,
    toolCallId: string,
    request: ApprovalDecisionRequest,
    options: Phase1ERequestOptions = {{}},
  ): Promise<ApprovalDecisionResult> {{
    const response = await this.requestRaw({{
      method: 'POST',
      path: `/v1/sessions/${{pathPart(sessionId, 'sessionId')}}/approvals/${{pathPart(toolCallId, 'toolCallId')}}`,
      headers: {{
        Accept: 'application/json', 'Content-Type': 'application/json',
        'Idempotency-Key': options.idempotencyKey ?? newIdempotencyKey(),
      }},
      body: JSON.stringify(request),
    }});
    return readJson<ApprovalDecisionResult>(response);
  }}
}}

export {{ Phase1EError }};
"""


def _render_py_models(document: dict[str, Any], digest: str) -> str:
    lines = [
        '"""Generated Phase 1E models and synchronous client. DO NOT EDIT."""',
        "",
        "# fmt: off",
        "# Source: sdk/protocol/schema/operant-phase1e.openapi.json",
        "# ruff: noqa: E501",
        f"# Generated by sdk/protocol/generate_phase1e.py; schema digest: {digest}",
        "from __future__ import annotations",
        "",
        "import json",
        "import uuid",
        "from collections.abc import Iterator",
        "from dataclasses import dataclass",
        "from typing import Any, Literal, TypedDict, cast",
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
        "Cursor = int",
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
        lines.append(f"class {name}(TypedDict, total=False):")
        properties = schema.get("properties", {})
        if isinstance(properties, dict) and properties:
            for property_name, property_schema in properties.items():
                if not isinstance(property_schema, dict):
                    continue
                lines.append(
                    f"    {property_name}: {_py_type(property_schema, property_name=property_name)}"
                )
        else:
            lines.append("    _empty: Any")
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
            "class ProtocolNegotiationError(Phase1EError):",
            "    def __init__(self, message: str) -> None:",
            "        super().__init__('protocol_incompatible', message, recovery='refresh_and_retry')",
            "",
            "class ScopedCursorTracker:",
            '    """Deduplicate only within one resource and stream scope; gaps are valid."""',
            "",
            "    def __init__(self) -> None:",
            "        self._seen: dict[tuple[str, str], set[int]] = {}",
            "        self._latest: dict[tuple[str, str], int] = {}",
            "",
            "    def accept(self, frame: SseFrame) -> bool:",
            "        cursor = frame.get('id')",
            "        if cursor is None:",
            "            return True",
            "        scope = frame.get('resource_scope')",
            "        stream_kind = frame.get('stream_kind')",
            "        if not isinstance(scope, str) or not isinstance(stream_kind, str) or not scope or not stream_kind:",
            "            raise TypeError('SSE scope is required')",
            "        key = (scope, stream_kind)",
            "        seen = self._seen.setdefault(key, set())",
            "        if cursor in seen:",
            "            return False",
            "        seen.add(cursor)",
            "        self._latest[key] = max(cursor, self._latest.get(key, cursor))",
            "        return True",
            "",
            "    def last(self, scope: str, stream_kind: str) -> int | None:",
            "        return self._latest.get((scope, stream_kind))",
            "",
            "def _new_idempotency_key() -> str:",
            "    return f'phase1e-{uuid.uuid4()}'",
            "",
            "def _cursor_value(value: int | None) -> str | None:",
            "    if value is None:",
            "        return None",
            "    if isinstance(value, bool) or not 0 <= value <= PHASE1E_MAX_CURSOR:",
            "        raise ValueError('cursor must be an integer between 0 and 2^63-1')",
            "    return str(value)",
            "",
            "def _required_path(value: str, label: str) -> str:",
            "    if not value:",
            "        raise ValueError(f'{label} must not be empty')",
            "    from urllib.parse import quote",
            "    return quote(value, safe='')",
            "",
            "def _parse_cursor_id(value: object) -> int | None:",
            "    if value is None:",
            "        return None",
            "    if not isinstance(value, str) or not value.isdecimal():",
            "        raise ValueError('SSE id must be a decimal cursor')",
            "    parsed = int(value)",
            "    _cursor_value(parsed)",
            "    return parsed",
            "",
            "class Phase1EClient:",
            "    protocol_version = PHASE1E_PROTOCOL_VERSION",
            "    schema_digest = PHASE1E_SCHEMA_DIGEST",
            "",
            "    def __init__(",
            "        self,",
            "        base_url: str = 'http://127.0.0.1:8000',",
            "        transport: Transport = default_transport,",
            "        client_version: str = PHASE1E_PROTOCOL_VERSION,",
            "    ) -> None:",
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
            "    def negotiate_protocol(self, *, force: bool = False) -> ProtocolNegotiation:",
            "        if self._negotiated is not None and not force:",
            "            return self._negotiated",
            "        response = self._request('GET', '/v1/protocol', skip_negotiation=True)",
            "        metadata: dict[str, Any] = response_json(response)",
            "        if not isinstance(metadata, dict):",
            "            raise ProtocolNegotiationError('protocol response is not an object')",
            "        if metadata.get('protocol_version') != PHASE1E_PROTOCOL_VERSION or metadata.get('min_client_version') != PHASE1E_PROTOCOL_VERSION:",
            "            raise ProtocolNegotiationError(f'unsupported Core protocol: {metadata.get(\"protocol_version\")}')",
            "        if metadata.get('schema_digest') != PHASE1E_SCHEMA_DIGEST:",
            "            raise ProtocolNegotiationError('Core Schema digest mismatch')",
            "        if not isinstance(metadata.get('capabilities'), list):",
            "            raise ProtocolNegotiationError('Core protocol capabilities are invalid')",
            "        self._negotiated = metadata  # type: ignore[assignment]",
            "        return metadata  # type: ignore[return-value]",
            "",
            "    def _request(",
            "        self,",
            "        method: str,",
            "        path: str,",
            "        *,",
            "        query: dict[str, str | None] | None = None,",
            "        body: dict[str, Any] | None = None,",
            "        idempotency_key: str | None = None,",
            "        last_event_id: int | None = None,",
            "        accept: str = 'application/json',",
            "        skip_negotiation: bool = False,",
            "    ) -> Any:",
            "        if not skip_negotiation:",
            "            self.negotiate_protocol()",
            "        headers = {'Accept': accept}",
            "        payload: str | None = None",
            "        if body is not None:",
            "            headers['Content-Type'] = 'application/json'",
            "            payload = json.dumps(body, ensure_ascii=False, separators=(',', ':'))",
            "        if idempotency_key is not None:",
            "            headers['Idempotency-Key'] = idempotency_key or _new_idempotency_key()",
            "        if last_event_id is not None:",
            "            cursor = _cursor_value(last_event_id)",
            "            assert cursor is not None",
            "            headers['Last-Event-ID'] = cursor",
            "        headers['X-Operant-Client-Version'] = self._client_version",
            "        from urllib.parse import urlencode",
            "        query_values = {key: value for key, value in (query or {}).items() if value is not None}",
            "        query_suffix = f'?{urlencode(query_values)}' if query_values else ''",
            "        request = TransportRequest(method=method, url=f'{self._base_url}{path}{query_suffix}', headers=headers, body=payload)",
            "        response = self._transport(request)",
            "        values = response_headers(response.headers)",
            "        self.last_response = ResponseMetadata(",
            "            status=response.status,",
            "            idempotency_key=values.get('idempotency-key'),",
            "            idempotency_replayed=values.get('idempotency-replayed', '').lower() == 'true',",
            "        )",
            "        if not 200 <= response.status < 300:",
            "            raise Phase1EError.from_response(response)",
            "        return response",
            "",
            "    def list_projects(self, *, after_cursor: int | None = None, limit: int | None = None) -> list[ProjectProjection]:",
            "        response = self._request('GET', '/v1/projects', query={'after_cursor': _cursor_value(after_cursor), 'limit': None if limit is None else str(limit)})",
            "        return cast(list[ProjectProjection], response_json(response))",
            "",
            "    def list_workspace_files(self, workspace_id: str, *, path: str | None = None, limit: int | None = None, page_token: str | None = None, snapshot: str | None = None, after_name: str | None = None) -> WorkspaceFilesPage:",
            "        query = {'path': path, 'limit': None if limit is None else str(limit), 'page_token': page_token, 'snapshot': snapshot, 'after_name': after_name}",
            "        response = self._request('GET', f'/v1/workspaces/{_required_path(workspace_id, \"workspace_id\")}/files', query=query)",
            "        return cast(WorkspaceFilesPage, response_json(response))",
            "",
            "    def list_threads(self, *, after_cursor: int | None = None, limit: int | None = None, parent_thread_id: str | None = None, workspace_ref: str | None = None, status: ThreadStatus | None = None) -> list[ThreadProjection]:",
            "        query = {'after_cursor': _cursor_value(after_cursor), 'limit': None if limit is None else str(limit), 'parent_thread_id': parent_thread_id, 'workspace_ref': workspace_ref, 'status': status}",
            "        response = self._request('GET', '/v1/threads', query=query)",
            "        return cast(list[ThreadProjection], response_json(response))",
            "",
            "    def create_session(self, request: CreateSessionRequest, *, idempotency_key: str | None = None) -> Session:",
            "        response = self._request('POST', '/v1/sessions', body=dict(request), idempotency_key=idempotency_key or _new_idempotency_key())",
            "        return cast(Session, response_json(response))",
            "",
            "    def run_session_stream(self, session_id: str, request: RunSessionRequest, *, idempotency_key: str | None = None, last_event_id: int | None = None) -> RunSessionStream:",
            "        response = self._request('POST', f'/v1/sessions/{_required_path(session_id, \"session_id\")}/runs', body=dict(request), idempotency_key=idempotency_key or _new_idempotency_key(), last_event_id=last_event_id, accept='text/event-stream, application/json')",
            "        metadata = self.last_response or ResponseMetadata(response.status)",
            "        if response.status == 202:",
            "            receipt: CommandReceipt = response_json(response)",
            "            return RunSessionStream(receipt=receipt, events=iter(()), metadata=metadata)",
            "        source = response.body if response.body is not None else response_text(response)",
            "        frames = parse_sse(source)",
            "        def events() -> Iterator[SseFrame]:",
            "            for frame in frames:",
            "                yield {",
            "                    'id': _parse_cursor_id(frame.get('id')),",
            "                    'event': frame.get('event', 'message'),",
            "                    'data': frame.get('data'),",
            "                    'resource_scope': f'session:{session_id}',",
            "                    'stream_kind': 'session.run',",
            "                }",
            "        return RunSessionStream(receipt=None, events=events(), metadata=metadata)",
            "",
            "    def list_pending_approvals(self, session_id: str) -> list[ApprovalProjection]:",
            "        response = self._request('GET', f'/v1/sessions/{_required_path(session_id, \"session_id\")}/approvals')",
            "        return cast(list[ApprovalProjection], response_json(response))",
            "",
            "    def submit_approval(self, session_id: str, tool_call_id: str, request: ApprovalDecisionRequest, *, idempotency_key: str | None = None) -> ApprovalDecisionResult:",
            "        response = self._request('POST', f'/v1/sessions/{_required_path(session_id, \"session_id\")}/approvals/{_required_path(tool_call_id, \"tool_call_id\")}', body=dict(request), idempotency_key=idempotency_key or _new_idempotency_key())",
            "        return cast(ApprovalDecisionResult, response_json(response))",
            "",
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
        ]
    )
    return "\n".join(lines)


def _validate_document(document: dict[str, Any]) -> None:
    if document.get("openapi") != "3.1.0":
        raise ValueError("Phase 1E Schema must use OpenAPI 3.1")
    info = document.get("info")
    if not isinstance(info, dict) or info.get("version") != PROTOCOL_VERSION:
        raise ValueError("Phase 1E Schema must declare phase1e.v1")
    paths = document.get("paths")
    if not isinstance(paths, dict):
        raise ValueError("Phase 1E Schema paths are missing")
    operation_ids = {
        str(operation.get("operationId"))
        for path in paths.values()
        if isinstance(path, dict)
        for operation in path.values()
        if isinstance(operation, dict) and operation.get("operationId")
    }
    expected = {
        "negotiateProtocol",
        "listProjects",
        "listWorkspaceFiles",
        "listThreads",
        "createSession",
        "runSessionStream",
        "listPendingApprovals",
        "submitApproval",
    }
    if operation_ids != expected:
        raise ValueError(f"unexpected Phase 1E operation ids: {sorted(operation_ids)}")
    serialized = _canonical_json(document).decode("utf-8").lower()
    for excluded in ("graph", "remote", "team", "scheduler", "oauth", "tui", "tauri"):
        if excluded in serialized:
            raise ValueError(f"excluded capability appears in formal Schema: {excluded}")


def generate() -> str:
    document = _read_schema()
    _validate_document(document)
    digest = _digest(document)
    _write_if_changed(
        DIGEST_PATH,
        f"{digest}  {SCHEMA_PATH.name}\n",
    )
    client = _render_ts_client(document, digest)
    client_suffix = client.split("export interface ResponseMetadata", 1)[1]
    _write_if_changed(
        TS_PATH,
        _render_ts_models(document, digest) + "\nexport interface ResponseMetadata" + client_suffix,
    )
    _write_if_changed(PY_PATH, _render_py_models(document, digest))
    _write_if_changed(
        PY_INIT_PATH,
        """\"\"\"Generated Phase 1E Python SDK.\"\"\"\n\nfrom .phase1e_generated import (\n    PHASE1E_MAX_CURSOR,\n    PHASE1E_PROTOCOL_VERSION,\n    PHASE1E_SCHEMA_DIGEST,\n    Phase1EClient,\n    ProtocolNegotiationError,\n)\n\n__all__ = [\n    \"PHASE1E_MAX_CURSOR\",\n    \"PHASE1E_PROTOCOL_VERSION\",\n    \"PHASE1E_SCHEMA_DIGEST\",\n    \"Phase1EClient\",\n    \"ProtocolNegotiationError\",\n]\n""",
    )
    return digest


if __name__ == "__main__":
    print(generate())
