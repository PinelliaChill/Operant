"""File/Git observations become durable, fenced Graph run requests."""

from __future__ import annotations

import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_h06_scheduler_hook import _allow_policy, _setup

from operant.api import create_app
from operant.application.scheduler import SchedulerConflictError
from operant.domain.scheduler import ScheduleDefinition, ScheduleStatus, TriggerKind
from operant.runtime.scheduler_watch import SchedulerWatchService, WatchProbeError, probe


def _watch_schedule(workspace: Path, path: Path, kind: str, name: str) -> ScheduleDefinition:
    return ScheduleDefinition(
        id=name,
        name=name,
        trigger_kind=TriggerKind.HOOK,
        hook_event_type=kind,
        watch_path=str(path),
        timezone_name="UTC",
        workflow_id="hook-graph",
        workflow_version=1,
        workflow_input={"workspace_or_target": str(workspace)},
        max_attempts=1,
    )


def _run_to_terminal(client: TestClient, app: Any, request_id: str) -> Any:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        client.portal.call(app.state.scheduler_coordinator.run_cycle)
        request = app.state.scheduler_store.get_request(request_id)
        if request.status.value in {
            "succeeded",
            "dead_letter",
            "cancelled",
            "manual_reconcile_required",
        }:
            return request
        time.sleep(0.05)
    raise AssertionError(f"request did not terminate: {request.status.value}")


