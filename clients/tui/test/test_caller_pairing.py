"""Mounted caller-pairing checks run in the installed TUI environment."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any

from operant_tui.app import OperantTui
from operant_tui.caller_pairing_screen import CallerPairingScreen
from textual.app import App
from textual.widgets import Button, Collapsible, Input, Select, Static


class FakeCaller:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.readback_state = "awaiting_approval"
        self.has_source = True
        self.fail_list = False

    def pending_request(self) -> dict[str, str] | None:
        return None

    def pair_ticket(self, ticket: str, name: str) -> dict[str, str]:
        self.calls.append(("pair", (ticket, name)))
        return {"scope": "skill_source.manage"}

    def list_sources(self) -> dict[str, str]:
        self.calls.append(("list", ()))
        if self.fail_list:
            raise ValueError("offline")
        return {
            "state": "completed",
            "result": {
                "items": [{"root_ref": "operant-default", "label": "内置技能"}]
                + (
                    [{"root_ref": "user-000000000001", "label": "技能库"}]
                    if self.has_source
                    else []
                )
            },
        }

    def add_source(self, path: str) -> dict[str, str]:
        self.calls.append(("add", (path,)))
        return {"state": "awaiting_approval", "approval_id": "approval_1", "action_hash": "a" * 64}

    def readback(self) -> dict[str, str]:
        self.calls.append(("readback", ()))
        if self.readback_state == "failed":
            return {"state": "failed", "error_code": "request_not_received"}
        return {"state": self.readback_state, "approval_id": "approval_1", "action_hash": "a" * 64}

    def remove_source(self, root_ref: str) -> dict[str, str]:
        self.calls.append(("remove", (root_ref,)))
        self.has_source = False
        return {"state": "completed", "operation": "remove"}

    def continue_approved(self, approval_id: str, action_hash: str) -> dict[str, str]:
        self.calls.append(("continue", (approval_id, action_hash)))
        self.has_source = True
        return {"state": "completed", "operation": "add"}

    def reset_identity(self, *, abandon_pairing: bool = False) -> None:
        self.calls.append(("reset_identity", (abandon_pairing,)))


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


class CallerPairingScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_source_refresh_failure_keeps_confirmed_write_success(self) -> None:
        caller = FakeCaller()
        app = Host(caller)
        async with app.run_test(size=(100, 65)) as pilot:
            await pilot.pause()
            app.screen.query_one("#caller-path", Input).value = "/tmp/skills"
            await pilot.click("#caller-add")
            await pilot.pause()
            caller.fail_list = True
            await pilot.click("#caller-continue")

            async def settled() -> None:
                while app.screen.busy:
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(settled(), timeout=3)
            status = str(app.screen.query_one("#caller-status", Static).render())
            assert "已完成" in status and "列表未更新" in status
            assert caller.calls.count(("add", ("/tmp/skills",))) == 1
            assert caller.calls.count(("continue", ("approval_1", "a" * 64))) == 1
            assert app.screen.query_one("#caller-source-select", Select).value is Select.NULL

    async def test_alt_7_opens_caller_pairing_screen(self) -> None:
        app = OperantTui(MainController(FakeCaller()))
        async with app.run_test() as pilot:
            await pilot.press("alt+7")
            await pilot.pause()
            assert isinstance(app.screen, CallerPairingScreen)

    async def test_mounted_pairing_screen_routes_buttons_and_clears_ticket(self) -> None:
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
            assert caller.calls[-2:] == [("remove", ("user-000000000001",)), ("list", ())]
            assert app.screen.query_one("#caller-source-select", Select).value is Select.NULL
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
            assert caller.calls[-2:] == [("continue", ("approval_1", "a" * 64)), ("list", ())]
            app.screen.query_one("#caller-source-select", Select).value = "user-000000000001"
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

    async def test_cli_pairing_start_skips_unpaired_conversation_queries(self) -> None:
        from types import SimpleNamespace

        from operant_tui.app import OperantTui
        from operant_tui.caller_pairing_screen import CallerPairingScreen

        class PairOnly:
            def pending_request(self):
                return None

        def unexpected_negotiate():
            raise AssertionError("standalone pairing must not query ungranted conversation routes")

        controller = SimpleNamespace(
            paired_skill_sources=PairOnly(), negotiate=unexpected_negotiate
        )
        app = OperantTui(controller, start_skill_sources=True)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, CallerPairingScreen)
            assert app.screen.query_one("#caller-ticket", Input).password
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("f7")
            await pilot.pause()
            assert isinstance(app.screen, CallerPairingScreen)
