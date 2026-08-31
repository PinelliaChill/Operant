from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest

from operant.domain.actions import (
    ApprovalRequest,
    ApprovalStatus,
    CommandExecution,
    CommandExecutionStatus,
    ToolActionReceipt,
    ToolActionReceiptStatus,
)
from operant.domain.evaluation import EvaluationRun, EvaluationRunEvent
from operant.domain.memory import Memory, MemoryKind, MemoryStatus
from operant.domain.models import ModelProfile, RolePreset, utc_now
from operant.domain.workflow import WorkflowRun
from operant.persistence.sqlite import (
    ActionOutcomeUnknownError,
    ConflictError,
    IdempotencyConflictError,
    MigrationError,
    SQLiteStore,
)

WEEK1_SCHEMA = """
CREATE TABLE model_profiles (id TEXT PRIMARY KEY, body TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE role_heads (id TEXT PRIMARY KEY, current_version INTEGER NOT NULL);
CREATE TABLE role_versions (
    role_id TEXT NOT NULL, version INTEGER NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY (role_id, version), FOREIGN KEY (role_id) REFERENCES role_heads(id)
);
CREATE TABLE sessions (id TEXT PRIMARY KEY, body TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE agents (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, status TEXT NOT NULL,
    body TEXT NOT NULL, created_at TEXT NOT NULL, FOREIGN KEY (session_id) REFERENCES sessions(id)
);
CREATE TABLE events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
    session_id TEXT NOT NULL, agent_id TEXT, event_type TEXT NOT NULL,
    body TEXT NOT NULL, created_at TEXT NOT NULL, FOREIGN KEY (session_id) REFERENCES sessions(id)
);
"""


def _seed_action_scope(store: SQLiteStore):
    profile = store.add_model_profile(
        ModelProfile(
            name="m0-test",
            model_id="m0-test-model",
            base_url="https://example.invalid/v1",
            secret_ref="OPERANT_TEST_KEY",
        )
    )
    role = store.create_role(
        RolePreset(
            name="M0 Coder",
            system_prompt="Test M0 persistence.",
            model_profile_id=profile.id,
        )
    )
    session = store.create_session(role.id)
    agent = store.create_agent(session.id)
    return session, agent


