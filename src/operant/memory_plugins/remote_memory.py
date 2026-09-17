"""Core side of the MP-5.4 Remote Memory boundary.

``RemoteMemoryService`` is intentionally a small adapter around the existing
MemoryManager, ledger and governance services.  It does not create a second
publication head or a second memory permission system.  A package is created
from current, Core-authorized versions, is bound to one Target/purpose/TTL,
and is persisted only in the Core database.  A Target upload is converted to
pending ledger proposals; it never publishes a version.

The Remote Control executor is synchronous today, so :meth:`execute` is
synchronous as well.  :meth:`execute_async` is provided for API callers that
already use an async command handler and returns affected IDs.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from math import ceil
from typing import Any, Protocol

from operant.contracts.b2_1 import (
    MemoryConditions,
    MemoryProposal,
    MemoryVersion,
    MemoryVersionRef,
    RpcContext,
    SourceRef,
)
from operant.contracts.b2_6_remote import (
    RemoteMemoryCandidate,
    RemoteMemoryCommand,
    RemoteMemoryPack,
    RemoteMemoryPackEntry,
    RemoteMemoryPackSummary,
    RemoteMemoryRecord,
    RemoteMemoryState,
    RemoteMemoryUpload,
    new_remote_memory_id,
    utc_now,
)
from operant.domain.memory import MemoryKind
from operant.domain.remote_execution import RemoteExecutionJob
from operant.memory_plugins.governance import GovernanceService
from operant.memory_plugins.ledger import (
    LedgerError,
    LedgerNotFoundError,
)
from operant.memory_plugins.retrieval import memory_sensitivity_visible
from operant.plugins.protocol import PluginError

MAX_REMOTE_MEMORY_TTL_SECONDS = 300
MAX_REMOTE_MEMORY_ENTRIES = 50
MAX_REMOTE_MEMORY_BYTES = 16_777_216
MAX_REMOTE_MEMORY_TOKENS = 1_000_000
REMOTE_MEMORY_TARGET_OPERATION = "memory.consume"


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _source_key(source: SourceRef) -> tuple[Any, ...]:
    return (
        source.source_type,
        source.source_id,
        source.revision,
        source.content_digest,
        source.permission_epoch,
    )


def _unique_sources(sources: Sequence[SourceRef]) -> tuple[SourceRef, ...]:
    values: dict[tuple[Any, ...], SourceRef] = {}
    for source in sources:
        values.setdefault(_source_key(source), source)
    return tuple(values.values())


class RemoteMemoryError(ValueError):
    """A typed, fail-closed error from the local Remote Memory adapter."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RemoteMemoryRuntime(Protocol):
    """Target runtime boundary used before a package is emitted.

    A production integration should provide ``validate_memory_target`` from
    the Remote Execution controller/lease owner.  The method must validate
    the registered Target identity and its live lease; a bare ``target_id``
    is never sufficient to create a package.
    """

    def validate_memory_target(
        self,
        target_id: str,
        *,
        purpose: str,
        expires_at: datetime,
        now: datetime,
    ) -> None: ...


