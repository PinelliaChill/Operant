"""Keyboard Pilot of the real TUI resource entry against the isolated Core."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from operant_tui.app import OperantTui
from operant_tui.controller import ClientController
from operant_tui.conversation_screen import ConversationScreen
from operant_tui.resource_screen import ResourceScreen
from textual.widgets import Button, Collapsible, Select, Static, Tree


async def until(check, description: str) -> None:  # type: ignore[no-untyped-def]
    for _ in range(100):
        if check():
            return
        await asyncio.sleep(0.1)
    raise AssertionError(description)


async def exercise(evidence: Path) -> None:
    seed = json.loads((evidence / "seed.json").read_text())
    app = OperantTui(ClientController("http://127.0.0.1:18772"))
    async with app.run_test(size=(72, 36)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ConversationScreen)
        await until(lambda: screen.connected, "TUI did not connect")
        tree = screen.query_one("#conversation-tree", Tree)
        pending = [tree.root]
        node = None
        while pending:
            current = pending.pop()
            if current.data == seed["thread_id"]:
                node = current
                break
            pending.extend(current.children)
        assert node is not None
        tree.select_node(node)
        tree.focus()
        await pilot.press("enter")
        await until(lambda: screen.selected is not None, "TUI did not select the fixture")
        for group in screen.query(Collapsible):
            if "子 Agent" in group.title:
                group.collapsed = False
        await pilot.pause()
        button = screen.query_one("#conversation-resources", Button)
        assert not button.disabled
        button.focus()
        await pilot.press("enter")
        await until(lambda: isinstance(app.screen, ResourceScreen), "resource entry did not open")
        resources = app.screen
        assert isinstance(resources, ResourceScreen)
        await until(lambda: seed["pinned_id"] in resources.items, "Core resources did not load")
        assert resources.items[seed["cleanup_id"]]["state"] == "deleted"
        assert resources.items[seed["pinned_id"]]["pinned"] is True
        choice = resources.query_one("#resource-choice", Select)
        choice.value = seed["pinned_id"]
        await pilot.pause()
        resources.query_one("#resource-preview-button", Button).focus()
        await pilot.press("enter")
        await until(
            lambda: "eligible" in str(resources.query_one("#resource-preview", Static).render()),
            "protected cleanup preview did not return",
        )
        assert resources.query_one("#resource-cleanup", Button).disabled
        resources.query_one("#resource-cancel", Button).focus()
        await pilot.press("enter")
        assert resources.preview_id is None
        resources.query_one("#resource-uncompleted", Button).focus()
        await pilot.press("enter")
        await until(
            lambda: "completed_at" in str(resources.query_one("#resource-status", Static).render()),
            "completion revoke did not return",
        )
        final = await asyncio.to_thread(resources.controller.resources, seed["thread_id"])
        assert final["policy"]["completed_at"] is None
        assert final["policy"]["completed_ttl_seconds"] == 7200
        result = {
            "status": "passed",
            "width": 72,
            "resource_entry_keyboard": True,
            "core_inventory_readback": True,
            "pinned_cleanup_disabled": True,
            "preview_cancel_no_delete": True,
            "completion_revoke_core_readback": True,
            "resources": final,
        }
        app.save_screenshot(str(evidence / "tui-resources.svg"))
        (evidence / "tui-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    asyncio.run(exercise(parser.parse_args().evidence_dir))
