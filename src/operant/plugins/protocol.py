"""Private PluginHost protocol primitives for the MP-1 implementation.

The public contract types live in :mod:`operant.contracts.b2_1`.  This module
only contains the host-side adapters and bounded transport helpers.  A plugin
never receives a Core service, a database connection, or an arbitrary path.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import platform
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from operant.contracts.b2_1 import (
    CandidateBatch,
    HostReadRequest,
    HostReadResult,
    IndexEvent,
    IndexReceipt,
    LifecycleRequest,
    LifecycleResult,
    MaintenanceInput,
    ModelProxyRequest,
    ModelProxyResult,
    PluginManifest,
    PrivateIndexRequest,
    PrivateIndexResource,
    PrivateIndexResult,
    ProposalBatch,
    RecallRequest,
    RpcContext,
    SourceBatch,
    SourceRef,
)
from operant.domain.models import utc_now

ENGINE_REQUEST_TYPES: dict[str, type[BaseModel]] = {
    "extract": SourceBatch,
    "recall": RecallRequest,
    "maintain": MaintenanceInput,
    "on_index_event": IndexEvent,
    "lifecycle": LifecycleRequest,
}
ENGINE_RESULT_TYPES: dict[str, type[BaseModel]] = {
    "extract": ProposalBatch,
    "recall": CandidateBatch,
    "maintain": ProposalBatch,
    "on_index_event": IndexReceipt,
    "lifecycle": LifecycleResult,
}

HOST_REQUEST_TYPES: dict[str, type[BaseModel]] = {
    "read_source": HostReadRequest,
    "search": RecallRequest,
    "model": ModelProxyRequest,
    "private_index": PrivateIndexRequest,
}
HOST_RESULT_TYPES: dict[str, type[BaseModel]] = {
    "read_source": HostReadResult,
    "search": CandidateBatch,
    "model": ModelProxyResult,
    "private_index": PrivateIndexResult,
}


class PluginError(RuntimeError):
    """A typed, fail-closed PluginHost failure."""

    def __init__(self, code: str, message: str, *, detail: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = dict(detail or {})


class PluginProtocolError(PluginError):
    def __init__(self, message: str, *, detail: Mapping[str, Any] | None = None) -> None:
        super().__init__("protocol_mismatch", message, detail=detail)


class IsolationUnavailableError(PluginError):
    def __init__(self, message: str = "actual plugin isolation is unavailable") -> None:
        super().__init__("isolation_unavailable", message)


class CancellationError(PluginError):
    def __init__(self, message: str = "plugin request was cancelled") -> None:
        super().__init__("cancelled", message)


class StaleEpochError(PluginError):
    def __init__(self, message: str = "plugin result is bound to a stale epoch") -> None:
        super().__init__("stale_epoch", message)


class BudgetExceededError(PluginError):
    def __init__(self, message: str = "plugin request exceeded its host budget") -> None:
        super().__init__("budget_exceeded", message)


class DeadlineExceededError(PluginError):
    def __init__(self, message: str = "plugin request exceeded its deadline") -> None:
        super().__init__("deadline_exceeded", message)


class PermissionDeniedError(PluginError):
    def __init__(self, message: str = "plugin host capability was denied") -> None:
        super().__init__("permission_denied", message)


class PackageUnavailableError(PluginError):
    def __init__(self, message: str = "plugin package is unavailable") -> None:
        super().__init__("package_unavailable", message)


class RestartRequiredError(PluginError):
    def __init__(self, message: str = "a safe host restart is required") -> None:
        super().__init__("restart_required", message)


class CleanupBlockedError(PluginError):
    def __init__(self, message: str = "plugin cleanup is blocked") -> None:
        super().__init__("cleanup_blocked", message)


class HostBudget(BaseModel):
    """A conservative per-call budget, always intersected with the Manifest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    max_request_bytes: int = Field(default=1_000_000, ge=1_024, le=16_777_216)
    max_response_bytes: int = Field(default=1_000_000, ge=1_024, le=16_777_216)
    max_log_chars: int = Field(default=20_000, ge=0, le=200_000)
    max_calls: int = Field(default=1_000, ge=1, le=100_000)
    max_concurrency: int = Field(default=4, ge=1, le=64)
    memory_mb: int = Field(default=512, ge=1, le=65536)
    max_cpu_seconds: float = Field(default=30.0, gt=0, le=3600)
    max_idle_seconds: float = Field(default=60.0, gt=0, le=3600)
    max_private_index_bytes: int = Field(default=20_000_000, ge=1_024, le=200_000_000)

    def intersect_manifest(self, manifest: PluginManifest) -> HostBudget:
        """Return the lower of host and package-declared transport limits."""

        return HostBudget(
            timeout_seconds=self.timeout_seconds,
            max_request_bytes=min(self.max_request_bytes, manifest.max_rpc_bytes),
            max_response_bytes=min(self.max_response_bytes, manifest.max_rpc_bytes),
            max_log_chars=self.max_log_chars,
            max_calls=self.max_calls,
            max_concurrency=min(self.max_concurrency, manifest.max_concurrency),
            memory_mb=min(self.memory_mb, manifest.memory_mb),
            max_cpu_seconds=self.max_cpu_seconds,
            max_idle_seconds=self.max_idle_seconds,
            max_private_index_bytes=self.max_private_index_bytes,
        )


