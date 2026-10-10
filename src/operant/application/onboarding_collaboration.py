"""Start the basic team through the existing Graph and Team runtime."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from operant.application.graph_execution import READ_ONLY_AGENT_TOOLS
from operant.application.service import ApplicationService
from operant.domain.actions import CommandExecution, CommandExecutionStatus
from operant.domain.graph import (
    EdgeSpec,
    GraphLimits,
    GraphRunStatus,
    IdempotencyClass,
    NodeKind,
    NodeSpec,
    PortSpec,
    WorkflowDefinition,
    WorkflowDefinitionStatus,
)
from operant.domain.models import Budget, ModelProfile, RolePreset
from operant.domain.security import Capability
from operant.domain.team import TeamDefinition, TeamMember
from operant.persistence.sqlite import ConflictError
from operant.protocol import canonical_action_hash


class TeamApprovalRequired(PermissionError):
    def __init__(self, approval_id: str | None, reason_code: str) -> None:
        self.approval_id = approval_id
        self.reason_code = reason_code
        super().__init__(reason_code)


class BasicTeamService:
    """One pinned planner → coder → reviewer template with no editor setup."""

    def __init__(self, app: FastAPI, service: ApplicationService, repo: Any) -> None:
        self.app = app
        self.service = service
        self.repo = repo

    def start(
        self,
        *,
        template_id: str,
        task: str,
        workspace_id: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if template_id != "basic":
            raise ValueError("unknown team template")
        task = task.strip()
        if not task or len(task) > 4_000:
            raise ValueError("team task must contain 1–4000 characters")
        selected_workspace_id = workspace_id or self.repo.get_setting("default_workspace_id")
        if not selected_workspace_id:
            raise ValueError("choose a workspace before starting a team")
        workspace = self.service.store.get_workspace_initialization_by_id(selected_workspace_id)
        path = Path(workspace.workspace_ref).resolve(strict=True)
        if not path.is_dir() or str(path) != workspace.workspace_ref or not workspace.writable:
            raise ValueError("team workspace is unavailable or read-only")
        action_hash = canonical_action_hash(
            {
                "operation": "start_template_team",
                "template_id": template_id,
                "task": task,
                "workspace_id": selected_workspace_id,
            }
        )
        with self.service.store._connect() as connection:
            previous = connection.execute(
                "SELECT id FROM command_executions WHERE command_type=? AND idempotency_key=?",
                ("setup.team_template.start", idempotency_key),
            ).fetchone()
        if previous is not None:
            command = self.service.store.get_command_execution(str(previous["id"]))
            if command.action_hash != action_hash:
                raise ConflictError("request identity already belongs to another team task")
            if command.status is CommandExecutionStatus.COMPLETED and command.response_json:
                return dict(json.loads(command.response_json))
            raise ConflictError("team start outcome needs reconciliation; do not retry")
        gateway = self.app.state.phase45_action_gateway
        action, decision, _ = gateway.guard(
            tool="onboarding",
            operation="team_start",
            target_id=selected_workspace_id,
            arguments={"request_hash": action_hash},
            capabilities=(Capability.WORKSPACE_WRITE,),
            idempotency_key=idempotency_key,
        )
        if decision.decision.value != "allow" or decision.lease is None:
            if decision.decision.value == "ask":
                raise TeamApprovalRequired(decision.approval_id, decision.reason_code)
            raise PermissionError(decision.reason_code)
        gateway.consume(decision.lease, action)

        command, created = self.service.store.reserve_command_execution(
            CommandExecution(
                command_type="setup.team_template.start",
                idempotency_key=idempotency_key,
                action_hash=action_hash,
            )
        )
        if not created:
            if command.status is CommandExecutionStatus.COMPLETED and command.response_json:
                return dict(json.loads(command.response_json))
            raise ConflictError("team start outcome needs reconciliation; do not retry")

        # All mutations below use the real Graph/Team stores. A crash after
        # admission leaves the command in progress for explicit reconciliation.
        roles, profile = self._roles()
        definition, team = self._definition(roles, profile, task, str(path))
        self.app.state.team_repository.put_team_definition(team)
        runtime = self.app.state.graph_runtime
        run = runtime.create_run(definition, input={"task": task}, workspace_or_target=str(path))
        try:
            prepared = self.app.state.b24_graph_executor.prepare(run.id)
            self.app.state.b24_freeze_graph_memory(run.id)
            started = runtime.start_run(run.id)
            self.app.state.b24_schedule_graph(run.id)
        except Exception:
            current = self.app.state.graph_repository.get_run(run.id)
            if current.status in {
                GraphRunStatus.CREATED,
                GraphRunStatus.QUEUED,
                GraphRunStatus.RUNNING,
            }:
                runtime.fail_run(run.id)
            raise
        result = {
            "workflow_definition_id": definition.workflow_id,
            "graph_run_id": run.id,
            "team_run_id": prepared.team_run_id,
            "status": started.status.value,
        }
        self.service.store.complete_command_execution(
            command.id,
            response_json=json.dumps(result, ensure_ascii=False, sort_keys=True),
            http_status=202,
            resource_type="graph_run",
            resource_id=run.id,
        )
        return result

    def _roles(self) -> tuple[tuple[RolePreset, RolePreset, RolePreset], ModelProfile]:
        roles = tuple(
            self.service.get_role(role_id)
            for role_id in ("role_planner", "role_coder", "role_reviewer")
        )
        if any(role.status.value != "active" for role in roles):
            raise ValueError("basic team members are inactive; finish setup first")
        profile_id = self.repo.get_setting("default_model_profile_id")
        if not isinstance(profile_id, str):
            raise ValueError("select a model before starting a team")
        profile = self.service.get_model_profile(profile_id)
        if not profile.enabled:
            raise ValueError("selected team model is inactive")
        return roles, profile  # type: ignore[return-value]

    @staticmethod
    def _definition(
        roles: tuple[RolePreset, RolePreset, RolePreset],
        profile: ModelProfile,
        task: str,
        workspace: str,
    ) -> tuple[WorkflowDefinition, TeamDefinition]:
        names = ("planner", "coder", "reviewer")
        nodes = tuple(
            NodeSpec(
                node_id=name,
                node_kind=NodeKind.AGENT,
                model_override=profile.id,
                input_ports=(PortSpec(name="input", required=False),),
                output_ports=(PortSpec(name="result", value_type="string", required=False),),
                writes_workspace=bool(
                    set(role.tool_policy.allowed_tools).difference(READ_ONLY_AGENT_TOOLS)
                ),
                idempotency_class=(
                    IdempotencyClass.NON_IDEMPOTENT
                    if set(role.tool_policy.allowed_tools).difference(READ_ONLY_AGENT_TOOLS)
                    else IdempotencyClass.PURE
                ),
                workspace_or_target=workspace,
                metadata={"role_id": role.id, "role_version": role.version, "task": task},
            )
            for name, role in zip(names, roles, strict=True)
        )
        team = TeamDefinition(
            members=tuple(
                TeamMember(
                    member_id=name,
                    agent_definition_id=role.id,
                    role=role.name,
                    can_coordinate=index == 0,
                )
                for index, (name, role) in enumerate(zip(names, roles, strict=True))
            ),
            default_coordinator="planner",
            max_active_agents=3,
            max_spawn_depth=0,
        )
        definition = WorkflowDefinition(
            name="基础协作团队",
            description="规划、执行、审查同一任务",
            nodes=nodes,
            edges=(
                EdgeSpec(
                    edge_id="plan-code",
                    source_node="planner",
                    source_port="result",
                    target_node="coder",
                    target_port="input",
                ),
                EdgeSpec(
                    edge_id="code-review",
                    source_node="coder",
                    source_port="result",
                    target_node="reviewer",
                    target_port="input",
                ),
            ),
            graph_limits=GraphLimits(max_parallel_nodes=1),
            default_budget=Budget(max_turns=24, timeout_seconds=1800),
            default_policy={
                "b24_executor": True,
                "team_id": team.team_id,
                "team_version": team.version,
            },
            locked_role_versions={role.id: role.version for role in roles},
            locked_provider_versions={
                profile.id: hashlib.sha256(
                    json.dumps(
                        profile.model_dump(mode="json"),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
            },
            status=WorkflowDefinitionStatus.PUBLISHED,
        )
        return definition, team
