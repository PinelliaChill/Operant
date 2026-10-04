"""Keyboard-accessible conversation workbench; projections come from Core."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from rich.syntax import Syntax
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import (
    Button,
    Collapsible,
    Footer,
    Input,
    Label,
    RichLog,
    Select,
    Static,
    TextArea,
    Tree,
)

from sdk.python_client.transport import Phase1EError

from .controller import CommandKeys, error_view
from .conversation import ConversationController, history_lines, session_id_for


class ConversationScreen(Screen[None]):
    GOAL_EDIT_FIELDS = (
        "token_budget",
        "cost_budget",
        "time_budget_seconds",
        "completion_criteria",
        "linked_thread_ids",
        "linked_workflow_run_ids",
    )
    PLAN_EDIT_FIELDS = (
        "assumptions",
        "constraints",
        "open_questions",
        "proposed_changes",
        "risk_items",
        "approval_requirements",
        "verification_plan",
    )
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
    #conversation-config-patch, #conversation-goal-fields, #conversation-plan-fields { height: 8; }
    #conversation-file-content { height: 18; }
    #conversation-task-details { max-height: 12; }
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
        self.projects: list[dict[str, Any]] = []
        self.role_id: str | None = None
        self.config_scopes: dict[str, dict[str, Any]] = {}
        self.goals: dict[str, dict[str, Any]] = {}
        self.plans: dict[str, dict[str, Any]] = {}
        self.checklist_items: dict[str, dict[str, Any]] = {}
        self.pending_terminal_approval: str | None = None
        self.terminal_approval_view: dict[str, Any] | None = None
        self.pending_terminal_dimensions: tuple[int, int] | None = None
        self.artifacts: dict[str, dict[str, Any]] = {}
        self.artifact_cursor: int | None = None
        self.artifact_has_more = False
        self.artifact_loading = False

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
                    [("文件", "file"), ("会话", "thread"), ("工件", "artifact")],
                    value="file",
                    id="conversation-reference-kind",
                )
                yield Input(
                    placeholder="文件路径或会话 ID；工件从授权列表选择", id="conversation-reference"
                )
                with Horizontal(classes="conversation-actions"):
                    yield Button("读取可引用工件", id="conversation-artifact-list", disabled=True)
                    yield Button("更多工件", id="conversation-artifact-more", disabled=True)
                yield Select([], prompt="选择工件", id="conversation-artifact-choice")
                yield Static("", id="conversation-artifact-detail", markup=False)
                yield Button("预览并附加引用", id="conversation-reference-add", disabled=True)
                yield Button("移除待发引用", id="conversation-reference-clear")
                yield Static("无待发引用", id="conversation-references", markup=False)
                yield Button("上下文详情", id="conversation-context", disabled=True)
                yield Button("临时资源与保留策略", id="conversation-resources", disabled=True)
                yield Label("/review 的只读审查角色（可选）")
                yield Select([], prompt="继承当前角色", id="conversation-review-role")
                yield Button("压缩上下文", id="conversation-compact", disabled=True)
                yield Button("清理上下文", id="conversation-clear", disabled=True)
                yield Static("", id="conversation-details", markup=False)
            with Collapsible(title="配置继承", collapsed=True):
                yield Button("读取有效配置与来源", id="conversation-config-load", disabled=True)
                yield Static("选择会话后读取配置", id="conversation-config-effective", markup=False)
                yield Select([], prompt="选择覆盖层", id="conversation-config-scope")
                yield Label("当前覆盖 JSON；留空字段表示继承。保存仅作用于新会话。")
                yield TextArea("{}", id="conversation-config-patch")
                with Horizontal(classes="conversation-actions"):
                    yield Button("保存覆盖", id="conversation-config-save", disabled=True)
                    yield Button("恢复继承", id="conversation-config-reset", disabled=True)
            with Collapsible(title="Goal / Plan", collapsed=True):
                yield Button("读取 Goal / Plan", id="conversation-goal-load", disabled=True)
                yield Input(placeholder="目标（必填）", id="conversation-goal-objective")
                yield Input(placeholder="完成条件；用分号分隔", id="conversation-goal-criteria")
                yield Input(placeholder="Token 预算（可选）", id="conversation-goal-budget")
                yield Button("创建 Goal", id="conversation-goal-create", disabled=True)
                yield Select([], prompt="选择 Goal", id="conversation-goal-choice")
                yield Input(placeholder="更新目标描述", id="conversation-goal-edit")
                yield Label("Goal 预算、完成条件与关联（JSON）")
                yield TextArea("{}", id="conversation-goal-fields")
                yield Select(
                    [(s, s) for s in ("active", "blocked", "completed", "cancelled")],
                    prompt="Goal 状态",
                    id="conversation-goal-status",
                )
                yield Input(placeholder="阻塞原因或完成结果引用", id="conversation-goal-result")
                yield Button("保存 Goal", id="conversation-goal-save", disabled=True)
                yield Input(placeholder="计划范围（必填）", id="conversation-plan-scope")
                yield Button("创建 Plan", id="conversation-plan-create", disabled=True)
                yield Select([], prompt="选择 Plan", id="conversation-plan-choice")
                yield Input(placeholder="更新计划范围", id="conversation-plan-edit")
                yield Label("Plan 假设、约束、变化、风险与验证（JSON）")
                yield TextArea("{}", id="conversation-plan-fields")
                yield Select(
                    [
                        (s, s)
                        for s in (
                            "draft",
                            "in_review",
                            "approved",
                            "in_progress",
                            "completed",
                            "superseded",
                        )
                    ],
                    prompt="Plan 状态",
                    id="conversation-plan-status",
                )
                yield Button("保存 Plan", id="conversation-plan-save", disabled=True)
                yield Input(placeholder="新增清单项", id="conversation-check-description")
                yield Button("添加清单项", id="conversation-check-create", disabled=True)
                yield Select([], prompt="选择清单项", id="conversation-check-choice")
                yield Select(
                    [(s, s) for s in ("todo", "in_progress", "blocked", "done", "skipped")],
                    prompt="清单状态",
                    id="conversation-check-status",
                )
                yield Input(placeholder="完成证据引用或阻塞原因", id="conversation-check-evidence")
                yield Button("保存清单状态", id="conversation-check-save", disabled=True)
                yield Static("", id="conversation-task-details", markup=False)
            with Collapsible(title="文件与代码预览", collapsed=True):
                yield Input(placeholder="工作区内相对文件路径", id="conversation-file-path")
                with Horizontal(classes="conversation-actions"):
                    yield Button("预览正文", id="conversation-file-read", disabled=True)
                    yield Button("查看 Diff", id="conversation-file-diff", disabled=True)
                yield Static("", id="conversation-file-meta", markup=False)
                yield RichLog(
                    id="conversation-file-content", wrap=False, markup=False, highlight=False
                )
            with Collapsible(title="交互终端", collapsed=True):
                yield Label(
                    "Host shell 继承本机用户权限；Core 仅审批创建，后续输入不逐条审批。"
                    "仅在可信工作区使用。Ctrl+Q 关闭。"
                )
                yield Button("打开终端", id="conversation-terminal-open", disabled=True)
                yield Static("", id="conversation-terminal-approval-detail", markup=False)
                with Horizontal(classes="conversation-actions"):
                    yield Button(
                        "刷新审批", id="conversation-terminal-approval-load", disabled=True
                    )
                    yield Button(
                        "批准 Host shell", id="conversation-terminal-approval-allow", disabled=True
                    )
                    yield Button("拒绝", id="conversation-terminal-approval-deny", disabled=True)
        yield Footer()

    async def on_mount(self) -> None:
        await self.refresh_catalog()
        self.set_interval(3, self.refresh_active)
        try:
            registry, extension_registry = await asyncio.gather(
                asyncio.to_thread(self.controller.commands),
                asyncio.to_thread(self.controller.extension_commands),
            )
            self.command_registry = [
                *registry["commands"],
                *(
                    {
                        "canonical_name": f"/{command['name']}",
                        "aliases": [],
                        "extension_name": command["name"],
                        "parameters": command["parameters"],
                    }
                    for command in extension_registry["commands"]
                ),
            ]
            self.query_one("#conversation-command-choice", Select).set_options(
                [(c["canonical_name"], c["canonical_name"]) for c in self.command_registry]
            )
        except Exception as exc:
            self.fail(exc)

    def status(self, text: str) -> None:
        self.query_one("#conversation-status", Static).update(text)

    @staticmethod
    def edit_fields(text: str, allowed: tuple[str, ...]) -> dict[str, Any]:
        value = json.loads(text)
        if not isinstance(value, dict) or set(value) - set(allowed):
            raise ValueError("编辑内容必须是仅含所列字段的 JSON 对象")
        return value

    def fail(self, exc: Exception) -> None:
        view = error_view(exc)
        self.status(f"{view.code}: {view.message} · {view.projection_trust}")
        if (
            view.code in {"transport_unavailable", "invalid_error_envelope", "client_error"}
            or "protocol" in view.code
        ):
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
        self.query_one("#conversation-resources", Button).disabled = not readable
        self.query_one("#conversation-create", Button).disabled = not self.connected
        for name in (
            "config-load",
            "config-save",
            "config-reset",
            "goal-load",
            "goal-create",
            "goal-save",
            "plan-create",
            "plan-save",
            "check-create",
            "check-save",
            "file-read",
            "file-diff",
            "terminal-open",
            "artifact-list",
        ):
            self.query_one(f"#conversation-{name}", Button).disabled = not readable
        self.query_one("#conversation-terminal-open", Button).disabled = not active
        self.query_one("#conversation-artifact-more", Button).disabled = (
            not readable or not self.artifact_has_more or self.artifact_loading
        )
        self.query_one("#conversation-terminal-approval-load", Button).disabled = (
            not readable or self.pending_terminal_approval is None
        )
        can_decide = (
            readable
            and self.terminal_approval_view is not None
            and self.terminal_approval_view.get("status") == "pending"
        )
        for name in ("allow", "deny"):
            self.query_one(
                f"#conversation-terminal-approval-{name}", Button
            ).disabled = not can_decide
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
        self.projects = catalog["projects"]
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
                parent_node = nodes.get(parent) if isinstance(parent, str) else None
                node = (parent_node or tree.root).add(
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
        self.config_scopes = {}
        self.goals = {}
        self.plans = {}
        self.checklist_items = {}
        self.pending_terminal_approval = None
        self.terminal_approval_view = None
        self.pending_terminal_dimensions = None
        self.artifacts = {}
        self.artifact_cursor = None
        self.artifact_has_more = False
        self.query_one("#conversation-artifact-choice", Select).set_options([])
        self.query_one("#conversation-artifact-detail", Static).update("")
        self.query_one("#conversation-terminal-approval-detail", Static).update("")
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
            skills = await asyncio.to_thread(self.controller.skill_commands, thread["id"])
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
        self.command_registry = [c for c in self.command_registry if not c.get("skill_name")]
        self.command_registry.extend(
            {
                "canonical_name": f"/{command['command']}",
                "aliases": [],
                "skill_name": command["command"],
                "parameters": command["parameters"],
            }
            for command in skills["commands"]
        )
        self.query_one("#conversation-command-choice", Select).set_options(
            [(c["canonical_name"], c["canonical_name"]) for c in self.command_registry]
        )
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
        self.role_id = snapshot.get("role_id")
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
        token, _, raw_arguments = message.partition(" ")
        skill = next(
            (
                command
                for command in self.command_registry
                if command.get("skill_name") and command["canonical_name"] == token
            ),
            None,
        )
        if skill is not None:
            try:
                arguments = json.loads(raw_arguments) if raw_arguments.strip() else {}
                if (
                    not isinstance(arguments, dict)
                    or set(arguments) != {"prompt"}
                    or not isinstance(arguments["prompt"], str)
                    or not 1 <= len(arguments["prompt"]) <= 4000
                ):
                    raise ValueError("Skill 参数必须是仅含 prompt 的 JSON 对象（1–4000 字符）")
                action = f"skill-command:{thread_id}:{message}"
                result = await asyncio.to_thread(
                    self.controller.skill_command,
                    thread_id,
                    skill["skill_name"],
                    arguments,
                    key=self.keys.get(action),
                )
                self.keys.release(action)
                if self.selected and self.selected["id"] == thread_id:
                    await self.load_selected()
                    self.status(f"[{result['status']}] {result.get('result', '')[:500]}")
            except Exception as exc:
                self.fail(exc)
            return
        extension = next(
            (
                command
                for command in self.command_registry
                if command.get("extension_name") and command["canonical_name"] == token
            ),
            None,
        )
        if extension is not None:
            try:
                arguments = json.loads(raw_arguments) if raw_arguments.strip() else {}
                if not isinstance(arguments, dict):
                    raise ValueError("扩展命令参数必须是 JSON 对象")
                action = f"extension-command:{thread_id}:{message}"
                result = await asyncio.to_thread(
                    self.controller.extension_command,
                    thread_id,
                    extension["extension_name"],
                    arguments,
                    key=self.keys.get(action),
                )
                self.keys.release(action)
                self.status(
                    f"[{result['status']}] "
                    + json.dumps(result.get("result", {}), ensure_ascii=False)[:500]
                )
            except Exception as exc:
                self.fail(exc)
            return
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

    @on(Button.Pressed, "#conversation-resources")
    async def show_resources(self) -> None:
        if self.selected is None or not self.connected:
            return
        from .resource_screen import ResourceScreen

        await self.app.push_screen(ResourceScreen(self.controller, self.selected["id"]))

    async def _load_artifact_page(self, *, reset: bool) -> None:
        if not self.selected or self.artifact_loading:
            return
        tid = self.selected["id"]
        cursor = None if reset else self.artifact_cursor
        self.artifact_loading = True
        self.update_controls()
        try:
            page = await asyncio.to_thread(self.controller.artifacts, tid, cursor)
            if not self.selected or self.selected["id"] != tid:
                return
            if reset:
                self.artifacts = {}
            for artifact in page["items"]:
                self.artifacts[artifact["id"]] = artifact
            following = page.get("next_cursor")
            if following is not None and (
                not isinstance(following, int) or (cursor is not None and following <= cursor)
            ):
                raise ValueError("工件分页游标未前进")
            self.artifact_cursor = following
            self.artifact_has_more = following is not None
            choices = self.query_one("#conversation-artifact-choice", Select)
            previous = choices.value
            choices.set_options(
                [
                    (f"{a['source']} · {a['summary'][:50]} · {a['id']}", a["id"])
                    for a in self.artifacts.values()
                ]
            )
            if previous in self.artifacts:
                choices.value = previous
            self.status(f"已读取 {len(self.artifacts)} 个当前会话可引用工件")
        except Exception as exc:
            self.fail(exc)
        finally:
            self.artifact_loading = False
            self.update_controls()

    @on(Button.Pressed, "#conversation-artifact-list")
    async def list_artifacts(self) -> None:
        await self._load_artifact_page(reset=True)

    @on(Button.Pressed, "#conversation-artifact-more")
    async def more_artifacts(self) -> None:
        if self.artifact_has_more:
            await self._load_artifact_page(reset=False)

    @on(Select.Changed, "#conversation-artifact-choice")
    def choose_artifact(self, event: Select.Changed) -> None:
        if isinstance(event.value, str) and event.value in self.artifacts:
            artifact = self.artifacts[event.value]
            self.query_one("#conversation-artifact-detail", Static).update(
                f"来源：{artifact['source']} · {artifact['media_type']} · "
                f"{artifact['size_bytes']} 字节\n"
                f"{artifact['summary']}\nSHA256 {artifact['content_hash']}"
            )

    @on(Button.Pressed, "#conversation-reference-add")
    async def add_reference(self) -> None:
        if self.selected is None:
            return
        tid = self.selected["id"]
        kind = self.query_one("#conversation-reference-kind", Select).value
        if kind == "artifact":
            chosen = self.query_one("#conversation-artifact-choice", Select).value
            target = chosen if isinstance(chosen, str) and chosen in self.artifacts else ""
        else:
            target = self.query_one("#conversation-reference", Input).value.strip()
        if not isinstance(kind, str) or not target:
            self.fail(ValueError("请选择引用类型和已授权来源"))
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

    @on(Button.Pressed, "#conversation-config-load")
    async def load_config(self) -> None:
        if not self.selected or not self.role_id:
            self.fail(ValueError("会话缺少角色，无法读取有效配置"))
            return
        tid = self.selected["id"]
        workspace = self.selected.get("workspace_ref")
        project = next(
            (p["project_id"] for p in self.projects if p.get("workspace_ref") == workspace), None
        )
        try:
            result = await asyncio.to_thread(
                self.controller.effective_config, self.role_id, project, workspace
            )
            if not self.selected or self.selected["id"] != tid:
                return
            self.config_scopes = result["scopes"]
            self.query_one("#conversation-config-effective", Static).update(
                "仅影响新会话；当前会话使用创建时冻结的配置。\n"
                + json.dumps(
                    {
                        "values": result["values"],
                        "sources": result["sources"],
                        "revisions": result["revisions"],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            self.query_one("#conversation-config-scope", Select).set_options(
                [
                    (name, name)
                    for name in ("global", "project", "workspace", "role")
                    if name in self.config_scopes
                ]
            )
        except Exception as exc:
            self.fail(exc)

    @on(Select.Changed, "#conversation-config-scope")
    def choose_config_scope(self, event: Select.Changed) -> None:
        if isinstance(event.value, str) and event.value in self.config_scopes:
            patch = self.config_scopes[event.value].get("patch", {})
            self.query_one("#conversation-config-patch", TextArea).text = json.dumps(
                patch, ensure_ascii=False, indent=2
            )

    async def _write_config(self, *, reset: bool) -> None:
        selected = self.query_one("#conversation-config-scope", Select).value
        if not isinstance(selected, str) or selected not in self.config_scopes:
            self.fail(ValueError("请先选择配置覆盖层"))
            return
        scope = self.config_scopes[selected]
        patch = self.query_one("#conversation-config-patch", TextArea).text
        action = f"config:{'reset' if reset else 'save'}:{selected}:{scope['revision']}:{patch}"
        try:
            if reset:
                await asyncio.to_thread(
                    self.controller.reset_config, scope, key=self.keys.get(action)
                )
            else:
                await asyncio.to_thread(
                    self.controller.save_config, scope, patch, key=self.keys.get(action)
                )
            self.keys.release(action)
            await self.load_config()
            self.query_one("#conversation-config-scope", Select).value = selected
            self.status("配置已更新；只影响新会话，当前会话的冻结快照不变")
        except Exception as exc:
            self.fail(exc)

    @on(Button.Pressed, "#conversation-config-save")
    async def save_config(self) -> None:
        await self._write_config(reset=False)

    @on(Button.Pressed, "#conversation-config-reset")
    async def reset_config(self) -> None:
        await self._write_config(reset=True)

    @on(Button.Pressed, "#conversation-goal-load")
    async def refresh_goals(self) -> None:
        if not self.selected:
            return
        tid = self.selected["id"]
        try:
            goals = await asyncio.to_thread(self.controller.goals, tid)
            if not self.selected or self.selected["id"] != tid:
                return
            previous = self.query_one("#conversation-goal-choice", Select).value
            self.goals = {g["id"]: g for g in goals}
            choices = self.query_one("#conversation-goal-choice", Select)
            choices.set_options(
                [(f"{g['status']} · {g['objective'][:60]}", g["id"]) for g in goals]
            )
            if previous in self.goals:
                choices.value = previous
        except Exception as exc:
            self.fail(exc)

    @on(Button.Pressed, "#conversation-goal-create")
    async def create_goal(self) -> None:
        if not self.selected:
            return
        tid = self.selected["id"]
        objective = self.query_one("#conversation-goal-objective", Input).value.strip()
        criteria = [
            x.strip()
            for x in self.query_one("#conversation-goal-criteria", Input).value.split(";")
            if x.strip()
        ]
        budget_text = self.query_one("#conversation-goal-budget", Input).value.strip()
        try:
            budget = int(budget_text) if budget_text else None
            if budget is not None and budget <= 0:
                raise ValueError("Token 预算必须为正整数")
            action = f"goal-create:{tid}:{objective}:{criteria}:{budget}"
            result = await asyncio.to_thread(
                self.controller.create_goal,
                tid,
                objective,
                criteria,
                budget,
                key=self.keys.get(action),
            )
            self.keys.release(action)
            await self.refresh_goals()
            self.query_one("#conversation-goal-choice", Select).value = result["id"]
            self.status(f"Goal 已创建：{result['id']}")
        except Exception as exc:
            self.fail(exc)

    @on(Select.Changed, "#conversation-goal-choice")
    async def choose_goal(self, event: Select.Changed) -> None:
        if not isinstance(event.value, str) or event.value not in self.goals:
            return
        goal = self.goals[event.value]
        self.query_one("#conversation-goal-edit", Input).value = goal["objective"]
        self.query_one("#conversation-goal-fields", TextArea).text = json.dumps(
            {name: goal.get(name) for name in self.GOAL_EDIT_FIELDS}, ensure_ascii=False, indent=2
        )
        self.query_one("#conversation-goal-status", Select).value = goal["status"]
        self.query_one("#conversation-goal-result", Input).value = (
            goal.get("blocked_reason") or goal.get("result_ref") or ""
        )
        self.query_one("#conversation-task-details", Static).update(
            json.dumps(goal, ensure_ascii=False, indent=2)
        )
        await self.refresh_plans()

    @on(Button.Pressed, "#conversation-goal-save")
    async def save_goal(self) -> None:
        gid = self.query_one("#conversation-goal-choice", Select).value
        if not isinstance(gid, str) or gid not in self.goals:
            self.fail(ValueError("请先选择 Goal"))
            return
        goal = self.goals[gid]
        objective = self.query_one("#conversation-goal-edit", Input).value.strip()
        status = self.query_one("#conversation-goal-status", Select).value
        result = self.query_one("#conversation-goal-result", Input).value.strip()
        try:
            changes: dict[str, Any] = self.edit_fields(
                self.query_one("#conversation-goal-fields", TextArea).text,
                self.GOAL_EDIT_FIELDS,
            )
        except Exception as exc:
            self.fail(exc)
            return
        changes.update(
            {
                "objective": objective,
                "status": status,
                "blocked_reason": result if status == "blocked" else None,
                "result_ref": result if status == "completed" else None,
            }
        )
        action = f"goal-save:{gid}:{goal['revision']}:{changes}"
        try:
            await asyncio.to_thread(
                self.controller.update_goal, goal, changes, key=self.keys.get(action)
            )
            self.keys.release(action)
            await self.refresh_goals()
            self.query_one("#conversation-goal-choice", Select).value = gid
            self.status("Goal 已由 Core 保存")
        except Exception as exc:
            self.fail(exc)

    async def refresh_plans(self) -> None:
        gid = self.query_one("#conversation-goal-choice", Select).value
        if not isinstance(gid, str) or gid not in self.goals:
            return
        try:
            plans = await asyncio.to_thread(self.controller.plans, gid)
            previous = self.query_one("#conversation-plan-choice", Select).value
            self.plans = {p["id"]: p for p in plans}
            choices = self.query_one("#conversation-plan-choice", Select)
            choices.set_options([(f"{p['status']} · {p['scope'][:60]}", p["id"]) for p in plans])
            if previous in self.plans:
                choices.value = previous
        except Exception as exc:
            self.fail(exc)

    @on(Button.Pressed, "#conversation-plan-create")
    async def create_plan(self) -> None:
        gid = self.query_one("#conversation-goal-choice", Select).value
        if not isinstance(gid, str) or gid not in self.goals:
            self.fail(ValueError("请先选择 Goal"))
            return
        scope = self.query_one("#conversation-plan-scope", Input).value.strip()
        action = f"plan-create:{gid}:{scope}"
        try:
            result = await asyncio.to_thread(
                self.controller.create_plan, gid, scope, key=self.keys.get(action)
            )
            self.keys.release(action)
            await self.refresh_plans()
            self.query_one("#conversation-plan-choice", Select).value = result["id"]
            self.status(f"Plan 已创建：{result['id']}")
        except Exception as exc:
            self.fail(exc)

    @on(Select.Changed, "#conversation-plan-choice")
    async def choose_plan(self, event: Select.Changed) -> None:
        if not isinstance(event.value, str) or event.value not in self.plans:
            return
        plan = self.plans[event.value]
        self.query_one("#conversation-plan-edit", Input).value = plan.get("scope", "")
        self.query_one("#conversation-plan-fields", TextArea).text = json.dumps(
            {name: plan.get(name, []) for name in self.PLAN_EDIT_FIELDS},
            ensure_ascii=False,
            indent=2,
        )
        self.query_one("#conversation-plan-status", Select).value = plan["status"]
        self.query_one("#conversation-task-details", Static).update(
            json.dumps(plan, ensure_ascii=False, indent=2)
        )
        await self.refresh_checklist()

    @on(Button.Pressed, "#conversation-plan-save")
    async def save_plan(self) -> None:
        pid = self.query_one("#conversation-plan-choice", Select).value
        if not isinstance(pid, str) or pid not in self.plans:
            self.fail(ValueError("请先选择 Plan"))
            return
        plan = self.plans[pid]
        try:
            changes = self.edit_fields(
                self.query_one("#conversation-plan-fields", TextArea).text,
                self.PLAN_EDIT_FIELDS,
            )
        except Exception as exc:
            self.fail(exc)
            return
        changes.update(
            {
                "scope": self.query_one("#conversation-plan-edit", Input).value.strip(),
                "status": self.query_one("#conversation-plan-status", Select).value,
            }
        )
        action = f"plan-save:{pid}:{plan['revision']}:{changes}"
        try:
            await asyncio.to_thread(
                self.controller.update_plan, plan, changes, key=self.keys.get(action)
            )
            self.keys.release(action)
            await self.refresh_plans()
            self.query_one("#conversation-plan-choice", Select).value = pid
            self.status("Plan 已由 Core 保存")
        except Exception as exc:
            self.fail(exc)

    async def refresh_checklist(self) -> None:
        pid = self.query_one("#conversation-plan-choice", Select).value
        if not isinstance(pid, str) or pid not in self.plans:
            return
        try:
            items = await asyncio.to_thread(self.controller.checklist, pid)
            previous = self.query_one("#conversation-check-choice", Select).value
            self.checklist_items = {item["id"]: item for item in items}
            choices = self.query_one("#conversation-check-choice", Select)
            choices.set_options(
                [(f"{item['status']} · {item['description'][:60]}", item["id"]) for item in items]
            )
            if previous in self.checklist_items:
                choices.value = previous
        except Exception as exc:
            self.fail(exc)

    @on(Button.Pressed, "#conversation-check-create")
    async def create_checklist(self) -> None:
        pid = self.query_one("#conversation-plan-choice", Select).value
        if not isinstance(pid, str) or pid not in self.plans:
            self.fail(ValueError("请先选择 Plan"))
            return
        description = self.query_one("#conversation-check-description", Input).value.strip()
        action = f"check-create:{pid}:{description}"
        try:
            result = await asyncio.to_thread(
                self.controller.create_checklist, pid, description, key=self.keys.get(action)
            )
            self.keys.release(action)
            await self.refresh_checklist()
            self.query_one("#conversation-check-choice", Select).value = result["id"]
            self.status(f"清单项已创建：{result['id']}")
        except Exception as exc:
            self.fail(exc)

    @on(Select.Changed, "#conversation-check-choice")
    def choose_checklist(self, event: Select.Changed) -> None:
        if isinstance(event.value, str) and event.value in self.checklist_items:
            item = self.checklist_items[event.value]
            self.query_one("#conversation-check-status", Select).value = item["status"]
            self.query_one("#conversation-check-evidence", Input).value = item.get(
                "blocker"
            ) or ";".join(item.get("evidence_refs", []))
            self.query_one("#conversation-task-details", Static).update(
                json.dumps(item, ensure_ascii=False, indent=2)
            )

    @on(Button.Pressed, "#conversation-check-save")
    async def save_checklist(self) -> None:
        cid = self.query_one("#conversation-check-choice", Select).value
        if not isinstance(cid, str) or cid not in self.checklist_items:
            self.fail(ValueError("请先选择清单项"))
            return
        item = self.checklist_items[cid]
        status = self.query_one("#conversation-check-status", Select).value
        if not isinstance(status, str):
            self.fail(ValueError("请选择清单状态"))
            return
        detail = self.query_one("#conversation-check-evidence", Input).value.strip()
        evidence = [x.strip() for x in detail.split(";") if x.strip()] if status == "done" else []
        blocker = detail if status == "blocked" else None
        action = f"check-save:{cid}:{item['revision']}:{status}:{detail}"
        try:
            await asyncio.to_thread(
                self.controller.set_checklist_status,
                item,
                status,
                evidence,
                blocker,
                key=self.keys.get(action),
            )
            self.keys.release(action)
            await self.refresh_checklist()
            self.query_one("#conversation-check-choice", Select).value = cid
            self.status("清单状态已由 Core 保存")
        except Exception as exc:
            self.fail(exc)

    async def _preview_file(self, *, diff: bool) -> None:
        if not self.selected:
            return
        tid = self.selected["id"]
        workspace = self.selected.get("workspace_ref")
        project = next(
            (p.get("project_id") for p in self.projects if p.get("workspace_ref") == workspace),
            None,
        )
        if not isinstance(project, str):
            self.fail(ValueError("会话工作区未注册，无法预览文件"))
            return
        path = self.query_one("#conversation-file-path", Input).value.strip()
        try:
            if diff:
                result = await asyncio.to_thread(self.controller.file_diff, project, path)
                content = result["diff"]
            else:
                result = await asyncio.to_thread(self.controller.file_content, project, path)
                content = result["content"]
            if not self.selected or self.selected["id"] != tid:
                return
            self.query_one("#conversation-file-meta", Static).update(
                f"{path} · {'Diff' if diff else str(result['size_bytes']) + ' 字节'}"
                + (" · 预览已截断" if result["truncated"] else "")
                + (" · SHA256 " + result["content_hash"] if not diff else "")
            )
            log = self.query_one("#conversation-file-content", RichLog)
            log.clear()
            if diff:
                log.write(Syntax(content or "（没有未提交差异）", "diff", line_numbers=False))
            else:
                language = path.rsplit(".", 1)[-1].lower() if "." in path else "text"
                if language not in {
                    "py",
                    "ts",
                    "tsx",
                    "js",
                    "jsx",
                    "json",
                    "md",
                    "sh",
                    "rs",
                    "sql",
                    "html",
                    "css",
                    "yaml",
                    "yml",
                    "toml",
                }:
                    language = "text"
                log.write(Syntax(content, language, line_numbers=True))
                self.query_one("#conversation-reference-kind", Select).value = "file"
                self.query_one("#conversation-reference", Input).value = path
        except Exception as exc:
            self.fail(exc)

    @on(Button.Pressed, "#conversation-file-read")
    async def read_file(self) -> None:
        await self._preview_file(diff=False)

    @on(Button.Pressed, "#conversation-file-diff")
    async def diff_file(self) -> None:
        await self._preview_file(diff=True)

    @on(Button.Pressed, "#conversation-terminal-open")
    async def open_terminal(self) -> None:
        if not self.connected or not self.selected or self.selected["status"] != "active":
            return
        from .terminal_screen import TerminalScreen

        tid = self.selected["id"]
        action = f"terminal-open:{tid}"
        if self.pending_terminal_dimensions is None:
            self.pending_terminal_dimensions = (
                max(20, min(300, self.size.width - 4)),
                max(5, min(150, self.size.height - 5)),
            )
        cols, rows = self.pending_terminal_dimensions
        try:
            view = await asyncio.to_thread(
                self.controller.create_terminal,
                tid,
                cols=cols,
                rows=rows,
                key=self.keys.get(action),
            )
            self.keys.release(action)
            self.pending_terminal_dimensions = None
            self.pending_terminal_approval = None
            self.terminal_approval_view = None
            self.query_one("#conversation-terminal-approval-detail", Static).update("")
            self.update_controls()
            await self.app.push_screen(TerminalScreen(self.controller, view, on_closed=self.status))
        except Exception as exc:
            # An approval-required response keeps the key for a deliberate retry after approval.
            if (
                isinstance(exc, Phase1EError)
                and isinstance(exc.detail, dict)
                and exc.detail.get("code") == "terminal_approval_required"
            ):
                approval_id = exc.detail.get("approval_id")
                if isinstance(approval_id, str) and self.selected and self.selected["id"] == tid:
                    self.pending_terminal_approval = approval_id
                    await self.load_terminal_approval()
                    self.status(f"终端需审批 {approval_id}；核对后决定，再次打开会使用同一请求键")
                return
            self.fail(exc)

    @on(Button.Pressed, "#conversation-terminal-approval-load")
    async def load_terminal_approval(self) -> None:
        approval_id = self.pending_terminal_approval
        if not approval_id or not self.selected or not self.connected:
            return
        tid = self.selected["id"]
        try:
            result = await asyncio.to_thread(self.controller.terminal_approval, approval_id)
            if (
                not self.selected
                or self.selected["id"] != tid
                or self.pending_terminal_approval != approval_id
            ):
                return
            self.terminal_approval_view = result
            self.query_one("#conversation-terminal-approval-detail", Static).update(
                f"审批 {approval_id} · {result['status']} · 截止 {result['expires_at']}\n"
                f"Action Hash：{result['action_hash']}\n"
                + json.dumps(result.get("target"), ensure_ascii=False, indent=2)
            )
            self.update_controls()
        except Exception as exc:
            self.fail(exc)

    async def _decide_terminal_approval(self, approved: bool) -> None:
        approval = self.terminal_approval_view
        approval_id = self.pending_terminal_approval
        if not approval or not approval_id or approval.get("status") != "pending":
            self.fail(ValueError("请先刷新并核对待处理的终端审批"))
            return
        action = f"terminal-approval:{approval_id}:{approved}"
        try:
            result = await asyncio.to_thread(
                self.controller.decide_terminal_approval,
                approval_id,
                approved=approved,
                key=self.keys.get(action),
            )
            self.keys.release(action)
            self.terminal_approval_view = result
            await self.load_terminal_approval()
            if not approved and self.selected:
                self.keys.release(f"terminal-open:{self.selected['id']}")
                self.pending_terminal_dimensions = None
            self.status("审批已保存；请再次点击“打开终端”" if approved else "终端请求已拒绝")
        except Exception as exc:
            self.fail(exc)

    @on(Button.Pressed, "#conversation-terminal-approval-allow")
    async def allow_terminal_approval(self) -> None:
        await self._decide_terminal_approval(True)

    @on(Button.Pressed, "#conversation-terminal-approval-deny")
    async def deny_terminal_approval(self) -> None:
        await self._decide_terminal_approval(False)