def test_unversioned_week1_and_full_week1_to_4_databases_upgrade_with_data(
    tmp_path: Path,
) -> None:
    week1_path = tmp_path / "week1.sqlite3"
    profile = ModelProfile(
        id="model_legacy",
        name="legacy",
        model_id="legacy-model",
        base_url="https://example.invalid/v1",
        secret_ref="OPERANT_LEGACY_KEY",
    )
    with sqlite3.connect(week1_path) as connection:
        connection.executescript(WEEK1_SCHEMA)
        connection.execute(
            "INSERT INTO model_profiles(id, body, created_at) VALUES (?, ?, ?)",
            (profile.id, profile.model_dump_json(), profile.created_at.isoformat()),
        )

    week1 = SQLiteStore(week1_path)
    assert week1.schema_version() == 0
    week1.initialize()
    assert week1.schema_version() == 7
    assert week1.get_model_profile(profile.id) == profile

    full_path = tmp_path / "week1-4.sqlite3"
    full = SQLiteStore(full_path)
    full.migrate(2)
    with sqlite3.connect(full_path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        timestamp = "2026-08-27T00:00:00+00:00"
        connection.execute(
            "INSERT INTO evaluation_suites VALUES (?, '{}', NULL, 'ready', ?)",
            ("suite_legacy", timestamp),
        )
        connection.execute(
            "INSERT INTO evaluation_cases VALUES (?, ?, 1, '{}', ?)",
            ("case_legacy", "suite_legacy", timestamp),
        )
        connection.execute(
            "INSERT INTO evaluation_variants VALUES (?, ?, 1, '{}', ?)",
            ("variant_legacy", "suite_legacy", timestamp),
        )
        connection.execute(
            "INSERT INTO evaluation_runs VALUES (?, ?, '{}', 'pending', 'sequential', ?, ?)",
            ("run_legacy", "suite_legacy", timestamp, timestamp),
        )
        connection.execute(
            "INSERT INTO evaluation_results VALUES (?, ?, ?, ?, 1, 'pending', '{}', ?, ?)",
            (
                "result_legacy",
                "run_legacy",
                "case_legacy",
                "variant_legacy",
                timestamp,
                timestamp,
            ),
        )
        connection.executescript(
            """
            ALTER TABLE evaluation_results RENAME TO evaluation_results_current;
            CREATE TABLE evaluation_results (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                case_id TEXT NOT NULL,
                variant_id TEXT NOT NULL,
                repetition INTEGER NOT NULL,
                status TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(run_id, case_id, variant_id, repetition),
                FOREIGN KEY (run_id) REFERENCES evaluation_runs(id),
                FOREIGN KEY (case_id) REFERENCES evaluation_cases(id),
                FOREIGN KEY (variant_id) REFERENCES evaluation_variants(id)
            );
            INSERT INTO evaluation_results(
                id, run_id, case_id, variant_id, repetition, status, body, created_at
            )
            SELECT
                id, run_id, case_id, variant_id, repetition, status, body, created_at
            FROM evaluation_results_current;
            DROP TABLE evaluation_results_current;
            CREATE INDEX idx_evaluation_results_run_status_created
                ON evaluation_results(run_id, status, created_at);
            """
        )
    workflow = full.create_workflow_run(
        WorkflowRun(
            id="workflow_legacy",
            task="Preserve this legacy workflow",
            workspace=str(tmp_path),
            planner_role_id="planner",
            coder_role_id="coder",
            reviewer_role_id="reviewer",
        )
    )
    memory = full.create_memory(
        Memory(
            id="memory_legacy",
            kind=MemoryKind.PROJECT,
            content="Legacy FTS migration marker",
            project_scope=str(tmp_path),
            status=MemoryStatus.ACTIVE,
        )
    )
    with sqlite3.connect(full_path) as connection:
        connection.execute("DROP TABLE schema_migrations")

    full.initialize()
    assert full.schema_version() == 7
    assert full.get_workflow_run(workflow.id) == workflow
    assert full.get_memory(memory.id) == memory
    assert full.search_memories("migration marker", project_scope=str(tmp_path)) == [memory]
    with sqlite3.connect(full_path) as connection:
        result = connection.execute(
            "SELECT id, created_at, updated_at FROM evaluation_results WHERE id = ?",
            ("result_legacy",),
        ).fetchone()
        updated_at = next(
            row
            for row in connection.execute("PRAGMA table_info(evaluation_results)").fetchall()
            if row[1] == "updated_at"
        )
    assert result == ("result_legacy", timestamp, timestamp)
    assert updated_at[3] == 1


def test_unversioned_schema_version_rejects_fake_week1_shape(tmp_path: Path) -> None:
    path = tmp_path / "fake-week1.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(WEEK1_SCHEMA)
        connection.executescript(
            """
            ALTER TABLE events RENAME TO events_valid;
            CREATE TABLE events (
                sequence INTEGER,
                id TEXT,
                session_id TEXT,
                agent_id TEXT,
                event_type TEXT,
                body TEXT,
                created_at TEXT
            );
            DROP TABLE events_valid;
            """
        )
    with pytest.raises(MigrationError):
        SQLiteStore(path).schema_version()


def test_migrations_are_atomic_reject_corruption_and_support_explicit_empty_rollback(
    tmp_path: Path,
) -> None:
    atomic_path = tmp_path / "atomic.sqlite3"
    SQLiteStore(atomic_path).migrate(2)

    class BrokenV3Store(SQLiteStore):
        def _upgrade_v3(self, connection: sqlite3.Connection) -> None:
            super()._upgrade_v3(connection)
            raise RuntimeError("injected migration failure")

    with pytest.raises(RuntimeError, match="injected migration failure"):
        BrokenV3Store(atomic_path).migrate()
    assert SQLiteStore(atomic_path).schema_version() == 2
    with sqlite3.connect(atomic_path) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='command_executions'"
            ).fetchone()
            is None
        )

    empty_atomic_path = tmp_path / "empty-atomic.sqlite3"
    with pytest.raises(RuntimeError, match="injected migration failure"):
        BrokenV3Store(empty_atomic_path).migrate()
    with sqlite3.connect(empty_atomic_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert tables == set()

    store = SQLiteStore(tmp_path / "rollback.sqlite3")
    store.initialize()
    assert store.rollback(2, isolated=True) == 2
    assert store.migrate() == 7
    store.reserve_command_execution(
        CommandExecution(
            command_type="POST:/v1/test",
            idempotency_key="populated",
            action_hash="a" * 64,
        )
    )
    with pytest.raises(MigrationError, match="audit data"):
        store.rollback(2, isolated=True)
    with pytest.raises(MigrationError, match="explicitly isolated"):
        store.rollback(2)

    for name, mutation, expected in (
        (
            "checksum",
            "UPDATE schema_migrations SET checksum='bad' WHERE version=2",
            "checksum mismatch",
        ),
        ("gap", "DELETE FROM schema_migrations WHERE version=2", "version gap"),
        (
            "future",
            "INSERT INTO schema_migrations VALUES (8, 'future', 'future', 'now')",
            "newer than this build",
        ),
    ):
        path = tmp_path / f"{name}.sqlite3"
        candidate = SQLiteStore(path)
        candidate.initialize()
        with sqlite3.connect(path) as connection:
            connection.execute(mutation)
        with pytest.raises(MigrationError, match=expected):
            candidate.schema_version()


def test_concurrent_migration_to_same_target_is_repeatable(tmp_path: Path) -> None:
    path = tmp_path / "concurrent.sqlite3"

    def migrate() -> int:
        return SQLiteStore(path).migrate()

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(lambda _item: migrate(), range(2))) == [7, 7]
    assert SQLiteStore(path).schema_version() == 7


