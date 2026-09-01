"""Phase 1E read projections and fail-closed workspace directory browsing."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any

from operant.domain.commands import WorkspaceInitialization
from operant.domain.projections import (
    ProjectProjection,
    ProjectThreadSummary,
    ProjectWorkflowRunSummary,
    WorkspaceFileEntry,
    WorkspaceFilesPage,
)
from operant.persistence.sqlite import SQLiteStore
from operant.protocol import redact_public_text
from operant.tools.execution import is_protected_workspace_name

MAX_WORKSPACE_FILE_PAGE_SIZE = 200
MAX_WORKSPACE_FILE_PATH_CHARS = 4_096
MAX_WORKSPACE_FILE_NAME_CHARS = 255


class WorkspaceProjectionError(RuntimeError):
    """Safe, transport-neutral failure from a workspace projection query."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        recovery: str = "none",
    ) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        self.recovery = recovery
        super().__init__(message)


class _WorkspacePathError(WorkspaceProjectionError):
    pass


class _WorkspaceCaseAliasError(_WorkspacePathError):
    def __init__(self) -> None:
        super().__init__(
            "workspace_path_case_mismatch",
            "workspace path component does not match the registered case",
        )


class _WorkspaceDirectoryChanged(WorkspaceProjectionError):
    def __init__(self) -> None:
        super().__init__(
            "workspace_directory_changed",
            "workspace directory changed; refresh the directory listing",
            status_code=409,
            recovery="refresh_and_retry",
        )


class _WorkspacePageTokenError(WorkspaceProjectionError):
    def __init__(self) -> None:
        super().__init__(
            "workspace_page_token_invalid",
            "workspace directory page token is invalid or expired",
            status_code=400,
            recovery="refresh_and_retry",
        )


@dataclass(frozen=True)
class _DirectoryEntry:
    name: str
    entry_type: str
    size_bytes: int | None
    modified_at: datetime | None
    # These identity values are private and are used only for detecting a
    # replacement while a page is being read. They are never serialized.
    device: int
    inode: int
    mode: int
    mtime_ns: int


@dataclass(frozen=True)
class _DirectoryScan:
    path: str
    entries: tuple[_DirectoryEntry, ...]
    snapshot: str
    identity: tuple[int, int]


def list_project_projections(
    store: SQLiteStore,
    *,
    after_cursor: int | None = None,
    limit: int = 100,
) -> list[ProjectProjection]:
    """Aggregate only persisted workspace, Thread and Workflow facts.

    Workflow Run stores its normalized absolute workspace in ``workspace``;
    comparison is deliberately exact and does not resolve either side. That
    prevents aliases and guessed legacy mappings from entering the projection.
    """

    initializations = store.list_workspace_initializations(
        after_cursor=after_cursor,
        limit=limit,
    )
    projects: list[ProjectProjection] = []
    for initialization in initializations:
        workspace_ref = initialization.workspace_ref
        threads = store.list_threads(workspace_ref=workspace_ref, limit=1000)
        workflow_runs = store.list_workflow_runs(workspace_ref=workspace_ref, limit=None)
        projects.append(
            ProjectProjection(
                project_id=initialization.id,
                workspace_ref=workspace_ref,
                readable=initialization.readable,
                writable=initialization.writable,
                created_at=initialization.created_at,
                threads=tuple(
                    ProjectThreadSummary(
                        id=thread.id,
                        status=thread.status.value,
                        created_at=thread.created_at,
                        updated_at=thread.updated_at,
                    )
                    for thread in threads
                ),
                workflow_runs=tuple(
                    ProjectWorkflowRunSummary(
                        id=run.id,
                        status=run.status.value,
                        current_stage=run.current_stage.value,
                        summary=redact_public_text(run.task, max_chars=500),
                        created_at=run.created_at,
                        updated_at=run.updated_at,
                    )
                    for run in workflow_runs
                ),
            )
        )
    return projects


def get_workspace_initialization(store: SQLiteStore, workspace_id: str) -> WorkspaceInitialization:
    """Load a workspace by its durable initialization ID."""

    return store.get_workspace_initialization_by_id(workspace_id)


