from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
from collections.abc import Iterable, Iterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, Final

_CONTENT_HASH_PATTERN: Final = re.compile(r"^[0-9a-f]{64}$")
_HASH_DIRECTORY: Final = "sha256"
_TEMP_DIRECTORY: Final = ".tmp"
_DEFAULT_MAX_SIZE_BYTES: Final = 100 * 1024 * 1024
_DEFAULT_CHUNK_SIZE: Final = 1024 * 1024
_DIRECTORY_OPEN_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_READ_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_FILE_WRITE_FLAGS: Final = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW


class ArtifactStoreError(RuntimeError):
    """Base class for stable, path-free Artifact Store failures."""


class ArtifactConfigurationError(ArtifactStoreError):
    """The configured store root or limit is unsafe or invalid."""


class ArtifactValidationError(ArtifactStoreError):
    """Caller-supplied hash, size, or byte stream is invalid."""


class ArtifactTooLargeError(ArtifactStoreError):
    """The artifact exceeds the configured store size limit."""


class ArtifactNotFoundError(ArtifactStoreError):
    """No regular blob exists for the requested content hash."""


class ArtifactCorruptionError(ArtifactStoreError):
    """A stored blob does not match its authoritative hash and size."""


class ArtifactSecurityError(ArtifactStoreError):
    """A managed path is a link or another unsafe filesystem object."""


@dataclass(frozen=True, slots=True)
class StoredBlob:
    """Internal write result; ``storage_key`` must never enter a public API."""

    content_hash: str
    size_bytes: int
    storage_key: str


