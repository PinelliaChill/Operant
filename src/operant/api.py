from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qsl, quote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from starlette.background import BackgroundTask
from starlette.middleware.base import RequestResponseEndpoint
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from operant.application.client_projection import WorkspaceProjectionError
from operant.application.evaluation import EvaluationRunner
from operant.application.protocol_metadata import (
    ProtocolSchemaUnavailable,
    phase1e_protocol_metadata,
)
from operant.application.service import ApplicationService
from operant.application.workflow import SequentialCodingWorkflow, WorkflowEvent
from operant.artifacts import (
    ArtifactCapabilityError,
    ArtifactContentDeletedError,
    ArtifactCorruptionError,
    ArtifactExportOutcomeUnknownError,
    ArtifactNotFoundError,
    ArtifactSecurityError,
    ArtifactStoreError,
    ArtifactTooLargeError,
    ArtifactValidationError,
)
from operant.domain.actions import CommandExecution, CommandExecutionStatus
from operant.domain.commands import ContextBaselineOperation, SlashCommandKind
from operant.domain.context import ContextRevision, ReferenceRequest
from operant.domain.evaluation import EvaluationResult, EvaluationRunEvent, EvaluationSuite
from operant.domain.memory import MemoryKind
from operant.domain.models import (
    Budget,
    Effort,
    EffortMapping,
    ModelProfile,
    RolePreset,
    RoleStatus,
    ToolPolicy,
    new_id,
)
from operant.domain.threads import (
    ArtifactSensitivity,
    ArtifactSourceRef,
    ConversationThread,
    Item,
    ItemPayload,
    RetentionPolicy,
    ThreadLegacyRef,
    ThreadStatus,
    Turn,
)
from operant.persistence.sqlite import (
    ActionOutcomeUnknownError,
    ConflictError,
    IdempotencyConflictError,
    NotFoundError,
    SQLiteStore,
)
from operant.protocol import (
    RecoveryAction,
    canonical_action_hash,
    error_payload,
    redact_public_data,
    redact_public_text,
)
from operant.providers.openai_compatible import (
    OpenAICompatibleProvider,
    ProviderError,
)
from operant.settings import database_path, load_local_env

MAX_ARTIFACT_UPLOAD_BYTES = 16 * 1024 * 1024
MAX_ARTIFACT_BASE64_CHARS = ((MAX_ARTIFACT_UPLOAD_BYTES + 2) // 3) * 4
MAX_ARTIFACT_REQUEST_BODY_BYTES = MAX_ARTIFACT_BASE64_CHARS + 64 * 1024


class _ArtifactRequestTooLarge(Exception):
    pass


class _ArtifactRequestBodyLimitMiddleware:
    """Stop oversized artifact streams without draining or buffering them."""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] != "/v1/artifacts":
            await self.app(scope, receive, send)
            return

        content_length: int | None = None
        for name, value in scope["headers"]:
            if name.lower() != b"content-length":
                continue
            try:
                content_length = int(value)
            except ValueError:
                await self._error_response(
                    scope,
                    receive,
                    send,
                    status_code=400,
                    code="invalid_content_length",
                    message="Content-Length must be a non-negative integer",
                )
                return
            if content_length < 0:
                await self._error_response(
                    scope,
                    receive,
                    send,
                    status_code=400,
                    code="invalid_content_length",
                    message="Content-Length must be a non-negative integer",
                )
                return
            break
        if content_length is not None and content_length > self.max_body_bytes:
            await self._too_large_response(scope, receive, send)
            return

        consumed = 0
        response_started = False

        async def bounded_receive() -> Message:
            nonlocal consumed
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > self.max_body_bytes:
                    raise _ArtifactRequestTooLarge
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, bounded_receive, tracked_send)
        except _ArtifactRequestTooLarge:
            if response_started:
                raise
            await self._too_large_response(scope, receive, send)

    async def _too_large_response(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._error_response(
            scope,
            receive,
            send,
            status_code=413,
            code="artifact_request_too_large",
            message="artifact request exceeds the API size limit",
        )

    @staticmethod
    async def _error_response(
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        status_code: int,
        code: str,
        message: str,
    ) -> None:
        response = JSONResponse(
            status_code=status_code,
            content=error_payload(code=code, message=message),
        )
        await response(scope, receive, send)


class CreateModelProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    model_id: str
    base_url: str
    secret_ref: str = "OPERANT_API_KEY"
    context_window: int | None = None
    default_token_budget: int | None = None
    input_usd_per_million_tokens: float | None = Field(default=None, ge=0)
    output_usd_per_million_tokens: float | None = Field(default=None, ge=0)
    supported_efforts: tuple[Effort, ...] = (
        Effort.LOW,
        Effort.MEDIUM,
        Effort.HIGH,
    )
    default_effort: Effort = Effort.MEDIUM
    effort_parameter: str | None = "reasoning_effort"
    effort_mapping: tuple[EffortMapping, ...] = (
        EffortMapping(effort=Effort.LOW, provider_value="low"),
        EffortMapping(effort=Effort.MEDIUM, provider_value="medium"),
        EffortMapping(effort=Effort.HIGH, provider_value="high"),
    )

    def to_domain(self) -> ModelProfile:
        return ModelProfile(**self.model_dump())


class UpdateModelProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    model_id: str | None = None
    base_url: str | None = None
    secret_ref: str | None = None
    context_window: int | None = None
    default_token_budget: int | None = None
    input_usd_per_million_tokens: float | None = Field(default=None, ge=0)
    output_usd_per_million_tokens: float | None = Field(default=None, ge=0)
    supported_efforts: tuple[Effort, ...] | None = None
    default_effort: Effort | None = None
    effort_parameter: str | None = None
    effort_mapping: tuple[EffortMapping, ...] | None = None
    enabled: bool | None = None


class DiscoverModelsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str
    secret_ref: str = "OPERANT_API_KEY"


class CreateRoleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    system_prompt: str
    model_profile_id: str
    effort: Effort = Effort.MEDIUM
    tool_policy: ToolPolicy = Field(default_factory=ToolPolicy)
    budget: Budget = Field(default_factory=Budget)
    memory_scope: str = "session"

    def to_domain(self) -> RolePreset:
        return RolePreset(**self.model_dump())


class UpdateRoleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    system_prompt: str | None = None
    model_profile_id: str | None = None
    effort: Effort | None = None
    tool_policy: ToolPolicy | None = None
    budget: Budget | None = None
    memory_scope: str | None = None
    status: RoleStatus | None = None


class CopyRoleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str


class SeedRolesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    planner_model_profile_id: str
    coder_model_profile_id: str
    reviewer_model_profile_id: str


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role_id: str | None = None
    new_role: CreateRoleRequest | None = None
    model_profile_id: str | None = None
    effort: Effort | None = None
    budget_overrides: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_role_source(self) -> CreateSessionRequest:
        if (self.role_id is None) == (self.new_role is None):
            raise ValueError("provide exactly one of role_id or new_role")
        return self


class RunSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1)
    workspace: str = Field(min_length=1)
    thread_id: str | None = Field(default=None, min_length=1, max_length=300)
    references: tuple[ReferenceRequest, ...] = Field(default=(), max_length=50)


class ApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved: bool


class WorkflowRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1)
    workspace: str = Field(min_length=1)
    main_role_id: str | None = "role_main"
    planner_role_id: str = "role_planner"
    explorer_role_ids: tuple[str, ...] = Field(
        default=("role_explorer",),
        max_length=4,
    )
    coder_role_id: str = "role_coder"
    reviewer_role_id: str = "role_reviewer"
    max_parallel_explorers: int = Field(default=2, ge=1, le=4)
    max_rework_rounds: int = Field(default=1, ge=0, le=3)


class WorkflowResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allow_coder_replay: bool = False


