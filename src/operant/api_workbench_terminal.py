"""Local, capability-gated interactive PTY sessions for the workbench."""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import fcntl
import hmac
import os
import pty
import secrets
import select
import signal
import struct
import subprocess
import sys
import termios
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from operant.api_workbench_context import thread_session
from operant.application.client_projection import _open_directory
from operant.application.phase45_gateway import Phase45ActionGateway
from operant.application.service import ApplicationService
from operant.domain.models import CommandRunnerType, new_id
from operant.domain.security import Capability, PolicyDecision, SecurityAuditEvent
from operant.domain.threads import ThreadStatus
from operant.persistence.sqlite import IdempotencyConflictError, NotFoundError

PROTOCOL = "operant.terminal.v1"
MAX_TERMINALS = 4
MAX_INPUT_CHARS = 16_384
MAX_OUTPUT_QUEUE = 64
TERMINAL_TIMEOUT_SECONDS = 900
TOKEN_TIMEOUT_SECONDS = 30


class TerminalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cols: int = Field(default=80, ge=20, le=300)
    rows: int = Field(default=24, ge=5, le=150)
    idempotency_key: str = Field(min_length=1, max_length=300)


class TerminalView(BaseModel):
    terminal_id: str
    thread_id: str
    status: Literal["waiting", "running", "exited", "terminated", "cleanup_unknown"]
    cols: int
    rows: int
    exit_code: int | None = None
    stream_token: str | None = None


@dataclass
class _Terminal:
    id: str
    thread_id: str
    session_id: str
    process: subprocess.Popen[bytes]
    master_fd: int
    token: str
    cols: int
    rows: int
    created_at: float
    action_hash: str
    status: Literal["waiting", "running", "exited", "terminated", "cleanup_unknown"] = "waiting"
    exit_code: int | None = None
    finished_at: float | None = None
    timer: asyncio.Task[None] | None = None

    def view(self, *, include_token: bool = False) -> TerminalView:
        return TerminalView(
            terminal_id=self.id,
            thread_id=self.thread_id,
            status=self.status,
            cols=self.cols,
            rows=self.rows,
            exit_code=self.exit_code,
            stream_token=self.token if include_token else None,
        )


