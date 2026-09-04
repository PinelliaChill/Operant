from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from operant.domain.multiwriter import WriterIsolationKind, WriterWorkspace


class ContainerLifecycleStatus(str, Enum):
    ABSENT = "absent"
    CREATED = "created"
    RUNNING = "running"
    STOPPED = "stopped"
    REMOVED = "removed"
    OUTCOME_UNKNOWN = "outcome_unknown"


class ContainerOutcomeUnknown(RuntimeError):
    """Docker accepted a request but its durable outcome cannot be proved."""


@dataclass(frozen=True)
class ContainerResourceLimits:
    cpus: float = 2.0
    memory_bytes: int = 2 * 1024 * 1024 * 1024
    pids: int = 256

    def __post_init__(self) -> None:
        if not 0.1 <= self.cpus <= 64:
            raise ValueError("container CPU limit is out of bounds")
        if not 64 * 1024 * 1024 <= self.memory_bytes <= 128 * 1024 * 1024 * 1024:
            raise ValueError("container memory limit is out of bounds")
        if not 16 <= self.pids <= 4096:
            raise ValueError("container PID limit is out of bounds")


@dataclass(frozen=True)
class ContainerWriterSpec:
    workspace: WriterWorkspace
    image: str
    command: tuple[str, ...]
    environment: Mapping[str, str]
    resources: ContainerResourceLimits = ContainerResourceLimits()

    def __post_init__(self) -> None:
        if self.workspace.isolation_kind is not WriterIsolationKind.CONTAINER:
            raise ValueError("Container Writer requires container isolation")
        if re.fullmatch(r"sha256:[0-9a-f]{64}", self.image) is None:
            raise ValueError("Container Writer image must use a sha256 digest ID")
        if not self.command or len(self.command) > 128:
            raise ValueError("Container Writer command is required and bounded")
        if any(not item or "\x00" in item or len(item) > 4096 for item in self.command):
            raise ValueError("Container Writer command contains an invalid argument")
        if len(self.environment) > 32:
            raise ValueError("Container Writer environment is too large")
        for name, value in self.environment.items():
            if (
                re.fullmatch(r"OPERANT_WRITER_[A-Z0-9_]{1,64}", name) is None
                or any(
                    marker in name
                    for marker in ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "COOKIE", "KEY")
                )
                or "\x00" in value
                or len(value) > 4096
            ):
                raise ValueError("Container Writer environment must use bounded writer variables")


class ContainerCommandRunner:
    def run(self, argv: Sequence[str], *, timeout_seconds: int) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                argv,
                check=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")},
            )
        except subprocess.TimeoutExpired as exc:
            raise ContainerOutcomeUnknown("Docker operation timed out") from exc
        except OSError as exc:
            raise RuntimeError("Docker executable is unavailable") from exc