def test_file_watch_persists_baseline_deduplicates_and_dispatches(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    watched = workspace / "task.txt"
    watched.write_text("first", encoding="utf-8")
    database = tmp_path / "watch.sqlite3"
    app = create_app(database, phase45_policy_engine=_allow_policy())
    with TestClient(app) as client:
        provider = _setup(client, app, workspace)
        client.portal.call(app.state.scheduler_coordinator.stop)
        schedule = _watch_schedule(workspace, watched, "file.changed", "file-watch")
        created = client.post("/v1/schedules", json=schedule.model_dump(mode="json"))
        assert created.status_code == 201, created.text
        client.portal.call(app.state.scheduler_coordinator.run_cycle)
        status = client.get("/v1/schedules/file-watch/watch-status")
        assert status.status_code == 200, status.text
        assert status.json()["initialized"] is True
        assert status.json()["generation"] == 0
        assert app.state.scheduler_store.list_requests() == ()

        watched.write_text("second and longer", encoding="utf-8")
        client.portal.call(app.state.scheduler_coordinator.run_cycle)
        requests = app.state.scheduler_store.list_requests()
        assert len(requests) == 1
        request = _run_to_terminal(client, app, requests[0].id)
        assert request.status.value == "succeeded"
        assert request.workflow_run_id is not None
        graph_run = app.state.graph_repository.get_run(request.workflow_run_id)
        assert graph_run.status.value == "completed"
        assert provider.calls == 1
        client.portal.call(app.state.scheduler_coordinator.run_cycle)
        assert len(app.state.scheduler_store.list_requests()) == 1

        paused = client.post(
            "/v1/schedules/file-watch/status",
            json={"status": "paused", "expected_version": 1},
        )
        assert paused.status_code == 200
        watched.write_text("third and much longer", encoding="utf-8")
        client.portal.call(app.state.scheduler_coordinator.run_cycle)
        assert len(app.state.scheduler_store.list_requests()) == 1

    # The baseline is durable; a new Core catches the change made while paused.
    restarted = create_app(database, phase45_policy_engine=_allow_policy())
    restarted.state.operant_service.provider = provider
    with TestClient(restarted) as client:
        client.portal.call(restarted.state.scheduler_coordinator.stop)
        resumed = client.post(
            "/v1/schedules/file-watch/status",
            json={"status": "enabled", "expected_version": 1},
        )
        assert resumed.status_code == 200
        client.portal.call(restarted.state.scheduler_coordinator.run_cycle)
        requests = restarted.state.scheduler_store.list_requests()
        assert len(requests) == 2
        assert requests[0].id != requests[1].id
        assert _run_to_terminal(client, restarted, requests[0].id).status.value == "succeeded"
        assert client.get("/v1/schedules/file-watch/watch-status").json()["generation"] == 2


def test_watch_symlink_error_is_persistent_and_never_dispatches(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = workspace / "linked.txt"
    link.symlink_to(outside)
    schedule = _watch_schedule(workspace, link, "file.changed", "symlink-watch")
    try:
        probe(schedule)
    except WatchProbeError as exc:
        assert exc.code == "watch.symlink_rejected"
    else:
        raise AssertionError("symlink was accepted")

    app = create_app(tmp_path / "error.sqlite3", phase45_policy_engine=_allow_policy())
    with TestClient(app) as client:
        _setup(client, app, workspace)
        client.portal.call(app.state.scheduler_coordinator.stop)
        created = client.post("/v1/schedules", json=schedule.model_dump(mode="json"))
        assert created.status_code == 201
        client.portal.call(app.state.scheduler_coordinator.run_cycle)
        status = client.get("/v1/schedules/symlink-watch/watch-status")
        assert status.status_code == 200
        assert status.json()["error_code"] == "watch.symlink_rejected"
        assert status.json()["initialized"] is False
        assert app.state.scheduler_store.list_requests() == ()


def test_git_head_probe_changes_only_after_commit(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    repository = workspace / "repo"
    repository.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    tracked = repository / "task.txt"
    tracked.write_text("first", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "task.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "first",
        ],
        check=True,
    )
    schedule = _watch_schedule(workspace, repository, "git.head.changed", "git-watch")
    first = probe(schedule)
    app = create_app(tmp_path / "git.sqlite3", phase45_policy_engine=_allow_policy())
    with TestClient(app) as client:
        provider = _setup(client, app, workspace)
        client.portal.call(app.state.scheduler_coordinator.stop)
        created = client.post("/v1/schedules", json=schedule.model_dump(mode="json"))
        assert created.status_code == 201, created.text
        client.portal.call(app.state.scheduler_coordinator.run_cycle)
        assert app.state.scheduler_store.list_requests() == ()
        tracked.write_text("uncommitted change", encoding="utf-8")
        assert probe(schedule) == first
        client.portal.call(app.state.scheduler_coordinator.run_cycle)
        assert app.state.scheduler_store.list_requests() == ()
        subprocess.run(["git", "-C", str(repository), "add", "task.txt"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-qm",
                "second",
            ],
            check=True,
        )
        assert probe(schedule) != first
        client.portal.call(app.state.scheduler_coordinator.run_cycle)
        requests = app.state.scheduler_store.list_requests()
        assert len(requests) == 1
        assert _run_to_terminal(client, app, requests[0].id).status.value == "succeeded"
        assert provider.calls == 1


def test_slow_watch_probe_does_not_block_api_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    watched = workspace / "task.txt"
    watched.write_text("initial", encoding="utf-8")
    app = create_app(tmp_path / "slow.sqlite3", phase45_policy_engine=_allow_policy())
    with TestClient(app) as client:
        _setup(client, app, workspace)
        client.portal.call(app.state.scheduler_coordinator.stop)
        app.state.scheduler_coordinator.stop_event.clear()
        schedule = _watch_schedule(workspace, watched, "file.changed", "slow-watch")
        assert (
            client.post("/v1/schedules", json=schedule.model_dump(mode="json")).status_code == 201
        )
        entered = threading.Event()

        def slow_probe(_schedule: ScheduleDefinition) -> str:
            entered.set()
            time.sleep(0.7)
            return "a" * 64

        monkeypatch.setattr("operant.runtime.scheduler_watch.probe", slow_probe)
        errors: list[BaseException] = []

        def run_cycle() -> None:
            try:
                client.portal.call(app.state.scheduler_coordinator.run_cycle_async)
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=run_cycle)
        thread.start()
        assert entered.wait(timeout=3)
        started = time.monotonic()
        response = client.get("/v1/schedules")
        elapsed = time.monotonic() - started
        app.state.scheduler_coordinator.stop_event.set()
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert errors == []
        assert response.status_code == 200
        assert elapsed < 0.45
        assert app.state.scheduler_store.get_watch_status("slow-watch", 1)["initialized"] is False


def test_expired_leader_cannot_commit_observation(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    watched = workspace / "task.txt"
    watched.write_text("initial", encoding="utf-8")
    app = create_app(tmp_path / "lease.sqlite3", phase45_policy_engine=_allow_policy())
    with TestClient(app) as client:
        _setup(client, app, workspace)
        client.portal.call(app.state.scheduler_coordinator.stop)
        schedule = _watch_schedule(workspace, watched, "file.changed", "lease-watch")
        assert (
            client.post("/v1/schedules", json=schedule.model_dump(mode="json")).status_code == 201
        )
        store = app.state.scheduler_store
        now = datetime.now(timezone.utc)
        leader = store.acquire_authority(
            "scheduler_leader", owner="expired-test", ttl_seconds=2, now=now
        )
        with pytest.raises(SchedulerConflictError, match="expired"):
            store.observe_watch(
                schedule,
                fingerprint="a" * 64,
                error_code=None,
                leader_lease=leader,
                observed_at=now + timedelta(seconds=3),
            )
        assert store.get_watch_status(schedule.id, schedule.version)["initialized"] is False
        assert store.list_requests() == ()


def test_probe_uses_time_after_observation_for_lease_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    watched = workspace / "task.txt"
    watched.write_text("initial", encoding="utf-8")
    app = create_app(tmp_path / "probe-lease.sqlite3", phase45_policy_engine=_allow_policy())
    with TestClient(app) as client:
        _setup(client, app, workspace)
        client.portal.call(app.state.scheduler_coordinator.stop)
        schedule = _watch_schedule(workspace, watched, "file.changed", "probe-lease-watch")
        assert (
            client.post("/v1/schedules", json=schedule.model_dump(mode="json")).status_code == 201
        )
        store = app.state.scheduler_store
        before_probe = datetime.now(timezone.utc)
        leader = store.acquire_authority(
            "scheduler_leader", owner="probe-lease-test", ttl_seconds=2, now=before_probe
        )

        class AfterSlowProbe:
            @staticmethod
            def now(_zone: object) -> datetime:
                return before_probe + timedelta(seconds=3)

        monkeypatch.setattr("operant.runtime.scheduler_watch.datetime", AfterSlowProbe)
        result = SchedulerWatchService(store).materialize_all(leader_lease=leader, now=before_probe)
        assert result == ()
        assert store.get_watch_status(schedule.id, schedule.version)["initialized"] is False
        assert store.list_requests() == ()


def test_paused_watch_keeps_queued_request_until_resume_and_cancel_stops_queue(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    watched = workspace / "task.txt"
    watched.write_text("initial", encoding="utf-8")
    schedule = _watch_schedule(workspace, watched, "file.changed", "pause-watch")
    store = create_app(tmp_path / "pause.sqlite3").state.scheduler_store
    store.put_schedule(schedule)
    leader = store.acquire_authority("scheduler_leader", owner="pause-test", ttl_seconds=30)
    writer = store.acquire_authority("runtime_writer", owner="pause-test", ttl_seconds=30)
    now = datetime.now(timezone.utc)
    assert (
        store.observe_watch(
            schedule,
            fingerprint=probe(schedule),
            error_code=None,
            leader_lease=leader,
            observed_at=now,
        )
        is None
    )
    watched.write_text("changed and longer", encoding="utf-8")
    queued = store.observe_watch(
        schedule,
        fingerprint=probe(schedule),
        error_code=None,
        leader_lease=leader,
        observed_at=datetime.now(timezone.utc),
    )
    assert queued is not None

    store.update_schedule_status(schedule.id, ScheduleStatus.PAUSED, expected_version=1)
    assert store.claim_due(writer, owner="pause-test", ttl_seconds=30) is None
    assert store.get_request(queued.id).status.value == "queued"

    store.update_schedule_status(schedule.id, ScheduleStatus.ENABLED, expected_version=1)
    claimed = store.claim_due(writer, owner="pause-test", ttl_seconds=30)
    assert claimed is not None and claimed[0].id == queued.id
    assert store.claim_due(writer, owner="pause-test", ttl_seconds=30) is None

    queued_after_resume = store.enqueue_hook(
        schedule,
        idempotency_key="watch:pause-watch:v1:g2:later",
        requested_at=datetime.now(timezone.utc),
    )
    store.update_schedule_status(schedule.id, ScheduleStatus.CANCELLED, expected_version=1)
    assert store.get_request(queued_after_resume.id).status.value == "cancelled"
    assert store.claim_due(writer, owner="pause-test", ttl_seconds=30) is None
