"""Read and append redacted Workflow suggestion conversations."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


class SuggestionConflictError(RuntimeError):
    pass


class SQLiteWorkflowSuggestionRepository:
    MAX_TURNS = 40

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = str(database_path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @staticmethod
    def _turn(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "turn_id": row["turn_id"],
            "ordinal": row["ordinal"],
            "instruction": row["instruction"],
            **json.loads(row["suggestion_json"]),
            "created_at": row["created_at"],
        }

    def get(self, conversation_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_suggestion_conversations WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(conversation_id)
            turns = connection.execute(
                "SELECT * FROM workflow_suggestion_turns WHERE conversation_id=? ORDER BY ordinal",
                (conversation_id,),
            ).fetchall()
        latest = self._turn(turns[-1]) if turns else None
        return {
            **dict(row),
            "turn_count": len(turns),
            "last_instruction": latest["instruction"] if latest else None,
            "last_changes": latest["changes"] if latest else [],
            "turns": [self._turn(turn) for turn in turns],
        }

    def list(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT c.*, COUNT(t.turn_id) AS turn_count FROM "
                "workflow_suggestion_conversations c LEFT JOIN workflow_suggestion_turns t "
                "ON t.conversation_id=c.conversation_id GROUP BY c.conversation_id "
                "ORDER BY c.updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            result = []
            for row in rows:
                latest = connection.execute(
                    "SELECT instruction, suggestion_json FROM workflow_suggestion_turns "
                    "WHERE conversation_id=? ORDER BY ordinal DESC LIMIT 1",
                    (row["conversation_id"],),
                ).fetchone()
                item = dict(row)
                item["last_instruction"] = latest["instruction"] if latest else None
                item["last_changes"] = (
                    json.loads(latest["suggestion_json"])["changes"] if latest else []
                )
                result.append(item)
        return result

    def append(
        self,
        *,
        conversation_id: str | None,
        expected_turn_count: int,
        team_id: str,
        team_version: int,
        base_workflow_id: str | None,
        base_version: int | None,
        instruction: str,
        suggestion: dict[str, Any],
    ) -> tuple[str, str]:
        now = datetime.now(timezone.utc).isoformat()
        new_id = conversation_id or f"suggestion_{uuid4().hex}"
        turn_id = f"suggestion_turn_{uuid4().hex}"
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if conversation_id is None:
                if expected_turn_count != 0:
                    raise SuggestionConflictError("new conversation must start empty")
                connection.execute(
                    "INSERT INTO workflow_suggestion_conversations VALUES (?,?,?,?,?,?,?)",
                    (new_id, team_id, team_version, base_workflow_id, base_version, now, now),
                )
            else:
                row = connection.execute(
                    "SELECT team_id, team_version, base_workflow_id, base_version FROM "
                    "workflow_suggestion_conversations WHERE conversation_id=?",
                    (new_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(new_id)
                if (
                    row["team_id"],
                    row["team_version"],
                    row["base_workflow_id"],
                    row["base_version"],
                ) != (team_id, team_version, base_workflow_id, base_version):
                    raise SuggestionConflictError("conversation binding changed")
            count = connection.execute(
                "SELECT COUNT(*) FROM workflow_suggestion_turns WHERE conversation_id=?",
                (new_id,),
            ).fetchone()[0]
            if count != expected_turn_count:
                raise SuggestionConflictError("conversation changed during suggestion")
            if count >= self.MAX_TURNS:
                raise SuggestionConflictError("conversation reached its 40-turn limit")
            connection.execute(
                "INSERT INTO workflow_suggestion_turns VALUES (?,?,?,?,?,?)",
                (
                    turn_id,
                    new_id,
                    count + 1,
                    instruction,
                    json.dumps(suggestion, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            connection.execute(
                "UPDATE workflow_suggestion_conversations SET updated_at=? WHERE conversation_id=?",
                (now, new_id),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        return new_id, turn_id
