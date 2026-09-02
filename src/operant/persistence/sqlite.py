from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from operant.domain.actions import (
    ApprovalAuditEvent,
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    CommandExecution,
    CommandExecutionStatus,
    ToolActionReceipt,
    ToolActionReceiptStatus,
)
from operant.domain.commands import (
    BTWSidecarEvent,
    BTWSidecarRun,
    BTWSidecarStatus,
    ContextBaseline,
    ContextBaselineOperation,
    Phase1DCommandAuditEvent,
    ReviewRun,
    ReviewRunStatus,
    SlashCommandKind,
    WorkspaceInitialization,
)
from operant.domain.context import (
    Compaction,
    CompactionSourceType,
    CompactionSummary,
    ContextReferenceType,
    ContextRevision,
    ContextSourceRef,
    ContextSourceType,
    ContextWatermark,
    ContextWatermarkState,
    PromptBlock,
    PromptBlockType,
    PromptLayout,
    ReferenceBinding,
    ToolResultStub,
    compaction_coverage_hash,
)
from operant.domain.evaluation import (
    ArtifactWorkspace,
    EvaluationCase,
    EvaluationMetrics,
    EvaluationResult,
    EvaluationResultStatus,
    EvaluationRun,
    EvaluationRunEvent,
    EvaluationRunStatus,
    EvaluationSuite,
    EvaluationSuiteStatus,
    EvaluationVariant,
    aggregate_evaluation_results,
    assert_evaluation_result_contract,
    interrupted_evaluation_failure_analysis,
)
from operant.domain.memory import (
    Memory,
    MemoryKind,
    MemorySource,
    MemoryStatus,
    parse_memory_scope,
)
from operant.domain.messages import Message, ToolDefinition
from operant.domain.models import (
    AgentInstance,
    AgentStatus,
    Event,
    ModelProfile,
    RolePreset,
    RoleSnapshot,
    RoleStatus,
    Session,
    SnapshotOverrides,
    new_id,
    utc_now,
)
from operant.domain.threads import (
    ApprovalLinkPayload,
    Artifact,
    ArtifactRefPayload,
    ArtifactRepairResult,
    ArtifactRetentionState,
    ArtifactSourceRef,
    ArtifactSourceType,
    CacheObservation,
    ConversationThread,
    Item,
    RetentionPolicy,
    SteeringPayload,
    ThreadLegacyRef,
    ThreadStatus,
    ToolCallPayload,
    ToolResultRefPayload,
    Turn,
)
from operant.domain.workflow import WorkflowRun, WorkflowRunEvent, WorkflowRunStatus


def _sha256_text(value: Any) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _thread_item_refs_sha256(value: Any) -> str:
    """Hash the frozen, ordered THREAD_ITEMS evidence representation."""

    try:
        refs = json.loads(str(value))
        if not isinstance(refs, list):
            return ""
        digest = hashlib.sha256()
        for ref in refs:
            if not isinstance(ref, dict):
                return ""
            digest.update(
                json.dumps(
                    {
                        "id": ref["source_id"],
                        "cursor": ref["cursor"],
                        "content_hash": ref["content_hash"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            digest.update(b"\n")
        return digest.hexdigest()
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return ""


def _memory_scope_allows(scope: Any, kind: Any) -> int:
    """SQLite predicate for the immutable Session memory-scope snapshot."""

    try:
        parsed_scope = parse_memory_scope(str(scope or ""))
        return int(parsed_scope.can_read(MemoryKind(str(kind))))
    except (TypeError, ValueError):
        return 0


class NotFoundError(LookupError):
    pass


class ConflictError(ValueError):
    pass


class MigrationError(RuntimeError):
    pass


class IdempotencyConflictError(ConflictError):
    pass


class ActionOutcomeUnknownError(ConflictError):
    pass


MigrationStep = Callable[[sqlite3.Connection], None]


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    manifest: str
    upgrade: MigrationStep
    downgrade: MigrationStep | None = None
    frozen_checksum: str | None = None

    @property
    def checksum(self) -> str:
        if self.frozen_checksum is not None:
            return self.frozen_checksum
        parts = [
            str(self.version),
            self.name,
            self.manifest,
        ]
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SessionRunLease:
    session_id: str
    lease_token: str
    owner_id: str
    generation: int
    workflow_run_id: str | None
    agent_id: str | None
    cancel_requested: bool
    acquired_at: datetime
    renewed_at: datetime
    expires_at: datetime
    released_at: datetime | None


@dataclass(frozen=True)
class WorkflowExecutionLease:
    workflow_run_id: str
    lease_token: str
    owner_id: str
    generation: int
    acquired_at: datetime
    renewed_at: datetime
    expires_at: datetime
    released_at: datetime | None


class SQLiteStore:
    _FROZEN_MANIFEST_SHA256 = {
        1: "9efa030568ef8f28749732f82f0023a548d4ff3af708f86be9f8d3a70037ef18",
        2: "1c1d79405a9f422aca4a84a9bd5e45b1647cb16d0c8b496f906932272fd05e98",
        3: "2ac49a0e18c49ccfbfce434425bb7dd09f39710f190b635d3908af96512d9b34",
        4: "dd860b7b4b448ab4296c1e7803a0fcb90a1e5f27015066916cf871e455373f1c",
        5: "dc6ce273aa869446a520af09fb19335369dab273ffd38789d38298356e5d9df6",
        6: "e12f7993df336c97f2a97532615abfcda457bfba4b10d902223634417e59d373",
        7: "b8516d3a7deec9a93867c45f323992238b17968829001c6af2cf61831fe70df4",
        8: "f3295d7911214ce19f2a6dc7fda63e21eebfe79c7cb4b3a16934ef40297da11b",
    }
    _FROZEN_MIGRATION_CHECKSUMS = {
        1: "08c9d964cf48e432baa70c5730e09577c8fd3c3da32ded12a1d06eb6d4af82c9",
        2: "f560a54b3361b717b8b0118efeb277b9869d66aa4528eb40015cbe571d9a9eef",
        3: "9be47a848c184b5f1ab81540abfc764dcae2a4b054ff4128d3ed153635d5f709",
        4: "7a787a9ce4262293dfd5a0ad524f7f50c245722abef763a64ea82a2eaed1fc14",
        5: "ec6dad28422980314a01fa56a1a2d28e1b2bee4744eb76d28240124a4a112490",
        6: "5430fb415059679846e3f0c18a3b6c998573a053b81c1b4ce4a67719f6f60f66",
        7: "15496ba9e4cde4e1dd622abdc141ca2dc63f5b155c3b2eb57a24472c9df3e06b",
        8: "bfd4f8367d6232d39b1fd9e9c916cc6de95fcfb8c89dfedd13d22f3da70b70e0",
    }
    _TWO_STEP_PREVIEW_HISTORY = (
        (
            1,
            "legacy_baseline",
            "b715a4bd390840330393e393212d657bbd141d0e78473dba003c87956f84970f",
        ),
        (
            2,
            "m0_commands_approvals",
            "0c97d5242ecde4446a07c12234aa7fe81bcff7c416da4866601bc061f0779212",
        ),
    )
    _THREE_STEP_PREVIEW_HISTORY = (
        (
            1,
            "week1_base",
            "6c83c1031cc78f8e78cacdf4c64a527ec9f5713ef61d8931fb53099d87d82e29",
        ),
        (
            2,
            "week3_week4_features",
            "9d93944346d425c634cbaae0a9eba31cd0f5553ba6bd3fbddb1c8744fc8e3b5f",
        ),
        (
            3,
            "m0_commands_approvals",
            "f059816854c8e77af418c4acdde76a61e712b5468d34ae9190571ec5a5c9fe44",
        ),
    )
    _MANIFEST_REWORK_PREVIEW_HISTORY = (
        (
            1,
            "week1_base",
            "f984b38c9c5d6ba792c55fc169901fc64f012d8118b9525c70958ce1aff827a8",
        ),
        (
            2,
            "week3_week4_features",
            "e60372ecc8429706c2f9b51035483cd6a5eab68777b8b2b88b6f0f978ec7a2a9",
        ),
        (
            3,
            "m0_commands_approvals",
            "20a5e2b308b838e7af9f054fb16e58f8d6ca3b505ec8901d61a2f7b3b7e0c4d2",
        ),
    )
    _PHASE1A_V5_PREVIEW_HISTORY = (
        (1, "week1_base", _FROZEN_MIGRATION_CHECKSUMS[1]),
        (2, "week3_week4_features", _FROZEN_MIGRATION_CHECKSUMS[2]),
        (3, "m0_commands_approvals", _FROZEN_MIGRATION_CHECKSUMS[3]),
        (4, "phase0_session_run_leases", _FROZEN_MIGRATION_CHECKSUMS[4]),
        (
            5,
            "phase1a_thread_artifact_history",
            "863fdbdb5d3030fecab446f0469faf2424d43f87b3f63c5913017e36b1056528",
        ),
    )
    _PHASE1D_V8_PREVIEW_HISTORY = (
        (1, "week1_base", _FROZEN_MIGRATION_CHECKSUMS[1]),
        (2, "week3_week4_features", _FROZEN_MIGRATION_CHECKSUMS[2]),
        (3, "m0_commands_approvals", _FROZEN_MIGRATION_CHECKSUMS[3]),
        (4, "phase0_session_run_leases", _FROZEN_MIGRATION_CHECKSUMS[4]),
        (5, "phase1a_thread_artifact_history", _FROZEN_MIGRATION_CHECKSUMS[5]),
        (6, "phase1b_context_composer", _FROZEN_MIGRATION_CHECKSUMS[6]),
        (7, "phase1c_artifact_retention_cache_observation", _FROZEN_MIGRATION_CHECKSUMS[7]),
        (
            8,
            "phase1d_command_context_sidecar",
            "eeda5ba8d0fb88ff9bfdfbcde1cd51d8c3b1f094b1f8bf073c9fefe6ea8f6080",
        ),
    )
    _PHASE1D_V8_CONTEXT_GUARD_PREVIEW_HISTORY = (
        (1, "week1_base", _FROZEN_MIGRATION_CHECKSUMS[1]),
        (2, "week3_week4_features", _FROZEN_MIGRATION_CHECKSUMS[2]),
        (3, "m0_commands_approvals", _FROZEN_MIGRATION_CHECKSUMS[3]),
        (4, "phase0_session_run_leases", _FROZEN_MIGRATION_CHECKSUMS[4]),
        (5, "phase1a_thread_artifact_history", _FROZEN_MIGRATION_CHECKSUMS[5]),
        (6, "phase1b_context_composer", _FROZEN_MIGRATION_CHECKSUMS[6]),
        (7, "phase1c_artifact_retention_cache_observation", _FROZEN_MIGRATION_CHECKSUMS[7]),
        (
            8,
            "phase1d_command_context_sidecar",
            "03758b816b16a2fcd42702f46686eea494c8b51b5f21591b27a400468b0bd0b2",
        ),
    )
    _PHASE1D_V8_RUN_SCOPE_PREVIEW_HISTORY = (
        (1, "week1_base", _FROZEN_MIGRATION_CHECKSUMS[1]),
        (2, "week3_week4_features", _FROZEN_MIGRATION_CHECKSUMS[2]),
        (3, "m0_commands_approvals", _FROZEN_MIGRATION_CHECKSUMS[3]),
        (4, "phase0_session_run_leases", _FROZEN_MIGRATION_CHECKSUMS[4]),
        (5, "phase1a_thread_artifact_history", _FROZEN_MIGRATION_CHECKSUMS[5]),
        (6, "phase1b_context_composer", _FROZEN_MIGRATION_CHECKSUMS[6]),
        (7, "phase1c_artifact_retention_cache_observation", _FROZEN_MIGRATION_CHECKSUMS[7]),
        (
            8,
            "phase1d_command_context_sidecar",
            "91a4f2e484d6549d67be62530c2864f3f3c6cd84821cd0e0817a452410fa9550",
        ),
    )

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.create_function(
            "sha256_text",
            1,
            _sha256_text,
            deterministic=True,
        )
        connection.create_function(
            "thread_item_refs_sha256",
            1,
            _thread_item_refs_sha256,
            deterministic=True,
        )
        connection.create_function(
            "memory_scope_allows",
            2,
            _memory_scope_allows,
            deterministic=True,
        )
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._interrupt_running_workflows(connection)
            self._interrupt_running_evaluations(connection)
            self._reconcile_in_progress_commands(connection)
            self._reconcile_in_progress_tool_actions(connection)
            self._expire_pending_approvals(connection)
            self._interrupt_running_phase1d_calls(connection)

    def migrate(
        self,
        target_version: int | None = None,
        *,
        _isolated_rollback: bool = False,
    ) -> int:
        """Move the schema transactionally to a known version.

        Version 1 is the immutable legacy baseline. Downgrades below it are
        deliberately unsupported because doing so would remove all user data.
        """

        self.path.parent.mkdir(parents=True, exist_ok=True)
        migrations = self._migrations()
        latest = migrations[-1].version
        target = latest if target_version is None else target_version
        if target < 1 or target > latest:
            raise MigrationError(f"target schema version must be between 1 and {latest}")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_migration_table_on_connection(connection)
            self._adopt_unversioned_legacy_history(connection, migrations)
            self._adopt_known_preview_history(connection, migrations)
            applied = self._validated_applied_migrations(migrations, connection=connection)
            current = max(applied, default=0)
            if target < current and not _isolated_rollback:
                raise MigrationError(
                    "downgrades require rollback(target_version, isolated=True) "
                    "on an isolated database"
                )
            if target > current:
                for migration in migrations:
                    if current < migration.version <= target:
                        self._apply_migration(connection, migration)
            elif target < current:
                for migration in reversed(migrations):
                    if target < migration.version <= current:
                        self._rollback_migration(connection, migration)
            applied_after = self._validated_applied_migrations(
                migrations,
                connection=connection,
            )
            actual = max(applied_after, default=0)
            if actual != target:
                raise MigrationError(
                    f"schema migration ended at version {actual}, expected {target}"
                )
            return actual

    def rollback(self, target_version: int, *, isolated: bool = False) -> int:
        """Downgrade only an explicitly isolated database with empty M0 audit tables."""

        if not isolated:
            raise MigrationError("rollback is allowed only for an explicitly isolated database")
        return self.migrate(target_version, _isolated_rollback=True)

    def schema_version(self) -> int:
        self._ensure_migration_table()
        migrations = self._migrations()
        applied = self._validated_applied_migrations(migrations)
        return max(applied, default=0)

    def list_applied_migrations(self) -> list[dict[str, str | int]]:
        self._ensure_migration_table()
        migrations = self._migrations()
        self._validated_applied_migrations(migrations)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT version, name, checksum, applied_at
                FROM schema_migrations ORDER BY version
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def _migrations(self) -> tuple[Migration, ...]:
        def build(
            version: int,
            name: str,
            upgrade: MigrationStep,
            downgrade: MigrationStep | None = None,
        ) -> Migration:
            manifest = self._migration_manifest(version)
            manifest_sha256 = hashlib.sha256(manifest.encode("utf-8")).hexdigest()
            if manifest_sha256 != self._FROZEN_MANIFEST_SHA256[version]:
                raise MigrationError(
                    f"migration {version} schema manifest changed; add a new migration version"
                )
            calculated_checksum = hashlib.sha256(
                "\n".join((str(version), name, manifest)).encode("utf-8")
            ).hexdigest()
            frozen_checksum = self._FROZEN_MIGRATION_CHECKSUMS[version]
            if calculated_checksum != frozen_checksum:
                raise MigrationError(
                    f"migration {version} frozen checksum does not match its manifest"
                )
            return Migration(
                version,
                name,
                manifest,
                upgrade,
                downgrade,
                frozen_checksum,
            )

        return (
            build(
                1,
                "week1_base",
                self._upgrade_v1,
            ),
            build(
                2,
                "week3_week4_features",
                self._upgrade_v2,
            ),
            build(
                3,
                "m0_commands_approvals",
                self._upgrade_v3,
                self._downgrade_v3,
            ),
            build(
                4,
                "phase0_session_run_leases",
                self._upgrade_v4,
                self._downgrade_v4,
            ),
            build(
                5,
                "phase1a_thread_artifact_history",
                self._upgrade_v5,
                self._downgrade_v5,
            ),
            build(
                6,
                "phase1b_context_composer",
                self._upgrade_v6,
                self._downgrade_v6,
            ),
            build(
                7,
                "phase1c_artifact_retention_cache_observation",
                self._upgrade_v7,
                self._downgrade_v7,
            ),
            build(
                8,
                "phase1d_command_context_sidecar",
                self._upgrade_v8,
                self._downgrade_v8,
            ),
        )

    def _ensure_migration_table(self) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_migration_table_on_connection(connection)

    @staticmethod
    def _ensure_migration_table_on_connection(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                checksum TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )

    def _adopt_known_preview_history(
        self,
        connection: sqlite3.Connection,
        migrations: tuple[Migration, ...],
    ) -> None:
        """Preserve databases created by the exact uncommitted M0 preview build."""

        rows = connection.execute(
            "SELECT version, name, checksum, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()
        identity = tuple(
            (int(row["version"]), str(row["name"]), str(row["checksum"])) for row in rows
        )
        if identity == self._TWO_STEP_PREVIEW_HISTORY:
            self._validate_v3_schema_shape(connection)
        elif identity == self._THREE_STEP_PREVIEW_HISTORY:
            self._validate_schema_contract(
                connection,
                version=3,
                allow_missing_evaluation_events=True,
            )
            self._upgrade_preview_v3_evaluation_events(connection)
            self._validate_v3_schema_shape(connection)
        elif identity == self._MANIFEST_REWORK_PREVIEW_HISTORY:
            self._validate_v3_schema_shape(connection)
        elif identity == self._PHASE1A_V5_PREVIEW_HISTORY:
            self._validate_schema_contract(
                connection,
                version=5,
                allow_missing_phase1a_reference_guards=True,
            )
            self._validate_preview_v5_reference_rows(connection)
            self._upgrade_preview_v5_reference_guards(connection)
            self._validate_v5_schema_shape(connection)
            connection.execute(
                "UPDATE schema_migrations SET checksum = ? WHERE version = 5",
                (migrations[4].checksum,),
            )
            return
        elif identity == self._PHASE1D_V8_PREVIEW_HISTORY:
            self._validate_schema_contract(
                connection,
                version=8,
                allow_legacy_phase1d_context_scope_guard=True,
                allow_legacy_phase1d_prompt_scope_guard=True,
                allow_missing_phase1d_run_scope_guards=True,
            )
            self._upgrade_preview_v8_context_scope_guard(connection)
            self._validate_v8_schema_shape(connection)
            connection.execute(
                "UPDATE schema_migrations SET checksum = ? WHERE version = 8",
                (migrations[7].checksum,),
            )
            return
        elif identity == self._PHASE1D_V8_CONTEXT_GUARD_PREVIEW_HISTORY:
            self._validate_schema_contract(
                connection,
                version=8,
                allow_legacy_phase1d_prompt_scope_guard=True,
                allow_missing_phase1d_run_scope_guards=True,
            )
            self._upgrade_preview_v8_context_scope_guard(connection)
            self._validate_v8_schema_shape(connection)
            connection.execute(
                "UPDATE schema_migrations SET checksum = ? WHERE version = 8",
                (migrations[7].checksum,),
            )
            return
        elif identity == self._PHASE1D_V8_RUN_SCOPE_PREVIEW_HISTORY:
            self._validate_schema_contract(
                connection,
                version=8,
                allow_legacy_phase1d_prompt_scope_guard=True,
                allow_missing_phase1d_run_scope_guards=True,
            )
            self._replace_phase1d_prompt_compaction_agent_guard(
                connection,
                allow_thread_items_cross_agent=True,
            )
            self._create_phase1d_run_scope_guards(connection)
            self._validate_v8_schema_shape(connection)
            connection.execute(
                "UPDATE schema_migrations SET checksum = ? WHERE version = 8",
                (migrations[7].checksum,),
            )
            return
        else:
            return
        applied_at = str(rows[-1]["applied_at"])
        connection.execute("DELETE FROM schema_migrations")
        for migration in migrations[:3]:
            connection.execute(
                """
                INSERT INTO schema_migrations(version, name, checksum, applied_at)
                VALUES (?, ?, ?, ?)
                """,
                (migration.version, migration.name, migration.checksum, applied_at),
            )

    def _adopt_unversioned_legacy_history(
        self,
        connection: sqlite3.Connection,
        migrations: tuple[Migration, ...],
    ) -> None:
        """Adopt only exact Week 1 or Week 1-4 databases without history rows."""

        history_row = connection.execute("SELECT 1 FROM schema_migrations LIMIT 1").fetchone()
        if history_row is not None:
            return
        existing_tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
            if not str(row["name"]).startswith("sqlite_")
            and str(row["name"]) != "schema_migrations"
        }
        if not existing_tables:
            return
        self._validate_legacy_schema_shape(connection)
        historical_version = 2 if "workflow_runs" in existing_tables else 1
        if historical_version == 2:
            self._upgrade_v2(connection)
            self._validate_v2_schema_shape(connection)
        applied_at = utc_now().isoformat()
        for migration in migrations[:historical_version]:
            connection.execute(
                """
                INSERT INTO schema_migrations(version, name, checksum, applied_at)
                VALUES (?, ?, ?, ?)
                """,
                (migration.version, migration.name, migration.checksum, applied_at),
            )

    def _validated_applied_migrations(
        self,
        migrations: tuple[Migration, ...],
        *,
        connection: sqlite3.Connection | None = None,
    ) -> dict[int, sqlite3.Row]:
        expected = {migration.version: migration for migration in migrations}
        if connection is None:
            with self._connect() as read_connection:
                self._validate_migration_table_shape(read_connection)
                rows = read_connection.execute(
                    "SELECT version, name, checksum, applied_at "
                    "FROM schema_migrations ORDER BY version"
                ).fetchall()
        else:
            self._validate_migration_table_shape(connection)
            rows = connection.execute(
                "SELECT version, name, checksum, applied_at FROM schema_migrations ORDER BY version"
            ).fetchall()
        applied = {int(row["version"]): row for row in rows}
        if applied and sorted(applied) != list(range(1, max(applied) + 1)):
            raise MigrationError("schema migration history contains a version gap")
        for version, row in applied.items():
            migration = expected.get(version)
            if migration is None:
                raise MigrationError(f"database schema version {version} is newer than this build")
            if row["name"] != migration.name or row["checksum"] != migration.checksum:
                raise MigrationError(f"schema migration checksum mismatch at version {version}")
        current = max(applied, default=0)
        if current:
            if connection is None:
                with self._connect() as validation_connection:
                    self._validate_schema_contract(
                        validation_connection,
                        version=current,
                    )
            else:
                self._validate_schema_contract(connection, version=current)
        else:
            if connection is None:
                with self._connect() as validation_connection:
                    self._validate_unversioned_if_present(validation_connection)
            else:
                self._validate_unversioned_if_present(connection)
        return applied

    def _validate_unversioned_if_present(self, connection: sqlite3.Connection) -> None:
        existing = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view', 'trigger') "
            "AND name != 'schema_migrations' AND name NOT LIKE 'sqlite_%' LIMIT 1"
        ).fetchone()
        if existing is not None:
            tables = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name != 'schema_migrations' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            }
            recognized = set(self._v1_required_columns()).union(self._v2_required_columns())
            if not tables.intersection(recognized):
                raise MigrationError("unversioned database contains no recognized legacy schema")
            self._validate_legacy_schema_shape(connection)

    def _validate_migration_table_shape(self, connection: sqlite3.Connection) -> None:
        _objects, expected_ddl = self._canonical_schema_objects(0)
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
        ).fetchone()
        actual_sql = "" if row is None else self._normalize_schema_sql(str(row["sql"]))
        if actual_sql != expected_ddl["schema_migrations"]:
            raise MigrationError("schema_migrations DDL differs from the migration manifest")

    def _apply_migration(
        self,
        connection: sqlite3.Connection,
        migration: Migration,
    ) -> None:
        existing = connection.execute(
            "SELECT checksum FROM schema_migrations WHERE version = ?",
            (migration.version,),
        ).fetchone()
        if existing is not None:
            if existing["checksum"] != migration.checksum:
                raise MigrationError(
                    f"schema migration checksum mismatch at version {migration.version}"
                )
            return
        if migration.version == 1:
            self._validate_legacy_schema_shape(connection)
        migration.upgrade(connection)
        if migration.version == 1:
            self._validate_v1_schema_shape(connection)
        elif migration.version == 2:
            self._validate_v2_schema_shape(connection)
        elif migration.version == 3:
            self._validate_v3_schema_shape(connection)
        elif migration.version == 4:
            self._validate_v4_schema_shape(connection)
        elif migration.version == 5:
            self._validate_v5_schema_shape(connection)
        elif migration.version == 6:
            self._validate_v6_schema_shape(connection)
        elif migration.version == 7:
            self._validate_v7_schema_shape(connection)
        elif migration.version == 8:
            self._validate_v8_schema_shape(connection)
        connection.execute(
            """
            INSERT INTO schema_migrations(version, name, checksum, applied_at)
            VALUES (?, ?, ?, ?)
            """,
            (migration.version, migration.name, migration.checksum, utc_now().isoformat()),
        )

    def _rollback_migration(
        self,
        connection: sqlite3.Connection,
        migration: Migration,
    ) -> None:
        if migration.downgrade is None:
            raise MigrationError(f"schema migration {migration.version} cannot be rolled back")
        row = connection.execute(
            "SELECT checksum FROM schema_migrations WHERE version = ?",
            (migration.version,),
        ).fetchone()
        if row is None:
            return
        if row["checksum"] != migration.checksum:
            raise MigrationError(
                f"schema migration checksum mismatch at version {migration.version}"
            )
        migration.downgrade(connection)
        self._validate_schema_contract(connection, version=migration.version - 1)
        connection.execute("DELETE FROM schema_migrations WHERE version = ?", (migration.version,))

    @staticmethod
    def _execute_sql_batch(connection: sqlite3.Connection, sql: str) -> None:
        """Execute a SQL script statement-by-statement without implicit commits."""

        statement = ""
        for line in sql.splitlines():
            statement += f"{line}\n"
            if sqlite3.complete_statement(statement):
                if statement.strip():
                    connection.execute(statement)
                statement = ""
        if statement.strip():
            raise MigrationError("migration ended with an incomplete SQL statement")

    @staticmethod
    def _v1_required_columns() -> dict[str, set[str]]:
        return {
            "model_profiles": {"id", "body", "created_at"},
            "role_heads": {"id", "current_version"},
            "role_versions": {"role_id", "version", "body", "created_at"},
            "sessions": {"id", "body", "created_at"},
            "agents": {"id", "session_id", "status", "body", "created_at"},
            "events": {
                "sequence",
                "id",
                "session_id",
                "agent_id",
                "event_type",
                "body",
                "created_at",
            },
        }

    @staticmethod
    def _v2_required_columns() -> dict[str, set[str]]:
        return {
            "workflow_runs": {
                "id",
                "body",
                "status",
                "current_stage",
                "created_at",
                "updated_at",
            },
            "workflow_run_events": {
                "sequence",
                "id",
                "workflow_run_id",
                "role",
                "session_id",
                "event_type",
                "body",
                "created_at",
            },
            "evaluation_suites": {"id", "body", "experiment", "status", "created_at"},
            "evaluation_cases": {"id", "suite_id", "ordinal", "body", "created_at"},
            "evaluation_variants": {"id", "suite_id", "ordinal", "body", "created_at"},
            "evaluation_runs": {
                "id",
                "suite_id",
                "body",
                "status",
                "execution_strategy",
                "created_at",
                "updated_at",
            },
            "evaluation_results": {
                "id",
                "run_id",
                "case_id",
                "variant_id",
                "repetition",
                "status",
                "body",
                "created_at",
                "updated_at",
            },
            "memories": {"id", "current_version", "created_at"},
            "memory_versions": {
                "memory_id",
                "version",
                "body",
                "kind",
                "content",
                "project_scope",
                "role_scope",
                "source_session_id",
                "source_task",
                "confidence",
                "status",
                "created_at",
            },
            "memory_fts": {"memory_id", "version", "content", "source_task", "project_scope"},
        }

    @staticmethod
    def _v3_required_columns() -> dict[str, set[str]]:
        return {
            "tool_action_receipts": {
                "id",
                "scope",
                "session_id",
                "agent_id",
                "idempotency_key",
                "action_hash",
                "command_name",
                "status",
                "result_json",
                "error_code",
                "created_at",
                "updated_at",
                "completed_at",
            },
            "command_executions": {
                "id",
                "command_type",
                "idempotency_key",
                "action_hash",
                "status",
                "resource_type",
                "resource_id",
                "response_json",
                "http_status",
                "error_code",
                "created_at",
                "updated_at",
                "completed_at",
            },
            "approval_requests": {
                "id",
                "session_id",
                "agent_id",
                "tool_action_receipt_id",
                "tool_call_id",
                "action_hash",
                "category",
                "detail_summary",
                "status",
                "requested_at",
                "expires_at",
                "updated_at",
                "decided_at",
            },
            "approval_decisions": {
                "id",
                "approval_id",
                "approved",
                "decided_by",
                "reason_code",
                "decided_at",
            },
            "approval_audit_events": {
                "sequence",
                "id",
                "approval_id",
                "event_type",
                "body",
                "created_at",
            },
            "evaluation_run_events": {
                "sequence",
                "id",
                "evaluation_run_id",
                "result_id",
                "event_type",
                "body",
                "created_at",
            },
        }

    @staticmethod
    def _v4_required_columns() -> dict[str, set[str]]:
        return {
            "session_run_leases": {
                "session_id",
                "lease_token",
                "owner_id",
                "generation",
                "workflow_run_id",
                "agent_id",
                "cancel_requested",
                "acquired_at",
                "renewed_at",
                "expires_at",
                "released_at",
            },
            "workflow_execution_leases": {
                "workflow_run_id",
                "lease_token",
                "owner_id",
                "generation",
                "acquired_at",
                "renewed_at",
                "expires_at",
                "released_at",
            },
        }

    @staticmethod
    def _v5_required_columns() -> dict[str, set[str]]:
        return {
            "threads": {
                "sequence",
                "id",
                "parent_thread_id",
                "workspace_ref",
                "status",
                "body",
                "created_at",
                "updated_at",
                "archived_at",
            },
            "turns": {
                "sequence",
                "id",
                "thread_id",
                "position",
                "body",
                "created_at",
            },
            "items": {
                "sequence",
                "id",
                "thread_id",
                "turn_id",
                "position",
                "item_type",
                "body",
                "created_at",
            },
            "artifact_blobs": {
                "content_hash",
                "storage_key",
                "size_bytes",
                "created_at",
            },
            "artifacts": {
                "sequence",
                "id",
                "content_hash",
                "media_type",
                "size_bytes",
                "sensitivity",
                "retention_policy_ref",
                "body",
                "created_at",
            },
            "artifact_source_refs": {
                "artifact_id",
                "ordinal",
                "source_type",
                "source_id",
            },
            "thread_legacy_refs": {
                "thread_id",
                "source_type",
                "source_id",
                "created_at",
            },
        }

    @staticmethod
    def _v6_required_columns() -> dict[str, set[str]]:
        return {
            "threads": {
                "sequence",
                "id",
                "parent_thread_id",
                "workspace_ref",
                "status",
                "body",
                "body_hash",
                "created_at",
                "updated_at",
                "archived_at",
            },
            "items": {
                "sequence",
                "id",
                "thread_id",
                "turn_id",
                "position",
                "item_type",
                "body",
                "body_hash",
                "created_at",
            },
            "memory_versions": {
                "memory_id",
                "version",
                "body",
                "body_hash",
                "kind",
                "content",
                "project_scope",
                "role_scope",
                "source_session_id",
                "source_task",
                "confidence",
                "status",
                "created_at",
            },
            "compactions": {
                "sequence",
                "id",
                "session_id",
                "agent_id",
                "thread_id",
                "source_type",
                "source_cursor_start",
                "source_cursor_end",
                "source_snapshot_hash",
                "summary_json",
                "content_hash",
                "covered_item_refs_json",
                "created_at",
            },
            "context_revisions": {
                "sequence",
                "id",
                "session_id",
                "agent_id",
                "thread_id",
                "workspace_ref",
                "request_ordinal",
                "model_id",
                "prompt_layout_version",
                "context_window",
                "reserved_output_tokens",
                "tool_schema_token_estimate",
                "safety_margin_tokens",
                "available_input_tokens",
                "pre_compaction_token_estimate",
                "input_token_estimate",
                "estimation_method",
                "watermark_state",
                "compaction_id",
                "messages_json",
                "tools_json",
                "tools_hash",
                "tool_result_stubs_json",
                "message_ids_json",
                "source_item_ids_json",
                "artifact_refs_json",
                "memory_refs_json",
                "compaction_refs_json",
                "source_snapshots_json",
                "token_estimate",
                "source_cursor_start",
                "source_cursor_end",
                "source_cursor_namespace",
                "created_at",
            },
            "prompt_blocks": {
                "revision_id",
                "position",
                "id",
                "block_type",
                "content",
                "content_hash",
                "source_refs_json",
                "stable_until",
                "visibility",
                "token_estimate",
                "cache_eligible",
            },
            "reference_bindings": {
                "revision_id",
                "position",
                "id",
                "ref_type",
                "user_text",
                "resolved_target",
                "source_snapshot_hash",
                "include_mode",
                "max_tokens",
                "visibility",
                "resolved_at",
            },
        }

    @staticmethod
    def _v7_required_columns() -> dict[str, set[str]]:
        return {
            "retention_policies": {
                "id",
                "object_type",
                "grace_period_seconds",
                "allow_physical_delete",
                "created_at",
            },
            "artifact_retention_states": {
                "artifact_id",
                "policy_ref",
                "lifecycle",
                "pinned",
                "scheduled_deletion_at",
                "trashed_at",
                "deleted_at",
                "updated_at",
            },
            "artifact_retention_audit_events": {
                "sequence",
                "id",
                "artifact_id",
                "event_type",
                "content_hash",
                "finding_hash",
                "action_hash",
                "outcome",
                "created_at",
            },
            "cache_observations": {
                "sequence",
                "id",
                "provider",
                "model",
                "request_id",
                "context_revision_id",
                "cache_scope",
                "breakpoint_id",
                "hit_status",
                "prompt_tokens",
                "completion_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "cache_key_hash",
                "stable_prefix_hash",
                "invalidation_reason",
                "created_at",
            },
        }

    @staticmethod
    def _v8_required_columns() -> dict[str, set[str]]:
        return {
            "workspace_initializations": {
                "sequence",
                "id",
                "workspace_ref",
                "workspace_hash",
                "readable",
                "writable",
                "created_at",
            },
            "context_baselines": {
                "sequence",
                "id",
                "session_id",
                "thread_id",
                "item_cursor_end",
                "operation",
                "compaction_id",
                "previous_baseline_id",
                "created_at",
            },
            "review_runs": {
                "sequence",
                "id",
                "session_id",
                "thread_id",
                "workspace_ref",
                "scope",
                "status",
                "artifact_id",
                "error_code",
                "created_at",
                "updated_at",
            },
            "btw_sidecar_runs": {
                "sequence",
                "id",
                "session_id",
                "agent_id",
                "thread_id",
                "workspace_ref",
                "source_item_cursor_end",
                "prompt",
                "prompt_hash",
                "status",
                "response",
                "response_hash",
                "context_revision_id",
                "promoted_turn_id",
                "promoted_item_id",
                "error_code",
                "created_at",
                "updated_at",
            },
            "btw_sidecar_events": {
                "sequence",
                "id",
                "sidecar_run_id",
                "event_type",
                "body",
                "created_at",
            },
            "phase1d_command_audit_events": {
                "sequence",
                "id",
                "command_execution_id",
                "command_kind",
                "event_type",
                "resource_type",
                "resource_id",
                "detail_json",
                "created_at",
            },
        }

    @classmethod
    def _required_columns_contract(cls, version: int) -> dict[str, set[str]]:
        tables = {
            "schema_migrations": {"version", "name", "checksum", "applied_at"},
        }
        if version >= 1:
            tables.update(cls._v1_required_columns())
        if version >= 2:
            tables.update(cls._v2_required_columns())
        if version >= 3:
            tables.update(cls._v3_required_columns())
        if version >= 4:
            tables.update(cls._v4_required_columns())
        if version >= 5:
            tables.update(cls._v5_required_columns())
        if version >= 6:
            tables.update(cls._v6_required_columns())
        if version >= 7:
            tables.update(cls._v7_required_columns())
        if version >= 8:
            tables.update(cls._v8_required_columns())
        return tables

    @staticmethod
    def _nullable_column_contract() -> frozenset[tuple[str, str]]:
        return frozenset(
            {
                ("events", "agent_id"),
                ("workflow_run_events", "session_id"),
                ("evaluation_suites", "experiment"),
                ("memory_versions", "project_scope"),
                ("memory_versions", "source_session_id"),
                ("memory_versions", "source_task"),
                ("tool_action_receipts", "result_json"),
                ("tool_action_receipts", "error_code"),
                ("tool_action_receipts", "completed_at"),
                ("command_executions", "resource_type"),
                ("command_executions", "resource_id"),
                ("command_executions", "response_json"),
                ("command_executions", "http_status"),
                ("command_executions", "error_code"),
                ("command_executions", "completed_at"),
                ("approval_requests", "decided_at"),
                ("approval_decisions", "reason_code"),
                ("evaluation_run_events", "result_id"),
                ("session_run_leases", "workflow_run_id"),
                ("session_run_leases", "agent_id"),
                ("session_run_leases", "released_at"),
                ("workflow_execution_leases", "released_at"),
                ("threads", "parent_thread_id"),
                ("threads", "workspace_ref"),
                ("threads", "archived_at"),
                ("compactions", "thread_id"),
                ("context_revisions", "thread_id"),
                ("context_revisions", "workspace_ref"),
                ("context_revisions", "context_window"),
                ("context_revisions", "reserved_output_tokens"),
                ("context_revisions", "safety_margin_tokens"),
                ("context_revisions", "available_input_tokens"),
                ("context_revisions", "compaction_id"),
                ("context_revisions", "source_cursor_start"),
                ("context_revisions", "source_cursor_end"),
                ("context_revisions", "source_cursor_namespace"),
                ("prompt_blocks", "stable_until"),
                ("prompt_blocks", "token_estimate"),
                ("reference_bindings", "user_text"),
                ("reference_bindings", "max_tokens"),
                ("artifact_retention_states", "scheduled_deletion_at"),
                ("artifact_retention_states", "trashed_at"),
                ("artifact_retention_states", "deleted_at"),
                ("artifact_retention_audit_events", "artifact_id"),
                ("artifact_retention_audit_events", "content_hash"),
                ("artifact_retention_audit_events", "finding_hash"),
                ("artifact_retention_audit_events", "action_hash"),
                ("cache_observations", "request_id"),
                ("cache_observations", "context_revision_id"),
                ("cache_observations", "cache_scope"),
                ("cache_observations", "breakpoint_id"),
                ("cache_observations", "prompt_tokens"),
                ("cache_observations", "completion_tokens"),
                ("cache_observations", "cache_read_tokens"),
                ("cache_observations", "cache_write_tokens"),
                ("cache_observations", "cache_key_hash"),
                ("cache_observations", "stable_prefix_hash"),
                ("cache_observations", "invalidation_reason"),
                ("context_baselines", "compaction_id"),
                ("context_baselines", "previous_baseline_id"),
                ("review_runs", "thread_id"),
                ("review_runs", "artifact_id"),
                ("review_runs", "error_code"),
                ("btw_sidecar_runs", "response"),
                ("btw_sidecar_runs", "response_hash"),
                ("btw_sidecar_runs", "context_revision_id"),
                ("btw_sidecar_runs", "promoted_turn_id"),
                ("btw_sidecar_runs", "promoted_item_id"),
                ("btw_sidecar_runs", "error_code"),
                ("phase1d_command_audit_events", "resource_type"),
                ("phase1d_command_audit_events", "resource_id"),
            }
        )

    @staticmethod
    def _integer_column_contract() -> frozenset[str]:
        return frozenset(
            {
                "sequence",
                "current_version",
                "version",
                "ordinal",
                "repetition",
                "http_status",
                "approved",
                "generation",
                "cancel_requested",
                "position",
                "size_bytes",
                "request_ordinal",
                "context_window",
                "reserved_output_tokens",
                "tool_schema_token_estimate",
                "safety_margin_tokens",
                "available_input_tokens",
                "pre_compaction_token_estimate",
                "input_token_estimate",
                "source_cursor_start",
                "source_cursor_end",
                "token_estimate",
                "cache_eligible",
                "max_tokens",
                "grace_period_seconds",
                "allow_physical_delete",
                "pinned",
                "prompt_tokens",
                "completion_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "readable",
                "writable",
                "item_cursor_end",
                "source_item_cursor_end",
            }
        )

    @classmethod
    def _column_contract(cls, version: int) -> dict[str, dict[str, dict[str, Any]]]:
        """Return the physical column contract included in migration checksums."""

        tables = cls._required_columns_contract(version)
        primary_keys = cls._primary_key_contract(version)
        nullable = cls._nullable_column_contract()
        integer_columns = cls._integer_column_contract()
        contract: dict[str, dict[str, dict[str, Any]]] = {}
        for table, columns in tables.items():
            if table == "memory_fts":
                contract[table] = {
                    column: {"type": "FTS5", "not_null": False} for column in sorted(columns)
                }
                continue
            primary_key = primary_keys[table]
            contract[table] = {}
            for column in sorted(columns):
                column_type = (
                    "REAL"
                    if column == "confidence"
                    else "INTEGER"
                    if column in integer_columns
                    else "TEXT"
                )
                not_null = not (
                    (table, column) in nullable or (len(primary_key) == 1 and column in primary_key)
                )
                contract[table][column] = {
                    "type": column_type,
                    "not_null": not_null,
                }
        return contract

    @classmethod
    def _migration_manifest(cls, version: int) -> str:
        """Return the immutable, auditable schema contract bound into checksums."""

        tables = cls._required_columns_contract(version)
        objects, ddl = cls._canonical_schema_objects(version)
        manifest = {
            "schema": "operant.sqlite.migration-manifest.v1",
            "version": version,
            "tables": {name: sorted(columns) for name, columns in sorted(tables.items())},
            "columns": cls._column_contract(version),
            "primary_keys": cls._primary_key_contract(version),
            "unique": cls._unique_contract(version),
            "foreign_keys": cls._foreign_key_contract(version),
            "indexes": cls._index_contract(version),
            "checks": (
                {
                    "approval_decisions": ["approved IN (0, 1)"],
                    **(
                        {
                            "session_run_leases": [
                                "generation >= 1",
                                "cancel_requested IN (0, 1)",
                            ],
                            "workflow_execution_leases": ["generation >= 1"],
                        }
                        if version >= 4
                        else {}
                    ),
                    **(
                        {
                            "threads": [
                                "status IN ('active', 'completed', 'cancelled', 'archived')",
                                "(status = 'archived' AND archived_at IS NOT NULL) OR "
                                "(status != 'archived' AND archived_at IS NULL)",
                                "parent_thread_id IS NULL OR parent_thread_id != id",
                                "workspace_ref IS NULL OR length(workspace_ref) >= 1",
                            ],
                            "turns": ["position >= 1"],
                            "items": [
                                "position >= 1",
                                "item_type IN ('user_message', 'agent_message', "
                                "'tool_call', 'tool_result_ref', 'artifact_ref', "
                                "'approval_link', 'steering', 'system_event')",
                            ],
                            "artifact_blobs": [
                                "size_bytes >= 0",
                                "length(content_hash) = 64",
                                "content_hash NOT GLOB '*[^0-9a-f]*'",
                                "storage_key = 'sha256/' || substr(content_hash, 1, 2) || '/' || "
                                "substr(content_hash, 3, 2) || '/' || content_hash",
                            ],
                            "artifacts": [
                                "size_bytes >= 0",
                                "sensitivity IN ('normal', 'sensitive', 'restricted')",
                                "length(content_hash) = 64",
                                "content_hash NOT GLOB '*[^0-9a-f]*'",
                                "length(media_type) >= 1",
                                "length(retention_policy_ref) >= 1",
                            ],
                            "artifact_source_refs": ["ordinal >= 1", "length(source_id) >= 1"],
                        }
                        if version >= 5
                        else {}
                    ),
                    **(
                        {
                            "compactions": [
                                "source_cursor_start >= 1",
                                "source_cursor_end >= source_cursor_start",
                                "source_type IN ('context_revisions', 'thread_items')",
                                "source_type != 'thread_items' OR thread_id IS NOT NULL",
                            ],
                            "context_revisions": [
                                "request_ordinal >= 1",
                                "context_window IS NULL OR context_window >= 1",
                                "reserved_output_tokens IS NULL OR reserved_output_tokens >= 1",
                                "tool_schema_token_estimate >= 0",
                                "safety_margin_tokens IS NULL OR safety_margin_tokens >= 1",
                                "available_input_tokens IS NULL OR available_input_tokens >= 0",
                                "pre_compaction_token_estimate >= 0",
                                "input_token_estimate >= 0",
                                "token_estimate >= 0",
                                "watermark_state IN ('green', 'yellow', 'red', "
                                "'emergency', 'unknown')",
                                "workspace_ref IS NULL OR length(workspace_ref) >= 1",
                                "json_valid(source_snapshots_json) AND "
                                "json_type(source_snapshots_json) = 'array'",
                                "(source_cursor_start IS NULL AND source_cursor_end IS NULL AND "
                                "source_cursor_namespace IS NULL) OR "
                                "(source_cursor_start >= 1 AND "
                                "source_cursor_end >= source_cursor_start AND "
                                "source_cursor_namespace = 'items.sequence')",
                            ],
                            "prompt_blocks": [
                                "position >= 1",
                                "token_estimate IS NULL OR token_estimate >= 0",
                                "cache_eligible IN (0, 1)",
                            ],
                            "reference_bindings": [
                                "position >= 1",
                                "max_tokens IS NULL OR max_tokens >= 1",
                            ],
                        }
                        if version >= 6
                        else {}
                    ),
                    **(
                        {
                            "retention_policies": [
                                "object_type = 'artifact'",
                                "grace_period_seconds BETWEEN 0 AND 31536000",
                                "allow_physical_delete IN (0, 1)",
                            ],
                            "artifact_retention_states": [
                                "lifecycle IN ('active', 'archived', "
                                "'deletion_scheduled', 'trashed', 'deleted')",
                                "pinned IN (0, 1)",
                            ],
                            "cache_observations": [
                                "hit_status IN ('hit', 'miss', 'unknown')",
                            ],
                        }
                        if version >= 7
                        else {}
                    ),
                    **(
                        {
                            "workspace_initializations": [
                                "readable IN (0, 1)",
                                "writable IN (0, 1)",
                                "length(workspace_hash) = 64",
                            ],
                            "context_baselines": [
                                "item_cursor_end >= 0",
                                "operation IN ('clear', 'compact')",
                            ],
                            "review_runs": [
                                "status IN ('running', 'completed', 'failed')",
                            ],
                            "btw_sidecar_runs": [
                                "source_item_cursor_end >= 0",
                                "status IN ('running', 'completed', 'failed', 'promoted')",
                            ],
                        }
                        if version >= 8
                        else {}
                    ),
                }
                if version >= 3
                else {}
            ),
            "virtual_tables": ({"memory_fts": "fts5"} if version >= 2 else {}),
            "objects": sorted(f"{object_type}:{name}" for object_type, name in objects),
            "ddl": ddl,
        }
        return json.dumps(manifest, sort_keys=True, separators=(",", ":"))

    @classmethod
    def _canonical_schema_objects(
        cls,
        version: int,
    ) -> tuple[set[tuple[str, str]], dict[str, str]]:
        """Materialize the exact managed SQLite DDL from the migration steps."""

        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        store = SQLiteStore(":memory:")
        try:
            cls._ensure_migration_table_on_connection(connection)
            if version >= 1:
                store._upgrade_v1(connection)
            if version >= 2:
                store._upgrade_v2(connection)
            if version >= 3:
                store._upgrade_v3(connection)
            if version >= 4:
                store._upgrade_v4(connection)
            if version >= 5:
                store._upgrade_v5(connection)
            if version >= 6:
                store._upgrade_v6(connection)
            if version >= 7:
                store._upgrade_v7(connection)
            if version >= 8:
                store._upgrade_v8(connection)
            rows = connection.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE type IN ('table', 'index', 'view', 'trigger') ORDER BY type, name"
            ).fetchall()
        finally:
            connection.close()
        objects = {(str(row["type"]), str(row["name"])) for row in rows}
        ddl = {
            str(row["name"]): cls._normalize_schema_sql(str(row["sql"]))
            for row in rows
            if row["sql"] is not None and not str(row["name"]).startswith("memory_fts_")
        }
        return objects, ddl

    @staticmethod
    def _normalize_schema_sql(sql: str) -> str:
        unquoted_identifiers = sql.translate(str.maketrans("", "", '"`[]'))
        return re.sub(r"\s+", "", unquoted_identifiers).lower()

    @staticmethod
    def _primary_key_contract(version: int) -> dict[str, tuple[str, ...]]:
        contract: dict[str, tuple[str, ...]] = {
            "schema_migrations": ("version",),
        }
        if version >= 1:
            contract.update(
                {
                    "model_profiles": ("id",),
                    "role_heads": ("id",),
                    "role_versions": ("role_id", "version"),
                    "sessions": ("id",),
                    "agents": ("id",),
                    "events": ("sequence",),
                }
            )
        if version >= 2:
            contract.update(
                {
                    "workflow_runs": ("id",),
                    "workflow_run_events": ("sequence",),
                    "evaluation_suites": ("id",),
                    "evaluation_cases": ("id",),
                    "evaluation_variants": ("id",),
                    "evaluation_runs": ("id",),
                    "evaluation_results": ("id",),
                    "memories": ("id",),
                    "memory_versions": ("memory_id", "version"),
                }
            )
        if version >= 3:
            contract.update(
                {
                    "tool_action_receipts": ("id",),
                    "command_executions": ("id",),
                    "approval_requests": ("id",),
                    "approval_decisions": ("id",),
                    "approval_audit_events": ("sequence",),
                    "evaluation_run_events": ("sequence",),
                }
            )
        if version >= 4:
            contract.update(
                {
                    "session_run_leases": ("session_id",),
                    "workflow_execution_leases": ("workflow_run_id",),
                }
            )
        if version >= 5:
            contract.update(
                {
                    "threads": ("sequence",),
                    "turns": ("sequence",),
                    "items": ("sequence",),
                    "artifact_blobs": ("content_hash",),
                    "artifacts": ("sequence",),
                    "artifact_source_refs": ("artifact_id", "ordinal"),
                    "thread_legacy_refs": ("source_type", "source_id"),
                }
            )
        if version >= 6:
            contract.update(
                {
                    "compactions": ("sequence",),
                    "context_revisions": ("sequence",),
                    "prompt_blocks": ("revision_id", "position"),
                    "reference_bindings": ("revision_id", "position"),
                }
            )
        if version >= 7:
            contract.update(
                {
                    "retention_policies": ("id",),
                    "artifact_retention_states": ("artifact_id",),
                    "artifact_retention_audit_events": ("sequence",),
                    "cache_observations": ("sequence",),
                }
            )
        if version >= 8:
            contract.update(
                {
                    "workspace_initializations": ("sequence",),
                    "context_baselines": ("sequence",),
                    "review_runs": ("sequence",),
                    "btw_sidecar_runs": ("sequence",),
                    "btw_sidecar_events": ("sequence",),
                    "phase1d_command_audit_events": ("sequence",),
                }
            )
        return contract

    @staticmethod
    def _unique_contract(version: int) -> dict[str, tuple[tuple[str, ...], ...]]:
        contract: dict[str, tuple[tuple[str, ...], ...]] = {}
        if version >= 1:
            contract["events"] = (("id",),)
        if version >= 2:
            contract.update(
                {
                    "workflow_run_events": (("id",),),
                    "evaluation_cases": (("suite_id", "ordinal"),),
                    "evaluation_variants": (("suite_id", "ordinal"),),
                    "evaluation_results": (("run_id", "case_id", "variant_id", "repetition"),),
                }
            )
        if version >= 3:
            contract.update(
                {
                    "tool_action_receipts": (("scope", "idempotency_key"),),
                    "command_executions": (("command_type", "idempotency_key"),),
                    "approval_requests": (("agent_id", "tool_call_id"),),
                    "approval_decisions": (("approval_id",),),
                    "approval_audit_events": (("id",),),
                    "evaluation_run_events": (("id",),),
                }
            )
        if version >= 4:
            contract.update(
                {
                    "session_run_leases": (("lease_token",),),
                    "workflow_execution_leases": (("lease_token",),),
                }
            )
        if version >= 5:
            contract.update(
                {
                    "threads": (("id",),),
                    "turns": (
                        ("id",),
                        ("thread_id", "position"),
                        ("id", "thread_id"),
                    ),
                    "items": (("id",), ("thread_id", "position")),
                    "artifact_blobs": (("storage_key",),),
                    "artifacts": (("id",), ("content_hash",)),
                    "artifact_source_refs": (("artifact_id", "source_type", "source_id"),),
                    "thread_legacy_refs": (("thread_id", "source_type", "source_id"),),
                }
            )
        if version >= 6:
            contract.update(
                {
                    "compactions": (("id",),),
                    "context_revisions": (("id",), ("agent_id", "request_ordinal")),
                    "prompt_blocks": (("id",),),
                    "reference_bindings": (
                        ("id",),
                        ("revision_id", "ref_type", "resolved_target"),
                    ),
                }
            )
        if version >= 7:
            contract.update(
                {
                    "artifact_retention_audit_events": (("id",),),
                    "cache_observations": (("id",),),
                }
            )
        if version >= 8:
            contract.update(
                {
                    "workspace_initializations": (
                        ("id",),
                        ("workspace_ref",),
                        ("workspace_hash",),
                    ),
                    "context_baselines": (("id",),),
                    "review_runs": (("id",),),
                    "btw_sidecar_runs": (
                        ("id",),
                        ("context_revision_id",),
                        ("promoted_item_id",),
                    ),
                    "btw_sidecar_events": (("id",),),
                    "phase1d_command_audit_events": (("id",),),
                }
            )
        return contract

    @staticmethod
    def _foreign_key_contract(
        version: int,
    ) -> dict[str, tuple[tuple[str, str, str, str], ...]]:
        contract: dict[str, tuple[tuple[str, str, str, str], ...]] = {}
        if version >= 1:
            contract.update(
                {
                    "role_versions": (("role_id", "role_heads", "id", "NO ACTION"),),
                    "agents": (("session_id", "sessions", "id", "NO ACTION"),),
                    "events": (("session_id", "sessions", "id", "NO ACTION"),),
                }
            )
        if version >= 2:
            contract.update(
                {
                    "workflow_run_events": (
                        ("workflow_run_id", "workflow_runs", "id", "NO ACTION"),
                    ),
                    "evaluation_cases": (("suite_id", "evaluation_suites", "id", "CASCADE"),),
                    "evaluation_variants": (("suite_id", "evaluation_suites", "id", "CASCADE"),),
                    "evaluation_runs": (("suite_id", "evaluation_suites", "id", "NO ACTION"),),
                    "evaluation_results": (
                        ("run_id", "evaluation_runs", "id", "NO ACTION"),
                        ("case_id", "evaluation_cases", "id", "NO ACTION"),
                        ("variant_id", "evaluation_variants", "id", "NO ACTION"),
                    ),
                    "memory_versions": (("memory_id", "memories", "id", "NO ACTION"),),
                }
            )
        if version >= 3:
            contract.update(
                {
                    "tool_action_receipts": (
                        ("session_id", "sessions", "id", "NO ACTION"),
                        ("agent_id", "agents", "id", "NO ACTION"),
                    ),
                    "approval_requests": (
                        ("session_id", "sessions", "id", "NO ACTION"),
                        ("agent_id", "agents", "id", "NO ACTION"),
                        (
                            "tool_action_receipt_id",
                            "tool_action_receipts",
                            "id",
                            "NO ACTION",
                        ),
                    ),
                    "approval_decisions": (
                        ("approval_id", "approval_requests", "id", "NO ACTION"),
                    ),
                    "approval_audit_events": (
                        ("approval_id", "approval_requests", "id", "NO ACTION"),
                    ),
                    "evaluation_run_events": (
                        ("evaluation_run_id", "evaluation_runs", "id", "NO ACTION"),
                        ("result_id", "evaluation_results", "id", "NO ACTION"),
                    ),
                }
            )
        if version >= 4:
            contract.update(
                {
                    "session_run_leases": (
                        ("session_id", "sessions", "id", "NO ACTION"),
                        ("workflow_run_id", "workflow_runs", "id", "NO ACTION"),
                        ("agent_id", "agents", "id", "NO ACTION"),
                    ),
                    "workflow_execution_leases": (
                        ("workflow_run_id", "workflow_runs", "id", "NO ACTION"),
                    ),
                }
            )
        if version >= 5:
            contract.update(
                {
                    "threads": (("parent_thread_id", "threads", "id", "NO ACTION"),),
                    "turns": (("thread_id", "threads", "id", "NO ACTION"),),
                    "items": (
                        ("thread_id", "turns", "thread_id", "NO ACTION"),
                        ("turn_id", "turns", "id", "NO ACTION"),
                    ),
                    "artifacts": (("content_hash", "artifact_blobs", "content_hash", "NO ACTION"),),
                    "artifact_source_refs": (("artifact_id", "artifacts", "id", "NO ACTION"),),
                    "thread_legacy_refs": (("thread_id", "threads", "id", "NO ACTION"),),
                }
            )
        if version >= 6:
            contract.update(
                {
                    "compactions": (
                        ("session_id", "sessions", "id", "NO ACTION"),
                        ("agent_id", "agents", "id", "NO ACTION"),
                        ("thread_id", "threads", "id", "NO ACTION"),
                    ),
                    "context_revisions": (
                        ("session_id", "sessions", "id", "NO ACTION"),
                        ("agent_id", "agents", "id", "NO ACTION"),
                        ("thread_id", "threads", "id", "NO ACTION"),
                        ("compaction_id", "compactions", "id", "NO ACTION"),
                    ),
                    "prompt_blocks": (("revision_id", "context_revisions", "id", "NO ACTION"),),
                    "reference_bindings": (
                        ("revision_id", "context_revisions", "id", "NO ACTION"),
                    ),
                }
            )
        if version >= 7:
            contract.update(
                {
                    "artifact_retention_states": (
                        ("artifact_id", "artifacts", "id", "NO ACTION"),
                        ("policy_ref", "retention_policies", "id", "NO ACTION"),
                    ),
                    "artifact_retention_audit_events": (
                        ("artifact_id", "artifacts", "id", "NO ACTION"),
                    ),
                    "cache_observations": (
                        (
                            "context_revision_id",
                            "context_revisions",
                            "id",
                            "NO ACTION",
                        ),
                    ),
                }
            )
        if version >= 8:
            contract.update(
                {
                    "context_baselines": (
                        ("session_id", "sessions", "id", "NO ACTION"),
                        ("thread_id", "threads", "id", "NO ACTION"),
                        ("compaction_id", "compactions", "id", "NO ACTION"),
                        (
                            "previous_baseline_id",
                            "context_baselines",
                            "id",
                            "NO ACTION",
                        ),
                    ),
                    "review_runs": (
                        ("session_id", "sessions", "id", "NO ACTION"),
                        ("thread_id", "threads", "id", "NO ACTION"),
                        ("artifact_id", "artifacts", "id", "NO ACTION"),
                    ),
                    "btw_sidecar_runs": (
                        ("session_id", "sessions", "id", "NO ACTION"),
                        ("agent_id", "agents", "id", "NO ACTION"),
                        ("thread_id", "threads", "id", "NO ACTION"),
                        (
                            "context_revision_id",
                            "context_revisions",
                            "id",
                            "NO ACTION",
                        ),
                        ("promoted_turn_id", "turns", "id", "NO ACTION"),
                        ("promoted_item_id", "items", "id", "NO ACTION"),
                    ),
                    "btw_sidecar_events": (
                        ("sidecar_run_id", "btw_sidecar_runs", "id", "NO ACTION"),
                    ),
                    "phase1d_command_audit_events": (
                        (
                            "command_execution_id",
                            "command_executions",
                            "id",
                            "NO ACTION",
                        ),
                    ),
                }
            )
        return contract

    @staticmethod
    def _index_contract(version: int) -> dict[str, tuple[str, ...]]:
        indexes: dict[str, tuple[str, ...]] = {}
        if version >= 2:
            indexes.update(
                {
                    "idx_workflow_run_events_run_sequence": (
                        "workflow_run_id",
                        "sequence",
                    ),
                    "idx_evaluation_cases_suite_ordinal": ("suite_id", "ordinal"),
                    "idx_evaluation_variants_suite_ordinal": ("suite_id", "ordinal"),
                    "idx_evaluation_runs_suite_status_created": (
                        "suite_id",
                        "status",
                        "created_at",
                    ),
                    "idx_evaluation_results_run_status_created": (
                        "run_id",
                        "status",
                        "created_at",
                    ),
                    "idx_memory_versions_source_session": ("source_session_id",),
                    "idx_memory_versions_project_scope": ("project_scope",),
                }
            )
        if version >= 3:
            indexes.update(
                {
                    "idx_events_session_sequence": ("session_id", "sequence"),
                    "idx_tool_action_receipts_session_created": (
                        "session_id",
                        "created_at",
                    ),
                    "idx_command_executions_status_updated": ("status", "updated_at"),
                    "idx_approval_requests_session_status_requested": (
                        "session_id",
                        "status",
                        "requested_at",
                    ),
                    "idx_approval_audit_approval_sequence": ("approval_id", "sequence"),
                    "idx_evaluation_run_events_run_sequence": (
                        "evaluation_run_id",
                        "sequence",
                    ),
                }
            )
        if version >= 4:
            indexes.update(
                {
                    "idx_session_run_leases_workflow_active": (
                        "workflow_run_id",
                        "released_at",
                    ),
                    "idx_workflow_execution_leases_active": ("released_at", "expires_at"),
                }
            )
        if version >= 5:
            indexes.update(
                {
                    "idx_threads_parent_sequence": ("parent_thread_id", "sequence"),
                    "idx_threads_workspace_status_sequence": (
                        "workspace_ref",
                        "status",
                        "sequence",
                    ),
                    "idx_turns_thread_sequence": ("thread_id", "sequence"),
                    "idx_items_thread_sequence": ("thread_id", "sequence"),
                    "idx_items_turn_sequence": ("turn_id", "sequence"),
                    "idx_artifacts_hash_sequence": ("content_hash", "sequence"),
                    "idx_artifact_sources_type_id": ("source_type", "source_id"),
                    "idx_thread_legacy_refs_thread": ("thread_id",),
                }
            )
        if version >= 6:
            indexes.update(
                {
                    "idx_compactions_agent_sequence": ("agent_id", "sequence"),
                    "idx_compactions_thread_sequence": ("thread_id", "sequence"),
                    "idx_context_revisions_session_sequence": ("session_id", "sequence"),
                    "idx_context_revisions_thread_sequence": ("thread_id", "sequence"),
                    "idx_prompt_blocks_revision_position": ("revision_id", "position"),
                    "idx_reference_bindings_target": ("ref_type", "resolved_target"),
                }
            )
        if version >= 7:
            indexes.update(
                {
                    "idx_artifact_retention_states_lifecycle_due": (
                        "lifecycle",
                        "scheduled_deletion_at",
                    ),
                    "idx_artifact_retention_audit_artifact_sequence": (
                        "artifact_id",
                        "sequence",
                    ),
                    "idx_cache_observations_context_sequence": (
                        "context_revision_id",
                        "sequence",
                    ),
                }
            )
        if version >= 8:
            indexes.update(
                {
                    "idx_context_baselines_session_thread_sequence": (
                        "session_id",
                        "thread_id",
                        "sequence",
                    ),
                    "idx_review_runs_session_sequence": ("session_id", "sequence"),
                    "idx_btw_sidecar_runs_thread_sequence": ("thread_id", "sequence"),
                    "idx_btw_sidecar_events_run_sequence": ("sidecar_run_id", "sequence"),
                    "idx_phase1d_command_audit_execution_sequence": (
                        "command_execution_id",
                        "sequence",
                    ),
                }
            )
        return indexes

    def _validate_legacy_schema_shape(self, connection: sqlite3.Connection) -> None:
        """Recognize only an empty DB, the real Week 1 base, or full Week 1-4."""

        week1 = self._v1_required_columns()
        later = self._v2_required_columns()
        required = {**week1, **later}
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        existing = {
            str(row["name"])
            for row in rows
            if not str(row["name"]).startswith("sqlite_")
            and str(row["name"]) != "schema_migrations"
        }
        allowed = set(required).union(
            {
                "memory_fts_data",
                "memory_fts_idx",
                "memory_fts_content",
                "memory_fts_docsize",
                "memory_fts_config",
            }
        )
        unknown = existing.difference(allowed)
        if unknown:
            raise MigrationError(f"legacy database contains unknown tables: {sorted(unknown)}")
        core_existing = existing.intersection(required)
        is_week1 = core_existing == set(week1)
        is_full = core_existing == set(required)
        if existing and not (is_week1 or is_full):
            missing_tables = sorted(
                (set(week1) if not set(week1).issubset(existing) else set(required)).difference(
                    existing
                )
            )
            raise MigrationError(
                "legacy database does not match a recognized historical baseline; "
                "missing required tables: "
                f"{missing_tables}"
            )
        for table in existing.intersection(required):
            actual = {
                str(row["name"])
                for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            }
            expected = set(required[table])
            if table == "evaluation_results":
                expected.discard("updated_at")
            missing = expected.difference(actual)
            if missing:
                raise MigrationError(
                    f"legacy table {table} is missing required columns: {sorted(missing)}"
                )
        if is_week1:
            self._validate_v1_schema_shape(connection)
        elif is_full:
            self._validate_v2_schema_shape(connection, allow_missing_updated_at=True)

    def _validate_v1_schema_shape(self, connection: sqlite3.Connection) -> None:
        self._validate_schema_contract(connection, version=1)

    def _validate_v2_schema_shape(
        self,
        connection: sqlite3.Connection,
        *,
        allow_missing_updated_at: bool = False,
    ) -> None:
        self._validate_schema_contract(
            connection,
            version=2,
            allow_missing_evaluation_updated_at=allow_missing_updated_at,
        )

    def _validate_v3_schema_shape(self, connection: sqlite3.Connection) -> None:
        self._validate_schema_contract(connection, version=3)

    def _validate_v4_schema_shape(self, connection: sqlite3.Connection) -> None:
        self._validate_schema_contract(connection, version=4)

    def _validate_v5_schema_shape(self, connection: sqlite3.Connection) -> None:
        self._validate_schema_contract(connection, version=5)

    def _validate_v6_schema_shape(self, connection: sqlite3.Connection) -> None:
        self._validate_schema_contract(connection, version=6)

    def _validate_v7_schema_shape(self, connection: sqlite3.Connection) -> None:
        self._validate_schema_contract(connection, version=7)

    def _validate_v8_schema_shape(self, connection: sqlite3.Connection) -> None:
        self._validate_schema_contract(connection, version=8)

    def _validate_schema_contract(
        self,
        connection: sqlite3.Connection,
        *,
        version: int,
        allow_missing_evaluation_updated_at: bool = False,
        allow_missing_evaluation_events: bool = False,
        allow_missing_phase1a_reference_guards: bool = False,
        allow_legacy_phase1d_context_scope_guard: bool = False,
        allow_legacy_phase1d_prompt_scope_guard: bool = False,
        allow_missing_phase1d_run_scope_guards: bool = False,
    ) -> None:
        required_tables = self._required_columns_contract(version)
        if allow_missing_evaluation_events:
            required_tables.pop("evaluation_run_events")
        primary_keys = self._primary_key_contract(version)
        column_contract = self._column_contract(version)
        expected_objects, expected_ddl = self._canonical_schema_objects(version)
        if allow_missing_evaluation_events:
            excluded = {
                ("table", "evaluation_run_events"),
                ("index", "idx_evaluation_run_events_run_sequence"),
                ("index", "sqlite_autoindex_evaluation_run_events_1"),
            }
            expected_objects.difference_update(excluded)
            expected_ddl.pop("evaluation_run_events", None)
            expected_ddl.pop("idx_evaluation_run_events_run_sequence", None)
        if allow_missing_phase1a_reference_guards:
            guard_names = {
                "artifact_source_refs_insert_guard",
                "items_tool_call_unique_guard",
                "items_tool_result_call_guard",
                "thread_legacy_refs_insert_guard",
            }
            expected_objects.difference_update(("trigger", name) for name in guard_names)
            for name in guard_names:
                expected_ddl.pop(name, None)
        if allow_legacy_phase1d_context_scope_guard:
            _v7_objects, v7_ddl = self._canonical_schema_objects(7)
            expected_ddl["context_revisions_scope_guard"] = v7_ddl["context_revisions_scope_guard"]
        if allow_legacy_phase1d_prompt_scope_guard:
            _v7_objects, v7_ddl = self._canonical_schema_objects(7)
            expected_ddl["prompt_blocks_source_refs_guard"] = v7_ddl[
                "prompt_blocks_source_refs_guard"
            ]
        if allow_missing_phase1d_run_scope_guards:
            guard_names = {
                "review_runs_scope_guard",
                "review_runs_identity_guard",
                "btw_sidecar_runs_scope_guard",
                "btw_sidecar_runs_identity_guard",
            }
            expected_objects.difference_update(("trigger", name) for name in guard_names)
            for name in guard_names:
                expected_ddl.pop(name, None)
        object_rows = connection.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE type IN ('table', 'index', 'view', 'trigger') ORDER BY type, name"
        ).fetchall()
        actual_objects = {
            (str(row["type"]), str(row["name"]))
            for row in object_rows
            if re.fullmatch(r"sqlite_stat[1-4]", str(row["name"])) is None
        }
        if actual_objects != expected_objects:
            missing = sorted(expected_objects.difference(actual_objects))
            extra = sorted(actual_objects.difference(expected_objects))
            raise MigrationError(
                "schema managed objects differ from the migration manifest; "
                f"missing={missing}, extra={extra}"
            )
        actual_ddl = {
            str(row["name"]): self._normalize_schema_sql(str(row["sql"]))
            for row in object_rows
            if row["sql"] is not None
            and not str(row["name"]).startswith("memory_fts_")
            and re.fullmatch(r"sqlite_stat[1-4]", str(row["name"])) is None
        }
        for name, expected_sql in expected_ddl.items():
            actual_sql = actual_ddl.get(name)
            if (
                allow_missing_evaluation_updated_at
                and name == "evaluation_results"
                and not any(
                    str(row["name"]) == "updated_at"
                    for row in connection.execute(
                        'PRAGMA table_info("evaluation_results")'
                    ).fetchall()
                )
            ):
                expected_sql = expected_sql.replace(
                    ",updated_attextnotnull",
                    "",
                )
            if actual_sql != expected_sql:
                raise MigrationError(
                    f"schema object {name} DDL differs from the migration manifest"
                )
        for table, expected_columns in required_tables.items():
            if table == "memory_fts":
                continue
            rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            actual_by_name = {str(row["name"]): row for row in rows}
            expected = set(expected_columns)
            if (
                allow_missing_evaluation_updated_at
                and table == "evaluation_results"
                and "updated_at" not in actual_by_name
            ):
                expected.remove("updated_at")
            if set(actual_by_name) != expected:
                raise MigrationError(
                    f"schema table {table} has unexpected columns: "
                    f"expected {sorted(expected)}, got {sorted(actual_by_name)}"
                )
            expected_pk = primary_keys[table]
            for column in expected:
                row = actual_by_name[column]
                expected_type = str(column_contract[table][column]["type"])
                actual_type = str(row["type"]).upper()
                if actual_type != expected_type:
                    raise MigrationError(
                        f"schema column {table}.{column} has type {actual_type!r}; "
                        f"expected {expected_type}"
                    )
                expected_pk_position = expected_pk.index(column) + 1 if column in expected_pk else 0
                if int(row["pk"]) != expected_pk_position:
                    raise MigrationError(
                        f"schema column {table}.{column} has unexpected primary-key position"
                    )
                expected_not_null = bool(column_contract[table][column]["not_null"])
                actual_not_null = bool(row["notnull"])
                if table == "evaluation_results" and column == "updated_at":
                    null_row = connection.execute(
                        "SELECT 1 FROM evaluation_results WHERE updated_at IS NULL LIMIT 1"
                    ).fetchone()
                    if null_row is not None:
                        raise MigrationError(
                            "legacy evaluation_results.updated_at contains NULL values"
                        )
                if actual_not_null is not expected_not_null:
                    raise MigrationError(
                        f"schema column {table}.{column} has unexpected NOT NULL constraint"
                    )

        expected_unique = self._unique_contract(version)
        for table in required_tables:
            if table == "memory_fts":
                continue
            actual_unique: set[tuple[str, ...]] = set()
            for row in connection.execute(f'PRAGMA index_list("{table}")').fetchall():
                if not bool(row["unique"]) or str(row["origin"]) == "pk":
                    continue
                index_name = str(row["name"])
                actual_unique.add(
                    tuple(
                        str(item["name"])
                        for item in connection.execute(
                            f'PRAGMA index_info("{index_name}")'
                        ).fetchall()
                    )
                )
            wanted = set(expected_unique.get(table, ()))
            if actual_unique != wanted:
                raise MigrationError(
                    f"schema table {table} has unexpected UNIQUE constraints: "
                    f"expected {sorted(wanted)}, got {sorted(actual_unique)}"
                )

        expected_foreign_keys = self._foreign_key_contract(version)
        for table in required_tables:
            if table == "memory_fts":
                continue
            actual_foreign_keys = {
                (
                    str(row["from"]),
                    str(row["table"]),
                    str(row["to"]),
                    str(row["on_delete"]).upper(),
                )
                for row in connection.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
            }
            wanted_foreign_keys = set(expected_foreign_keys.get(table, ()))
            if actual_foreign_keys != wanted_foreign_keys:
                raise MigrationError(
                    f"schema table {table} has unexpected foreign keys: "
                    f"expected {sorted(wanted_foreign_keys)}, "
                    f"got {sorted(actual_foreign_keys)}"
                )

        expected_indexes = self._index_contract(version)
        if allow_missing_evaluation_events:
            expected_indexes.pop("idx_evaluation_run_events_run_sequence")
        for index, expected_index_columns in expected_indexes.items():
            index_row = None
            for table in required_tables:
                index_row = next(
                    (
                        row
                        for row in connection.execute(f'PRAGMA index_list("{table}")').fetchall()
                        if str(row["name"]) == index
                    ),
                    None,
                )
                if index_row is not None:
                    break
            if (
                index_row is None
                or bool(index_row["unique"])
                or str(index_row["origin"]) != "c"
                or bool(index_row["partial"])
            ):
                raise MigrationError(f"schema index {index} has unexpected attributes")
            actual_index_columns = tuple(
                str(row["name"])
                for row in connection.execute(f'PRAGMA index_info("{index}")').fetchall()
            )
            if actual_index_columns != expected_index_columns:
                raise MigrationError(
                    f"schema index {index} has unexpected columns: {actual_index_columns!r}"
                )

        if version >= 2:
            fts_row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'memory_fts'"
            ).fetchone()
            if fts_row is None or "using fts5" not in str(fts_row["sql"]).lower():
                raise MigrationError("memory_fts is not an FTS5 virtual table")
            fts_columns = {
                str(row["name"])
                for row in connection.execute('PRAGMA table_info("memory_fts")').fetchall()
            }
            if fts_columns != self._v2_required_columns()["memory_fts"]:
                raise MigrationError("memory_fts has unexpected columns")
            self._validate_fts5_integrity(connection)

        if version >= 3:
            approval_sql_row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'approval_decisions'"
            ).fetchone()
            normalized_sql = re.sub(
                r"\s+", "", "" if approval_sql_row is None else str(approval_sql_row["sql"])
            ).lower()
            if "check(approvedin(0,1))" not in normalized_sql:
                raise MigrationError("approval_decisions is missing its approved CHECK constraint")

        try:
            foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        except sqlite3.DatabaseError as exc:
            raise MigrationError("schema foreign-key validation failed") from exc
        if foreign_key_errors:
            first = foreign_key_errors[0]
            raise MigrationError(
                "foreign_key_check failed for "
                f"{first['table']} rowid={first['rowid']} parent={first['parent']}"
            )

    @staticmethod
    def _validate_fts5_integrity(connection: sqlite3.Connection) -> None:
        """Run FTS5's full shadow-index check without retaining writes."""

        connection.execute("SAVEPOINT operant_fts5_integrity")
        try:
            connection.execute(
                "INSERT INTO memory_fts(memory_fts, rank) VALUES('integrity-check', 1)"
            )
        except sqlite3.DatabaseError as exc:
            connection.execute("ROLLBACK TO operant_fts5_integrity")
            connection.execute("RELEASE operant_fts5_integrity")
            raise MigrationError("memory_fts failed its FTS5 integrity check") from exc
        connection.execute("ROLLBACK TO operant_fts5_integrity")
        connection.execute("RELEASE operant_fts5_integrity")

    def _upgrade_v1(self, connection: sqlite3.Connection) -> None:
        self._execute_sql_batch(
            connection,
            """
                CREATE TABLE IF NOT EXISTS model_profiles (
                    id TEXT PRIMARY KEY,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS role_heads (
                    id TEXT PRIMARY KEY,
                    current_version INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS role_versions (
                    role_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (role_id, version),
                    FOREIGN KEY (role_id) REFERENCES role_heads(id)
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agents (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                );

                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT UNIQUE NOT NULL,
                    session_id TEXT NOT NULL,
                    agent_id TEXT,
                    event_type TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                );
                """,
        )

    def _upgrade_v2(self, connection: sqlite3.Connection) -> None:
        self._execute_sql_batch(
            connection,
            """
                CREATE TABLE IF NOT EXISTS workflow_runs (
                    id TEXT PRIMARY KEY,
                    body TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_stage TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS workflow_run_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT UNIQUE NOT NULL,
                    workflow_run_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    session_id TEXT,
                    event_type TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (workflow_run_id) REFERENCES workflow_runs(id)
                );

                CREATE INDEX IF NOT EXISTS idx_workflow_run_events_run_sequence
                    ON workflow_run_events(workflow_run_id, sequence);

                CREATE TABLE IF NOT EXISTS evaluation_suites (
                    id TEXT PRIMARY KEY,
                    body TEXT NOT NULL,
                    experiment TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS evaluation_cases (
                    id TEXT PRIMARY KEY,
                    suite_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(suite_id, ordinal),
                    FOREIGN KEY (suite_id) REFERENCES evaluation_suites(id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS evaluation_variants (
                    id TEXT PRIMARY KEY,
                    suite_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(suite_id, ordinal),
                    FOREIGN KEY (suite_id) REFERENCES evaluation_suites(id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS evaluation_runs (
                    id TEXT PRIMARY KEY,
                    suite_id TEXT NOT NULL,
                    body TEXT NOT NULL,
                    status TEXT NOT NULL,
                    execution_strategy TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (suite_id) REFERENCES evaluation_suites(id)
                );

                CREATE TABLE IF NOT EXISTS evaluation_results (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    variant_id TEXT NOT NULL,
                    repetition INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(run_id, case_id, variant_id, repetition),
                    FOREIGN KEY (run_id) REFERENCES evaluation_runs(id),
                    FOREIGN KEY (case_id) REFERENCES evaluation_cases(id),
                    FOREIGN KEY (variant_id) REFERENCES evaluation_variants(id)
                );

                CREATE INDEX IF NOT EXISTS idx_evaluation_cases_suite_ordinal
                    ON evaluation_cases(suite_id, ordinal);
                CREATE INDEX IF NOT EXISTS idx_evaluation_variants_suite_ordinal
                    ON evaluation_variants(suite_id, ordinal);
                CREATE INDEX IF NOT EXISTS idx_evaluation_runs_suite_status_created
                    ON evaluation_runs(suite_id, status, created_at);

                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    current_version INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memory_versions (
                    memory_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    body TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    project_scope TEXT,
                    role_scope TEXT NOT NULL,
                    source_session_id TEXT,
                    source_task TEXT,
                    confidence REAL NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (memory_id, version),
                    FOREIGN KEY (memory_id) REFERENCES memories(id)
                );

                CREATE INDEX IF NOT EXISTS idx_memory_versions_source_session
                    ON memory_versions(source_session_id);
                CREATE INDEX IF NOT EXISTS idx_memory_versions_project_scope
                    ON memory_versions(project_scope);

                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                    memory_id UNINDEXED,
                    version UNINDEXED,
                    content,
                    source_task,
                    project_scope
                );
                """,
        )
        self._ensure_evaluation_result_columns(connection)
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_evaluation_results_run_status_created
            ON evaluation_results(run_id, status, created_at)
            """
        )

    def _upgrade_v3(self, connection: sqlite3.Connection) -> None:
        self._execute_sql_batch(
            connection,
            """
            CREATE INDEX idx_events_session_sequence
                ON events(session_id, sequence);

            CREATE TABLE tool_action_receipts (
                id TEXT PRIMARY KEY,
                scope TEXT NOT NULL,
                session_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                action_hash TEXT NOT NULL,
                command_name TEXT NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT,
                error_code TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                UNIQUE(scope, idempotency_key),
                FOREIGN KEY (session_id) REFERENCES sessions(id),
                FOREIGN KEY (agent_id) REFERENCES agents(id)
            );

            CREATE INDEX idx_tool_action_receipts_session_created
                ON tool_action_receipts(session_id, created_at);

            CREATE TABLE command_executions (
                id TEXT PRIMARY KEY,
                command_type TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                action_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                resource_type TEXT,
                resource_id TEXT,
                response_json TEXT,
                http_status INTEGER,
                error_code TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                UNIQUE(command_type, idempotency_key)
            );

            CREATE INDEX idx_command_executions_status_updated
                ON command_executions(status, updated_at);

            CREATE TABLE approval_requests (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                tool_action_receipt_id TEXT NOT NULL,
                tool_call_id TEXT NOT NULL,
                action_hash TEXT NOT NULL,
                category TEXT NOT NULL,
                detail_summary TEXT NOT NULL,
                status TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                decided_at TEXT,
                UNIQUE(agent_id, tool_call_id),
                FOREIGN KEY (session_id) REFERENCES sessions(id),
                FOREIGN KEY (agent_id) REFERENCES agents(id),
                FOREIGN KEY (tool_action_receipt_id) REFERENCES tool_action_receipts(id)
            );

            CREATE INDEX idx_approval_requests_session_status_requested
                ON approval_requests(session_id, status, requested_at);

            CREATE TABLE approval_decisions (
                id TEXT PRIMARY KEY,
                approval_id TEXT UNIQUE NOT NULL,
                approved INTEGER NOT NULL CHECK (approved IN (0, 1)),
                decided_by TEXT NOT NULL,
                reason_code TEXT,
                decided_at TEXT NOT NULL,
                FOREIGN KEY (approval_id) REFERENCES approval_requests(id)
            );

            CREATE TABLE approval_audit_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT UNIQUE NOT NULL,
                approval_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (approval_id) REFERENCES approval_requests(id)
            );

            CREATE INDEX idx_approval_audit_approval_sequence
                ON approval_audit_events(approval_id, sequence);

            CREATE TABLE evaluation_run_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT UNIQUE NOT NULL,
                evaluation_run_id TEXT NOT NULL,
                result_id TEXT,
                event_type TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (evaluation_run_id) REFERENCES evaluation_runs(id),
                FOREIGN KEY (result_id) REFERENCES evaluation_results(id)
            );

            CREATE INDEX idx_evaluation_run_events_run_sequence
                ON evaluation_run_events(evaluation_run_id, sequence);
            """,
        )

    def _upgrade_preview_v8_context_scope_guard(self, connection: sqlite3.Connection) -> None:
        """Upgrade only the exact unmerged Phase 1D preview guards."""

        self._execute_sql_batch(
            connection,
            """
            DROP TRIGGER context_revisions_scope_guard;

            CREATE TRIGGER context_revisions_scope_guard
            BEFORE INSERT ON context_revisions
            WHEN NOT EXISTS (
                    SELECT 1 FROM agents
                    WHERE id = NEW.agent_id AND session_id = NEW.session_id
                )
                OR (
                    NEW.thread_id IS NOT NULL
                    AND NEW.workspace_ref IS NOT NULL
                    AND NOT EXISTS (
                        SELECT 1 FROM threads
                        WHERE id = NEW.thread_id
                            AND workspace_ref = NEW.workspace_ref
                    )
                )
                OR (
                    NEW.compaction_id IS NOT NULL
                    AND NOT EXISTS (
                        SELECT 1 FROM compactions
                        WHERE id = NEW.compaction_id
                            AND session_id = NEW.session_id
                            AND thread_id IS NEW.thread_id
                            AND (
                                source_type = 'thread_items'
                                OR agent_id = NEW.agent_id
                            )
                    )
                )
                OR NEW.prompt_layout_version != 'phase1b.v1'
                OR (
                    (NEW.source_cursor_start IS NULL)
                    != (NEW.source_cursor_end IS NULL)
                )
                OR (
                    NEW.source_cursor_start IS NULL
                    AND NEW.source_cursor_namespace IS NOT NULL
                )
                OR (
                    NEW.source_cursor_start IS NOT NULL
                    AND COALESCE(NEW.source_cursor_namespace, '') != 'items.sequence'
                )
                OR (
                    NEW.thread_id IS NULL
                    AND (
                        NEW.source_cursor_start IS NOT NULL
                        OR NEW.source_cursor_end IS NOT NULL
                        OR json_array_length(NEW.source_item_ids_json) != 0
                    )
                )
                OR (
                    NEW.source_cursor_start IS NOT NULL
                    AND (
                        NOT EXISTS (
                            SELECT 1 FROM items
                            WHERE sequence = NEW.source_cursor_start
                                AND thread_id = NEW.thread_id
                        )
                        OR NOT EXISTS (
                            SELECT 1 FROM items
                            WHERE sequence = NEW.source_cursor_end
                                AND thread_id = NEW.thread_id
                        )
                    )
                )
                OR EXISTS (
                    SELECT 1 FROM json_each(NEW.source_item_ids_json) AS item_id
                    WHERE NOT EXISTS (
                        SELECT 1 FROM items
                        WHERE id = item_id.value AND thread_id = NEW.thread_id
                    )
                )
                OR (
                    NEW.compaction_id IS NULL
                    AND json_array_length(NEW.compaction_refs_json) != 0
                )
                OR (
                    NEW.compaction_id IS NOT NULL
                    AND (
                        json_array_length(NEW.compaction_refs_json) != 1
                        OR json_extract(NEW.compaction_refs_json, '$[0]') != NEW.compaction_id
                    )
                )
                OR json_type(NEW.memory_refs_json) != 'array'
                OR EXISTS (
                    SELECT 1 FROM json_each(NEW.memory_refs_json) AS memory_ref
                    WHERE NOT EXISTS (
                        SELECT 1 FROM memories WHERE id = memory_ref.value
                    )
                )
            BEGIN
                SELECT RAISE(ABORT, 'ContextRevision source scope is invalid');
            END;
            """,
        )
        self._replace_phase1d_prompt_compaction_agent_guard(
            connection,
            allow_thread_items_cross_agent=True,
        )
        self._create_phase1d_run_scope_guards(connection)

    @staticmethod
    def _create_phase1d_run_scope_guards(connection: sqlite3.Connection) -> None:
        """Bind Review and Sidecar immutable identities to their declared scope."""

        SQLiteStore._execute_sql_batch(
            connection,
            """
            CREATE TRIGGER review_runs_scope_guard
            BEFORE INSERT ON review_runs
            WHEN NEW.thread_id IS NOT NULL
                AND NOT EXISTS (
                    SELECT 1 FROM threads
                    WHERE id = NEW.thread_id AND workspace_ref = NEW.workspace_ref
                )
            BEGIN
                SELECT RAISE(ABORT, 'Review run scope is invalid');
            END;

            CREATE TRIGGER review_runs_identity_guard
            BEFORE UPDATE ON review_runs
            WHEN OLD.session_id != NEW.session_id
                OR OLD.thread_id IS NOT NEW.thread_id
                OR OLD.workspace_ref != NEW.workspace_ref
                OR OLD.scope != NEW.scope
                OR OLD.created_at != NEW.created_at
            BEGIN
                SELECT RAISE(ABORT, 'Review run identity is immutable');
            END;

            CREATE TRIGGER btw_sidecar_runs_scope_guard
            BEFORE INSERT ON btw_sidecar_runs
            WHEN NOT EXISTS (
                    SELECT 1 FROM agents
                    WHERE id = NEW.agent_id AND session_id = NEW.session_id
                )
                OR NOT EXISTS (
                    SELECT 1 FROM threads
                    WHERE id = NEW.thread_id AND workspace_ref = NEW.workspace_ref
                )
            BEGIN
                SELECT RAISE(ABORT, 'BTW Sidecar run scope is invalid');
            END;

            CREATE TRIGGER btw_sidecar_runs_identity_guard
            BEFORE UPDATE ON btw_sidecar_runs
            WHEN OLD.session_id != NEW.session_id
                OR OLD.agent_id != NEW.agent_id
                OR OLD.thread_id != NEW.thread_id
                OR OLD.workspace_ref != NEW.workspace_ref
                OR OLD.source_item_cursor_end != NEW.source_item_cursor_end
                OR OLD.prompt != NEW.prompt
                OR OLD.prompt_hash != NEW.prompt_hash
                OR OLD.created_at != NEW.created_at
            BEGIN
                SELECT RAISE(ABORT, 'BTW Sidecar run identity is immutable');
            END;
            """,
        )

    @staticmethod
    def _replace_phase1d_prompt_compaction_agent_guard(
        connection: sqlite3.Connection,
        *,
        allow_thread_items_cross_agent: bool,
    ) -> None:
        """Replace only the Compaction ownership clause in the v6 Prompt Block guard."""

        row = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'trigger' AND name = 'prompt_blocks_source_refs_guard'"
        ).fetchone()
        if row is None or row["sql"] is None:
            raise MigrationError("Prompt Block source reference guard is missing")
        strict = """AND r.session_id = c.session_id
                                    AND r.agent_id = c.agent_id
                                    AND r.thread_id IS c.thread_id"""
        relaxed = """AND r.session_id = c.session_id
                                    AND r.thread_id IS c.thread_id
                                    AND (
                                        c.source_type = 'thread_items'
                                        OR r.agent_id = c.agent_id
                                    )"""
        source = strict if allow_thread_items_cross_agent else relaxed
        replacement = relaxed if allow_thread_items_cross_agent else strict
        trigger_sql = str(row["sql"])
        if trigger_sql.count(source) != 1:
            raise MigrationError("Prompt Block Compaction ownership guard has drifted")
        connection.execute("DROP TRIGGER prompt_blocks_source_refs_guard")
        connection.execute(trigger_sql.replace(source, replacement, 1))

    def _upgrade_preview_v3_evaluation_events(self, connection: sqlite3.Connection) -> None:
        """Bring the exact first M0 preview schema to the final v3 contract."""

        self._execute_sql_batch(
            connection,
            """
            CREATE TABLE evaluation_run_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT UNIQUE NOT NULL,
                evaluation_run_id TEXT NOT NULL,
                result_id TEXT,
                event_type TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (evaluation_run_id) REFERENCES evaluation_runs(id),
                FOREIGN KEY (result_id) REFERENCES evaluation_results(id)
            );

            CREATE INDEX idx_evaluation_run_events_run_sequence
                ON evaluation_run_events(evaluation_run_id, sequence);
            """,
        )

    def _upgrade_v4(self, connection: sqlite3.Connection) -> None:
        self._execute_sql_batch(
            connection,
            """
            CREATE TABLE session_run_leases (
                session_id TEXT PRIMARY KEY,
                lease_token TEXT UNIQUE NOT NULL,
                owner_id TEXT NOT NULL,
                generation INTEGER NOT NULL CHECK (generation >= 1),
                workflow_run_id TEXT,
                agent_id TEXT,
                cancel_requested INTEGER NOT NULL CHECK (cancel_requested IN (0, 1)),
                acquired_at TEXT NOT NULL,
                renewed_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                released_at TEXT,
                FOREIGN KEY (session_id) REFERENCES sessions(id),
                FOREIGN KEY (workflow_run_id) REFERENCES workflow_runs(id),
                FOREIGN KEY (agent_id) REFERENCES agents(id)
            );

            CREATE INDEX idx_session_run_leases_workflow_active
                ON session_run_leases(workflow_run_id, released_at);

            CREATE TABLE workflow_execution_leases (
                workflow_run_id TEXT PRIMARY KEY,
                lease_token TEXT UNIQUE NOT NULL,
                owner_id TEXT NOT NULL,
                generation INTEGER NOT NULL CHECK (generation >= 1),
                acquired_at TEXT NOT NULL,
                renewed_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                released_at TEXT,
                FOREIGN KEY (workflow_run_id) REFERENCES workflow_runs(id)
            );

            CREATE INDEX idx_workflow_execution_leases_active
                ON workflow_execution_leases(released_at, expires_at);
            """,
        )
        # Any RUNNING row that predates v4 cannot own a v4 execution guard or
        # Session lease and is therefore an actual interrupted legacy run.
        self._interrupt_running_workflows(connection)

    def _downgrade_v4(self, connection: sqlite3.Connection) -> None:
        populated = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM session_run_leases)
                + (SELECT COUNT(*) FROM workflow_execution_leases) AS row_count
            """
        ).fetchone()
        if populated is not None and int(populated["row_count"]) > 0:
            raise MigrationError("refusing to roll back execution leases while they contain data")
        self._execute_sql_batch(
            connection,
            """
            DROP INDEX idx_workflow_execution_leases_active;
            DROP TABLE workflow_execution_leases;
            DROP INDEX idx_session_run_leases_workflow_active;
            DROP TABLE session_run_leases;
            """,
        )

    def _upgrade_preview_v5_reference_guards(self, connection: sqlite3.Connection) -> None:
        """Upgrade only the exact unmerged Phase 1A preview schema."""

        self._execute_sql_batch(
            connection,
            """
            CREATE TRIGGER items_tool_call_unique_guard
            BEFORE INSERT ON items
            WHEN NEW.item_type = 'tool_call'
                AND EXISTS (
                    SELECT 1 FROM items
                    WHERE thread_id = NEW.thread_id
                        AND item_type = 'tool_call'
                        AND json_extract(body, '$.payload.tool_call_id')
                            = json_extract(NEW.body, '$.payload.tool_call_id')
                )
            BEGIN
                SELECT RAISE(ABORT, 'duplicate tool call in thread');
            END;

            CREATE TRIGGER items_tool_result_call_guard
            BEFORE INSERT ON items
            WHEN NEW.item_type = 'tool_result_ref'
                AND NOT EXISTS (
                    SELECT 1 FROM items
                    WHERE thread_id = NEW.thread_id
                        AND position < NEW.position
                        AND item_type = 'tool_call'
                        AND json_extract(body, '$.payload.tool_call_id')
                            = json_extract(NEW.body, '$.payload.tool_call_id')
                )
            BEGIN
                SELECT RAISE(ABORT, 'tool call reference not found in thread');
            END;

            CREATE TRIGGER artifact_source_refs_insert_guard
            BEFORE INSERT ON artifact_source_refs
            WHEN (NEW.source_type = 'thread' AND NOT EXISTS (
                    SELECT 1 FROM threads WHERE id = NEW.source_id
                ))
                OR (NEW.source_type = 'turn' AND NOT EXISTS (
                    SELECT 1 FROM turns WHERE id = NEW.source_id
                ))
                OR (NEW.source_type = 'item' AND NOT EXISTS (
                    SELECT 1 FROM items WHERE id = NEW.source_id
                ))
                OR (NEW.source_type = 'session' AND NOT EXISTS (
                    SELECT 1 FROM sessions WHERE id = NEW.source_id
                ))
                OR (NEW.source_type = 'agent' AND NOT EXISTS (
                    SELECT 1 FROM agents WHERE id = NEW.source_id
                ))
                OR (NEW.source_type = 'workflow_run' AND NOT EXISTS (
                    SELECT 1 FROM workflow_runs WHERE id = NEW.source_id
                ))
                OR (NEW.source_type = 'tool_action_receipt' AND NOT EXISTS (
                    SELECT 1 FROM tool_action_receipts WHERE id = NEW.source_id
                ))
                OR (NEW.source_type = 'approval' AND NOT EXISTS (
                    SELECT 1 FROM approval_requests WHERE id = NEW.source_id
                ))
                OR (NEW.source_type = 'evaluation_run' AND NOT EXISTS (
                    SELECT 1 FROM evaluation_runs WHERE id = NEW.source_id
                ))
                OR (NEW.source_type = 'tool_call'
                    AND NOT EXISTS (
                        SELECT 1 FROM items
                        WHERE item_type = 'tool_call'
                            AND json_extract(body, '$.payload.tool_call_id') = NEW.source_id
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM approval_requests WHERE tool_call_id = NEW.source_id
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM tool_action_receipts
                        WHERE idempotency_key = NEW.source_id
                    ))
            BEGIN
                SELECT RAISE(ABORT, 'artifact source reference not found');
            END;

            CREATE TRIGGER thread_legacy_refs_insert_guard
            BEFORE INSERT ON thread_legacy_refs
            WHEN (NEW.source_type = 'session' AND NOT EXISTS (
                    SELECT 1 FROM sessions WHERE id = NEW.source_id
                ))
                OR (NEW.source_type = 'workflow_run' AND NOT EXISTS (
                    SELECT 1 FROM workflow_runs WHERE id = NEW.source_id
                ))
            BEGIN
                SELECT RAISE(ABORT, 'legacy source not found');
            END;
            """,
        )

    def _validate_preview_v5_reference_rows(self, connection: sqlite3.Connection) -> None:
        duplicate_tool_call = connection.execute(
            """
            SELECT 1 FROM items
            WHERE item_type = 'tool_call'
            GROUP BY thread_id, json_extract(body, '$.payload.tool_call_id')
            HAVING COUNT(*) > 1
            LIMIT 1
            """
        ).fetchone()
        bogus_tool_result = connection.execute(
            """
            SELECT 1 FROM items AS result
            WHERE result.item_type = 'tool_result_ref'
                AND NOT EXISTS (
                    SELECT 1 FROM items AS call
                    WHERE call.thread_id = result.thread_id
                        AND call.position < result.position
                        AND call.item_type = 'tool_call'
                        AND json_extract(call.body, '$.payload.tool_call_id')
                            = json_extract(result.body, '$.payload.tool_call_id')
                )
            LIMIT 1
            """
        ).fetchone()
        if duplicate_tool_call is not None or bogus_tool_result is not None:
            raise MigrationError("Phase 1A preview contains invalid tool call history")
        try:
            for row in connection.execute(
                "SELECT source_type, source_id FROM artifact_source_refs"
            ).fetchall():
                self._validate_artifact_source_ref(
                    connection,
                    ArtifactSourceRef.model_validate(
                        {
                            "source_type": str(row["source_type"]),
                            "source_id": str(row["source_id"]),
                        }
                    ),
                )
            for row in connection.execute(
                "SELECT source_type, source_id FROM thread_legacy_refs"
            ).fetchall():
                self._validate_legacy_ref(
                    connection,
                    ThreadLegacyRef.model_validate(
                        {
                            "source_type": str(row["source_type"]),
                            "source_id": str(row["source_id"]),
                        }
                    ),
                )
        except (NotFoundError, ValueError) as exc:
            raise MigrationError("Phase 1A preview contains invalid source references") from exc

    def _upgrade_v5(self, connection: sqlite3.Connection) -> None:
        self._execute_sql_batch(
            connection,
            """
            CREATE TABLE threads (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT UNIQUE NOT NULL,
                parent_thread_id TEXT,
                workspace_ref TEXT,
                status TEXT NOT NULL CHECK (
                    status IN ('active', 'completed', 'cancelled', 'archived')
                ),
                body TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                archived_at TEXT,
                CHECK (
                    (status = 'archived' AND archived_at IS NOT NULL)
                    OR (status != 'archived' AND archived_at IS NULL)
                ),
                CHECK (parent_thread_id IS NULL OR parent_thread_id != id),
                CHECK (workspace_ref IS NULL OR length(workspace_ref) >= 1),
                FOREIGN KEY (parent_thread_id) REFERENCES threads(id)
            );

            CREATE INDEX idx_threads_parent_sequence
                ON threads(parent_thread_id, sequence);
            CREATE INDEX idx_threads_workspace_status_sequence
                ON threads(workspace_ref, status, sequence);

            CREATE TABLE turns (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT UNIQUE NOT NULL,
                thread_id TEXT NOT NULL,
                position INTEGER NOT NULL CHECK (position >= 1),
                body TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(thread_id, position),
                UNIQUE(id, thread_id),
                FOREIGN KEY (thread_id) REFERENCES threads(id)
            );

            CREATE INDEX idx_turns_thread_sequence
                ON turns(thread_id, sequence);

            CREATE TABLE items (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT UNIQUE NOT NULL,
                thread_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                position INTEGER NOT NULL CHECK (position >= 1),
                item_type TEXT NOT NULL CHECK (
                    item_type IN (
                        'user_message', 'agent_message', 'tool_call',
                        'tool_result_ref', 'artifact_ref', 'approval_link',
                        'steering', 'system_event'
                    )
                ),
                body TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(thread_id, position),
                FOREIGN KEY (turn_id, thread_id) REFERENCES turns(id, thread_id)
            );

            CREATE INDEX idx_items_thread_sequence
                ON items(thread_id, sequence);
            CREATE INDEX idx_items_turn_sequence
                ON items(turn_id, sequence);

            CREATE TABLE artifact_blobs (
                content_hash TEXT PRIMARY KEY CHECK (
                    length(content_hash) = 64
                    AND content_hash NOT GLOB '*[^0-9a-f]*'
                ),
                storage_key TEXT UNIQUE NOT NULL CHECK (
                    storage_key = 'sha256/' || substr(content_hash, 1, 2)
                        || '/' || substr(content_hash, 3, 2) || '/' || content_hash
                ),
                size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
                created_at TEXT NOT NULL
            );

            CREATE TABLE artifacts (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT UNIQUE NOT NULL,
                content_hash TEXT NOT NULL CHECK (
                    length(content_hash) = 64
                    AND content_hash NOT GLOB '*[^0-9a-f]*'
                ),
                media_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
                sensitivity TEXT NOT NULL CHECK (
                    sensitivity IN ('normal', 'sensitive', 'restricted')
                ),
                retention_policy_ref TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(content_hash),
                CHECK (length(media_type) >= 1),
                CHECK (length(retention_policy_ref) >= 1),
                FOREIGN KEY (content_hash) REFERENCES artifact_blobs(content_hash)
            );

            CREATE INDEX idx_artifacts_hash_sequence
                ON artifacts(content_hash, sequence);

            CREATE TABLE artifact_source_refs (
                artifact_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
                source_type TEXT NOT NULL CHECK (
                    source_type IN (
                        'thread', 'turn', 'item', 'session', 'agent',
                        'workflow_run', 'tool_call', 'tool_action_receipt',
                        'approval', 'evaluation_run'
                    )
                ),
                source_id TEXT NOT NULL,
                PRIMARY KEY (artifact_id, ordinal),
                UNIQUE(artifact_id, source_type, source_id),
                CHECK (length(source_id) >= 1),
                FOREIGN KEY (artifact_id) REFERENCES artifacts(id)
            );

            CREATE INDEX idx_artifact_sources_type_id
                ON artifact_source_refs(source_type, source_id);

            CREATE TABLE thread_legacy_refs (
                thread_id TEXT NOT NULL,
                source_type TEXT NOT NULL CHECK (source_type IN ('session', 'workflow_run')),
                source_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (source_type, source_id),
                UNIQUE(thread_id, source_type, source_id),
                FOREIGN KEY (thread_id) REFERENCES threads(id)
            );

            CREATE INDEX idx_thread_legacy_refs_thread
                ON thread_legacy_refs(thread_id);

            CREATE TRIGGER threads_identity_no_update
            BEFORE UPDATE OF id, parent_thread_id, workspace_ref, created_at ON threads
            BEGIN
                SELECT RAISE(ABORT, 'thread identity is immutable');
            END;

            CREATE TRIGGER threads_body_guard_insert
            BEFORE INSERT ON threads
            WHEN json_extract(NEW.body, '$.id') IS NOT NEW.id
                OR json_extract(NEW.body, '$.parent_thread_id') IS NOT NEW.parent_thread_id
                OR json_extract(NEW.body, '$.workspace_ref') IS NOT NEW.workspace_ref
                OR json_extract(NEW.body, '$.status') IS NOT NEW.status
            BEGIN
                SELECT RAISE(ABORT, 'thread body metadata mismatch');
            END;

            CREATE TRIGGER threads_body_guard_update
            BEFORE UPDATE OF body, status, updated_at, archived_at ON threads
            WHEN json_extract(NEW.body, '$.id') IS NOT NEW.id
                OR json_extract(NEW.body, '$.parent_thread_id') IS NOT NEW.parent_thread_id
                OR json_extract(NEW.body, '$.workspace_ref') IS NOT NEW.workspace_ref
                OR json_extract(NEW.body, '$.status') IS NOT NEW.status
            BEGIN
                SELECT RAISE(ABORT, 'thread body metadata mismatch');
            END;

            CREATE TRIGGER threads_no_delete
            BEFORE DELETE ON threads
            BEGIN
                SELECT RAISE(ABORT, 'canonical thread history cannot be deleted');
            END;

            CREATE TRIGGER threads_status_transition_guard
            BEFORE UPDATE OF status ON threads
            WHEN OLD.status != NEW.status
                AND NOT (
                    (OLD.status = 'active' AND NEW.status IN (
                        'completed', 'cancelled', 'archived'
                    ))
                    OR (
                        OLD.status IN ('completed', 'cancelled')
                        AND NEW.status = 'archived'
                    )
                )
            BEGIN
                SELECT RAISE(ABORT, 'invalid thread status transition');
            END;

            CREATE TRIGGER turns_active_thread_guard
            BEFORE INSERT ON turns
            WHEN NOT EXISTS (
                SELECT 1 FROM threads
                WHERE id = NEW.thread_id AND status = 'active'
            )
            BEGIN
                SELECT RAISE(ABORT, 'thread is not active');
            END;

            CREATE TRIGGER turns_no_update
            BEFORE UPDATE ON turns
            BEGIN
                SELECT RAISE(ABORT, 'canonical history is append-only');
            END;

            CREATE TRIGGER turns_body_guard
            BEFORE INSERT ON turns
            WHEN json_extract(NEW.body, '$.id') IS NOT NEW.id
                OR json_extract(NEW.body, '$.thread_id') IS NOT NEW.thread_id
                OR json_extract(NEW.body, '$.position') IS NOT NEW.position
            BEGIN
                SELECT RAISE(ABORT, 'turn body metadata mismatch');
            END;

            CREATE TRIGGER turns_no_delete
            BEFORE DELETE ON turns
            BEGIN
                SELECT RAISE(ABORT, 'canonical history is append-only');
            END;

            CREATE TRIGGER items_no_update
            BEFORE UPDATE ON items
            BEGIN
                SELECT RAISE(ABORT, 'canonical history is append-only');
            END;

            CREATE TRIGGER items_no_delete
            BEFORE DELETE ON items
            BEGIN
                SELECT RAISE(ABORT, 'canonical history is append-only');
            END;

            CREATE TRIGGER items_active_thread_guard
            BEFORE INSERT ON items
            WHEN NOT EXISTS (
                SELECT 1 FROM threads
                WHERE id = NEW.thread_id AND status = 'active'
            )
            BEGIN
                SELECT RAISE(ABORT, 'thread is not active');
            END;

            CREATE TRIGGER items_body_guard
            BEFORE INSERT ON items
            WHEN json_extract(NEW.body, '$.id') IS NOT NEW.id
                OR json_extract(NEW.body, '$.thread_id') IS NOT NEW.thread_id
                OR json_extract(NEW.body, '$.turn_id') IS NOT NEW.turn_id
                OR json_extract(NEW.body, '$.position') IS NOT NEW.position
                OR json_extract(NEW.body, '$.payload.type') IS NOT NEW.item_type
            BEGIN
                SELECT RAISE(ABORT, 'item body metadata mismatch');
            END;

            CREATE TRIGGER items_artifact_ref_guard
            BEFORE INSERT ON items
            WHEN NEW.item_type IN ('artifact_ref', 'tool_result_ref')
                AND NOT EXISTS (
                    SELECT 1 FROM artifacts
                    WHERE id = json_extract(NEW.body, '$.payload.artifact_id')
                )
            BEGIN
                SELECT RAISE(ABORT, 'artifact reference not found');
            END;

            CREATE TRIGGER items_tool_call_unique_guard
            BEFORE INSERT ON items
            WHEN NEW.item_type = 'tool_call'
                AND EXISTS (
                    SELECT 1 FROM items
                    WHERE thread_id = NEW.thread_id
                        AND item_type = 'tool_call'
                        AND json_extract(body, '$.payload.tool_call_id')
                            = json_extract(NEW.body, '$.payload.tool_call_id')
                )
            BEGIN
                SELECT RAISE(ABORT, 'duplicate tool call in thread');
            END;

            CREATE TRIGGER items_tool_result_call_guard
            BEFORE INSERT ON items
            WHEN NEW.item_type = 'tool_result_ref'
                AND NOT EXISTS (
                    SELECT 1 FROM items
                    WHERE thread_id = NEW.thread_id
                        AND position < NEW.position
                        AND item_type = 'tool_call'
                        AND json_extract(body, '$.payload.tool_call_id')
                            = json_extract(NEW.body, '$.payload.tool_call_id')
                )
            BEGIN
                SELECT RAISE(ABORT, 'tool call reference not found in thread');
            END;

            CREATE TRIGGER items_approval_link_guard
            BEFORE INSERT ON items
            WHEN NEW.item_type = 'approval_link'
                AND NOT EXISTS (
                    SELECT 1 FROM approval_requests
                    WHERE id = json_extract(NEW.body, '$.payload.approval_id')
                )
            BEGIN
                SELECT RAISE(ABORT, 'approval reference not found');
            END;

            CREATE TRIGGER artifacts_blob_size_guard
            BEFORE INSERT ON artifacts
            WHEN NOT EXISTS (
                SELECT 1 FROM artifact_blobs
                WHERE content_hash = NEW.content_hash
                    AND size_bytes = NEW.size_bytes
            )
            BEGIN
                SELECT RAISE(ABORT, 'artifact blob metadata mismatch');
            END;

            CREATE TRIGGER artifacts_body_guard
            BEFORE INSERT ON artifacts
            WHEN json_extract(NEW.body, '$.id') IS NOT NEW.id
                OR json_extract(NEW.body, '$.content_hash') IS NOT NEW.content_hash
                OR json_extract(NEW.body, '$.media_type') IS NOT NEW.media_type
                OR json_extract(NEW.body, '$.size_bytes') IS NOT NEW.size_bytes
                OR json_extract(NEW.body, '$.sensitivity') IS NOT NEW.sensitivity
                OR json_extract(NEW.body, '$.retention_policy_ref')
                    IS NOT NEW.retention_policy_ref
            BEGIN
                SELECT RAISE(ABORT, 'artifact body metadata mismatch');
            END;

            CREATE TRIGGER artifact_blobs_no_update
            BEFORE UPDATE ON artifact_blobs
            BEGIN
                SELECT RAISE(ABORT, 'artifact blob identity is immutable');
            END;

            CREATE TRIGGER artifact_blobs_no_delete
            BEFORE DELETE ON artifact_blobs
            BEGIN
                SELECT RAISE(ABORT, 'artifact blob identity is immutable');
            END;

            CREATE TRIGGER artifacts_no_update
            BEFORE UPDATE ON artifacts
            BEGIN
                SELECT RAISE(ABORT, 'artifact metadata is immutable');
            END;

            CREATE TRIGGER artifacts_no_delete
            BEFORE DELETE ON artifacts
            BEGIN
                SELECT RAISE(ABORT, 'artifact metadata is immutable');
            END;

            CREATE TRIGGER artifact_source_refs_no_update
            BEFORE UPDATE ON artifact_source_refs
            BEGIN
                SELECT RAISE(ABORT, 'artifact source refs are immutable');
            END;

            CREATE TRIGGER artifact_source_refs_insert_guard
            BEFORE INSERT ON artifact_source_refs
            WHEN (NEW.source_type = 'thread' AND NOT EXISTS (
                    SELECT 1 FROM threads WHERE id = NEW.source_id
                ))
                OR (NEW.source_type = 'turn' AND NOT EXISTS (
                    SELECT 1 FROM turns WHERE id = NEW.source_id
                ))
                OR (NEW.source_type = 'item' AND NOT EXISTS (
                    SELECT 1 FROM items WHERE id = NEW.source_id
                ))
                OR (NEW.source_type = 'session' AND NOT EXISTS (
                    SELECT 1 FROM sessions WHERE id = NEW.source_id
                ))
                OR (NEW.source_type = 'agent' AND NOT EXISTS (
                    SELECT 1 FROM agents WHERE id = NEW.source_id
                ))
                OR (NEW.source_type = 'workflow_run' AND NOT EXISTS (
                    SELECT 1 FROM workflow_runs WHERE id = NEW.source_id
                ))
                OR (NEW.source_type = 'tool_action_receipt' AND NOT EXISTS (
                    SELECT 1 FROM tool_action_receipts WHERE id = NEW.source_id
                ))
                OR (NEW.source_type = 'approval' AND NOT EXISTS (
                    SELECT 1 FROM approval_requests WHERE id = NEW.source_id
                ))
                OR (NEW.source_type = 'evaluation_run' AND NOT EXISTS (
                    SELECT 1 FROM evaluation_runs WHERE id = NEW.source_id
                ))
                OR (NEW.source_type = 'tool_call'
                    AND NOT EXISTS (
                        SELECT 1 FROM items
                        WHERE item_type = 'tool_call'
                            AND json_extract(body, '$.payload.tool_call_id') = NEW.source_id
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM approval_requests WHERE tool_call_id = NEW.source_id
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM tool_action_receipts
                        WHERE idempotency_key = NEW.source_id
                    ))
            BEGIN
                SELECT RAISE(ABORT, 'artifact source reference not found');
            END;

            CREATE TRIGGER artifact_source_refs_no_delete
            BEFORE DELETE ON artifact_source_refs
            BEGIN
                SELECT RAISE(ABORT, 'artifact source refs are immutable');
            END;

            CREATE TRIGGER thread_legacy_refs_no_update
            BEFORE UPDATE ON thread_legacy_refs
            BEGIN
                SELECT RAISE(ABORT, 'thread legacy refs are immutable');
            END;

            CREATE TRIGGER thread_legacy_refs_insert_guard
            BEFORE INSERT ON thread_legacy_refs
            WHEN (NEW.source_type = 'session' AND NOT EXISTS (
                    SELECT 1 FROM sessions WHERE id = NEW.source_id
                ))
                OR (NEW.source_type = 'workflow_run' AND NOT EXISTS (
                    SELECT 1 FROM workflow_runs WHERE id = NEW.source_id
                ))
            BEGIN
                SELECT RAISE(ABORT, 'legacy source not found');
            END;

            CREATE TRIGGER thread_legacy_refs_no_delete
            BEFORE DELETE ON thread_legacy_refs
            BEGIN
                SELECT RAISE(ABORT, 'thread legacy refs are immutable');
            END;
            """,
        )

    def _downgrade_v5(self, connection: sqlite3.Connection) -> None:
        populated = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM threads)
                + (SELECT COUNT(*) FROM turns)
                + (SELECT COUNT(*) FROM items)
                + (SELECT COUNT(*) FROM artifact_blobs)
                + (SELECT COUNT(*) FROM artifacts)
                + (SELECT COUNT(*) FROM artifact_source_refs)
                + (SELECT COUNT(*) FROM thread_legacy_refs) AS row_count
            """
        ).fetchone()
        if populated is not None and int(populated["row_count"]) > 0:
            raise MigrationError("refusing to roll back Phase 1A tables while they contain data")
        self._execute_sql_batch(
            connection,
            """
            DROP TRIGGER thread_legacy_refs_no_delete;
            DROP TRIGGER thread_legacy_refs_insert_guard;
            DROP TRIGGER thread_legacy_refs_no_update;
            DROP TRIGGER artifact_source_refs_no_delete;
            DROP TRIGGER artifact_source_refs_insert_guard;
            DROP TRIGGER artifact_source_refs_no_update;
            DROP TRIGGER artifacts_no_delete;
            DROP TRIGGER artifacts_no_update;
            DROP TRIGGER artifacts_body_guard;
            DROP TRIGGER artifacts_blob_size_guard;
            DROP TRIGGER artifact_blobs_no_delete;
            DROP TRIGGER artifact_blobs_no_update;
            DROP TRIGGER items_no_delete;
            DROP TRIGGER items_no_update;
            DROP TRIGGER items_approval_link_guard;
            DROP TRIGGER items_tool_result_call_guard;
            DROP TRIGGER items_tool_call_unique_guard;
            DROP TRIGGER items_artifact_ref_guard;
            DROP TRIGGER items_body_guard;
            DROP TRIGGER items_active_thread_guard;
            DROP TRIGGER turns_no_delete;
            DROP TRIGGER turns_no_update;
            DROP TRIGGER turns_body_guard;
            DROP TRIGGER turns_active_thread_guard;
            DROP TRIGGER threads_no_delete;
            DROP TRIGGER threads_status_transition_guard;
            DROP TRIGGER threads_body_guard_update;
            DROP TRIGGER threads_body_guard_insert;
            DROP TRIGGER threads_identity_no_update;
            DROP INDEX idx_thread_legacy_refs_thread;
            DROP TABLE thread_legacy_refs;
            DROP INDEX idx_artifact_sources_type_id;
            DROP TABLE artifact_source_refs;
            DROP INDEX idx_artifacts_hash_sequence;
            DROP TABLE artifacts;
            DROP TABLE artifact_blobs;
            DROP INDEX idx_items_turn_sequence;
            DROP INDEX idx_items_thread_sequence;
            DROP TABLE items;
            DROP INDEX idx_turns_thread_sequence;
            DROP TABLE turns;
            DROP INDEX idx_threads_workspace_status_sequence;
            DROP INDEX idx_threads_parent_sequence;
            DROP TABLE threads;
            """,
        )

    @staticmethod
    def _ensure_v6_body_hash_column(
        connection: sqlite3.Connection,
        table: str,
    ) -> None:
        """Persist canonical JSON hashes for SQL-only provenance guards."""

        if table not in {"memory_versions", "threads", "items"}:
            raise ValueError("unsupported v6 body-hash table")
        columns = {
            str(row["name"])
            for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        }
        restore_item_guard = table == "items" and "body_hash" not in columns
        if restore_item_guard:
            connection.execute("DROP TRIGGER items_no_update")
        if "body_hash" not in columns:
            connection.execute(
                f"ALTER TABLE \"{table}\" ADD COLUMN body_hash TEXT NOT NULL DEFAULT ''"
            )
        if table == "memory_versions":
            rows = connection.execute(
                'SELECT memory_id, version, body FROM "memory_versions"'
            ).fetchall()
        else:
            rows = connection.execute(f'SELECT sequence, body FROM "{table}"').fetchall()
        for row in rows:
            body_hash = hashlib.sha256(str(row["body"]).encode("utf-8")).hexdigest()
            if table == "memory_versions":
                connection.execute(
                    'UPDATE "memory_versions" SET body_hash = ? '
                    "WHERE memory_id = ? AND version = ?",
                    (body_hash, row["memory_id"], row["version"]),
                )
            else:
                connection.execute(
                    f'UPDATE "{table}" SET body_hash = ? WHERE sequence = ?',
                    (body_hash, row["sequence"]),
                )
        if restore_item_guard:
            connection.execute(
                """
                CREATE TRIGGER items_no_update
                BEFORE UPDATE ON items
                BEGIN
                    SELECT RAISE(ABORT, 'canonical history is append-only');
                END
                """
            )

    def _upgrade_v6(self, connection: sqlite3.Connection) -> None:
        for table in ("memory_versions", "threads", "items"):
            self._ensure_v6_body_hash_column(connection, table)
        self._execute_sql_batch(
            connection,
            """
            CREATE TABLE compactions (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT UNIQUE NOT NULL,
                session_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                thread_id TEXT,
                source_type TEXT NOT NULL CHECK (
                    source_type IN ('context_revisions', 'thread_items')
                ),
                source_cursor_start INTEGER NOT NULL CHECK (source_cursor_start >= 1),
                source_cursor_end INTEGER NOT NULL CHECK (
                    source_cursor_end >= source_cursor_start
                ),
                source_snapshot_hash TEXT NOT NULL CHECK (
                    length(source_snapshot_hash) = 64
                    AND source_snapshot_hash NOT GLOB '*[^0-9a-f]*'
                ),
                summary_json TEXT NOT NULL CHECK (json_valid(summary_json)),
                content_hash TEXT NOT NULL CHECK (
                    length(content_hash) = 64
                    AND content_hash NOT GLOB '*[^0-9a-f]*'
                ),
                covered_item_refs_json TEXT NOT NULL CHECK (
                    json_valid(covered_item_refs_json)
                    AND json_type(covered_item_refs_json) = 'array'
                ),
                created_at TEXT NOT NULL,
                CHECK (source_type != 'thread_items' OR thread_id IS NOT NULL),
                FOREIGN KEY (session_id) REFERENCES sessions(id),
                FOREIGN KEY (agent_id) REFERENCES agents(id),
                FOREIGN KEY (thread_id) REFERENCES threads(id)
            );

            CREATE INDEX idx_compactions_agent_sequence
                ON compactions(agent_id, sequence);
            CREATE INDEX idx_compactions_thread_sequence
                ON compactions(thread_id, sequence);

            CREATE TABLE context_revisions (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT UNIQUE NOT NULL,
                session_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                thread_id TEXT,
                workspace_ref TEXT CHECK (
                    workspace_ref IS NULL OR length(workspace_ref) >= 1
                ),
                request_ordinal INTEGER NOT NULL CHECK (request_ordinal >= 1),
                model_id TEXT NOT NULL,
                prompt_layout_version TEXT NOT NULL CHECK (
                    prompt_layout_version = 'phase1b.v1'
                ),
                context_window INTEGER CHECK (context_window IS NULL OR context_window >= 1),
                reserved_output_tokens INTEGER CHECK (
                    reserved_output_tokens IS NULL OR reserved_output_tokens >= 1
                ),
                tool_schema_token_estimate INTEGER NOT NULL CHECK (
                    tool_schema_token_estimate >= 0
                ),
                safety_margin_tokens INTEGER CHECK (
                    safety_margin_tokens IS NULL OR safety_margin_tokens >= 1
                ),
                available_input_tokens INTEGER CHECK (
                    available_input_tokens IS NULL OR available_input_tokens >= 0
                ),
                pre_compaction_token_estimate INTEGER NOT NULL CHECK (
                    pre_compaction_token_estimate >= 0
                ),
                input_token_estimate INTEGER NOT NULL CHECK (input_token_estimate >= 0),
                estimation_method TEXT NOT NULL,
                watermark_state TEXT NOT NULL CHECK (
                    watermark_state IN ('green', 'yellow', 'red', 'emergency', 'unknown')
                ),
                compaction_id TEXT,
                messages_json TEXT NOT NULL CHECK (json_valid(messages_json)),
                tools_json TEXT NOT NULL CHECK (json_valid(tools_json)),
                tools_hash TEXT NOT NULL CHECK (
                    length(tools_hash) = 64
                    AND tools_hash NOT GLOB '*[^0-9a-f]*'
                ),
                tool_result_stubs_json TEXT NOT NULL CHECK (
                    json_valid(tool_result_stubs_json)
                ),
                message_ids_json TEXT NOT NULL CHECK (json_valid(message_ids_json)),
                source_item_ids_json TEXT NOT NULL CHECK (json_valid(source_item_ids_json)),
                artifact_refs_json TEXT NOT NULL CHECK (json_valid(artifact_refs_json)),
                memory_refs_json TEXT NOT NULL CHECK (json_valid(memory_refs_json)),
                compaction_refs_json TEXT NOT NULL CHECK (json_valid(compaction_refs_json)),
                source_snapshots_json TEXT NOT NULL CHECK (
                    json_valid(source_snapshots_json)
                    AND json_type(source_snapshots_json) = 'array'
                ),
                token_estimate INTEGER NOT NULL CHECK (token_estimate >= 0),
                source_cursor_start INTEGER,
                source_cursor_end INTEGER,
                source_cursor_namespace TEXT CHECK (
                    source_cursor_namespace IS NULL
                    OR source_cursor_namespace = 'items.sequence'
                ),
                created_at TEXT NOT NULL,
                UNIQUE(agent_id, request_ordinal),
                CHECK (
                    (
                        source_cursor_start IS NULL
                        AND source_cursor_end IS NULL
                        AND source_cursor_namespace IS NULL
                    )
                    OR (
                        source_cursor_start >= 1
                        AND source_cursor_end >= source_cursor_start
                        AND source_cursor_namespace = 'items.sequence'
                    )
                ),
                FOREIGN KEY (session_id) REFERENCES sessions(id),
                FOREIGN KEY (agent_id) REFERENCES agents(id),
                FOREIGN KEY (thread_id) REFERENCES threads(id),
                FOREIGN KEY (compaction_id) REFERENCES compactions(id)
            );

            CREATE INDEX idx_context_revisions_session_sequence
                ON context_revisions(session_id, sequence);
            CREATE INDEX idx_context_revisions_thread_sequence
                ON context_revisions(thread_id, sequence);

            CREATE TABLE prompt_blocks (
                revision_id TEXT NOT NULL,
                position INTEGER NOT NULL CHECK (position >= 1),
                id TEXT UNIQUE NOT NULL,
                block_type TEXT NOT NULL CHECK (
                    block_type IN (
                        'role_instructions', 'tool_schema', 'explicit_references',
                        'compaction', 'conversation'
                    )
                ),
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL CHECK (
                    length(content_hash) = 64
                    AND content_hash NOT GLOB '*[^0-9a-f]*'
                ),
                source_refs_json TEXT NOT NULL CHECK (
                    json_valid(source_refs_json)
                    AND json_type(source_refs_json) = 'array'
                    AND json_array_length(source_refs_json) >= 1
                ),
                stable_until TEXT,
                visibility TEXT NOT NULL CHECK (
                    visibility IN ('role_private', 'session')
                ),
                token_estimate INTEGER CHECK (token_estimate IS NULL OR token_estimate >= 0),
                cache_eligible INTEGER NOT NULL CHECK (cache_eligible IN (0, 1)),
                PRIMARY KEY (revision_id, position),
                FOREIGN KEY (revision_id) REFERENCES context_revisions(id)
            );

            CREATE INDEX idx_prompt_blocks_revision_position
                ON prompt_blocks(revision_id, position);

            CREATE TABLE reference_bindings (
                revision_id TEXT NOT NULL,
                position INTEGER NOT NULL CHECK (position >= 1),
                id TEXT UNIQUE NOT NULL,
                ref_type TEXT NOT NULL CHECK (
                    ref_type IN ('thread', 'item', 'artifact', 'memory')
                ),
                user_text TEXT,
                resolved_target TEXT NOT NULL,
                source_snapshot_hash TEXT NOT NULL CHECK (
                    length(source_snapshot_hash) = 64
                    AND source_snapshot_hash NOT GLOB '*[^0-9a-f]*'
                ),
                include_mode TEXT NOT NULL CHECK (include_mode IN ('inline', 'metadata')),
                max_tokens INTEGER CHECK (max_tokens IS NULL OR max_tokens >= 1),
                visibility TEXT NOT NULL CHECK (
                    visibility IN ('role_private', 'session')
                ),
                resolved_at TEXT NOT NULL,
                PRIMARY KEY (revision_id, position),
                UNIQUE(revision_id, ref_type, resolved_target),
                FOREIGN KEY (revision_id) REFERENCES context_revisions(id)
            );

            CREATE INDEX idx_reference_bindings_target
                ON reference_bindings(ref_type, resolved_target);

            CREATE TRIGGER compactions_scope_guard
            BEFORE INSERT ON compactions
            WHEN sha256_text(NEW.summary_json) != NEW.content_hash
                OR NOT EXISTS (
                    SELECT 1 FROM agents
                    WHERE id = NEW.agent_id AND session_id = NEW.session_id
                )
                OR (
                    NEW.source_type = 'context_revisions'
                    AND (
                        NOT EXISTS (
                            SELECT 1 FROM context_revisions
                            WHERE sequence = NEW.source_cursor_start
                                AND session_id = NEW.session_id
                                AND agent_id = NEW.agent_id
                                AND thread_id IS NEW.thread_id
                        )
                        OR NOT EXISTS (
                            SELECT 1 FROM context_revisions
                            WHERE sequence = NEW.source_cursor_end
                                AND session_id = NEW.session_id
                                AND agent_id = NEW.agent_id
                                AND thread_id IS NEW.thread_id
                        )
                    )
                )
                OR (
                    NEW.source_type = 'thread_items'
                    AND (
                        json_array_length(NEW.covered_item_refs_json) = 0
                        OR EXISTS (
                            SELECT 1 FROM json_each(NEW.covered_item_refs_json) AS ref
                            WHERE COALESCE(json_type(ref.value), '') != 'object'
                                OR (SELECT COUNT(*) FROM json_each(ref.value)) != 4
                                OR EXISTS (
                                    SELECT 1 FROM json_each(ref.value) AS field
                                    WHERE field.key NOT IN (
                                        'source_type', 'source_id', 'cursor', 'content_hash'
                                    )
                                )
                                OR COALESCE(
                                    json_type(ref.value, '$.source_type'), ''
                                ) != 'text'
                                OR json_extract(ref.value, '$.source_type') != 'item'
                                OR COALESCE(
                                    json_type(ref.value, '$.source_id'), ''
                                ) != 'text'
                                OR length(
                                    json_extract(ref.value, '$.source_id')
                                ) NOT BETWEEN 1 AND 300
                                OR COALESCE(
                                    json_type(ref.value, '$.cursor'), ''
                                ) != 'integer'
                                OR json_extract(ref.value, '$.cursor') < 1
                                OR COALESCE(
                                    json_type(ref.value, '$.content_hash'), ''
                                ) != 'text'
                                OR length(
                                    json_extract(ref.value, '$.content_hash')
                                ) != 64
                                OR json_extract(
                                    ref.value, '$.content_hash'
                                ) GLOB '*[^0-9a-f]*'
                                OR NOT EXISTS (
                                    SELECT 1 FROM items
                                    WHERE id = json_extract(ref.value, '$.source_id')
                                        AND sequence = json_extract(ref.value, '$.cursor')
                                        AND thread_id = NEW.thread_id
                                        AND sha256_text(body) =
                                            json_extract(ref.value, '$.content_hash')
                                )
                        )
                        OR EXISTS (
                            SELECT 1
                            FROM json_each(NEW.covered_item_refs_json) AS ref
                            WHERE CAST(ref.key AS INTEGER) > 0
                                AND json_extract(ref.value, '$.cursor') <= json_extract(
                                    NEW.covered_item_refs_json,
                                    '$[' || (CAST(ref.key AS INTEGER) - 1) || '].cursor'
                                )
                        )
                        OR json_extract(
                            NEW.covered_item_refs_json, '$[0].cursor'
                        ) != NEW.source_cursor_start
                        OR json_extract(
                            NEW.covered_item_refs_json,
                            '$[' || (
                                json_array_length(NEW.covered_item_refs_json) - 1
                            ) || '].cursor'
                        ) != NEW.source_cursor_end
                        OR thread_item_refs_sha256(NEW.covered_item_refs_json) !=
                            NEW.source_snapshot_hash
                        OR NOT EXISTS (
                            SELECT 1 FROM items
                            WHERE sequence = NEW.source_cursor_start
                                AND thread_id = NEW.thread_id
                        )
                        OR NOT EXISTS (
                            SELECT 1 FROM items
                            WHERE sequence = NEW.source_cursor_end
                                AND thread_id = NEW.thread_id
                        )
                    )
                )
                OR (
                    NEW.source_type = 'context_revisions'
                    AND json_array_length(NEW.covered_item_refs_json) != 0
                )
                OR (
                    NEW.thread_id IS NOT NULL
                    AND NOT EXISTS (SELECT 1 FROM threads WHERE id = NEW.thread_id)
                )
            BEGIN
                SELECT RAISE(ABORT, 'compaction source scope is invalid');
            END;

            CREATE TRIGGER compactions_no_update
            BEFORE UPDATE ON compactions
            BEGIN
                SELECT RAISE(ABORT, 'compactions are append-only');
            END;

            CREATE TRIGGER compactions_no_delete
            BEFORE DELETE ON compactions
            BEGIN
                SELECT RAISE(ABORT, 'compactions are append-only');
            END;

            CREATE TRIGGER context_revisions_memory_sources_guard
            BEFORE INSERT ON context_revisions
            WHEN json_type(NEW.source_snapshots_json) != 'array'
                OR EXISTS (
                    SELECT 1
                    FROM json_each(NEW.source_snapshots_json) AS source
                    WHERE COALESCE(
                        json_type(
                            NEW.source_snapshots_json,
                            '$[' || CAST(source.key AS INTEGER) || ']'
                        ),
                        ''
                    ) != 'object'
                        OR (
                            SELECT COUNT(*)
                            FROM json_each(
                                CASE
                                    WHEN json_type(
                                        NEW.source_snapshots_json,
                                        '$[' || CAST(source.key AS INTEGER) || ']'
                                    ) = 'object'
                                    THEN source.value
                                    ELSE '{}'
                                END
                            )
                        ) != 4
                        OR EXISTS (
                            SELECT 1
                            FROM json_each(
                                CASE
                                    WHEN json_type(
                                        NEW.source_snapshots_json,
                                        '$[' || CAST(source.key AS INTEGER) || ']'
                                    ) = 'object'
                                    THEN source.value
                                    ELSE '{}'
                                END
                            ) AS field
                            WHERE field.key NOT IN (
                                'source_type', 'source_id', 'cursor', 'content_hash'
                            )
                        )
                        OR COALESCE(
                            json_type(
                                NEW.source_snapshots_json,
                                '$[' || CAST(source.key AS INTEGER) || '].source_type'
                            ),
                            ''
                        ) != 'text'
                        OR json_extract(
                            NEW.source_snapshots_json,
                            '$[' || CAST(source.key AS INTEGER) || '].source_type'
                        ) NOT IN (
                            'session', 'agent', 'thread', 'item', 'artifact', 'memory',
                            'tool_schema', 'compaction'
                        )
                        OR COALESCE(
                            json_type(
                                NEW.source_snapshots_json,
                                '$[' || CAST(source.key AS INTEGER) || '].source_id'
                            ),
                            ''
                        ) != 'text'
                        OR length(
                            json_extract(
                                NEW.source_snapshots_json,
                                '$[' || CAST(source.key AS INTEGER) || '].source_id'
                            )
                        ) NOT BETWEEN 1 AND 300
                        OR COALESCE(
                            json_type(
                                NEW.source_snapshots_json,
                                '$[' || CAST(source.key AS INTEGER) || '].cursor'
                            ),
                            ''
                        ) NOT IN (
                            'null', 'integer'
                        )
                        OR (
                            json_type(
                                NEW.source_snapshots_json,
                                '$[' || CAST(source.key AS INTEGER) || '].cursor'
                            ) = 'integer'
                            AND json_extract(
                                NEW.source_snapshots_json,
                                '$[' || CAST(source.key AS INTEGER) || '].cursor'
                            ) < 1
                        )
                        OR COALESCE(
                            json_type(
                                NEW.source_snapshots_json,
                                '$[' || CAST(source.key AS INTEGER) || '].content_hash'
                            ),
                            ''
                        ) != 'text'
                        OR length(
                            json_extract(
                                NEW.source_snapshots_json,
                                '$[' || CAST(source.key AS INTEGER) || '].content_hash'
                            )
                        ) != 64
                        OR json_extract(
                            NEW.source_snapshots_json,
                            '$[' || CAST(source.key AS INTEGER) || '].content_hash'
                        ) GLOB '*[^0-9a-f]*'
                )
                OR json_type(NEW.memory_refs_json) != 'array'
                OR EXISTS (
                    SELECT 1
                    FROM json_each(NEW.memory_refs_json) AS memory_ref
                    WHERE COALESCE(
                        json_type(
                            NEW.memory_refs_json,
                            '$[' || CAST(memory_ref.key AS INTEGER) || ']'
                        ),
                        ''
                    ) != 'text'
                        OR length(memory_ref.value) NOT BETWEEN 1 AND 300
                )
                OR EXISTS (
                    SELECT 1
                    FROM json_each(NEW.memory_refs_json) AS first_ref
                    WHERE EXISTS (
                        SELECT 1
                        FROM json_each(NEW.memory_refs_json) AS later_ref
                        WHERE later_ref.key > first_ref.key
                            AND later_ref.value = first_ref.value
                    )
                )
                OR EXISTS (
                    SELECT 1
                    FROM json_each(NEW.source_snapshots_json) AS first_source
                    WHERE json_extract(
                        NEW.source_snapshots_json,
                        '$[' || CAST(first_source.key AS INTEGER) || '].source_type'
                    ) = 'memory'
                        AND EXISTS (
                            SELECT 1
                            FROM json_each(NEW.source_snapshots_json) AS later_source
                            WHERE later_source.key > first_source.key
                                AND json_extract(
                                    NEW.source_snapshots_json,
                                    '$[' || CAST(later_source.key AS INTEGER) || '].source_type'
                                ) = 'memory'
                                AND json_extract(
                                    NEW.source_snapshots_json,
                                    '$[' || CAST(later_source.key AS INTEGER) || '].source_id'
                                ) = json_extract(
                                    NEW.source_snapshots_json,
                                    '$[' || CAST(first_source.key AS INTEGER) || '].source_id'
                                )
                        )
                )
                OR json_array_length(NEW.memory_refs_json) != (
                    SELECT COUNT(
                        DISTINCT json_extract(
                            NEW.source_snapshots_json,
                            '$[' || CAST(source.key AS INTEGER) || '].source_id'
                        )
                    )
                    FROM json_each(NEW.source_snapshots_json) AS source
                    WHERE json_extract(
                        NEW.source_snapshots_json,
                        '$[' || CAST(source.key AS INTEGER) || '].source_type'
                    ) = 'memory'
                )
                OR EXISTS (
                    SELECT 1
                    FROM json_each(NEW.memory_refs_json) AS memory_ref
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM json_each(NEW.source_snapshots_json) AS source
                        WHERE json_extract(
                            NEW.source_snapshots_json,
                            '$[' || CAST(source.key AS INTEGER) || '].source_type'
                        ) = 'memory'
                            AND json_extract(
                                NEW.source_snapshots_json,
                                '$[' || CAST(source.key AS INTEGER) || '].source_id'
                            ) = memory_ref.value
                    )
                )
                OR EXISTS (
                    SELECT 1
                    FROM json_each(NEW.source_snapshots_json) AS source
                    WHERE json_extract(
                        NEW.source_snapshots_json,
                        '$[' || CAST(source.key AS INTEGER) || '].source_type'
                    ) = 'memory'
                        AND NOT EXISTS (
                            SELECT 1
                            FROM json_each(NEW.memory_refs_json) AS memory_ref
                            WHERE memory_ref.value = json_extract(
                                NEW.source_snapshots_json,
                                '$[' || CAST(source.key AS INTEGER) || '].source_id'
                            )
                        )
                )
                OR EXISTS (
                    SELECT 1
                    FROM json_each(NEW.memory_refs_json) AS memory_ref
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM json_each(NEW.source_snapshots_json) AS source
                        WHERE json_extract(
                            NEW.source_snapshots_json,
                            '$[' || CAST(source.key AS INTEGER) || '].source_type'
                        ) = 'memory'
                            AND json_extract(
                                NEW.source_snapshots_json,
                                '$[' || CAST(source.key AS INTEGER) || '].source_id'
                            ) = memory_ref.value
                            AND (
                                SELECT COUNT(
                                    DISTINCT json_extract(
                                        NEW.source_snapshots_json,
                                        '$[' || CAST(previous.key AS INTEGER) || '].source_id'
                                    )
                                )
                                FROM json_each(NEW.source_snapshots_json) AS previous
                                WHERE previous.key < source.key
                                    AND json_extract(
                                        NEW.source_snapshots_json,
                                        '$[' || CAST(previous.key AS INTEGER) || '].source_type'
                                    ) = 'memory'
                            ) = memory_ref.key
                    )
                )
                OR EXISTS (
                    SELECT 1
                    FROM json_each(NEW.source_snapshots_json) AS source
                    WHERE json_extract(
                        NEW.source_snapshots_json,
                        '$[' || CAST(source.key AS INTEGER) || '].source_type'
                    ) = 'memory'
                        AND (
                            json_type(
                                NEW.source_snapshots_json,
                                '$[' || CAST(source.key AS INTEGER) || '].cursor'
                            ) != 'integer'
                            OR NOT EXISTS (
                                SELECT 1
                                FROM memories AS m
                                JOIN memory_versions AS mv
                                    ON mv.memory_id = m.id
                                    AND mv.version = json_extract(
                                        NEW.source_snapshots_json,
                                        '$[' || CAST(source.key AS INTEGER) || '].cursor'
                                    )
                                JOIN sessions AS s ON s.id = NEW.session_id
                                WHERE m.id = json_extract(
                                        NEW.source_snapshots_json,
                                        '$[' || CAST(source.key AS INTEGER) || '].source_id'
                                    )
                                    AND m.current_version = mv.version
                                    AND mv.status = 'active'
                                    AND mv.body_hash = json_extract(
                                        NEW.source_snapshots_json,
                                        '$[' || CAST(source.key AS INTEGER) || '].content_hash'
                                    )
                                    AND json_valid(mv.role_scope)
                                    AND json_type(mv.role_scope) = 'array'
                                    AND (
                                        SELECT CASE mv.kind
                                            WHEN 'working' THEN instr(value, ' working ') > 0
                                                OR instr(value, ' session ') > 0
                                                OR instr(value, ' all ') > 0
                                                OR instr(value, ' * ') > 0
                                            WHEN 'episodic' THEN instr(value, ' episodic ') > 0
                                                OR instr(value, ' episode ') > 0
                                                OR instr(value, ' all ') > 0
                                                OR instr(value, ' * ') > 0
                                            WHEN 'project' THEN instr(value, ' project ') > 0
                                                OR instr(value, ' project_knowledge ') > 0
                                                OR instr(value, ' all ') > 0
                                                OR instr(value, ' * ') > 0
                                            ELSE 0
                                        END
                                        FROM (
                                            SELECT ' ' || replace(
                                                replace(
                                                    replace(
                                                        replace(
                                                            replace(
                                                                replace(scope, '[', ' '),
                                                                ']', ' '
                                                            ),
                                                            ',', ' '
                                                        ),
                                                        ';', ' '
                                                    ),
                                                    ':', ' '
                                                ),
                                                '=', ' '
                                            ) || ' ' AS value
                                            FROM (
                                                SELECT CASE
                                                    WHEN instr(scope, 'read') > 0 THEN substr(
                                                        scope,
                                                        instr(scope, 'read') + 4,
                                                        CASE
                                                            WHEN instr(scope, 'write') >
                                                                instr(scope, 'read')
                                                            THEN instr(scope, 'write') -
                                                                instr(scope, 'read') - 4
                                                            ELSE length(scope)
                                                        END
                                                    )
                                                    ELSE scope
                                                END AS scope
                                                FROM (
                                                    SELECT lower(trim(json_extract(
                                                        s.body,
                                                        '$.role_snapshot.memory_scope'
                                                    ))) AS scope
                                                ) AS raw_scope
                                            ) AS parsed_scope
                                        ) AS normalized
                                    )
                                    AND (
                                        mv.kind != 'working'
                                        OR mv.source_session_id = NEW.session_id
                                    )
                                    AND (
                                        mv.kind != 'project'
                                        OR (
                                            NEW.workspace_ref IS NOT NULL
                                            AND mv.project_scope = NEW.workspace_ref
                                        )
                                    )
                                    AND (
                                        json_array_length(mv.role_scope) = 0
                                        OR EXISTS (
                                            SELECT 1
                                            FROM json_each(mv.role_scope) AS allowed
                                            WHERE allowed.value IN (
                                                json_extract(s.body, '$.role_snapshot.role_id'),
                                                json_extract(s.body, '$.role_snapshot.role_name'),
                                                '*'
                                            )
                                        )
                                    )
                            )
                        )
                )
            BEGIN
                SELECT RAISE(ABORT, 'ContextRevision Memory source snapshot is invalid');
            END;

            CREATE TRIGGER context_revisions_scope_guard
            BEFORE INSERT ON context_revisions
            WHEN NOT EXISTS (
                    SELECT 1 FROM agents
                    WHERE id = NEW.agent_id AND session_id = NEW.session_id
                )
                OR (
                    NEW.thread_id IS NOT NULL
                    AND NEW.workspace_ref IS NOT NULL
                    AND NOT EXISTS (
                        SELECT 1 FROM threads
                        WHERE id = NEW.thread_id
                            AND workspace_ref = NEW.workspace_ref
                    )
                )
                OR (
                    NEW.compaction_id IS NOT NULL
                    AND NOT EXISTS (
                        SELECT 1 FROM compactions
                        WHERE id = NEW.compaction_id
                            AND session_id = NEW.session_id
                            AND agent_id = NEW.agent_id
                            AND thread_id IS NEW.thread_id
                    )
                )
                OR NEW.prompt_layout_version != 'phase1b.v1'
                OR (
                    (NEW.source_cursor_start IS NULL)
                    != (NEW.source_cursor_end IS NULL)
                )
                OR (
                    NEW.source_cursor_start IS NULL
                    AND NEW.source_cursor_namespace IS NOT NULL
                )
                OR (
                    NEW.source_cursor_start IS NOT NULL
                    AND COALESCE(NEW.source_cursor_namespace, '') != 'items.sequence'
                )
                OR (
                    NEW.thread_id IS NULL
                    AND (
                        NEW.source_cursor_start IS NOT NULL
                        OR NEW.source_cursor_end IS NOT NULL
                        OR json_array_length(NEW.source_item_ids_json) != 0
                    )
                )
                OR (
                    NEW.source_cursor_start IS NOT NULL
                    AND (
                        NOT EXISTS (
                            SELECT 1 FROM items
                            WHERE sequence = NEW.source_cursor_start
                                AND thread_id = NEW.thread_id
                        )
                        OR NOT EXISTS (
                            SELECT 1 FROM items
                            WHERE sequence = NEW.source_cursor_end
                                AND thread_id = NEW.thread_id
                        )
                    )
                )
                OR EXISTS (
                    SELECT 1 FROM json_each(NEW.source_item_ids_json) AS item_id
                    WHERE NOT EXISTS (
                        SELECT 1 FROM items
                        WHERE id = item_id.value AND thread_id = NEW.thread_id
                    )
                )
                OR (
                    NEW.compaction_id IS NULL
                    AND json_array_length(NEW.compaction_refs_json) != 0
                )
                OR (
                    NEW.compaction_id IS NOT NULL
                    AND (
                        json_array_length(NEW.compaction_refs_json) != 1
                        OR json_extract(NEW.compaction_refs_json, '$[0]') != NEW.compaction_id
                    )
                )
                OR json_type(NEW.memory_refs_json) != 'array'
                OR EXISTS (
                    SELECT 1 FROM json_each(NEW.memory_refs_json) AS memory_ref
                    WHERE NOT EXISTS (
                        SELECT 1 FROM memories WHERE id = memory_ref.value
                    )
                )
            BEGIN
                SELECT RAISE(ABORT, 'ContextRevision source scope is invalid');
            END;

            CREATE TRIGGER context_revisions_no_update
            BEFORE UPDATE ON context_revisions
            BEGIN
                SELECT RAISE(ABORT, 'ContextRevisions are immutable');
            END;

            CREATE TRIGGER context_revisions_no_delete
            BEFORE DELETE ON context_revisions
            BEGIN
                SELECT RAISE(ABORT, 'ContextRevisions are immutable');
            END;

            CREATE TRIGGER prompt_blocks_position_guard
            BEFORE INSERT ON prompt_blocks
            WHEN NEW.position != (
                SELECT COALESCE(MAX(position), 0) + 1
                FROM prompt_blocks WHERE revision_id = NEW.revision_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'Prompt Block positions must be contiguous');
            END;

            CREATE TRIGGER prompt_blocks_source_refs_guard
            BEFORE INSERT ON prompt_blocks
            WHEN json_type(NEW.source_refs_json) != 'array'
                OR json_array_length(NEW.source_refs_json) < 1
                OR (
                    NEW.block_type = 'compaction'
                    AND (
                        json_array_length(NEW.source_refs_json) != 1
                        OR json_extract(
                            NEW.source_refs_json, '$[0].source_type'
                        ) != 'compaction'
                    )
                )
                OR (
                    NEW.block_type != 'compaction'
                    AND EXISTS (
                        SELECT 1 FROM json_each(NEW.source_refs_json) AS compaction_ref
                        WHERE json_extract(compaction_ref.value, '$.source_type') = 'compaction'
                    )
                )
                OR EXISTS (
                SELECT 1 FROM json_each(NEW.source_refs_json) AS ref
                WHERE COALESCE(json_type(ref.value), '') != 'object'
                    OR (SELECT COUNT(*) FROM json_each(ref.value)) != 4
                    OR EXISTS (
                        SELECT 1 FROM json_each(ref.value) AS field
                        WHERE field.key NOT IN (
                            'source_type', 'source_id', 'cursor', 'content_hash'
                        )
                    )
                    OR COALESCE(json_type(ref.value, '$.source_type'), '') != 'text'
                    OR COALESCE(json_type(ref.value, '$.source_id'), '') != 'text'
                    OR length(json_extract(ref.value, '$.source_id')) NOT BETWEEN 1 AND 300
                    OR COALESCE(json_type(ref.value, '$.cursor'), '') NOT IN (
                        'null', 'integer'
                    )
                    OR (
                        json_type(ref.value, '$.cursor') = 'integer'
                        AND json_extract(ref.value, '$.cursor') < 1
                    )
                    OR COALESCE(json_type(ref.value, '$.content_hash'), '') != 'text'
                    OR length(json_extract(ref.value, '$.content_hash')) != 64
                    OR json_extract(ref.value, '$.content_hash') GLOB '*[^0-9a-f]*'
                    OR json_extract(ref.value, '$.source_type') NOT IN (
                        'session', 'agent', 'thread', 'item', 'artifact', 'memory',
                        'tool_schema', 'compaction'
                    )
                    OR (
                        json_extract(ref.value, '$.source_type') = 'session'
                        AND (
                            json_extract(ref.value, '$.cursor') IS NOT NULL
                            OR json_extract(ref.value, '$.content_hash') != NEW.content_hash
                            OR NOT EXISTS (
                                SELECT 1 FROM context_revisions AS r
                                WHERE r.id = NEW.revision_id
                                    AND r.session_id = json_extract(ref.value, '$.source_id')
                            )
                        )
                    )
                    OR (
                        json_extract(ref.value, '$.source_type') = 'agent'
                        AND (
                            json_extract(ref.value, '$.cursor') IS NOT NULL
                            OR json_extract(ref.value, '$.content_hash') != NEW.content_hash
                            OR NOT EXISTS (
                                SELECT 1 FROM context_revisions AS r
                                WHERE r.id = NEW.revision_id
                                    AND r.agent_id = json_extract(ref.value, '$.source_id')
                            )
                        )
                    )
                    OR (
                        json_extract(ref.value, '$.source_type') = 'tool_schema'
                        AND (
                            json_extract(ref.value, '$.cursor') IS NOT NULL
                            OR NOT EXISTS (
                                SELECT 1 FROM context_revisions AS r
                                WHERE r.id = NEW.revision_id
                                    AND r.session_id = json_extract(ref.value, '$.source_id')
                                    AND r.tools_hash =
                                        json_extract(ref.value, '$.content_hash')
                            )
                        )
                    )
                    OR (
                        json_extract(ref.value, '$.source_type') = 'item'
                        AND NOT EXISTS (
                            SELECT 1 FROM items AS i
                            JOIN context_revisions AS r
                                ON r.id = NEW.revision_id AND r.thread_id = i.thread_id
                            WHERE i.id = json_extract(ref.value, '$.source_id')
                                AND i.sequence = json_extract(ref.value, '$.cursor')
                                AND i.body_hash =
                                    json_extract(ref.value, '$.content_hash')
                        )
                    )
                    OR (
                        json_extract(ref.value, '$.source_type') = 'thread'
                        AND NOT EXISTS (
                            SELECT 1 FROM threads AS t
                            JOIN context_revisions AS r
                                ON r.id = NEW.revision_id AND r.thread_id = t.id
                            WHERE t.id = json_extract(ref.value, '$.source_id')
                                AND t.sequence = json_extract(ref.value, '$.cursor')
                                AND (
                                    r.workspace_ref IS NULL
                                    OR t.workspace_ref = r.workspace_ref
                                )
                                AND t.body_hash =
                                    json_extract(ref.value, '$.content_hash')
                        )
                    )
                    OR (
                        json_extract(ref.value, '$.source_type') = 'artifact'
                        AND NOT EXISTS (
                            SELECT 1 FROM artifacts
                            WHERE id = json_extract(ref.value, '$.source_id')
                                AND sequence = json_extract(ref.value, '$.cursor')
                                AND content_hash = json_extract(ref.value, '$.content_hash')
                                AND sensitivity != 'restricted'
                        )
                    )
                    OR (
                        json_extract(ref.value, '$.source_type') = 'memory'
                        AND (
                            COALESCE(json_type(ref.value, '$.cursor'), '') != 'integer'
                            OR NOT EXISTS (
                                SELECT 1
                                FROM memories AS m
                                JOIN memory_versions AS mv
                                    ON mv.memory_id = m.id
                                    AND mv.version = json_extract(ref.value, '$.cursor')
                                JOIN context_revisions AS r
                                    ON r.id = NEW.revision_id
                                JOIN sessions AS s
                                    ON s.id = r.session_id
                                WHERE m.id = json_extract(ref.value, '$.source_id')
                                    AND m.current_version = mv.version
                                    AND mv.status = 'active'
                                    AND mv.body_hash =
                                        json_extract(ref.value, '$.content_hash')
                                    AND EXISTS (
                                        SELECT 1
                                        FROM json_each(r.memory_refs_json) AS memory_ref
                                        WHERE memory_ref.value = json_extract(
                                            ref.value, '$.source_id'
                                        )
                                    )
                                    AND (
                                        SELECT CASE mv.kind
                                            WHEN 'working' THEN instr(value, ' working ') > 0
                                                OR instr(value, ' session ') > 0
                                                OR instr(value, ' all ') > 0
                                                OR instr(value, ' * ') > 0
                                            WHEN 'episodic' THEN instr(value, ' episodic ') > 0
                                                OR instr(value, ' episode ') > 0
                                                OR instr(value, ' all ') > 0
                                                OR instr(value, ' * ') > 0
                                            WHEN 'project' THEN instr(value, ' project ') > 0
                                                OR instr(value, ' project_knowledge ') > 0
                                                OR instr(value, ' all ') > 0
                                                OR instr(value, ' * ') > 0
                                            ELSE 0
                                        END
                                        FROM (
                                            SELECT ' ' || replace(
                                                replace(
                                                    replace(
                                                        replace(
                                                            replace(
                                                                replace(scope, '[', ' '),
                                                                ']', ' '
                                                            ),
                                                            ',', ' '
                                                        ),
                                                        ';', ' '
                                                    ),
                                                    ':', ' '
                                                ),
                                                '=', ' '
                                            ) || ' ' AS value
                                            FROM (
                                                SELECT CASE
                                                    WHEN instr(scope, 'read') > 0 THEN substr(
                                                        scope,
                                                        instr(scope, 'read') + 4,
                                                        CASE
                                                            WHEN instr(scope, 'write') >
                                                                instr(scope, 'read')
                                                            THEN instr(scope, 'write') -
                                                                instr(scope, 'read') - 4
                                                            ELSE length(scope)
                                                        END
                                                    )
                                                    ELSE scope
                                                END AS scope
                                                FROM (
                                                    SELECT lower(trim(json_extract(
                                                        s.body,
                                                        '$.role_snapshot.memory_scope'
                                                    ))) AS scope
                                                ) AS raw_scope
                                            ) AS parsed_scope
                                        ) AS normalized
                                    )
                                    AND (
                                        mv.kind != 'working'
                                        OR mv.source_session_id = r.session_id
                                    )
                                    AND (
                                        mv.kind != 'project'
                                        OR (
                                            r.workspace_ref IS NOT NULL
                                            AND mv.project_scope = r.workspace_ref
                                        )
                                    )
                                    AND (
                                        json_array_length(mv.role_scope) = 0
                                        OR EXISTS (
                                            SELECT 1 FROM json_each(mv.role_scope) AS allowed
                                            WHERE allowed.value IN (
                                                json_extract(s.body, '$.role_snapshot.role_id'),
                                                json_extract(s.body, '$.role_snapshot.role_name'),
                                                '*'
                                            )
                                        )
                                    )
                            )
                        )
                    )
                    OR (
                        json_extract(ref.value, '$.source_type') = 'compaction'
                        AND (
                            json_extract(ref.value, '$.cursor') IS NOT NULL
                            OR NOT EXISTS (
                                SELECT 1
                                FROM compactions AS c
                                JOIN context_revisions AS r
                                    ON r.id = NEW.revision_id
                                    AND r.compaction_id = c.id
                                    AND r.session_id = c.session_id
                                    AND r.agent_id = c.agent_id
                                    AND r.thread_id IS c.thread_id
                                WHERE c.id = json_extract(ref.value, '$.source_id')
                                    AND c.content_hash =
                                        json_extract(ref.value, '$.content_hash')
                            )
                        )
                    )
            )
            BEGIN
                SELECT RAISE(ABORT, 'Prompt Block source reference is invalid');
            END;

            CREATE TRIGGER prompt_blocks_no_update
            BEFORE UPDATE ON prompt_blocks
            BEGIN
                SELECT RAISE(ABORT, 'Prompt Blocks are immutable');
            END;

            CREATE TRIGGER prompt_blocks_no_delete
            BEFORE DELETE ON prompt_blocks
            BEGIN
                SELECT RAISE(ABORT, 'Prompt Blocks are immutable');
            END;

            CREATE TRIGGER reference_bindings_position_guard
            BEFORE INSERT ON reference_bindings
            WHEN NEW.position != (
                SELECT COALESCE(MAX(position), 0) + 1
                FROM reference_bindings WHERE revision_id = NEW.revision_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'Reference Binding positions must be contiguous');
            END;

            CREATE TRIGGER reference_bindings_target_guard
            BEFORE INSERT ON reference_bindings
            WHEN (
                    NEW.ref_type = 'thread'
                    AND NOT EXISTS (
                        SELECT 1 FROM context_revisions
                        WHERE id = NEW.revision_id AND thread_id = NEW.resolved_target
                    )
                )
                OR (
                    NEW.ref_type = 'item'
                    AND NOT EXISTS (
                        SELECT 1 FROM items AS i
                        JOIN context_revisions AS r
                            ON r.id = NEW.revision_id AND r.thread_id = i.thread_id
                        WHERE i.id = NEW.resolved_target
                    )
                )
                OR (
                    NEW.ref_type = 'artifact'
                    AND NOT EXISTS (
                        SELECT 1 FROM artifacts
                        WHERE id = NEW.resolved_target
                            AND sensitivity != 'restricted'
                            AND (
                                NEW.include_mode = 'metadata'
                                OR sensitivity = 'normal'
                            )
                    )
                )
                OR (
                    NEW.ref_type = 'memory'
                    AND NOT EXISTS (
                        SELECT 1
                        FROM context_revisions AS r
                        JOIN prompt_blocks AS b ON b.revision_id = r.id
                        CROSS JOIN json_each(b.source_refs_json) AS source
                        JOIN memories AS m
                            ON m.id = json_extract(source.value, '$.source_id')
                        JOIN memory_versions AS mv
                            ON mv.memory_id = m.id
                            AND mv.version = json_extract(source.value, '$.cursor')
                        JOIN sessions AS s ON s.id = r.session_id
                        WHERE r.id = NEW.revision_id
                            AND json_extract(source.value, '$.source_type') = 'memory'
                            AND json_extract(source.value, '$.source_id') = NEW.resolved_target
                            AND json_extract(source.value, '$.content_hash') =
                                NEW.source_snapshot_hash
                            AND json_type(source.value, '$.cursor') = 'integer'
                            AND EXISTS (
                                SELECT 1
                                FROM json_each(r.memory_refs_json) AS memory_ref
                                WHERE memory_ref.value = NEW.resolved_target
                            )
                            AND m.current_version = mv.version
                            AND mv.status = 'active'
                            AND mv.body_hash = NEW.source_snapshot_hash
                            AND (
                                SELECT CASE mv.kind
                                    WHEN 'working' THEN instr(value, ' working ') > 0
                                        OR instr(value, ' session ') > 0
                                        OR instr(value, ' all ') > 0
                                        OR instr(value, ' * ') > 0
                                    WHEN 'episodic' THEN instr(value, ' episodic ') > 0
                                        OR instr(value, ' episode ') > 0
                                        OR instr(value, ' all ') > 0
                                        OR instr(value, ' * ') > 0
                                    WHEN 'project' THEN instr(value, ' project ') > 0
                                        OR instr(value, ' project_knowledge ') > 0
                                        OR instr(value, ' all ') > 0
                                        OR instr(value, ' * ') > 0
                                    ELSE 0
                                END
                                FROM (
                                    SELECT ' ' || replace(
                                        replace(
                                            replace(
                                                replace(
                                                    replace(
                                                        replace(scope, '[', ' '),
                                                        ']', ' '
                                                    ),
                                                    ',', ' '
                                                ),
                                                ';', ' '
                                            ),
                                            ':', ' '
                                        ),
                                        '=', ' '
                                    ) || ' ' AS value
                                    FROM (
                                        SELECT CASE
                                            WHEN instr(scope, 'read') > 0 THEN substr(
                                                scope,
                                                instr(scope, 'read') + 4,
                                                CASE
                                                    WHEN instr(scope, 'write') >
                                                        instr(scope, 'read')
                                                    THEN instr(scope, 'write') -
                                                        instr(scope, 'read') - 4
                                                    ELSE length(scope)
                                                END
                                            )
                                            ELSE scope
                                        END AS scope
                                        FROM (
                                            SELECT lower(trim(json_extract(
                                                s.body,
                                                '$.role_snapshot.memory_scope'
                                            ))) AS scope
                                        ) AS raw_scope
                                    ) AS parsed_scope
                                ) AS normalized
                            )
                            AND (
                                mv.kind != 'working'
                                OR mv.source_session_id = r.session_id
                            )
                            AND (
                                mv.kind != 'project'
                                OR (
                                    r.workspace_ref IS NOT NULL
                                    AND mv.project_scope = r.workspace_ref
                                )
                            )
                            AND (
                                json_array_length(mv.role_scope) = 0
                                OR EXISTS (
                                    SELECT 1 FROM json_each(mv.role_scope) AS allowed
                                    WHERE allowed.value IN (
                                        json_extract(s.body, '$.role_snapshot.role_id'),
                                        json_extract(s.body, '$.role_snapshot.role_name'),
                                        '*'
                                    )
                                )
                            )
                    )
                )
            BEGIN
                SELECT RAISE(ABORT, 'Context reference target is invalid');
            END;

            CREATE TRIGGER reference_bindings_no_update
            BEFORE UPDATE ON reference_bindings
            BEGIN
                SELECT RAISE(ABORT, 'Reference Bindings are immutable');
            END;

            CREATE TRIGGER reference_bindings_no_delete
            BEFORE DELETE ON reference_bindings
            BEGIN
                SELECT RAISE(ABORT, 'Reference Bindings are immutable');
            END;
            """,
        )

    def _upgrade_v7(self, connection: sqlite3.Connection) -> None:
        self._execute_sql_batch(
            connection,
            """
            CREATE TABLE retention_policies (
                id TEXT PRIMARY KEY,
                object_type TEXT NOT NULL CHECK (object_type = 'artifact'),
                grace_period_seconds INTEGER NOT NULL CHECK (
                    grace_period_seconds BETWEEN 0 AND 31536000
                ),
                allow_physical_delete INTEGER NOT NULL CHECK (
                    allow_physical_delete IN (0, 1)
                ),
                created_at TEXT NOT NULL
            );

            CREATE TABLE artifact_retention_states (
                artifact_id TEXT PRIMARY KEY,
                policy_ref TEXT NOT NULL,
                lifecycle TEXT NOT NULL CHECK (
                    lifecycle IN (
                        'active', 'archived', 'deletion_scheduled', 'trashed', 'deleted'
                    )
                ),
                pinned INTEGER NOT NULL CHECK (pinned IN (0, 1)),
                scheduled_deletion_at TEXT,
                trashed_at TEXT,
                deleted_at TEXT,
                updated_at TEXT NOT NULL,
                CHECK (
                    (lifecycle IN ('active', 'archived')
                        AND scheduled_deletion_at IS NULL
                        AND trashed_at IS NULL
                        AND deleted_at IS NULL)
                    OR (lifecycle = 'deletion_scheduled'
                        AND pinned = 0
                        AND scheduled_deletion_at IS NOT NULL
                        AND trashed_at IS NULL
                        AND deleted_at IS NULL)
                    OR (lifecycle = 'trashed'
                        AND pinned = 0
                        AND scheduled_deletion_at IS NOT NULL
                        AND trashed_at IS NOT NULL
                        AND deleted_at IS NULL)
                    OR (lifecycle = 'deleted'
                        AND pinned = 0
                        AND scheduled_deletion_at IS NOT NULL
                        AND trashed_at IS NOT NULL
                        AND deleted_at IS NOT NULL)
                ),
                FOREIGN KEY (artifact_id) REFERENCES artifacts(id),
                FOREIGN KEY (policy_ref) REFERENCES retention_policies(id)
            );

            CREATE INDEX idx_artifact_retention_states_lifecycle_due
                ON artifact_retention_states(lifecycle, scheduled_deletion_at);

            INSERT INTO retention_policies(
                id, object_type, grace_period_seconds, allow_physical_delete, created_at
            )
            SELECT retention_policy_ref, 'artifact', 86400, 0, MIN(created_at)
            FROM artifacts GROUP BY retention_policy_ref;

            INSERT INTO artifact_retention_states(
                artifact_id, policy_ref, lifecycle, pinned, scheduled_deletion_at,
                trashed_at, deleted_at, updated_at
            )
            SELECT id, retention_policy_ref, 'active', 0, NULL, NULL, NULL, created_at
            FROM artifacts;

            CREATE TABLE artifact_retention_audit_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT UNIQUE NOT NULL,
                artifact_id TEXT,
                event_type TEXT NOT NULL CHECK (
                    event_type IN (
                        'retention.pin_changed', 'retention.archived',
                        'retention.deletion_scheduled', 'retention.trashed',
                        'retention.restored', 'retention.physical_delete_completed',
                        'retention.physical_delete_outcome_unknown',
                        'artifact.audit.finding', 'artifact.repair.completed',
                        'artifact.repair.refused', 'artifact.repair.outcome_unknown'
                    )
                ),
                content_hash TEXT CHECK (
                    content_hash IS NULL OR (
                        length(content_hash) = 64
                        AND content_hash NOT GLOB '*[^0-9a-f]*'
                    )
                ),
                finding_hash TEXT CHECK (
                    finding_hash IS NULL OR (
                        length(finding_hash) = 64
                        AND finding_hash NOT GLOB '*[^0-9a-f]*'
                    )
                ),
                action_hash TEXT CHECK (
                    action_hash IS NULL OR (
                        length(action_hash) = 64
                        AND action_hash NOT GLOB '*[^0-9a-f]*'
                    )
                ),
                outcome TEXT NOT NULL CHECK (
                    outcome IN ('observed', 'completed', 'refused', 'outcome_unknown')
                ),
                created_at TEXT NOT NULL,
                FOREIGN KEY (artifact_id) REFERENCES artifacts(id)
            );

            CREATE INDEX idx_artifact_retention_audit_artifact_sequence
                ON artifact_retention_audit_events(artifact_id, sequence);

            CREATE TABLE cache_observations (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT UNIQUE NOT NULL,
                provider TEXT NOT NULL CHECK (length(provider) BETWEEN 1 AND 200),
                model TEXT NOT NULL CHECK (length(model) BETWEEN 1 AND 300),
                request_id TEXT CHECK (request_id IS NULL OR length(request_id) <= 300),
                context_revision_id TEXT,
                cache_scope TEXT CHECK (cache_scope IS NULL OR length(cache_scope) <= 300),
                breakpoint_id TEXT CHECK (breakpoint_id IS NULL OR length(breakpoint_id) <= 300),
                hit_status TEXT NOT NULL CHECK (hit_status IN ('hit', 'miss', 'unknown')),
                prompt_tokens INTEGER CHECK (prompt_tokens IS NULL OR prompt_tokens >= 0),
                completion_tokens INTEGER CHECK (
                    completion_tokens IS NULL OR completion_tokens >= 0
                ),
                cache_read_tokens INTEGER CHECK (
                    cache_read_tokens IS NULL OR cache_read_tokens >= 0
                ),
                cache_write_tokens INTEGER CHECK (
                    cache_write_tokens IS NULL OR cache_write_tokens >= 0
                ),
                cache_key_hash TEXT CHECK (
                    cache_key_hash IS NULL OR (
                        length(cache_key_hash) = 64
                        AND cache_key_hash NOT GLOB '*[^0-9a-f]*'
                    )
                ),
                stable_prefix_hash TEXT CHECK (
                    stable_prefix_hash IS NULL OR (
                        length(stable_prefix_hash) = 64
                        AND stable_prefix_hash NOT GLOB '*[^0-9a-f]*'
                    )
                ),
                invalidation_reason TEXT CHECK (
                    invalidation_reason IS NULL OR invalidation_reason IN (
                        'provider_reported', 'prefix_changed', 'ttl_expired',
                        'model_changed', 'scope_changed', 'unknown'
                    )
                ),
                created_at TEXT NOT NULL,
                FOREIGN KEY (context_revision_id) REFERENCES context_revisions(id)
            );

            CREATE INDEX idx_cache_observations_context_sequence
                ON cache_observations(context_revision_id, sequence);

            CREATE TRIGGER retention_policies_no_update
            BEFORE UPDATE ON retention_policies
            BEGIN
                SELECT RAISE(ABORT, 'retention policies are immutable');
            END;

            CREATE TRIGGER retention_policies_no_delete
            BEFORE DELETE ON retention_policies
            BEGIN
                SELECT RAISE(ABORT, 'retention policies are immutable');
            END;

            CREATE TRIGGER artifact_retention_audit_events_no_update
            BEFORE UPDATE ON artifact_retention_audit_events
            BEGIN
                SELECT RAISE(ABORT, 'Artifact retention audit is append-only');
            END;

            CREATE TRIGGER artifact_retention_audit_events_no_delete
            BEFORE DELETE ON artifact_retention_audit_events
            BEGIN
                SELECT RAISE(ABORT, 'Artifact retention audit is append-only');
            END;

            CREATE TRIGGER cache_observations_no_update
            BEFORE UPDATE ON cache_observations
            BEGIN
                SELECT RAISE(ABORT, 'CacheObservation is append-only');
            END;

            CREATE TRIGGER cache_observations_no_delete
            BEFORE DELETE ON cache_observations
            BEGIN
                SELECT RAISE(ABORT, 'CacheObservation is append-only');
            END;
            """,
        )

    def _downgrade_v7(self, connection: sqlite3.Connection) -> None:
        populated = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM retention_policies)
                + (SELECT COUNT(*) FROM artifact_retention_states)
                + (SELECT COUNT(*) FROM artifact_retention_audit_events)
                + (SELECT COUNT(*) FROM cache_observations) AS row_count
            """
        ).fetchone()
        if populated is not None and int(populated["row_count"]) > 0:
            raise MigrationError("refusing to roll back Phase 1C tables while they contain data")
        self._execute_sql_batch(
            connection,
            """
            DROP TRIGGER cache_observations_no_delete;
            DROP TRIGGER cache_observations_no_update;
            DROP TRIGGER artifact_retention_audit_events_no_delete;
            DROP TRIGGER artifact_retention_audit_events_no_update;
            DROP TRIGGER retention_policies_no_delete;
            DROP TRIGGER retention_policies_no_update;
            DROP INDEX idx_cache_observations_context_sequence;
            DROP TABLE cache_observations;
            DROP INDEX idx_artifact_retention_audit_artifact_sequence;
            DROP TABLE artifact_retention_audit_events;
            DROP INDEX idx_artifact_retention_states_lifecycle_due;
            DROP TABLE artifact_retention_states;
            DROP TABLE retention_policies;
            """,
        )

    def _upgrade_v8(self, connection: sqlite3.Connection) -> None:
        self._execute_sql_batch(
            connection,
            """
            DROP TRIGGER context_revisions_scope_guard;

            CREATE TRIGGER context_revisions_scope_guard
            BEFORE INSERT ON context_revisions
            WHEN NOT EXISTS (
                    SELECT 1 FROM agents
                    WHERE id = NEW.agent_id AND session_id = NEW.session_id
                )
                OR (
                    NEW.thread_id IS NOT NULL
                    AND NEW.workspace_ref IS NOT NULL
                    AND NOT EXISTS (
                        SELECT 1 FROM threads
                        WHERE id = NEW.thread_id
                            AND workspace_ref = NEW.workspace_ref
                    )
                )
                OR (
                    NEW.compaction_id IS NOT NULL
                    AND NOT EXISTS (
                        SELECT 1 FROM compactions
                        WHERE id = NEW.compaction_id
                            AND session_id = NEW.session_id
                            AND thread_id IS NEW.thread_id
                            AND (
                                source_type = 'thread_items'
                                OR agent_id = NEW.agent_id
                            )
                    )
                )
                OR NEW.prompt_layout_version != 'phase1b.v1'
                OR (
                    (NEW.source_cursor_start IS NULL)
                    != (NEW.source_cursor_end IS NULL)
                )
                OR (
                    NEW.source_cursor_start IS NULL
                    AND NEW.source_cursor_namespace IS NOT NULL
                )
                OR (
                    NEW.source_cursor_start IS NOT NULL
                    AND COALESCE(NEW.source_cursor_namespace, '') != 'items.sequence'
                )
                OR (
                    NEW.thread_id IS NULL
                    AND (
                        NEW.source_cursor_start IS NOT NULL
                        OR NEW.source_cursor_end IS NOT NULL
                        OR json_array_length(NEW.source_item_ids_json) != 0
                    )
                )
                OR (
                    NEW.source_cursor_start IS NOT NULL
                    AND (
                        NOT EXISTS (
                            SELECT 1 FROM items
                            WHERE sequence = NEW.source_cursor_start
                                AND thread_id = NEW.thread_id
                        )
                        OR NOT EXISTS (
                            SELECT 1 FROM items
                            WHERE sequence = NEW.source_cursor_end
                                AND thread_id = NEW.thread_id
                        )
                    )
                )
                OR EXISTS (
                    SELECT 1 FROM json_each(NEW.source_item_ids_json) AS item_id
                    WHERE NOT EXISTS (
                        SELECT 1 FROM items
                        WHERE id = item_id.value AND thread_id = NEW.thread_id
                    )
                )
                OR (
                    NEW.compaction_id IS NULL
                    AND json_array_length(NEW.compaction_refs_json) != 0
                )
                OR (
                    NEW.compaction_id IS NOT NULL
                    AND (
                        json_array_length(NEW.compaction_refs_json) != 1
                        OR json_extract(NEW.compaction_refs_json, '$[0]') != NEW.compaction_id
                    )
                )
                OR json_type(NEW.memory_refs_json) != 'array'
                OR EXISTS (
                    SELECT 1 FROM json_each(NEW.memory_refs_json) AS memory_ref
                    WHERE NOT EXISTS (
                        SELECT 1 FROM memories WHERE id = memory_ref.value
                    )
                )
            BEGIN
                SELECT RAISE(ABORT, 'ContextRevision source scope is invalid');
            END;

            CREATE TABLE workspace_initializations (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT UNIQUE NOT NULL,
                workspace_ref TEXT UNIQUE NOT NULL CHECK (length(workspace_ref) >= 1),
                workspace_hash TEXT UNIQUE NOT NULL CHECK (
                    length(workspace_hash) = 64
                    AND workspace_hash NOT GLOB '*[^0-9a-f]*'
                ),
                readable INTEGER NOT NULL CHECK (readable IN (0, 1)),
                writable INTEGER NOT NULL CHECK (writable IN (0, 1)),
                created_at TEXT NOT NULL
            );

            CREATE TABLE context_baselines (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT UNIQUE NOT NULL,
                session_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                item_cursor_end INTEGER NOT NULL CHECK (item_cursor_end >= 0),
                operation TEXT NOT NULL CHECK (operation IN ('clear', 'compact')),
                compaction_id TEXT,
                previous_baseline_id TEXT,
                created_at TEXT NOT NULL,
                CHECK (
                    (operation = 'clear' AND compaction_id IS NULL)
                    OR (operation = 'compact' AND compaction_id IS NOT NULL)
                ),
                FOREIGN KEY (session_id) REFERENCES sessions(id),
                FOREIGN KEY (thread_id) REFERENCES threads(id),
                FOREIGN KEY (compaction_id) REFERENCES compactions(id),
                FOREIGN KEY (previous_baseline_id) REFERENCES context_baselines(id)
            );

            CREATE INDEX idx_context_baselines_session_thread_sequence
                ON context_baselines(session_id, thread_id, sequence);

            CREATE TABLE review_runs (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT UNIQUE NOT NULL,
                session_id TEXT NOT NULL,
                thread_id TEXT,
                workspace_ref TEXT NOT NULL CHECK (length(workspace_ref) >= 1),
                scope TEXT NOT NULL CHECK (length(scope) BETWEEN 1 AND 2000),
                status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
                artifact_id TEXT,
                error_code TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK (
                    (status = 'completed' AND artifact_id IS NOT NULL AND error_code IS NULL)
                    OR (status = 'failed' AND artifact_id IS NULL AND error_code IS NOT NULL)
                    OR (status = 'running' AND artifact_id IS NULL AND error_code IS NULL)
                ),
                FOREIGN KEY (session_id) REFERENCES sessions(id),
                FOREIGN KEY (thread_id) REFERENCES threads(id),
                FOREIGN KEY (artifact_id) REFERENCES artifacts(id)
            );

            CREATE INDEX idx_review_runs_session_sequence
                ON review_runs(session_id, sequence);

            CREATE TABLE btw_sidecar_runs (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT UNIQUE NOT NULL,
                session_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                workspace_ref TEXT NOT NULL CHECK (length(workspace_ref) >= 1),
                source_item_cursor_end INTEGER NOT NULL CHECK (source_item_cursor_end >= 0),
                prompt TEXT NOT NULL CHECK (length(prompt) BETWEEN 1 AND 100000),
                prompt_hash TEXT NOT NULL CHECK (
                    length(prompt_hash) = 64 AND prompt_hash NOT GLOB '*[^0-9a-f]*'
                ),
                status TEXT NOT NULL CHECK (
                    status IN ('running', 'completed', 'failed', 'promoted')
                ),
                response TEXT,
                response_hash TEXT CHECK (
                    response_hash IS NULL OR (
                        length(response_hash) = 64
                        AND response_hash NOT GLOB '*[^0-9a-f]*'
                    )
                ),
                context_revision_id TEXT UNIQUE,
                promoted_turn_id TEXT,
                promoted_item_id TEXT UNIQUE,
                error_code TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK (
                    (status = 'running' AND response IS NULL AND response_hash IS NULL
                        AND context_revision_id IS NULL AND promoted_turn_id IS NULL
                        AND promoted_item_id IS NULL AND error_code IS NULL)
                    OR (status = 'failed' AND promoted_turn_id IS NULL
                        AND promoted_item_id IS NULL AND error_code IS NOT NULL)
                    OR (status = 'completed' AND response IS NOT NULL
                        AND response_hash IS NOT NULL AND context_revision_id IS NOT NULL
                        AND promoted_turn_id IS NULL AND promoted_item_id IS NULL
                        AND error_code IS NULL)
                    OR (status = 'promoted' AND response IS NOT NULL
                        AND response_hash IS NOT NULL AND context_revision_id IS NOT NULL
                        AND promoted_turn_id IS NOT NULL AND promoted_item_id IS NOT NULL
                        AND error_code IS NULL)
                ),
                FOREIGN KEY (session_id) REFERENCES sessions(id),
                FOREIGN KEY (agent_id) REFERENCES agents(id),
                FOREIGN KEY (thread_id) REFERENCES threads(id),
                FOREIGN KEY (context_revision_id) REFERENCES context_revisions(id),
                FOREIGN KEY (promoted_turn_id) REFERENCES turns(id),
                FOREIGN KEY (promoted_item_id) REFERENCES items(id)
            );

            CREATE INDEX idx_btw_sidecar_runs_thread_sequence
                ON btw_sidecar_runs(thread_id, sequence);

            CREATE TABLE btw_sidecar_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT UNIQUE NOT NULL,
                sidecar_run_id TEXT NOT NULL,
                event_type TEXT NOT NULL CHECK (
                    event_type IN (
                        'btw.started', 'btw.model_completed', 'btw.failed', 'btw.promoted'
                    )
                ),
                body TEXT NOT NULL CHECK (json_valid(body)),
                created_at TEXT NOT NULL,
                FOREIGN KEY (sidecar_run_id) REFERENCES btw_sidecar_runs(id)
            );

            CREATE INDEX idx_btw_sidecar_events_run_sequence
                ON btw_sidecar_events(sidecar_run_id, sequence);

            CREATE TABLE phase1d_command_audit_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT UNIQUE NOT NULL,
                command_execution_id TEXT NOT NULL,
                command_kind TEXT NOT NULL CHECK (
                    command_kind IN (
                        'workspace.initialize', 'review.run',
                        'context.clear', 'context.compact'
                    )
                ),
                event_type TEXT NOT NULL CHECK (length(event_type) BETWEEN 1 AND 200),
                resource_type TEXT,
                resource_id TEXT,
                detail_json TEXT NOT NULL CHECK (
                    json_valid(detail_json) AND json_type(detail_json) = 'object'
                ),
                created_at TEXT NOT NULL,
                FOREIGN KEY (command_execution_id) REFERENCES command_executions(id)
            );

            CREATE INDEX idx_phase1d_command_audit_execution_sequence
                ON phase1d_command_audit_events(command_execution_id, sequence);

            CREATE TRIGGER workspace_initializations_no_update
            BEFORE UPDATE ON workspace_initializations
            BEGIN
                SELECT RAISE(ABORT, 'Workspace initialization facts are immutable');
            END;

            CREATE TRIGGER workspace_initializations_no_delete
            BEFORE DELETE ON workspace_initializations
            BEGIN
                SELECT RAISE(ABORT, 'Workspace initialization facts are immutable');
            END;

            CREATE TRIGGER context_baselines_scope_guard
            BEFORE INSERT ON context_baselines
            WHEN NOT EXISTS (
                    SELECT 1 FROM sessions WHERE id = NEW.session_id
                )
                OR NOT EXISTS (
                    SELECT 1 FROM threads WHERE id = NEW.thread_id
                )
                OR (
                    NEW.item_cursor_end > 0
                    AND NOT EXISTS (
                        SELECT 1 FROM items
                        WHERE sequence = NEW.item_cursor_end AND thread_id = NEW.thread_id
                    )
                )
                OR (
                    NEW.previous_baseline_id IS NOT NULL
                    AND NOT EXISTS (
                        SELECT 1 FROM context_baselines
                        WHERE id = NEW.previous_baseline_id
                            AND session_id = NEW.session_id
                            AND thread_id = NEW.thread_id
                            AND item_cursor_end <= NEW.item_cursor_end
                    )
                )
                OR (
                    NEW.operation = 'compact'
                    AND NOT EXISTS (
                        SELECT 1 FROM compactions
                        WHERE id = NEW.compaction_id
                            AND session_id = NEW.session_id
                            AND thread_id = NEW.thread_id
                            AND source_type = 'thread_items'
                            AND source_cursor_end = NEW.item_cursor_end
                    )
                )
            BEGIN
                SELECT RAISE(ABORT, 'Context baseline scope is invalid');
            END;

            CREATE TRIGGER context_baselines_no_update
            BEFORE UPDATE ON context_baselines
            BEGIN
                SELECT RAISE(ABORT, 'Context baselines are append-only');
            END;

            CREATE TRIGGER context_baselines_no_delete
            BEFORE DELETE ON context_baselines
            BEGIN
                SELECT RAISE(ABORT, 'Context baselines are append-only');
            END;

            CREATE TRIGGER btw_sidecar_events_no_update
            BEFORE UPDATE ON btw_sidecar_events
            BEGIN
                SELECT RAISE(ABORT, 'BTW Sidecar events are append-only');
            END;

            CREATE TRIGGER btw_sidecar_events_no_delete
            BEFORE DELETE ON btw_sidecar_events
            BEGIN
                SELECT RAISE(ABORT, 'BTW Sidecar events are append-only');
            END;

            CREATE TRIGGER phase1d_command_audit_events_no_update
            BEFORE UPDATE ON phase1d_command_audit_events
            BEGIN
                SELECT RAISE(ABORT, 'Phase 1D command audit is append-only');
            END;

            CREATE TRIGGER phase1d_command_audit_events_no_delete
            BEFORE DELETE ON phase1d_command_audit_events
            BEGIN
                SELECT RAISE(ABORT, 'Phase 1D command audit is append-only');
            END;
            """,
        )
        self._replace_phase1d_prompt_compaction_agent_guard(
            connection,
            allow_thread_items_cross_agent=True,
        )
        self._create_phase1d_run_scope_guards(connection)

    def _downgrade_v8(self, connection: sqlite3.Connection) -> None:
        populated = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM workspace_initializations)
                + (SELECT COUNT(*) FROM context_baselines)
                + (SELECT COUNT(*) FROM review_runs)
                + (SELECT COUNT(*) FROM btw_sidecar_runs)
                + (SELECT COUNT(*) FROM btw_sidecar_events)
                + (SELECT COUNT(*) FROM phase1d_command_audit_events) AS row_count
            """
        ).fetchone()
        if populated is not None and int(populated["row_count"]) > 0:
            raise MigrationError("refusing to roll back Phase 1D tables while they contain data")
        self._execute_sql_batch(
            connection,
            """
            DROP TRIGGER phase1d_command_audit_events_no_delete;
            DROP TRIGGER phase1d_command_audit_events_no_update;
            DROP TRIGGER btw_sidecar_events_no_delete;
            DROP TRIGGER btw_sidecar_events_no_update;
            DROP TRIGGER btw_sidecar_runs_identity_guard;
            DROP TRIGGER btw_sidecar_runs_scope_guard;
            DROP TRIGGER review_runs_identity_guard;
            DROP TRIGGER review_runs_scope_guard;
            DROP TRIGGER context_baselines_no_delete;
            DROP TRIGGER context_baselines_no_update;
            DROP TRIGGER context_baselines_scope_guard;
            DROP TRIGGER workspace_initializations_no_delete;
            DROP TRIGGER workspace_initializations_no_update;
            DROP INDEX idx_phase1d_command_audit_execution_sequence;
            DROP TABLE phase1d_command_audit_events;
            DROP INDEX idx_btw_sidecar_events_run_sequence;
            DROP TABLE btw_sidecar_events;
            DROP INDEX idx_btw_sidecar_runs_thread_sequence;
            DROP TABLE btw_sidecar_runs;
            DROP INDEX idx_review_runs_session_sequence;
            DROP TABLE review_runs;
            DROP INDEX idx_context_baselines_session_thread_sequence;
            DROP TABLE context_baselines;
            DROP TABLE workspace_initializations;

            DROP TRIGGER context_revisions_scope_guard;

            CREATE TRIGGER context_revisions_scope_guard
            BEFORE INSERT ON context_revisions
            WHEN NOT EXISTS (
                    SELECT 1 FROM agents
                    WHERE id = NEW.agent_id AND session_id = NEW.session_id
                )
                OR (
                    NEW.thread_id IS NOT NULL
                    AND NEW.workspace_ref IS NOT NULL
                    AND NOT EXISTS (
                        SELECT 1 FROM threads
                        WHERE id = NEW.thread_id
                            AND workspace_ref = NEW.workspace_ref
                    )
                )
                OR (
                    NEW.compaction_id IS NOT NULL
                    AND NOT EXISTS (
                        SELECT 1 FROM compactions
                        WHERE id = NEW.compaction_id
                            AND session_id = NEW.session_id
                            AND agent_id = NEW.agent_id
                            AND thread_id IS NEW.thread_id
                    )
                )
                OR NEW.prompt_layout_version != 'phase1b.v1'
                OR (
                    (NEW.source_cursor_start IS NULL)
                    != (NEW.source_cursor_end IS NULL)
                )
                OR (
                    NEW.source_cursor_start IS NULL
                    AND NEW.source_cursor_namespace IS NOT NULL
                )
                OR (
                    NEW.source_cursor_start IS NOT NULL
                    AND COALESCE(NEW.source_cursor_namespace, '') != 'items.sequence'
                )
                OR (
                    NEW.thread_id IS NULL
                    AND (
                        NEW.source_cursor_start IS NOT NULL
                        OR NEW.source_cursor_end IS NOT NULL
                        OR json_array_length(NEW.source_item_ids_json) != 0
                    )
                )
                OR (
                    NEW.source_cursor_start IS NOT NULL
                    AND (
                        NOT EXISTS (
                            SELECT 1 FROM items
                            WHERE sequence = NEW.source_cursor_start
                                AND thread_id = NEW.thread_id
                        )
                        OR NOT EXISTS (
                            SELECT 1 FROM items
                            WHERE sequence = NEW.source_cursor_end
                                AND thread_id = NEW.thread_id
                        )
                    )
                )
                OR EXISTS (
                    SELECT 1 FROM json_each(NEW.source_item_ids_json) AS item_id
                    WHERE NOT EXISTS (
                        SELECT 1 FROM items
                        WHERE id = item_id.value AND thread_id = NEW.thread_id
                    )
                )
                OR (
                    NEW.compaction_id IS NULL
                    AND json_array_length(NEW.compaction_refs_json) != 0
                )
                OR (
                    NEW.compaction_id IS NOT NULL
                    AND (
                        json_array_length(NEW.compaction_refs_json) != 1
                        OR json_extract(NEW.compaction_refs_json, '$[0]') != NEW.compaction_id
                    )
                )
                OR json_type(NEW.memory_refs_json) != 'array'
                OR EXISTS (
                    SELECT 1 FROM json_each(NEW.memory_refs_json) AS memory_ref
                    WHERE NOT EXISTS (
                        SELECT 1 FROM memories WHERE id = memory_ref.value
                    )
                )
            BEGIN
                SELECT RAISE(ABORT, 'ContextRevision source scope is invalid');
            END;
            """,
        )
        self._replace_phase1d_prompt_compaction_agent_guard(
            connection,
            allow_thread_items_cross_agent=False,
        )

    def _downgrade_v6(self, connection: sqlite3.Connection) -> None:
        populated = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM compactions)
                + (SELECT COUNT(*) FROM context_revisions)
                + (SELECT COUNT(*) FROM prompt_blocks)
                + (SELECT COUNT(*) FROM reference_bindings) AS row_count
            """
        ).fetchone()
        if populated is not None and int(populated["row_count"]) > 0:
            raise MigrationError("refusing to roll back Phase 1B tables while they contain data")
        self._execute_sql_batch(
            connection,
            """
            DROP TRIGGER reference_bindings_no_delete;
            DROP TRIGGER reference_bindings_no_update;
            DROP TRIGGER reference_bindings_target_guard;
            DROP TRIGGER reference_bindings_position_guard;
            DROP TRIGGER prompt_blocks_no_delete;
            DROP TRIGGER prompt_blocks_no_update;
            DROP TRIGGER prompt_blocks_source_refs_guard;
            DROP TRIGGER prompt_blocks_position_guard;
            DROP TRIGGER context_revisions_no_delete;
            DROP TRIGGER context_revisions_no_update;
            DROP TRIGGER context_revisions_memory_sources_guard;
            DROP TRIGGER context_revisions_scope_guard;
            DROP TRIGGER compactions_no_delete;
            DROP TRIGGER compactions_no_update;
            DROP TRIGGER compactions_scope_guard;
            DROP INDEX idx_reference_bindings_target;
            DROP TABLE reference_bindings;
            DROP INDEX idx_prompt_blocks_revision_position;
            DROP TABLE prompt_blocks;
            DROP INDEX idx_context_revisions_thread_sequence;
            DROP INDEX idx_context_revisions_session_sequence;
            DROP TABLE context_revisions;
            DROP INDEX idx_compactions_thread_sequence;
            DROP INDEX idx_compactions_agent_sequence;
            DROP TABLE compactions;
            """,
        )
        for table in ("memory_versions", "threads", "items"):
            connection.execute(f'ALTER TABLE "{table}" DROP COLUMN body_hash')

    def _downgrade_v3(self, connection: sqlite3.Connection) -> None:
        populated = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM tool_action_receipts)
                + (SELECT COUNT(*) FROM command_executions)
                + (SELECT COUNT(*) FROM approval_requests)
                + (SELECT COUNT(*) FROM approval_decisions)
                + (SELECT COUNT(*) FROM approval_audit_events)
                + (SELECT COUNT(*) FROM evaluation_run_events) AS row_count
            """
        ).fetchone()
        if populated is not None and int(populated["row_count"]) > 0:
            raise MigrationError("refusing to roll back M0 tables while they contain audit data")
        self._execute_sql_batch(
            connection,
            """
            DROP INDEX idx_approval_audit_approval_sequence;
            DROP TABLE approval_audit_events;
            DROP INDEX idx_evaluation_run_events_run_sequence;
            DROP TABLE evaluation_run_events;
            DROP TABLE approval_decisions;
            DROP INDEX idx_approval_requests_session_status_requested;
            DROP TABLE approval_requests;
            DROP INDEX idx_command_executions_status_updated;
            DROP TABLE command_executions;
            DROP INDEX idx_tool_action_receipts_session_created;
            DROP TABLE tool_action_receipts;
            DROP INDEX idx_events_session_sequence;
            """,
        )

    @staticmethod
    def _ensure_evaluation_result_columns(connection: sqlite3.Connection) -> None:
        """Upgrade historical result tables to the one canonical NOT NULL shape."""

        column_rows = connection.execute("PRAGMA table_info(evaluation_results)").fetchall()
        columns = {str(row["name"]): row for row in column_rows}
        if "updated_at" not in columns:
            connection.execute("ALTER TABLE evaluation_results ADD COLUMN updated_at TEXT")
            connection.execute(
                "UPDATE evaluation_results SET updated_at = created_at WHERE updated_at IS NULL"
            )
            column_rows = connection.execute("PRAGMA table_info(evaluation_results)").fetchall()
            columns = {str(row["name"]): row for row in column_rows}
        null_row = connection.execute(
            "SELECT 1 FROM evaluation_results WHERE updated_at IS NULL LIMIT 1"
        ).fetchone()
        if null_row is not None:
            raise MigrationError("legacy evaluation_results.updated_at contains NULL values")
        if bool(columns["updated_at"]["notnull"]):
            return
        SQLiteStore._execute_sql_batch(
            connection,
            """
            CREATE TABLE evaluation_results__operant_v2 (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                case_id TEXT NOT NULL,
                variant_id TEXT NOT NULL,
                repetition INTEGER NOT NULL,
                status TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(run_id, case_id, variant_id, repetition),
                FOREIGN KEY (run_id) REFERENCES evaluation_runs(id),
                FOREIGN KEY (case_id) REFERENCES evaluation_cases(id),
                FOREIGN KEY (variant_id) REFERENCES evaluation_variants(id)
            );

            INSERT INTO evaluation_results__operant_v2(
                id, run_id, case_id, variant_id, repetition, status, body,
                created_at, updated_at
            )
            SELECT
                id, run_id, case_id, variant_id, repetition, status, body,
                created_at, updated_at
            FROM evaluation_results;

            DROP TABLE evaluation_results;
            ALTER TABLE evaluation_results__operant_v2 RENAME TO evaluation_results;
            """,
        )

    @staticmethod
    def _interrupt_running_workflows(
        connection: sqlite3.Connection,
        *,
        now: datetime | None = None,
    ) -> None:
        """Make runs left in ``running`` state safe to inspect after restart."""

        observed_at = utc_now() if now is None else now
        rows = connection.execute(
            """
            SELECT id, body, updated_at FROM workflow_runs
            WHERE status = ?
              AND NOT EXISTS (
                  SELECT 1 FROM workflow_execution_leases AS guard
                  WHERE guard.workflow_run_id = workflow_runs.id
                    AND guard.released_at IS NULL
                    AND guard.expires_at > ?
              )
              AND NOT EXISTS (
                  SELECT 1 FROM session_run_leases AS lease
                  WHERE lease.workflow_run_id = workflow_runs.id
                    AND lease.released_at IS NULL
                    AND lease.cancel_requested = 0
                    AND lease.expires_at > ?
              )
            """,
            (
                WorkflowRunStatus.RUNNING.value,
                observed_at.isoformat(),
                observed_at.isoformat(),
            ),
        ).fetchall()
        if not rows:
            return
        interrupted_at = observed_at
        for row in rows:
            workflow_run = WorkflowRun.model_validate_json(row["body"])
            updated = workflow_run.model_copy(
                update={
                    "status": WorkflowRunStatus.INTERRUPTED,
                    "updated_at": interrupted_at,
                }
            )
            connection.execute(
                """
                UPDATE workflow_runs
                SET body = ?, status = ?, current_stage = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    updated.model_dump_json(),
                    updated.status.value,
                    updated.current_stage.value,
                    updated.updated_at.isoformat(),
                    row["id"],
                ),
            )

    @staticmethod
    def _interrupt_running_evaluations(connection: sqlite3.Connection) -> None:
        """Atomically preserve unknown scheduled rows after an evaluator restart."""

        rows = connection.execute(
            "SELECT id, suite_id, body FROM evaluation_runs WHERE status = ?",
            (EvaluationRunStatus.RUNNING.value,),
        ).fetchall()
        if not rows:
            return
        interrupted_at = utc_now()
        for row in rows:
            evaluation_run = EvaluationRun.model_validate_json(row["body"])
            suite_row = connection.execute(
                "SELECT body FROM evaluation_suites WHERE id = ?", (row["suite_id"],)
            ).fetchone()
            if suite_row is None:
                raise NotFoundError(f"evaluation suite not found: {row['suite_id']}")
            suite = SQLiteStore._load_evaluation_suite(connection, suite_row["body"])
            cases = {case.id: case for case in suite.cases}
            variants = {variant.id: variant for variant in suite.variants}
            pending_rows = connection.execute(
                "SELECT id, body FROM evaluation_results WHERE run_id = ? AND status = ?",
                (evaluation_run.id, EvaluationResultStatus.PENDING.value),
            ).fetchall()
            for pending_row in pending_rows:
                pending = EvaluationResult.model_validate_json(pending_row["body"])
                artifact_workspace = pending.artifact_workspace
                if artifact_workspace is None:
                    # Pre-v1 recovery rows did not reserve the namespace.  A
                    # deterministic ref preserves a safe identifier without
                    # pretending that an artifact was actually captured.
                    artifact_workspace = ArtifactWorkspace(
                        artifact_ref=(
                            f"evaluation/{pending.run_id}/{pending.case_id}/"
                            f"{pending.variant_id}/{pending.repetition}"
                        )
                    )
                interrupted = EvaluationResult.model_validate(
                    {
                        **pending.model_dump(),
                        "status": EvaluationResultStatus.INTERRUPTED,
                        "metrics": EvaluationMetrics(),
                        "changed_paths": (),
                        "snapshot": None,
                        "artifact_workspace": artifact_workspace,
                        "verification": (),
                        "execution_facts": (),
                        "failure_analysis": interrupted_evaluation_failure_analysis(),
                        "trace_session_ids": (),
                        "trace_workflow_run_id": None,
                        "updated_at": interrupted_at,
                        "finished_at": interrupted_at,
                    }
                )
                case = cases.get(interrupted.case_id)
                variant = variants.get(interrupted.variant_id)
                if case is None or variant is None:
                    raise NotFoundError("evaluation result references a missing case or variant")
                assert_evaluation_result_contract(case, variant, interrupted)
                connection.execute(
                    """
                    UPDATE evaluation_results
                    SET status = ?, body = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        interrupted.status.value,
                        interrupted.model_dump_json(),
                        interrupted.updated_at.isoformat(),
                        pending_row["id"],
                    ),
                )

            result_rows = connection.execute(
                "SELECT body FROM evaluation_results WHERE run_id = ?",
                (evaluation_run.id,),
            ).fetchall()
            results = tuple(
                EvaluationResult.model_validate_json(result_row["body"])
                for result_row in result_rows
            )
            updated = evaluation_run.model_copy(
                update={
                    "status": EvaluationRunStatus.INTERRUPTED,
                    "finished_at": interrupted_at,
                    "updated_at": interrupted_at,
                    "last_error_type": "process_interrupted",
                    "aggregate": aggregate_evaluation_results(
                        results,
                        suite_id=suite.id,
                        run_id=evaluation_run.id,
                        expected_result_count=suite.expanded_result_count,
                    ),
                }
            )
            connection.execute(
                """
                UPDATE evaluation_runs
                SET body = ?, status = ?, execution_strategy = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    updated.model_dump_json(),
                    updated.status.value,
                    updated.execution_strategy.value,
                    updated.updated_at.isoformat(),
                    row["id"],
                ),
            )

    @staticmethod
    def _reconcile_in_progress_commands(connection: sqlite3.Connection) -> None:
        """Never replay a REST Command whose pre-crash outcome is uncertain."""

        reconciled_at = utc_now().isoformat()
        connection.execute(
            """
            UPDATE command_executions
            SET status = ?, error_code = ?, updated_at = ?, completed_at = ?
            WHERE status = ?
            """,
            (
                CommandExecutionStatus.MANUAL_RECONCILE_REQUIRED.value,
                "process_interrupted",
                reconciled_at,
                reconciled_at,
                CommandExecutionStatus.IN_PROGRESS.value,
            ),
        )

    @staticmethod
    def _reconcile_in_progress_tool_actions(connection: sqlite3.Connection) -> None:
        reconciled_at = utc_now().isoformat()
        connection.execute(
            """
            UPDATE tool_action_receipts
            SET status = ?, error_code = ?, updated_at = ?
            WHERE status = ? AND NOT EXISTS (
                SELECT 1 FROM session_run_leases AS lease
                WHERE lease.session_id = tool_action_receipts.session_id
                  AND lease.agent_id = tool_action_receipts.agent_id
                  AND lease.released_at IS NULL
                  AND lease.cancel_requested = 0
                  AND lease.expires_at > ?
            )
            """,
            (
                ToolActionReceiptStatus.OUTCOME_UNKNOWN.value,
                "process_interrupted",
                reconciled_at,
                ToolActionReceiptStatus.IN_PROGRESS.value,
                reconciled_at,
            ),
        )

    @staticmethod
    def _expire_pending_approvals(
        connection: sqlite3.Connection, session_id: str | None = None
    ) -> None:
        now = utc_now()
        expirable = (ApprovalStatus.PENDING.value, ApprovalStatus.APPROVED.value)
        parameters: list[Any] = [*expirable, now.isoformat()]
        query = (
            "SELECT id, status FROM approval_requests WHERE status IN (?, ?) AND expires_at <= ?"
        )
        if session_id is not None:
            query += " AND session_id = ?"
            parameters.append(session_id)
        rows = connection.execute(query, parameters).fetchall()
        for row in rows:
            cursor = connection.execute(
                """
                UPDATE approval_requests
                SET status = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    ApprovalStatus.EXPIRED.value,
                    now.isoformat(),
                    row["id"],
                    row["status"],
                ),
            )
            if cursor.rowcount != 1:
                continue
            SQLiteStore._append_approval_audit_row(
                connection,
                ApprovalAuditEvent(
                    approval_id=row["id"],
                    event_type="approval.expired",
                    payload={"status": ApprovalStatus.EXPIRED.value},
                    created_at=now,
                ),
            )

    def add_model_profile(self, profile: ModelProfile) -> ModelProfile:
        with self._connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO model_profiles(id, body, created_at) VALUES (?, ?, ?)",
                    (profile.id, profile.model_dump_json(), profile.created_at.isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"model profile already exists: {profile.id}") from exc
        return profile

    def get_model_profile(self, profile_id: str) -> ModelProfile:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT body FROM model_profiles WHERE id = ?", (profile_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"model profile not found: {profile_id}")
        return ModelProfile.model_validate_json(row["body"])

    def list_model_profiles(self) -> list[ModelProfile]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT body FROM model_profiles ORDER BY created_at, id"
            ).fetchall()
        return [ModelProfile.model_validate_json(row["body"]) for row in rows]

    def get_model_profile_by_name(self, name: str) -> ModelProfile:
        for profile in self.list_model_profiles():
            if profile.name == name:
                return profile
        raise NotFoundError(f"model profile not found by name: {name}")

    def update_model_profile(self, profile_id: str, **changes: Any) -> ModelProfile:
        current = self.get_model_profile(profile_id)
        forbidden = {"id", "created_at"}
        attempted = forbidden.intersection(changes)
        if attempted:
            raise ValueError(f"cannot change model profile identity fields: {sorted(attempted)}")
        updated = ModelProfile.model_validate({**current.model_dump(), **changes})
        with self._connect() as connection:
            connection.execute(
                "UPDATE model_profiles SET body = ? WHERE id = ?",
                (updated.model_dump_json(), profile_id),
            )
        return updated

    def deactivate_model_profile(self, profile_id: str) -> ModelProfile:
        return self.update_model_profile(profile_id, enabled=False)

    def create_role(self, role: RolePreset) -> RolePreset:
        if role.version != 1:
            raise ValueError("a new role must start at version 1")
        self._validate_role_model(role)
        with self._connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO role_heads(id, current_version) VALUES (?, 1)", (role.id,)
                )
                connection.execute(
                    """
                    INSERT INTO role_versions(role_id, version, body, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (role.id, role.version, role.model_dump_json(), role.created_at.isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"role already exists: {role.id}") from exc
        return role

    def get_role(self, role_id: str, version: int | None = None) -> RolePreset:
        with self._connect() as connection:
            if version is None:
                head = connection.execute(
                    "SELECT current_version FROM role_heads WHERE id = ?", (role_id,)
                ).fetchone()
                if head is None:
                    raise NotFoundError(f"role not found: {role_id}")
                version = int(head["current_version"])
            row = connection.execute(
                "SELECT body FROM role_versions WHERE role_id = ? AND version = ?",
                (role_id, version),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"role version not found: {role_id}@{version}")
        return RolePreset.model_validate_json(row["body"])

    def get_role_by_name(self, name: str) -> RolePreset:
        for role in self.list_roles(include_inactive=True):
            if role.name == name:
                return role
        raise NotFoundError(f"role not found by name: {name}")

    def list_role_versions(self, role_id: str) -> list[RolePreset]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT body FROM role_versions
                WHERE role_id = ? ORDER BY version
                """,
                (role_id,),
            ).fetchall()
        if not rows:
            raise NotFoundError(f"role not found: {role_id}")
        return [RolePreset.model_validate_json(row["body"]) for row in rows]

    def update_role(self, role_id: str, **changes: Any) -> RolePreset:
        current = self.get_role(role_id)
        forbidden = {"id", "version", "created_at"}
        attempted = forbidden.intersection(changes)
        if attempted:
            raise ValueError(f"cannot change version identity fields: {sorted(attempted)}")
        updated = current.model_copy(
            update={
                **changes,
                "version": current.version + 1,
                "created_at": current.created_at.__class__.now(current.created_at.tzinfo),
            }
        )
        updated = RolePreset.model_validate(updated.model_dump())
        self._validate_role_model(updated)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO role_versions(role_id, version, body, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    updated.id,
                    updated.version,
                    updated.model_dump_json(),
                    updated.created_at.isoformat(),
                ),
            )
            connection.execute(
                "UPDATE role_heads SET current_version = ? WHERE id = ?",
                (updated.version, updated.id),
            )
        return updated

    def deactivate_role(self, role_id: str) -> RolePreset:
        return self.update_role(role_id, status=RoleStatus.INACTIVE)

    def copy_role(self, role_id: str, *, name: str) -> RolePreset:
        source = self.get_role(role_id)
        copied = RolePreset(
            name=name,
            system_prompt=source.system_prompt,
            model_profile_id=source.model_profile_id,
            effort=source.effort,
            tool_policy=source.tool_policy,
            budget=source.budget,
            memory_scope=source.memory_scope,
        )
        return self.create_role(copied)

    def create_session(
        self,
        role_id: str,
        *,
        model_profile_id: str | None = None,
        effort: str | None = None,
        budget_overrides: dict[str, Any] | None = None,
        thread_id: str | None = None,
    ) -> Session:
        role = self.get_role(role_id)
        if role.status is RoleStatus.INACTIVE:
            raise ValueError("cannot create a session from an inactive role")
        selected_profile_id = model_profile_id or role.model_profile_id
        profile = self.get_model_profile(selected_profile_id)
        if not profile.enabled:
            raise ValueError(f"model profile is inactive: {profile.id}")

        selected_effort = role.effort if effort is None else type(role.effort)(effort)
        if selected_effort not in profile.supported_efforts:
            raise ValueError(
                f"effort {selected_effort.value!r} is not supported by {profile.name!r}"
            )

        budget = role.budget
        if budget.max_output_tokens is None and profile.default_token_budget is not None:
            budget = budget.model_copy(update={"max_output_tokens": profile.default_token_budget})
        overridden_budget_fields: tuple[str, ...] = ()
        if budget_overrides:
            budget = type(role.budget).model_validate({**budget.model_dump(), **budget_overrides})
            overridden_budget_fields = tuple(sorted(budget_overrides))

        snapshot = RoleSnapshot(
            role_id=role.id,
            role_version=role.version,
            role_name=role.name,
            system_prompt=role.system_prompt,
            model_profile_id=profile.id,
            model_profile_name=profile.name,
            provider=profile.provider,
            model_id=profile.model_id,
            base_url=profile.base_url,
            secret_ref=profile.secret_ref,
            context_window=profile.context_window,
            input_usd_per_million_tokens=profile.input_usd_per_million_tokens,
            output_usd_per_million_tokens=profile.output_usd_per_million_tokens,
            effort=selected_effort,
            provider_effort_parameter=profile.effort_parameter,
            provider_effort_value=profile.provider_effort_value(selected_effort),
            tool_policy=role.tool_policy,
            budget=budget,
            memory_scope=role.memory_scope,
            overrides=SnapshotOverrides(
                effort_overridden=effort is not None,
                model_profile_overridden=model_profile_id is not None,
                budget_fields=overridden_budget_fields,
            ),
        )
        session = Session(role_snapshot=snapshot)
        with self._connect() as connection:
            # The session row and its canonical Thread legacy reference are a
            # single identity boundary.  Validate and write both on the same
            # transaction so a bad/competing Thread can never leave an
            # unbound Session behind.
            connection.execute("BEGIN IMMEDIATE")
            if thread_id is not None:
                self._assert_active_thread(connection, thread_id)
                existing_session_ref = connection.execute(
                    """
                    SELECT source_id FROM thread_legacy_refs
                    WHERE thread_id = ? AND source_type = 'session'
                    """,
                    (thread_id,),
                ).fetchone()
                if existing_session_ref is not None:
                    raise ConflictError("thread is already bound to a session")
            try:
                connection.execute(
                    "INSERT INTO sessions(id, body, created_at) VALUES (?, ?, ?)",
                    (session.id, session.model_dump_json(), session.created_at.isoformat()),
                )
                if thread_id is not None:
                    connection.execute(
                        """
                        INSERT INTO thread_legacy_refs(
                            thread_id, source_type, source_id, created_at
                        ) VALUES (?, 'session', ?, ?)
                        """,
                        (thread_id, session.id, session.created_at.isoformat()),
                    )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("session or Thread legacy mapping already exists") from exc
        return session

    def get_session(self, session_id: str) -> Session:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT body FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"session not found: {session_id}")
        return Session.model_validate_json(row["body"])

    def create_agent(self, session_id: str) -> AgentInstance:
        session = self.get_session(session_id)
        agent = AgentInstance(session_id=session.id, role_snapshot=session.role_snapshot)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO agents(id, session_id, status, body, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    agent.id,
                    agent.session_id,
                    agent.status.value,
                    agent.model_dump_json(),
                    agent.created_at.isoformat(),
                ),
            )
        return agent

    def get_agent(self, agent_id: str) -> AgentInstance:
        with self._connect() as connection:
            row = connection.execute("SELECT body FROM agents WHERE id = ?", (agent_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"agent not found: {agent_id}")
        return AgentInstance.model_validate_json(row["body"])

    def update_agent_status(self, agent_id: str, status: AgentStatus) -> AgentInstance:
        current = self.get_agent(agent_id)
        updated = AgentInstance.model_validate({**current.model_dump(), "status": status})
        with self._connect() as connection:
            connection.execute(
                "UPDATE agents SET status = ?, body = ? WHERE id = ?",
                (status.value, updated.model_dump_json(), agent_id),
            )
        return updated

    def append_event(self, event: Event) -> Event:
        self.get_session(event.session_id)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO events(
                    id, session_id, agent_id, event_type, body, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.session_id,
                    event.agent_id,
                    event.event_type,
                    json.dumps(event.payload, ensure_ascii=False, separators=(",", ":")),
                    event.created_at.isoformat(),
                ),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("SQLite did not return an event cursor")
            event_cursor = int(cursor.lastrowid)
        return event.model_copy(update={"cursor": event_cursor})

    def list_events(
        self,
        session_id: str,
        *,
        after_cursor: int | None = None,
        limit: int = 1000,
    ) -> list[Event]:
        if after_cursor is not None and after_cursor < 0:
            raise ValueError("event cursor must not be negative")
        if not 1 <= limit <= 1000:
            raise ValueError("event list limit must be between 1 and 1000")
        query = """
            SELECT sequence, id, session_id, agent_id, event_type, body, created_at
            FROM events WHERE session_id = ?
        """
        parameters: list[Any] = [session_id]
        if after_cursor is not None:
            query += " AND sequence > ?"
            parameters.append(after_cursor)
        query += " ORDER BY sequence"
        query += " LIMIT ?"
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            Event(
                id=row["id"],
                cursor=int(row["sequence"]),
                session_id=row["session_id"],
                agent_id=row["agent_id"],
                event_type=row["event_type"],
                payload=json.loads(row["body"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    # Phase 1A canonical Thread history

    def create_thread(self, thread: ConversationThread) -> ConversationThread:
        if thread.cursor is not None:
            raise ValueError("a new thread cannot provide a cursor")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if thread.parent_thread_id is not None:
                parent = connection.execute(
                    "SELECT 1 FROM threads WHERE id = ?",
                    (thread.parent_thread_id,),
                ).fetchone()
                if parent is None:
                    raise NotFoundError(f"parent thread not found: {thread.parent_thread_id}")
            for legacy_ref in thread.legacy_refs:
                self._validate_legacy_ref(connection, legacy_ref)
            try:
                body = thread.model_dump_json()
                thread_columns = {
                    str(row["name"])
                    for row in connection.execute('PRAGMA table_info("threads")').fetchall()
                }
                if "body_hash" in thread_columns:
                    fields = (
                        "id, parent_thread_id, workspace_ref, status, body, body_hash, "
                        "created_at, updated_at, archived_at"
                    )
                    values: tuple[Any, ...]
                    values = (
                        thread.id,
                        thread.parent_thread_id,
                        thread.workspace_ref,
                        thread.status.value,
                        body,
                        hashlib.sha256(body.encode("utf-8")).hexdigest(),
                        thread.created_at.isoformat(),
                        thread.updated_at.isoformat(),
                        thread.archived_at.isoformat() if thread.archived_at is not None else None,
                    )
                else:
                    fields = (
                        "id, parent_thread_id, workspace_ref, status, body, "
                        "created_at, updated_at, archived_at"
                    )
                    values = (
                        thread.id,
                        thread.parent_thread_id,
                        thread.workspace_ref,
                        thread.status.value,
                        body,
                        thread.created_at.isoformat(),
                        thread.updated_at.isoformat(),
                        thread.archived_at.isoformat() if thread.archived_at is not None else None,
                    )
                placeholders = ", ".join("?" for _ in values)
                cursor = connection.execute(
                    f"INSERT INTO threads({fields}) VALUES ({placeholders})",
                    values,
                ).lastrowid
                if cursor is None:
                    raise RuntimeError("thread insert did not produce a cursor")
                for legacy_ref in thread.legacy_refs:
                    connection.execute(
                        """
                        INSERT INTO thread_legacy_refs(
                            thread_id, source_type, source_id, created_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            thread.id,
                            legacy_ref.source_type.value,
                            legacy_ref.source_id,
                            thread.created_at.isoformat(),
                        ),
                    )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("thread identity or legacy mapping already exists") from exc
            row = connection.execute(
                "SELECT * FROM threads WHERE id = ?",
                (thread.id,),
            ).fetchone()
            assert row is not None
            return self._thread_from_row(connection, row)

    def get_thread(self, thread_id: str) -> ConversationThread:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM threads WHERE id = ?",
                (thread_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"thread not found: {thread_id}")
            return self._thread_from_row(connection, row)

    def context_source_body_hash(
        self,
        source_type: ContextSourceType,
        source_id: str,
        *,
        cursor: int | None,
    ) -> str:
        """Return the hash of the exact canonical JSON body used by provenance."""

        if source_type not in {
            ContextSourceType.THREAD,
            ContextSourceType.ITEM,
            ContextSourceType.MEMORY,
        }:
            raise ValueError("only canonical body Context sources have a body hash")
        with self._connect() as connection:
            if source_type is ContextSourceType.THREAD:
                row = connection.execute(
                    "SELECT body FROM threads WHERE id = ? AND sequence = ?",
                    (source_id, cursor),
                ).fetchone()
            elif source_type is ContextSourceType.ITEM:
                row = connection.execute(
                    "SELECT body FROM items WHERE id = ? AND sequence = ?",
                    (source_id, cursor),
                ).fetchone()
            else:
                if cursor is None:
                    row = connection.execute(
                        """
                        SELECT mv.body
                        FROM memories AS m
                        JOIN memory_versions AS mv
                            ON mv.memory_id = m.id AND mv.version = m.current_version
                        WHERE m.id = ? AND mv.status = 'active'
                        """,
                        (source_id,),
                    ).fetchone()
                else:
                    row = connection.execute(
                        """
                        SELECT mv.body
                        FROM memories AS m
                        JOIN memory_versions AS mv
                            ON mv.memory_id = m.id AND mv.version = ?
                        WHERE m.id = ?
                        """,
                        (cursor, source_id),
                    ).fetchone()
        if row is None:
            raise NotFoundError("canonical Context source body not found")
        return hashlib.sha256(str(row["body"]).encode("utf-8")).hexdigest()

    def list_threads(
        self,
        *,
        after_cursor: int | None = None,
        limit: int = 100,
        parent_thread_id: str | None = None,
        workspace_ref: str | None = None,
        status: ThreadStatus | str | None = None,
    ) -> list[ConversationThread]:
        cursor, page_limit = self._validate_cursor_page(after_cursor, limit)
        where = ["sequence > ?"]
        params: list[Any] = [cursor]
        if parent_thread_id is not None:
            where.append("parent_thread_id = ?")
            params.append(parent_thread_id)
        if workspace_ref is not None:
            where.append("workspace_ref = ?")
            params.append(workspace_ref)
        if status is not None:
            normalized_status = ThreadStatus(status)
            where.append("status = ?")
            params.append(normalized_status.value)
        params.append(page_limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM threads WHERE {' AND '.join(where)} ORDER BY sequence LIMIT ?",
                params,
            ).fetchall()
            return [self._thread_from_row(connection, row) for row in rows]

    def get_thread_by_legacy_ref(
        self,
        legacy_ref: ThreadLegacyRef,
    ) -> ConversationThread:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT threads.*
                FROM threads
                JOIN thread_legacy_refs ON thread_legacy_refs.thread_id = threads.id
                WHERE thread_legacy_refs.source_type = ?
                    AND thread_legacy_refs.source_id = ?
                """,
                (legacy_ref.source_type.value, legacy_ref.source_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("thread legacy mapping not found")
            return self._thread_from_row(connection, row)

    def set_thread_status(
        self,
        thread_id: str,
        status: ThreadStatus | str,
    ) -> ConversationThread:
        normalized_status = ThreadStatus(status)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM threads WHERE id = ?",
                (thread_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"thread not found: {thread_id}")
            current = self._thread_from_row(connection, row)
            if current.status is normalized_status:
                return current
            allowed = (
                current.status is ThreadStatus.ACTIVE
                and normalized_status
                in {ThreadStatus.COMPLETED, ThreadStatus.CANCELLED, ThreadStatus.ARCHIVED}
            ) or (
                current.status in {ThreadStatus.COMPLETED, ThreadStatus.CANCELLED}
                and normalized_status is ThreadStatus.ARCHIVED
            )
            if not allowed:
                raise ConflictError("invalid thread status transition")
            updated_at = utc_now()
            updated = ConversationThread.model_validate(
                {
                    **current.model_dump(),
                    "status": normalized_status,
                    "updated_at": updated_at,
                    "archived_at": (
                        updated_at if normalized_status is ThreadStatus.ARCHIVED else None
                    ),
                }
            )
            try:
                body = updated.model_dump_json()
                thread_columns = {
                    str(item["name"])
                    for item in connection.execute('PRAGMA table_info("threads")').fetchall()
                }
                if "body_hash" in thread_columns:
                    connection.execute(
                        """
                        UPDATE threads
                        SET status = ?, body = ?, body_hash = ?, updated_at = ?, archived_at = ?
                        WHERE id = ?
                        """,
                        (
                            updated.status.value,
                            body,
                            hashlib.sha256(body.encode("utf-8")).hexdigest(),
                            updated.updated_at.isoformat(),
                            (
                                updated.archived_at.isoformat()
                                if updated.archived_at is not None
                                else None
                            ),
                            thread_id,
                        ),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE threads
                        SET status = ?, body = ?, updated_at = ?, archived_at = ?
                        WHERE id = ?
                        """,
                        (
                            updated.status.value,
                            body,
                            updated.updated_at.isoformat(),
                            (
                                updated.archived_at.isoformat()
                                if updated.archived_at is not None
                                else None
                            ),
                            thread_id,
                        ),
                    )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("invalid thread status transition") from exc
            return updated

    def archive_thread(self, thread_id: str) -> ConversationThread:
        return self.set_thread_status(thread_id, ThreadStatus.ARCHIVED)

    def create_turn(self, turn: Turn) -> Turn:
        if turn.cursor is not None or turn.position is not None:
            raise ValueError("a new turn cannot provide a cursor or position")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_active_thread(connection, turn.thread_id)
            position = int(
                connection.execute(
                    "SELECT COALESCE(MAX(position), 0) + 1 FROM turns WHERE thread_id = ?",
                    (turn.thread_id,),
                ).fetchone()[0]
            )
            persisted = Turn.model_validate({**turn.model_dump(), "position": position})
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO turns(id, thread_id, position, body, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        persisted.id,
                        persisted.thread_id,
                        position,
                        persisted.model_dump_json(),
                        persisted.created_at.isoformat(),
                    ),
                ).lastrowid
            except sqlite3.IntegrityError as exc:
                raise ConflictError("turn identity or position already exists") from exc
            if cursor is None:
                raise RuntimeError("turn insert did not produce a cursor")
            return Turn.model_validate({**persisted.model_dump(), "cursor": int(cursor)})

    def list_turns(
        self,
        thread_id: str,
        *,
        after_cursor: int | None = None,
        limit: int = 100,
    ) -> list[Turn]:
        cursor, page_limit = self._validate_cursor_page(after_cursor, limit)
        with self._connect() as connection:
            if (
                connection.execute("SELECT 1 FROM threads WHERE id = ?", (thread_id,)).fetchone()
                is None
            ):
                raise NotFoundError(f"thread not found: {thread_id}")
            rows = connection.execute(
                """
                SELECT * FROM turns
                WHERE thread_id = ? AND sequence > ?
                ORDER BY sequence LIMIT ?
                """,
                (thread_id, cursor, page_limit),
            ).fetchall()
        return [self._turn_from_row(row) for row in rows]

    def append_item(self, item: Item) -> Item:
        if item.cursor is not None or item.position is not None:
            raise ValueError("a new item cannot provide a cursor or position")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_active_thread(connection, item.thread_id)
            turn_row = connection.execute(
                "SELECT 1 FROM turns WHERE id = ? AND thread_id = ?",
                (item.turn_id, item.thread_id),
            ).fetchone()
            if turn_row is None:
                raise NotFoundError("turn not found in thread")
            self._validate_item_reference(connection, item)
            position = int(
                connection.execute(
                    "SELECT COALESCE(MAX(position), 0) + 1 FROM items WHERE thread_id = ?",
                    (item.thread_id,),
                ).fetchone()[0]
            )
            persisted = Item.model_validate({**item.model_dump(), "position": position})
            try:
                body = persisted.model_dump_json()
                item_columns = {
                    str(row["name"])
                    for row in connection.execute('PRAGMA table_info("items")').fetchall()
                }
                if "body_hash" in item_columns:
                    fields = (
                        "id, thread_id, turn_id, position, item_type, body, body_hash, created_at"
                    )
                    values: tuple[Any, ...]
                    values = (
                        persisted.id,
                        persisted.thread_id,
                        persisted.turn_id,
                        position,
                        persisted.item_type.value,
                        body,
                        hashlib.sha256(body.encode("utf-8")).hexdigest(),
                        persisted.created_at.isoformat(),
                    )
                else:
                    fields = "id, thread_id, turn_id, position, item_type, body, created_at"
                    values = (
                        persisted.id,
                        persisted.thread_id,
                        persisted.turn_id,
                        position,
                        persisted.item_type.value,
                        body,
                        persisted.created_at.isoformat(),
                    )
                placeholders = ", ".join("?" for _ in values)
                cursor = connection.execute(
                    f"INSERT INTO items({fields}) VALUES ({placeholders})",
                    values,
                ).lastrowid
            except sqlite3.IntegrityError as exc:
                raise ConflictError("item identity, position, or reference is invalid") from exc
            if cursor is None:
                raise RuntimeError("item insert did not produce a cursor")
            return Item.model_validate({**persisted.model_dump(), "cursor": int(cursor)})

    def get_item(self, item_id: str) -> Item:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM items WHERE id = ?",
                (item_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"item not found: {item_id}")
        return self._item_from_row(row)

    def list_items(
        self,
        thread_id: str,
        *,
        after_cursor: int | None = None,
        limit: int = 100,
        turn_id: str | None = None,
    ) -> list[Item]:
        cursor, page_limit = self._validate_cursor_page(after_cursor, limit)
        where = ["thread_id = ?", "sequence > ?"]
        params: list[Any] = [thread_id, cursor]
        if turn_id is not None:
            where.append("turn_id = ?")
            params.append(turn_id)
        params.append(page_limit)
        with self._connect() as connection:
            if (
                connection.execute("SELECT 1 FROM threads WHERE id = ?", (thread_id,)).fetchone()
                is None
            ):
                raise NotFoundError(f"thread not found: {thread_id}")
            rows = connection.execute(
                f"SELECT * FROM items WHERE {' AND '.join(where)} ORDER BY sequence LIMIT ?",
                params,
            ).fetchall()
        return [self._item_from_row(row) for row in rows]

    # Phase 1A Artifact metadata. The storage key is internal-only.

    def validate_artifact_source_refs(
        self,
        source_refs: Collection[ArtifactSourceRef],
    ) -> None:
        """Preflight source references without creating blob-store state."""

        with self._connect() as connection:
            connection.execute("BEGIN")
            for source_ref in source_refs:
                self._validate_artifact_source_ref(connection, source_ref)

    def register_artifact(
        self,
        artifact: Artifact,
        *,
        storage_key: str,
    ) -> tuple[Artifact, bool]:
        if artifact.cursor is not None:
            raise ValueError("a new artifact cannot provide a cursor")
        expected_storage_key = self._artifact_storage_key(artifact.content_hash)
        if storage_key != expected_storage_key:
            raise ValueError("artifact storage key does not match its content hash")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for source_ref in artifact.source_refs:
                self._validate_artifact_source_ref(connection, source_ref)
            existing_row = connection.execute(
                "SELECT * FROM artifacts WHERE content_hash = ?",
                (artifact.content_hash,),
            ).fetchone()
            if existing_row is not None:
                existing = self._artifact_from_row(connection, existing_row)
                if not self._artifact_metadata_matches(existing, artifact):
                    raise ConflictError("artifact content hash already has different metadata")
                blob_row = connection.execute(
                    "SELECT storage_key, size_bytes FROM artifact_blobs WHERE content_hash = ?",
                    (artifact.content_hash,),
                ).fetchone()
                if (
                    blob_row is None
                    or blob_row["storage_key"] != storage_key
                    or int(blob_row["size_bytes"]) != artifact.size_bytes
                ):
                    raise ConflictError("artifact blob metadata is inconsistent")
                self._ensure_artifact_retention_projection(connection, existing)
                return existing, False

            blob_row = connection.execute(
                "SELECT storage_key, size_bytes FROM artifact_blobs WHERE content_hash = ?",
                (artifact.content_hash,),
            ).fetchone()
            if blob_row is None:
                connection.execute(
                    """
                    INSERT INTO artifact_blobs(content_hash, storage_key, size_bytes, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        artifact.content_hash,
                        storage_key,
                        artifact.size_bytes,
                        artifact.created_at.isoformat(),
                    ),
                )
            elif (
                blob_row["storage_key"] != storage_key
                or int(blob_row["size_bytes"]) != artifact.size_bytes
            ):
                raise ConflictError("artifact blob metadata is inconsistent")
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO artifacts(
                        id, content_hash, media_type, size_bytes, sensitivity,
                        retention_policy_ref, body, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact.id,
                        artifact.content_hash,
                        artifact.media_type,
                        artifact.size_bytes,
                        artifact.sensitivity.value,
                        artifact.retention_policy_ref,
                        artifact.model_dump_json(),
                        artifact.created_at.isoformat(),
                    ),
                ).lastrowid
                if cursor is None:
                    raise RuntimeError("artifact insert did not produce a cursor")
                for ordinal, source_ref in enumerate(artifact.source_refs, start=1):
                    connection.execute(
                        """
                        INSERT INTO artifact_source_refs(
                            artifact_id, ordinal, source_type, source_id
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            artifact.id,
                            ordinal,
                            source_ref.source_type.value,
                            source_ref.source_id,
                        ),
                    )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("artifact identity or metadata already exists") from exc
            row = connection.execute(
                "SELECT * FROM artifacts WHERE id = ?",
                (artifact.id,),
            ).fetchone()
            assert row is not None
            registered = self._artifact_from_row(connection, row)
            self._ensure_artifact_retention_projection(connection, registered)
            return registered, True

    def get_artifact(self, artifact_id: str) -> Artifact:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE id = ?",
                (artifact_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"artifact not found: {artifact_id}")
            return self._artifact_from_row(connection, row)

    def get_artifact_by_hash(self, content_hash: str) -> Artifact:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE content_hash = ?",
                (content_hash,),
            ).fetchone()
            if row is None:
                raise NotFoundError("artifact not found by content hash")
            return self._artifact_from_row(connection, row)

    def list_artifacts(
        self,
        *,
        after_cursor: int | None = None,
        limit: int = 100,
        sensitivity: str | None = None,
    ) -> list[Artifact]:
        cursor, page_limit = self._validate_cursor_page(after_cursor, limit)
        where = ["sequence > ?"]
        params: list[Any] = [cursor]
        if sensitivity is not None:
            where.append("sensitivity = ?")
            params.append(sensitivity)
        params.append(page_limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM artifacts WHERE {' AND '.join(where)} ORDER BY sequence LIMIT ?",
                params,
            ).fetchall()
            return [self._artifact_from_row(connection, row) for row in rows]

    def get_artifact_blob_record(self, content_hash: str) -> dict[str, str | int]:
        """Return physical metadata for trusted internal storage adapters only."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT storage_key, size_bytes
                FROM artifact_blobs WHERE content_hash = ?
                """,
                (content_hash,),
            ).fetchone()
        if row is None:
            raise NotFoundError("artifact blob metadata not found")
        return {"storage_key": str(row["storage_key"]), "size_bytes": int(row["size_bytes"])}

    @staticmethod
    def _ensure_artifact_retention_projection(
        connection: sqlite3.Connection,
        artifact: Artifact,
    ) -> None:
        if (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'artifact_retention_states'"
            ).fetchone()
            is None
        ):
            return
        connection.execute(
            """
            INSERT OR IGNORE INTO retention_policies(
                id, object_type, grace_period_seconds, allow_physical_delete, created_at
            ) VALUES (?, 'artifact', 86400, 0, ?)
            """,
            (artifact.retention_policy_ref, artifact.created_at.isoformat()),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO artifact_retention_states(
                artifact_id, policy_ref, lifecycle, pinned, scheduled_deletion_at,
                trashed_at, deleted_at, updated_at
            ) VALUES (?, ?, 'active', 0, NULL, NULL, NULL, ?)
            """,
            (
                artifact.id,
                artifact.retention_policy_ref,
                artifact.created_at.isoformat(),
            ),
        )

    def create_retention_policy(self, policy: RetentionPolicy) -> RetentionPolicy:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM retention_policies WHERE id = ?", (policy.id,)
            ).fetchone()
            if existing is not None:
                current = self._retention_policy_from_row(existing)
                if current != policy:
                    raise ConflictError("retention policy already exists with another contract")
                return current
            try:
                connection.execute(
                    """
                    INSERT INTO retention_policies(
                        id, object_type, grace_period_seconds,
                        allow_physical_delete, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        policy.id,
                        policy.object_type,
                        policy.grace_period_seconds,
                        int(policy.allow_physical_delete),
                        policy.created_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("retention policy is invalid") from exc
            return policy

    def get_retention_policy(self, policy_id: str) -> RetentionPolicy:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM retention_policies WHERE id = ?", (policy_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"retention policy not found: {policy_id}")
        return self._retention_policy_from_row(row)

    def get_artifact_retention_state(self, artifact_id: str) -> ArtifactRetentionState:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM artifact_retention_states WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
            if row is None:
                if (
                    connection.execute(
                        "SELECT 1 FROM artifacts WHERE id = ?", (artifact_id,)
                    ).fetchone()
                    is None
                ):
                    raise NotFoundError(f"artifact not found: {artifact_id}")
                raise ConflictError("Artifact retention projection is missing")
        return self._artifact_retention_state_from_row(row)

    def update_artifact_retention_state(
        self,
        state: ArtifactRetentionState,
        *,
        expected_updated_at: datetime,
        event_type: str,
        action_hash: str,
        outcome: str = "completed",
    ) -> ArtifactRetentionState:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE artifact_retention_states SET
                    lifecycle = ?, pinned = ?, scheduled_deletion_at = ?,
                    trashed_at = ?, deleted_at = ?, updated_at = ?
                WHERE artifact_id = ? AND updated_at = ? AND policy_ref = ?
                """,
                (
                    state.lifecycle.value,
                    int(state.pinned),
                    None
                    if state.scheduled_deletion_at is None
                    else state.scheduled_deletion_at.isoformat(),
                    None if state.trashed_at is None else state.trashed_at.isoformat(),
                    None if state.deleted_at is None else state.deleted_at.isoformat(),
                    state.updated_at.isoformat(),
                    state.artifact_id,
                    expected_updated_at.isoformat(),
                    state.policy_ref,
                ),
            ).rowcount
            if updated != 1:
                raise ConflictError("Artifact retention state changed concurrently")
            artifact = connection.execute(
                "SELECT content_hash FROM artifacts WHERE id = ?", (state.artifact_id,)
            ).fetchone()
            assert artifact is not None
            self._append_artifact_audit_event_on_connection(
                connection,
                artifact_id=state.artifact_id,
                event_type=event_type,
                content_hash=str(artifact["content_hash"]),
                action_hash=action_hash,
                outcome=outcome,
            )
            row = connection.execute(
                "SELECT * FROM artifact_retention_states WHERE artifact_id = ?",
                (state.artifact_id,),
            ).fetchone()
            assert row is not None
            return self._artifact_retention_state_from_row(row)

    def artifact_deletion_blockers(self, artifact_id: str) -> tuple[str, ...]:
        """Return conservative normalized references; unknown evidence fails closed."""

        with self._connect() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM artifacts WHERE id = ?", (artifact_id,)
                ).fetchone()
                is None
            ):
                raise NotFoundError(f"artifact not found: {artifact_id}")
            blockers: list[str] = []
            if (
                connection.execute(
                    "SELECT 1 FROM artifact_source_refs WHERE artifact_id = ? LIMIT 1",
                    (artifact_id,),
                ).fetchone()
                is not None
            ):
                blockers.append("source_reference")
            if (
                connection.execute(
                    """
                SELECT 1 FROM items
                WHERE item_type IN ('artifact_ref', 'tool_result_ref')
                    AND json_extract(body, '$.payload.artifact_id') = ? LIMIT 1
                """,
                    (artifact_id,),
                ).fetchone()
                is not None
            ):
                blockers.append("canonical_history")
            if (
                connection.execute(
                    """
                SELECT 1 FROM context_revisions
                WHERE EXISTS (
                    SELECT 1 FROM json_each(artifact_refs_json) WHERE value = ?
                ) OR EXISTS (
                    SELECT 1 FROM json_each(tool_result_stubs_json)
                    WHERE json_extract(value, '$.artifact_id') = ?
                ) LIMIT 1
                """,
                    (artifact_id, artifact_id),
                ).fetchone()
                is not None
            ):
                blockers.append("context_revision")
            if (
                connection.execute(
                    """
                SELECT 1 FROM compactions, json_tree(compactions.summary_json)
                WHERE json_tree.type = 'text' AND json_tree.value = ? LIMIT 1
                """,
                    (artifact_id,),
                ).fetchone()
                is not None
            ):
                blockers.append("compaction")
            if (
                connection.execute(
                    """
                SELECT 1 FROM command_executions
                WHERE status IN ('in_progress', 'manual_reconcile_required')
                    AND (
                        resource_id = ?
                        OR instr(COALESCE(response_json, ''), ?) > 0
                    )
                LIMIT 1
                """,
                    (artifact_id, artifact_id),
                ).fetchone()
                is not None
            ):
                blockers.append("unknown_command_outcome")
            if (
                connection.execute(
                    """
                SELECT 1 FROM tool_action_receipts
                WHERE status IN ('in_progress', 'outcome_unknown')
                    AND instr(COALESCE(result_json, ''), ?) > 0
                LIMIT 1
                """,
                    (artifact_id,),
                ).fetchone()
                is not None
            ):
                blockers.append("unknown_tool_action_outcome")
            conservative_scans = (
                (
                    "approval",
                    "approval_requests",
                    ("detail_summary", "tool_call_id"),
                ),
                ("approval_audit", "approval_audit_events", ("body",)),
                ("session_event", "events", ("body",)),
                ("session", "sessions", ("body",)),
                ("agent", "agents", ("body",)),
                ("workflow_run", "workflow_runs", ("body",)),
                ("workflow_event", "workflow_run_events", ("body",)),
                ("evaluation_suite", "evaluation_suites", ("body",)),
                ("evaluation_run", "evaluation_runs", ("body",)),
                ("evaluation_result", "evaluation_results", ("body",)),
                ("evaluation_event", "evaluation_run_events", ("body",)),
                ("memory", "memory_versions", ("body", "content")),
            )
            try:
                for blocker_name, table, columns in conservative_scans:
                    predicate = " OR ".join(
                        f"instr(COALESCE({column}, ''), ?) > 0" for column in columns
                    )
                    if (
                        connection.execute(
                            f'SELECT 1 FROM "{table}" WHERE {predicate} LIMIT 1',
                            tuple(artifact_id for _column in columns),
                        ).fetchone()
                        is not None
                    ):
                        blockers.append(blocker_name)
            except sqlite3.DatabaseError:
                blockers.append("reference_scan_failed")
            blob_row = connection.execute(
                """
                SELECT content_hash FROM artifacts WHERE id = ?
                """,
                (artifact_id,),
            ).fetchone()
            assert blob_row is not None
            shared_count = connection.execute(
                "SELECT COUNT(*) AS count FROM artifacts WHERE content_hash = ?",
                (blob_row["content_hash"],),
            ).fetchone()
            if shared_count is None or int(shared_count["count"]) != 1:
                blockers.append("shared_or_unknown_blob_reference")
            return tuple(blockers)

    def append_artifact_repair_event(
        self,
        result: ArtifactRepairResult,
        *,
        content_hash: str,
        action_hash: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            event_type = f"artifact.repair.{result.outcome}"
            self._append_artifact_audit_event_on_connection(
                connection,
                artifact_id=None,
                event_type=event_type,
                content_hash=content_hash,
                finding_hash=result.finding_hash,
                action_hash=action_hash,
                outcome=result.outcome,
            )

    @staticmethod
    def _append_artifact_audit_event_on_connection(
        connection: sqlite3.Connection,
        *,
        artifact_id: str | None,
        event_type: str,
        outcome: str,
        content_hash: str | None = None,
        finding_hash: str | None = None,
        action_hash: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO artifact_retention_audit_events(
                id, artifact_id, event_type, content_hash, finding_hash,
                action_hash, outcome, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("artifact_audit_event"),
                artifact_id,
                event_type,
                content_hash,
                finding_hash,
                action_hash,
                outcome,
                utc_now().isoformat(),
            ),
        )

    def list_artifact_blob_references(self) -> dict[str, tuple[str, int, str]]:
        """Return hash -> (artifact id, size, lifecycle) for read-only audit."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT a.content_hash, a.id, a.size_bytes, s.lifecycle
                FROM artifacts AS a
                JOIN artifact_retention_states AS s ON s.artifact_id = a.id
                """
            ).fetchall()
        return {
            str(row["content_hash"]): (
                str(row["id"]),
                int(row["size_bytes"]),
                str(row["lifecycle"]),
            )
            for row in rows
        }

    def add_cache_observation(self, observation: CacheObservation) -> CacheObservation:
        if observation.cursor is not None:
            raise ValueError("a new CacheObservation cannot provide a cursor")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if (
                observation.context_revision_id is not None
                and connection.execute(
                    "SELECT 1 FROM context_revisions WHERE id = ?",
                    (observation.context_revision_id,),
                ).fetchone()
                is None
            ):
                raise NotFoundError("CacheObservation ContextRevision not found")
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO cache_observations(
                        id, provider, model, request_id, context_revision_id,
                        cache_scope, breakpoint_id, hit_status, prompt_tokens,
                        completion_tokens, cache_read_tokens, cache_write_tokens,
                        cache_key_hash, stable_prefix_hash, invalidation_reason, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        observation.id,
                        observation.provider,
                        observation.model,
                        observation.request_id,
                        observation.context_revision_id,
                        observation.cache_scope,
                        observation.breakpoint_id,
                        observation.hit_status.value,
                        observation.prompt_tokens,
                        observation.completion_tokens,
                        observation.cache_read_tokens,
                        observation.cache_write_tokens,
                        observation.cache_key_hash,
                        observation.stable_prefix_hash,
                        observation.invalidation_reason,
                        observation.created_at.isoformat(),
                    ),
                ).lastrowid
            except sqlite3.IntegrityError as exc:
                raise ConflictError("CacheObservation is invalid or already exists") from exc
            assert cursor is not None
            return observation.model_copy(update={"cursor": int(cursor)})

    def list_cache_observations(
        self,
        *,
        context_revision_id: str | None = None,
        after_cursor: int | None = None,
        limit: int = 100,
    ) -> list[CacheObservation]:
        cursor, page_limit = self._validate_cursor_page(after_cursor, limit)
        where = ["sequence > ?"]
        params: list[Any] = [cursor]
        if context_revision_id is not None:
            where.append("context_revision_id = ?")
            params.append(context_revision_id)
        params.append(page_limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM cache_observations WHERE {' AND '.join(where)} "
                "ORDER BY sequence LIMIT ?",
                params,
            ).fetchall()
        return [self._cache_observation_from_row(row) for row in rows]

    @staticmethod
    def _retention_policy_from_row(row: sqlite3.Row) -> RetentionPolicy:
        return RetentionPolicy(
            id=row["id"],
            object_type=row["object_type"],
            grace_period_seconds=int(row["grace_period_seconds"]),
            allow_physical_delete=bool(row["allow_physical_delete"]),
            created_at=row["created_at"],
        )

    @staticmethod
    def _artifact_retention_state_from_row(row: sqlite3.Row) -> ArtifactRetentionState:
        return ArtifactRetentionState(
            artifact_id=row["artifact_id"],
            policy_ref=row["policy_ref"],
            lifecycle=row["lifecycle"],
            pinned=bool(row["pinned"]),
            scheduled_deletion_at=row["scheduled_deletion_at"],
            trashed_at=row["trashed_at"],
            deleted_at=row["deleted_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _cache_observation_from_row(row: sqlite3.Row) -> CacheObservation:
        return CacheObservation(
            id=row["id"],
            cursor=int(row["sequence"]),
            provider=row["provider"],
            model=row["model"],
            request_id=row["request_id"],
            context_revision_id=row["context_revision_id"],
            cache_scope=row["cache_scope"],
            breakpoint_id=row["breakpoint_id"],
            hit_status=row["hit_status"],
            prompt_tokens=row["prompt_tokens"],
            completion_tokens=row["completion_tokens"],
            cache_read_tokens=row["cache_read_tokens"],
            cache_write_tokens=row["cache_write_tokens"],
            cache_key_hash=row["cache_key_hash"],
            stable_prefix_hash=row["stable_prefix_hash"],
            invalidation_reason=row["invalidation_reason"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _validate_cursor_page(after_cursor: int | None, limit: int) -> tuple[int, int]:
        if isinstance(limit, bool) or limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        if after_cursor is None:
            return 0, limit
        if isinstance(after_cursor, bool) or after_cursor < 0:
            raise ValueError("after_cursor must be a non-negative integer")
        return after_cursor, limit

    @staticmethod
    def _artifact_storage_key(content_hash: str) -> str:
        if re.fullmatch(r"[0-9a-f]{64}", content_hash) is None:
            raise ValueError("invalid artifact content hash")
        return f"sha256/{content_hash[:2]}/{content_hash[2:4]}/{content_hash}"

    @staticmethod
    def _assert_active_thread(connection: sqlite3.Connection, thread_id: str) -> None:
        row = connection.execute(
            "SELECT status FROM threads WHERE id = ?",
            (thread_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"thread not found: {thread_id}")
        if row["status"] != ThreadStatus.ACTIVE.value:
            raise ConflictError("thread is not active")

    @staticmethod
    def _validate_legacy_ref(
        connection: sqlite3.Connection,
        legacy_ref: ThreadLegacyRef,
    ) -> None:
        table = {
            "session": "sessions",
            "workflow_run": "workflow_runs",
        }[legacy_ref.source_type.value]
        row = connection.execute(
            f'SELECT 1 FROM "{table}" WHERE id = ?',
            (legacy_ref.source_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("legacy source not found")

    @staticmethod
    def _validate_item_reference(connection: sqlite3.Connection, item: Item) -> None:
        payload = item.payload
        if isinstance(payload, (ArtifactRefPayload, ToolResultRefPayload)):
            artifact_exists = connection.execute(
                "SELECT 1 FROM artifacts WHERE id = ?", (payload.artifact_id,)
            ).fetchone()
            if artifact_exists is None:
                raise NotFoundError(f"artifact not found: {payload.artifact_id}")
        if isinstance(payload, ToolCallPayload):
            duplicate = connection.execute(
                """
                SELECT 1 FROM items
                WHERE thread_id = ? AND item_type = 'tool_call'
                    AND json_extract(body, '$.payload.tool_call_id') = ?
                """,
                (item.thread_id, payload.tool_call_id),
            ).fetchone()
            if duplicate is not None:
                raise ConflictError("tool call already exists in thread")
        elif isinstance(payload, ToolResultRefPayload):
            tool_call_exists = connection.execute(
                """
                SELECT 1 FROM items
                WHERE thread_id = ? AND item_type = 'tool_call'
                    AND json_extract(body, '$.payload.tool_call_id') = ?
                """,
                (item.thread_id, payload.tool_call_id),
            ).fetchone()
            if tool_call_exists is None:
                raise NotFoundError("tool call reference not found in thread")
        elif isinstance(payload, ApprovalLinkPayload):
            approval_exists = connection.execute(
                "SELECT 1 FROM approval_requests WHERE id = ?", (payload.approval_id,)
            ).fetchone()
            if approval_exists is None:
                raise NotFoundError(f"approval not found: {payload.approval_id}")

    @staticmethod
    def _validate_artifact_source_ref(
        connection: sqlite3.Connection,
        source_ref: ArtifactSourceRef,
    ) -> None:
        table = {
            ArtifactSourceType.THREAD: "threads",
            ArtifactSourceType.TURN: "turns",
            ArtifactSourceType.ITEM: "items",
            ArtifactSourceType.SESSION: "sessions",
            ArtifactSourceType.AGENT: "agents",
            ArtifactSourceType.WORKFLOW_RUN: "workflow_runs",
            ArtifactSourceType.TOOL_ACTION_RECEIPT: "tool_action_receipts",
            ArtifactSourceType.APPROVAL: "approval_requests",
            ArtifactSourceType.EVALUATION_RUN: "evaluation_runs",
        }.get(source_ref.source_type)
        if table is not None:
            row = connection.execute(
                f'SELECT 1 FROM "{table}" WHERE id = ?',
                (source_ref.source_id,),
            ).fetchone()
        else:
            # Tool calls have no legacy first-class table. Require a durable
            # canonical Item, Approval, or Action receipt carrying that ID.
            row = connection.execute(
                """
                SELECT 1 FROM items
                WHERE item_type = 'tool_call'
                    AND json_extract(body, '$.payload.tool_call_id') = ?
                UNION ALL
                SELECT 1 FROM approval_requests WHERE tool_call_id = ?
                UNION ALL
                SELECT 1 FROM tool_action_receipts WHERE idempotency_key = ?
                LIMIT 1
                """,
                (source_ref.source_id, source_ref.source_id, source_ref.source_id),
            ).fetchone()
        if row is None:
            raise NotFoundError("artifact source reference not found")

    @staticmethod
    def _thread_from_row(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> ConversationThread:
        legacy_rows = connection.execute(
            """
            SELECT source_type, source_id FROM thread_legacy_refs
            WHERE thread_id = ? ORDER BY source_type, source_id
            """,
            (row["id"],),
        ).fetchall()
        refs = tuple(
            ThreadLegacyRef(source_type=ref["source_type"], source_id=ref["source_id"])
            for ref in legacy_rows
        )
        stored = ConversationThread.model_validate_json(row["body"])
        return ConversationThread.model_validate(
            {
                **stored.model_dump(),
                "cursor": int(row["sequence"]),
                "parent_thread_id": row["parent_thread_id"],
                "workspace_ref": row["workspace_ref"],
                "status": row["status"],
                "legacy_refs": refs,
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "archived_at": row["archived_at"],
            }
        )

    @staticmethod
    def _turn_from_row(row: sqlite3.Row) -> Turn:
        stored = Turn.model_validate_json(row["body"])
        return Turn.model_validate(
            {
                **stored.model_dump(),
                "cursor": int(row["sequence"]),
                "thread_id": row["thread_id"],
                "position": int(row["position"]),
                "created_at": row["created_at"],
            }
        )

    @staticmethod
    def _item_from_row(row: sqlite3.Row) -> Item:
        stored = Item.model_validate_json(row["body"])
        return Item.model_validate(
            {
                **stored.model_dump(),
                "cursor": int(row["sequence"]),
                "thread_id": row["thread_id"],
                "turn_id": row["turn_id"],
                "position": int(row["position"]),
                "created_at": row["created_at"],
            }
        )

    @staticmethod
    def _artifact_from_row(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> Artifact:
        source_rows = connection.execute(
            """
            SELECT source_type, source_id FROM artifact_source_refs
            WHERE artifact_id = ? ORDER BY ordinal
            """,
            (row["id"],),
        ).fetchall()
        source_refs = tuple(
            ArtifactSourceRef(source_type=ref["source_type"], source_id=ref["source_id"])
            for ref in source_rows
        )
        stored = Artifact.model_validate_json(row["body"])
        return Artifact.model_validate(
            {
                **stored.model_dump(),
                "cursor": int(row["sequence"]),
                "content_hash": row["content_hash"],
                "media_type": row["media_type"],
                "size_bytes": int(row["size_bytes"]),
                "sensitivity": row["sensitivity"],
                "source_refs": source_refs,
                "retention_policy_ref": row["retention_policy_ref"],
                "created_at": row["created_at"],
            }
        )

    @staticmethod
    def _artifact_metadata_matches(existing: Artifact, requested: Artifact) -> bool:
        existing_sources = {(ref.source_type, ref.source_id) for ref in existing.source_refs}
        requested_sources = {(ref.source_type, ref.source_id) for ref in requested.source_refs}
        return (
            existing.content_hash == requested.content_hash
            and existing.media_type == requested.media_type
            and existing.size_bytes == requested.size_bytes
            and existing.sensitivity is requested.sensitivity
            and existing.retention_policy_ref == requested.retention_policy_ref
            and existing_sources == requested_sources
        )

    # Phase 1B immutable Context evidence and append-only Compaction.

    @staticmethod
    def _validate_context_revision_item_evidence(
        connection: sqlite3.Connection,
        revision: ContextRevision,
    ) -> None:
        if revision.source_cursor_start is not None:
            assert revision.source_cursor_end is not None
            for cursor in {revision.source_cursor_start, revision.source_cursor_end}:
                if (
                    connection.execute(
                        "SELECT 1 FROM items WHERE sequence = ? AND thread_id = ?",
                        (cursor, revision.thread_id),
                    ).fetchone()
                    is None
                ):
                    raise ConflictError("ContextRevision Item cursor coverage is invalid")
        for item_id in revision.source_item_ids:
            if (
                connection.execute(
                    "SELECT 1 FROM items WHERE id = ? AND thread_id = ?",
                    (item_id, revision.thread_id),
                ).fetchone()
                is None
            ):
                raise ConflictError("ContextRevision source Item is outside its Thread")

    @staticmethod
    def _validate_memory_source_ref(
        connection: sqlite3.Connection,
        revision: ContextRevision,
        source: ContextSourceRef,
        *,
        current_sources: bool,
    ) -> None:
        """Validate one versioned Memory against the frozen run scope.

        A write may only reference the active head.  A historical read uses
        the exact version and hash captured by the revision, so a later head
        update or deactivation cannot invalidate an already persisted prompt.
        Scope checks remain fail-closed in both cases.
        """

        if source.cursor is None:
            raise ConflictError("Prompt Block Memory source is invalid: version is required")
        row = connection.execute(
            """
            SELECT m.current_version, mv.body, mv.version, mv.status,
                   s.body AS session_body
            FROM memories AS m
            JOIN memory_versions AS mv
                ON mv.memory_id = m.id AND mv.version = ?
            JOIN sessions AS s ON s.id = ?
            WHERE m.id = ?
            """,
            (source.cursor, revision.session_id, source.source_id),
        ).fetchone()
        if row is None:
            raise ConflictError("Prompt Block Memory source version is invalid")
        if current_sources and (
            int(row["current_version"]) != int(row["version"])
            or row["status"] != MemoryStatus.ACTIVE.value
        ):
            raise ConflictError("Prompt Block Memory source is not the active head")
        memory = Memory.model_validate_json(row["body"])
        if (
            memory.id != source.source_id
            or memory.version != int(source.cursor)
            or hashlib.sha256(str(row["body"]).encode("utf-8")).hexdigest() != source.content_hash
        ):
            raise ConflictError("Prompt Block Memory source hash is invalid")
        try:
            session = Session.model_validate_json(row["session_body"])
            allowed = parse_memory_scope(session.role_snapshot.memory_scope).can_read(memory.kind)
        except (TypeError, ValueError) as exc:
            raise ConflictError("Prompt Block Memory scope snapshot is invalid") from exc
        if not allowed:
            raise ConflictError("Prompt Block Memory scope is not allowed for this role")
        if memory.kind is MemoryKind.WORKING and memory.source_session_id != revision.session_id:
            raise ConflictError("Prompt Block Working Memory belongs to another Session")
        if memory.kind is MemoryKind.PROJECT and (
            revision.workspace_ref is None or memory.project_scope != revision.workspace_ref
        ):
            raise ConflictError("Prompt Block Project Memory belongs to another Workspace")
        if memory.role_scope and not (
            set(memory.role_scope).intersection(
                {session.role_snapshot.role_id, session.role_snapshot.role_name, "*"}
            )
        ):
            raise ConflictError("Prompt Block Memory role scope does not include this role")

    @staticmethod
    def _context_memory_source_refs(revision: ContextRevision) -> tuple[ContextSourceRef, ...]:
        return tuple(
            source
            for block in revision.blocks
            for source in block.source_refs
            if source.source_type is ContextSourceType.MEMORY
        )

    @staticmethod
    def _validate_context_reference_bindings(
        connection: sqlite3.Connection,
        revision: ContextRevision,
        *,
        current_sources: bool,
    ) -> None:
        """Bind Memory references to the exact PromptBlock source snapshot."""

        memory_sources = SQLiteStore._context_memory_source_refs(revision)
        expected_memory_refs = tuple(dict.fromkeys(source.source_id for source in memory_sources))
        if revision.memory_refs != expected_memory_refs:
            raise ConflictError(
                "ContextRevision memory_refs do not match Prompt Block Memory evidence"
            )
        for binding in revision.reference_bindings:
            if binding.ref_type is not ContextReferenceType.MEMORY:
                continue
            matching_sources = tuple(
                source
                for source in memory_sources
                if source.source_id == binding.resolved_target
                and source.content_hash == binding.source_snapshot_hash
            )
            if len(matching_sources) != 1:
                raise ConflictError("Reference Binding Memory source is invalid")
            SQLiteStore._validate_memory_source_ref(
                connection,
                revision,
                matching_sources[0],
                current_sources=current_sources,
            )

    @staticmethod
    def _validate_context_source_refs(
        connection: sqlite3.Connection,
        revision: ContextRevision,
        *,
        pending_compaction: Compaction | None = None,
        current_sources: bool = True,
    ) -> None:
        for block in revision.blocks:
            if not block.source_refs:
                raise ConflictError("Prompt Block requires typed source evidence")
            for source in block.source_refs:
                if source.source_type is ContextSourceType.SESSION:
                    if (
                        source.source_id != revision.session_id
                        or source.cursor is not None
                        or source.content_hash != block.content_hash
                    ):
                        raise ConflictError("Prompt Block Session source is invalid")
                elif source.source_type is ContextSourceType.AGENT:
                    if (
                        source.source_id != revision.agent_id
                        or source.cursor is not None
                        or source.content_hash != block.content_hash
                    ):
                        raise ConflictError("Prompt Block Agent source is invalid")
                elif source.source_type is ContextSourceType.TOOL_SCHEMA:
                    tools_json = json.dumps(
                        [tool.model_dump(mode="json") for tool in revision.tools],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    if (
                        source.source_id != revision.session_id
                        or source.cursor is not None
                        or source.content_hash
                        != hashlib.sha256(tools_json.encode("utf-8")).hexdigest()
                    ):
                        raise ConflictError("Prompt Block Tool Schema source is invalid")
                elif source.source_type is ContextSourceType.ITEM:
                    row = connection.execute(
                        "SELECT * FROM items WHERE id = ? AND sequence = ? AND thread_id = ?",
                        (source.source_id, source.cursor, revision.thread_id),
                    ).fetchone()
                    if row is None:
                        raise ConflictError("Prompt Block Item source is invalid")
                    if (
                        hashlib.sha256(str(row["body"]).encode("utf-8")).hexdigest()
                        != source.content_hash
                    ):
                        raise ConflictError("Prompt Block Item source hash is invalid")
                elif source.source_type is ContextSourceType.THREAD:
                    row = connection.execute(
                        "SELECT body, workspace_ref FROM threads WHERE id = ? AND sequence = ?",
                        (source.source_id, source.cursor),
                    ).fetchone()
                    if row is None or source.source_id != revision.thread_id:
                        raise ConflictError("Prompt Block Thread source is invalid")
                    if revision.workspace_ref is not None and (
                        row["workspace_ref"] != revision.workspace_ref
                    ):
                        raise ConflictError("Prompt Block Thread workspace scope is invalid")
                    if current_sources and (
                        hashlib.sha256(str(row["body"]).encode("utf-8")).hexdigest()
                        != source.content_hash
                    ):
                        raise ConflictError("Prompt Block Thread source hash is invalid")
                elif source.source_type is ContextSourceType.ARTIFACT:
                    if (
                        connection.execute(
                            """
                            SELECT 1 FROM artifacts
                            WHERE id = ? AND sequence = ? AND content_hash = ?
                                AND sensitivity != 'restricted'
                            """,
                            (source.source_id, source.cursor, source.content_hash),
                        ).fetchone()
                        is None
                    ):
                        raise ConflictError("Prompt Block Artifact source is invalid")
                elif source.source_type is ContextSourceType.MEMORY:
                    SQLiteStore._validate_memory_source_ref(
                        connection,
                        revision,
                        source,
                        current_sources=current_sources,
                    )
                elif source.source_type is ContextSourceType.COMPACTION:
                    valid_pending = (
                        pending_compaction is not None
                        and pending_compaction.id == source.source_id
                        and pending_compaction.id == revision.compaction_id
                        and pending_compaction.session_id == revision.session_id
                        and pending_compaction.thread_id == revision.thread_id
                        and pending_compaction.content_hash == source.content_hash
                        and (
                            pending_compaction.source_type is CompactionSourceType.THREAD_ITEMS
                            or pending_compaction.agent_id == revision.agent_id
                        )
                    )
                    valid_persisted = connection.execute(
                        """
                        SELECT 1 FROM compactions
                        WHERE id = ? AND id = ? AND session_id = ?
                            AND thread_id IS ? AND content_hash = ?
                            AND (source_type = 'thread_items' OR agent_id = ?)
                        """,
                        (
                            source.source_id,
                            revision.compaction_id,
                            revision.session_id,
                            revision.thread_id,
                            source.content_hash,
                            revision.agent_id,
                        ),
                    ).fetchone()
                    if source.cursor is not None or not (valid_pending or valid_persisted):
                        raise ConflictError("Prompt Block Compaction source is invalid")
                else:
                    raise ConflictError("Prompt Block source type is invalid")

    @staticmethod
    def _validate_thread_item_compaction_evidence(
        connection: sqlite3.Connection,
        compaction: Compaction,
    ) -> None:
        if not compaction.covered_item_refs:
            raise ConflictError("Thread Item Compaction requires exact Item evidence")
        if any(
            source.source_type is not ContextSourceType.ITEM or source.cursor is None
            for source in compaction.covered_item_refs
        ):
            raise ConflictError("Thread Item Compaction evidence must use Item cursors")
        item_ids = [source.source_id for source in compaction.covered_item_refs]
        item_cursors = [source.cursor for source in compaction.covered_item_refs]
        if len(item_ids) != len(set(item_ids)) or any(
            current is None or previous is None or current <= previous
            for previous, current in zip(item_cursors, item_cursors[1:], strict=False)
        ):
            raise ConflictError(
                "Thread Item Compaction evidence must be unique and strictly ordered"
            )
        if (
            item_cursors[0] != compaction.source_cursor_start
            or item_cursors[-1] != compaction.source_cursor_end
        ):
            raise ConflictError("Thread Item Compaction range does not match its evidence")
        digest = hashlib.sha256()
        for source in compaction.covered_item_refs:
            row = connection.execute(
                "SELECT * FROM items WHERE id = ? AND sequence = ? AND thread_id = ?",
                (source.source_id, source.cursor, compaction.thread_id),
            ).fetchone()
            if row is None:
                raise ConflictError("Thread Item Compaction evidence is outside its Thread")
            item_hash = hashlib.sha256(str(row["body"]).encode("utf-8")).hexdigest()
            if item_hash != source.content_hash:
                raise ConflictError("Thread Item Compaction evidence hash is invalid")
            digest.update(
                json.dumps(
                    {
                        "id": source.source_id,
                        "cursor": source.cursor,
                        "content_hash": source.content_hash,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            digest.update(b"\n")
        if digest.hexdigest() != compaction.source_snapshot_hash:
            raise ConflictError("Thread Item Compaction snapshot hash is invalid")

    @staticmethod
    def _validate_persisted_compaction_evidence(
        connection: sqlite3.Connection,
        compaction: Compaction,
    ) -> None:
        if compaction.source_type is CompactionSourceType.THREAD_ITEMS:
            SQLiteStore._validate_thread_item_compaction_evidence(connection, compaction)
            return
        if compaction.covered_item_refs:
            raise ConflictError("ContextRevision Compaction cannot claim Item evidence")
        if compaction_coverage_hash(compaction.summary) != compaction.source_snapshot_hash:
            raise ConflictError("Compaction source hash does not match its coverage evidence")
        for cursor in {compaction.source_cursor_start, compaction.source_cursor_end}:
            if (
                connection.execute(
                    """
                    SELECT 1 FROM context_revisions
                    WHERE sequence = ? AND session_id = ? AND agent_id = ?
                        AND thread_id IS ?
                    """,
                    (
                        cursor,
                        compaction.session_id,
                        compaction.agent_id,
                        compaction.thread_id,
                    ),
                ).fetchone()
                is None
            ):
                raise ConflictError("Compaction ContextRevision cursor coverage is invalid")

    def append_context_revision(
        self,
        revision: ContextRevision,
        *,
        compaction: Compaction | None = None,
    ) -> ContextRevision:
        if revision.cursor is not None:
            raise ValueError("a new ContextRevision cannot provide a cursor")
        frozen_layout = PromptLayout()
        if revision.prompt_layout != frozen_layout:
            raise ValueError("Phase 1B only accepts the frozen PromptLayout")
        actual_block_types = tuple(block.block_type for block in revision.blocks)
        expected_block_types = tuple(
            block_type
            for block_type in frozen_layout.block_order
            if block_type in actual_block_types
        )
        if (
            len(actual_block_types) != len(set(actual_block_types))
            or actual_block_types != expected_block_types
        ):
            raise ValueError("Prompt blocks do not follow the frozen PromptLayout")
        if not {
            PromptBlockType.ROLE_INSTRUCTIONS,
            PromptBlockType.TOOL_SCHEMA,
            PromptBlockType.CONVERSATION,
        }.issubset(actual_block_types):
            raise ValueError("ContextRevision is missing required base Prompt Blocks")
        expected_snapshots: list[ContextSourceRef] = []
        seen_snapshots: set[tuple[ContextSourceType, str, int | None, str]] = set()
        for block in revision.blocks:
            for source in block.source_refs:
                key = (source.source_type, source.source_id, source.cursor, source.content_hash)
                if key in seen_snapshots:
                    continue
                seen_snapshots.add(key)
                expected_snapshots.append(source)
        # ``model_copy(update={"blocks": ...})`` is intentionally supported
        # by callers constructing a candidate revision.  Rebind this
        # denormalized field from the candidate blocks before Store validation;
        # the persisted row still gets an immutable snapshot that readback can
        # compare against the stored Prompt Blocks.
        if revision.source_snapshots != tuple(expected_snapshots):
            revision = revision.model_copy(update={"source_snapshots": tuple(expected_snapshots)})
        compaction_blocks = [
            block for block in revision.blocks if block.block_type is PromptBlockType.COMPACTION
        ]
        compaction_sources = [
            source
            for block in revision.blocks
            for source in block.source_refs
            if source.source_type is ContextSourceType.COMPACTION
        ]
        if revision.compaction_id is None:
            if compaction_blocks or compaction_sources:
                raise ConflictError("Prompt Block Compaction source is invalid: detached evidence")
        elif (
            len(compaction_blocks) != 1
            or len(compaction_sources) != 1
            or compaction_sources[0].source_id != revision.compaction_id
        ):
            raise ConflictError("Prompt Block Compaction source is invalid: evidence is incomplete")
        expected_compaction_refs = (
            () if revision.compaction_id is None else (revision.compaction_id,)
        )
        if revision.compaction_refs != expected_compaction_refs:
            raise ConflictError("ContextRevision compaction_refs do not match compaction_id")
        if (revision.source_cursor_start is None) != (revision.source_cursor_end is None):
            raise ValueError("ContextRevision source cursor range must be complete")
        if revision.thread_id is None and (
            revision.source_cursor_start is not None
            or revision.source_cursor_end is not None
            or revision.source_item_ids
        ):
            raise ValueError("ContextRevision without a Thread cannot claim Item evidence")
        expected_cursor_namespace = (
            "items.sequence" if revision.source_cursor_start is not None else None
        )
        if revision.source_cursor_namespace != expected_cursor_namespace:
            raise ValueError("ContextRevision Item cursor namespace is invalid")
        if (revision.compaction_id is None) != (compaction is None):
            raise ValueError("ContextRevision Compaction evidence is incomplete")
        if compaction is not None:
            if compaction.cursor is not None:
                raise ValueError("a new Compaction cannot provide a cursor")
            if compaction.id != revision.compaction_id:
                raise ValueError("ContextRevision references a different Compaction")
            if (
                compaction.session_id != revision.session_id
                or compaction.thread_id != revision.thread_id
                or (
                    compaction.source_type is CompactionSourceType.CONTEXT_REVISIONS
                    and compaction.agent_id != revision.agent_id
                )
            ):
                raise ValueError("ContextRevision and Compaction scopes differ")
            summary_json = compaction.summary.model_dump_json()
            if hashlib.sha256(summary_json.encode("utf-8")).hexdigest() != compaction.content_hash:
                raise ValueError("Compaction content hash does not match its summary")
            if (
                compaction.source_type is CompactionSourceType.CONTEXT_REVISIONS
                and compaction_coverage_hash(compaction.summary) != compaction.source_snapshot_hash
            ):
                raise ValueError("Compaction source hash does not match its coverage evidence")
        for block in revision.blocks:
            if hashlib.sha256(block.content.encode("utf-8")).hexdigest() != block.content_hash:
                raise ValueError("Prompt Block content hash does not match its content")

        messages_json = json.dumps(
            [message.model_dump(mode="json") for message in revision.messages],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        tools_json = json.dumps(
            [tool.model_dump(mode="json") for tool in revision.tools],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        tool_result_stubs_json = json.dumps(
            [stub.model_dump(mode="json") for stub in revision.tool_result_stubs],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        message_ids_json = json.dumps(revision.message_ids, separators=(",", ":"))
        source_item_ids_json = json.dumps(revision.source_item_ids, separators=(",", ":"))
        artifact_refs_json = json.dumps(revision.artifact_refs, separators=(",", ":"))
        memory_refs_json = json.dumps(revision.memory_refs, separators=(",", ":"))
        compaction_refs_json = json.dumps(revision.compaction_refs, separators=(",", ":"))
        for stub in revision.tool_result_stubs:
            matching_messages = [
                message
                for message in revision.messages
                if message.role.value == "tool" and message.tool_call_id == stub.tool_call_id
            ]
            if not matching_messages or not any(
                stub.artifact_id in (message.content or "")
                and stub.content_hash in (message.content or "")
                for message in matching_messages
            ):
                raise ValueError("Tool Result Stub is detached from its Provider input message")
        watermark = revision.watermark
        if watermark.estimation_method is None:
            raise ValueError("Context token estimates require an estimation method")
        if (
            watermark.pre_compaction_token_estimate is None
            or watermark.input_token_estimate is None
        ):
            raise ValueError(
                "ContextRevision token estimates must be explicit or unknown, not zero-filled"
            )
        if watermark.tool_schema_token_estimate is None:
            raise ValueError("tool schema token estimate is required")

        with self._connect() as connection:
            self._validate_context_source_refs(
                connection,
                revision,
                pending_compaction=compaction,
            )
            self._validate_context_reference_bindings(
                connection,
                revision,
                current_sources=True,
            )

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing_row = connection.execute(
                    """
                    SELECT * FROM context_revisions
                    WHERE agent_id = ? AND request_ordinal = ?
                    """,
                    (revision.agent_id, revision.request_ordinal),
                ).fetchone()
                if existing_row is not None:
                    existing = self._context_revision_from_row(connection, existing_row)
                    existing_compaction: Compaction | None = None
                    if existing.compaction_id is not None:
                        existing_compaction_row = connection.execute(
                            "SELECT * FROM compactions WHERE id = ?",
                            (existing.compaction_id,),
                        ).fetchone()
                        assert existing_compaction_row is not None
                        existing_compaction = self._compaction_from_row(existing_compaction_row)
                    if (
                        existing.id == revision.id
                        and self._context_revision_semantic_payload(existing)
                        == self._context_revision_semantic_payload(revision)
                        and self._compaction_semantic_payload(existing_compaction)
                        == self._compaction_semantic_payload(compaction)
                    ):
                        return existing
                    raise ConflictError(
                        "ContextRevision request ordinal already has different evidence"
                    )
                self._validate_context_revision_item_evidence(connection, revision)
                self._validate_context_source_refs(
                    connection,
                    revision,
                    pending_compaction=compaction,
                )
                self._validate_context_reference_bindings(
                    connection,
                    revision,
                    current_sources=True,
                )
                if compaction is not None:
                    existing_compaction_row = connection.execute(
                        "SELECT * FROM compactions WHERE id = ?",
                        (compaction.id,),
                    ).fetchone()
                    if existing_compaction_row is not None:
                        existing_compaction = self._compaction_from_row(existing_compaction_row)
                        if self._compaction_semantic_payload(
                            existing_compaction
                        ) != self._compaction_semantic_payload(compaction):
                            raise ConflictError(
                                "Compaction identity already has different evidence"
                            )
                        # A deterministic THREAD_ITEMS Compaction may be
                        # referenced by multiple immutable ContextRevisions.
                        # Revalidate the existing row under this transaction,
                        # then reuse it instead of violating the unique ID.
                        self._validate_persisted_compaction_evidence(
                            connection,
                            existing_compaction,
                        )
                        compaction = existing_compaction
                    else:
                        covered_item_refs_json = json.dumps(
                            [ref.model_dump(mode="json") for ref in compaction.covered_item_refs],
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        if compaction.source_type is CompactionSourceType.CONTEXT_REVISIONS:
                            for source_cursor in {
                                compaction.source_cursor_start,
                                compaction.source_cursor_end,
                            }:
                                if (
                                    connection.execute(
                                        """
                                    SELECT 1 FROM context_revisions
                                    WHERE sequence = ? AND session_id = ? AND agent_id = ?
                                        AND thread_id IS ? AND request_ordinal < ?
                                    """,
                                        (
                                            source_cursor,
                                            revision.session_id,
                                            revision.agent_id,
                                            revision.thread_id,
                                            revision.request_ordinal,
                                        ),
                                    ).fetchone()
                                    is None
                                ):
                                    raise ConflictError(
                                        "Compaction ContextRevision cursor coverage is invalid"
                                    )
                        else:
                            self._validate_thread_item_compaction_evidence(connection, compaction)
                        summary_json = compaction.summary.model_dump_json()
                        compaction_cursor = connection.execute(
                            """
                            INSERT INTO compactions(
                                id, session_id, agent_id, thread_id, source_type,
                                source_cursor_start, source_cursor_end, source_snapshot_hash,
                                summary_json, content_hash, covered_item_refs_json, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                compaction.id,
                                compaction.session_id,
                                compaction.agent_id,
                                compaction.thread_id,
                                compaction.source_type.value,
                                compaction.source_cursor_start,
                                compaction.source_cursor_end,
                                compaction.source_snapshot_hash,
                                summary_json,
                                compaction.content_hash,
                                covered_item_refs_json,
                                compaction.created_at.isoformat(),
                            ),
                        ).lastrowid
                        if compaction_cursor is None:
                            raise RuntimeError("Compaction insert did not produce a cursor")

                for stub in revision.tool_result_stubs:
                    artifact_row = connection.execute(
                        """
                        SELECT id FROM artifacts
                        WHERE id = ? AND content_hash = ? AND size_bytes = ?
                            AND sensitivity = 'normal'
                        """,
                        (stub.artifact_id, stub.content_hash, stub.stored_size),
                    ).fetchone()
                    if artifact_row is None:
                        raise ConflictError(
                            "Tool Result Stub Artifact is missing, sensitive, or inconsistent"
                        )

                cursor = connection.execute(
                    """
                    INSERT INTO context_revisions(
                        id, session_id, agent_id, thread_id, workspace_ref,
                        request_ordinal, model_id,
                        prompt_layout_version, context_window, reserved_output_tokens,
                        tool_schema_token_estimate, safety_margin_tokens,
                        available_input_tokens, pre_compaction_token_estimate,
                        input_token_estimate, estimation_method, watermark_state,
                        compaction_id, messages_json, tools_json,
                        tools_hash, tool_result_stubs_json, message_ids_json, source_item_ids_json,
                        artifact_refs_json, memory_refs_json, compaction_refs_json,
                        source_snapshots_json,
                        token_estimate, source_cursor_start, source_cursor_end,
                        source_cursor_namespace, created_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        revision.id,
                        revision.session_id,
                        revision.agent_id,
                        revision.thread_id,
                        revision.workspace_ref,
                        revision.request_ordinal,
                        revision.model_id,
                        revision.prompt_layout.version,
                        watermark.context_window,
                        watermark.reserved_output_tokens,
                        watermark.tool_schema_token_estimate,
                        watermark.safety_margin_tokens,
                        watermark.available_input_tokens,
                        watermark.pre_compaction_token_estimate,
                        watermark.input_token_estimate,
                        watermark.estimation_method,
                        watermark.state.value,
                        revision.compaction_id,
                        messages_json,
                        tools_json,
                        hashlib.sha256(tools_json.encode("utf-8")).hexdigest(),
                        tool_result_stubs_json,
                        message_ids_json,
                        source_item_ids_json,
                        artifact_refs_json,
                        memory_refs_json,
                        compaction_refs_json,
                        json.dumps(
                            [ref.model_dump(mode="json") for ref in revision.source_snapshots],
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        revision.token_estimate,
                        revision.source_cursor_start,
                        revision.source_cursor_end,
                        revision.source_cursor_namespace,
                        revision.created_at.isoformat(),
                    ),
                ).lastrowid
                if cursor is None:
                    raise RuntimeError("ContextRevision insert did not produce a cursor")
                for block in revision.blocks:
                    connection.execute(
                        """
                        INSERT INTO prompt_blocks(
                            revision_id, position, id, block_type, content, content_hash,
                            source_refs_json, stable_until, visibility, token_estimate,
                            cache_eligible
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            revision.id,
                            block.position,
                            block.id,
                            block.block_type.value,
                            block.content,
                            block.content_hash,
                            json.dumps(
                                [ref.model_dump(mode="json") for ref in block.source_refs],
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                            None if block.stable_until is None else block.stable_until.isoformat(),
                            block.visibility.value,
                            block.token_estimate,
                            int(block.cache_eligible),
                        ),
                    )
                for binding in revision.reference_bindings:
                    connection.execute(
                        """
                        INSERT INTO reference_bindings(
                            revision_id, position, id, ref_type, user_text,
                            resolved_target, source_snapshot_hash, include_mode,
                            max_tokens, visibility, resolved_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            revision.id,
                            binding.position,
                            binding.id,
                            binding.ref_type.value,
                            binding.user_text,
                            binding.resolved_target,
                            binding.source_snapshot_hash,
                            binding.include_mode.value,
                            binding.max_tokens,
                            binding.visibility.value,
                            binding.resolved_at.isoformat(),
                        ),
                    )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("ContextRevision scope, order, or identity is invalid") from exc
            row = connection.execute(
                "SELECT * FROM context_revisions WHERE id = ?", (revision.id,)
            ).fetchone()
            assert row is not None
            return self._context_revision_from_row(connection, row)

    def get_context_revision(self, revision_id: str) -> ContextRevision:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM context_revisions WHERE id = ?", (revision_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"ContextRevision not found: {revision_id}")
            return self._context_revision_from_row(connection, row)

    def list_context_revisions(
        self,
        session_id: str,
        *,
        after_cursor: int | None = None,
        limit: int = 100,
    ) -> list[ContextRevision]:
        cursor, page_limit = self._validate_cursor_page(after_cursor, limit)
        with self._connect() as connection:
            if (
                connection.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone()
                is None
            ):
                raise NotFoundError(f"session not found: {session_id}")
            rows = connection.execute(
                """
                SELECT * FROM context_revisions
                WHERE session_id = ? AND sequence > ?
                ORDER BY sequence LIMIT ?
                """,
                (session_id, cursor, page_limit),
            ).fetchall()
            return [self._context_revision_from_row(connection, row) for row in rows]

    def context_revision_cursor_bounds(
        self,
        agent_id: str,
        *,
        before_request_ordinal: int,
    ) -> tuple[int, int] | None:
        if before_request_ordinal < 1:
            raise ValueError("before_request_ordinal must be positive")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT MIN(sequence) AS cursor_start, MAX(sequence) AS cursor_end
                FROM context_revisions
                WHERE agent_id = ? AND request_ordinal < ?
                """,
                (agent_id, before_request_ordinal),
            ).fetchone()
        assert row is not None
        if row["cursor_start"] is None or row["cursor_end"] is None:
            return None
        return int(row["cursor_start"]), int(row["cursor_end"])

    def get_compaction(self, compaction_id: str) -> Compaction:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM compactions WHERE id = ?", (compaction_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Compaction not found: {compaction_id}")
            compaction = self._compaction_from_row(row)
            self._validate_persisted_compaction_evidence(connection, compaction)
            return compaction

    @staticmethod
    def _compaction_from_row(row: sqlite3.Row) -> Compaction:
        summary_json = str(row["summary_json"])
        if hashlib.sha256(summary_json.encode("utf-8")).hexdigest() != row["content_hash"]:
            raise ConflictError("Compaction content hash verification failed")
        return Compaction(
            id=row["id"],
            cursor=int(row["sequence"]),
            session_id=row["session_id"],
            agent_id=row["agent_id"],
            thread_id=row["thread_id"],
            source_type=row["source_type"],
            source_cursor_start=int(row["source_cursor_start"]),
            source_cursor_end=int(row["source_cursor_end"]),
            source_snapshot_hash=row["source_snapshot_hash"],
            summary=CompactionSummary.model_validate_json(summary_json),
            content_hash=row["content_hash"],
            covered_item_refs=tuple(
                ContextSourceRef.model_validate(item)
                for item in json.loads(row["covered_item_refs_json"])
            ),
            created_at=row["created_at"],
        )

    @staticmethod
    def _context_revision_semantic_payload(revision: ContextRevision) -> dict[str, Any]:
        return {
            "session_id": revision.session_id,
            "agent_id": revision.agent_id,
            "thread_id": revision.thread_id,
            "workspace_ref": revision.workspace_ref,
            "request_ordinal": revision.request_ordinal,
            "model_id": revision.model_id,
            "prompt_layout": revision.prompt_layout.model_dump(mode="json"),
            "messages": [message.model_dump(mode="json") for message in revision.messages],
            "tools": [tool.model_dump(mode="json") for tool in revision.tools],
            "blocks": [
                {
                    "position": block.position,
                    "block_type": block.block_type.value,
                    "content": block.content,
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
                    "position": binding.position,
                    "ref_type": binding.ref_type.value,
                    "user_text": binding.user_text,
                    "resolved_target": binding.resolved_target,
                    "source_snapshot_hash": binding.source_snapshot_hash,
                    "include_mode": binding.include_mode.value,
                    "max_tokens": binding.max_tokens,
                    "visibility": binding.visibility.value,
                }
                for binding in revision.reference_bindings
            ],
            "tool_result_stubs": [
                stub.model_dump(mode="json") for stub in revision.tool_result_stubs
            ],
            "watermark": revision.watermark.model_dump(mode="json"),
            "has_compaction": revision.compaction_id is not None,
            "message_ids": revision.message_ids,
            "source_item_ids": revision.source_item_ids,
            "artifact_refs": revision.artifact_refs,
            "memory_refs": revision.memory_refs,
            "compaction_refs": revision.compaction_refs,
            "source_snapshots": [
                source.model_dump(mode="json") for source in revision.source_snapshots
            ],
            "token_estimate": revision.token_estimate,
            "source_cursor_start": revision.source_cursor_start,
            "source_cursor_end": revision.source_cursor_end,
            "source_cursor_namespace": revision.source_cursor_namespace,
        }

    @staticmethod
    def _compaction_semantic_payload(compaction: Compaction | None) -> dict[str, Any] | None:
        if compaction is None:
            return None
        return {
            "session_id": compaction.session_id,
            "agent_id": compaction.agent_id,
            "thread_id": compaction.thread_id,
            "source_type": compaction.source_type.value,
            "source_cursor_start": compaction.source_cursor_start,
            "source_cursor_end": compaction.source_cursor_end,
            "source_snapshot_hash": compaction.source_snapshot_hash,
            "summary": compaction.summary.model_dump(mode="json"),
            "content_hash": compaction.content_hash,
            "covered_item_refs": [
                ref.model_dump(mode="json") for ref in compaction.covered_item_refs
            ],
        }

    @staticmethod
    def _context_revision_from_row(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> ContextRevision:
        block_rows = connection.execute(
            "SELECT * FROM prompt_blocks WHERE revision_id = ? ORDER BY position",
            (row["id"],),
        ).fetchall()
        blocks: list[PromptBlock] = []
        for block_row in block_rows:
            content = str(block_row["content"])
            if hashlib.sha256(content.encode("utf-8")).hexdigest() != block_row["content_hash"]:
                raise ConflictError("Prompt Block content hash verification failed")
            blocks.append(
                PromptBlock(
                    id=block_row["id"],
                    revision_id=row["id"],
                    position=int(block_row["position"]),
                    block_type=block_row["block_type"],
                    content=content,
                    content_hash=block_row["content_hash"],
                    source_refs=tuple(
                        ContextSourceRef.model_validate(item)
                        for item in json.loads(block_row["source_refs_json"])
                    ),
                    stable_until=block_row["stable_until"],
                    visibility=block_row["visibility"],
                    token_estimate=(
                        None
                        if block_row["token_estimate"] is None
                        else int(block_row["token_estimate"])
                    ),
                    cache_eligible=bool(block_row["cache_eligible"]),
                )
            )
        required_block_types = {
            PromptBlockType.ROLE_INSTRUCTIONS,
            PromptBlockType.TOOL_SCHEMA,
            PromptBlockType.CONVERSATION,
        }
        actual_block_types = tuple(block.block_type for block in blocks)
        if not blocks or not required_block_types.issubset(actual_block_types):
            raise ConflictError("ContextRevision is missing required Prompt Blocks")
        binding_rows = connection.execute(
            "SELECT * FROM reference_bindings WHERE revision_id = ? ORDER BY position",
            (row["id"],),
        ).fetchall()
        bindings = tuple(
            ReferenceBinding(
                id=binding["id"],
                revision_id=row["id"],
                position=int(binding["position"]),
                ref_type=binding["ref_type"],
                user_text=binding["user_text"],
                resolved_target=binding["resolved_target"],
                source_snapshot_hash=binding["source_snapshot_hash"],
                include_mode=binding["include_mode"],
                max_tokens=(None if binding["max_tokens"] is None else int(binding["max_tokens"])),
                visibility=binding["visibility"],
                resolved_at=binding["resolved_at"],
            )
            for binding in binding_rows
        )
        messages = tuple(Message.model_validate(item) for item in json.loads(row["messages_json"]))
        tools = tuple(ToolDefinition.model_validate(item) for item in json.loads(row["tools_json"]))
        tool_result_stubs = tuple(
            ToolResultStub.model_validate(item)
            for item in json.loads(row["tool_result_stubs_json"])
        )
        stored_source_snapshots = tuple(
            ContextSourceRef.model_validate(item)
            for item in json.loads(row["source_snapshots_json"])
        )
        stored_compaction_refs = tuple(json.loads(row["compaction_refs_json"]))
        compaction_id = row["compaction_id"]
        compaction_blocks = [
            block for block in blocks if block.block_type is PromptBlockType.COMPACTION
        ]
        compaction_sources = [
            source
            for block in blocks
            for source in block.source_refs
            if source.source_type is ContextSourceType.COMPACTION
        ]
        expected_compaction_refs = () if compaction_id is None else (compaction_id,)
        if stored_compaction_refs != expected_compaction_refs:
            raise ConflictError("ContextRevision compaction_refs verification failed")
        if compaction_id is None:
            if compaction_blocks or compaction_sources:
                raise ConflictError("ContextRevision has detached Compaction Prompt evidence")
        elif (
            len(compaction_blocks) != 1
            or len(compaction_sources) != 1
            or compaction_sources[0].source_id != compaction_id
        ):
            raise ConflictError("ContextRevision Compaction Prompt evidence is incomplete")
        watermark = ContextWatermark(
            state=ContextWatermarkState(row["watermark_state"]),
            context_window=(None if row["context_window"] is None else int(row["context_window"])),
            reserved_output_tokens=(
                None
                if row["reserved_output_tokens"] is None
                else int(row["reserved_output_tokens"])
            ),
            tool_schema_token_estimate=int(row["tool_schema_token_estimate"]),
            safety_margin_tokens=(
                None if row["safety_margin_tokens"] is None else int(row["safety_margin_tokens"])
            ),
            available_input_tokens=(
                None
                if row["available_input_tokens"] is None
                else int(row["available_input_tokens"])
            ),
            pre_compaction_token_estimate=int(row["pre_compaction_token_estimate"]),
            input_token_estimate=int(row["input_token_estimate"]),
            estimation_method=row["estimation_method"],
        )
        revision = ContextRevision(
            id=row["id"],
            cursor=int(row["sequence"]),
            session_id=row["session_id"],
            agent_id=row["agent_id"],
            thread_id=row["thread_id"],
            workspace_ref=row["workspace_ref"],
            request_ordinal=int(row["request_ordinal"]),
            model_id=row["model_id"],
            prompt_layout=PromptLayout(version=row["prompt_layout_version"]),
            messages=messages,
            tools=tools,
            blocks=tuple(blocks),
            reference_bindings=bindings,
            tool_result_stubs=tool_result_stubs,
            watermark=watermark,
            compaction_id=row["compaction_id"],
            message_ids=tuple(json.loads(row["message_ids_json"])),
            source_item_ids=tuple(json.loads(row["source_item_ids_json"])),
            artifact_refs=tuple(json.loads(row["artifact_refs_json"])),
            memory_refs=tuple(json.loads(row["memory_refs_json"])),
            compaction_refs=stored_compaction_refs,
            source_snapshots=stored_source_snapshots,
            token_estimate=int(row["token_estimate"]),
            source_cursor_start=(
                None if row["source_cursor_start"] is None else int(row["source_cursor_start"])
            ),
            source_cursor_end=(
                None if row["source_cursor_end"] is None else int(row["source_cursor_end"])
            ),
            source_cursor_namespace=row["source_cursor_namespace"],
            created_at=row["created_at"],
        )
        SQLiteStore._validate_context_source_refs(
            connection,
            revision,
            current_sources=False,
        )
        SQLiteStore._validate_context_reference_bindings(
            connection,
            revision,
            current_sources=False,
        )
        expected_source_snapshots: list[ContextSourceRef] = []
        seen_source_snapshots: set[tuple[ContextSourceType, str, int | None, str]] = set()
        for block in blocks:
            for source in block.source_refs:
                key = (source.source_type, source.source_id, source.cursor, source.content_hash)
                if key in seen_source_snapshots:
                    continue
                seen_source_snapshots.add(key)
                expected_source_snapshots.append(source)
        if stored_source_snapshots != tuple(expected_source_snapshots):
            raise ConflictError("ContextRevision source snapshots verification failed")
        if revision.compaction_id is not None:
            compaction_row = connection.execute(
                "SELECT * FROM compactions WHERE id = ?", (revision.compaction_id,)
            ).fetchone()
            if compaction_row is None:
                raise ConflictError("ContextRevision Compaction evidence is missing")
            compaction = SQLiteStore._compaction_from_row(compaction_row)
            SQLiteStore._validate_persisted_compaction_evidence(connection, compaction)
        SQLiteStore._validate_context_revision_item_evidence(connection, revision)
        return revision

    # Phase 0 durable Workflow coordinator and Session execution leases

    def acquire_workflow_execution_lease(
        self,
        workflow_run_id: str,
        *,
        owner_id: str,
        ttl_seconds: float,
        now: datetime | None = None,
    ) -> WorkflowExecutionLease | None:
        """Acquire the coordinator guard before marking a Workflow RUNNING."""

        if ttl_seconds <= 0:
            raise ValueError("workflow execution lease TTL must be positive")
        observed_at = utc_now() if now is None else now
        expires_at = observed_at + timedelta(seconds=ttl_seconds)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            workflow_row = connection.execute(
                "SELECT status FROM workflow_runs WHERE id = ?", (workflow_run_id,)
            ).fetchone()
            if workflow_row is None:
                raise NotFoundError(f"workflow run not found: {workflow_run_id}")
            if workflow_row["status"] != WorkflowRunStatus.CREATED.value:
                raise ConflictError("workflow run cannot acquire an execution lease")
            row = connection.execute(
                "SELECT * FROM workflow_execution_leases WHERE workflow_run_id = ?",
                (workflow_run_id,),
            ).fetchone()
            generation = 1
            if row is not None:
                existing = self._workflow_execution_lease_from_row(row)
                if existing.released_at is None and existing.expires_at > observed_at:
                    return None
                generation = existing.generation + 1
            lease_token = new_id("workflow_lease")
            connection.execute(
                """
                INSERT INTO workflow_execution_leases(
                    workflow_run_id, lease_token, owner_id, generation,
                    acquired_at, renewed_at, expires_at, released_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(workflow_run_id) DO UPDATE SET
                    lease_token = excluded.lease_token,
                    owner_id = excluded.owner_id,
                    generation = excluded.generation,
                    acquired_at = excluded.acquired_at,
                    renewed_at = excluded.renewed_at,
                    expires_at = excluded.expires_at,
                    released_at = NULL
                """,
                (
                    workflow_run_id,
                    lease_token,
                    owner_id,
                    generation,
                    observed_at.isoformat(),
                    observed_at.isoformat(),
                    expires_at.isoformat(),
                ),
            )
            claimed = connection.execute(
                "SELECT * FROM workflow_execution_leases WHERE workflow_run_id = ?",
                (workflow_run_id,),
            ).fetchone()
        assert claimed is not None
        return self._workflow_execution_lease_from_row(claimed)

    def activate_workflow_run(
        self,
        lease: WorkflowExecutionLease,
        *,
        now: datetime | None = None,
    ) -> WorkflowRun | None:
        """Move a guarded CREATED run to RUNNING without reviving a cancelled run."""

        return self.update_workflow_run_if_status(
            lease.workflow_run_id,
            expected_status=WorkflowRunStatus.CREATED,
            execution_lease=lease,
            now=now,
            status=WorkflowRunStatus.RUNNING,
        )

    def renew_workflow_execution_lease(
        self,
        lease: WorkflowExecutionLease,
        *,
        ttl_seconds: float,
        now: datetime | None = None,
    ) -> WorkflowExecutionLease | None:
        if ttl_seconds <= 0:
            raise ValueError("workflow execution lease TTL must be positive")
        observed_at = utc_now() if now is None else now
        expires_at = observed_at + timedelta(seconds=ttl_seconds)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE workflow_execution_leases
                SET renewed_at = ?, expires_at = ?
                WHERE workflow_run_id = ? AND lease_token = ? AND generation = ?
                  AND owner_id = ? AND released_at IS NULL AND expires_at > ?
                  AND EXISTS (
                      SELECT 1 FROM workflow_runs
                      WHERE id = workflow_execution_leases.workflow_run_id
                        AND status = ?
                  )
                """,
                (
                    observed_at.isoformat(),
                    expires_at.isoformat(),
                    lease.workflow_run_id,
                    lease.lease_token,
                    lease.generation,
                    lease.owner_id,
                    observed_at.isoformat(),
                    WorkflowRunStatus.RUNNING.value,
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT * FROM workflow_execution_leases WHERE workflow_run_id = ?",
                (lease.workflow_run_id,),
            ).fetchone()
        assert row is not None
        return self._workflow_execution_lease_from_row(row)

    def assert_workflow_execution_lease(
        self,
        lease: WorkflowExecutionLease,
        *,
        now: datetime | None = None,
    ) -> WorkflowExecutionLease:
        observed_at = utc_now() if now is None else now
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT guard.* FROM workflow_execution_leases AS guard
                JOIN workflow_runs AS run ON run.id = guard.workflow_run_id
                WHERE guard.workflow_run_id = ?
                  AND run.status = ?
                """,
                (lease.workflow_run_id, WorkflowRunStatus.RUNNING.value),
            ).fetchone()
        if row is None:
            raise ConflictError("workflow execution lease is unavailable")
        current = self._workflow_execution_lease_from_row(row)
        if (
            current.lease_token != lease.lease_token
            or current.generation != lease.generation
            or current.owner_id != lease.owner_id
            or current.released_at is not None
            or current.expires_at <= observed_at
        ):
            raise ConflictError("workflow execution lease is expired, cancelled, or fenced")
        return current

    def release_workflow_execution_lease(
        self,
        lease: WorkflowExecutionLease,
        *,
        now: datetime | None = None,
    ) -> bool:
        observed_at = utc_now() if now is None else now
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE workflow_execution_leases SET released_at = ?
                WHERE workflow_run_id = ? AND lease_token = ? AND generation = ?
                  AND owner_id = ? AND released_at IS NULL
                """,
                (
                    observed_at.isoformat(),
                    lease.workflow_run_id,
                    lease.lease_token,
                    lease.generation,
                    lease.owner_id,
                ),
            )
        return cursor.rowcount == 1

    def interrupt_workflow_run_if_execution_lease_matches(
        self,
        lease: WorkflowExecutionLease,
        *,
        error_type: str,
        now: datetime | None = None,
    ) -> bool:
        """Record coordinator loss only while the expected guard still owns the run."""

        observed_at = utc_now() if now is None else now
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT run.body FROM workflow_runs AS run
                JOIN workflow_execution_leases AS guard
                  ON guard.workflow_run_id = run.id
                WHERE run.id = ? AND run.status = ?
                  AND guard.lease_token = ? AND guard.generation = ?
                  AND guard.owner_id = ?
                """,
                (
                    lease.workflow_run_id,
                    WorkflowRunStatus.RUNNING.value,
                    lease.lease_token,
                    lease.generation,
                    lease.owner_id,
                ),
            ).fetchone()
            if row is None:
                return False
            current = WorkflowRun.model_validate_json(row["body"])
            interrupted = current.model_copy(
                update={
                    "status": WorkflowRunStatus.INTERRUPTED,
                    "last_error_type": error_type,
                    "updated_at": observed_at,
                }
            )
            cursor = connection.execute(
                """
                UPDATE workflow_runs
                SET body = ?, status = ?, current_stage = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    interrupted.model_dump_json(),
                    interrupted.status.value,
                    interrupted.current_stage.value,
                    interrupted.updated_at.isoformat(),
                    interrupted.id,
                    WorkflowRunStatus.RUNNING.value,
                ),
            )
        return cursor.rowcount == 1

    def get_workflow_execution_lease(self, workflow_run_id: str) -> WorkflowExecutionLease:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_execution_leases WHERE workflow_run_id = ?",
                (workflow_run_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"workflow execution lease not found: {workflow_run_id}")
        return self._workflow_execution_lease_from_row(row)

    @staticmethod
    def _workflow_execution_lease_from_row(row: sqlite3.Row) -> WorkflowExecutionLease:
        return WorkflowExecutionLease(
            workflow_run_id=str(row["workflow_run_id"]),
            lease_token=str(row["lease_token"]),
            owner_id=str(row["owner_id"]),
            generation=int(row["generation"]),
            acquired_at=datetime.fromisoformat(str(row["acquired_at"])),
            renewed_at=datetime.fromisoformat(str(row["renewed_at"])),
            expires_at=datetime.fromisoformat(str(row["expires_at"])),
            released_at=(
                None
                if row["released_at"] is None
                else datetime.fromisoformat(str(row["released_at"]))
            ),
        )

    def acquire_session_run_lease(
        self,
        session_id: str,
        *,
        owner_id: str,
        ttl_seconds: float,
        workflow_run_id: str | None = None,
        workflow_execution_lease: WorkflowExecutionLease | None = None,
        now: datetime | None = None,
    ) -> SessionRunLease | None:
        """Atomically acquire or reclaim the single run lease for a Session.

        An expired lease may be reclaimed only when its previous Agent has no
        uncertain side-effect receipt.  This prevents a process crash from
        turning an unknown write into an automatic replay.
        """

        if ttl_seconds <= 0:
            raise ValueError("session run lease TTL must be positive")
        observed_at = utc_now() if now is None else now
        expires_at = observed_at + timedelta(seconds=ttl_seconds)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if (
                connection.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone()
                is None
            ):
                raise NotFoundError(f"session not found: {session_id}")
            if workflow_run_id is not None:
                workflow_row = connection.execute(
                    "SELECT status FROM workflow_runs WHERE id = ?", (workflow_run_id,)
                ).fetchone()
                if workflow_row is None:
                    raise NotFoundError(f"workflow run not found: {workflow_run_id}")
                if workflow_row["status"] != WorkflowRunStatus.RUNNING.value:
                    raise ConflictError("workflow run is not running")
                if workflow_execution_lease is None:
                    raise ConflictError("workflow execution lease is required for a child run")
                if workflow_execution_lease.workflow_run_id != workflow_run_id:
                    raise ConflictError("workflow execution lease belongs to another run")
                guard = connection.execute(
                    """
                    SELECT 1 FROM workflow_execution_leases
                    WHERE workflow_run_id = ? AND lease_token = ? AND generation = ?
                      AND owner_id = ? AND released_at IS NULL AND expires_at > ?
                    """,
                    (
                        workflow_run_id,
                        workflow_execution_lease.lease_token,
                        workflow_execution_lease.generation,
                        workflow_execution_lease.owner_id,
                        observed_at.isoformat(),
                    ),
                ).fetchone()
                if guard is None:
                    raise ConflictError("workflow execution lease is expired, cancelled, or fenced")
            pending = connection.execute(
                """
                SELECT 1 FROM approval_requests
                WHERE session_id = ? AND status = ? LIMIT 1
                """,
                (session_id, ApprovalStatus.PENDING.value),
            ).fetchone()
            if pending is not None:
                raise ConflictError(
                    "session has a pending durable approval; decide or reconcile it "
                    "before starting another run"
                )
            row = connection.execute(
                "SELECT * FROM session_run_leases WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            generation = 1
            if row is not None:
                existing = self._session_run_lease_from_row(row)
                if existing.released_at is None and existing.expires_at > observed_at:
                    return None
                generation = existing.generation + 1
            # A durable denial proves the approval-gated action was never
            # authorized. Resolve only that exact receipt while admitting a
            # later run; do not pre-empt a still-live Agent continuation.
            connection.execute(
                """
                UPDATE tool_action_receipts
                SET status = ?, result_json = ?, error_code = ?,
                    updated_at = ?, completed_at = ?
                WHERE session_id = ? AND status IN (?, ?)
                  AND EXISTS (
                      SELECT 1
                      FROM approval_requests AS request
                      JOIN approval_decisions AS decision
                        ON decision.approval_id = request.id
                      WHERE request.tool_action_receipt_id = tool_action_receipts.id
                        AND request.session_id = tool_action_receipts.session_id
                        AND request.agent_id = tool_action_receipts.agent_id
                        AND decision.approved = 0
                  )
                """,
                (
                    ToolActionReceiptStatus.FAILED.value,
                    json.dumps(
                        {"error": "approval denied"},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    "approval_denied",
                    observed_at.isoformat(),
                    observed_at.isoformat(),
                    session_id,
                    ToolActionReceiptStatus.IN_PROGRESS.value,
                    ToolActionReceiptStatus.OUTCOME_UNKNOWN.value,
                ),
            )
            uncertain = connection.execute(
                """
                SELECT 1 FROM tool_action_receipts
                WHERE session_id = ? AND status IN (?, ?)
                LIMIT 1
                """,
                (
                    session_id,
                    ToolActionReceiptStatus.IN_PROGRESS.value,
                    ToolActionReceiptStatus.OUTCOME_UNKNOWN.value,
                ),
            ).fetchone()
            if uncertain is not None:
                connection.execute(
                    """
                    UPDATE tool_action_receipts
                    SET status = ?, error_code = ?, updated_at = ?
                    WHERE session_id = ? AND status = ?
                    """,
                    (
                        ToolActionReceiptStatus.OUTCOME_UNKNOWN.value,
                        "session_lease_expired",
                        observed_at.isoformat(),
                        session_id,
                        ToolActionReceiptStatus.IN_PROGRESS.value,
                    ),
                )
                # Persist the fail-safe state before surfacing the
                # manual-reconcile conflict to the caller.
                connection.commit()
                raise ActionOutcomeUnknownError(
                    "session has an uncertain tool action; manual reconciliation required"
                )
            lease_token = new_id("lease")
            connection.execute(
                """
                INSERT INTO session_run_leases(
                    session_id, lease_token, owner_id, generation,
                    workflow_run_id, agent_id, cancel_requested,
                    acquired_at, renewed_at, expires_at, released_at
                ) VALUES (?, ?, ?, ?, ?, NULL, 0, ?, ?, ?, NULL)
                ON CONFLICT(session_id) DO UPDATE SET
                    lease_token = excluded.lease_token,
                    owner_id = excluded.owner_id,
                    generation = excluded.generation,
                    workflow_run_id = excluded.workflow_run_id,
                    agent_id = NULL,
                    cancel_requested = 0,
                    acquired_at = excluded.acquired_at,
                    renewed_at = excluded.renewed_at,
                    expires_at = excluded.expires_at,
                    released_at = NULL
                """,
                (
                    session_id,
                    lease_token,
                    owner_id,
                    generation,
                    workflow_run_id,
                    observed_at.isoformat(),
                    observed_at.isoformat(),
                    expires_at.isoformat(),
                ),
            )
            claimed = connection.execute(
                "SELECT * FROM session_run_leases WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        assert claimed is not None
        return self._session_run_lease_from_row(claimed)

    def bind_session_run_lease_agent(
        self,
        lease: SessionRunLease,
        agent_id: str,
        *,
        now: datetime | None = None,
    ) -> SessionRunLease:
        observed_at = utc_now() if now is None else now
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            agent = connection.execute(
                "SELECT session_id FROM agents WHERE id = ?", (agent_id,)
            ).fetchone()
            if agent is None:
                raise NotFoundError(f"agent not found: {agent_id}")
            if str(agent["session_id"]) != lease.session_id:
                raise ConflictError("agent belongs to another session")
            cursor = connection.execute(
                """
                UPDATE session_run_leases SET agent_id = ?
                WHERE session_id = ? AND lease_token = ? AND generation = ? AND owner_id = ?
                  AND released_at IS NULL AND cancel_requested = 0 AND expires_at > ?
                """,
                (
                    agent_id,
                    lease.session_id,
                    lease.lease_token,
                    lease.generation,
                    lease.owner_id,
                    observed_at.isoformat(),
                ),
            )
            if cursor.rowcount != 1:
                raise ConflictError("session run lease is no longer current")
            row = connection.execute(
                "SELECT * FROM session_run_leases WHERE session_id = ?",
                (lease.session_id,),
            ).fetchone()
        assert row is not None
        return self._session_run_lease_from_row(row)

    def renew_session_run_lease(
        self,
        lease: SessionRunLease,
        *,
        ttl_seconds: float,
        now: datetime | None = None,
    ) -> SessionRunLease | None:
        if ttl_seconds <= 0:
            raise ValueError("session run lease TTL must be positive")
        observed_at = utc_now() if now is None else now
        expires_at = observed_at + timedelta(seconds=ttl_seconds)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE session_run_leases
                SET renewed_at = ?, expires_at = ?
                WHERE session_id = ? AND lease_token = ? AND generation = ? AND owner_id = ?
                  AND released_at IS NULL AND cancel_requested = 0 AND expires_at > ?
                """,
                (
                    observed_at.isoformat(),
                    expires_at.isoformat(),
                    lease.session_id,
                    lease.lease_token,
                    lease.generation,
                    lease.owner_id,
                    observed_at.isoformat(),
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT * FROM session_run_leases WHERE session_id = ?",
                (lease.session_id,),
            ).fetchone()
        assert row is not None
        return self._session_run_lease_from_row(row)

    def assert_session_run_lease(
        self,
        lease: SessionRunLease,
        *,
        now: datetime | None = None,
    ) -> SessionRunLease:
        observed_at = utc_now() if now is None else now
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM session_run_leases
                WHERE session_id = ? AND lease_token = ? AND generation = ? AND owner_id = ?
                """,
                (
                    lease.session_id,
                    lease.lease_token,
                    lease.generation,
                    lease.owner_id,
                ),
            ).fetchone()
        if row is None:
            raise ConflictError("session run lease is expired, cancelled, or fenced")
        current = self._session_run_lease_from_row(row)
        if (
            current.lease_token != lease.lease_token
            or current.generation != lease.generation
            or current.owner_id != lease.owner_id
            or current.released_at is not None
            or current.cancel_requested
            or current.expires_at <= observed_at
        ):
            raise ConflictError("session run lease is expired, cancelled, or fenced")
        return current

    def release_session_run_lease(
        self,
        lease: SessionRunLease,
        *,
        now: datetime | None = None,
    ) -> bool:
        observed_at = utc_now() if now is None else now
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE session_run_leases
                SET released_at = ?, expires_at = ?
                WHERE session_id = ? AND lease_token = ? AND generation = ? AND owner_id = ?
                  AND released_at IS NULL
                """,
                (
                    observed_at.isoformat(),
                    observed_at.isoformat(),
                    lease.session_id,
                    lease.lease_token,
                    lease.generation,
                    lease.owner_id,
                ),
            )
        return cursor.rowcount == 1

    def cancel_session_run_lease(
        self,
        session_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        observed_at = utc_now() if now is None else now
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT 1 FROM session_run_leases
                WHERE session_id = ? AND released_at IS NULL AND expires_at > ?
                """,
                (session_id, observed_at.isoformat()),
            ).fetchone()
            if row is None:
                return False
            connection.execute(
                """
                UPDATE session_run_leases SET cancel_requested = 1
                WHERE session_id = ? AND released_at IS NULL AND expires_at > ?
                """,
                (session_id, observed_at.isoformat()),
            )
        return True

    def cancel_workflow_run_leases(
        self,
        workflow_run_id: str,
        *,
        now: datetime | None = None,
    ) -> tuple[str, ...]:
        observed_at = utc_now() if now is None else now
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT session_id FROM session_run_leases
                WHERE workflow_run_id = ? AND released_at IS NULL AND expires_at > ?
                ORDER BY session_id
                """,
                (workflow_run_id, observed_at.isoformat()),
            ).fetchall()
            connection.execute(
                """
                UPDATE session_run_leases
                SET cancel_requested = 1, released_at = ?, expires_at = ?
                WHERE workflow_run_id = ? AND released_at IS NULL AND expires_at > ?
                """,
                (
                    observed_at.isoformat(),
                    observed_at.isoformat(),
                    workflow_run_id,
                    observed_at.isoformat(),
                ),
            )
        return tuple(str(row["session_id"]) for row in rows)

    def get_session_run_lease(self, session_id: str) -> SessionRunLease:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM session_run_leases WHERE session_id = ?", (session_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"session run lease not found: {session_id}")
        return self._session_run_lease_from_row(row)

    @staticmethod
    def _session_run_lease_from_row(row: sqlite3.Row) -> SessionRunLease:
        return SessionRunLease(
            session_id=str(row["session_id"]),
            lease_token=str(row["lease_token"]),
            owner_id=str(row["owner_id"]),
            generation=int(row["generation"]),
            workflow_run_id=(
                None if row["workflow_run_id"] is None else str(row["workflow_run_id"])
            ),
            agent_id=None if row["agent_id"] is None else str(row["agent_id"]),
            cancel_requested=bool(row["cancel_requested"]),
            acquired_at=datetime.fromisoformat(str(row["acquired_at"])),
            renewed_at=datetime.fromisoformat(str(row["renewed_at"])),
            expires_at=datetime.fromisoformat(str(row["expires_at"])),
            released_at=(
                None
                if row["released_at"] is None
                else datetime.fromisoformat(str(row["released_at"]))
            ),
        )

    # M0 command and Action Gateway persistence

    def reserve_tool_action(
        self,
        receipt: ToolActionReceipt,
        *,
        lease: SessionRunLease | None = None,
        now: datetime | None = None,
    ) -> tuple[ToolActionReceipt, bool]:
        """Claim one agent-scoped Tool Call or return its durable prior result."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if lease is not None:
                observed_at = utc_now() if now is None else now
                lease_row = connection.execute(
                    """
                    SELECT * FROM session_run_leases
                    WHERE session_id = ? AND lease_token = ? AND generation = ?
                      AND owner_id = ? AND released_at IS NULL
                      AND cancel_requested = 0 AND expires_at > ?
                    """,
                    (
                        lease.session_id,
                        lease.lease_token,
                        lease.generation,
                        lease.owner_id,
                        observed_at.isoformat(),
                    ),
                ).fetchone()
                if lease_row is None or lease.session_id != receipt.session_id:
                    raise ConflictError("session run lease is expired, cancelled, or fenced")
            row = connection.execute(
                """
                SELECT * FROM tool_action_receipts
                WHERE scope = ? AND idempotency_key = ?
                """,
                (receipt.scope, receipt.idempotency_key),
            ).fetchone()
            if row is not None:
                existing = self._tool_action_from_row(row)
                existing_binding = (
                    existing.scope,
                    existing.idempotency_key,
                    existing.action_hash,
                    existing.session_id,
                    existing.agent_id,
                    existing.command_name,
                )
                requested_binding = (
                    receipt.scope,
                    receipt.idempotency_key,
                    receipt.action_hash,
                    receipt.session_id,
                    receipt.agent_id,
                    receipt.command_name,
                )
                if existing_binding != requested_binding:
                    raise IdempotencyConflictError(
                        "tool idempotency key was already used for a different action context"
                    )
                if existing.status in {
                    ToolActionReceiptStatus.IN_PROGRESS,
                    ToolActionReceiptStatus.OUTCOME_UNKNOWN,
                }:
                    raise ActionOutcomeUnknownError(
                        "tool action is already in progress or its outcome is unknown"
                    )
                return existing, False

            existing_id = connection.execute(
                "SELECT 1 FROM tool_action_receipts WHERE id = ?", (receipt.id,)
            ).fetchone()
            if existing_id is not None:
                raise IdempotencyConflictError(
                    "tool action receipt ID is already bound to a different action context"
                )
            if (
                receipt.status != ToolActionReceiptStatus.IN_PROGRESS
                or receipt.result_json is not None
                or receipt.error_code is not None
                or receipt.completed_at is not None
            ):
                raise ConflictError(
                    "a new tool action receipt must start in_progress without an outcome"
                )

            agent_row = connection.execute(
                "SELECT session_id FROM agents WHERE id = ?", (receipt.agent_id,)
            ).fetchone()
            if agent_row is None:
                raise ConflictError("tool action receipt agent does not exist")
            if agent_row["session_id"] != receipt.session_id:
                raise ConflictError("tool action receipt agent does not belong to session")
            try:
                connection.execute(
                    """
                    INSERT INTO tool_action_receipts(
                        id, scope, session_id, agent_id, idempotency_key, action_hash,
                        command_name, status, result_json, error_code, created_at,
                        updated_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        receipt.id,
                        receipt.scope,
                        receipt.session_id,
                        receipt.agent_id,
                        receipt.idempotency_key,
                        receipt.action_hash,
                        receipt.command_name,
                        receipt.status.value,
                        receipt.result_json,
                        receipt.error_code,
                        receipt.created_at.isoformat(),
                        receipt.updated_at.isoformat(),
                        None,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("unable to reserve tool action") from exc
        return receipt, True

    def complete_tool_action(
        self,
        receipt_id: str,
        *,
        action_hash: str,
        result_json: str,
    ) -> ToolActionReceipt:
        completed_at = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE tool_action_receipts
                SET status = ?, result_json = ?, error_code = NULL,
                    updated_at = ?, completed_at = ?
                WHERE id = ? AND action_hash = ? AND status = ?
                """,
                (
                    ToolActionReceiptStatus.COMPLETED.value,
                    result_json,
                    completed_at.isoformat(),
                    completed_at.isoformat(),
                    receipt_id,
                    action_hash,
                    ToolActionReceiptStatus.IN_PROGRESS.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ActionOutcomeUnknownError("tool action claim is no longer executable")
            row = connection.execute(
                "SELECT * FROM tool_action_receipts WHERE id = ?", (receipt_id,)
            ).fetchone()
        assert row is not None
        return self._tool_action_from_row(row)

    def fail_tool_action(
        self,
        receipt_id: str,
        *,
        action_hash: str,
        error_code: str,
        result_json: str,
    ) -> ToolActionReceipt:
        failed_at = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE tool_action_receipts
                SET status = ?, result_json = ?, error_code = ?,
                    updated_at = ?, completed_at = ?
                WHERE id = ? AND action_hash = ? AND status = ?
                """,
                (
                    ToolActionReceiptStatus.FAILED.value,
                    result_json,
                    error_code,
                    failed_at.isoformat(),
                    failed_at.isoformat(),
                    receipt_id,
                    action_hash,
                    ToolActionReceiptStatus.IN_PROGRESS.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ActionOutcomeUnknownError("tool action claim is no longer executable")
            row = connection.execute(
                "SELECT * FROM tool_action_receipts WHERE id = ?", (receipt_id,)
            ).fetchone()
        assert row is not None
        return self._tool_action_from_row(row)

    def get_tool_action_receipt(self, receipt_id: str) -> ToolActionReceipt:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tool_action_receipts WHERE id = ?", (receipt_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"tool action receipt not found: {receipt_id}")
        return self._tool_action_from_row(row)

    def reserve_command_execution(self, command: CommandExecution) -> tuple[CommandExecution, bool]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM command_executions
                WHERE command_type = ? AND idempotency_key = ?
                """,
                (command.command_type, command.idempotency_key),
            ).fetchone()
            if row is not None:
                existing = self._command_execution_from_row(row)
                if existing.action_hash != command.action_hash:
                    raise IdempotencyConflictError(
                        "idempotency key was already used with a different command payload"
                    )
                return existing, False
            connection.execute(
                """
                INSERT INTO command_executions(
                    id, command_type, idempotency_key, action_hash, status,
                    resource_type, resource_id, response_json, http_status,
                    error_code, created_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    command.id,
                    command.command_type,
                    command.idempotency_key,
                    command.action_hash,
                    command.status.value,
                    command.resource_type,
                    command.resource_id,
                    command.response_json,
                    command.http_status,
                    command.error_code,
                    command.created_at.isoformat(),
                    command.updated_at.isoformat(),
                    None,
                ),
            )
        return command, True

    def complete_command_execution(
        self,
        command_id: str,
        *,
        response_json: str,
        http_status: int,
        resource_type: str | None = None,
        resource_id: str | None = None,
    ) -> CommandExecution:
        completed_at = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE command_executions
                SET status = ?, response_json = ?, http_status = ?,
                    resource_type = COALESCE(?, resource_type),
                    resource_id = COALESCE(?, resource_id),
                    error_code = NULL, updated_at = ?, completed_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    CommandExecutionStatus.COMPLETED.value,
                    response_json,
                    http_status,
                    resource_type,
                    resource_id,
                    completed_at.isoformat(),
                    completed_at.isoformat(),
                    command_id,
                    CommandExecutionStatus.IN_PROGRESS.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ActionOutcomeUnknownError("command execution is no longer claimable")
            row = connection.execute(
                "SELECT * FROM command_executions WHERE id = ?", (command_id,)
            ).fetchone()
        assert row is not None
        return self._command_execution_from_row(row)

    def fail_command_execution(
        self,
        command_id: str,
        *,
        error_code: str,
        http_status: int,
        response_json: str,
    ) -> CommandExecution:
        failed_at = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE command_executions
                SET status = ?, response_json = ?, http_status = ?, error_code = ?,
                    updated_at = ?, completed_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    CommandExecutionStatus.FAILED.value,
                    response_json,
                    http_status,
                    error_code,
                    failed_at.isoformat(),
                    failed_at.isoformat(),
                    command_id,
                    CommandExecutionStatus.IN_PROGRESS.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ActionOutcomeUnknownError("command execution is no longer claimable")
            row = connection.execute(
                "SELECT * FROM command_executions WHERE id = ?", (command_id,)
            ).fetchone()
        assert row is not None
        return self._command_execution_from_row(row)

    def mark_command_manual_reconcile(
        self,
        command_id: str,
        *,
        error_code: str,
    ) -> CommandExecution:
        """Close an uncommitted command claim whose side-effect outcome is unknown."""

        reconciled_at = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE command_executions
                SET status = ?, error_code = ?, updated_at = ?, completed_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    CommandExecutionStatus.MANUAL_RECONCILE_REQUIRED.value,
                    error_code[:200],
                    reconciled_at.isoformat(),
                    reconciled_at.isoformat(),
                    command_id,
                    CommandExecutionStatus.IN_PROGRESS.value,
                ),
            )
            row = connection.execute(
                "SELECT * FROM command_executions WHERE id = ?", (command_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"command execution not found: {command_id}")
        return self._command_execution_from_row(row)

    def get_command_execution(self, command_id: str) -> CommandExecution:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM command_executions WHERE id = ?", (command_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"command execution not found: {command_id}")
        return self._command_execution_from_row(row)

    def create_approval_request(self, approval: ApprovalRequest) -> ApprovalRequest:
        if approval.status is not ApprovalStatus.PENDING:
            raise ConflictError("approval request must start in pending status")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            receipt_row = connection.execute(
                "SELECT * FROM tool_action_receipts WHERE id = ?",
                (approval.tool_action_receipt_id,),
            ).fetchone()
            if receipt_row is None:
                raise ConflictError("approval requires an existing tool action receipt")
            receipt = self._tool_action_from_row(receipt_row)
            row = connection.execute(
                """
                SELECT * FROM approval_requests
                WHERE agent_id = ? AND tool_call_id = ?
                """,
                (approval.agent_id, approval.tool_call_id),
            ).fetchone()
            if row is not None:
                existing = self._approval_request_from_row(row)
                if (
                    existing.action_hash != approval.action_hash
                    or existing.tool_action_receipt_id != approval.tool_action_receipt_id
                    or existing.session_id != approval.session_id
                ):
                    raise IdempotencyConflictError(
                        "approval tool call id was reused for a different action"
                    )
            if (
                receipt.session_id != approval.session_id
                or receipt.agent_id != approval.agent_id
                or receipt.idempotency_key != approval.tool_call_id
                or receipt.action_hash != approval.action_hash
            ):
                raise ConflictError("approval does not match its tool action receipt")
            if receipt.status is not ToolActionReceiptStatus.IN_PROGRESS:
                raise ConflictError("tool action receipt is not awaiting approval")
            if row is not None:
                return existing
            connection.execute(
                """
                INSERT INTO approval_requests(
                    id, session_id, agent_id, tool_action_receipt_id, tool_call_id,
                    action_hash, category, detail_summary, status, requested_at,
                    expires_at, updated_at, decided_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval.id,
                    approval.session_id,
                    approval.agent_id,
                    approval.tool_action_receipt_id,
                    approval.tool_call_id,
                    approval.action_hash,
                    approval.category,
                    approval.detail_summary,
                    approval.status.value,
                    approval.requested_at.isoformat(),
                    approval.expires_at.isoformat(),
                    approval.updated_at.isoformat(),
                    None,
                ),
            )
            self._append_approval_audit_row(
                connection,
                ApprovalAuditEvent(
                    approval_id=approval.id,
                    event_type="approval.requested",
                    payload={"status": ApprovalStatus.PENDING.value},
                ),
            )
        return approval

    def get_approval_request(self, session_id: str, tool_call_id: str) -> ApprovalRequest:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_pending_approvals(connection, session_id)
            row = self._select_approval_row(connection, session_id, tool_call_id)
        if row is None:
            raise NotFoundError(f"approval not found: {session_id}/{tool_call_id}")
        return self._approval_request_from_row(row)

    def list_approval_requests(
        self,
        session_id: str,
        *,
        status: ApprovalStatus | str | None = ApprovalStatus.PENDING,
    ) -> list[ApprovalRequest]:
        parameters: list[Any] = [session_id]
        query = "SELECT * FROM approval_requests WHERE session_id = ?"
        if status is not None:
            normalized = status if isinstance(status, ApprovalStatus) else ApprovalStatus(status)
            query += " AND status = ?"
            parameters.append(normalized.value)
        query += " ORDER BY requested_at, id"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_pending_approvals(connection, session_id)
            rows = connection.execute(query, parameters).fetchall()
        return [self._approval_request_from_row(row) for row in rows]

    def decide_approval(
        self,
        session_id: str,
        tool_call_id: str,
        *,
        approved: bool,
        decided_by: str = "user",
        reason_code: str | None = None,
    ) -> tuple[ApprovalRequest, ApprovalDecision, bool]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire_pending_approvals(connection, session_id)
            row = self._select_approval_row(connection, session_id, tool_call_id)
            if row is None:
                raise NotFoundError(f"approval not found: {session_id}/{tool_call_id}")
            request = self._approval_request_from_row(row)
            decision_row = connection.execute(
                "SELECT * FROM approval_decisions WHERE approval_id = ?", (request.id,)
            ).fetchone()
            if decision_row is not None:
                decision = self._approval_decision_from_row(decision_row)
                if decision.approved != approved:
                    raise ConflictError("approval already has the opposite decision")
                return request, decision, False
            if request.status is not ApprovalStatus.PENDING:
                raise ConflictError("approval is not pending")
            decided_at = utc_now()
            target_status = ApprovalStatus.APPROVED if approved else ApprovalStatus.DENIED
            decision = ApprovalDecision(
                approval_id=request.id,
                approved=approved,
                decided_by=decided_by,
                reason_code=reason_code,
                decided_at=decided_at,
            )
            connection.execute(
                """
                INSERT INTO approval_decisions(
                    id, approval_id, approved, decided_by, reason_code, decided_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.id,
                    decision.approval_id,
                    int(decision.approved),
                    decision.decided_by,
                    decision.reason_code,
                    decision.decided_at.isoformat(),
                ),
            )
            cursor = connection.execute(
                """
                UPDATE approval_requests
                SET status = ?, updated_at = ?, decided_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    target_status.value,
                    decided_at.isoformat(),
                    decided_at.isoformat(),
                    request.id,
                    ApprovalStatus.PENDING.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ConflictError("approval is no longer pending")
            self._append_approval_audit_row(
                connection,
                ApprovalAuditEvent(
                    approval_id=request.id,
                    event_type="approval.decided",
                    payload={"status": target_status.value, "approved": approved},
                    created_at=decided_at,
                ),
            )
            updated_row = connection.execute(
                "SELECT * FROM approval_requests WHERE id = ?", (request.id,)
            ).fetchone()
        assert updated_row is not None
        return self._approval_request_from_row(updated_row), decision, True

    def get_approval_decision(self, approval_id: str) -> ApprovalDecision | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM approval_decisions WHERE approval_id = ?", (approval_id,)
            ).fetchone()
        return None if row is None else self._approval_decision_from_row(row)

    def list_approval_audit_events(self, approval_id: str) -> list[ApprovalAuditEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT sequence, id, approval_id, event_type, body, created_at
                FROM approval_audit_events
                WHERE approval_id = ? ORDER BY sequence
                """,
                (approval_id,),
            ).fetchall()
        return [
            ApprovalAuditEvent(
                id=row["id"],
                approval_id=row["approval_id"],
                cursor=int(row["sequence"]),
                event_type=row["event_type"],
                payload=json.loads(row["body"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    @staticmethod
    def _append_approval_audit_row(
        connection: sqlite3.Connection, event: ApprovalAuditEvent
    ) -> None:
        connection.execute(
            """
            INSERT INTO approval_audit_events(id, approval_id, event_type, body, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                event.id,
                event.approval_id,
                event.event_type,
                json.dumps(event.payload, ensure_ascii=False, separators=(",", ":")),
                event.created_at.isoformat(),
            ),
        )

    @staticmethod
    def _tool_action_from_row(row: sqlite3.Row) -> ToolActionReceipt:
        return ToolActionReceipt(
            id=row["id"],
            scope=row["scope"],
            session_id=row["session_id"],
            agent_id=row["agent_id"],
            idempotency_key=row["idempotency_key"],
            action_hash=row["action_hash"],
            command_name=row["command_name"],
            status=row["status"],
            result_json=row["result_json"],
            error_code=row["error_code"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
        )

    @staticmethod
    def _command_execution_from_row(row: sqlite3.Row) -> CommandExecution:
        return CommandExecution(
            id=row["id"],
            command_type=row["command_type"],
            idempotency_key=row["idempotency_key"],
            action_hash=row["action_hash"],
            status=row["status"],
            resource_type=row["resource_type"],
            resource_id=row["resource_id"],
            response_json=row["response_json"],
            http_status=row["http_status"],
            error_code=row["error_code"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
        )

    @staticmethod
    def _select_approval_row(
        connection: sqlite3.Connection, session_id: str, tool_call_id: str
    ) -> sqlite3.Row | None:
        rows = connection.execute(
            """
            SELECT * FROM approval_requests
            WHERE session_id = ? AND tool_call_id = ?
            ORDER BY CASE WHEN status = 'pending' THEN 0 ELSE 1 END,
                     requested_at DESC, id DESC
            """,
            (session_id, tool_call_id),
        ).fetchall()
        pending = [row for row in rows if row["status"] == ApprovalStatus.PENDING.value]
        if len(pending) > 1:
            raise ConflictError("multiple pending approvals share the same legacy tool call id")
        if pending:
            selected: sqlite3.Row = pending[0]
            return selected
        if not rows:
            return None
        selected = rows[0]
        return selected

    @staticmethod
    def _approval_request_from_row(row: sqlite3.Row) -> ApprovalRequest:
        return ApprovalRequest(
            id=row["id"],
            session_id=row["session_id"],
            agent_id=row["agent_id"],
            tool_action_receipt_id=row["tool_action_receipt_id"],
            tool_call_id=row["tool_call_id"],
            action_hash=row["action_hash"],
            category=row["category"],
            detail_summary=row["detail_summary"],
            status=row["status"],
            requested_at=row["requested_at"],
            expires_at=row["expires_at"],
            updated_at=row["updated_at"],
            decided_at=row["decided_at"],
        )

    @staticmethod
    def _approval_decision_from_row(row: sqlite3.Row) -> ApprovalDecision:
        return ApprovalDecision(
            id=row["id"],
            approval_id=row["approval_id"],
            approved=bool(row["approved"]),
            decided_by=row["decided_by"],
            reason_code=row["reason_code"],
            decided_at=row["decided_at"],
        )

    # Workflow persistence

    def create_workflow_run(self, workflow_run: WorkflowRun) -> WorkflowRun:
        with self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO workflow_runs(
                        id, body, status, current_stage, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        workflow_run.id,
                        workflow_run.model_dump_json(),
                        workflow_run.status.value,
                        workflow_run.current_stage.value,
                        workflow_run.created_at.isoformat(),
                        workflow_run.updated_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"workflow run already exists: {workflow_run.id}") from exc
        return workflow_run

    def get_workflow_run(self, workflow_run_id: str) -> WorkflowRun:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT body FROM workflow_runs WHERE id = ?", (workflow_run_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"workflow run not found: {workflow_run_id}")
        return WorkflowRun.model_validate_json(row["body"])

    def list_workflow_runs(
        self,
        *,
        status: WorkflowRunStatus | str | None = None,
        workspace_ref: str | None = None,
        limit: int | None = None,
    ) -> list[WorkflowRun]:
        parameters: list[Any] = []
        conditions: list[str] = []
        if status is not None:
            normalized_status = (
                status if isinstance(status, WorkflowRunStatus) else WorkflowRunStatus(status)
            )
            conditions.append("status = ?")
            parameters.append(normalized_status.value)
        if workspace_ref is not None:
            # Workflow Run predates the Workspace projection and stores its
            # normalized workspace only in the immutable JSON body. Keep the
            # join exact; do not resolve aliases or infer legacy ownership.
            conditions.append("json_extract(body, '$.workspace') = ?")
            parameters.append(workspace_ref)
        query = "SELECT body FROM workflow_runs"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at, id"
        if limit is not None:
            if limit < 1:
                raise ValueError("workflow run list limit must be positive")
            query += " LIMIT ?"
            parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [WorkflowRun.model_validate_json(row["body"]) for row in rows]

    def update_workflow_run(self, workflow_run_id: str, **changes: Any) -> WorkflowRun:
        forbidden = {"id", "created_at"}
        attempted = forbidden.intersection(changes)
        if attempted:
            raise ValueError(f"cannot change workflow run identity fields: {sorted(attempted)}")
        current = self.get_workflow_run(workflow_run_id)
        updated_data = current.model_dump()
        updated_data.update(changes)
        updated_data["updated_at"] = utc_now()
        updated = WorkflowRun.model_validate(updated_data)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE workflow_runs
                SET body = ?, status = ?, current_stage = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    updated.model_dump_json(),
                    updated.status.value,
                    updated.current_stage.value,
                    updated.updated_at.isoformat(),
                    workflow_run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise NotFoundError(f"workflow run not found: {workflow_run_id}")
        return updated

    def update_workflow_run_if_status(
        self,
        workflow_run_id: str,
        *,
        expected_status: WorkflowRunStatus | str,
        execution_lease: WorkflowExecutionLease | None = None,
        now: datetime | None = None,
        **changes: Any,
    ) -> WorkflowRun | None:
        """Atomically update a Workflow only from the expected persisted status."""

        forbidden = {"id", "created_at"}
        attempted = forbidden.intersection(changes)
        if attempted:
            raise ValueError(f"cannot change workflow run identity fields: {sorted(attempted)}")
        normalized_status = (
            expected_status
            if isinstance(expected_status, WorkflowRunStatus)
            else WorkflowRunStatus(expected_status)
        )
        observed_at = utc_now() if now is None else now
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT body, status FROM workflow_runs WHERE id = ?",
                (workflow_run_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"workflow run not found: {workflow_run_id}")
            current = WorkflowRun.model_validate_json(row["body"])
            if (
                str(row["status"]) != normalized_status.value
                or current.status is not normalized_status
            ):
                return None
            if execution_lease is not None:
                if execution_lease.workflow_run_id != workflow_run_id:
                    raise ConflictError("workflow execution lease belongs to another run")
                guard = connection.execute(
                    """
                    SELECT 1 FROM workflow_execution_leases
                    WHERE workflow_run_id = ? AND lease_token = ? AND generation = ?
                      AND owner_id = ? AND released_at IS NULL AND expires_at > ?
                    """,
                    (
                        workflow_run_id,
                        execution_lease.lease_token,
                        execution_lease.generation,
                        execution_lease.owner_id,
                        observed_at.isoformat(),
                    ),
                ).fetchone()
                if guard is None:
                    return None
            updated_data = current.model_dump()
            updated_data.update(changes)
            updated_data["updated_at"] = observed_at
            updated = WorkflowRun.model_validate(updated_data)
            cursor = connection.execute(
                """
                UPDATE workflow_runs
                SET body = ?, status = ?, current_stage = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    updated.model_dump_json(),
                    updated.status.value,
                    updated.current_stage.value,
                    updated.updated_at.isoformat(),
                    workflow_run_id,
                    normalized_status.value,
                ),
            )
            if cursor.rowcount != 1:
                return None
        return updated

    def cancel_workflow_run_atomically(
        self,
        workflow_run_id: str,
        *,
        now: datetime | None = None,
    ) -> tuple[bool, tuple[str, ...]]:
        """Cancel a Workflow and fence every unreleased child lease in one commit."""

        observed_at = utc_now() if now is None else now
        terminal_statuses = {
            WorkflowRunStatus.COMPLETED,
            WorkflowRunStatus.FAILED,
            WorkflowRunStatus.CANCELLED,
        }
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT body, status FROM workflow_runs WHERE id = ?",
                (workflow_run_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"workflow run not found: {workflow_run_id}")
            current = WorkflowRun.model_validate_json(row["body"])
            if current.status in terminal_statuses:
                return False, ()
            cancelled = WorkflowRun.model_validate(
                {
                    **current.model_dump(),
                    "status": WorkflowRunStatus.CANCELLED,
                    "last_error_type": "cancelled",
                    "updated_at": observed_at,
                }
            )
            connection.execute(
                """
                UPDATE workflow_runs
                SET body = ?, status = ?, current_stage = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    cancelled.model_dump_json(),
                    cancelled.status.value,
                    cancelled.current_stage.value,
                    cancelled.updated_at.isoformat(),
                    workflow_run_id,
                ),
            )
            rows = connection.execute(
                """
                SELECT session_id FROM session_run_leases
                WHERE workflow_run_id = ? AND released_at IS NULL
                ORDER BY session_id
                """,
                (workflow_run_id,),
            ).fetchall()
            connection.execute(
                """
                UPDATE session_run_leases SET cancel_requested = 1
                WHERE workflow_run_id = ? AND released_at IS NULL
                """,
                (workflow_run_id,),
            )
        return True, tuple(str(row["session_id"]) for row in rows)

    def append_workflow_event(self, event: WorkflowRunEvent) -> WorkflowRunEvent:
        self.get_workflow_run(event.workflow_run_id)
        with self._connect() as connection:
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO workflow_run_events(
                        id, workflow_run_id, role, session_id, event_type, body, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.id,
                        event.workflow_run_id,
                        event.role,
                        event.session_id,
                        event.event_type,
                        json.dumps(event.payload, ensure_ascii=False, separators=(",", ":")),
                        event.created_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"workflow event already exists: {event.id}") from exc
            if cursor.lastrowid is None:
                raise RuntimeError("SQLite did not return a workflow event sequence")
            sequence = cursor.lastrowid
        return event.model_copy(update={"sequence": sequence, "cursor": sequence})

    def list_workflow_events(
        self,
        workflow_run_id: str,
        *,
        after_cursor: int | None = None,
        limit: int = 1000,
    ) -> list[WorkflowRunEvent]:
        self.get_workflow_run(workflow_run_id)
        if after_cursor is not None and after_cursor < 0:
            raise ValueError("event cursor must not be negative")
        if not 1 <= limit <= 1000:
            raise ValueError("event list limit must be between 1 and 1000")
        query = """
            SELECT sequence, id, workflow_run_id, role, session_id,
                   event_type, body, created_at
            FROM workflow_run_events
            WHERE workflow_run_id = ?
        """
        parameters: list[Any] = [workflow_run_id]
        if after_cursor is not None:
            query += " AND sequence > ?"
            parameters.append(after_cursor)
        query += " ORDER BY sequence"
        query += " LIMIT ?"
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            WorkflowRunEvent(
                id=row["id"],
                workflow_run_id=row["workflow_run_id"],
                sequence=int(row["sequence"]),
                cursor=int(row["sequence"]),
                role=row["role"],
                session_id=row["session_id"],
                event_type=row["event_type"],
                payload=json.loads(row["body"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    # Explicit aliases keep the workflow-level API discoverable to callers
    # that use "workflow" rather than the persisted "workflow run" name.

    def create_workflow(self, workflow_run: WorkflowRun) -> WorkflowRun:
        return self.create_workflow_run(workflow_run)

    def get_workflow(self, workflow_run_id: str) -> WorkflowRun:
        return self.get_workflow_run(workflow_run_id)

    def list_workflows(
        self,
        *,
        status: WorkflowRunStatus | str | None = None,
        limit: int | None = None,
    ) -> list[WorkflowRun]:
        return self.list_workflow_runs(status=status, limit=limit)

    def update_workflow(self, workflow_run_id: str, **changes: Any) -> WorkflowRun:
        return self.update_workflow_run(workflow_run_id, **changes)

    def append_workflow_run_event(self, event: WorkflowRunEvent) -> WorkflowRunEvent:
        return self.append_workflow_event(event)

    def list_workflow_run_events(self, workflow_run_id: str) -> list[WorkflowRunEvent]:
        return self.list_workflow_events(workflow_run_id)

    # Evaluation persistence

    def create_evaluation_suite(self, suite: EvaluationSuite) -> EvaluationSuite:
        """Persist an immutable suite and its ordered cases/variants atomically."""

        with self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO evaluation_suites(id, body, experiment, status, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        suite.id,
                        suite.model_dump_json(),
                        suite.experiment.value if suite.experiment is not None else None,
                        suite.status.value,
                        suite.created_at.isoformat(),
                    ),
                )
                for ordinal, case in enumerate(suite.cases, start=1):
                    connection.execute(
                        """
                        INSERT INTO evaluation_cases(id, suite_id, ordinal, body, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            case.id,
                            suite.id,
                            ordinal,
                            case.model_dump_json(),
                            suite.created_at.isoformat(),
                        ),
                    )
                for ordinal, variant in enumerate(suite.variants, start=1):
                    connection.execute(
                        """
                        INSERT INTO evaluation_variants(id, suite_id, ordinal, body, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            variant.id,
                            suite.id,
                            ordinal,
                            variant.model_dump_json(),
                            suite.created_at.isoformat(),
                        ),
                    )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(
                    f"evaluation suite or child already exists: {suite.id}"
                ) from exc
        return suite

    def get_evaluation_suite(self, suite_id: str) -> EvaluationSuite:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT body FROM evaluation_suites WHERE id = ?", (suite_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"evaluation suite not found: {suite_id}")
            return self._load_evaluation_suite(connection, row["body"])

    def list_evaluation_suites(
        self,
        *,
        status: EvaluationSuiteStatus | str | None = None,
        limit: int | None = None,
    ) -> list[EvaluationSuite]:
        parameters: list[Any] = []
        query = "SELECT body FROM evaluation_suites"
        if status is not None:
            normalized_status = (
                status
                if isinstance(status, EvaluationSuiteStatus)
                else EvaluationSuiteStatus(status)
            )
            query += " WHERE status = ?"
            parameters.append(normalized_status.value)
        query += " ORDER BY created_at, id"
        if limit is not None:
            if limit < 1:
                raise ValueError("evaluation suite list limit must be positive")
            query += " LIMIT ?"
            parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
            return [self._load_evaluation_suite(connection, row["body"]) for row in rows]

    @staticmethod
    def _load_evaluation_suite(
        connection: sqlite3.Connection,
        serialized_suite: str,
    ) -> EvaluationSuite:
        data = json.loads(serialized_suite)
        suite_id = data["id"]
        case_rows = connection.execute(
            "SELECT body FROM evaluation_cases WHERE suite_id = ? ORDER BY ordinal", (suite_id,)
        ).fetchall()
        variant_rows = connection.execute(
            "SELECT body FROM evaluation_variants WHERE suite_id = ? ORDER BY ordinal", (suite_id,)
        ).fetchall()
        data["cases"] = [json.loads(row["body"]) for row in case_rows]
        data["variants"] = [json.loads(row["body"]) for row in variant_rows]
        return EvaluationSuite.model_validate(data)

    def create_evaluation_run(self, evaluation_run: EvaluationRun) -> EvaluationRun:
        self.get_evaluation_suite(evaluation_run.suite_id)
        with self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO evaluation_runs(
                        id, suite_id, body, status, execution_strategy, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        evaluation_run.id,
                        evaluation_run.suite_id,
                        evaluation_run.model_dump_json(),
                        evaluation_run.status.value,
                        evaluation_run.execution_strategy.value,
                        evaluation_run.created_at.isoformat(),
                        evaluation_run.updated_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"evaluation run already exists: {evaluation_run.id}") from exc
        return evaluation_run

    def get_evaluation_run(self, evaluation_run_id: str) -> EvaluationRun:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT body FROM evaluation_runs WHERE id = ?", (evaluation_run_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"evaluation run not found: {evaluation_run_id}")
        return EvaluationRun.model_validate_json(row["body"])

    def list_evaluation_runs(
        self,
        *,
        suite_id: str | None = None,
        status: EvaluationRunStatus | str | None = None,
        limit: int | None = None,
    ) -> list[EvaluationRun]:
        conditions: list[str] = []
        parameters: list[Any] = []
        if suite_id is not None:
            conditions.append("suite_id = ?")
            parameters.append(suite_id)
        if status is not None:
            normalized_status = (
                status if isinstance(status, EvaluationRunStatus) else EvaluationRunStatus(status)
            )
            conditions.append("status = ?")
            parameters.append(normalized_status.value)
        query = "SELECT body FROM evaluation_runs"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at, id"
        if limit is not None:
            if limit < 1:
                raise ValueError("evaluation run list limit must be positive")
            query += " LIMIT ?"
            parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [EvaluationRun.model_validate_json(row["body"]) for row in rows]

    def update_evaluation_run(self, evaluation_run_id: str, **changes: Any) -> EvaluationRun:
        forbidden = {"id", "suite_id", "created_at"}
        attempted = forbidden.intersection(changes)
        if attempted:
            raise ValueError(f"cannot change evaluation run identity fields: {sorted(attempted)}")
        current = self.get_evaluation_run(evaluation_run_id)
        updated_data = current.model_dump()
        updated_data.update(changes)
        updated_data["updated_at"] = utc_now()
        updated = EvaluationRun.model_validate(updated_data)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE evaluation_runs
                SET body = ?, status = ?, execution_strategy = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    updated.model_dump_json(),
                    updated.status.value,
                    updated.execution_strategy.value,
                    updated.updated_at.isoformat(),
                    evaluation_run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise NotFoundError(f"evaluation run not found: {evaluation_run_id}")
        return updated

    def append_evaluation_event(self, event: EvaluationRunEvent) -> EvaluationRunEvent:
        self.get_evaluation_run(event.evaluation_run_id)
        with self._connect() as connection:
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO evaluation_run_events(
                        id, evaluation_run_id, result_id, event_type, body, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.id,
                        event.evaluation_run_id,
                        event.result_id,
                        event.event_type,
                        json.dumps(event.payload, ensure_ascii=False, separators=(",", ":")),
                        event.created_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"evaluation event could not be appended: {event.id}") from exc
            if cursor.lastrowid is None:
                raise RuntimeError("SQLite did not return an evaluation event cursor")
            event_cursor = int(cursor.lastrowid)
        return event.model_copy(update={"cursor": event_cursor})

    def list_evaluation_events(
        self,
        evaluation_run_id: str,
        *,
        after_cursor: int | None = None,
        limit: int = 1000,
    ) -> list[EvaluationRunEvent]:
        self.get_evaluation_run(evaluation_run_id)
        if after_cursor is not None and after_cursor < 0:
            raise ValueError("event cursor must not be negative")
        if not 1 <= limit <= 1000:
            raise ValueError("event list limit must be between 1 and 1000")
        query = """
            SELECT sequence, id, evaluation_run_id, result_id, event_type, body, created_at
            FROM evaluation_run_events WHERE evaluation_run_id = ?
        """
        parameters: list[Any] = [evaluation_run_id]
        if after_cursor is not None:
            query += " AND sequence > ?"
            parameters.append(after_cursor)
        query += " ORDER BY sequence LIMIT ?"
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            EvaluationRunEvent(
                id=row["id"],
                evaluation_run_id=row["evaluation_run_id"],
                cursor=int(row["sequence"]),
                result_id=row["result_id"],
                event_type=row["event_type"],
                payload=json.loads(row["body"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def append_evaluation_result(self, result: EvaluationResult) -> EvaluationResult:
        """Create one unique scheduled result after proving its suite membership."""

        with self._connect() as connection:
            run_row = connection.execute(
                "SELECT suite_id FROM evaluation_runs WHERE id = ?", (result.run_id,)
            ).fetchone()
            if run_row is None:
                raise NotFoundError(f"evaluation run not found: {result.run_id}")
            suite_id = run_row["suite_id"]
            suite_row = connection.execute(
                "SELECT body FROM evaluation_suites WHERE id = ?", (suite_id,)
            ).fetchone()
            if suite_row is None:
                raise NotFoundError(f"evaluation suite not found: {suite_id}")
            suite = self._load_evaluation_suite(connection, suite_row["body"])
            if result.repetition > suite.repetitions:
                raise ValueError(
                    "evaluation result repetition exceeds the evaluation suite repetition count"
                )
            case_row = connection.execute(
                "SELECT body FROM evaluation_cases WHERE id = ? AND suite_id = ?",
                (result.case_id, suite_id),
            ).fetchone()
            if case_row is None:
                raise NotFoundError(f"evaluation case not found in suite: {result.case_id}")
            variant_row = connection.execute(
                "SELECT body FROM evaluation_variants WHERE id = ? AND suite_id = ?",
                (result.variant_id, suite_id),
            ).fetchone()
            if variant_row is None:
                raise NotFoundError(f"evaluation variant not found in suite: {result.variant_id}")
            case = EvaluationCase.model_validate_json(case_row["body"])
            variant = EvaluationVariant.model_validate_json(variant_row["body"])
            assert_evaluation_result_contract(case, variant, result)
            try:
                connection.execute(
                    """
                    INSERT INTO evaluation_results(
                        id, run_id, case_id, variant_id, repetition,
                        status, body, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        result.id,
                        result.run_id,
                        result.case_id,
                        result.variant_id,
                        result.repetition,
                        result.status.value,
                        result.model_dump_json(),
                        result.created_at.isoformat(),
                        result.updated_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(
                    "evaluation result already exists for this run, case, variant, and repetition"
                ) from exc
        return result

    def get_evaluation_result(self, result_id: str) -> EvaluationResult:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT body FROM evaluation_results WHERE id = ?", (result_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"evaluation result not found: {result_id}")
        return EvaluationResult.model_validate_json(row["body"])

    def update_evaluation_result(self, result_id: str, **changes: Any) -> EvaluationResult:
        """Atomically advance one persisted result from pending to its terminal fact."""

        forbidden = {
            "id",
            "run_id",
            "case_id",
            "variant_id",
            "repetition",
            "created_at",
            "updated_at",
        }
        terminal_statuses = {
            EvaluationResultStatus.PASSED,
            EvaluationResultStatus.FAILED,
            EvaluationResultStatus.ERROR,
            EvaluationResultStatus.SKIPPED,
            EvaluationResultStatus.INTERRUPTED,
        }

        with self._connect() as connection:
            # Keep the persisted body/status/updated-at tuple coherent even if
            # two in-process runners happen to finish the same result together.
            connection.execute("BEGIN IMMEDIATE")
            result_row = connection.execute(
                "SELECT body FROM evaluation_results WHERE id = ?", (result_id,)
            ).fetchone()
            if result_row is None:
                raise NotFoundError(f"evaluation result not found: {result_id}")
            current = EvaluationResult.model_validate_json(result_row["body"])
            if current.status is not EvaluationResultStatus.PENDING:
                raise ValueError(
                    "evaluation results may only be updated once from pending to a terminal status"
                )
            attempted = forbidden.intersection(changes)
            if attempted:
                raise ValueError(
                    f"cannot change evaluation result identity fields: {sorted(attempted)}"
                )
            updated_data = current.model_dump()
            updated_data.update(changes)
            updated_data["updated_at"] = utc_now()
            updated = EvaluationResult.model_validate(updated_data)
            if updated.status not in terminal_statuses:
                raise ValueError(
                    "evaluation results must transition from pending to a terminal status"
                )
            run_row = connection.execute(
                "SELECT suite_id FROM evaluation_runs WHERE id = ?", (updated.run_id,)
            ).fetchone()
            if run_row is None:
                raise NotFoundError(f"evaluation run not found: {updated.run_id}")
            suite_row = connection.execute(
                "SELECT body FROM evaluation_suites WHERE id = ?", (run_row["suite_id"],)
            ).fetchone()
            if suite_row is None:
                raise NotFoundError(f"evaluation suite not found: {run_row['suite_id']}")
            suite = self._load_evaluation_suite(connection, suite_row["body"])
            if updated.repetition > suite.repetitions:
                raise ValueError(
                    "evaluation result repetition exceeds the evaluation suite repetition count"
                )
            case_row = connection.execute(
                "SELECT body FROM evaluation_cases WHERE id = ? AND suite_id = ?",
                (updated.case_id, suite.id),
            ).fetchone()
            variant_row = connection.execute(
                "SELECT body FROM evaluation_variants WHERE id = ? AND suite_id = ?",
                (updated.variant_id, suite.id),
            ).fetchone()
            if case_row is None or variant_row is None:
                raise NotFoundError("evaluation result references a missing case or variant")
            if updated.status in terminal_statuses:
                assert_evaluation_result_contract(
                    EvaluationCase.model_validate_json(case_row["body"]),
                    EvaluationVariant.model_validate_json(variant_row["body"]),
                    updated,
                )
            cursor = connection.execute(
                """
                UPDATE evaluation_results
                SET status = ?, body = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    updated.status.value,
                    updated.model_dump_json(),
                    updated.updated_at.isoformat(),
                    result_id,
                ),
            )
            if cursor.rowcount != 1:
                raise NotFoundError(f"evaluation result not found: {result_id}")
        return updated

    def list_evaluation_results(self, evaluation_run_id: str) -> list[EvaluationResult]:
        self.get_evaluation_run(evaluation_run_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT body FROM evaluation_results
                WHERE run_id = ?
                ORDER BY case_id, variant_id, repetition, created_at, id
                """,
                (evaluation_run_id,),
            ).fetchall()
        return [EvaluationResult.model_validate_json(row["body"]) for row in rows]

    # Memory Store

    def create_memory(self, memory: Memory) -> Memory:
        """Persist the first version of a Memory and add it to the FTS index."""

        if memory.version != 1:
            raise ValueError("a new memory must start at version 1")
        with self._connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO memories(id, current_version, created_at) VALUES (?, ?, ?)",
                    (memory.id, memory.version, memory.created_at.isoformat()),
                )
                self._insert_memory_version(connection, memory)
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"memory already exists: {memory.id}") from exc
        return memory

    def get_memory(self, memory_id: str, version: int | None = None) -> Memory:
        with self._connect() as connection:
            if version is None:
                head = connection.execute(
                    "SELECT current_version FROM memories WHERE id = ?", (memory_id,)
                ).fetchone()
                if head is None:
                    raise NotFoundError(f"memory not found: {memory_id}")
                version = int(head["current_version"])
            row = connection.execute(
                """
                SELECT body FROM memory_versions
                WHERE memory_id = ? AND version = ?
                """,
                (memory_id, version),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"memory version not found: {memory_id}@{version}")
        return Memory.model_validate_json(row["body"])

    def list_memory_versions(self, memory_id: str) -> list[Memory]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT body FROM memory_versions
                WHERE memory_id = ? ORDER BY version
                """,
                (memory_id,),
            ).fetchall()
        if not rows:
            raise NotFoundError(f"memory not found: {memory_id}")
        return [Memory.model_validate_json(row["body"]) for row in rows]

    def update_memory(self, memory_id: str, **changes: Any) -> Memory:
        """Create a new immutable version of a Memory.

        Identity and version metadata are controlled by the store.  All other
        fields are validated by the domain model before the transaction is
        committed.
        """

        forbidden = {"id", "version", "created_at"}
        attempted = forbidden.intersection(changes)
        if attempted:
            raise ValueError(f"cannot change memory identity fields: {sorted(attempted)}")
        current = self.get_memory(memory_id)
        updated_data = current.model_dump()
        updated_data.update(changes)
        updated_data["version"] = current.version + 1
        updated_data["created_at"] = utc_now()
        updated = Memory.model_validate(updated_data)
        with self._connect() as connection:
            head = connection.execute(
                "SELECT current_version FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
            if head is None:
                raise NotFoundError(f"memory not found: {memory_id}")
            if int(head["current_version"]) != current.version:
                raise ConflictError(f"memory changed while updating: {memory_id}")
            self._insert_memory_version(connection, updated)
            connection.execute(
                "UPDATE memories SET current_version = ? WHERE id = ?",
                (updated.version, memory_id),
            )
        return updated

    def deactivate_memory(self, memory_id: str) -> Memory:
        return self.update_memory(memory_id, status=MemoryStatus.INACTIVE)

    def trace_memory_source(self, memory_id: str, version: int | None = None) -> MemorySource:
        return self.get_memory(memory_id, version).source

    def list_memories_by_source(
        self,
        source_session_id: str,
        *,
        source_task: str | None = None,
        include_inactive: bool = False,
    ) -> list[Memory]:
        conditions = [
            "v.source_session_id = ?",
            "h.current_version = v.version",
        ]
        parameters: list[Any] = [source_session_id]
        if source_task is not None:
            conditions.append("v.source_task = ?")
            parameters.append(source_task)
        if not include_inactive:
            conditions.append("v.status != ?")
            parameters.append(MemoryStatus.INACTIVE.value)
        query = (
            """
            SELECT v.body
            FROM memories AS h
            JOIN memory_versions AS v ON v.memory_id = h.id
            WHERE """
            + " AND ".join(conditions)
            + " ORDER BY v.created_at, v.memory_id"
        )
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [Memory.model_validate_json(row["body"]) for row in rows]

    def search_memories(
        self,
        query: str,
        *,
        project_scope: str | None = None,
        source_session_id: str | None = None,
        kinds: Collection[MemoryKind | str] | None = None,
        role_id: str | None = None,
        role_name: str | None = None,
        include_candidates: bool = False,
        limit: int = 20,
    ) -> list[Memory]:
        """Search current, accessible versions through SQLite FTS5.

        Scope-sensitive working and project filtering is intentionally applied
        in SQL before the small role-scope post-filter.  Historical versions
        remain in the FTS table for source/version inspection but the head join
        ensures normal queries only return the current version.
        """

        if not 1 <= limit <= 100:
            raise ValueError("memory search limit must be between 1 and 100")
        normalized_kinds: tuple[str, ...] = ()
        if kinds is not None:
            normalized_kinds = tuple(
                kind.value if isinstance(kind, MemoryKind) else MemoryKind(kind).value
                for kind in kinds
            )
            if not normalized_kinds:
                return []

        conditions = [
            "h.current_version = v.version",
            "fts.memory_id = v.memory_id",
            "fts.version = v.version",
        ]
        parameters: list[Any] = []
        if normalized_kinds:
            placeholders = ", ".join("?" for _ in normalized_kinds)
            conditions.append(f"v.kind IN ({placeholders})")
            parameters.extend(normalized_kinds)
        if include_candidates:
            conditions.append("v.status IN (?, ?)")
            parameters.extend([MemoryStatus.ACTIVE.value, MemoryStatus.CANDIDATE.value])
        else:
            conditions.append("v.status = ?")
            parameters.append(MemoryStatus.ACTIVE.value)

        # Project knowledge must never leak across project boundaries.  A
        # missing project scope therefore excludes project items rather than
        # treating them as globally visible.
        if project_scope is None:
            conditions.append("v.kind != ?")
            parameters.append(MemoryKind.PROJECT.value)
        else:
            conditions.append("(v.kind != ? OR v.project_scope = ?)")
            parameters.extend([MemoryKind.PROJECT.value, project_scope])

        # Working memory is always bound to one source session.
        if source_session_id is None:
            conditions.append("v.kind != ?")
            parameters.append(MemoryKind.WORKING.value)
        else:
            conditions.append("(v.kind != ? OR v.source_session_id = ?)")
            parameters.extend([MemoryKind.WORKING.value, source_session_id])

        fts_query = self._build_fts_query(query)
        match_clause = ""
        if fts_query:
            match_clause = " AND memory_fts MATCH ?"
            parameters.append(fts_query)
        sql = (
            "SELECT v.body, bm25(memory_fts) AS rank "
            "FROM memory_fts AS fts "
            "JOIN memories AS h ON h.id = fts.memory_id "
            "JOIN memory_versions AS v ON v.memory_id = fts.memory_id "
            "WHERE "
            + " AND ".join(conditions)
            + match_clause
            + " ORDER BY rank, v.created_at DESC LIMIT ?"
        )
        parameters.append(min(limit * 10, 1000))

        try:
            with self._connect() as connection:
                rows = connection.execute(sql, parameters).fetchall()
        except sqlite3.OperationalError as exc:
            raise ValueError("invalid memory full-text query") from exc

        memories: list[Memory] = []
        for row in rows:
            memory = Memory.model_validate_json(row["body"])
            if memory.role_scope and not self._role_scope_matches(
                memory,
                role_id=role_id,
                role_name=role_name,
            ):
                continue
            memories.append(memory)
            if len(memories) >= limit:
                break
        return memories

    @staticmethod
    def _insert_memory_version(connection: sqlite3.Connection, memory: Memory) -> None:
        body = memory.model_dump_json()
        columns = {
            str(row["name"])
            for row in connection.execute('PRAGMA table_info("memory_versions")').fetchall()
        }
        if "body_hash" in columns:
            fields = (
                "memory_id, version, body, body_hash, kind, content, project_scope, "
                "role_scope, source_session_id, source_task, confidence, status, created_at"
            )
            values: tuple[Any, ...]
            values = (
                memory.id,
                memory.version,
                body,
                hashlib.sha256(body.encode("utf-8")).hexdigest(),
                memory.kind.value,
                memory.content,
                memory.project_scope,
                json.dumps(list(memory.role_scope), ensure_ascii=False, separators=(",", ":")),
                memory.source_session_id,
                memory.source_task,
                memory.confidence,
                memory.status.value,
                memory.created_at.isoformat(),
            )
        else:
            fields = (
                "memory_id, version, body, kind, content, project_scope, role_scope, "
                "source_session_id, source_task, confidence, status, created_at"
            )
            values = (
                memory.id,
                memory.version,
                body,
                memory.kind.value,
                memory.content,
                memory.project_scope,
                json.dumps(list(memory.role_scope), ensure_ascii=False, separators=(",", ":")),
                memory.source_session_id,
                memory.source_task,
                memory.confidence,
                memory.status.value,
                memory.created_at.isoformat(),
            )
        placeholders = ", ".join("?" for _ in values)
        connection.execute(
            f"INSERT INTO memory_versions({fields}) VALUES ({placeholders})",
            values,
        )
        connection.execute(
            """
            INSERT INTO memory_fts(memory_id, version, content, source_task, project_scope)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                memory.id,
                memory.version,
                memory.content,
                memory.source_task or "",
                memory.project_scope or "",
            ),
        )

    # Phase 1D Slash commands, explicit Context baselines, and BTW Sidecar

    def register_workspace(
        self,
        initialization: WorkspaceInitialization,
    ) -> tuple[WorkspaceInitialization, bool]:
        if initialization.cursor is not None:
            raise ValueError("a new Workspace initialization cannot provide a cursor")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM workspace_initializations WHERE workspace_ref = ?",
                (initialization.workspace_ref,),
            ).fetchone()
            if row is not None:
                existing = self._workspace_initialization_from_row(row)
                if (
                    existing.workspace_hash == initialization.workspace_hash
                    and existing.readable == initialization.readable
                    and existing.writable == initialization.writable
                ):
                    return existing, False
                raise ConflictError("Workspace is already registered with different facts")
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO workspace_initializations(
                        id, workspace_ref, workspace_hash, readable, writable, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        initialization.id,
                        initialization.workspace_ref,
                        initialization.workspace_hash,
                        int(initialization.readable),
                        int(initialization.writable),
                        initialization.created_at.isoformat(),
                    ),
                ).lastrowid
            except sqlite3.IntegrityError as exc:
                raise ConflictError("Workspace initialization identity already exists") from exc
            if cursor is None:
                raise RuntimeError("Workspace initialization insert did not produce a cursor")
            return initialization.model_copy(update={"cursor": int(cursor)}), True

    def get_workspace_initialization(self, workspace_hash: str) -> WorkspaceInitialization:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM workspace_initializations WHERE workspace_hash = ?",
                (workspace_hash,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Workspace initialization not found: {workspace_hash}")
        return self._workspace_initialization_from_row(row)

    def get_workspace_initialization_by_id(self, workspace_id: str) -> WorkspaceInitialization:
        """Return one immutable workspace registration by its public projection ID."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM workspace_initializations WHERE id = ?",
                (workspace_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Workspace initialization not found: {workspace_id}")
        return self._workspace_initialization_from_row(row)

    def list_workspace_initializations(
        self,
        *,
        after_cursor: int | None = None,
        limit: int = 100,
    ) -> list[WorkspaceInitialization]:
        cursor, page_limit = self._validate_cursor_page(after_cursor, limit)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM workspace_initializations
                WHERE sequence > ? ORDER BY sequence LIMIT ?
                """,
                (cursor, page_limit),
            ).fetchall()
        return [self._workspace_initialization_from_row(row) for row in rows]

    @staticmethod
    def _workspace_initialization_from_row(row: sqlite3.Row) -> WorkspaceInitialization:
        return WorkspaceInitialization(
            id=row["id"],
            cursor=int(row["sequence"]),
            workspace_ref=row["workspace_ref"],
            workspace_hash=row["workspace_hash"],
            readable=bool(row["readable"]),
            writable=bool(row["writable"]),
            created_at=row["created_at"],
        )

    def append_thread_compaction(self, compaction: Compaction) -> Compaction:
        """Append exact THREAD_ITEMS evidence without inventing a model request."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._append_thread_compaction_on_connection(connection, compaction)

    def _append_thread_compaction_on_connection(
        self,
        connection: sqlite3.Connection,
        compaction: Compaction,
    ) -> Compaction:
        if compaction.cursor is not None:
            raise ValueError("a new Compaction cannot provide a cursor")
        if compaction.source_type is not CompactionSourceType.THREAD_ITEMS:
            raise ValueError("explicit Context compaction must cover Thread Items")
        summary_json = compaction.summary.model_dump_json()
        if hashlib.sha256(summary_json.encode("utf-8")).hexdigest() != compaction.content_hash:
            raise ValueError("Compaction content hash does not match its summary")
        refs_json = json.dumps(
            [ref.model_dump(mode="json") for ref in compaction.covered_item_refs],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self._validate_thread_item_compaction_evidence(connection, compaction)
        existing_row = connection.execute(
            "SELECT * FROM compactions WHERE id = ?",
            (compaction.id,),
        ).fetchone()
        if existing_row is not None:
            existing = self._compaction_from_row(existing_row)
            self._validate_persisted_compaction_evidence(connection, existing)
            if self._compaction_semantic_payload(existing) == self._compaction_semantic_payload(
                compaction
            ):
                return existing
            raise ConflictError("Compaction identity already has different evidence")
        try:
            cursor = connection.execute(
                """
                INSERT INTO compactions(
                    id, session_id, agent_id, thread_id, source_type,
                    source_cursor_start, source_cursor_end, source_snapshot_hash,
                    summary_json, content_hash, covered_item_refs_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    compaction.id,
                    compaction.session_id,
                    compaction.agent_id,
                    compaction.thread_id,
                    compaction.source_type.value,
                    compaction.source_cursor_start,
                    compaction.source_cursor_end,
                    compaction.source_snapshot_hash,
                    summary_json,
                    compaction.content_hash,
                    refs_json,
                    compaction.created_at.isoformat(),
                ),
            ).lastrowid
        except sqlite3.IntegrityError as exc:
            raise ConflictError("Compaction scope or identity is invalid") from exc
        if cursor is None:
            raise RuntimeError("Compaction insert did not produce a cursor")
        return compaction.model_copy(update={"cursor": int(cursor)})

    def append_context_baseline(
        self,
        baseline: ContextBaseline,
        *,
        compaction: Compaction | None = None,
    ) -> ContextBaseline:
        if baseline.cursor is not None:
            raise ValueError("a new Context baseline cannot provide a cursor")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if (baseline.operation is ContextBaselineOperation.COMPACT) != (compaction is not None):
                raise ValueError("Context baseline Compaction evidence is incomplete")
            if compaction is not None:
                if compaction.id != baseline.compaction_id:
                    raise ValueError("Context baseline references a different Compaction")
                compaction = self._append_thread_compaction_on_connection(connection, compaction)
            existing_row = connection.execute(
                "SELECT * FROM context_baselines WHERE id = ?",
                (baseline.id,),
            ).fetchone()
            if existing_row is not None:
                existing = self._context_baseline_from_row(existing_row)
                candidate = baseline.model_copy(
                    update={"previous_baseline_id": existing.previous_baseline_id}
                )
                if self._context_baseline_payload(existing) == self._context_baseline_payload(
                    candidate
                ):
                    return existing
                raise ConflictError("Context baseline identity already has different evidence")
            if (
                connection.execute(
                    "SELECT 1 FROM sessions WHERE id = ?", (baseline.session_id,)
                ).fetchone()
                is None
            ):
                raise NotFoundError(f"session not found: {baseline.session_id}")
            thread_row = connection.execute(
                "SELECT 1 FROM threads WHERE id = ?",
                (baseline.thread_id,),
            ).fetchone()
            if thread_row is None:
                raise NotFoundError(f"thread not found: {baseline.thread_id}")
            latest_item = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS cursor FROM items WHERE thread_id = ?",
                (baseline.thread_id,),
            ).fetchone()
            assert latest_item is not None
            if int(latest_item["cursor"]) != baseline.item_cursor_end:
                raise ConflictError("Context baseline must advance to the latest Thread Item")
            previous_row = connection.execute(
                """
                SELECT * FROM context_baselines
                WHERE session_id = ? AND thread_id = ?
                ORDER BY sequence DESC LIMIT 1
                """,
                (baseline.session_id, baseline.thread_id),
            ).fetchone()
            previous_id = None if previous_row is None else str(previous_row["id"])
            if baseline.previous_baseline_id not in {None, previous_id}:
                raise ConflictError("Context baseline predecessor is stale")
            persisted = baseline.model_copy(update={"previous_baseline_id": previous_id})
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO context_baselines(
                        id, session_id, thread_id, item_cursor_end, operation,
                        compaction_id, previous_baseline_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        persisted.id,
                        persisted.session_id,
                        persisted.thread_id,
                        persisted.item_cursor_end,
                        persisted.operation.value,
                        persisted.compaction_id,
                        persisted.previous_baseline_id,
                        persisted.created_at.isoformat(),
                    ),
                ).lastrowid
            except sqlite3.IntegrityError as exc:
                raise ConflictError("Context baseline scope is invalid") from exc
            if cursor is None:
                raise RuntimeError("Context baseline insert did not produce a cursor")
            return persisted.model_copy(update={"cursor": int(cursor)})

    def get_active_context_baseline(
        self,
        session_id: str,
        thread_id: str,
    ) -> ContextBaseline | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM context_baselines
                WHERE session_id = ? AND thread_id = ?
                ORDER BY sequence DESC LIMIT 1
                """,
                (session_id, thread_id),
            ).fetchone()
        return None if row is None else self._context_baseline_from_row(row)

    def list_context_baselines(
        self,
        session_id: str,
        thread_id: str,
        *,
        after_cursor: int | None = None,
        limit: int = 100,
    ) -> list[ContextBaseline]:
        cursor, page_limit = self._validate_cursor_page(after_cursor, limit)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM context_baselines
                WHERE session_id = ? AND thread_id = ? AND sequence > ?
                ORDER BY sequence LIMIT ?
                """,
                (session_id, thread_id, cursor, page_limit),
            ).fetchall()
        return [self._context_baseline_from_row(row) for row in rows]

    @staticmethod
    def _context_baseline_from_row(row: sqlite3.Row) -> ContextBaseline:
        return ContextBaseline(
            id=row["id"],
            cursor=int(row["sequence"]),
            session_id=row["session_id"],
            thread_id=row["thread_id"],
            item_cursor_end=int(row["item_cursor_end"]),
            operation=ContextBaselineOperation(row["operation"]),
            compaction_id=row["compaction_id"],
            previous_baseline_id=row["previous_baseline_id"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _context_baseline_payload(baseline: ContextBaseline) -> dict[str, Any]:
        return {
            "session_id": baseline.session_id,
            "thread_id": baseline.thread_id,
            "item_cursor_end": baseline.item_cursor_end,
            "operation": baseline.operation.value,
            "compaction_id": baseline.compaction_id,
            "previous_baseline_id": baseline.previous_baseline_id,
        }

    def create_review_run(self, run: ReviewRun) -> ReviewRun:
        if run.cursor is not None or run.status is not ReviewRunStatus.RUNNING:
            raise ValueError("a new Review run must start running without a cursor")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if (
                run.thread_id is not None
                and connection.execute(
                    "SELECT 1 FROM threads WHERE id = ? AND workspace_ref = ?",
                    (run.thread_id, run.workspace_ref),
                ).fetchone()
                is None
            ):
                raise ConflictError("Review Thread workspace scope is invalid")
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO review_runs(
                        id, session_id, thread_id, workspace_ref, scope, status,
                        artifact_id, error_code, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
                    """,
                    (
                        run.id,
                        run.session_id,
                        run.thread_id,
                        run.workspace_ref,
                        run.scope,
                        run.status.value,
                        run.created_at.isoformat(),
                        run.updated_at.isoformat(),
                    ),
                ).lastrowid
            except sqlite3.IntegrityError as exc:
                raise ConflictError("Review run identity or scope is invalid") from exc
            if cursor is None:
                raise RuntimeError("Review run insert did not produce a cursor")
            return run.model_copy(update={"cursor": int(cursor)})

    def get_review_run(self, run_id: str) -> ReviewRun:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM review_runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"Review run not found: {run_id}")
            return self._review_run_from_row(connection, row)

    def finish_review_run(
        self,
        run_id: str,
        *,
        artifact_id: str | None = None,
        error_code: str | None = None,
    ) -> ReviewRun:
        if (artifact_id is None) == (error_code is None):
            raise ValueError("Review completion requires exactly one result")
        status = ReviewRunStatus.COMPLETED if artifact_id is not None else ReviewRunStatus.FAILED
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM review_runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"Review run not found: {run_id}")
            current = self._review_run_from_row(connection, row)
            if current.status is not ReviewRunStatus.RUNNING:
                if (
                    current.status is status
                    and current.artifact_id == artifact_id
                    and current.error_code == error_code
                ):
                    return current
                raise ConflictError("Review run is already terminal")
            connection.execute(
                """
                UPDATE review_runs
                SET status = ?, artifact_id = ?, error_code = ?, updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (status.value, artifact_id, error_code, now.isoformat(), run_id),
            )
            updated = connection.execute(
                "SELECT * FROM review_runs WHERE id = ?", (run_id,)
            ).fetchone()
            assert updated is not None
            return self._review_run_from_row(connection, updated)

    def update_review_run(self, review_run_id: str, **changes: Any) -> ReviewRun:
        allowed = {"status", "artifact_id", "error_code"}
        if not changes or set(changes) - allowed:
            raise ValueError("Review run update contains unsupported fields")
        raw_status = changes.get("status")
        status = None if raw_status is None else ReviewRunStatus(raw_status)
        artifact_id = changes.get("artifact_id")
        error_code = changes.get("error_code")
        if status is ReviewRunStatus.RUNNING:
            raise ValueError("Review run cannot transition back to running")
        if status is ReviewRunStatus.COMPLETED or artifact_id is not None:
            if artifact_id is None:
                raise ValueError("completed Review run requires artifact_id")
            return self.finish_review_run(review_run_id, artifact_id=str(artifact_id))
        if status is ReviewRunStatus.FAILED or error_code is not None:
            if error_code is None:
                raise ValueError("failed Review run requires error_code")
            return self.finish_review_run(review_run_id, error_code=str(error_code))
        raise ValueError("Review run update does not describe a terminal transition")

    def list_review_runs(
        self,
        *,
        session_id: str | None = None,
        after_cursor: int | None = None,
        limit: int = 100,
    ) -> list[ReviewRun]:
        cursor, page_limit = self._validate_cursor_page(after_cursor, limit)
        where = ["sequence > ?"]
        params: list[Any] = [cursor]
        if session_id is not None:
            where.append("session_id = ?")
            params.append(session_id)
        params.append(page_limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM review_runs WHERE {' AND '.join(where)} ORDER BY sequence LIMIT ?",
                params,
            ).fetchall()
            return [self._review_run_from_row(connection, row) for row in rows]

    @staticmethod
    def _review_run_from_row(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> ReviewRun:
        run = ReviewRun(
            id=row["id"],
            cursor=int(row["sequence"]),
            session_id=row["session_id"],
            thread_id=row["thread_id"],
            workspace_ref=row["workspace_ref"],
            scope=row["scope"],
            status=ReviewRunStatus(row["status"]),
            artifact_id=row["artifact_id"],
            error_code=row["error_code"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
        if (
            run.thread_id is not None
            and connection.execute(
                "SELECT 1 FROM threads WHERE id = ? AND workspace_ref = ?",
                (run.thread_id, run.workspace_ref),
            ).fetchone()
            is None
        ):
            raise ConflictError("Stored Review Thread workspace scope is invalid")
        return run

    def create_btw_sidecar_run(self, run: BTWSidecarRun) -> BTWSidecarRun:
        if run.cursor is not None or run.status is not BTWSidecarStatus.RUNNING:
            raise ValueError("a new BTW Sidecar run must start running without a cursor")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if (
                connection.execute(
                    "SELECT 1 FROM agents WHERE id = ? AND session_id = ?",
                    (run.agent_id, run.session_id),
                ).fetchone()
                is None
            ):
                raise ConflictError("BTW Sidecar Agent session scope is invalid")
            if (
                connection.execute(
                    "SELECT 1 FROM threads WHERE id = ? AND workspace_ref = ?",
                    (run.thread_id, run.workspace_ref),
                ).fetchone()
                is None
            ):
                raise ConflictError("BTW Sidecar Thread workspace scope is invalid")
            latest = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS cursor FROM items WHERE thread_id = ?",
                (run.thread_id,),
            ).fetchone()
            assert latest is not None
            if int(latest["cursor"]) != run.source_item_cursor_end:
                raise ConflictError("BTW Sidecar source cursor is stale")
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO btw_sidecar_runs(
                        id, session_id, agent_id, thread_id, workspace_ref,
                        source_item_cursor_end, prompt, prompt_hash, status, response,
                        response_hash, context_revision_id, promoted_turn_id,
                        promoted_item_id, error_code, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', NULL, NULL, NULL,
                              NULL, NULL, NULL, ?, ?)
                    """,
                    (
                        run.id,
                        run.session_id,
                        run.agent_id,
                        run.thread_id,
                        run.workspace_ref,
                        run.source_item_cursor_end,
                        run.prompt,
                        run.prompt_hash,
                        run.created_at.isoformat(),
                        run.updated_at.isoformat(),
                    ),
                ).lastrowid
            except sqlite3.IntegrityError as exc:
                raise ConflictError("BTW Sidecar identity or scope is invalid") from exc
            if cursor is None:
                raise RuntimeError("BTW Sidecar insert did not produce a cursor")
            return run.model_copy(update={"cursor": int(cursor)})

    def get_btw_sidecar_run(self, run_id: str) -> BTWSidecarRun:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM btw_sidecar_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"BTW Sidecar run not found: {run_id}")
            return self._btw_sidecar_run_from_row(connection, row)

    def finish_btw_sidecar_run(
        self,
        run_id: str,
        *,
        response: str | None = None,
        response_hash: str | None = None,
        context_revision_id: str | None = None,
        error_code: str | None = None,
    ) -> BTWSidecarRun:
        completed = error_code is None
        if completed and (response is None or response_hash is None or context_revision_id is None):
            raise ValueError("completed BTW Sidecar requires response evidence")
        if not completed and any(
            value is not None for value in (response, response_hash, context_revision_id)
        ):
            raise ValueError("failed BTW Sidecar cannot claim response evidence")
        status = BTWSidecarStatus.COMPLETED if completed else BTWSidecarStatus.FAILED
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM btw_sidecar_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"BTW Sidecar run not found: {run_id}")
            current = self._btw_sidecar_run_from_row(connection, row)
            if current.status is not BTWSidecarStatus.RUNNING:
                if (
                    current.status is status
                    and current.response == response
                    and current.response_hash == response_hash
                    and current.context_revision_id == context_revision_id
                    and current.error_code == error_code
                ):
                    return current
                raise ConflictError("BTW Sidecar run is already terminal")
            connection.execute(
                """
                UPDATE btw_sidecar_runs
                SET status = ?, response = ?, response_hash = ?, context_revision_id = ?,
                    error_code = ?, updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (
                    status.value,
                    response,
                    response_hash,
                    context_revision_id,
                    error_code,
                    now.isoformat(),
                    run_id,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM btw_sidecar_runs WHERE id = ?", (run_id,)
            ).fetchone()
            assert updated is not None
            return self._btw_sidecar_run_from_row(connection, updated)

    def update_btw_sidecar_run(self, run_id: str, **changes: Any) -> BTWSidecarRun:
        allowed = {
            "status",
            "response",
            "response_hash",
            "context_revision_id",
            "error_code",
        }
        if not changes or set(changes) - allowed:
            raise ValueError("BTW Sidecar update contains unsupported fields")
        raw_status = changes.get("status")
        status = None if raw_status is None else BTWSidecarStatus(raw_status)
        if status in {None, BTWSidecarStatus.COMPLETED} and changes.get("error_code") is None:
            return self.finish_btw_sidecar_run(
                run_id,
                response=changes.get("response"),
                response_hash=changes.get("response_hash"),
                context_revision_id=changes.get("context_revision_id"),
            )
        if status is BTWSidecarStatus.FAILED or changes.get("error_code") is not None:
            error_code = changes.get("error_code")
            if error_code is None:
                raise ValueError("failed BTW Sidecar requires an error_code")
            return self.finish_btw_sidecar_run(run_id, error_code=str(error_code))
        raise ValueError("BTW Sidecar promotion requires promote_btw_sidecar")

    def promote_btw_sidecar(
        self,
        run_id: str,
        *,
        turn: Turn,
        steering_item: Item,
    ) -> tuple[BTWSidecarRun, Turn, Item, bool]:
        """Atomically append one Steering Item and one promotion event."""

        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM btw_sidecar_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"BTW Sidecar run not found: {run_id}")
            current = self._btw_sidecar_run_from_row(connection, row)
            if current.status is BTWSidecarStatus.PROMOTED:
                assert current.promoted_turn_id is not None
                assert current.promoted_item_id is not None
                turn_row = connection.execute(
                    "SELECT * FROM turns WHERE id = ?", (current.promoted_turn_id,)
                ).fetchone()
                item_row = connection.execute(
                    "SELECT * FROM items WHERE id = ?", (current.promoted_item_id,)
                ).fetchone()
                assert turn_row is not None and item_row is not None
                stored_turn = self._turn_from_row(turn_row)
                stored_item = self._item_from_row(item_row)
                return current, stored_turn, stored_item, False
            if current.status is not BTWSidecarStatus.COMPLETED or current.response is None:
                raise ConflictError("only a completed BTW Sidecar can be promoted")
            self._assert_active_thread(connection, current.thread_id)
            if turn.cursor is not None or turn.position is not None:
                raise ValueError("a promotion Turn cannot provide cursor or position")
            if steering_item.cursor is not None or steering_item.position is not None:
                raise ValueError("a promotion Item cannot provide cursor or position")
            if turn.thread_id != current.thread_id:
                raise ConflictError("promotion Turn belongs to a different Thread")
            if (
                steering_item.thread_id != current.thread_id
                or steering_item.turn_id != turn.id
                or not isinstance(steering_item.payload, SteeringPayload)
                or steering_item.payload.text != current.response
            ):
                raise ConflictError("promotion Item is not the exact Sidecar Steering result")
            turn_position = int(
                connection.execute(
                    "SELECT COALESCE(MAX(position), 0) + 1 FROM turns WHERE thread_id = ?",
                    (current.thread_id,),
                ).fetchone()[0]
            )
            turn = turn.model_copy(update={"position": turn_position})
            turn_cursor = connection.execute(
                """
                INSERT INTO turns(id, thread_id, position, body, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    turn.id,
                    turn.thread_id,
                    turn.position,
                    turn.model_dump_json(),
                    turn.created_at.isoformat(),
                ),
            ).lastrowid
            item_position = int(
                connection.execute(
                    "SELECT COALESCE(MAX(position), 0) + 1 FROM items WHERE thread_id = ?",
                    (current.thread_id,),
                ).fetchone()[0]
            )
            item = steering_item.model_copy(update={"position": item_position})
            body = item.model_dump_json()
            item_cursor = connection.execute(
                """
                INSERT INTO items(
                    id, thread_id, turn_id, position, item_type, body, body_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.id,
                    item.thread_id,
                    item.turn_id,
                    item.position,
                    item.item_type.value,
                    body,
                    hashlib.sha256(body.encode("utf-8")).hexdigest(),
                    item.created_at.isoformat(),
                ),
            ).lastrowid
            connection.execute(
                """
                UPDATE btw_sidecar_runs
                SET status = 'promoted', promoted_turn_id = ?, promoted_item_id = ?, updated_at = ?
                WHERE id = ? AND status = 'completed'
                """,
                (turn.id, item.id, now.isoformat(), run_id),
            )
            connection.execute(
                """
                INSERT INTO btw_sidecar_events(
                    id, sidecar_run_id, event_type, body, created_at
                ) VALUES (?, ?, 'btw.promoted', ?, ?)
                """,
                (
                    new_id("btw_event"),
                    run_id,
                    json.dumps(
                        {
                            "sidecar_run_id": run_id,
                            "turn_id": turn.id,
                            "item_id": item.id,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    now.isoformat(),
                ),
            )
            promoted_row = connection.execute(
                "SELECT * FROM btw_sidecar_runs WHERE id = ?", (run_id,)
            ).fetchone()
            assert promoted_row is not None and turn_cursor is not None and item_cursor is not None
            return (
                self._btw_sidecar_run_from_row(connection, promoted_row),
                turn.model_copy(update={"cursor": int(turn_cursor)}),
                item.model_copy(update={"cursor": int(item_cursor)}),
                True,
            )

    @staticmethod
    def _btw_sidecar_run_from_row(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> BTWSidecarRun:
        run = BTWSidecarRun(
            id=row["id"],
            cursor=int(row["sequence"]),
            session_id=row["session_id"],
            agent_id=row["agent_id"],
            thread_id=row["thread_id"],
            workspace_ref=row["workspace_ref"],
            source_item_cursor_end=int(row["source_item_cursor_end"]),
            prompt=row["prompt"],
            prompt_hash=row["prompt_hash"],
            status=BTWSidecarStatus(row["status"]),
            response=row["response"],
            response_hash=row["response_hash"],
            context_revision_id=row["context_revision_id"],
            promoted_turn_id=row["promoted_turn_id"],
            promoted_item_id=row["promoted_item_id"],
            error_code=row["error_code"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
        if (
            connection.execute(
                "SELECT 1 FROM agents WHERE id = ? AND session_id = ?",
                (run.agent_id, run.session_id),
            ).fetchone()
            is None
        ):
            raise ConflictError("Stored BTW Sidecar Agent session scope is invalid")
        if (
            connection.execute(
                "SELECT 1 FROM threads WHERE id = ? AND workspace_ref = ?",
                (run.thread_id, run.workspace_ref),
            ).fetchone()
            is None
        ):
            raise ConflictError("Stored BTW Sidecar Thread workspace scope is invalid")
        return run

    def append_btw_sidecar_event(self, event: BTWSidecarEvent) -> BTWSidecarEvent:
        if event.cursor is not None:
            raise ValueError("a new BTW Sidecar event cannot provide a cursor")
        with self._connect() as connection:
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO btw_sidecar_events(
                        id, sidecar_run_id, event_type, body, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        event.id,
                        event.sidecar_run_id,
                        event.event_type,
                        json.dumps(
                            event.payload,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        event.created_at.isoformat(),
                    ),
                ).lastrowid
            except sqlite3.IntegrityError as exc:
                raise ConflictError("BTW Sidecar event identity or scope is invalid") from exc
            if cursor is None:
                raise RuntimeError("BTW Sidecar event insert did not produce a cursor")
            return event.model_copy(update={"cursor": int(cursor)})

    def list_btw_sidecar_events(
        self,
        run_id: str,
        *,
        after_cursor: int | None = None,
        limit: int = 100,
    ) -> list[BTWSidecarEvent]:
        cursor, page_limit = self._validate_cursor_page(after_cursor, limit)
        with self._connect() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM btw_sidecar_runs WHERE id = ?", (run_id,)
                ).fetchone()
                is None
            ):
                raise NotFoundError(f"BTW Sidecar run not found: {run_id}")
            rows = connection.execute(
                """
                SELECT * FROM btw_sidecar_events
                WHERE sidecar_run_id = ? AND sequence > ?
                ORDER BY sequence LIMIT ?
                """,
                (run_id, cursor, page_limit),
            ).fetchall()
        return [
            BTWSidecarEvent(
                id=row["id"],
                cursor=int(row["sequence"]),
                sidecar_run_id=row["sidecar_run_id"],
                event_type=row["event_type"],
                payload=json.loads(row["body"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def append_phase1d_command_audit_event(
        self,
        event: Phase1DCommandAuditEvent,
    ) -> Phase1DCommandAuditEvent:
        if event.cursor is not None:
            raise ValueError("a new command audit event cannot provide a cursor")
        with self._connect() as connection:
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO phase1d_command_audit_events(
                        id, command_execution_id, command_kind, event_type,
                        resource_type, resource_id, detail_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.id,
                        event.command_execution_id,
                        event.command_kind.value,
                        event.event_type,
                        event.resource_type,
                        event.resource_id,
                        json.dumps(
                            event.detail,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        event.created_at.isoformat(),
                    ),
                ).lastrowid
            except sqlite3.IntegrityError as exc:
                raise ConflictError("Phase 1D command audit scope is invalid") from exc
            if cursor is None:
                raise RuntimeError("command audit insert did not produce a cursor")
            return event.model_copy(update={"cursor": int(cursor)})

    def list_phase1d_command_audit_events(
        self,
        command_execution_id: str,
        *,
        after_cursor: int | None = None,
        limit: int = 100,
    ) -> list[Phase1DCommandAuditEvent]:
        cursor, page_limit = self._validate_cursor_page(after_cursor, limit)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM phase1d_command_audit_events
                WHERE command_execution_id = ? AND sequence > ?
                ORDER BY sequence LIMIT ?
                """,
                (command_execution_id, cursor, page_limit),
            ).fetchall()
        return [
            Phase1DCommandAuditEvent(
                id=row["id"],
                cursor=int(row["sequence"]),
                command_execution_id=row["command_execution_id"],
                command_kind=SlashCommandKind(row["command_kind"]),
                event_type=row["event_type"],
                resource_type=row["resource_type"],
                resource_id=row["resource_id"],
                detail=json.loads(row["detail_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def _interrupt_running_phase1d_calls(self, connection: sqlite3.Connection) -> None:
        now = utc_now().isoformat()
        connection.execute(
            """
            UPDATE review_runs
            SET status = 'failed', error_code = 'process_interrupted', updated_at = ?
            WHERE status = 'running'
            """,
            (now,),
        )
        interrupted = connection.execute(
            "SELECT id FROM btw_sidecar_runs WHERE status = 'running'"
        ).fetchall()
        connection.execute(
            """
            UPDATE btw_sidecar_runs
            SET status = 'failed', error_code = 'process_interrupted', updated_at = ?
            WHERE status = 'running'
            """,
            (now,),
        )
        for row in interrupted:
            connection.execute(
                """
                INSERT INTO btw_sidecar_events(
                    id, sidecar_run_id, event_type, body, created_at
                ) VALUES (?, ?, 'btw.failed', ?, ?)
                """,
                (
                    new_id("btw_event"),
                    row["id"],
                    json.dumps(
                        {"error_code": "process_interrupted"},
                        separators=(",", ":"),
                    ),
                    now,
                ),
            )

    @staticmethod
    def _build_fts_query(value: str) -> str:
        tokens = re.findall(r"[^\W_]+", value, flags=re.UNICODE)
        parts: list[str] = []
        for token in tokens:
            escaped = token.replace('"', '""')
            parts.append(f'"{escaped}"')
        return " AND ".join(parts)

    @staticmethod
    def _role_scope_matches(
        memory: Memory,
        *,
        role_id: str | None,
        role_name: str | None,
    ) -> bool:
        return bool(
            set(memory.role_scope).intersection(
                candidate for candidate in (role_id, role_name) if candidate is not None
            )
            or "*" in memory.role_scope
        )

    def list_roles(self, *, include_inactive: bool = False) -> list[RolePreset]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT versions.body
                FROM role_heads AS heads
                JOIN role_versions AS versions
                  ON versions.role_id = heads.id
                 AND versions.version = heads.current_version
                ORDER BY versions.created_at, versions.role_id
                """
            ).fetchall()
        roles = [RolePreset.model_validate_json(row["body"]) for row in rows]
        if include_inactive:
            return roles
        return [role for role in roles if role.status is RoleStatus.ACTIVE]

    def _validate_role_model(self, role: RolePreset) -> None:
        profile = self.get_model_profile(role.model_profile_id)
        if role.effort not in profile.supported_efforts:
            raise ValueError(f"effort {role.effort.value!r} is not supported by {profile.name!r}")
