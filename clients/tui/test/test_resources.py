from __future__ import annotations

import unittest

from operant_tui.resource_screen import ResourceScreen
from textual.app import App
from textual.widgets import Button, Select


class FixtureController:
    def __init__(self):
        self.deleted = []
        self.pinned = False

    def resources(self, thread_id, **cursors):
        return {
            "policy": {"completed_ttl_seconds": 3600, "unanswered_ttl_seconds": 259200},
            "resources": [
                {
                    "id": "artifact:temporary",
                    "kind": "context_reference_snapshot",
                    "size_bytes": 42,
                    "owner_thread_id": thread_id,
                    "retention_reason": "temporary",
                    "hold_reason": None,
                    "state": "active",
                    "pinned": self.pinned,
                },
                {
                    "id": "thread_history:history",
                    "kind": "thread_history",
                    "size_bytes": 100,
                    "retention_reason": "canonical_history",
                    "hold_reason": "canonical_history",
                    "state": "retained",
                    "pinned": False,
                },
            ],
        }

    def preview_resource_cleanup(self, thread_id, resource_id, *, key):
        return {
            "items": [
                {"resource_id": resource_id, "eligible": resource_id == "artifact:temporary"}
            ],
            "total_bytes": 42,
        }

    def cleanup_resource(self, thread_id, resource_id, *, key):
        self.deleted.append(resource_id)
        return {"removed_ids": [resource_id], "skipped": []}


class ResourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_requires_preview_and_selection_change_or_cancel_invalidates_it(self):
        controller = FixtureController()
        screen = ResourceScreen(controller, "thread-fixture")

        class ResourceApp(App):
            async def on_mount(self):
                await self.push_screen(screen)

        app = ResourceApp()
        async with app.run_test(size=(72, 32)) as pilot:
            await pilot.pause()
            self.assertTrue(screen.query_one("#resource-cleanup", Button).disabled)
            choice = screen.query_one("#resource-choice", Select)
            choice.value = "artifact:temporary"
            await pilot.pause()
            await screen.preview_cleanup()
            self.assertFalse(screen.query_one("#resource-cleanup", Button).disabled)
            choice.value = "thread_history:history"
            await pilot.pause()
            self.assertTrue(screen.query_one("#resource-cleanup", Button).disabled)
            await screen.cleanup()
            self.assertEqual(controller.deleted, [])
            choice.value = "artifact:temporary"
            await pilot.pause()
            await screen.preview_cleanup()
            screen.cancel()
            await screen.cleanup()
            self.assertEqual(controller.deleted, [])
            await screen.preview_cleanup()
            await screen.cleanup()
            self.assertEqual(controller.deleted, ["artifact:temporary"])
            self.assertTrue(screen.query_one("#resource-cleanup", Button).disabled)


if __name__ == "__main__":
    unittest.main()
