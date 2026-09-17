"""Core-facing B2-6 sharing and Writer-promotion service.

The service owns only explicit association and authorization metadata.  The
memory ledger remains the source of immutable versions and publication heads;
the MultiWriter repository remains the source of Writer workspaces, artifacts
and merge outcomes.  A caller may use this module with an ``ApplicationService``
or ``MemoryManager`` object, or directly with a store in focused tests.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4

from pydantic import TypeAdapter

from operant.contracts.b2_1 import (
    DatasetTransfer,
    MemoryConditions,
    MemoryVersion,
    MemoryVersionRef,
    PersonalScope,
    RpcContext,
    RunScope,
    Scope,
    SessionScope,
    SourceRef,
    WorkspaceScope,
)
from operant.contracts.b2_6_sharing import (
    CleanupBlocker,
    DatasetTransferState,
    ProjectWorktreeRegistration,
    SharingCommand,
    SharingConsumer,
    SharingGrant,
    SharingResult,
    SharingState,
    VisibilityDecision,
    WriterMemoryCandidate,
    WriterMemoryEvidence,
    WriterPromotionDecision,
)
from operant.domain.multiwriter import MergeRunStatus
from operant.memory_plugins.ledger import MemoryLedger
from operant.memory_plugins.sharing_schema import SCHEMA_SQL
from operant.persistence.multiwriter import SQLiteMultiWriterRepository

if TYPE_CHECKING:
    from operant.persistence.sqlite import SQLiteStore


_SCOPE_ADAPTER: TypeAdapter[Any] = TypeAdapter(Scope)
_SHARE_PURPOSES = frozenset({"recall", "source_read", "publish", "share"})


class SharingError(RuntimeError):
    """Base class for explicit sharing failures."""


class SharingSchemaError(SharingError):
    """The B2-6 sharing tables have not been installed by the Core migration."""


class SharingNotFoundError(LookupError, SharingError):
    """A requested registration, grant, consumer, transfer or evidence row is absent."""


class SharingConflictError(ValueError, SharingError):
    """A CAS, immutable identity, state or authority check failed."""


class SharingPermissionError(PermissionError, SharingError):
    """A current grant, project association or actor check denied an operation."""


class SharingValidationError(ValueError, SharingError):
    """An input could not be represented by the B2-6 contract."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _json(value: Any) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _as_scope(value: Scope | Mapping[str, Any]) -> Scope:
    try:
        if isinstance(value, (WorkspaceScope, SessionScope, RunScope, PersonalScope)):
            return value
        return cast(Scope, _SCOPE_ADAPTER.validate_python(value))
    except Exception as exc:
        raise SharingValidationError("invalid sharing scope") from exc


def _as_ref(value: MemoryVersionRef | Mapping[str, Any]) -> MemoryVersionRef:
    try:
        return (
            value if isinstance(value, MemoryVersionRef) else MemoryVersionRef.model_validate(value)
        )
    except Exception as exc:
        raise SharingValidationError("invalid memory version reference") from exc


def _scope_identity(scope: Scope) -> tuple[str, ...]:
    if isinstance(scope, WorkspaceScope):
        return (scope.kind, scope.project_id, scope.workspace_id)
    if isinstance(scope, SessionScope):
        return (scope.kind, scope.project_id, scope.workspace_id, scope.session_id)
    if isinstance(scope, RunScope):
        return (
            scope.kind,
            scope.project_id,
            scope.workspace_id,
            scope.run_id,
            scope.writer_id or "",
        )
    return (scope.kind, scope.principal_id, scope.opt_in_grant_id)


def scope_permits(source: Scope, target: Scope) -> bool:
    """Return whether ``target`` is no broader than ``source``.

    A grant may narrow a personal or workspace scope to a concrete consumer,
    but it cannot turn a Run or Session fact into project-wide knowledge.  The
    helper is public so recall and API adapters can apply the same rule.
    """

    source = _as_scope(source)
    target = _as_scope(target)
    if source == target:
        return True
    if source.kind == "personal":
        return True
    if isinstance(source, WorkspaceScope) and isinstance(target, (SessionScope, RunScope)):
        return (source.project_id, source.workspace_id) == (
            target.project_id,
            target.workspace_id,
        )
    return False


def _explicit_grant_permits(source: Scope, target: Scope) -> bool:
    """Check an explicit grant's shape before its exact refs are considered."""

    source = _as_scope(source)
    target = _as_scope(target)
    if isinstance(source, (RunScope, SessionScope)):
        return source == target
    if isinstance(source, WorkspaceScope):
        return isinstance(target, (WorkspaceScope, SessionScope, RunScope, PersonalScope))
    if source.kind == "personal":
        return isinstance(target, (WorkspaceScope, SessionScope, RunScope, PersonalScope))
    return False


def _scope_target_matches(grant_target: Scope, requested: Scope) -> bool:
    if isinstance(grant_target, PersonalScope) or isinstance(requested, PersonalScope):
        return grant_target == requested
    return grant_target == requested or scope_permits(grant_target, requested)


def _commit_ref(value: str) -> str:
    normalized = value[4:] if value.startswith("git:") else value
    if not 7 <= len(normalized) <= 128 or any(char.isspace() for char in normalized):
        raise SharingValidationError("commit reference is invalid")
    return normalized


def _tree_digest(value: str) -> str:
    """Represent Git's SHA-1 tree object as the contract's SHA-256 digest."""

    return hashlib.sha256(f"git-tree:{value}".encode("ascii")).hexdigest()


