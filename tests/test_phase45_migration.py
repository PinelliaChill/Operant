from __future__ import annotations

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from operant.domain.models import ModelProfile
from operant.persistence.sqlite import MigrationError, SQLiteStore

V11_TABLES = {
    "skill_candidates",
    "mcp_servers",
    "mcp_tool_snapshots",
    "mcp_lifecycle_events",
    "schedule_definitions",
    "schedule_heads",
    "run_requests",
    "scheduler_authority_leases",
    "job_leases",
    "job_attempts",
    "scheduler_graph_dispatches",
}

V12_TABLES = {
    "mcp_action_receipts",
    "mcp_server_start_leases",
    "mcp_stdio_sandboxes",
    "phase45_approval_requests",
}

FROZEN_V1_TO_V10_MANIFESTS = {
    1: "9efa030568ef8f28749732f82f0023a548d4ff3af708f86be9f8d3a70037ef18",
    2: "1c1d79405a9f422aca4a84a9bd5e45b1647cb16d0c8b496f906932272fd05e98",
    3: "2ac49a0e18c49ccfbfce434425bb7dd09f39710f190b635d3908af96512d9b34",
    4: "dd860b7b4b448ab4296c1e7803a0fcb90a1e5f27015066916cf871e455373f1c",
    5: "dc6ce273aa869446a520af09fb19335369dab273ffd38789d38298356e5d9df6",
    6: "e12f7993df336c97f2a97532615abfcda457bfba4b10d902223634417e59d373",
    7: "b8516d3a7deec9a93867c45f323992238b17968829001c6af2cf61831fe70df4",
    8: "f3295d7911214ce19f2a6dc7fda63e21eebfe79c7cb4b3a16934ef40297da11b",
    9: "884512be9442684acd9b76a9f478b658e5b3d9fb3a576c52a0fe893baab769d5",
    10: "cc99b8ac7b8f7b7898d807adb3252ea993a9c435ce8ac31b1109183776c32680",
}

FROZEN_V1_TO_V10_CHECKSUMS = {
    1: "08c9d964cf48e432baa70c5730e09577c8fd3c3da32ded12a1d06eb6d4af82c9",
    2: "f560a54b3361b717b8b0118efeb277b9869d66aa4528eb40015cbe571d9a9eef",
    3: "9be47a848c184b5f1ab81540abfc764dcae2a4b054ff4128d3ed153635d5f709",
    4: "7a787a9ce4262293dfd5a0ad524f7f50c245722abef763a64ea82a2eaed1fc14",
    5: "ec6dad28422980314a01fa56a1a2d28e1b2bee4744eb76d28240124a4a112490",
    6: "5430fb415059679846e3f0c18a3b6c998573a053b81c1b4ce4a67719f6f60f66",
    7: "15496ba9e4cde4e1dd622abdc141ca2dc63f5b155c3b2eb57a24472c9df3e06b",
    8: "bfd4f8367d6232d39b1fd9e9c916cc6de95fcfb8c89dfedd13d22f3da70b70e0",
    9: "acf376be788cefcdb4640ebd6a282faaf921735a4b0903f071180a3b5e3ef273",
    10: "5153ded0e3637722fc9972c17cca5c9ca55e737e0993e5d46bf5486e041a5765",
}


def _table_names(database: Path) -> set[str]:
    with sqlite3.connect(database) as connection:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }


def test_v11_manifest_is_frozen_without_rewriting_v1_to_v10() -> None:
    assert {
        version: SQLiteStore._FROZEN_MANIFEST_SHA256[version] for version in range(1, 11)
    } == FROZEN_V1_TO_V10_MANIFESTS
    assert {
        version: SQLiteStore._FROZEN_MIGRATION_CHECKSUMS[version] for version in range(1, 11)
    } == FROZEN_V1_TO_V10_CHECKSUMS

    manifest = SQLiteStore._migration_manifest(11)
    assert (
        hashlib.sha256(manifest.encode()).hexdigest() == (SQLiteStore._FROZEN_MANIFEST_SHA256[11])
    )
    checksum = hashlib.sha256(
        "\n".join(("11", "phase5a_skill_mcp_scheduler", manifest)).encode()
    ).hexdigest()
    assert checksum == SQLiteStore._FROZEN_MIGRATION_CHECKSUMS[11]


def test_v12_is_additive_and_v11_manifest_remains_frozen(tmp_path: Path) -> None:
    database = tmp_path / "v12.sqlite3"
    store = SQLiteStore(database)
    assert store.migrate(11) == 11
    assert V12_TABLES.isdisjoint(_table_names(database))
    v11_manifest = SQLiteStore._migration_manifest(11)

    assert store.migrate(target_version=12) == 12
    assert V12_TABLES.issubset(_table_names(database))
    assert SQLiteStore._migration_manifest(11) == v11_manifest
    manifest = SQLiteStore._migration_manifest(12)
    assert hashlib.sha256(manifest.encode()).hexdigest() == SQLiteStore._FROZEN_MANIFEST_SHA256[12]


