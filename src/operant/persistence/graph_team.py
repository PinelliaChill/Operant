from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from operant.application.graph import GraphConflictError, GraphRepository
from operant.application.team import (
    BoardApplyResult,
    MessageProjection,
    ProjectionApplyResult,
    TeamRepository,
)
from operant.domain.graph import GraphWorkflowRun, NodeAttempt, NodeRun, WorkflowDefinition
from operant.domain.team import (
    ArtifactBoardUpdate,
    DeliveryStatus,
    MailboxDelivery,
    MessageAck,
    MessageAudience,
    MessageEnvelope,
    MessageKind,
    MessageStatus,
    OutboxItem,
    RosterEntry,
    RosterMemberStatus,
    TaskBoardUpdate,
    TeamArtifact,
    TeamDefinition,
    TeamRun,
    TeamRunStatus,
    TeamTask,
)
from operant.persistence.sqlite import SQLiteStore


def _json(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _definition_row_id(kind: str, identity: str, version: int) -> str:
    return f"{kind}:{identity}:v{version}"


class SQLiteGraphRepository(GraphRepository):
    """SQLite-backed Graph authority with optimistic revisions and durable events."""

    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def put_definition(self, definition: WorkflowDefinition) -> None:
        body = _json(definition)
        row_id = _definition_row_id("workflow", definition.workflow_id, definition.version)
        try:
            with self.store._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT body FROM workflow_definitions WHERE workflow_id = ? AND version = ?",
                    (definition.workflow_id, definition.version),
                ).fetchone()
                if existing is not None:
                    if str(existing["body"]) != body:
                        raise GraphConflictError("published definition revisions are immutable")
                    return
                connection.execute(
                    """
                    INSERT INTO workflow_definitions(
                        id, workflow_id, version, status, body, body_hash, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row_id,
                        definition.workflow_id,
                        definition.version,
                        definition.status.value,
                        body,
                        hashlib.sha256(body.encode("utf-8")).hexdigest(),
                        definition.created_at.isoformat(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise GraphConflictError(str(exc)) from exc

    def get_definition(self, workflow_id: str, version: int) -> WorkflowDefinition:
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT body FROM workflow_definitions WHERE workflow_id = ? AND version = ?",
                (workflow_id, version),
            ).fetchone()
        if row is None:
            raise KeyError((workflow_id, version))
        return WorkflowDefinition.model_validate_json(str(row["body"]))

    def create_run(self, run: GraphWorkflowRun, node_runs: tuple[NodeRun, ...]) -> None:
        definition_id = _definition_row_id(
            "workflow", run.workflow_definition_id, run.workflow_definition_version
        )
        try:
            with self.store._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO graph_workflow_runs(
                        id, workflow_definition_id, workflow_definition_version, team_run_id,
                        legacy_workflow_run_id, status, body, created_at, updated_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run.id,
                        definition_id,
                        run.workflow_definition_version,
                        run.team_run_id,
                        run.legacy_workflow_run_id,
                        run.status.value,
                        _json(run),
                        run.created_at.isoformat(),
                        run.updated_at.isoformat(),
                        run.completed_at.isoformat() if run.completed_at else None,
                    ),
                )
                for node in node_runs:
                    connection.execute(
                        """
                        INSERT INTO node_runs(
                            id, workflow_run_id, node_id, status, active_attempt_id,
                            body, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            node.id,
                            node.workflow_run_id,
                            node.node_id,
                            node.status.value,
                            node.active_attempt_id,
                            _json(node),
                            node.created_at.isoformat(),
                            node.updated_at.isoformat(),
                        ),
                    )
                self._append_event(connection, run.id, "graph.run.created", {"status": run.status})
        except sqlite3.IntegrityError as exc:
            raise GraphConflictError(str(exc)) from exc

    def get_run(self, run_id: str) -> GraphWorkflowRun:
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT body FROM graph_workflow_runs WHERE id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return GraphWorkflowRun.model_validate_json(str(row["body"]))

    def get_run_by_legacy_workflow_run_id(self, workflow_run_id: str) -> GraphWorkflowRun:
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT body FROM graph_workflow_runs WHERE legacy_workflow_run_id = ?",
                (workflow_run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(workflow_run_id)
        return GraphWorkflowRun.model_validate_json(str(row["body"]))

    def update_run(self, run: GraphWorkflowRun, *, expected_revision: int) -> None:
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT body FROM graph_workflow_runs WHERE id = ?", (run.id,)
            ).fetchone()
            if current is None:
                raise KeyError(run.id)
            stored = GraphWorkflowRun.model_validate_json(str(current["body"]))
            if stored.revision != expected_revision or run.revision != expected_revision + 1:
                raise GraphConflictError("workflow run revision conflict")
            connection.execute(
                """
                UPDATE graph_workflow_runs
                SET status = ?, body = ?, team_run_id = ?, legacy_workflow_run_id = ?,
                    updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    run.status.value,
                    _json(run),
                    run.team_run_id,
                    run.legacy_workflow_run_id,
                    run.updated_at.isoformat(),
                    run.completed_at.isoformat() if run.completed_at else None,
                    run.id,
                ),
            )
            self._append_event(
                connection,
                run.id,
                "graph.run.updated",
                {"status": run.status, "revision": run.revision},
            )

    def list_node_runs(self, run_id: str) -> tuple[NodeRun, ...]:
        with self.store._connect() as connection:
            rows = connection.execute(
                "SELECT body FROM node_runs WHERE workflow_run_id = ? ORDER BY sequence", (run_id,)
            ).fetchall()
        return tuple(NodeRun.model_validate_json(str(row["body"])) for row in rows)

    def get_node_run(self, node_run_id: str) -> NodeRun:
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT body FROM node_runs WHERE id = ?", (node_run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(node_run_id)
        return NodeRun.model_validate_json(str(row["body"]))

    def update_node_run(self, node_run: NodeRun, *, expected_revision: int) -> None:
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._update_node(connection, node_run, expected_revision=expected_revision)
            self._append_event(
                connection,
                node_run.workflow_run_id,
                "graph.node.updated",
                {"node_id": node_run.node_id, "status": node_run.status},
                node_run_id=node_run.id,
                attempt_id=node_run.active_attempt_id,
            )

    def start_node_attempt(
        self, node_run: NodeRun, attempt: NodeAttempt, *, expected_node_revision: int
    ) -> None:
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._insert_attempt(connection, attempt)
                self._update_node(connection, node_run, expected_revision=expected_node_revision)
            except sqlite3.IntegrityError as exc:
                raise GraphConflictError(str(exc)) from exc
            self._append_event(
                connection,
                node_run.workflow_run_id,
                "graph.node.attempt.started",
                {"node_id": node_run.node_id, "attempt_number": attempt.attempt_number},
                node_run_id=node_run.id,
                attempt_id=attempt.id,
            )

    def get_attempt(self, attempt_id: str) -> NodeAttempt:
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT body FROM node_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
        if row is None:
            raise KeyError(attempt_id)
        return NodeAttempt.model_validate_json(str(row["body"]))

    def finish_node_attempt(
        self, node_run: NodeRun, attempt: NodeAttempt, *, expected_node_revision: int
    ) -> None:
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT body FROM node_attempts WHERE id = ?", (attempt.id,)
            ).fetchone()
            if current is None:
                raise KeyError(attempt.id)
            stored = NodeAttempt.model_validate_json(str(current["body"]))
            if stored.result.value != "running":
                raise GraphConflictError("terminal attempts are immutable")
            self._update_attempt_row(connection, attempt)
            self._update_node(connection, node_run, expected_revision=expected_node_revision)
            self._append_event(
                connection,
                node_run.workflow_run_id,
                "graph.node.attempt.finished",
                {"node_id": node_run.node_id, "result": attempt.result},
                node_run_id=node_run.id,
                attempt_id=attempt.id,
            )

    def update_attempt(self, attempt: NodeAttempt) -> None:
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT body FROM node_attempts WHERE id = ?", (attempt.id,)
            ).fetchone()
            if current is None:
                raise KeyError(attempt.id)
            stored = NodeAttempt.model_validate_json(str(current["body"]))
            if stored.result.value != "running":
                raise GraphConflictError("terminal attempts are immutable")
            self._update_attempt_row(connection, attempt)

    def list_attempts(self, node_run_id: str) -> tuple[NodeAttempt, ...]:
        with self.store._connect() as connection:
            rows = connection.execute(
                "SELECT body FROM node_attempts WHERE node_run_id = ? ORDER BY attempt_number",
                (node_run_id,),
            ).fetchall()
        return tuple(NodeAttempt.model_validate_json(str(row["body"])) for row in rows)

    def list_events(
        self, run_id: str, *, after_cursor: int = 0, limit: int = 1000
    ) -> list[dict[str, Any]]:
        if after_cursor < 0 or not 1 <= limit <= 1000:
            raise ValueError("invalid event cursor or limit")
        with self.store._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, sequence, run_sequence, schema_version, event_type, body, created_at
                FROM graph_run_events
                WHERE workflow_run_id = ? AND sequence > ?
                ORDER BY sequence LIMIT ?
                """,
                (run_id, after_cursor, limit),
            ).fetchall()
        return [
            {
                "event_id": str(row["id"]),
                "cursor": int(row["sequence"]),
                "run_sequence": int(row["run_sequence"]),
                "schema_version": str(row["schema_version"]),
                "event_type": str(row["event_type"]),
                "resource_scope": f"graph_run:{run_id}",
                "stream_kind": "graph.run",
                "payload": json.loads(str(row["body"])),
                "occurred_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def list_recoverable_run_ids(self) -> tuple[str, ...]:
        with self.store._connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM graph_workflow_runs
                WHERE status IN ('created', 'queued', 'running', 'waiting_input',
                                 'waiting_approval', 'interrupted')
                ORDER BY sequence
                """
            ).fetchall()
        return tuple(str(row["id"]) for row in rows)

    @staticmethod
    def _update_node(
        connection: sqlite3.Connection, node: NodeRun, *, expected_revision: int
    ) -> None:
        row = connection.execute("SELECT body FROM node_runs WHERE id = ?", (node.id,)).fetchone()
        if row is None:
            raise KeyError(node.id)
        stored = NodeRun.model_validate_json(str(row["body"]))
        if stored.revision != expected_revision or node.revision != expected_revision + 1:
            raise GraphConflictError("node run revision conflict")
        connection.execute(
            """
            UPDATE node_runs SET status = ?, active_attempt_id = ?, body = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                node.status.value,
                node.active_attempt_id,
                _json(node),
                node.updated_at.isoformat(),
                node.id,
            ),
        )

    @staticmethod
    def _insert_attempt(connection: sqlite3.Connection, attempt: NodeAttempt) -> None:
        effect = {
            "started": "outcome_unknown",
            "unknown": "outcome_unknown",
        }.get(attempt.side_effect_state.value, attempt.side_effect_state.value)
        connection.execute(
            """
            INSERT INTO node_attempts(
                id, node_run_id, attempt_number, status, side_effect_state,
                idempotency_key, body, created_at, updated_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt.id,
                attempt.node_run_id,
                attempt.attempt_number,
                attempt.result.value,
                effect,
                attempt.idempotency_key or attempt.id,
                _json(attempt),
                attempt.started_at.isoformat(),
                attempt.started_at.isoformat(),
                attempt.completed_at.isoformat() if attempt.completed_at else None,
            ),
        )

    @staticmethod
    def _update_attempt_row(connection: sqlite3.Connection, attempt: NodeAttempt) -> None:
        effect = {
            "started": "outcome_unknown",
            "unknown": "outcome_unknown",
        }.get(attempt.side_effect_state.value, attempt.side_effect_state.value)
        connection.execute(
            """
            UPDATE node_attempts
            SET status = ?, side_effect_state = ?, body = ?, updated_at = ?, completed_at = ?
            WHERE id = ?
            """,
            (
                attempt.result.value,
                effect,
                _json(attempt),
                _now(),
                attempt.completed_at.isoformat() if attempt.completed_at else None,
                attempt.id,
            ),
        )

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        run_id: str,
        event_type: str,
        payload: Any,
        *,
        node_run_id: str | None = None,
        attempt_id: str | None = None,
    ) -> None:
        next_sequence = int(
            connection.execute(
                """
                SELECT COALESCE(MAX(run_sequence), 0) + 1
                FROM graph_run_events WHERE workflow_run_id = ?
                """,
                (run_id,),
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO graph_run_events(
                id, workflow_run_id, node_run_id, attempt_id, run_sequence,
                schema_version, event_type, body, created_at
            ) VALUES (?, ?, ?, ?, ?, 'phase23.v1', ?, ?, ?)
            """,
            (
                f"graph_event_{run_id}_{next_sequence}",
                run_id,
                node_run_id,
                attempt_id,
                next_sequence,
                event_type,
                _json(payload),
                _now(),
            ),
        )


