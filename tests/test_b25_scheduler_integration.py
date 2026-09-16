"""Actual API/Scheduler/Graph/Host paths with a controlled (not real) Provider."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

from operant.api import create_app
from operant.application.security import PolicyEngine
from operant.domain.messages import ModelResponse, ModelUsage, ProviderEvent
from operant.domain.models import ModelProfile
from operant.domain.security import PolicyBundle, PolicyDecision
from operant.domain.threads import ConversationThread, Item, Turn, UserMessagePayload


class ControlledProvider:
    def __init__(self, *, mode="valid"):
        self.mode = mode
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()

    async def stream(self, *, snapshot, messages, tools):
        assert not tools
        self.calls += 1
        self.entered.set()
        if self.mode == "blocking":
            try:
                await asyncio.to_thread(self.release.wait, 3)
            except asyncio.CancelledError:
                # An uncooperative model must still be unable to commit after close.
                await asyncio.to_thread(self.release.wait, 3)
        prompt = "\n".join(m.content or "" for m in messages)
        job = re.search(r'"request_id"\s*:\s*"([^"]+)"', prompt).group(1)
        cursor = int(re.search(r'"source_watermark"\s*:\s*(\d+)', prompt).group(1))
        content = json.dumps(
            {
                "request_id": job,
                "source_watermark": cursor,
                "candidates": [{"content": "项目使用中文注释", "source_indices": [0]}],
            }
        )
        if self.mode == "invalid":
            content = "not-json"
        yield ProviderEvent(
            event_type="model.completed",
            response=ModelResponse(
                content=content,
                finish_reason="stop",
                usage=ModelUsage(prompt_tokens=10, completion_tokens=8, total_tokens=18),
            ),
        )


def command(client, *, version="b2-5", **body):
    response = client.post(f"/v1/{version}/commands", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def setup(client, app, tmp_path, provider, *, content="本项目统一采用中文注释"):
    service = app.state.operant_service
    service.provider = provider
    profile = service.add_model_profile(
        ModelProfile(
            name="test",
            model_id="controlled-model",
            base_url="https://invalid.test/v1",
            secret_ref="TEST_KEY",
            context_window=32768,
        )
    )
    project = command(
        client,
        version="b2-3",
        action="project_create",
        name="测试项目",
        workspace_path=str(tmp_path),
    )["state"]["projects"][-1]["project_id"]
    install = command(
        client,
        version="b2-3",
        action="plugin_install",
        plugin_id="memory-standard",
        mode="trusted_in_process",
    )["state"]["installations"][-1]["installation_id"]
    command(
        client, version="b2-3", action="binding_select", project_id=project, installation_id=install
    )
    configured = command(client, action="maintenance_configure", project_id=project, enabled=True)
    assert configured["state"]["maintenance_enabled"]
    thread = service.create_thread(ConversationThread(workspace_ref=str(tmp_path)))
    turn = service.create_turn(Turn(thread_id=thread.id))
    service.append_item(
        Item(thread_id=thread.id, turn_id=turn.id, payload=UserMessagePayload(text=content))
    )
    return project, install, profile


def wait_job(client, project, job, *, states=None):
    deadline = time.monotonic() + 12
    states = states or {
        "succeeded",
        "no_change",
        "dead_letter",
        "cancelled",
        "blocked",
        "manual_reconcile_required",
    }
    while time.monotonic() < deadline:
        response = client.get(f"/v1/b2-5/projects/{project}/governance")
        assert response.status_code == 200, response.text
        state = response.json()
        current = next((j for j in state["jobs"] if j["job_id"] == job), None)
        if current and current["state"] in states:
            return current, state
        time.sleep(0.03)
    raise AssertionError("Scheduler job did not reach expected state")


def _policy_engine(decision: PolicyDecision) -> PolicyEngine:
    return PolicyEngine(
        PolicyBundle(
            bundle_id="b25-scheduler-test",
            version=f"b25-scheduler-test.{decision.value}",
            default_decision=decision,
            rules=(),
        )
    )


def _stop_scheduler(client, app) -> None:
    """Freeze the real Scheduler so this test owns the dispatch checkpoint."""
    client.portal.call(app.state.scheduler_coordinator.stop)


def _graph_run_count(app) -> int:
    with app.state.operant_service.store._connect() as connection:
        row = connection.execute("SELECT COUNT(*) FROM graph_workflow_runs").fetchone()
    assert row is not None
    return int(row[0])


def _queued_job(client, project, profile):
    response = client.post(
        "/v1/b2-5/commands",
        json={
            "action": "maintenance_create",
            "project_id": project,
            "model_profile_id": profile.id,
        },
    )
    assert response.status_code == 200, response.text
    job_id = response.json()["affected_ids"][0]
    projection = client.get(f"/v1/b2-5/projects/{project}/governance")
    assert projection.status_code == 200, projection.text
    job = next(item for item in projection.json()["jobs"] if item["job_id"] == job_id)
    assert job["run_request_id"]
    return job_id, job


@pytest.mark.parametrize("decision", [PolicyDecision.DENY, PolicyDecision.ASK])
def test_maintenance_policy_deny_or_ask_blocks_before_model_or_graph_creation(tmp_path, decision):
    """The actual maintenance command must fail closed before snapshot creation."""
    app = create_app(tmp_path / "core.sqlite3")
    provider = ControlledProvider()
    with TestClient(app) as client:
        project, _install, profile = setup(client, app, tmp_path, provider)
        _stop_scheduler(client, app)

        # Phase45 API and Scheduler use this same policy object. Replacing the
        # bundle after setup leaves project/plugin bootstrapping available while
        # making the maintenance route exercise the deny/ask boundary itself.
        app.state.policy_engine.bundle = _policy_engine(decision).bundle
        response = client.post(
            "/v1/b2-5/commands",
            json={
                "action": "maintenance_create",
                "project_id": project,
                "model_profile_id": profile.id,
            },
        )

        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "policy_denied"
        assert provider.calls == 0
        assert _graph_run_count(app) == 0
        assert app.state.scheduler_store.list_requests() == ()
        with sqlite3.connect(app.state.operant_service.store.path) as connection:
            assert (
                connection.execute("SELECT COUNT(*) FROM workflow_definitions").fetchone()[0] == 0
            )


@pytest.mark.parametrize("decision", [PolicyDecision.DENY, PolicyDecision.ASK])
def test_queued_maintenance_dispatch_policy_deny_or_ask_blocks_graph_gateway(tmp_path, decision):
    """A queued maintenance request must still pass the Graph policy gate."""
    app = create_app(tmp_path / "core.sqlite3")
    provider = ControlledProvider()
    with TestClient(app) as client:
        project, _install, profile = setup(client, app, tmp_path, provider)
        _stop_scheduler(client, app)
        job_id, job = _queued_job(client, project, profile)

        # The request is already registered before the policy changes.  Running
        # the real Scheduler cycle now must reach GraphSchedulerActionGateway's
        # policy check, rather than using the maintenance wrapper as a bypass.
        app.state.policy_engine.bundle = _policy_engine(decision).bundle
        client.portal.call(app.state.scheduler_coordinator.run_cycle)

        request = app.state.scheduler_store.get_request(job["run_request_id"])
        assert request.status.value == "retry_wait"
        assert request.last_error_code == f"scheduler.policy_{decision.value}"
        assert provider.calls == 0
        assert _graph_run_count(app) == 0
        state = client.get(f"/v1/b2-5/projects/{project}/governance").json()
        current = next(item for item in state["jobs"] if item["job_id"] == job_id)
        assert current["state"] == "retry_wait"
        assert current["processed_cursor"] == 0
        assert state["proposals"] == []


def test_registered_run_request_snapshot_tamper_is_rejected_without_ordinary_graph_fallback(
    tmp_path,
):
    app = create_app(tmp_path / "core.sqlite3")
    provider = ControlledProvider()
    with TestClient(app) as client:
        project, _install, profile = setup(client, app, tmp_path, provider)
        _stop_scheduler(client, app)
        job_id, job = _queued_job(client, project, profile)
        request_id = job["run_request_id"]
        assert request_id

        with sqlite3.connect(app.state.operant_service.store.path) as connection:
            row = connection.execute(
                "SELECT workflow_input_json FROM run_requests WHERE request_id=?", (request_id,)
            ).fetchone()
            assert row is not None
            tampered = json.loads(row[0])
            tampered["maintenance"]["model_id"] = "forged-model"
            connection.execute(
                "UPDATE run_requests SET workflow_input_json=? WHERE request_id=?",
                (json.dumps(tampered, sort_keys=True, separators=(",", ":")), request_id),
            )

        client.portal.call(app.state.scheduler_coordinator.run_cycle)
        request = app.state.scheduler_store.get_request(request_id)
        assert request.status.value == "retry_wait"
        assert request.last_error_code == "maintenance.registered_request_mismatch"
        assert provider.calls == 0
        assert _graph_run_count(app) == 0
        state = client.get(f"/v1/b2-5/projects/{project}/governance").json()
        current = next(item for item in state["jobs"] if item["job_id"] == job_id)
        assert current["state"] == "retry_wait"
        assert current["processed_cursor"] == 0
        assert state["proposals"] == []


def test_missing_queued_maintenance_registration_requires_manual_reconciliation(tmp_path):
    """A registration crash must not fall through to an ordinary Graph run."""
    app = create_app(tmp_path / "core.sqlite3")
    provider = ControlledProvider()
    with TestClient(app) as client:
        project, _install, profile = setup(client, app, tmp_path, provider)
        _stop_scheduler(client, app)
        _job_id, job = _queued_job(client, project, profile)
        request_id = job["run_request_id"]
        assert request_id

        # Simulate a process crash after Scheduler accepted the request but
        # before the maintenance projection's register_queued commit.
        with sqlite3.connect(app.state.operant_service.store.path) as connection:
            deleted = connection.execute(
                "DELETE FROM b25_maintenance_jobs WHERE run_request_id=?", (request_id,)
            ).rowcount
            assert deleted == 1

        client.portal.call(app.state.scheduler_coordinator.run_cycle)

        request = app.state.scheduler_store.get_request(request_id)
        assert request.status.value == "manual_reconcile_required"
        assert request.last_error_code == "maintenance.registration_missing"
        assert provider.calls == 0
        assert _graph_run_count(app) == 0


def test_maintenance_switch_close_blocks_new_job_while_plugin_stays_enabled(tmp_path):
    app = create_app(tmp_path / "core.sqlite3")
    provider = ControlledProvider()
    with TestClient(app) as client:
        project, install, profile = setup(client, app, tmp_path, provider)
        _stop_scheduler(client, app)

        closed = client.post(
            "/v1/b2-5/commands",
            json={"action": "maintenance_configure", "project_id": project, "enabled": False},
        )
        assert closed.status_code == 200, closed.text
        blocked = client.post(
            "/v1/b2-5/commands",
            json={
                "action": "maintenance_create",
                "project_id": project,
                "model_profile_id": profile.id,
            },
        )
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["error"]["code"] == "maintenance.disabled"
        assert provider.calls == 0
        assert app.state.scheduler_store.list_requests() == ()
        management = client.get("/v1/b2-3/management")
        assert management.status_code == 200, management.text
        installation = next(
            item
            for item in management.json()["installations"]
            if item["installation_id"] == install
        )
        assert installation["state"] in {"enabled", "installed"}
        assert installation["state"] not in {"disabled", "uninstalled"}


def test_model_profile_change_after_extraction_started_blocks_before_commit(tmp_path):
    app = create_app(tmp_path / "core.sqlite3")
    provider = ControlledProvider(mode="blocking")
    with TestClient(app) as client:
        project, _install, profile = setup(client, app, tmp_path, provider)
        job = command(
            client, action="maintenance_create", project_id=project, model_profile_id=profile.id
        )["affected_ids"][0]
        assert provider.entered.wait(5)

        app.state.operant_service.update_model_profile(profile.id, model_id="changed-before-commit")
        provider.release.set()
        current, state = wait_job(client, project, job, states={"dead_letter"})

        assert current["state"] == "dead_letter", current
        assert current["error_code"] == "maintenance.snapshot_stale"
        assert current["processed_cursor"] == 0
        assert state["proposals"] == []
        assert provider.calls == 1


def test_real_scheduler_graph_commit_and_idle_model_suppression(tmp_path):
    app = create_app(tmp_path / "core.sqlite3")
    provider = ControlledProvider()
    with TestClient(app) as client:
        project, _, profile = setup(client, app, tmp_path, provider)
        job = command(
            client, action="maintenance_create", project_id=project, model_profile_id=profile.id
        )["affected_ids"][0]
        current, state = wait_job(client, project, job)
        assert current["state"] == "succeeded", current
        assert (
            len(state["proposals"]) == 1 and state["proposals"][0]["proposal"]["state"] == "pending"
        )
        assert not any(r["head"]["state"] == "published" for r in state["records"])
        assert provider.calls == 1
        # Scheduler must observe actual persisted Graph terminal state.
        client.portal.call(app.state.scheduler_coordinator.run_cycle)
        run = app.state.graph_repository.get_run(current["graph_run_id"])
        assert run.status.value == "completed" and run.workspace_or_target == str(tmp_path)
        nodes = app.state.graph_repository.list_node_runs(run.id)
        assert app.state.graph_repository.list_attempts(nodes[0].id)[0].result.value == "succeeded"
        empty = command(
            client, action="maintenance_create", project_id=project, model_profile_id=profile.id
        )["affected_ids"][0]
        empty, _ = wait_job(client, project, empty)
        assert (
            empty["state"] == "no_change" and empty["input_tokens"] == empty["output_tokens"] == 0
        )
        assert provider.calls == 1


def test_global_close_reopen_blocks_uncooperative_late_commit(tmp_path):
    app = create_app(tmp_path / "core.sqlite3")
    provider = ControlledProvider(mode="blocking")
    with TestClient(app) as client:
        project, install, profile = setup(client, app, tmp_path, provider)
        job = command(
            client, action="maintenance_create", project_id=project, model_profile_id=profile.id
        )["affected_ids"][0]
        assert provider.entered.wait(5)
        command(client, version="b2-3", action="memory_switch", enabled=False)
        command(client, version="b2-3", action="memory_switch", enabled=True)
        command(client, version="b2-3", action="plugin_enable", installation_id=install)
        provider.release.set()
        current, state = wait_job(client, project, job)
        assert current["state"] in {"cancelled", "dead_letter"}
        assert state["proposals"] == [] and current["processed_cursor"] == 0
        assert provider.calls == 1


def test_dead_letter_retry_and_attempt_budget_are_real_scheduler_state(tmp_path):
    app = create_app(tmp_path / "core.sqlite3")
    provider = ControlledProvider(mode="invalid")
    with TestClient(app) as client:
        project, _, profile = setup(client, app, tmp_path, provider)
        job = command(
            client,
            action="maintenance_create",
            project_id=project,
            model_profile_id=profile.id,
            max_attempts=2,
        )["affected_ids"][0]
        current, _ = wait_job(client, project, job, states={"dead_letter"})
        assert current["processed_cursor"] == 0 and provider.calls == 1
        provider.mode = "valid"
        command(client, action="maintenance_retry", project_id=project, job_id=job)
        current, state = wait_job(client, project, job, states={"succeeded", "dead_letter"})
        assert current["state"] == "succeeded", current
        assert (
            current["attempts"] == 2
            and current["input_tokens"] == 20
            and current["output_tokens"] == 16
        )
        assert len(state["proposals"]) == 1 and provider.calls == 2


def test_input_budget_rejects_before_provider(tmp_path):
    app = create_app(tmp_path / "core.sqlite3")
    provider = ControlledProvider()
    with TestClient(app) as client:
        project, _, profile = setup(client, app, tmp_path, provider, content="长来源边界" * 2200)
        job = command(
            client,
            action="maintenance_create",
            project_id=project,
            model_profile_id=profile.id,
            max_attempts=1,
        )["affected_ids"][0]
        current, state = wait_job(client, project, job)
        assert current["state"] == "dead_letter" and provider.calls == 0
        assert state["proposals"] == [] and current["processed_cursor"] == 0
