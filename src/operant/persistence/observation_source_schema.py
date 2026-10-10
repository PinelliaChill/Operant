"""Private v25 provenance for observations used by local capability actions."""

from __future__ import annotations

import sqlite3
from typing import Any


def upgrade(connection: sqlite3.Connection) -> None:
    # Existing observations intentionally receive no authority: observe again.
    connection.execute("""CREATE TABLE capability_observation_sources (
        observation_id TEXT PRIMARY KEY NOT NULL,
        source_job_id TEXT NOT NULL,
        lease_id TEXT NOT NULL,
        lease_fencing INTEGER NOT NULL CHECK(lease_fencing >= 1),
        scope_digest TEXT NOT NULL CHECK(
            length(scope_digest) = 64 AND scope_digest NOT GLOB '*[^0-9a-f]*'
        ),
        FOREIGN KEY(observation_id) REFERENCES capability_observations(observation_id),
        FOREIGN KEY(source_job_id) REFERENCES remote_execution_jobs(job_id),
        FOREIGN KEY(lease_id) REFERENCES remote_target_leases(lease_id)
    )""")


def downgrade(connection: sqlite3.Connection) -> None:
    if connection.execute("SELECT 1 FROM capability_observation_sources LIMIT 1").fetchone():
        from operant.persistence.sqlite import MigrationError

        raise MigrationError("observation v25 contains data; restore a full isolated snapshot")
    connection.execute("DROP TABLE capability_observation_sources")


def schema_contracts() -> tuple[
    dict[str, set[str]],
    dict[str, dict[str, dict[str, Any]]],
    dict[str, tuple[str, ...]],
    dict[str, tuple[tuple[str, ...], ...]],
    dict[str, tuple[str, ...]],
    dict[str, tuple[tuple[str, str, str, str], ...]],
]:
    table = "capability_observation_sources"
    names = {"observation_id", "source_job_id", "lease_id", "lease_fencing", "scope_digest"}
    return (
        {table: names},
        {
            table: {
                name: {"type": "INTEGER" if name == "lease_fencing" else "TEXT", "not_null": True}
                for name in names
            }
        },
        {table: ("observation_id",)},
        {},
        {},
        {
            table: (
                ("observation_id", "capability_observations", "observation_id", "NO ACTION"),
                ("source_job_id", "remote_execution_jobs", "job_id", "NO ACTION"),
                ("lease_id", "remote_target_leases", "lease_id", "NO ACTION"),
            )
        },
    )
