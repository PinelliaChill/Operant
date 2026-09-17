from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

from typer.testing import CliRunner

import operant.cli as cli
from operant.application.service import ApplicationService
from operant.application.workflow import WorkflowEvent
from operant.domain.models import ModelProfile, RolePreset
from operant.domain.workflow import WorkflowRun, WorkflowRunEvent, WorkflowRunStatus
from operant.persistence.sqlite import SQLiteStore
from operant.providers.openai_compatible import OpenAICompatibleProvider

runner = CliRunner()


def make_service(tmp_path: Path) -> ApplicationService:
    service = ApplicationService(
        SQLiteStore(tmp_path / "cli.sqlite3"),
        OpenAICompatibleProvider(),
    )
    service.initialize()
    profile = service.add_model_profile(
        ModelProfile(
            id="cli_model",
            name="cli-model",
            model_id="cli-model",
            base_url="https://relay.example.com/v1",
            secret_ref="OPERANT_TEST_KEY",
        )
    )
    service.create_role(
        RolePreset(
            id="role_memory_cli",
            name="Memory CLI Role",
            system_prompt="Manage project knowledge.",
            model_profile_id=profile.id,
            memory_scope="read: [project, episodic]; write: [project, episodic]",
        )
    )
    return service


def make_workflow(service: ApplicationService, tmp_path: Path, workflow_id: str) -> WorkflowRun:
    workflow = service.create_workflow_run(
        WorkflowRun(
            id=workflow_id,
            task="Inspect the calculator",
            workspace=str(tmp_path.resolve()),
            main_role_id="role_main",
            planner_role_id="role_planner",
            explorer_role_ids=("role_explorer",),
            coder_role_id="role_coder",
            reviewer_role_id="role_reviewer",
        )
    )
    service.update_workflow_run(
        workflow.id,
        status=WorkflowRunStatus.RUNNING,
    )
    service.append_workflow_event(
        WorkflowRunEvent(
            workflow_run_id=workflow.id,
            role="planner",
            event_type="agent.completed",
            payload={"content": "plan"},
        )
    )
    return workflow


def test_workflow_inspection_trace_and_cancel_commands(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = make_service(tmp_path)
    workflow = make_workflow(service, tmp_path, "workflow_cli")
    monkeypatch.setattr(cli, "_service", lambda: service)

    listed = runner.invoke(cli.app, ["workflow", "list"])
    assert listed.exit_code == 0
    assert workflow.id in listed.stdout

    shown = runner.invoke(cli.app, ["workflow", "show", workflow.id])
    assert shown.exit_code == 0
    assert json.loads(shown.stdout)["id"] == workflow.id

    events = runner.invoke(cli.app, ["workflow", "events", workflow.id])
    assert events.exit_code == 0
    assert '"event_type": "agent.completed"' in events.stdout

    trace = runner.invoke(cli.app, ["workflow", "trace", workflow.id])
    assert trace.exit_code == 0
    assert json.loads(trace.stdout)["workflow_run_id"] == workflow.id

    trace_jsonl = runner.invoke(cli.app, ["workflow", "trace", workflow.id, "--jsonl"])
    assert trace_jsonl.exit_code == 0
    jsonl_records = [json.loads(line) for line in trace_jsonl.stdout.splitlines() if line]
    assert jsonl_records[0]["record_type"] == "trace.workflow"
    assert jsonl_records[-1]["record_type"] == "trace.workflow_summary"

    cancelled = runner.invoke(cli.app, ["workflow", "cancel", workflow.id])
    assert cancelled.exit_code == 0
    assert "已取消" in cancelled.stdout
    assert service.get_workflow_run(workflow.id).status is WorkflowRunStatus.CANCELLED


def test_workflow_resume_passes_allow_coder_replay(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = make_service(tmp_path)
    workflow = make_workflow(service, tmp_path, "workflow_resume_cli")
    service.update_workflow_run(workflow.id, status=WorkflowRunStatus.INTERRUPTED)
    calls: list[tuple[str, bool]] = []

    class FakeWorkflow:
        def __init__(self, candidate_service: ApplicationService) -> None:
            assert candidate_service is service

        async def resume(
            self,
            workflow_run_id: str,
            *,
            allow_coder_replay: bool = False,
        ) -> AsyncIterator[WorkflowEvent]:
            calls.append((workflow_run_id, allow_coder_replay))
            yield WorkflowEvent(
                workflow_run_id=workflow_run_id,
                role="workflow",
                session_id="",
                event_type="workflow.completed",
            )

    monkeypatch.setattr(cli, "_service", lambda: service)
    monkeypatch.setattr(cli, "SequentialCodingWorkflow", FakeWorkflow)

    result = runner.invoke(
        cli.app,
        ["workflow", "resume", workflow.id, "--allow-coder-replay"],
    )

    assert result.exit_code == 0
    assert calls == [(workflow.id, True)]
    assert '"event_type": "workflow.completed"' in result.stdout


def test_legacy_memory_commands_require_formal_management(tmp_path: Path, monkeypatch) -> None:
    service = make_service(tmp_path)
    session = service.create_session("role_memory_cli")
    monkeypatch.setattr(cli, "_service", lambda: service)

    added = runner.invoke(
        cli.app,
        [
            "memory",
            "add",
            "--kind",
            "project",
            "--content",
            "Run pytest tests/test_calculator.py.",
            "--session-id",
            session.id,
            "--project-scope",
            "calculator",
            "--source-task",
            "Task A",
            "--confidence",
            "0.96",
        ],
    )
    assert added.exit_code == 2
    assert "Proposal/CAS" in added.output

    searched = runner.invoke(
        cli.app,
        [
            "memory",
            "search",
            "pytest",
            "--session-id",
            session.id,
            "--project-scope",
            "calculator",
        ],
    )
    assert searched.exit_code == 2
    assert "插件未安装或未选择" in searched.output

    removed_option = runner.invoke(
        cli.app,
        [
            "memory",
            "add",
            "--kind",
            "project",
            "--content",
            "legacy option",
            "--session-id",
            session.id,
            "--allow-conservative-activation",
        ],
    )
    assert removed_option.exit_code == 2
    assert "allow-conservative-activation" in removed_option.output

    confirmed = runner.invoke(
        cli.app,
        ["memory", "confirm", "legacy-memory", "--session-id", session.id],
    )
    assert confirmed.exit_code == 2
    assert "memory_confirm" in confirmed.output

    deactivated = runner.invoke(
        cli.app,
        ["memory", "deactivate", "legacy-memory", "--session-id", session.id],
    )
    assert deactivated.exit_code == 2
    assert "memory_deactivate" in deactivated.output
