from __future__ import annotations

import sqlite3

import pytest

from operant.application.graph import GraphRuntime
from operant.domain.graph import NodeKind, NodeSpec, WorkflowDefinition, WorkflowDefinitionStatus
from operant.persistence.graph_team import SQLiteGraphRepository
from operant.persistence.sqlite import MigrationError, SQLiteStore

PHASE23_TABLES = {
    "workflow_definitions",
    "graph_workflow_runs",
    "graph_run_leases",
    "node_runs",
    "node_attempts",
    "graph_run_events",
    "team_definitions",
    "team_runs",
    "team_roster",
    "team_messages",
    "mailbox_deliveries",
    "team_tasks",
    "artifact_board_items",
    "team_run_events",
}


def test_v8_upgrades_to_v9_and_empty_v9_rolls_back_in_isolation(tmp_path) -> None:
    path = tmp_path / "operant.db"
    store = SQLiteStore(path)
    assert store.migrate(target_version=8) == 8
    assert store.migrate() == 10
    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert PHASE23_TABLES.issubset(tables)
    assert store.rollback(8, isolated=True) == 8
    with sqlite3.connect(path) as connection:
        remaining = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert PHASE23_TABLES.isdisjoint(remaining)


def test_v9_refuses_data_loss_rollback(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "operant.db")
    store.initialize()
    GraphRuntime(SQLiteGraphRepository(store)).create_run(
        WorkflowDefinition(
            workflow_id="migration.graph",
            name="migration graph",
            nodes=(NodeSpec(node_id="artifact", node_kind=NodeKind.ARTIFACT),),
            status=WorkflowDefinitionStatus.PUBLISHED,
        )
    )
    with pytest.raises(MigrationError, match="contain data"):
        store.rollback(8, isolated=True)
    assert store.schema_version() == 10


def test_v9_schema_drift_is_detected_before_runtime_use(tmp_path) -> None:
    path = tmp_path / "operant.db"
    store = SQLiteStore(path)
    store.initialize()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER graph_run_events_no_delete")
    with pytest.raises(MigrationError, match="schema managed objects"):
        SQLiteStore(path).schema_version()