class TerminalManager:
    def __init__(self, service: ApplicationService, gateway: Phase45ActionGateway) -> None:
        self.service = service
        self.gateway = gateway
        self.sessions: dict[str, _Terminal] = {}
        self.idempotency: dict[str, tuple[str, int, int, str]] = {}
        self._cancelled_sessions: set[str] = set()
        self._create_lock = asyncio.Lock()

    def prune(self) -> None:
        finished = sorted(
            (item for item in self.sessions.values() if item.finished_at is not None),
            key=lambda item: item.finished_at or 0,
        )
        excess = max(0, len(finished) - 256)
        old = time.monotonic() - 3600
        evicted = {
            item.id
            for index, item in enumerate(finished)
            if index < excess or (item.finished_at or 0) < old
        }
        for terminal_id in evicted:
            self.sessions.pop(terminal_id, None)
        self.idempotency = {
            key: value for key, value in self.idempotency.items() if value[3] not in evicted
        }

    def _get(self, terminal_id: str) -> _Terminal:
        try:
            return self.sessions[terminal_id]
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="terminal not found") from exc

    async def create(self, thread_id: str, body: TerminalCreate) -> TerminalView:
        async with self._create_lock:
            return await self._create_locked(thread_id, body)

    async def _create_locked(self, thread_id: str, body: TerminalCreate) -> TerminalView:
        self.prune()
        with self.service.store._connect() as connection:
            unresolved = connection.execute(
                "SELECT 1 FROM security_audit_events "
                "WHERE event_type='terminal.cleanup_unknown' "
                "AND json_extract(detail, '$.thread_id') = ? LIMIT 1",
                (thread_id,),
            ).fetchone()
        if unresolved is not None or any(
            item.thread_id == thread_id and item.status == "cleanup_unknown"
            for item in self.sessions.values()
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "terminal_cleanup_unknown",
                    "message": "terminal cleanup needs manual reconciliation",
                },
            )
        previous = self.idempotency.get(body.idempotency_key)
        if previous is not None:
            if previous[:3] != (thread_id, body.cols, body.rows):
                raise HTTPException(status_code=409, detail="terminal idempotency key conflict")
            item = self._get(previous[3])
            return item.view(include_token=item.status == "waiting")
        try:
            session = thread_session(self.service, thread_id)
            thread = self.service.get_thread(thread_id)
        except (NotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="ordinary conversation not found") from exc
        policy = session.role_snapshot.tool_policy
        if session.id in self._cancelled_sessions:
            raise HTTPException(status_code=409, detail="conversation session was cancelled")
        if "run_command" not in policy.allowed_tools or not policy.command_execution:
            raise HTTPException(status_code=403, detail="session role cannot run commands")
        if policy.command_execution_policy.runner is not CommandRunnerType.HOST:
            raise HTTPException(
                status_code=403, detail="session role is not configured for Host execution"
            )
        if not thread.workspace_ref:
            raise HTTPException(status_code=403, detail="conversation has no workspace")
        if thread.status is not ThreadStatus.ACTIVE:
            raise HTTPException(status_code=409, detail="conversation is no longer active")
        active = sum(item.status in {"waiting", "running"} for item in self.sessions.values())
        if active >= MAX_TERMINALS:
            raise HTTPException(status_code=429, detail="too many active terminals")
        root = thread.workspace_ref
        # Reject replaced or symlinked registered roots before policy and spawn.
        root_fd, _, root_identity = _open_directory(root, ())
        os.close(root_fd)
        shell = "/bin/sh"
        try:
            action, result, _ = self.gateway.guard(
                tool="workbench_terminal",
                operation="create",
                target_id=thread_id,
                arguments={
                    "argv": [shell, "-i"],
                    "cwd": root,
                    "cols": body.cols,
                    "rows": body.rows,
                },
                capabilities=(Capability.PROCESS_EXEC,),
                idempotency_key=body.idempotency_key,
                workspace=root,
                sandbox_profile="trusted-host-pty",
                network_profile="host",
            )
        except IdempotencyConflictError as exc:
            raise HTTPException(
                status_code=409, detail="terminal idempotency key conflict"
            ) from exc
        audit = self.gateway.repository.list_security_audit(action.action_hash, limit=500)
        if any(event.event_type == "terminal.spawn_reserved" for event in audit):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "terminal_outcome_unknown",
                    "message": "earlier terminal creation needs manual reconciliation",
                },
            )
        if result.decision.value == "ask":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "terminal_approval_required",
                    "message": "terminal creation requires approval",
                    "approval_id": result.approval_id,
                    "idempotency_key": body.idempotency_key,
                },
            )
        if result.decision.value != "allow" or result.lease is None:
            raise HTTPException(status_code=403, detail="terminal creation denied by policy")
        self.gateway.consume(result.lease, action)
        self.gateway.repository.append_security_audit(
            SecurityAuditEvent(
                action_hash=action.action_hash,
                principal=self.gateway.principal,
                event_type="terminal.spawn_reserved",
                decision=PolicyDecision.ALLOW,
                detail={"thread_id": thread_id},
            )
        )
        spawn_task = asyncio.create_task(
            asyncio.to_thread(self._spawn_shell, root, root_identity, body.cols, body.rows)
        )
        try:
            process, master = await asyncio.shield(spawn_task)
        except asyncio.CancelledError:
            # to_thread keeps running after request cancellation. Wait for its
            # bounded READY handshake, then reap the exact unpublished child.
            try:
                process, master = await asyncio.shield(spawn_task)
            except Exception:
                raise asyncio.CancelledError from None
            self._reap_unpublished(
                process,
                master,
                thread_id=thread_id,
                session_id=session.id,
                action_hash=action.action_hash,
                cols=body.cols,
                rows=body.rows,
            )
            raise
        terminal_id = new_id("terminal")
        token = secrets.token_urlsafe(32)
        item = _Terminal(
            id=terminal_id,
            thread_id=thread_id,
            session_id=session.id,
            process=process,
            master_fd=master,
            token=token,
            cols=body.cols,
            rows=body.rows,
            created_at=time.monotonic(),
            action_hash=action.action_hash,
        )
        self.sessions[terminal_id] = item
        if (
            self.service.get_thread(thread_id).status is not ThreadStatus.ACTIVE
            or session.id in self._cancelled_sessions
        ):
            self.stop(terminal_id)
            if item.status != "cleanup_unknown":
                self.sessions.pop(terminal_id, None)
            raise HTTPException(
                status_code=409, detail="conversation was cancelled during terminal startup"
            )
        self.idempotency[body.idempotency_key] = (thread_id, body.cols, body.rows, terminal_id)
        item.timer = asyncio.create_task(self._expire(item))
        self._audit(item, "terminal.started")
        return item.view(include_token=True)

    def _reap_unpublished(
        self,
        process: subprocess.Popen[bytes],
        master: int,
        *,
        thread_id: str,
        session_id: str,
        action_hash: str,
        cols: int,
        rows: int,
    ) -> None:
        item = _Terminal(
            id=new_id("terminal"),
            thread_id=thread_id,
            session_id=session_id,
            process=process,
            master_fd=master,
            token="",
            cols=cols,
            rows=rows,
            created_at=time.monotonic(),
            action_hash=action_hash,
        )
        self.sessions[item.id] = item
        self.stop(item.id)
        if item.status != "cleanup_unknown":
            self.sessions.pop(item.id, None)

    @staticmethod
    def _spawn_shell(
        root: str, root_identity: tuple[int, int], cols: int, rows: int
    ) -> tuple[subprocess.Popen[bytes], int]:
        bound_fd, _, rebound_identity = _open_directory(root, ())
        try:
            master, slave = pty.openpty()
        except BaseException:
            os.close(bound_fd)
            raise
        process: subprocess.Popen[bytes] | None = None
        try:
            if rebound_identity != root_identity:
                raise HTTPException(status_code=409, detail="workspace root changed")
            _resize(slave, cols, rows)
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    "import fcntl,os,sys,termios; os.fchdir(int(sys.argv[1])); "
                    "fcntl.ioctl(0,termios.TIOCSCTTY,0); "
                    "os.execv('/bin/sh',['/bin/sh','-i'])",
                    str(bound_fd),
                ],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                cwd="/",
                pass_fds=(bound_fd,),
                env={
                    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                    "HOME": root,
                    "HISTFILE": os.devnull,
                    "HISTSIZE": "0",
                    "HISTFILESIZE": "0",
                    "TERM": "xterm-256color",
                    "LC_ALL": "C.UTF-8",
                },
                start_new_session=True,
                close_fds=True,
            )
            # Do not expose a PTY until the shell has applied its bounded
            # startup settings. Split the nonce in the echoed command; only
            # executed output contains the complete marker.
            marker = f"OPERANT_READY_{secrets.token_hex(6)}".encode()
            midpoint = len(marker) // 2
            os.write(
                master,
                b"set +m; echo '" + marker[:midpoint] + b"''" + marker[midpoint:] + b"'\n",
            )
            startup = bytearray()
            deadline = time.monotonic() + 3
            # PTY line endings and prompts differ across shells/platforms.
            # The echoed input has the nonce split by quotes, so only the
            # executed echo can contain these bytes contiguously.
            while marker not in startup:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or process.poll() is not None:
                    raise OSError("terminal shell did not become ready")
                if not select.select([master], [], [], min(0.2, remaining))[0]:
                    continue
                startup.extend(os.read(master, 4096))
                if len(startup) > 16_384:
                    raise OSError("terminal startup output exceeded its bound")
        except BaseException:
            if process is not None and process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=1)
            os.close(master)
            os.close(slave)
            raise
        finally:
            os.close(bound_fd)
        os.close(slave)
        assert process is not None
        return process, master

    async def _expire(self, item: _Terminal) -> None:
        try:
            await asyncio.sleep(TOKEN_TIMEOUT_SECONDS)
            if item.status == "waiting":
                self.stop(item.id)
                return
            await asyncio.sleep(TERMINAL_TIMEOUT_SECONDS - TOKEN_TIMEOUT_SECONDS)
            self.stop(item.id)
        except asyncio.CancelledError:
            return

    def stop(self, terminal_id: str) -> TerminalView:
        item = self._get(terminal_id)
        original_status = item.status
        if original_status in {"waiting", "running", "exited"}:
            try:
                descendants = _descendants(item.process.pid)
                descendants_clean = _terminate_descendants(descendants)
            except OSError:
                descendants_clean = False
            # Closing the PTY sends HUP to foreground jobs and unblocks readers.
            with contextlib.suppress(OSError):
                os.close(item.master_fd)
            if item.process.poll() is None:
                self._signal(item.process, signal.SIGTERM)
                try:
                    item.process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self._signal(item.process, signal.SIGKILL)
                    item.process.wait(timeout=1)
            item.exit_code = item.process.returncode
            if item.status != "exited":
                item.status = "terminated" if descendants_clean else "cleanup_unknown"
            item.finished_at = time.monotonic()
            if original_status in {"waiting", "running"}:
                self._audit(
                    item,
                    "terminal.terminated" if descendants_clean else "terminal.cleanup_unknown",
                )
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        if item.timer is not None and item.timer is not current:
            item.timer.cancel()
        return item.view()

    @staticmethod
    def _signal(process: subprocess.Popen[bytes], sig: signal.Signals) -> None:
        try:
            pgid = os.getpgid(process.pid)
        except ProcessLookupError:
            return
        except PermissionError:
            pgid = None
        if pgid == process.pid and pgid != os.getpgrp():
            try:
                os.killpg(pgid, sig)
                return
            except ProcessLookupError:
                return
            except PermissionError:
                pass
        # A sandbox may reject group signals. Never signal our own group.
        with contextlib.suppress(ProcessLookupError):
            process.send_signal(sig)

    def exit(self, item: _Terminal) -> None:
        if item.status in {"waiting", "running"}:
            item.exit_code = item.process.poll()
            try:
                cleaned = _terminate_group_members(item.process.pid)
            except OSError:
                cleaned = False
            item.status = "exited" if cleaned else "cleanup_unknown"
            item.finished_at = time.monotonic()
            with contextlib.suppress(OSError):
                os.close(item.master_fd)
            self._audit(item, "terminal.exited" if cleaned else "terminal.cleanup_unknown")
        if item.timer is not None:
            item.timer.cancel()

    def close(self) -> None:
        for terminal_id in tuple(self.sessions):
            self.stop(terminal_id)
        self.sessions.clear()
        self.idempotency.clear()

    def cancel_session(self, session_id: str) -> None:
        self._cancelled_sessions.add(session_id)
        for item in tuple(self.sessions.values()):
            if item.session_id == session_id and item.status in {"waiting", "running"}:
                self.stop(item.id)

    def cancel_thread(self, thread_id: str) -> None:
        for item in tuple(self.sessions.values()):
            if item.thread_id == thread_id and item.status in {"waiting", "running"}:
                self.stop(item.id)

    def _audit(self, item: _Terminal, event_type: str) -> None:
        self.gateway.repository.append_security_audit(
            SecurityAuditEvent(
                action_hash=item.action_hash,
                principal=self.gateway.principal,
                event_type=event_type,
                decision=PolicyDecision.ALLOW,
                detail={"terminal_id": item.id, "thread_id": item.thread_id},
            )
        )


