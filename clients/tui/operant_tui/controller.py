"""Protocol-only controller shared by the Textual widgets and unit tests."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from sdk.python_client import Phase1EClient, Phase23Client, Phase45Client, Phase56Client
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
    ) -> None:
        base_url = validate_core_url(core_url)
        self.phase1e = phase1e or Phase1EClient(base_url)
        self.phase23 = phase23 or Phase23Client(base_url)
        self.phase45 = phase45 or Phase45Client(base_url)
        self.phase56 = phase56 or Phase56Client(base_url)

    def negotiate(self) -> None:
        for client in (self.phase1e, self.phase23, self.phase45, self.phase56):
            client.negotiate_protocol(force=True)

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