def test_each_migration_and_downgrade_step_revalidates_the_manifest(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "step-validation.sqlite3")
    assert store.migrate(1) == 1
    assert store.schema_version() == 1
    assert store.migrate(2) == 2
    assert store.schema_version() == 2
    assert store.migrate(3) == 3
    assert store.schema_version() == 3
    assert store.rollback(2, isolated=True) == 2
    assert store.schema_version() == 2
    assert store.migrate(3) == 3
    assert store.migrate(4) == 4
    assert store.schema_version() == 4
    assert store.rollback(3, isolated=True) == 3
    assert store.migrate(4) == 4
    assert store.migrate(5) == 5
    assert store.schema_version() == 5
    assert store.rollback(4, isolated=True) == 4
    assert store.migrate(5) == 5
    assert store.migrate(6) == 6
    assert store.schema_version() == 6
    assert store.rollback(5, isolated=True) == 5
    assert store.migrate(6) == 6
    assert store.migrate(7) == 7
    assert store.schema_version() == 7
    assert store.rollback(6, isolated=True) == 6
    assert store.migrate(7) == 7

    migrations = store._migrations()
    manifest = json.loads(migrations[-1].manifest)
    assert manifest["columns"]["schema_migrations"]["version"] == {
        "type": "INTEGER",
        "not_null": False,
    }
    assert manifest["columns"]["evaluation_results"]["updated_at"] == {
        "type": "TEXT",
        "not_null": True,
    }
    assert "evaluation_run_events" in manifest["ddl"]
    assert "table:memory_fts_data" in manifest["objects"]
    assert len(migrations[-1].checksum) == 64
    assert {
        migration.version: hashlib.sha256(migration.manifest.encode("utf-8")).hexdigest()
        for migration in migrations
    } == SQLiteStore._FROZEN_MANIFEST_SHA256
    assert {
        migration.version: migration.checksum for migration in migrations
    } == SQLiteStore._FROZEN_MIGRATION_CHECKSUMS


