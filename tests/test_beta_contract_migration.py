from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from operant.persistence.sqlite import MigrationError, SQLiteStore


def _insert_remote_session(connection: sqlite3.Connection) -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    host_id = "host-beta"
    device_id = "device-beta"
    session_id = "session-beta"
    connection.execute(
        """
        INSERT INTO remote_control_hosts(
            host_id, display_name, signing_public_key, exchange_public_key,
            core_version, protocol_version, capabilities_json, enabled,
            online_state, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, '[]', 1, 'online', ?, ?)
        """,
        (
            host_id,
            "Beta host",
            "s" * 32,
            "e" * 32,
            "2.0-beta",
            "phase56.v1",
            now.isoformat(),
            now.isoformat(),
        ),
    )
    connection.execute(
        """
        INSERT INTO remote_devices(
            device_id, host_id, display_name, signing_public_key,
            exchange_public_key, scopes_json, version, paired_at
        ) VALUES (?, ?, ?, ?, ?, '["view"]', 1, ?)
        """,
        (device_id, host_id, "Beta device", "d" * 32, "x" * 32, now.isoformat()),
    )
    connection.execute(
        """
        INSERT INTO remote_sessions(
            remote_session_id, host_id, device_id, transport_mode,
            protocol_version, event_cursor, connection_state, session_key_ref,
            expires_at, connected_at, created_at
        ) VALUES (?, ?, ?, 'direct', 'phase56.v1', 0, 'connected', ?, ?, ?, ?)
        """,
        (
            session_id,
            host_id,
            device_id,
            "OPERANT_TEST_SESSION_KEY",
            (now + timedelta(minutes=5)).isoformat(),
            now.isoformat(),
            now.isoformat(),
        ),
    )
    return session_id, device_id


def test_v14_manifest_is_frozen_and_additive(tmp_path: Path) -> None:
    database = tmp_path / "beta.sqlite3"
    store = SQLiteStore(database)
    assert store.migrate(target_version=13) == 13
    v13_manifest = SQLiteStore._migration_manifest(13)

    assert store.migrate() == 14
    assert store.schema_version() == 14
    assert SQLiteStore._migration_manifest(13) == v13_manifest
    manifest = SQLiteStore._migration_manifest(14)
    assert (
        hashlib.sha256(manifest.encode()).hexdigest() == (SQLiteStore._FROZEN_MANIFEST_SHA256[14])
    )
    checksum = hashlib.sha256(
        "\n".join(("14", "beta_remote_gateway_container_lifecycle", manifest)).encode()
    ).hexdigest()
    assert checksum == SQLiteStore._FROZEN_MIGRATION_CHECKSUMS[14]

    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert {
        "remote_gateway_connections",
        "remote_gateway_events",
        "writer_container_lifecycles",
        "writer_container_lifecycle_events",
    }.issubset(tables)


def test_v14_gateway_binding_and_events_are_guarded(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "gateway.sqlite3")
    store.initialize()
    now = datetime.now(timezone.utc)
    with store._connect() as connection:
        session_id, device_id = _insert_remote_session(connection)
        connection.execute(
            """
            INSERT INTO remote_gateway_connections(
                connection_id, remote_session_id, device_id, transport_mode,
                event_cursor, status, owner_id, fencing, lease_expires_at,
                last_seen_at, created_at, updated_at
            ) VALUES (?, ?, ?, 'direct', 0, 'connected', ?, 1, ?, ?, ?, ?)
            """,
            (
                "connection-beta",
                session_id,
                device_id,
                "gateway-owner",
                (now + timedelta(seconds=30)).isoformat(),
                now.isoformat(),
                now.isoformat(),
                now.isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO remote_gateway_events(
                id, connection_id, event_type, body_json, created_at
            ) VALUES ('gateway-event-beta', 'connection-beta', 'connected', '{}', ?)
            """,
            (now.isoformat(),),
        )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE remote_gateway_events SET event_type = 'changed' WHERE id = ?",
                ("gateway-event-beta",),
            )
        with pytest.raises(sqlite3.IntegrityError, match="device does not match"):
            connection.execute(
                """
                INSERT INTO remote_gateway_connections(
                    connection_id, remote_session_id, device_id, transport_mode,
                    event_cursor, status, owner_id, fencing, lease_expires_at,
                    last_seen_at, created_at, updated_at
                ) VALUES ('bad-connection', ?, 'different-device', 'relay', 0,
                          'connected', 'owner', 1, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    (now + timedelta(seconds=30)).isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )


def test_v14_rollback_requires_empty_tables(tmp_path: Path) -> None:
    empty = SQLiteStore(tmp_path / "empty.sqlite3")
    empty.initialize()
    assert empty.rollback(13, isolated=True) == 13

    populated = SQLiteStore(tmp_path / "populated.sqlite3")
    populated.initialize()
    now = datetime.now(timezone.utc)
    with populated._connect() as connection:
        session_id, device_id = _insert_remote_session(connection)
        connection.execute(
            """
            INSERT INTO remote_gateway_connections(
                connection_id, remote_session_id, device_id, transport_mode,
                event_cursor, status, owner_id, fencing, lease_expires_at,
                last_seen_at, created_at, updated_at
            ) VALUES ('connection-beta', ?, ?, 'direct', 0, 'connected',
                      'gateway-owner', 1, ?, ?, ?, ?)
            """,
            (
                session_id,
                device_id,
                (now + timedelta(seconds=30)).isoformat(),
                now.isoformat(),
                now.isoformat(),
                now.isoformat(),
            ),
        )
    with pytest.raises(MigrationError, match="contain data"):
        populated.rollback(13, isolated=True)
