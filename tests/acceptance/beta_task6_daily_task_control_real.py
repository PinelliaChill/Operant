"""H-08/H-12 real daily Goal, Plan, BTW and frozen configuration acceptance."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from operant.api import create_app
from operant.application.configuration import ConfigService
from operant.application.plan_generation import generate_plan_draft
from operant.application.task_control import TaskControlService
from operant.domain.models import Budget, Effort, ModelProfile, RolePreset, ToolPolicy
from operant.domain.task_control import Goal, PlanStatus
from operant.domain.threads import ConversationThread
from operant.settings import load_local_env

MODEL = "gpt-6-luna"


async def main(root: Path, env_file: Path | None) -> None:
    if not root.is_absolute():
        raise ValueError("absolute evidence directory required")
    root.mkdir(parents=True, exist_ok=False)
    workspace = root / "workspace"
    workspace.mkdir()
    (workspace / "brief.txt").write_text(
        "Daily brief: report only observed facts. No file changes are needed.\n",
        encoding="utf-8",
    )
    if env_file:
        load_local_env(env_file)
    result: dict[str, object] = {
        "status": "failed",
        "model_id": MODEL,
        "entry": "ApplicationService and TaskControlService formal runtime",
        "synthetic_workspace": str(workspace),
    }
    app = create_app(root / "core.sqlite3", artifact_root=root / "artifacts")
    service = app.state.operant_service
    try:
        available = await service.discover_models(
            base_url=os.environ["OPERANT_BASE_URL"], secret_ref="OPERANT_API_KEY"
        )
        if MODEL not in available:
            raise RuntimeError("exact gpt-6-luna unavailable from Discovery")
        result["discovery_exact_match"] = True
        profile = service.add_model_profile(
            ModelProfile(
                name="Task6 daily control",
                model_id=MODEL,
                base_url=os.environ["OPERANT_BASE_URL"],
                secret_ref="OPERANT_API_KEY",
                supported_efforts=(Effort.LOW,),
                default_effort=Effort.LOW,
                effort_parameter=None,
                context_window=32768,
            )
        )
        source_role = service.create_role(
            RolePreset(
                name="Task6 daily source",
                model_profile_id=profile.id,
                effort=Effort.LOW,
                system_prompt="Answer the daily Goal using confirmed facts only.",
                tool_policy=ToolPolicy(),
                budget=Budget(max_turns=3, max_output_tokens=500, timeout_seconds=120),
            )
        )
        planner_role = service.create_role(
            RolePreset(
                id="role_planner",
                name="Task6 daily planner",
                model_profile_id=profile.id,
                effort=Effort.LOW,
                system_prompt="Produce concise JSON plans without claiming execution.",
                tool_policy=ToolPolicy(allowed_tools=("read_file", "search_files", "git_diff")),
                budget=Budget(max_turns=2, max_output_tokens=1200, timeout_seconds=120),
            )
        )
        config = ConfigService(service.store)
        thread = service.create_thread(ConversationThread(workspace_ref=str(workspace.resolve())))
        before = service.create_session(source_role.id, thread_id=thread.id)
        original_prompt = before.role_snapshot.system_prompt
        configured = config.put_scope(
            "global",
            "default",
            patch={
                "system_prompt": (
                    "Daily criterion: mention evidence and distinguish draft from done."
                ),
                "budget": {"max_turns": 2},
            },
            expected_revision=0,
        )
        new_thread = service.create_thread(
            ConversationThread(workspace_ref=str(workspace.resolve()))
        )
        source = service.create_session(source_role.id, thread_id=new_thread.id)
        effective = config.effective(source_role, workspace_ref=str(workspace.resolve()))
        if (
            "Daily criterion" in original_prompt
            or "Daily criterion" in before.role_snapshot.system_prompt
            or "Daily criterion" not in source.role_snapshot.system_prompt
            or source.role_snapshot.budget.max_turns != 2
            or effective.sources["system_prompt"].scope_type != "global"
        ):
            raise AssertionError("H-08 frozen configuration inheritance failed")
        result["h08"] = {
            "scope_revision": configured.revision,
            "old_snapshot_frozen": True,
            "new_snapshot_inherited": True,
            "effective_source": effective.sources["system_prompt"].scope_type,
        }
        control = TaskControlService(service.store)
        goal = control.create_goal(
            Goal(
                owner_thread_id=new_thread.id,
                objective="Create a daily plan to summarize brief.txt and check facts.",
                completion_criteria=("Plan is reviewed before execution",),
                token_budget=900,
                time_budget_seconds=90,
            )
        )
        draft = await generate_plan_draft(
            service,
            goal_id=goal.id,
            source_session_id=source.id,
            thread_id=new_thread.id,
            workspace=workspace,
            planner_role_id=planner_role.id,
        )
        if (
            draft.plan.status is not PlanStatus.DRAFT
            or draft.plan.source_mode != "read_only"
            or not draft.plan.created_from_context_revision
            or control.get_plan(draft.plan.id).id != draft.plan.id
        ):
            raise AssertionError("H-12 real Plan draft was not durable and read-only")
        result["plan"] = {
            "status": draft.plan.status.value,
            "source_mode": draft.plan.source_mode,
            "planner_session_id": draft.planner_session_id,
            "context_revision_present": True,
            "proposed_changes": len(draft.plan.proposed_changes),
        }
        sidecar_id: str | None = None
        sidecar_events = []
        async for event in service.run_btw_sidecar(
            session_id=source.id,
            thread_id=new_thread.id,
            workspace=workspace,
            prompt="Read-only BTW: what should the user verify before approving this daily Plan?",
        ):
            sidecar_id = event.sidecar_run_id
            sidecar_events.append(event.event_type)
        if sidecar_id is None:
            raise AssertionError("BTW did not start")
        sidecar = service.get_btw_sidecar_run(sidecar_id)
        if sidecar.status.value != "completed" or not sidecar.response:
            raise AssertionError("BTW did not complete with a real model response")
        promoted, turn, item = service.promote_btw_sidecar(sidecar_id)
        if promoted.status.value != "promoted" or turn.thread_id != new_thread.id:
            raise AssertionError("BTW explicit promotion did not persist")
        result["btw"] = {
            "completed_before_promotion": True,
            "promoted_status": promoted.status.value,
            "item_id": item.id,
            "event_types": sidecar_events,
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
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    asyncio.run(main(args.evidence_dir, args.env_file))
