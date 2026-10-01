"""Bounded, workspace-scoped text and Git diff previews."""

from __future__ import annotations

import contextlib
import hashlib
import os
import select
import stat
import subprocess
import sys
import time

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel

from operant.application.client_projection import (
    WorkspaceProjectionError,
    _ensure_root_reference_unchanged,
    _find_exact_child,
    _open_directory,
    _validate_relative_path,
    _WorkspaceCaseAliasError,
)
from operant.application.service import ApplicationService
from operant.persistence.sqlite import NotFoundError
from operant.protocol import redact_public_text

MAX_PREVIEW_BYTES = 128_000


class WorkbenchFileContent(BaseModel):
    workspace_id: str
    path: str
    content: str
    content_hash: str  # SHA-256 of the returned preview bytes, not a full file version
    hash_scope: str = "preview"
    size_bytes: int
    truncated: bool


class WorkbenchFileDiff(BaseModel):
    workspace_id: str
    path: str
    diff: str
    truncated: bool


def _workspace(service: ApplicationService, workspace_id: str) -> str:
    record = service.store.get_workspace_initialization_by_id(workspace_id)
    if not record.readable:
        raise WorkspaceProjectionError(
            "workspace_not_readable", "registered workspace is not readable", status_code=403
        )
    return record.workspace_ref


def _file_descriptor(root: str, path: str) -> tuple[int, tuple[int, int]]:
    parts = _validate_relative_path(path)
    if not parts:
        raise WorkspaceProjectionError("workspace_path_invalid", "file path is required")
    parent_fd, _, root_identity = _open_directory(root, parts[:-1])
    try:
        match = _find_exact_child(parent_fd, parts[-1])
        if match is None:
            raise WorkspaceProjectionError(
                "workspace_path_not_found", "workspace file does not exist", status_code=404
            )
        if match.name != parts[-1]:
            raise _WorkspaceCaseAliasError()
        try:
            expected = os.stat(match.name, dir_fd=parent_fd, follow_symlinks=False)
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise WorkspaceProjectionError(
                "workspace_file_changed",
                "workspace file changed; refresh the file listing",
                status_code=409,
            ) from exc
        except OSError as exc:
            raise WorkspaceProjectionError(
                "workspace_file_unavailable", "workspace file cannot be opened", status_code=403
            ) from exc
        if stat.S_ISLNK(expected.st_mode):
            raise WorkspaceProjectionError(
                "workspace_symlink_forbidden",
                "workspace path cannot contain a symbolic link",
                status_code=403,
            )
        if not stat.S_ISREG(expected.st_mode):
            raise WorkspaceProjectionError(
                "workspace_file_not_regular",
                "workspace path is not a regular file",
                status_code=400,
            )
        try:
            fd = os.open(
                match.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=parent_fd,
            )
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise WorkspaceProjectionError(
                "workspace_path_not_found", "workspace file does not exist", status_code=404
            ) from exc
        except OSError as exc:
            raise WorkspaceProjectionError(
                "workspace_file_unavailable", "workspace file cannot be opened", status_code=403
            ) from exc
    finally:
        os.close(parent_fd)
    try:
        opened = os.fstat(fd)
    except OSError as exc:
        os.close(fd)
        raise WorkspaceProjectionError(
            "workspace_file_unavailable", "workspace file cannot be opened", status_code=403
        ) from exc
    if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
        os.close(fd)
        raise WorkspaceProjectionError(
            "workspace_file_changed",
            "workspace file changed; refresh the file listing",
            status_code=409,
        )
    if not stat.S_ISREG(opened.st_mode):
        os.close(fd)
        raise WorkspaceProjectionError(
            "workspace_file_not_regular", "workspace path is not a regular file", status_code=400
        )
    try:
        _ensure_root_reference_unchanged(root, root_identity)
    except BaseException:
        os.close(fd)
        raise
    return fd, root_identity


def read_file_content(
    service: ApplicationService, workspace_id: str, path: str, max_bytes: int = 64_000
) -> WorkbenchFileContent:
    root = _workspace(service, workspace_id)
    fd, _ = _file_descriptor(root, path)
    try:
        size = os.fstat(fd).st_size
        chunks = bytearray()
        while len(chunks) <= max_bytes:
            block = os.read(fd, min(16_384, max_bytes + 1 - len(chunks)))
            if not block:
                break
            chunks.extend(block)
        content = bytes(chunks)
    finally:
        os.close(fd)
    if b"\x00" in content:
        raise WorkspaceProjectionError(
            "workspace_file_binary", "binary files cannot be previewed", status_code=415
        )
    preview = content[:max_bytes]
    try:
        preview.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        # Truncation may land inside one UTF-8 scalar. Only that trailing
        # partial scalar is dropped; malformed bytes elsewhere still fail.
        if (
            len(content) > max_bytes
            and exc.end == len(preview)
            and exc.reason == "unexpected end of data"
        ):
            preview = preview[: exc.start]
        else:
            raise WorkspaceProjectionError(
                "workspace_file_encoding", "file is not valid UTF-8 text", status_code=415
            ) from exc
    try:
        decoded = preview.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise WorkspaceProjectionError(
            "workspace_file_encoding", "file is not valid UTF-8 text", status_code=415
        ) from exc
    return WorkbenchFileContent(
        workspace_id=workspace_id,
        path=path,
        content=redact_public_text(decoded, max_chars=max_bytes),
        content_hash=hashlib.sha256(preview).hexdigest(),
        size_bytes=size,
        truncated=len(content) > max_bytes,
    )


