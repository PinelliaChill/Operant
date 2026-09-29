"""One live PTY attachment. Core owns the process and reports cleanup status."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from rich.text import Text
from textual.app import ComposeResult
from textual.events import Key, Resize
from textual.screen import Screen
from textual.widgets import Footer, Label, RichLog
from websockets.asyncio.client import connect
from websockets.typing import Subprotocol


def terminal_stream_url(core_url: str, terminal_id: str) -> str:
    parsed = urlsplit(core_url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise ValueError("交互终端只能连接本机 Core")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Core URL 不能包含凭据或查询参数")
    if not terminal_id:
        raise ValueError("Core 未提供终端 ID")
    scheme = "wss" if parsed.scheme == "https" else "ws"
    path = f"{parsed.path.rstrip('/')}/v1/workbench/terminals/{quote(terminal_id, safe='')}/stream"
    return urlunsplit((scheme, parsed.netloc, path, "", ""))


def terminal_token_protocol(token: str) -> Subprotocol:
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", token):
        raise ValueError("Core 未提供有效终端令牌")
    return Subprotocol(f"operant.token.{token}")


class TerminalScreen(Screen[None]):
    CSS = """
    TerminalScreen { layout: vertical; }
    #terminal-status { height: auto; min-height: 2; padding: 0 1; }
    #terminal-output { height: 1fr; border: solid $primary; }
    """
    BINDINGS = [("ctrl+q", "close", "关闭终端")]

    def __init__(
        self,
        controller: Any,
        view: dict[str, Any],
        on_closed: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__()
        self.controller = controller
        self.view = view
        self.socket: Any = None
        self.stream_task: asyncio.Task[None] | None = None
        self.closed = False
        self.on_closed = on_closed

    def compose(self) -> ComposeResult:
        yield Label("连接终端… Ctrl+Q 关闭", id="terminal-status")
        yield RichLog(id="terminal-output", wrap=False, markup=False, highlight=False)
        yield Footer()

    async def on_mount(self) -> None:
        self.stream_task = asyncio.create_task(self._stream())

    async def _stream(self) -> None:
        label = self.query_one("#terminal-status", Label)
        try:
            url = terminal_stream_url(
                self.controller.core.core_url,
                self.view["terminal_id"],
            )
            token_protocol = terminal_token_protocol(self.view.get("stream_token") or "")
            async with connect(
                url,
                subprotocols=[Subprotocol("operant.terminal.v1"), token_protocol],
                open_timeout=10,
                max_size=65_536,
            ) as socket:
                self.socket = socket
                if socket.subprotocol != "operant.terminal.v1":
                    raise ValueError("终端协议协商失败")
                label.update("终端已连接 · 输入直接发送到会话工作区 · Ctrl+Q 关闭")
                self.focus()
                async for raw in socket:
                    frame = json.loads(raw)
                    if frame.get("type") == "output":
                        self.query_one("#terminal-output", RichLog).write(
                            Text.from_ansi(str(frame.get("data", "")))
                        )
                    elif frame.get("type") == "exit":
                        label.update(f"终端已退出 · 状态码 {frame.get('exit_code')} · Ctrl+Q 返回")
                        break
        except asyncio.CancelledError:
            raise
        except Exception:
            label.update("终端连接已结束。输入不会重放；请回到会话核对。")
        finally:
            self.socket = None

    async def on_key(self, event: Key) -> None:
        if self.socket is None:
            return
        special = {
            "enter": "\r",
            "backspace": "\x7f",
            "tab": "\t",
            "escape": "\x1b",
            "up": "\x1b[A",
            "down": "\x1b[B",
            "right": "\x1b[C",
            "left": "\x1b[D",
            "ctrl+c": "\x03",
            "ctrl+d": "\x04",
            "ctrl+z": "\x1a",
        }
        data = special.get(event.key, event.character)
        if not data:
            return
        event.stop()
        event.prevent_default()
        try:
            await self.socket.send(json.dumps({"type": "input", "data": data}))
        except Exception:
            self.query_one("#terminal-status", Label).update(
                "终端输入未确认。请核对实际进程结果；不会重放。"
            )

    async def on_resize(self, event: Resize) -> None:
        if self.socket is None:
            return
        try:
            await self.socket.send(
                json.dumps(
                    {
                        "type": "resize",
                        "cols": max(20, min(300, event.size.width - 4)),
                        "rows": max(5, min(150, event.size.height - 5)),
                    }
                )
            )
        except Exception:
            self.query_one("#terminal-status", Label).update("终端尺寸更新失败；请关闭后核对状态")

    async def action_close(self) -> None:
        self.app.pop_screen()

    async def on_unmount(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.socket is not None:
            await self.socket.close()
        if self.stream_task is not None:
            self.stream_task.cancel()
        try:
            result = await asyncio.to_thread(
                self.controller.stop_terminal, self.view["terminal_id"]
            )
            if result.get("status") == "cleanup_unknown":
                self._report_close(
                    f"终端 {self.view['terminal_id']} 清理未确认，需人工核对工作区内进程"
                )
        except Exception:
            self._report_close(
                f"终端 {self.view['terminal_id']} 关闭结果未知，需人工核对工作区内进程；"
                "不会自动重放关闭"
            )

    def _report_close(self, message: str) -> None:
        if self.on_closed is not None:
            self.on_closed(message)
        self.app.notify(message, severity="warning", timeout=15)