class ContainerWriterLifecycle:
    """Bounded Docker lifecycle adapter for an administrator-mapped writer root.

    The adapter never guesses after a Docker timeout and never uses ``--rm``;
    this preserves inspectable state for explicit reconciliation. Persisting
    lifecycle revision/fencing requires the public SQLite contract owned by the
    integration agent.
    """

    def __init__(
        self,
        workspace_roots: Mapping[str, str | Path],
        *,
        runner: ContainerCommandRunner | None = None,
        timeout_seconds: int = 30,
        stop_timeout_seconds: int = 10,
    ) -> None:
        if not workspace_roots:
            raise ValueError("Container Writer requires an administrator root mapping")
        if not 1 <= timeout_seconds <= 300 or not 1 <= stop_timeout_seconds <= 60:
            raise ValueError("Container Writer timeout is out of bounds")
        self.timeout_seconds = timeout_seconds
        self.stop_timeout_seconds = stop_timeout_seconds
        self.runner = runner or ContainerCommandRunner()
        self._roots: dict[str, Path] = {}
        for reference, configured in workspace_roots.items():
            candidate = Path(configured)
            if not reference or not candidate.is_absolute():
                raise ValueError("Container Writer roots require opaque refs and absolute paths")
            resolved = candidate.resolve(strict=True)
            if (
                not resolved.is_dir()
                or resolved == Path(resolved.anchor)
                or resolved == Path.home()
            ):
                raise ValueError("Container Writer root is unsafe")
            if candidate.is_symlink():
                raise ValueError("Container Writer root cannot be a symlink")
            self._roots[reference] = resolved

    @staticmethod
    def container_name(workspace_id: str) -> str:
        normalized = "".join(
            character if character.isalnum() else "-" for character in workspace_id
        )
        normalized = normalized.strip("-")[:48]
        if not normalized:
            raise ValueError("writer workspace ID cannot form a container name")
        return f"operant-writer-{normalized}"

    def build_create_argv(self, spec: ContainerWriterSpec) -> list[str]:
        root = self._root(spec.workspace.isolation_ref)
        name = self.container_name(spec.workspace.writer_workspace_id)
        mount = f"type=bind,src={root},dst=/workspace"
        argv = [
            "docker",
            "create",
            "--name",
            name,
            "--init",
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=268435456",
            "--pids-limit",
            str(spec.resources.pids),
            "--cpus",
            str(spec.resources.cpus),
            "--memory",
            str(spec.resources.memory_bytes),
            "--mount",
            mount,
            "--workdir",
            "/workspace",
            "--label",
            "operant.managed=true",
            "--label",
            f"operant.writer_workspace_id={spec.workspace.writer_workspace_id}",
            "--label",
            f"operant.graph_run_id={spec.workspace.graph_run_id}",
            "--label",
            f"operant.spec_sha256={self._spec_sha256(spec)}",
        ]
        for name, value in sorted(spec.environment.items()):
            argv.extend(("--env", f"{name}={value}"))
        argv.append(spec.image)
        argv.extend(spec.command)
        return argv

    def create(self, spec: ContainerWriterSpec) -> ContainerLifecycleStatus:
        current = self.inspect(
            spec.workspace.writer_workspace_id,
            expected_spec_sha256=self._spec_sha256(spec),
        )
        if current in {ContainerLifecycleStatus.CREATED, ContainerLifecycleStatus.STOPPED}:
            return current
        if current is ContainerLifecycleStatus.RUNNING:
            raise RuntimeError("Container Writer is already running")
        result = self.runner.run(self.build_create_argv(spec), timeout_seconds=self.timeout_seconds)
        if result.returncode != 0:
            raise RuntimeError("Docker failed to create Container Writer")
        return ContainerLifecycleStatus.CREATED

    def start(self, workspace_id: str) -> ContainerLifecycleStatus:
        current = self.inspect(workspace_id)
        if current is ContainerLifecycleStatus.RUNNING:
            return current
        if current not in {ContainerLifecycleStatus.CREATED, ContainerLifecycleStatus.STOPPED}:
            raise RuntimeError("Container Writer must exist before start")
        self._checked(("docker", "start", self.container_name(workspace_id)))
        return ContainerLifecycleStatus.RUNNING

    def stop(self, workspace_id: str) -> ContainerLifecycleStatus:
        current = self.inspect(workspace_id)
        if current in {ContainerLifecycleStatus.CREATED, ContainerLifecycleStatus.STOPPED}:
            return current
        if current is ContainerLifecycleStatus.ABSENT:
            return ContainerLifecycleStatus.REMOVED
        self._checked(
            (
                "docker",
                "stop",
                "--time",
                str(self.stop_timeout_seconds),
                self.container_name(workspace_id),
            )
        )
        return ContainerLifecycleStatus.STOPPED

    def remove(self, workspace_id: str) -> ContainerLifecycleStatus:
        current = self.inspect(workspace_id)
        if current is ContainerLifecycleStatus.ABSENT:
            return ContainerLifecycleStatus.REMOVED
        if current is ContainerLifecycleStatus.RUNNING:
            raise RuntimeError("running Container Writer must be stopped before removal")
        self._checked(("docker", "rm", self.container_name(workspace_id)))
        return ContainerLifecycleStatus.REMOVED

    def cleanup_after_crash(self, workspace_id: str) -> ContainerLifecycleStatus:
        current = self.inspect(workspace_id)
        if current is ContainerLifecycleStatus.RUNNING:
            raise ContainerOutcomeUnknown(
                "running Container Writer survived its owner; inspect before stopping"
            )
        if current in {ContainerLifecycleStatus.CREATED, ContainerLifecycleStatus.STOPPED}:
            return self.remove(workspace_id)
        return ContainerLifecycleStatus.REMOVED

    def inspect(
        self,
        workspace_id: str,
        *,
        expected_spec_sha256: str | None = None,
    ) -> ContainerLifecycleStatus:
        result = self.runner.run(
            (
                "docker",
                "inspect",
                "--format",
                "{{json .}}",
                self.container_name(workspace_id),
            ),
            timeout_seconds=self.timeout_seconds,
        )
        if result.returncode != 0:
            # Docker uses non-zero for not-found, but other daemon errors are
            # not safely distinguishable unless the structured object exists.
            if "No such object" in result.stderr or "No such container" in result.stderr:
                return ContainerLifecycleStatus.ABSENT
            raise ContainerOutcomeUnknown("unable to inspect Container Writer")
        try:
            body = json.loads(result.stdout)
            labels = body["Config"]["Labels"]
            running = body["State"]["Running"]
            status = body["State"]["Status"]
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ContainerOutcomeUnknown("Docker returned an invalid inspect result") from exc
        if (
            labels.get("operant.managed") != "true"
            or labels.get("operant.writer_workspace_id") != workspace_id
        ):
            raise PermissionError("container identity is not owned by Operant")
        if (
            expected_spec_sha256 is not None
            and labels.get("operant.spec_sha256") != expected_spec_sha256
        ):
            raise RuntimeError("existing Container Writer does not match the requested spec")
        if running is True:
            return ContainerLifecycleStatus.RUNNING
        if status == "created":
            return ContainerLifecycleStatus.CREATED
        return ContainerLifecycleStatus.STOPPED

    def _root(self, reference: str) -> Path:
        try:
            root = self._roots[reference]
        except KeyError as exc:
            raise ValueError("unknown Container Writer isolation ref") from exc
        current = root.resolve(strict=True)
        if current != root or not current.is_dir():
            raise ValueError("Container Writer root identity changed")
        return current

    def _spec_sha256(self, spec: ContainerWriterSpec) -> str:
        body = {
            "workspace_id": spec.workspace.writer_workspace_id,
            "graph_run_id": spec.workspace.graph_run_id,
            "node_run_id": spec.workspace.node_run_id,
            "isolation_ref": spec.workspace.isolation_ref,
            "base_revision": spec.workspace.base_revision,
            "ownership_paths": spec.workspace.ownership_paths,
            "root": str(self._root(spec.workspace.isolation_ref)),
            "image": spec.image,
            "command": spec.command,
            "environment": dict(sorted(spec.environment.items())),
            "resources": {
                "cpus": spec.resources.cpus,
                "memory_bytes": spec.resources.memory_bytes,
                "pids": spec.resources.pids,
            },
        }
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _checked(self, argv: Sequence[str]) -> None:
        result = self.runner.run(argv, timeout_seconds=self.timeout_seconds)
        if result.returncode != 0:
            raise RuntimeError("Docker Container Writer lifecycle operation failed")
