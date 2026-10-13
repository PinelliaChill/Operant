"""Mounted Textual caller-pairing screen uses only the scoped client interface."""

# ruff: noqa: E402

from __future__ import annotations

import asyncio
import importlib.util
from types import SimpleNamespace
from typing import Any

import pytest

TEXTUAL_AVAILABLE = (
    importlib.util.find_spec("textual") is not None
    and importlib.util.find_spec("operant_tui") is not None
)
if TEXTUAL_AVAILABLE:
    from operant_tui.app import OperantTui
    from operant_tui.caller_pairing_screen import CallerPairingScreen
    from textual.app import App
    from textual.widgets import Button, Collapsible, Input, Select, Static


class FakeCaller:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.readback_state = "awaiting_approval"

    def pending_request(self) -> dict[str, str] | None:
        return None

    def pair_ticket(self, ticket: str, name: str) -> dict[str, str]:
        self.calls.append(("pair", (ticket, name)))
        return {"scope": "skill_source.manage"}

    def list_sources(self) -> dict[str, str]:
        self.calls.append(("list", ()))
        return {
            "state": "completed",
            "result": {"items": [{"root_ref": "user-000000000001", "label": "技能库"}]},
        }

    def add_source(self, path: str) -> dict[str, str]:
        self.calls.append(("add", (path,)))
        return {
            "state": "awaiting_approval",
            "approval_id": "approval_1",
            "action_hash": "a" * 64,
        }

    def readback(self) -> dict[str, str]:
        self.calls.append(("readback", ()))
        if self.readback_state == "failed":
            return {"state": "failed", "error_code": "request_not_received"}
        return {
            "state": self.readback_state,
            "approval_id": "approval_1",
            "action_hash": "a" * 64,
        }

    def remove_source(self, root_ref: str) -> dict[str, str]:
        self.calls.append(("remove", (root_ref,)))
        return {"state": "completed"}

    def continue_approved(self, approval_id: str, action_hash: str) -> dict[str, str]:
        self.calls.append(("continue", (approval_id, action_hash)))
        return {"state": "completed"}

    def reset_identity(self, *, abandon_pairing: bool = False) -> None:
        self.calls.append(("reset_identity", (abandon_pairing,)))


if TEXTUAL_AVAILABLE:

    class Host(App[None]):
        def __init__(self, caller: FakeCaller) -> None:
            super().__init__()
            self.caller = caller

        async def on_mount(self) -> None:
            self.push_screen(CallerPairingScreen(SimpleNamespace(paired_skill_sources=self.caller)))


class MainController:
    def __init__(self, caller: FakeCaller) -> None:
        self.paired_skill_sources = caller

    def negotiate(self) -> None:
        raise ValueError("offline")


@pytest.mark.asyncio
@pytest.mark.skipif(not TEXTUAL_AVAILABLE, reason="Textual is installed in the TUI project")
async def test_alt_7_opens_caller_pairing_screen() -> None:
    app = OperantTui(MainController(FakeCaller()))  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await pilot.press("alt+7")
        await pilot.pause()
        assert isinstance(app.screen, CallerPairingScreen)


@pytest.mark.asyncio
@pytest.mark.skipif(not TEXTUAL_AVAILABLE, reason="Textual is installed in the TUI project")
async def test_mounted_pairing_screen_routes_buttons_and_clears_ticket() -> None:
    caller = FakeCaller()
    app = Host(caller)
    async with app.run_test() as pilot:

        async def settled() -> None:
            await pilot.pause()

            async def wait_for_operation() -> None:
                while app.screen.busy or any(
                    button.has_class("-active") for button in app.screen.query(Button)
                ):
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(wait_for_operation(), timeout=5)

        await pilot.resize_terminal(100, 65)
        await settled()
        app.screen.query_one("#caller-ticket", Input).value = "ticket-secret"
        app.screen.query_one("#caller-name", Input).value = "My TUI"
        await pilot.click("#caller-pair")
        await settled()
        assert caller.calls[-1] == ("pair", ("ticket-secret", "My TUI"))
        assert app.screen.query_one("#caller-ticket", Input).value == ""
        assert app.screen.query_one("#caller-advanced", Collapsible).collapsed
        for old_id in (
            "#caller-root-ref",
            "#caller-request-id",
            "#caller-approval-id",
            "#caller-action-hash",
        ):
            assert not app.screen.query(old_id)
        await pilot.click("#caller-list")
        await settled()
        assert app.screen.query_one("#caller-source-select", Select).value is Select.NULL
        app.screen.query_one("#caller-source-select", Select).value = "user-000000000001"
        await pilot.click("#caller-remove")
        await settled()
        assert caller.calls[-1] == ("remove", ("user-000000000001",))
        app.screen.query_one("#caller-path", Input).value = "/tmp/skills"
        await pilot.click("#caller-add")
        await settled()
        assert caller.calls[-1] == ("add", ("/tmp/skills",))
        assert "等待" in str(app.screen.query_one("#caller-status", Static).render())
        assert "approval_1" not in str(app.screen.query_one("#caller-status", Static).render())
        await pilot.click("#caller-readback")
        await settled()
        assert caller.calls[-1] == ("readback", ())
        await pilot.click("#caller-continue")
        await settled()
        assert caller.calls[-1] == ("continue", ("approval_1", "a" * 64))
        assert caller.calls.count(("add", ("/tmp/skills",))) == 1
        caller.readback_state = "unconfirmed"
        await pilot.click("#caller-readback")
        await settled()
        assert "结果未确认" in str(app.screen.query_one("#caller-status", Static).render())
        caller.readback_state = "failed"
        await pilot.click("#caller-readback")
        await settled()
        assert "该请求未执行，已停止接收" in str(
            app.screen.query_one("#caller-status", Static).render()
        )
        app.screen.query_one("#caller-advanced", Collapsible).collapsed = False
        await settled()
        await pilot.click("#caller-reset-identity")
        await settled()
        assert caller.calls[-1] == ("reset_identity", (True,))
        assert "新配对码" in str(app.screen.query_one("#caller-status", Static).render())
