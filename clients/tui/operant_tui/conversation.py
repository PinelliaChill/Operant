"""Daily conversation operations over the same generated clients as the GUI."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from sdk.python_client.b2_generated import B2Client
from sdk.python_client.phase3_generated import Phase3Client
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
    def __init__(
        self,
        controller: Any,
        *,
        b2: Any = None,
        workbench: Any = None,
        phase3: Any = None,
        phase56: Any = None,
    ) -> None:
        self.core = controller
        self.b2 = b2 or B2Client(controller.core_url)
        self._workbench = workbench
        self._phase3 = phase3
        self._phase56 = phase56

    @property
    def phase56(self) -> Any:
        if self._phase56 is None:
            from sdk.python_client.phase56_generated import Phase56Client

            self._phase56 = Phase56Client(self.core.core_url)
        return self._phase56

    @property
    def phase3(self) -> Any:
        if self._phase3 is None:
            self._phase3 = Phase3Client(self.core.core_url)
        return self._phase3

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

    def extension_commands(self) -> dict[str, Any]:
        return self.phase56.list_extension_commands()

    def extension_command(
        self, thread_id: str, name: str, arguments: dict[str, Any], *, key: str
    ) -> dict[str, Any]:
        return self.phase56.execute_extension_command(
            thread_id,
            {"command": name, "arguments": arguments, "idempotency_key": key},
            idempotency_key=key,
        )

    def skill_commands(self, thread_id: str) -> dict[str, Any]:
        return self.phase56.list_skill_commands(thread_id)

    def skill_command(
        self, thread_id: str, name: str, arguments: dict[str, Any], *, key: str
    ) -> dict[str, Any]:
        return self.phase56.execute_skill_command(
            thread_id,
            {"command": name, "arguments": arguments, "idempotency_key": key},
            idempotency_key=key,
        )

    def command(
        self, thread_id: str, text: str, *, key: str, reviewer_role_id: str | None = None
    ) -> Any:
        return self.workbench.execute_workbench_command(
            thread_id, {"text": text, "reviewer_role_id": reviewer_role_id}, idempotency_key=key
        )

    def context(self, thread_id: str) -> Any:
        return self.workbench.get_workbench_context(thread_id)

    def resources(self, thread_id: str, **cursors: int) -> Any:
        return self.workbench.list_workbench_resources(thread_id, **cursors)

    def resource_policy(self, thread_id: str, completed: int, unanswered: int, *, key: str) -> Any:
        return self.workbench.update_workbench_resource_policy(
            thread_id,
            {"completed_ttl_seconds": completed, "unanswered_ttl_seconds": unanswered},
            idempotency_key=key,
        )

    def confirm_resources(self, thread_id: str, completed: bool, *, key: str) -> Any:
        return self.workbench.confirm_workbench_resources(
            thread_id, {"completed": completed}, idempotency_key=key
        )

    def pin_resource(self, thread_id: str, resource_id: str, pinned: bool, *, key: str) -> Any:
        return self.workbench.set_workbench_resource_pin(
            thread_id, resource_id, {"pinned": pinned}, idempotency_key=key
        )

    def preview_resource_cleanup(self, thread_id: str, resource_id: str, *, key: str) -> Any:
        return self.workbench.preview_workbench_resource_cleanup(
            thread_id, {"resource_ids": [resource_id], "mode": "manual"}, idempotency_key=key
        )

    def cleanup_resource(self, thread_id: str, resource_id: str, *, key: str) -> Any:
        return self.workbench.cleanup_workbench_resources(
            thread_id, {"resource_ids": [resource_id], "mode": "manual"}, idempotency_key=key
        )

    def file_content(self, workspace_id: str, path: str) -> dict[str, Any]:
        if not path.strip():
            raise ValueError("请填写工作区内相对路径")
        return self.workbench.get_workbench_file_content(
            workspace_id, path=path.strip(), max_bytes=64_000
        )

    def file_diff(self, workspace_id: str, path: str) -> dict[str, Any]:
        if not path.strip():
            raise ValueError("请填写工作区内相对路径")
        return self.workbench.get_workbench_file_diff(
            workspace_id, path=path.strip(), max_bytes=64_000
        )

    def create_terminal(self, thread_id: str, *, cols: int, rows: int, key: str) -> dict[str, Any]:
        return self.workbench.create_workbench_terminal(
            thread_id,
            {"cols": cols, "rows": rows, "idempotency_key": key},
            idempotency_key=key,
        )

    def stop_terminal(self, terminal_id: str) -> dict[str, Any]:
        return self.workbench.delete_workbench_terminal(terminal_id)

    def terminal_approval(self, approval_id: str) -> dict[str, Any]:
        return self.core.phase45.get_phase45_approval(approval_id)

    def decide_terminal_approval(
        self, approval_id: str, *, approved: bool, key: str
    ) -> dict[str, Any]:
        return self.core.phase45.decide_phase45_approval(
            approval_id, {"approved": approved}, idempotency_key=key
        )

    def effective_config(
        self, role_id: str, project_id: str | None, workspace_ref: str | None
    ) -> dict[str, Any]:
        return self.phase3.get_effective_config(
            role_id=role_id, project_id=project_id, workspace_ref=workspace_ref
        )

    def save_config(self, scope: dict[str, Any], patch_text: str, *, key: str) -> dict[str, Any]:
        patch = json.loads(patch_text)
        if not isinstance(patch, dict):
            raise ValueError("配置覆盖必须是 JSON 对象")
        return self.phase3.put_config_scope(
            scope["scope_type"],
            scope["scope_id"],
            {"patch": patch, "expected_revision": scope["revision"]},
            idempotency_key=key,
        )

    def reset_config(self, scope: dict[str, Any], *, key: str) -> dict[str, Any]:
        return self.phase3.delete_config_scope(
            scope["scope_type"],
            scope["scope_id"],
            expected_revision=scope["revision"],
            idempotency_key=key,
        )

    def goals(self, thread_id: str) -> list[dict[str, Any]]:
        return self.phase3.list_goals(owner_thread_id=thread_id)

    def create_goal(
        self,
        thread_id: str,
        objective: str,
        criteria: list[str],
        token_budget: int | None,
        *,
        key: str,
    ) -> dict[str, Any]:
        if not objective.strip():
            raise ValueError("请填写目标")
        return self.phase3.create_goal(
            {
                "owner_thread_id": thread_id,
                "objective": objective.strip(),
                "completion_criteria": criteria,
                "token_budget": token_budget,
            },
            idempotency_key=key,
        )

    def update_goal(
        self, goal: dict[str, Any], changes: dict[str, Any], *, key: str
    ) -> dict[str, Any]:
        return self.phase3.update_goal(
            goal["id"],
            {"expected_revision": goal["revision"], "changes": changes},
            idempotency_key=key,
        )

    def plans(self, goal_id: str) -> list[dict[str, Any]]:
        return self.phase3.list_plans(goal_id)

    def create_plan(self, goal_id: str, scope: str, *, key: str) -> dict[str, Any]:
        if not scope.strip():
            raise ValueError("请填写计划范围")
        return self.phase3.create_plan(
            goal_id, {"goal_id": goal_id, "scope": scope.strip()}, idempotency_key=key
        )

    def update_plan(
        self, plan: dict[str, Any], changes: dict[str, Any], *, key: str
    ) -> dict[str, Any]:
        return self.phase3.update_plan(
            plan["id"],
            {"expected_revision": plan["revision"], "changes": changes},
            idempotency_key=key,
        )

    def checklist(self, plan_id: str) -> list[dict[str, Any]]:
        return self.phase3.list_checklist_items(plan_id)

    def create_checklist(self, plan_id: str, description: str, *, key: str) -> dict[str, Any]:
        if not description.strip():
            raise ValueError("请填写清单项")
        return self.phase3.create_checklist_item(
            plan_id,
            {"plan_id": plan_id, "description": description.strip()},
            idempotency_key=key,
        )

    def set_checklist_status(
        self,
        item: dict[str, Any],
        status: str,
        evidence: list[str],
        blocker: str | None,
        *,
        key: str,
    ) -> dict[str, Any]:
        return self.phase3.command_checklist_status(
            item["id"],
            {
                "expected_revision": item["revision"],
                "status": status,
                "evidence_refs": evidence,
                "blocker": blocker,
            },
            idempotency_key=key,
        )

    def reference(self, thread_id: str, kind: str, target: str, *, key: str) -> Any:
        return self.workbench.create_workbench_reference(
            thread_id, {"kind": kind, "target": target}, idempotency_key=key
        )

    def artifacts(self, thread_id: str, after_cursor: int | None) -> dict[str, Any]:
        return self.workbench.list_workbench_reference_artifacts(
            thread_id, after_cursor=after_cursor, limit=50
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
