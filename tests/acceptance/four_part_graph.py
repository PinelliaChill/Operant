"""Opt-in real-model check for Graph execution with inherited configuration."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from operant.application.configuration import ConfigService
from operant.application.graph import GraphRuntime
from operant.application.graph_execution import BoundedGraphExecutor
from operant.application.service import ApplicationService
from operant.domain.graph import (
    EdgeSpec,
    GraphRunStatus,
    NodeKind,
    NodeRunStatus,
    NodeSpec,
    PortSpec,
    WorkflowDefinition,
    WorkflowDefinitionStatus,
)
from operant.domain.models import Budget, Effort, ModelProfile, RolePreset, ToolPolicy
from operant.domain.team import TeamDefinition, TeamMember
from operant.persistence.graph_team import SQLiteGraphRepository, SQLiteTeamRepository
from operant.persistence.sqlite import SQLiteStore
from operant.providers.openai_compatible import OpenAICompatibleProvider
from operant.settings import load_local_env


async def main() -> None:
    if os.environ.get("OPERANT_FOUR_PART_REAL_MODEL") != "1":
        raise RuntimeError("set OPERANT_FOUR_PART_REAL_MODEL=1 for this acceptance")
    load_local_env(os.environ["OPERANT_ACCEPTANCE_ENV_FILE"])
    model_id = os.environ["OPERANT_ACCEPTANCE_MODEL_ID"]
    with tempfile.TemporaryDirectory(prefix="operant-four-part-graph-") as temporary:
        root = Path(temporary).resolve()
        (root / "input.txt").write_text("integration fixture\n", encoding="utf-8")
        service = ApplicationService(SQLiteStore(root / "core.sqlite3"), OpenAICompatibleProvider())
        service.initialize()
        try:
            profile = service.add_model_profile(
                ModelProfile(
                    name="Four-part Graph acceptance",
                    model_id=model_id,
                    base_url=os.environ["OPERANT_BASE_URL"],
                    secret_ref="OPERANT_API_KEY",
                    supported_efforts=(Effort.LOW,),
                    default_effort=Effort.LOW,
                    effort_parameter=None,
                )
            )
            role = service.create_role(
                RolePreset(
                    name="Graph integration worker",
                    system_prompt="Answer the assigned task briefly.",
                    model_profile_id=profile.id,
                    effort=Effort.LOW,
                    tool_policy=ToolPolicy(allowed_tools=("read_file",)),
                    budget=Budget(max_turns=4, max_output_tokens=1024),
                )
            )
            ConfigService(service.store).put_scope(
                "global",
                "default",
                patch={
                    "system_prompt": "Integration configuration is active.",
                    "tool_policy": ToolPolicy(allowed_tools=("read_file",)).model_dump(mode="json"),
                },
                expected_revision=0,
            )
            graphs = SQLiteGraphRepository(service.store)
            teams = SQLiteTeamRepository(service.store)
            runtime = GraphRuntime(graphs)
            definition = WorkflowDefinition(
                workflow_id="four-part-graph",
                version=1,
                name="Graph configuration and Tool",
                nodes=(
                    NodeSpec(
                        node_id="agent",
                        node_kind=NodeKind.AGENT,
                        output_ports=(
                            PortSpec(name="result", value_type="string", required=False),
                        ),
                        metadata={
                            "role_id": role.id,
                            "role_version": role.version,
                            "task": "Reply READY. Do not call tools.",
                        },
                    ),
                    NodeSpec(
                        node_id="read",
                        node_kind=NodeKind.TOOL,
                        input_ports=(PortSpec(name="input", required=False),),
                        output_ports=(PortSpec(name="result", value_type="object"),),
                        metadata={
                            "role_id": role.id,
                            "role_version": role.version,
                            "tool_name": "read_file",
                            "arguments": {"path": "input.txt"},
                        },
                    ),
                ),
                edges=(
                    EdgeSpec(
                        edge_id="agent-read",
                        source_node="agent",
                        source_port="result",
                        target_node="read",
                        target_port="input",
                    ),
                ),
                default_policy={"team_id": "four-part-team", "team_version": 1},
                locked_role_versions={role.id: role.version},
                status=WorkflowDefinitionStatus.PUBLISHED,
            )
            teams.put_team_definition(
                TeamDefinition(
                    team_id="four-part-team",
                    version=1,
                    members=(
                        TeamMember(
                            member_id="agent",
                            agent_definition_id=role.id,
                            role="Worker",
                            can_coordinate=True,
                        ),
                    ),
                    default_coordinator="agent",
                )
            )
            run = runtime.create_run(definition, workspace_or_target=str(root))
            result = await BoundedGraphExecutor(service, graphs, runtime, teams).run(run.id)
            nodes = {node.node_id: node for node in result.node_runs}
            if result.status is not GraphRunStatus.COMPLETED:
                raise RuntimeError(f"Graph ended in {result.status.value}")
            if nodes["agent"].status is not NodeRunStatus.SUCCEEDED:
                raise RuntimeError("real-model Agent node did not succeed")
            if nodes["read"].output_refs["result"]["content"] != "integration fixture\n":
                raise RuntimeError("Graph Tool did not read the isolated fixture")
            attempts = graphs.list_attempts(nodes["agent"].id)
            if not attempts:
                raise RuntimeError("Graph Agent attempt is missing")
            agent_id = attempts[-1].agent_instance_id
            if agent_id is None:
                raise RuntimeError("Graph Agent identity is missing")
            snapshot = service.get_session(
                service.store.get_agent(agent_id).session_id
            ).role_snapshot
            if "Integration configuration is active." not in snapshot.system_prompt:
                raise RuntimeError("Graph Agent did not freeze effective configuration")
            print("model_id", model_id)
            print("entry", "BoundedGraphExecutor.run")
            print("graph_status", result.status.value)
            print("agent_status", nodes["agent"].status.value)
            print("tool_status", nodes["read"].status.value)
        finally:
            service.close()


asyncio.run(main())