def test_exact_preview_histories_upgrade_but_unknown_or_drifted_preview_is_rejected(
    tmp_path: Path,
) -> None:
    preview_path = tmp_path / "known-preview.sqlite3"
    preview = SQLiteStore(preview_path)
    preview.migrate(3)
    profile = preview.add_model_profile(
        ModelProfile(
            id="model_preview_preserved",
            name="preview-preserved",
            model_id="preview-preserved",
            base_url="https://example.invalid/v1",
            secret_ref="OPERANT_PREVIEW_KEY",
        )
    )
    with sqlite3.connect(preview_path) as connection:
        connection.execute("DROP INDEX idx_evaluation_run_events_run_sequence")
        connection.execute("DROP TABLE evaluation_run_events")
        for version, name, checksum in SQLiteStore._THREE_STEP_PREVIEW_HISTORY:
            connection.execute(
                "UPDATE schema_migrations SET name = ?, checksum = ? WHERE version = ?",
                (name, checksum, version),
            )

    upgraded = SQLiteStore(preview_path)
    upgraded.initialize()
    assert upgraded.schema_version() == 7
    assert upgraded.get_model_profile(profile.id) == profile
    with sqlite3.connect(preview_path) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='evaluation_run_events'"
        ).fetchone() == (1,)

    rework_path = tmp_path / "known-manifest-rework-preview.sqlite3"
    rework = SQLiteStore(rework_path)
    rework.migrate(3)
    with sqlite3.connect(rework_path) as connection:
        for version, name, checksum in SQLiteStore._MANIFEST_REWORK_PREVIEW_HISTORY:
            connection.execute(
                "UPDATE schema_migrations SET name = ?, checksum = ? WHERE version = ?",
                (name, checksum, version),
            )
    SQLiteStore(rework_path).initialize()
    assert SQLiteStore(rework_path).schema_version() == 7

    unknown_path = tmp_path / "unknown-preview.sqlite3"
    unknown = SQLiteStore(unknown_path)
    unknown.initialize()
    with sqlite3.connect(unknown_path) as connection:
        connection.execute(
            "UPDATE schema_migrations SET checksum = ? WHERE version = 1",
            ("f" * 64,),
        )
    with pytest.raises(MigrationError, match="checksum mismatch"):
        unknown.migrate()

    drifted_path = tmp_path / "drifted-preview.sqlite3"
    drifted = SQLiteStore(drifted_path)
    drifted.initialize()
    with sqlite3.connect(drifted_path) as connection:
        connection.execute("DROP INDEX idx_evaluation_run_events_run_sequence")
        connection.execute("DROP TABLE evaluation_run_events")
        connection.execute("DROP INDEX idx_events_session_sequence")
        for version, name, checksum in SQLiteStore._THREE_STEP_PREVIEW_HISTORY:
            connection.execute(
                "UPDATE schema_migrations SET name = ?, checksum = ? WHERE version = ?",
                (name, checksum, version),
            )
    with pytest.raises(MigrationError):
        drifted.initialize()


