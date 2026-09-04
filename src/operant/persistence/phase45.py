from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timezone
from typing import Any

from operant.mcp import McpTool
from operant.persistence.sqlite import ConflictError, SQLiteStore
from operant.skills import DiscoveredSkill


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class SQLitePhase45Repository:
    """Durable Skill/MCP projections backed by additive v11/v12 schemas."""

    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    @staticmethod
    def skill_root_ref(root: str) -> str:
        digest = hashlib.sha256(root.encode("utf-8")).hexdigest()
        return f"skill-root:{digest}"

    def replace_skill_candidates(
        self, root_ref: str, candidates: tuple[DiscoveredSkill, ...]
    ) -> tuple[dict[str, Any], ...]:
        discovered_at = _now()
        rows: list[dict[str, Any]] = []
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM skill_candidates WHERE root_ref = ?", (root_ref,))
            for candidate in candidates:
                snapshot = candidate.model_dump(mode="json")
                candidate_id = hashlib.sha256(
                    f"{root_ref}\0{candidate.relative_directory}\0{candidate.manifest_sha256}".encode()
                ).hexdigest()
                connection.execute(
                    """
                    INSERT INTO skill_candidates(
                        candidate_id, root_ref, relative_directory, name, description,
                        manifest_sha256, snapshot_json, trust_status, discovered_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'untrusted_candidate', ?)
                    """,
                    (
                        candidate_id,
                        root_ref,
                        candidate.relative_directory,
                        candidate.name,
                        candidate.description,
                        candidate.manifest_sha256,
                        _json(snapshot),
                        discovered_at,
                    ),
                )
                rows.append(self._skill_projection(candidate_id, root_ref, snapshot, discovered_at))
        return tuple(rows)

    def list_skill_candidates(self, *, limit: int = 200) -> tuple[dict[str, Any], ...]:
        if not 1 <= limit <= 500:
            raise ValueError("skill candidate limit is invalid")
        with self.store._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM skill_candidates ORDER BY discovered_at DESC, candidate_id LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(
            self._skill_projection(
                str(row["candidate_id"]),
                str(row["root_ref"]),
                json.loads(row["snapshot_json"]),
                str(row["discovered_at"]),
            )
            for row in rows
        )

    @staticmethod
    def _skill_projection(
        candidate_id: str, root_ref: str, snapshot: dict[str, Any], discovered_at: str
    ) -> dict[str, Any]:
        return {
            "candidate_id": candidate_id,
            "root_ref": root_ref,
            "relative_directory": snapshot["relative_directory"],
            "name": snapshot["name"],
            "description": snapshot["description"],
            "manifest_sha256": snapshot["manifest_sha256"],
            "resources": snapshot.get("resources", []),
            "trust_status": "untrusted_candidate",
            "discovered_at": discovered_at,
        }

    def put_mcp_server(self, config: dict[str, Any], *, create_only: bool) -> dict[str, Any]:
        server_id = str(config["server_id"])
        now = _now()
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT lifecycle_status, created_at FROM mcp_servers WHERE server_id = ?",
                (server_id,),
            ).fetchone()
            if create_only and existing is not None:
                raise ConflictError("MCP server already exists")
            if existing is not None and existing["lifecycle_status"] == "deleted":
                raise ConflictError("deleted MCP server IDs cannot be reused")
            if existing is not None and existing["lifecycle_status"] not in {"stopped", "failed"}:
                raise ConflictError("running MCP server configuration is immutable")
            created_at = now if existing is None else str(existing["created_at"])
            connection.execute(
                """
                INSERT INTO mcp_servers(
                    server_id, transport, endpoint_ref, secret_ref, stdio_argv_json,
                    cwd_ref, environment_refs_json, allow_loopback_http,
                    lifecycle_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'stopped', ?, ?)
                ON CONFLICT(server_id) DO UPDATE SET
                    transport=excluded.transport, endpoint_ref=excluded.endpoint_ref,
                    secret_ref=excluded.secret_ref, stdio_argv_json=excluded.stdio_argv_json,
                    cwd_ref=excluded.cwd_ref, environment_refs_json=excluded.environment_refs_json,
                    allow_loopback_http=excluded.allow_loopback_http,
                    updated_at=excluded.updated_at
                """,
                (
                    server_id,
                    config["transport"],
                    config.get("endpoint_ref"),
                    config.get("secret_ref"),
                    None if config.get("stdio_argv") is None else _json(config["stdio_argv"]),
                    config.get("cwd_ref"),
                    _json(config.get("environment_refs", {})),
                    int(bool(config.get("allow_loopback_http", False))),
                    created_at,
                    now,
                ),
            )
            if config["transport"] == "stdio":
                connection.execute(
                    """
                    INSERT INTO mcp_stdio_sandboxes(server_id, workspace_root_ref, docker_image)
                    VALUES (?, ?, ?)
                    ON CONFLICT(server_id) DO UPDATE SET
                        workspace_root_ref=excluded.workspace_root_ref,
                        docker_image=excluded.docker_image
                    """,
                    (
                        server_id,
                        config["workspace_root_ref"],
                        config["docker_image"],
                    ),
                )
            else:
                connection.execute(
                    "DELETE FROM mcp_stdio_sandboxes WHERE server_id = ?",
                    (server_id,),
                )
        return self.get_mcp_server(server_id)

    def get_mcp_server(self, server_id: str) -> dict[str, Any]:
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT * FROM mcp_servers WHERE server_id = ?", (server_id,)
            ).fetchone()
            sandbox = connection.execute(
                "SELECT * FROM mcp_stdio_sandboxes WHERE server_id = ?", (server_id,)
            ).fetchone()
        if row is None:
            raise KeyError(server_id)
        if row["lifecycle_status"] == "deleted":
            raise KeyError(server_id)
        return self._server(row, sandbox)

    def list_mcp_servers(self) -> tuple[dict[str, Any], ...]:
        with self.store._connect() as connection:
            rows = connection.execute(
                """
                SELECT mcp_servers.*, mcp_stdio_sandboxes.workspace_root_ref,
                       mcp_stdio_sandboxes.docker_image
                FROM mcp_servers LEFT JOIN mcp_stdio_sandboxes USING(server_id)
                WHERE lifecycle_status != 'deleted' ORDER BY server_id
                """
            ).fetchall()
        return tuple(self._server(row, row) for row in rows)

    def reconcile_mcp_lifecycle(self) -> int:
        """Fail closed for process-local transports lost across Core restart."""

        now = _now()
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT server_id FROM mcp_servers "
                "WHERE lifecycle_status IN ('starting','running') ORDER BY server_id"
            ).fetchall()
            for row in rows:
                server_id = str(row["server_id"])
                event_id = hashlib.sha256(
                    f"{server_id}\0mcp.interrupted\0{now}".encode()
                ).hexdigest()
                connection.execute(
                    "UPDATE mcp_servers SET lifecycle_status='failed', updated_at=? "
                    "WHERE server_id=?",
                    (now, server_id),
                )
                connection.execute(
                    "INSERT INTO mcp_lifecycle_events(id,server_id,event_type,lifecycle_status,"
                    "detail_json,security_audit_event_id,created_at) "
                    "VALUES (?,?,'mcp.interrupted','failed','{}',NULL,?)",
                    (event_id, server_id, now),
                )
        return len(rows)

    @staticmethod
    def _server(row: Any, sandbox: Any | None = None) -> dict[str, Any]:
        return {
            "server_id": row["server_id"],
            "transport": row["transport"],
            "endpoint_ref": row["endpoint_ref"],
            "secret_ref": row["secret_ref"],
            "stdio_argv": None
            if row["stdio_argv_json"] is None
            else json.loads(row["stdio_argv_json"]),
            "cwd_ref": row["cwd_ref"],
            "environment_refs": json.loads(row["environment_refs_json"]),
            "allow_loopback_http": bool(row["allow_loopback_http"]),
            "lifecycle_status": row["lifecycle_status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "workspace_root_ref": None if sandbox is None else sandbox["workspace_root_ref"],
            "docker_image": None if sandbox is None else sandbox["docker_image"],
        }

    def claim_mcp_start(
        self, server_id: str, owner: str, *, expected_updated_at: str, ttl_seconds: int = 60
    ) -> tuple[str, int]:
        now = datetime.now(timezone.utc)
        expires_at = datetime.fromtimestamp(now.timestamp() + ttl_seconds, timezone.utc)
        token = secrets.token_urlsafe(24)
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT lifecycle_status, updated_at FROM mcp_servers WHERE server_id = ?",
                (server_id,),
            ).fetchone()
            if row is None:
                raise KeyError(server_id)
            if row["lifecycle_status"] not in {"stopped", "failed"}:
                raise ConflictError("MCP server is already starting or running")
            if row["updated_at"] != expected_updated_at:
                raise ConflictError("MCP server configuration changed before start")
            lease = connection.execute(
                "SELECT fencing FROM mcp_server_start_leases WHERE server_id = ?", (server_id,)
            ).fetchone()
            fencing = 1 if lease is None else int(lease["fencing"]) + 1
            connection.execute(
                """
                INSERT INTO mcp_server_start_leases(server_id, owner, token, fencing, expires_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(server_id) DO UPDATE SET owner=excluded.owner, token=excluded.token,
                    fencing=excluded.fencing, expires_at=excluded.expires_at
                """,
                (server_id, owner, token, fencing, expires_at.isoformat()),
            )
            changed = connection.execute(
                "UPDATE mcp_servers SET lifecycle_status='starting', updated_at=? "
                "WHERE server_id=? AND lifecycle_status IN ('stopped','failed')",
                (now.isoformat(), server_id),
            ).rowcount
            if changed != 1:
                raise ConflictError("MCP server start lost its lifecycle fence")
        return token, fencing

    def finish_mcp_start(
        self,
        server_id: str,
        token: str,
        fencing: int,
        *,
        status: str,
        event_type: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        if status not in {"running", "failed", "stopped"}:
            raise ValueError("invalid fenced MCP lifecycle status")
        now = _now()
        event_id = hashlib.sha256(f"{server_id}\0{event_type}\0{now}".encode()).hexdigest()
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lease = connection.execute(
                "SELECT token, fencing, expires_at FROM mcp_server_start_leases WHERE server_id=?",
                (server_id,),
            ).fetchone()
            if (
                lease is None
                or lease["token"] != token
                or int(lease["fencing"]) != fencing
                or lease["expires_at"] is None
                or str(lease["expires_at"]) <= now
            ):
                raise ConflictError("stale MCP lifecycle fence")
            changed = connection.execute(
                "UPDATE mcp_servers SET lifecycle_status=?, updated_at=? "
                "WHERE server_id=? AND lifecycle_status='starting'",
                (status, now, server_id),
            ).rowcount
            if changed != 1:
                raise ConflictError("MCP lifecycle is no longer starting")
            connection.execute(
                "UPDATE mcp_server_start_leases SET owner=NULL, token=NULL, expires_at=NULL "
                "WHERE server_id=? AND token=? AND fencing=?",
                (server_id, token, fencing),
            )
            connection.execute(
                "INSERT INTO mcp_lifecycle_events(id,server_id,event_type,lifecycle_status,"
                "detail_json,security_audit_event_id,created_at) VALUES (?,?,?,?,?,NULL,?)",
                (event_id, server_id, event_type, status, _json(detail or {}), now),
            )

    def reserve_mcp_action(
        self,
        *,
        action_hash: str,
        server_id: str,
        tool_name: str,
        schema_sha256: str,
        arguments_sha256: str,
    ) -> dict[str, Any]:
        now = _now()
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM mcp_action_receipts WHERE action_hash=?", (action_hash,)
            ).fetchone()
            if row is None:
                connection.execute(
                    """INSERT INTO mcp_action_receipts(
                    action_hash,server_id,tool_name,schema_sha256,arguments_sha256,status,
                    result_json,error_code,created_at,updated_at,completed_at)
                    VALUES (?,?,?,?,?,'reserved',NULL,NULL,?,?,NULL)""",
                    (action_hash, server_id, tool_name, schema_sha256, arguments_sha256, now, now),
                )
                row = connection.execute(
                    "SELECT * FROM mcp_action_receipts WHERE action_hash=?", (action_hash,)
                ).fetchone()
            assert row is not None
            if any(
                (
                    row["server_id"] != server_id,
                    row["tool_name"] != tool_name,
                    row["schema_sha256"] != schema_sha256,
                    row["arguments_sha256"] != arguments_sha256,
                )
            ):
                raise ConflictError("MCP action hash is bound to different call inputs")
            return dict(row)

    def mark_mcp_action_sent(self, action_hash: str) -> None:
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE mcp_action_receipts SET status='sent', updated_at=? "
                "WHERE action_hash=? AND status='reserved'",
                (_now(), action_hash),
            ).rowcount
            if changed != 1:
                raise ConflictError("MCP action is not reserved for execution")

    def complete_mcp_action(self, action_hash: str, result: Any) -> None:
        now = _now()
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE mcp_action_receipts SET status='completed',result_json=?,error_code=NULL,"
                "updated_at=?,completed_at=? WHERE action_hash=? AND status='sent'",
                (_json(result), now, now, action_hash),
            ).rowcount
            if changed != 1:
                raise ConflictError("MCP action outcome cannot be completed")

    def mark_mcp_action_unknown(self, action_hash: str, error_code: str) -> None:
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE mcp_action_receipts SET status='outcome_unknown',error_code=?,updated_at=? "
                "WHERE action_hash=? AND status='sent'",
                (error_code[:200], _now(), action_hash),
            ).rowcount
            if changed != 1:
                raise ConflictError("MCP action is not awaiting an outcome")

    def get_mcp_action_receipt(self, action_hash: str) -> dict[str, Any]:
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT * FROM mcp_action_receipts WHERE action_hash=?",
                (action_hash,),
            ).fetchone()
        if row is None:
            raise KeyError(action_hash)
        return {
            "action_hash": row["action_hash"],
            "server_id": row["server_id"],
            "tool_name": row["tool_name"],
            "schema_sha256": row["schema_sha256"],
            "arguments_sha256": row["arguments_sha256"],
            "status": row["status"],
            "error_code": row["error_code"],
            "result_available": row["result_json"] is not None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "completed_at": row["completed_at"],
        }

    def ensure_phase45_approval(
        self,
        *,
        approval_id: str,
        action_hash: str,
        target: dict[str, Any],
        policy_version: str,
        expires_at: str,
    ) -> tuple[dict[str, Any], bool]:
        now = _now()
        target_json = _json(target)
        created = False
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM phase45_approval_requests WHERE action_hash=?", (action_hash,)
            ).fetchone()
            if row is None:
                connection.execute(
                    """INSERT INTO phase45_approval_requests(
                    id,action_hash,target_json,policy_version,status,requested_at,expires_at,
                    decided_by,reason_code,decided_at,consumed_at)
                    VALUES (?,?,?,?,'pending',?,?,NULL,NULL,NULL,NULL)""",
                    (approval_id, action_hash, target_json, policy_version, now, expires_at),
                )
                created = True
                row = connection.execute(
                    "SELECT * FROM phase45_approval_requests WHERE action_hash=?", (action_hash,)
                ).fetchone()
            assert row is not None
            if row["target_json"] != target_json or row["policy_version"] != policy_version:
                raise ConflictError("approval action binding changed")
            return self._approval(row), created

    def get_phase45_approval(self, approval_id: str) -> dict[str, Any]:
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM phase45_approval_requests WHERE id=?", (approval_id,)
            ).fetchone()
            if row is None:
                raise KeyError(approval_id)
            if row["status"] == "pending" and row["expires_at"] <= _now():
                connection.execute(
                    "UPDATE phase45_approval_requests SET status='expired' "
                    "WHERE id=? AND status='pending'",
                    (approval_id,),
                )
                row = connection.execute(
                    "SELECT * FROM phase45_approval_requests WHERE id=?", (approval_id,)
                ).fetchone()
        assert row is not None
        return self._approval(row)

    def decide_phase45_approval(
        self,
        approval_id: str,
        *,
        approved: bool,
        decided_by: str,
        reason_code: str | None,
    ) -> tuple[dict[str, Any], bool]:
        if decided_by not in {"user", "reviewer"}:
            raise ValueError("approval decision identity is invalid")
        now = _now()
        expired = False
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM phase45_approval_requests WHERE id=?", (approval_id,)
            ).fetchone()
            if row is None:
                raise KeyError(approval_id)
            if row["status"] == "pending" and row["expires_at"] <= now:
                connection.execute(
                    "UPDATE phase45_approval_requests SET status='expired' WHERE id=?",
                    (approval_id,),
                )
                expired = True
            else:
                desired = "approved" if approved else "denied"
                if row["status"] == desired:
                    return self._approval(row), False
                if row["status"] != "pending":
                    raise ConflictError("approval is no longer pending")
                connection.execute(
                    "UPDATE phase45_approval_requests SET "
                    "status=?,decided_by=?,reason_code=?,decided_at=? "
                    "WHERE id=? AND status='pending'",
                    (desired, decided_by, reason_code, now, approval_id),
                )
                row = connection.execute(
                    "SELECT * FROM phase45_approval_requests WHERE id=?", (approval_id,)
                ).fetchone()
        if expired:
            raise ConflictError("approval has expired")
        assert row is not None
        return self._approval(row), True

    def consume_phase45_approval(self, action_hash: str, *, policy_version: str) -> bool:
        now = _now()
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE phase45_approval_requests SET status='consumed',consumed_at=? "
                "WHERE action_hash=? AND policy_version=? AND status='approved' AND expires_at>?",
                (now, action_hash, policy_version, now),
            ).rowcount
        return changed == 1

    @staticmethod
    def _approval(row: Any) -> dict[str, Any]:
        return {
            "approval_id": row["id"],
            "action_hash": row["action_hash"],
            "target": json.loads(row["target_json"]),
            "policy_version": row["policy_version"],
            "status": row["status"],
            "requested_at": row["requested_at"],
            "expires_at": row["expires_at"],
            "decided_by": row["decided_by"],
            "reason_code": row["reason_code"],
            "decided_at": row["decided_at"],
            "consumed_at": row["consumed_at"],
        }

    def delete_mcp_server(self, server_id: str) -> None:
        now = _now()
        event_id = hashlib.sha256(f"{server_id}\0mcp.deleted\0{now}".encode()).hexdigest()
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT lifecycle_status FROM mcp_servers WHERE server_id = ?", (server_id,)
            ).fetchone()
            if row is None:
                raise KeyError(server_id)
            if row["lifecycle_status"] != "stopped":
                raise ConflictError("MCP server must be stopped before deletion")
            connection.execute(
                "UPDATE mcp_servers SET lifecycle_status='deleted', updated_at=? WHERE server_id=?",
                (now, server_id),
            )
            connection.execute(
                "INSERT INTO mcp_lifecycle_events(id,server_id,event_type,lifecycle_status,"
                "detail_json,security_audit_event_id,created_at) "
                "VALUES (?,?,'mcp.deleted','deleted','{}',NULL,?)",
                (event_id, server_id, now),
            )

    def set_mcp_lifecycle(
        self,
        server_id: str,
        status: str,
        event_type: str,
        *,
        detail: dict[str, Any] | None = None,
        security_audit_event_id: str | None = None,
    ) -> None:
        now = _now()
        event_id = hashlib.sha256(f"{server_id}\0{event_type}\0{now}".encode()).hexdigest()
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE mcp_servers SET lifecycle_status = ?, updated_at = ? WHERE server_id = ?",
                (status, now, server_id),
            ).rowcount
            if changed != 1:
                raise KeyError(server_id)
            connection.execute(
                """
                INSERT INTO mcp_lifecycle_events(
                    id, server_id, event_type, lifecycle_status, detail_json,
                    security_audit_event_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    server_id,
                    event_type,
                    status,
                    _json(detail or {}),
                    security_audit_event_id,
                    now,
                ),
            )

    def replace_mcp_tools(self, server_id: str, tools: tuple[McpTool, ...]) -> int:
        now = _now()
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT COALESCE(MAX(snapshot_version), 0) AS version "
                "FROM mcp_tool_snapshots WHERE server_id = ?",
                (server_id,),
            ).fetchone()
            version = int(row["version"]) + 1
            for tool in tools:
                connection.execute(
                    """
                    INSERT INTO mcp_tool_snapshots(
                        server_id, snapshot_version, tool_name, schema_sha256,
                        tool_json, discovered_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        server_id,
                        version,
                        tool.name,
                        tool.schema_sha256,
                        tool.model_dump_json(),
                        now,
                    ),
                )
        return version

    def list_mcp_tools(self, server_id: str) -> tuple[dict[str, Any], ...]:
        with self.store._connect() as connection:
            version_row = connection.execute(
                "SELECT MAX(snapshot_version) AS version FROM mcp_tool_snapshots "
                "WHERE server_id = ?",
                (server_id,),
            ).fetchone()
            version = None if version_row is None else version_row["version"]
            if version is None:
                return ()
            rows = connection.execute(
                "SELECT tool_json FROM mcp_tool_snapshots "
                "WHERE server_id = ? AND snapshot_version = ? ORDER BY tool_name",
                (server_id, version),
            ).fetchall()
        return tuple(json.loads(row["tool_json"]) for row in rows)