class RemoteMemoryService:
    """Build and review bounded memory packages using Core authority."""

    def __init__(
        self,
        manager: Any,
        remote_runtime: RemoteMemoryRuntime | None = None,
        *,
        clock: Callable[[], datetime] = utc_now,
        max_entries: int = MAX_REMOTE_MEMORY_ENTRIES,
        max_pack_bytes: int = MAX_REMOTE_MEMORY_BYTES,
        max_pack_tokens: int = MAX_REMOTE_MEMORY_TOKENS,
    ) -> None:
        if not 1 <= max_entries <= MAX_REMOTE_MEMORY_ENTRIES:
            raise ValueError("remote memory entry limit is out of bounds")
        if not 1_024 <= max_pack_bytes <= MAX_REMOTE_MEMORY_BYTES:
            raise ValueError("remote memory byte limit is out of bounds")
        if not 1 <= max_pack_tokens <= MAX_REMOTE_MEMORY_TOKENS:
            raise ValueError("remote memory token limit is out of bounds")
        self.manager = manager
        self.remote_runtime = remote_runtime
        self.clock = clock
        self.max_entries = max_entries
        self.max_pack_bytes = max_pack_bytes
        self.max_pack_tokens = max_pack_tokens

    def state(self, project_id: str) -> RemoteMemoryState:
        """Return the current authorized project memory and package summary."""

        try:
            project, installation, binding, context = self._runtime(
                project_id, require_enabled=False
            )
        except Exception as exc:
            return RemoteMemoryState(
                action="state",
                project_id=project_id,
                status="rejected",
                message=str(exc) or "memory project is unavailable",
            )

        if (
            installation.state != "enabled"
            or not binding.enabled
            or not binding.global_enabled
            or not bool(getattr(self.manager, "_state", {}).get("global_enabled", True))
            or not bool(project.get("memory_enabled", True))
        ):
            packs = self._pack_summaries(project_id)
            return RemoteMemoryState(
                action="state",
                project_id=project_id,
                dataset_id=installation.dataset_id,
                status="rejected",
                message="memory binding is disabled",
                packs=packs,
                affected_ids=tuple(item.package_id for item in packs),
                permission_epoch=binding.permission_epoch,
                binding_epoch=binding.binding_epoch,
            )

        records = self._records(
            project,
            installation.dataset_id,
            context,
            session_id=None,
            agent_instance_id=None,
            limit=self.max_entries,
        )
        packs = self._pack_summaries(project_id)
        return RemoteMemoryState(
            action="state",
            project_id=project_id,
            dataset_id=installation.dataset_id,
            status="ready",
            message="状态来自 Core 当前已发布记忆",
            records=tuple(records),
            packs=packs,
            affected_ids=tuple(item.package_id for item in packs),
            permission_epoch=binding.permission_epoch,
            binding_epoch=binding.binding_epoch,
            result_ref=f"remote-memory-state:{project_id}",
        )

    def execute(self, command: RemoteMemoryCommand) -> RemoteMemoryState:
        """Execute one bounded package operation synchronously.

        ``remote_pack_create`` and ``remote_pack_revoke`` are local Core
        operations.  ``remote_upload_review`` creates only pending ledger
        proposals.  Existing B2-3/B2-5 review commands remain responsible for
        final publication.
        """

        if command.action == "remote_pack_create":
            return self._create_pack(command)
        if command.action == "remote_pack_revoke":
            return self._revoke_pack(command)
        if command.action == "remote_upload_review":
            assert command.upload is not None
            return self._review_upload(command, command.upload)
        raise RemoteMemoryError("unsupported_action", "unsupported remote memory action")

    async def execute_async(self, command: RemoteMemoryCommand) -> list[str]:
        """Async compatibility wrapper returning the affected object IDs."""

        return list(self.execute(command).affected_ids)

    def execute_remote_payload(self, payload: Any, _action: Any | None = None) -> str:
        """Adapt a RemoteControl ``memory.query``/``memory.command`` payload.

        RemoteControl receipts intentionally retain only an opaque result
        reference.  The caller can fetch the current projection through
        :meth:`state`; no memory body is placed in the Remote receipt or Relay.
        """

        tool = getattr(payload, "tool", None)
        operation = getattr(payload, "operation", None)
        target_id = getattr(payload, "target_id", None)
        arguments = getattr(payload, "arguments", {})
        if tool != "memory" or not isinstance(arguments, Mapping):
            raise RemoteMemoryError(
                "invalid_remote_payload", "remote payload is not a memory operation"
            )
        if not isinstance(target_id, str) or not target_id:
            raise RemoteMemoryError("invalid_remote_payload", "memory target project is required")

        if operation == "query":
            result = self.state(target_id)
        elif operation == "command":
            command_data = dict(arguments)
            command_data.setdefault("project_id", target_id)
            command_data.setdefault("target_id", target_id)
            result = self.execute(RemoteMemoryCommand.model_validate(command_data))
        else:
            raise RemoteMemoryError(
                "unsupported_remote_operation", "remote memory operation is not registered"
            )
        return result.result_ref or f"remote-memory:{target_id}:{result.action}"

    # Alias used by ``create_app(..., phase56_remote_executor=...)`` callers.
    remote_executor = execute_remote_payload

    def target_payload(self, pack: RemoteMemoryPack) -> dict[str, Any]:
        """Encode a verified package for a RemoteExecution Job argument."""

        self._validate_pack_digest(pack)
        return {"remote_memory_pack": pack.model_dump(mode="json")}

    @staticmethod
    def parse_target_payload(arguments: Mapping[str, Any]) -> RemoteMemoryPack:
        raw = arguments.get("remote_memory_pack")
        if not isinstance(raw, Mapping):
            raise RemoteMemoryError("memory_pack_missing", "remote job has no memory package")
        return RemoteMemoryPack.model_validate(raw)

    def _runtime(
        self, project_id: str, *, require_enabled: bool
    ) -> tuple[dict[str, Any], Any, Any, RpcContext]:
        if not isinstance(project_id, str) or not project_id.strip():
            raise RemoteMemoryError("invalid_project", "project_id is required")
        try:
            project = self.manager._project(project_id)
        except Exception as exc:
            raise RemoteMemoryError("project_not_found", "memory project was not found") from exc
        try:
            installation = self.manager._installation(project, enabled=require_enabled)
        except (PluginError, PermissionError, ValueError) as exc:
            raise RemoteMemoryError("memory_unavailable", str(exc)) from exc
        binding_id = installation.binding_id
        if not binding_id:
            raise RemoteMemoryError("binding_unavailable", "memory project has no active binding")
        try:
            binding = self.manager.registry.get_binding(binding_id)
        except Exception as exc:
            raise RemoteMemoryError("binding_unavailable", "memory binding was not found") from exc
        request_id = new_remote_memory_id("remote_memory_request")
        context = RpcContext(
            sdk_version="operant-memory-sdk.v1",
            request_id=request_id,
            installation_id=installation.installation_id,
            dataset_id=installation.dataset_id,
            scope=self.manager._scope(project),
            deadline=self._now() + timedelta(seconds=30),
            cancel_token=new_remote_memory_id("cancel"),
            idempotency_key=new_remote_memory_id("request"),
            request_digest=_digest(
                {
                    "project_id": project_id,
                    "dataset_id": installation.dataset_id,
                    "request_id": request_id,
                }
            ),
            binding_epoch=binding.binding_epoch,
            permission_epoch=binding.permission_epoch,
            lease_fencing=0,
        )
        return project, installation, binding, context

    def _validate_target_runtime(
        self,
        target_id: str,
        *,
        purpose: str,
        expires_at: datetime,
        now: datetime,
    ) -> None:
        runtime = self.remote_runtime
        validator = getattr(runtime, "validate_memory_target", None)
        if not callable(validator):
            raise RemoteMemoryError(
                "target_runtime_unavailable",
                "remote Target runtime has no registration and lease validator",
            )
        try:
            validator(
                target_id,
                purpose=purpose,
                expires_at=expires_at,
                now=now,
            )
        except RemoteMemoryError:
            raise
        except Exception as exc:
            # Do not expose endpoint, credential, lease token or connector
            # details in the Core/API error surface.
            raise RemoteMemoryError(
                "target_unavailable",
                "registered Target or live lease validation failed",
            ) from exc

    def _session_visibility(
        self,
        project: Mapping[str, Any],
        command: RemoteMemoryCommand | None,
        *,
        workspace_ref: str,
    ) -> tuple[Any | None, str | None]:
        if command is None or command.session_id is None:
            return None, command.agent_instance_id if command else None
        try:
            session = self.manager.service.get_session(command.session_id)
        except Exception as exc:
            raise RemoteMemoryError("session_not_found", "memory session was not found") from exc
        # Session stores do not carry a workspace field.  If the historical
        # Thread binding exists, it is the authoritative workspace relation.
        from operant.domain.threads import LegacySourceType, ThreadLegacyRef

        try:
            thread = self.manager.store.get_thread_by_legacy_ref(
                ThreadLegacyRef(
                    source_type=LegacySourceType.SESSION,
                    source_id=session.id,
                )
            )
        except Exception as exc:
            # A Session without an auditable canonical Thread cannot establish
            # which workspace it belongs to.  Do not widen a role's project
            # scope merely because the Session itself is valid.
            raise RemoteMemoryError(
                "session_binding_unavailable",
                "session has no verifiable project Thread binding",
            ) from exc
        if thread is None:
            raise RemoteMemoryError(
                "session_binding_unavailable",
                "session has no verifiable project Thread binding",
            )
        if thread.workspace_ref != workspace_ref:
            raise RemoteMemoryError(
                "session_project_mismatch", "session is outside the memory project"
            )
        try:
            self.manager.service._authorize_memory(
                session.role_snapshot,
                MemoryKind.PROJECT,
                operation="read",
                project_scope=workspace_ref,
            )
        except (PermissionError, ValueError) as exc:
            raise RemoteMemoryError("role_scope_denied", str(exc)) from exc
        agent_id = command.agent_instance_id
        if agent_id is not None:
            try:
                agent = self.manager.service.store.get_agent(agent_id)
            except Exception as exc:
                raise RemoteMemoryError("agent_not_found", "memory Agent was not found") from exc
            if agent.session_id != session.id:
                raise RemoteMemoryError(
                    "agent_session_mismatch", "Agent is outside the memory session"
                )
        return session.role_snapshot, agent_id

    def _records(
        self,
        project: Mapping[str, Any],
        dataset_id: str,
        context: RpcContext,
        *,
        session_id: str | None,
        agent_instance_id: str | None,
        query: str | None = None,
        record_ids: Sequence[str] = (),
        limit: int = MAX_REMOTE_MEMORY_ENTRIES,
    ) -> list[RemoteMemoryRecord]:
        workspace = self.manager.store.get_workspace_initialization_by_id(
            project["workspace_id"]
        ).workspace_ref
        command = RemoteMemoryCommand(
            action="remote_pack_create",
            project_id=str(project["project_id"]),
            target_id="state",
            query=query,
            record_ids=tuple(record_ids),
            session_id=session_id,
            agent_instance_id=agent_instance_id,
        )
        snapshot, resolved_agent_id = self._session_visibility(
            project, command if session_id else None, workspace_ref=workspace
        )
        if record_ids:
            candidates: list[MemoryVersion] = []
            seen: set[str] = set()
            for record_id in record_ids:
                if record_id in seen:
                    continue
                seen.add(record_id)
                try:
                    value = self.manager.ledger.get_version(dataset_id, record_id)
                except (LedgerError, KeyError):
                    continue
                if query and query.casefold() not in value.content.casefold():
                    continue
                candidates.append(value)
                if len(candidates) >= limit:
                    break
        else:
            candidates = self.manager.ledger.query(
                dataset_id,
                query,
                scope=context.scope,
                include_candidates=False,
                include_inactive=False,
                include_legacy=False,
                limit=limit,
            )
        governance = GovernanceService(self.manager)
        records: list[RemoteMemoryRecord] = []
        for version in candidates:
            if not self._version_visible(
                project,
                version,
                context=context,
                governance=governance,
                snapshot=snapshot,
                agent_instance_id=resolved_agent_id,
            ):
                continue
            head = self.manager.ledger.get_head(dataset_id, version.ref.record_id)
            sensitivity = version.sensitivity
            if sensitivity not in {"public", "internal"}:
                continue
            records.append(
                RemoteMemoryRecord(
                    ref=version.ref,
                    content=version.content,
                    content_type=version.content_type,
                    sensitivity=sensitivity,
                    source_refs=version.sources,
                    head_revision=head.revision,
                    currently_usable=True,
                )
            )
        return records

    def _version_visible(
        self,
        project: Mapping[str, Any],
        version: MemoryVersion,
        *,
        context: RpcContext,
        governance: GovernanceService,
        snapshot: Any | None,
        agent_instance_id: str | None,
    ) -> bool:
        if version.ref.dataset_id != context.dataset_id or version.scope != context.scope:
            return False
        if version.sensitivity not in {"public", "internal"} or not memory_sensitivity_visible(
            version, "internal"
        ):
            return False
        if version.role_ids and (
            snapshot is None
            or not (snapshot.role_id in version.role_ids or snapshot.role_name in version.role_ids)
        ):
            return False
        if version.agent_ids and agent_instance_id not in version.agent_ids:
            return False
        try:
            if not governance.version_dependencies_valid(
                version.ref,
                project_id=str(project["project_id"]),
                context=context,
            ):
                return False
            return all(self.manager.authorize_source(context, source) for source in version.sources)
        except Exception:
            return False

    def _create_pack(self, command: RemoteMemoryCommand) -> RemoteMemoryState:
        project, installation, binding, context = self._runtime(
            command.project_id, require_enabled=True
        )
        # Package creation is a remote side effect.  The Core may expose
        # project state without a connector, but it must not mint a package
        # for an arbitrary Target id.  The configured runtime owns target
        # registration and lease/fencing checks.
        if self.remote_runtime is None:
            raise RemoteMemoryError(
                "target_runtime_unavailable",
                "remote Target runtime is not configured; package creation is blocked",
            )
        issued_at = self._now()
        expires_at = issued_at + timedelta(
            seconds=min(command.ttl_seconds, MAX_REMOTE_MEMORY_TTL_SECONDS)
        )
        self._validate_target_runtime(
            command.target_id,
            purpose=command.purpose,
            expires_at=expires_at,
            now=issued_at,
        )
        versions = self._records(
            project,
            installation.dataset_id,
            context,
            session_id=command.session_id,
            agent_instance_id=command.agent_instance_id,
            query=command.query,
            record_ids=command.record_ids,
            limit=min(command.limit, self.max_entries),
        )
        entries: list[RemoteMemoryPackEntry] = []
        byte_count = 0
        token_count = 0
        for record in versions:
            entry = RemoteMemoryPackEntry(
                ref=record.ref,
                content=record.content,
                content_type=record.content_type,
                sensitivity=record.sensitivity,
                source_refs=record.source_refs,
            )
            entry_bytes = len(
                json.dumps(
                    entry.model_dump(mode="json"),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            entry_tokens = max(1, ceil(len(record.content.encode("utf-8")) / 4))
            if byte_count + entry_bytes > min(command.max_bytes, self.max_pack_bytes):
                if command.record_ids:
                    raise RemoteMemoryError(
                        "budget_exceeded", "selected memory exceeds package byte budget"
                    )
                continue
            if token_count + entry_tokens > min(command.max_tokens, self.max_pack_tokens):
                if command.record_ids:
                    raise RemoteMemoryError(
                        "budget_exceeded", "selected memory exceeds package token budget"
                    )
                continue
            entries.append(entry)
            byte_count += entry_bytes
            token_count += entry_tokens
            if len(entries) >= self.max_entries:
                break

        package_id = self._package_id(command)
        pack = RemoteMemoryPack(
            package_id=package_id,
            project_id=command.project_id,
            dataset_id=installation.dataset_id,
            target_id=command.target_id,
            purpose=command.purpose,
            issued_at=issued_at,
            expires_at=expires_at,
            binding_epoch=binding.binding_epoch,
            permission_epoch=binding.permission_epoch,
            revocation_epoch=binding.permission_epoch,
            entries=tuple(entries),
            token_count=token_count,
            byte_count=byte_count,
            max_tokens=min(command.max_tokens, self.max_pack_tokens),
            max_bytes=min(command.max_bytes, self.max_pack_bytes),
        )
        pack = pack.model_copy(update={"package_digest": pack.calculated_digest()})
        self._persist_pack(pack)
        records = self._records(
            project,
            installation.dataset_id,
            context,
            session_id=command.session_id,
            agent_instance_id=command.agent_instance_id,
            query=command.query,
            record_ids=command.record_ids,
            limit=self.max_entries,
        )
        return RemoteMemoryState(
            action=command.action,
            project_id=command.project_id,
            dataset_id=installation.dataset_id,
            status="completed",
            message="已生成用途和期限受限的 Remote Memory 包",
            records=tuple(records),
            packs=self._pack_summaries(command.project_id),
            pack=pack,
            affected_ids=(pack.package_id, *tuple(item.ref.record_id for item in pack.entries)),
            result_ref=f"remote-memory-pack:{pack.package_id}",
            permission_epoch=binding.permission_epoch,
            binding_epoch=binding.binding_epoch,
            expires_at=pack.expires_at,
        )

    def _revoke_pack(self, command: RemoteMemoryCommand) -> RemoteMemoryState:
        project, installation, binding, _context = self._runtime(
            command.project_id, require_enabled=False
        )
        assert command.package_id is not None and command.package_digest is not None
        pack = self._load_pack(command.package_id, command.package_digest)
        if pack.project_id != command.project_id or pack.target_id != command.target_id:
            raise RemoteMemoryError(
                "package_binding_invalid", "package is bound to another project or Target"
            )
        if pack.status == "active" and pack.expires_at > self._now():
            next_pack = pack.model_copy(update={"status": "revoked"})
            self._set_pack_status(pack.package_id, "revoked")
            status: str = "revoked"
            message = "Remote Memory 包已撤销"
        elif pack.status == "expired" or pack.expires_at <= self._now():
            self._set_pack_status(pack.package_id, "expired")
            next_pack = pack.model_copy(update={"status": "expired"})
            status = "expired"
            message = "Remote Memory 包已过期"
        else:
            next_pack = pack
            status = "revoked"
            message = "Remote Memory 包已撤销"
        return RemoteMemoryState(
            action=command.action,
            project_id=command.project_id,
            dataset_id=installation.dataset_id,
            status=status,  # type: ignore[arg-type]
            message=message,
            packs=self._pack_summaries(command.project_id),
            pack=next_pack,
            affected_ids=(pack.package_id,),
            result_ref=f"remote-memory-pack:{pack.package_id}",
            permission_epoch=binding.permission_epoch,
            binding_epoch=binding.binding_epoch,
            expires_at=pack.expires_at,
        )

    def _review_upload(
        self, command: RemoteMemoryCommand, upload: RemoteMemoryUpload
    ) -> RemoteMemoryState:
        project, installation, binding, context = self._runtime(
            command.project_id, require_enabled=True
        )
        existing = self._existing_upload(upload.upload_id)
        calculated_digest = upload.calculated_source_digest()
        if not upload.has_valid_source_digest():
            raise RemoteMemoryError(
                "upload_digest_mismatch", "remote upload source digest is invalid"
            )
        if existing is not None:
            stored_digest, state, envelope = existing
            if stored_digest != calculated_digest:
                raise RemoteMemoryError(
                    "upload_replay_conflict", "upload ID is bound to another payload"
                )
            if state in {"accepted", "pending_review"}:
                affected = tuple(str(item) for item in envelope.get("affected_ids", []))
                return RemoteMemoryState(
                    action=command.action,
                    project_id=command.project_id,
                    dataset_id=installation.dataset_id,
                    status="pending_review",
                    message="上传已接收，候选仍需本地复核",
                    upload_id=upload.upload_id,
                    affected_ids=affected,
                    packs=self._pack_summaries(command.project_id),
                    permission_epoch=binding.permission_epoch,
                    binding_epoch=binding.binding_epoch,
                )
            return RemoteMemoryState(
                action=command.action,
                project_id=command.project_id,
                dataset_id=installation.dataset_id,
                status="rejected",
                message="上传已被 Core 拒绝",
                upload_id=upload.upload_id,
                packs=self._pack_summaries(command.project_id),
                permission_epoch=binding.permission_epoch,
                binding_epoch=binding.binding_epoch,
            )

        pack = self._load_pack(upload.package_id, upload.package_digest)
        if pack.project_id != command.project_id or pack.dataset_id != installation.dataset_id:
            raise RemoteMemoryError(
                "package_binding_invalid", "upload package belongs to another dataset"
            )
        if pack.target_id != upload.target_id or pack.purpose != upload.purpose:
            raise RemoteMemoryError(
                "package_binding_invalid", "upload package Target or purpose differs"
            )
        if pack.status != "active" or pack.expires_at <= self._now():
            self._set_pack_status(
                pack.package_id, "expired" if pack.expires_at <= self._now() else pack.status
            )
            raise RemoteMemoryError("package_unavailable", "upload package is expired or revoked")
        if (
            pack.permission_epoch != binding.permission_epoch
            or pack.binding_epoch != binding.binding_epoch
        ):
            raise RemoteMemoryError("stale_epoch", "upload package permission epoch is stale")
        if upload.dataset_id != installation.dataset_id:
            raise RemoteMemoryError(
                "unknown_owner", "upload dataset differs from the active binding"
            )
        if upload.submitted_at > self._now() + timedelta(seconds=60):
            raise RemoteMemoryError(
                "upload_from_future", "upload timestamp exceeds allowed clock skew"
            )

        sources = _unique_sources(
            (
                *upload.source_refs,
                *(source for item in upload.candidates for source in item.source_refs),
            )
        )
        if not sources:
            raise RemoteMemoryError(
                "provenance_required", "remote candidates require source references"
            )
        if not all(self.manager.authorize_source(context, source) for source in sources):
            raise RemoteMemoryError(
                "source_not_authorized", "one or more upload sources are not current"
            )

        # Validate all candidate source sets and target identities before the
        # first ledger write.  The ledger transaction below repeats the
        # immutable version/CAS checks while holding its write lock, so a
        # multi-candidate upload cannot leave a partial batch behind.
        prepared: list[tuple[RemoteMemoryCandidate, MemoryVersion, int, str]] = []
        prepared_record_ids: set[str] = set()
        for _index, candidate in enumerate(upload.candidates):
            candidate_sources = _unique_sources((*upload.source_refs, *candidate.source_refs))
            if not candidate_sources or not all(
                self.manager.authorize_source(context, source) for source in candidate_sources
            ):
                raise RemoteMemoryError("source_not_authorized", "candidate source is not current")
            record_id = candidate.record_id or new_remote_memory_id("memory")
            if record_id in prepared_record_ids:
                raise RemoteMemoryError(
                    "duplicate_candidate", "remote upload contains duplicate record IDs"
                )
            prepared_record_ids.add(record_id)
            try:
                head = self.manager.ledger.get_head(installation.dataset_id, record_id)
                existing_versions = self.manager.ledger.list_versions(
                    installation.dataset_id, record_id
                )
                if head.state != "published" or head.published_version is None:
                    raise RemoteMemoryError(
                        "record_not_published", "remote upload can only extend a published record"
                    )
                expected_revision = (
                    head.revision
                    if candidate.expected_head_revision is None
                    else candidate.expected_head_revision
                )
                version_number = max(item.ref.version for item in existing_versions) + 1
            except LedgerNotFoundError:
                head = None
                expected_revision = (
                    0
                    if candidate.expected_head_revision is None
                    else candidate.expected_head_revision
                )
                version_number = 1
            if expected_revision != (0 if head is None else head.revision):
                raise RemoteMemoryError("revision_conflict", "remote upload base head is stale")
            version = MemoryVersion(
                ref=MemoryVersionRef(
                    dataset_id=installation.dataset_id,
                    record_id=record_id,
                    version=version_number,
                    content_digest=hashlib.sha256(candidate.content.encode("utf-8")).hexdigest(),
                ),
                owner=installation.owner,
                kind="project",
                content_type=candidate.content_type,
                scope=context.scope,
                role_ids=(),
                agent_ids=(),
                content=candidate.content,
                sources=candidate_sources,
                evidence="inferred",
                sensitivity="internal",
                retention_policy_id="default",
                conditions=MemoryConditions(
                    commit_ref=None,
                    tree_digest=None,
                    file_fingerprints={},
                    environment_digest=None,
                    tool_versions={},
                    verified_at=None,
                    valid_from=self._now(),
                    valid_until=None,
                ),
                recorded_at=self._now(),
            )
            operation = "modify" if head is not None else "create"
            prepared.append((candidate, version, expected_revision, operation))

        proposals = self._persist_upload_batch_atomic(
            upload,
            prepared,
            dataset_id=installation.dataset_id,
            permission_epoch=binding.permission_epoch,
        )
        return RemoteMemoryState(
            action=command.action,
            project_id=command.project_id,
            dataset_id=installation.dataset_id,
            status="pending_review",
            message="Target 上传已接收；候选已进入本地复核队列，未发布",
            upload_id=upload.upload_id,
            affected_ids=proposals,
            packs=self._pack_summaries(command.project_id),
            permission_epoch=binding.permission_epoch,
            binding_epoch=binding.binding_epoch,
        )

    def _package_id(self, command: RemoteMemoryCommand) -> str:
        if command.idempotency_key:
            return (
                "remote_pack_"
                + _digest(
                    {
                        "project_id": command.project_id,
                        "target_id": command.target_id,
                        "purpose": command.purpose,
                        "idempotency_key": command.idempotency_key,
                    }
                )[:32]
            )
        return new_remote_memory_id("remote_pack")

    def _persist_pack(self, pack: RemoteMemoryPack) -> None:
        body = pack.model_dump_json()
        with self.manager.store._connect() as connection:
            row = connection.execute(
                "SELECT package_digest, body FROM b26_memory_packs WHERE package_id=?",
                (pack.package_id,),
            ).fetchone()
            if row is not None:
                if row["package_digest"] != pack.package_digest:
                    raise RemoteMemoryError(
                        "package_replay_conflict", "package ID is bound to another request"
                    )
                return
            try:
                connection.execute(
                    "INSERT INTO b26_memory_packs("
                    "package_id,package_digest,project_id,dataset_id,target_id,purpose,"
                    "issued_at,expires_at,binding_epoch,permission_epoch,revocation_epoch,"
                    "status,entry_count,body) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        pack.package_id,
                        pack.package_digest,
                        pack.project_id,
                        pack.dataset_id,
                        pack.target_id,
                        pack.purpose,
                        pack.issued_at.isoformat(),
                        pack.expires_at.isoformat(),
                        pack.binding_epoch,
                        pack.permission_epoch,
                        pack.revocation_epoch,
                        pack.status,
                        len(pack.entries),
                        body,
                    ),
                )
            except Exception as exc:
                raise RemoteMemoryError(
                    "package_persist_conflict", "remote memory package could not be persisted"
                ) from exc

    def _load_pack(self, package_id: str, package_digest: str) -> RemoteMemoryPack:
        with self.manager.store._connect() as connection:
            row = connection.execute(
                "SELECT * FROM b26_memory_packs WHERE package_id=?",
                (package_id,),
            ).fetchone()
        if row is None or row["package_digest"] != package_digest:
            raise RemoteMemoryError("package_not_found", "remote memory package was not found")
        try:
            pack = RemoteMemoryPack.model_validate_json(row["body"])
        except Exception as exc:
            raise RemoteMemoryError("package_corrupt", "remote memory package is invalid") from exc
        self._validate_pack_digest(pack)
        status = str(row["status"])
        if status == "active" and pack.expires_at <= self._now():
            self._set_pack_status(pack.package_id, "expired")
            status = "expired"
        if status != pack.status:
            pack = pack.model_copy(update={"status": status})
        return pack

    def _validate_pack_digest(self, pack: RemoteMemoryPack) -> None:
        if not pack.has_valid_digest():
            raise RemoteMemoryError(
                "package_digest_mismatch", "remote memory package digest is invalid"
            )

    def _set_pack_status(self, package_id: str, status: str) -> None:
        if status not in {"active", "revoked", "expired"}:
            raise ValueError("invalid remote memory package status")
        with self.manager.store._connect() as connection:
            connection.execute(
                "UPDATE b26_memory_packs SET status=? WHERE package_id=?",
                (status, package_id),
            )

    def _pack_summaries(self, project_id: str) -> tuple[RemoteMemoryPackSummary, ...]:
        now = self._now()
        with self.manager.store._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM b26_memory_packs WHERE project_id=? ORDER BY issued_at, package_id",
                (project_id,),
            ).fetchall()
        result: list[RemoteMemoryPackSummary] = []
        for row in rows:
            status = str(row["status"])
            try:
                issued = datetime.fromisoformat(str(row["issued_at"]))
                expires = datetime.fromisoformat(str(row["expires_at"]))
                if issued.tzinfo is None:
                    issued = issued.replace(tzinfo=timezone.utc)
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if status == "active" and expires <= now:
                self._set_pack_status(str(row["package_id"]), "expired")
                status = "expired"
            result.append(
                RemoteMemoryPackSummary(
                    package_id=str(row["package_id"]),
                    package_digest=str(row["package_digest"]),
                    project_id=str(row["project_id"]),
                    dataset_id=str(row["dataset_id"]),
                    target_id=str(row["target_id"]),
                    purpose=str(row["purpose"]),
                    issued_at=issued,
                    expires_at=expires,
                    status=status,  # type: ignore[arg-type]
                    entry_count=int(row["entry_count"]),
                )
            )
        return tuple(result)

    def _persist_upload_batch_atomic(
        self,
        upload: RemoteMemoryUpload,
        prepared: Sequence[tuple[RemoteMemoryCandidate, MemoryVersion, int, str]],
        *,
        dataset_id: str,
        permission_epoch: int,
    ) -> tuple[str, ...]:
        """Persist every candidate, proposal and upload receipt in one ledger transaction.

        ``MemoryLedger.save_version`` and ``MemoryLedger.propose`` each own a
        transaction when called separately.  Calling them in a loop therefore
        allows a later candidate to fail after earlier candidates have already
        committed.  This method uses the ledger's transaction boundary and its
        immutable-row helpers directly, then writes the upload receipt on the
        same connection.  Any validation, CAS or database failure rolls back
        the complete batch, making a retry with the same upload ID safe.
        """

        ledger = self.manager.ledger
        proposals: list[str] = []
        with ledger._write() as connection:
            # The preflight lookup in ``_review_upload`` handles ordinary
            # retries.  Recheck under the write lock for a concurrent caller so
            # two workers cannot both create the same batch.
            existing = connection.execute(
                "SELECT 1 FROM b26_memory_uploads WHERE upload_id=?",
                (upload.upload_id,),
            ).fetchone()
            if existing is not None:
                raise RemoteMemoryError(
                    "upload_replay_conflict", "upload ID was concurrently claimed"
                )

            seen_record_ids: set[str] = set()
            for index, (candidate, version, expected_revision, operation) in enumerate(prepared):
                record_id = version.ref.record_id
                if record_id in seen_record_ids:
                    raise RemoteMemoryError(
                        "duplicate_candidate", "remote upload contains duplicate record IDs"
                    )
                seen_record_ids.add(record_id)
                ledger._check_owner(version, dataset_id)

                head = ledger._stored_head(connection, dataset_id, record_id)
                if head is None:
                    ledger._check_expected(expected_revision, 0)
                    if (
                        ledger._stored_tombstone(connection, dataset_id, record_id) is not None
                        or ledger._stored_tombstone(connection, dataset_id, None) is not None
                    ):
                        raise LedgerError("record is tombstoned and cannot receive a late version")
                    if version.ref.version != 1:
                        raise LedgerError("a new memory record must start at version 1")
                    head = ledger._head_for_create(
                        dataset_id,
                        record_id,
                        permission_epoch=permission_epoch,
                    )
                    ledger._insert_head(connection, head)
                else:
                    if head.state in {"deleted", "revoked"}:
                        raise LedgerError("record is tombstoned and cannot receive a late version")
                    ledger._check_expected(expected_revision, head.revision)
                    max_row = connection.execute(
                        """
                        SELECT MAX(version) AS max_version FROM memory_ledger_versions
                        WHERE dataset_id = ? AND record_id = ?
                        """,
                        (dataset_id, record_id),
                    ).fetchone()
                    max_version = int(max_row["max_version"] or 0)
                    if version.ref.version != max_version + 1:
                        raise LedgerError(
                            f"memory version must advance from {max_version} to {max_version + 1}"
                        )

                stored = ledger._stored_version(
                    connection,
                    dataset_id,
                    record_id,
                    version.ref.version,
                )
                if stored is None:
                    ledger._insert_version(connection, version)
                elif stored.model_dump(mode="json") != version.model_dump(mode="json"):
                    raise LedgerError("immutable memory version identity has different content")

                proposal = MemoryProposal(
                    proposal_id=new_remote_memory_id("proposal"),
                    proposal_revision=0,
                    owner=version.owner,
                    operation=operation,  # type: ignore[arg-type]
                    base_head=head,
                    proposed_version=version.ref,
                    source_refs=version.sources,
                    extractor_version="remote-target.v1",
                    reason=f"remote Target upload: {candidate.reason}",
                    state="pending",
                )
                proposal_key = f"remote-upload:{upload.upload_id}:proposal:{index}"
                proposal_digest = _digest(
                    {
                        "operation": "propose",
                        "dataset_id": dataset_id,
                        "record_id": record_id,
                        "version": version.model_dump(mode="json"),
                        "expected_head_revision": expected_revision,
                        "permission_epoch": permission_epoch,
                        "operation_value": operation,
                        "source_refs": [
                            source.model_dump(mode="json") for source in version.sources
                        ],
                        "reason": proposal.reason,
                        "extractor_version": proposal.extractor_version,
                    }
                )
                try:
                    connection.execute(
                        """
                        INSERT INTO memory_ledger_proposals(
                            proposal_id, dataset_id, record_id, proposal_revision,
                            base_head_revision, proposed_version, state, body,
                            idempotency_key, request_digest, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            proposal.proposal_id,
                            dataset_id,
                            record_id,
                            proposal.proposal_revision,
                            proposal.base_head.revision,
                            proposal.proposed_version.version,
                            proposal.state,
                            proposal.model_dump_json(),
                            proposal_key,
                            proposal_digest,
                            self._now().isoformat(),
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise LedgerError(
                        "proposal identity or idempotency key already exists"
                    ) from exc

                save_key = f"remote-upload:{upload.upload_id}:save:{index}"
                save_digest = _digest(
                    {
                        "operation": "save_version",
                        "dataset_id": dataset_id,
                        "version": version.model_dump(mode="json"),
                        "expected_head_revision": expected_revision,
                        "permission_epoch": permission_epoch,
                    }
                )
                ledger._store_idempotency(
                    connection,
                    "save_version",
                    dataset_id,
                    save_key,
                    save_digest,
                    "version",
                    version,
                )
                ledger._store_idempotency(
                    connection,
                    "propose",
                    dataset_id,
                    proposal_key,
                    proposal_digest,
                    "proposal",
                    proposal,
                )
                proposals.append(proposal.proposal_id)

            self._persist_upload_on_connection(
                connection,
                upload,
                state="accepted",
                affected_ids=proposals,
            )
        return tuple(proposals)

    def _persist_upload(
        self,
        upload: RemoteMemoryUpload,
        *,
        state: str,
        affected_ids: Sequence[str] = (),
        rejection_code: str | None = None,
    ) -> None:
        with self.manager.store._connect() as connection:
            self._persist_upload_on_connection(
                connection,
                upload,
                state=state,
                affected_ids=affected_ids,
                rejection_code=rejection_code,
            )

    def _persist_upload_on_connection(
        self,
        connection: sqlite3.Connection,
        upload: RemoteMemoryUpload,
        *,
        state: str,
        affected_ids: Sequence[str] = (),
        rejection_code: str | None = None,
    ) -> None:
        now = self._now().isoformat()
        digest = upload.calculated_source_digest()
        body = json.dumps(
            {"upload": upload.model_dump(mode="json"), "affected_ids": list(affected_ids)},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        connection.execute(
            "INSERT INTO b26_memory_uploads("
            "upload_id,package_id,package_digest,project_id,dataset_id,target_id,purpose,"
            "source_digest,state,rejection_code,body,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                upload.upload_id,
                upload.package_id,
                upload.package_digest,
                upload.project_id,
                upload.dataset_id,
                upload.target_id,
                upload.purpose,
                digest,
                state,
                rejection_code,
                body,
                upload.submitted_at.isoformat(),
                now,
            ),
        )

    def _existing_upload(self, upload_id: str) -> tuple[str, str, dict[str, Any]] | None:
        with self.manager.store._connect() as connection:
            row = connection.execute(
                "SELECT source_digest,state,body FROM b26_memory_uploads WHERE upload_id=?",
                (upload_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            envelope = json.loads(row["body"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise RemoteMemoryError("upload_corrupt", "stored remote upload is invalid") from exc
        if not isinstance(envelope, dict):
            raise RemoteMemoryError("upload_corrupt", "stored remote upload is invalid")
        return str(row["source_digest"]), str(row["state"]), envelope

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("remote memory clock must be timezone-aware")
        return value.astimezone(timezone.utc)


class RemoteMemoryTargetAdapter:
    """A target-side adapter that consumes a package from a remote Job.

    This adapter has no filesystem, network or Core database access.  It is
    useful for an actual Target connector implementation to share the same
    validation and for deterministic integration tests to prove that the
    package is consumed as a typed object rather than treated as arbitrary Job
    arguments.
    """

    def __init__(
        self,
        target_id: str,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if not target_id:
            raise ValueError("Target ID is required")
        self.target_id = target_id
        self.clock = clock
        self.received: dict[str, RemoteMemoryPack] = {}
        self.revoked: dict[str, str] = {}

    def consume(self, pack: RemoteMemoryPack | Mapping[str, Any]) -> RemoteMemoryPack:
        if isinstance(pack, RemoteMemoryPack):
            value = pack
        else:
            raw = pack.get("remote_memory_pack")
            if raw is not None:
                if not isinstance(raw, Mapping):
                    raise RemoteMemoryError(
                        "memory_pack_invalid", "remote memory package envelope is invalid"
                    )
                pack = raw
            value = RemoteMemoryPack.model_validate(pack)
        revoked_digest = self.revoked.get(value.package_id)
        if revoked_digest is not None:
            if revoked_digest != value.package_digest:
                raise RemoteMemoryError(
                    "package_digest_mismatch", "remote memory package digest is invalid"
                )
            raise RemoteMemoryError("package_unavailable", "memory package is revoked or expired")
        if value.target_id != self.target_id:
            raise RemoteMemoryError(
                "target_binding_invalid", "memory package belongs to another Target"
            )
        if value.status != "active":
            raise RemoteMemoryError("package_unavailable", "memory package is revoked or expired")
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("remote memory clock must be timezone-aware")
        if value.expires_at <= now.astimezone(timezone.utc):
            raise RemoteMemoryError("package_expired", "memory package is expired")
        if not value.has_valid_digest():
            raise RemoteMemoryError("package_digest_mismatch", "memory package digest is invalid")
        self.received[value.package_id] = value
        return value

    def revoke(self, pack: RemoteMemoryPack | Mapping[str, Any]) -> None:
        """Apply a Core revocation notification before accepting stale copies."""

        if isinstance(pack, RemoteMemoryPack):
            value = pack
        else:
            raw = pack.get("remote_memory_pack")
            if isinstance(raw, Mapping):
                pack = raw
            value = RemoteMemoryPack.model_validate(pack)
        if value.target_id != self.target_id:
            raise RemoteMemoryError(
                "target_binding_invalid", "memory package belongs to another Target"
            )
        if not value.has_valid_digest():
            raise RemoteMemoryError(
                "package_digest_mismatch", "remote memory package digest is invalid"
            )
        self.revoked[value.package_id] = value.package_digest
        self.received.pop(value.package_id, None)

    def consume_job(self, job: RemoteExecutionJob) -> RemoteMemoryPack:
        if job.target_id != self.target_id:
            raise RemoteMemoryError(
                "target_binding_invalid", "remote Job belongs to another Target"
            )
        raw = job.arguments.get("remote_memory_pack")
        if not isinstance(raw, Mapping):
            raise RemoteMemoryError("memory_pack_missing", "remote Job has no typed memory package")
        return self.consume(raw)


class RemoteMemoryTargetRuntime:
    """Bind package creation to a registered Target and its live lease.

    ``RemoteExecutionController`` remains the authority for registration,
    capability manifests and fenced leases.  This bridge only reads that
    authority; it never stores credentials or moves a lease.  A caller builds
    it with the controller and the connector that owns the current lease,
    then passes it as ``RemoteMemoryService(..., remote_runtime=runtime)``.
    """

    def __init__(self, controller: Any, connector: Any) -> None:
        self.controller = controller
        self.connector = connector

    def validate_memory_target(
        self,
        target_id: str,
        *,
        purpose: str,
        expires_at: datetime,
        now: datetime,
    ) -> None:
        del purpose  # Purpose is bound into the package after this check.
        connector_target = getattr(self.connector, "target_id", None)
        if connector_target != target_id:
            raise RemoteMemoryError(
                "target_binding_invalid",
                "package Target does not match the active connector",
            )
        repository = getattr(self.controller, "repository", None)
        if repository is None:
            raise RemoteMemoryError(
                "target_runtime_unavailable",
                "Remote Execution controller is not configured",
            )
        try:
            target = repository.get_target(target_id)
        except Exception as exc:
            raise RemoteMemoryError("target_not_found", "registered Target was not found") from exc
        status = getattr(target.status, "value", target.status)
        if status != "online":
            raise RemoteMemoryError("target_unavailable", "Target is not online")
        manifest = target.capability_manifest
        capabilities = {getattr(value, "value", value) for value in manifest.capabilities}
        if "remote.target.exec" not in capabilities:
            raise RemoteMemoryError(
                "target_capability_missing",
                "Target does not advertise remote execution",
            )
        if REMOTE_MEMORY_TARGET_OPERATION not in manifest.supported_operations:
            raise RemoteMemoryError(
                "target_capability_missing",
                "Target does not advertise memory package consumption",
            )
        lease_id = getattr(self.connector, "lease_id", None)
        lease_token = getattr(self.connector, "lease_token", None)
        lease_fencing = getattr(self.connector, "lease_fencing", None)
        if (
            not isinstance(lease_id, str)
            or not isinstance(lease_token, str)
            or not isinstance(lease_fencing, int)
        ):
            raise RemoteMemoryError(
                "target_runtime_unavailable",
                "active Target lease credentials are unavailable",
            )
        with repository.store._connect() as connection:
            lease = repository._require_live_lease(
                connection,
                target_id=target_id,
                lease_id=lease_id,
                token=lease_token,
                fencing=lease_fencing,
                now=now,
            )
            lease_expires_at = datetime.fromisoformat(str(lease["expires_at"]))
        if lease_expires_at.tzinfo is None:
            lease_expires_at = lease_expires_at.replace(tzinfo=timezone.utc)
        if lease_expires_at < expires_at:
            raise RemoteMemoryError(
                "target_lease_too_short",
                "Target lease expires before the memory package",
            )


__all__ = [
    "MAX_REMOTE_MEMORY_BYTES",
    "MAX_REMOTE_MEMORY_ENTRIES",
    "MAX_REMOTE_MEMORY_TTL_SECONDS",
    "MAX_REMOTE_MEMORY_TOKENS",
    "REMOTE_MEMORY_TARGET_OPERATION",
    "RemoteMemoryError",
    "RemoteMemoryService",
    "RemoteMemoryTargetAdapter",
    "RemoteMemoryTargetRuntime",
]
