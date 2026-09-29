"""Runnable H-07 file, diff and PTY scenarios."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import select
import signal
import subprocess
import threading
import time
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from starlette.websockets import WebSocketDisconnect

from operant.api import create_app
from operant.api_workbench_terminal import TerminalCreate, TerminalManager
from operant.application.security import PolicyEngine
from operant.domain.models import ModelProfile, RolePreset, ToolPolicy
from operant.domain.security import PolicyBundle, PolicyDecision
from operant.domain.threads import ConversationThread


def _scope(tmp_path: Path, *, allow_terminal: bool = False, command_execution: bool = True):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    engine = (
        PolicyEngine(
            PolicyBundle(
                bundle_id="test", version="test.v1", default_decision=PolicyDecision.ALLOW, rules=()
            )
        )
        if allow_terminal
        else None
    )
    app = create_app(tmp_path / "core.sqlite3", phase45_policy_engine=engine)
    service = app.state.operant_service
    profile = service.add_model_profile(
        ModelProfile(
            name="test",
            model_id="test",
            base_url="https://example.invalid/v1",
            secret_ref="TEST_WORKBENCH_KEY",
        )
    )
    role = service.create_role(
        RolePreset(
            name="terminal",
            system_prompt="test",
            model_profile_id=profile.id,
            tool_policy=ToolPolicy(
                allowed_tools=("read_file", "run_command"),
                command_execution=command_execution,
                workspace_write=True,
            ),
        )
    )
    thread = service.create_thread(ConversationThread(workspace_ref=str(workspace)))
    service.create_session(role.id, thread_id=thread.id)
    initialization, _ = service.initialize_workspace(workspace)
    return app, thread, initialization, workspace


def test_h07_preview_bounds_protected_paths_and_unicode(tmp_path: Path) -> None:
    app, _, initialization, workspace = _scope(tmp_path)
    (workspace / "notes.txt").write_text("你好世界\n", encoding="utf-8")
    (workspace / ".env").write_text("SECRET=hidden")
    (tmp_path / "outside.txt").write_text("outside")
    (workspace / "alias.txt").symlink_to(tmp_path / "outside.txt")
    with TestClient(app) as client:
        base = f"/v1/workspaces/{initialization.id}/file-content"
        good = client.get(base, params={"path": "notes.txt", "max_bytes": 4})
        assert good.status_code == 200
        body = good.json()
        assert body["content"] == "你"
        assert body["truncated"] is True
        assert body["hash_scope"] == "preview"
        assert body["content_hash"] == hashlib.sha256("你".encode()).hexdigest()
        for path in ("../outside.txt", ".env", "alias.txt", "/etc/passwd"):
            denied = client.get(base, params={"path": path})
            assert denied.status_code in {400, 403}
            assert "outside" not in denied.text and "hidden" not in denied.text
        (workspace / "binary.bin").write_bytes(b"a\x00b")
        binary = client.get(base, params={"path": "binary.bin"})
        assert binary.status_code == 415
        (workspace / "long.txt").write_text("中文" * 100_000, encoding="utf-8")
        bounded = client.get(base, params={"path": "long.txt", "max_bytes": 1024})
        assert bounded.status_code == 200
        assert bounded.json()["truncated"] is True
        assert len(bounded.json()["content"].encode()) <= 1024


def test_h07_existing_shell_history_cannot_be_previewed(tmp_path: Path) -> None:
    app, _, initialization, workspace = _scope(tmp_path)
    history = workspace / ".bash_history"
    history.write_text("private command marker\n", encoding="utf-8")
    with TestClient(app) as client:
        base = f"/v1/workspaces/{initialization.id}"
        for endpoint in ("file-content", "diff"):
            denied = client.get(f"{base}/{endpoint}", params={"path": history.name})
            assert denied.status_code == 403
            assert "private command marker" not in denied.text
    assert history.read_text(encoding="utf-8") == "private command marker\n"


def test_h07_diff_is_bounded_and_workspace_scoped(tmp_path: Path) -> None:
    app, _, initialization, workspace = _scope(tmp_path)
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    target = workspace / "code.py"
    target.write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(workspace), "add", "code.py"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(workspace),
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=Test",
            "commit",
            "-qm",
            "base",
        ],
        check=True,
    )
    target.write_text("value = 2\n", encoding="utf-8")
    with TestClient(app) as client:
        base = f"/v1/workspaces/{initialization.id}/diff"
        result = client.get(base, params={"path": "code.py"})
        assert result.status_code == 200
        assert "-value = 1" in result.json()["diff"]
        assert "+value = 2" in result.json()["diff"]
        limited = client.get(base, params={"path": "code.py", "max_bytes": 10})
        assert limited.status_code == 200 and limited.json()["truncated"] is True
        assert client.get(base, params={"path": "../outside.txt"}).status_code == 400


def test_h07_terminal_requires_policy_approval(tmp_path: Path) -> None:
    app, thread, _, _ = _scope(tmp_path)
    with TestClient(app) as client:
        result = client.post(
            f"/v1/workbench/threads/{thread.id}/terminals",
            json={"cols": 80, "rows": 24, "idempotency_key": "h07-approval"},
        )
        assert result.status_code == 409
        assert result.json()["error"]["code"] == "terminal_approval_required"
        assert result.json()["detail"]["approval_id"]
        assert not app.state.workbench_terminals.sessions


def test_h07_terminal_respects_role_command_switch(tmp_path: Path) -> None:
    app, thread, _, _ = _scope(tmp_path, allow_terminal=True, command_execution=False)
    with TestClient(app) as client:
        result = client.post(
            f"/v1/workbench/threads/{thread.id}/terminals",
            json={"cols": 80, "rows": 24, "idempotency_key": "h07-disabled"},
        )
        assert result.status_code == 403
        assert not app.state.workbench_terminals.sessions


def test_h07_terminal_io_token_and_disconnect_cleanup(tmp_path: Path) -> None:
    app, thread, _, _ = _scope(tmp_path, allow_terminal=True)
    with TestClient(app) as client:
        created = client.post(
            f"/v1/workbench/threads/{thread.id}/terminals",
            json={"cols": 80, "rows": 24, "idempotency_key": "h07-terminal"},
        )
        assert created.status_code == 200, created.text
        view = created.json()
        terminal_id, token = view["terminal_id"], view["stream_token"]
        assert app.state.workbench_terminals.sessions[terminal_id].token == token
        retry = client.post(
            f"/v1/workbench/threads/{thread.id}/terminals",
            json={"cols": 80, "rows": 24, "idempotency_key": "h07-terminal"},
        )
        assert retry.status_code == 200
        assert retry.json()["terminal_id"] == terminal_id
        assert len(app.state.workbench_terminals.sessions) == 1
        conflict = client.post(
            f"/v1/workbench/threads/{thread.id}/terminals",
            json={"cols": 81, "rows": 24, "idempotency_key": "h07-terminal"},
        )
        assert conflict.status_code == 409
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(
                f"/v1/workbench/terminals/{terminal_id}/stream",
                subprotocols=["operant.terminal.v1", f"operant.token.{token}"],
                headers={"origin": "https://evil.example"},
            ),
        ):
            pass
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(
                f"/v1/workbench/terminals/{terminal_id}/stream",
                subprotocols=["operant.terminal.v1", "operant.token.wrong"],
            ),
        ):
            pass
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(
                f"/v1/workbench/terminals/{terminal_id}/stream?token={token}",
                subprotocols=["operant.terminal.v1", f"operant.token.{token}"],
            ),
        ):
            pass
        assert app.state.workbench_terminals.sessions[terminal_id].token == token
        with client.websocket_connect(
            f"/v1/workbench/terminals/{terminal_id}/stream",
            subprotocols=["operant.terminal.v1", f"operant.token.{token}"],
        ) as websocket:
            websocket.send_json({"type": "input", "data": "printf 'h07-ready\\n'\n"})
            seen = ""
            for _ in range(12):
                frame = websocket.receive_json()
                if frame["type"] == "output":
                    seen += frame["data"]
                if "h07-ready" in seen:
                    break
            assert "h07-ready" in seen
            with (
                pytest.raises(WebSocketDisconnect),
                client.websocket_connect(
                    f"/v1/workbench/terminals/{terminal_id}/stream",
                    subprotocols=["operant.terminal.v1", f"operant.token.{token}"],
                ),
            ):
                pass
        current = client.get(f"/v1/workbench/terminals/{terminal_id}")
        assert current.status_code == 200
        assert current.json()["status"] in {"terminated", "exited"}
        assert client.delete(f"/v1/workbench/terminals/{terminal_id}").status_code == 200


def test_h07_cancelling_thread_reaps_terminal(tmp_path: Path) -> None:
    app, thread, _, _ = _scope(tmp_path, allow_terminal=True)
    with TestClient(app) as client:
        created = client.post(
            f"/v1/workbench/threads/{thread.id}/terminals",
            json={"cols": 80, "rows": 24, "idempotency_key": "h07-cancel"},
        )
        assert created.status_code == 200
        terminal_id = created.json()["terminal_id"]
        item = app.state.workbench_terminals.sessions[terminal_id]
        assert item.process.poll() is None
        assert item.process.pid != os.getpgrp()
        app.state.operant_service.set_thread_status(thread.id, "cancelled")
        assert item.process.poll() is not None
        assert client.get(f"/v1/workbench/terminals/{terminal_id}").json()["status"] == "terminated"
        assert client.delete(f"/v1/workbench/terminals/{terminal_id}").status_code == 200


def test_h07_close_reaps_short_background_job(tmp_path: Path) -> None:
    app, thread, _, _ = _scope(tmp_path, allow_terminal=True)
    with TestClient(app) as client:
        created = client.post(
            f"/v1/workbench/threads/{thread.id}/terminals",
            json={"cols": 80, "rows": 24, "idempotency_key": "h07-child-reap"},
        )
        assert created.status_code == 200
        terminal_id = created.json()["terminal_id"]
        item = app.state.workbench_terminals.sessions[terminal_id]
        child_pid: int | None = None
        os.write(item.master_fd, b"sleep 5 & echo H07PID:$!\n")
        seen = ""
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if not select.select([item.master_fd], [], [], 0.2)[0]:
                continue
            seen += os.read(item.master_fd, 4096).decode("utf-8", "replace")
            match = re.search(r"H07PID:(\d+)", seen)
            if match:
                child_pid = int(match.group(1))
                break
        assert child_pid is not None
        assert child_pid != os.getpid()
        assert os.getpgid(child_pid) == item.process.pid
        assert client.delete(f"/v1/workbench/terminals/{terminal_id}").status_code == 200
        try:
            for _ in range(10):
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                raise AssertionError("background PTY child survived terminal close")
        finally:
            # The bounded test child is exact and known; do not leave it running.
            with contextlib.suppress(ProcessLookupError):
                os.kill(child_pid, signal.SIGKILL)


def test_h07_shell_exit_reaps_background_job(tmp_path: Path) -> None:
    app, thread, _, _ = _scope(tmp_path, allow_terminal=True)
    with TestClient(app) as client:
        created = client.post(
            f"/v1/workbench/threads/{thread.id}/terminals",
            json={"cols": 80, "rows": 24, "idempotency_key": "h07-shell-exit"},
        )
        assert created.status_code == 200
        terminal_id = created.json()["terminal_id"]
        token = created.json()["stream_token"]
        child_pid: int | None = None
        with client.websocket_connect(
            f"/v1/workbench/terminals/{terminal_id}/stream",
            subprotocols=["operant.terminal.v1", f"operant.token.{token}"],
        ) as websocket:
            websocket.send_json({"type": "input", "data": "sleep 5 & echo H07PID:$!\nexit\n"})
            seen = ""
            for _ in range(25):
                frame = websocket.receive_json()
                if frame["type"] == "output":
                    seen += frame["data"]
                    match = re.search(r"H07PID:(\d+)", seen)
                    if match:
                        child_pid = int(match.group(1))
                if frame["type"] == "exit":
                    break
            else:
                raise AssertionError("terminal did not report shell exit")
        assert child_pid is not None
        assert client.get(f"/v1/workbench/terminals/{terminal_id}").json()["status"] == "exited"
        try:
            for _ in range(10):
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                raise AssertionError("background job survived shell exit")
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.kill(child_pid, signal.SIGKILL)


def test_h07_terminal_exit_does_not_write_workspace_history(tmp_path: Path) -> None:
    app, thread, _, workspace = _scope(tmp_path, allow_terminal=True)
    with TestClient(app) as client:
        created = client.post(
            f"/v1/workbench/threads/{thread.id}/terminals",
            json={"cols": 80, "rows": 24, "idempotency_key": "h07-no-history"},
        )
        assert created.status_code == 200
        terminal_id = created.json()["terminal_id"]
        item = app.state.workbench_terminals.sessions[terminal_id]
        marker = workspace / "command-ran.txt"
        try:
            os.write(item.master_fd, b"printf ran > command-ran.txt\n")
            deadline = time.monotonic() + 8
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert marker.read_text(encoding="utf-8") == "ran"
            os.write(item.master_fd, b"exit\n")
            item.process.wait(timeout=8)
            app.state.workbench_terminals.exit(item)
        finally:
            if item.process.poll() is None:
                app.state.workbench_terminals.stop(terminal_id)
    assert not any(
        (workspace / name).exists() for name in (".bash_history", ".sh_history", ".history")
    )


def test_h07_unconfirmed_cleanup_blocks_same_thread_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, thread, _, _ = _scope(tmp_path, allow_terminal=True)
    with TestClient(app) as client:
        created = client.post(
            f"/v1/workbench/threads/{thread.id}/terminals",
            json={"cols": 80, "rows": 24, "idempotency_key": "h07-unknown"},
        )
        assert created.status_code == 200
        terminal_id = created.json()["terminal_id"]

        def unavailable(_pid: int) -> list[tuple[int, int]]:
            raise OSError("injected enumeration failure")

        monkeypatch.setattr("operant.api_workbench_terminal._descendants", unavailable)
        closed = client.delete(f"/v1/workbench/terminals/{terminal_id}")
        assert closed.status_code == 200
        assert closed.json()["status"] == "cleanup_unknown"
        retried = client.post(
            f"/v1/workbench/threads/{thread.id}/terminals",
            json={"cols": 80, "rows": 24, "idempotency_key": "h07-after-unknown"},
        )
        assert retried.status_code == 409
        assert retried.json()["error"]["code"] == "terminal_cleanup_unknown"
        with app.state.operant_service.store._connect() as connection:
            audit = connection.execute(
                "SELECT 1 FROM security_audit_events "
                "WHERE event_type='terminal.cleanup_unknown' "
                "AND json_extract(detail, '$.thread_id') = ?",
                (thread.id,),
            ).fetchone()
        assert audit is not None
    restarted = create_app(
        tmp_path / "core.sqlite3",
        phase45_policy_engine=PolicyEngine(
            PolicyBundle(
                bundle_id="test",
                version="test.v1",
                default_decision=PolicyDecision.ALLOW,
                rules=(),
            )
        ),
    )
    with TestClient(restarted) as client:
        denied = client.post(
            f"/v1/workbench/threads/{thread.id}/terminals",
            json={"cols": 80, "rows": 24, "idempotency_key": "h07-after-restart"},
        )
        assert denied.status_code == 409
        assert denied.json()["error"]["code"] == "terminal_cleanup_unknown"


@pytest.mark.asyncio
async def test_h07_ready_wait_does_not_block_health_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, thread, _, _ = _scope(tmp_path, allow_terminal=True)
    entered = threading.Event()
    release = threading.Event()
    original = TerminalManager._spawn_shell

    def delayed(root: str, identity: tuple[int, int], cols: int, rows: int):
        entered.set()
        if not release.wait(3):
            raise TimeoutError("test did not release terminal startup")
        return original(root, identity, cols, rows)

    monkeypatch.setattr(TerminalManager, "_spawn_shell", staticmethod(delayed))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        create_task = asyncio.create_task(
            client.post(
                f"/v1/workbench/threads/{thread.id}/terminals",
                json={"cols": 80, "rows": 24, "idempotency_key": "h07-concurrent"},
            )
        )
        assert await asyncio.to_thread(entered.wait, 2)
        health = await asyncio.wait_for(client.get("/healthz"), timeout=0.5)
        assert health.status_code == 200
        release.set()
        created = await create_task
        assert created.status_code == 200
        await client.delete(f"/v1/workbench/terminals/{created.json()['terminal_id']}")
    app.state.workbench_terminals.close()


@pytest.mark.asyncio
async def test_h07_cancelled_create_reaps_unpublished_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, thread, _, _ = _scope(tmp_path, allow_terminal=True)
    manager = app.state.workbench_terminals
    entered = threading.Event()
    release = threading.Event()
    original = TerminalManager._spawn_shell

    def delayed(root: str, identity: tuple[int, int], cols: int, rows: int):
        entered.set()
        if not release.wait(3):
            raise TimeoutError("test did not release terminal startup")
        return original(root, identity, cols, rows)

    monkeypatch.setattr(TerminalManager, "_spawn_shell", staticmethod(delayed))
    task = asyncio.create_task(
        manager.create(
            thread.id,
            TerminalCreate(cols=80, rows=24, idempotency_key="h07-cancel-create"),
        )
    )
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(item.process.poll() is not None for item in manager.sessions.values())
    assert not any(item.status in {"waiting", "running"} for item in manager.sessions.values())
    manager.close()


@pytest.mark.asyncio
async def test_h07_thread_cancelled_during_ready_wait_cannot_publish_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, thread, _, _ = _scope(tmp_path, allow_terminal=True)
    manager = app.state.workbench_terminals
    entered = threading.Event()
    release = threading.Event()
    original = TerminalManager._spawn_shell

    def delayed(root: str, identity: tuple[int, int], cols: int, rows: int):
        entered.set()
        if not release.wait(3):
            raise TimeoutError("test did not release terminal startup")
        return original(root, identity, cols, rows)

    monkeypatch.setattr(TerminalManager, "_spawn_shell", staticmethod(delayed))
    task = asyncio.create_task(
        manager.create(
            thread.id,
            TerminalCreate(cols=80, rows=24, idempotency_key="h07-cancel-during-start"),
        )
    )
    assert await asyncio.to_thread(entered.wait, 2)
    app.state.operant_service.set_thread_status(thread.id, "cancelled")
    release.set()
    with pytest.raises(HTTPException) as failure:
        await task
    assert failure.value.status_code == 409
    assert all(item.process.poll() is not None for item in manager.sessions.values())
    assert not any(item.status in {"waiting", "running"} for item in manager.sessions.values())
    manager.close()