@pytest.mark.parametrize(
    "mutation",
    [
        "fake_migration_table",
        "extra_table",
        "extra_view",
        "extra_trigger",
        "extra_index",
        "duplicate_unique",
        "drop_required_index",
        "drop_fts_data",
        "drop_fts_idx",
        "fts_options",
        "remove_autoincrement",
        "remove_foreign_key",
        "extra_default",
        "orphan_row",
    ],
)
def test_schema_manifest_rejects_structural_and_data_drift(
    tmp_path: Path,
    mutation: str,
) -> None:
    def corrupt(path: Path) -> None:
        SQLiteStore(path).initialize()
        with sqlite3.connect(path) as connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            if mutation == "fake_migration_table":
                connection.executescript(
                    """
                    ALTER TABLE schema_migrations RENAME TO schema_migrations_valid;
                    CREATE TABLE schema_migrations (
                        version INTEGER, name TEXT, checksum TEXT, applied_at TEXT
                    );
                    INSERT INTO schema_migrations SELECT * FROM schema_migrations_valid;
                    DROP TABLE schema_migrations_valid;
                    """
                )
            elif mutation == "extra_table":
                connection.execute("CREATE TABLE unexpected_table(id TEXT)")
            elif mutation == "extra_view":
                connection.execute("CREATE VIEW unexpected_view AS SELECT id FROM sessions")
            elif mutation == "extra_trigger":
                connection.execute(
                    "CREATE TRIGGER unexpected_trigger AFTER INSERT ON sessions BEGIN SELECT 1; END"
                )
            elif mutation == "extra_index":
                connection.execute("CREATE INDEX unexpected_index ON sessions(created_at)")
            elif mutation == "duplicate_unique":
                connection.execute("CREATE UNIQUE INDEX duplicate_event_id ON events(id)")
            elif mutation == "drop_required_index":
                connection.execute("DROP INDEX idx_events_session_sequence")
            elif mutation == "drop_fts_data":
                if hasattr(connection, "setconfig"):
                    connection.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, False)
                connection.execute("PRAGMA writable_schema = ON")
                connection.execute(
                    "DELETE FROM sqlite_master WHERE type='table' AND name='memory_fts_data'"
                )
                connection.execute("PRAGMA writable_schema = OFF")
            elif mutation == "drop_fts_idx":
                if hasattr(connection, "setconfig"):
                    connection.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, False)
                connection.execute("PRAGMA writable_schema = ON")
                connection.execute(
                    "DELETE FROM sqlite_master WHERE type='table' AND name='memory_fts_idx'"
                )
                connection.execute("PRAGMA writable_schema = OFF")
            elif mutation == "fts_options":
                connection.execute("DROP TABLE memory_fts")
                connection.execute(
                    "CREATE VIRTUAL TABLE memory_fts USING fts5("
                    "memory_id UNINDEXED, version UNINDEXED, content, source_task, "
                    "project_scope, tokenize='porter')"
                )
            elif mutation == "remove_autoincrement":
                connection.executescript(
                    """
                    DROP INDEX idx_evaluation_run_events_run_sequence;
                    DROP TABLE evaluation_run_events;
                    CREATE TABLE evaluation_run_events (
                        sequence INTEGER PRIMARY KEY,
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
                    """
                )
            elif mutation == "remove_foreign_key":
                connection.executescript(
                    """
                    DROP TABLE approval_decisions;
                    CREATE TABLE approval_decisions (
                        id TEXT PRIMARY KEY,
                        approval_id TEXT UNIQUE NOT NULL,
                        approved INTEGER NOT NULL CHECK (approved IN (0, 1)),
                        decided_by TEXT NOT NULL,
                        reason_code TEXT,
                        decided_at TEXT NOT NULL
                    );
                    """
                )
            elif mutation == "extra_default":
                connection.executescript(
                    """
                    DROP TABLE approval_decisions;
                    CREATE TABLE approval_decisions (
                        id TEXT PRIMARY KEY,
                        approval_id TEXT UNIQUE NOT NULL,
                        approved INTEGER NOT NULL DEFAULT 0 CHECK (approved IN (0, 1)),
                        decided_by TEXT NOT NULL,
                        reason_code TEXT,
                        decided_at TEXT NOT NULL,
                        FOREIGN KEY (approval_id) REFERENCES approval_requests(id)
                    );
                    """
                )
            else:
                connection.execute(
                    "INSERT INTO agents(id, session_id, status, body, created_at) "
                    "VALUES ('agent_orphan', 'session_missing', 'idle', '{}', 'now')"
                )

    for operation in ("schema_version", "migrate"):
        path = tmp_path / f"{mutation}-{operation}.sqlite3"
        corrupt(path)
        with pytest.raises(MigrationError):
            getattr(SQLiteStore(path), operation)()


