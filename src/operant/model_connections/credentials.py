from __future__ import annotations

import ast
import fcntl
import hmac
import json
import os
import re
import secrets
import stat
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

_REFERENCE = re.compile(r"^OPERANT_CONNECTION_[A-Z0-9_]+$")
_OPERATOR_REFERENCES = frozenset(
    {
        "OPERANT_GEMINI_OAUTH_CLIENT_ID",
        "OPERANT_GEMINI_OAUTH_CLIENT_SECRET",
    }
)


class CredentialError(RuntimeError):
    """A credential cannot be stored safely."""


class CredentialStore:
    """Own only generated references in a private, untracked .env file."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def request_fingerprint(
        self,
        context: str,
        value: str,
        *,
        before_create: Callable[[], None],
        allow_create: bool,
    ) -> str:
        """Keep request matching opaque to someone with only the metadata DB."""
        reference = "OPERANT_CONNECTION_REQUEST_FINGERPRINT_KEY"
        with self.locked("request-fingerprint"):
            key = self.get(reference)
            if key is None:
                if not allow_create:
                    raise CredentialError("request fingerprint key is missing")
                before_create()
                key = secrets.token_hex(32)
                self.put(reference, key)
            if not re.fullmatch(r"[0-9a-f]{64}", key):
                raise CredentialError("invalid request fingerprint key")
        message = json.dumps([context, value], separators=(",", ":")).encode()
        return hmac.digest(bytes.fromhex(key), message, "sha256").hex()

    def get(self, reference: str) -> str | None:
        self._check_reference(reference)
        for line in self._read_lines():
            key, sep, value = line.partition("=")
            if sep and key == reference:
                try:
                    loaded = json.loads(value)
                except json.JSONDecodeError as exc:
                    raise CredentialError("invalid credential file") from exc
                if not isinstance(loaded, str):
                    raise CredentialError("invalid credential file")
                os.environ[reference] = loaded
                return loaded
        return None

    def get_operator_configuration(self, reference: str) -> str | None:
        if reference not in _OPERATOR_REFERENCES:
            raise CredentialError("operator configuration name is not supported")
        for line in self._read_lines():
            key, separator, raw = line.partition("=")
            if separator and key.strip() == reference:
                value = raw.strip()
                if value.startswith(('"', "'")):
                    try:
                        value = ast.literal_eval(value)
                    except (SyntaxError, ValueError) as exc:
                        raise CredentialError("invalid operator configuration") from exc
                if not isinstance(value, str) or any(
                    char in value for char in ("\x00", "\n", "\r")
                ):
                    raise CredentialError("invalid operator configuration")
                return value or None
        configured = os.environ.get(reference)
        return configured or None

    def put(self, reference: str, value: str) -> None:
        self.put_many({reference: value})

    def contains(self, reference: str) -> bool:
        """Check a protected reference without loading its value into the environment."""
        self._check_reference(reference)
        return any(line.partition("=")[0] == reference for line in self._read_lines())

    def put_if_absent(self, reference: str, value: str) -> bool:
        """Never replace an earlier credential after an uncertain create result."""
        self._check_reference(reference)
        if not value or any(char in value for char in ("\x00", "\n", "\r")):
            raise CredentialError("invalid credential")
        with self.locked("env"):
            if self.contains(reference):
                return False
            self._rewrite_locked({reference: value})
            os.environ[reference] = value
        return True

    def put_many(self, values: dict[str, str]) -> None:
        for reference, value in values.items():
            self._check_reference(reference)
            if not value or any(char in value for char in ("\x00", "\n", "\r")):
                raise CredentialError("invalid credential")
        self._rewrite(values)
        os.environ.update(values)

    def delete(self, reference: str) -> None:
        self.delete_many((reference,))

    def delete_many(self, references: tuple[str, ...]) -> None:
        for reference in references:
            self._check_reference(reference)
        self._rewrite(dict.fromkeys(references))
        for reference in references:
            os.environ.pop(reference, None)

    def _rewrite(self, changes: Mapping[str, str | None]) -> None:
        with self.locked("env"):
            self._rewrite_locked(changes)

    def _rewrite_locked(self, changes: Mapping[str, str | None]) -> None:
        lines = self._read_lines()
        updated = [line for line in lines if line.partition("=")[0] not in changes]
        if updated and not updated[-1].endswith("\n"):
            updated[-1] += "\n"
        for reference, value in changes.items():
            if value is not None:
                updated.append(f"{reference}={json.dumps(value, ensure_ascii=False)}\n")
        parent = self.path.parent
        self._check_directory(parent)
        fd, name = tempfile.mkstemp(prefix=".operant-env-", dir=parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                output.writelines(updated)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.path)
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    @contextmanager
    def locked(self, name: str) -> Iterator[None]:
        fd = self.acquire_lock(name)
        try:
            yield
        finally:
            self.release_lock(fd)

    def acquire_lock(self, name: str) -> int:
        fd = self._open_lock(name, nonblocking=False)
        assert fd is not None
        return fd

    def try_acquire_lock(self, name: str) -> int | None:
        return self._open_lock(name, nonblocking=True)

    def _open_lock(self, name: str, *, nonblocking: bool) -> int | None:
        if not re.fullmatch(r"[a-z0-9-]{1,100}", name):
            raise CredentialError("invalid credential lock name")
        parent = self.path.parent
        self._check_directory(parent)
        flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
        fd = os.open(parent / f".operant-{name}.lock", flags, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise CredentialError("credential lock must be a private regular file")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
            except BlockingIOError:
                if nonblocking:
                    os.close(fd)
                    return None
                raise
            return fd
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    def release_lock(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _read_lines(self) -> list[str]:
        self._check_directory(self.path.parent)
        if not self.path.exists() and not self.path.is_symlink():
            return []
        info = self.path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise CredentialError("credential file must be a user-owned regular file")
        if info.st_mode & 0o077:
            raise CredentialError("credential file must have mode 0600")
        with self.path.open(encoding="utf-8") as source:
            return source.readlines()

    @staticmethod
    def _check_directory(path: Path) -> None:
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise CredentialError("credential directory must be user-owned")
        if info.st_mode & 0o022:
            raise CredentialError("credential directory must not be group/world writable")

    @staticmethod
    def _check_reference(reference: str) -> None:
        if not _REFERENCE.fullmatch(reference):
            raise CredentialError("invalid credential reference")
