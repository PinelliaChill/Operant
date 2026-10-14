"""Durable human decision binding for Graph Approval nodes (schema v22)."""

from __future__ import annotations

import sqlite3
from typing import Any


def upgrade(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE graph_node_approvals (
            approval_id TEXT PRIMARY KEY NOT NULL,
            graph_run_id TEXT NOT NULL,
            node_run_id TEXT NOT NULL,
            wait_token TEXT NOT NULL,
            action_hash TEXT NOT NULL CHECK (
                length(action_hash) = 64 AND action_hash NOT GLOB '*[^0-9a-f]*'
            ),
            category TEXT NOT NULL,
            detail TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('pending', 'approved', 'denied', 'expired')
            ),
            requested_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            decided_by TEXT,
            decided_at TEXT,
            UNIQUE(node_run_id, wait_token),
            FOREIGN KEY (graph_run_id) REFERENCES graph_workflow_runs(id),
            FOREIGN KEY (node_run_id) REFERENCES node_runs(id)
        )"""
    )
    connection.execute(
        "CREATE INDEX idx_graph_node_approvals_pending ON graph_node_approvals(status, expires_at)"
    )


def downgrade(connection: sqlite3.Connection) -> None:
    if connection.execute("SELECT 1 FROM graph_node_approvals LIMIT 1").fetchone():
        from operant.persistence.sqlite import MigrationError

        raise MigrationError("Graph node approvals contain data; rollback is unsafe")
    connection.execute("DROP INDEX idx_graph_node_approvals_pending")
    connection.execute("DROP TABLE graph_node_approvals")


def schema_contracts() -> tuple[
    dict[str, set[str]],
    dict[str, dict[str, dict[str, Any]]],
    dict[str, tuple[str, ...]],
    dict[str, tuple[tuple[str, ...], ...]],
    dict[str, tuple[str, ...]],
    dict[str, tuple[tuple[str, str, str, str], ...]],
]:
    columns = {
        "graph_node_approvals": {
            "approval_id",
            "graph_run_id",
            "node_run_id",
            "wait_token",
            "action_hash",
            "category",
            "detail",
            "status",
            "requested_at",
            "expires_at",
            "decided_by",
            "decided_at",
        }
    }
    physical = {
        "graph_node_approvals": {
            column: {"type": "TEXT", "not_null": column not in {"decided_by", "decided_at"}}
            for column in columns["graph_node_approvals"]
        }
    }
    return (
        columns,
        physical,
        {"graph_node_approvals": ("approval_id",)},
        {"graph_node_approvals": (("node_run_id", "wait_token"),)},
        {"idx_graph_node_approvals_pending": ("status", "expires_at")},
        {
            "graph_node_approvals": (
                ("graph_run_id", "graph_workflow_runs", "id", "NO ACTION"),
                ("node_run_id", "node_runs", "id", "NO ACTION"),
            )
        },
    )