def test_tool_and_command_receipts_replay_conflict_and_restart_unknown(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "receipts.sqlite3")
    store.initialize()
    session, agent = _seed_action_scope(store)
    receipt = ToolActionReceipt(
        scope="test-scope",
        session_id=session.id,
        agent_id=agent.id,
        idempotency_key="tool-call-1",
        action_hash="a" * 64,
        command_name="apply_patch",
    )
    reserved, created = store.reserve_tool_action(receipt)
    assert created and reserved.status is ToolActionReceiptStatus.IN_PROGRESS
    completed = store.complete_tool_action(
        reserved.id,
        action_hash=reserved.action_hash,
        result_json='{"created":true}',
    )
    replay, created = store.reserve_tool_action(receipt.model_copy(update={"id": "other"}))
    assert not created and replay == completed
    conflicting_bindings = (
        {"scope": "other-scope"},
        {"idempotency_key": "other-key"},
        {"action_hash": "b" * 64},
        {"session_id": "other-session"},
        {"agent_id": "other-agent"},
        {"command_name": "run_command"},
    )
    for update in conflicting_bindings:
        with pytest.raises(IdempotencyConflictError):
            store.reserve_tool_action(receipt.model_copy(update=update))

    failed_receipt = receipt.model_copy(
        update={"id": "action_failed", "idempotency_key": "tool-call-failed"}
    )
    failed_receipt, _ = store.reserve_tool_action(failed_receipt)
    failed = store.fail_tool_action(
        failed_receipt.id,
        action_hash=failed_receipt.action_hash,
        error_code="tool_failed",
        result_json='{"error":"safe failure"}',
    )
    failed_replay, created = store.reserve_tool_action(failed_receipt)
    assert not created and failed_replay == failed
    assert failed_replay.status is ToolActionReceiptStatus.FAILED

    unknown = receipt.model_copy(update={"id": "action_unknown", "idempotency_key": "tool-call-2"})
    store.reserve_tool_action(unknown)
    SQLiteStore(store.path).initialize()
    with pytest.raises(ActionOutcomeUnknownError):
        store.reserve_tool_action(unknown)

    command = CommandExecution(
        command_type="POST:/v1/test",
        idempotency_key="command-1",
        action_hash="c" * 64,
    )
    command, _ = store.reserve_command_execution(command)
    completed_command = store.complete_command_execution(
        command.id,
        response_json='{"ok":true}',
        http_status=200,
    )
    command_replay, created = store.reserve_command_execution(command)
    assert not created and command_replay == completed_command
    with pytest.raises(IdempotencyConflictError):
        store.reserve_command_execution(command.model_copy(update={"action_hash": "e" * 64}))

    unknown_command = command.model_copy(
        update={"id": "command_unknown", "idempotency_key": "command-unknown"}
    )
    store.reserve_command_execution(unknown_command)
    SQLiteStore(store.path).initialize()
    assert (
        store.get_command_execution(unknown_command.id).status
        is CommandExecutionStatus.MANUAL_RECONCILE_REQUIRED
    )