def _resize(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _mac_proc() -> ctypes.CDLL:
    library = ctypes.CDLL("/usr/lib/libproc.dylib")
    library.proc_listpids.argtypes = [
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    library.proc_listpids.restype = ctypes.c_int
    library.proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    library.proc_pidinfo.restype = ctypes.c_int
    return library


def _children_of(ppid: int) -> list[int]:
    if sys.platform == "darwin":
        library = _mac_proc()
        estimated = library.proc_listpids(6, ppid, None, 0)
        if estimated < 0:
            raise OSError("cannot enumerate PTY descendants")
        buffer = (ctypes.c_int * max(64, estimated // 4 + 32))()
        size = library.proc_listpids(6, ppid, buffer, ctypes.sizeof(buffer))
        if size < 0 or size >= ctypes.sizeof(buffer):
            raise OSError("PTY descendant list is unavailable or truncated")
        return [pid for pid in buffer[: size // 4] if pid > 0]
    if sys.platform.startswith("linux"):
        try:
            raw = Path(f"/proc/{ppid}/task/{ppid}/children").read_text()
        except FileNotFoundError:
            return []
        return [int(value) for value in raw.split()]
    raise OSError("PTY descendant enumeration is unavailable")


def _parent_of(pid: int) -> int | None:
    if sys.platform == "darwin":
        info = ctypes.create_string_buffer(64)
        size = _mac_proc().proc_pidinfo(pid, 13, 0, info, 64)
        if size != 64:
            return None
        observed_pid, ppid = struct.unpack_from("II", info.raw)
        return int(ppid) if observed_pid == pid else None
    if sys.platform.startswith("linux"):
        try:
            raw = Path(f"/proc/{pid}/stat").read_text()
        except FileNotFoundError:
            return None
        return int(raw[raw.rfind(")") + 2 :].split()[1])
    return None


def _descendants(root_pid: int) -> list[tuple[int, int]]:
    pending = [root_pid]
    found: list[tuple[int, int]] = []
    while pending:
        parent = pending.pop()
        for child in _children_of(parent):
            if _parent_of(child) != parent:
                continue
            found.append((child, parent))
            pending.append(child)
            if len(found) > 256:
                raise OSError("PTY has too many descendants to verify")
    return list(reversed(found))


def _terminate_descendants(descendants: list[tuple[int, int]]) -> bool:
    confirmed = True
    for pid, parent in descendants:
        if _parent_of(pid) != parent:
            confirmed = False
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            continue
        except PermissionError:
            confirmed = False
    return confirmed


def _group_members(pgid: int) -> list[int]:
    if sys.platform == "darwin":
        library = _mac_proc()
        estimated = library.proc_listpids(2, pgid, None, 0)
        if estimated < 0:
            raise OSError("cannot enumerate PTY process group")
        buffer = (ctypes.c_int * max(64, estimated // 4 + 32))()
        size = library.proc_listpids(2, pgid, buffer, ctypes.sizeof(buffer))
        if size < 0 or size >= ctypes.sizeof(buffer):
            raise OSError("PTY process group list is unavailable or truncated")
        return [pid for pid in buffer[: size // 4] if pid > 0]
    if sys.platform.startswith("linux"):
        members = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if os.getpgid(int(entry.name)) == pgid:
                    members.append(int(entry.name))
            except (ProcessLookupError, PermissionError):
                continue
        return members
    raise OSError("PTY process group enumeration is unavailable")


def _terminate_group_members(pgid: int) -> bool:
    confirmed = True
    for pid in _group_members(pgid):
        if pid == os.getpid() or pid == os.getpgrp():
            raise OSError("PTY process group overlaps Core")
        try:
            if os.getpgid(pid) != pgid:
                confirmed = False
                continue
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            continue
        except PermissionError:
            confirmed = False
    return confirmed


def _local(host: str | None) -> bool:
    return host in {"127.0.0.1", "::1", "testclient"}


def _local_origin(value: str | None) -> bool:
    if value is None:
        return True  # local Python/TUI clients need not send a browser Origin
    parsed = urlsplit(value)
    return (
        parsed.scheme in {"http", "https", "tauri"}
        and parsed.hostname in {"localhost", "127.0.0.1", "::1", "tauri.localhost"}
        and not parsed.username
        and not parsed.password
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
    )


def install_workbench_terminal_routes(
    app: FastAPI, service: ApplicationService, gateway: Phase45ActionGateway
) -> TerminalManager:
    manager = TerminalManager(service, gateway)
    service.terminal_manager = manager
    app.router.add_event_handler("shutdown", manager.close)
    app.state.workbench_terminals = manager

    @app.post(
        "/v1/workbench/threads/{thread_id}/terminals",
        response_model=TerminalView,
        operation_id="createWorkbenchTerminal",
    )
    async def create(request: Request, thread_id: str, body: TerminalCreate) -> JSONResponse:
        if not _local(None if request.client is None else request.client.host):
            raise HTTPException(status_code=403, detail="local terminal only")
        view = await manager.create(thread_id, body)
        return JSONResponse(
            content=view.model_dump(mode="json"),
            headers={"Cache-Control": "no-store"},
        )

    @app.get(
        "/v1/workbench/terminals/{terminal_id}",
        response_model=TerminalView,
        operation_id="getWorkbenchTerminal",
    )
    def get(request: Request, terminal_id: str) -> TerminalView:
        if not _local(None if request.client is None else request.client.host):
            raise HTTPException(status_code=403, detail="local terminal only")
        manager.prune()
        return manager._get(terminal_id).view()

    @app.delete(
        "/v1/workbench/terminals/{terminal_id}",
        response_model=TerminalView,
        operation_id="deleteWorkbenchTerminal",
    )
    async def delete(request: Request, terminal_id: str) -> TerminalView:
        if not _local(None if request.client is None else request.client.host):
            raise HTTPException(status_code=403, detail="local terminal only")
        manager.prune()
        return manager.stop(terminal_id)

    @app.websocket("/v1/workbench/terminals/{terminal_id}/stream")
    async def stream(websocket: WebSocket, terminal_id: str) -> None:
        if not _local(None if websocket.client is None else websocket.client.host):
            await websocket.close(code=4403)
            return
        if not _local_origin(websocket.headers.get("origin")):
            await websocket.close(code=4403)
            return
        offered = {
            candidate.strip()
            for candidate in websocket.headers.get("sec-websocket-protocol", "").split(",")
            if candidate.strip()
        }
        if PROTOCOL not in offered or websocket.query_params:
            await websocket.close(code=4406)
            return
        item = manager.sessions.get(terminal_id)
        token_protocols = [value for value in offered if value.startswith("operant.token.")]
        supplied = (
            token_protocols[0].removeprefix("operant.token.") if len(token_protocols) == 1 else ""
        )
        if (
            item is None
            or item.status != "waiting"
            or time.monotonic() - item.created_at > TOKEN_TIMEOUT_SECONDS
            or len(offered) != 2
            or not hmac.compare_digest(item.token, supplied)
        ):
            await websocket.close(code=4401)
            return
        item.token = ""
        item.status = "running"
        await websocket.accept(subprotocol=PROTOCOL)
        os.set_blocking(item.master_fd, False)
        outputs: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=MAX_OUTPUT_QUEUE)

        async def read_pty() -> None:
            post_exit_chunks = 0
            try:
                while item.status == "running":
                    shell_exited = item.process.poll() is not None
                    try:
                        chunk = os.read(item.master_fd, 4096)
                    except BlockingIOError:
                        if shell_exited:
                            break
                        await asyncio.sleep(0.03)
                        continue
                    except OSError:
                        break
                    if not chunk:
                        break
                    if shell_exited:
                        post_exit_chunks += 1
                    try:
                        outputs.put_nowait(
                            {"type": "output", "data": chunk.decode("utf-8", "replace")}
                        )
                    except asyncio.QueueFull:
                        manager.stop(item.id)
                        await websocket.close(code=1013, reason="terminal output overflow")
                        return
                    if post_exit_chunks >= 16:
                        break
            finally:
                if item.status == "running":
                    if item.process.poll() is None:
                        manager.stop(item.id)
                    else:
                        manager.exit(item)
                if item.status != "terminated":
                    await outputs.put({"type": "exit", "exit_code": item.exit_code})

        async def send_output() -> None:
            while True:
                frame = await outputs.get()
                await websocket.send_json(frame)
                if frame["type"] == "exit":
                    return

        async def receive_input() -> None:
            while item.status == "running":
                frame = await websocket.receive_json()
                if not isinstance(frame, dict):
                    await websocket.close(code=4400)
                    return
                kind = frame.get("type")
                if kind == "input":
                    data = frame.get("data")
                    if not isinstance(data, str) or len(data) > MAX_INPUT_CHARS:
                        await websocket.close(code=4400)
                        return
                    pending = data.encode("utf-8")
                    while pending and item.status == "running":
                        try:
                            pending = pending[os.write(item.master_fd, pending) :]
                        except BlockingIOError:
                            await asyncio.sleep(0.01)
                elif kind == "resize":
                    cols, rows = frame.get("cols"), frame.get("rows")
                    if (
                        not isinstance(cols, int)
                        or not 20 <= cols <= 300
                        or not isinstance(rows, int)
                        or not 5 <= rows <= 150
                    ):
                        await websocket.close(code=4400)
                        return
                    _resize(item.master_fd, cols, rows)
                    item.cols, item.rows = cols, rows
                else:
                    await websocket.close(code=4400)
                    return

        reader = asyncio.create_task(read_pty())
        sender = asyncio.create_task(send_output())
        receiver = asyncio.create_task(receive_input())
        try:
            done, pending = await asyncio.wait(
                (sender, receiver), return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                with contextlib.suppress(WebSocketDisconnect, OSError):
                    task.result()
            for task in pending:
                task.cancel()
        finally:
            manager.stop(item.id)
            reader.cancel()
            sender.cancel()
            receiver.cancel()

    return manager