def list_workspace_files(
    initialization: WorkspaceInitialization,
    *,
    path: str = ".",
    limit: int = 100,
    page_token: str | None = None,
    snapshot: str | None = None,
    after_name: str | None = None,
    expected_identity: tuple[int, int] | None = None,
) -> tuple[WorkspaceFilesPage, tuple[int, int]]:
    """List safe immediate directory metadata with deterministic pagination.

    The returned ``snapshot`` is a directory manifest digest, not an event
    cursor. A page request carrying a token/snapshot is rejected when the
    manifest no longer matches, so callers must refresh instead of pretending
    that a stale page can be replayed.
    """

    if isinstance(limit, bool) or not 1 <= limit <= MAX_WORKSPACE_FILE_PAGE_SIZE:
        raise WorkspaceProjectionError(
            "workspace_page_limit_invalid",
            f"workspace page limit must be between 1 and {MAX_WORKSPACE_FILE_PAGE_SIZE}",
        )
    if not initialization.readable:
        raise WorkspaceProjectionError(
            "workspace_not_readable",
            "registered workspace is not readable",
            status_code=403,
        )

    requested_parts = _validate_relative_path(path)
    token_data = _decode_page_token(page_token) if page_token is not None else None
    if token_data is not None:
        if (
            token_data.get("workspace_id") != initialization.id
            or token_data.get("path") != _display_path(requested_parts)
            or not isinstance(token_data.get("snapshot"), str)
            or not isinstance(token_data.get("after_name"), str)
        ):
            raise _WorkspacePageTokenError()
        token_snapshot = token_data["snapshot"]
        token_after_name = token_data["after_name"]
        if snapshot is not None and snapshot != token_snapshot:
            raise _WorkspacePageTokenError()
        snapshot = token_snapshot
        if after_name is not None and after_name != token_after_name:
            raise _WorkspacePageTokenError()
        after_name = token_after_name
    if snapshot is not None and not _is_digest(snapshot):
        raise _WorkspacePageTokenError()
    if after_name is not None:
        _validate_page_name(after_name)

    directory_fd, canonical_parts, root_identity = _open_directory(
        initialization.workspace_ref, requested_parts
    )
    try:
        first_scan = _scan_directory(directory_fd, canonical_parts)
        if expected_identity is not None and root_identity != expected_identity:
            raise _WorkspaceDirectoryChanged()
        if snapshot is not None and snapshot != first_scan.snapshot:
            raise _WorkspaceDirectoryChanged()

        start_index = 0
        if after_name is not None:
            names = [entry.name for entry in first_scan.entries]
            try:
                start_index = names.index(after_name) + 1
            except ValueError as exc:
                # A missing page anchor is indistinguishable from directory
                # mutation to the caller; force a fresh projection.
                raise _WorkspaceDirectoryChanged() from exc

        page_entries = first_scan.entries[start_index : start_index + limit]
        next_page_token: str | None = None
        next_index = start_index + len(page_entries)
        if next_index < len(first_scan.entries):
            next_page_token = _encode_page_token(
                {
                    "version": 1,
                    "workspace_id": initialization.id,
                    "path": first_scan.path,
                    "snapshot": first_scan.snapshot,
                    "after_name": page_entries[-1].name,
                }
            )

        # A second manifest check catches ordinary directory membership and
        # metadata races. The fd keeps the directory itself pinned while the
        # check runs, and identity changes are treated as refresh-required.
        second_scan = _scan_directory(directory_fd, canonical_parts)
        if (
            second_scan.identity != first_scan.identity
            or second_scan.snapshot != first_scan.snapshot
        ):
            raise _WorkspaceDirectoryChanged()
        _ensure_root_reference_unchanged(initialization.workspace_ref, root_identity)
        return (
            WorkspaceFilesPage(
                workspace_id=initialization.id,
                path=first_scan.path,
                entries=tuple(
                    WorkspaceFileEntry(
                        path=_entry_path(first_scan.path, entry.name),
                        name=entry.name,
                        type=entry.entry_type,  # type: ignore[arg-type]
                        size_bytes=entry.size_bytes,
                        modified_at=entry.modified_at,
                    )
                    for entry in page_entries
                ),
                snapshot=first_scan.snapshot,
                next_page_token=next_page_token,
            ),
            root_identity,
        )
    finally:
        os.close(directory_fd)


