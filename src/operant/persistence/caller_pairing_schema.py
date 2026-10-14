"""v26 private persistence for additive local caller pairing."""

from __future__ import annotations

import sqlite3
from typing import Any

_TABLES = (
    "caller_pairing_challenges",
    "caller_pairing_devices",
    "caller_pairing_nonces",
    "caller_pairing_receipts",
)


def upgrade(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE caller_pairing_challenges (
        ticket_id TEXT PRIMARY KEY NOT NULL,
        core_epoch_id TEXT NOT NULL,
        code_hash TEXT NOT NULL,
        base_url TEXT NOT NULL,
        expires_at INTEGER NOT NULL,
        consumed_at INTEGER,
        pair_request_id TEXT,
        pair_request_hash TEXT,
        device_id TEXT,
        pair_reply_json TEXT
    )""")
    connection.execute("""CREATE TABLE caller_pairing_devices (
        device_id TEXT PRIMARY KEY NOT NULL,
        display_name TEXT NOT NULL,
        signing_public_key TEXT NOT NULL,
        exchange_public_key TEXT NOT NULL,
        signing_key_fingerprint TEXT NOT NULL,
        core_epoch_id TEXT NOT NULL,
        scope TEXT NOT NULL CHECK(scope = 'skill_source.manage'),
        expires_at INTEGER NOT NULL,
        created_at INTEGER NOT NULL,
        revoked_at INTEGER
    )""")
    connection.execute("""CREATE TABLE caller_pairing_nonces (
        device_id TEXT NOT NULL,
        core_epoch_id TEXT NOT NULL,
        nonce TEXT NOT NULL,
        request_hash TEXT NOT NULL,
        expires_at INTEGER NOT NULL,
        PRIMARY KEY(device_id, core_epoch_id, nonce),
        FOREIGN KEY(device_id) REFERENCES caller_pairing_devices(device_id) ON DELETE RESTRICT
    )""")
    connection.execute("""CREATE TABLE caller_pairing_receipts (
        device_id TEXT NOT NULL,
        request_id TEXT NOT NULL,
        operation TEXT NOT NULL CHECK(operation IN ('list','add','remove')),
        payload_hash TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN
            ('in_progress','awaiting_approval','completed','failed','unconfirmed')),
        http_status INTEGER NOT NULL,
        result_json TEXT,
        approval_id TEXT,
        action_hash TEXT,
        original_args_json TEXT,
        error_code TEXT,
        core_epoch_id TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        PRIMARY KEY(device_id, request_id),
        FOREIGN KEY(device_id) REFERENCES caller_pairing_devices(device_id) ON DELETE RESTRICT
    )""")
    connection.execute(
        "CREATE UNIQUE INDEX caller_pairing_pair_request "
        "ON caller_pairing_challenges(pair_request_id)"
    )
    connection.execute(
        "CREATE INDEX caller_pairing_nonce_expiry ON caller_pairing_nonces(expires_at)"
    )


def downgrade(connection: sqlite3.Connection) -> None:
    if any(connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() for table in _TABLES):
        from operant.persistence.sqlite import MigrationError

        raise MigrationError("caller pairing v26 contains data; restore an isolated snapshot")
    for table in reversed(_TABLES):
        connection.execute(f"DROP TABLE {table}")


def schema_contracts() -> tuple[
    dict[str, set[str]],
    dict[str, dict[str, dict[str, Any]]],
    dict[str, tuple[str, ...]],
    dict[str, tuple[tuple[str, ...], ...]],
    dict[str, tuple[str, ...]],
    dict[str, tuple[tuple[str, str, str, str], ...]],
]:
    columns = {
        "caller_pairing_challenges": {
            "ticket_id",
            "core_epoch_id",
            "code_hash",
            "base_url",
            "expires_at",
            "consumed_at",
            "pair_request_id",
            "pair_request_hash",
            "device_id",
            "pair_reply_json",
        },
        "caller_pairing_devices": {
            "device_id",
            "display_name",
            "signing_public_key",
            "exchange_public_key",
            "signing_key_fingerprint",
            "core_epoch_id",
            "scope",
            "expires_at",
            "created_at",
            "revoked_at",
        },
        "caller_pairing_nonces": {
            "device_id",
            "core_epoch_id",
            "nonce",
            "request_hash",
            "expires_at",
        },
        "caller_pairing_receipts": {
            "device_id",
            "request_id",
            "operation",
            "payload_hash",
            "state",
            "http_status",
            "result_json",
            "approval_id",
            "action_hash",
            "original_args_json",
            "error_code",
            "core_epoch_id",
            "created_at",
            "updated_at",
        },
    }
    integers = {
        "expires_at",
        "consumed_at",
        "created_at",
        "updated_at",
        "revoked_at",
        "http_status",
    }
    nullable = (
        {
            ("caller_pairing_challenges", name)
            for name in (
                "consumed_at",
                "pair_request_id",
                "pair_request_hash",
                "device_id",
                "pair_reply_json",
            )
        }
        | {
            ("caller_pairing_devices", "revoked_at"),
        }
        | {
            ("caller_pairing_receipts", name)
            for name in (
                "result_json",
                "approval_id",
                "action_hash",
                "original_args_json",
                "error_code",
            )
        }
    )
    physical = {
        table: {
            name: {
                "type": "INTEGER" if name in integers else "TEXT",
                "not_null": (table, name) not in nullable,
            }
            for name in names
        }
        for table, names in columns.items()
    }
    pks = {
        "caller_pairing_challenges": ("ticket_id",),
        "caller_pairing_devices": ("device_id",),
        "caller_pairing_nonces": ("device_id", "core_epoch_id", "nonce"),
        "caller_pairing_receipts": ("device_id", "request_id"),
    }
    indexes: dict[str, tuple[tuple[str, ...], ...]] = {
        "caller_pairing_challenges": (("pair_request_id",),),
    }
    named_indexes: dict[str, tuple[str, ...]] = {
        "caller_pairing_nonce_expiry": ("expires_at",),
    }
    foreign_keys: dict[str, tuple[tuple[str, str, str, str], ...]] = {
        "caller_pairing_nonces": (
            ("device_id", "caller_pairing_devices", "device_id", "RESTRICT"),
        ),
        "caller_pairing_receipts": (
            ("device_id", "caller_pairing_devices", "device_id", "RESTRICT"),
        ),
    }
    return columns, physical, pks, indexes, named_indexes, foreign_keys
