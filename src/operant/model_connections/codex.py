"""Isolated official Codex CLI sign-in for Operant's Codex connection.

Only the CLI subprocess sees the temporary CODEX_HOME. No existing Codex
profile, keychain entry, or config file is read or changed by this module.
"""

from __future__ import annotations

import asyncio
import atexit
import base64
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from operant.model_connections.credentials import CredentialStore
from operant.model_connections.oauth import ConnectionRepository, OAuthError, secret_ref

_TOKEN_URL = "https://auth.openai.com/oauth/token"
_REVOKE_URL = "https://auth.openai.com/oauth/revoke"
_CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_BASE_URL = "https://chatgpt.com/backend-api/codex"
_ATTEMPT_SECONDS = 600
_ACCOUNT_ID = re.compile(r"^[A-Za-z0-9_:-]{1,200}$")


def _claims(token: str) -> dict[str, Any]:
    if len(token) > 128_000 or token.count(".") != 2:
        raise OAuthError("Codex 登录结果无效")
    try:
        part = token.split(".")[1]
        decoded = base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
        body = json.loads(decoded)
    except (IndexError, ValueError, UnicodeDecodeError) as exc:
        raise OAuthError("Codex 登录结果无效") from exc
    if not isinstance(body, dict):
        raise OAuthError("Codex 登录结果无效")
    return body


def _private_file(path: Path) -> bytes:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise OAuthError("Codex 登录凭据文件权限不安全")
    if info.st_size > 128_000:
        raise OAuthError("Codex 登录结果过大")
    with path.open("rb") as source:
        return source.read(128_001)


def _cli_path() -> Path:
    configured = os.environ.get("OPERANT_CODEX_CLI_PATH")
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured))
    else:
        for directory in os.environ.get("PATH", "").split(os.pathsep):
            path = Path(directory)
            # Ignore the current directory and relative/PATH injection entries.
            if path.is_absolute() and path != Path.cwd():
                candidates.append(path / "codex")
        candidates.extend(
            Path(path)
            for path in (
                "/opt/homebrew/bin/codex",
                "/usr/local/bin/codex",
                "/usr/bin/codex",
            )
        )
        nvm = Path.home() / ".nvm/versions/node"
        if nvm.is_dir():
            candidates.extend(sorted(nvm.glob("*/bin/codex"), reverse=True))
    for candidate in candidates:
        if not candidate.is_absolute() or candidate.name != "codex":
            continue
        try:
            target = candidate.resolve(strict=True)
            info = target.stat()
            parent_info = candidate.parent.stat()
            if (
                stat.S_ISREG(info.st_mode)
                and info.st_uid in {0, os.getuid()}
                and not info.st_mode & 0o022
                and not parent_info.st_mode & 0o022
                and os.access(target, os.X_OK)
            ):
                return candidate
        except (OSError, RuntimeError):
            continue
    raise OAuthError("未找到 Codex CLI。请先安装官方 Codex CLI，再重试登录。")


@dataclass(slots=True)
class CodexAttempt:
    id: str
    connection_id: str
    started_at: float
    home: Path
    marker: Path
    process: subprocess.Popen[bytes]
    parent_fd: int | None
    login_lock_fd: int | None
    created_new: bool
    auth_generation_before: str | None
    provider: str = "codex"
    status: str = "pending"
    error: str | None = None
    authorization_url: str | None = None


