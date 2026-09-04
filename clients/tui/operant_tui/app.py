from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Button, Footer, Header, Input, Label, ListItem, ListView, Static

from .controller import ClientController, CommandKeys, error_view, layout_for_width


def projection_field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


class OperantTui(App[None]):
    """Keyboard-first projection client; all authority stays in Operant Core."""

    CSS = """
    Screen { layout: vertical; }
    #connection { height: 3; padding: 0 1; }
    #workspace { height: 1fr; }
    .pane { border: solid $primary; padding: 1; min-width: 24; }
    .wide #sessions { width: 24%; display: block; }
    .wide #main { width: 48%; }
    .wide #inspector { width: 28%; display: block; }
    .medium #sessions { display: none; }
    .medium #main { width: 65%; }
    .medium #inspector { width: 35%; display: block; }
    .narrow #sessions, .narrow #inspector { display: none; }
    .narrow #main { width: 100%; }
    Input, Button { margin-bottom: 1; }
    #error { color: $error; min-height: 3; }
    """

    BINDINGS = [
        Binding("alt+1", "focus_pane('sessions')", "会话"),
        Binding("alt+2", "focus_pane('main')", "运行"),
        Binding("alt+3", "focus_pane('inspector')", "检查器"),
        Binding("?", "help", "帮助"),
        Binding("/", "command", "命令"),
        Binding("escape", "dismiss_layer", "关闭"),
        Binding("q", "quit", "退出", show=False),
    ]

    def __init__(self, controller: ClientController) -> None:
        super().__init__()
        self.controller = controller
        self.command_keys = CommandKeys()
        self.pending_cancel_run_id: str | None = None
        self.stream_run_id: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("正在协商 phase1e/23/45/56…", id="connection")
        with Horizontal(id="workspace", classes="narrow"):
            with Vertical(id="sessions", classes="pane"):
                yield Label("会话 / 审批")
                yield Input(placeholder="Session ID", id="session-id")
                yield Button("读取待审批", id="load-approvals")
                yield ListView(id="approval-list")
            with Vertical(id="main", classes="pane"):
                yield Label("Graph 运行")
                yield Input(placeholder="Graph Run ID", id="run-id")
                with Container():
                    yield Button("刷新状态", id="refresh", variant="primary")
                    yield Button("从 Cursor 订阅", id="stream")
                    yield Button("恢复", id="resume", variant="success")
                    yield Button("取消…", id="cancel", variant="error")
                yield Static("尚未读取运行投影。", id="run-status")
                yield ListView(id="node-list")
            with Vertical(id="inspector", classes="pane"):
                yield Label("连接与错误")
                yield Static("颜色不是唯一状态信号。", id="error")
                yield Static("Cursor: —", id="cursor")
        yield Footer()

    async def on_mount(self) -> None:
        self._apply_layout(self.size.width)
        try:
            await asyncio.to_thread(self.controller.negotiate)
        except Exception as exc:
            self._show_error(exc)
        else:
            self.query_one("#connection", Static).update("[已连接] 协议与 Schema digest 已协商")
            self._set_mutations_enabled(True)

    def on_resize(self) -> None:
        self._apply_layout(self.size.width)

    def _apply_layout(self, width: int) -> None:
        workspace = self.query_one("#workspace")
        workspace.set_classes(layout_for_width(width))

    def _show_error(self, exc: Exception) -> None:
        view = error_view(exc)
        if view.code != "invalid_input":
            self.query_one("#connection", Static).update("[已断开] Core 状态不可用；当前视图只读")
            self._set_mutations_enabled(False)
        self.query_one("#error", Static).update(
            f"{view.code}: {view.message}\n{view.projection_trust}"
        )

    def _set_mutations_enabled(self, enabled: bool) -> None:
        self.query_one("#resume", Button).disabled = not enabled
        self.query_one("#cancel", Button).disabled = not enabled

    async def _refresh(self) -> None:
        run_id = self.query_one("#run-id", Input).value
        try:
            run, nodes = await asyncio.to_thread(self.controller.graph_projection, run_id)
        except Exception as exc:
            self._show_error(exc)
            return
        await self._render_projection(run, nodes)
        self.query_one("#connection", Static).update("[已连接] 投影已由 Core 校正")
        self._set_mutations_enabled(True)

    async def _render_projection(self, run: Any, nodes: list[Any]) -> None:
        self.query_one("#run-status", Static).update(
            f"[{projection_field(run, 'status', 'unknown')}] "
            f"revision {projection_field(run, 'revision', '—')} · 当前节点 "
            f"{', '.join(projection_field(run, 'current_node_ids', [])) or '无'}"
        )
        node_list = self.query_one("#node-list", ListView)
        await node_list.clear()
        await node_list.extend(
            [
                ListItem(
                    Label(
                        f"{projection_field(node, 'node_id', 'unknown')}  "
                        f"[{projection_field(node, 'status', 'unknown')}]"
                    )
                )
                for node in nodes
            ]
        )

    @on(Button.Pressed, "#refresh")
    async def refresh_projection(self) -> None:
        await self._refresh()

    @on(Button.Pressed, "#resume")
    async def resume(self) -> None:
        run_id = self.query_one("#run-id", Input).value.strip()
        logical_action = f"resume:{run_id}"
        try:
            await asyncio.to_thread(
                self.controller.resume_graph,
                run_id,
                idempotency_key=self.command_keys.get(logical_action),
            )
            self.command_keys.release(logical_action)
            await self._refresh()
        except Exception as exc:
            self._show_error(exc)

    @on(Button.Pressed, "#cancel")
    def confirm_cancel(self) -> None:
        run_id = self.query_one("#run-id", Input).value.strip()
        if not run_id:
            self._show_error(ValueError("Graph Run ID is required"))
            return
        self.pending_cancel_run_id = run_id
        self.query_one("#error", Static).update(
            f"危险操作：按 Ctrl+X 确认取消 Graph Run {run_id}；Esc 放弃。Core 将裁决结果。"
        )

    async def action_confirm_cancel(self) -> None:
        run_id = self.query_one("#run-id", Input).value.strip()
        if not self.pending_cancel_run_id or self.pending_cancel_run_id != run_id:
            self.query_one("#error", Static).update("请先点击“取消…”并核对目标，再按 Ctrl+X。")
            return
        self.pending_cancel_run_id = None
        logical_action = f"cancel:{run_id}"
        try:
            await asyncio.to_thread(
                self.controller.cancel_graph,
                run_id,
                idempotency_key=self.command_keys.get(logical_action),
            )
            self.command_keys.release(logical_action)
            await self._refresh()
        except Exception as exc:
            self._show_error(exc)

    @on(Button.Pressed, "#load-approvals")
    async def load_approvals(self) -> None:
        session_id = self.query_one("#session-id", Input).value
        try:
            approvals = await asyncio.to_thread(self.controller.approvals, session_id)
        except Exception as exc:
            self._show_error(exc)
            return
        listing = self.query_one("#approval-list", ListView)
        await listing.clear()
        await listing.extend(
            [
                ListItem(
                    Label(
                        f"{projection_field(item, 'category', 'unknown')} "
                        f"[{projection_field(item, 'status', 'unknown')}] "
                        f"{str(projection_field(item, 'action_hash', ''))[:12]}"
                    )
                )
                for item in approvals
            ]
        )

    @on(Button.Pressed, "#stream")
    async def stream(self) -> None:
        run_id = self.query_one("#run-id", Input).value.strip()
        if self.stream_run_id is not None:
            self.query_one("#error", Static).update(
                f"正在订阅 {self.stream_run_id}；为避免跨 scope Cursor，请等待该流结束后再切换。"
            )
            return
        self.stream_run_id = run_id

        def read_stream() -> Exception | None:
            try:
                for frame in self.controller.graph_events(
                    run_id, after_cursor=self.controller.graph_cursor(run_id)
                ):
                    cursor = self.controller.accept_graph_event(run_id, frame)
                    if cursor is None:
                        continue
                    event_name = projection_field(frame, "event", "message")
                    self.call_from_thread(
                        self.query_one("#cursor", Static).update,
                        f"Cursor: {cursor} · {event_name}",
                    )
            except Exception as exc:
                return exc
            return None

        failure = await asyncio.to_thread(read_stream)
        self.stream_run_id = None
        if failure is not None:
            view = error_view(failure)
            if view.code in {"cursor_expired", "cursor_out_of_range"}:
                self.controller.reset_graph_cursor(run_id)
                await self._refresh()
            self._show_error(failure)
            return
        try:
            run, nodes = await asyncio.to_thread(self.controller.graph_projection, run_id)
        except Exception as exc:
            self._show_error(exc)
            return
        await self._render_projection(run, nodes)
        terminal = projection_field(run, "status") in {"completed", "failed", "cancelled"}
        if terminal:
            self.query_one("#connection", Static).update("[已连接] 事件流已在持久终态结束")
            self._set_mutations_enabled(True)
        else:
            self._show_error(
                RuntimeError("SSE 在运行到达持久终态前结束；已校正投影并保留 Cursor。")
            )

    def action_focus_pane(self, pane_id: str) -> None:
        pane = self.query_one(f"#{pane_id}")
        if pane.display:
            focusable = pane.query("Input, Button, ListView").first()
            focusable.focus()

    def action_help(self) -> None:
        self.query_one("#error", Static).update(
            "Tab/Shift+Tab 移动焦点；Alt+1/2/3 切换窗格；/ 聚焦命令输入；"
            "Esc 关闭；取消需要二次确认。"
        )

    def action_command(self) -> None:
        self.query_one("#run-id", Input).focus()

    def action_dismiss_layer(self) -> None:
        self.pending_cancel_run_id = None
        self.query_one("#error", Static).update("")


# Textual maps this explicit confirmation chord without making a dangerous
# action a single-key binding.
OperantTui.BINDINGS.append(Binding("ctrl+x", "confirm_cancel", "确认取消", show=False))
