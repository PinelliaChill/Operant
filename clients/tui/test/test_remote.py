from __future__ import annotations

import unittest
from typing import Any

from operant_tui.remote_screen import RemoteScreen, parse_scopes
from textual.app import App
from textual.widgets import Button, Input, Static


class RemoteScreenTests(unittest.IsolatedAsyncioTestCase):
    def test_scope_validation(self) -> None:
        self.assertEqual(
            parse_scopes("remote.control.observe, remote.control.command"),
            ["remote.control.observe", "remote.control.command"],
        )
        for value in ("remote.control.command", "remote.control.observe,unknown"):
            with self.assertRaises(ValueError):
                parse_scopes(value)

    async def test_pairing_session_and_lease_are_explicit(self) -> None:
        class Controller:
            calls: list[tuple[Any, ...]] = []
            fail = False

            def remote_projection(self) -> dict[str, Any]:
                if self.fail:
                    raise RuntimeError("connection lost")
                return {
                    "hosts": [
                        {
                            "host_id": "host-1",
                            "enabled": True,
                            "online_state": "online",
                            "protocol_version": "phase56.v1",
                        }
                    ],
                    "devices": [
                        {
                            "host_id": "host-1",
                            "device_id": "device-1",
                            "display_name": "other",
                            "scopes": ["remote.control.observe"],
                        }
                    ],
                    "sessions": [],
                    "events": [],
                    "connections": [],
                    "targets": [],
                    "jobs": [],
                }

            def create_remote_pairing(self, host_id: str, scopes: list[str]) -> dict[str, Any]:
                self.calls.append(("pair", host_id, scopes))
                return {
                    "challenge_id": "challenge-1",
                    "host_id": host_id,
                    "one_time_code": "sample-one-time-code",
                    "allowed_scopes": scopes,
                    "expires_at": "2030-01-01T00:00:00Z",
                    "relay_url": None,
                    "host_signing_public_key": "a" * 44,
                    "host_exchange_public_key": "b" * 44,
                }

            def create_remote_session(
                self, host_id: str, device_id: str, mode: str
            ) -> dict[str, Any]:
                self.calls.append(("session", host_id, device_id, mode))
                return {"remote_session_id": "session-1"}

            def acquire_remote_target_lease(self, target_id: str, workspace: str) -> dict[str, Any]:
                self.calls.append(("lease", target_id, workspace))
                return {
                    "lease_id": "lease-1",
                    "token": "sample-lease-token",
                    "fencing": 1,
                    "expires_at": "2030-01-01T00:00:00+00:00",
                }

        controller = Controller()
        screen = RemoteScreen(controller)  # type: ignore[arg-type]
        async with App().run_test(size=(110, 50)) as pilot:
            await pilot.app.push_screen(screen)
            await pilot.click("#remote-pair")
            await pilot.pause()
            self.assertEqual(controller.calls[0], ("pair", "host-1", ["remote.control.observe"]))
            self.assertIn("challenge_id", str(screen.query_one("#remote-ticket", Static).content))
            screen.query_one("#remote-device", Input).value = "device-1"
            await pilot.click("#remote-session-create")
            await pilot.pause()
            self.assertIn(("session", "host-1", "device-1", "direct"), controller.calls)
            screen.query_one("#target-id", Input).value = "target-1"
            screen.query_one("#target-workspace", Input).value = "/allowed"
            screen.query_one("#target-lease", Button).scroll_visible(animate=False)
            await pilot.pause()
            await pilot.click("#target-lease")
            await pilot.pause()
            self.assertEqual(screen.lease["token"], "sample-lease-token")
            controller.fail = True
            await screen.refresh_projection()
            self.assertTrue(screen.query_one("#target-job-submit", Button).disabled)
            controller.fail = False
            await screen.refresh_projection()
            self.assertFalse(screen.query_one("#target-job-submit", Button).disabled)
            screen.action_close()
            self.assertIsNone(screen.lease)
