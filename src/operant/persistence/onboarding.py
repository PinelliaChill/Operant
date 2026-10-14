"""Transactional setup metadata. Secrets live in a protected .env, never here."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal, cast

from pydantic import ValidationError

from operant.contracts.onboarding import (
    ConversationInitialization,
    ConversationMetadata,
    validate_title,
)
from operant.domain.models import utc_now
from operant.persistence.sqlite import ConflictError, NotFoundError, SQLiteStore

_CREDENTIAL_KEYS = frozenset(
    {
        "api_key",
        "access_token",
        "refresh_token",
        "id_token",
        "client_secret",
        "password",
        "cookie",
        "code_verifier",
        "authorization_code",
    }
)


def _json(value: Any, *, limit: int = 262_144) -> str:
    def check(node: Any) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                if str(key).lower() in _CREDENTIAL_KEYS:
                    raise ValueError("credential values cannot be stored in setup metadata")
                check(child)
        elif isinstance(node, (list, tuple)):
            for child in node:
                check(child)

    check(value)
    result = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    if len(result.encode()) > limit:
        raise ValueError("setup metadata exceeds storage limit")
    return result


def _opaque_provider_record(record: Mapping[str, Any]) -> None:
    common = {"provider", "connection_id"}
    if not isinstance(record.get("connection_id"), str) or not record["connection_id"]:
        raise ValueError("provider metadata requires a connection")
    provider = record.get("provider")
    if provider == "chatgpt":
        items = record.get("items")
        if set(record) != common | {"items"} or not isinstance(items, list):
            raise ValueError("invalid encrypted provider metadata")
        for item in items:
            if (
                not isinstance(item, dict)
                or set(item).difference({"type", "id", "encrypted_content"})
                or item.get("type") != "reasoning"
                or not isinstance(item.get("encrypted_content"), str)
                or not item["encrypted_content"]
                or ("id" in item and not isinstance(item["id"], str))
            ):
                raise ValueError("only opaque encrypted reasoning may be stored")
        return
    if provider == "gemini":
        if set(record) == common | {"tool_call_id", "thought_signature"}:
            if all(
                isinstance(record[key], str) and record[key]
                for key in ("tool_call_id", "thought_signature")
            ):
                return
        elif set(record) == common | {"segments"}:
            segments = record["segments"]
            if isinstance(segments, list) and all(
                isinstance(part, dict)
                and set(part) == {"length", "thought_signature"}
                and type(part["length"]) is int
                and part["length"] >= 0
                and (
                    part["thought_signature"] is None or isinstance(part["thought_signature"], str)
                )
                for part in segments
            ):
                return
    raise ValueError("only opaque provider signatures may be stored")


class UXRepository:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT body_json FROM ux_settings WHERE key=?", (key,)
            ).fetchone()
        return default if row is None else json.loads(row["body_json"])

    def set_setting(self, key: str, value: Any) -> None:
        body = _json(value)
        with self.store._connect() as connection:
            connection.execute(
                "INSERT INTO ux_settings(key,body_json) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET body_json=excluded.body_json",
                (key, body),
            )

    def get_connection(self, connection_id: str) -> dict[str, Any] | None:
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT body_json FROM ux_model_connections WHERE id=?", (connection_id,)
            ).fetchone()
        return None if row is None else dict(json.loads(row["body_json"]))

    def list_connections(self) -> list[dict[str, Any]]:
        with self.store._connect() as connection:
            rows = connection.execute(
                "SELECT body_json FROM ux_model_connections ORDER BY id"
            ).fetchall()
        return [dict(json.loads(row["body_json"])) for row in rows]

    def save_connection(self, connection_id: str, record: Mapping[str, Any]) -> None:
        body = _json(dict(record))
        with self.store._connect() as connection:
            connection.execute(
                "INSERT INTO ux_model_connections(id,body_json,updated_at) VALUES (?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET body_json=excluded.body_json,"
                "updated_at=excluded.updated_at",
                (connection_id, body, utc_now().isoformat()),
            )

    def delete_connection(self, connection_id: str) -> None:
        with self.store._connect() as connection:
            connection.execute(
                "DELETE FROM ux_provider_metadata WHERE connection_id=?", (connection_id,)
            )
            connection.execute("DELETE FROM ux_model_connections WHERE id=?", (connection_id,))

    def get_provider_metadata(self, key: str) -> dict[str, Any] | None:
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT body_json FROM ux_provider_metadata WHERE key=?", (key,)
            ).fetchone()
        return None if row is None else dict(json.loads(row["body_json"]))

    def save_provider_metadata(self, key: str, record: Mapping[str, Any]) -> None:
        # Only opaque provider signatures/parts, never hidden reasoning text.
        _opaque_provider_record(record)
        body = _json(dict(record), limit=262_144)
        with self.store._connect() as connection:
            connection.execute(
                "INSERT INTO ux_provider_metadata(key,connection_id,body_json,updated_at) "
                "VALUES (?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET body_json=excluded.body_json,"
                "updated_at=excluded.updated_at",
                (key, str(record.get("connection_id", "")), body, utc_now().isoformat()),
            )

    def delete_provider_metadata(self, key: str) -> None:
        with self.store._connect() as connection:
            connection.execute("DELETE FROM ux_provider_metadata WHERE key=?", (key,))

    def get_command(self, key: str, fingerprint: str) -> dict[str, Any] | None:
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT fingerprint,body_json FROM ux_commands WHERE key=?", (key,)
            ).fetchone()
        if row is None:
            return None
        if row["fingerprint"] != fingerprint:
            raise ConflictError("请求编号已用于另一项操作")
        result = dict(json.loads(row["body_json"]))
        result.pop("_credential_fingerprint_version", None)
        return result

    def get_command_fingerprint(self, key: str) -> str | None:
        """Read the private matching version before validating a legacy receipt."""
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT fingerprint FROM ux_commands WHERE key=?", (key,)
            ).fetchone()
        return None if row is None else str(row["fingerprint"])

    def get_command_fingerprint_version(self, key: str) -> str | None:
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT body_json FROM ux_commands WHERE key=?", (key,)
            ).fetchone()
        if row is None:
            return None
        value = json.loads(row["body_json"]).get("_credential_fingerprint_version")
        return None if value is None else str(value)

    def get_connection_request_snapshot(
        self, key: str, connection_id: str
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Read a connection and its original command receipt in one SQLite snapshot."""
        with self.store._connect() as connection:
            connection.execute("BEGIN")
            saved = connection.execute(
                "SELECT body_json FROM ux_model_connections WHERE id=?", (connection_id,)
            ).fetchone()
            command = connection.execute(
                "SELECT fingerprint,body_json FROM ux_commands WHERE key=?", (key,)
            ).fetchone()

        def decoded_object(raw: str) -> dict[str, Any] | None:
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                return None
            return dict(value) if isinstance(value, dict) else None

        # An existing but malformed row is unconfirmed, never a successful
        # receipt or an absent connection.
        record = None if saved is None else decoded_object(saved["body_json"]) or {}
        receipt = (
            None
            if command is None
            else {
                "fingerprint": str(command["fingerprint"]),
                "result": decoded_object(command["body_json"]),
            }
        )
        return record, receipt

    def get_conversation_command_metadata(self, key: str) -> ConversationMetadata | None:
        """Read the atomic creation receipt without repeating a mutation."""
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT body_json FROM ux_commands WHERE key=?", (key,)
            ).fetchone()
        if row is None:
            return None
        try:
            result = ConversationInitialization.model_validate_json(row["body_json"])
        except ValidationError:
            return None
        with self.store._connect() as connection:
            bound = connection.execute(
                "SELECT 1 FROM thread_legacy_refs WHERE thread_id=? "
                "AND source_type='session' AND source_id=?",
                (result.thread_id, result.session_id),
            ).fetchone()
        if bound is None:
            return None
        return self.get_metadata(result.thread_id)

    def save_command(self, key: str, fingerprint: str, result: Mapping[str, Any]) -> None:
        body = _json(dict(result))
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT fingerprint,body_json FROM ux_commands WHERE key=?", (key,)
            ).fetchone()
            if row is not None:
                if row["fingerprint"] != fingerprint or row["body_json"] != body:
                    raise ConflictError("请求编号已用于另一项操作")
                return
            connection.execute(
                "INSERT INTO ux_commands(key,fingerprint,body_json,updated_at) VALUES (?,?,?,?)",
                (key, fingerprint, body, utc_now().isoformat()),
            )

    def get_metadata(self, thread_id: str) -> ConversationMetadata:
        self.store.get_thread(thread_id)
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT * FROM ux_conversation_metadata WHERE thread_id=?", (thread_id,)
            ).fetchone()
            if row is not None:
                return ConversationMetadata.model_validate(
                    {key: row[key] for key in ConversationMetadata.model_fields}
                )
            # Old conversations may predate v24. A read never mutates their history.
            first = connection.execute(
                "SELECT json_extract(i.body,'$.payload.text') AS text FROM items i "
                "JOIN turns t ON t.id=i.turn_id WHERE t.thread_id=? "
                "AND json_extract(i.body,'$.payload.type')='user_message' "
                "ORDER BY i.sequence LIMIT 1",
                (thread_id,),
            ).fetchone()
        title = "新对话" if first is None else " ".join(str(first["text"]).split())[:40] or "新对话"
        return ConversationMetadata(
            thread_id=thread_id,
            title=title,
            title_source="default" if first is None else "auto",
            revision=0,
        )

    def list_metadata(self, thread_ids: Sequence[str] | None = None) -> list[ConversationMetadata]:
        if thread_ids is None:
            with self.store._connect() as connection:
                thread_ids = [
                    str(row["id"])
                    for row in connection.execute(
                        "SELECT id FROM threads ORDER BY updated_at DESC LIMIT 1000"
                    ).fetchall()
                ]
        return [self.get_metadata(thread_id) for thread_id in thread_ids]

    def rename(
        self, thread_id: str, title: str, expected_revision: int | None = None
    ) -> ConversationMetadata:
        return self.put_thread_metadata(thread_id, title, "manual", expected_revision)

    def get_thread_metadata(self, thread_id: str) -> dict[str, Any]:
        return self.get_metadata(thread_id).model_dump(mode="json")

    def put_thread_metadata(
        self,
        thread_id: str,
        title: str,
        title_source: str,
        expected_revision: int | None = None,
    ) -> ConversationMetadata:
        title = validate_title(title)
        if len(title) > 100 or title_source not in {"default", "auto", "manual"}:
            raise ValueError("invalid conversation title metadata")
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if (
                connection.execute("SELECT 1 FROM threads WHERE id=?", (thread_id,)).fetchone()
                is None
            ):
                raise NotFoundError("conversation not found")
            row = connection.execute(
                "SELECT revision,title_source FROM ux_conversation_metadata WHERE thread_id=?",
                (thread_id,),
            ).fetchone()
            revision = 0 if row is None else int(row["revision"])
            if expected_revision is not None and revision != expected_revision:
                raise ConflictError("名称已改变，请刷新后重试")
            if title_source == "auto" and row is not None and row["title_source"] != "default":
                raise ConflictError("conversation already has a name")
            revision += 1
            connection.execute(
                "INSERT INTO ux_conversation_metadata("
                "thread_id,title,title_source,revision,updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(thread_id) DO UPDATE SET title=excluded.title,"
                "title_source=excluded.title_source,"
                "revision=excluded.revision,updated_at=excluded.updated_at",
                (thread_id, title, title_source, revision, utc_now().isoformat()),
            )
        return ConversationMetadata(
            thread_id=thread_id,
            title=title,
            title_source=cast(Literal["manual", "auto", "default"], title_source),
            revision=revision,
        )
