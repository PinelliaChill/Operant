"""Bounded preflight through an installed plugin and formal Session; synthetic data."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from pathlib import Path

from b24_provider_diagnostics import attach_provider_diagnostics

from operant.settings import load_local_env


async def main(
    *,
    output: Path | None = None,
    phase: str = "preflight_not_final_acceptance",
    model_id: str = "gpt-oss-20b",
) -> None:
    root = Path(__file__).resolve().parents[1]
    runroot = Path(tempfile.mkdtemp(prefix="operant-b24-model-")).resolve()
    load_local_env("/Users/bigo/agentworkspace/codexworkspace/operant/.env")
    os.environ["OPERANT_DB_PATH"] = str(runroot / "core.sqlite3")
    from operant.api import create_app
    from operant.contracts.b2_3 import ManagementCommand
    from operant.domain.models import Budget, ModelProfile, RolePreset, ToolPolicy
    from operant.domain.threads import ConversationThread
    from operant.memory_plugins.manager import MemoryManager

    app = create_app(runroot / "core.sqlite3")
    service = app.state.operant_service
    provider_errors = attach_provider_diagnostics(service.provider)
    manager = MemoryManager(service)
    service.memory_manager = manager

    async def cmd(**kwargs):
        return await manager.execute(ManagementCommand(**kwargs))

    project = (
        (
            await cmd(
                action="project_create", name="B24 synthetic preflight", workspace_path=str(runroot)
            )
        )
        .state.projects[-1]
        .project_id
    )
    installation = (
        (await cmd(action="plugin_install", plugin_id="memory-standard", mode="trusted_in_process"))
        .state.installations[-1]
        .installation_id
    )
    await cmd(action="binding_select", project_id=project, installation_id=installation)
    await cmd(
        action="memory_save",
        project_id=project,
        content="B24 验收项目的测试代号为 RIVER_42。仅适用于本临时测试项目。",
        confirmed=True,
    )
    profile = service.add_model_profile(
        ModelProfile(
            name="B24 preflight discovered model",
            model_id=model_id,
            base_url=os.environ["OPERANT_BASE_URL"],
            secret_ref="OPERANT_API_KEY",
            context_window=32768,
        )
    )
    role = service.create_role(
        RolePreset(
            name="B24 read-only",
            system_prompt=(
                "Use authorized memory evidence and read_file if asked. "
                "Reply briefly. Never write files."
            ),
            model_profile_id=profile.id,
            memory_scope="read: [project]; write: []",
            budget=Budget(max_turns=3, max_output_tokens=2048, timeout_seconds=90),
            tool_policy=ToolPolicy(allowed_tools=("read_file",)),
        )
    )
    thread = service.create_thread(ConversationThread(workspace_ref=str(runroot)))
    session = service.create_session(role.id, thread_id=thread.id)
    (runroot / "probe.txt").write_text("The independent tool check is 6 times 7.\n")
    result = {
        "phase": phase,
        "provider_errors": provider_errors,
        "model_id": profile.model_id,
        "workspace": str(runroot),
        "database": str(runroot / "core.sqlite3"),
        "session_id": session.id,
        "thread_id": thread.id,
        "entry": "ApplicationService.run_session",
        "tool_policy": ["read_file"],
        "output_budget": 2048,
        "events": [],
    }
    try:
        events = [
            e
            async for e in service.run_session(
                session.id,
                user_message=(
                    "检索 B24 测试代号，并用 read_file 读取 probe.txt，最后给出测试代号和算式结果。"
                ),
                workspace=str(runroot),
                thread_id=thread.id,
            )
        ]
        result["events"] = [e.event_type for e in events]
        result["failures"] = [
            {"event": e.event_type, "error_class": e.payload.get("error_type")}
            for e in events
            if e.event_type == "agent.failed"
        ]
        result["answer"] = next(
            (
                e.payload.get("content")
                for e in reversed(events)
                if e.event_type == "agent.completed"
            ),
            None,
        )
        revisions = service.store.list_context_revisions(session.id)
        result["context_revision_ids"] = [r.id for r in revisions]
        result["memory_sent"] = any(
            "RIVER_42" in (m.content or "") for r in revisions for m in r.messages
        )
        result["completed"] = any(e.event_type == "agent.completed" for e in events)
        result["tool_called"] = any(e.event_type == "tool.completed" for e in events)
    except Exception as e:
        result["error_class"] = type(e).__name__
    finally:
        await manager.close()
        service.close()
    (output or root / "docs/design/b2-4/model-preflight.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2)
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--phase", default="preflight_not_final_acceptance")
    parser.add_argument("--model-id", default="gpt-oss-20b")
    args = parser.parse_args()
    asyncio.run(main(output=args.output, phase=args.phase, model_id=args.model_id))