@pytest.mark.parametrize("starting_version", range(1, 11))
def test_v1_through_v10_upgrade_to_v11_preserves_existing_rows(
    tmp_path: Path, starting_version: int
) -> None:
    database = tmp_path / f"from-v{starting_version}.sqlite3"
    store = SQLiteStore(database)
    assert store.migrate(starting_version) == starting_version
    profile = store.add_model_profile(
        ModelProfile(
            id=f"model-from-v{starting_version}",
            name=f"from-v{starting_version}",
            model_id="legacy-model",
            base_url="https://example.invalid/v1",
            secret_ref="OPERANT_PHASE45_TEST_KEY",
        )
    )

    assert store.migrate(11) == 11
    assert store.schema_version() == 11
    assert store.get_model_profile(profile.id) == profile
    assert V11_TABLES.issubset(_table_names(database))
    applied = store.list_applied_migrations()
    assert [row["version"] for row in applied] == list(range(1, 12))
    assert applied[-1]["checksum"] == SQLiteStore._FROZEN_MIGRATION_CHECKSUMS[11]


def test_concurrent_v11_initialization_is_serial_and_repeatable(tmp_path: Path) -> None:
    database = tmp_path / "concurrent.sqlite3"

    def initialize() -> int:
        store = SQLiteStore(database)
        store.initialize()
        return store.schema_version()

    with ThreadPoolExecutor(max_workers=4) as executor:
        versions = list(executor.map(lambda _index: initialize(), range(8)))

    assert versions == [16] * 8
    store = SQLiteStore(database)
    assert [row["version"] for row in store.list_applied_migrations()] == list(range(1, 17))
    assert V11_TABLES.issubset(_table_names(database))


def test_v11_failure_is_atomic_for_v10_and_empty_databases(tmp_path: Path) -> None:
    class BrokenV11Store(SQLiteStore):
        def _upgrade_v11(self, connection: sqlite3.Connection) -> None:
            super()._upgrade_v11(connection)
            connection.execute("CREATE TABLE injected_phase45_failure(id TEXT)")
            raise RuntimeError("injected v11 failure")

    database = tmp_path / "from-v10.sqlite3"
    SQLiteStore(database).migrate(10)
    with pytest.raises(RuntimeError, match="injected v11 failure"):
        BrokenV11Store(database).migrate()
    assert SQLiteStore(database).schema_version() == 10
    assert V11_TABLES.isdisjoint(_table_names(database))
    assert "injected_phase45_failure" not in _table_names(database)

    empty_database = tmp_path / "empty.sqlite3"
    with pytest.raises(RuntimeError, match="injected v11 failure"):
        BrokenV11Store(empty_database).migrate()
    assert _table_names(empty_database) == set()


def test_v11_rollback_requires_empty_isolated_database(tmp_path: Path) -> None:
    empty_database = tmp_path / "empty-rollback.sqlite3"
    empty = SQLiteStore(empty_database)
    assert empty.migrate(11) == 11
    with pytest.raises(MigrationError, match="explicitly isolated"):
        empty.rollback(10)
    assert empty.rollback(10, isolated=True) == 10
    assert V11_TABLES.isdisjoint(_table_names(empty_database))

    populated_database = tmp_path / "populated-rollback.sqlite3"
    populated = SQLiteStore(populated_database)
    populated.migrate()
    with sqlite3.connect(populated_database) as connection:
        connection.execute(
            """
            INSERT INTO skill_candidates(
                candidate_id, root_ref, relative_directory, name, description,
                manifest_sha256, snapshot_json, trust_status, discovered_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "skill:test",
                "OPERANT_SKILL_ROOT",
                "test",
                "test",
                "test candidate",
                "a" * 64,
                "{}",
                "untrusted_candidate",
                "2026-09-03T00:00:00+00:00",
            ),
        )
    with pytest.raises(MigrationError, match="Phase 5A tables"):
        populated.rollback(10, isolated=True)
    assert populated.schema_version() == 16


def test_mcp_lifecycle_events_are_append_only_and_configs_store_references(
    tmp_path: Path,
) -> None:
    database = tmp_path / "mcp-lifecycle.sqlite3"
    SQLiteStore(database).migrate()
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO mcp_servers(
                server_id, transport, endpoint_ref, secret_ref, stdio_argv_json,
                cwd_ref, environment_refs_json, allow_loopback_http,
                lifecycle_status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "local-test",
                "stdio",
                None,
                "OPERANT_MCP_TEST_KEY",
                '["mcp-server"]',
                "OPERANT_MCP_TEST_CWD",
                '{"API_KEY":"OPERANT_MCP_TEST_KEY"}',
                0,
                "ready",
                "2026-09-03T00:00:00+00:00",
                "2026-09-03T00:00:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO mcp_lifecycle_events(
                id, server_id, event_type, lifecycle_status, detail_json,
                security_audit_event_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "mcp-event-1",
                "local-test",
                "server.ready",
                "ready",
                "{}",
                None,
                "2026-09-03T00:00:00+00:00",
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE mcp_lifecycle_events SET lifecycle_status = 'stopped' "
                "WHERE id = 'mcp-event-1'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM mcp_lifecycle_events WHERE id = 'mcp-event-1'")

        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(mcp_servers)")}
    assert "secret_ref" in columns
    assert "secret" not in columns
