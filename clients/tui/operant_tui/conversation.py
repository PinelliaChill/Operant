"""Daily conversation operations over the same generated clients as the GUI."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from sdk.python_client.b2_generated import B2Client
from sdk.python_client.transport import Phase1EError


def session_id_for(thread: dict[str, Any]) -> str | None:
    return next(
        (r["source_id"] for r in thread.get("legacy_refs", []) if r["source_type"] == "session"),
        None,
    )


def history_lines(items: list[dict[str, Any]]) -> list[str]:
    """Render committed public items as plain text, never terminal markup."""
    lines = []
    for item in items:
        payload = item.get("payload", {})
        kind = payload.get("type", payload.get("kind", "event"))
        content = payload.get("content", payload.get("text", ""))
        if content:
            lines.append(f"{kind}: {content}")
        elif payload.get("tool_name"):
            lines.append(f"{kind}: {payload['tool_name']}")
    return lines


class ConversationController:
    def __init__(self, controller: Any, *, b2: Any = None, workbench: Any = None) -> None:
        self.core = controller
        self.b2 = b2 or B2Client(controller.core_url)
        self._workbench = workbench

    @property
    def workbench(self) -> Any:
        if self._workbench is None:
            from sdk.python_client.workbench_generated import WorkbenchClient

            self._workbench = WorkbenchClient(self.core.core_url)
        return self._workbench

    def catalog(self) -> dict[str, Any]:
        return {
            "projects": self.core.phase1e.list_projects(),
            "threads": self.core.phase1e.list_threads(limit=1000),
            "roles": self.b2.list_roles(),
        }

    def collaboration(self, thread_id: str) -> dict[str, Any]:
        return {
            "children": self.child_agents(thread_id),
            "messages": self.workbench.list_workbench_agent_messages(thread_id),
        }

    def child_agents(self, parent_thread_id: str) -> list[dict[str, Any]]:
        return self.workbench.list_workbench_child_agents(parent_thread_id)

    def commands(self) -> dict[str, Any]:
        return self.workbench.list_workbench_commands()

    def command(
        self, thread_id: str, text: str, *, key: str, reviewer_role_id: str | None = None
    ) -> Any:
        return self.workbench.execute_workbench_command(
            thread_id, {"text": text, "reviewer_role_id": reviewer_role_id}, idempotency_key=key
        )

    def context(self, thread_id: str) -> Any:
        return self.workbench.get_workbench_context(thread_id)

    def reference(self, thread_id: str, kind: str, target: str, *, key: str) -> Any:
        return self.workbench.create_workbench_reference(
            thread_id, {"kind": kind, "target": target}, idempotency_key=key
        )

    def child(self, thread_id: str, task: str, *, key: str) -> Any:
        return self.workbench.create_workbench_child_agent(
            thread_id, {"task": task}, idempotency_key=key
        )

    def cancel(self, thread_id: str, *, key: str) -> Any:
        return self.workbench.cancel_workbench_thread(thread_id, idempotency_key=key)

    def mail(self, thread_id: str, recipient: str, body: str, *, key: str) -> Any:
        return self.workbench.send_workbench_agent_message(
            thread_id,
            {"recipient_thread_id": recipient, "body": body, "idempotency_key": key},
            idempotency_key=key,
        )

    def history(self, thread: dict[str, Any]) -> dict[str, Any]:
        session_id = session_id_for(thread)
        if not session_id:
            raise ValueError("这个历史条目没有普通会话，请选择其他会话")
        result = self.b2.get_session_history(session_id, limit=999)
        items = list(result["items"])
        cursor = result["next_cursor"]
        while cursor is not None:
            page = self.b2.get_session_history(session_id, after_cursor=cursor, limit=999)
            items.extend(page["items"])
            following = page["next_cursor"]
            if following is not None and following <= cursor:
                raise ValueError("历史分页游标未前进，请重新读取")
            cursor = following
        return {**result, "items": items, "next_cursor": None}

    def create(self, workspace: str, role_id: str, *, key: str) -> dict[str, Any]:
        if not workspace or not role_id:
            raise ValueError("请选择工作区和角色")
        thread = self.b2.create_thread({"workspace_id": workspace}, idempotency_key=key + ":thread")
        session = self.core.phase1e.create_session(
            {"role_id": role_id, "thread_id": thread["id"]}, idempotency_key=key + ":session"
        )
        return {"thread_id": thread["id"], "session_id": session["id"]}

    def run(
        self,
        thread: dict[str, Any],
        message: str,
        references: list[dict[str, Any]],
        *,
        key: str,
    ) -> Iterator[Any]:
        session_id = session_id_for(thread)
        if not session_id or not message.strip() or not thread.get("workspace_ref"):
            raise ValueError("需要普通会话、工作区和非空消息")
        stream = self.core.phase1e.run_session_stream(
            session_id,
            {
                "message": message,
                "workspace": thread["workspace_ref"],
                "thread_id": thread["id"],
                "references": references,
            },
            idempotency_key=key,
        )
        if stream.receipt is not None:
            raise Phase1EError(
                "command_replayed",
                "该发送已提交；请刷新历史核对结果，不会再次发送",
                recovery="manual_reconcile",
            )
        terminal = False
        for frame in stream.events:
            event = frame.get("event")
            if event in {
                "agent.failed",
                "agent.timed_out",
                "agent.stream_error",
                "budget.exhausted",
                "session.run_failed",
            }:
                raise Phase1EError(
                    "session_run_failed",
                    f"Core 返回 {event}；请查看历史与状态",
                    recovery="manual_reconcile",
                )
            if event in {"agent.completed", "agent.cancelled"}:
                terminal = True
            yield frame
        if not terminal:
            raise Phase1EError(
                "incomplete_session_stream",
                "事件流未确认终态；请刷新历史核对",
                recovery="manual_reconcile",
            )