class SharingService:
    """Explicit Project/worktree sharing and verified Writer evidence."""

    def __init__(
        self,
        manager_or_store: Any,
        *,
        ledger: MemoryLedger | None = None,
        writer_repository: SQLiteMultiWriterRepository | None = None,
        writer_adapter: Any | None = None,
        principal_resolver: Callable[[str], str] | None = None,
        git_timeout_seconds: int = 30,
        initialize_schema: bool = False,
    ) -> None:
        manager = manager_or_store if hasattr(manager_or_store, "store") else None
        self.manager = manager
        self.store: SQLiteStore = manager.store if manager is not None else manager_or_store
        self.ledger = ledger or (getattr(manager, "ledger", None) if manager is not None else None)
        self.registry = getattr(manager, "registry", None) if manager is not None else None
        self.writer_repository = writer_repository or SQLiteMultiWriterRepository(self.store)
        self.writer_adapter = writer_adapter
        self.principal_resolver = principal_resolver
        if not 1 <= git_timeout_seconds <= 300:
            raise ValueError("Git verification timeout must be between 1 and 300 seconds")
        self.git_timeout_seconds = git_timeout_seconds
        if initialize_schema:
            self.ensure_schema()

    def ensure_schema(self) -> None:
        """Install the feature tables for an isolated test database.

        Production Core should include :data:`SCHEMA_SQL` in its v18 migration;
        this method is intentionally explicit and is not called by read paths.
        """

        with self.store._connect() as connection:
            connection.executescript(SCHEMA_SQL)

    def _require_schema(self, connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            ("b26_worktree_registrations",),
        ).fetchone()
        if row is None:
            raise SharingSchemaError("B2-6 sharing schema is not installed; apply v18")

    def _project(self, project_id: str) -> Mapping[str, Any] | None:
        if self.manager is None:
            return None
        lookup = getattr(self.manager, "_project", None)
        if not callable(lookup):
            return None
        try:
            return cast(Mapping[str, Any], lookup(project_id))
        except Exception as exc:
            raise SharingNotFoundError(f"project not found: {project_id}") from exc

    def _trusted_principal(self, project_id: str, supplied: str | None) -> str:
        """Resolve the actor from Core context when an adapter provides it."""

        trusted: str | None = None
        if self.principal_resolver is not None:
            trusted = self.principal_resolver(project_id)
        elif self.manager is not None:
            for name in ("principal_id", "user_id", "current_principal_id"):
                value = getattr(self.manager, name, None)
                if isinstance(value, str) and value:
                    trusted = value
                    break
            if trusted is None and self.registry is not None:
                try:
                    project = self._project(project_id)
                    if project is None:
                        raise SharingNotFoundError("project not found")
                    installation_id = project.get("installation_id")
                    if installation_id:
                        trusted = self.registry.get_installation(installation_id).owner.principal_id
                except Exception as exc:
                    raise SharingPermissionError(
                        "authenticated local principal is unavailable"
                    ) from exc
        if trusted is not None:
            if supplied is not None and supplied != trusted:
                raise SharingPermissionError(
                    "sharing actor is not the authenticated local principal"
                )
            return trusted
        raise SharingPermissionError("sharing actor must come from Core authentication")

    @staticmethod
    def _row_time(value: Any) -> datetime:
        try:
            return datetime.fromisoformat(str(value))
        except (TypeError, ValueError) as exc:
            raise SharingValidationError("stored sharing timestamp is invalid") from exc

    @staticmethod
    def _registration(row: sqlite3.Row) -> ProjectWorktreeRegistration:
        return ProjectWorktreeRegistration(
            registration_id=row["registration_id"],
            project_id=row["project_id"],
            workspace_id=row["workspace_id"],
            worktree_id=row["worktree_id"],
            workspace_ref=row["workspace_ref"],
            branch_ref=row["branch_ref"],
            commit_ref=row["commit_ref"],
            tree_digest=row["tree_digest"],
            principal_id=row["principal_id"],
            association_revision=row["association_revision"],
            permission_epoch=row["permission_epoch"],
            state=row["state"],
            revoked_at=None
            if row["revoked_at"] is None
            else SharingService._row_time(row["revoked_at"]),
            revoke_reason=row["revoke_reason"],
            created_at=SharingService._row_time(row["created_at"]),
            updated_at=SharingService._row_time(row["updated_at"]),
        )

    @staticmethod
    def _grant(row: sqlite3.Row) -> SharingGrant:
        return SharingGrant(
            grant_id=row["grant_id"],
            project_id=row["project_id"],
            source_dataset_id=row["source_dataset_id"],
            source_scope=_as_scope(json.loads(row["source_scope_json"])),
            target_scope=_as_scope(json.loads(row["target_scope_json"])),
            subject_id=row["subject_id"],
            grantor_id=row["grantor_id"],
            purpose=row["purpose"],
            memory_refs=tuple(
                MemoryVersionRef.model_validate(item)
                for item in json.loads(row["memory_refs_json"])
            ),
            policy_revision=row["policy_revision"],
            permission_epoch=row["permission_epoch"],
            expires_at=SharingService._row_time(row["expires_at"]),
            revision=row["revision"],
            state=row["state"],
            revoked_at=None
            if row["revoked_at"] is None
            else SharingService._row_time(row["revoked_at"]),
            revoke_reason=row["revoke_reason"],
            created_at=SharingService._row_time(row["created_at"]),
            updated_at=SharingService._row_time(row["updated_at"]),
        )

    @staticmethod
    def _consumer(row: sqlite3.Row) -> SharingConsumer:
        return SharingConsumer(
            consumer_id=row["consumer_id"],
            project_id=row["project_id"],
            dataset_id=row["dataset_id"],
            installation_id=row["installation_id"],
            grant_id=row["grant_id"],
            purpose=row["purpose"],
            state=row["state"],
            revision=row["revision"],
            revoked_at=None
            if row["revoked_at"] is None
            else SharingService._row_time(row["revoked_at"]),
            reason=row["reason"],
            created_at=SharingService._row_time(row["created_at"]),
            updated_at=SharingService._row_time(row["updated_at"]),
        )

    @staticmethod
    def _transfer(row: sqlite3.Row) -> DatasetTransferState:
        return DatasetTransferState(
            transfer_id=row["transfer_id"],
            project_id=row["project_id"],
            request=DatasetTransfer.model_validate_json(row["request_json"]),
            source_installation_id=row["source_installation_id"],
            source_consumer_id=row["source_consumer_id"],
            destination_consumer_id=row["destination_consumer_id"],
            state=row["state"],
            revision=row["revision"],
            evidence_refs=tuple(json.loads(row["evidence_refs_json"])),
            reason=row["reason"],
            created_at=SharingService._row_time(row["created_at"]),
            updated_at=SharingService._row_time(row["updated_at"]),
        )

    @staticmethod
    def _evidence(row: sqlite3.Row) -> WriterMemoryEvidence:
        return WriterMemoryEvidence(
            evidence_id=row["evidence_id"],
            memory_ref=MemoryVersionRef.model_validate_json(row["memory_ref_json"]),
            candidate_head_revision=row["candidate_head_revision"],
            project_id=row["project_id"],
            workspace_id=row["workspace_id"],
            worktree_id=row["worktree_id"],
            writer_workspace_id=row["writer_workspace_id"],
            graph_run_id=row["graph_run_id"],
            run_id=row["run_id"],
            branch_ref=row["branch_ref"],
            base_revision=row["base_revision"],
            base_tree_digest=row["base_tree_digest"],
            merge_run_id=row["merge_run_id"],
            target_isolation_ref=row["target_isolation_ref"],
            target_commit_ref=row["target_commit_ref"],
            target_tree_digest=row["target_tree_digest"],
            verification_artifact_refs=tuple(json.loads(row["verification_artifact_refs_json"])),
            verification_digest=row["verification_digest"],
            verification_status=row["verification_status"],
            state=row["state"],
            published_memory_ref=(
                None
                if row["published_memory_ref_json"] is None
                else MemoryVersionRef.model_validate_json(row["published_memory_ref_json"])
            ),
            permission_epoch=row["permission_epoch"],
            revision=row["revision"],
            reason=row["reason"],
            created_at=SharingService._row_time(row["created_at"]),
            updated_at=SharingService._row_time(row["updated_at"]),
        )

    def _active_registration(
        self, project_id: str, workspace_id: str
    ) -> ProjectWorktreeRegistration:
        with self.store._connect() as connection:
            self._require_schema(connection)
            row = connection.execute(
                """SELECT * FROM b26_worktree_registrations
                   WHERE project_id=? AND workspace_id=? AND state='active'
                   ORDER BY association_revision DESC LIMIT 1""",
                (project_id, workspace_id),
            ).fetchone()
        if row is None:
            raise SharingPermissionError(
                "target workspace has no active Project-worktree registration"
            )
        return self._registration(row)

    def _registration_by_worktree(
        self, project_id: str, worktree_id: str
    ) -> ProjectWorktreeRegistration:
        with self.store._connect() as connection:
            self._require_schema(connection)
            row = connection.execute(
                """SELECT * FROM b26_worktree_registrations
                   WHERE project_id=? AND worktree_id=?
                   ORDER BY association_revision DESC LIMIT 1""",
                (project_id, worktree_id),
            ).fetchone()
        if row is None:
            raise SharingNotFoundError("Project-worktree registration not found")
        return self._registration(row)

    def _workspace_for_registration(
        self,
        project_id: str,
        *,
        workspace_id: str | None,
        workspace_path: str | None,
    ) -> tuple[Any, Path]:
        if workspace_id is not None and workspace_path is not None:
            raise SharingValidationError("worktree registration accepts one workspace identity")
        if workspace_id is None and workspace_path is None:
            raise SharingValidationError("worktree registration needs a workspace identity")
        try:
            if workspace_id is not None:
                initialization = self.store.get_workspace_initialization_by_id(workspace_id)
                configured = Path(initialization.workspace_ref)
            else:
                configured = Path(workspace_path or "")
                if not configured.is_absolute():
                    raise ValueError("workspace path must be absolute")
                resolved = configured.resolve(strict=True)
                initialization = self.store.get_workspace_initialization(
                    hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()
                )
        except Exception as exc:
            if workspace_path is None or self.manager is None:
                raise SharingNotFoundError("Core workspace initialization is unavailable") from exc
            try:
                initialization, _created = self.manager.service.initialize_workspace(workspace_path)
            except Exception as initialize_exc:
                raise SharingNotFoundError(
                    "Core workspace initialization is unavailable"
                ) from initialize_exc
        root = Path(initialization.workspace_ref)
        if not root.is_absolute():
            raise SharingValidationError("registered workspace path is not absolute")
        try:
            root = root.resolve(strict=True)
        except OSError as exc:
            raise SharingValidationError("registered workspace path is unavailable") from exc
        if Path(initialization.workspace_ref).is_symlink() or not root.is_dir():
            raise SharingValidationError("registered workspace path identity is unsafe")
        project = self._project(project_id)
        if project is not None and project.get("workspace_id") != initialization.id:
            raise SharingPermissionError("workspace is not the explicit workspace of this project")
        return initialization, root

    def _project_permission_epoch(self, project_id: str) -> int:
        if self.registry is None:
            return 0
        project = self._project(project_id)
        if project is None or not project.get("installation_id"):
            return 0
        installation = self.registry.get_installation(project["installation_id"])
        return int(installation.permission_epoch)

    def _dataset_permission_epoch(self, dataset_id: str) -> int:
        """Read the current epoch from the registry owner of a dataset."""

        if self.registry is None:
            return 0
        try:
            dataset = self.registry.get_dataset(dataset_id)
            if dataset.installation_id is None:
                return 0
            return int(self.registry.get_installation(dataset.installation_id).permission_epoch)
        except Exception as exc:
            raise SharingPermissionError("dataset permission epoch is unavailable") from exc

    def _core_authorize_writer_source(
        self,
        source: SourceRef,
        *,
        project_id: str,
        dataset_id: str,
        permission_epoch: int,
    ) -> None:
        """Require Core to resolve and authorize a non-ledger Writer source.

        ``SourceRef`` metadata is caller supplied.  Writer promotion may only
        retain an Item source after the canonical Manager/Governance path has
        checked its owning thread, revision, scope, epoch and content digest.
        The sharing service repeats the digest check against the immutable Item
        so an adapter cannot turn an arbitrary source id into evidence.
        """

        if source.source_type != "item":
            # Governance currently exposes canonical user Items as the only
            # non-ledger extraction source.  Mailbox, artifact and event
            # payloads must be registered by their owning Core service before
            # they can become Writer evidence.
            raise SharingPermissionError(
                f"Writer source type {source.source_type!r} has no Core canonical authority"
            )
        if not isinstance(source.scope, (WorkspaceScope, SessionScope, RunScope)):
            raise SharingPermissionError("Writer Item source has no project scope")
        if source.scope.project_id != project_id:
            raise SharingPermissionError("Writer source belongs to another project")
        if source.permission_epoch != permission_epoch:
            raise SharingPermissionError("Writer source permission epoch is stale")
        manager = self.manager
        if manager is None or self.registry is None:
            raise SharingPermissionError("Core source authorization is unavailable")
        authorizer = getattr(manager, "authorize_source", None)
        if not callable(authorizer):
            raise SharingPermissionError("Core source authorization is unavailable")
        project = self._project(project_id)
        if project is None or not project.get("installation_id"):
            raise SharingPermissionError("project has no bound memory installation")
        try:
            installation = self.registry.get_installation(project["installation_id"])
        except Exception as exc:
            raise SharingPermissionError("project installation is unavailable") from exc
        if installation.dataset_id != dataset_id:
            raise SharingPermissionError("Writer source dataset does not match the project")
        binding_epoch = int(getattr(installation, "binding_epoch", 0))
        lease_fencing = 1
        binding_id = getattr(installation, "binding_id", None)
        if binding_id:
            try:
                binding = self.registry.get_binding(binding_id)
                binding_epoch = int(binding.binding_epoch)
                lease_fencing = max(1, len(binding.active_run_ids) + 1)
            except Exception as exc:
                raise SharingPermissionError("project binding is unavailable") from exc
        context = RpcContext(
            sdk_version="operant-memory-sdk.v1",
            request_id="sharing_source_" + uuid4().hex,
            installation_id=installation.installation_id,
            dataset_id=dataset_id,
            scope=source.scope,
            deadline=_now() + timedelta(seconds=30),
            cancel_token="sharing_cancel_" + uuid4().hex,
            idempotency_key="sharing_source_" + uuid4().hex,
            request_digest=_digest(source),
            binding_epoch=binding_epoch,
            permission_epoch=permission_epoch,
            lease_fencing=lease_fencing,
        )
        try:
            authorized = bool(authorizer(context, source))
        except Exception as exc:
            raise SharingPermissionError("Core source authorization failed") from exc
        if not authorized:
            raise SharingPermissionError("Writer source is not Core-authorized")

        # Keep a direct canonical-content fence alongside the Core callback.
        # Manager/Governance accepts the legacy source-local revision shape;
        # this check deliberately does not replace that compatibility logic.
        try:
            item = manager.store.get_item(source.source_id)
            payload = item.payload
            body = getattr(payload, "text", None)
            expected_digest = (
                hashlib.sha256(body.encode("utf-8")).hexdigest()
                if isinstance(body, str)
                else _digest(payload)
            )
        except Exception as exc:
            raise SharingPermissionError("Writer canonical Item is unavailable") from exc
        if expected_digest != source.content_digest:
            raise SharingPermissionError("Writer canonical Item digest does not match")

    def _version_sources_current(self, version: MemoryVersion, expected_epoch: int) -> bool:
        """Check the source epoch chain used by same-scope implicit access."""

        if self.ledger is None:
            return False
        ledger = self.ledger
        assert ledger is not None
        visited: set[tuple[str, str, int]] = set()

        def visit(current: MemoryVersion) -> bool:
            for source in current.sources:
                if source.availability != "available" or source.permission_epoch != expected_epoch:
                    return False
                if not scope_permits(source.scope, version.scope):
                    return False
                if source.source_type != "memory_version":
                    continue
                marker = (current.ref.dataset_id, source.source_id, source.revision)
                if marker in visited:
                    continue
                visited.add(marker)
                try:
                    nested = ledger.get_version(
                        current.ref.dataset_id, source.source_id, source.revision
                    )
                except Exception:
                    return False
                if nested.ref.content_digest != source.content_digest or not visit(nested):
                    return False
            return True

        return visit(version)

    def _registry_dataset(self, dataset_id: str) -> Any:
        if self.registry is None:
            raise SharingPermissionError("dataset ownership requires the Core registry")
        try:
            return self.registry.get_dataset(dataset_id)
        except Exception as exc:
            raise SharingNotFoundError("dataset ownership record is unavailable") from exc

    def _local_git_identity(self, root: Path) -> tuple[str, str, str]:
        commit = _commit_ref(self._git(root, "rev-parse", "HEAD"))
        tree = self._git(root, "rev-parse", "HEAD^{tree}")
        if len(tree) != 40 or any(char not in "0123456789abcdef" for char in tree):
            raise SharingValidationError("Git worktree tree identity is invalid")
        try:
            branch = self._git(root, "symbolic-ref", "--short", "HEAD")
        except SharingValidationError:
            branch = f"detached:{commit}"
        if any(char in branch for char in ("\x00", "\n", "\r")):
            raise SharingValidationError("Git branch identity is invalid")
        return branch, commit, _tree_digest(tree)

    def _registration_from_command(self, command: SharingCommand) -> ProjectWorktreeRegistration:
        supplied = command.registration
        workspace_id = command.workspace_id or (None if supplied is None else supplied.workspace_id)
        workspace_path = command.workspace_path
        initialization, root = self._workspace_for_registration(
            command.project_id,
            workspace_id=workspace_id,
            workspace_path=workspace_path,
        )
        branch, commit, tree_digest = self._local_git_identity(root)
        if command.branch_ref is not None and command.branch_ref != branch:
            raise SharingConflictError("requested branch differs from the local Git branch")
        worktree_id = command.worktree_id or (
            "worktree_" + hashlib.sha256(f"{root}\n{branch}\n{commit}".encode()).hexdigest()
        )
        actor = self._trusted_principal(command.project_id, None)
        permission_epoch = self._project_permission_epoch(command.project_id)
        expected = {
            "project_id": command.project_id,
            "workspace_id": initialization.id,
            "worktree_id": worktree_id,
            "workspace_ref": f"workspace:{initialization.workspace_hash}",
            "branch_ref": branch,
            "commit_ref": f"git:{commit}",
            "tree_digest": tree_digest,
            "principal_id": actor,
            "permission_epoch": permission_epoch,
        }
        if supplied is not None:
            for field, value in expected.items():
                incoming = getattr(supplied, field)
                if field == "commit_ref" and incoming is not None:
                    incoming = f"git:{_commit_ref(incoming)}"
                if incoming != value:
                    raise SharingPermissionError(
                        f"worktree registration field {field} is not locally verified"
                    )
            registration_id = supplied.registration_id
            association_revision = supplied.association_revision
        else:
            registration_id = (
                "worktree_registration_"
                + hashlib.sha256(f"{command.project_id}\n{worktree_id}".encode()).hexdigest()
            )
            association_revision = 0
        return ProjectWorktreeRegistration(
            registration_id=registration_id,
            project_id=command.project_id,
            workspace_id=initialization.id,
            worktree_id=worktree_id,
            workspace_ref=f"workspace:{initialization.workspace_hash}",
            branch_ref=branch,
            commit_ref=f"git:{commit}",
            tree_digest=tree_digest,
            principal_id=actor,
            association_revision=association_revision,
            permission_epoch=permission_epoch,
        )

    def register_worktree(
        self,
        registration: ProjectWorktreeRegistration,
        *,
        expected_revision: int | None = None,
    ) -> ProjectWorktreeRegistration:
        if registration.state != "active":
            raise SharingValidationError("worktree registration must start active")
        actor = self._trusted_principal(registration.project_id, None)
        if actor != registration.principal_id:
            registration = registration.model_copy(update={"principal_id": actor})
        self._project(registration.project_id)
        with self.store._connect() as connection:
            self._require_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM b26_worktree_registrations WHERE registration_id=?",
                (registration.registration_id,),
            ).fetchone()
            if row is None:
                duplicate = connection.execute(
                    """SELECT * FROM b26_worktree_registrations
                       WHERE project_id=? AND worktree_id=?""",
                    (registration.project_id, registration.worktree_id),
                ).fetchone()
                if duplicate is not None:
                    current = self._registration(duplicate)
                    if current == registration:
                        return current
                    raise SharingConflictError(
                        "Project-worktree identity already belongs to another registration"
                    )
                if registration.association_revision != 0:
                    raise SharingConflictError(
                        "new worktree registration must start at revision zero"
                    )
                connection.execute(
                    """INSERT INTO b26_worktree_registrations(
                       registration_id,project_id,workspace_id,worktree_id,workspace_ref,
                       branch_ref,commit_ref,tree_digest,principal_id,association_revision,
                       permission_epoch,state,revoked_at,revoke_reason,created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        registration.registration_id,
                        registration.project_id,
                        registration.workspace_id,
                        registration.worktree_id,
                        registration.workspace_ref,
                        registration.branch_ref,
                        registration.commit_ref,
                        registration.tree_digest,
                        registration.principal_id,
                        0,
                        registration.permission_epoch,
                        "active",
                        None,
                        None,
                        registration.created_at.isoformat(),
                        registration.updated_at.isoformat(),
                    ),
                )
                return registration
            current = self._registration(row)
            identity = (
                current.project_id,
                current.workspace_id,
                current.worktree_id,
                current.workspace_ref,
                current.branch_ref,
                current.commit_ref,
                current.tree_digest,
                current.principal_id,
            )
            incoming_identity = (
                registration.project_id,
                registration.workspace_id,
                registration.worktree_id,
                registration.workspace_ref,
                registration.branch_ref,
                registration.commit_ref,
                registration.tree_digest,
                registration.principal_id,
            )
            if current.state == "active" and identity == incoming_identity:
                return current
            if expected_revision is None or expected_revision != current.association_revision:
                raise SharingConflictError("worktree registration revision conflict")
            updated = registration.model_copy(
                update={
                    "association_revision": current.association_revision + 1,
                    "created_at": current.created_at,
                    "updated_at": _now(),
                    "state": "active",
                    "revoked_at": None,
                    "revoke_reason": None,
                }
            )
            connection.execute(
                """UPDATE b26_worktree_registrations SET
                   project_id=?,workspace_id=?,worktree_id=?,workspace_ref=?,branch_ref=?,
                   commit_ref=?,tree_digest=?,principal_id=?,association_revision=?,
                   permission_epoch=?,state=?,revoked_at=?,revoke_reason=?,updated_at=?
                   WHERE registration_id=? AND association_revision=?""",
                (
                    updated.project_id,
                    updated.workspace_id,
                    updated.worktree_id,
                    updated.workspace_ref,
                    updated.branch_ref,
                    updated.commit_ref,
                    updated.tree_digest,
                    updated.principal_id,
                    updated.association_revision,
                    updated.permission_epoch,
                    updated.state,
                    None,
                    None,
                    updated.updated_at.isoformat(),
                    updated.registration_id,
                    current.association_revision,
                ),
            )
            return updated

    def revoke_worktree(
        self,
        registration_id: str,
        *,
        expected_revision: int,
        reason: str,
    ) -> ProjectWorktreeRegistration:
        if not reason.strip():
            raise SharingValidationError("worktree revocation reason is required")
        with self.store._connect() as connection:
            self._require_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM b26_worktree_registrations WHERE registration_id=?",
                (registration_id,),
            ).fetchone()
            if row is None:
                raise SharingNotFoundError("Project-worktree registration not found")
            current = self._registration(row)
            if current.state == "revoked":
                return current
            if current.association_revision != expected_revision:
                raise SharingConflictError("worktree registration revision conflict")
            now = _now()
            updated = current.model_copy(
                update={
                    "association_revision": current.association_revision + 1,
                    "state": "revoked",
                    "revoked_at": now,
                    "revoke_reason": reason.strip(),
                    "updated_at": now,
                }
            )
            assert updated.revoked_at is not None
            connection.execute(
                """UPDATE b26_worktree_registrations SET association_revision=?,state=?,
                   revoked_at=?,revoke_reason=?,updated_at=?
                   WHERE registration_id=? AND association_revision=?""",
                (
                    updated.association_revision,
                    updated.state,
                    updated.revoked_at.isoformat(),
                    updated.revoke_reason,
                    updated.updated_at.isoformat(),
                    registration_id,
                    expected_revision,
                ),
            )
            return updated

    def list_worktrees(self, project_id: str) -> tuple[ProjectWorktreeRegistration, ...]:
        with self.store._connect() as connection:
            self._require_schema(connection)
            rows = connection.execute(
                "SELECT * FROM b26_worktree_registrations WHERE project_id=? "
                "ORDER BY association_revision, registration_id",
                (project_id,),
            ).fetchall()
        return tuple(self._registration(row) for row in rows)

    def _ledger_version(self, ref: MemoryVersionRef) -> MemoryVersion | None:
        if self.ledger is None:
            return None
        try:
            return self.ledger.get_version(ref.dataset_id, ref.record_id, ref.version)
        except Exception as exc:
            raise SharingPermissionError("memory version is unavailable") from exc

    def _validate_grant_refs(self, grant: SharingGrant) -> None:
        self._validate_source_registration(grant.source_scope)
        for ref in grant.memory_refs:
            version = self._ledger_version(ref)
            if version is None:
                continue
            if version.ref != ref or version.scope != grant.source_scope:
                raise SharingPermissionError(
                    "grant source scope or digest does not match the memory version"
                )
            try:
                head = self.ledger.get_head(ref.dataset_id, ref.record_id)  # type: ignore[union-attr]
            except Exception as exc:
                raise SharingPermissionError("grant source head is unavailable") from exc
            if head.state != "published" or head.published_version != ref:
                raise SharingPermissionError("only the current published version may be shared")

    def _validate_source_registration(self, scope: Scope) -> None:
        if isinstance(scope, (WorkspaceScope, SessionScope, RunScope)):
            self._active_registration(scope.project_id, scope.workspace_id)

    def _validate_target_registration(self, scope: Scope) -> None:
        if isinstance(scope, (WorkspaceScope, SessionScope, RunScope)):
            self._active_registration(scope.project_id, scope.workspace_id)

    def create_grant(
        self,
        grant: SharingGrant,
        *,
        trusted_grantor_id: str | None = None,
    ) -> SharingGrant:
        if grant.state != "active":
            raise SharingValidationError("a new sharing grant must start active")
        if not _explicit_grant_permits(grant.source_scope, grant.target_scope):
            raise SharingPermissionError("sharing target would broaden source visibility")
        grantor = self._trusted_principal(grant.project_id, trusted_grantor_id)
        current_epoch = self._project_permission_epoch(grant.project_id)
        if grant.grantor_id != grantor or grant.permission_epoch != current_epoch:
            grant = grant.model_copy(
                update={"grantor_id": grantor, "permission_epoch": current_epoch}
            )
        if isinstance(grant.source_scope, RunScope) and grant.target_scope != grant.source_scope:
            raise SharingPermissionError("Run-scoped memory cannot be shared outside its Run")
        if (
            isinstance(grant.source_scope, SessionScope)
            and grant.target_scope != grant.source_scope
        ):
            raise SharingPermissionError(
                "Session-scoped memory cannot be shared outside its Session"
            )
        if (
            isinstance(grant.target_scope, PersonalScope)
            and grant.target_scope.principal_id != grant.subject_id
        ):
            raise SharingPermissionError("personal grant target must name its subject")
        self._validate_target_registration(grant.target_scope)
        self._validate_grant_refs(grant)
        with self.store._connect() as connection:
            self._require_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM b26_sharing_grants WHERE grant_id=?", (grant.grant_id,)
            ).fetchone()
            if row is not None:
                current = self._grant(row)
                if current == grant or (
                    current.state == "active"
                    and current.source_dataset_id == grant.source_dataset_id
                    and current.source_scope == grant.source_scope
                    and current.target_scope == grant.target_scope
                    and current.subject_id == grant.subject_id
                    and current.purpose == grant.purpose
                    and current.memory_refs == grant.memory_refs
                ):
                    return current
                raise SharingConflictError("sharing grant identity already exists")
            if grant.revision != 0:
                raise SharingConflictError("new sharing grant must start at revision zero")
            connection.execute(
                """INSERT INTO b26_sharing_grants(
                   grant_id,project_id,source_dataset_id,source_scope_json,target_scope_json,
                   subject_id,grantor_id,purpose,memory_refs_json,policy_revision,
                   permission_epoch,expires_at,revision,state,revoked_at,revoke_reason,
                   created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    grant.grant_id,
                    grant.project_id,
                    grant.source_dataset_id,
                    _json(grant.source_scope),
                    _json(grant.target_scope),
                    grant.subject_id,
                    grant.grantor_id,
                    grant.purpose,
                    _json(grant.memory_refs),
                    grant.policy_revision,
                    grant.permission_epoch,
                    grant.expires_at.isoformat(),
                    grant.revision,
                    grant.state,
                    None,
                    None,
                    grant.created_at.isoformat(),
                    grant.updated_at.isoformat(),
                ),
            )
            return grant

    def _grant_by_id(self, grant_id: str) -> SharingGrant:
        with self.store._connect() as connection:
            self._require_schema(connection)
            row = connection.execute(
                "SELECT * FROM b26_sharing_grants WHERE grant_id=?", (grant_id,)
            ).fetchone()
        if row is None:
            raise SharingNotFoundError("sharing grant not found")
        return self._grant(row)

    def list_grants(self, project_id: str) -> tuple[SharingGrant, ...]:
        with self.store._connect() as connection:
            self._require_schema(connection)
            rows = connection.execute(
                "SELECT * FROM b26_sharing_grants WHERE project_id=? ORDER BY created_at,grant_id",
                (project_id,),
            ).fetchall()
        return tuple(self._grant(row) for row in rows)

    def revoke_grant(
        self,
        grant_id: str,
        *,
        expected_revision: int,
        reason: str,
    ) -> SharingGrant:
        if not reason.strip():
            raise SharingValidationError("grant revocation reason is required")
        with self.store._connect() as connection:
            self._require_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM b26_sharing_grants WHERE grant_id=?", (grant_id,)
            ).fetchone()
            if row is None:
                raise SharingNotFoundError("sharing grant not found")
            current = self._grant(row)
            if current.state == "revoked":
                return current
            if current.revision != expected_revision:
                raise SharingConflictError("sharing grant revision conflict")
            now = _now()
            updated = current.model_copy(
                update={
                    "revision": current.revision + 1,
                    "state": "revoked",
                    "revoked_at": now,
                    "revoke_reason": reason.strip(),
                    "updated_at": now,
                }
            )
            assert updated.revoked_at is not None
            connection.execute(
                """UPDATE b26_sharing_grants SET revision=?,state=?,revoked_at=?,
                   revoke_reason=?,updated_at=? WHERE grant_id=? AND revision=?""",
                (
                    updated.revision,
                    updated.state,
                    updated.revoked_at.isoformat(),
                    updated.revoke_reason,
                    updated.updated_at.isoformat(),
                    grant_id,
                    expected_revision,
                ),
            )
            return updated

    def authorize(
        self,
        ref: MemoryVersionRef,
        *,
        target_scope: Scope,
        subject_id: str,
        purpose: Literal[
            "recall", "source_read", "publish", "share", "transfer", "delete"
        ] = "recall",
        permission_epoch: int | None = None,
        grant_id: str | None = None,
        now: datetime | None = None,
    ) -> VisibilityDecision:
        observed = now or _now()
        target_scope = _as_scope(target_scope)
        version = self._ledger_version(ref)
        if version is None:
            return VisibilityDecision(
                status="not_selected",
                allowed=False,
                reason_code="memory_unavailable",
                source_ref=ref,
                checked_at=observed,
            )
        try:
            head = self.ledger.get_head(ref.dataset_id, ref.record_id)  # type: ignore[union-attr]
        except Exception:
            return VisibilityDecision(
                status="not_selected",
                allowed=False,
                reason_code="memory_head_unavailable",
                source_ref=ref,
                checked_at=observed,
            )
        if head.state != "published" or head.published_version != ref:
            return VisibilityDecision(
                status="not_selected",
                allowed=False,
                reason_code="memory_not_current_head",
                source_ref=ref,
                checked_at=observed,
            )
        if version.ref != ref:
            return VisibilityDecision(
                status="not_selected",
                allowed=False,
                reason_code="memory_digest_mismatch",
                source_ref=ref,
                checked_at=observed,
            )
        if version.scope == target_scope and grant_id is not None:
            current_epoch = (
                self._project_permission_epoch(target_scope.project_id)
                if isinstance(target_scope, (WorkspaceScope, SessionScope, RunScope))
                else self._dataset_permission_epoch(ref.dataset_id)
            )
            if permission_epoch is not None and permission_epoch != current_epoch:
                return VisibilityDecision(
                    status="permission_epoch_mismatch",
                    allowed=False,
                    reason_code="target_epoch_stale",
                    source_ref=ref,
                    permission_epoch=current_epoch,
                    checked_at=observed,
                )
            if not self._version_sources_current(version, current_epoch):
                return VisibilityDecision(
                    status="permission_epoch_mismatch",
                    allowed=False,
                    reason_code="source_epoch_stale",
                    source_ref=ref,
                    permission_epoch=current_epoch,
                    checked_at=observed,
                )
        if version.scope == target_scope and grant_id is None:
            current_epoch = (
                self._project_permission_epoch(target_scope.project_id)
                if isinstance(target_scope, (WorkspaceScope, SessionScope, RunScope))
                else self._dataset_permission_epoch(ref.dataset_id)
            )
            if permission_epoch is not None and permission_epoch != current_epoch:
                return VisibilityDecision(
                    status="permission_epoch_mismatch",
                    allowed=False,
                    reason_code="target_epoch_stale",
                    source_ref=ref,
                    permission_epoch=current_epoch,
                    checked_at=observed,
                )
            if not self._version_sources_current(version, current_epoch):
                return VisibilityDecision(
                    status="permission_epoch_mismatch",
                    allowed=False,
                    reason_code="source_epoch_stale",
                    source_ref=ref,
                    permission_epoch=current_epoch,
                    checked_at=observed,
                )
            try:
                self._validate_target_registration(target_scope)
            except SharingPermissionError:
                return VisibilityDecision(
                    status="registration_missing",
                    allowed=False,
                    reason_code="registration_missing",
                    source_ref=ref,
                    checked_at=observed,
                )
            return VisibilityDecision(
                status="allowed",
                allowed=True,
                reason_code="same_scope",
                source_ref=ref,
                permission_epoch=permission_epoch,
                checked_at=observed,
            )
        with self.store._connect() as connection:
            self._require_schema(connection)
            rows = connection.execute(
                "SELECT * FROM b26_sharing_grants WHERE source_dataset_id=? "
                "AND (grant_id=? OR ? IS NULL) ORDER BY revision DESC,grant_id",
                (ref.dataset_id, grant_id, grant_id),
            ).fetchall()
        saw_expired = False
        saw_revoked = False
        saw_epoch = False
        saw_scope = False
        for row in rows:
            grant = self._grant(row)
            if grant.source_scope != version.scope:
                continue
            if ref not in grant.memory_refs:
                continue
            if grant.subject_id != subject_id:
                continue
            if grant.permission_epoch != self._project_permission_epoch(grant.project_id):
                saw_epoch = True
                continue
            if purpose != grant.purpose and not (
                grant.purpose == "share" and purpose in {"recall", "source_read"}
            ):
                continue
            if grant.state == "revoked":
                saw_revoked = True
                continue
            if grant.state == "expired" or grant.expires_at <= observed:
                saw_expired = True
                continue
            if permission_epoch is not None and grant.permission_epoch != permission_epoch:
                saw_epoch = True
                continue
            if not _scope_target_matches(grant.target_scope, target_scope):
                saw_scope = True
                continue
            try:
                self._validate_target_registration(grant.target_scope)
            except SharingPermissionError:
                return VisibilityDecision(
                    status="registration_missing",
                    allowed=False,
                    reason_code="registration_missing",
                    source_ref=ref,
                    grant_id=grant.grant_id,
                    permission_epoch=grant.permission_epoch,
                    checked_at=observed,
                )
            return VisibilityDecision(
                status="allowed",
                allowed=True,
                reason_code="explicit_grant",
                source_ref=ref,
                grant_id=grant.grant_id,
                permission_epoch=grant.permission_epoch,
                checked_at=observed,
            )
        status: Literal[
            "denied",
            "expired",
            "revoked",
            "scope_mismatch",
            "permission_epoch_mismatch",
            "registration_missing",
            "not_selected",
        ]
        reason = "permission_denied"
        if saw_revoked:
            status, reason = "revoked", "grant_revoked"
        elif saw_expired:
            status, reason = "expired", "grant_expired"
        elif saw_epoch:
            status, reason = "permission_epoch_mismatch", "grant_epoch_stale"
        elif saw_scope:
            status, reason = "scope_mismatch", "scope_not_narrower"
        else:
            status, reason = "denied", "grant_missing"
        return VisibilityDecision(
            status=status,
            allowed=False,
            reason_code=reason,
            source_ref=ref,
            checked_at=observed,
        )

    def is_visible(self, ref: MemoryVersionRef, **kwargs: Any) -> bool:
        return self.authorize(ref, **kwargs).allowed

    authorize_memory = authorize
    check_visibility = authorize

    def attach_consumer(
        self,
        *,
        project_id: str,
        dataset_id: str,
        installation_id: str,
        grant_id: str,
        purpose: Literal["primary_engine", "shared_read", "export", "migration"],
        consumer_id: str | None = None,
    ) -> SharingConsumer:
        grant = self._grant_by_id(grant_id)
        if grant.source_dataset_id != dataset_id:
            raise SharingPermissionError("consumer dataset does not match grant")
        if grant.state != "active" or grant.expires_at <= _now():
            raise SharingPermissionError("consumer grant is no longer active")
        if purpose == "shared_read" and grant.purpose not in {"share", "recall", "source_read"}:
            raise SharingPermissionError("grant purpose cannot create a shared reader")
        self._validate_target_registration(grant.target_scope)
        if (
            hasattr(grant.target_scope, "project_id")
            and grant.target_scope.project_id != project_id
        ):
            raise SharingPermissionError("consumer project does not match the grant target")
        requested = SharingConsumer(
            consumer_id=consumer_id or f"consumer_{dataset_id}_{installation_id}",
            project_id=project_id,
            dataset_id=dataset_id,
            installation_id=installation_id,
            grant_id=grant_id,
            purpose=purpose,
        )
        with self.store._connect() as connection:
            self._require_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT * FROM b26_dataset_consumers
                   WHERE dataset_id=? AND installation_id=? AND purpose=?""",
                (dataset_id, installation_id, purpose),
            ).fetchone()
            if row is not None:
                current = self._consumer(row)
                if current.state == "active" and current.grant_id == grant_id:
                    return current
                now = _now()
                updated = requested.model_copy(
                    update={
                        "consumer_id": current.consumer_id,
                        "revision": current.revision + 1,
                        "created_at": current.created_at,
                        "updated_at": now,
                    }
                )
                connection.execute(
                    """UPDATE b26_dataset_consumers SET project_id=?,grant_id=?,state=?,
                       revision=?,revoked_at=?,reason=?,updated_at=?
                       WHERE consumer_id=? AND revision=?""",
                    (
                        updated.project_id,
                        updated.grant_id,
                        "active",
                        updated.revision,
                        None,
                        None,
                        updated.updated_at.isoformat(),
                        current.consumer_id,
                        current.revision,
                    ),
                )
                return updated
            connection.execute(
                """INSERT INTO b26_dataset_consumers(
                   consumer_id,project_id,dataset_id,installation_id,grant_id,purpose,
                   state,revision,revoked_at,reason,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    requested.consumer_id,
                    requested.project_id,
                    requested.dataset_id,
                    requested.installation_id,
                    requested.grant_id,
                    requested.purpose,
                    requested.state,
                    requested.revision,
                    None,
                    None,
                    requested.created_at.isoformat(),
                    requested.updated_at.isoformat(),
                ),
            )
            result = requested
        if self.registry is not None:
            self.registry.attach_dataset_consumer(dataset_id, installation_id)
        return result

    def revoke_consumer(
        self,
        consumer_id: str,
        *,
        expected_revision: int,
        reason: str,
    ) -> SharingConsumer:
        if not reason.strip():
            raise SharingValidationError("consumer revocation reason is required")
        with self.store._connect() as connection:
            self._require_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM b26_dataset_consumers WHERE consumer_id=?", (consumer_id,)
            ).fetchone()
            if row is None:
                raise SharingNotFoundError("dataset consumer not found")
            current = self._consumer(row)
            if current.state != "active":
                return current
            if current.revision != expected_revision:
                raise SharingConflictError("dataset consumer revision conflict")
            now = _now()
            updated = current.model_copy(
                update={
                    "revision": current.revision + 1,
                    "state": "revoked",
                    "revoked_at": now,
                    "reason": reason.strip(),
                    "updated_at": now,
                }
            )
            assert updated.revoked_at is not None
            connection.execute(
                """UPDATE b26_dataset_consumers SET state=?,revision=?,revoked_at=?,
                   reason=?,updated_at=? WHERE consumer_id=? AND revision=?""",
                (
                    updated.state,
                    updated.revision,
                    updated.revoked_at.isoformat(),
                    updated.reason,
                    updated.updated_at.isoformat(),
                    consumer_id,
                    expected_revision,
                ),
            )
            result = updated
        if self.registry is not None:
            self.registry.revoke_dataset_consumer(current.dataset_id, current.installation_id)
        return result

    def dataset_consumers(self, dataset_id: str) -> tuple[SharingConsumer, ...]:
        with self.store._connect() as connection:
            self._require_schema(connection)
            rows = connection.execute(
                "SELECT * FROM b26_dataset_consumers WHERE dataset_id=? "
                "ORDER BY created_at,consumer_id",
                (dataset_id,),
            ).fetchall()
        return tuple(self._consumer(row) for row in rows)

    def cleanup_blockers(self, dataset_id: str, installation_id: str) -> tuple[CleanupBlocker, ...]:
        blockers: list[CleanupBlocker] = []
        for consumer in self.dataset_consumers(dataset_id):
            if consumer.state == "active" and consumer.installation_id != installation_id:
                blockers.append(
                    CleanupBlocker(
                        dataset_id=dataset_id,
                        installation_id=installation_id,
                        blocker_id=consumer.consumer_id,
                        reason="shared_consumer",
                    )
                )
        with self.store._connect() as connection:
            self._require_schema(connection)
            grants = connection.execute(
                "SELECT * FROM b26_sharing_grants WHERE source_dataset_id=? AND state='active'",
                (dataset_id,),
            ).fetchall()
            transfers = connection.execute(
                """SELECT * FROM b26_dataset_transfers
                   WHERE state IN ('pending','validated')
                   AND json_extract(request_json,'$.dataset_id')=?""",
                (dataset_id,),
            ).fetchall()
        for row in grants:
            grant = self._grant(row)
            blockers.append(
                CleanupBlocker(
                    dataset_id=dataset_id,
                    installation_id=installation_id,
                    blocker_id=grant.grant_id,
                    reason="active_grant",
                )
            )
        for row in transfers:
            transfer = self._transfer(row)
            blockers.append(
                CleanupBlocker(
                    dataset_id=dataset_id,
                    installation_id=installation_id,
                    blocker_id=transfer.transfer_id,
                    reason="pending_transfer",
                )
            )
        return tuple(blockers)

    def begin_transfer(
        self,
        request: DatasetTransfer,
        *,
        project_id: str,
        source_installation_id: str | None = None,
        source_consumer_id: str | None = None,
        transfer_id: str | None = None,
    ) -> DatasetTransferState:
        grant = self._grant_by_id(request.authorization_grant_id)
        if grant.project_id != project_id:
            raise SharingPermissionError("transfer must be initiated by the owning project")
        if grant.purpose != "transfer" or grant.state != "active" or grant.expires_at <= _now():
            raise SharingPermissionError("dataset transfer authorization is not active")
        if grant.source_dataset_id != request.dataset_id:
            raise SharingPermissionError("transfer grant does not own the source dataset")
        dataset = self._registry_dataset(request.dataset_id)
        if dataset.revision != request.expected_revision:
            raise SharingConflictError("dataset transfer revision is stale")
        if source_installation_id is None:
            source_installation_id = dataset.installation_id
        if source_installation_id is None:
            raise SharingPermissionError("source dataset has no owning installation")
        if dataset.installation_id != source_installation_id:
            raise SharingPermissionError("source installation does not own the dataset")
        self._validate_target_registration(grant.target_scope)
        destination_consumer_id = (
            f"consumer_{request.destination_dataset_id}_{request.destination_installation_id}"
        )
        value = DatasetTransferState(
            transfer_id=transfer_id or f"transfer_{uuid4().hex}",
            project_id=project_id,
            request=request,
            source_installation_id=source_installation_id,
            source_consumer_id=source_consumer_id,
            destination_consumer_id=destination_consumer_id,
        )
        with self.store._connect() as connection:
            self._require_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM b26_dataset_transfers WHERE transfer_id=?", (value.transfer_id,)
            ).fetchone()
            if row is not None:
                current = self._transfer(row)
                if current.request == request:
                    return current
                raise SharingConflictError("dataset transfer identity already exists")
            connection.execute(
                """INSERT INTO b26_dataset_transfers(
                   transfer_id,project_id,request_json,source_installation_id,source_consumer_id,
                   destination_consumer_id,state,revision,evidence_refs_json,reason,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    value.transfer_id,
                    value.project_id,
                    _json(request),
                    value.source_installation_id,
                    value.source_consumer_id,
                    value.destination_consumer_id,
                    value.state,
                    value.revision,
                    _json(value.evidence_refs),
                    value.reason,
                    value.created_at.isoformat(),
                    value.updated_at.isoformat(),
                ),
            )
            return value

    def _transfer_by_id(self, transfer_id: str) -> DatasetTransferState:
        with self.store._connect() as connection:
            self._require_schema(connection)
            row = connection.execute(
                "SELECT * FROM b26_dataset_transfers WHERE transfer_id=?", (transfer_id,)
            ).fetchone()
        if row is None:
            raise SharingNotFoundError("dataset transfer not found")
        return self._transfer(row)

    def validate_transfer(
        self,
        transfer_id: str,
        *,
        evidence_refs: Sequence[str] = (),
        expected_revision: int,
    ) -> DatasetTransferState:
        current = self._transfer_by_id(transfer_id)
        if current.state == "validated":
            return current
        if current.state != "pending" or current.revision != expected_revision:
            raise SharingConflictError("dataset transfer is not pending at the expected revision")
        grant = self._grant_by_id(current.request.authorization_grant_id)
        if grant.state != "active" or grant.expires_at <= _now():
            raise SharingPermissionError("dataset transfer grant is no longer active")
        if self.registry is None:
            raise SharingPermissionError("dataset transfer requires the Core registry")
        source_dataset = self._registry_dataset(current.request.dataset_id)
        destination_installation = self.registry.get_installation(
            current.request.destination_installation_id
        )
        if source_dataset.revision != current.request.expected_revision:
            raise SharingConflictError("source dataset changed during transfer validation")
        if source_dataset.installation_id != current.source_installation_id:
            raise SharingPermissionError("source dataset owner changed during transfer validation")
        if destination_installation.state not in {
            "disabled",
            "failed",
            "blocked",
        }:
            raise SharingConflictError("destination installation is active and cannot receive data")
        if destination_installation.binding_id is not None:
            destination_binding = self.registry.get_binding(destination_installation.binding_id)
            if destination_binding.enabled:
                raise SharingConflictError("destination binding is active and cannot receive data")
        if current.request.mode == "transfer":
            external_consumers = tuple(
                consumer
                for consumer in self.dataset_consumers(current.request.dataset_id)
                if consumer.state == "active"
                and consumer.installation_id != current.source_installation_id
            )
            if external_consumers:
                raise SharingConflictError("active shared consumers block dataset transfer")
        if current.source_installation_id is not None:
            source_installation = self.registry.get_installation(current.source_installation_id)
            if source_installation.binding_id:
                binding = self.registry.get_binding(source_installation.binding_id)
                if binding.active_run_ids:
                    raise SharingConflictError("active source Runs block dataset transfer")
        if destination_installation.binding_id:
            binding = self.registry.get_binding(destination_installation.binding_id)
            if binding.active_run_ids:
                raise SharingConflictError("active destination Runs block dataset transfer")
        source_digest = _digest(
            {
                "dataset": source_dataset,
                "source_installation_id": current.source_installation_id,
                "source_resources": tuple(
                    resource.resource.resource_id
                    for resource in self.registry.resources_for(current.source_installation_id)
                ),
            }
        )
        destination_digest = _digest(
            {
                "destination_installation_id": destination_installation.installation_id,
                "destination_dataset_id": current.request.destination_dataset_id,
                "target_scope": grant.target_scope,
            }
        )
        generated_refs = (
            f"transfer-source:{source_digest}",
            f"transfer-destination:{destination_digest}",
        )
        refs = tuple(dict.fromkeys((*generated_refs, *(item for item in evidence_refs if item))))
        now = _now()
        updated = current.model_copy(
            update={
                "state": "validated",
                "revision": current.revision + 1,
                "evidence_refs": refs,
                "updated_at": now,
                "reason": None,
            }
        )
        with self.store._connect() as connection:
            self._require_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            result = connection.execute(
                """UPDATE b26_dataset_transfers SET state=?,revision=?,evidence_refs_json=?,
                   reason=?,updated_at=? WHERE transfer_id=? AND revision=? AND state='pending'""",
                (
                    updated.state,
                    updated.revision,
                    _json(updated.evidence_refs),
                    None,
                    updated.updated_at.isoformat(),
                    transfer_id,
                    expected_revision,
                ),
            )
            if result.rowcount != 1:
                raise SharingConflictError("dataset transfer revision conflict")
        return updated

    def commit_transfer(
        self,
        transfer_id: str,
        *,
        expected_revision: int,
    ) -> DatasetTransferState:
        current = self._transfer_by_id(transfer_id)
        if current.state == "committed":
            return current
        if current.state != "validated" or current.revision != expected_revision:
            raise SharingConflictError("dataset transfer requires validated state")
        grant = self._grant_by_id(current.request.authorization_grant_id)
        if grant.state != "active" or grant.expires_at <= _now():
            raise SharingPermissionError("dataset transfer grant is no longer active")
        if self.registry is None or self.ledger is None:
            raise SharingPermissionError("dataset transfer requires Core registry and ledger")
        destination_installation = self.registry.get_installation(
            current.request.destination_installation_id
        )
        if current.request.mode == "authorized_copy":
            self.ledger.copy_dataset(
                current.request.dataset_id,
                current.request.destination_dataset_id,
                owner=destination_installation.owner,
                target_scope=grant.target_scope,
                idempotency_key=current.transfer_id,
            )
        self.registry.transfer_dataset(
            current.request.dataset_id,
            destination_dataset_id=current.request.destination_dataset_id,
            destination_installation_id=current.request.destination_installation_id,
            expected_revision=current.request.expected_revision,
            mode=current.request.mode,
        )
        now = _now()
        destination_dataset = current.request.destination_dataset_id
        destination_project_id = (
            grant.target_scope.project_id
            if hasattr(grant.target_scope, "project_id")
            else current.project_id
        )
        destination = SharingConsumer(
            consumer_id=current.destination_consumer_id,
            project_id=destination_project_id,
            dataset_id=destination_dataset,
            installation_id=current.request.destination_installation_id,
            grant_id=current.request.authorization_grant_id,
            purpose="migration",
        )
        updated = current.model_copy(
            update={"state": "committed", "revision": current.revision + 1, "updated_at": now}
        )
        with self.store._connect() as connection:
            self._require_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            if current.source_consumer_id and current.request.mode == "transfer":
                connection.execute(
                    """UPDATE b26_dataset_consumers SET state='transferred',revision=revision+1,
                       revoked_at=?,reason=?,updated_at=? WHERE consumer_id=? AND state='active'""",
                    (
                        now.isoformat(),
                        "dataset transfer committed",
                        now.isoformat(),
                        current.source_consumer_id,
                    ),
                )
            existing = connection.execute(
                "SELECT * FROM b26_dataset_consumers WHERE consumer_id=?",
                (destination.consumer_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO b26_dataset_consumers(
                       consumer_id,project_id,dataset_id,installation_id,grant_id,purpose,
                       state,revision,revoked_at,reason,created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        destination.consumer_id,
                        destination.project_id,
                        destination.dataset_id,
                        destination.installation_id,
                        destination.grant_id,
                        destination.purpose,
                        destination.state,
                        destination.revision,
                        None,
                        None,
                        destination.created_at.isoformat(),
                        destination.updated_at.isoformat(),
                    ),
                )
            else:
                consumer = self._consumer(existing)
                if consumer.dataset_id != destination.dataset_id:
                    raise SharingConflictError("destination consumer belongs to another dataset")
                connection.execute(
                    """UPDATE b26_dataset_consumers SET state='active',grant_id=?,
                       revision=revision+1,
                       revoked_at=NULL,reason=NULL,updated_at=? WHERE consumer_id=?""",
                    (destination.grant_id, now.isoformat(), destination.consumer_id),
                )
            result = connection.execute(
                """UPDATE b26_dataset_transfers SET state=?,revision=?,updated_at=?
                   WHERE transfer_id=? AND revision=? AND state='validated'""",
                (
                    updated.state,
                    updated.revision,
                    updated.updated_at.isoformat(),
                    transfer_id,
                    expected_revision,
                ),
            )
            if result.rowcount != 1:
                raise SharingConflictError("dataset transfer revision conflict")
        return updated

    def revoke_transfer(
        self,
        transfer_id: str,
        *,
        expected_revision: int,
        reason: str,
    ) -> DatasetTransferState:
        if not reason.strip():
            raise SharingValidationError("transfer revocation reason is required")
        current = self._transfer_by_id(transfer_id)
        if current.state == "revoked":
            return current
        if (
            current.state not in {"pending", "validated", "blocked"}
            or current.revision != expected_revision
        ):
            raise SharingConflictError("dataset transfer cannot be revoked in its current state")
        updated = current.model_copy(
            update={
                "state": "revoked",
                "revision": current.revision + 1,
                "reason": reason.strip(),
                "updated_at": _now(),
            }
        )
        with self.store._connect() as connection:
            self._require_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            result = connection.execute(
                """UPDATE b26_dataset_transfers SET state=?,revision=?,reason=?,updated_at=?
                   WHERE transfer_id=? AND revision=?""",
                (
                    updated.state,
                    updated.revision,
                    updated.reason,
                    updated.updated_at.isoformat(),
                    transfer_id,
                    expected_revision,
                ),
            )
            if result.rowcount != 1:
                raise SharingConflictError("dataset transfer revision conflict")
        return updated

    def list_transfers(self, project_id: str) -> tuple[DatasetTransferState, ...]:
        with self.store._connect() as connection:
            self._require_schema(connection)
            rows = connection.execute(
                "SELECT * FROM b26_dataset_transfers WHERE project_id=? "
                "ORDER BY created_at,transfer_id",
                (project_id,),
            ).fetchall()
        return tuple(self._transfer(row) for row in rows)

    def create_personal_preference(
        self,
        *,
        project_id: str,
        content: str,
        source_refs: Sequence[Any],
        record_id: str | None = None,
    ) -> MemoryVersion:
        """Create and publish one user opted-in Personal preference.

        The source list is exact and is checked against the current local
        principal, project and permission epoch.  The resulting Personal
        scope is never implicitly visible to another project; a later
        ``grant_create`` command must name the exact version reference.
        """

        if self.ledger is None or self.registry is None:
            raise SharingPermissionError("personal preference requires Core ledger and registry")
        ledger = self.ledger
        assert ledger is not None
        if not content.strip():
            raise SharingValidationError("personal preference content is blank")
        try:
            normalized_sources = tuple(
                item if isinstance(item, SourceRef) else SourceRef.model_validate(item)
                for item in source_refs
            )
        except Exception as exc:
            raise SharingValidationError("personal preference source reference is invalid") from exc
        if not normalized_sources:
            raise SharingValidationError("personal preference requires exact source references")
        actor = self._trusted_principal(project_id, None)
        project = self._project(project_id)
        if project is None or not project.get("installation_id"):
            raise SharingPermissionError("project has no bound memory installation")
        installation = self.registry.get_installation(project["installation_id"])
        dataset_id = str(installation.dataset_id)
        effective_epoch = int(installation.permission_epoch)
        sensitivity_rank = {"public": 0, "internal": 1, "sensitive": 2}
        effective_sensitivity: Literal["public", "internal", "sensitive"] = "internal"
        role_sets: list[set[str]] = []
        agent_sets: list[set[str]] = []
        valid_untils: list[datetime] = []
        visited: set[tuple[str, str, int]] = set()

        def inspect(items: Sequence[SourceRef]) -> None:
            nonlocal effective_sensitivity
            for source in items:
                if source.availability != "available" or source.permission_epoch != effective_epoch:
                    raise SharingPermissionError(
                        "personal preference source is unavailable or stale"
                    )
                if isinstance(source.scope, PersonalScope):
                    if source.scope.principal_id != actor:
                        raise SharingPermissionError(
                            "personal preference source belongs to another principal"
                        )
                elif hasattr(source.scope, "project_id") and source.scope.project_id != project_id:
                    raise SharingPermissionError(
                        "personal preference source belongs to another project"
                    )
                if source.source_type != "memory_version":
                    self._core_authorize_writer_source(
                        source,
                        project_id=project_id,
                        dataset_id=dataset_id,
                        permission_epoch=effective_epoch,
                    )
                    continue
                key = (dataset_id, source.source_id, source.revision)
                if key in visited:
                    continue
                visited.add(key)
                try:
                    nested = ledger.get_version(dataset_id, source.source_id, source.revision)
                except Exception as exc:
                    raise SharingPermissionError(
                        "personal preference memory source is unavailable"
                    ) from exc
                if nested.ref.content_digest != source.content_digest:
                    raise SharingPermissionError("personal preference source digest does not match")
                if nested.role_ids:
                    role_sets.append(set(nested.role_ids))
                if nested.agent_ids:
                    agent_sets.append(set(nested.agent_ids))
                effective_sensitivity = max(
                    effective_sensitivity,
                    nested.sensitivity,
                    key=lambda item: sensitivity_rank[item],
                )
                if nested.conditions.valid_until is not None:
                    valid_untils.append(nested.conditions.valid_until)
                inspect(nested.sources)

        inspect(normalized_sources)
        role_ids = tuple(sorted(set.intersection(*role_sets))) if role_sets else ()
        agent_ids = tuple(sorted(set.intersection(*agent_sets))) if agent_sets else ()
        valid_until = min(valid_untils) if valid_untils else None
        if record_id is None:
            record_id = f"personal_preference_{uuid4().hex}"
            version_number = 1
            expected_head_revision = 0
        else:
            try:
                existing_versions = ledger.list_versions(dataset_id, record_id)
                current_head = ledger.get_head(dataset_id, record_id)
            except Exception:
                existing_versions = []
                current_head = None
            version_number = max((item.ref.version for item in existing_versions), default=0) + 1
            expected_head_revision = 0 if current_head is None else current_head.revision
        now = _now()
        version = MemoryVersion(
            ref=MemoryVersionRef(
                dataset_id=dataset_id,
                record_id=record_id,
                version=version_number,
                content_digest=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            ),
            owner=installation.owner,
            kind="episodic",
            content_type="preference",
            scope=PersonalScope(
                kind="personal",
                principal_id=actor,
                opt_in_grant_id=f"personal_optin_{uuid4().hex}",
            ),
            role_ids=role_ids,
            agent_ids=agent_ids,
            content=content,
            sources=normalized_sources,
            evidence="user_asserted",
            sensitivity=effective_sensitivity,
            retention_policy_id="retention_personal_preference",
            conditions=MemoryConditions(
                commit_ref=None,
                tree_digest=None,
                file_fingerprints={},
                environment_digest=None,
                tool_versions={},
                verified_at=None,
                valid_from=now,
                valid_until=valid_until,
            ),
            recorded_at=now,
        )
        saved = ledger.save_version(
            version,
            dataset_id=dataset_id,
            expected_head_revision=expected_head_revision,
            permission_epoch=effective_epoch,
        )
        proposal = ledger.propose(
            saved,
            operation="create" if version_number == 1 else "modify",
            expected_head_revision=expected_head_revision,
            permission_epoch=effective_epoch,
            reason="explicit personal preference opt-in",
        )
        ledger.confirm_proposal(
            proposal,
            dataset_id=dataset_id,
            expected_head_revision=expected_head_revision,
            permission_epoch=effective_epoch,
        )
        return saved

    def bind_writer_memory(
        self,
        memory_ref: MemoryVersionRef,
        *,
        project_id: str,
        worktree_id: str,
        writer_workspace_id: str,
        run_id: str,
        graph_run_id: str | None = None,
        evidence_id: str | None = None,
        permission_epoch: int = 0,
    ) -> WriterMemoryEvidence:
        registration = self._registration_by_worktree(project_id, worktree_id)
        if registration.state != "active":
            raise SharingPermissionError("writer worktree registration is revoked")
        version = self._ledger_version(memory_ref)
        if version is None:
            raise SharingPermissionError("writer memory candidate is unavailable")
        if not isinstance(version.scope, RunScope):
            raise SharingValidationError("Writer memory must use RunScope")
        if (
            version.scope.project_id != project_id
            or version.scope.workspace_id != registration.workspace_id
            or version.scope.run_id != run_id
            or (
                version.scope.writer_id is not None
                and version.scope.writer_id != writer_workspace_id
            )
        ):
            raise SharingPermissionError("writer memory RunScope does not match its registration")
        self._validate_writer_source_constraints(version)
        if self.ledger is not None:
            head = self.ledger.get_head(memory_ref.dataset_id, memory_ref.record_id)
            if head.state == "published" and head.published_version == memory_ref:
                raise SharingConflictError(
                    "published project memory cannot be rebound as Writer evidence"
                )
        try:
            workspace = self.writer_repository.get_workspace(writer_workspace_id)
        except Exception as exc:
            raise SharingNotFoundError("Writer workspace is not registered") from exc
        if graph_run_id is not None and workspace.graph_run_id != graph_run_id:
            raise SharingPermissionError("Writer workspace belongs to another graph Run")
        if workspace.base_revision != _commit_ref(workspace.base_revision):
            raise SharingValidationError("Writer base revision is invalid")
        if workspace.isolation_ref == registration.workspace_ref:
            raise SharingPermissionError("Writer isolation cannot be the project workspace")
        evidence = WriterMemoryEvidence(
            evidence_id=evidence_id or f"writer_evidence_{uuid4().hex}",
            memory_ref=memory_ref,
            candidate_head_revision=(
                self.ledger.get_head(memory_ref.dataset_id, memory_ref.record_id).revision
                if self.ledger is not None
                else 0
            ),
            project_id=project_id,
            workspace_id=registration.workspace_id,
            worktree_id=worktree_id,
            writer_workspace_id=writer_workspace_id,
            graph_run_id=workspace.graph_run_id,
            run_id=run_id,
            branch_ref=registration.branch_ref,
            base_revision=workspace.base_revision,
            base_tree_digest=registration.tree_digest,
            permission_epoch=permission_epoch,
        )
        with self.store._connect() as connection:
            self._require_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM b26_writer_memory_evidence WHERE evidence_id=?",
                (evidence.evidence_id,),
            ).fetchone()
            if row is not None:
                current = self._evidence(row)
                if (
                    current.memory_ref == evidence.memory_ref
                    and current.writer_workspace_id == evidence.writer_workspace_id
                ):
                    return current
                raise SharingConflictError("Writer evidence identity already exists")
            connection.execute(
                """INSERT INTO b26_writer_memory_evidence(
                   evidence_id,memory_ref_json,dataset_id,candidate_head_revision,
                   project_id,workspace_id,worktree_id,
                   writer_workspace_id,graph_run_id,run_id,branch_ref,base_revision,base_tree_digest,
                   merge_run_id,target_isolation_ref,target_commit_ref,target_tree_digest,
                   verification_artifact_refs_json,verification_digest,verification_status,state,
                   published_memory_ref_json,permission_epoch,revision,reason,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    evidence.evidence_id,
                    _json(memory_ref),
                    evidence.memory_ref.dataset_id,
                    evidence.candidate_head_revision,
                    evidence.project_id,
                    evidence.workspace_id,
                    evidence.worktree_id,
                    evidence.writer_workspace_id,
                    evidence.graph_run_id,
                    evidence.run_id,
                    evidence.branch_ref,
                    evidence.base_revision,
                    evidence.base_tree_digest,
                    None,
                    None,
                    None,
                    None,
                    _json(evidence.verification_artifact_refs),
                    None,
                    evidence.verification_status,
                    evidence.state,
                    None,
                    evidence.permission_epoch,
                    evidence.revision,
                    evidence.reason,
                    evidence.created_at.isoformat(),
                    evidence.updated_at.isoformat(),
                ),
            )
            return evidence

    def _validate_writer_source_constraints(self, version: MemoryVersion) -> None:
        """Recheck provenance scope, epochs and restrictions at each gate."""

        if self.ledger is None:
            raise SharingPermissionError("Writer source validation requires the Core ledger")
        ledger = self.ledger
        if not isinstance(version.scope, (WorkspaceScope, SessionScope, RunScope)):
            raise SharingValidationError("Writer source must carry a project/workspace scope")
        version_scope = version.scope
        current_epoch = self._project_permission_epoch(version_scope.project_id)
        sensitivity_rank = {"public": 0, "internal": 1, "sensitive": 2}
        visited: set[tuple[str, str, int]] = set()

        def visit(candidate: MemoryVersion) -> None:
            for source in candidate.sources:
                if source.availability != "available" or source.permission_epoch != current_epoch:
                    raise SharingPermissionError("Writer source is unavailable or stale")
                if not scope_permits(source.scope, version.scope):
                    raise SharingPermissionError(
                        "Writer source scope is broader than the candidate"
                    )
                if source.source_type != "memory_version":
                    self._core_authorize_writer_source(
                        source,
                        project_id=version_scope.project_id,
                        dataset_id=version.ref.dataset_id,
                        permission_epoch=current_epoch,
                    )
                    continue
                key = (version.ref.dataset_id, source.source_id, source.revision)
                if key in visited:
                    continue
                visited.add(key)
                try:
                    nested = ledger.get_version(
                        version.ref.dataset_id, source.source_id, source.revision
                    )
                except Exception as exc:
                    raise SharingPermissionError("Writer source memory is unavailable") from exc
                if nested.ref.content_digest != source.content_digest:
                    raise SharingPermissionError("Writer source digest does not match")
                if nested.role_ids and (
                    not candidate.role_ids or not set(candidate.role_ids).issubset(nested.role_ids)
                ):
                    raise SharingPermissionError("Writer role restriction was broadened")
                if nested.agent_ids and (
                    not candidate.agent_ids
                    or not set(candidate.agent_ids).issubset(nested.agent_ids)
                ):
                    raise SharingPermissionError("Writer agent restriction was broadened")
                if nested.conditions.valid_until is not None and (
                    candidate.conditions.valid_until is None
                    or candidate.conditions.valid_until > nested.conditions.valid_until
                ):
                    raise SharingPermissionError("Writer source expiry was broadened")
                if sensitivity_rank[candidate.sensitivity] < sensitivity_rank[nested.sensitivity]:
                    raise SharingPermissionError("Writer source sensitivity was weakened")
                visit(nested)

        visit(version)

    def propose_writer_memory(
        self,
        *,
        project_id: str,
        worktree_id: str,
        writer_workspace_id: str,
        run_id: str,
        content: str,
        source_refs: Sequence[Any],
        record_id: str | None = None,
        content_type: Literal["fact", "preference", "episode", "procedure"] = "fact",
        sensitivity: Literal["public", "internal", "sensitive"] = "internal",
        permission_epoch: int | None = None,
    ) -> WriterMemoryEvidence:
        """Create and bind a Run-scoped Writer candidate from exact sources."""

        if self.ledger is None or self.registry is None:
            raise SharingPermissionError(
                "Writer candidate creation requires Core ledger and registry"
            )
        ledger = self.ledger
        assert ledger is not None
        if not content.strip():
            raise SharingValidationError("Writer candidate content is blank")
        try:
            normalized_sources = tuple(
                item if isinstance(item, SourceRef) else SourceRef.model_validate(item)
                for item in source_refs
            )
        except Exception as exc:
            raise SharingValidationError("Writer candidate source reference is invalid") from exc
        if not normalized_sources:
            raise SharingValidationError("Writer candidate requires exact source references")
        registration = self._registration_by_worktree(project_id, worktree_id)
        if registration.state != "active":
            raise SharingPermissionError("Writer worktree registration is revoked")
        workspace = self.writer_repository.get_workspace(writer_workspace_id)
        project = self._project(project_id)
        if project is None or not project.get("installation_id"):
            raise SharingPermissionError("project has no bound memory installation")
        installation = self.registry.get_installation(project["installation_id"])
        dataset_id = str(installation.dataset_id)
        effective_epoch = (
            installation.permission_epoch if permission_epoch is None else permission_epoch
        )
        if effective_epoch != installation.permission_epoch:
            raise SharingConflictError("Writer candidate permission epoch is stale")
        candidate_scope = RunScope(
            kind="run",
            project_id=project_id,
            workspace_id=registration.workspace_id,
            run_id=run_id,
            writer_id=writer_workspace_id,
        )
        role_sets: list[set[str]] = []
        agent_sets: list[set[str]] = []
        sensitivity_rank = {"public": 0, "internal": 1, "sensitive": 2}
        effective_sensitivity: Literal["public", "internal", "sensitive"] = "internal"
        valid_untils: list[datetime] = []
        visited_memory_refs: set[tuple[str, str, int]] = set()

        def inspect_sources(items: Sequence[SourceRef]) -> None:
            nonlocal effective_sensitivity
            for source in items:
                if source.availability != "available":
                    raise SharingPermissionError("Writer candidate source is unavailable")
                if source.permission_epoch != effective_epoch:
                    raise SharingPermissionError(
                        "Writer candidate source permission epoch is stale"
                    )
                if not scope_permits(source.scope, candidate_scope):
                    raise SharingPermissionError("Writer candidate would broaden source scope")
                if source.source_type != "memory_version":
                    self._core_authorize_writer_source(
                        source,
                        project_id=project_id,
                        dataset_id=dataset_id,
                        permission_epoch=effective_epoch,
                    )
                    continue
                key = (dataset_id, source.source_id, source.revision)
                if key in visited_memory_refs:
                    continue
                visited_memory_refs.add(key)
                try:
                    source_version = ledger.get_version(
                        dataset_id, source.source_id, source.revision
                    )
                except Exception as exc:
                    raise SharingPermissionError(
                        "Writer candidate memory source is unavailable"
                    ) from exc
                if source_version.ref.content_digest != source.content_digest:
                    raise SharingPermissionError("Writer candidate source digest does not match")
                if source_version.role_ids:
                    role_sets.append(set(source_version.role_ids))
                if source_version.agent_ids:
                    agent_sets.append(set(source_version.agent_ids))
                effective_sensitivity = max(
                    effective_sensitivity,
                    source_version.sensitivity,
                    key=lambda item: sensitivity_rank[item],
                )
                if source_version.conditions.valid_until is not None:
                    valid_untils.append(source_version.conditions.valid_until)
                inspect_sources(source_version.sources)

        inspect_sources(normalized_sources)
        role_ids = tuple(sorted(set.intersection(*role_sets))) if role_sets else ()
        agent_ids = tuple(sorted(set.intersection(*agent_sets))) if agent_sets else ()
        valid_until = min(valid_untils) if valid_untils else None
        if record_id is None:
            record_id = f"writer_memory_{uuid4().hex}"
            version_number = 1
            expected_head_revision = 0
        else:
            try:
                versions = self.ledger.list_versions(dataset_id, record_id)
                current_head = self.ledger.get_head(dataset_id, record_id)
            except Exception:
                versions = []
                current_head = None
            version_number = max((item.ref.version for item in versions), default=0) + 1
            expected_head_revision = 0 if current_head is None else current_head.revision
        content_digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        now = _now()
        version = MemoryVersion(
            ref=MemoryVersionRef(
                dataset_id=dataset_id,
                record_id=record_id,
                version=version_number,
                content_digest=content_digest,
            ),
            owner=installation.owner,
            kind="project",
            content_type=content_type,
            scope=candidate_scope,
            role_ids=role_ids,
            agent_ids=agent_ids,
            content=content,
            sources=normalized_sources,
            evidence="inferred",
            sensitivity=effective_sensitivity,
            retention_policy_id="retention_writer_default",
            conditions=MemoryConditions(
                commit_ref=None,
                tree_digest=None,
                file_fingerprints={},
                environment_digest=None,
                tool_versions={},
                verified_at=None,
                valid_from=now,
                valid_until=valid_until,
            ),
            recorded_at=now,
        )
        saved = self.ledger.save_version(
            version,
            dataset_id=dataset_id,
            expected_head_revision=expected_head_revision,
            permission_epoch=effective_epoch,
        )
        return self.bind_writer_memory(
            saved.ref,
            project_id=project_id,
            worktree_id=worktree_id,
            writer_workspace_id=writer_workspace_id,
            run_id=run_id,
            graph_run_id=workspace.graph_run_id,
            permission_epoch=effective_epoch,
        )

    def _evidence_by_id(self, evidence_id: str) -> WriterMemoryEvidence:
        with self.store._connect() as connection:
            self._require_schema(connection)
            row = connection.execute(
                "SELECT * FROM b26_writer_memory_evidence WHERE evidence_id=?", (evidence_id,)
            ).fetchone()
        if row is None:
            raise SharingNotFoundError("Writer memory evidence not found")
        return self._evidence(row)

    def _adapter_workspace_path(self, isolation_ref: str) -> Path:
        resolver = (
            None
            if self.writer_adapter is None
            else getattr(self.writer_adapter, "workspace_path", None)
        )
        if not callable(resolver):
            raise SharingValidationError("local Git verification adapter is unavailable")
        try:
            configured = Path(resolver(isolation_ref))
            if not configured.is_absolute():
                raise ValueError
            resolved = configured.resolve(strict=True)
        except (OSError, TypeError, ValueError) as exc:
            raise SharingValidationError(
                "Writer target is not mapped to a trusted local workspace"
            ) from exc
        if not resolved.is_dir() or configured.is_symlink():
            raise SharingValidationError("Writer target workspace identity is unsafe")
        return resolved

    def _git(self, root: Path, *arguments: str, allow_empty: bool = False) -> str:
        try:
            result = subprocess.run(
                ["git", *arguments],
                cwd=root,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.git_timeout_seconds,
                env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SharingValidationError("local Git verification failed") from exc
        if result.returncode != 0:
            raise SharingValidationError("local Git verification failed")
        value = result.stdout.strip()
        if not value and not allow_empty:
            raise SharingValidationError("local Git verification returned no identity")
        return value

    def _target_git_identity(
        self, isolation_ref: str, result_artifact_ref: str | None
    ) -> tuple[str, str]:
        root = self._adapter_workspace_path(isolation_ref)
        top = Path(self._git(root, "rev-parse", "--show-toplevel")).resolve(strict=True)
        if top != root:
            raise SharingValidationError("Writer target Git root identity changed")
        if self._git(root, "status", "--porcelain", "--untracked-files=all", allow_empty=True):
            raise SharingValidationError("Writer target tree is not clean")
        commit = _commit_ref(self._git(root, "rev-parse", "HEAD"))
        tree = self._git(root, "rev-parse", "HEAD^{tree}")
        if len(tree) != 40 or any(char not in "0123456789abcdef" for char in tree):
            raise SharingValidationError("Writer target tree identity is invalid")
        if result_artifact_ref and result_artifact_ref.startswith("git:"):
            expected = _commit_ref(result_artifact_ref)
            if commit != expected:
                raise SharingValidationError("verified target HEAD differs from the merge result")
        return commit, _tree_digest(tree)

    def _merge_for_evidence(self, evidence: WriterMemoryEvidence, merge_run_id: str) -> Any:
        try:
            merge = self.writer_repository.get_merge_run(merge_run_id)
        except Exception as exc:
            raise SharingNotFoundError("merge run is unavailable") from exc
        if merge.graph_run_id != evidence.graph_run_id:
            raise SharingPermissionError("merge run belongs to another graph Run")
        if merge.status is not MergeRunStatus.SUCCEEDED or not merge.result_artifact_ref:
            raise SharingConflictError(
                "only a succeeded merge with a result artifact can promote Writer memory"
            )
        if merge.base_revision != evidence.base_revision:
            raise SharingConflictError("merge base differs from Writer memory base")
        try:
            writer_workspace = self.writer_repository.get_workspace(evidence.writer_workspace_id)
        except Exception as exc:
            raise SharingNotFoundError("Writer evidence workspace is unavailable") from exc
        if merge.target_isolation_ref == writer_workspace.isolation_ref:
            raise SharingConflictError("merge target cannot be the Writer branch")
        if any(
            conflict.status.value == "open"
            for conflict in self.writer_repository.list_conflicts(evidence.graph_run_id)
        ):
            raise SharingConflictError("open Writer conflict blocks promotion")
        artifacts = []
        for artifact_id in merge.artifact_ids:
            try:
                artifact = self.writer_repository.get_artifact(artifact_id)
            except Exception as exc:
                raise SharingNotFoundError("merge artifact is unavailable") from exc
            artifacts.append(artifact)
        if not any(item.writer_workspace_id == evidence.writer_workspace_id for item in artifacts):
            raise SharingPermissionError("merge does not include the Writer evidence workspace")
        if any(item.base_revision != evidence.base_revision for item in artifacts):
            raise SharingConflictError("merge contains an artifact with a stale Writer base")
        return merge

    def _validate_verification_artifacts(
        self,
        refs: Sequence[str],
        *,
        merge: Any,
        target_commit_ref: str,
        target_tree_digest: str,
    ) -> tuple[str, ...]:
        """Recheck exact Core-generated Git verification facts, not caller text."""

        manager = self.manager
        core_service = None if manager is None else getattr(manager, "service", None)
        get_artifact = getattr(core_service, "get_artifact", None)
        read_artifact = getattr(core_service, "_read_artifact_for_context", None)
        if not callable(get_artifact) or not callable(read_artifact):
            raise SharingValidationError("Core ArtifactStore is unavailable")
        if len(refs) != 1:
            raise SharingValidationError("one Core target verification artifact is required")
        expected = self._verification_report(merge, target_commit_ref, target_tree_digest)
        digests: list[str] = []
        for artifact_id in refs:
            try:
                artifact = get_artifact(artifact_id)
                body = read_artifact(artifact_id)
            except Exception as exc:
                raise SharingValidationError(
                    "verification artifact is unavailable or not Core-owned"
                ) from exc
            if not isinstance(body, bytes) or hashlib.sha256(body).hexdigest() != (
                artifact.content_hash
            ):
                raise SharingValidationError("verification artifact content digest is invalid")
            try:
                report = json.loads(body)
            except (UnicodeDecodeError, ValueError) as exc:
                raise SharingValidationError(
                    "verification artifact must contain the Core merge report"
                ) from exc
            if report != expected or artifact.media_type != (
                "application/vnd.operant.writer-target-verification+json"
            ):
                raise SharingValidationError(
                    "verification artifact does not attest this merge target"
                )
            digests.append(artifact.content_hash)
        return tuple(digests)

    @staticmethod
    def _verification_report(merge: Any, commit: str, tree: str) -> dict[str, Any]:
        return {
            "verifier": "operant.git-merge-target.v1",
            "verification_scope": "git_merge_identity_and_clean_tree",
            "merge_run_id": merge.merge_run_id,
            "merge_revision": merge.expected_revision,
            "target_isolation_ref": merge.target_isolation_ref,
            "target_commit_ref": commit,
            "target_tree_digest": tree,
            "artifact_ids": list(merge.artifact_ids),
            "target_tree_clean": True,
            "business_tests_attested": False,
        }

    def _verification_digest(
        self, merge: Any, commit: str, tree: str, refs: Sequence[str], hashes: Sequence[str]
    ) -> str:
        return _digest(
            {
                **self._verification_report(merge, commit, tree),
                "verification_artifact_refs": tuple(refs),
                "verification_artifact_digests": tuple(hashes),
            }
        )

    def verify_writer_memory(
        self,
        evidence_id: str,
        *,
        merge_run_id: str,
        verification_artifact_refs: Sequence[str] = (),
        expected_revision: int | None = None,
    ) -> WriterMemoryEvidence:
        current = self._evidence_by_id(evidence_id)
        if current.state == "revoked":
            raise SharingPermissionError("revoked Writer evidence cannot be verified")
        if verification_artifact_refs:
            return self._block_evidence(
                current,
                verification_status="failed",
                reason="verification artifacts must be generated by Core, not supplied by callers",
                expected_revision=expected_revision,
            )
        if current.state == "published":
            return current
        try:
            merge = self._merge_for_evidence(current, merge_run_id)
            commit, tree = self._target_git_identity(
                merge.target_isolation_ref,
                merge.result_artifact_ref,
            )
        except SharingConflictError as exc:
            return self._block_evidence(
                current,
                verification_status="failed",
                reason=str(exc),
                expected_revision=expected_revision,
            )
        except SharingValidationError as exc:
            return self._block_evidence(
                current,
                verification_status="unknown",
                reason=str(exc),
                expected_revision=expected_revision,
            )
        core_service = getattr(self.manager, "service", None)
        if core_service is None or not callable(getattr(core_service, "create_artifact", None)):
            return self._block_evidence(
                current,
                verification_status="failed",
                reason="Core ArtifactStore is unavailable",
                expected_revision=expected_revision,
            )
        artifact, _created = core_service.create_artifact(
            content=_json(self._verification_report(merge, commit, tree)).encode("utf-8"),
            media_type="application/vnd.operant.writer-target-verification+json",
        )
        refs = (artifact.id,)
        try:
            artifact_digests = self._validate_verification_artifacts(
                refs,
                merge=merge,
                target_commit_ref=commit,
                target_tree_digest=tree,
            )
        except SharingValidationError as exc:
            return self._block_evidence(
                current,
                verification_status="unknown",
                reason=str(exc),
                expected_revision=expected_revision,
            )
        verification_digest = self._verification_digest(merge, commit, tree, refs, artifact_digests)
        if expected_revision is None:
            expected_revision = current.revision
        updated = current.model_copy(
            update={
                "merge_run_id": merge.merge_run_id,
                "target_isolation_ref": merge.target_isolation_ref,
                "target_commit_ref": commit,
                "target_tree_digest": tree,
                "verification_artifact_refs": refs,
                "verification_digest": verification_digest,
                "verification_status": "passed",
                "state": "eligible",
                "reason": None,
                "revision": current.revision + 1,
                "updated_at": _now(),
            }
        )
        with self.store._connect() as connection:
            self._require_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            result = connection.execute(
                """UPDATE b26_writer_memory_evidence SET merge_run_id=?,target_isolation_ref=?,
                   target_commit_ref=?,target_tree_digest=?,verification_artifact_refs_json=?,
                   verification_digest=?,verification_status=?,state=?,reason=?,revision=?,updated_at=?
                   WHERE evidence_id=? AND revision=?""",
                (
                    updated.merge_run_id,
                    updated.target_isolation_ref,
                    updated.target_commit_ref,
                    updated.target_tree_digest,
                    _json(updated.verification_artifact_refs),
                    updated.verification_digest,
                    updated.verification_status,
                    updated.state,
                    None,
                    updated.revision,
                    updated.updated_at.isoformat(),
                    evidence_id,
                    expected_revision,
                ),
            )
            if result.rowcount != 1:
                raise SharingConflictError("Writer evidence revision conflict")
        return updated

    def _block_evidence(
        self,
        current: WriterMemoryEvidence,
        *,
        verification_status: Literal["failed", "unknown"],
        reason: str,
        expected_revision: int | None,
    ) -> WriterMemoryEvidence:
        expected = current.revision if expected_revision is None else expected_revision
        if expected != current.revision:
            raise SharingConflictError("Writer evidence revision conflict")
        updated = current.model_copy(
            update={
                "verification_status": verification_status,
                "state": "blocked",
                "merge_run_id": None,
                "target_isolation_ref": None,
                "target_commit_ref": None,
                "target_tree_digest": None,
                "verification_artifact_refs": (),
                "verification_digest": None,
                "reason": reason[:2000],
                "revision": current.revision + 1,
                "updated_at": _now(),
            }
        )
        with self.store._connect() as connection:
            self._require_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            result = connection.execute(
                """UPDATE b26_writer_memory_evidence SET merge_run_id=NULL,
                   target_isolation_ref=NULL,
                   target_commit_ref=NULL,target_tree_digest=NULL,verification_artifact_refs_json=?,
                   verification_digest=NULL,verification_status=?,state=?,reason=?,revision=?,updated_at=?
                   WHERE evidence_id=? AND revision=?""",
                (
                    _json(()),
                    updated.verification_status,
                    updated.state,
                    updated.reason,
                    updated.revision,
                    updated.updated_at.isoformat(),
                    current.evidence_id,
                    current.revision,
                ),
            )
            if result.rowcount != 1:
                raise SharingConflictError("Writer evidence revision conflict")
        return updated

    def evaluate_writer_promotion(self, evidence_id: str) -> WriterPromotionDecision:
        evidence = self._evidence_by_id(evidence_id)
        if evidence.state == "revoked":
            return WriterPromotionDecision(
                evidence_id=evidence_id,
                status="revoked",
                verification_status=evidence.verification_status,
                reason=evidence.reason or "writer evidence revoked",
                merge_run_id=evidence.merge_run_id,
                target_commit_ref=evidence.target_commit_ref,
                target_tree_digest=evidence.target_tree_digest,
                verification_artifact_refs=evidence.verification_artifact_refs,
            )
        if evidence.verification_status != "passed" or evidence.merge_run_id is None:
            return WriterPromotionDecision(
                evidence_id=evidence_id,
                status="candidate" if evidence.state == "candidate" else "blocked",
                verification_status=evidence.verification_status,
                reason=evidence.reason or "merge target verification is required",
                merge_run_id=evidence.merge_run_id,
                target_commit_ref=evidence.target_commit_ref,
                target_tree_digest=evidence.target_tree_digest,
                verification_artifact_refs=evidence.verification_artifact_refs,
            )
        try:
            merge = self._merge_for_evidence(evidence, evidence.merge_run_id)
            commit, tree = self._target_git_identity(
                merge.target_isolation_ref,
                merge.result_artifact_ref,
            )
            if commit != evidence.target_commit_ref or tree != evidence.target_tree_digest:
                raise SharingValidationError("target Git identity changed after verification")
            hashes = self._validate_verification_artifacts(
                evidence.verification_artifact_refs,
                merge=merge,
                target_commit_ref=commit,
                target_tree_digest=tree,
            )
            if evidence.verification_digest != self._verification_digest(
                merge, commit, tree, evidence.verification_artifact_refs, hashes
            ):
                raise SharingValidationError("target verification evidence digest changed")
        except (SharingError, ValueError) as exc:
            return WriterPromotionDecision(
                evidence_id=evidence_id,
                status="blocked",
                verification_status="unknown",
                reason=str(exc),
                merge_run_id=evidence.merge_run_id,
                target_commit_ref=evidence.target_commit_ref,
                target_tree_digest=evidence.target_tree_digest,
                verification_artifact_refs=evidence.verification_artifact_refs,
            )
        return WriterPromotionDecision(
            evidence_id=evidence_id,
            status="published" if evidence.state == "published" else "eligible",
            verification_status="passed",
            merge_run_id=evidence.merge_run_id,
            target_commit_ref=evidence.target_commit_ref,
            target_tree_digest=evidence.target_tree_digest,
            verification_artifact_refs=evidence.verification_artifact_refs,
        )

    def mark_writer_published(
        self,
        evidence_id: str,
        *,
        published_memory_ref: MemoryVersionRef,
        expected_revision: int,
    ) -> WriterMemoryEvidence:
        evidence = self._evidence_by_id(evidence_id)
        decision = self.evaluate_writer_promotion(evidence_id)
        if decision.status != "eligible":
            raise SharingConflictError("Writer evidence is not currently eligible for publication")
        if evidence.revision != expected_revision:
            raise SharingConflictError("Writer evidence revision conflict")
        if published_memory_ref == evidence.memory_ref:
            raise SharingValidationError("published memory must be a new project version")
        updated = evidence.model_copy(
            update={
                "state": "published",
                "published_memory_ref": published_memory_ref,
                "revision": evidence.revision + 1,
                "updated_at": _now(),
            }
        )
        with self.store._connect() as connection:
            self._require_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            result = connection.execute(
                """UPDATE b26_writer_memory_evidence SET state=?,
                   published_memory_ref_json=?,
                   revision=?,updated_at=? WHERE evidence_id=? AND revision=?
                   AND state='eligible'""",
                (
                    updated.state,
                    _json(published_memory_ref),
                    updated.revision,
                    updated.updated_at.isoformat(),
                    evidence_id,
                    expected_revision,
                ),
            )
            if result.rowcount != 1:
                raise SharingConflictError("Writer evidence revision conflict")
        return updated

    def promote_writer_memory(
        self,
        evidence_id: str,
        *,
        expected_revision: int,
    ) -> WriterMemoryEvidence:
        """Verify again and publish the derived project version in the ledger."""

        if self.ledger is None:
            raise SharingPermissionError("Writer promotion requires the Core ledger")
        evidence = self._evidence_by_id(evidence_id)
        if evidence.state == "published":
            return evidence
        if evidence.revision != expected_revision:
            raise SharingConflictError("Writer evidence revision conflict")
        try:
            candidate = self.ledger.get_version(
                evidence.memory_ref.dataset_id,
                evidence.memory_ref.record_id,
                evidence.memory_ref.version,
            )
        except Exception as exc:
            raise SharingPermissionError("Writer candidate is unavailable") from exc
        if candidate.ref != evidence.memory_ref:
            raise SharingPermissionError("Writer candidate digest does not match evidence")
        self._validate_writer_source_constraints(candidate)
        decision = self.evaluate_writer_promotion(evidence_id)
        if decision.status != "eligible":
            raise SharingConflictError(decision.reason or "Writer evidence is not eligible")
        registration = self._registration_by_worktree(evidence.project_id, evidence.worktree_id)
        if registration.state != "active":
            raise SharingPermissionError("Writer worktree registration is revoked")
        if evidence.permission_epoch != self._project_permission_epoch(evidence.project_id):
            raise SharingPermissionError("Writer evidence permission epoch is stale")
        target_scope = WorkspaceScope(
            kind="workspace",
            project_id=evidence.project_id,
            workspace_id=registration.workspace_id,
        )
        promoted, _head = self.ledger.promote_verified(
            evidence.memory_ref,
            target_scope=target_scope,
            target_commit_ref=evidence.target_commit_ref or "",
            target_tree_digest=evidence.target_tree_digest or "",
            verification_artifact_refs=evidence.verification_artifact_refs,
            verification_digest=evidence.verification_digest or "",
            expected_head_revision=evidence.candidate_head_revision,
            permission_epoch=evidence.permission_epoch,
            reason="verified Writer merge target",
            idempotency_key=evidence.evidence_id,
        )
        return self.mark_writer_published(
            evidence_id,
            published_memory_ref=promoted.ref,
            expected_revision=expected_revision,
        )

    def revoke_writer_memory(
        self,
        evidence_id: str,
        *,
        expected_revision: int,
        reason: str,
    ) -> WriterMemoryEvidence:
        if not reason.strip():
            raise SharingValidationError("Writer evidence revocation reason is required")
        evidence = self._evidence_by_id(evidence_id)
        if evidence.state == "revoked":
            return evidence
        if evidence.revision != expected_revision:
            raise SharingConflictError("Writer evidence revision conflict")
        updated = evidence.model_copy(
            update={
                "state": "revoked",
                "reason": reason.strip(),
                "revision": evidence.revision + 1,
                "updated_at": _now(),
            }
        )
        with self.store._connect() as connection:
            self._require_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            result = connection.execute(
                """UPDATE b26_writer_memory_evidence SET state=?,reason=?,revision=?,updated_at=?
                   WHERE evidence_id=? AND revision=?""",
                (
                    updated.state,
                    updated.reason,
                    updated.revision,
                    updated.updated_at.isoformat(),
                    evidence_id,
                    expected_revision,
                ),
            )
            if result.rowcount != 1:
                raise SharingConflictError("Writer evidence revision conflict")
        return updated

    def writer_evidence(self, project_id: str) -> tuple[WriterMemoryEvidence, ...]:
        with self.store._connect() as connection:
            self._require_schema(connection)
            rows = connection.execute(
                "SELECT * FROM b26_writer_memory_evidence WHERE project_id=? "
                "ORDER BY created_at,evidence_id",
                (project_id,),
            ).fetchall()
        return tuple(self._evidence(row) for row in rows)

    def writer_candidates(self, project_id: str) -> tuple[WriterMemoryCandidate, ...]:
        """Return only bound Run candidates owned by the requested project."""

        if self.ledger is None:
            return ()
        candidates: list[WriterMemoryCandidate] = []
        for evidence in self.writer_evidence(project_id):
            try:
                memory = self.ledger.get_version(
                    evidence.memory_ref.dataset_id,
                    evidence.memory_ref.record_id,
                    evidence.memory_ref.version,
                )
            except Exception:
                continue
            if memory.ref != evidence.memory_ref or not isinstance(memory.scope, RunScope):
                continue
            candidates.append(
                WriterMemoryCandidate(
                    evidence_id=evidence.evidence_id,
                    memory=memory,
                    project_id=evidence.project_id,
                    workspace_id=evidence.workspace_id,
                    worktree_id=evidence.worktree_id,
                    writer_workspace_id=evidence.writer_workspace_id,
                    graph_run_id=evidence.graph_run_id,
                    run_id=evidence.run_id,
                    branch_ref=evidence.branch_ref,
                    state=evidence.state,
                    revision=evidence.revision,
                )
            )
        return tuple(candidates)

    def personal_preferences(self, project_id: str) -> tuple[MemoryVersion, ...]:
        """Return this local principal's usable preferences with exact scope/owner.

        The client can display and explicitly grant these versions without
        inventing PersonalScope metadata or maintaining another preference store.
        """

        if self.manager is None or self.ledger is None or self.registry is None:
            return ()
        try:
            actor = self._trusted_principal(project_id, None)
            project = self._project(project_id)
            if project is None or not project.get("installation_id"):
                return ()
            installation = self.registry.get_installation(project["installation_id"])
            dataset_id = str(installation.dataset_id)
        except Exception:
            return ()
        versions: list[MemoryVersion] = []
        for head in self.ledger.list_heads(dataset_id):
            if head.state != "published" or head.published_version is None:
                continue
            try:
                version = self.ledger.get_version(
                    dataset_id, head.published_version.record_id, head.published_version.version
                )
            except Exception:
                continue
            if (
                version.ref == head.published_version
                and isinstance(version.scope, PersonalScope)
                and version.scope.principal_id == actor
                and version.content_type == "preference"
            ):
                from operant.memory_plugins.governance import GovernanceService

                if GovernanceService(self.manager).version_dependencies_valid(
                    version.ref, project_id=project_id
                ):
                    versions.append(version)
        return tuple(versions)

    def state(self, project_id: str) -> SharingState:
        return SharingState(
            project_id=project_id,
            projection_revision=self._projection_revision(project_id),
            worktrees=self.list_worktrees(project_id),
            grants=self.list_grants(project_id),
            consumers=self._list_consumers_for_project(project_id),
            transfers=self.list_transfers(project_id),
            writer_evidence=self.writer_evidence(project_id),
            writer_candidates=self.writer_candidates(project_id),
            personal_preferences=self.personal_preferences(project_id),
        )

    def _list_consumers_for_project(self, project_id: str) -> tuple[SharingConsumer, ...]:
        with self.store._connect() as connection:
            self._require_schema(connection)
            rows = connection.execute(
                "SELECT * FROM b26_dataset_consumers WHERE project_id=? "
                "ORDER BY created_at,consumer_id",
                (project_id,),
            ).fetchall()
        return tuple(self._consumer(row) for row in rows)

    def _projection_revision(self, project_id: str) -> int:
        with self.store._connect() as connection:
            self._require_schema(connection)
            values = []
            for table, column in (
                ("b26_worktree_registrations", "association_revision"),
                ("b26_sharing_grants", "revision"),
                ("b26_dataset_consumers", "revision"),
                ("b26_dataset_transfers", "revision"),
                ("b26_writer_memory_evidence", "revision"),
            ):
                row = connection.execute(
                    f"SELECT MAX({column}) AS value FROM {table} WHERE project_id=?",
                    (project_id,),
                ).fetchone()
                values.append(int(row["value"] or 0))
            return max(values, default=0)

    async def execute(self, command: SharingCommand) -> SharingResult:
        """Execute a feature command after Core has authenticated its actor.

        Idempotency and event journaling belong to the parent B2-6 command
        gateway.  Direct methods still use explicit IDs and revision CAS so a
        replay cannot silently apply a different object.
        """

        action = command.action
        affected: tuple[str, ...] = ()
        status: Literal["completed", "blocked", "revoked", "failed"] = "completed"
        message = "B2-6 sharing command completed"
        value: Any
        if action == "worktree_register":
            registration = self._registration_from_command(command)
            value = self.register_worktree(
                registration,
                expected_revision=command.expected_revision,
            )
            affected = (value.registration_id,)
            message = "Project-worktree registration saved"
        elif action == "worktree_revoke":
            if command.registration_id is None or command.expected_revision is None:
                raise SharingValidationError(
                    "worktree_revoke requires registration_id and expected_revision"
                )
            value = self.revoke_worktree(
                command.registration_id,
                expected_revision=command.expected_revision,
                reason=command.reason or "explicitly revoked",
            )
            affected = (value.registration_id,)
            status = "revoked"
            message = "Project-worktree registration revoked"
        elif action == "grant_create":
            if command.grant is None:
                raise SharingValidationError("grant_create requires grant")
            value = self.create_grant(command.grant)
            affected = (value.grant_id,)
            message = "explicit sharing grant saved"
        elif action == "grant_revoke":
            if command.grant_id is None or command.expected_revision is None:
                raise SharingValidationError("grant_revoke requires grant_id and expected_revision")
            value = self.revoke_grant(
                command.grant_id,
                expected_revision=command.expected_revision,
                reason=command.reason or "explicitly revoked",
            )
            affected = (value.grant_id,)
            status = "revoked"
            message = "sharing grant revoked"
        elif action == "dataset_transfer_begin":
            if command.transfer is None:
                raise SharingValidationError("dataset_transfer_begin requires transfer")
            value = self.begin_transfer(
                command.transfer,
                project_id=command.project_id,
                source_installation_id=command.installation_id,
                transfer_id=command.transfer_id,
            )
            affected = (value.transfer_id,)
            message = "dataset transfer staged"
        elif action == "dataset_transfer_validate":
            if command.transfer_id is None or command.expected_revision is None:
                raise SharingValidationError(
                    "dataset_transfer_validate requires transfer_id and expected_revision"
                )
            value = self.validate_transfer(
                command.transfer_id,
                evidence_refs=command.verification_artifact_refs,
                expected_revision=command.expected_revision,
            )
            affected = (value.transfer_id,)
            message = "dataset transfer validated"
        elif action == "dataset_transfer_commit":
            if command.transfer_id is None or command.expected_revision is None:
                raise SharingValidationError(
                    "dataset_transfer_commit requires transfer_id and expected_revision"
                )
            value = self.commit_transfer(
                command.transfer_id,
                expected_revision=command.expected_revision,
            )
            affected = (value.transfer_id, value.destination_consumer_id)
            message = "dataset transfer committed"
        elif action == "dataset_transfer_revoke":
            if command.transfer_id is None or command.expected_revision is None:
                raise SharingValidationError(
                    "dataset_transfer_revoke requires transfer_id and expected_revision"
                )
            value = self.revoke_transfer(
                command.transfer_id,
                expected_revision=command.expected_revision,
                reason=command.reason or "explicitly revoked",
            )
            affected = (value.transfer_id,)
            status = "revoked"
            message = "dataset transfer revoked"
        elif action == "consumer_attach":
            if (
                not command.dataset_id
                or not command.installation_id
                or not command.consumer_grant_id
            ):
                raise SharingValidationError(
                    "consumer_attach requires dataset, installation and grant"
                )
            value = self.attach_consumer(
                project_id=command.project_id,
                dataset_id=command.dataset_id,
                installation_id=command.installation_id,
                grant_id=command.consumer_grant_id,
                purpose=command.consumer_purpose or "shared_read",
                consumer_id=command.consumer_id,
            )
            affected = (value.consumer_id,)
            message = "dataset consumer attached"
        elif action == "consumer_revoke":
            if command.consumer_id is None or command.expected_revision is None:
                raise SharingValidationError(
                    "consumer_revoke requires consumer_id and expected_revision"
                )
            value = self.revoke_consumer(
                command.consumer_id,
                expected_revision=command.expected_revision,
                reason=command.reason or "explicitly revoked",
            )
            affected = (value.consumer_id,)
            status = "revoked"
            message = "dataset consumer revoked"
        elif action == "personal_preference_create":
            if command.content is None:
                raise SharingValidationError("personal_preference_create requires content")
            value = self.create_personal_preference(
                project_id=command.project_id,
                content=command.content,
                source_refs=command.source_refs,
                record_id=command.record_id,
            )
            affected = (value.ref.record_id,)
            message = "personal preference published with explicit opt-in scope"
        elif action == "writer_memory_bind":
            if (
                command.memory_ref is None
                or command.worktree_id is None
                or command.writer_workspace_id is None
                or command.run_id is None
            ):
                raise SharingValidationError(
                    "writer_memory_bind requires memory, worktree, Writer workspace and Run"
                )
            value = self.bind_writer_memory(
                command.memory_ref,
                project_id=command.project_id,
                worktree_id=command.worktree_id,
                writer_workspace_id=command.writer_workspace_id,
                run_id=command.run_id,
                graph_run_id=command.graph_run_id,
                evidence_id=command.evidence_id,
            )
            affected = (value.evidence_id,)
            message = "Writer memory candidate bound"
        elif action == "writer_memory_propose":
            if (
                command.worktree_id is None
                or command.writer_workspace_id is None
                or command.run_id is None
                or command.content is None
            ):
                raise SharingValidationError(
                    "writer_memory_propose requires worktree, Writer workspace, Run and content"
                )
            value = self.propose_writer_memory(
                project_id=command.project_id,
                worktree_id=command.worktree_id,
                writer_workspace_id=command.writer_workspace_id,
                run_id=command.run_id,
                content=command.content,
                source_refs=command.source_refs,
                record_id=command.record_id,
                content_type=command.content_type or "fact",
                sensitivity=command.sensitivity or "internal",
            )
            affected = (value.evidence_id, value.memory_ref.record_id)
            message = "Writer memory candidate created and bound"
        elif action == "writer_memory_verify":
            if command.evidence_id is None or command.merge_run_id is None:
                raise SharingValidationError(
                    "writer_memory_verify requires evidence_id and merge_run_id"
                )
            value = self.verify_writer_memory(
                command.evidence_id,
                merge_run_id=command.merge_run_id,
                verification_artifact_refs=command.verification_artifact_refs,
                expected_revision=command.expected_revision,
            )
            affected = (value.evidence_id,)
            if value.state == "blocked":
                status = "blocked"
                message = value.reason or "Writer target verification blocked"
            else:
                message = "Writer target verified; Core may publish a new project version"
        elif action == "writer_memory_promote":
            if command.evidence_id is None:
                raise SharingValidationError("writer_memory_promote requires evidence_id")
            expected = command.expected_revision
            if expected is None:
                expected = self._evidence_by_id(command.evidence_id).revision
            try:
                value = self.promote_writer_memory(
                    command.evidence_id,
                    expected_revision=expected,
                )
            except SharingError as exc:
                status = "blocked"
                affected = (command.evidence_id,)
                message = str(exc)
            else:
                affected = (
                    value.evidence_id,
                    value.published_memory_ref.record_id
                    if value.published_memory_ref
                    else value.memory_ref.record_id,
                )
                message = "Writer evidence verified and published as a project memory version"
        elif action == "writer_memory_revoke":
            if command.evidence_id is None or command.expected_revision is None:
                raise SharingValidationError(
                    "writer_memory_revoke requires evidence_id and expected_revision"
                )
            value = self.revoke_writer_memory(
                command.evidence_id,
                expected_revision=command.expected_revision,
                reason=command.reason or "explicitly revoked",
            )
            affected = (value.evidence_id,)
            status = "revoked"
            message = "Writer memory evidence revoked"
        else:  # pragma: no cover - Literal validation owns the public set.
            raise SharingValidationError("unsupported B2-6 sharing action")
        return SharingResult(
            status=status,
            message=message,
            affected_ids=affected,
            state=self.state(command.project_id),
        )


# Short names used by adapters and focused tests.
MemorySharingService = SharingService
ExperienceSharingService = SharingService


__all__ = [
    "ExperienceSharingService",
    "MemorySharingService",
    "SharingConflictError",
    "SharingError",
    "SharingNotFoundError",
    "SharingPermissionError",
    "SharingSchemaError",
    "SharingService",
    "SharingValidationError",
    "scope_permits",
]
