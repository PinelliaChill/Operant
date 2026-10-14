"""Version 21 durable retention clocks for Workbench temporary resources."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import TYPE_CHECKING, Any

from operant.domain.models import utc_now
from operant.domain.resource_governance import ResourcePolicy
from operant.domain.threads import RetentionPolicy

if TYPE_CHECKING:
    from operant.persistence.sqlite import SQLiteStore


def upgrade(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE workbench_resource_policies (
            thread_id TEXT PRIMARY KEY NOT NULL,
            completed_ttl_seconds INTEGER NOT NULL DEFAULT 3600
                CHECK (completed_ttl_seconds BETWEEN 3600 AND 31536000),
            unanswered_ttl_seconds INTEGER NOT NULL DEFAULT 259200
                CHECK (unanswered_ttl_seconds BETWEEN 3600 AND 31536000),
            completed_at TEXT,
            unanswered_since TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (thread_id) REFERENCES threads(id)
        )"""
    )
    connection.execute(
        """CREATE INDEX idx_workbench_resource_policies_due
        ON workbench_resource_policies(completed_at, unanswered_since)"""
    )


def downgrade(connection: sqlite3.Connection) -> None:
    from operant.persistence.sqlite import MigrationError

    if connection.execute("SELECT 1 FROM workbench_resource_policies LIMIT 1").fetchone():
        raise MigrationError("resource governance contains data; rollback is unsafe")
    if connection.execute(
        "SELECT 1 FROM artifacts WHERE retention_policy_ref='context-reference-temporary' LIMIT 1"
    ).fetchone():
        raise MigrationError("temporary context snapshots exist; rollback is unsafe")
    connection.execute("DROP INDEX idx_workbench_resource_policies_due")
    connection.execute("DROP TABLE workbench_resource_policies")


def schema_contracts() -> tuple[
    dict[str, set[str]],
    dict[str, dict[str, dict[str, Any]]],
    dict[str, tuple[str, ...]],
    dict[str, tuple[tuple[str, ...], ...]],
    dict[str, tuple[str, ...]],
    dict[str, tuple[tuple[str, str, str, str], ...]],
]:
    columns = {
        "workbench_resource_policies": {
            "thread_id",
            "completed_ttl_seconds",
            "unanswered_ttl_seconds",
            "completed_at",
            "unanswered_since",
            "updated_at",
        }
    }
    physical = {
        "workbench_resource_policies": {
            name: {
                "type": "INTEGER" if name.endswith("_ttl_seconds") else "TEXT",
                "not_null": name not in {"completed_at", "unanswered_since"},
            }
            for name in columns["workbench_resource_policies"]
        }
    }
    return (
        columns,
        physical,
        {"workbench_resource_policies": ("thread_id",)},
        {"workbench_resource_policies": ()},
        {"idx_workbench_resource_policies_due": ("completed_at", "unanswered_since")},
        {"workbench_resource_policies": (("thread_id", "threads", "id", "NO ACTION"),)},
    )