@dataclass(frozen=True)
class SandboxEvidence:
    """Evidence returned only after a real sandbox-exec probe succeeds."""

    evidence_ref: str
    profile: str
    runner: str


def _sandbox_literal(path: Path | str) -> str:
    value = str(path)
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError("sandbox path contains a control character")
    return json.dumps(value)


def build_sandbox_profile(
    *,
    package_root: Path,
    data_root: Path,
    state_root: Path,
    logs_root: Path,
    tmp_root: Path,
    executable: Path,
) -> str:
    """Build a deny-by-default profile for an untrusted stdio plugin.

    The profile grants read access to the package and system runtime, and
    write access only to Host-managed data/state/log/tmp directories.  Network
    access is intentionally absent.  Paths are controlled by Host and encoded
    as literals, so a plugin cannot supply profile syntax.
    """

    read_paths = [package_root.resolve(), Path(sys.prefix).resolve(), Path(executable).resolve()]
    # Python on macOS may load framework/runtime files from these locations.
    # Keep these roots limited to system/runtime locations.  In particular do
    # not grant all of /private/var: on macOS it also contains user caches and
    # temporary material outside this plugin's managed tmp directory.
    read_paths.extend(
        (
            Path("/usr"),
            Path("/System"),
            Path("/Library/Frameworks"),
            Path(sys.base_prefix).resolve(),
        )
    )
    lines = ["(version 1)", "(deny default)"]
    for path in read_paths:
        lines.append(f"(allow file-read* (subpath {_sandbox_literal(path)}))")
        # dyld maps the interpreter and libpython with a separate Seatbelt
        # operation; keep this grant to immutable runtime roots only.
        lines.append(f"(allow file-map-executable (subpath {_sandbox_literal(path)}))")
    lines.append(f"(allow process-exec (literal {_sandbox_literal(executable)}))")
    for path in (data_root, state_root, logs_root, tmp_root):
        lines.append(f"(allow file-read* (subpath {_sandbox_literal(path)}))")
        lines.append(f"(allow file-write* (subpath {_sandbox_literal(path)}))")
    # Python startup on macOS performs a few kernel/IPC reads.  These grants
    # do not widen filesystem or network access and are narrower than allowing
    # the default profile.
    lines.extend(
        (
            "(allow process-info*)",
            "(allow sysctl-read)",
            "(allow mach-lookup)",
            "(allow ipc-posix-shm*)",
            '(allow file-read-data (literal "/"))',
            '(allow file-read* (literal "/dev/null") (literal "/dev/random") '
            '(literal "/dev/urandom") (literal "/dev/zero"))',
            "(allow signal (target self))",
        )
    )
    # Explicitly deny network even if a host profile later grows another allow.
    lines.append("(deny network*)")
    return "\n".join(lines) + "\n"


