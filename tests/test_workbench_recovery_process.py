"""Kill a real Core process at model, tool and approval boundaries."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from operant.application.service import ApplicationService
from operant.domain.models import Budget, ModelProfile, RolePreset, ToolPolicy
from operant.domain.threads import ConversationThread
from operant.persistence.sqlite import (
    ActionOutcomeUnknownError,
    ConflictError,
    SQLiteStore,
)
from operant.providers.openai_compatible import OpenAICompatibleProvider

WORKER = Path(__file__).with_name("workbench_crash_worker.py")


def _port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_until(predicate, *, seconds: float = 12) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("Core recovery checkpoint was not reached")


def _start_core(
    *, db: Path, artifacts: Path, marker: Path, recipient: str, mode: str, log: Path
) -> tuple[subprocess.Popen[bytes], str]:
    port = _port()
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    output = log.open("wb")
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                str(WORKER),
                "--db",
                str(db),
                "--artifact-root",
                str(artifacts),
                "--marker",
                str(marker),
                "--recipient",
                recipient,
                "--mode",
                mode,
                "--port",
                str(port),
            ],
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
    finally:
        output.close()
    base = f"http://127.0.0.1:{port}"

    def ready() -> bool:
        if process.poll() is not None:
            raise AssertionError(f"Core exited before startup: {log.read_text()}")
        try:
            return (
                httpx.get(
                    f"{base}/v1/workbench/threads/{recipient}/children", timeout=0.3
                ).status_code
                == 200
            )
        except httpx.TransportError:
            return False

    _wait_until(ready)
    return process, base


def _stop_core(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


@pytest.mark.parametrize("boundary", ["model", "tool", "approval"])
def test_child_run_crash_requires_reconciliation_without_replay(
    tmp_path: Path, boundary: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    if boundary == "approval":
        subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
        (workspace / "change.txt").write_text("change\n", encoding="utf-8")
    db = tmp_path / "core.sqlite3"
    service = ApplicationService(
        SQLiteStore(db), OpenAICompatibleProvider(), artifact_root=tmp_path / "seed-artifacts"
    )
    service.initialize()
    profile = service.add_model_profile(
        ModelProfile(
            name="crash-test",
            model_id="local-test",
            base_url="https://example.invalid/v1",
            secret_ref="OPERANT_WORKBENCH_TEST_KEY",
        )
    )
    policy = (
        ToolPolicy(allowed_tools=("run_command",), command_execution=True)
        if boundary == "approval"
        else ToolPolicy(allowed_tools=("send_agent_message",))
        if boundary == "tool"
        else ToolPolicy()
    )
    role = service.create_role(
        RolePreset(
            name="crash-test",
            system_prompt="Finish the bounded task.",
            model_profile_id=profile.id,
            budget=Budget(max_turns=3),
            tool_policy=policy,
        )
    )
    parent = service.create_thread(ConversationThread(workspace_ref=str(workspace)))
    service.create_session(role.id, thread_id=parent.id)
    marker = tmp_path / "checkpoint.txt"
    process, base = _start_core(
        db=db,
        artifacts=tmp_path / "worker-artifacts",
        marker=marker,
        recipient=parent.id,
        mode=boundary,
        log=tmp_path / "worker.log",
    )
    child: dict[str, object]
    try:
        response = httpx.post(
            f"{base}/v1/workbench/threads/{parent.id}/children",
            json={"task": "crash boundary"},
            timeout=5,
        )
        assert response.status_code == 201, response.text
        child = response.json()
        child_id = str(child["thread_id"])
        session_id = str(child["session_id"])
        if boundary == "approval":
            _wait_until(lambda: bool(service.list_pending_approvals(session_id)))
        else:
            _wait_until(marker.exists)
        _stop_core(process)
        assert process.returncode != 0
        recovered, restart_base = _start_core(
            db=db,
            artifacts=tmp_path / "worker-artifacts",
            marker=tmp_path / "restart-marker.txt",
            recipient=parent.id,
            mode="complete",
            log=tmp_path / "restart.log",
        )
        try:

            def interrupted() -> bool:
                result = httpx.get(
                    f"{restart_base}/v1/workbench/threads/{parent.id}/children", timeout=2
                )
                assert result.status_code == 200, result.text
                return result.json()[0]["status"] == "interrupted"

            _wait_until(interrupted, seconds=8)
            assert not (tmp_path / "restart-marker.txt").exists()
            assert service.get_thread(child_id).status.value == "active"
            if boundary == "tool":
                with service.store._connect() as connection:
                    assert (
                        connection.execute(
                            "SELECT COUNT(*) FROM workbench_messages WHERE sender_thread_id=?",
                            (child_id,),
                        ).fetchone()[0]
                        == 1
                    )
                with pytest.raises(ActionOutcomeUnknownError):
                    service.admit_session_run(session_id)
            elif boundary == "approval":
                assert len(service.list_pending_approvals(session_id)) == 1
                with pytest.raises(ConflictError, match="pending durable approval"):
                    service.admit_session_run(session_id)
                staged = subprocess.run(
                    ["git", "diff", "--cached", "--name-only"],
                    cwd=workspace,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                assert staged.stdout == ""
        finally:
            _stop_core(recovered)
    finally:
        _stop_core(process)