class ResourcePolicyRepository:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def get(self, thread_id: str) -> ResourcePolicy:
        self.store.get_thread(thread_id)
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT * FROM workbench_resource_policies WHERE thread_id=?", (thread_id,)
            ).fetchone()
        if row is None:
            return ResourcePolicy()
        return ResourcePolicy(
            completed_ttl_seconds=int(row["completed_ttl_seconds"]),
            unanswered_ttl_seconds=int(row["unanswered_ttl_seconds"]),
            completed_at=(
                datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None
            ),
            unanswered_since=(
                datetime.fromisoformat(row["unanswered_since"]) if row["unanswered_since"] else None
            ),
        )

    def ensure_snapshot_policy(self) -> None:
        from operant.persistence.sqlite import ConflictError, NotFoundError

        try:
            policy = self.store.get_retention_policy("context-reference-temporary")
        except NotFoundError:
            try:
                policy = self.store.create_retention_policy(
                    RetentionPolicy(
                        id="context-reference-temporary",
                        grace_period_seconds=0,
                        allow_physical_delete=True,
                    )
                )
            except ConflictError:
                # Another producer won the first-create race. Verify its contract.
                policy = self.store.get_retention_policy("context-reference-temporary")
        if (
            policy.object_type != "artifact"
            or policy.grace_period_seconds != 0
            or not policy.allow_physical_delete
        ):
            raise ConflictError("temporary snapshot retention policy has an unsafe contract")

    def patch_ttl(
        self, thread_id: str, *, completed_ttl_seconds: int, unanswered_ttl_seconds: int
    ) -> ResourcePolicy:
        self.store.get_thread(thread_id)
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO workbench_resource_policies
                (thread_id,completed_ttl_seconds,unanswered_ttl_seconds,
                 completed_at,unanswered_since,updated_at)
                VALUES (?,?,?,NULL,NULL,?)
                ON CONFLICT(thread_id) DO UPDATE SET
                  completed_ttl_seconds=excluded.completed_ttl_seconds,
                  unanswered_ttl_seconds=excluded.unanswered_ttl_seconds,
                  updated_at=excluded.updated_at""",
                (thread_id, completed_ttl_seconds, unanswered_ttl_seconds, utc_now().isoformat()),
            )
        return self.get(thread_id)

    def confirm(self, thread_id: str, *, completed: bool) -> ResourcePolicy:
        self.store.get_thread(thread_id)
        now = utc_now().isoformat()
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO workbench_resource_policies
                (thread_id,completed_ttl_seconds,unanswered_ttl_seconds,
                 completed_at,unanswered_since,updated_at)
                VALUES (?,3600,259200,?,NULL,?)
                ON CONFLICT(thread_id) DO UPDATE SET
                  completed_at=excluded.completed_at,
                  updated_at=excluded.updated_at""",
                (thread_id, now if completed else None, now),
            )
        return self.get(thread_id)

    def save(self, thread_id: str, policy: ResourcePolicy) -> ResourcePolicy:
        self.store.get_thread(thread_id)
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO workbench_resource_policies
                (thread_id,completed_ttl_seconds,unanswered_ttl_seconds,
                 completed_at,unanswered_since,updated_at)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(thread_id) DO UPDATE SET
                  completed_ttl_seconds=excluded.completed_ttl_seconds,
                  unanswered_ttl_seconds=excluded.unanswered_ttl_seconds,
                  completed_at=excluded.completed_at,
                  unanswered_since=excluded.unanswered_since,
                  updated_at=excluded.updated_at""",
                (
                    thread_id,
                    policy.completed_ttl_seconds,
                    policy.unanswered_ttl_seconds,
                    policy.completed_at.isoformat() if policy.completed_at else None,
                    policy.unanswered_since.isoformat() if policy.unanswered_since else None,
                    utc_now().isoformat(),
                ),
            )
        return policy

    def observe_snapshot(self, thread_id: str) -> None:
        """Start the unanswered clock when the first actual snapshot is created."""
        self.store.get_thread(thread_id)
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO workbench_resource_policies
                (thread_id,completed_ttl_seconds,unanswered_ttl_seconds,
                 completed_at,unanswered_since,updated_at)
                VALUES (?,3600,259200,NULL,?,?)
                ON CONFLICT(thread_id) DO UPDATE SET
                  completed_at=NULL,
                  unanswered_since=excluded.unanswered_since,
                  updated_at=excluded.updated_at""",
                (thread_id, utc_now().isoformat(), utc_now().isoformat()),
            )

    def scan_threads(self, *, after_thread_id: str = "", limit: int = 100) -> list[str]:
        with self.store._connect() as connection:
            rows = connection.execute(
                """SELECT thread_id FROM workbench_resource_policies
                WHERE thread_id > ? AND
                      (completed_at IS NOT NULL OR unanswered_since IS NOT NULL)
                ORDER BY thread_id LIMIT ?""",
                (after_thread_id, limit),
            ).fetchall()
        return [str(row["thread_id"]) for row in rows]