def _validate_relative_path(path: str) -> tuple[str, ...]:
    if not isinstance(path, str) or not path or len(path) > MAX_WORKSPACE_FILE_PATH_CHARS:
        raise _WorkspacePathError(
            "workspace_path_invalid",
            "workspace path must be a bounded relative path",
        )
    if "\x00" in path or "\\" in path:
        raise _WorkspacePathError("workspace_path_invalid", "workspace path is invalid")
    if Path(path).is_absolute():
        raise _WorkspacePathError("workspace_path_absolute", "workspace path must be relative")
    windows_path = PureWindowsPath(path)
    if windows_path.is_absolute() or bool(windows_path.drive):
        raise _WorkspacePathError("workspace_path_absolute", "workspace path must be relative")
    if path == ".":
        return ()
    raw_parts = path.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise _WorkspacePathError(
            "workspace_path_invalid", "workspace path contains invalid components"
        )
    if any(len(part) > MAX_WORKSPACE_FILE_NAME_CHARS for part in raw_parts):
        raise _WorkspacePathError("workspace_path_invalid", "workspace path component is too long")
    if any(_is_sensitive_name(part) for part in raw_parts):
        raise WorkspaceProjectionError(
            "workspace_path_forbidden",
            "workspace path names a protected resource",
            status_code=403,
        )
    return tuple(raw_parts)


def _display_path(parts: tuple[str, ...]) -> str:
    return "." if not parts else "/".join(parts)


def _validate_page_name(name: str) -> None:
    if (
        not name
        or len(name) > MAX_WORKSPACE_FILE_NAME_CHARS
        or "/" in name
        or "\\" in name
        or "\x00" in name
        or name in {".", ".."}
        or _is_sensitive_name(name)
    ):
        raise _WorkspacePageTokenError()


def _open_directory(
    root_ref: str,
    parts: tuple[str, ...],
) -> tuple[int, tuple[str, ...], tuple[int, int]]:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(root_ref, flags)
    except (FileNotFoundError, NotADirectoryError):
        raise WorkspaceProjectionError(
            "workspace_unavailable",
            "registered workspace directory is unavailable",
            status_code=409,
            recovery="refresh_and_retry",
        ) from None
    except PermissionError:
        raise WorkspaceProjectionError(
            "workspace_not_readable",
            "registered workspace is not readable",
            status_code=403,
        ) from None
    except OSError:
        raise WorkspaceProjectionError(
            "workspace_unavailable",
            "registered workspace directory is unavailable",
            status_code=409,
            recovery="refresh_and_retry",
        ) from None

    root_stat = os.fstat(fd)
    if not stat.S_ISDIR(root_stat.st_mode):
        os.close(fd)
        raise WorkspaceProjectionError(
            "workspace_unavailable",
            "registered workspace directory is unavailable",
            status_code=409,
            recovery="refresh_and_retry",
        )
    root_identity = (int(root_stat.st_dev), int(root_stat.st_ino))
    canonical_parts: list[str] = []
    try:
        for requested in parts:
            match = _find_exact_child(fd, requested)
            if match is None:
                raise WorkspaceProjectionError(
                    "workspace_path_not_found",
                    "workspace directory does not exist",
                    status_code=404,
                )
            if match.name != requested:
                raise _WorkspaceCaseAliasError()
            try:
                child_stat = os.stat(match.name, dir_fd=fd, follow_symlinks=False)
            except (FileNotFoundError, NotADirectoryError):
                raise _WorkspaceDirectoryChanged() from None
            if stat.S_ISLNK(child_stat.st_mode):
                raise WorkspaceProjectionError(
                    "workspace_symlink_forbidden",
                    "workspace path cannot contain a symbolic link",
                    status_code=403,
                )
            if not stat.S_ISDIR(child_stat.st_mode):
                raise WorkspaceProjectionError(
                    "workspace_path_not_directory",
                    "workspace path is not a directory",
                    status_code=400,
                )
            try:
                child_fd = os.open(match.name, flags, dir_fd=fd)
            except FileNotFoundError:
                raise _WorkspaceDirectoryChanged() from None
            except OSError as exc:
                if exc.errno in {getattr(os, "ELOOP", 62), getattr(os, "ENOTDIR", 20)}:
                    raise WorkspaceProjectionError(
                        "workspace_symlink_forbidden",
                        "workspace path cannot contain a symbolic link",
                        status_code=403,
                    ) from None
                raise _WorkspaceDirectoryChanged() from None
            try:
                opened_stat = os.fstat(child_fd)
                if (opened_stat.st_dev, opened_stat.st_ino) != (
                    child_stat.st_dev,
                    child_stat.st_ino,
                ):
                    raise _WorkspaceDirectoryChanged()
            except BaseException:
                os.close(child_fd)
                raise
            os.close(fd)
            fd = child_fd
            canonical_parts.append(match.name)
        return fd, tuple(canonical_parts), root_identity
    except BaseException:
        os.close(fd)
        raise


