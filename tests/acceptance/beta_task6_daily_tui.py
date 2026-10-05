"""H-08/H-12 TUI controller -> generated Phase3 client -> live isolated Core."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

from operant.api import create_app
from operant.domain.models import ModelProfile, RolePreset
from operant.domain.threads import ConversationThread
from tests.acceptance.beta_task4_local_control_real import running_core


def main(root: Path) -> None:
    from operant_tui.conversation import ConversationController

    if not root.is_absolute():
        raise ValueError("absolute evidence directory required")
    root.mkdir(parents=True, exist_ok=False)
    workspace = root / "workspace"
    workspace.mkdir()
    result: dict[str, object] = {
        "status": "failed",
        "entry": "ConversationController -> generated Phase3Client -> live loopback Core",
        "synthetic_workspace": str(workspace),
    }
    app = create_app(root / "core.sqlite3", artifact_root=root / "artifacts")
    service = app.state.operant_service
    try:
        profile = service.add_model_profile(
            ModelProfile(
                name="TUI daily control no model call",
                model_id="not-used",
                base_url="https://example.invalid/v1",
                secret_ref="UNUSED_TUI_DAILY_KEY",
            )
        )
        role = service.create_role(
            RolePreset(
                name="TUI daily control",
                model_profile_id=profile.id,
                system_prompt="Original role prompt.",
            )
        )
        thread = service.create_thread(ConversationThread(workspace_ref=str(workspace)))
        with running_core(app) as origin:
            controller = ConversationController(SimpleNamespace(core_url=origin))
            before = controller.effective_config(role.id, None, str(workspace))
            scope = before["scopes"]["global"]
            updated = controller.save_config(
                scope,
                '{"system_prompt":"TUI daily criterion: use evidence."}',
                key="tui-config-1",
            )
            after = controller.effective_config(role.id, None, str(workspace))
            if updated["revision"] != scope["revision"] + 1:
                raise AssertionError("TUI config revision did not advance")
            if "TUI daily criterion" not in after["values"]["system_prompt"]:
                raise AssertionError("TUI effective config did not read back")
            goal = controller.create_goal(
                thread.id, "Prepare daily brief", ["Facts checked"], 500, key="tui-goal-1"
            )
            goals = controller.goals(thread.id)
            plan = controller.create_plan(
                goal["id"], "Read source and draft summary", key="tui-plan-1"
            )
            plan = controller.update_plan(
                plan, {"verification_plan": ["Check source file"]}, key="tui-plan-2"
            )
            plans = controller.plans(goal["id"])
            if (
                len(goals) != 1
                or goals[0]["id"] != goal["id"]
                or len(plans) != 1
                or plans[0]["revision"] != 2
            ):
                raise AssertionError("TUI Goal/Plan write-read sequence failed")
            result["config"] = {
                "revision": updated["revision"],
                "effective_source": after["sources"]["system_prompt"]["scope_type"],
            }
            result["goal_plan"] = {
                "goal_status": goals[0]["status"],
                "plan_status": plans[0]["status"],
                "plan_revision": plans[0]["revision"],
                "verification_plan": plans[0]["verification_plan"],
            }
            result["status"] = "passed"
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        raise
    finally:
        (root / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        service.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", required=True, type=Path)
    main(parser.parse_args().evidence_dir)
