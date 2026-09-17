"""Actual generated HTTP client in a headless Textual application, not native GUI QA."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from operant_tui.app import OperantTui
from operant_tui.controller import ClientController
from operant_tui.experience import ExperienceScreen
from textual.widgets import Button, Input, Select


async def check(args: argparse.Namespace) -> dict:
    controller = ClientController(args.core_url)
    app = OperantTui(controller)
    async with app.run_test(size=(110, 45)) as pilot:
        await pilot.press("alt+4")
        screen = app.screen
        assert isinstance(screen, ExperienceScreen), "screen_missing"
        screen.query_one("#experience-project", Input).value = args.project_id
        await screen.refresh_state()
        assert screen.state and screen.state["project_id"] == args.project_id, "projection_missing"
        skill = screen.state["skills"]["skills"][0]
        screen.query_one("#experience-object", Select).value = "skill:" + skill["skill_id"]
        screen.query_one("#experience-action", Select).value = "skill_rollback"
        screen.query_one("#experience-version", Input).value = str(skill["rollback_versions"][0])
        screen.prepare()
        assert not screen.query_one("#experience-confirm", Button).disabled, "preparation_disabled"
        assert (
            screen.pending and screen.pending["skill_version"] == skill["head"]["published_version"]
        ), "version_mismatch"
        assert screen.pending["expected_head_revision"] == skill["head"]["head_revision"], (
            "head_mismatch"
        )
        await pilot.press("escape")
        assert not isinstance(app.screen, ExperienceScreen), "escape_failed"
        return {
            "completed": True,
            "entry": "OperantTui Alt+4 / generated B26Client over actual local HTTP",
            "kind": "headless_textual_live_client",
            "project_id": args.project_id,
            "skill_id": skill["skill_id"],
            "head_revision": skill["head"]["head_revision"],
            "checks": [
                "protocol_negotiation",
                "live_projection",
                "exact_command_preparation",
                "escape_return",
            ],
            "write_submitted": False,
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--core-url", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = asyncio.run(check(args))
    except Exception as exc:
        result = {"completed": False, "error_class": type(exc).__name__}
        if isinstance(exc, AssertionError):
            result["failure_code"] = str(exc)
    root = Path(__file__).resolve().parents[1]
    result["source_hashes"] = {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((root / "clients/tui/operant_tui").glob("*.py"))
    }
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in result.items() if k != "source_hashes"}))
    if not result["completed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
