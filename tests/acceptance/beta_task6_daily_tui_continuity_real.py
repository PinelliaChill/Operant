"""TUI controller continuity through a live Core and a real model.

Uses a fresh synthetic workspace and database. A mounted Textual skill-command
pilot is covered by beta_task4_skill_tui_pilot.py; this checks the generated
client path shared by the mounted screen without duplicating that pilot.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

from operant.api import create_app
from operant.application.default_skill_pack import DEFAULT_SKILL_PACK, install_default_skill_pack
from operant.application.security import PolicyEngine
from operant.contracts.b2_3 import ManagementCommand
from operant.domain.models import Budget, Effort, ModelProfile, RolePreset, ToolPolicy
from operant.domain.security import PolicyBundle, PolicyDecision, PolicyLayer, PolicyRule
from operant.settings import load_local_env
from tests.acceptance.beta_task4_local_control_real import running_core

MODEL = "gpt-6-luna"


def _policy() -> PolicyEngine:
    return PolicyEngine(
        PolicyBundle(
            bundle_id="task6-tui-continuity",
            version="test.v1",
            default_decision=PolicyDecision.DENY,
            rules=(
                PolicyRule(
                    rule_id="isolated-read",
                    layer=PolicyLayer.SYSTEM,
                    decision=PolicyDecision.ALLOW,
                    reason="isolated synthetic workspace",
                ),
            ),
        )
    )


async def main(root: Path, env_file: Path | None) -> None:
    from operant_tui.controller import ClientController
    from operant_tui.conversation import ConversationController

    if not root.is_absolute():
        raise ValueError("absolute evidence directory required")
    root.mkdir(parents=True, exist_ok=False)
    workspace = root / "workspace"
    workspace.mkdir()
    (workspace / "brief.txt").write_text(
        "The daily brief is for new teammates. Cite only confirmed facts.\n",
        encoding="utf-8",
    )
    if env_file:
        load_local_env(env_file)
    if not os.environ.get("OPERANT_API_KEY"):
        raise RuntimeError("OPERANT_API_KEY is unavailable")
    result: dict[str, Any] = {
        "status": "failed",
        "entry": "ConversationController -> generated clients -> live HTTP Core",
        "model_id": MODEL,
        "synthetic_workspace": str(workspace),
    }
    app = create_app(
        root / "core.sqlite3",
        artifact_root=root / "artifacts",
        phase45_policy_engine=_policy(),
        phase56_local_authorizer=lambda _request: True,
    )
    service = app.state.operant_service
    manager = service.memory_manager_factory()
    try:
        discovered = await service.discover_models(
            base_url=os.environ["OPERANT_BASE_URL"], secret_ref="OPERANT_API_KEY"
        )
        if MODEL not in discovered:
            raise AssertionError("formal Discovery omitted exact gpt-6-luna")
        result["discovery_exact_match"] = True
        project = await manager.execute(
            ManagementCommand(
                action="project_create", name="task6-tui-continuity", workspace_path=str(workspace)
            )
        )
        project_id = project.state.projects[-1].project_id
        skill_ids = await install_default_skill_pack(manager, project_id=project_id)
        skill_id_by_name = {
            entry.skill_name: skill_id
            for entry, skill_id in zip(DEFAULT_SKILL_PACK, skill_ids, strict=True)
        }
        initialization, _ = service.initialize_workspace(workspace)
        profile = service.add_model_profile(
            ModelProfile(
                name="task6-tui-continuity",
                model_id=MODEL,
                base_url=os.environ["OPERANT_BASE_URL"],
                secret_ref="OPERANT_API_KEY",
                supported_efforts=(Effort.LOW,),
                default_effort=Effort.LOW,
                effort_parameter=None,
            )
        )
        role = service.create_role(
            RolePreset(
                name="task6-tui-continuity",
                system_prompt="Answer briefly using confirmed facts. Do not call tools.",
                model_profile_id=profile.id,
                effort=Effort.LOW,
                tool_policy=ToolPolicy(allowed_tools=("read_file",)),
                budget=Budget(max_turns=2, max_output_tokens=350, timeout_seconds=120),
                memory_scope="none",
            )
        )
        with running_core(app) as origin:
            controller = ConversationController(ClientController(origin))
            created = controller.create(initialization.id, role.id, key="tui-continuity-create")
            threads = controller.core.phase1e.list_threads(workspace_ref=str(workspace))
            thread = next(item for item in threads if item["id"] == created["thread_id"])
            if not any(
                item.get("source_type") == "session"
                and item.get("source_id") == created["session_id"]
                for item in thread.get("legacy_refs", [])
            ):
                raise AssertionError("TUI-created Session missing from Thread projection")
            events = list(
                controller.run(
                    thread,
                    "Who is the daily brief for? Reply in one sentence.",
                    [],
                    key="tui-continuity-ordinary",
                )
            )
            completed = [frame for frame in events if frame.get("event") == "agent.completed"]
            if len(completed) != 1:
                raise AssertionError("TUI ordinary Session did not complete")
            history = controller.history(thread)
            if not history["items"]:
                raise AssertionError("TUI history did not show committed result")
            result["ordinary_session"] = {
                "completed": True,
                "history_items": len(history["items"]),
                "stream_events": len(events),
            }

            registry = controller.skill_commands(thread["id"])
            commands = {item["skill_id"]: item["command"] for item in registry["commands"]}
            if set(commands) != set(skill_ids):
                raise AssertionError("TUI did not discover all six enabled bundled Skill commands")
            grill_id = skill_id_by_name["grill-me"]
            skill_run = controller.skill_command(
                thread["id"],
                commands[grill_id],
                {
                    "prompt": (
                        "Grill my daily brief plan. Ask one concrete question about the "
                        "missing success criterion. Do not answer it for me."
                    )
                },
                key="tui-continuity-skill",
            )
            if skill_run["status"] != "completed" or not any(
                mark in skill_run["result"] for mark in ("?", "？")
            ):
                raise AssertionError("TUI explicit grill-me Skill did not return a question")
            result["skill_command"] = {
                "discovered_count": len(commands),
                "grill_me_completed": True,
                "question_present": True,
            }

            reference = controller.reference(
                thread["id"], "file", "brief.txt", key="tui-continuity-reference"
            )
            if reference["source"] != "file:brief.txt":
                raise AssertionError("TUI file reference snapshot has wrong source")
            referenced_events = list(
                controller.run(
                    thread,
                    "Using the attached brief, who is the audience?",
                    [reference["reference"]],
                    key="tui-continuity-referenced-run",
                )
            )
            if not any(frame.get("event") == "agent.completed" for frame in referenced_events):
                raise AssertionError("TUI referenced Session did not complete")
            context = controller.context(thread["id"])
            if not any(
                item.get("resolved_target") == reference["reference"]["target_id"]
                for item in context.get("references", [])
            ):
                raise AssertionError("TUI attached reference absent from formal context revision")
            result["reference"] = {
                "created": True,
                "attached_run_completed": True,
                "bound_in_context_revision": True,
            }
            result["status"] = "passed"
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        raise
    finally:
        (root / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        await manager.close()
        service.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    asyncio.run(main(args.evidence_dir, args.env_file))
