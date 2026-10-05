"""Protocol-only controller shared by the Textual widgets and unit tests."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from sdk.python_client import Phase1EClient, Phase23Client, Phase45Client, Phase56Client
from sdk.python_client.b2_6_generated import B26Client
from sdk.python_client.beta_generated import BetaClient
from sdk.python_client.phase23_generated import ScopedCursorTracker
from sdk.python_client.phase56_generated import PHASE56_PROTOCOL_VERSION
from sdk.python_client.transport import Phase1EError

LayoutMode = Literal["wide", "medium", "narrow"]


class CommandKeys:
    """Keep one idempotency key for each unresolved logical write."""

    def __init__(self) -> None:
        self._keys: dict[str, str] = {}

    def get(self, logical_action: str) -> str:
        import secrets

        return self._keys.setdefault(logical_action, f"tui-{secrets.token_hex(12)}")

    def release(self, logical_action: str) -> None:
        self._keys.pop(logical_action, None)


@dataclass(frozen=True)
class ClientErrorView:
    code: str
    message: str
    recovery: str
    safe_to_retry: bool
    projection_trust: str


def layout_for_width(width: int) -> LayoutMode:
    if width >= 140:
        return "wide"
    if width >= 100:
        return "medium"
    return "narrow"


def validate_core_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Core URL must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Core URL must not contain credentials, query, or fragment")
    return value.rstrip("/")


def error_view(error: Exception) -> ClientErrorView:
    if isinstance(error, Phase1EError):
        cursor_expired = error.code in {"cursor_expired", "cursor_out_of_range"}
        schema_error = "protocol" in error.code or "schema" in error.code
        guidance = error.message
        if cursor_expired:
            guidance = f"{guidance}；Cursor 已过期，请重新读取完整投影，不会自动重复 Command。"
        elif schema_error:
            guidance = f"{guidance}；协议不兼容，请更新客户端或 Core。"
        return ClientErrorView(
            code=error.code,
            message=guidance,
            recovery=error.recovery,
            safe_to_retry=error.retryable and error.recovery != "manual_reconcile",
            projection_trust="保留最近投影；写操作结果未知时必须人工核对。",
        )
    if isinstance(error, ValueError):
        return ClientErrorView(
            code="invalid_input",
            message=str(error),
            recovery="edit_input",
            safe_to_retry=False,
            projection_trust="输入未发送到 Core；现有投影未改变。",
        )
    return ClientErrorView(
        code="client_error",
        message=str(error) or "Core transport is unavailable",
        recovery="retry_later",
        safe_to_retry=True,
        projection_trust="保留最近投影；未确认任何写操作已生效。",
    )


class ClientController:
    """Thin orchestration over generated clients; Core remains authoritative."""

    def __init__(
        self,
        core_url: str,
        *,
        phase1e: Any | None = None,
        phase23: Any | None = None,
        phase45: Any | None = None,
        phase56: Any | None = None,
        b26: Any | None = None,
    ) -> None:
        base_url = validate_core_url(core_url)
        self.core_url = base_url
        self.phase1e = phase1e or Phase1EClient(base_url)
        self.phase23 = phase23 or Phase23Client(base_url)
        self.phase45 = phase45 or Phase45Client(base_url)
        self.phase56 = phase56 or Phase56Client(base_url)
        self.b26 = b26 or B26Client(base_url)
        self._graph_cursors: dict[str, ScopedCursorTracker] = {}

    def negotiate(self) -> None:
        for client in (self.phase1e, self.phase23, self.phase45, self.phase56):
            client.negotiate_protocol(force=True)

    def experience(self, project_id: str) -> Any:
        project_id = project_id.strip()
        if not project_id:
            raise ValueError("请填写项目 ID")
        self.b26.negotiate_protocol()
        return self.b26.get_experience(project_id)

    def experience_command(self, command: dict[str, Any], *, idempotency_key: str) -> Any:
        if not command.get("project_id") or not command.get("action"):
            raise ValueError("经验命令需要项目和操作")
        # The generated client owns schema negotiation and error semantics.
        return self.b26.execute(command, idempotency_key=idempotency_key)

    def graph_projection(self, run_id: str) -> tuple[Any, list[Any]]:
        run_id = run_id.strip()
        if not run_id:
            raise ValueError("Graph Run ID is required")
        return self.phase23.get_graph_run(run_id), self.phase23.list_node_runs(run_id)

    def resume_graph(self, run_id: str, *, idempotency_key: str) -> Any:
        run_id = run_id.strip()
        if not run_id:
            raise ValueError("Graph Run ID is required")
        return self.phase23.resume_graph_run(
            run_id,
            {"allow_unknown_side_effect_replay": False},
            idempotency_key=idempotency_key,
        )

    def provide_graph_input(
        self, run_id: str, node_id: str, wait_token: str, value: str, *, idempotency_key: str
    ) -> Any:
        if not run_id.strip() or not node_id.strip() or not wait_token.strip() or not value.strip():
            raise ValueError("Graph Run、等待节点、wait token 和输入都不能为空")
        return self.phase23.provide_node_input(
            run_id.strip(),
            node_id.strip(),
            {"wait_token": wait_token, "value": value},
            idempotency_key=idempotency_key,
        )

    def graph_node_approval(self, run_id: str, node_id: str) -> Any:
        if not run_id.strip() or not node_id.strip():
            raise ValueError("Graph Run 和审批节点 ID 不能为空")
        return self.phase23.get_graph_node_approval(run_id.strip(), node_id.strip())

    def decide_graph_node_approval(
        self,
        run_id: str,
        node_id: str,
        approval_id: str,
        wait_token: str,
        approved: bool,
        *,
        idempotency_key: str,
    ) -> Any:
        if (
            not run_id.strip()
            or not node_id.strip()
            or not approval_id.strip()
            or not wait_token.strip()
        ):
            raise ValueError("审批请求身份和等待令牌不能为空")
        return self.phase23.decide_graph_node_approval(
            run_id.strip(),
            node_id.strip(),
            {"approval_id": approval_id, "wait_token": wait_token, "approved": approved},
            idempotency_key=idempotency_key,
        )

    def cancel_graph(self, run_id: str, *, idempotency_key: str) -> Any:
        run_id = run_id.strip()
        if not run_id:
            raise ValueError("Graph Run ID is required")
        return self.phase23.cancel_graph_run(run_id, idempotency_key=idempotency_key)

    def approvals(self, session_id: str) -> list[Any]:
        session_id = session_id.strip()
        if not session_id:
            raise ValueError("Session ID is required")
        return self.phase1e.list_pending_approvals(session_id)

    def graph_events(self, run_id: str, *, after_cursor: int | None) -> Iterator[Any]:
        run_id = run_id.strip()
        if not run_id:
            raise ValueError("Graph Run ID is required")
        stream = self.phase23.stream_graph_run_events(run_id, last_event_id=after_cursor)
        return iter(stream.events)

    def accept_graph_event(self, run_id: str, frame: Any) -> int | None:
        tracker = self._graph_cursors.setdefault(run_id, ScopedCursorTracker())
        scope = f"graph_run:{run_id}"
        previous = tracker.last(scope, "graph.run")
        if not tracker.accept(frame):
            return None
        current = tracker.last(scope, "graph.run")
        if previous is not None and current == previous:
            return None
        return current

    def graph_cursor(self, run_id: str) -> int | None:
        tracker = self._graph_cursors.get(run_id)
        return None if tracker is None else tracker.last(f"graph_run:{run_id}", "graph.run")

    def reset_graph_cursor(self, run_id: str) -> None:
        self._graph_cursors.pop(run_id, None)

    def remote_projection(self) -> dict[str, Any]:
        """Read only the Core's Remote projections; never infer an Ack locally."""
        hosts = self.phase56.list_remote_hosts()["items"]
        return {
            "hosts": hosts,
            "devices": [
                item
                for host in hosts
                for item in self.phase56.list_remote_devices(host_id=host["host_id"])["items"]
            ],
            "sessions": [
                item
                for host in hosts
                for item in self.phase56.list_remote_sessions(host_id=host["host_id"])["items"]
            ],
            "events": [
                item
                for host in hosts
                for item in self.phase56.list_remote_control_events(
                    host_id=host["host_id"], limit=20
                )["items"]
            ],
            "connections": BetaClient(self.core_url).list_remote_gateway_connections()["items"],
            "targets": self.phase56.list_remote_targets()["items"],
            "jobs": self.phase56.list_remote_target_jobs()["items"],
        }

    def create_remote_pairing(self, host_id: str, scopes: list[str]) -> Any:
        if not host_id or "remote.control.observe" not in scopes:
            raise ValueError("必须选择 Host 和查看状态权限")
        return self.phase56.create_pairing_challenge(
            {"host_id": host_id, "allowed_scopes": scopes, "ttl_seconds": 300}
        )

    def create_remote_session(self, host_id: str, device_id: str, mode: str) -> Any:
        if mode not in {"direct", "relay"}:
            raise ValueError("传输方式只能是 direct 或 relay")
        return self.phase56.create_remote_session(
            {
                "host_id": host_id,
                "device_id": device_id,
                "transport_mode": mode,
                "protocol_version": PHASE56_PROTOCOL_VERSION,
            }
        )

    def close_remote_session(self, session_id: str) -> Any:
        if not session_id.strip():
            raise ValueError("请选择会话")
        return self.phase56.close_remote_session(session_id.strip())

    def revoke_remote_device(self, device_id: str) -> Any:
        if not device_id.strip():
            raise ValueError("请选择设备")
        return self.phase56.revoke_remote_device(device_id.strip())

    def remote_command(self, command_id: str) -> Any:
        if not command_id.strip():
            raise ValueError("请输入 Command ID")
        return self.phase56.get_remote_command(command_id.strip())

    def remote_job_result(self, job_id: str) -> Any:
        if not job_id.strip():
            raise ValueError("请输入 Job ID")
        return self.phase56.get_remote_target_job_result(job_id.strip())

    def register_remote_target(self, request: dict[str, Any]) -> Any:
        credential_ref = request.get("credential_ref", "")
        if not isinstance(credential_ref, str) or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", credential_ref
        ):
            raise ValueError("凭据引用只能填写环境变量名，不能填写 Token 值")
        return self.phase56.register_remote_target(request)

    def acquire_remote_target_lease(self, target_id: str, workspace_ref: str) -> Any:
        import secrets

        if not target_id.strip() or not workspace_ref.strip():
            raise ValueError("Target ID 和 Workspace Ref 不能为空")
        key = f"tui-target-{secrets.token_hex(12)}"
        return self.phase56.acquire_remote_target_lease(
            target_id.strip(),
            {
                "owner": "tui-local-user",
                "workspace_ref": workspace_ref.strip(),
                "ttl_seconds": 300,
                "idempotency_key": key,
            },
            idempotency_key=key,
        )

    def release_remote_target_lease(self, target_id: str, lease: dict[str, Any]) -> Any:
        import secrets

        key = f"tui-target-{secrets.token_hex(12)}"
        return self.phase56.release_remote_target_lease(
            target_id,
            {
                "lease_id": lease["lease_id"],
                "token": lease["token"],
                "fencing": lease["fencing"],
                "idempotency_key": key,
            },
            idempotency_key=key,
        )

    def create_remote_target_job(
        self,
        target_id: str,
        lease: dict[str, Any],
        capability: str,
        operation: str,
        arguments: dict[str, Any],
        idempotency: str,
    ) -> Any:
        import secrets

        if capability not in {"remote.target.read", "remote.target.exec"}:
            raise ValueError("当前 TUI 仅派发 Target read/exec")
        if idempotency not in {"idempotent", "non_idempotent"} or not operation.strip():
            raise ValueError("请检查幂等性和 Operation")
        key = f"tui-target-{secrets.token_hex(12)}"
        return self.phase56.create_remote_target_job(
            target_id,
            {
                "lease_id": lease["lease_id"],
                "token": lease["token"],
                "fencing": lease["fencing"],
                "capability": capability,
                "operation": operation.strip(),
                "arguments": arguments,
                "idempotency_key": key,
                "idempotency": idempotency,
            },
            idempotency_key=key,
        )

    def cancel_remote_target_job(self, job_id: str) -> Any:
        import secrets

        if not job_id.strip():
            raise ValueError("请输入 Job ID")
        key = f"tui-target-{secrets.token_hex(12)}"
        return self.phase56.cancel_remote_target_job(
            job_id.strip(), {"idempotency_key": key}, idempotency_key=key
        )
