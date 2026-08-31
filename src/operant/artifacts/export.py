from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from contextlib import suppress
from pathlib import Path, PurePosixPath

from operant.artifacts.store import (
    _FILE_WRITE_FLAGS,
    ArtifactExportOutcomeUnknownError,
    ArtifactSecurityError,
    ArtifactStoreError,
    _open_existing_absolute_directory,
    _open_existing_directory,
    _require_exact_entry,
    _same_object,
    _write_all,
)

_PROTECTED_COMPONENTS = frozenset({".git", ".operant", ".env", ".ssh"})


def artifact_export_scope_fingerprint(
    *,
    workspace_root: str | Path,
    relative_path: str,
) -> str:
    """Bind an export grant to the current root/parent inode chain and path."""

    root, normalized = _validated_export_scope(workspace_root, relative_path)
    root_fd = _open_existing_absolute_directory(root)
    parent_fd = root_fd
    owned_fds: list[int] = []
    try:
        descriptors = [root_fd]
        for component in normalized.parts[:-1]:
            child_fd = _open_existing_directory(parent_fd, component)
            owned_fds.append(child_fd)
            parent_fd = child_fd
            descriptors.append(child_fd)
        return _opened_scope_fingerprint(normalized, descriptors)
    finally:
        for descriptor in reversed(owned_fds):
            os.close(descriptor)
        os.close(root_fd)


def _identity(status: os.stat_result) -> tuple[int, int]:
    return status.st_dev, status.st_ino


def _opened_scope_fingerprint(
    relative_path: PurePosixPath,
    descriptors: list[int],
) -> str:
    payload = {
        "schema": "operant.artifact-export-scope.v1",
        "relative_path": relative_path.as_posix(),
        "directory_identities": [_identity(os.fstat(item)) for item in descriptors],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _validated_export_scope(
    workspace_root: str | Path,
    relative_path: str,
) -> tuple[Path, PurePosixPath]:
    root = Path(workspace_root)
    normalized = PurePosixPath(relative_path)
    if not root.is_absolute() or root.parent == root or root == Path.home():
        raise ArtifactSecurityError("invalid export workspace boundary")
    if (
        normalized.is_absolute()
        or not normalized.parts
        or any(part in {"", ".", ".."} for part in normalized.parts)
        or any(part.casefold() in _PROTECTED_COMPONENTS for part in normalized.parts)
    ):
        raise ArtifactSecurityError("invalid Artifact export path")
    return root, normalized


def export_artifact_bytes(
    content: bytes,
    *,
    workspace_root: str | Path,
    relative_path: str,
    expected_scope_hash: str,
) -> str:
    """Atomically export to one explicit existing workspace-relative directory."""

    root, normalized = _validated_export_scope(workspace_root, relative_path)

    root_fd = _open_existing_absolute_directory(root)
    parent_fd = root_fd
    owned_fds: list[int] = []
    temp_name = f".operant-export-{secrets.token_hex(16)}"
    target_name = normalized.parts[-1]
    temp_fd = -1
    published = False
    try:
        for component in normalized.parts[:-1]:
            child_fd = _open_existing_directory(parent_fd, component)
            owned_fds.append(child_fd)
            parent_fd = child_fd
        actual_scope_hash = _opened_scope_fingerprint(
            normalized,
            [root_fd, *owned_fds],
        )
        if not hmac.compare_digest(actual_scope_hash, expected_scope_hash):
            raise ArtifactSecurityError("Artifact export scope changed before publication")
        try:
            _require_exact_entry(parent_fd, target_name)
        except Exception as exc:
            # A case-folded alias is a security failure; exact absence is the
            # only acceptable create state.
            from operant.artifacts.store import ArtifactNotFoundError

            if not isinstance(exc, ArtifactNotFoundError):
                raise
        else:
            raise ArtifactSecurityError("Artifact export target already exists")
        temp_fd = os.open(temp_name, _FILE_WRITE_FLAGS, 0o600, dir_fd=parent_fd)
        _write_all(temp_fd, content)
        os.fsync(temp_fd)
        os.close(temp_fd)
        temp_fd = -1
        try:
            os.link(
                temp_name,
                target_name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            raise ArtifactSecurityError("Artifact export target already exists") from None
        except OSError:
            raise ArtifactStoreError("Artifact export failed safely") from None
        published = True
        os.fsync(parent_fd)
        reopened_root_fd = _open_existing_absolute_directory(root)
        reopened_parent_fd = reopened_root_fd
        reopened_owned: list[int] = []
        try:
            if not _same_object(os.fstat(root_fd), os.fstat(reopened_root_fd)):
                raise ArtifactSecurityError("Artifact export workspace was replaced")
            for index, component in enumerate(normalized.parts[:-1]):
                child_fd = _open_existing_directory(reopened_parent_fd, component)
                reopened_owned.append(child_fd)
                if not _same_object(os.fstat(owned_fds[index]), os.fstat(child_fd)):
                    raise ArtifactSecurityError("Artifact export path was replaced")
                reopened_parent_fd = child_fd
            target_status = os.stat(target_name, dir_fd=parent_fd, follow_symlinks=False)
            reopened_target = os.stat(
                target_name,
                dir_fd=reopened_parent_fd,
                follow_symlinks=False,
            )
            if not _same_object(target_status, reopened_target):
                raise ArtifactSecurityError("Artifact export target was replaced")
        finally:
            for descriptor in reversed(reopened_owned):
                os.close(descriptor)
            os.close(reopened_root_fd)
        return normalized.as_posix()
    except (ArtifactSecurityError, ArtifactStoreError) as exc:
        if published:
            raise ArtifactExportOutcomeUnknownError(
                "Artifact export outcome requires manual reconciliation"
            ) from exc
        raise
    except OSError as exc:
        if published:
            raise ArtifactExportOutcomeUnknownError(
                "Artifact export outcome requires manual reconciliation"
            ) from exc
        raise ArtifactStoreError("Artifact export failed safely") from None
    finally:
        if temp_fd >= 0:
            os.close(temp_fd)
        with suppress(OSError):
            os.unlink(temp_name, dir_fd=parent_fd)
        if not published:
            # The destination was never replaced or truncated.
            pass
        for descriptor in reversed(owned_fds):
            os.close(descriptor)
        os.close(root_fd)
