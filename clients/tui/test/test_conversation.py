from __future__ import annotations

import unittest
from types import SimpleNamespace

from operant_tui.conversation import ConversationController, history_lines
from operant_tui.conversation_screen import ConversationScreen
from textual.app import App
from textual.widgets import Button, Tree

from sdk.python_client.transport import Phase1EError


def thread(tid="parent", parent=None):
    return {
        "id": tid,
        "status": "active",
        "parent_thread_id": parent,
        "workspace_ref": "/workspace",
        "legacy_refs": [{"source_type": "session", "source_id": "session-" + tid}],
    }


class ConversationTests(unittest.TestCase):
    def test_history_follows_cursor_without_exceeding_service_limit(self):
        calls = []

        class Client:
            def get_session_history(self, sid, *, limit, after_cursor=None):
                calls.append((sid, limit, after_cursor))
                return {
                    "items": [{"id": "first" if after_cursor is None else "last"}],
                    "next_cursor": 7 if after_cursor is None else None,
                }

        controller = ConversationController(SimpleNamespace(), b2=Client())
        result = controller.history(thread())
        self.assertEqual([i["id"] for i in result["items"]], ["first", "last"])
        self.assertEqual(calls, [("session-parent", 999, None), ("session-parent", 999, 7)])

    def test_failed_or_incomplete_stream_never_reports_success(self):
        class Client:
            frames = []

            def run_session_stream(self, *args, **kwargs):
                return SimpleNamespace(receipt=None, events=iter(self.frames))

        client = Client()
        controller = ConversationController(SimpleNamespace(phase1e=client), b2=object())
        for frames in ([], [{"event": "agent.failed"}], [{"event": "agent.stream_error"}]):
            client.frames = frames
            with self.assertRaises(Phase1EError):
                list(controller.run(thread(), "task", [], key="stable"))
        client.frames = [{"event": "agent.completed"}]
        self.assertEqual(len(list(controller.run(thread(), "task", [], key="stable"))), 1)

    def test_retry_retains_thread_and_session_command_keys(self):
        calls = []

        class Client:
            def create_thread(self, body, *, idempotency_key):
                calls.append(("thread", body, idempotency_key))
                return {"id": "thread-1"}

            def create_session(self, body, *, idempotency_key):
                calls.append(("session", body, idempotency_key))
                return {"id": "session-1"}

        client = Client()
        controller = ConversationController(SimpleNamespace(phase1e=client), b2=client)
        controller.create("project", "role", key="logical-action")
        controller.create("project", "role", key="logical-action")
        self.assertEqual(calls[:2], calls[2:])
        self.assertEqual(calls[1][1]["thread_id"], "thread-1")

    def test_history_does_not_render_hidden_context_or_tool_arguments(self):
        lines = history_lines(
            [
                {"payload": {"type": "user_message", "text": "[bold]literal[/bold]"}},
                {
                    "payload": {
                        "type": "tool_call",
                        "tool_name": "read_file",
                        "arguments": {"private": "not for display"},
                    }
                },
            ]
        )
        self.assertEqual(lines, ["user_message: [bold]literal[/bold]", "tool_call: read_file"])


class ConversationScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_child_task_state_is_separate_from_active_conversation(self):
        class Controller:
            task_status = "completed"

            def commands(self):
                return {"commands": []}

            def child_agents(self, parent):
                return [{"thread_id": "child", "status": self.task_status}]

            def collaboration(self, tid):
                return {
                    "children": self.child_agents(tid) if tid == "parent" else [],
                    "messages": [],
                }

            def catalog(self):
                return {
                    "projects": [],
                    "roles": [],
                    "threads": [thread(), thread("child", "parent")],
                }

            def history(self, value):
                return {"items": [], "session": {"role_snapshot": {"model_id": "discovered"}}}

        controller = Controller()
        screen = ConversationScreen(SimpleNamespace(core_url="http://127.0.0.1:8000"))
        screen.controller = controller
        async with App().run_test(size=(80, 40)) as pilot:
            await pilot.app.push_screen(screen)
            await pilot.pause()
            screen.switch_selection(screen.threads["child"])
            await screen.load_selected()
            root = screen.query_one("#conversation-tree", Tree).root
            self.assertIn("任务 completed · 会话 active", str(root.children[0].children[0].label))
            self.assertFalse(screen.query_one("#conversation-send", Button).disabled)
            self.assertFalse(screen.active_children)
            controller.task_status = "running"
            await screen.load_selected()
            self.assertTrue(screen.active_children)
            self.assertIn("任务 running", str(root.children[0].children[0].label))

    async def test_nested_history_and_disconnect_disable_mutations(self):
        class Controller:
            fail = False

            def commands(self):
                return {"commands": []}

            def collaboration(self, tid):
                return {"children": [], "messages": []}

            def catalog(self):
                if self.fail:
                    raise RuntimeError("disconnected")
                return {
                    "projects": [],
                    "roles": [],
                    "threads": [thread(), thread("child", "parent")],
                }

            def history(self, value):
                return {"items": [], "session": {"role_snapshot": {"model_id": "discovered"}}}

        controller = Controller()
        screen = ConversationScreen(SimpleNamespace(core_url="http://127.0.0.1:8000"))
        screen.controller = controller
        async with App().run_test(size=(80, 40)) as pilot:
            await pilot.app.push_screen(screen)
            await pilot.pause()
            root = screen.query_one("#conversation-tree", Tree).root
            self.assertEqual(root.children[0].data, "parent")
            self.assertEqual(root.children[0].children[0].data, "child")
            screen.selected = thread()
            await screen.load_selected()
            self.assertFalse(screen.query_one("#conversation-send", Button).disabled)
            controller.fail = True
            await screen.refresh_catalog()
            self.assertTrue(screen.query_one("#conversation-send", Button).disabled)
            self.assertTrue(screen.query_one("#conversation-child", Button).disabled)
            self.assertEqual(screen.selected["id"], "parent")
