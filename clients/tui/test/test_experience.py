from __future__ import annotations

import copy
import unittest

from operant_tui.experience import ExperienceScreen, prepare_experience_command
from textual.app import App
from textual.widgets import Button, Input, Select


def state():
    return {
        "project_id": "p",
        "unresolved_command_ids": [],
        "skills": {
            "skills": [
                {
                    "skill_id": "s",
                    "version": {
                        "version": 3,
                        "name": "check",
                        "procedure_ref": "r",
                        "artifact": {"content_hash": "a" * 64},
                    },
                    "head": {
                        "published_version": 2,
                        "head_revision": 7,
                        "permission_epoch": 4,
                        "state": "published",
                    },
                    "rollback_versions": [1, 2],
                }
            ]
        },
        "sharing": {"writer_evidence": [], "grants": []},
        "remote": {"packs": []},
    }


class PreparationTests(unittest.TestCase):
    def test_publish_pins_draft_but_disable_pins_actual_published_version(self):
        value = state()
        publish = prepare_experience_command(value, "skill:s", "skill_publish")
        disable = prepare_experience_command(value, "skill:s", "skill_disable")
        self.assertEqual(publish["skill_version"], 3)
        self.assertEqual(disable["skill_version"], 2)
        self.assertEqual(disable["expected_head_revision"], 7)
        value["skills"]["skills"][0]["head"]["head_revision"] = 8
        self.assertEqual(disable["expected_head_revision"], 7)

    def test_unknown_object_and_unpublished_rollback_are_rejected(self):
        with self.assertRaises(ValueError):
            prepare_experience_command(state(), "skill:other", "skill_disable")
        with self.assertRaises(ValueError):
            prepare_experience_command(state(), "skill:s", "skill_rollback", rollback_version="99")
        draft = state()
        draft["skills"]["skills"][0]["head"]["published_version"] = None
        for action in ("skill_disable", "skill_rollback"):
            with self.assertRaisesRegex(ValueError, "尚未发布"):
                prepare_experience_command(draft, "skill:s", action, rollback_version="1")


class ScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_textual_screen_requires_confirmation_and_keeps_disconnect_read_only(self):
        class Controller:
            fail = False
            calls = []

            def experience(self, project):
                if self.fail:
                    raise RuntimeError("connection lost")
                return copy.deepcopy(state())

            def experience_command(self, command, *, idempotency_key):
                self.calls.append((command, idempotency_key))
                return {"message": "completed", "state": state()}

        controller = Controller()
        screen = ExperienceScreen(controller)
        app = App()
        async with app.run_test(size=(110, 45)) as pilot:
            await app.push_screen(screen)
            screen.query_one("#experience-project", Input).value = "p"
            await screen.refresh_state()
            self.assertTrue(screen.query_one("#experience-confirm", Button).disabled)
            screen.query_one("#experience-object", Select).value = "skill:s"
            screen.query_one("#experience-action", Select).value = "skill_disable"
            screen.prepare()
            self.assertFalse(screen.query_one("#experience-confirm", Button).disabled)
            await screen.confirm()
            self.assertEqual(len(controller.calls), 1)
            self.assertEqual(controller.calls[0][0]["skill_version"], 2)
            controller.fail = True
            await screen.refresh_state()
            self.assertTrue(screen.query_one("#experience-prepare", Button).disabled)
            self.assertTrue(screen.query_one("#experience-confirm", Button).disabled)
            self.assertEqual(screen.state["project_id"], "p")
            await pilot.pause()