def read_file_diff(
    service: ApplicationService, workspace_id: str, path: str, max_bytes: int = 64_000
) -> WorkbenchFileDiff:
    root = _workspace(service, workspace_id)
    fd, root_identity = _file_descriptor(root, path)
    os.close(fd)
    root_fd, _, rebound_identity = _open_directory(root, ())
    if root_identity != rebound_identity:
        os.close(root_fd)
        raise WorkspaceProjectionError(
            "workspace_unavailable", "registered workspace directory changed", status_code=409
        )
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/nonexistent",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "LC_ALL": "C",
    }
    try:
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    "import os,sys; os.fchdir(int(sys.argv[1])); os.execvp('git',sys.argv[2:])",
                    str(root_fd),
                    "git",
                    "-c",
                    "core.fsmonitor=false",
                    "--no-pager",
                    "diff",
                    "--no-ext-diff",
                    "--no-textconv",
                    "HEAD",
                    "--",
                    path,
                ],
                cwd="/",
                pass_fds=(root_fd,),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env=environment,
            )
        finally:
            os.close(root_fd)
        chunks = bytearray()
        deadline = time.monotonic() + 5
        try:
            assert process.stdout is not None
            while len(chunks) <= max_bytes:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
                    raise TimeoutError
                block = os.read(process.stdout.fileno(), min(16_384, max_bytes + 1 - len(chunks)))
                if not block:
                    break
                chunks.extend(block)
        finally:
            truncated_output = len(chunks) > max_bytes
            if truncated_output and process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
            try:
                process.wait(
                    timeout=(1 if truncated_output else max(0.01, deadline - time.monotonic()))
                )
            except subprocess.TimeoutExpired:
                if process.poll() is None:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                process.wait(timeout=1)
                if not truncated_output:
                    raise
    except (OSError, TimeoutError, subprocess.TimeoutExpired) as exc:
        raise WorkspaceProjectionError(
            "workspace_diff_unavailable", "Git diff is unavailable", status_code=409
        ) from exc
    if process.returncode != 0 and len(chunks) <= max_bytes:
        raise WorkspaceProjectionError(
            "workspace_diff_unavailable", "Git diff is unavailable", status_code=409
        )
    raw = bytes(chunks)
    preview = raw[:max_bytes]
    try:
        decoded = preview.decode("utf-8")
    except UnicodeDecodeError as exc:
        if (
            len(raw) > max_bytes
            and exc.end == len(preview)
            and exc.reason == "unexpected end of data"
        ):
            decoded = preview[: exc.start].decode("utf-8")
        else:
            raise WorkspaceProjectionError(
                "workspace_diff_encoding", "Git diff is not valid UTF-8", status_code=415
            ) from exc
    return WorkbenchFileDiff(
        workspace_id=workspace_id,
        path=path,
        diff=redact_public_text(decoded, max_chars=max_bytes),
        truncated=len(raw) > max_bytes,
    )


def install_workbench_file_routes(app: FastAPI, service: ApplicationService) -> None:
    def local(request: Request) -> None:
        if request.client is None or request.client.host not in {"127.0.0.1", "::1", "testclient"}:
            raise HTTPException(status_code=403, detail="local workspace preview only")

    @app.get(
        "/v1/workspaces/{workspace_id}/file-content",
        response_model=WorkbenchFileContent,
        operation_id="getWorkbenchFileContent",
    )
    def file_content(
        request: Request,
        workspace_id: str,
        path: str = Query(min_length=1, max_length=4096),
        max_bytes: int = Query(default=64_000, ge=1, le=MAX_PREVIEW_BYTES),
    ) -> WorkbenchFileContent:
        local(request)
        try:
            return read_file_content(service, workspace_id, path, max_bytes)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail="workspace is not registered") from exc
        except WorkspaceProjectionError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": exc.message},
            ) from exc

    @app.get(
        "/v1/workspaces/{workspace_id}/diff",
        response_model=WorkbenchFileDiff,
        operation_id="getWorkbenchFileDiff",
    )
    def file_diff(
        request: Request,
        workspace_id: str,
        path: str = Query(min_length=1, max_length=4096),
        max_bytes: int = Query(default=64_000, ge=1, le=MAX_PREVIEW_BYTES),
    ) -> WorkbenchFileDiff:
        local(request)
        try:
            return read_file_diff(service, workspace_id, path, max_bytes)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail="workspace is not registered") from exc
        except WorkspaceProjectionError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": exc.message},
            ) from exc