class CodexConnections:
    def __init__(
        self,
        repo: ConnectionRepository,
        credentials: CredentialStore,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Any = time.time,
    ) -> None:
        self.repo = repo
        self.credentials = credentials
        self.transport = transport
        self.clock = clock
        self.attempts: dict[str, CodexAttempt] = {}
        self._lock = threading.RLock()
        self._refresh_locks: dict[str, asyncio.Lock] = {}
        atexit.register(self.close)

    def start(self, *, connection_id: str | None = None) -> CodexAttempt:
        with self._lock:
            existing = self.repo.get_connection(connection_id) if connection_id else None
            if connection_id and existing is None:
                raise OAuthError("connection does not exist")
            if existing and (existing.get("provider"), existing.get("auth_method")) != (
                "codex",
                "oauth",
            ):
                raise OAuthError("connection provider does not match")
            if any(item.status == "pending" for item in self.attempts.values()):
                raise OAuthError("Codex 登录正在进行")
            login_lock_fd = self.credentials.try_acquire_lock("codex-login-global")
            if login_lock_fd is None:
                raise OAuthError("Codex 登录正在进行")
            try:
                with socket.socket() as probe:
                    try:
                        probe.bind(("127.0.0.1", 1455))
                    except OSError as exc:
                        raise OAuthError(
                            "Codex 登录端口 1455 已被占用，请关闭占用程序后重试。"
                        ) from exc
                cli = _cli_path()
            except BaseException:
                self.credentials.release_lock(login_lock_fd)
                raise
            connection_id = connection_id or str(uuid.uuid4())
            attempt_id = str(uuid.uuid4())
            parent = self.credentials.path.parent
            marker = parent / f".operant-codex-attempt-{attempt_id}.state"
            home: Path | None = None
            try:
                home = Path(tempfile.mkdtemp(prefix=".operant-codex-login-", dir=parent))
                os.chmod(home, 0o700)
            except OSError as exc:
                if home is not None:
                    shutil.rmtree(home, ignore_errors=True)
                self.credentials.release_lock(login_lock_fd)
                raise OAuthError("无法创建 Codex 登录临时目录") from exc
            read_fd: int | None = None
            write_fd: int | None = None
            process: subprocess.Popen[bytes] | None = None
            try:
                config = home / "config.toml"
                config.write_text('cli_auth_credentials_store = "file"\n', encoding="utf-8")
                config.chmod(0o600)
                env = {
                    "HOME": str(home),
                    "CODEX_HOME": str(home),
                    "PATH": os.pathsep.join(
                        dict.fromkeys(
                            (
                                str(cli.parent),
                                str(cli.resolve().parent),
                                "/opt/homebrew/bin",
                                "/usr/local/bin",
                                "/usr/bin",
                                "/bin",
                            )
                        )
                    ),
                    "TMPDIR": str(home),
                    "LANG": os.environ.get("LANG", "en_US.UTF-8"),
                    "USER": os.environ.get("USER", ""),
                    "LOGNAME": os.environ.get("LOGNAME", ""),
                }
                for name in (
                    "HTTP_PROXY",
                    "HTTPS_PROXY",
                    "ALL_PROXY",
                    "NO_PROXY",
                    "http_proxy",
                    "https_proxy",
                    "all_proxy",
                    "no_proxy",
                    "SSL_CERT_FILE",
                    "SSL_CERT_DIR",
                    "REQUESTS_CA_BUNDLE",
                    "CURL_CA_BUNDLE",
                ):
                    if value := os.environ.get(name):
                        env[name] = value
                read_fd, write_fd = os.pipe()
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "operant.model_connections.codex_login_helper",
                        str(cli),
                        str(home),
                        str(read_fd),
                        str(marker),
                    ],
                    cwd=home,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    close_fds=True,
                    pass_fds=(read_fd, login_lock_fd),
                )
                os.close(read_fd)
                read_fd = None
            except (OSError, ValueError) as exc:
                if read_fd is not None:
                    os.close(read_fd)
                if write_fd is not None:
                    os.close(write_fd)
                shutil.rmtree(home)
                self.credentials.release_lock(login_lock_fd)
                raise OAuthError("无法启动 Codex CLI 登录") from exc
            assert process is not None and write_fd is not None
            attempt = CodexAttempt(
                id=attempt_id,
                connection_id=connection_id,
                started_at=float(self.clock()),
                home=home,
                marker=marker,
                process=process,
                parent_fd=write_fd,
                login_lock_fd=login_lock_fd,
                created_new=existing is None,
                auth_generation_before=existing.get("auth_generation") if existing else None,
            )
            try:
                if existing is None:
                    self.repo.save_connection(
                        connection_id,
                        {
                            "connection_id": connection_id,
                            "name": "Codex",
                            "provider": "codex",
                            "auth_method": "oauth",
                            "status": "needs_auth",
                            "base_url": _BASE_URL,
                            "model_ids": [],
                            "profile_ids": [],
                            "secret_ref": secret_ref(connection_id, "ACCESS_TOKEN"),
                        },
                    )
                self.attempts[attempt.id] = attempt
            except Exception:
                self._cleanup(attempt)
                raise
            threading.Thread(target=self._watch, args=(attempt.id,), daemon=True).start()
            return attempt

    def _watch(self, attempt_id: str) -> None:
        attempt = self.attempts[attempt_id]
        assert attempt.process.stdout is not None
        outcome = attempt.process.stdout.readline().decode("ascii", errors="replace").strip()
        with self._lock:
            if attempt.status != "pending":
                return
            if outcome == "timeout":
                attempt.status = "expired"
                self._cleanup(attempt)
                return
            if outcome != "0":
                attempt.status = "failed"
                attempt.error = "Codex 登录未完成，请重试。"
                self._cleanup(attempt)
                return
            try:
                raw = _private_file(attempt.home / "auth.json")
                body = json.loads(raw)
                if not isinstance(body, dict) or body.get("auth_mode") not in {
                    None,
                    "chatgpt",
                    "chatgpt_auth_tokens",
                }:
                    raise OAuthError("Codex 登录结果不是 ChatGPT 账号")
                tokens = body.get("tokens")
                if not isinstance(tokens, dict):
                    raise OAuthError("Codex 登录结果缺少令牌")
                access, refresh, identity = (
                    tokens.get("access_token"),
                    tokens.get("refresh_token"),
                    tokens.get("id_token"),
                )
                if (
                    not isinstance(access, str)
                    or not access
                    or not isinstance(refresh, str)
                    or not refresh
                    or not isinstance(identity, str)
                    or not identity
                ):
                    raise OAuthError("Codex 登录结果缺少令牌")
                claims = _claims(identity)
                access_claims = _claims(access)
                auth_claim = claims.get("https://api.openai.com/auth")
                access_auth = access_claims.get("https://api.openai.com/auth")
                account_id = tokens.get("account_id") or (
                    auth_claim.get("chatgpt_account_id") if isinstance(auth_claim, dict) else None
                )
                access_account = (
                    access_auth.get("chatgpt_account_id") if isinstance(access_auth, dict) else None
                )
                if (
                    not isinstance(account_id, str)
                    or _ACCOUNT_ID.fullmatch(account_id) is None
                    or account_id != access_account
                ):
                    raise OAuthError("Codex 登录结果缺少账号标识")
                expires_at = access_claims.get("exp")
                if not isinstance(expires_at, int) or expires_at <= int(self.clock()):
                    raise OAuthError("Codex 登录令牌已过期")
                references = {
                    secret_ref(attempt.connection_id, "ACCESS_TOKEN"): access,
                    secret_ref(attempt.connection_id, "REFRESH_TOKEN"): refresh,
                    secret_ref(attempt.connection_id, "ID_TOKEN"): identity,
                }
                with self.credentials.locked(
                    "codex-refresh-" + attempt.connection_id.replace("-", "")
                ):
                    current = self.repo.get_connection(attempt.connection_id)
                    if current is None or current.get("provider") != "codex":
                        raise OAuthError("Codex 连接已取消")
                    previous_account = current.get("account_id")
                    if isinstance(previous_account, str) and previous_account != account_id:
                        raise OAuthError("账号与此 Codex 连接不一致，请新建连接")
                    if attempt.created_new and any(
                        self.credentials.contains(reference) for reference in references
                    ):
                        raise OAuthError("Codex 登录结果需人工核对")
                    self.credentials.put_many(references)
                    self.repo.save_connection(
                        attempt.connection_id,
                        {
                            **current,
                            "status": "connected",
                            "error": None,
                            "model_ids": [],
                            "model_names": {},
                            "model_capabilities": {},
                            "account_id": account_id,
                            "auth_generation": str(uuid.uuid4()),
                            "selection_generation": None,
                            "account_label": "".join(
                                char
                                for char in str(claims.get("email") or "Codex")
                                if char.isprintable()
                            )[:100],
                            "expires_at": expires_at,
                        },
                    )
                attempt.status = "completed"
            except (OSError, ValueError, OAuthError) as exc:
                attempt.status = "failed"
                attempt.error = str(exc) if isinstance(exc, OAuthError) else "Codex 登录结果无效"
            except Exception:
                # A credential write followed by a metadata error is an
                # unknown outcome. Keep the connection for manual inspection.
                attempt.status = "failed"
                attempt.error = "Codex 登录结果需人工核对"
            finally:
                self._cleanup(attempt)

    def status(self, attempt_id: str) -> CodexAttempt:
        with self._lock:
            attempt = self._attempt(attempt_id)
            if attempt.status == "pending" and self.clock() - attempt.started_at > _ATTEMPT_SECONDS:
                attempt.status = "expired"
                self._cleanup(attempt)
            return attempt

    def cancel(self, attempt_id: str) -> None:
        with self._lock:
            attempt = self.status(attempt_id)
            if attempt.status == "pending":
                attempt.status = "cancelled"
                self._cleanup(attempt)

    def _attempt(self, attempt_id: str) -> CodexAttempt:
        attempt = self.attempts.get(attempt_id)
        if attempt is None:
            raise OAuthError("OAuth attempt not found")
        return attempt

    def was_reaped(self, attempt_id: str) -> bool:
        try:
            uuid.UUID(attempt_id)
            marker = self.credentials.path.parent / f".operant-codex-attempt-{attempt_id}.state"
            return _private_file(marker) == b"stopped\n"
        except (OSError, ValueError, OAuthError):
            return False

    @staticmethod
    def _stop(attempt: CodexAttempt) -> bool:
        if attempt.parent_fd is not None:
            with suppress(OSError):
                os.close(attempt.parent_fd)
            attempt.parent_fd = None
        try:
            attempt.process.wait(timeout=2)
            return True
        except subprocess.TimeoutExpired:
            pass
        # The helper and CLI share an owned session/process group. A forced
        # stop must target both; killing only the helper can orphan the CLI.
        for signal_number in (signal.SIGTERM, signal.SIGKILL):
            if attempt.process.poll() is not None:
                return True
            with suppress(ProcessLookupError):
                os.killpg(attempt.process.pid, signal_number)
            try:
                attempt.process.wait(timeout=2)
                return True
            except subprocess.TimeoutExpired:
                continue
        return attempt.process.poll() is not None

    def _cleanup(self, attempt: CodexAttempt) -> None:
        stopped = self._stop(attempt)
        reaped = stopped and self.was_reaped(attempt.id)
        if reaped:
            shutil.rmtree(attempt.home, ignore_errors=True)
        if attempt.login_lock_fd is not None:
            self.credentials.release_lock(attempt.login_lock_fd)
            attempt.login_lock_fd = None
        if not reaped:
            # No helper tombstone means its child cleanup is unverified.
            # Keep the receipt unconfirmed and do not invent a stopped marker.
            attempt.status = "failed"
            attempt.error = "Codex 登录子进程退出待核对"

    def close(self) -> None:
        with self._lock:
            for attempt in self.attempts.values():
                if attempt.status == "pending":
                    attempt.status = "expired"
                    self._cleanup(attempt)

    async def _acquire_refresh_lock(self, connection_id: str) -> int:
        deadline = time.monotonic() + 30
        name = "codex-refresh-" + connection_id.replace("-", "")
        while time.monotonic() < deadline:
            fd = self.credentials.try_acquire_lock(name)
            if fd is not None:
                return fd
            await asyncio.sleep(0.05)
        raise OAuthError("Codex 凭据更新繁忙，请稍后重试")

    async def access_token(self, record: dict[str, Any]) -> tuple[str, str]:
        connection_id = str(record["connection_id"])
        lock = self._refresh_locks.setdefault(connection_id, asyncio.Lock())
        async with lock:
            fd = await self._acquire_refresh_lock(connection_id)
            try:
                return await self._access_token_locked(record)
            finally:
                self.credentials.release_lock(fd)

    async def _access_token_locked(self, record: dict[str, Any]) -> tuple[str, str]:
        connection_id = str(record["connection_id"])
        current = self.repo.get_connection(connection_id)
        if current is None or current.get("provider") != "codex":
            raise OAuthError("Codex 连接不可用")
        if (
            current.get("status") == "needs_auth"
            or current.get("error") == "revocation_unconfirmed"
        ):
            raise OAuthError("Codex 连接需要重新登录")
        token = self.credentials.get(secret_ref(connection_id, "ACCESS_TOKEN"))
        account_id = current.get("account_id")
        if not token or not isinstance(account_id, str):
            raise OAuthError("Codex 连接需要重新登录")
        if float(current.get("expires_at") or 0) > float(self.clock()) + 90:
            return token, account_id
        refresh = self.credentials.get(secret_ref(connection_id, "REFRESH_TOKEN"))
        if not refresh:
            raise OAuthError("Codex 连接需要重新登录")
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=20) as client:
                response = await client.post(
                    _TOKEN_URL,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh,
                        "client_id": _CODEX_CLIENT_ID,
                    },
                )
            if response.status_code != 200:
                error_code = None
                if len(response.content) <= 65_536:
                    with suppress(ValueError):
                        error_body = response.json()
                        if isinstance(error_body, dict):
                            error_code = error_body.get("error")
                            if isinstance(error_code, dict):
                                error_code = error_code.get("code")
                if response.status_code == 401 or error_code == "invalid_grant":
                    self.repo.save_connection(
                        connection_id,
                        {**current, "status": "needs_auth", "error": "authentication_required"},
                    )
                    raise OAuthError("Codex 登录已过期，请重新登录")
                raise OAuthError("Codex 续期失败，请稍后重试")
            body = response.json()
            access = body.get("access_token") if isinstance(body, dict) else None
            replacement = body.get("refresh_token", refresh) if isinstance(body, dict) else None
            replacement_identity = body.get("id_token") if isinstance(body, dict) else None
            if (
                not isinstance(access, str)
                or not access
                or not isinstance(replacement, str)
                or not replacement
            ):
                raise OAuthError("Codex 续期结果无效")
            # The official refresh response does not promise expires_in. The
            # Access JWT exp is the authority for this connection's lifetime.
            refreshed_claims = _claims(access)
            expires_at = refreshed_claims.get("exp")
            if not isinstance(expires_at, int) or expires_at <= int(self.clock()) + 90:
                raise OAuthError("Codex 续期结果无效")
            refreshed_auth = refreshed_claims.get("https://api.openai.com/auth")
            if (
                not isinstance(refreshed_auth, dict)
                or refreshed_auth.get("chatgpt_account_id") != account_id
            ):
                raise OAuthError("Codex 续期账号不匹配")
            updated = {
                secret_ref(connection_id, "ACCESS_TOKEN"): access,
                secret_ref(connection_id, "REFRESH_TOKEN"): replacement,
            }
            if replacement_identity is not None:
                if not isinstance(replacement_identity, str) or not replacement_identity:
                    raise OAuthError("Codex 续期结果无效")
                identity_claims = _claims(replacement_identity)
                identity_auth = identity_claims.get("https://api.openai.com/auth")
                if (
                    not isinstance(identity_auth, dict)
                    or identity_auth.get("chatgpt_account_id") != account_id
                ):
                    raise OAuthError("Codex 续期账号不匹配")
                updated[secret_ref(connection_id, "ID_TOKEN")] = replacement_identity
            self.credentials.put_many(updated)
            self.repo.save_connection(connection_id, {**current, "expires_at": expires_at})
            return access, account_id
        except (httpx.HTTPError, ValueError) as exc:
            raise OAuthError("Codex 续期失败，请稍后重试") from exc

    async def disconnect(self, record: dict[str, Any]) -> None:
        connection_id = str(record["connection_id"])
        lock = self._refresh_locks.setdefault(connection_id, asyncio.Lock())
        async with lock:
            fd = await self._acquire_refresh_lock(connection_id)
            try:
                await self._disconnect_locked(record)
            finally:
                self.credentials.release_lock(fd)

    async def _disconnect_locked(self, record: dict[str, Any]) -> None:
        connection_id = str(record["connection_id"])
        current = self.repo.get_connection(connection_id)
        if current is None:
            raise OAuthError("Codex 连接不可用")
        refresh = self.credentials.get(secret_ref(connection_id, "REFRESH_TOKEN"))
        if refresh:
            try:
                async with httpx.AsyncClient(transport=self.transport, timeout=20) as client:
                    response = await client.post(
                        _REVOKE_URL,
                        json={
                            "token": refresh,
                            "token_type_hint": "refresh_token",
                            "client_id": _CODEX_CLIENT_ID,
                        },
                    )
                if response.status_code not in {200, 204}:
                    raise OAuthError("Codex 撤销结果未确认")
            except (httpx.HTTPError, OAuthError) as exc:
                self.repo.save_connection(
                    connection_id,
                    {**current, "status": "needs_auth", "error": "revocation_unconfirmed"},
                )
                raise OAuthError("Codex 撤销结果未确认") from exc
        self.credentials.delete_many(
            tuple(
                secret_ref(connection_id, suffix)
                for suffix in ("ACCESS_TOKEN", "REFRESH_TOKEN", "ID_TOKEN")
            )
        )
        self.repo.delete_connection(connection_id)
