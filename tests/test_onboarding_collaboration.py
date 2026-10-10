from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from operant.api_onboarding import install_onboarding_routes
from operant.application.graph import GraphRuntime
from operant.application.graph_execution import BoundedGraphExecutor
from operant.application.onboarding import OnboardingService
from operant.application.onboarding_collaboration import BasicTeamService
from operant.application.phase45_gateway import Phase45ActionGateway
from operant.application.security import PolicyEngine, balanced_policy_bundle
from operant.application.service import ApplicationService
from operant.contracts.onboarding import SetupBootstrap
from operant.domain.graph import GraphRunStatus
from operant.domain.models import Effort, ModelProfile
from operant.persistence.graph_team import SQLiteGraphRepository, SQLiteTeamRepository
from operant.persistence.onboarding import UXRepository
from operant.persistence.phase45 import SQLitePhase45Repository
from operant.persistence.security import SQLiteSecurityRepository
from operant.persistence.sqlite import SQLiteStore


@pytest.mark.asyncio
async def test_basic_team_uses_real_graph_and_team_admission(tmp_path: Path) -> None:
    service = ApplicationService(SQLiteStore(tmp_path / "core.sqlite"), object())  # type: ignore[arg-type]
    service.initialize()
    profile = service.add_model_profile(
        ModelProfile(
            name="ready",
            model_id="test-model",
            base_url="https://example.test/v1",
            secret_ref="TEST_MODEL_KEY",
            effort_parameter=None,
        )
    )
    repo = UXRepository(service.store)
    onboarding = OnboardingService(service, repo)
    state = await onboarding.bootstrap(SetupBootstrap(model_profile_id=profile.id))
    assert state.default_workspace_id is not None
    app = FastAPI()
    app.state.phase45_action_gateway = Phase45ActionGateway(
        SQLiteSecurityRepository(service.store),
        SQLitePhase45Repository(service.store),
        PolicyEngine(balanced_policy_bundle()),
    )
    graph_repository = SQLiteGraphRepository(service.store)
    team_repository = SQLiteTeamRepository(service.store)
    runtime = GraphRuntime(graph_repository)
    app.state.graph_repository = graph_repository
    app.state.team_repository = team_repository
    app.state.graph_runtime = runtime
    app.state.b24_graph_executor = BoundedGraphExecutor(
        service, graph_repository, runtime, team_repository
    )
    app.state.b24_freeze_graph_memory = lambda _run_id: None
    scheduled: list[str] = []

    def schedule_on_loop(run_id: str) -> None:
        assert asyncio.get_running_loop().is_running()
        scheduled.append(run_id)

    app.state.b24_schedule_graph = schedule_on_loop
    team = BasicTeamService(app, service, repo)
    try:
        first = team.start(
            template_id="basic",
            task="实现并检查一个小改动",
            workspace_id=state.default_workspace_id,
            idempotency_key="basic-team-1",
        )
        assert first["status"] == GraphRunStatus.RUNNING.value
        assert first["team_run_id"]
        assert scheduled == [first["graph_run_id"]]
        roster = team_repository.list_roster(first["team_run_id"])
        assert len(roster) == 3
        assert {repo.get_metadata(entry.thread_id).title for entry in roster} == {
            f"{service.get_role(role_id).name} · 基础协作团队"
            for role_id in ("role_planner", "role_coder", "role_reviewer")
        }
        renamed = repo.rename(roster[0].thread_id, "手动命名的团队对话")
        assert renamed.title_source == "manual"
        app.state.b24_graph_executor.prepare(first["graph_run_id"])
        definition = graph_repository.get_definition(first["workflow_definition_id"], 1)
        app.state.b24_graph_executor._name_graph_thread(
            roster[0].thread_id, service.get_role("role_planner"), definition
        )
        restored = UXRepository(SQLiteStore(service.store.path)).get_metadata(roster[0].thread_id)
        assert restored.title == "手动命名的团队对话"
        assert restored.title_source == "manual"
        assert graph_repository.get_run(first["graph_run_id"]).status is GraphRunStatus.RUNNING
        second: dict[str, Any] = team.start(
            template_id="basic",
            task="实现并检查一个小改动",
            workspace_id=state.default_workspace_id,
            idempotency_key="basic-team-1",
        )
        assert second == first
        assert scheduled == [first["graph_run_id"]]

        install_onboarding_routes(app, service, repo)
        refreshed_sources: list[str] = []
        app.state.refresh_skill_sources = refreshed_sources.append
        client = TestClient(app)
        templates = client.get("/v1/setup/team-templates")
        assert templates.status_code == 200
        assert templates.json()["items"][0]["template_id"] == "basic"
        started = client.post(
            "/v1/setup/teams/start",
            json={
                "template_id": "basic",
                "task": "分析另一项任务",
                "workspace_id": state.default_workspace_id,
            },
            headers={"Idempotency-Key": "basic-team-2"},
        )
        assert started.status_code == 202, started.text
        assert refreshed_sources == [
            service.store.get_workspace_initialization_by_id(
                state.default_workspace_id
            ).workspace_ref
        ]
        assert (
            graph_repository.get_run(started.json()["graph_run_id"]).status
            is GraphRunStatus.RUNNING
        )

        original_roles = {
            role_id: service.get_role(role_id)
            for role_id in ("role_planner", "role_coder", "role_reviewer")
        }
        role_ids_before_switch = {role.id for role in service.list_roles()}
        old_snapshots = tuple(
            service.get_session(
                service.store.get_agent(entry.agent_instance_id).session_id
            ).role_snapshot
            for entry in team_repository.list_roster(first["team_run_id"])
        )
        assert all(snapshot.model_profile_id == profile.id for snapshot in old_snapshots)
        service.deactivate_model_profile(profile.id)
        replacement = service.add_model_profile(
            ModelProfile(
                name="replacement",
                model_id="low-only-model",
                base_url="https://example.test/v1",
                secret_ref="REPLACEMENT_MODEL_KEY",
                supported_efforts=(Effort.LOW,),
                default_effort=Effort.LOW,
                effort_parameter=None,
            )
        )
        assert (await onboarding.bootstrap(SetupBootstrap(model_profile_id=replacement.id))).ready
        switched = team.start(
            template_id="basic",
            task="使用新模型执行基础团队",
            workspace_id=state.default_workspace_id,
            idempotency_key="basic-team-new-model",
        )
        switched_snapshots = tuple(
            service.get_session(
                service.store.get_agent(entry.agent_instance_id).session_id
            ).role_snapshot
            for entry in team_repository.list_roster(switched["team_run_id"])
        )
        assert len(switched_snapshots) == 3
        assert all(
            snapshot.model_profile_id == replacement.id and snapshot.effort is Effort.LOW
            for snapshot in switched_snapshots
        )
        assert all(snapshot.model_profile_id == profile.id for snapshot in old_snapshots)
        assert {role_id: service.get_role(role_id) for role_id in original_roles} == original_roles
        assert {role.id for role in service.list_roles()} == role_ids_before_switch
    finally:
        if service.memory_manager is not None:
            await service.memory_manager.close()
        service.close()
