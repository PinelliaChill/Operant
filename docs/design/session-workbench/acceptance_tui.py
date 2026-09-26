"""Pilot the real TUI against an already running, isolated Core.

Without action flags this reads parent/child conversations. --reference-file
creates a bounded snapshot Artifact; --send-message and --context-commands
perform real Core writes. Run it only after the GUI acceptance segment on the
frozen scratch database. Never point this script at a user database.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from operant_tui.app import OperantTui
from operant_tui.controller import ClientController
from operant_tui.conversation_screen import ConversationScreen
from textual.widgets import Button, Collapsible, Input, Static, Tree


def loopback_origin(value: str) -> str:
    parsed = urlsplit(value)
    try:
        is_loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
    except ValueError:
        is_loopback = False
    if (
        parsed.scheme != "http"
        or not is_loopback
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise ValueError("Core must be a plain HTTP loopback origin")
    return value.rstrip("/")


def temporary_output(raw: Path) -> Path:
    output = raw.expanduser().resolve(strict=False)
    temporary = Path(tempfile.gettempdir()).resolve()
    if output == temporary or temporary not in output.parents:
        raise ValueError("screenshots must be saved below the system temporary directory")
    return output


async def until_connected(screen: ConversationScreen) -> None:
    for _ in range(100):
        if screen.connected:
            return
        await asyncio.sleep(0.1)
    raise RuntimeError("TUI did not connect to the acceptance Core")


async def until(predicate, description: str, *, seconds: float = 30) -> None:
    for _ in range(int(seconds * 5)):
        if predicate():
            return
        await asyncio.sleep(0.2)
    raise AssertionError(f"TUI did not confirm {description}")


async def until_context_operation(
    screen: ConversationScreen, thread_id: str, operation: str
) -> dict:
    for _ in range(150):
        context = await asyncio.to_thread(screen.controller.context, thread_id)
        if context.get("baseline_operation") == operation:
            return context
        await asyncio.sleep(0.2)
    raise AssertionError(f"Core did not record {operation} context baseline")


async def activate(pilot: Any, screen: ConversationScreen, selector: str) -> None:
    """Use the same keyboard path a user can take when a control is scrolled away."""
    button = screen.query_one(selector, Button)
    if button.disabled:
        raise AssertionError(f"TUI control is disabled: {selector}")
    button.focus()
    await pilot.press("enter")


def find_node(tree: Tree, thread_id: str):
    pending = [tree.root]
    while pending:
        node = pending.pop()
        if node.data == thread_id:
            return node
        pending.extend(node.children)
    raise AssertionError(f"conversation {thread_id} is absent from the TUI tree")


async def exercise(
    core_url: str,
    parent_id: str,
    child_id: str,
    screenshot_dir: Path,
    reference_file: str | None,
    send_message: str | None,
    context_commands: bool,
) -> None:
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, object] = {"parent": parent_id, "child": child_id}
    app = OperantTui(ClientController(core_url))
    async with app.run_test(size=(150, 48)) as pilot:
        await pilot.pause()
        screen = app.screen
        if not isinstance(screen, ConversationScreen):
            raise AssertionError("the real TUI did not open its conversation screen")
        await until_connected(screen)
        tree = screen.query_one("#conversation-tree", Tree)
        parent = find_node(tree, parent_id)
        child = find_node(tree, child_id)
        if child.parent is not parent:
            raise AssertionError("child is not nested below its parent in the TUI")
        parent.expand()
        tree.select_node(parent)
        await pilot.pause()
        if screen.selected is None or screen.selected["id"] != parent_id:
            raise AssertionError("parent conversation could not be opened")
        app.save_screenshot(filename="parent-wide.svg", path=str(screenshot_dir))

        tree.select_node(child)
        await pilot.pause()
        if screen.selected is None or screen.selected["id"] != child_id:
            raise AssertionError("child conversation could not be opened")
        result["nested_tree"] = True

        app.save_screenshot(filename="child-wide.svg", path=str(screenshot_dir))
        await pilot.resize_terminal(82, 44)
        await pilot.pause()
        app.save_screenshot(filename="child-narrow.svg", path=str(screenshot_dir))

        await pilot.resize_terminal(150, 48)
        tree.select_node(parent)
        await pilot.pause()
        if screen.selected is None or screen.selected["id"] != parent_id:
            raise AssertionError("parent conversation could not be reopened")

        details = next(widget for widget in screen.query(Collapsible) if "子 Agent" in widget.title)
        details.collapsed = False
        await activate(pilot, screen, "#conversation-context")
        await until(
            lambda: bool(str(screen.query_one("#conversation-details", Static).content).strip()),
            "visible context details",
        )
        context_text = str(screen.query_one("#conversation-details", Static).content)
        if not context_text.strip():
            raise AssertionError("context control returned no visible detail")
        result["context_visible"] = True

        if reference_file is not None:
            field = screen.query_one("#conversation-reference", Input)
            field.focus()
            await pilot.press(*reference_file)
            await activate(pilot, screen, "#conversation-reference-add")
            await pilot.pause()
            if not screen.references:
                raise AssertionError("reference preview was not attached in the TUI")
            result["reference_attached"] = True

        if send_message is not None:
            history_before_send = await asyncio.to_thread(
                screen.controller.history, screen.selected
            )
            before_ids = {item["id"] for item in history_before_send["items"]}
            field = screen.query_one("#conversation-message", Input)
            field.value = send_message
            await activate(pilot, screen, "#conversation-send")

            def sent() -> bool:
                if not screen.connected:
                    raise AssertionError("TUI disconnected during model run; inspect Core state")
                return parent_id not in screen.running and not field.value.strip()

            await until(
                sent,
                "a completed TUI model run",
                seconds=130,
            )
            history = await asyncio.to_thread(screen.controller.history, screen.selected)
            added = [item for item in history["items"] if item["id"] not in before_ids]
            if not any(
                item["payload"].get("type") == "user_message"
                and item["payload"].get("text") == send_message
                for item in added
            ) or not any(item["payload"].get("type") == "agent_message" for item in added):
                raise AssertionError("TUI send lacks new persisted user and Agent messages")
            if "已完成" not in str(screen.query_one("#conversation-status", Static).content):
                raise AssertionError("TUI did not show a completed model terminal state")
            result["real_tui_send"] = True
            result["new_item_count"] = len(added)

        if context_commands:
            history_before = await asyncio.to_thread(screen.controller.history, screen.selected)
            baseline_before = (await asyncio.to_thread(screen.controller.context, parent_id)).get(
                "baseline_operation"
            )
            await activate(pilot, screen, "#conversation-compact")
            await until_context_operation(screen, parent_id, "compact")
            await activate(pilot, screen, "#conversation-clear")
            if screen.pending_clear != parent_id:
                raise AssertionError("TUI clear skipped its explicit confirmation")
            screen.query_one("#conversation-message", Input).focus()
            await pilot.pause()
            await activate(pilot, screen, "#conversation-clear")
            await until_context_operation(screen, parent_id, "clear")
            history_after = await asyncio.to_thread(screen.controller.history, screen.selected)
            before_command_ids = {item["id"] for item in history_before["items"]}
            after_command_ids = {item["id"] for item in history_after["items"]}
            if not before_command_ids.issubset(after_command_ids):
                raise AssertionError("TUI context commands removed canonical history")
            if baseline_before == "clear":
                result["baseline_was_already_clear"] = True
            result["compact_and_clear"] = True

        await activate(pilot, screen, "#conversation-context")
        screen.query_one("#conversation-details", Static).scroll_visible(immediate=True)
        await pilot.pause()
        app.save_screenshot(filename="parent-actions-wide.svg", path=str(screenshot_dir))
    print(json.dumps(result, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-url", required=True)
    parser.add_argument("--parent-id", required=True)
    parser.add_argument("--child-id", required=True)
    parser.add_argument("--screenshot-dir", type=Path, required=True)
    parser.add_argument("--reference-file", help="relative file path in the parent workspace")
    parser.add_argument("--send-message", help="send a real model request from the parent TUI")
    parser.add_argument("--context-commands", action="store_true")
    args = parser.parse_args()
    asyncio.run(
        exercise(
            loopback_origin(args.core_url),
            args.parent_id,
            args.child_id,
            temporary_output(args.screenshot_dir),
            args.reference_file,
            args.send_message,
            args.context_commands,
        )
    )


if __name__ == "__main__":
    main()
