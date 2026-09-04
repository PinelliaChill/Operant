from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from operant.application.graph import GraphConflictError
from operant.domain.multiwriter import WriterLease
from operant.persistence.multiwriter import SQLiteMultiWriterRepository
from operant.persistence.sqlite import NotFoundError, SQLiteStore


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class SQLiteContainerLifecycleRepository:
    """v14 Container Writer projection with durable transitions and fencing."""

    _INTERMEDIATE = frozenset({"creating", "stopping", "removing"})
    _KNOWN = frozenset({"created", "running", "stopped", "removed"})

    def __init__(self, store: SQLiteStore) -> None:
        self.store = store
        self.writer_repository = SQLiteMultiWriterRepository(store)

    def get(self, workspace_id: str) -> dict[str, Any]:
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT * FROM writer_container_lifecycles WHERE writer_workspace_id = ?",
                (workspace_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"Container Writer lifecycle not found: {workspace_id}")
        return self._projection(row)

    def begin_create(
        self,
        *,
        workspace_id: str,
        container_name: str,
        image_ref: str,
        mount_ref: str,
        resource_limits: dict[str, Any],
        lease: WriterLease,
        action_hash: str,
    ) -> dict[str, Any]:
        at = _now()
        self._validate_binding(workspace_id, lease)
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.writer_repository.assert_lease(lease, connection=connection)
            existing = connection.execute(
                "SELECT * FROM writer_container_lifecycles WHERE writer_workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            if existing is not None:
                raise GraphConflictError("Container Writer lifecycle already exists")
            connection.execute(
                """
                INSERT INTO writer_container_lifecycles(
                    writer_workspace_id, container_name, image_ref, mount_ref, status,
                    revision, owner_id, fencing, lease_expires_at, action_hash,
                    resource_limits_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'creating', 1, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workspace_id,
                    container_name,
                    image_ref,
                    mount_ref,
                    lease.owner,
                    lease.fencing,
                    lease.expires_at.isoformat(),
                    action_hash,
                    _json(resource_limits),
                    at.isoformat(),
                    at.isoformat(),
                ),
            )
            self._append_event(
                connection,
                workspace_id=workspace_id,
                revision=1,
                event_type="container.create_started",
                body={"status": "creating"},
                action_hash=action_hash,
                at=at,
            )
            row = connection.execute(
                "SELECT * FROM writer_container_lifecycles WHERE writer_workspace_id = ?",
                (workspace_id,),
            ).fetchone()
        assert row is not None
        return self._projection(row)

    def begin_action(
        self,
        workspace_id: str,
        *,
        lease: WriterLease,
        action_hash: str,
        operation: str,
    ) -> dict[str, Any]:
        transitions = {
            "start": ({"created", "stopped"}, "creating"),
            "stop": ({"running"}, "stopping"),
            "remove": ({"created", "stopped"}, "removing"),
        }
        if operation not in transitions:
            raise ValueError("unsupported Container Writer operation")
        expected, pending = transitions[operation]
        at = _now()
        self._validate_binding(workspace_id, lease)
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.writer_repository.assert_lease(lease, connection=connection)
            current = connection.execute(
                "SELECT * FROM writer_container_lifecycles WHERE writer_workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            if current is None:
                raise NotFoundError(f"Container Writer lifecycle not found: {workspace_id}")
            if str(current["status"]) not in expected:
                raise GraphConflictError(
                    f"Container Writer cannot {operation} from {current['status']}"
                )
            revision = int(current["revision"]) + 1
            result = connection.execute(
                """
                UPDATE writer_container_lifecycles
                SET status = ?, revision = ?, owner_id = ?, fencing = ?,
                    lease_expires_at = ?, action_hash = ?, last_error_code = NULL,
                    updated_at = ?
                WHERE writer_workspace_id = ? AND revision = ?
                """,
                (
                    pending,
                    revision,
                    lease.owner,
                    lease.fencing,
                    lease.expires_at.isoformat(),
                    action_hash,
                    at.isoformat(),
                    workspace_id,
                    int(current["revision"]),
                ),
            )
            if result.rowcount != 1:
                raise GraphConflictError("Container Writer lifecycle was concurrently updated")
            self._append_event(
                connection,
                workspace_id=workspace_id,
                revision=revision,
                event_type=f"container.{operation}_started",
                body={"status": pending},
                action_hash=action_hash,
                at=at,
            )
            row = connection.execute(
                "SELECT * FROM writer_container_lifecycles WHERE writer_workspace_id = ?",
                (workspace_id,),
            ).fetchone()
        assert row is not None
        return self._projection(row)

    def complete(
        self,
        workspace_id: str,
        *,
        lease: WriterLease,
        action_hash: str,
        status: str,
    ) -> dict[str, Any]:
        if status not in self._KNOWN:
            raise ValueError("Container Writer completion status is invalid")
        return self._finish(
            workspace_id,
            lease=lease,
            action_hash=action_hash,
            status=status,
            error_code=None,
            event_type=f"container.{status}",
        )

    def mark_unknown(
        self,
        workspace_id: str,
        *,
        lease: WriterLease,
        action_hash: str,
        error_code: str,
    ) -> dict[str, Any]:
        return self._finish(
            workspace_id,
            lease=lease,
            action_hash=action_hash,
            status="outcome_unknown",
            error_code=error_code,
            event_type="container.outcome_unknown",
        )

    def reconcile(
        self,
        workspace_id: str,
        *,
        lease: WriterLease,
        action_hash: str,
        status: str,
    ) -> dict[str, Any]:
        if status not in self._KNOWN:
            raise ValueError("Container Writer reconciliation status is invalid")
        at = _now()
        self._validate_binding(workspace_id, lease)
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.writer_repository.assert_lease(lease, connection=connection)
            current = connection.execute(
                "SELECT * FROM writer_container_lifecycles WHERE writer_workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            if current is None:
                raise NotFoundError(f"Container Writer lifecycle not found: {workspace_id}")
            if current["status"] != "outcome_unknown":
                raise GraphConflictError("only outcome-unknown Container Writers can reconcile")
            revision = int(current["revision"]) + 1
            connection.execute(
                """
                UPDATE writer_container_lifecycles
                SET status = ?, revision = ?, owner_id = NULL,
                    fencing = ?, lease_expires_at = NULL, action_hash = ?,
                    last_error_code = NULL, updated_at = ?,
                    stopped_at = CASE WHEN ? = 'stopped' THEN ? ELSE stopped_at END,
                    removed_at = CASE WHEN ? = 'removed' THEN ? ELSE NULL END
                WHERE writer_workspace_id = ? AND revision = ?
                """,
                (
                    status,
                    revision,
                    lease.fencing,
                    action_hash,
                    at.isoformat(),
                    status,
                    at.isoformat(),
                    status,
                    at.isoformat(),
                    workspace_id,
                    int(current["revision"]),
                ),
            )
            self._append_event(
                connection,
                workspace_id=workspace_id,
                revision=revision,
                event_type="container.reconciled",
                body={"status": status},
                action_hash=action_hash,
                at=at,
            )
            row = connection.execute(
                "SELECT * FROM writer_container_lifecycles WHERE writer_workspace_id = ?",
                (workspace_id,),
            ).fetchone()
        assert row is not None
        return self._projection(row)

    def _finish(
        self,
        workspace_id: str,
        *,
        lease: WriterLease,
        action_hash: str,
        status: str,
        error_code: str | None,
        event_type: str,
    ) -> dict[str, Any]:
        at = _now()
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM writer_container_lifecycles WHERE writer_workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            if current is None:
                raise NotFoundError(f"Container Writer lifecycle not found: {workspace_id}")
            if (
                current["status"] not in self._INTERMEDIATE
                or current["owner_id"] != lease.owner
                or int(current["fencing"]) != lease.fencing
                or current["action_hash"] != action_hash
            ):
                raise GraphConflictError("Container Writer operation was fenced")
            revision = int(current["revision"]) + 1
            connection.execute(
                """
                UPDATE writer_container_lifecycles
                SET status = ?, revision = ?,
                    owner_id = CASE WHEN ? = 'running' THEN owner_id ELSE NULL END,
                    lease_expires_at = CASE
                        WHEN ? = 'running' THEN lease_expires_at ELSE NULL
                    END,
                    last_error_code = ?, updated_at = ?,
                    stopped_at = CASE WHEN ? = 'stopped' THEN ? ELSE stopped_at END,
                    removed_at = CASE WHEN ? = 'removed' THEN ? ELSE removed_at END
                WHERE writer_workspace_id = ? AND revision = ?
                """,
                (
                    status,
                    revision,
                    status,
                    status,
                    error_code,
                    at.isoformat(),
                    status,
                    at.isoformat(),
                    status,
                    at.isoformat(),
                    workspace_id,
                    int(current["revision"]),
                ),
            )
            self._append_event(
                connection,
                workspace_id=workspace_id,
                revision=revision,
                event_type=event_type,
                body={"status": status, "error_code": error_code},
                action_hash=action_hash,
                at=at,
            )
            row = connection.execute(
                "SELECT * FROM writer_container_lifecycles WHERE writer_workspace_id = ?",
                (workspace_id,),
            ).fetchone()
        assert row is not None
        return self._projection(row)

    @staticmethod
    def _validate_binding(workspace_id: str, lease: WriterLease) -> None:
        if lease.writer_workspace_id != workspace_id:
            raise GraphConflictError("writer lease belongs to another workspace")

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        *,
        workspace_id: str,
        revision: int,
        event_type: str,
        body: dict[str, Any],
        action_hash: str,
        at: datetime,
    ) -> None:
        connection.execute(
            """
            INSERT INTO writer_container_lifecycle_events(
                id, writer_workspace_id, revision, event_type, body_json,
                action_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"container_event_{uuid4().hex}",
                workspace_id,
                revision,
                event_type,
                _json(body),
                action_hash,
                at.isoformat(),
            ),
        )

    @staticmethod
    def _projection(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "writer_workspace_id": row["writer_workspace_id"],
            "container_name": row["container_name"],
            "image_ref": row["image_ref"],
            "mount_ref": row["mount_ref"],
            "status": row["status"],
            "revision": int(row["revision"]),
            "owner_id": row["owner_id"],
            "fencing": int(row["fencing"]),
            "lease_expires_at": row["lease_expires_at"],
            "action_hash": row["action_hash"],
            "resource_limits": json.loads(row["resource_limits_json"]),
            "last_error_code": row["last_error_code"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "stopped_at": row["stopped_at"],
            "removed_at": row["removed_at"],
        }


class SQLiteRemoteGatewayConnectionRepository:
    """v14 durable direct-Gateway connection registry owned by one Core process."""

    def __init__(
        self,
        store: SQLiteStore,
        *,
        owner_id: str | None = None,
        lease_seconds: int = 30,
    ) -> None:
        if not 3 <= lease_seconds <= 300:
            raise ValueError("Gateway connection lease must be between 3 and 300 seconds")
        self.store = store
        self.owner_id = owner_id or f"gateway-core:{uuid4().hex}"
        self.lease_seconds = lease_seconds
        self.reconcile_expired()

    def open(
        self,
        *,
        remote_session_id: str,
        device_id: str,
        event_cursor: int,
    ) -> str:
        at = _now()
        if event_cursor < 0:
            raise ValueError("Gateway event cursor must be non-negative")
        connection_id = f"gateway_connection_{uuid4().hex}"
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._reconcile_expired(connection, at)
            row = connection.execute(
                "SELECT COALESCE(MAX(fencing), 0) AS fencing "
                "FROM remote_gateway_connections WHERE remote_session_id = ?",
                (remote_session_id,),
            ).fetchone()
            fencing = 1 if row is None else int(row["fencing"]) + 1
            try:
                connection.execute(
                    """
                    INSERT INTO remote_gateway_connections(
                        connection_id, remote_session_id, device_id, transport_mode,
                        event_cursor, status, owner_id, fencing, lease_expires_at,
                        last_seen_at, created_at, updated_at
                    ) VALUES (?, ?, ?, 'direct', ?, 'connected', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        connection_id,
                        remote_session_id,
                        device_id,
                        event_cursor,
                        self.owner_id,
                        fencing,
                        (at + timedelta(seconds=self.lease_seconds)).isoformat(),
                        at.isoformat(),
                        at.isoformat(),
                        at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise GraphConflictError(
                    "remote session already has an active Gateway connection"
                ) from exc
            self._append_gateway_event(
                connection,
                connection_id=connection_id,
                event_type="gateway.connected",
                body={"event_cursor": event_cursor, "transport_mode": "direct"},
                at=at,
            )
        return connection_id

    def heartbeat(self, connection_id: str, *, event_cursor: int | None = None) -> None:
        at = _now()
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._owned(connection, connection_id)
            cursor = int(row["event_cursor"])
            if event_cursor is not None:
                if event_cursor < cursor:
                    raise GraphConflictError("Gateway cursor cannot move backwards")
                cursor = event_cursor
            updated = connection.execute(
                """
                UPDATE remote_gateway_connections
                SET event_cursor = ?, last_seen_at = ?, lease_expires_at = ?, updated_at = ?
                WHERE connection_id = ? AND owner_id = ? AND fencing = ?
                    AND status = 'connected'
                """,
                (
                    cursor,
                    at.isoformat(),
                    (at + timedelta(seconds=self.lease_seconds)).isoformat(),
                    at.isoformat(),
                    connection_id,
                    self.owner_id,
                    int(row["fencing"]),
                ),
            )
            if updated.rowcount != 1:
                raise GraphConflictError("Gateway connection was fenced")

    def close(self, connection_id: str, *, error_code: str | None = None) -> None:
        at = _now()
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM remote_gateway_connections WHERE connection_id = ?",
                (connection_id,),
            ).fetchone()
            if row is None or row["status"] == "closed":
                return
            if row["owner_id"] != self.owner_id:
                raise GraphConflictError("Gateway connection is owned by another Core")
            connection.execute(
                """
                UPDATE remote_gateway_connections
                SET status = 'closed', closed_at = ?, error_code = ?, updated_at = ?,
                    lease_expires_at = ?
                WHERE connection_id = ? AND owner_id = ? AND fencing = ?
                """,
                (
                    at.isoformat(),
                    error_code,
                    at.isoformat(),
                    at.isoformat(),
                    connection_id,
                    self.owner_id,
                    int(row["fencing"]),
                ),
            )
            self._append_gateway_event(
                connection,
                connection_id=connection_id,
                event_type="gateway.closed",
                body={"error_code": error_code},
                at=at,
            )

    def list(self, *, limit: int = 200) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("Gateway connection limit is invalid")
        with self.store._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM remote_gateway_connections ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._gateway_projection(row) for row in rows]

    def reconcile_expired(self) -> int:
        at = _now()
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._reconcile_expired(connection, at)

    def _owned(self, connection: sqlite3.Connection, connection_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM remote_gateway_connections WHERE connection_id = ?",
            (connection_id,),
        ).fetchone()
        if row is None or row["owner_id"] != self.owner_id or row["status"] != "connected":
            raise GraphConflictError("Gateway connection is missing, closed, or fenced")
        return row

    @staticmethod
    def _reconcile_expired(connection: sqlite3.Connection, at: datetime) -> int:
        return int(
            connection.execute(
                """
                UPDATE remote_gateway_connections
                SET status = 'disconnected', error_code = 'gateway.owner_lease_expired',
                    updated_at = ?
                WHERE status IN ('connecting', 'connected', 'backoff')
                    AND lease_expires_at <= ?
                """,
                (at.isoformat(), at.isoformat()),
            ).rowcount
        )

    @staticmethod
    def _append_gateway_event(
        connection: sqlite3.Connection,
        *,
        connection_id: str,
        event_type: str,
        body: dict[str, Any],
        at: datetime,
    ) -> None:
        connection.execute(
            """
            INSERT INTO remote_gateway_events(
                id, connection_id, event_type, body_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                f"gateway_event_{uuid4().hex}",
                connection_id,
                event_type,
                _json(body),
                at.isoformat(),
            ),
        )

    @staticmethod
    def _gateway_projection(row: sqlite3.Row) -> dict[str, Any]:
        return {
            key: row[key]
            for key in (
                "connection_id",
                "remote_session_id",
                "device_id",
                "transport_mode",
                "event_cursor",
                "status",
                "owner_id",
                "fencing",
                "lease_expires_at",
                "last_seen_at",
                "closed_at",
                "error_code",
                "created_at",
                "updated_at",
            )
        }
