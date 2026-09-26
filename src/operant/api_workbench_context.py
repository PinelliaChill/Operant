"""Bounded context and explicit command routes for the conversation workbench."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from operant.application.service import ApplicationService
from operant.domain.commands import (
    ContextBaselineOperation,
    SlashCommandDefinition,
    SlashCommandKind,
)
from operant.domain.context import ContextReferenceType, ReferenceIncludeMode, ReferenceRequest
from operant.domain.models import Session, utc_now
from operant.domain.threads import ArtifactSensitivity, ArtifactSourceRef, ArtifactSourceType
from operant.persistence.sqlite import ConflictError, NotFoundError
from operant.protocol import redact_public_text
from operant.tools.execution import is_protected_workspace_name


class WorkbenchReferenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["file", "thread"]
    target: str = Field(min_length=1, max_length=2048)
    max_tokens: int = Field(default=2000, ge=128, le=8000)


class WorkbenchReferenceView(BaseModel):
    reference: ReferenceRequest
    source: str
    summary: str
    content_hash: str
    size_bytes: int
    truncated: bool


class WorkbenchCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=2100)
    registry_version: str | None = None
    reviewer_role_id: str | None = None


class WorkbenchCommandResult(BaseModel):
    command: str
    status: str
    message: str
    resource_id: str | None = None


class WorkbenchCommandRegistry(BaseModel):
    registry_version: str
    commands: list[SlashCommandDefinition]


class WorkbenchContextView(BaseModel):
    session_id: str
    thread_id: str
    revision_id: str | None = None
    token_estimate: int | None = None
    context_window: int | None = None
    watermark: str = "unknown"
    pre_compaction_token_estimate: int | None = None
    compaction_id: str | None = None
    baseline_operation: str | None = None
    baseline_id: str | None = None
    blocks: list[dict[str, Any]] = Field(default_factory=list)
    references: list[dict[str, Any]] = Field(default_factory=list)


def thread_session(service: ApplicationService, thread_id: str) -> Session:
    thread = service.get_thread(thread_id)
    session_id = next((r.source_id for r in thread.legacy_refs if r.source_type == "session"), None)
    if session_id is None:
        raise ValueError("thread has no ordinary session")
    return service.get_session(session_id)


def read_workspace_text(root: Path, relative: str, limit: int = 128_000) -> tuple[str, bool]:
    """Open each path segment without following links; read at most limit+1 bytes."""
    path = Path(relative)
    if (
        path.is_absolute()
        or not path.parts
        or any(p in {"..", "."} or is_protected_workspace_name(p) for p in path.parts)
    ):
        raise PermissionError("reference must be an unprotected relative workspace file")
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for index, part in enumerate(path.parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if index < len(path.parts) - 1:
                flags |= os.O_DIRECTORY
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise PermissionError("reference must be a regular file")
        chunks = bytearray()
        while len(chunks) <= limit:
            chunk = os.read(descriptor, min(16_384, limit + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        truncated = len(chunks) > limit
        text = bytes(chunks[:limit]).decode("utf-8", errors="strict")
        if "\x00" in text:
            raise ValueError("binary files cannot be attached as text references")
        return text, truncated
    finally:
        os.close(descriptor)


def create_reference(
    service: ApplicationService, thread_id: str, body: WorkbenchReferenceRequest
) -> WorkbenchReferenceView:
    session = thread_session(service, thread_id)
    thread = service.get_thread(thread_id)
    if not thread.workspace_ref:
        raise PermissionError("thread has no bound workspace")
    root = Path(thread.workspace_ref).resolve(strict=True)
    if "read_file" not in session.role_snapshot.tool_policy.allowed_tools:
        raise PermissionError("the session role does not allow reading references")
    if body.kind == "file":
        text, truncated = read_workspace_text(root, body.target)
        source = f"file:{body.target}"
    else:
        target = service.get_thread(body.target)
        if not target.workspace_ref or Path(target.workspace_ref).resolve(strict=True) != root:
            raise PermissionError("referenced conversation belongs to another workspace")
        # Only canonical public user/Agent messages; never ContextRevision or prompts.
        with service.store._connect() as connection:
            rows = connection.execute(
                "SELECT body FROM items WHERE thread_id = ? ORDER BY sequence DESC LIMIT 21",
                (target.id,),
            ).fetchall()
        parts = []
        for row in reversed(rows[:20]):
            payload = json.loads(row["body"])["payload"]
            if payload.get("type") in {"user_message", "agent_message"}:
                parts.append(f"{payload['type']}: {payload['text']}")
        text, truncated = "\n".join(parts), len(rows) > 20
        source = f"thread:{target.id}"
    max_chars = body.max_tokens * 3
    truncated = truncated or len(text) > max_chars
    text = redact_public_text(text[:max_chars], max_chars=max_chars)
    # Include the recipient scope in the immutable snapshot, avoiding cross-scope
    # deduplication of content-hash-unique Artifact metadata.
    content = json.dumps(
        {
            "recipient_thread_id": thread_id,
            "source": source,
            "truncated": truncated,
            "content": text,
        },
        ensure_ascii=False,
        sort_keys=True,
    ).encode()
    artifact, _ = service.create_artifact(
        content=content,
        media_type="application/json",
        source_refs=(
            ArtifactSourceRef(source_type=ArtifactSourceType.THREAD, source_id=thread_id),
        ),
        retention_policy_ref="context-reference",
    )
    if artifact.sensitivity is not ArtifactSensitivity.NORMAL:
        raise PermissionError("restricted reference snapshot")
    summary = f"{source} · {len(content)} bytes · 有界快照，正文按需读取"
    return WorkbenchReferenceView(
        reference=ReferenceRequest(
            ref_type=ContextReferenceType.ARTIFACT,
            target_id=artifact.id,
            include_mode=ReferenceIncludeMode.METADATA,
            max_tokens=body.max_tokens,
            user_text=summary[:500],
        ),
        source=source,
        summary=summary,
        content_hash=artifact.content_hash,
        size_bytes=artifact.size_bytes,
        truncated=truncated,
    )


def read_context_reference(
    service: ApplicationService,
    thread_id: str,
    references: tuple[ReferenceRequest, ...],
    artifact_id: str,
) -> dict[str, Any]:
    if not any(
        r.ref_type is ContextReferenceType.ARTIFACT and r.target_id == artifact_id
        for r in references
    ):
        raise PermissionError("reference was not explicitly attached to this run")
    artifact = service.get_artifact(artifact_id)
    if artifact.sensitivity is not ArtifactSensitivity.NORMAL or not any(
        r.source_type is ArtifactSourceType.THREAD and r.source_id == thread_id
        for r in artifact.source_refs
    ):
        raise PermissionError("reference is not readable by this conversation")
    raw = service._read_artifact_for_context(artifact_id)
    if len(raw) > 128_000:
        raise ValueError("reference exceeds the bounded read limit")
    result: dict[str, Any] = json.loads(raw)
    if result.get("recipient_thread_id") != thread_id:
        raise PermissionError("reference recipient mismatch")
    return {"artifact_id": artifact_id, "content_hash": artifact.content_hash, **result}


def context_view(service: ApplicationService, thread_id: str) -> WorkbenchContextView:
    session = thread_session(service, thread_id)
    with service.store._connect() as connection:
        row = connection.execute(
            "SELECT id FROM context_revisions WHERE session_id = ? ORDER BY sequence DESC LIMIT 1",
            (session.id,),
        ).fetchone()
    baseline = service.get_active_context_baseline(session.id, thread_id)
    result = WorkbenchContextView(
        session_id=session.id,
        thread_id=thread_id,
        context_window=session.role_snapshot.context_window,
        baseline_operation=None if baseline is None else baseline.operation.value,
        baseline_id=None if baseline is None else baseline.id,
        compaction_id=None if baseline is None else baseline.compaction_id,
    )
    if row is None:
        return result
    revision = service.get_context_revision(session.id, row["id"])
    result.revision_id = revision.id
    result.token_estimate = revision.token_estimate
    result.watermark = revision.watermark.state.value
    result.pre_compaction_token_estimate = revision.watermark.pre_compaction_token_estimate
    result.compaction_id = result.compaction_id or revision.compaction_id
    result.blocks = [
        {
            "type": b.block_type.value,
            "token_estimate": b.token_estimate,
            "source_refs": [r.model_dump(mode="json") for r in b.source_refs],
        }
        for b in revision.blocks
    ]
    result.references = [r.model_dump(mode="json") for r in revision.reference_bindings]
    return result


def _reject_active_run(service: ApplicationService, session_id: str) -> None:
    try:
        lease = service.store.get_session_run_lease(session_id)
    except NotFoundError:
        return
    if lease.released_at is None:
        reason = "active run" if lease.expires_at > utc_now() else "unreconciled interrupted run"
        raise ConflictError(f"context cannot change during {reason}")


def install_workbench_context_routes(app: FastAPI, service: ApplicationService) -> None:
    @app.get(
        "/v1/workbench/commands",
        operation_id="listWorkbenchCommands",
        response_model=WorkbenchCommandRegistry,
    )
    def commands() -> WorkbenchCommandRegistry:
        return WorkbenchCommandRegistry(
            registry_version=service.slash_commands.version,
            commands=list(service.list_slash_commands()),
        )

    @app.get(
        "/v1/workbench/threads/{thread_id}/context",
        operation_id="getWorkbenchContext",
        response_model=WorkbenchContextView,
    )
    def context(thread_id: str) -> WorkbenchContextView:
        try:
            return context_view(service, thread_id)
        except (NotFoundError, ValueError, PermissionError) as exc:
            raise _http_error(exc) from exc

    @app.post(
        "/v1/workbench/threads/{thread_id}/references",
        operation_id="createWorkbenchReference",
        response_model=WorkbenchReferenceView,
    )
    def reference(thread_id: str, body: WorkbenchReferenceRequest) -> WorkbenchReferenceView:
        try:
            return create_reference(service, thread_id, body)
        except (NotFoundError, ValueError, PermissionError, OSError, ConflictError) as exc:
            raise _http_error(exc) from exc

    @app.post(
        "/v1/workbench/threads/{thread_id}/commands",
        operation_id="executeWorkbenchCommand",
        response_model=WorkbenchCommandResult,
    )
    async def execute(
        thread_id: str, body: WorkbenchCommandRequest, request: Request
    ) -> WorkbenchCommandResult:
        try:
            session = thread_session(service, thread_id)
            thread = service.get_thread(thread_id)
            resolved = service.resolve_slash_command(
                body.text, registry_version=body.registry_version
            )
            if resolved.command_kind is not SlashCommandKind.REVIEW and resolved.arguments:
                raise ValueError("this command does not accept arguments")
            execution_id = request.state.command_execution_id
            if resolved.command_kind in {
                SlashCommandKind.CONTEXT_CLEAR,
                SlashCommandKind.CONTEXT_COMPACT,
            }:
                _reject_active_run(service, session.id)
                view = context_view(service, thread_id)
                agent_id = None
                if view.revision_id:
                    agent_id = service.get_context_revision(session.id, view.revision_id).agent_id
                operation = (
                    ContextBaselineOperation.CLEAR
                    if resolved.command_kind is SlashCommandKind.CONTEXT_CLEAR
                    else ContextBaselineOperation.COMPACT
                )
                baseline = service.append_context_baseline(
                    session_id=session.id,
                    thread_id=thread_id,
                    operation=operation,
                    agent_id=agent_id,
                )
                service.append_phase1d_audit(
                    command_execution_id=execution_id,
                    command_kind=resolved.command_kind,
                    event_type=f"context.{operation.value}",
                    resource_type="context_baseline",
                    resource_id=baseline.id,
                    detail={"thread_id": thread_id, "session_id": session.id},
                )
                return WorkbenchCommandResult(
                    command=resolved.canonical_name,
                    status="completed",
                    message="上下文基线已更新，下轮生效；历史保留",
                    resource_id=baseline.id,
                )
            if resolved.command_kind is SlashCommandKind.WORKSPACE_INIT:
                if not thread.workspace_ref:
                    raise ValueError("thread has no bound workspace")
                workspace, _ = service.initialize_workspace(thread.workspace_ref)
                service.append_phase1d_audit(
                    command_execution_id=execution_id,
                    command_kind=resolved.command_kind,
                    event_type="workspace.registered",
                    resource_type="workspace",
                    resource_id=workspace.id,
                    detail={"workspace_id": workspace.id},
                )
                return WorkbenchCommandResult(
                    command=resolved.canonical_name,
                    status="completed",
                    message="当前工作区已注册",
                    resource_id=workspace.id,
                )
            if not thread.workspace_ref:
                raise ValueError("thread has no bound workspace")
            last = None
            async for event in service.run_review(
                command_execution_id=execution_id,
                reviewer_role_id=body.reviewer_role_id or session.role_snapshot.role_id,
                workspace=thread.workspace_ref,
                scope=resolved.arguments or "working tree",
                thread_id=thread_id,
            ):
                last = event
            if last is None:
                raise ConflictError("review produced no terminal result")
            status = "completed" if last.event_type == "review.completed" else "failed"
            return WorkbenchCommandResult(
                command=resolved.canonical_name,
                status=status,
                message=last.event_type,
                resource_id=last.resource_id,
            )
        except (NotFoundError, ValueError, PermissionError, LookupError, ConflictError) as exc:
            raise _http_error(exc) from exc


def _http_error(exc: Exception) -> HTTPException:
    status = 404 if isinstance(exc, (NotFoundError, LookupError)) else 400
    if isinstance(exc, PermissionError):
        status = 403
    if isinstance(exc, ConflictError):
        status = 409
    return HTTPException(status_code=status, detail=redact_public_text(str(exc), max_chars=500))
