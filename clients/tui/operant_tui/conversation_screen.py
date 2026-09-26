"""Keyboard-accessible conversation workbench; projections come from Core."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Collapsible, Footer, Input, Label, RichLog, Select, Static, Tree

from .controller import CommandKeys, error_view
from .conversation import ConversationController, history_lines, session_id_for


class ConversationScreen(Screen[None]):
    CSS = """
    ConversationScreen { layout: vertical; }
    #conversation-body { height: 1fr; padding: 0 1; }
    #conversation-tree { height: 10; border: solid $primary; }
    #conversation-history { height: 16; min-height: 8; border: solid $primary; }
    #conversation-status { height: auto; min-height: 2; }
    .conversation-actions { height: auto; layout: horizontal; }
    .conversation-actions Button { min-width: 10; width: 1fr; }
    Input, Select { margin-bottom: 1; }
    #conversation-details { height: auto; }
    """
    BINDINGS = [
        ("escape", "close", "运行监控"),
        ("ctrl+r", "refresh", "刷新"),
        ("ctrl+l", "compose", "输入"),
    ]

    def __init__(self, controller: Any) -> None:
        super().__init__()
        self.controller = ConversationController(controller)
        self.keys = CommandKeys()
        self.threads: dict[str, dict[str, Any]] = {}
        self.selected: dict[str, Any] | None = None
        self.references: list[dict[str, Any]] = []
        self.running: set[str] = set()
        self.connected = False
        self.selection_version = 0
        self.drafts: dict[str, tuple[str, list[dict[str, Any]]]] = {}
        self.command_registry: list[dict[str, Any]] = []
        self.pending_clear: str | None = None
        self.child_pending = False
        self.refreshing = False
        self.active_children = False

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="conversation-body"):
            yield Label("会话工作台 · Enter 打开会话，Space 折叠父子历史")
            with Collapsible(title="新建会话", collapsed=True):
                yield Label("已注册工作区")
                yield Select([], prompt="选择工作区", id="conversation-workspace")
                yield Label("角色（模型、权限与预算由角色继承）")
                yield Select([], prompt="选择角色", id="conversation-role")
                yield Button("新建会话", id="conversation-create")
            yield Tree("会话", id="conversation-tree")
            yield Static("正在读取 Core…", id="conversation-status", markup=False)
            yield RichLog(id="conversation-history", wrap=True, markup=False, highlight=False)
            yield Label("消息 / 斜杠命令")
            yield Input(placeholder="输入任务，或 / 查看命令", id="conversation-message")
            yield Select([], prompt="常用命令", id="conversation-command-choice")
            with Horizontal(classes="conversation-actions"):
                yield Button("发送", id="conversation-send", variant="primary", disabled=True)
                yield Button("刷新", id="conversation-refresh")
                yield Button("取消运行", id="conversation-cancel", variant="error", disabled=True)
            with Collapsible(title="子 Agent、定向消息与上下文", collapsed=True):
                yield Label("子 Agent 任务（继承当前模型、权限和工作区）")
                yield Input(placeholder="明确职责和预期结果", id="conversation-child-task")
                yield Button("创建子 Agent", id="conversation-child", disabled=True)
                yield Label("定向消息收件人")
                yield Select([], prompt="选择同组会话", id="conversation-recipient")
                yield Input(placeholder="仅向选定收件人发送", id="conversation-mail-body")
                yield Button("发送定向消息", id="conversation-mail", disabled=True)
                yield Label("引用（先预览摘要，再附加到下一条消息）")
                yield Select(
                    [("文件", "file"), ("会话", "thread")],
                    value="file",
                    id="conversation-reference-kind",
                )
                yield Input(
                    placeholder="工作区内相对文件路径或会话 ID", id="conversation-reference"
                )
                yield Button("预览并附加引用", id="conversation-reference-add", disabled=True)
                yield Button("移除待发引用", id="conversation-reference-clear")
                yield Static("无待发引用", id="conversation-references", markup=False)
                yield Button("上下文详情", id="conversation-context", disabled=True)
                yield Label("/review 的只读审查角色（可选）")
                yield Select([], prompt="继承当前角色", id="conversation-review-role")
                yield Button("压缩上下文", id="conversation-compact", disabled=True)
                yield Button("清理上下文", id="conversation-clear", disabled=True)
                yield Static("", id="conversation-details", markup=False)
        yield Footer()

    async def on_mount(self) -> None:
        await self.refresh_catalog()
        self.set_interval(3, self.refresh_active)
        try:
            registry = await asyncio.to_thread(self.controller.commands)
            self.command_registry = registry["commands"]
            self.query_one("#conversation-command-choice", Select).set_options(
                [(c["canonical_name"], c["canonical_name"]) for c in self.command_registry]
            )
        except Exception as exc:
            self.fail(exc)

    def status(self, text: str) -> None:
        self.query_one("#conversation-status", Static).update(text)

    def fail(self, exc: Exception) -> None:
        view = error_view(exc)
        self.status(f"{view.code}: {view.message} · {view.projection_trust}")
        if view.code != "invalid_input":
            self.connected = False
        self.update_controls()

    def update_controls(self) -> None:
        readable = self.selected is not None and self.connected
        active = readable and self.selected is not None and self.selected["status"] == "active"
        busy = self.selected is not None and self.selected["id"] in self.running
        for name in ("send", "child", "mail", "reference-add", "context", "compact", "clear"):
            self.query_one(f"#conversation-{name}", Button).disabled = not active or (
                bool(busy) and name in {"send", "compact", "clear"}
            )
        self.query_one("#conversation-cancel", Button).disabled = not active
        self.query_one("#conversation-context", Button).disabled = not readable
        self.query_one("#conversation-create", Button).disabled = not self.connected
        if self.child_pending:
            self.query_one("#conversation-child", Button).disabled = True

    async def refresh_active(self) -> None:
        if self.connected and (self.running or self.active_children):
            await self.refresh_catalog()

    async def refresh_catalog(self) -> None:
        if self.refreshing:
            return
        self.refreshing = True
        try:
            await self._refresh_catalog()
        finally:
            self.refreshing = False

    async def _refresh_catalog(self) -> None:
        try:
            catalog = await asyncio.to_thread(self.controller.catalog)
        except Exception as exc:
            self.fail(exc)
            return
        self.connected = True
        self.threads = {t["id"]: t for t in catalog["threads"] if session_id_for(t)}
        for widget_id, options in (
            (
                "conversation-workspace",
                [(p["workspace_ref"], p["project_id"]) for p in catalog["projects"]],
            ),
            ("conversation-role", [(r["name"], r["id"]) for r in catalog["roles"]]),
            (
                "conversation-review-role",
                [
                    (r["name"], r["id"])
                    for r in catalog["roles"]
                    if set(r.get("tool_policy", {}).get("allowed_tools", []))
                    == {"read_file", "search_files", "git_diff"}
                    and not r.get("tool_policy", {}).get("workspace_write")
                    and not r.get("tool_policy", {}).get("command_execution")
                ],
            ),
        ):
            select = self.query_one(f"#{widget_id}", Select)
            previous = select.value
            select.set_options(options)
            if any(value == previous for _, value in options):
                select.value = previous
        tree = self.query_one("#conversation-tree", Tree)
        expanded = {n.data for n in tree.root.children if n.is_expanded}
        tree.clear()
        remaining = dict(self.threads)
        nodes: dict[str, Any] = {}
        while remaining:
            progressed = False
            for tid, thread in list(remaining.items()):
                parent = thread.get("parent_thread_id")
                if parent in remaining:
                    continue
                node = nodes.get(parent, tree.root).add(
                    Text(f"[会话 {thread['status']}] {tid}"), data=tid, expand=tid in expanded
                )
                nodes[tid] = node
                del remaining[tid]
                progressed = True
            if not progressed:
                self.fail(ValueError("父子会话投影出现循环，请检查 Core"))
                break
        tree.root.expand()
        if self.selected and self.selected["id"] in self.threads:
            self.selected = self.threads[self.selected["id"]]
            await self.load_selected()
        else:
            self.status("已连接 · 选择历史会话，或展开“新建会话”")
        self.update_controls()

    @on(Tree.NodeSelected, "#conversation-tree")
    async def select_thread(self, event: Tree.NodeSelected) -> None:
        if event.node.data not in self.threads:
            return
        self.switch_selection(self.threads[event.node.data])
        await self.load_selected()

    def switch_selection(self, thread: dict[str, Any]) -> None:
        message = self.query_one("#conversation-message", Input)
        if self.selected:
            self.drafts[self.selected["id"]] = (message.value, list(self.references))
        self.selection_version += 1
        self.selected = thread
        self.pending_clear = None
        text, references = self.drafts.get(thread["id"], ("", []))
        message.value = text
        self.references = list(references)
        self.render_references()
        self.update_controls()

    async def load_selected(self) -> None:
        thread = self.selected
        version = self.selection_version
        if thread is None:
            return
        try:
            history = await asyncio.to_thread(self.controller.history, thread)
            collaboration = await asyncio.to_thread(self.controller.collaboration, thread["id"])
            parent_children = (
                await asyncio.to_thread(self.controller.child_agents, thread["parent_thread_id"])
                if thread.get("parent_thread_id") is not None
                else []
            )
        except Exception as exc:
            if version == self.selection_version:
                self.fail(exc)
            return
        if version != self.selection_version:
            return
        task_states = {
            child["thread_id"]: child["status"]
            for child in [*collaboration["children"], *parent_children]
        }
        pending_nodes = list(self.query_one("#conversation-tree", Tree).root.children)
        while pending_nodes:
            node = pending_nodes.pop()
            pending_nodes.extend(node.children)
            if node.data in task_states and node.data in self.threads:
                node.set_label(
                    Text(
                        f"[任务 {task_states[node.data]} · "
                        f"会话 {self.threads[node.data]['status']}] {node.data}"
                    )
                )
        log = self.query_one("#conversation-history", RichLog)
        log.clear()
        for line in history_lines(history["items"]):
            log.write(Text(line))
        for child in collaboration["children"]:
            log.write(
                Text(
                    f"子 Agent [{child['status']}] {child['thread_id']} · "
                    f"{child.get('result') or child.get('task', '')}"
                )
            )
        self.active_children = any(state in {"queued", "running"} for state in task_states.values())
        for message in collaboration["messages"]:
            log.write(
                Text(
                    f"{message['sender_thread_id']} → {message['recipient_thread_id']}: "
                    f"{message['body']} [{message.get('delivery_status', '未知')}; "
                    f"{message.get('wake_status', '未知')}]"
                )
            )
            if message.get("wake_status") == "limit_reached":
                log.write(Text("自动唤醒额度已用完；消息保留，可进入收件会话手动继续。"))
        snapshot = history["session"]["role_snapshot"]
        self.status(
            f"{thread['id']} · 会话 {thread['status']}"
            + (f" · 任务 {task_states[thread['id']]}" if thread["id"] in task_states else "")
            + f" · {snapshot.get('model_id', '—')}\n"
            f"工作区：{thread.get('workspace_ref') or '未绑定'}"
        )
        self.query_one("#conversation-recipient", Select).set_options(
            [
                (t["id"], t["id"])
                for t in self.threads.values()
                if t["id"] != thread["id"]
                and (
                    t.get("parent_thread_id") == thread["id"]
                    or thread.get("parent_thread_id") == t["id"]
                    or (
                        thread.get("parent_thread_id") is not None
                        and t.get("parent_thread_id") == thread.get("parent_thread_id")
                    )
                )
            ]
        )
        self.update_controls()

    @on(Button.Pressed, "#conversation-create")
    async def create(self) -> None:
        workspace = self.query_one("#conversation-workspace", Select).value
        role = self.query_one("#conversation-role", Select).value
        if not isinstance(workspace, str) or not isinstance(role, str):
            self.fail(ValueError("请选择工作区和角色"))
            return
        action = f"create:{workspace}:{role}"
        try:
            result = await asyncio.to_thread(
                self.controller.create, workspace, role, key=self.keys.get(action)
            )
            self.keys.release(action)
            await self.refresh_catalog()
            self.switch_selection(self.threads[result["thread_id"]])
            await self.load_selected()
            self.action_compose()
        except Exception as exc:
            self.fail(exc)

    @on(Button.Pressed, "#conversation-send")
    @on(Input.Submitted, "#conversation-message")
    def send(self) -> None:
        if not self.connected or self.selected is None or self.selected["status"] != "active":
            return
        message = self.query_one("#conversation-message", Input).value.strip()
        if not message or self.selected["id"] in self.running:
            return
        if message.startswith("/"):
            self.execute_slash(message)
            return
        self.running.add(self.selected["id"])
        self.update_controls()
        self.run_message(dict(self.selected), message, list(self.references))

    @work(group="conversation-runs", exit_on_error=False)
    async def run_message(
        self, thread: dict[str, Any], message: str, references: list[dict[str, Any]]
    ) -> None:
        tid = thread["id"]
        self.running.add(tid)
        self.update_controls()
        action = f"send:{tid}:{message}:{references}"
        self.status(f"{tid} 正在运行；可打开子会话或取消")
        try:
            outcome = "未知"

            def consume() -> None:
                nonlocal outcome
                for frame in self.controller.run(
                    thread, message, references, key=self.keys.get(action)
                ):
                    if frame.get("event") == "agent.cancelled":
                        outcome = "已取消"
                    elif frame.get("event") == "agent.completed":
                        outcome = "已完成"

            await asyncio.to_thread(consume)
            self.keys.release(action)
            if self.selected and self.selected["id"] == tid:
                field = self.query_one("#conversation-message", Input)
                if field.value.strip() == message and self.references == references:
                    field.value = ""
                    self.references = []
                    self.render_references()
            elif self.drafts.get(tid) == (message, references):
                self.drafts.pop(tid, None)
            await self.refresh_catalog()
            if self.selected and self.selected["id"] == tid:
                self.status(f"{tid} {outcome}；消息与结果已保留在历史")
        except Exception as exc:
            self.fail(exc)
        finally:
            self.running.discard(tid)
            self.update_controls()

    def render_references(self) -> None:
        self.query_one("#conversation-references", Static).update(
            "\n".join(str(r.get("target_id", "")) for r in self.references) or "无待发引用"
        )

    @on(Button.Pressed, "#conversation-reference-clear")
    def clear_references(self) -> None:
        self.references = []
        self.render_references()

    @on(Button.Pressed, "#conversation-refresh")
    async def action_refresh(self) -> None:
        await self.refresh_catalog()

    def action_compose(self) -> None:
        self.query_one("#conversation-message", Input).focus()

    def action_close(self) -> None:
        self.app.pop_screen()

    @work(exit_on_error=False)
    async def execute_slash(self, message: str) -> None:
        if self.selected is None or not self.connected:
            return
        thread_id = self.selected["id"]
        if message == "/":
            self.query_one("#conversation-command-choice", Select).focus()
            self.status("选择命令后按 Enter；参数由 Core 检查")
            return
        clear_names = {"/clear-context", "/清空上下文"}
        if message in clear_names and self.pending_clear != thread_id:
            self.pending_clear = thread_id
            self.status("清理将从下一轮上下文移除旧消息，历史仍保留。再次执行以确认。")
            return
        self.pending_clear = None
        selected_role = self.query_one("#conversation-review-role", Select).value
        reviewer = selected_role if isinstance(selected_role, str) else None
        action = f"command:{thread_id}:{message}:{reviewer}"
        try:
            result = await asyncio.to_thread(
                self.controller.command,
                thread_id,
                message,
                key=self.keys.get(action),
                reviewer_role_id=reviewer,
            )
            self.keys.release(action)
            if self.selected and self.selected["id"] == thread_id:
                await self.load_selected()
                self.status(f"[{result['status']}] {result['message']}")
        except Exception as exc:
            self.fail(exc)

    @on(Select.Changed, "#conversation-command-choice")
    def choose_command(self, event: Select.Changed) -> None:
        if isinstance(event.value, str):
            self.query_one("#conversation-message", Input).value = event.value
            self.action_compose()

    @on(Input.Changed, "#conversation-message")
    def suggest_commands(self, event: Input.Changed) -> None:
        text = event.value.strip()
        if text.startswith("/") and " " not in text:
            names = [
                c["canonical_name"]
                for c in self.command_registry
                if any(a.startswith(text) for a in [c["canonical_name"], *c.get("aliases", [])])
            ]
            self.status(
                "可用命令：" + " · ".join(names) if names else "未找到命令；Core 会检查输入"
            )

    @on(Button.Pressed, "#conversation-compact")
    def compact(self) -> None:
        self.execute_slash("/compact-context")

    @on(Button.Pressed, "#conversation-clear")
    def clear_context(self) -> None:
        self.execute_slash("/clear-context")

    @on(Button.Pressed, "#conversation-context")
    async def show_context(self) -> None:
        if self.selected is None:
            return
        tid = self.selected["id"]
        try:
            result = await asyncio.to_thread(self.controller.context, tid)
            if self.selected and self.selected["id"] == tid:
                tokens = result.get("token_estimate")
                before = result.get("pre_compaction_token_estimate")
                self.query_one("#conversation-details", Static).update(
                    f"Token 估算：{tokens if tokens is not None else '未知'} / "
                    f"{result.get('context_window') or '未知'} · {result['watermark']}\n"
                    f"压缩前：{before if before is not None else '未知'}\n"
                    f"当前基线：{result.get('baseline_operation') or '无'}\n"
                    + json.dumps(result.get("blocks", []), ensure_ascii=False, indent=2)
                )
        except Exception as exc:
            self.fail(exc)

    @on(Button.Pressed, "#conversation-reference-add")
    async def add_reference(self) -> None:
        if self.selected is None:
            return
        tid = self.selected["id"]
        kind = self.query_one("#conversation-reference-kind", Select).value
        target = self.query_one("#conversation-reference", Input).value.strip()
        if not isinstance(kind, str) or not target:
            self.fail(ValueError("请选择引用类型并填写来源"))
            return
        action = f"reference:{tid}:{kind}:{target}"
        try:
            result = await asyncio.to_thread(
                self.controller.reference, tid, kind, target, key=self.keys.get(action)
            )
            self.keys.release(action)
            if self.selected and self.selected["id"] == tid:
                reference = result["reference"]
                if reference not in self.references:
                    self.references.append(reference)
                self.render_references()
                self.status(
                    f"{result['summary']} · SHA256 {result['content_hash']}"
                    + (" · 已截断" if result["truncated"] else "")
                )
        except Exception as exc:
            self.fail(exc)

    @on(Button.Pressed, "#conversation-child")
    async def create_child(self) -> None:
        if self.selected is None or self.child_pending:
            return
        tid = self.selected["id"]
        task = self.query_one("#conversation-child-task", Input).value.strip()
        if not task:
            self.fail(ValueError("请填写子 Agent 的职责与任务"))
            return
        action = f"child:{tid}:{task}"
        self.child_pending = True
        self.update_controls()
        try:
            result = await asyncio.to_thread(
                self.controller.child, tid, task, key=self.keys.get(action)
            )
            self.keys.release(action)
            if self.selected and self.selected["id"] == tid:
                field = self.query_one("#conversation-child-task", Input)
                if field.value.strip() == task:
                    field.value = ""
            await self.refresh_catalog()
            self.status(f"子 Agent {result['thread_id']} · {result['status']}")
        except Exception as exc:
            self.fail(exc)
        finally:
            self.child_pending = False
            self.update_controls()

    @on(Button.Pressed, "#conversation-cancel")
    async def cancel(self) -> None:
        if self.selected is None:
            return
        tid = self.selected["id"]
        action = f"cancel:{tid}"
        try:
            await asyncio.to_thread(self.controller.cancel, tid, key=self.keys.get(action))
            self.keys.release(action)
            await self.refresh_catalog()
            self.status(f"已向 Core 提交取消 {tid} 及子会话；以刷新后的状态为准")
        except Exception as exc:
            self.fail(exc)

    @on(Button.Pressed, "#conversation-mail")
    async def send_mail(self) -> None:
        if self.selected is None:
            return
        tid = self.selected["id"]
        recipient = self.query_one("#conversation-recipient", Select).value
        body = self.query_one("#conversation-mail-body", Input).value.strip()
        if not isinstance(recipient, str) or not body:
            self.fail(ValueError("请选择收件人并填写消息"))
            return
        action = f"mail:{tid}:{recipient}:{body}"
        try:
            await asyncio.to_thread(
                self.controller.mail, tid, recipient, body, key=self.keys.get(action)
            )
            self.keys.release(action)
            if self.selected and self.selected["id"] == tid:
                self.query_one("#conversation-mail-body", Input).value = ""
                await self.load_selected()
                self.status(f"消息已投递给 {recipient}；模型消费状态由 Core 裁决")
        except Exception as exc:
            self.fail(exc)
