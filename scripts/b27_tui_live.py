"""Headless Textual actions against a real isolated Core, never a user database."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

import httpx
from operant_tui.app import OperantTui
from operant_tui.controller import ClientController
from operant_tui.experience import ExperienceScreen
from textual.widgets import Button, Input, Select


def seed(core_url: str, project: str) -> str:
    with httpx.Client(base_url=core_url, trust_env=False, timeout=20) as client:
        serial = 0

        def command(version: str = "b2-6", **body):
            nonlocal serial
            serial += 1
            response = client.post(
                f"/v1/{version}/commands",
                json={"project_id": project, **body},
                headers={"Idempotency-Key": f"b27-tui-seed-{project}-{serial}"},
            )
            assert response.status_code == 200, (body["action"], response.status_code)
            return response.json()

        state = client.get(f"/v1/b2-6/projects/{project}/experience").json()
        records = client.get(f"/v1/b2-5/projects/{project}/governance").json()["records"]
        source = records[0]["version"]
        result = command(
            action="procedure_propose",
            content="1. 核对 B27 合成工作区。\n2. 读取 probe.txt 并核对合成标记。",
            sources=[
                {
                    "source_type": "memory_version",
                    "source_id": source["ref"]["record_id"],
                    "revision": source["ref"]["version"],
                    "content_digest": source["ref"]["content_digest"],
                    "scope": source["scope"],
                    "permission_epoch": state["remote"]["permission_epoch"],
                    "availability": "available",
                }
            ],
        )
        proposals = client.get(f"/v1/b2-5/projects/{project}/governance").json()["proposals"]
        entry = next(p for p in proposals if p["proposal"]["proposal_id"] in result["affected_ids"])
        proposal = entry["proposal"]
        command(
            "b2-5",
            action="review",
            decision="accept",
            selections=[
                {
                    "proposal_id": proposal["proposal_id"],
                    "proposal_revision": proposal["proposal_revision"],
                    "proposed_version": proposal["proposed_version"],
                    "base_head_revision": proposal["base_head"]["revision"],
                }
            ],
        )
        result = command(
            action="skill_draft",
            procedure_ref=entry["version"]["ref"],
            name="b27-tui-check",
            description="B27 合成客户端兼容验证",
        )
        return result["state"]["skills"]["skills"][-1]["skill_id"]


async def check(args: argparse.Namespace) -> dict:
    skill_id = seed(args.core_url, args.project_id)
    controller = ClientController(args.core_url)
    app = OperantTui(controller)
    steps = []
    async with app.run_test(size=(110, 45)) as pilot:
        await pilot.press("alt+4")
        screen = app.screen
        assert isinstance(screen, ExperienceScreen)
        screen.query_one("#experience-project", Input).value = args.project_id
        await screen.refresh_state()
        for action, expected_state in (
            ("skill_validate", "draft"),
            ("skill_publish", "published"),
            ("skill_disable", "disabled"),
            ("skill_rollback", "published"),
        ):
            screen.query_one("#experience-object", Select).value = "skill:" + skill_id
            screen.query_one("#experience-action", Select).value = action
            screen.query_one("#experience-version", Input).value = "1"
            screen.prepare()
            assert screen.pending and not screen.query_one("#experience-confirm", Button).disabled
            exact = dict(screen.pending)
            await screen.confirm()
            assert screen.state
            item = next(s for s in screen.state["skills"]["skills"] if s["skill_id"] == skill_id)
            assert item["head"]["state"] == expected_state
            steps.append({"command": exact, "head": item["head"]})
        # Real failed connection, not a fake controller response.
        screen.controller = ClientController("http://127.0.0.1:1")
        await screen.refresh_state()
        assert screen.query_one("#experience-prepare", Button).disabled
        assert screen.query_one("#experience-confirm", Button).disabled
        assert screen.state and screen.state["project_id"] == args.project_id
        await pilot.press("escape")
        assert not isinstance(app.screen, ExperienceScreen)
    return {
        "completed": True,
        "kind": "headless Textual / real local HTTP / installed generated SDK",
        "native_desktop": False,
        "project_id": args.project_id,
        "skill_id": skill_id,
        "steps": steps,
        "checks": ["Alt+4", "exact_object_commands", "disconnect_readonly", "Escape"],
        "model_called": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--core-url", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = asyncio.run(check(args))
    except Exception as exc:
        result = {"completed": False, "error_class": type(exc).__name__}
        if isinstance(exc, AssertionError):
            result["failure"] = str(exc)
    root = Path(__file__).resolve().parents[1]
    result["source_hashes"] = {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (root / "clients/tui/operant_tui").glob("*.py")
    }
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "source_hashes"}, ensure_ascii=False))
    if not result["completed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