class SandboxProbe:
    """Probe real macOS sandbox admission without treating a subprocess as isolation."""

    def __init__(self, runner: str = "/usr/bin/sandbox-exec") -> None:
        self.runner = runner

    def check(
        self,
        *,
        package_root: Path,
        data_root: Path,
        state_root: Path,
        logs_root: Path,
        tmp_root: Path,
        executable: Path,
    ) -> SandboxEvidence:
        if platform.system() != "Darwin" or not Path(self.runner).is_file():
            raise IsolationUnavailableError()
        try:
            package_root = package_root.resolve(strict=True)
            data_root = data_root.resolve(strict=True)
            state_root = state_root.resolve(strict=True)
            logs_root = logs_root.resolve(strict=True)
            tmp_root = tmp_root.resolve(strict=True)
            executable = executable.resolve(strict=True)
        except OSError as exc:
            raise IsolationUnavailableError("sandbox paths are unavailable") from exc
        profile = build_sandbox_profile(
            package_root=package_root,
            data_root=data_root,
            state_root=state_root,
            logs_root=logs_root,
            tmp_root=tmp_root,
            executable=executable,
        )
        try:
            home_probe_dir = Path(
                tempfile.mkdtemp(prefix=".operant-sandbox-probe-", dir=Path.home())
            )
            home_sentinel = home_probe_dir / "sentinel"
            home_write_target = home_probe_dir / "write-attempt"
            home_sentinel.write_bytes(b"operant sandbox probe")
            probe_fd, probe_name = tempfile.mkstemp(
                prefix=f".sandbox-probe-{uuid4().hex}-", dir=package_root.parent
            )
            os.close(probe_fd)
            probe_target = Path(probe_name)
            probe_target.unlink()
        except OSError as exc:
            raise IsolationUnavailableError(
                "sandbox probe could not create controlled sentinels"
            ) from exc
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(0.5)
            probe_code = (
                "import os,socket,sys; "
                "outside=sys.argv[1]; home_sentinel=sys.argv[2]; home_write=sys.argv[3]; "
                "port=int(sys.argv[4]); "
                "\ntry:\n open(outside,'wb').write(b'x'); sys.exit(21)\n"
                "except (PermissionError,OSError): pass; "
                "\ntry:\n open(home_sentinel,'rb').read(); sys.exit(22)\n"
                "except (PermissionError,OSError): pass; "
                "\ntry:\n open(home_write,'wb').write(b'x'); sys.exit(23)\n"
                "except (PermissionError,OSError): pass; "
                "\ns=socket.socket(); s.settimeout(0.4); "
                "\ntry:\n s.connect(('127.0.0.1',port)); sys.exit(23)\n"
                "except PermissionError: sys.exit(0)\n"
                "except OSError: sys.exit(24)"
            )
            completed = subprocess.run(
                [
                    self.runner,
                    "-p",
                    profile,
                    str(executable),
                    "-I",
                    "-S",
                    "-c",
                    probe_code,
                    str(probe_target),
                    str(home_sentinel),
                    str(home_write_target),
                    str(listener.getsockname()[1]),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=False,
                timeout=3,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise IsolationUnavailableError() from exc
        finally:
            listener.close()
            from contextlib import suppress

            with suppress(OSError):
                probe_target.unlink()
            with suppress(OSError):
                home_sentinel.unlink()
            with suppress(OSError):
                home_write_target.unlink()
            with suppress(OSError):
                home_probe_dir.rmdir()
        if completed.returncode != 0:
            raise IsolationUnavailableError(
                "sandbox probe did not reject outside file and loopback access"
            )
        profile_digest = hashlib.sha256(profile.encode("utf-8")).hexdigest()
        return SandboxEvidence(
            evidence_ref=f"sandbox-exec:{profile_digest}",
            profile=profile,
            runner=self.runner,
        )


def canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    except (TypeError, ValueError) as exc:
        raise PluginProtocolError("plugin payload is not JSON serializable") from exc


def digest_payload(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def encode_rpc(payload: Mapping[str, Any], *, max_bytes: int) -> bytes:
    encoded = canonical_json(payload)
    if len(encoded) > max_bytes:
        raise BudgetExceededError("RPC frame exceeds the configured byte limit")
    return encoded + b"\n"


def decode_rpc(line: bytes, *, max_bytes: int) -> dict[str, Any]:
    if len(line) > max_bytes + 1:
        raise PluginProtocolError("RPC frame exceeds the configured byte limit")
    try:
        value = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PluginProtocolError("RPC frame is not valid JSON") from exc
    if not isinstance(value, dict):
        raise PluginProtocolError("RPC frame must be an object")
    return value


def _safe_relative_path(value: str) -> str:
    if not value or "\x00" in value or value.startswith(("/", "\\")):
        raise PermissionDeniedError("resource path is invalid")
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PermissionDeniedError("resource path must stay inside the managed directory")
    return path.as_posix()


def compute_package_digest(
    root: Path, *, max_files: int = 4_096, max_bytes: int = 100_000_000
) -> str:
    """Hash a package tree deterministically while rejecting symlinks."""

    root = Path(root)
    try:
        root_stat = root.lstat()
    except OSError as exc:
        raise PackageUnavailableError() from exc
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise PackageUnavailableError("plugin package must be a real directory")
    digest = hashlib.sha256()
    count = 0
    total = 0
    try:
        entries = sorted(
            (path for path in root.rglob("*") if path != root),
            key=lambda path: path.relative_to(root).as_posix(),
        )
        for path in entries:
            relative = path.relative_to(root).as_posix()
            # Importing a verified Python entrypoint may create interpreter
            # bytecode.  It is derived cache data, not package identity.
            if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
                continue
            item_stat = path.lstat()
            if stat.S_ISLNK(item_stat.st_mode):
                raise PackageUnavailableError("plugin package cannot contain symlinks")
            if stat.S_ISDIR(item_stat.st_mode):
                continue
            if not stat.S_ISREG(item_stat.st_mode):
                raise PackageUnavailableError("plugin package contains a non-regular file")
            count += 1
            if count > max_files:
                raise PackageUnavailableError("plugin package contains too many files")
            if item_stat.st_size > max_bytes - total:
                raise PackageUnavailableError("plugin package exceeds the byte limit")
            # O_NOFOLLOW closes the common replacement race between lstat and read.
            flags = os.O_RDONLY | int(getattr(os, "O_NOFOLLOW", 0))
            try:
                descriptor = os.open(path, flags)
            except OSError as exc:
                raise PackageUnavailableError("plugin package file cannot be opened") from exc
            try:
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode) or opened.st_dev != item_stat.st_dev:
                    raise PackageUnavailableError("plugin package changed while being read")
                chunks: list[bytes] = []
                while True:
                    chunk = os.read(descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                data = b"".join(chunks)
            finally:
                os.close(descriptor)
            total += len(data)
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(len(data)).encode("ascii"))
            digest.update(b"\0")
            digest.update(data)
    except OSError as exc:
        raise PackageUnavailableError() from exc
    return digest.hexdigest()


def copy_package(src: Path, dst: Path) -> None:
    """Copy a validated package without following a symlink."""

    src = Path(src)
    dst = Path(dst)
    if dst.exists() or dst.is_symlink():
        raise PackageUnavailableError("managed package destination already exists")
    root_stat = src.lstat()
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise PackageUnavailableError("plugin package must be a real directory")
    dst.mkdir(parents=True)
    for current, directories, files in os.walk(src, topdown=True, followlinks=False):
        current_path = Path(current)
        directories[:] = [name for name in sorted(directories) if name != "__pycache__"]
        files[:] = [name for name in sorted(files) if Path(name).suffix not in {".pyc", ".pyo"}]
        safe_directories: list[str] = []
        for name in directories:
            path = current_path / name
            item_stat = path.lstat()
            if stat.S_ISLNK(item_stat.st_mode):
                raise PackageUnavailableError("plugin package cannot contain symlinks")
            if not stat.S_ISDIR(item_stat.st_mode):
                raise PackageUnavailableError("plugin package contains a non-directory")
            safe_directories.append(name)
            (dst / path.relative_to(src)).mkdir(parents=True, exist_ok=False)
        directories[:] = safe_directories
        for name in files:
            source = current_path / name
            item_stat = source.lstat()
            if stat.S_ISLNK(item_stat.st_mode) or not stat.S_ISREG(item_stat.st_mode):
                raise PackageUnavailableError("plugin package contains an unsafe file")
            target = dst / source.relative_to(src)
            shutil.copyfile(source, target, follow_symlinks=False)
            os.chmod(target, stat.S_IMODE(item_stat.st_mode) & 0o755)


def path_under(root: Path, relative: str) -> Path:
    """Resolve a Host-owned relative locator and reject symlink escapes."""

    safe = _safe_relative_path(relative)
    root = Path(root)
    current = root
    for part in Path(safe).parts:
        current = current / part
        try:
            item_stat = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(item_stat.st_mode):
            raise PermissionDeniedError("managed resource path contains a symlink")
    resolved = current.resolve(strict=False)
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise PermissionDeniedError("managed resource path escapes its root") from exc
    return current


@contextmanager
def _managed_parent(path: Path, *, create: bool = False) -> Iterator[tuple[int, str]]:
    """Pin every ancestor without following symlinks, including at open time."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or len(path.parts) < 2:
        raise PermissionDeniedError("managed resource path must be absolute")
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise PermissionDeniedError("safe managed directory access is unavailable")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:-1]:
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                with suppress(FileExistsError):
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor, path.name
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise PermissionDeniedError("managed resource directory changed or is unsafe") from exc
    finally:
        os.close(descriptor)


def ensure_managed_resource(path: Path, *, directory: bool = False) -> None:
    with _managed_parent(path, create=True) as (parent, name):
        if directory:
            with suppress(FileExistsError):
                os.mkdir(name, 0o700, dir_fd=parent)
            descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        else:
            descriptor = os.open(
                name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent
            )
        try:
            opened = os.fstat(descriptor)
            if not directory and (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1):
                raise PermissionDeniedError("managed resource must be a private regular file")
        finally:
            os.close(descriptor)


def _read_managed_file(path: Path, *, max_bytes: int = 1_000_000) -> str:
    with _managed_parent(path) as (parent, name):
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise PermissionDeniedError("managed resource must be a private regular file")
            if opened.st_size > max_bytes:
                raise BudgetExceededError("private index read exceeds response budget")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, min(65536, max_bytes - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise BudgetExceededError("private index read exceeds response budget")
                chunks.append(chunk)
            return b"".join(chunks).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PluginProtocolError("private index is not UTF-8") from exc
        finally:
            os.close(descriptor)


def _write_managed_file(path: Path, data: bytes) -> None:
    with _managed_parent(path, create=True) as (parent, name):
        # Check type and hard-link count before truncating any existing inode.
        descriptor = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent
        )
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise PermissionDeniedError("managed resource must be a private regular file")
            os.ftruncate(descriptor, 0)
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _delete_managed_file(path: Path) -> None:
    try:
        with _managed_parent(path) as (parent, name):
            item_stat = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(item_stat.st_mode):
                raise PermissionDeniedError("private index path was replaced")
            os.unlink(name, dir_fd=parent)
    except FileNotFoundError:
        pass


@dataclass(frozen=True)
class HostCallbacks:
    """Core-owned callbacks exposed through the narrow Host API."""

    read_source: Callable[[HostReadRequest], HostReadResult | Awaitable[HostReadResult]] | None = (
        None
    )
    search: Callable[[RecallRequest], CandidateBatch | Awaitable[CandidateBatch]] | None = None
    model: Callable[[ModelProxyRequest], ModelProxyResult | Awaitable[ModelProxyResult]] | None = (
        None
    )


@dataclass
class _CallBudget:
    limits: HostBudget
    calls: int = 0
    request_bytes: int = 0
    response_bytes: int = 0
    log_chars: int = 0
    private_index_bytes: int = 0

    def charge_request(self, size: int) -> None:
        self.calls += 1
        if self.calls > self.limits.max_calls:
            raise BudgetExceededError("plugin call budget exhausted")
        self.request_bytes += size
        if size > self.limits.max_request_bytes:
            raise BudgetExceededError("plugin request exceeds the configured byte limit")

    def charge_response(self, size: int) -> None:
        self.response_bytes += size
        if size > self.limits.max_response_bytes:
            raise BudgetExceededError("plugin response exceeds the configured byte limit")

    def charge_log(self, size: int) -> bool:
        if self.log_chars + size > self.limits.max_log_chars:
            remaining = max(0, self.limits.max_log_chars - self.log_chars)
            self.log_chars += min(size, remaining)
            return False
        self.log_chars += size
        return True

    def charge_private_index(self, size: int) -> None:
        if self.private_index_bytes + size > self.limits.max_private_index_bytes:
            raise BudgetExceededError("private index budget exhausted")
        self.private_index_bytes += size


class RestrictedHostApi:
    """The only API object visible to an in-process plugin or stdio bridge."""

    def __init__(
        self,
        *,
        context: RpcContext,
        installation_root: Path,
        callbacks: HostCallbacks,
        limits: HostBudget,
        resource_lookup: Callable[[str], tuple[PrivateIndexResource, Path] | None],
        resource_register: Callable[[str, str, bool], PrivateIndexResource],
        resource_revision: Callable[[str], int],
        resource_bump: Callable[[str], int],
        cancel_event: asyncio.Event,
        validate_active: Callable[[], None] | None = None,
    ) -> None:
        self.context = context
        self._installation_root = Path(installation_root)
        self._callbacks = callbacks
        self._budget = _CallBudget(limits)
        self._resource_lookup = resource_lookup
        self._resource_register = resource_register
        self._resource_revision = resource_revision
        self._resource_bump = resource_bump
        self._cancel_event = cancel_event
        self._validate_active = validate_active
        self._logs: list[str] = []

    @property
    def logs(self) -> tuple[str, ...]:
        return tuple(self._logs)

    def check_cancelled(self) -> None:
        if self._validate_active is not None:
            self._validate_active()
        if self._cancel_event.is_set():
            raise CancellationError()
        if utc_now() >= self.context.deadline:
            raise DeadlineExceededError()

    def log(self, message: str, *, level: str = "info") -> None:
        self.check_cancelled()
        if not isinstance(message, str) or not isinstance(level, str):
            raise PluginProtocolError("plugin log must be text")
        text = f"{level}: {message}"[:2_000]
        if self._budget.charge_log(len(text)):
            self._logs.append(text)

    def register_resource(
        self,
        *,
        relative_path: str,
        category: str = "state",
        reconstructible: bool = True,
    ) -> PrivateIndexResource:
        """Register a Host-managed private index/state locator.

        The callback receives only a relative locator and returns a typed
        resource.  It cannot grant a plugin a raw file path or a Core row.
        """

        self.check_cancelled()
        return self._resource_register(relative_path, category, reconstructible)

    async def read_source(self, request: HostReadRequest) -> HostReadResult:
        self._check_context(request.context)
        self.check_cancelled()
        self._budget.charge_request(len(canonical_json(request.model_dump(mode="json"))))
        callback = self._callbacks.read_source
        if callback is None:
            raise PermissionDeniedError("source reads are not configured for this host")
        result = callback(request)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, HostReadResult):
            raise PluginProtocolError("read_source callback returned an invalid result")
        self._budget.charge_response(len(canonical_json(result.model_dump(mode="json"))))
        return result

    async def search(self, request: RecallRequest) -> CandidateBatch:
        self._check_context(request.context)
        self.check_cancelled()
        self._budget.charge_request(len(canonical_json(request.model_dump(mode="json"))))
        callback = self._callbacks.search
        if callback is None:
            raise PermissionDeniedError("search is not configured for this host")
        result = callback(request)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, CandidateBatch):
            raise PluginProtocolError("search callback returned an invalid result")
        self._budget.charge_response(len(canonical_json(result.model_dump(mode="json"))))
        return result

    async def model(self, request: ModelProxyRequest) -> ModelProxyResult:
        self._check_context(request.context)
        self.check_cancelled()
        self._budget.charge_request(len(canonical_json(request.model_dump(mode="json"))))
        callback = self._callbacks.model
        if callback is None:
            raise PermissionDeniedError("model proxy is not configured for this host")
        result = callback(request)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, ModelProxyResult):
            raise PluginProtocolError("model callback returned an invalid result")
        self._budget.charge_response(len(canonical_json(result.model_dump(mode="json"))))
        return result

    def private_index(self, request: PrivateIndexRequest) -> PrivateIndexResult:
        self._check_context(request.context)
        self.check_cancelled()
        self._budget.charge_request(len(canonical_json(request.model_dump(mode="json"))))
        if request.resource.installation_id != self.context.installation_id:
            raise PermissionDeniedError("private index belongs to another installation")
        found = self._resource_lookup(request.resource.resource_id)
        if found is None:
            raise PermissionDeniedError("private index resource is not registered by Host")
        resource, path = found
        if resource != request.resource:
            raise PermissionDeniedError("private index resource metadata changed")
        current_revision = self._resource_revision(resource.resource_id)
        if request.expected_revision != current_revision:
            raise PluginError("revision_conflict", "private index revision is stale")
        if request.operation == "read":
            try:
                data = _read_managed_file(path, max_bytes=self._budget.limits.max_response_bytes)
            except FileNotFoundError:
                result = PrivateIndexResult(
                    resource_id=resource.resource_id,
                    revision=self._resource_revision(resource.resource_id),
                    content_digest=None,
                    payload=None,
                )
            else:
                result = PrivateIndexResult(
                    resource_id=resource.resource_id,
                    revision=self._resource_revision(resource.resource_id),
                    content_digest=hashlib.sha256(data.encode("utf-8")).hexdigest(),
                    payload=data,
                )
        elif request.operation == "delete":
            _delete_managed_file(path)
            revision = self._resource_bump(resource.resource_id)
            result = PrivateIndexResult(
                resource_id=resource.resource_id,
                revision=revision,
                content_digest=None,
                payload=None,
            )
        else:
            assert request.payload is not None
            encoded = request.payload.encode("utf-8")
            self._budget.charge_private_index(len(encoded))
            _write_managed_file(path, encoded)
            revision = self._resource_bump(resource.resource_id)
            result = PrivateIndexResult(
                resource_id=resource.resource_id,
                revision=revision,
                content_digest=hashlib.sha256(encoded).hexdigest(),
                payload=None,
            )
        self._budget.charge_response(len(canonical_json(result.model_dump(mode="json"))))
        return result

    def _check_context(self, context: RpcContext) -> None:
        if context != self.context:
            raise StaleEpochError("nested Host API context does not match the active request")

    async def dispatch(self, method: str, params: Mapping[str, Any]) -> BaseModel:
        """Dispatch a stdio plugin's Host API request through typed models."""

        operation = method.removeprefix("host.")
        request_type = HOST_REQUEST_TYPES.get(operation)
        if request_type is None:
            raise PluginProtocolError("unsupported Host API method")
        try:
            request = request_type.model_validate(params)
        except Exception as exc:
            raise PluginProtocolError("Host API request failed schema validation") from exc
        if operation == "read_source":
            return await self.read_source(request)  # type: ignore[arg-type]
        if operation == "search":
            return await self.search(request)  # type: ignore[arg-type]
        if operation == "model":
            return await self.model(request)  # type: ignore[arg-type]
        return self.private_index(request)  # type: ignore[arg-type]


class PluginImplementation(Protocol):
    """Minimal in-process adapter contract."""

    def handle(
        self, operation: str, request: BaseModel, host: RestrictedHostApi
    ) -> BaseModel | Awaitable[BaseModel]: ...


def _ensure_result(operation: str, result: BaseModel) -> BaseModel:
    expected = ENGINE_RESULT_TYPES.get(operation)
    if expected is None or not isinstance(result, expected):
        raise PluginProtocolError(f"plugin returned an invalid result for {operation}")
    if isinstance(result, (CandidateBatch, ProposalBatch)) and not result.request_id:
        raise PluginProtocolError("plugin result request_id is empty")
    return result


class InProcessPluginEngine:
    """Trusted, certification-gated adapter using the same RestrictedHostApi."""

    def __init__(self, implementation: PluginImplementation) -> None:
        self.implementation = implementation
        self.started = False

    async def start(self) -> None:
        self.started = True

    async def invoke(
        self, operation: str, request: BaseModel, host: RestrictedHostApi
    ) -> BaseModel:
        if not self.started:
            raise PackageUnavailableError("in-process plugin is not started")
        host.check_cancelled()
        try:
            result = self.implementation.handle(operation, request, host)
            if inspect.isawaitable(result):
                result = await result
        except PluginError:
            raise
        except asyncio.CancelledError as exc:
            raise CancellationError() from exc
        except Exception as exc:
            raise PluginError("plugin_failed", "in-process plugin failed") from exc
        host.check_cancelled()
        return _ensure_result(operation, result)

    async def cancel(self, _request_id: str) -> None:
        # Cooperative cancellation is delivered by RestrictedHostApi.  A
        # plugin that cannot observe it will be marked restart-required by Host.
        return None

    async def close(self) -> None:
        close = getattr(self.implementation, "close", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result
        self.started = False


class StdioPluginEngine:
    """Reusable bounded JSON-RPC engine with bidirectional Host callbacks."""

    def __init__(
        self,
        *,
        argv: Sequence[str],
        package_root: Path,
        mode: str,
        sandbox_probe: SandboxProbe,
        data_root: Path,
        state_root: Path,
        logs_root: Path,
        tmp_root: Path,
        executable: Path | None = None,
        limits: HostBudget | None = None,
        sandbox_evidence: SandboxEvidence | None = None,
        failure_callback: Callable[[str], object] | None = None,
    ) -> None:
        if not argv or any(
            not isinstance(item, str) or not item or "\x00" in item for item in argv
        ):
            raise PluginProtocolError("stdio argv is invalid")
        if mode not in {"isolated", "trusted_in_process"}:
            raise PluginProtocolError("unknown plugin execution mode")
        self.argv = tuple(argv)
        self.package_root = Path(package_root)
        self.mode = mode
        self.sandbox_probe = sandbox_probe
        self.data_root = Path(data_root)
        self.state_root = Path(state_root)
        self.logs_root = Path(logs_root)
        self.tmp_root = Path(tmp_root)
        self.executable = Path(executable or sys.executable)
        self.limits = limits or HostBudget()
        self._failure_callback = failure_callback
        self._resource_error: PluginError | None = None
        self._watchdog_task: asyncio.Task[None] | None = None
        self._last_activity = time.monotonic()
        self.process: asyncio.subprocess.Process | None = None
        self._request_id = 0
        self._lock = asyncio.Lock()
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr = ""
        self._active: dict[str, asyncio.Event] = {}
        self.sandbox_evidence: SandboxEvidence | None = sandbox_evidence

    @property
    def stderr(self) -> str:
        return self._stderr

    async def start(self) -> None:
        if self._resource_error is not None:
            raise self._resource_error
        if self.process is not None and self.process.returncode is None:
            return
        self.package_root = self.package_root.resolve(strict=True)
        if self.mode == "isolated":
            if self.sandbox_evidence is None:
                self.sandbox_evidence = self.sandbox_probe.check(
                    package_root=self.package_root,
                    data_root=self.data_root,
                    state_root=self.state_root,
                    logs_root=self.logs_root,
                    tmp_root=self.tmp_root,
                    executable=self.executable,
                )
            assert self.sandbox_evidence is not None
            command = [
                self.sandbox_evidence.runner,
                "-p",
                self.sandbox_evidence.profile,
                *self.argv,
            ]
        else:
            command = list(self.argv)
        environment = {"PATH": os.environ.get("PATH", ""), "PYTHONUNBUFFERED": "1"}
        try:
            self.process = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *command,
                    cwd=self.package_root,
                    start_new_session=True,
                    env=environment,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=self.limits.max_response_bytes + 1,
                ),
                timeout=self.limits.timeout_seconds,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            self.process = None
            raise PackageUnavailableError("stdio plugin process could not start") from exc
        assert self.process.stderr is not None
        self._stderr_task = asyncio.create_task(self._drain_stderr(self.process.stderr))
        self._last_activity = time.monotonic()
        self._watchdog_task = asyncio.create_task(self._watch_resources())

    async def _watch_resources(self) -> None:
        process = self.process
        assert process is not None
        try:
            while process.returncode is None:
                await asyncio.sleep(0.2)
                if process.returncode is not None:
                    return
                probe = await asyncio.create_subprocess_exec(
                    "/bin/ps",
                    "-o",
                    "rss=,time=",
                    "-p",
                    str(process.pid),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                try:
                    output, _ = await asyncio.wait_for(probe.communicate(), 2)
                except asyncio.TimeoutError:
                    probe.kill()
                    await probe.wait()
                    raise PackageUnavailableError("resource monitor unavailable") from None
                if process.returncode is not None:
                    return
                fields = output.decode("ascii").split()
                if probe.returncode or len(fields) != 2:
                    raise PackageUnavailableError("resource monitor unavailable")
                rss_kb = int(fields[0])
                cpu = 0.0
                for part in fields[1].split(":"):
                    cpu = cpu * 60 + float(part)
                if rss_kb > self.limits.memory_mb * 1024 or cpu > self.limits.max_cpu_seconds:
                    raise BudgetExceededError("isolated plugin exceeded RSS or CPU budget")
                if (
                    not self._active
                    and time.monotonic() - self._last_activity > self.limits.max_idle_seconds
                ):
                    raise DeadlineExceededError("isolated plugin exceeded idle lifetime")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._resource_error = (
                exc
                if isinstance(exc, PluginError)
                else PackageUnavailableError("resource monitor unavailable")
            )
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
            if self._failure_callback is not None:
                self._failure_callback(self._resource_error.code)

    async def invoke(
        self, operation: str, request: BaseModel, host: RestrictedHostApi
    ) -> BaseModel:
        if operation not in ENGINE_REQUEST_TYPES:
            raise PluginProtocolError("unsupported plugin operation")
        await self.start()
        process = self.process
        if process is None or process.returncode is not None:
            raise PackageUnavailableError("stdio plugin process is not running")
        encoded_request = request.model_dump(mode="json")
        context = encoded_request.get("context")
        if not isinstance(context, dict):
            raise PluginProtocolError("engine request is missing RpcContext")
        request_id = str(context.get("request_id", ""))
        if not request_id:
            raise PluginProtocolError("engine request has no request_id")
        async with self._lock:
            self._last_activity = time.monotonic()
            self._active[request_id] = host._cancel_event
            self._request_id += 1
            rpc_id = self._request_id
            payload = {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "method": operation,
                "params": encoded_request,
            }
            try:
                await self._write(payload)
                response = await self._read_until_response(rpc_id, host)
            except asyncio.CancelledError as exc:
                raise CancellationError() from exc
            finally:
                self._active.pop(request_id, None)
                self._last_activity = time.monotonic()
        if "error" in response:
            raise PluginError("plugin_failed", "stdio plugin returned an error")
        result_value = response.get("result")
        if not isinstance(result_value, dict):
            raise PluginProtocolError("stdio plugin response result must be an object")
        result_type = ENGINE_RESULT_TYPES[operation]
        try:
            result = result_type.model_validate(result_value)
        except Exception as exc:
            raise PluginProtocolError("stdio plugin result failed schema validation") from exc
        return _ensure_result(operation, result)

    async def _write(self, payload: Mapping[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None:
            raise PackageUnavailableError("stdio plugin stdin is unavailable")
        encoded = encode_rpc(payload, max_bytes=self.limits.max_request_bytes)
        process.stdin.write(encoded)
        try:
            await asyncio.wait_for(process.stdin.drain(), timeout=self.limits.timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise DeadlineExceededError() from exc

    async def _read_until_response(self, rpc_id: int, host: RestrictedHostApi) -> dict[str, Any]:
        process = self.process
        if process is None or process.stdout is None:
            raise PackageUnavailableError("stdio plugin stdout is unavailable")
        while True:
            if host._cancel_event.is_set():
                raise CancellationError()
            try:
                line = await asyncio.wait_for(
                    process.stdout.readline(), self.limits.timeout_seconds
                )
            except asyncio.TimeoutError as exc:
                raise DeadlineExceededError() from exc
            if self._resource_error is not None:
                raise self._resource_error
            if not line:
                if host._cancel_event.is_set():
                    raise CancellationError()
                raise PackageUnavailableError("stdio plugin exited before its response")
            frame = decode_rpc(line, max_bytes=self.limits.max_response_bytes)
            if "method" in frame:
                method = frame.get("method")
                params = frame.get("params")
                callback_id = frame.get("id")
                if (
                    not isinstance(method, str)
                    or not isinstance(params, dict)
                    or not isinstance(callback_id, int)
                ):
                    raise PluginProtocolError("stdio Host callback frame is invalid")
                try:
                    callback_result = await host.dispatch(method, params)
                    callback_payload = {
                        "jsonrpc": "2.0",
                        "id": callback_id,
                        "result": callback_result.model_dump(mode="json"),
                    }
                except PluginError as exc:
                    callback_payload = {
                        "jsonrpc": "2.0",
                        "id": callback_id,
                        "error": {"code": exc.code, "message": str(exc)},
                    }
                await self._write(callback_payload)
                continue
            if frame.get("id") != rpc_id:
                raise PluginProtocolError("stdio response ID does not match request")
            return frame

    async def cancel(self, request_id: str) -> None:
        event = self._active.get(request_id)
        if event is not None:
            event.set()
        # A blocked stdio read cannot be safely interrupted by an untrusted
        # plugin.  Terminate this process; the next call explicitly respawns it
        # after a fresh admission check.
        process = self.process
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 2)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        self.process = None

    async def close(self) -> None:
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            await asyncio.gather(self._watchdog_task, return_exceptions=True)
            self._watchdog_task = None
        process = self.process
        self.process = None
        if process is not None:
            if process.stdin is not None:
                process.stdin.close()
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), 2)
                except asyncio.TimeoutError:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
        if self._stderr_task is not None:
            try:
                await asyncio.wait_for(self._stderr_task, 2)
            except asyncio.TimeoutError:
                self._stderr_task.cancel()
            self._stderr_task = None

    async def _drain_stderr(self, stream: asyncio.StreamReader) -> None:
        captured = bytearray()
        byte_limit = self.limits.max_log_chars * 4
        while True:
            chunk = await stream.read(4_096)
            if not chunk:
                break
            if len(captured) < byte_limit:
                captured.extend(chunk[: byte_limit - len(captured)])
        self._stderr = captured.decode("utf-8", errors="replace")[: self.limits.max_log_chars]


class StdioHostClient:
    """Small helper for example plugins using the bidirectional protocol."""

    def __init__(self, stdin: Any, stdout: Any, *, max_bytes: int = 1_000_000) -> None:
        self.stdin = stdin
        self.stdout = stdout
        self.max_bytes = max_bytes
        self._request_id = 0

    def request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        self._request_id += 1
        frame = {"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": dict(params)}
        self.stdin.write(encode_rpc(frame, max_bytes=self.max_bytes).decode("utf-8"))
        self.stdin.flush()
        line = self.stdout.readline()
        if not line:
            raise RuntimeError("Host API response ended unexpectedly")
        response = decode_rpc(line.encode("utf-8"), max_bytes=self.max_bytes)
        if response.get("id") != self._request_id:
            raise RuntimeError("Host API response ID mismatch")
        if "error" in response:
            error = response["error"]
            raise RuntimeError(str(error))
        result = response.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("Host API result is invalid")
        return result


__all__ = [
    "ENGINE_REQUEST_TYPES",
    "ENGINE_RESULT_TYPES",
    "HOST_REQUEST_TYPES",
    "HOST_RESULT_TYPES",
    "BudgetExceededError",
    "CancellationError",
    "CleanupBlockedError",
    "DeadlineExceededError",
    "HostBudget",
    "HostCallbacks",
    "InProcessPluginEngine",
    "IsolationUnavailableError",
    "PackageUnavailableError",
    "PermissionDeniedError",
    "PluginError",
    "PluginImplementation",
    "PluginProtocolError",
    "RestrictedHostApi",
    "RestartRequiredError",
    "SandboxEvidence",
    "SandboxProbe",
    "SourceRef",
    "StaleEpochError",
    "StdioHostClient",
    "StdioPluginEngine",
    "build_sandbox_profile",
    "canonical_json",
    "compute_package_digest",
    "copy_package",
    "decode_rpc",
    "digest_payload",
    "encode_rpc",
    "path_under",
]