def test_new_tool_receipt_requires_clean_initial_state_and_agent_session_binding(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "receipt-boundary.sqlite3")
    store.initialize()
    session, agent = _seed_action_scope(store)
    other_session, other_agent = _seed_action_scope(store)
    receipt = ToolActionReceipt(
        scope="receipt-boundary",
        session_id=session.id,
        agent_id=agent.id,
        idempotency_key="receipt-boundary-call",
        action_hash="a" * 64,
        command_name="apply_patch",
    )

    invalid_initial_states = (
        {"status": ToolActionReceiptStatus.COMPLETED},
        {"result_json": '{"forged":true}'},
        {"error_code": "forged_error"},
        {"completed_at": utc_now()},
    )
    for index, update in enumerate(invalid_initial_states):
        candidate = receipt.model_copy(
            update={
                "id": f"invalid_initial_{index}",
                "idempotency_key": f"invalid-initial-{index}",
                **update,
            }
        )
        with pytest.raises(ConflictError, match="must start in_progress"):
            store.reserve_tool_action(candidate)

    with pytest.raises(ConflictError, match="does not exist"):
        store.reserve_tool_action(
            receipt.model_copy(
                update={
                    "id": "missing_agent_receipt",
                    "idempotency_key": "missing-agent",
                    "agent_id": "agent_missing",
                }
            )
        )
    with pytest.raises(ConflictError, match="does not belong"):
        store.reserve_tool_action(
            receipt.model_copy(
                update={
                    "id": "wrong_session_receipt",
                    "idempotency_key": "wrong-session",
                    "agent_id": other_agent.id,
                }
            )
        )
    assert other_session.id != session.id


def test_approval_cas_expiry_audit_restart_and_exact_action_hash(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "approvals.sqlite3")
    store.initialize()
    session, agent = _seed_action_scope(store)
    receipt, _ = store.reserve_tool_action(
        ToolActionReceipt(
            scope="approval-scope",
            session_id=session.id,
            agent_id=agent.id,
            idempotency_key="approval-tool",
            action_hash="d" * 64,
            command_name="run_command",
        )
    )
    approval = ApprovalRequest(
        session_id=session.id,
        agent_id=agent.id,
        tool_action_receipt_id=receipt.id,
        tool_call_id="approval-tool",
        action_hash=receipt.action_hash,
        category="network",
        detail_summary="run_command category=network; executable=curl; argument_count=2",
    )
    assert store.create_approval_request(approval) == approval
    assert store.get_approval_request(session.id, approval.tool_call_id) == approval
    assert store.list_approval_requests(session.id) == [approval]
    with pytest.raises(IdempotencyConflictError, match="different action"):
        store.create_approval_request(approval.model_copy(update={"action_hash": "e" * 64}))

    decided, decision, changed = store.decide_approval(
        session.id, approval.tool_call_id, approved=True
    )
    assert changed and decision.approved and decided.status is ApprovalStatus.APPROVED
    assert store.decide_approval(session.id, approval.tool_call_id, approved=True)[2] is False
    with pytest.raises(ConflictError, match="opposite"):
        store.decide_approval(session.id, approval.tool_call_id, approved=False)
    assert [event.event_type for event in store.list_approval_audit_events(approval.id)] == [
        "approval.requested",
        "approval.decided",
    ]

    restarted = SQLiteStore(store.path)
    restarted.initialize()
    assert restarted.get_approval_decision(approval.id) == decision

    expiring_receipt, _ = restarted.reserve_tool_action(
        receipt.model_copy(update={"id": "action_expiring", "idempotency_key": "expiring-tool"})
    )
    expiring = approval.model_copy(
        update={
            "id": "approval_expiring",
            "tool_action_receipt_id": expiring_receipt.id,
            "tool_call_id": "expiring-tool",
            "expires_at": utc_now() - timedelta(seconds=1),
        }
    )
    restarted.create_approval_request(expiring)
    assert (
        restarted.get_approval_request(session.id, expiring.tool_call_id).status
        is ApprovalStatus.EXPIRED
    )
    assert [event.event_type for event in restarted.list_approval_audit_events(expiring.id)] == [
        "approval.requested",
        "approval.expired",
    ]
    with pytest.raises(ConflictError, match="not pending"):
        restarted.decide_approval(session.id, expiring.tool_call_id, approved=True)


