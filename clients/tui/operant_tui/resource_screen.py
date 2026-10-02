"""Resource governance over the negotiated Workbench client, with explicit preview."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from textual import on
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Input, Label, Select, Static

from .controller import CommandKeys, error_view
from .conversation import ConversationController


class ResourceScreen(Screen[None]):
    BINDINGS = [("escape", "close", "返回会话")]
    CSS = """
    #resource-body { height: 1fr; padding: 1 2; }
    #resource-details, #resource-status, #resource-preview { height: auto; }
    Input, Select, Button { margin-bottom: 1; }
    """

    def __init__(self, controller: ConversationController, thread_id: str) -> None:
        super().__init__()
        self.controller = controller
        self.thread_id = thread_id
        self.keys = CommandKeys()
        self.items: dict[str, dict[str, Any]] = {}
        self.next_cursor: dict[str, int] = {}
        self.preview_id: str | None = None
        self.connected = True
        self.busy = False

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="resource-body"):
            yield Label("临时资源与保留策略")
            yield Static("", id="resource-status", markup=False)
            yield Button("刷新资源", id="resource-refresh")
            yield Button("更多资源", id="resource-more", disabled=True)
            yield Static("", id="resource-details", markup=False)
            yield Select([], prompt="选择资源", id="resource-choice")
            yield Button("切换 Pin 保留", id="resource-pin", disabled=True)
            yield Label("确认完成后的保留时间（小时，至少 1）")
            yield Input("1", id="resource-completed-hours", type="number")
            yield Label("未回复时的保留时间（小时，默认 72）")
            yield Input("72", id="resource-unanswered-hours", type="number")
            yield Button("保存保留时间", id="resource-policy")
            yield Button("确认任务完成", id="resource-completed")
            yield Button("撤回完成确认", id="resource-uncompleted")
            yield Button("预览选中资源清理", id="resource-preview-button", disabled=True)
            yield Static("", id="resource-preview", markup=False)
            yield Button("确认清理临时资源", id="resource-cleanup", disabled=True)
            yield Button("取消清理", id="resource-cancel")
            yield Label("聊天、长期记忆、审批审计、Pin 和可恢复运行会保留。")
        yield Footer()

    async def on_mount(self) -> None:
        await self.load()

    def action_close(self) -> None:
        self.dismiss(None)

    def status(self, text: str) -> None:
        self.query_one("#resource-status", Static).update(text)

    def selected_item(self) -> dict[str, Any] | None:
        value = self.query_one("#resource-choice", Select).value
        return self.items.get(str(value))

    def controls(self) -> None:
        item = self.selected_item()
        for name in ("policy", "completed", "uncompleted", "preview-button"):
            self.query_one(f"#resource-{name}", Button).disabled = self.busy or not self.connected
        self.query_one("#resource-preview-button", Button).disabled |= item is None
        self.query_one("#resource-more", Button).disabled = (
            self.busy or not self.connected or not self.next_cursor
        )
        self.query_one("#resource-refresh", Button).disabled = self.busy
        self.query_one("#resource-pin", Button).disabled = (
            self.busy
            or not self.connected
            or item is None
            or item.get("kind") not in {"context_reference_snapshot", "artifact"}
            or item.get("state") != "active"
            or item.get("hold_reason") == "legacy_snapshot_policy"
        )
        self.query_one("#resource-cleanup", Button).disabled = (
            self.busy or not self.connected or self.preview_id is None
        )
        self.query_one("#resource-choice", Select).disabled = self.busy

    async def load(self, *, more: bool = False) -> None:
        self.busy = True
        self.preview_id = None
        self.controls()
        try:
            page = await asyncio.to_thread(
                self.controller.resources, self.thread_id, **(self.next_cursor if more else {})
            )
            self.connected = True
            if not more:
                self.items.clear()
            self.items.update({item["id"]: item for item in page["resources"]})
            self.next_cursor = page.get("next_cursor", {}) if page.get("truncated") else {}
            self.query_one("#resource-choice", Select).set_options(
                [
                    (f"{item['kind']} · {item['size_bytes']} B · {item['id']}", item["id"])
                    for item in self.items.values()
                ]
            )
            policy = page["policy"]
            for field, policy_name in (("completed", "completed"), ("unanswered", "unanswered")):
                self.query_one(f"#resource-{field}-hours", Input).value = str(
                    policy[f"{policy_name}_ttl_seconds"] / 3600
                )
            self.query_one("#resource-details", Static).update(
                f"已读取 {len(self.items)} 项 · "
                f"{sum(item['size_bytes'] for item in self.items.values())} B"
                + (" · 尚有更多资源" if self.next_cursor else "")
            )
        except Exception as exc:
            self.failed(exc)
        finally:
            self.busy = False
            self.controls()

    def failed(self, exc: Exception) -> None:
        view = error_view(exc)
        self.status(f"{view.code}: {view.message} · 请刷新核对，不自动重放")
        if (
            view.code in {"transport_unavailable", "invalid_error_envelope", "client_error"}
            or "protocol" in view.code
        ):
            self.connected = False

    async def mutate(self, identity: str, operation: Callable[[str], Any]) -> None:
        if self.busy or not self.connected:
            return
        self.busy = True
        self.controls()
        key = self.keys.get(identity)
        try:
            result = await asyncio.to_thread(operation, key)
            self.keys.release(identity)
            self.status(json.dumps(result, ensure_ascii=False))
            await self.load()
        except Exception as exc:
            self.failed(exc)
        finally:
            self.busy = False
            self.controls()

    @on(Select.Changed, "#resource-choice")
    def selection(self) -> None:
        self.preview_id = None
        self.query_one("#resource-preview", Static).update("")
        item = self.selected_item()
        if item:
            self.query_one("#resource-details", Static).update(
                f"{item['id']} · {item['size_bytes']} B\n"
                f"归属：{item.get('owner_thread_id') or '未核验'}\n"
                f"保留原因：{item['retention_reason']}\n"
                f"保留锁：{item.get('hold_reason') or '无'}\n"
                f"到期：{item.get('due_at') or '不适用'}"
            )
        self.controls()

    @on(Button.Pressed, "#resource-refresh")
    async def refresh_resources(self) -> None:
        await self.load()

    @on(Button.Pressed, "#resource-more")
    async def more_resources(self) -> None:
        await self.load(more=True)

    @on(Button.Pressed, "#resource-pin")
    async def pin(self) -> None:
        item = self.selected_item()
        if item:
            pinned = not item.get("pinned", False)
            await self.mutate(
                f"pin:{item['id']}:{pinned}",
                lambda key: self.controller.pin_resource(
                    self.thread_id, item["id"], pinned, key=key
                ),
            )

    @on(Button.Pressed, "#resource-policy")
    async def policy(self) -> None:
        try:
            completed = float(self.query_one("#resource-completed-hours", Input).value)
            unanswered = float(self.query_one("#resource-unanswered-hours", Input).value)
            if not 1 <= completed <= 8760 or not 1 <= unanswered <= 8760:
                raise ValueError("保留时间应为 1～8760 小时")
            await self.mutate(
                f"policy:{completed}:{unanswered}",
                lambda key: self.controller.resource_policy(
                    self.thread_id, round(completed * 3600), round(unanswered * 3600), key=key
                ),
            )
        except ValueError as exc:
            self.status(str(exc))

    @on(Button.Pressed, "#resource-completed")
    async def completed(self) -> None:
        await self.mutate(
            "completed:true",
            lambda key: self.controller.confirm_resources(self.thread_id, True, key=key),
        )

    @on(Button.Pressed, "#resource-uncompleted")
    async def uncompleted(self) -> None:
        await self.mutate(
            "completed:false",
            lambda key: self.controller.confirm_resources(self.thread_id, False, key=key),
        )

    @on(Button.Pressed, "#resource-preview-button")
    async def preview_cleanup(self) -> None:
        item = self.selected_item()
        if not item:
            return
        self.busy = True
        self.controls()
        try:
            result = await asyncio.to_thread(
                self.controller.preview_resource_cleanup,
                self.thread_id,
                item["id"],
                key=self.keys.get(f"preview:{item['id']}"),
            )
            self.keys.release(f"preview:{item['id']}")
            self.query_one("#resource-preview", Static).update(
                json.dumps(result, ensure_ascii=False)
            )
            self.preview_id = item["id"] if result["items"][0]["eligible"] else None
        except Exception as exc:
            self.failed(exc)
        finally:
            self.busy = False
            self.controls()

    @on(Button.Pressed, "#resource-cleanup")
    async def cleanup(self) -> None:
        resource_id = self.preview_id
        if resource_id:
            self.preview_id = None
            await self.mutate(
                f"cleanup:{resource_id}",
                lambda key: self.controller.cleanup_resource(self.thread_id, resource_id, key=key),
            )

    @on(Button.Pressed, "#resource-cancel")
    def cancel(self) -> None:
        self.preview_id = None
        self.query_one("#resource-preview", Static).update("清理已取消，未提交删除。")
        self.controls()