class EvaluationRunRequest(BaseModel):
    """Start one persisted suite in an isolated, explicitly local artifact root."""

    model_config = ConfigDict(extra="forbid")

    suite_id: str = Field(min_length=1, max_length=200)
    artifact_root: str = Field(min_length=1, max_length=4_096)

    @field_validator("artifact_root")
    @classmethod
    def validate_artifact_root(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or not Path(normalized).is_absolute():
            raise ValueError("artifact_root must be an absolute path")
        return normalized


class CreateMemoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    kind: MemoryKind
    content: str = Field(min_length=1)
    project_scope: str | None = None
    role_scope: tuple[str, ...] = ()
    source_task: str | None = None
    confidence: float = Field(default=0.5, ge=0, le=1)
    confirmed: bool = False


class CreateThreadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_thread_id: str | None = Field(default=None, max_length=300)
    workspace_ref: str | None = Field(default=None, min_length=1, max_length=2048)
    legacy_refs: tuple[ThreadLegacyRef, ...] = Field(default=(), max_length=16)

    @field_validator("parent_thread_id", "workspace_ref")
    @classmethod
    def normalize_optional_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("reference must not be blank")
        return normalized

    def to_domain(self) -> ConversationThread:
        return ConversationThread(**self.model_dump())


class CreateTurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    def to_domain(self, thread_id: str) -> Turn:
        return Turn(thread_id=thread_id)


class AppendItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload: ItemPayload

    def to_domain(self, *, thread_id: str, turn_id: str) -> Item:
        return Item(thread_id=thread_id, turn_id=turn_id, payload=self.payload)


class CreateArtifactRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_base64: str | None = Field(default=None, max_length=MAX_ARTIFACT_BASE64_CHARS)
    content_text: str | None = Field(default=None, max_length=MAX_ARTIFACT_UPLOAD_BYTES)
    media_type: str = Field(min_length=3, max_length=255)
    sensitivity: ArtifactSensitivity = ArtifactSensitivity.NORMAL
    source_refs: tuple[ArtifactSourceRef, ...] = Field(default=(), max_length=100)
    retention_policy_ref: str = Field(default="default", min_length=1, max_length=300)

    @model_validator(mode="after")
    def validate_content_source(self) -> CreateArtifactRequest:
        if (self.content_base64 is None) == (self.content_text is None):
            raise ValueError("provide exactly one of content_base64 or content_text")
        return self

    @field_validator("media_type")
    @classmethod
    def validate_media_type(cls, value: str) -> str:
        normalized = value.strip().lower()
        type_part, separator, subtype_part = normalized.partition("/")
        allowed = frozenset("abcdefghijklmnopqrstuvwxyz0123456789!#$&^_.+-")
        if (
            not separator
            or not type_part
            or not subtype_part
            or "/" in subtype_part
            or any(character not in allowed for character in type_part)
            or any(character not in allowed for character in subtype_part)
        ):
            raise ValueError("media_type must be a valid type/subtype without parameters")
        return normalized

    @field_validator("retention_policy_ref")
    @classmethod
    def normalize_retention_policy_ref(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("retention_policy_ref must not be blank")
        return normalized

    def decoded_content(self) -> bytes:
        if self.content_text is not None:
            content = self.content_text.encode("utf-8")
        else:
            assert self.content_base64 is not None
            try:
                content = base64.b64decode(self.content_base64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError("content_base64 must be strict RFC 4648 base64") from exc
        if len(content) > MAX_ARTIFACT_UPLOAD_BYTES:
            raise ValueError("artifact content exceeds the API size limit")
        return content


class ExportArtifactRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_root: str = Field(min_length=1, max_length=4_096)
    relative_path: str = Field(min_length=1, max_length=1_024)

    @field_validator("workspace_root")
    @classmethod
    def validate_workspace_root(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("workspace_root must be absolute")
        return value


class CreateRetentionPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=300)
    grace_period_seconds: int = Field(default=86_400, ge=0, le=31_536_000)
    allow_physical_delete: bool = False

    def to_domain(self) -> RetentionPolicy:
        return RetentionPolicy(**self.model_dump())


class RepairOrphanBlobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    finding_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReconcilePhysicalDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prior_command_id: str = Field(min_length=1, max_length=300)
    prior_action_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class SetArtifactPinRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pinned: bool


class InitializeWorkspaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace: str = Field(min_length=1, max_length=4_096)

    @field_validator("workspace")
    @classmethod
    def require_absolute_workspace(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("workspace must be an absolute path")
        return value


class ContextBaselineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1, max_length=300)
    thread_id: str = Field(min_length=1, max_length=300)
    agent_id: str | None = Field(default=None, min_length=1, max_length=300)


class ReviewCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace: str = Field(min_length=1, max_length=4_096)
    thread_id: str | None = Field(default=None, min_length=1, max_length=300)
    reviewer_role_id: str = Field(default="role_reviewer", min_length=1, max_length=300)
    scope: str = Field(default="working tree", min_length=1, max_length=2_000)

    @field_validator("workspace")
    @classmethod
    def require_absolute_workspace(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("workspace must be an absolute path")
        return value


class BTWSidecarRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1, max_length=300)
    thread_id: str = Field(min_length=1, max_length=300)
    workspace: str = Field(min_length=1, max_length=4_096)
    prompt: str = Field(min_length=1, max_length=100_000)

    @field_validator("workspace")
    @classmethod
    def require_absolute_workspace(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("workspace must be an absolute path")
        return value


def _safe_evaluation_result_payload(result: EvaluationResult) -> dict[str, Any]:
    """Return a result suitable for CLI/API export, never its local workspace path."""

    payload = result.model_dump(mode="json")
    artifact_workspace = payload.get("artifact_workspace")
    if isinstance(artifact_workspace, dict):
        artifact_workspace.pop("local_workspace_path", None)
    return payload


def _artifact_capability_from_request(request: Request) -> str:
    authorization = request.headers.get("Authorization", "")
    scheme, separator, token = authorization.partition(" ")
    if scheme != "Artifact" or not separator or not token or len(token) > 4_096:
        raise HTTPException(status_code=403, detail="Artifact capability is required")
    return token


def _safe_evaluation_error_type(exc: Exception) -> str:
    """Classify stream failures without returning provider, filesystem, or prompt text."""

    if isinstance(exc, NotFoundError):
        return "not_found"
    if isinstance(exc, ValueError):
        return "validation_error"
    if isinstance(exc, OSError):
        return "io_error"
    return "runner_error"


def _context_revision_evidence(revision: ContextRevision) -> dict[str, Any]:
    """Expose explainability metadata without returning prompt or Tool content."""

    return {
        "id": revision.id,
        "cursor": revision.cursor,
        "session_id": revision.session_id,
        "agent_id": revision.agent_id,
        "agent_instance_id": revision.agent_instance_id,
        "thread_id": revision.thread_id,
        "request_ordinal": revision.request_ordinal,
        "model_id": revision.model_id,
        "prompt_layout": revision.prompt_layout.model_dump(mode="json"),
        "prompt_layout_version": revision.prompt_layout_version,
        "message_count": len(revision.messages),
        "message_ids": list(revision.message_ids),
        "tool_count": len(revision.tools),
        "blocks": [
            {
                "id": block.id,
                "position": block.position,
                "block_type": block.block_type.value,
                "content_hash": block.content_hash,
                "source_refs": [source.model_dump(mode="json") for source in block.source_refs],
                "stable_until": block.stable_until,
                "visibility": block.visibility.value,
                "token_estimate": block.token_estimate,
                "cache_eligible": block.cache_eligible,
            }
            for block in revision.blocks
        ],
        "reference_bindings": [
            {
                "id": binding.id,
                "position": binding.position,
                "ref_type": binding.ref_type.value,
                "resolved_target": binding.resolved_target,
                "source_snapshot_hash": binding.source_snapshot_hash,
                "include_mode": binding.include_mode.value,
                "max_tokens": binding.max_tokens,
                "visibility": binding.visibility.value,
                "resolved_at": binding.resolved_at,
            }
            for binding in revision.reference_bindings
        ],
        "tool_result_stubs": [
            {
                "artifact_id": stub.artifact_id,
                "tool_call_id": stub.tool_call_id,
                "content_hash": stub.content_hash,
                "original_size": stub.original_size,
                "stored_size": stub.stored_size,
                "fetch_capability": stub.fetch_capability,
            }
            for stub in revision.tool_result_stubs
        ],
        "watermark": revision.watermark.model_dump(mode="json"),
        "compaction_id": revision.compaction_id,
        "source_item_ids": list(revision.source_item_ids),
        "artifact_refs": list(revision.artifact_refs),
        "memory_refs": list(revision.memory_refs),
        "compaction_refs": list(revision.compaction_refs),
        "token_estimate": revision.token_estimate,
        "source_cursor_start": revision.source_cursor_start,
        "source_cursor_end": revision.source_cursor_end,
        "source_cursor_namespace": revision.source_cursor_namespace,
        "created_at": revision.created_at,
    }


def _sse_event(
    event_type: str,
    payload: dict[str, Any],
    *,
    cursor: int | None = None,
) -> str:
    event_id = "" if cursor is None else f"id: {cursor}\n"
    return f"{event_id}event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _phase1d_audit_payload(event: Any) -> dict[str, Any]:
    payload = {
        "cursor": event.cursor,
        "command_execution_id": event.command_execution_id,
        "command_kind": event.command_kind.value,
        "resource_type": event.resource_type,
        "resource_id": event.resource_id,
        "created_at": event.created_at.isoformat(),
    }
    payload.update(event.detail)
    return payload


MAX_EVENT_CURSOR = 2**63 - 1


def _parse_event_cursor(value: str | None, *, field_name: str) -> int | None:
    if value is None:
        return None
    try:
        cursor = int(value)
    except ValueError as exc:
        raise ValueError(
            f"{field_name} must be an integer between 0 and {MAX_EVENT_CURSOR}"
        ) from exc
    if not 0 <= cursor <= MAX_EVENT_CURSOR:
        raise ValueError(f"{field_name} must be an integer between 0 and {MAX_EVENT_CURSOR}")
    return cursor


def _parse_last_event_id(value: str | None) -> int | None:
    return _parse_event_cursor(value, field_name="Last-Event-ID")


def _stored_command_payload(payload: Any, *, status_code: int) -> Any:
    """Bound and redact every durable Command response before persistence/replay."""

    safe = redact_public_data(payload)
    if status_code < 400:
        return safe
    if isinstance(safe, dict) and "detail" in safe:
        error = safe.get("error")
        if isinstance(error, dict) and {
            "code",
            "message",
            "retryable",
            "recovery",
        }.issubset(error):
            return safe
    return error_payload(
        code=f"http_{status_code}",
        message="command request failed",
        recovery=(
            RecoveryAction.RETRY_LATER
            if status_code in {408, 425, 429, 502, 503, 504}
            else RecoveryAction.NONE
        ),
        retryable=status_code in {408, 425, 429, 502, 503, 504},
    )


def _command_scope(method: str, path: str) -> str | None:
    """Return a stable, versioned Command type without persisting path values."""

    if method not in {"POST", "PATCH", "DELETE", "PUT"}:
        return None
    if path in {"/v1/models/discover"} or (
        path.startswith("/v1/models/") and path.endswith("/health")
    ):
        return None
    if not path.startswith("/v1/"):
        return None
    return f"rest-command.v2:{method}"


def _canonical_command_resource(path: str) -> str:
    """Preserve the one historical REST alias without collapsing other paths."""

    if path in {"/v1/tasks", "/v1/workflows/coding/runs"}:
        return "/v1/tasks"
    return path


def _command_request_hash(
    *,
    scope: str,
    path: str,
    query: str,
    body: bytes,
) -> str:
    try:
        parsed_body: Any = None if not body else json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed_body = {"invalid_json_sha256": canonical_action_hash({"raw": body.hex()})}
    return canonical_action_hash(
        {
            "kind": "rest_command",
            "scope": scope,
            "resource_path_hash": canonical_action_hash(
                {
                    "kind": "rest_resource_path",
                    "path": _canonical_command_resource(path),
                }
            ),
            "query": sorted(parse_qsl(query, keep_blank_values=True)),
            "body": parsed_body,
        }
    )


def _is_sse_replay_route(method: str, path: str) -> bool:
    if method == "POST" and path.startswith("/v1/sessions/") and path.endswith("/runs"):
        return True
    if method == "POST" and path.startswith("/v1/tasks/") and path.endswith("/resume"):
        return True
    if method == "GET" and path.startswith("/v1/threads/") and path.endswith("/items/stream"):
        return True
    if method == "GET" and path.startswith("/v1/sidecars/btw/") and path.endswith("/events/stream"):
        return True
    if (
        method == "GET"
        and path.startswith("/v1/command-executions/")
        and path.endswith("/events/stream")
    ):
        return True
    return (
        method == "GET"
        and path.startswith("/v1/evaluations/runs/")
        and path.endswith("/events/stream")
    )


def _is_event_cursor_query_route(method: str, path: str) -> bool:
    if method != "GET":
        return False
    return (
        (path.startswith("/v1/sessions/") and path.endswith("/events"))
        or (path.startswith("/v1/tasks/") and path.endswith("/events"))
        or (path.startswith("/v1/threads/") and path.endswith("/items"))
        or (path.startswith("/v1/threads/") and path.endswith("/turns"))
        or (path == "/v1/threads")
        or (path in {"/v1/artifacts", "/v1/cache-observations"})
        or (path.startswith("/v1/threads/") and path.endswith("/items/stream"))
        or (
            path.startswith("/v1/sidecars/btw/")
            and (path.endswith("/events") or path.endswith("/events/stream"))
        )
        or (
            path.startswith("/v1/command-executions/")
            and (path.endswith("/events") or path.endswith("/events/stream"))
        )
        or (
            path.startswith("/v1/evaluations/runs/")
            and (path.endswith("/events") or path.endswith("/events/stream"))
        )
    )


_FIRST_STREAM_FAILURE_OUTCOMES = {
    "evaluation.error": (
        "evaluation_stream_failed",
        "evaluation stream failed before it was accepted",
        500,
        RecoveryAction.MANUAL_RECONCILE,
    ),
    "workflow.resume_rejected": (
        "workflow_resume_rejected",
        "workflow resume request was rejected",
        409,
        RecoveryAction.MANUAL_RECONCILE,
    ),
}
_PHASE1D_STREAM_START_FAILURE_OUTCOMES = {
    "review.stream_error": {
        "review_start_not_found": (
            "review_start_not_found",
            "Review stream dependencies were not found",
            404,
            RecoveryAction.NONE,
        ),
        "review_start_validation_failed": (
            "review_start_validation_failed",
            "Review stream request is invalid",
            400,
            RecoveryAction.NONE,
        ),
        "review_start_policy_denied": (
            "review_start_policy_denied",
            "Review stream request is not allowed",
            403,
            RecoveryAction.NONE,
        ),
    },
    "btw.stream_error": {
        "btw_start_not_found": (
            "btw_start_not_found",
            "BTW Sidecar stream dependencies were not found",
            404,
            RecoveryAction.NONE,
        ),
        "btw_start_validation_failed": (
            "btw_start_validation_failed",
            "BTW Sidecar stream request is invalid",
            400,
            RecoveryAction.NONE,
        ),
        "btw_start_policy_denied": (
            "btw_start_policy_denied",
            "BTW Sidecar stream request is not allowed",
            403,
            RecoveryAction.NONE,
        ),
    },
}
_FAILED_FIRST_STREAM_EVENTS = frozenset(
    _FIRST_STREAM_FAILURE_OUTCOMES | _PHASE1D_STREAM_START_FAILURE_OUTCOMES
)
_UNKNOWN_FIRST_STREAM_EVENTS = frozenset(
    {
        "agent.stream_error",
        "workflow.resume_failed",
        "workflow.stream_error",
    }
)
MAX_FIRST_SSE_FRAME_BYTES = 256_000


def _first_sse_frame_end(buffer: bytes) -> int | None:
    boundaries = tuple(
        index + len(marker)
        for marker in (b"\n\n", b"\r\n\r\n")
        if (index := buffer.find(marker)) >= 0
    )
    return min(boundaries, default=None)


def _parse_sse_frame(frame: bytes) -> tuple[str, dict[str, Any]] | None:
    event_type = "message"
    data_lines: list[str] = []
    text = frame.decode("utf-8", errors="replace").replace("\r\n", "\n")
    for line in text.splitlines():
        if line.startswith("event:"):
            event_type = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if not event_type or not data_lines:
        return None
    try:
        payload = json.loads("\n".join(data_lines))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return event_type, payload


def _phase1d_stream_start_error(kind: str, exc: Exception) -> dict[str, Any]:
    label = "Review" if kind == "review" else "BTW Sidecar"
    if isinstance(exc, NotFoundError):
        suffix = "not_found"
        message = f"{label} stream dependencies were not found"
    elif isinstance(exc, PermissionError):
        suffix = "policy_denied"
        message = f"{label} stream request is not allowed"
    else:
        suffix = "validation_failed"
        message = f"{label} stream request is invalid"
    return error_payload(
        code=f"{kind}_start_{suffix}",
        message=message,
        recovery=RecoveryAction.NONE,
    )


def _failed_first_stream_outcome(
    event_type: str,
    payload: dict[str, Any],
) -> tuple[str, str, int, RecoveryAction]:
    fixed = _FIRST_STREAM_FAILURE_OUTCOMES.get(event_type)
    if fixed is not None:
        return fixed
    variants = _PHASE1D_STREAM_START_FAILURE_OUTCOMES[event_type]
    error = payload.get("error")
    error_code = error.get("code") if isinstance(error, dict) else None
    if isinstance(error_code, str) and error_code in variants:
        return variants[error_code]
    prefix = "review" if event_type == "review.stream_error" else "btw"
    label = "Review" if prefix == "review" else "BTW Sidecar"
    return (
        f"{prefix}_start_validation_failed",
        f"{label} stream request is invalid",
        400,
        RecoveryAction.NONE,
    )


def _stream_resource(
    *,
    store: SQLiteStore,
    path: str,
    event_type: str,
    payload: dict[str, Any],
) -> tuple[str | None, str | None, str | None, int | None]:
    cursor = payload.get("cursor")
    durable_cursor = cursor if isinstance(cursor, int) and cursor >= 1 else None
    candidates = (
        ()
        if event_type.startswith(("review.", "btw."))
        else (
            ("workflow", "workflow_run_id", "/v1/tasks/{}/events"),
            ("evaluation", "evaluation_run_id", "/v1/evaluations/runs/{}/events"),
            ("session", "session_id", "/v1/sessions/{}/events"),
        )
    )

    def is_committed(resource_type: str, resource_id: str, cursor: int | None) -> bool:
        if cursor is None:
            return False
        try:
            if resource_type == "session":
                events = store.list_events(resource_id, after_cursor=cursor - 1, limit=1)
                return bool(events and events[0].cursor == cursor)
            elif resource_type == "workflow":
                workflow_events = store.list_workflow_events(
                    resource_id,
                    after_cursor=cursor - 1,
                    limit=1,
                )
                return bool(workflow_events and workflow_events[0].cursor == cursor)
            else:
                evaluation_events = store.list_evaluation_events(
                    resource_id,
                    after_cursor=cursor - 1,
                    limit=1,
                )
                return bool(evaluation_events and evaluation_events[0].cursor == cursor)
        except (NotFoundError, ValueError):
            return False

    for resource_type, key, replay_template in candidates:
        resource_id = payload.get(key)
        if isinstance(resource_id, str) and 0 < len(resource_id) <= 300:
            return (
                resource_type,
                resource_id,
                replay_template.format(quote(resource_id, safe="")),
                durable_cursor
                if is_committed(resource_type, resource_id, durable_cursor)
                else None,
            )
    if event_type.startswith("btw."):
        sidecar_run_id = payload.get("sidecar_run_id")
        if isinstance(sidecar_run_id, str) and 0 < len(sidecar_run_id) <= 300:
            committed = False
            if durable_cursor is not None:
                try:
                    sidecar_events = store.list_btw_sidecar_events(
                        sidecar_run_id,
                        after_cursor=durable_cursor - 1,
                        limit=1,
                    )
                    committed = bool(sidecar_events and sidecar_events[0].cursor == durable_cursor)
                except (NotFoundError, ValueError):
                    pass
            return (
                "sidecar",
                sidecar_run_id,
                f"/v1/sidecars/btw/{quote(sidecar_run_id, safe='')}/events/stream",
                durable_cursor if committed else None,
            )
    if event_type.startswith("review."):
        review_run_id = payload.get("review_run_id")
        command_execution_id = payload.get("command_execution_id")
        if (
            isinstance(review_run_id, str)
            and 0 < len(review_run_id) <= 300
            and isinstance(command_execution_id, str)
            and 0 < len(command_execution_id) <= 300
        ):
            committed = False
            if durable_cursor is not None:
                audit_events = store.list_phase1d_command_audit_events(
                    command_execution_id,
                    after_cursor=durable_cursor - 1,
                    limit=1,
                )
                committed = bool(
                    audit_events
                    and audit_events[0].cursor == durable_cursor
                    and audit_events[0].resource_id == review_run_id
                )
            return (
                "review",
                review_run_id,
                f"/v1/command-executions/{quote(command_execution_id, safe='')}/events/stream",
                durable_cursor if committed else None,
            )
    if event_type.startswith("agent.") and path.startswith("/v1/sessions/"):
        suffix = "/runs"
        encoded_id = path[len("/v1/sessions/") : -len(suffix)] if path.endswith(suffix) else ""
        if encoded_id and "/" not in encoded_id and len(encoded_id) <= 300:
            return (
                "session",
                encoded_id,
                f"/v1/sessions/{quote(encoded_id, safe='')}/events",
                durable_cursor if is_committed("session", encoded_id, durable_cursor) else None,
            )
    return None, None, None, durable_cursor


def _first_stream_summary(
    *,
    store: SQLiteStore,
    command_id: str,
    path: str,
    frame: bytes,
) -> tuple[
    CommandExecutionStatus,
    dict[str, Any] | None,
    str,
    int,
    str | None,
    str | None,
]:
    parsed = _parse_sse_frame(frame)
    if parsed is None:
        return (
            CommandExecutionStatus.MANUAL_RECONCILE_REQUIRED,
            None,
            "stream_first_frame_invalid",
            409,
            None,
            None,
        )
    event_type, payload = parsed
    resource_type, resource_id, replay_url, cursor = _stream_resource(
        store=store,
        path=path,
        event_type=event_type,
        payload=payload,
    )
    is_failed = event_type in _FAILED_FIRST_STREAM_EVENTS
    is_unknown = event_type in _UNKNOWN_FIRST_STREAM_EVENTS
    replay_available = (
        resource_id is not None and cursor is not None and not (is_failed or is_unknown)
    )
    summary: dict[str, Any] = {
        "command_kind": "stream",
        "accepted": not (is_failed or is_unknown),
        "stream_replay_available": replay_available,
        "command_id": command_id,
        "first_event_type": event_type,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "replay_url": replay_url if replay_available else None,
        "replay_after_cursor": 0 if replay_available else None,
        "recovery": "replay_events" if replay_available else "not_available",
    }
    if resource_type is not None and resource_id is not None:
        summary[f"{resource_type}_id"] = resource_id
        summary["resource"] = {"type": resource_type, "id": resource_id}
    if is_unknown:
        return (
            CommandExecutionStatus.MANUAL_RECONCILE_REQUIRED,
            None,
            event_type[:200],
            409,
            resource_type,
            resource_id,
        )
    if is_failed:
        (
            failure_code,
            failure_message,
            failure_status,
            failure_recovery,
        ) = _failed_first_stream_outcome(
            event_type,
            payload,
        )
        failure_envelope = error_payload(
            code=failure_code,
            message=failure_message,
            recovery=failure_recovery,
        )
        failure_envelope.update(summary)
        failure_envelope["recovery"] = failure_recovery.value
        return (
            CommandExecutionStatus.FAILED,
            failure_envelope,
            failure_code,
            failure_status,
            resource_type,
            resource_id,
        )
    if not replay_available:
        return (
            CommandExecutionStatus.MANUAL_RECONCILE_REQUIRED,
            None,
            "stream_acceptance_unverified",
            409,
            resource_type,
            resource_id,
        )
    return (
        CommandExecutionStatus.COMPLETED,
        summary,
        "",
        202,
        resource_type or "stream",
        resource_id,
    )


def create_app(
    db_path: str | Path | None = None,
    *,
    artifact_root: str | Path | None = None,
    artifact_max_size_bytes: int = MAX_ARTIFACT_UPLOAD_BYTES,
    artifact_capability_secret: bytes | None = None,
    physical_delete_enabled: bool = False,
    physical_delete_authorization: str | None = None,
) -> FastAPI:
    load_local_env()
    store = SQLiteStore(db_path or database_path())
    configured_artifact_root = (
        store.path.parent.absolute() / "artifacts" if artifact_root is None else Path(artifact_root)
    )
    if not configured_artifact_root.is_absolute():
        raise ValueError("artifact_root must be an absolute path")
    service = ApplicationService(
        store,
        OpenAICompatibleProvider(),
        artifact_root=configured_artifact_root,
        artifact_max_size_bytes=artifact_max_size_bytes,
        artifact_capability_secret=artifact_capability_secret,
        physical_delete_enabled=physical_delete_enabled,
        physical_delete_authorization=physical_delete_authorization,
    )
    service.initialize()
    workflow = SequentialCodingWorkflow(service)
    app = FastAPI(
        title="Operant API",
        description="由角色预设驱动的多模型 Coding Agent Runtime。",
        version="0.1.0",
    )
    app.router.add_event_handler("shutdown", service.close)
    app.state.operant_service = service

    @app.exception_handler(HTTPException)
    async def http_error_handler(_request: Request, exc: HTTPException) -> JSONResponse:
        detail = redact_public_data(exc.detail)
        message = (
            redact_public_text(str(exc.detail)) if isinstance(exc.detail, str) else "request failed"
        )
        code = f"http_{exc.status_code}"
        recovery = RecoveryAction.NONE
        if exc.status_code == 400 and message.startswith("Last-Event-ID requires"):
            code = "invalid_event_cursor"
            recovery = RecoveryAction.REFRESH_AND_RETRY
        if exc.status_code >= 500:
            message = "upstream or internal service request failed"
            detail = message
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(
                code=code,
                message=message,
                retryable=exc.status_code in {408, 425, 429, 502, 503, 504},
                recovery=(
                    RecoveryAction.RETRY_LATER
                    if exc.status_code in {408, 425, 429, 502, 503, 504}
                    else recovery
                ),
                legacy_detail=detail,
            ),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        detail = [
            {
                "type": str(item.get("type", "validation_error")),
                "loc": [
                    str(part) if not isinstance(part, int) else part for part in item.get("loc", ())
                ],
                "msg": redact_public_text(str(item.get("msg", "invalid value"))),
            }
            for item in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=error_payload(
                code="request_validation_failed",
                message="request validation failed",
                legacy_detail=detail,
            ),
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(_request: Request, _exc: Exception) -> JSONResponse:
        message = "internal service error"
        return JSONResponse(
            status_code=500,
            content=error_payload(
                code="internal_error",
                message=message,
                recovery=RecoveryAction.MANUAL_RECONCILE,
                legacy_detail=message,
            ),
        )

    def protocol_response(
        *,
        status_code: int,
        code: str,
        message: str,
        recovery: RecoveryAction = RecoveryAction.NONE,
        retryable: bool = False,
        idempotency_key: str | None = None,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status_code,
            content=error_payload(
                code=code,
                message=message,
                retryable=retryable,
                recovery=recovery,
            ),
            headers=(None if idempotency_key is None else {"Idempotency-Key": idempotency_key}),
        )

    @app.middleware("http")
    async def command_idempotency_middleware(
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        raw_after_cursor = request.query_params.get("after_cursor")
        if raw_after_cursor is not None and _is_event_cursor_query_route(
            request.method, request.url.path
        ):
            try:
                _parse_event_cursor(raw_after_cursor, field_name="after_cursor")
            except ValueError:
                return protocol_response(
                    status_code=400,
                    code="invalid_event_cursor",
                    message=(f"after_cursor must be an integer between 0 and {MAX_EVENT_CURSOR}"),
                    recovery=RecoveryAction.REFRESH_AND_RETRY,
                )

        last_event_id = request.headers.get("Last-Event-ID")
        if _is_sse_replay_route(request.method, request.url.path):
            try:
                parsed_last_event_id = _parse_last_event_id(last_event_id)
            except ValueError:
                return protocol_response(
                    status_code=400,
                    code="invalid_event_cursor",
                    message=(f"Last-Event-ID must be an integer between 0 and {MAX_EVENT_CURSOR}"),
                    recovery=RecoveryAction.REFRESH_AND_RETRY,
                )
            if parsed_last_event_id is not None:
                return await call_next(request)

        idempotency_key = request.headers.get("Idempotency-Key")
        scope = _command_scope(request.method, request.url.path)
        if scope is None:
            return await call_next(request)
        # No current mutating API route has a canonical trailing slash. Let
        # Starlette issue its 307 before reserving the key; the redirected
        # canonical request is the command that owns the durable receipt.
        if request.url.path.endswith("/"):
            return await call_next(request)
        if idempotency_key is None:
            idempotency_key = new_id("idem")
        if not idempotency_key or len(idempotency_key) > 300:
            return protocol_response(
                status_code=400,
                code="invalid_idempotency_key",
                message="Idempotency-Key must contain between 1 and 300 characters",
            )

        body = await request.body()
        try:
            action_hash = _command_request_hash(
                scope=scope,
                path=request.url.path,
                query=request.url.query,
                body=body,
            )
        except (TypeError, ValueError):
            return protocol_response(
                status_code=400,
                code="invalid_command_payload",
                message="command payload cannot be canonicalized",
                idempotency_key=idempotency_key,
            )
        command = CommandExecution(
            command_type=scope,
            idempotency_key=idempotency_key,
            action_hash=action_hash,
        )
        try:
            execution, created = store.reserve_command_execution(command)
        except IdempotencyConflictError:
            return protocol_response(
                status_code=409,
                code="idempotency_key_conflict",
                message="Idempotency-Key was already used for a different command",
                recovery=RecoveryAction.USE_NEW_IDEMPOTENCY_KEY,
                idempotency_key=idempotency_key,
            )

        request.state.command_execution_id = execution.id
        if not created:
            if (
                execution.status
                in {CommandExecutionStatus.COMPLETED, CommandExecutionStatus.FAILED}
                and execution.response_json is not None
                and execution.http_status is not None
            ):
                try:
                    replay_payload = json.loads(execution.response_json)
                except json.JSONDecodeError:
                    replay_payload = error_payload(
                        code="stored_command_response_invalid",
                        message="stored command response is unavailable",
                        recovery=RecoveryAction.MANUAL_RECONCILE,
                    )
                    return JSONResponse(
                        status_code=500,
                        content=replay_payload,
                        headers={"Idempotency-Key": idempotency_key},
                    )
                if (
                    execution.status is CommandExecutionStatus.COMPLETED
                    and isinstance(replay_payload, dict)
                    and replay_payload.get("command_kind") == "stream"
                ):
                    return JSONResponse(
                        status_code=202,
                        content=replay_payload,
                        headers={
                            "Idempotency-Key": idempotency_key,
                            "Idempotency-Replayed": "true",
                        },
                    )
                return JSONResponse(
                    status_code=execution.http_status,
                    content=replay_payload,
                    headers={
                        "Idempotency-Key": idempotency_key,
                        "Idempotency-Replayed": "true",
                    },
                )
            if execution.status is CommandExecutionStatus.IN_PROGRESS:
                return protocol_response(
                    status_code=409,
                    code="command_in_progress",
                    message="command with this Idempotency-Key is still in progress",
                    retryable=True,
                    recovery=RecoveryAction.RETRY_SAME_IDEMPOTENCY_KEY,
                    idempotency_key=idempotency_key,
                )
            return protocol_response(
                status_code=409,
                code="command_outcome_unknown",
                message="command outcome is unknown and requires manual reconciliation",
                recovery=RecoveryAction.MANUAL_RECONCILE,
                idempotency_key=idempotency_key,
            )

        try:
            response = await call_next(request)
        except BaseException:
            store.mark_command_manual_reconcile(
                execution.id,
                error_code="response_not_started",
            )
            raise
        content_type = response.headers.get("content-type", "")
        if content_type.startswith("text/event-stream"):
            original_iterator = cast(Any, response).body_iterator

            async def receipt_stream() -> AsyncIterator[bytes | str]:
                receipt_closed = False
                first_frame_buffer = bytearray()
                try:
                    async for chunk in original_iterator:
                        if receipt_closed:
                            yield chunk
                            continue
                        first_frame_buffer.extend(
                            chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8")
                        )
                        frame_end = _first_sse_frame_end(bytes(first_frame_buffer))
                        if (
                            frame_end is None
                            and len(first_frame_buffer) > MAX_FIRST_SSE_FRAME_BYTES
                        ) or (frame_end is not None and frame_end > MAX_FIRST_SSE_FRAME_BYTES):
                            store.mark_command_manual_reconcile(
                                execution.id,
                                error_code="stream_first_frame_too_large",
                            )
                            receipt_closed = True
                            bounded_error = error_payload(
                                code="stream_first_frame_too_large",
                                message="stream ended because its first event exceeded the limit",
                                recovery=RecoveryAction.MANUAL_RECONCILE,
                            )
                            yield _sse_event("command.stream_error", bounded_error)
                            await original_iterator.aclose()
                            return
                        if frame_end is None:
                            continue
                        first_frame = bytes(first_frame_buffer[:frame_end])
                        (
                            terminal_status,
                            summary,
                            error_code,
                            stored_http_status,
                            resource_type,
                            resource_id,
                        ) = _first_stream_summary(
                            store=store,
                            command_id=execution.id,
                            path=request.url.path,
                            frame=first_frame,
                        )
                        if terminal_status is CommandExecutionStatus.COMPLETED:
                            assert summary is not None
                            store.complete_command_execution(
                                execution.id,
                                response_json=json.dumps(summary, ensure_ascii=False),
                                http_status=stored_http_status,
                                resource_type=resource_type,
                                resource_id=resource_id,
                            )
                        elif terminal_status is CommandExecutionStatus.FAILED:
                            assert summary is not None
                            store.fail_command_execution(
                                execution.id,
                                error_code=error_code,
                                http_status=stored_http_status,
                                response_json=json.dumps(summary, ensure_ascii=False),
                            )
                        else:
                            store.mark_command_manual_reconcile(
                                execution.id,
                                error_code=error_code,
                            )
                        receipt_closed = True
                        yield bytes(first_frame_buffer)
                        first_frame_buffer.clear()
                except BaseException:
                    if not receipt_closed:
                        store.mark_command_manual_reconcile(
                            execution.id,
                            error_code="stream_not_started",
                        )
                    raise
                if not receipt_closed:
                    store.mark_command_manual_reconcile(
                        execution.id,
                        error_code="stream_ended_before_first_event",
                    )

            headers = {
                key: value
                for key, value in response.headers.items()
                if key.lower() not in {"content-length", "content-type"}
            }
            headers["Idempotency-Key"] = idempotency_key
            return StreamingResponse(
                receipt_stream(),
                status_code=response.status_code,
                media_type="text/event-stream",
                headers=headers,
                background=response.background,
            )

        chunks: list[bytes] = []
        try:
            async for chunk in cast(Any, response).body_iterator:
                chunks.append(chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8"))
        except BaseException:
            store.mark_command_manual_reconcile(
                execution.id,
                error_code="response_body_incomplete",
            )
            raise
        response_body = b"".join(chunks)
        try:
            stored_payload = json.loads(response_body) if response_body else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            stored_payload = response_body.decode("utf-8", errors="replace")
        stored_payload = _stored_command_payload(
            stored_payload,
            status_code=response.status_code,
        )
        stored_json = json.dumps(stored_payload, ensure_ascii=False)
        if response.status_code >= 400:
            store.fail_command_execution(
                execution.id,
                error_code=f"http_{response.status_code}",
                http_status=response.status_code,
                response_json=stored_json,
            )
        else:
            store.complete_command_execution(
                execution.id,
                response_json=stored_json,
                http_status=response.status_code,
            )
        safe_headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower() not in {"content-length", "content-type"}
        }
        safe_headers["Idempotency-Key"] = idempotency_key
        return JSONResponse(
            content=stored_payload,
            status_code=response.status_code,
            headers=safe_headers,
            background=response.background,
        )

    web_root = Path(__file__).with_name("web")
    app.mount(
        "/web/static",
        StaticFiles(directory=web_root / "static"),
        name="web-static",
    )

    @app.get("/web", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/web/", response_class=HTMLResponse, include_in_schema=False)
    async def web_workbench() -> HTMLResponse:
        return HTMLResponse((web_root / "index.html").read_text(encoding="utf-8"))

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/protocol", response_model=None)
    async def get_protocol() -> dict[str, Any] | JSONResponse:
        try:
            return phase1e_protocol_metadata()
        except ProtocolSchemaUnavailable:
            # Do not return a made-up digest when the generated Schema line
            # has not installed its manifest yet. Clients must fail closed.
            return JSONResponse(
                status_code=503,
                content=error_payload(
                    code="protocol_schema_unavailable",
                    message="generated protocol Schema digest is unavailable",
                    recovery=RecoveryAction.RETRY_LATER,
                    retryable=True,
                ),
            )

    @app.get("/v1/projects", response_model=None)
    async def list_projects(
        after_cursor: int | None = Query(default=None, ge=0, le=MAX_EVENT_CURSOR),
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> list[dict[str, Any]] | JSONResponse:
        try:
            projects = service.list_project_projections(
                after_cursor=after_cursor,
                limit=limit,
            )
            return [project.model_dump(mode="json") for project in projects]
        except ValueError:
            return JSONResponse(
                status_code=400,
                content=error_payload(
                    code="invalid_project_cursor",
                    message="project cursor or limit is invalid",
                    recovery=RecoveryAction.REFRESH_AND_RETRY,
                ),
            )

    @app.get("/v1/workspaces/{workspace_id}/files", response_model=None)
    async def list_workspace_file_metadata(
        workspace_id: str,
        path: str = Query(default=".", min_length=1, max_length=4_096),
        limit: int = Query(default=100, ge=1, le=200),
        page_token: str | None = Query(default=None, max_length=2_000),
        snapshot: str | None = Query(default=None, max_length=64),
        after_name: str | None = Query(default=None, max_length=255),
        after: str | None = Query(default=None, max_length=255),
    ) -> dict[str, Any] | JSONResponse:
        if after_name is not None and after is not None and after_name != after:
            return JSONResponse(
                status_code=400,
                content=error_payload(
                    code="workspace_page_token_invalid",
                    message="conflicting directory page anchors were provided",
                    recovery=RecoveryAction.REFRESH_AND_RETRY,
                ),
            )
        try:
            page = service.list_workspace_files(
                workspace_id,
                path=path,
                limit=limit,
                page_token=page_token,
                snapshot=snapshot,
                after_name=after_name if after_name is not None else after,
            )
            return page.model_dump(mode="json")
        except NotFoundError:
            return JSONResponse(
                status_code=404,
                content=error_payload(
                    code="workspace_not_registered",
                    message="workspace is not registered",
                ),
            )
        except WorkspaceProjectionError as exc:
            recovery = RecoveryAction(exc.recovery)
            return JSONResponse(
                status_code=exc.status_code,
                content=error_payload(
                    code=exc.code,
                    message=exc.message,
                    recovery=recovery,
                    retryable=exc.status_code in {408, 425, 429, 502, 503, 504},
                ),
            )

    @app.get("/v1/slash-commands")
    async def list_slash_commands() -> dict[str, Any]:
        commands = service.list_slash_commands()
        return {
            "registry_version": service.slash_commands.version,
            "commands": [command.model_dump(mode="json") for command in commands],
        }

    @app.get("/v1/slash-commands/resolve")
    async def resolve_slash_command(
        text: str = Query(min_length=1, max_length=2_100),
        registry_version: str | None = Query(default=None, max_length=100),
    ) -> dict[str, Any]:
        try:
            resolved = service.resolve_slash_command(
                text,
                registry_version=registry_version,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="slash command is not registered") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid slash command query") from exc
        return resolved.model_dump(mode="json")

    @app.post("/v1/commands/workspace/init")
    async def initialize_workspace(
        request: InitializeWorkspaceRequest,
        raw_request: Request,
    ) -> dict[str, Any]:
        try:
            initialization, created = service.initialize_workspace(request.workspace)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="workspace does not exist") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="workspace is not readable") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid workspace") from exc
        audit = service.append_phase1d_audit(
            command_execution_id=raw_request.state.command_execution_id,
            command_kind=SlashCommandKind.WORKSPACE_INIT,
            event_type="workspace.registered",
            resource_type="workspace",
            resource_id=initialization.id,
            detail={
                "workspace_id": initialization.id,
                "workspace_hash": initialization.workspace_hash,
                "created": created,
                "readable": initialization.readable,
                "writable": initialization.writable,
            },
        )
        return {
            "id": initialization.id,
            "cursor": initialization.cursor,
            "audit_cursor": audit.cursor,
            "workspace_hash": initialization.workspace_hash,
            "readable": initialization.readable,
            "writable": initialization.writable,
            "created": created,
            "created_at": initialization.created_at,
        }

    async def apply_context_baseline(
        request: ContextBaselineRequest,
        *,
        operation: ContextBaselineOperation,
        command_execution_id: str,
    ) -> dict[str, Any]:
        try:
            baseline = service.append_context_baseline(
                session_id=request.session_id,
                thread_id=request.thread_id,
                operation=operation,
                agent_id=request.agent_id,
            )
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail="Context scope was not found") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="Context scope is not allowed") from exc
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail="Context baseline conflicts") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid Context baseline request") from exc
        command_kind = (
            SlashCommandKind.CONTEXT_CLEAR
            if operation is ContextBaselineOperation.CLEAR
            else SlashCommandKind.CONTEXT_COMPACT
        )
        audit = service.append_phase1d_audit(
            command_execution_id=command_execution_id,
            command_kind=command_kind,
            event_type=f"context.{operation.value}",
            resource_type="context_baseline",
            resource_id=baseline.id,
            detail={
                "baseline_id": baseline.id,
                "session_id": baseline.session_id,
                "thread_id": baseline.thread_id,
                "item_cursor_end": baseline.item_cursor_end,
                "compaction_id": baseline.compaction_id,
            },
        )
        result = baseline.model_dump(mode="json")
        result["audit_cursor"] = audit.cursor
        return result

    @app.post("/v1/commands/context/clear")
    async def clear_context(
        request: ContextBaselineRequest,
        raw_request: Request,
    ) -> dict[str, Any]:
        return await apply_context_baseline(
            request,
            operation=ContextBaselineOperation.CLEAR,
            command_execution_id=raw_request.state.command_execution_id,
        )

    @app.post("/v1/commands/context/compact")
    async def compact_context(
        request: ContextBaselineRequest,
        raw_request: Request,
    ) -> dict[str, Any]:
        return await apply_context_baseline(
            request,
            operation=ContextBaselineOperation.COMPACT,
            command_execution_id=raw_request.state.command_execution_id,
        )

    @app.get("/v1/context-baselines")
    async def list_context_baselines(
        session_id: str = Query(min_length=1, max_length=300),
        thread_id: str = Query(min_length=1, max_length=300),
        after_cursor: int | None = Query(default=None, ge=0, le=MAX_EVENT_CURSOR),
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> list[dict[str, Any]]:
        try:
            baselines = service.list_context_baselines(
                session_id,
                thread_id,
                after_cursor=after_cursor,
                limit=limit,
            )
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail="Context scope was not found") from exc
        return [baseline.model_dump(mode="json") for baseline in baselines]

    @app.post("/v1/commands/review")
    async def run_review(
        request: ReviewCommandRequest,
        raw_request: Request,
    ) -> StreamingResponse:
        async def events() -> AsyncIterator[str]:
            try:
                async for event in service.run_review(
                    command_execution_id=raw_request.state.command_execution_id,
                    reviewer_role_id=request.reviewer_role_id,
                    workspace=request.workspace,
                    scope=request.scope,
                    thread_id=request.thread_id,
                ):
                    yield _sse_event(
                        event.event_type,
                        _phase1d_audit_payload(event),
                        cursor=event.cursor,
                    )
            except asyncio.CancelledError:
                raise
            except (NotFoundError, ValueError, PermissionError) as exc:
                payload = _phase1d_stream_start_error("review", exc)
                yield _sse_event("review.stream_error", payload)

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.get("/v1/reviews/{review_run_id}")
    async def get_review_run(review_run_id: str) -> dict[str, Any]:
        try:
            run = store.get_review_run(review_run_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail="Review run not found") from exc
        return {
            "id": run.id,
            "cursor": run.cursor,
            "session_id": run.session_id,
            "thread_id": run.thread_id,
            "scope_hash": hashlib.sha256(run.scope.encode("utf-8")).hexdigest(),
            "status": run.status.value,
            "artifact_id": run.artifact_id,
            "error_code": run.error_code,
            "created_at": run.created_at,
            "updated_at": run.updated_at,
        }

    @app.get("/v1/command-executions/{command_execution_id}/events")
    async def list_command_audit_events(
        command_execution_id: str,
        after_cursor: int | None = Query(default=None, ge=0, le=MAX_EVENT_CURSOR),
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> list[dict[str, Any]]:
        events = service.list_phase1d_audit_events(
            command_execution_id,
            after_cursor=after_cursor,
            limit=limit,
        )
        return [
            {"event_type": event.event_type, **_phase1d_audit_payload(event)} for event in events
        ]

    @app.get("/v1/command-executions/{command_execution_id}/events/stream")
    async def stream_command_audit_events(
        command_execution_id: str,
        raw_request: Request,
    ) -> StreamingResponse:
        after_cursor = _parse_last_event_id(raw_request.headers.get("Last-Event-ID"))
        events = service.list_phase1d_audit_events(
            command_execution_id,
            after_cursor=after_cursor,
            limit=1000,
        )

        async def replay() -> AsyncIterator[str]:
            for event in events:
                yield _sse_event(
                    event.event_type,
                    _phase1d_audit_payload(event),
                    cursor=event.cursor,
                )

        return StreamingResponse(replay(), media_type="text/event-stream")

    @app.post("/v1/sidecars/btw")
    async def run_btw_sidecar(request: BTWSidecarRequest) -> StreamingResponse:
        async def events() -> AsyncIterator[str]:
            try:
                async for event in service.run_btw_sidecar(
                    session_id=request.session_id,
                    thread_id=request.thread_id,
                    workspace=request.workspace,
                    prompt=request.prompt,
                ):
                    yield _sse_event(
                        event.event_type,
                        event.model_dump(mode="json"),
                        cursor=event.cursor,
                    )
            except asyncio.CancelledError:
                raise
            except (NotFoundError, ValueError, PermissionError) as exc:
                payload = _phase1d_stream_start_error("btw", exc)
                yield _sse_event("btw.stream_error", payload)

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.get("/v1/sidecars/btw/{run_id}")
    async def get_btw_sidecar(run_id: str) -> dict[str, Any]:
        try:
            run = service.get_btw_sidecar_run(run_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail="BTW Sidecar run not found") from exc
        return {
            "id": run.id,
            "cursor": run.cursor,
            "session_id": run.session_id,
            "agent_id": run.agent_id,
            "thread_id": run.thread_id,
            "source_item_cursor_end": run.source_item_cursor_end,
            "status": run.status.value,
            "response": run.response,
            "context_revision_id": run.context_revision_id,
            "promoted_turn_id": run.promoted_turn_id,
            "promoted_item_id": run.promoted_item_id,
            "error_code": run.error_code,
            "created_at": run.created_at,
            "updated_at": run.updated_at,
        }

    @app.get("/v1/sidecars/btw/{run_id}/events")
    async def list_btw_events(
        run_id: str,
        after_cursor: int | None = Query(default=None, ge=0, le=MAX_EVENT_CURSOR),
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> list[dict[str, Any]]:
        try:
            events = service.list_btw_sidecar_events(
                run_id,
                after_cursor=after_cursor,
                limit=limit,
            )
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail="BTW Sidecar run not found") from exc
        return [event.model_dump(mode="json") for event in events]

    @app.post("/v1/sidecars/btw/{run_id}/cancel")
    async def cancel_btw_sidecar(run_id: str) -> dict[str, bool]:
        try:
            accepted = service.cancel_btw_sidecar(run_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail="BTW Sidecar run not found") from exc
        return {"accepted": accepted}

    @app.get("/v1/sidecars/btw/{run_id}/events/stream")
    async def stream_btw_events(run_id: str, raw_request: Request) -> StreamingResponse:
        after_cursor = _parse_last_event_id(raw_request.headers.get("Last-Event-ID"))
        try:
            committed = service.list_btw_sidecar_events(
                run_id,
                after_cursor=after_cursor,
                limit=1000,
            )
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail="BTW Sidecar run not found") from exc

        async def replay() -> AsyncIterator[str]:
            for event in committed:
                yield _sse_event(
                    event.event_type,
                    event.model_dump(mode="json"),
                    cursor=event.cursor,
                )

        return StreamingResponse(replay(), media_type="text/event-stream")

    @app.post("/v1/sidecars/btw/{run_id}/promote")
    async def promote_btw_sidecar(run_id: str) -> dict[str, Any]:
        try:
            run, turn, item = service.promote_btw_sidecar(run_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail="BTW Sidecar run not found") from exc
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail="BTW Sidecar cannot be promoted") from exc
        return {
            "sidecar_run_id": run.id,
            "status": run.status.value,
            "turn_id": turn.id,
            "item_id": item.id,
        }

    @app.get("/v1/models")
    async def list_models() -> list[dict[str, object]]:
        return [profile.model_dump(mode="json") for profile in service.list_model_profiles()]

    @app.post("/v1/models", status_code=201)
    async def create_model(
        request: CreateModelProfileRequest,
    ) -> dict[str, object]:
        try:
            profile = service.add_model_profile(request.to_domain())
        except (ConflictError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return profile.model_dump(mode="json")

    @app.get("/v1/models/{profile_id}")
    async def get_model(profile_id: str) -> dict[str, object]:
        try:
            profile = service.get_model_profile(profile_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return profile.model_dump(mode="json")

    @app.patch("/v1/models/{profile_id}")
    async def update_model(
        profile_id: str, request: UpdateModelProfileRequest
    ) -> dict[str, object]:
        try:
            profile = service.update_model_profile(
                profile_id, **request.model_dump(exclude_unset=True)
            )
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return profile.model_dump(mode="json")

    @app.delete("/v1/models/{profile_id}")
    async def deactivate_model(profile_id: str) -> dict[str, object]:
        try:
            profile = service.deactivate_model_profile(profile_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return profile.model_dump(mode="json")

    @app.post("/v1/models/discover")
    async def discover_models(
        request: DiscoverModelsRequest,
    ) -> dict[str, list[str]]:
        try:
            model_ids = await service.discover_models(
                base_url=request.base_url,
                secret_ref=request.secret_ref,
            )
        except ProviderError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"model_ids": model_ids}

    @app.post("/v1/models/{profile_id}/health")
    async def check_model(profile_id: str) -> dict[str, object]:
        try:
            return await service.check_model_profile(profile_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ProviderError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/v1/roles")
    async def list_roles(
        include_inactive: bool = False,
    ) -> list[dict[str, object]]:
        return [
            role.model_dump(mode="json")
            for role in service.list_roles(include_inactive=include_inactive)
        ]

    @app.post("/v1/roles", status_code=201)
    async def create_role(
        request: CreateRoleRequest,
    ) -> dict[str, object]:
        try:
            role = service.create_role(request.to_domain())
        except (ConflictError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return role.model_dump(mode="json")

    @app.post("/v1/roles/seed-defaults")
    async def seed_default_roles(
        request: SeedRolesRequest,
    ) -> list[dict[str, object]]:
        try:
            roles = service.seed_default_roles(**request.model_dump())
        except (NotFoundError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return [role.model_dump(mode="json") for role in roles]

    @app.get("/v1/roles/{role_id}")
    async def get_role(role_id: str, version: int | None = None) -> dict[str, object]:
        try:
            role = service.get_role(role_id, version)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return role.model_dump(mode="json")

    @app.get("/v1/roles/{role_id}/versions")
    async def list_role_versions(
        role_id: str,
    ) -> list[dict[str, object]]:
        try:
            roles = service.list_role_versions(role_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return [role.model_dump(mode="json") for role in roles]

    @app.patch("/v1/roles/{role_id}")
    async def update_role(role_id: str, request: UpdateRoleRequest) -> dict[str, object]:
        try:
            role = service.update_role(role_id, **request.model_dump(exclude_unset=True))
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return role.model_dump(mode="json")

    @app.post("/v1/roles/{role_id}/copy", status_code=201)
    async def copy_role(role_id: str, request: CopyRoleRequest) -> dict[str, object]:
        try:
            role = service.copy_role(role_id, name=request.name)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return role.model_dump(mode="json")

    @app.delete("/v1/roles/{role_id}")
    async def deactivate_role(role_id: str) -> dict[str, object]:
        try:
            role = service.deactivate_role(role_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return role.model_dump(mode="json")

    @app.get("/v1/threads")
    async def list_threads(
        after_cursor: int | None = Query(default=None, ge=0, le=MAX_EVENT_CURSOR),
        limit: int = Query(default=100, ge=1, le=1000),
        parent_thread_id: str | None = Query(default=None, max_length=300),
        workspace_ref: str | None = Query(default=None, max_length=2048),
        status: ThreadStatus | None = None,
    ) -> list[dict[str, object]]:
        threads = service.list_threads(
            after_cursor=after_cursor,
            limit=limit,
            parent_thread_id=parent_thread_id,
            workspace_ref=workspace_ref,
            status=status,
        )
        return [thread.model_dump(mode="json") for thread in threads]

    @app.post("/v1/threads", status_code=201)
    async def create_thread(request: CreateThreadRequest) -> dict[str, object]:
        try:
            thread = service.create_thread(request.to_domain())
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return thread.model_dump(mode="json")

    @app.get("/v1/threads/{thread_id}")
    async def get_thread(thread_id: str) -> dict[str, object]:
        try:
            thread = service.get_thread(thread_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return thread.model_dump(mode="json")

    @app.post("/v1/threads/{thread_id}/archive")
    async def archive_thread(thread_id: str) -> dict[str, object]:
        try:
            thread = service.archive_thread(thread_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return thread.model_dump(mode="json")

    @app.get("/v1/threads/{thread_id}/turns")
    async def list_turns(
        thread_id: str,
        after_cursor: int | None = Query(default=None, ge=0, le=MAX_EVENT_CURSOR),
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> list[dict[str, object]]:
        try:
            turns = service.list_turns(
                thread_id,
                after_cursor=after_cursor,
                limit=limit,
            )
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return [turn.model_dump(mode="json") for turn in turns]

    @app.post("/v1/threads/{thread_id}/turns", status_code=201)
    async def create_turn(
        thread_id: str,
        request: CreateTurnRequest,
    ) -> dict[str, object]:
        try:
            turn = service.create_turn(request.to_domain(thread_id))
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return turn.model_dump(mode="json")

    @app.get("/v1/threads/{thread_id}/items")
    async def list_thread_items(
        thread_id: str,
        after_cursor: int | None = Query(default=None, ge=0, le=MAX_EVENT_CURSOR),
        limit: int = Query(default=100, ge=1, le=1000),
        turn_id: str | None = Query(default=None, max_length=300),
    ) -> list[dict[str, object]]:
        try:
            items = service.list_items(
                thread_id,
                after_cursor=after_cursor,
                limit=limit,
                turn_id=turn_id,
            )
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return [item.model_dump(mode="json") for item in items]

    @app.get("/v1/threads/{thread_id}/items/stream")
    async def stream_thread_items(
        thread_id: str,
        raw_request: Request,
        after_cursor: int | None = Query(default=None, ge=0, le=MAX_EVENT_CURSOR),
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> StreamingResponse:
        header_cursor = _parse_last_event_id(raw_request.headers.get("Last-Event-ID"))
        replay_cursor = header_cursor if header_cursor is not None else after_cursor
        try:
            items = service.list_items(
                thread_id,
                after_cursor=replay_cursor,
                limit=limit,
            )
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        async def replay_items() -> AsyncIterator[str]:
            for item in items:
                yield _sse_event(
                    "thread.item.appended",
                    item.model_dump(mode="json"),
                    cursor=item.cursor,
                )

        return StreamingResponse(replay_items(), media_type="text/event-stream")

    @app.post("/v1/threads/{thread_id}/turns/{turn_id}/items", status_code=201)
    async def append_thread_item(
        thread_id: str,
        turn_id: str,
        request: AppendItemRequest,
    ) -> dict[str, object]:
        try:
            item = service.append_item(request.to_domain(thread_id=thread_id, turn_id=turn_id))
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return item.model_dump(mode="json")

    @app.get("/v1/artifacts")
    async def list_artifacts(
        after_cursor: int | None = Query(default=None, ge=0, le=MAX_EVENT_CURSOR),
        limit: int = Query(default=100, ge=1, le=1000),
        sensitivity: ArtifactSensitivity | None = None,
    ) -> list[dict[str, object]]:
        artifacts = service.list_artifacts(
            after_cursor=after_cursor,
            limit=limit,
            sensitivity=sensitivity,
        )
        return [artifact.model_dump(mode="json") for artifact in artifacts]

    @app.post("/v1/artifacts", responses={200: {"description": "Deduplicated artifact"}})
    async def create_artifact(request: CreateArtifactRequest) -> JSONResponse:
        try:
            content = request.decoded_content()
            artifact, created = service.create_artifact(
                content=content,
                media_type=request.media_type,
                sensitivity=request.sensitivity,
                source_refs=request.source_refs,
                retention_policy_ref=request.retention_policy_ref,
            )
        except ArtifactTooLargeError as exc:
            raise HTTPException(status_code=413, detail="artifact content is too large") from exc
        except ArtifactValidationError as exc:
            raise HTTPException(status_code=400, detail="artifact content is invalid") from exc
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ArtifactStoreError as exc:
            raise HTTPException(status_code=500, detail="artifact storage failed safely") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(
            status_code=201 if created else 200,
            content=artifact.model_dump(mode="json"),
        )

    @app.get("/v1/artifacts/{artifact_id}")
    async def get_artifact(artifact_id: str) -> Response:
        try:
            artifact = service.get_artifact(artifact_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ArtifactCorruptionError, ArtifactNotFoundError, ArtifactSecurityError):
            return protocol_response(
                status_code=409,
                code="artifact_integrity_failed",
                message="artifact content failed integrity verification",
                recovery=RecoveryAction.MANUAL_RECONCILE,
            )
        except ArtifactStoreError as exc:
            raise HTTPException(status_code=500, detail="artifact storage failed safely") from exc
        return JSONResponse(content=artifact.model_dump(mode="json"))

    @app.get("/v1/artifacts/{artifact_id}/content")
    async def read_artifact_content(artifact_id: str, request: Request) -> Response:
        try:
            payload = service.read_artifact_text(
                artifact_id,
                capability=_artifact_capability_from_request(request),
            )
        except ArtifactCapabilityError as exc:
            raise HTTPException(status_code=403, detail="Artifact access is denied") from exc
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail="Artifact not found") from exc
        except ArtifactContentDeletedError:
            return protocol_response(
                status_code=410,
                code="artifact_content_deleted",
                message="Artifact content was explicitly deleted",
            )
        except (ArtifactCorruptionError, ArtifactNotFoundError, ArtifactSecurityError):
            return protocol_response(
                status_code=409,
                code="artifact_integrity_failed",
                message="Artifact content failed integrity verification",
                recovery=RecoveryAction.MANUAL_RECONCILE,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(content=payload)

    @app.get("/v1/artifacts/{artifact_id}/download")
    async def download_artifact(artifact_id: str, request: Request) -> Response:
        try:
            artifact, content = service.download_artifact(
                artifact_id,
                capability=_artifact_capability_from_request(request),
            )
        except ArtifactCapabilityError as exc:
            raise HTTPException(status_code=403, detail="Artifact access is denied") from exc
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail="Artifact not found") from exc
        except ArtifactContentDeletedError:
            return protocol_response(
                status_code=410,
                code="artifact_content_deleted",
                message="Artifact content was explicitly deleted",
            )
        except (ArtifactCorruptionError, ArtifactNotFoundError, ArtifactSecurityError):
            return protocol_response(
                status_code=409,
                code="artifact_integrity_failed",
                message="Artifact content failed integrity verification",
                recovery=RecoveryAction.MANUAL_RECONCILE,
            )
        return Response(
            content=content,
            media_type=artifact.media_type,
            headers={
                "Content-Disposition": 'attachment; filename="artifact.bin"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/v1/artifacts/{artifact_id}/export")
    async def export_artifact(
        artifact_id: str,
        export_request: ExportArtifactRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            return service.export_artifact(
                artifact_id,
                capability=_artifact_capability_from_request(request),
                workspace_root=export_request.workspace_root,
                relative_path=export_request.relative_path,
            )
        except ArtifactCapabilityError as exc:
            raise HTTPException(status_code=403, detail="Artifact access is denied") from exc
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail="Artifact not found") from exc
        except ArtifactExportOutcomeUnknownError:
            raise
        except (
            ArtifactContentDeletedError,
            ArtifactCorruptionError,
            ArtifactNotFoundError,
            ArtifactSecurityError,
        ) as exc:
            raise HTTPException(status_code=409, detail="Artifact export failed safely") from exc
        except ArtifactStoreError as exc:
            raise HTTPException(status_code=500, detail="Artifact export failed safely") from exc

    @app.get("/v1/artifacts/{artifact_id}/retention")
    async def get_artifact_retention(artifact_id: str) -> dict[str, Any]:
        try:
            state = service.get_artifact_retention_state(artifact_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return state.model_dump(mode="json")

    def retention_response(action: Any) -> dict[str, Any]:
        try:
            state = action()
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ArtifactCapabilityError, PermissionError) as exc:
            raise HTTPException(status_code=403, detail="Artifact action is denied") from exc
        return cast(dict[str, Any], state.model_dump(mode="json"))

    @app.post("/v1/artifacts/{artifact_id}/retention/pin")
    async def set_artifact_pin(
        artifact_id: str,
        pin_request: SetArtifactPinRequest,
        request: Request,
    ) -> dict[str, Any]:
        capability = _artifact_capability_from_request(request)
        return retention_response(
            lambda: service.set_artifact_pin(
                artifact_id,
                pinned=pin_request.pinned,
                capability=capability,
            )
        )

    @app.post("/v1/artifacts/{artifact_id}/retention/archive")
    async def archive_artifact(artifact_id: str, request: Request) -> dict[str, Any]:
        capability = _artifact_capability_from_request(request)
        return retention_response(
            lambda: service.archive_artifact(artifact_id, capability=capability)
        )

    @app.post("/v1/artifacts/{artifact_id}/retention/schedule-deletion")
    async def schedule_artifact_deletion(
        artifact_id: str,
        request: Request,
    ) -> dict[str, Any]:
        capability = _artifact_capability_from_request(request)
        return retention_response(
            lambda: service.schedule_artifact_deletion(
                artifact_id,
                capability=capability,
            )
        )

    @app.post("/v1/artifacts/{artifact_id}/retention/trash")
    async def trash_artifact(artifact_id: str, request: Request) -> dict[str, Any]:
        capability = _artifact_capability_from_request(request)
        return retention_response(
            lambda: service.trash_artifact(artifact_id, capability=capability)
        )

    @app.post("/v1/artifacts/{artifact_id}/retention/restore")
    async def restore_artifact(artifact_id: str, request: Request) -> dict[str, Any]:
        capability = _artifact_capability_from_request(request)
        return retention_response(
            lambda: service.restore_artifact(artifact_id, capability=capability)
        )

    @app.post("/v1/artifacts/{artifact_id}/retention/physical-delete")
    async def physically_delete_artifact(
        artifact_id: str,
        request: Request,
    ) -> dict[str, Any]:
        capability = _artifact_capability_from_request(request)
        return retention_response(
            lambda: service.physically_delete_artifact(
                artifact_id,
                capability=capability,
            )
        )

    @app.post("/v1/artifacts/{artifact_id}/retention/reconcile-physical-delete")
    async def reconcile_physically_deleted_artifact(
        artifact_id: str,
        reconcile_request: ReconcilePhysicalDeleteRequest,
        request: Request,
    ) -> dict[str, Any]:
        capability = _artifact_capability_from_request(request)
        return retention_response(
            lambda: service.reconcile_physically_deleted_artifact(
                artifact_id,
                prior_command_id=reconcile_request.prior_command_id,
                prior_action_hash=reconcile_request.prior_action_hash,
                capability=capability,
            )
        )

    @app.post("/v1/artifact-repairs/orphan-blob")
    async def repair_orphan_blob(
        repair_request: RepairOrphanBlobRequest,
        request: Request,
    ) -> dict[str, Any]:
        try:
            result = service.repair_orphan_blob(
                content_hash=repair_request.content_hash,
                finding_hash=repair_request.finding_hash,
                capability=_artifact_capability_from_request(request),
            )
        except ArtifactCapabilityError as exc:
            raise HTTPException(status_code=403, detail="Artifact repair is denied") from exc
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return result.model_dump(mode="json")

    @app.post("/v1/retention-policies", status_code=201)
    async def create_retention_policy(
        request: CreateRetentionPolicyRequest,
    ) -> dict[str, Any]:
        try:
            policy = service.create_retention_policy(request.to_domain())
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return policy.model_dump(mode="json")

    @app.get("/v1/artifact-audits")
    async def audit_artifacts() -> dict[str, Any]:
        return service.audit_artifacts().model_dump(mode="json")

    @app.get("/v1/cache-observations")
    async def list_cache_observations(
        context_revision_id: str | None = None,
        after_cursor: int | None = Query(default=None, ge=0, le=MAX_EVENT_CURSOR),
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> list[dict[str, Any]]:
        observations = service.list_cache_observations(
            context_revision_id=context_revision_id,
            after_cursor=after_cursor,
            limit=limit,
        )
        return [observation.model_dump(mode="json") for observation in observations]

    @app.post("/v1/sessions", status_code=201)
    async def create_session(
        request: CreateSessionRequest,
    ) -> dict[str, object]:
        try:
            session = service.create_session(
                request.role_id,
                new_role=(None if request.new_role is None else request.new_role.to_domain()),
                model_profile_id=request.model_profile_id,
                effort=(None if request.effort is None else request.effort.value),
                budget_overrides=request.budget_overrides,
            )
        except (NotFoundError, ConflictError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return session.model_dump(mode="json")

    @app.get("/v1/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, object]:
        try:
            session = service.get_session(session_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return session.model_dump(mode="json")

    @app.get("/v1/sessions/{session_id}/events")
    async def list_events(
        session_id: str,
        after_cursor: int | None = Query(default=None, ge=0, le=MAX_EVENT_CURSOR),
        limit: int = Query(default=1000, ge=1, le=1000),
    ) -> list[dict[str, object]]:
        try:
            events = service.list_events(
                session_id,
                after_cursor=after_cursor,
                limit=limit,
            )
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return [event.model_dump(mode="json") for event in events]

    @app.get("/v1/sessions/{session_id}/context-revisions")
    async def list_context_revisions(
        session_id: str,
        after_cursor: int | None = Query(default=None, ge=0, le=MAX_EVENT_CURSOR),
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> list[dict[str, Any]]:
        try:
            revisions = service.list_context_revisions(
                session_id,
                after_cursor=after_cursor,
                limit=limit,
            )
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return [_context_revision_evidence(revision) for revision in revisions]

    @app.get("/v1/sessions/{session_id}/context-revisions/{revision_id}")
    async def get_context_revision(
        session_id: str,
        revision_id: str,
    ) -> dict[str, Any]:
        try:
            revision = service.get_context_revision(session_id, revision_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _context_revision_evidence(revision)

    @app.post("/v1/sessions/{session_id}/runs")
    async def run_session(
        session_id: str,
        request: RunSessionRequest,
        raw_request: Request,
    ) -> Response:
        try:
            service.get_session(session_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        after_cursor = _parse_last_event_id(raw_request.headers.get("Last-Event-ID"))
        if after_cursor is not None:
            committed = service.list_events(
                session_id,
                after_cursor=after_cursor,
                limit=1000,
            )

            async def replay_events() -> AsyncIterator[str]:
                for event in committed:
                    turn = event.payload.get("turn", 0)
                    payload = {key: value for key, value in event.payload.items() if key != "turn"}
                    data = {
                        "event_type": event.event_type,
                        "turn": turn if isinstance(turn, int) and turn >= 0 else 0,
                        "cursor": event.cursor,
                        "payload": payload,
                    }
                    yield _sse_event(event.event_type, data, cursor=event.cursor)

            return StreamingResponse(replay_events(), media_type="text/event-stream")

        try:
            admitted = service.admit_session_run(session_id)
        except ConflictError as exc:
            manual_reconcile = isinstance(exc, ActionOutcomeUnknownError) or (
                "pending durable approval" in str(exc)
            )
            return protocol_response(
                status_code=409,
                code=(
                    "session_manual_reconcile_required"
                    if isinstance(exc, ActionOutcomeUnknownError)
                    else "session_pending_approval"
                    if manual_reconcile
                    else "session_run_conflict"
                ),
                message=redact_public_text(str(exc), max_chars=300),
                retryable=not manual_reconcile,
                recovery=(
                    RecoveryAction.MANUAL_RECONCILE
                    if manual_reconcile
                    else RecoveryAction.RETRY_LATER
                ),
            )
        if not admitted:
            return protocol_response(
                status_code=409,
                code="session_run_conflict",
                message="session already has an active run",
                retryable=True,
                recovery=RecoveryAction.RETRY_LATER,
            )
        admitted_lease = service.admitted_session_run_lease(session_id)
        if admitted_lease is None:
            return protocol_response(
                status_code=409,
                code="session_run_conflict",
                message="session run lease is unavailable",
                retryable=True,
                recovery=RecoveryAction.RETRY_LATER,
            )

        async def stream_events() -> AsyncIterator[str]:
            try:
                async for event in service.run_session(
                    session_id,
                    user_message=request.message,
                    workspace=request.workspace,
                    thread_id=request.thread_id,
                    references=request.references,
                    _admission_granted=True,
                ):
                    yield _sse_event(
                        event.event_type,
                        event.model_dump(mode="json"),
                        cursor=event.cursor,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                payload = error_payload(
                    code="session_stream_failed",
                    message="session stream stopped before completion",
                    recovery=RecoveryAction.MANUAL_RECONCILE,
                )
                payload.update(
                    {
                        "error_type": "stream_error",
                        "message": "session stream stopped before completion",
                    }
                )
                yield _sse_event("agent.stream_error", payload)
            finally:
                service.release_session_run(session_id, admitted_lease)

        return StreamingResponse(
            stream_events(),
            media_type="text/event-stream",
            background=BackgroundTask(
                service.release_session_run,
                session_id,
                admitted_lease,
            ),
        )

    @app.post("/v1/sessions/{session_id}/cancel")
    async def cancel_session(session_id: str) -> dict[str, bool]:
        try:
            accepted = service.cancel_session(session_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"accepted": accepted}

    @app.get("/v1/sessions/{session_id}/approvals")
    async def list_approvals(
        session_id: str,
    ) -> list[dict[str, object]]:
        try:
            return service.list_pending_approvals(session_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/v1/sessions/{session_id}/approvals/{tool_call_id}")
    async def decide_approval(
        session_id: str,
        tool_call_id: str,
        request: ApprovalDecisionRequest,
    ) -> dict[str, object]:
        try:
            return service.decide_approval(
                session_id,
                tool_call_id,
                approved=request.approved,
            )
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v1/workflows/coding/runs")
    @app.post("/v1/tasks")
    async def run_coding_workflow(
        request: WorkflowRunRequest,
        raw_request: Request,
    ) -> StreamingResponse:
        if raw_request.headers.get("Last-Event-ID") is not None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Last-Event-ID requires an existing workflow run; use the task event query"
                ),
            )
        try:
            workflow.validate_configuration(
                planner_role_id=request.planner_role_id,
                explorer_role_ids=request.explorer_role_ids,
                coder_role_id=request.coder_role_id,
                reviewer_role_id=request.reviewer_role_id,
                max_parallel_explorers=request.max_parallel_explorers,
                main_role_id=request.main_role_id,
            )
        except (NotFoundError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        async def stream_events() -> AsyncIterator[str]:
            try:
                async for event in workflow.run(
                    task=request.task,
                    workspace=request.workspace,
                    main_role_id=request.main_role_id,
                    planner_role_id=request.planner_role_id,
                    explorer_role_ids=request.explorer_role_ids,
                    coder_role_id=request.coder_role_id,
                    reviewer_role_id=request.reviewer_role_id,
                    max_parallel_explorers=request.max_parallel_explorers,
                    max_rework_rounds=request.max_rework_rounds,
                ):
                    yield _sse_event(
                        event.event_type,
                        event.model_dump(mode="json"),
                        cursor=event.cursor,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                payload = error_payload(
                    code="workflow_stream_failed",
                    message="workflow stream stopped before completion",
                    recovery=RecoveryAction.MANUAL_RECONCILE,
                )
                payload.update(
                    {
                        "error_type": "stream_error",
                        "message": "workflow stream stopped before completion",
                    }
                )
                yield _sse_event("workflow.stream_error", payload)

        return StreamingResponse(stream_events(), media_type="text/event-stream")

    @app.get("/v1/tasks")
    async def list_workflow_runs() -> list[dict[str, object]]:
        return [run.model_dump(mode="json") for run in service.list_workflow_runs()]

    @app.get("/v1/tasks/{workflow_run_id}")
    async def get_workflow_run(workflow_run_id: str) -> dict[str, object]:
        try:
            run = service.get_workflow_run(workflow_run_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return run.model_dump(mode="json")

    @app.get("/v1/tasks/{workflow_run_id}/events")
    async def list_workflow_run_events(
        workflow_run_id: str,
        after_cursor: int | None = Query(default=None, ge=0, le=MAX_EVENT_CURSOR),
        limit: int = Query(default=1000, ge=1, le=1000),
    ) -> list[dict[str, object]]:
        try:
            events = service.list_workflow_events(
                workflow_run_id,
                after_cursor=after_cursor,
                limit=limit,
            )
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return [event.model_dump(mode="json") for event in events]

    @app.get("/v1/tasks/{workflow_run_id}/trace")
    async def get_workflow_trace(workflow_run_id: str) -> dict[str, object]:
        try:
            trace = service.get_workflow_trace(workflow_run_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return trace.model_dump(mode="json")

    @app.get("/v1/tasks/{workflow_run_id}/trace.jsonl")
    async def export_workflow_trace(workflow_run_id: str) -> StreamingResponse:
        try:
            lines = service.export_workflow_trace_jsonl(workflow_run_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        async def stream_lines() -> AsyncIterator[str]:
            for line in lines:
                yield f"{line}\n"

        return StreamingResponse(
            stream_lines(),
            media_type="application/x-ndjson",
            headers={
                "Content-Disposition": (f'attachment; filename="{workflow_run_id}-trace.jsonl"')
            },
        )

    @app.post("/v1/tasks/{workflow_run_id}/resume")
    async def resume_workflow_run(
        workflow_run_id: str,
        request: WorkflowResumeRequest,
        raw_request: Request,
    ) -> StreamingResponse:
        try:
            service.get_workflow_run(workflow_run_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        after_cursor = _parse_last_event_id(raw_request.headers.get("Last-Event-ID"))
        if after_cursor is not None:
            committed = service.list_workflow_events(
                workflow_run_id,
                after_cursor=after_cursor,
                limit=1000,
            )

            async def replay_events() -> AsyncIterator[str]:
                for event in committed:
                    data = WorkflowEvent(
                        workflow_run_id=event.workflow_run_id,
                        cursor=event.cursor,
                        role=event.role,
                        session_id=event.session_id or "",
                        event_type=event.event_type,
                        payload=event.payload,
                    ).model_dump(mode="json")
                    yield _sse_event(event.event_type, data, cursor=event.cursor)

            return StreamingResponse(replay_events(), media_type="text/event-stream")

        async def stream_events() -> AsyncIterator[str]:
            try:
                async for event in workflow.resume(
                    workflow_run_id,
                    allow_coder_replay=request.allow_coder_replay,
                ):
                    yield _sse_event(
                        event.event_type,
                        event.model_dump(mode="json"),
                        cursor=event.cursor,
                    )
            except asyncio.CancelledError:
                raise
            except ValueError:
                error = error_payload(
                    code="workflow_resume_rejected",
                    message="workflow resume request was rejected",
                    recovery=RecoveryAction.MANUAL_RECONCILE,
                )
                error.update(
                    {
                        "error_type": "ValueError",
                        "message": "workflow resume request was rejected",
                    }
                )
                yield _sse_event("workflow.resume_rejected", error)
            except Exception:
                error = error_payload(
                    code="workflow_resume_failed",
                    message="workflow resume stream stopped before completion",
                    recovery=RecoveryAction.MANUAL_RECONCILE,
                )
                error.update(
                    {
                        "error_type": "stream_error",
                        "message": "workflow resume stream stopped before completion",
                    }
                )
                yield _sse_event("workflow.resume_failed", error)

        return StreamingResponse(stream_events(), media_type="text/event-stream")

    @app.post("/v1/tasks/{workflow_run_id}/cancel")
    async def cancel_workflow_run(workflow_run_id: str) -> dict[str, bool]:
        try:
            accepted = service.cancel_workflow_run(workflow_run_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"accepted": accepted}

    # Evaluation Runner v1. The HTTP layer only translates typed Service/
    # Runner calls; SQLite remains behind ApplicationService.

    @app.get("/v1/evaluations/suites")
    async def list_evaluation_suites(
        status: str | None = None,
        limit: int | None = Query(default=None, ge=1),
    ) -> list[dict[str, Any]]:
        try:
            suites = service.list_evaluation_suites(status=status, limit=limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid evaluation suite query") from exc
        return [suite.model_dump(mode="json") for suite in suites]

    @app.post("/v1/evaluations/suites", status_code=201)
    async def create_evaluation_suite(request: dict[str, Any]) -> dict[str, Any]:
        try:
            suite = EvaluationSuite.model_validate(request)
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail="invalid evaluation suite") from exc
        try:
            created = service.create_evaluation_suite(suite)
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail="evaluation suite already exists") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid evaluation suite") from exc
        return created.model_dump(mode="json")

    @app.get("/v1/evaluations/suites/{suite_id}")
    async def get_evaluation_suite(suite_id: str) -> dict[str, Any]:
        try:
            suite = service.get_evaluation_suite(suite_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail="evaluation suite not found") from exc
        return suite.model_dump(mode="json")

    @app.get("/v1/evaluations/runs")
    async def list_evaluation_runs(
        suite_id: str | None = None,
        status: str | None = None,
        limit: int | None = Query(default=None, ge=1),
    ) -> list[dict[str, Any]]:
        try:
            runs = service.list_evaluation_runs(
                suite_id=suite_id,
                status=status,
                limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid evaluation run query") from exc
        return [run.model_dump(mode="json") for run in runs]

    @app.post("/v1/evaluations/runs")
    async def run_evaluation_suite(request: EvaluationRunRequest) -> StreamingResponse:
        try:
            service.get_evaluation_suite(request.suite_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail="evaluation suite not found") from exc
        evaluation_runner = EvaluationRunner(service)

        async def stream_events() -> AsyncIterator[str]:
            evaluation_run_id: str | None = None
            try:
                async for event in evaluation_runner.run_suite(
                    request.suite_id,
                    artifact_root=request.artifact_root,
                ):
                    evaluation_run_id = event.evaluation_run_id
                    yield _sse_event(
                        event.event_type,
                        event.model_dump(mode="json"),
                        cursor=event.cursor,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                safe_error_type = _safe_evaluation_error_type(exc)
                payload = error_payload(
                    code=f"evaluation_{safe_error_type}",
                    message="evaluation runner stopped before completion",
                    recovery=RecoveryAction.MANUAL_RECONCILE,
                )
                payload.update(
                    {
                        "evaluation_run_id": evaluation_run_id,
                        "error_type": safe_error_type,
                        "message": "evaluation runner stopped before completion",
                    }
                )
                cursor: int | None = None
                if evaluation_run_id is not None:
                    try:
                        persisted_error = service.append_evaluation_event(
                            EvaluationRunEvent(
                                evaluation_run_id=evaluation_run_id,
                                event_type="evaluation.error",
                                payload=payload,
                            )
                        )
                    except (ConflictError, NotFoundError):
                        pass
                    else:
                        cursor = persisted_error.cursor
                        payload = persisted_error.model_dump(mode="json")
                yield _sse_event(
                    "evaluation.error",
                    payload,
                    cursor=cursor,
                )

        return StreamingResponse(stream_events(), media_type="text/event-stream")

    @app.get("/v1/evaluations/runs/{evaluation_run_id}")
    async def get_evaluation_run(evaluation_run_id: str) -> dict[str, Any]:
        try:
            run = service.get_evaluation_run(evaluation_run_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail="evaluation run not found") from exc
        return run.model_dump(mode="json")

    @app.get("/v1/evaluations/runs/{evaluation_run_id}/events")
    async def list_evaluation_events(
        evaluation_run_id: str,
        after_cursor: int | None = Query(default=None, ge=0, le=MAX_EVENT_CURSOR),
        limit: int = Query(default=1000, ge=1, le=1000),
    ) -> list[dict[str, Any]]:
        try:
            events = service.list_evaluation_events(
                evaluation_run_id,
                after_cursor=after_cursor,
                limit=limit,
            )
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail="evaluation run not found") from exc
        return [event.model_dump(mode="json") for event in events]

    @app.get("/v1/evaluations/runs/{evaluation_run_id}/events/stream")
    async def stream_evaluation_events(
        evaluation_run_id: str,
        raw_request: Request,
        after_cursor: int | None = Query(default=None, ge=0, le=MAX_EVENT_CURSOR),
        limit: int = Query(default=1000, ge=1, le=1000),
    ) -> StreamingResponse:
        header_cursor = _parse_last_event_id(raw_request.headers.get("Last-Event-ID"))
        replay_cursor = header_cursor if header_cursor is not None else after_cursor
        try:
            events = service.list_evaluation_events(
                evaluation_run_id,
                after_cursor=replay_cursor,
                limit=limit,
            )
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail="evaluation run not found") from exc

        async def replay_events() -> AsyncIterator[str]:
            for event in events:
                yield _sse_event(
                    event.event_type,
                    event.model_dump(mode="json"),
                    cursor=event.cursor,
                )

        return StreamingResponse(replay_events(), media_type="text/event-stream")

    @app.get("/v1/evaluations/runs/{evaluation_run_id}/results")
    async def list_evaluation_results(evaluation_run_id: str) -> list[dict[str, Any]]:
        try:
            results = service.list_evaluation_results(evaluation_run_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail="evaluation run not found") from exc
        return [_safe_evaluation_result_payload(result) for result in results]

    @app.post("/v1/memories", status_code=201)
    async def create_memory(request: CreateMemoryRequest) -> dict[str, object]:
        try:
            session = service.get_session(request.session_id)
            memory = service.save_memory(
                snapshot=session.role_snapshot,
                session_id=session.id,
                kind=request.kind,
                content=request.content,
                project_scope=request.project_scope,
                role_scope=request.role_scope,
                source_session_id=session.id,
                source_task=request.source_task,
                confidence=request.confidence,
                confirmed=request.confirmed,
            )
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (PermissionError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return memory.model_dump(mode="json")

    @app.get("/v1/memories/search")
    async def search_memories(
        session_id: str,
        query: str = Query(min_length=1),
        project_scope: str | None = None,
        include_candidates: bool = False,
    ) -> list[dict[str, object]]:
        try:
            session = service.get_session(session_id)
            memories = service.query_memories(
                query,
                snapshot=session.role_snapshot,
                session_id=session.id,
                project_scope=project_scope,
                include_candidates=include_candidates,
            )
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (PermissionError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return [memory.model_dump(mode="json") for memory in memories]

    @app.post("/v1/memories/{memory_id}/confirm")
    async def confirm_memory(
        memory_id: str,
        session_id: str,
        project_scope: str | None = None,
    ) -> dict[str, object]:
        try:
            session = service.get_session(session_id)
            memory = service.confirm_memory(
                memory_id,
                snapshot=session.role_snapshot,
                session_id=session.id,
                project_scope=project_scope,
            )
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (PermissionError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return memory.model_dump(mode="json")

    @app.delete("/v1/memories/{memory_id}")
    async def deactivate_memory(
        memory_id: str,
        session_id: str,
        project_scope: str | None = None,
    ) -> dict[str, object]:
        try:
            session = service.get_session(session_id)
            memory = service.deactivate_memory(
                memory_id,
                snapshot=session.role_snapshot,
                session_id=session.id,
                project_scope=project_scope,
            )
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (PermissionError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return memory.model_dump(mode="json")

    # Added last so this pure ASGI guard wraps the BaseHTTP command middleware:
    # oversized chunked bodies fail before request.body() can buffer them.
    app.add_middleware(
        _ArtifactRequestBodyLimitMiddleware,
        max_body_bytes=MAX_ARTIFACT_REQUEST_BODY_BYTES,
    )
    return app


app = create_app()