class SQLiteTeamRepository(TeamRepository):
    """Single-writer Team store; messages, outbox intent and deliveries commit atomically."""

    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def put_team_definition(self, definition: TeamDefinition) -> TeamDefinition:
        body = _json(definition)
        row_id = _definition_row_id("team", definition.team_id, definition.version)
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT body FROM team_definitions WHERE team_id = ? AND version = ?",
                (definition.team_id, definition.version),
            ).fetchone()
            if existing is not None:
                if str(existing["body"]) != body:
                    raise ValueError("Team Definition versions are immutable")
                return definition
            connection.execute(
                """
                INSERT INTO team_definitions(
                    id, team_id, version, status, body, body_hash, created_at
                )
                VALUES (?, ?, ?, 'published', ?, ?, ?)
                """,
                (row_id, definition.team_id, definition.version, body, _hash(definition), _now()),
            )
        return definition

    def get_team_definition(self, team_id: str, version: int) -> TeamDefinition | None:
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT body FROM team_definitions WHERE team_id = ? AND version = ?",
                (team_id, version),
            ).fetchone()
        return None if row is None else TeamDefinition.model_validate_json(str(row["body"]))

    def put_team_run(self, run: TeamRun) -> TeamRun:
        definition_id = _definition_row_id("team", run.team_id, run.team_version)
        status = "running" if run.status is TeamRunStatus.ACTIVE else run.status.value
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT body FROM team_runs WHERE id = ?", (run.team_run_id,)
            ).fetchone()
            if existing is not None:
                stored = TeamRun.model_validate_json(str(existing["body"]))
                if stored != run:
                    raise ValueError("Team Run already exists with different content")
                return stored
            connection.execute(
                """
                INSERT INTO team_runs(
                    id, team_definition_id, team_definition_version, workflow_run_id,
                    status, body, created_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.team_run_id,
                    definition_id,
                    run.team_version,
                    run.workflow_run_id,
                    status,
                    _json(run),
                    run.created_at.isoformat(),
                    run.updated_at.isoformat(),
                    run.updated_at.isoformat()
                    if run.status
                    in {TeamRunStatus.COMPLETED, TeamRunStatus.FAILED, TeamRunStatus.CANCELLED}
                    else None,
                ),
            )
            self._append_event(
                connection, run.team_run_id, "team.run.created", {"status": run.status}
            )
        return run

    def get_team_run(self, team_run_id: str) -> TeamRun | None:
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT body FROM team_runs WHERE id = ?", (team_run_id,)
            ).fetchone()
        return None if row is None else TeamRun.model_validate_json(str(row["body"]))

    def put_team_run_with_roster(self, run: TeamRun, entries: tuple[RosterEntry, ...]) -> TeamRun:
        """Create a Team run and its complete initial Roster atomically."""

        if any(entry.team_run_id != run.team_run_id for entry in entries):
            raise ValueError("Roster entries must belong to the Team run")
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._put_team_run_with_roster(connection, run, entries)

    def bind_graph_and_put_team_run_with_roster(
        self, run: TeamRun, entries: tuple[RosterEntry, ...]
    ) -> TeamRun:
        """CAS-bind one Graph and create its Team, Roster and initial events atomically."""

        if not entries:
            raise ValueError("Team Run requires a non-empty initial Roster")
        if any(entry.team_run_id != run.team_run_id for entry in entries):
            raise ValueError("Roster entries must belong to the Team run")
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            graph_row = connection.execute(
                "SELECT body FROM graph_workflow_runs WHERE id = ?", (run.workflow_run_id,)
            ).fetchone()
            if graph_row is None:
                raise KeyError(run.workflow_run_id)
            graph_run = GraphWorkflowRun.model_validate_json(str(graph_row["body"]))
            updated_at = datetime.now(timezone.utc)
            bound_graph_run = graph_run.model_copy(
                update={
                    "team_run_id": run.team_run_id,
                    "revision": graph_run.revision + 1,
                    "updated_at": updated_at,
                }
            )
            self._put_team_run_with_roster(connection, run, entries)
            cursor = connection.execute(
                """
                UPDATE graph_workflow_runs
                SET team_run_id = ?, body = ?, updated_at = ?
                WHERE id = ? AND team_run_id IS NULL
                """,
                (
                    run.team_run_id,
                    _json(bound_graph_run),
                    updated_at.isoformat(),
                    run.workflow_run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise GraphConflictError("Graph Run is already bound to a different Team Run")
            SQLiteGraphRepository._append_event(
                connection,
                run.workflow_run_id,
                "graph.run.updated",
                {
                    "status": bound_graph_run.status,
                    "revision": bound_graph_run.revision,
                    "team_run_id": run.team_run_id,
                },
            )
            return run

    def _put_team_run_with_roster(
        self,
        connection: sqlite3.Connection,
        run: TeamRun,
        entries: tuple[RosterEntry, ...],
    ) -> TeamRun:
        definition_id = _definition_row_id("team", run.team_id, run.team_version)
        status = "running" if run.status is TeamRunStatus.ACTIVE else run.status.value
        for entry in entries:
            agent_exists = connection.execute(
                "SELECT 1 FROM agents WHERE id = ?", (entry.agent_instance_id,)
            ).fetchone()
            thread_exists = connection.execute(
                "SELECT 1 FROM threads WHERE id = ?", (entry.thread_id,)
            ).fetchone()
            if agent_exists is None or thread_exists is None:
                raise ValueError("Roster agent and thread must already exist")
        existing = connection.execute(
            "SELECT body FROM team_runs WHERE id = ?", (run.team_run_id,)
        ).fetchone()
        if existing is not None:
            stored = TeamRun.model_validate_json(str(existing["body"]))
            if stored != run:
                raise ValueError("Team Run already exists with different content")
            stored_entries = self._list_roster_with_connection(connection, run.team_run_id)
            if stored_entries != entries:
                raise ValueError("Team Run already exists with a different Roster")
            return stored
        connection.execute(
            """
            INSERT INTO team_runs(
                id, team_definition_id, team_definition_version, workflow_run_id,
                status, body, created_at, updated_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.team_run_id,
                definition_id,
                run.team_version,
                run.workflow_run_id,
                status,
                _json(run),
                run.created_at.isoformat(),
                run.updated_at.isoformat(),
                run.updated_at.isoformat()
                if run.status
                in {TeamRunStatus.COMPLETED, TeamRunStatus.FAILED, TeamRunStatus.CANCELLED}
                else None,
            ),
        )
        for entry in entries:
            top_status = {
                RosterMemberStatus.INVITED: "active",
                RosterMemberStatus.IDLE: "active",
                RosterMemberStatus.CANCELLED: "left",
            }.get(entry.status, entry.status.value)
            joined_at = (entry.joined_at or datetime.now(timezone.utc)).isoformat()
            connection.execute(
                """
                INSERT INTO team_roster(
                    team_run_id, agent_id, thread_id, role, status, body, joined_at, left_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.team_run_id,
                    entry.agent_instance_id,
                    entry.thread_id,
                    entry.member_id,
                    top_status,
                    _json(entry),
                    joined_at,
                    entry.left_at.isoformat() if entry.left_at else None,
                ),
            )
        self._append_event(connection, run.team_run_id, "team.run.created", {"status": run.status})
        return run

    def put_roster_entry(self, entry: RosterEntry) -> RosterEntry:
        top_status = {
            RosterMemberStatus.INVITED: "active",
            RosterMemberStatus.IDLE: "active",
            RosterMemberStatus.CANCELLED: "left",
        }.get(entry.status, entry.status.value)
        joined_at = (entry.joined_at or datetime.now(timezone.utc)).isoformat()
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO team_roster(
                    team_run_id, agent_id, thread_id, role, status, body, joined_at, left_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(team_run_id, agent_id) DO UPDATE SET
                    thread_id = excluded.thread_id, role = excluded.role, status = excluded.status,
                    body = excluded.body, joined_at = excluded.joined_at, left_at = excluded.left_at
                """,
                (
                    entry.team_run_id,
                    entry.agent_instance_id,
                    entry.thread_id,
                    entry.member_id,
                    top_status,
                    _json(entry),
                    joined_at,
                    entry.left_at.isoformat() if entry.left_at else None,
                ),
            )
        return entry

    def list_roster(self, team_run_id: str) -> tuple[RosterEntry, ...]:
        with self.store._connect() as connection:
            return self._list_roster_with_connection(connection, team_run_id)

    @staticmethod
    def _list_roster_with_connection(
        connection: sqlite3.Connection, team_run_id: str
    ) -> tuple[RosterEntry, ...]:
        rows = connection.execute(
            "SELECT body FROM team_roster WHERE team_run_id = ? ORDER BY agent_id",
            (team_run_id,),
        ).fetchall()
        return tuple(RosterEntry.model_validate_json(str(row["body"])) for row in rows)

    def project_message(
        self, projection: MessageProjection, *, idempotency_key: str
    ) -> ProjectionApplyResult:
        request_hash = _hash(
            {
                "message": projection.message.model_dump(mode="json"),
                "recipients": list(projection.message.recipient_ids),
            }
        )
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT body FROM team_messages WHERE json_extract(body, '$.command_key') = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                wrapper = json.loads(str(existing["body"]))
                if wrapper["command_hash"] != request_hash:
                    raise ValueError("message idempotency key was reused with different input")
                return self._projection_from_wrapper(connection, wrapper, created=False)
            message = projection.message
            wrapper = {
                "message": message.model_dump(mode="json"),
                "outbox": projection.outbox.model_dump(mode="json"),
                "command_key": idempotency_key,
                "command_hash": request_hash,
            }
            audience = "broadcast" if message.audience is MessageAudience.TEAM else "direct"
            message_kind = _snake_message_kind(message.message_kind)
            status = "queued" if message.status is MessageStatus.PENDING else message.status.value
            connection.execute(
                """
                INSERT INTO team_messages(
                    id, team_run_id, workflow_run_id, sender_agent_id, audience,
                    message_kind, requires_ack, status, body, created_at, delivered_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message.message_id,
                    message.team_run_id,
                    message.workflow_run_id,
                    message.sender_id,
                    audience,
                    message_kind,
                    int(message.requires_ack),
                    status,
                    _json(wrapper),
                    message.created_at.isoformat(),
                    message.delivered_at.isoformat() if message.delivered_at else None,
                    None,
                ),
            )
            deliveries: list[MailboxDelivery] = []
            for delivery in projection.deliveries:
                cursor = connection.execute(
                    """
                    INSERT INTO mailbox_deliveries(
                        id, message_id, team_run_id, recipient_agent_id, status,
                        created_at, delivered_at, acknowledged_at
                    ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)
                    RETURNING sequence
                    """,
                    (
                        delivery.delivery_id,
                        delivery.message_id,
                        delivery.team_run_id,
                        delivery.recipient_id,
                        message.created_at.isoformat(),
                        delivery.delivered_at.isoformat() if delivery.delivered_at else None,
                        delivery.acked_at.isoformat() if delivery.acked_at else None,
                    ),
                ).fetchone()[0]
                deliveries.append(delivery.model_copy(update={"cursor": int(cursor)}))
            self._append_event(
                connection,
                message.team_run_id,
                "team.message.projected",
                {"message_id": message.message_id, "recipients": list(message.recipient_ids)},
            )
            return ProjectionApplyResult(
                message=message,
                outbox=projection.outbox,
                deliveries=tuple(deliveries),
                created=True,
            )

    def get_message(self, message_id: str) -> MessageEnvelope | None:
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT body FROM team_messages WHERE id = ?", (message_id,)
            ).fetchone()
        if row is None:
            return None
        return MessageEnvelope.model_validate(json.loads(str(row["body"]))["message"])

    def list_messages(
        self, team_run_id: str, *, after_cursor: int = 0, limit: int = 100
    ) -> tuple[tuple[int, MessageEnvelope], ...]:
        _validate_page(after_cursor, limit)
        with self.store._connect() as connection:
            rows = connection.execute(
                """
                SELECT sequence, body FROM team_messages
                WHERE team_run_id = ? AND sequence > ? ORDER BY sequence LIMIT ?
                """,
                (team_run_id, after_cursor, limit),
            ).fetchall()
        return tuple(
            (
                int(row["sequence"]),
                MessageEnvelope.model_validate(json.loads(str(row["body"]))["message"]),
            )
            for row in rows
        )

    def list_messages_for_viewer(
        self,
        *,
        team_run_id: str,
        viewer_id: str,
        after_cursor: int = 0,
        limit: int = 100,
    ) -> tuple[tuple[int, MessageEnvelope], ...]:
        """Return a paginated UI projection without exposing owner/audit rows.

        Visibility is resolved in SQLite before ``LIMIT`` so a page containing
        private messages for other agents cannot hide later visible messages or
        force an unbounded application-side scan.
        """

        _validate_page(after_cursor, limit)
        with self.store._connect() as connection:
            rows = connection.execute(
                """
                SELECT m.sequence, m.body
                FROM team_messages AS m
                WHERE m.team_run_id = ? AND m.sequence > ?
                  AND json_extract(m.body, '$.message.ui_visibility')
                      NOT IN ('hidden', 'owner_audit')
                  AND (
                    m.sender_agent_id = ?
                    OR m.audience = 'broadcast'
                    OR json_extract(m.body, '$.message.ui_visibility') = 'team'
                    OR (
                      json_extract(m.body, '$.message.ui_visibility') = 'recipients'
                      AND EXISTS (
                        SELECT 1 FROM mailbox_deliveries AS d
                        WHERE d.message_id = m.id AND d.team_run_id = m.team_run_id
                          AND d.recipient_agent_id = ?
                      )
                    )
                  )
                ORDER BY m.sequence LIMIT ?
                """,
                (team_run_id, after_cursor, viewer_id, viewer_id, limit),
            ).fetchall()
        return tuple(
            (
                int(row["sequence"]),
                MessageEnvelope.model_validate(json.loads(str(row["body"]))["message"]),
            )
            for row in rows
        )

    def get_delivery(self, delivery_id: str) -> MailboxDelivery | None:
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT * FROM mailbox_deliveries WHERE id = ?", (delivery_id,)
            ).fetchone()
        return None if row is None else self._delivery(row)

    def list_inbox(
        self,
        *,
        team_run_id: str,
        recipient_id: str,
        after_cursor: int = 0,
        limit: int = 100,
    ) -> tuple[tuple[MailboxDelivery, MessageEnvelope], ...]:
        _validate_page(after_cursor, limit)
        with self.store._connect() as connection:
            rows = connection.execute(
                """
                SELECT d.*, m.body AS message_body
                FROM mailbox_deliveries AS d
                JOIN team_messages AS m ON m.id = d.message_id
                WHERE d.team_run_id = ? AND d.recipient_agent_id = ? AND d.sequence > ?
                ORDER BY d.sequence LIMIT ?
                """,
                (team_run_id, recipient_id, after_cursor, limit),
            ).fetchall()
        return tuple(
            (
                self._delivery(row),
                MessageEnvelope.model_validate(json.loads(str(row["message_body"]))["message"]),
            )
            for row in rows
        )

    def list_outbox(
        self,
        *,
        team_run_id: str,
        sender_id: str,
        after_cursor: int = 0,
        limit: int = 100,
    ) -> tuple[MessageEnvelope, ...]:
        _validate_page(after_cursor, limit)
        with self.store._connect() as connection:
            rows = connection.execute(
                """
                SELECT body FROM team_messages
                WHERE team_run_id = ? AND sender_agent_id = ? AND sequence > ?
                ORDER BY sequence LIMIT ?
                """,
                (team_run_id, sender_id, after_cursor, limit),
            ).fetchall()
        return tuple(
            MessageEnvelope.model_validate(json.loads(str(row["body"]))["message"]) for row in rows
        )

    def ack_delivery(self, ack: MessageAck) -> MessageAck:
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM mailbox_deliveries WHERE id = ?", (ack.delivery_id,)
            ).fetchone()
            if row is None:
                raise KeyError(ack.delivery_id)
            if (
                str(row["message_id"]) != ack.message_id
                or str(row["team_run_id"]) != ack.team_run_id
                or str(row["recipient_agent_id"]) != ack.recipient_id
                or int(row["sequence"]) != ack.cursor
            ):
                raise ValueError("Ack scope or cursor does not match the delivery")
            reused = connection.execute(
                """
                SELECT id FROM mailbox_deliveries
                WHERE recipient_agent_id = ? AND ack_idempotency_key = ?
                """,
                (ack.recipient_id, ack.idempotency_key),
            ).fetchone()
            if reused is not None and str(reused["id"]) != ack.delivery_id:
                raise ValueError("Ack idempotency key was reused for another delivery")
            if row["ack_idempotency_key"] is not None:
                if str(row["ack_idempotency_key"]) != ack.idempotency_key:
                    raise ValueError("delivery was already acknowledged with another key")
                if row["ack_body"] is None:
                    raise ValueError("stored Ack fact is unavailable")
                return MessageAck.model_validate_json(str(row["ack_body"]))
            connection.execute(
                """
                UPDATE mailbox_deliveries
                SET status = 'acknowledged', ack_idempotency_key = ?, ack_body = ?,
                    delivered_at = COALESCE(delivered_at, ?), acknowledged_at = ?
                WHERE id = ?
                """,
                (
                    ack.idempotency_key,
                    _json(ack),
                    ack.acked_at.isoformat(),
                    ack.acked_at.isoformat(),
                    ack.delivery_id,
                ),
            )
            self._append_event(
                connection,
                ack.team_run_id,
                "team.mailbox.acknowledged",
                {"delivery_id": ack.delivery_id, "recipient_id": ack.recipient_id},
            )
        return ack

    def apply_task_update(self, update: TaskBoardUpdate) -> BoardApplyResult:
        task = update.task
        request_hash = _hash(update)
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM team_tasks WHERE id = ?", (task.task_id,)
            ).fetchone()
            if row is None:
                if update.expected_revision not in {None, 0}:
                    raise ValueError("Task Board revision conflict")
                history = {
                    update.idempotency_key: {"hash": request_hash, "revision": task.revision}
                }
                wrapper = {"task": task.model_dump(mode="json"), "idempotency": history}
                body = _json(wrapper)
                connection.execute(
                    """
                    INSERT INTO team_tasks(
                        id, team_run_id, assignee_agent_id, status, version,
                        body, body_hash, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task.task_id,
                        task.team_run_id,
                        task.assignee_ids[0] if len(task.assignee_ids) == 1 else None,
                        task.status.value,
                        task.revision,
                        body,
                        hashlib.sha256(body.encode("utf-8")).hexdigest(),
                        task.updated_at.isoformat(),
                        task.updated_at.isoformat(),
                    ),
                )
                created = True
            else:
                wrapper = json.loads(str(row["body"]))
                history = wrapper.get("idempotency", {})
                prior = history.get(update.idempotency_key)
                if prior is not None:
                    if prior["hash"] != request_hash:
                        raise ValueError("Task Board idempotency key conflict")
                    return BoardApplyResult(revision=int(prior["revision"]), created=False)
                current = TeamTask.model_validate(wrapper["task"])
                if (
                    update.expected_revision != current.revision
                    or task.revision != current.revision + 1
                ):
                    raise ValueError("Task Board revision conflict")
                history[update.idempotency_key] = {"hash": request_hash, "revision": task.revision}
                body = _json({"task": task.model_dump(mode="json"), "idempotency": history})
                connection.execute(
                    """
                    UPDATE team_tasks SET assignee_agent_id = ?, status = ?, version = ?,
                        body = ?, body_hash = ?, updated_at = ? WHERE id = ?
                    """,
                    (
                        task.assignee_ids[0] if len(task.assignee_ids) == 1 else None,
                        task.status.value,
                        task.revision,
                        body,
                        hashlib.sha256(body.encode("utf-8")).hexdigest(),
                        task.updated_at.isoformat(),
                        task.task_id,
                    ),
                )
                created = False
            self._append_event(
                connection,
                task.team_run_id,
                "team.task.updated",
                {"task_id": task.task_id, "revision": task.revision},
            )
        return BoardApplyResult(revision=task.revision, created=created)

    def apply_artifact_update(self, update: ArtifactBoardUpdate) -> BoardApplyResult:
        artifact = update.artifact
        request_hash = _hash(update)
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM artifact_board_items WHERE team_run_id = ? AND artifact_id = ?",
                (artifact.team_run_id, artifact.artifact_id),
            ).fetchone()
            if row is None:
                if update.expected_revision not in {None, 0}:
                    raise ValueError("Artifact Board revision conflict")
                wrapper = {
                    "artifact": artifact.model_dump(mode="json"),
                    "idempotency": {
                        update.idempotency_key: {
                            "hash": request_hash,
                            "revision": artifact.revision,
                        }
                    },
                }
                connection.execute(
                    """
                    INSERT INTO artifact_board_items(
                        id, team_run_id, artifact_id, published_by_agent_id,
                        message_id, body, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"board_{artifact.team_run_id}_{artifact.artifact_id}",
                        artifact.team_run_id,
                        artifact.artifact_id,
                        artifact.publisher_id,
                        artifact.source_message_id,
                        _json(wrapper),
                        artifact.published_at.isoformat(),
                    ),
                )
                created = True
            else:
                wrapper = json.loads(str(row["body"]))
                history = wrapper.get("idempotency", {})
                prior = history.get(update.idempotency_key)
                if prior is not None:
                    if prior["hash"] != request_hash:
                        raise ValueError("Artifact Board idempotency key conflict")
                    return BoardApplyResult(revision=int(prior["revision"]), created=False)
                current = TeamArtifact.model_validate(wrapper["artifact"])
                if (
                    update.expected_revision != current.revision
                    or artifact.revision != current.revision + 1
                ):
                    raise ValueError("Artifact Board revision conflict")
                history[update.idempotency_key] = {
                    "hash": request_hash,
                    "revision": artifact.revision,
                }
                connection.execute(
                    """
                    UPDATE artifact_board_items
                    SET published_by_agent_id = ?, message_id = ?, body = ?
                    WHERE id = ?
                    """,
                    (
                        artifact.publisher_id,
                        artifact.source_message_id,
                        _json(
                            {"artifact": artifact.model_dump(mode="json"), "idempotency": history}
                        ),
                        row["id"],
                    ),
                )
                created = False
            self._append_event(
                connection,
                artifact.team_run_id,
                "team.artifact.published",
                {"artifact_id": artifact.artifact_id, "revision": artifact.revision},
            )
        return BoardApplyResult(revision=artifact.revision, created=created)

    def list_tasks(self, team_run_id: str) -> tuple[TeamTask, ...]:
        with self.store._connect() as connection:
            rows = connection.execute(
                "SELECT body FROM team_tasks WHERE team_run_id = ? ORDER BY sequence",
                (team_run_id,),
            ).fetchall()
        return tuple(TeamTask.model_validate(json.loads(str(row["body"]))["task"]) for row in rows)

    def list_artifacts(
        self, *, team_run_id: str, viewer_id: str, owner_audit: bool = False
    ) -> tuple[TeamArtifact, ...]:
        with self.store._connect() as connection:
            rows = connection.execute(
                "SELECT body FROM artifact_board_items WHERE team_run_id = ? ORDER BY sequence",
                (team_run_id,),
            ).fetchall()
        artifacts = tuple(
            TeamArtifact.model_validate(json.loads(str(row["body"]))["artifact"]) for row in rows
        )
        if owner_audit:
            return artifacts
        return tuple(
            artifact
            for artifact in artifacts
            if artifact.visibility.value == "team"
            or artifact.publisher_id == viewer_id
            or viewer_id in artifact.recipient_ids
        )

    def list_events(
        self, team_run_id: str, *, after_cursor: int = 0, limit: int = 1000
    ) -> list[dict[str, Any]]:
        _validate_page(after_cursor, limit)
        with self.store._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, sequence, run_sequence, schema_version, event_type, body, created_at
                FROM team_run_events
                WHERE team_run_id = ? AND sequence > ? ORDER BY sequence LIMIT ?
                """,
                (team_run_id, after_cursor, limit),
            ).fetchall()
        return [
            {
                "event_id": str(row["id"]),
                "cursor": int(row["sequence"]),
                "run_sequence": int(row["run_sequence"]),
                "schema_version": str(row["schema_version"]),
                "event_type": str(row["event_type"]),
                "resource_scope": f"team_run:{team_run_id}",
                "stream_kind": "team.run",
                "payload": json.loads(str(row["body"])),
                "occurred_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def _projection_from_wrapper(
        self, connection: sqlite3.Connection, wrapper: dict[str, Any], *, created: bool
    ) -> ProjectionApplyResult:
        message = MessageEnvelope.model_validate(wrapper["message"])
        outbox = OutboxItem.model_validate(wrapper["outbox"])
        rows = connection.execute(
            "SELECT * FROM mailbox_deliveries WHERE message_id = ? ORDER BY sequence",
            (message.message_id,),
        ).fetchall()
        return ProjectionApplyResult(
            message=message,
            outbox=outbox,
            deliveries=tuple(self._delivery(row) for row in rows),
            created=created,
        )

    @staticmethod
    def _delivery(row: sqlite3.Row) -> MailboxDelivery:
        raw_status = str(row["status"])
        status = (
            DeliveryStatus.ACKED if raw_status == "acknowledged" else DeliveryStatus(raw_status)
        )
        return MailboxDelivery(
            delivery_id=str(row["id"]),
            message_id=str(row["message_id"]),
            team_run_id=str(row["team_run_id"]),
            recipient_id=str(row["recipient_agent_id"]),
            idempotency_key=f"message:{row['message_id']}:recipient:{row['recipient_agent_id']}",
            status=status,
            cursor=int(row["sequence"]),
            delivered_at=datetime.fromisoformat(str(row["delivered_at"]))
            if row["delivered_at"]
            else None,
            acked_at=datetime.fromisoformat(str(row["acknowledged_at"]))
            if row["acknowledged_at"]
            else None,
        )

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection, team_run_id: str, event_type: str, payload: Any
    ) -> None:
        next_sequence = int(
            connection.execute(
                """
                SELECT COALESCE(MAX(run_sequence), 0) + 1
                FROM team_run_events WHERE team_run_id = ?
                """,
                (team_run_id,),
            ).fetchone()[0]
        )
        event_id = f"team_event_{team_run_id}_{next_sequence}"
        connection.execute(
            """
            INSERT INTO team_run_events(
                id, team_run_id, run_sequence, schema_version, event_type, body, created_at
            ) VALUES (?, ?, ?, 'phase23.v1', ?, ?, ?)
            """,
            (
                event_id,
                team_run_id,
                next_sequence,
                event_type,
                _json(payload),
                _now(),
            ),
        )


def _validate_page(after_cursor: int, limit: int) -> None:
    if after_cursor < 0 or not 1 <= limit <= 1000:
        raise ValueError("invalid cursor or limit")


def _snake_message_kind(kind: MessageKind) -> str:
    output = []
    for index, character in enumerate(kind.value):
        if index and character.isupper():
            output.append("_")
        output.append(character.lower())
    return "".join(output)
