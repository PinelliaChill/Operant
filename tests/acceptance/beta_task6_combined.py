"""Opt-in real multi-agent, corrected memory and Graph artifact acceptance.

Only a fresh, absolute synthetic directory is used. Credentials are supplied
in the process environment; the script never reads or copies a host .env.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from operant.api import create_app
from operant.api_workbench_agents import SendAgentMessageRequest
from operant.application.security import PolicyEngine
from operant.contracts.b2_3 import ManagementCommand
from operant.domain.graph import (
    EdgeSpec,
    GraphRunStatus,
    IdempotencyClass,
    NodeKind,
    NodeSpec,
    PortSpec,
    WorkflowDefinition,
    WorkflowDefinitionStatus,
)
from operant.domain.models import Budget, Effort, ModelProfile, RolePreset, ToolPolicy
from operant.domain.security import PolicyBundle, PolicyDecision, PolicyLayer, PolicyRule
from operant.domain.team import TeamDefinition, TeamMember
from operant.domain.threads import ConversationThread


async def main(root: Path, model_id: str) -> None:
    if os.environ.get("OPERANT_TASK6_REAL_ACCEPTANCE") != "1":
        raise RuntimeError("explicit isolated real acceptance opt-in required")
    if not root.is_absolute():
        raise ValueError("absolute evidence directory required")
    root.mkdir(parents=True, exist_ok=False)
    workspace = root / "workspace"
    workspace.mkdir()
    (workspace / "probe.txt").write_text("Independent daily check: 7 times 13.\n")
    result: dict[str, object] = {"status": "failed", "model_id": model_id}
    policy = PolicyEngine(
        PolicyBundle(
            bundle_id="task6-synthetic-only",
            version="v1",
            default_decision=PolicyDecision.DENY,
            rules=(
                PolicyRule(
                    rule_id="synthetic-combination",
                    layer=PolicyLayer.SYSTEM,
                    decision=PolicyDecision.ALLOW,
                    reason="bounded synthetic workspace with read and collaboration only",
                ),
            ),
        )
    )
    app = create_app(
        root / "core.sqlite3", artifact_root=root / "artifacts", phase45_policy_engine=policy
    )
    service = app.state.operant_service
    manager = service.memory_manager_factory()
    service.memory_manager = manager

    async def command(**kwargs):
        return await manager.execute(ManagementCommand(**kwargs))

    try:
        found = await service.discover_models(
            base_url=os.environ["OPERANT_BASE_URL"], secret_ref="OPERANT_API_KEY"
        )
        if model_id not in found:
            raise RuntimeError("exact model ID absent from formal Discovery")
        result["discovery_exact_match"] = True
        project = (
            await command(
                action="project_create", name="Task6 daily", workspace_path=str(workspace)
            )
        ).state.projects[-1]
        installed = (
            await command(
                action="plugin_install", plugin_id="memory-standard", mode="trusted_in_process"
            )
        ).state.installations[-1]
        await command(
            action="binding_select",
            project_id=project.project_id,
            installation_id=installed.installation_id,
        )
        record = (
            await command(
                action="memory_save",
                project_id=project.project_id,
                content="Daily report code / 日报代号为 ORCHID_61，日报用中文。",
                confirmed=True,
            )
        ).state.records[0]
        profile = service.add_model_profile(
            ModelProfile(
                name="Task6 discovered daily model",
                model_id=model_id,
                base_url=os.environ["OPERANT_BASE_URL"],
                secret_ref="OPERANT_API_KEY",
                supported_efforts=(Effort.LOW,),
                default_effort=Effort.LOW,
                effort_parameter=None,
                context_window=32768,
            )
        )
        worker = service.create_role(
            RolePreset(
                name="Daily read-only child",
                model_profile_id=profile.id,
                effort=Effort.LOW,
                system_prompt="Read the assigned file and recalled memory, then answer in Chinese.",
                memory_scope="project",
                tool_policy=ToolPolicy(allowed_tools=("read_file",)),
                budget=Budget(max_turns=4, max_output_tokens=1024, timeout_seconds=180),
            )
        )
        role = service.create_role(
            RolePreset(
                name="Daily coordinator",
                model_profile_id=profile.id,
                effort=Effort.LOW,
                system_prompt=(
                    "Complete only the synthetic daily task. Use confirmed project memory. "
                    "When requested, delegate exactly one child to the named read-only Role, "
                    "wait for its result, and summarize. Never delegate again or send "
                    "unsolicited messages. Keep the task language and memory lookup terms."
                ),
                memory_scope="project",
                tool_policy=ToolPolicy(
                    allowed_tools=(
                        "read_file",
                        "delegate_agent",
                        "send_agent_message",
                        "wait_for_agent",
                    )
                ),
                budget=Budget(max_turns=8, max_output_tokens=4096, timeout_seconds=240),
            )
        )
        thread = service.create_thread(ConversationThread(workspace_ref=str(workspace)))
        session = service.create_session(role.id, thread_id=thread.id)
        events = [
            event
            async for event in service.run_session(
                session.id,
                user_message=(
                    f"请委派且只委派一名子 Agent，选择只读角色 {worker.id}，"
                    "让它用 read_file 读取 probe.txt，"
                    "结合已确认项目记忆，求出算式结果及日报代号。等待它完成，"
                    "然后写一句中文日报，必须包含它回报的代号和结果。"
                ),
                workspace=workspace,
                thread_id=thread.id,
            )
        ]
        completed = next((e for e in reversed(events) if e.event_type == "agent.completed"), None)
        if completed is None:
            raise AssertionError([e.event_type for e in events])
        children = service.workbench.list_children(thread.id)
        if len(children) != 1 or children[0].status != "completed":
            raise AssertionError("single real child did not complete")
        parent_text = str(completed.payload.get("content", ""))
        child_text = children[0].result or ""
        result["initial_answers"] = {"parent": parent_text, "child": child_text}
        if not all("ORCHID_61" in text and "91" in text for text in (parent_text, child_text)):
            raise AssertionError("parent or child omitted recalled code or calculated result")
        if any("未确认" in text or "无法确认" in text for text in (parent_text, child_text)):
            raise AssertionError("published memory was incorrectly described as unconfirmed")
        child_id = children[0].thread_id
        body = SendAgentMessageRequest(
            recipient_thread_id=child_id,
            body="请核对刚才的算式结果，用一句话回复父任务，不再委派：结果为91。",
            idempotency_key="task6-private-followup",
        )
        sent, created = service.workbench.send_once(thread.id, body)
        if not created or await service.workbench.wake(child_id) != "scheduled":
            raise AssertionError("private followup failed to wake completed child")
        wake_task = service.workbench.tasks.get(child_id) or service.workbench.wake_tasks.get(
            child_id
        )
        if wake_task is None:
            raise AssertionError("scheduled private followup has no active task")
        await wake_task
        replayed, recreated = service.workbench.send_once(thread.id, body)
        if recreated or replayed.message_id != sent.message_id:
            raise AssertionError("private message retry was not deduplicated")
        consumed = next(
            m for m in service.workbench.list_messages(child_id) if m.message_id == sent.message_id
        )
        if consumed.delivery_status != "consumed":
            raise AssertionError("real followup did not consume private message")
        result["collaboration"] = {
            "parent_thread_id": thread.id,
            "session_id": session.id,
            "child_thread_id": children[0].thread_id,
            "parent_result": parent_text,
            "child_result": child_text,
            "private_message_id": sent.message_id,
            "private_message_consumed": True,
            "duplicate_created": False,
        }
        proposed = await command(
            action="memory_propose",
            project_id=project.project_id,
            record_id=record.record_id,
            expected_revision=record.revision,
            content="Daily report code / 日报代号更新为 ORCHID_72，日报仍用中文。",
        )
        updated = next(r for r in proposed.state.records if r.record_id == record.record_id)
        if updated.content != record.content:
            raise AssertionError("unconfirmed proposal modified the published preference")
        await command(
            action="memory_confirm",
            project_id=project.project_id,
            proposal_id=updated.proposals[-1].proposal_id,
            expected_revision=record.revision,
        )
        roles = [
            service.create_role(
                RolePreset(
                    name=f"Daily Graph worker {i}",
                    model_profile_id=profile.id,
                    effort=Effort.LOW,
                    system_prompt=(
                        "Use confirmed project memory. Read only; concise Chinese output."
                    ),
                    memory_scope="project",
                    tool_policy=ToolPolicy(allowed_tools=("read_file",)),
                    budget=Budget(max_turns=3, max_output_tokens=512, timeout_seconds=120),
                )
            )
            for i in range(2)
        ]
        team = TeamDefinition(
            members=tuple(
                TeamMember(
                    member_id=f"worker{i}",
                    agent_definition_id=r.id,
                    role=r.name,
                    can_coordinate=i == 0,
                )
                for i, r in enumerate(roles)
            ),
            default_coordinator="worker0",
        )
        app.state.team_repository.put_team_definition(team)
        nodes = tuple(
            NodeSpec(
                node_id=f"worker{i}",
                node_kind=NodeKind.AGENT,
                input_ports=(PortSpec(name="input", value_type="string", required=False),),
                output_ports=(PortSpec(name="result", value_type="string"),),
                metadata={
                    "role_id": r.id,
                    "role_version": r.version,
                    "task": (
                        "read_file 读取 probe.txt，结合已确认项目记忆，写一句中文日报，"
                        "包含当前代号和算式结果。"
                        if i == 0
                        else "核对日报与当前记忆，输出中文日报，含当前代号和算式结果。"
                    ),
                },
            )
            for i, r in enumerate(roles)
        ) + (
            NodeSpec(
                node_id="report",
                node_kind=NodeKind.ARTIFACT,
                idempotency_class=IdempotencyClass.IDEMPOTENT,
                input_ports=(PortSpec(name="content", value_type="string"),),
                output_ports=(PortSpec(name="artifact_id", value_type="string"),),
                metadata={
                    "content": {"$input": "content"},
                    "title": "Daily reviewed report",
                    "media_type": "text/plain",
                },
            ),
        )
        definition = WorkflowDefinition(
            name="Daily memory and reviewed artifact",
            status=WorkflowDefinitionStatus.PUBLISHED,
            nodes=nodes,
            edges=(
                EdgeSpec(
                    edge_id="review",
                    source_node="worker0",
                    source_port="result",
                    target_node="worker1",
                    target_port="input",
                ),
                EdgeSpec(
                    edge_id="publish",
                    source_node="worker1",
                    source_port="result",
                    target_node="report",
                    target_port="content",
                ),
            ),
            locked_role_versions={r.id: r.version for r in roles},
            default_policy={
                "team_id": team.team_id,
                "team_version": team.version,
                "b24_executor": True,
            },
            default_budget=Budget(max_turns=6, max_output_tokens=2048, timeout_seconds=240),
        )
        run = app.state.graph_runtime.create_run(definition, workspace_or_target=str(workspace))
        app.state.b24_graph_executor.prepare(run.id)
        app.state.b24_freeze_graph_memory(run.id)
        outcome = await app.state.b24_graph_executor.run(run.id)
        if outcome.run.status is not GraphRunStatus.COMPLETED:
            raise AssertionError(outcome.run.status.value)
        graph_nodes = app.state.graph_repository.list_node_runs(run.id)
        outputs = [n.output_refs for n in graph_nodes]
        serialized = json.dumps(outputs, ensure_ascii=False)
        if "ORCHID_72" not in serialized or "91" not in serialized or "ORCHID_61" in serialized:
            raise AssertionError("Graph artifact failed corrected-memory task")
        result["graph"] = {"run_id": run.id, "status": outcome.run.status.value, "outputs": outputs}
        result["status"] = "passed"
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        raise
    finally:
        await manager.close()
        service.close()
        (root / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(root / "result.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--model-id", required=True)
    args = parser.parse_args()
    asyncio.run(main(args.evidence_dir, args.model_id))
