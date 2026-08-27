from __future__ import annotations

import asyncio
import os
import shutil
import signal
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from operant.domain.models import CommandExecutionPolicy

PROTECTED_WORKSPACE_NAMES = frozenset(
    {
        ".git",
        ".env",
        ".env.local",
        ".operant",
        ".credentials",
        ".git-credentials",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "credentials.json",
        "service-account.json",
        "service_account.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
    }
)
PROTECTED_WORKSPACE_SUFFIXES = (".key", ".p12", ".pfx", ".pem")
SNAPSHOT_EXCLUDED_NAMES = PROTECTED_WORKSPACE_NAMES | frozenset(
    {".operant", ".pytest_cache", ".venv", "__pycache__", "node_modules"}
)


def is_protected_workspace_name(name: str) -> bool:
    lowered = name.lower()
    return (
        lowered in PROTECTED_WORKSPACE_NAMES
        or lowered.startswith(".env.")
        or lowered.endswith(PROTECTED_WORKSPACE_SUFFIXES)
    )


class CommandRunnerError(RuntimeError):
    pass


class CommandTimedOut(CommandRunnerError):
    pass


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool


class CommandRunner(Protocol):
    async def run(
        self,
        *,
        argv: Sequence[str],
        workspace: Path,
        cwd: Path,
        timeout_seconds: int,
        workspace_write: bool,
        output_limit: int,
    ) -> CommandResult: ...


def _truncate(text: str, output_limit: int) -> tuple[str, bool]:
    return (text[:output_limit], len(text) > output_limit)


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except (PermissionError, ProcessLookupError):
            if process.returncode is None:
                process.kill()
    await process.communicate()


async def _run_subprocess(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: int,
    output_limit: int,
    cleanup: Callable[[], Awaitable[None]] | None = None,
) -> CommandResult:
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise CommandRunnerError(f"command executable was not found: {argv[0]}") from exc

    async def clean_up_after_interruption() -> None:
        await _terminate_process(process)
        if cleanup is not None:
            await cleanup()

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        await clean_up_after_interruption()
        raise CommandTimedOut(f"command timed out after {timeout_seconds}s") from exc
    except asyncio.CancelledError:
        await clean_up_after_interruption()
        raise

    stdout_text, stdout_truncated = _truncate(
        (stdout or b"").decode("utf-8", errors="replace"), output_limit
    )
    stderr_text, stderr_truncated = _truncate(
        (stderr or b"").decode("utf-8", errors="replace"), output_limit
    )
    if process.returncode is None:
        raise CommandRunnerError("command ended without an exit code")
    return CommandResult(
        argv=tuple(argv),
        exit_code=process.returncode,
        stdout=stdout_text,
        stderr=stderr_text,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )


class HostCommandRunner:
    """Direct runner retained for explicitly trusted local workspaces only."""

    async def run(
        self,
        *,
        argv: Sequence[str],
        workspace: Path,
        cwd: Path,
        timeout_seconds: int,
        workspace_write: bool,
        output_limit: int,
    ) -> CommandResult:
        del workspace, workspace_write
        return await _run_subprocess(
            argv,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            output_limit=output_limit,
        )


class DockerCommandRunner:
    """Run a command against a filtered workspace snapshot inside Docker."""

    def __init__(self, policy: CommandExecutionPolicy) -> None:
        self.policy = policy

    async def run(
        self,
        *,
        argv: Sequence[str],
        workspace: Path,
        cwd: Path,
        timeout_seconds: int,
        workspace_write: bool,
        output_limit: int,
    ) -> CommandResult:
        root = workspace.resolve()
        if root == Path(root.anchor) or root == Path.home().resolve():
            raise CommandRunnerError("refusing to mount a filesystem root or the home directory")
        if "," in str(root):
            raise CommandRunnerError("Docker runner does not support commas in workspace paths")

        relative_cwd = cwd.resolve().relative_to(root)
        temporary_root = Path(tempfile.mkdtemp(prefix="operant-sandbox-"))
        snapshot = temporary_root / "workspace"
        cidfile = temporary_root / "container-id"
        try:
            await asyncio.to_thread(self._copy_workspace_snapshot, root, snapshot)
            docker_argv = self.build_argv(
                argv=argv,
                snapshot=snapshot,
                relative_cwd=relative_cwd,
                cidfile=cidfile,
                workspace_write=workspace_write,
                policy=self.policy,
            )
            try:
                return await _run_subprocess(
                    docker_argv,
                    cwd=temporary_root,
                    timeout_seconds=timeout_seconds,
                    output_limit=output_limit,
                    cleanup=lambda: self._remove_container(cidfile),
                )
            except CommandRunnerError as exc:
                if "docker" in str(exc).lower():
                    raise CommandRunnerError(
                        "Docker runner is unavailable; install and start Docker before retrying"
                    ) from exc
                raise
        finally:
            await asyncio.to_thread(shutil.rmtree, temporary_root, ignore_errors=True)

    @staticmethod
    def _copy_workspace_snapshot(source: Path, destination: Path) -> None:
        def ignore(_directory: str, names: list[str]) -> set[str]:
            return {
                name
                for name in names
                if name.lower() in SNAPSHOT_EXCLUDED_NAMES or is_protected_workspace_name(name)
            }

        shutil.copytree(source, destination, symlinks=True, ignore=ignore)

    @staticmethod
    def build_argv(
        *,
        argv: Sequence[str],
        snapshot: Path,
        relative_cwd: Path,
        cidfile: Path,
        workspace_write: bool,
        policy: CommandExecutionPolicy,
    ) -> list[str]:
        mount = f"type=bind,src={snapshot.resolve()},dst=/workspace"
        if not workspace_write:
            mount = f"{mount},readonly"
        workdir = "/workspace" if relative_cwd == Path(".") else f"/workspace/{relative_cwd}"
        return [
            "docker",
            "run",
            "--rm",
            "--init",
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,noexec,size=64m",
            "--tmpfs",
            "/var/tmp:rw,nosuid,nodev,noexec,size=64m",
            "--cpus",
            str(policy.cpu_limit),
            "--memory",
            f"{policy.memory_limit_mb}m",
            "--pids-limit",
            str(policy.pids_limit),
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--mount",
            mount,
            "--workdir",
            workdir,
            "--cidfile",
            str(cidfile),
            policy.docker_image,
            *argv,
        ]

    @staticmethod
    async def _remove_container(cidfile: Path) -> None:
        try:
            container_id = cidfile.read_text(encoding="utf-8").strip()
        except OSError:
            return
        if not container_id:
            return
        try:
            process = await asyncio.create_subprocess_exec(
                "docker",
                "rm",
                "--force",
                container_id,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return
        await process.communicate()