def test_approval_creation_requires_pending_exact_in_progress_receipt(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "approval-boundary.sqlite3")
    store.initialize()
    session, agent = _seed_action_scope(store)
    other_session, other_agent = _seed_action_scope(store)
    receipt, _ = store.reserve_tool_action(
        ToolActionReceipt(
            scope="approval-boundary",
            session_id=session.id,
            agent_id=agent.id,
            idempotency_key="approval-boundary-call",
            action_hash="a" * 64,
            command_name="apply_patch",
        )
    )
    approval = ApprovalRequest(
        session_id=session.id,
        agent_id=agent.id,
        tool_action_receipt_id=receipt.id,
        tool_call_id=receipt.idempotency_key,
        action_hash=receipt.action_hash,
        category="workspace_write",
        detail_summary="apply_patch requires approval",
    )

    with pytest.raises(ConflictError, match="pending"):
        store.create_approval_request(
            approval.model_copy(update={"status": ApprovalStatus.APPROVED})
        )
    with pytest.raises(ConflictError, match="existing tool action receipt"):
        store.create_approval_request(
            approval.model_copy(update={"tool_action_receipt_id": "action_missing"})
        )
    with pytest.raises(ConflictError, match="does not match"):
        store.create_approval_request(approval.model_copy(update={"action_hash": "b" * 64}))
    with pytest.raises(ConflictError, match="does not match"):
        store.create_approval_request(
            approval.model_copy(update={"tool_call_id": "different-tool-call"})
        )
    with pytest.raises(ConflictError, match="does not match"):
        store.create_approval_request(
            approval.model_copy(update={"session_id": other_session.id, "agent_id": other_agent.id})
        )

    completed_receipt, _ = store.reserve_tool_action(
        receipt.model_copy(
            update={
                "id": "action_already_completed",
                "scope": "approval-boundary-completed",
                "idempotency_key": "approval-completed-call",
            }
        )
    )
    store.complete_tool_action(
        completed_receipt.id,
        action_hash=completed_receipt.action_hash,
        result_json='{"ok":true}',
    )
    with pytest.raises(ConflictError, match="not awaiting approval"):
        store.create_approval_request(
            approval.model_copy(
                update={
                    "id": "approval_completed_receipt",
                    "tool_action_receipt_id": completed_receipt.id,
                    "tool_call_id": completed_receipt.idempotency_key,
                }
            )
        )

    persisted = store.create_approval_request(approval)
    assert persisted.status is ApprovalStatus.PENDING
    assert store.get_tool_action_receipt(receipt.id) == receipt

    store.complete_tool_action(
        receipt.id,
        action_hash=receipt.action_hash,
        result_json='{"ok":true}',
    )
    with pytest.raises(ConflictError, match="not awaiting approval"):
        store.create_approval_request(approval)


def test_evaluation_events_use_live_sqlite_cursor_and_open_interval_replay(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "evaluation-events.sqlite3")
    store.initialize()
    run = EvaluationRun(id="eval_run_cursor", suite_id="suite_cursor")
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "INSERT INTO evaluation_suites(id, body, experiment, status, created_at) "
            "VALUES (?, '{}', NULL, 'ready', ?)",
            (run.suite_id, run.created_at.isoformat()),
        )
        connection.execute(
            "INSERT INTO evaluation_runs(id, suite_id, body, status, execution_strategy, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run.id,
                run.suite_id,
                run.model_dump_json(),
                run.status.value,
                run.execution_strategy.value,
                run.created_at.isoformat(),
                run.updated_at.isoformat(),
            ),
        )
    first = store.append_evaluation_event(
        EvaluationRunEvent(evaluation_run_id=run.id, event_type="evaluation.run_started")
    )
    second = store.append_evaluation_event(
        EvaluationRunEvent(evaluation_run_id=run.id, event_type="evaluation.run_finished")
    )
    assert first.cursor is not None and second.cursor is not None
    assert second.cursor > first.cursor
    assert store.list_evaluation_events(run.id, after_cursor=first.cursor) == [second]
