"""Hook to durable Scheduler queue to the formal Graph Agent executor."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from operant.api import create_app
from operant.application.security import PolicyEngine
from operant.domain.graph import NodeKind, NodeSpec, WorkflowDefinition, WorkflowDefinitionStatus
from operant.domain.messages import ModelResponse, ModelUsage, ProviderEvent
from operant.domain.models import ModelProfile, RolePreset
from operant.domain.scheduler import ScheduleDefinition, TriggerKind
from operant.domain.security import PolicyBundle, PolicyDecision
from operant.domain.team import TeamDefinition, TeamMember


class ControlledProvider:
    def __init__(self, *, block: asyncio.Event | None = None) -> None:
        self.calls = 0
        self.block = block
        self.entered = threading.Event()

    async def stream(
        self, *, snapshot: Any, messages: Sequence[Any], tools: Sequence[Any]
    ) -> AsyncIterator[ProviderEvent]:
        del snapshot, messages, tools
        self.calls += 1
        self.entered.set()
        if self.block is not None:
            await self.block.wait()
        yield ProviderEvent(
            event_type="model.completed",
            response=ModelResponse(
                content="hook completed",
                finish_reason="stop",
                usage=ModelUsage(prompt_tokens=5, completion_tokens=3, total_tokens=8),
            ),
        )


def _allow_policy() -> PolicyEngine:
    return PolicyEngine(
        PolicyBundle(
            bundle_id="hook-test",
            version="hook-test.v1",
            default_decision=PolicyDecision.ALLOW,
            rules=(),
        )
    )


def _setup(
    client: TestClient, app: Any, workspace: Path, *, provider: ControlledProvider | None = None
) -> ControlledProvider:
    provider = provider or ControlledProvider()
    service = app.state.operant_service
    service.provider = provider
    profile = service.add_model_profile(
        ModelProfile(
            id="hook-profile",
            name="hook model",
            model_id="controlled-hook-model",
            base_url="https://provider.invalid/v1",
            secret_ref="HOOK_TEST_KEY",
        )
    )
    role = service.create_role(
        RolePreset(
            id="hook-role",
            name="Hook worker",
            system_prompt="Complete the assigned task.",
            model_profile_id=profile.id,
        )
    )
    app.state.team_repository.put_team_definition(
        TeamDefinition(
            team_id="hook-team",
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
    app.state.graph_repository.put_definition(
        WorkflowDefinition(
            workflow_id="hook-graph",
            version=1,
            name="Hook graph",
            nodes=(
                NodeSpec(
                    node_id="agent",
                    node_kind=NodeKind.AGENT,
                    metadata={
                        "role_id": role.id,
                        "role_version": role.version,
                        "task": "Return a short result.",
                    },
                ),
            ),
            default_policy={"team_id": "hook-team", "team_version": 1},
            locked_role_versions={role.id: role.version},
            status=WorkflowDefinitionStatus.PUBLISHED,
        )
    )
    schedule = ScheduleDefinition(
        id="hook-schedule",
        name="Application signal",
        trigger_kind=TriggerKind.HOOK,
        hook_event_type="application.signal",
        timezone_name="UTC",
        workflow_id="hook-graph",
        workflow_version=1,
        workflow_input={"workspace_or_target": str(workspace)},
        max_attempts=1,
    )
    response = client.post("/v1/schedules", json=schedule.model_dump(mode="json"))
    assert response.status_code == 201, response.text
    return provider


def _wait_terminal(app: Any, request_id: str) -> Any:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        request = app.state.scheduler_store.get_request(request_id)
        if request.status.value in {
            "succeeded",
            "dead_letter",
            "cancelled",
            "manual_reconcile_required",
        }:
            return request
        time.sleep(0.05)
    request = app.state.scheduler_store.get_request(request_id)
    raise AssertionError(
        f"hook request did not reach terminal state: {request.status.value}; "
        f"scheduler={app.state.scheduler_coordinator.last_error_code}"
    )


def test_local_application_signal_deduplicates_and_executes_graph_agent(tmp_path: Path) -> None:
    app = create_app(tmp_path / "core.sqlite3", phase45_policy_engine=_allow_policy())
    with TestClient(app) as client:
        provider = _setup(client, app, tmp_path)
        payload = {"event_type": "application.signal", "event_id": "source-event-1"}
        first = client.post("/v1/schedules/hook-schedule/hook", json=payload)
        second = client.post("/v1/schedules/hook-schedule/hook", json=payload)
        assert first.status_code == second.status_code == 200
        assert first.json()["id"] == second.json()["id"]

        request = _wait_terminal(app, first.json()["id"])
        assert request.status.value == "succeeded", request
        assert request.workflow_run_id is not None
        run = app.state.graph_repository.get_run(request.workflow_run_id)
        assert run.status.value == "completed"
        assert run.workspace_or_target == str(tmp_path.resolve())
        assert provider.calls == 1
        assert app.state.graph_repository.list_node_runs(run.id)[0].status.value == "succeeded"

        paused = client.post(
            "/v1/schedules/hook-schedule/status",
            json={"status": "paused", "expected_version": 1},
        )
        assert paused.status_code == 200
        blocked = client.post(
            "/v1/schedules/hook-schedule/hook",
            json={"event_type": "application.signal", "event_id": "source-event-2"},
        )
        assert blocked.status_code == 409
        assert provider.calls == 1


def test_expired_bound_graph_lease_is_taken_over_without_new_dispatch_attempt(
    tmp_path: Path,
) -> None:
    app = create_app(tmp_path / "core.sqlite3", phase45_policy_engine=_allow_policy())
    with TestClient(app) as client:
        _setup(client, app, tmp_path)
        client.portal.call(app.state.scheduler_coordinator.stop)
        signal = client.post(
            "/v1/schedules/hook-schedule/hook",
            json={"event_type": "application.signal", "event_id": "restart-1"},
        )
        assert signal.status_code == 200
        request_id = signal.json()["id"]
        coordinator = app.state.scheduler_coordinator
        client.portal.call(coordinator.run_cycle)
        request = app.state.scheduler_store.get_request(request_id)
        assert request.workflow_run_id is not None
        assert request.attempt_count == 1
        # Simulate a lost Core before observing the Graph terminal state.
        with app.state.operant_service.store._connect() as connection:
            connection.execute(
                "UPDATE job_leases SET expires_at=? WHERE run_request_id=?",
                ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), request_id),
            )
        coordinator.release_leases()
        # The new writer takes the same bound run even though max_attempts=1.
        writer = app.state.scheduler_store.acquire_authority(
            "runtime_writer", owner="new-writer", ttl_seconds=30
        )
        recovered = app.state.scheduler_store.renew_dispatched_jobs(
            writer, owner="new-writer", ttl_seconds=30
        )
        assert len(recovered) == 1
        assert recovered[0][0].id == request_id
        assert recovered[0][0].attempt_count == 1
        assert recovered[0][0].workflow_run_id == request.workflow_run_id


def test_hook_request_cancel_before_dispatch_does_not_create_graph(tmp_path: Path) -> None:
    app = create_app(tmp_path / "core.sqlite3", phase45_policy_engine=_allow_policy())
    with TestClient(app) as client:
        provider = _setup(client, app, tmp_path)
        client.portal.call(app.state.scheduler_coordinator.stop)
        queued = client.post(
            "/v1/schedules/hook-schedule/hook",
            json={"event_type": "application.signal", "event_id": "e" * 200},
        )
        assert queued.status_code == 200
        request_id = queued.json()["id"]
        cancelled = client.post(f"/v1/scheduler/queue/{request_id}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        client.portal.call(app.state.scheduler_coordinator.run_cycle)
        assert app.state.scheduler_store.get_request(request_id).status.value == "cancelled"
        assert provider.calls == 0
        with app.state.operant_service.store._connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM graph_workflow_runs").fetchone()[0] == 0


def test_hook_rejects_remote_source_and_unknown_event_type(tmp_path: Path) -> None:
    app = create_app(tmp_path / "core.sqlite3", phase45_policy_engine=_allow_policy())
    with TestClient(app, client=("203.0.113.7", 12345)) as remote:
        _setup(remote, app, tmp_path)
        signal = remote.post(
            "/v1/schedules/hook-schedule/hook",
            json={"event_type": "application.signal", "event_id": "remote-event"},
        )
        assert signal.status_code == 403
        unknown = remote.post(
            "/v1/schedules/hook-schedule/hook",
            json={"event_type": "filesystem.changed", "event_id": "event-1"},
        )
        assert unknown.status_code == 422
        assert app.state.scheduler_store.list_requests() == ()


def test_schedule_rejects_unusable_workspace_before_persisting(tmp_path: Path) -> None:
    app = create_app(tmp_path / "core.sqlite3", phase45_policy_engine=_allow_policy())
    with TestClient(app) as client:
        _setup(client, app, tmp_path)
        schedule = app.state.scheduler_store.get_schedule("hook-schedule")
        invalid = schedule.model_copy(
            update={
                "id": "invalid-workspace-hook",
                "workflow_input": {"workspace_or_target": "relative/path"},
            }
        )
        rejected = client.post("/v1/schedules", json=invalid.model_dump(mode="json"))
        assert rejected.status_code == 422
        assert "absolute path" in rejected.text
        assert [item.id for item in app.state.scheduler_store.list_schedules()] == ["hook-schedule"]


def test_shutdown_interrupts_scheduled_graph_and_new_core_resumes_same_run(tmp_path: Path) -> None:
    database = tmp_path / "core.sqlite3"
    first_app = create_app(database, phase45_policy_engine=_allow_policy())
    blocked = ControlledProvider(block=asyncio.Event())
    with TestClient(first_app) as client:
        _setup(client, first_app, tmp_path, provider=blocked)
        response = client.post(
            "/v1/schedules/hook-schedule/hook",
            json={"event_type": "application.signal", "event_id": "core-restart"},
        )
        assert response.status_code == 200
        request_id = response.json()["id"]
        assert blocked.entered.wait(5)
        graph_id = first_app.state.scheduler_store.get_request(request_id).workflow_run_id
        assert graph_id is not None
    assert first_app.state.graph_repository.get_run(graph_id).status.value == "interrupted"
    with first_app.state.operant_service.store._connect() as connection:
        connection.execute(
            "UPDATE job_leases SET expires_at=? WHERE run_request_id=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), request_id),
        )

    restarted_app = create_app(database, phase45_policy_engine=_allow_policy())
    resumed_provider = ControlledProvider()
    restarted_app.state.operant_service.provider = resumed_provider
    with TestClient(restarted_app):
        result = _wait_terminal(restarted_app, request_id)
        assert result.status.value == "succeeded", (
            result.status.value,
            result.last_error_code,
            restarted_app.state.graph_repository.get_run(graph_id).status.value,
            [
                (
                    node.status.value,
                    [
                        (a.result.value if a.result else None, a.failure_class)
                        for a in restarted_app.state.graph_repository.list_attempts(node.id)
                    ],
                )
                for node in restarted_app.state.graph_repository.list_node_runs(graph_id)
            ],
            resumed_provider.calls,
        )
        assert result.workflow_run_id == graph_id
        assert result.attempt_count == 1
        assert restarted_app.state.graph_repository.get_run(graph_id).status.value == "completed"
        assert resumed_provider.calls == 1
