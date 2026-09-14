"""Synthetic, bounded real Graph/Team model preflight; no user database."""

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
    runroot = Path(tempfile.mkdtemp(prefix="operant-b24-graph-")).resolve()
    load_local_env("/Users/bigo/agentworkspace/codexworkspace/operant/.env")
    os.environ["OPERANT_DB_PATH"] = str(runroot / "core.sqlite3")
    from operant.api import create_app
    from operant.application.team import TeamRuntime
    from operant.contracts.b2_3 import ManagementCommand
    from operant.domain.graph import (
        NodeKind,
        NodeSpec,
        WorkflowDefinition,
        WorkflowDefinitionStatus,
    )
    from operant.domain.models import Budget, ModelProfile, RolePreset, ToolPolicy
    from operant.domain.team import (
        MessageAudience,
        MessageEnvelope,
        MessageKind,
        TeamDefinition,
        TeamMember,
    )
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
                action="project_create", name="B24 joint preflight", workspace_path=str(runroot)
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
        content="B24 联合测试项目的测试代号是 CEDAR_29，仅适用于此临时项目。",
        confirmed=True,
    )
    profile = service.add_model_profile(
        ModelProfile(
            name="B24 graph discovered model",
            model_id=model_id,
            base_url=os.environ["OPERANT_BASE_URL"],
            secret_ref="OPERANT_API_KEY",
            context_window=32768,
        )
    )
    roles = [
        service.create_role(
            RolePreset(
                name=f"B24 member {i}",
                system_prompt=(
                    "Use authorized memory and read_file. Reply briefly; never write files."
                ),
                model_profile_id=profile.id,
                memory_scope="read: [project]; write: []",
                budget=Budget(max_turns=3, max_output_tokens=2048, timeout_seconds=90),
                tool_policy=ToolPolicy(allowed_tools=("read_file",)),
            )
        )
        for i in range(2)
    ]
    team = TeamDefinition(
        members=tuple(
            TeamMember(
                member_id=f"member_{i}",
                agent_definition_id=role.id,
                role=role.name,
                can_coordinate=i == 0,
            )
            for i, role in enumerate(roles)
        ),
        default_coordinator="member_0",
    )
    app.state.team_repository.put_team_definition(team)
    definition = WorkflowDefinition(
        name="B24 J2 two Agent preflight",
        status=WorkflowDefinitionStatus.PUBLISHED,
        nodes=tuple(
            NodeSpec(
                node_id=f"member_{i}",
                node_kind=NodeKind.AGENT,
                metadata={
                    "role_id": role.id,
                    "role_version": role.version,
                    "task": (
                        "检索 B24 联合测试代号，read_file 读取 probe.txt，然后回答代号和算式结果。"
                    ),
                },
            )
            for i, role in enumerate(roles)
        ),
        default_policy={
            "b24_executor": True,
            "team_id": team.team_id,
            "team_version": team.version,
        },
        locked_role_versions={r.id: r.version for r in roles},
        default_budget=Budget(max_turns=6, max_output_tokens=4096, timeout_seconds=120),
    )
    app.state.graph_repository.put_definition(definition)
    run = app.state.graph_runtime.create_run(definition, workspace_or_target=str(runroot))
    teamrun = app.state.b24_graph_executor.prepare(run.id)
    app.state.b24_freeze_graph_memory(run.id)
    roster = app.state.team_repository.list_roster(teamrun.team_run_id)
    (runroot / "probe.txt").write_text("The independent check is 6 times 7.\n")
    teamrt = TeamRuntime(app.state.team_repository)
    teamrt.send_message(
        MessageEnvelope(
            workflow_run_id=run.id,
            team_run_id=teamrun.team_run_id,
            sender_id=roster[1].agent_instance_id,
            recipient_ids=(roster[0].agent_instance_id,),
            audience=MessageAudience.DIRECT,
            message_kind=MessageKind.OBSERVATION,
            payload={"text": "PRIVATE_ONE_CODE is visible only to the first member."},
        ),
        idempotency_key="b24-private-message",
    )
    result = {
        "phase": phase,
        "provider_errors": provider_errors,
        "model_id": profile.model_id,
        "entry": "BoundedGraphExecutor.run -> ApplicationService.run_session",
        "workspace": str(runroot),
        "database": str(runroot / "core.sqlite3"),
        "graph_run_id": run.id,
        "team_run_id": teamrun.team_run_id,
        "output_budget": 4096,
        "tool_policy": ["read_file"],
    }
    try:
        outcome = await app.state.b24_graph_executor.run(run.id)
        result["status"] = outcome.status.value
        result["output_tokens"] = outcome.run.consumed_output_tokens
        result["members"] = []
        for member in roster:
            agent = service.store.get_agent(member.agent_instance_id)
            revisions = service.store.list_context_revisions(agent.session_id)
            text = "\n".join(m.content or "" for r in revisions for m in r.messages)
            result["members"].append(
                {
                    "member_id": member.member_id,
                    "agent_id": agent.id,
                    "session_id": agent.session_id,
                    "status": agent.status.value,
                    "contexts": [r.id for r in revisions],
                    "memory_sent": "CEDAR_29" in text,
                    "private_message_sent": "PRIVATE_ONE_CODE" in text,
                }
            )
        result["private_isolation"] = (
            result["members"][0]["private_message_sent"]
            and not result["members"][1]["private_message_sent"]
        )
        result["same_chain"] = all(
            m["memory_sent"] and m["status"] == "completed" for m in result["members"]
        )
    except Exception as exc:
        result["error_class"] = type(exc).__name__
    finally:
        await manager.close()
        service.close()
    (output or root / "docs/design/b2-4/graph-model-preflight.json").write_text(
        json.dumps(result, indent=2)
    )
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--phase", default="preflight_not_final_acceptance")
    parser.add_argument("--model-id", default="gpt-oss-20b")
    args = parser.parse_args()
    asyncio.run(main(output=args.output, phase=args.phase, model_id=args.model_id))
