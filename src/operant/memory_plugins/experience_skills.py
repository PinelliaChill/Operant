"""Core-owned lifecycle for Skills derived from verified ``procedure`` memory.

The module deliberately keeps the generated Skill separate from the existing
user-installed Skill discovery path.  A generated version is immutable, its
publication pointer is CAS guarded, and its bytes live in the existing
content-addressed Artifact Store supplied by ``ApplicationService``.  This
module owns no tool permission and never executes generated content.

``ExperienceSkillService`` is intentionally usable before the B2-6 API is
assembled.  The public integration points are ``state`` and ``execute``;
runtime code can additionally use ``snapshot_for_run`` and
``assert_snapshot_usable`` to re-check source revocation before every model
request.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal, Protocol, cast

from operant.contracts.b2_1 import (
    DatasetOwner,
    MemoryConditions,
    MemoryVersion,
    MemoryVersionRef,
    SourceRef,
)
from operant.contracts.b2_5 import GovernanceEntry
from operant.contracts.b2_6_skills import (
    SkillArtifact,
    SkillCommand,
    SkillHead,
    SkillResourceHash,
    SkillRunSnapshot,
    SkillState,
    SkillTrustStatus,
    SkillValidation,
    SkillValidationIssue,
    SkillValidationStatus,
    SkillVersion,
    SkillView,
)
from operant.domain.models import new_id, utc_now
from operant.memory_plugins.governance import source_key
from operant.memory_plugins.ledger import MemoryLedger

from .experience_skills_schema import SCHEMA_SQL

_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_FRONTMATTER_KEYS = frozenset({"name", "description"})
_MAX_SKILL_BYTES = 2_000_000
_VALIDATOR_VERSION = "operant-b2-6-skill-validator.v1"


class SkillError(RuntimeError):
    """Base error for generated Skill lifecycle operations."""


class SkillNotFoundError(LookupError, SkillError):
    """The requested generated Skill or version does not exist."""


class SkillConflictError(ValueError, SkillError):
    """A Skill CAS, immutable identity, or lifecycle check failed."""


class SkillPermissionError(PermissionError, SkillError):
    """The current project, source, scope, or permission epoch is ineligible."""


class SkillValidationError(ValueError, SkillError):
    """A generated Skill document or validation request is malformed."""


class SkillBlobStore(Protocol):
    """The subset of the existing ArtifactStore needed by this service."""

    def put_bytes(self, content: bytes) -> Any: ...

    def read(self, content_hash: str, size_bytes: int) -> bytes: ...

    def verify(self, content_hash: str, size_bytes: int) -> None: ...


SourceAuthorizer = Callable[[str, SourceRef, int], bool]


def _json_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _json_value(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, set):
        return sorted(_json_value(item) for item in value)
    if hasattr(value, "value"):
        return _json_value(value.value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _json(value: Any) -> str:
    return json.dumps(_json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _source_identity(source: SourceRef) -> dict[str, Any]:
    """Identity used by B2-5 ``source_key``; mutable access state is excluded."""

    return {
        "source_type": source.source_type,
        "source_id": source.source_id,
        "source_revision": source.revision,
        "content_digest": source.content_digest,
    }


def _source_digest(procedure: MemoryVersion, sources: Sequence[SourceRef]) -> str:
    identities = sorted((_source_identity(source) for source in sources), key=_json)
    return _digest({"procedure": procedure.ref, "sources": identities})


def _is_procedure_dependency(source: SourceRef, procedure: MemoryVersion) -> bool:
    """Identify the synthetic dependency used to revoke the procedure itself."""

    return (
        source.source_type == "memory_version"
        and source.source_id == procedure.ref.record_id
        and source.revision == procedure.ref.version
        and source.content_digest == procedure.ref.content_digest
    )


def _safe_frontmatter_value(value: str, label: str) -> str:
    if not value or len(value) > 4_000 or any(char in value for char in "\x00\r\n"):
        raise SkillValidationError(f"{label} contains an invalid control character or length")
    # These are the same unsafe YAML constructs rejected by SkillDiscovery.
    if re.search(r"(?:^|[\s\[{,:])(?:!!|![A-Za-z]|&[A-Za-z]|\*[A-Za-z])", value):
        raise SkillValidationError(f"{label} contains an unsafe YAML construct")
    if "${" in value or "<(" in value:
        raise SkillValidationError(f"{label} contains an unsafe expansion marker")
    return value


def render_skill_document(*, name: str, description: str, procedure_text: str) -> bytes:
    """Render a deterministic, text-only SKILL.md document.

    The procedure text is data in the generated artifact.  No executable
    resource or ``allowed-tools`` declaration is inferred from it.
    """

    if _SKILL_NAME_RE.fullmatch(name) is None:
        raise SkillValidationError("Skill name is not a safe lowercase identifier")
    description = _safe_frontmatter_value(description, "Skill description")
    if not procedure_text.strip() or "\x00" in procedure_text:
        raise SkillValidationError("procedure content is blank or contains NUL")
    document = (
        "---\n"
        f"name: {name}\n"
        f"description: {json.dumps(description, ensure_ascii=False)}\n"
        "---\n\n"
        "# Generated procedure\n\n"
        f"{procedure_text.strip()}\n"
    )
    encoded = document.encode("utf-8")
    if len(encoded) > _MAX_SKILL_BYTES:
        raise SkillValidationError("generated Skill document exceeds the byte limit")
    return encoded


def validate_skill_document(
    content: bytes, *, expected_name: str, expected_description: str
) -> None:
    """Validate the bounded frontmatter/body shape without importing code."""

    if len(content) > _MAX_SKILL_BYTES:
        raise SkillValidationError("Skill document exceeds the byte limit")
    try:
        text = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise SkillValidationError("Skill document is not valid UTF-8") from exc
    if not text.startswith("---\n"):
        raise SkillValidationError("Skill document must start with frontmatter")
    boundary = text.find("\n---\n", 4)
    if boundary < 0:
        raise SkillValidationError("Skill document frontmatter is not terminated")
    values: dict[str, str] = {}
    for line in text[4:boundary].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "\t" in line or line[:1].isspace() or ":" not in line:
            raise SkillValidationError("Skill frontmatter must be flat and well formed")
        key, raw_value = line.split(":", 1)
        key = key.strip()
        if key not in _FRONTMATTER_KEYS or key in values:
            raise SkillValidationError("Skill frontmatter contains an unsupported or duplicate key")
        value = raw_value.strip()
        if key == "description":
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                raise SkillValidationError(
                    "Skill description must be a JSON string scalar"
                ) from exc
            if not isinstance(parsed, str):
                raise SkillValidationError("Skill description must be a string")
            value = parsed
        values[key] = _safe_frontmatter_value(value, key)
    if values.get("name") != expected_name or values.get("description") != expected_description:
        raise SkillValidationError("Skill frontmatter does not match its immutable version")
    body = text[boundary + 5 :]
    if not body.strip():
        raise SkillValidationError("Skill document body is blank")


class ExperienceSkillService:
    """Create and govern source-bound Skill artifacts in the Core."""

    def __init__(
        self,
        manager: Any,
        *,
        artifact_store: SkillBlobStore | None = None,
        source_authorizer: SourceAuthorizer | None = None,
        initialize_schema: bool = False,
    ) -> None:
        self.manager = manager
        self.store = manager.store
        self.ledger: MemoryLedger = manager.ledger
        if artifact_store is None:
            service = getattr(manager, "service", None)
            factory = getattr(service, "_artifact_blob_store", None)
            artifact_store = factory() if callable(factory) else None
        if artifact_store is None:
            raise SkillError("Core Artifact Store is required for generated Skill artifacts")
        self.artifact_store = artifact_store
        self._source_authorizer = source_authorizer
        if initialize_schema:
            self.ensure_schema()

    def ensure_schema(self) -> None:
        with self.store._connect() as connection:
            connection.executescript(SCHEMA_SQL)

    def _project(self, project_id: str) -> dict[str, Any]:
        try:
            project = self.manager._project(project_id)
        except Exception as exc:
            raise SkillNotFoundError("project is unavailable") from exc
        if project.get("archived"):
            raise SkillPermissionError("project is archived")
        return cast(dict[str, Any], project)

    def _runtime_identity(
        self, project_id: str, *, enabled: bool = True
    ) -> tuple[dict[str, Any], Any, Any, Any]:
        project = self._project(project_id)
        installation_id = project.get("installation_id")
        if not installation_id:
            raise SkillPermissionError("project has no selected memory installation")
        try:
            installation = self.manager.registry.get_installation(installation_id)
        except Exception as exc:
            raise SkillPermissionError("selected memory installation is unavailable") from exc
        if not installation.binding_id:
            raise SkillPermissionError("memory installation has no binding")
        binding = self.manager.registry.get_binding(installation.binding_id)
        if enabled and (
            installation.state != "enabled"
            or not binding.enabled
            or not binding.global_enabled
            or not getattr(self.manager, "_state", {}).get("global_enabled", True)
            or not project.get("memory_enabled", True)
        ):
            raise SkillPermissionError("memory binding is disabled")
        return project, installation, binding, self.manager._scope(project)

    def _current_permission_epoch(self, project_id: str) -> int:
        _project, _installation, binding, _scope = self._runtime_identity(project_id, enabled=False)
        return int(binding.permission_epoch)

    def _source_usable(self, project_id: str, source: SourceRef, permission_epoch: int) -> bool:
        if source.availability != "available" or source.permission_epoch != permission_epoch:
            return False
        if self._source_authorizer is not None:
            try:
                return self._source_authorizer(project_id, source, permission_epoch) is True
            except Exception:
                return False
        try:
            from operant.memory_plugins.governance import GovernanceService

            return GovernanceService(self.manager).source_authorized(project_id, source)
        except Exception:
            # A missing governance projection must not become an implicit
            # source grant.  Test doubles can provide ``source_authorizer``.
            return False

    def _procedure_dependencies_usable(
        self,
        project_id: str,
        procedure: MemoryVersion,
        permission_epoch: int,
    ) -> bool:
        try:
            from operant.memory_plugins.governance import GovernanceService

            if not GovernanceService(self.manager).version_dependencies_valid(
                procedure.ref,
                project_id=project_id,
            ):
                return False
        except Exception:
            return False
        # Governance checks canonical source identity and revocation.  Retain
        # the binding epoch check here so a stale draft cannot be republished.
        return all(
            self._source_usable(project_id, source, permission_epoch)
            for source in procedure.sources
        )

    def _load_procedure(
        self,
        project_id: str,
        ref: MemoryVersionRef,
        *,
        scope: Any,
        permission_epoch: int,
    ) -> MemoryVersion:
        project = self._project(project_id)
        installation_id = project.get("installation_id")
        if not installation_id:
            raise SkillPermissionError("project has no selected memory installation")
        try:
            installation = self.manager.registry.get_installation(installation_id)
        except Exception as exc:
            raise SkillPermissionError("selected memory installation is unavailable") from exc
        if ref.dataset_id != installation.dataset_id:
            raise SkillPermissionError("procedure belongs to another dataset")
        try:
            procedure = self.ledger.authorize_ref(ref, dataset_id=ref.dataset_id, scope=scope)
        except Exception as exc:
            raise SkillPermissionError(
                "procedure reference is not the current authorized head"
            ) from exc
        if procedure.content_type != "procedure":
            raise SkillValidationError("only procedure Memory can become an experience Skill")
        if procedure.scope != scope:
            raise SkillPermissionError("procedure scope does not match the project")
        if not self._procedure_dependencies_usable(project_id, procedure, permission_epoch):
            raise SkillPermissionError("procedure source is unavailable or revoked")
        return procedure

    def _expanded_dependency_sources(
        self,
        procedure: MemoryVersion,
        *,
        permission_epoch: int,
    ) -> tuple[SourceRef, ...]:
        """Flatten nested Memory sources for direct revoke propagation."""

        result: list[SourceRef] = []
        seen: set[str] = set()
        visiting: set[tuple[str, int]] = set()

        def add(source: SourceRef) -> None:
            key = source_key(source)
            if key not in seen:
                seen.add(key)
                result.append(source)

        def visit(version: MemoryVersion) -> None:
            marker = (version.ref.record_id, version.ref.version)
            if marker in visiting:
                raise SkillValidationError("procedure provenance contains a cycle")
            visiting.add(marker)
            for source in version.sources:
                add(source)
                if source.source_type == "memory_version":
                    try:
                        nested = self.ledger.get_version(
                            procedure.ref.dataset_id,
                            source.source_id,
                            source.revision,
                        )
                    except Exception:
                        # The direct SourceRef remains in the dependency set;
                        # validation will fail closed if it cannot be resolved.
                        continue
                    visit(nested)
            visiting.remove(marker)

        # A procedure is itself a source dependency.  It is represented using
        # the same stable key format as a MemoryVersion SourceRef.
        add(
            SourceRef(
                source_type="memory_version",
                source_id=procedure.ref.record_id,
                revision=procedure.ref.version,
                content_digest=procedure.ref.content_digest,
                scope=procedure.scope,
                permission_epoch=permission_epoch,
                availability="available",
            )
        )
        visit(procedure)
        return tuple(result)

    @staticmethod
    def _artifact_from_content(content: bytes, artifact_store: SkillBlobStore) -> SkillArtifact:
        local_hash = hashlib.sha256(content).hexdigest()
        if len(content) < 1 or len(content) > _MAX_SKILL_BYTES:
            raise SkillValidationError("Skill artifact size is outside the configured bound")
        try:
            stored = artifact_store.put_bytes(content)
        except Exception as exc:
            raise SkillError("Core Artifact Store rejected the Skill artifact") from exc
        actual_hash = getattr(stored, "content_hash", None)
        actual_size = getattr(stored, "size_bytes", None)
        if actual_hash != local_hash or actual_size != len(content):
            raise SkillConflictError("Artifact Store returned a mismatched content identity")
        resource = SkillResourceHash(
            relative_path="SKILL.md",
            kind="manifest",
            size_bytes=len(content),
            sha256=local_hash,
        )
        return SkillArtifact(
            artifact_id=f"artifact_skill_{local_hash}",
            media_type="text/markdown; charset=utf-8",
            content_hash=local_hash,
            size_bytes=len(content),
            manifest_hash=local_hash,
            resources=(resource,),
        )

    def _stored_version(
        self, connection: sqlite3.Connection, skill_id: str, version: int
    ) -> SkillVersion:
        row = connection.execute(
            "SELECT * FROM b26_skill_versions WHERE skill_id=? AND version=?",
            (skill_id, version),
        ).fetchone()
        if row is None:
            raise SkillNotFoundError(
                f"generated Skill version is unavailable: {skill_id}@{version}"
            )
        try:
            result = SkillVersion(
                skill_id=row["skill_id"],
                version=row["version"],
                name=row["name"],
                description=row["description"],
                procedure_ref=json.loads(row["procedure_ref_json"]),
                source_refs=tuple(json.loads(row["source_refs_json"])),
                source_digest=row["source_digest"],
                scope=json.loads(row["scope_json"]),
                role_ids=tuple(json.loads(row["role_ids_json"])),
                agent_ids=tuple(json.loads(row["agent_ids_json"])),
                sensitivity=row["sensitivity"],
                artifact=json.loads(row["artifact_json"]),
                trust_status=row["trust_status"],
                created_at=row["created_at"],
            )
        except Exception as exc:
            raise SkillValidationError("stored Skill version failed contract validation") from exc
        if (
            result.artifact.content_hash != row["artifact_content_hash"]
            or result.artifact.size_bytes != row["artifact_size_bytes"]
        ):
            raise SkillValidationError("stored Skill artifact identity is inconsistent")
        return result

    @staticmethod
    def _stored_head(connection: sqlite3.Connection, skill_id: str) -> SkillHead:
        row = connection.execute(
            "SELECT * FROM b26_skill_heads WHERE skill_id=?", (skill_id,)
        ).fetchone()
        if row is None:
            raise SkillNotFoundError(f"generated Skill is unavailable: {skill_id}")
        try:
            return SkillHead(
                skill_id=row["skill_id"],
                head_revision=row["head_revision"],
                published_version=row["published_version"],
                state=row["state"],
                permission_epoch=row["permission_epoch"],
                updated_at=row["updated_at"],
            )
        except Exception as exc:
            raise SkillValidationError("stored Skill head failed contract validation") from exc

    @staticmethod
    def _stored_validation(
        connection: sqlite3.Connection, skill_id: str, version: int
    ) -> SkillValidation | None:
        row = connection.execute(
            """
            SELECT * FROM b26_skill_validations
            WHERE skill_id=? AND skill_version=?
            ORDER BY checked_at DESC, validation_id DESC LIMIT 1
            """,
            (skill_id, version),
        ).fetchone()
        if row is None:
            return None
        try:
            issues = tuple(
                SkillValidationIssue.model_validate(item) for item in json.loads(row["issues_json"])
            )
            return SkillValidation(
                validation_id=row["validation_id"],
                skill_id=row["skill_id"],
                skill_version=row["skill_version"],
                status=row["status"],
                validator_version=row["validator_version"],
                artifact_hash=row["artifact_hash"],
                source_digest=row["source_digest"],
                issues=issues,
                checked_at=row["checked_at"],
            )
        except Exception as exc:
            raise SkillValidationError(
                "stored Skill validation failed contract validation"
            ) from exc

    def _dependency_state(
        self, connection: sqlite3.Connection, skill_id: str, version: int
    ) -> Literal["active", "blocked", "revoked"]:
        rows = connection.execute(
            "SELECT state FROM b26_skill_dependencies WHERE skill_id=? AND skill_version=?",
            (skill_id, version),
        ).fetchall()
        states = {str(row["state"]) for row in rows}
        if "revoked" in states:
            return "revoked"
        if "blocked" in states:
            return "blocked"
        return "active"

    def _rollback_versions(self, connection: sqlite3.Connection, skill_id: str) -> tuple[int, ...]:
        rows = connection.execute(
            """
            SELECT DISTINCT skill_version FROM b26_skill_events
            WHERE skill_id=? AND action IN ('skill_publish', 'skill_rollback') AND state='published'
            ORDER BY skill_version
            """,
            (skill_id,),
        ).fetchall()
        return tuple(int(row["skill_version"]) for row in rows)

    def _view(
        self,
        connection: sqlite3.Connection,
        skill_id: str,
        *,
        version: int | None = None,
    ) -> SkillView:
        head = self._stored_head(connection, skill_id)
        target_version = version
        if target_version is None:
            row = connection.execute(
                "SELECT MAX(version) AS version FROM b26_skill_versions WHERE skill_id=?",
                (skill_id,),
            ).fetchone()
            if row is None or row["version"] is None:
                raise SkillNotFoundError(f"generated Skill has no versions: {skill_id}")
            latest_version = int(row["version"])
            # Keep a newer untrusted draft visible while preserving the real
            # publication pointer in ``head``.  This lets the client validate
            # and publish v2 without silently falling back to v1.
            target_version = (
                latest_version
                if head.published_version is None or latest_version > head.published_version
                else head.published_version
            )
        skill_version = self._stored_version(connection, skill_id, int(target_version))
        validation = self._stored_validation(connection, skill_id, skill_version.version)
        dependency_state = self._dependency_state(connection, skill_id, skill_version.version)
        trust_status: SkillTrustStatus = "untrusted_draft"
        if dependency_state == "revoked" or head.state == "revoked":
            trust_status = "revoked"
        elif head.published_version == skill_version.version and head.state == "published":
            trust_status = "core_published"
        elif validation is not None and validation.status == "passed":
            trust_status = "validated"
        return SkillView(
            skill_id=skill_id,
            version=skill_version,
            head=head,
            validation=validation,
            dependency_state=dependency_state,
            rollback_versions=self._rollback_versions(connection, skill_id),
            trust_status=trust_status,
        )

    def _insert_event(
        self,
        connection: sqlite3.Connection,
        *,
        skill_id: str,
        skill_version: int | None,
        action: str,
        state: str,
        head_revision: int,
        source_digest: str | None = None,
        artifact_hash: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO b26_skill_events(
                event_id, skill_id, skill_version, action, state, head_revision,
                source_digest, artifact_hash, detail_json, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("skill_event"),
                skill_id,
                skill_version,
                action,
                state,
                head_revision,
                source_digest,
                artifact_hash,
                _json(dict(detail or {})),
                utc_now().isoformat(),
            ),
        )

    def propose_procedure(
        self,
        project_id: str,
        *,
        content: str,
        sources: Sequence[SourceRef],
        record_id: str | None = None,
        permission_epoch: int | None = None,
        reason: str = "procedure candidate for experience Skill",
    ) -> GovernanceEntry:
        """Create a reviewed B2-5 procedure candidate.

        This is deliberately a proposal-only operation.  The returned
        ``GovernanceEntry`` remains pending until the user calls the existing
        exact Proposal review path; ``skill_draft`` accepts only its published
        ``MemoryVersionRef``.
        """

        _project, installation, binding, scope = self._runtime_identity(project_id)
        current_epoch = int(binding.permission_epoch)
        epoch = current_epoch if permission_epoch is None else permission_epoch
        if epoch != current_epoch:
            raise SkillPermissionError("procedure proposal uses a stale permission epoch")
        if not isinstance(content, str) or not content.strip():
            raise SkillValidationError("procedure content is empty")
        normalized_sources = tuple(sources)
        identities = {
            (source.source_type, source.source_id, source.revision, source.content_digest)
            for source in normalized_sources
        }
        if len(identities) != len(normalized_sources):
            raise SkillValidationError("procedure source references must be unique")
        if not normalized_sources:
            raise SkillValidationError("procedure proposal requires exact sources")
        for source in normalized_sources:
            if (
                source.scope != scope
                or source.permission_epoch != epoch
                or source.availability != "available"
                or not self._source_usable(project_id, source, epoch)
            ):
                raise SkillPermissionError("procedure source is unavailable or unauthorized")

        try:
            previous = self.ledger.list_versions(installation.dataset_id, record_id or "")
            next_version = max(item.ref.version for item in previous) + 1
        except Exception:
            next_version = 1
        actual_record = record_id or new_id("procedure")
        now = utc_now()
        conditions = MemoryConditions(
            commit_ref=None,
            tree_digest=None,
            file_fingerprints={},
            environment_digest=None,
            tool_versions={},
            verified_at=None,
            valid_from=now,
            valid_until=None,
        )
        procedure = MemoryVersion(
            ref=MemoryVersionRef(
                dataset_id=installation.dataset_id,
                record_id=actual_record,
                version=next_version,
                content_digest=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            ),
            owner=DatasetOwner.model_validate(installation.owner),
            kind="project",
            content_type="procedure",
            scope=scope,
            role_ids=(),
            agent_ids=(),
            content=content,
            sources=normalized_sources,
            evidence="inferred",
            sensitivity="internal",
            retention_policy_id="retain-default",
            conditions=conditions,
            recorded_at=now,
        )
        from operant.memory_plugins.governance import GovernanceService

        return GovernanceService(self.manager).propose(
            project_id,
            version=procedure,
            reason=reason,
            extractor_version="user",
        )

    def draft_from_procedure(
        self,
        project_id: str,
        procedure_ref: MemoryVersionRef,
        *,
        name: str,
        description: str,
        skill_id: str | None = None,
        expected_head_revision: int | None = None,
        expected_published_version: int | None = None,
        permission_epoch: int | None = None,
    ) -> SkillView:
        _project, installation, _binding, scope = self._runtime_identity(project_id)
        epoch = (
            self._current_permission_epoch(project_id)
            if permission_epoch is None
            else permission_epoch
        )
        if procedure_ref.dataset_id != installation.dataset_id:
            raise SkillPermissionError("procedure belongs to another dataset")
        procedure = self._load_procedure(
            project_id,
            procedure_ref,
            scope=scope,
            permission_epoch=epoch,
        )
        if _SKILL_NAME_RE.fullmatch(name) is None:
            raise SkillValidationError("Skill name is not a safe lowercase identifier")
        description = _safe_frontmatter_value(description, "Skill description")
        sources = self._expanded_dependency_sources(procedure, permission_epoch=epoch)
        if not all(
            self._source_usable(project_id, source, epoch)
            for source in sources
            if not _is_procedure_dependency(source, procedure)
        ):
            raise SkillPermissionError("procedure dependency source is unavailable or revoked")
        source_digest = _source_digest(procedure, sources)
        content = render_skill_document(
            name=name,
            description=description,
            procedure_text=procedure.content,
        )
        artifact = self._artifact_from_content(content, self.artifact_store)
        resolved_skill_id = skill_id or "skill_" + _digest(
            {"procedure": procedure.ref, "name": name}
        )
        now = utc_now()
        skill_version = SkillVersion(
            skill_id=resolved_skill_id,
            version=1,
            name=name,
            description=description,
            procedure_ref=procedure.ref,
            source_refs=tuple(procedure.sources),
            source_digest=source_digest,
            scope=scope,
            role_ids=tuple(procedure.role_ids),
            agent_ids=tuple(procedure.agent_ids),
            sensitivity=procedure.sensitivity,
            artifact=artifact,
            trust_status="untrusted_draft",
            created_at=now,
        )
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_head_row = connection.execute(
                "SELECT * FROM b26_skill_heads WHERE skill_id=?", (resolved_skill_id,)
            ).fetchone()
            if existing_head_row is not None:
                existing_head = self._stored_head(connection, resolved_skill_id)
                if (
                    expected_head_revision is not None
                    and existing_head.head_revision != expected_head_revision
                ):
                    raise SkillConflictError("Skill head revision conflict")
                if (
                    expected_published_version is not None
                    and existing_head.published_version != expected_published_version
                ):
                    raise SkillConflictError("Skill publication changed while drafting")
                existing_versions = connection.execute(
                    "SELECT version FROM b26_skill_versions WHERE skill_id=? ORDER BY version",
                    (resolved_skill_id,),
                ).fetchall()
                for row in existing_versions:
                    existing = self._stored_version(
                        connection, resolved_skill_id, int(row["version"])
                    )
                    if (
                        existing.source_digest == source_digest
                        and existing.artifact.content_hash == artifact.content_hash
                        and existing.description == description
                    ):
                        return self._view(connection, resolved_skill_id, version=existing.version)
                next_version = max(int(row["version"]) for row in existing_versions) + 1
                skill_version = skill_version.model_copy(update={"version": next_version})
                head_revision = existing_head.head_revision
                head_state = existing_head.state
                connection.execute(
                    """
                    INSERT INTO b26_skill_versions(
                        skill_id, version, name, description, procedure_ref_json,
                        source_refs_json, source_digest, scope_json, role_ids_json,
                        agent_ids_json, sensitivity, artifact_id, artifact_content_hash,
                        artifact_size_bytes, artifact_json, trust_status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    self._version_values(skill_version),
                )
            else:
                if (
                    expected_head_revision not in (None, 0)
                    or expected_published_version is not None
                ):
                    raise SkillConflictError("new Skill head revision must be zero")
                head_revision = 0
                head_state = "draft"
                connection.execute(
                    """
                    INSERT INTO b26_skill_versions(
                        skill_id, version, name, description, procedure_ref_json,
                        source_refs_json, source_digest, scope_json, role_ids_json,
                        agent_ids_json, sensitivity, artifact_id,
                        artifact_content_hash, artifact_size_bytes, artifact_json,
                        trust_status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    self._version_values(skill_version),
                )
                connection.execute(
                    """
                    INSERT INTO b26_skill_heads(
                        skill_id, head_revision, published_version, state,
                        permission_epoch, updated_at
                    ) VALUES (?, 0, NULL, 'draft', ?, ?)
                    """,
                    (resolved_skill_id, epoch, now.isoformat()),
                )
            for dependency in sources:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO b26_skill_dependencies(
                        skill_id, skill_version, source_key, source_json,
                        state, reason, updated_at
                    ) VALUES (?, ?, ?, ?, 'active', NULL, ?)
                    """,
                    (
                        resolved_skill_id,
                        skill_version.version,
                        source_key(dependency),
                        _json(dependency),
                        now.isoformat(),
                    ),
                )
            self._insert_event(
                connection,
                skill_id=resolved_skill_id,
                skill_version=skill_version.version,
                action="skill_draft",
                state=head_state,
                head_revision=head_revision,
                source_digest=source_digest,
                artifact_hash=artifact.content_hash,
            )
            return self._view(connection, resolved_skill_id, version=skill_version.version)

    @staticmethod
    def _version_values(version: SkillVersion) -> tuple[Any, ...]:
        return (
            version.skill_id,
            version.version,
            version.name,
            version.description,
            _json(version.procedure_ref),
            _json(version.source_refs),
            version.source_digest,
            _json(version.scope),
            _json(version.role_ids),
            _json(version.agent_ids),
            version.sensitivity,
            version.artifact.artifact_id,
            version.artifact.content_hash,
            version.artifact.size_bytes,
            _json(version.artifact),
            version.trust_status,
            version.created_at.isoformat(),
        )

    def validate(
        self,
        project_id: str,
        skill_id: str,
        *,
        skill_version: int | None = None,
        expected_head_revision: int | None = None,
        permission_epoch: int | None = None,
    ) -> SkillView:
        _project, _installation, _binding, scope = self._runtime_identity(project_id)
        epoch = (
            self._current_permission_epoch(project_id)
            if permission_epoch is None
            else permission_epoch
        )
        with self.store._connect() as connection:
            if skill_version is None:
                row = connection.execute(
                    "SELECT MAX(version) AS version FROM b26_skill_versions WHERE skill_id=?",
                    (skill_id,),
                ).fetchone()
                if row is None or row["version"] is None:
                    raise SkillNotFoundError("Skill version is unavailable")
                skill_version = int(row["version"])
            version = self._stored_version(connection, skill_id, skill_version)
            if version.scope != scope:
                raise SkillPermissionError("Skill is outside the project scope")
            head = self._stored_head(connection, skill_id)
            if expected_head_revision is not None and head.head_revision != expected_head_revision:
                raise SkillConflictError("Skill head revision conflict")
        status: SkillValidationStatus = "passed"
        issues: list[SkillValidationIssue] = []
        try:
            self.artifact_store.verify(version.artifact.content_hash, version.artifact.size_bytes)
            content = self.artifact_store.read(
                version.artifact.content_hash, version.artifact.size_bytes
            )
            if hashlib.sha256(content).hexdigest() != version.artifact.content_hash:
                raise SkillValidationError("Skill Artifact content digest changed")
            validate_skill_document(
                content,
                expected_name=version.name,
                expected_description=version.description,
            )
            procedure = self._load_procedure(
                project_id,
                version.procedure_ref,
                scope=scope,
                permission_epoch=epoch,
            )
            if procedure.sources != version.source_refs:
                raise SkillConflictError("procedure source references changed")
            expanded = self._expanded_dependency_sources(procedure, permission_epoch=epoch)
            if not all(
                self._source_usable(project_id, source, epoch)
                for source in expanded
                if not _is_procedure_dependency(source, procedure)
            ):
                raise SkillPermissionError("procedure dependency source is unavailable or revoked")
            if _source_digest(procedure, expanded) != version.source_digest:
                raise SkillConflictError("procedure source digest changed")
        except SkillPermissionError as exc:
            status = "blocked"
            issues.append(SkillValidationIssue(code="source_unavailable", message=str(exc)[:500]))
        except (SkillValidationError, SkillConflictError) as exc:
            status = "failed"
            issues.append(SkillValidationIssue(code="validation_failed", message=str(exc)[:500]))
        except Exception:
            status = "failed"
            issues.append(
                SkillValidationIssue(
                    code="artifact_unavailable",
                    message="Skill Artifact or procedure could not be verified",
                )
            )
        validation = SkillValidation(
            validation_id=new_id("skill_validation"),
            skill_id=skill_id,
            skill_version=skill_version,
            status=status,
            validator_version=_VALIDATOR_VERSION,
            artifact_hash=version.artifact.content_hash,
            source_digest=version.source_digest,
            issues=tuple(issues),
            checked_at=utc_now(),
        )
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._stored_version(connection, skill_id, skill_version)
            if current.artifact.content_hash != validation.artifact_hash:
                raise SkillConflictError("Skill version changed during validation")
            current_head = self._stored_head(connection, skill_id)
            if (
                expected_head_revision is not None
                and current_head.head_revision != expected_head_revision
            ):
                raise SkillConflictError("Skill head revision changed during validation")
            head = current_head
            connection.execute(
                """
                INSERT INTO b26_skill_validations(
                    validation_id, skill_id, skill_version, status,
                    validator_version, artifact_hash, source_digest, issues_json, checked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    validation.validation_id,
                    validation.skill_id,
                    validation.skill_version,
                    validation.status,
                    validation.validator_version,
                    validation.artifact_hash,
                    validation.source_digest,
                    _json(validation.issues),
                    validation.checked_at.isoformat(),
                ),
            )
            self._insert_event(
                connection,
                skill_id=skill_id,
                skill_version=skill_version,
                action="skill_validate",
                state=head.state,
                head_revision=head.head_revision,
                source_digest=version.source_digest,
                artifact_hash=version.artifact.content_hash,
                detail={"status": status, "issue_count": len(issues)},
            )
            return self._view(connection, skill_id, version=skill_version)

    def _require_publishable(
        self,
        project_id: str,
        version: SkillVersion,
        validation: SkillValidation | None,
        *,
        permission_epoch: int,
    ) -> None:
        if validation is None or validation.status != "passed":
            raise SkillConflictError("Skill must pass validation before publication")
        if (
            validation.skill_version != version.version
            or validation.artifact_hash != version.artifact.content_hash
            or validation.source_digest != version.source_digest
        ):
            raise SkillConflictError("Skill validation is stale for this immutable version")
        _project, _installation, _binding, scope = self._runtime_identity(project_id)
        if version.scope != scope:
            raise SkillPermissionError("Skill is outside the project scope")
        self._load_procedure(
            project_id,
            version.procedure_ref,
            scope=scope,
            permission_epoch=permission_epoch,
        )

    def publish(
        self,
        project_id: str,
        skill_id: str,
        *,
        skill_version: int | None = None,
        expected_head_revision: int | None = None,
        permission_epoch: int | None = None,
    ) -> SkillView:
        epoch = (
            self._current_permission_epoch(project_id)
            if permission_epoch is None
            else permission_epoch
        )
        with self.store._connect() as connection:
            if skill_version is None:
                row = connection.execute(
                    "SELECT MAX(version) AS version FROM b26_skill_versions WHERE skill_id=?",
                    (skill_id,),
                ).fetchone()
                if row is None or row["version"] is None:
                    raise SkillNotFoundError("Skill version is unavailable")
                skill_version = int(row["version"])
            version = self._stored_version(connection, skill_id, skill_version)
            validation = self._stored_validation(connection, skill_id, skill_version)
        self._require_publishable(project_id, version, validation, permission_epoch=epoch)
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current_head = self._stored_head(connection, skill_id)
            if (
                expected_head_revision is not None
                and current_head.head_revision != expected_head_revision
            ):
                raise SkillConflictError("Skill head revision conflict")
            if (
                current_head.state == "published"
                and current_head.published_version == skill_version
            ):
                return self._view(connection, skill_id, version=skill_version)
            dependency_state = self._dependency_state(connection, skill_id, skill_version)
            if dependency_state != "active":
                raise SkillPermissionError("Skill source dependency is no longer active")
            next_head = current_head.model_copy(
                update={
                    "head_revision": current_head.head_revision + 1,
                    "published_version": skill_version,
                    "state": "published",
                    "permission_epoch": epoch,
                    "updated_at": utc_now(),
                }
            )
            changed = connection.execute(
                """
                UPDATE b26_skill_heads
                SET head_revision=?, published_version=?, state=?, permission_epoch=?, updated_at=?
                WHERE skill_id=? AND head_revision=?
                """,
                (
                    next_head.head_revision,
                    next_head.published_version,
                    next_head.state,
                    next_head.permission_epoch,
                    next_head.updated_at.isoformat(),
                    skill_id,
                    current_head.head_revision,
                ),
            ).rowcount
            if changed != 1:
                raise SkillConflictError("Skill head changed during publication")
            self._insert_event(
                connection,
                skill_id=skill_id,
                skill_version=skill_version,
                action="skill_publish",
                state="published",
                head_revision=next_head.head_revision,
                source_digest=version.source_digest,
                artifact_hash=version.artifact.content_hash,
            )
            return self._view(connection, skill_id, version=skill_version)

    def disable(
        self,
        project_id: str,
        skill_id: str,
        *,
        skill_version: int | None = None,
        expected_head_revision: int | None = None,
        permission_epoch: int | None = None,
        reason: str | None = None,
    ) -> SkillView:
        _project, _installation, binding, scope = self._runtime_identity(project_id, enabled=False)
        current_epoch = int(binding.permission_epoch)
        if permission_epoch is not None and permission_epoch != current_epoch:
            raise SkillPermissionError("Skill disable uses a stale permission epoch")
        if reason is not None and (not reason.strip() or len(reason) > 500):
            raise SkillValidationError("disable reason is empty or too long")
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            head = self._stored_head(connection, skill_id)
            version = self._stored_version(connection, skill_id, head.published_version or 1)
            if expected_head_revision is not None and head.head_revision != expected_head_revision:
                raise SkillConflictError("Skill head revision conflict")
            if skill_version is not None and head.published_version != skill_version:
                raise SkillConflictError("Skill version is not the current publication")
            if version.scope != scope:
                raise SkillPermissionError("Skill is outside the project scope")
            if head.state == "disabled":
                return self._view(connection, skill_id)
            if head.published_version is None:
                raise SkillConflictError("a draft Skill cannot be disabled")
            next_head = head.model_copy(
                update={
                    "head_revision": head.head_revision + 1,
                    "state": "disabled",
                    "updated_at": utc_now(),
                }
            )
            changed = connection.execute(
                """
                UPDATE b26_skill_heads SET head_revision=?, state=?, updated_at=?
                WHERE skill_id=? AND head_revision=?
                """,
                (
                    next_head.head_revision,
                    next_head.state,
                    next_head.updated_at.isoformat(),
                    skill_id,
                    head.head_revision,
                ),
            ).rowcount
            if changed != 1:
                raise SkillConflictError("Skill head changed during disable")
            self._insert_event(
                connection,
                skill_id=skill_id,
                skill_version=head.published_version,
                action="skill_disable",
                state="disabled",
                head_revision=next_head.head_revision,
                source_digest=version.source_digest,
                artifact_hash=version.artifact.content_hash,
                detail={"reason": reason or "explicit user disable"},
            )
            return self._view(connection, skill_id)

    def rollback(
        self,
        project_id: str,
        skill_id: str,
        target_version: int,
        *,
        skill_version: int | None = None,
        expected_head_revision: int | None = None,
        permission_epoch: int | None = None,
    ) -> SkillView:
        epoch = (
            self._current_permission_epoch(project_id)
            if permission_epoch is None
            else permission_epoch
        )
        with self.store._connect() as connection:
            target = self._stored_version(connection, skill_id, target_version)
            validation = self._stored_validation(connection, skill_id, target_version)
            published_versions = self._rollback_versions(connection, skill_id)
        if target_version not in published_versions:
            raise SkillConflictError("rollback target was never published")
        self._require_publishable(project_id, target, validation, permission_epoch=epoch)
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current_head = self._stored_head(connection, skill_id)
            if (
                expected_head_revision is not None
                and current_head.head_revision != expected_head_revision
            ):
                raise SkillConflictError("Skill head revision conflict")
            if skill_version is not None and current_head.published_version != skill_version:
                raise SkillConflictError("Skill version is not the current publication")
            if (
                current_head.published_version == target_version
                and current_head.state == "published"
            ):
                return self._view(connection, skill_id, version=target_version)
            if self._dependency_state(connection, skill_id, target_version) != "active":
                raise SkillPermissionError("rollback target has a revoked source")
            next_head = current_head.model_copy(
                update={
                    "head_revision": current_head.head_revision + 1,
                    "published_version": target_version,
                    "state": "published",
                    "permission_epoch": epoch,
                    "updated_at": utc_now(),
                }
            )
            changed = connection.execute(
                """
                UPDATE b26_skill_heads
                SET head_revision=?, published_version=?, state=?, permission_epoch=?, updated_at=?
                WHERE skill_id=? AND head_revision=?
                """,
                (
                    next_head.head_revision,
                    target_version,
                    next_head.state,
                    next_head.permission_epoch,
                    next_head.updated_at.isoformat(),
                    skill_id,
                    current_head.head_revision,
                ),
            ).rowcount
            if changed != 1:
                raise SkillConflictError("Skill head changed during rollback")
            self._insert_event(
                connection,
                skill_id=skill_id,
                skill_version=target_version,
                action="skill_rollback",
                state="published",
                head_revision=next_head.head_revision,
                source_digest=target.source_digest,
                artifact_hash=target.artifact.content_hash,
            )
            return self._view(connection, skill_id, version=target_version)

    def propagate_source_revocation(
        self,
        project_id: str,
        source: SourceRef | str,
        *,
        reason: str = "source revoked",
    ) -> tuple[str, ...]:
        _project, installation, _binding, _scope = self._runtime_identity(project_id, enabled=False)
        if not reason.strip() or len(reason) > 500:
            raise SkillValidationError("source revocation reason is empty or too long")
        key = source_key(source) if isinstance(source, SourceRef) else source
        if not isinstance(key, str) or not key.strip():
            raise SkillValidationError("source revocation key is empty")
        affected: list[str] = []
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT skill_id, skill_version FROM b26_skill_dependencies
                WHERE source_key=? AND state != 'revoked'
                ORDER BY skill_id, skill_version
                """,
                (key,),
            ).fetchall()
            for row in rows:
                skill_id = str(row["skill_id"])
                version = int(row["skill_version"])
                connection.execute(
                    """
                    UPDATE b26_skill_dependencies
                    SET state='revoked', reason=?, updated_at=?
                    WHERE skill_id=? AND skill_version=? AND source_key=?
                    """,
                    (reason, utc_now().isoformat(), skill_id, version, key),
                )
                identifier = f"{skill_id}@{version}"
                if identifier not in affected:
                    affected.append(identifier)
                head = self._stored_head(connection, skill_id)
                if head.published_version != version or head.state == "revoked":
                    continue
                next_head = head.model_copy(
                    update={
                        "head_revision": head.head_revision + 1,
                        "state": "revoked",
                        "updated_at": utc_now(),
                    }
                )
                connection.execute(
                    """
                    UPDATE b26_skill_heads SET head_revision=?, state=?, updated_at=?
                    WHERE skill_id=? AND head_revision=?
                    """,
                    (
                        next_head.head_revision,
                        next_head.state,
                        next_head.updated_at.isoformat(),
                        skill_id,
                        head.head_revision,
                    ),
                )
                version_row = self._stored_version(connection, skill_id, version)
                self._insert_event(
                    connection,
                    skill_id=skill_id,
                    skill_version=version,
                    action="skill_source_revoke",
                    state="revoked",
                    head_revision=next_head.head_revision,
                    source_digest=version_row.source_digest,
                    artifact_hash=version_row.artifact.content_hash,
                    detail={"source_key": key, "reason": reason},
                )
        del installation  # Keep the runtime identity check explicit for callers.
        return tuple(affected)

    def _snapshot_version(self, snapshot: SkillRunSnapshot) -> SkillVersion:
        with self.store._connect() as connection:
            return self._stored_version(connection, snapshot.skill_id, snapshot.skill_version)

    def snapshot_for_run(
        self,
        project_id: str,
        *,
        session_id: str,
        role_id: str,
        agent_id: str,
        model_profile_id: str,
        permission_epoch: int | None = None,
        skill_ids: Sequence[str] | None = None,
        max_skills: int = 16,
    ) -> tuple[SkillRunSnapshot, ...]:
        if not 1 <= max_skills <= 64:
            raise SkillValidationError("max_skills is outside the configured bound")
        _project, _installation, binding, scope = self._runtime_identity(project_id)
        current_epoch = int(binding.permission_epoch)
        epoch = current_epoch if permission_epoch is None else permission_epoch
        if epoch != current_epoch:
            raise SkillPermissionError("Skill snapshot uses a stale permission epoch")
        allowed = set(skill_ids) if skill_ids is not None else None
        snapshots: list[SkillRunSnapshot] = []
        with self.store._connect() as connection:
            rows = connection.execute(
                "SELECT skill_id FROM b26_skill_heads WHERE state='published' ORDER BY skill_id"
            ).fetchall()
            for row in rows:
                if len(snapshots) >= max_skills:
                    break
                skill_id = str(row["skill_id"])
                if allowed is not None and skill_id not in allowed:
                    continue
                head = self._stored_head(connection, skill_id)
                if head.published_version is None:
                    continue
                version = self._stored_version(connection, skill_id, head.published_version)
                validation = self._stored_validation(connection, skill_id, version.version)
                if (
                    version.scope != scope
                    or (version.role_ids and role_id not in version.role_ids)
                    or (version.agent_ids and agent_id not in version.agent_ids)
                    or validation is None
                    or validation.status != "passed"
                    or head.permission_epoch != epoch
                    or self._dependency_state(connection, skill_id, version.version) != "active"
                ):
                    continue
                content = self.artifact_store.read(
                    version.artifact.content_hash,
                    version.artifact.size_bytes,
                )
                snapshot = SkillRunSnapshot(
                    skill_id=skill_id,
                    skill_version=version.version,
                    session_id=session_id,
                    role_id=role_id,
                    agent_id=agent_id,
                    model_profile_id=model_profile_id,
                    artifact_id=version.artifact.artifact_id,
                    artifact_hash=version.artifact.content_hash,
                    source_digest=version.source_digest,
                    source_refs=tuple(version.source_refs),
                    scope=version.scope,
                    permission_epoch=epoch,
                    content=content.decode("utf-8"),
                )
                snapshots.append(snapshot)
        for snapshot in snapshots:
            self.assert_snapshot_usable(
                project_id,
                snapshot,
                session_id=session_id,
                role_id=role_id,
                agent_id=agent_id,
                model_profile_id=model_profile_id,
                permission_epoch=epoch,
            )
        return tuple(snapshots)

    def assert_snapshot_usable(
        self,
        project_id: str,
        snapshot: SkillRunSnapshot,
        *,
        session_id: str | None = None,
        role_id: str | None = None,
        agent_id: str | None = None,
        model_profile_id: str | None = None,
        permission_epoch: int | None = None,
    ) -> None:
        _project, _installation, binding, scope = self._runtime_identity(project_id)
        current_epoch = int(binding.permission_epoch)
        epoch = current_epoch if permission_epoch is None else permission_epoch
        version = self._snapshot_version(snapshot)
        if (
            (session_id is not None and snapshot.session_id != session_id)
            or (role_id is not None and snapshot.role_id != role_id)
            or (agent_id is not None and snapshot.agent_id != agent_id)
            or (model_profile_id is not None and snapshot.model_profile_id != model_profile_id)
            or snapshot.role_id == ""
            or snapshot.agent_id == ""
            or snapshot.model_profile_id == ""
            or snapshot.session_id == ""
            or version.artifact.artifact_id != snapshot.artifact_id
            or version.artifact.content_hash != snapshot.artifact_hash
            or version.source_digest != snapshot.source_digest
            or tuple(version.source_refs) != tuple(snapshot.source_refs)
            or version.scope != scope
            or snapshot.scope != scope
            or snapshot.permission_epoch != epoch
            or epoch != current_epoch
            or (version.role_ids and snapshot.role_id not in version.role_ids)
            or (version.agent_ids and snapshot.agent_id not in version.agent_ids)
        ):
            raise SkillPermissionError("Skill run snapshot identity is stale")
        with self.store._connect() as connection:
            head = self._stored_head(connection, snapshot.skill_id)
            validation = self._stored_validation(
                connection, snapshot.skill_id, snapshot.skill_version
            )
            if head.state in {"disabled", "revoked", "blocked"}:
                raise SkillPermissionError("Skill is disabled or source-revoked")
            if validation is None or validation.status != "passed":
                raise SkillPermissionError("Skill run snapshot lacks current validation")
            if (
                self._dependency_state(connection, snapshot.skill_id, snapshot.skill_version)
                != "active"
            ):
                raise SkillPermissionError("Skill source dependency is revoked")
        try:
            self.artifact_store.verify(version.artifact.content_hash, version.artifact.size_bytes)
            content = snapshot.content.encode("utf-8")
            if hashlib.sha256(content).hexdigest() != version.artifact.content_hash:
                raise SkillValidationError("Skill run snapshot content digest changed")
            validate_skill_document(
                content,
                expected_name=version.name,
                expected_description=version.description,
            )
        except Exception as exc:
            raise SkillPermissionError("Skill Artifact integrity check failed") from exc
        try:
            from operant.memory_plugins.governance import GovernanceService

            if not GovernanceService(self.manager).version_dependencies_valid(
                version.procedure_ref,
                project_id=project_id,
            ):
                raise SkillPermissionError("procedure source dependency is revoked")
            procedure = self.ledger.get_version(
                version.procedure_ref.dataset_id,
                version.procedure_ref.record_id,
                version.procedure_ref.version,
            )
            if (
                procedure.content_type != "procedure"
                or procedure.sources != version.source_refs
                or _source_digest(
                    procedure,
                    self._expanded_dependency_sources(procedure, permission_epoch=epoch),
                )
                != version.source_digest
            ):
                raise SkillPermissionError("procedure provenance changed")
            expanded = self._expanded_dependency_sources(procedure, permission_epoch=epoch)
            if not all(
                self._source_usable(project_id, source, epoch)
                for source in expanded
                if not _is_procedure_dependency(source, procedure)
            ):
                raise SkillPermissionError("procedure dependency source is unavailable or revoked")
        except SkillPermissionError:
            raise
        except Exception as exc:
            raise SkillPermissionError("procedure source dependency is unavailable") from exc
        for source in snapshot.source_refs:
            if not self._source_usable(project_id, source, epoch):
                raise SkillPermissionError("Skill source permission or availability changed")

    def state(self, project_id: str) -> SkillState:
        project = self._project(project_id)
        scope = self.manager._scope(project)
        try:
            _project, _installation, _binding, _scope = self._runtime_identity(project_id)
            available = True
        except SkillPermissionError:
            available = False
        skills: list[SkillView] = []
        with self.store._connect() as connection:
            rows = connection.execute(
                "SELECT skill_id FROM b26_skill_heads ORDER BY skill_id"
            ).fetchall()
            for row in rows:
                view = self._view(connection, str(row["skill_id"]))
                if view.version.scope == scope:
                    if not available:
                        view = view.model_copy(update={"dependency_state": "blocked"})
                    skills.append(view)
            cursor_row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS sequence FROM b26_skill_events"
            ).fetchone()
            cursor = str(cursor_row["sequence"] if cursor_row is not None else 0)
        return SkillState(project_id=project_id, skills=tuple(skills), cursor=cursor)

    async def execute(self, command: SkillCommand) -> list[str]:
        """Execute one typed command and return stable affected IDs."""

        epoch = command.permission_epoch
        if command.action == "procedure_propose":
            assert command.content is not None
            proposal = self.propose_procedure(
                command.project_id,
                content=command.content,
                sources=command.sources,
                record_id=command.record_id,
                permission_epoch=epoch,
                reason=command.reason or "procedure candidate for experience Skill",
            )
            return [proposal.proposal.proposal_id]
        if command.action == "skill_draft":
            assert command.procedure_ref is not None
            assert command.name is not None and command.description is not None
            view = self.draft_from_procedure(
                command.project_id,
                command.procedure_ref,
                name=command.name,
                description=command.description,
                skill_id=command.skill_id,
                expected_head_revision=command.expected_head_revision,
                expected_published_version=command.skill_version,
                permission_epoch=epoch,
            )
            return [f"{view.skill_id}@{view.version.version}"]
        assert command.skill_id is not None
        if command.action == "skill_validate":
            view = self.validate(
                command.project_id,
                command.skill_id,
                skill_version=command.skill_version,
                expected_head_revision=command.expected_head_revision,
                permission_epoch=epoch,
            )
        elif command.action == "skill_publish":
            view = self.publish(
                command.project_id,
                command.skill_id,
                skill_version=command.skill_version,
                expected_head_revision=command.expected_head_revision,
                permission_epoch=epoch,
            )
        elif command.action == "skill_disable":
            view = self.disable(
                command.project_id,
                command.skill_id,
                skill_version=command.skill_version,
                expected_head_revision=command.expected_head_revision,
                permission_epoch=epoch,
                reason=command.reason,
            )
        elif command.action == "skill_rollback":
            assert command.rollback_to_version is not None
            view = self.rollback(
                command.project_id,
                command.skill_id,
                command.rollback_to_version,
                skill_version=command.skill_version,
                expected_head_revision=command.expected_head_revision,
                permission_epoch=epoch,
            )
        else:  # pragma: no cover - SkillCommand's Literal is exhaustive.
            raise SkillValidationError("unsupported Skill command")
        return [f"{view.skill_id}@{view.version.version}"]


__all__ = [
    "ExperienceSkillService",
    "SkillConflictError",
    "SkillError",
    "SkillNotFoundError",
    "SkillPermissionError",
    "SkillValidationError",
    "render_skill_document",
    "validate_skill_document",
]