class ArtifactStore:
    """Atomic, content-addressed storage for artifact bytes.

    Metadata such as media type, sensitivity, source references, and retention
    policy remains in the authoritative database ``Artifact`` model. This
    component accepts no caller-selected storage path and exposes no local path.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        max_size_bytes: int = _DEFAULT_MAX_SIZE_BYTES,
        chunk_size: int = _DEFAULT_CHUNK_SIZE,
    ) -> None:
        if isinstance(max_size_bytes, bool) or max_size_bytes < 1:
            raise ArtifactConfigurationError("invalid artifact store size limit")
        if isinstance(chunk_size, bool) or chunk_size < 1:
            raise ArtifactConfigurationError("invalid artifact store chunk size")

        configured_root = Path(root)
        if (
            not configured_root.is_absolute()
            or configured_root.parent == configured_root
            or ".." in configured_root.parts
        ):
            raise ArtifactConfigurationError("invalid artifact store root")

        self._root = configured_root
        self._max_size_bytes = max_size_bytes
        self._chunk_size = chunk_size
        self._root_fd = -1
        self._hash_root_fd = -1
        self._temp_fd = -1
        self._initialize()

    def __enter__(self) -> ArtifactStore:
        self._ensure_open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()

    @property
    def max_size_bytes(self) -> int:
        return self._max_size_bytes

    @staticmethod
    def storage_key_for_hash(content_hash: str) -> str:
        """Return the controlled relative lookup key for persistence internals."""

        validated_hash = _validate_content_hash(content_hash)
        return f"{_HASH_DIRECTORY}/{validated_hash[:2]}/{validated_hash[2:4]}/{validated_hash}"

    def close(self) -> None:
        for attribute in ("_temp_fd", "_hash_root_fd", "_root_fd"):
            descriptor = getattr(self, attribute, -1)
            if descriptor >= 0:
                with suppress(OSError):
                    os.close(descriptor)
                setattr(self, attribute, -1)

    def put_bytes(self, content: bytes | bytearray | memoryview) -> StoredBlob:
        if not isinstance(content, (bytes, bytearray, memoryview)):
            raise ArtifactValidationError("artifact content must be bytes")
        return self.put_stream((bytes(content),))

    def put_stream(self, source: BinaryIO | Iterable[bytes]) -> StoredBlob:
        """Fully write and fsync a temporary blob before publishing it atomically."""

        self._validate_managed_roots()
        temporary_name = f"upload-{secrets.token_hex(16)}"
        temporary_fd = -1
        try:
            temporary_fd = os.open(
                temporary_name,
                _FILE_WRITE_FLAGS,
                0o600,
                dir_fd=self._temp_fd,
            )
            digest = hashlib.sha256()
            size_bytes = 0
            for chunk in self._iter_chunks(source):
                if not isinstance(chunk, bytes):
                    raise ArtifactValidationError("artifact stream must yield bytes")
                if not chunk:
                    continue
                if size_bytes + len(chunk) > self._max_size_bytes:
                    raise ArtifactTooLargeError("artifact exceeds configured size limit")
                _write_all(temporary_fd, chunk)
                digest.update(chunk)
                size_bytes += len(chunk)
            os.fsync(temporary_fd)

            content_hash = digest.hexdigest()
            self._publish(
                temporary_name=temporary_name,
                content_hash=content_hash,
                size_bytes=size_bytes,
            )
            # Reopen through the managed path after publication. A valid blob
            # written into a concurrently detached tree must not be accepted.
            self._inspect(content_hash, size_bytes, return_content=False)
            self._validate_managed_roots()
            return StoredBlob(
                content_hash=content_hash,
                size_bytes=size_bytes,
                storage_key=self.storage_key_for_hash(content_hash),
            )
        except ArtifactStoreError:
            raise
        except Exception:
            raise ArtifactStoreError("artifact storage operation failed") from None
        finally:
            if temporary_fd >= 0:
                with suppress(OSError):
                    os.close(temporary_fd)
            if self._temp_fd >= 0:
                try:
                    os.unlink(temporary_name, dir_fd=self._temp_fd)
                except FileNotFoundError:
                    pass
                except OSError:
                    # The primary error is more useful, and no path may leak.
                    pass

    def read(self, content_hash: str, size_bytes: int) -> bytes:
        """Read bytes only after checking the authoritative hash and size."""

        result = self._inspect(content_hash, size_bytes, return_content=True)
        assert result is not None
        return result

    def verify(self, content_hash: str, size_bytes: int) -> None:
        """Raise a stable error unless the blob matches both metadata fields."""

        self._inspect(content_hash, size_bytes, return_content=False)

    def _initialize(self) -> None:
        try:
            self._root_fd = _open_or_create_absolute_directory(self._root)
            self._hash_root_fd = _open_or_create_directory(self._root_fd, _HASH_DIRECTORY)
            self._temp_fd = _open_or_create_directory(self._root_fd, _TEMP_DIRECTORY)
            self._validate_managed_roots()
        except ArtifactStoreError:
            self.close()
            raise
        except OSError:
            self.close()
            raise ArtifactConfigurationError("artifact store initialization failed") from None

    def _validate_managed_roots(self) -> None:
        self._ensure_open()
        reopened_root_fd = -1
        try:
            reopened_root_fd = _open_existing_absolute_directory(self._root)
            root_status = os.fstat(reopened_root_fd)
            opened_root_status = os.fstat(self._root_fd)
            if not stat.S_ISDIR(root_status.st_mode) or not _same_object(
                root_status, opened_root_status
            ):
                raise ArtifactSecurityError("artifact storage boundary violation")
            self._validate_managed_directory(_HASH_DIRECTORY, self._hash_root_fd)
            self._validate_managed_directory(_TEMP_DIRECTORY, self._temp_fd)
        except ArtifactSecurityError:
            raise
        except ArtifactStoreError:
            raise ArtifactSecurityError("artifact storage boundary violation") from None
        except OSError:
            raise ArtifactSecurityError("artifact storage boundary violation") from None
        finally:
            if reopened_root_fd >= 0:
                os.close(reopened_root_fd)

    def _validate_managed_directory(self, name: str, descriptor: int) -> None:
        _require_exact_entry(self._root_fd, name)
        path_status = os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
        opened_status = os.fstat(descriptor)
        if (
            stat.S_ISLNK(path_status.st_mode)
            or not stat.S_ISDIR(path_status.st_mode)
            or not _same_object(path_status, opened_status)
        ):
            raise ArtifactSecurityError("artifact storage boundary violation")

    def _publish(self, *, temporary_name: str, content_hash: str, size_bytes: int) -> None:
        first_fd, second_fd = self._open_shards(content_hash, create=True)
        try:
            try:
                os.link(
                    temporary_name,
                    content_hash,
                    src_dir_fd=self._temp_fd,
                    dst_dir_fd=second_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                pass
            except OSError:
                raise ArtifactStoreError("artifact publication failed") from None
            self._verify_target(second_fd, content_hash, size_bytes, return_content=False)
            try:
                os.fsync(second_fd)
            except OSError:
                raise ArtifactStoreError("artifact publication failed") from None
        finally:
            os.close(second_fd)
            os.close(first_fd)

    def _inspect(
        self,
        content_hash: str,
        size_bytes: int,
        *,
        return_content: bool,
    ) -> bytes | None:
        validated_hash = _validate_content_hash(content_hash)
        validated_size = self._validate_size(size_bytes)
        self._validate_managed_roots()
        first_fd, second_fd = self._open_shards(validated_hash, create=False)
        try:
            result = self._verify_target(
                second_fd,
                validated_hash,
                validated_size,
                return_content=return_content,
            )
            self._validate_open_shards(validated_hash, first_fd, second_fd)
            self._validate_managed_roots()
            return result
        finally:
            os.close(second_fd)
            os.close(first_fd)

    def _validate_open_shards(self, content_hash: str, first_fd: int, second_fd: int) -> None:
        reopened_first_fd, reopened_second_fd = self._open_shards(content_hash, create=False)
        try:
            if not _same_object(
                os.fstat(first_fd), os.fstat(reopened_first_fd)
            ) or not _same_object(os.fstat(second_fd), os.fstat(reopened_second_fd)):
                raise ArtifactSecurityError("artifact storage boundary violation")
        finally:
            os.close(reopened_second_fd)
            os.close(reopened_first_fd)

    def _validate_size(self, size_bytes: int) -> int:
        if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
            raise ArtifactValidationError("invalid artifact size")
        if size_bytes > self._max_size_bytes:
            raise ArtifactTooLargeError("artifact exceeds configured size limit")
        return size_bytes

    def _open_shards(self, content_hash: str, *, create: bool) -> tuple[int, int]:
        first_fd = -1
        try:
            if create:
                first_fd = _open_or_create_directory(self._hash_root_fd, content_hash[:2])
                second_fd = _open_or_create_directory(first_fd, content_hash[2:4])
            else:
                first_fd = _open_existing_directory(self._hash_root_fd, content_hash[:2])
                second_fd = _open_existing_directory(first_fd, content_hash[2:4])
            return first_fd, second_fd
        except ArtifactStoreError:
            if first_fd >= 0:
                os.close(first_fd)
            raise
        except OSError:
            if first_fd >= 0:
                os.close(first_fd)
            raise ArtifactSecurityError("artifact storage boundary violation") from None

    def _verify_target(
        self,
        parent_fd: int,
        content_hash: str,
        expected_size: int,
        *,
        return_content: bool,
    ) -> bytes | None:
        descriptor = -1
        try:
            try:
                _require_exact_entry(parent_fd, content_hash)
                target_status = os.stat(
                    content_hash,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                raise ArtifactNotFoundError("artifact blob not found") from None
            except OSError:
                raise ArtifactSecurityError("artifact storage boundary violation") from None
            if not stat.S_ISREG(target_status.st_mode):
                raise ArtifactSecurityError("artifact storage boundary violation")

            try:
                descriptor = os.open(content_hash, _FILE_READ_FLAGS, dir_fd=parent_fd)
            except FileNotFoundError:
                raise ArtifactNotFoundError("artifact blob not found") from None
            except OSError:
                raise ArtifactSecurityError("artifact storage boundary violation") from None
            opened_status = os.fstat(descriptor)
            if not stat.S_ISREG(opened_status.st_mode) or not _same_object(
                target_status,
                opened_status,
            ):
                raise ArtifactSecurityError("artifact storage boundary violation")
            if opened_status.st_size != expected_size:
                raise ArtifactCorruptionError("artifact blob failed integrity verification")

            digest = hashlib.sha256()
            content = bytearray() if return_content else None
            actual_size = 0
            while True:
                chunk = os.read(descriptor, self._chunk_size)
                if not chunk:
                    break
                actual_size += len(chunk)
                if actual_size > expected_size:
                    raise ArtifactCorruptionError("artifact blob failed integrity verification")
                digest.update(chunk)
                if content is not None:
                    content.extend(chunk)
            ending_status = os.fstat(descriptor)
            try:
                ending_path_status = os.stat(
                    content_hash,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except OSError:
                raise ArtifactSecurityError("artifact storage boundary violation") from None
            if not stat.S_ISREG(ending_path_status.st_mode) or not _same_object(
                ending_path_status,
                ending_status,
            ):
                raise ArtifactSecurityError("artifact storage boundary violation")
            if (
                actual_size != expected_size
                or digest.hexdigest() != content_hash
                or ending_status.st_size != expected_size
            ):
                raise ArtifactCorruptionError("artifact blob failed integrity verification")
            return bytes(content) if content is not None else None
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _iter_chunks(self, source: BinaryIO | Iterable[bytes]) -> Iterator[bytes]:
        if hasattr(source, "read"):
            reader = source
            while True:
                chunk = reader.read(self._chunk_size)
                if chunk == b"":
                    return
                yield chunk
        else:
            yield from source

    def _ensure_open(self) -> None:
        if self._root_fd < 0 or self._hash_root_fd < 0 or self._temp_fd < 0:
            raise ArtifactStoreError("artifact store is closed")


def _validate_content_hash(content_hash: str) -> str:
    if not isinstance(content_hash, str) or _CONTENT_HASH_PATTERN.fullmatch(content_hash) is None:
        raise ArtifactValidationError("invalid artifact content hash")
    return content_hash


def _open_or_create_directory(parent_fd: int, name: str) -> int:
    created = False
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        created = True
    except FileExistsError:
        pass
    except OSError:
        raise ArtifactSecurityError("artifact storage boundary violation") from None
    descriptor = _open_existing_directory(parent_fd, name)
    if created:
        try:
            os.fsync(parent_fd)
        except OSError:
            os.close(descriptor)
            raise ArtifactStoreError("artifact directory synchronization failed") from None
    return descriptor


def _open_existing_directory(parent_fd: int, name: str) -> int:
    _require_exact_entry(parent_fd, name)
    try:
        path_status = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(path_status.st_mode):
            raise ArtifactSecurityError("artifact storage boundary violation")
        descriptor = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        raise ArtifactNotFoundError("artifact blob not found") from None
    except OSError:
        raise ArtifactSecurityError("artifact storage boundary violation") from None
    try:
        opened_status = os.fstat(descriptor)
        if not stat.S_ISDIR(opened_status.st_mode) or not _same_object(
            path_status,
            opened_status,
        ):
            raise ArtifactSecurityError("artifact storage boundary violation")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _write_all(descriptor: int, content: bytes) -> None:
    remaining = memoryview(content)
    while remaining:
        try:
            written = os.write(descriptor, remaining)
        except OSError:
            raise ArtifactStoreError("artifact write failed") from None
        if written < 1:
            raise ArtifactStoreError("artifact write failed")
        remaining = remaining[written:]


def _same_object(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _require_exact_entry(parent_fd: int, name: str) -> None:
    """Reject case-folded aliases even on case-insensitive filesystems."""

    scan_fd = -1
    try:
        # ``listdir(fd)`` may advance a shared directory offset on some
        # platforms. Opening ``.`` creates an independent open-file
        # description, so concurrent callers cannot make the scan incomplete.
        scan_fd = os.open(".", _DIRECTORY_OPEN_FLAGS, dir_fd=parent_fd)
        entries = os.listdir(scan_fd)
        if name in entries:
            return
        if any(entry.casefold() == name.casefold() for entry in entries):
            raise ArtifactSecurityError("artifact storage boundary violation")
        raise ArtifactNotFoundError("artifact blob not found")
    except ArtifactStoreError:
        raise
    except OSError:
        raise ArtifactSecurityError("artifact storage boundary violation") from None
    finally:
        if scan_fd >= 0:
            os.close(scan_fd)


def _open_or_create_absolute_directory(path: Path) -> int:
    current_fd = -1
    try:
        current_fd = os.open(path.anchor, _DIRECTORY_OPEN_FLAGS)
        for component in path.parts[1:]:
            child_fd = _open_or_create_directory(current_fd, component)
            os.close(current_fd)
            current_fd = child_fd
        return current_fd
    except BaseException:
        if current_fd >= 0:
            os.close(current_fd)
        raise


def _open_existing_absolute_directory(path: Path) -> int:
    current_fd = -1
    try:
        current_fd = os.open(path.anchor, _DIRECTORY_OPEN_FLAGS)
        for component in path.parts[1:]:
            child_fd = _open_existing_directory(current_fd, component)
            os.close(current_fd)
            current_fd = child_fd
        return current_fd
    except BaseException:
        if current_fd >= 0:
            os.close(current_fd)
        raise
