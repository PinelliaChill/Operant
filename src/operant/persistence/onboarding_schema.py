"""Additive schema v24 for setup, conversation names and private provider data."""

from __future__ import annotations

import sqlite3
from typing import Any

TABLES = (
    "ux_conversation_metadata",
    "ux_model_connections",
    "ux_settings",
    "ux_commands",
    "ux_provider_metadata",
)


def upgrade(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE ux_conversation_metadata (
        thread_id TEXT PRIMARY KEY NOT NULL,
        title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 100),
        title_source TEXT NOT NULL CHECK(title_source IN ('manual','auto','default')),
        revision INTEGER NOT NULL CHECK(revision >= 1),
        updated_at TEXT NOT NULL,
        FOREIGN KEY(thread_id) REFERENCES threads(id)
    )""")
    connection.execute("""CREATE TABLE ux_model_connections (
        id TEXT PRIMARY KEY NOT NULL,
        body_json TEXT NOT NULL CHECK(json_valid(body_json)),
        updated_at TEXT NOT NULL
    )""")
    connection.execute("""CREATE TABLE ux_settings (
        key TEXT PRIMARY KEY NOT NULL,
        body_json TEXT NOT NULL CHECK(json_valid(body_json))
    )""")
    connection.execute("""CREATE TABLE ux_commands (
        key TEXT PRIMARY KEY NOT NULL,
        fingerprint TEXT NOT NULL CHECK(length(fingerprint) = 64),
        body_json TEXT NOT NULL CHECK(json_valid(body_json)),
        updated_at TEXT NOT NULL
    )""")
    connection.execute("""CREATE TABLE ux_provider_metadata (
        key TEXT PRIMARY KEY NOT NULL,
        connection_id TEXT NOT NULL,
        body_json TEXT NOT NULL CHECK(json_valid(body_json)),
        updated_at TEXT NOT NULL
    )""")
    connection.execute(
        "CREATE INDEX idx_ux_provider_connection ON ux_provider_metadata(connection_id)"
    )
    # A committed first user message names a conversation in the same transaction.
    # Manual names are never replaced. Old clients/history wire types remain frozen.
    connection.execute("""CREATE TRIGGER ux_first_message_title AFTER INSERT ON items
    WHEN json_extract(NEW.body, '$.payload.type') = 'user_message'
         AND length(trim(replace(replace(replace(json_extract(NEW.body, '$.payload.text'),
             char(10),' '),char(13),' '),char(9),' '))) > 0
    BEGIN
        INSERT INTO ux_conversation_metadata(thread_id,title,title_source,revision,updated_at)
        SELECT t.thread_id,
               substr(trim(replace(replace(replace((
                   SELECT json_extract(i.body,'$.payload.text') FROM items i
                   WHERE i.thread_id=NEW.thread_id AND i.item_type='user_message'
                     AND length(trim(replace(replace(replace(json_extract(i.body,'$.payload.text'),
                         char(10),' '),char(13),' '),char(9),' ')))>0
                   ORDER BY i.sequence LIMIT 1
                   ),char(10),' '),char(13),' '),char(9),' ')),1,40),
               'auto',1,json_extract(NEW.body,'$.created_at')
        FROM turns t WHERE t.id = NEW.turn_id
        ON CONFLICT(thread_id) DO UPDATE SET
            title=excluded.title, title_source='auto',
            revision=ux_conversation_metadata.revision+1, updated_at=excluded.updated_at
        WHERE ux_conversation_metadata.title_source='default';
    END""")


def downgrade(connection: sqlite3.Connection) -> None:
    if any(connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() for table in TABLES):
        from operant.persistence.sqlite import MigrationError

        raise MigrationError("onboarding v24 contains data; restore a full isolated snapshot")
    connection.execute("DROP TRIGGER ux_first_message_title")
    connection.execute("DROP INDEX idx_ux_provider_connection")
    for table in reversed(TABLES):
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
        "ux_conversation_metadata": {
            "thread_id",
            "title",
            "title_source",
            "revision",
            "updated_at",
        },
        "ux_model_connections": {"id", "body_json", "updated_at"},
        "ux_settings": {"key", "body_json"},
        "ux_commands": {"key", "fingerprint", "body_json", "updated_at"},
        "ux_provider_metadata": {"key", "connection_id", "body_json", "updated_at"},
    }
    physical = {
        table: {
            name: {"type": "INTEGER" if name == "revision" else "TEXT", "not_null": True}
            for name in names
        }
        for table, names in columns.items()
    }
    return (
        columns,
        physical,
        {
            table: (
                ("thread_id",)
                if table == "ux_conversation_metadata"
                else ("id",)
                if table == "ux_model_connections"
                else ("key",)
            )
            for table in columns
        },
        {},
        {"idx_ux_provider_connection": ("connection_id",)},
        {"ux_conversation_metadata": (("thread_id", "threads", "id", "NO ACTION"),)},
    )