def _find_exact_child(fd: int, requested: str) -> os.DirEntry[str] | None:
    alias: os.DirEntry[str] | None = None
    try:
        with os.scandir(fd) as entries:
            for entry in entries:
                if entry.name == requested:
                    return entry
                if _name_alias(entry.name, requested):
                    alias = entry
    except PermissionError:
        raise WorkspaceProjectionError(
            "workspace_not_readable",
            "workspace directory is not readable",
            status_code=403,
        ) from None
    if alias is not None:
        return alias
    return None


def _name_alias(existing: str, requested: str) -> bool:
    return _normalized_name(existing).casefold() == _normalized_name(requested).casefold()


def _normalized_name(name: str) -> str:
    return unicodedata.normalize("NFC", name)


def _scan_directory(fd: int, canonical_parts: tuple[str, ...]) -> _DirectoryScan:
    try:
        directory_stat = os.fstat(fd)
        entries: list[_DirectoryEntry] = []
        with os.scandir(fd) as scandir_entries:
            for entry in scandir_entries:
                name = entry.name
                if _is_sensitive_name(name):
                    continue
                if not name or len(name) > MAX_WORKSPACE_FILE_NAME_CHARS:
                    continue
                try:
                    item_stat = entry.stat(follow_symlinks=False)
                except (FileNotFoundError, OSError):
                    raise _WorkspaceDirectoryChanged() from None
                mode = item_stat.st_mode
                # Symlink entries are omitted entirely. They are never opened,
                # followed, or surfaced as a browsable file type.
                if stat.S_ISLNK(mode):
                    continue
                if stat.S_ISDIR(mode):
                    entry_type = "directory"
                    size_bytes = None
                elif stat.S_ISREG(mode):
                    entry_type = "file"
                    size_bytes = min(max(int(item_stat.st_size), 0), 2**63 - 1)
                else:
                    # Devices, sockets, fifos, and other special files are not
                    # safe client browsing targets.
                    continue
                modified_at = datetime.fromtimestamp(item_stat.st_mtime, tz=timezone.utc)
                entries.append(
                    _DirectoryEntry(
                        name=name,
                        entry_type=entry_type,
                        size_bytes=size_bytes,
                        modified_at=modified_at,
                        device=int(item_stat.st_dev),
                        inode=int(item_stat.st_ino),
                        mode=int(mode),
                        mtime_ns=int(item_stat.st_mtime_ns),
                    )
                )
    except PermissionError:
        raise WorkspaceProjectionError(
            "workspace_not_readable",
            "workspace directory is not readable",
            status_code=403,
        ) from None
    except (FileNotFoundError, NotADirectoryError):
        raise _WorkspaceDirectoryChanged() from None

    entries.sort(key=lambda item: (item.name.casefold(), item.name))
    manifest = [
        {
            "name": entry.name,
            "type": entry.entry_type,
            "size": entry.size_bytes,
            "mtime_ns": entry.mtime_ns,
            "mode": entry.mode & stat.S_IFMT(entry.mode),
            "device": entry.device,
            "inode": entry.inode,
        }
        for entry in entries
    ]
    serialized = json.dumps(
        {
            "path": _display_path(canonical_parts),
            "directory": {
                "device": int(directory_stat.st_dev),
                "inode": int(directory_stat.st_ino),
                "mtime_ns": int(directory_stat.st_mtime_ns),
            },
            "entries": manifest,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _DirectoryScan(
        path=_display_path(canonical_parts),
        entries=tuple(entries),
        snapshot=hashlib.sha256(serialized).hexdigest(),
        identity=(int(directory_stat.st_dev), int(directory_stat.st_ino)),
    )


def _ensure_root_reference_unchanged(
    root_ref: str,
    expected_identity: tuple[int, int],
) -> None:
    """Reject a path replacement discovered after the directory was opened."""

    try:
        root_stat = os.stat(root_ref, follow_symlinks=False)
    except OSError:
        raise _WorkspaceDirectoryChanged() from None
    if (
        not stat.S_ISDIR(root_stat.st_mode)
        or (
            int(root_stat.st_dev),
            int(root_stat.st_ino),
        )
        != expected_identity
    ):
        raise _WorkspaceDirectoryChanged()


def _entry_path(directory_path: str, name: str) -> str:
    path = f"{directory_path}/{name}" if directory_path != "." else name
    if len(path) > MAX_WORKSPACE_FILE_PATH_CHARS:
        raise WorkspaceProjectionError(
            "workspace_path_invalid",
            "workspace entry path exceeds the metadata size limit",
        )
    return path


def _is_sensitive_name(name: str) -> bool:
    lowered = name.casefold()
    if is_protected_workspace_name(name):
        return True
    if lowered.startswith(".env"):
        return True
    if lowered in {
        ".aws",
        ".azure",
        ".docker",
        ".dockerconfigjson",
        ".gnupg",
        ".kube",
        ".ssh",
        "authorized_keys",
        "client_secret.json",
        "credentials.json",
        "htpasswd",
        "known_hosts",
        "oauth.json",
        "passwords.json",
        "secret.json",
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
        "service-account.json",
        "service_account.json",
    }:
        return True
    if lowered.endswith((".crt", ".cer", ".csr", ".der", ".jks", ".key", ".p12", ".pfx", ".pem")):
        return True
    stem = lowered.rsplit(".", 1)[0] if "." in lowered else lowered
    if stem.startswith("id_") and stem in {"id_dsa", "id_ecdsa", "id_ed25519", "id_rsa"}:
        return True
    tokens = {
        "api-key",
        "api_key",
        "apikey",
        "access-token",
        "client-secret",
        "client_secret",
        "credential",
        "credentials",
        "cookie",
        "authorization",
        "oauth",
        "password",
        "passwd",
        "private-key",
        "private_key",
        "secret",
        "secrets",
        "token",
    }
    normalized = re.sub(r"[._]+", "-", lowered)
    normalized_tokens = {token.replace("_", "-") for token in tokens}
    return any(
        normalized == token
        or normalized.startswith(f"{token}-")
        or normalized.endswith(f"-{token}")
        or f"-{token}-" in normalized
        for token in normalized_tokens
    )


def _encode_page_token(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_page_token(token: str) -> dict[str, Any]:
    if not token or len(token) > 2_000:
        raise _WorkspacePageTokenError()
    try:
        padding = "=" * (-len(token) % 4)
        decoded = base64.b64decode(token + padding, altchars=b"-_", validate=True)
        payload = json.loads(decoded)
    except (ValueError, UnicodeError, json.JSONDecodeError, binascii.Error):
        raise _WorkspacePageTokenError() from None
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise _WorkspacePageTokenError()
    return payload


def _is_digest(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)
