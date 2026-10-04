from __future__ import annotations

import asyncio
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

from operant_tui.app import OperantTui
from operant_tui.conversation import ConversationController, history_lines
from operant_tui.conversation_screen import ConversationScreen
from operant_tui.terminal_screen import (
    TerminalScreen,
    terminal_stream_url,
    terminal_token_protocol,
)
from textual.app import App
from textual.widgets import Button, Input, Select, Static, TextArea, Tree

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
    def test_extension_commands_use_generated_phase56_client_and_same_retry_key(self):
        calls = []

        class Phase56:
            def list_extension_commands(self):
                return {
                    "registry_version": "operant-local-extension.v1",
                    "commands": [{"name": "ext_demo_status__abc123def456"}],
                }

            def execute_extension_command(self, thread_id, request, *, idempotency_key):
                calls.append((thread_id, request, idempotency_key))
                return {"command": request["command"], "status": "completed", "result": {}}

        controller = ConversationController(SimpleNamespace(), b2=object(), phase56=Phase56())
        self.assertEqual(
            controller.extension_commands()["commands"][0]["name"],
            "ext_demo_status__abc123def456",
        )
        controller.extension_command(
            "thread-1", "ext_demo_status__abc123def456", {"label": "today"}, key="retry-1"
        )
        self.assertEqual(calls[0][0], "thread-1")
        self.assertEqual(calls[0][1]["arguments"], {"label": "today"})
        self.assertEqual(calls[0][1]["idempotency_key"], calls[0][2])

    def test_configuration_uses_effective_scope_revision_and_replace_patch(self):
        calls = []

        class Phase3:
            def get_effective_config(self, **kwargs):
                calls.append(("effective", kwargs))
                return {"scopes": {"role": {"scope_id": "role-1", "revision": 3}}}

            def put_config_scope(self, scope_type, scope_id, request, *, idempotency_key):
                calls.append(("put", scope_type, scope_id, request, idempotency_key))
                return {"revision": 4}

            def delete_config_scope(
                self, scope_type, scope_id, *, expected_revision, idempotency_key
            ):
                calls.append(("delete", scope_type, scope_id, expected_revision, idempotency_key))
                return {"deleted": True}

        controller = ConversationController(SimpleNamespace(), b2=object(), phase3=Phase3())
        scope = {"scope_type": "role", "scope_id": "role-1", "revision": 3}
        controller.effective_config("role-1", "project-1", "/workspace")
        controller.save_config(scope, '{"effort":"low"}', key="same-key")
        controller.reset_config(scope, key="reset-key")
        self.assertEqual(calls[0][1]["workspace_ref"], "/workspace")
        self.assertEqual(calls[1][3], {"patch": {"effort": "low"}, "expected_revision": 3})
        self.assertEqual(calls[2][-2:], (3, "reset-key"))
        with self.assertRaises(ValueError):
            controller.save_config(scope, '["not a patch"]', key="invalid")
        self.assertEqual(len(calls), 3)

    def test_goal_plan_and_checklist_forward_cas_and_evidence(self):
        calls = []

        class Phase3:
            def create_goal(self, request, *, idempotency_key):
                calls.append(("goal", request, idempotency_key))
                return {"id": "goal-1"}

            def update_goal(self, goal_id, request, *, idempotency_key):
                calls.append(("update-goal", goal_id, request, idempotency_key))
                return request

            def create_plan(self, goal_id, request, *, idempotency_key):
                calls.append(("plan", goal_id, request, idempotency_key))
                return {"id": "plan-1"}

            def update_plan(self, plan_id, request, *, idempotency_key):
                calls.append(("update-plan", plan_id, request, idempotency_key))
                return request

            def command_checklist_status(self, item_id, request, *, idempotency_key):
                calls.append(("check", item_id, request, idempotency_key))
                return request

        controller = ConversationController(SimpleNamespace(), b2=object(), phase3=Phase3())
        controller.create_goal("thread-1", "Finish", ["verified"], 1000, key="g")
        controller.update_goal(
            {"id": "goal-1", "revision": 2},
            {"status": "completed", "result_ref": "artifact-1"},
            key="ug",
        )
        controller.create_plan("goal-1", "Scope", key="p")
        controller.update_plan({"id": "plan-1", "revision": 5}, {"status": "approved"}, key="up")
        controller.set_checklist_status(
            {"id": "check-1", "revision": 4}, "done", ["artifact-1"], None, key="c"
        )
        self.assertEqual(calls[1][2]["expected_revision"], 2)
        self.assertEqual(calls[3][2]["expected_revision"], 5)
        self.assertEqual(calls[4][2]["evidence_refs"], ["artifact-1"])
        with self.assertRaises(ValueError):
            controller.create_plan("goal-1", " ", key="bad")
        self.assertEqual(len(calls), 5)

    def test_file_and_terminal_use_generated_client_and_bounded_preview(self):
        calls = []

        class Workbench:
            def get_workbench_file_content(self, workspace_id, **kwargs):
                calls.append(("file", workspace_id, kwargs))
                return {"content": "hi"}

            def get_workbench_file_diff(self, workspace_id, **kwargs):
                calls.append(("diff", workspace_id, kwargs))
                return {"diff": ""}

            def create_workbench_terminal(self, thread_id, request, *, idempotency_key):
                calls.append(("terminal", thread_id, request, idempotency_key))
                return {"terminal_id": "terminal-1"}

            def delete_workbench_terminal(self, terminal_id):
                calls.append(("close", terminal_id))
                return {"status": "terminated"}

        controller = ConversationController(SimpleNamespace(), b2=object(), workbench=Workbench())
        controller.file_content("project-1", "src/main.py")
        controller.file_diff("project-1", "src/main.py")
        controller.create_terminal("thread-1", cols=80, rows=24, key="stable")
        controller.stop_terminal("terminal-1")
        self.assertEqual(calls[0][2]["max_bytes"], 64_000)
        self.assertEqual(calls[2][2]["idempotency_key"], "stable")
        self.assertEqual(calls[2][3], "stable")
        with self.assertRaises(ValueError):
            controller.file_content("project-1", " ")
        self.assertEqual(len(calls), 4)

    def test_terminal_approval_uses_phase45_client(self):
        calls = []

        class Phase45:
            def get_phase45_approval(self, approval_id):
                calls.append(("read", approval_id))
                return {"approval_id": approval_id, "status": "pending"}

            def decide_phase45_approval(self, approval_id, request, *, idempotency_key):
                calls.append(("decide", approval_id, request, idempotency_key))
                return {"approval_id": approval_id, "status": "approved"}

        core = SimpleNamespace(phase45=Phase45())
        controller = ConversationController(core, b2=object())
        self.assertEqual(controller.terminal_approval("approval-1")["status"], "pending")
        controller.decide_terminal_approval("approval-1", approved=True, key="stable")
        self.assertEqual(calls[-1], ("decide", "approval-1", {"approved": True}, "stable"))

    def test_artifact_options_use_thread_scoped_generated_client(self):
        calls = []

        class Workbench:
            def list_workbench_reference_artifacts(self, tid, *, after_cursor, limit):
                calls.append((tid, after_cursor, limit))
                return {"items": [], "next_cursor": None}

        controller = ConversationController(SimpleNamespace(), b2=object(), workbench=Workbench())
        self.assertEqual(controller.artifacts("thread-1", 25), {"items": [], "next_cursor": None})
        self.assertEqual(calls, [("thread-1", 25, 50)])

    def test_terminal_url_stays_loopback_and_never_contains_token(self):
        url = terminal_stream_url("http://127.0.0.1:8000", "terminal-1")
        self.assertEqual(url, "ws://127.0.0.1:8000/v1/workbench/terminals/terminal-1/stream")
        with self.assertRaises(ValueError):
            terminal_stream_url("https://example.com", "terminal-1")
        with self.assertRaises(ValueError):
            terminal_stream_url("http://127.0.0.1:8000", "")
        with self.assertRaises(ValueError):
            terminal_stream_url("http://user:pass@localhost:8000", "terminal-1")
        self.assertTrue(
            terminal_stream_url("http://localhost:8000", "terminal-1").startswith(
                "ws://localhost:8000/"
            )
        )
        self.assertEqual(
            terminal_token_protocol("abcdefghijklmnop"), "operant.token.abcdefghijklmnop"
        )
        with self.assertRaises(ValueError):
            terminal_token_protocol("in valid")

    def test_goal_and_plan_json_fields_reject_unknown_or_non_object(self):
        self.assertEqual(
            ConversationScreen.edit_fields(
                '{"completion_criteria":["reviewed"]}', ConversationScreen.GOAL_EDIT_FIELDS
            ),
            {"completion_criteria": ["reviewed"]},
        )
        for raw in ("[]", '{"revision":999}', '{"unexpected":true}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                ConversationScreen.edit_fields(raw, ConversationScreen.PLAN_EDIT_FIELDS)

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
    async def test_artifact_pilot_only_attaches_selected_authorized_item(self):
        class Controller:
            references = []

            def commands(self):
                return {"commands": []}

            def extension_commands(self):
                return {"commands": []}

            def skill_commands(self, tid):
                return {"commands": []}

            def catalog(self):
                return {"projects": [], "roles": [], "threads": [thread()]}

            def history(self, value):
                return {
                    "items": [],
                    "session": {"role_snapshot": {"role_id": "role-1", "model_id": "model-1"}},
                }

            def collaboration(self, tid):
                return {"children": [], "messages": []}

            def artifacts(self, tid, cursor):
                assert tid == "parent"
                if cursor is None:
                    return {
                        "items": [
                            {
                                "id": "artifact-1",
                                "source": "session-parent",
                                "summary": "read me",
                                "media_type": "text/plain",
                                "content_hash": "abc",
                                "size_bytes": 7,
                            }
                        ],
                        "next_cursor": 1,
                    }
                return {
                    "items": [
                        {
                            "id": "artifact-2",
                            "source": "parent",
                            "summary": "second",
                            "media_type": "text/plain",
                            "content_hash": "def",
                            "size_bytes": 8,
                        }
                    ],
                    "next_cursor": None,
                }

            def reference(self, tid, kind, target, *, key):
                self.references.append((tid, kind, target))
                return {
                    "reference": {"target_id": target},
                    "summary": "selected",
                    "content_hash": "abc",
                    "truncated": False,
                }

        controller = Controller()
        screen = ConversationScreen(SimpleNamespace(core_url="http://127.0.0.1:8000"))
        screen.controller = controller
        async with App().run_test(size=(100, 45)) as pilot:
            await pilot.app.push_screen(screen)
            await pilot.pause()
            screen.switch_selection(screen.threads["parent"])
            await screen.load_selected()
            screen.query_one("#conversation-reference-kind", Select).value = "artifact"
            screen.query_one("#conversation-reference", Input).value = "arbitrary-id"
            await screen.add_reference()
            self.assertEqual(controller.references, [])
            await screen.list_artifacts()
            await screen.more_artifacts()
            self.assertEqual(set(screen.artifacts), {"artifact-1", "artifact-2"})
            screen.query_one("#conversation-artifact-choice", Select).value = "artifact-2"
            await pilot.pause()
            await screen.add_reference()
            self.assertEqual(controller.references, [("parent", "artifact", "artifact-2")])

    async def test_terminal_approval_pilot_requires_explicit_decision_and_keeps_retry_key(self):
        class Controller:
            requests = []
            decisions = []
            approval_status = "pending"

            def commands(self):
                return {"commands": []}

            def extension_commands(self):
                return {"commands": []}

            def skill_commands(self, tid):
                return {"commands": []}

            def catalog(self):
                return {"projects": [], "roles": [], "threads": [thread()]}

            def history(self, value):
                return {
                    "items": [],
                    "session": {"role_snapshot": {"role_id": "role-1", "model_id": "model-1"}},
                }

            def collaboration(self, tid):
                return {"children": [], "messages": []}

            def create_terminal(self, tid, *, cols, rows, key):
                self.requests.append(key)
                raise Phase1EError(
                    "terminal_approval_required",
                    "approval required",
                    detail={
                        "code": "terminal_approval_required",
                        "approval_id": "approval-1",
                    },
                )

            def terminal_approval(self, approval_id):
                return {
                    "approval_id": approval_id,
                    "status": self.approval_status,
                    "expires_at": "future",
                    "action_hash": "hash-1",
                    "target": {"thread_id": "parent", "tool": "workbench_terminal"},
                }

            def decide_terminal_approval(self, approval_id, *, approved, key):
                self.decisions.append((approval_id, approved, key))
                self.approval_status = "approved" if approved else "denied"
                return self.terminal_approval(approval_id)

        controller = Controller()
        screen = ConversationScreen(SimpleNamespace(core_url="http://127.0.0.1:8000"))
        screen.controller = controller
        async with App().run_test(size=(100, 45)) as pilot:
            await pilot.app.push_screen(screen)
            await pilot.pause()
            screen.switch_selection(screen.threads["parent"])
            await screen.load_selected()
            await screen.open_terminal()
            self.assertEqual(screen.pending_terminal_approval, "approval-1")
            self.assertFalse(
                screen.query_one("#conversation-terminal-approval-allow", Button).disabled
            )
            original_key = controller.requests[0]
            await screen.allow_terminal_approval()
            self.assertEqual(controller.decisions[0][1], True)
            self.assertEqual(screen.keys.get("terminal-open:parent"), original_key)
            self.assertTrue(
                screen.query_one("#conversation-terminal-approval-allow", Button).disabled
            )

    async def test_config_pilot_uses_loaded_revision_and_keeps_connection(self):
        class Controller:
            revision = 2
            saves = []

            def commands(self):
                return {"commands": []}

            def extension_commands(self):
                return {"commands": []}

            def skill_commands(self, tid):
                return {"commands": []}

            def catalog(self):
                return {
                    "projects": [{"workspace_ref": "/workspace", "project_id": "project-1"}],
                    "roles": [],
                    "threads": [thread()],
                }

            def history(self, value):
                return {
                    "items": [],
                    "session": {"role_snapshot": {"role_id": "role-1", "model_id": "model-1"}},
                }

            def collaboration(self, tid):
                return {"children": [], "messages": []}

            def effective_config(self, role_id, project_id, workspace_ref):
                assert (role_id, project_id, workspace_ref) == ("role-1", "project-1", "/workspace")
                return {
                    "values": {"effort": "medium"},
                    "sources": {"effort": {"scope_type": "role"}},
                    "revisions": {"role": self.revision},
                    "scopes": {
                        "role": {
                            "scope_type": "role",
                            "scope_id": "role-1",
                            "revision": self.revision,
                            "patch": {"effort": "medium"},
                        }
                    },
                }

            def save_config(self, scope, patch_text, *, key):
                self.saves.append((scope["revision"], patch_text))
                self.revision += 1
                return {"revision": self.revision}

        controller = Controller()
        screen = ConversationScreen(SimpleNamespace(core_url="http://127.0.0.1:8000"))
        screen.controller = controller
        async with App().run_test(size=(100, 45)) as pilot:
            await pilot.app.push_screen(screen)
            await pilot.pause()
            screen.switch_selection(screen.threads["parent"])
            await screen.load_selected()
            await screen.load_config()
            screen.query_one("#conversation-config-scope", Select).value = "role"
            await pilot.pause()
            screen.query_one("#conversation-config-patch", TextArea).text = '{"effort":"low"}'
            await screen.save_config()
            await pilot.pause()
            self.assertEqual(controller.saves, [(2, '{"effort":"low"}')])
            self.assertTrue(screen.connected)
            self.assertEqual(screen.config_scopes["role"]["revision"], 3)

    async def test_terminal_pilot_sends_keys_and_closes(self):
        class Socket:
            subprotocol = "operant.terminal.v1"

            def __init__(self):
                self.sent = []
                self.closed = asyncio.Event()

            async def send(self, frame):
                self.sent.append(frame)

            async def close(self):
                self.closed.set()

            def __aiter__(self):
                return self

            async def __anext__(self):
                await self.closed.wait()
                raise StopAsyncIteration

        socket = Socket()
        stopped = []
        reports = []

        @asynccontextmanager
        async def fake_connect(*args, **kwargs):
            self.assertEqual(
                args[0], "ws://localhost:8000/v1/workbench/terminals/terminal-1/stream"
            )
            self.assertEqual(
                kwargs["subprotocols"], ["operant.terminal.v1", "operant.token.abcdefghijklmnop"]
            )
            yield socket
            await socket.close()

        def stop_terminal(tid):
            stopped.append(tid)
            return {"status": "cleanup_unknown"}

        controller = SimpleNamespace(
            core=SimpleNamespace(core_url="http://localhost:8000"),
            stop_terminal=stop_terminal,
        )
        screen = TerminalScreen(
            controller,
            {"terminal_id": "terminal-1", "stream_token": "abcdefghijklmnop"},
            on_closed=reports.append,
        )

        class TestApp(OperantTui):
            async def on_mount(self):
                pass

        with patch("operant_tui.terminal_screen.connect", fake_connect):
            async with TestApp(SimpleNamespace()).run_test(size=(80, 24)) as pilot:
                await pilot.app.push_screen(screen)
                await pilot.pause()
                await pilot.press("a", "enter")
                await pilot.pause()
                self.assertIn('{"type": "input", "data": "a"}', socket.sent)
                self.assertIn('{"type": "input", "data": "\\r"}', socket.sent)
                screen.append_output("p")
                screen.append_output("w")
                screen.append_output("d\r\n/workspace\r\nsh$ ")
                await pilot.pause()
                lines = "\n".join(line.text for line in screen.query_one("#terminal-output").lines)
                self.assertIn("pwd", lines)
                self.assertIn("/workspace", lines)
                self.assertEqual(
                    screen.query_one("#terminal-current-line", Static).content.plain, "sh$ "
                )
                await pilot.press("ctrl+q")
                await pilot.pause()
                self.assertNotIsInstance(pilot.app.screen, TerminalScreen)
                self.assertTrue(screen.closed)
        self.assertEqual(stopped, ["terminal-1"])
        self.assertEqual(reports, ["终端 terminal-1 清理未确认，需人工核对工作区内进程"])

    async def test_child_task_state_is_separate_from_active_conversation(self):
        class Controller:
            task_status = "completed"

            def commands(self):
                return {"commands": []}

            def extension_commands(self):
                return {"commands": []}

            def skill_commands(self, tid):
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

            def extension_commands(self):
                return {"commands": []}

            def skill_commands(self, tid):
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
