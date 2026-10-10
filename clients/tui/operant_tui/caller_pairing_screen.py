"""Independent caller pairing and skill source controls for the local TUI."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from textual import on
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Collapsible, Footer, Input, Label, Select, Static

from sdk.python_client.caller_pairing import PairingClientError

from .controller import ClientController


class CallerPairingScreen(Screen[None]):
    """The pasted ticket authorizes only skill-source management."""

    CSS = """
    CallerPairingScreen { layout: vertical; }
    #caller-body { height: 1fr; padding: 0 1; }
    #caller-status, #caller-pending { height: auto; min-height: 2; }
    #caller-result { height: auto; }
    Input, Button, Select { margin-bottom: 1; }
    """
    BINDINGS = [("escape", "close", "返回"), ("ctrl+r", "refresh_pending", "核对待办")]

    def __init__(self, controller: ClientController) -> None:
        super().__init__()
        self.client = controller.paired_skill_sources
        self.busy = False
        self.bound_approval: tuple[str, str] | None = None

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="caller-body"):
            yield Label("独立客户端 · 技能目录")
            yield Static(
                "仅能查看、添加、删除技能来源。先在本机桌面确认并生成配对码，然后粘贴到这里。",
                id="caller-status",
                markup=False,
            )
            yield Input(placeholder="粘贴完整配对码", password=True, id="caller-ticket")
            yield Input(placeholder="这台客户端的名称", id="caller-name")
            yield Button("配对", id="caller-pair", variant="primary")
            yield Label("技能来源")
            yield Button("读取来源", id="caller-list")
            yield Select([], prompt="选择要移除的来源", id="caller-source-select")
            yield Input(placeholder="要添加的绝对目录路径", id="caller-path")
            yield Button("添加来源", id="caller-add")
            yield Button("移除所选来源", id="caller-remove", variant="error")
            yield Label("写入结果未知时回读原请求；不会自动重发添加或移除。")
            yield Static("无待核对请求。", id="caller-pending", markup=False)
            yield Button("回读原请求", id="caller-readback")
            yield Button("审批后继续", id="caller-continue", disabled=True)
            with Collapsible(title="高级信息", collapsed=True, id="caller-advanced"):
                yield Button("用原封包核对刚才的配对", id="caller-pair-retry")
                yield Static("", id="caller-detail", markup=False)
                yield Static("", id="caller-result", markup=False)
        yield Footer()

    async def on_mount(self) -> None:
        await self.action_refresh_pending()

    def action_close(self) -> None:
        self.query_one("#caller-ticket", Input).value = ""
        self.dismiss(None)

    async def action_refresh_pending(self) -> None:
        try:
            pending = await asyncio.to_thread(self.client.pending_request)
        except PairingClientError as exc:
            self.query_one("#caller-pending", Static).update(str(exc))
            return
        if pending is None:
            self.query_one("#caller-pending", Static).update("无待核对请求。")
            self.query_one("#caller-detail", Static).update("无待核对请求。")
        else:
            operation = "添加" if pending["operation"] == "add" else "移除"
            self.query_one("#caller-pending", Static).update(
                f"有一笔{operation}来源请求待核对。请回读，不要重新提交。"
            )
            self.query_one("#caller-detail", Static).update(
                f"Request ID: {pending['request_id']}\nPayload hash: {pending['payload_hash']}"
            )

    def _show_sources(self, receipt: dict[str, Any]) -> None:
        result = receipt.get("result")
        items = result.get("items") if isinstance(result, dict) else None
        if not isinstance(items, list):
            return
        options: list[tuple[str, str]] = []
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                continue
            root_ref = item.get("root_ref")
            if not isinstance(root_ref, str) or not root_ref:
                continue
            title = item.get("label") or item.get("path") or f"来源 {index}"
            options.append((str(title), root_ref))
        selector = self.query_one("#caller-source-select", Select)
        selector.set_options(options)
        selector.clear()

    async def _run(self, label: str, operation: Callable[[], Any]) -> None:
        if self.busy:
            return
        self.busy = True
        self.bound_approval = None
        self.query_one("#caller-continue", Button).disabled = True
        self.query_one("#caller-status", Static).update(f"正在{label}…")
        try:
            result = await asyncio.to_thread(operation)
            if isinstance(result, dict) and "state" in result:
                state = result["state"]
                if state == "completed":
                    status = f"{label}已完成。"
                elif state == "awaiting_approval":
                    approval_id = result.get("approval_id")
                    action_hash = result.get("action_hash")
                    if isinstance(approval_id, str) and isinstance(action_hash, str):
                        self.bound_approval = (approval_id, action_hash)
                        self.query_one("#caller-continue", Button).disabled = False
                    status = "等待本机 Action Gateway 审批；批准后点“审批后继续”。"
                elif state == "unconfirmed":
                    status = "结果未确认。请回读原请求并人工核对，勿重新添加或移除。"
                elif state == "in_progress":
                    status = "Core 仍在处理。稍后回读原请求，勿重新提交。"
                else:
                    status = f"{label}未完成。请查看 Core 回执或回读原请求。"
                if label == "读取来源" and state == "completed":
                    self._show_sources(result)
            else:
                status = f"{label}已验证。"
            self.query_one("#caller-status", Static).update(status)
            self.query_one("#caller-result", Static).update(
                json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2)
            )
        except PairingClientError as exc:
            self.query_one("#caller-status", Static).update(str(exc))
            self.query_one("#caller-detail", Static).update(f"错误类别: {exc.code}")
        except Exception:
            self.query_one("#caller-status", Static).update("客户端暂时无法完成此操作。")
        finally:
            self.busy = False
            await self.action_refresh_pending()

    @on(Button.Pressed, "#caller-pair")
    async def pair(self) -> None:
        ticket = self.query_one("#caller-ticket", Input).value.strip()
        name = self.query_one("#caller-name", Input).value.strip()
        self.query_one("#caller-ticket", Input).value = ""
        await self._run("配对", lambda: self.client.pair_ticket(ticket, name))

    @on(Button.Pressed, "#caller-pair-retry")
    async def pair_retry(self) -> None:
        await self._run("配对核对", self.client.retry_pair_same_envelope)

    @on(Button.Pressed, "#caller-list")
    async def list_sources(self) -> None:
        await self._run("读取来源", self.client.list_sources)

    @on(Button.Pressed, "#caller-add")
    async def add_source(self) -> None:
        path = self.query_one("#caller-path", Input).value.strip()
        await self._run("添加来源", lambda: self.client.add_source(path))

    @on(Button.Pressed, "#caller-remove")
    async def remove_source(self) -> None:
        selected = self.query_one("#caller-source-select", Select).value
        if selected is Select.NULL or not isinstance(selected, str):
            self.query_one("#caller-status", Static).update("请先从来源列表选择要移除的目录。")
            return
        if not selected.startswith("user-"):
            self.query_one("#caller-status", Static).update("内置来源不能从这里移除。")
            return
        root_ref = selected
        await self._run("删除来源", lambda: self.client.remove_source(root_ref))

    @on(Button.Pressed, "#caller-readback")
    async def readback(self) -> None:
        await self._run("回读原请求", self.client.readback)

    @on(Button.Pressed, "#caller-continue")
    async def continue_approved(self) -> None:
        if self.bound_approval is None:
            self.query_one("#caller-status", Static).update("请先回读等待审批的原请求。")
            return
        approval_id, action_hash = self.bound_approval
        await self._run(
            "继续原请求", lambda: self.client.continue_approved(approval_id, action_hash)
        )
