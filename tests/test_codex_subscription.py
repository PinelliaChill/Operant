"""Deterministic Codex connection tests; no installed CLI or account is used."""

from __future__ import annotations

import base64
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from operant.api_model_connections import install_model_connection_routes
from operant.domain.messages import Message, MessageRole, ToolDefinition
from operant.domain.models import Effort, RoleSnapshot
from operant.model_connections.codex import CodexAttempt, CodexConnections
from operant.model_connections.credentials import CredentialStore
from operant.model_connections.oauth import OAuthConnections, secret_ref
from operant.persistence.onboarding import UXRepository
from operant.persistence.sqlite import SQLiteStore
from operant.providers.codex_responses import CodexResponsesProvider
from operant.providers.openai_compatible import ProviderError
from operant.providers.router import ConnectionProviderRouter


def _jwt(account_id: str, expires_at: int) -> str:
    payload = {
        "exp": expires_at,
        "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
    }
    middle = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"e30.{middle}.signature"


def _repository(tmp_path: Path) -> tuple[UXRepository, CredentialStore, SQLiteStore]:
    store = SQLiteStore(tmp_path / "operant.db")
    store.initialize()
    return UXRepository(store), CredentialStore(tmp_path / ".env"), store


def _fake_cli(tmp_path: Path) -> Path:
    cli = tmp_path / "codex"
    cli.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    cli.chmod(0o700)
    return cli


def test_codex_cancel_and_shutdown_reap_only_owned_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = _fake_cli(tmp_path)
    monkeypatch.setattr("operant.model_connections.codex._cli_path", lambda: cli)
    repo, credentials, _ = _repository(tmp_path)
    manager = CodexConnections(repo, credentials)
    first = manager.start()
    assert stat.S_IMODE(first.home.stat().st_mode) == 0o700
    assert stat.S_IMODE((first.home / "config.toml").stat().st_mode) == 0o600
    with pytest.raises(Exception, match="正在进行"):
        manager.start()
    competing_manager = CodexConnections(repo, credentials)
    with pytest.raises(Exception, match="正在进行"):
        competing_manager.start()
    manager.cancel(first.id)
    assert not first.home.exists()
    assert manager.was_reaped(first.id)
    second = manager.start()
    manager.close()
    assert not second.home.exists()
    assert manager.was_reaped(second.id)
    assert repo.get_connection(first.connection_id) is not None


def test_codex_parent_pipe_loss_reaps_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = _fake_cli(tmp_path)
    monkeypatch.setattr("operant.model_connections.codex._cli_path", lambda: cli)
    repo, credentials, _ = _repository(tmp_path)
    manager = CodexConnections(repo, credentials)
    attempt = manager.start()
    assert attempt.parent_fd is not None
    os.close(attempt.parent_fd)
    attempt.parent_fd = None
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline and not manager.was_reaped(attempt.id):
        time.sleep(0.05)
    assert manager.was_reaped(attempt.id)
    assert not attempt.home.exists()
    manager.close()


def test_codex_forced_group_stop_keeps_unknown_receipt_unconfirmed(tmp_path: Path) -> None:
    helper = tmp_path / "unresponsive-helper.py"
    fake_cli = tmp_path / "codex"
    fake_cli.write_text(
        f"#!{sys.executable}\n"
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    fake_cli.chmod(0o700)
    child_pid_file = tmp_path / "owned-child.pid"
    helper.write_text(
        "import pathlib, signal, subprocess, sys, time\n"
        f"child = subprocess.Popen([{str(fake_cli)!r}, 'login'])\n"
        f"pathlib.Path({str(child_pid_file)!r}).write_text(str(child.pid))\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    process = subprocess.Popen(
        [sys.executable, str(helper)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not child_pid_file.exists():
        time.sleep(0.05)
    assert child_pid_file.exists()
    child_pid = int(child_pid_file.read_text())
    repo, credentials, _ = _repository(tmp_path)
    manager = CodexConnections(repo, credentials)
    home = tmp_path / ".operant-codex-login-owned"
    home.mkdir(mode=0o700)
    lock_fd = credentials.try_acquire_lock("codex-login-global")
    assert lock_fd is not None
    attempt = CodexAttempt(
        id="b52a15ed-7fbf-4555-ab48-a66e3dc8bfde",
        connection_id="forced-stop",
        started_at=time.time(),
        home=home,
        marker=tmp_path / ".operant-codex-attempt-b52a15ed-7fbf-4555-ab48-a66e3dc8bfde.state",
        process=process,
        parent_fd=None,
        login_lock_fd=lock_fd,
        created_new=True,
        auth_generation_before=None,
        status="cancelled",
    )
    manager._cleanup(attempt)
    assert process.poll() is not None
    assert attempt.status == "failed"
    assert attempt.error == "Codex 登录子进程退出待核对"
    assert not attempt.marker.exists()
    assert home.exists()
    # The CLI shared the owned helper group and received the forced stop.
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("owned child survived the helper group stop")


def test_codex_start_database_failure_reaps_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = _fake_cli(tmp_path)
    monkeypatch.setattr("operant.model_connections.codex._cli_path", lambda: cli)
    repo, credentials, _ = _repository(tmp_path)

    def fail_save(_connection_id: str, _record: dict[str, Any]) -> None:
        raise RuntimeError("synthetic database failure")

    monkeypatch.setattr(repo, "save_connection", fail_save)
    manager = CodexConnections(repo, credentials)
    with pytest.raises(RuntimeError, match="synthetic database failure"):
        manager.start()
    assert not [path for path in tmp_path.glob(".operant-codex-login-*") if path.is_dir()]
    assert manager.attempts == {}
    fd = credentials.try_acquire_lock("codex-login-global")
    assert fd is not None
    credentials.release_lock(fd)


def test_codex_cli_inherits_network_env_without_auth_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed = tmp_path / "child-env.json"
    cli = tmp_path / "codex"
    cli.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib\n"
        f"pathlib.Path({str(observed)!r}).write_text(json.dumps({{"
        "'https_proxy': os.environ.get('HTTPS_PROXY'),"
        "'has_openai_key': 'OPENAI_API_KEY' in os.environ,"
        "'codex_home': os.environ.get('CODEX_HOME'),"
        "'has_codex_override': 'CODEX_CONFIG_DIR' in os.environ"
        "}))\n",
        encoding="utf-8",
    )
    cli.chmod(0o700)
    monkeypatch.setattr("operant.model_connections.codex._cli_path", lambda: cli)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:19999")
    monkeypatch.setenv("OPENAI_API_KEY", "private-test-token")
    monkeypatch.setenv("CODEX_CONFIG_DIR", str(tmp_path / "untrusted-config"))
    repo, credentials, _ = _repository(tmp_path)
    manager = CodexConnections(repo, credentials)
    attempt = manager.start()
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline and not observed.exists():
        time.sleep(0.05)
    assert observed.exists()
    body = json.loads(observed.read_text())
    assert body["https_proxy"] == "http://127.0.0.1:19999"
    assert body["has_openai_key"] is False
    assert body["has_codex_override"] is False
    assert body["codex_home"] == str(attempt.home)
    manager.close()


def test_codex_reauth_rejects_other_account_before_credential_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    account, other = "acct_previous", "acct_other"
    auth = {
        "auth_mode": "chatgpt",
        "tokens": {
            "access_token": _jwt(other, int(time.time()) + 3600),
            "id_token": _jwt(other, int(time.time()) + 3600),
            "refresh_token": "other-refresh",
            "account_id": other,
        },
    }
    cli = tmp_path / "codex"
    cli.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib\n"
        f"pathlib.Path(os.environ['CODEX_HOME'], 'auth.json').write_text({json.dumps(auth)!r})\n"
        "pathlib.Path(os.environ['CODEX_HOME'], 'auth.json').chmod(0o600)\n",
        encoding="utf-8",
    )
    cli.chmod(0o700)
    monkeypatch.setattr("operant.model_connections.codex._cli_path", lambda: cli)
    repo, credentials, _ = _repository(tmp_path)
    connection_id = "existing-connection"
    previous_token = _jwt(account, int(time.time()) + 3600)
    credentials.put(secret_ref(connection_id, "ACCESS_TOKEN"), previous_token)
    repo.save_connection(
        connection_id,
        {
            "connection_id": connection_id,
            "provider": "codex",
            "auth_method": "oauth",
            "status": "ready",
            "secret_ref": secret_ref(connection_id, "ACCESS_TOKEN"),
            "account_id": account,
            "auth_generation": "previous-generation",
        },
    )
    manager = CodexConnections(repo, credentials)
    attempt = manager.start(connection_id=connection_id)
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline and manager.status(attempt.id).status == "pending":
        time.sleep(0.05)
    assert manager.status(attempt.id).status == "failed"
    assert attempt.error == "账号与此 Codex 连接不一致，请新建连接"
    assert credentials.get(secret_ref(connection_id, "ACCESS_TOKEN")) == previous_token
    assert repo.get_connection(connection_id)["auth_generation"] == "previous-generation"


def test_codex_login_uses_access_exp_and_requires_new_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = "acct_fixture"
    access_exp = int(time.time()) + 3600
    auth = {
        "auth_mode": "chatgpt",
        "tokens": {
            "access_token": _jwt(account, access_exp),
            "id_token": _jwt(account, int(time.time()) + 300),
            "refresh_token": "new-refresh",
            "account_id": account,
        },
    }
    cli = tmp_path / "codex"
    cli.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib\n"
        f"pathlib.Path(os.environ['CODEX_HOME'], 'auth.json').write_text({json.dumps(auth)!r})\n"
        "pathlib.Path(os.environ['CODEX_HOME'], 'auth.json').chmod(0o600)\n",
        encoding="utf-8",
    )
    cli.chmod(0o700)
    monkeypatch.setattr("operant.model_connections.codex._cli_path", lambda: cli)
    repo, credentials, _ = _repository(tmp_path)
    connection_id = "same-account-reauth"
    repo.save_connection(
        connection_id,
        {
            "connection_id": connection_id,
            "provider": "codex",
            "auth_method": "oauth",
            "status": "ready",
            "secret_ref": secret_ref(connection_id, "ACCESS_TOKEN"),
            "account_id": account,
            "auth_generation": "old-generation",
            "selection_generation": "old-generation",
            "profile_ids": ["old-profile"],
        },
    )
    manager = CodexConnections(repo, credentials)
    attempt = manager.start(connection_id=connection_id)
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline and manager.status(attempt.id).status == "pending":
        time.sleep(0.05)
    assert manager.status(attempt.id).status == "completed"
    current = repo.get_connection(connection_id)
    assert current is not None
    assert current["status"] == "connected"
    assert current["expires_at"] == access_exp
    assert current["profile_ids"] == ["old-profile"]
    assert current["selection_generation"] is None
    assert current["auth_generation"] != "old-generation"
    assert (
        credentials.get(secret_ref(connection_id, "ACCESS_TOKEN")) == auth["tokens"]["access_token"]
    )
    router = ConnectionProviderRouter(
        repo,
        credentials,
        OAuthConnections(repo, credentials),
        codex=manager,
    )
    router._report_status(secret_ref(connection_id, "ACCESS_TOKEN"), 200, "connected")
    assert repo.get_connection(connection_id)["status"] == "connected"


def test_codex_lost_start_response_recovers_after_core_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = _fake_cli(tmp_path)
    monkeypatch.setattr("operant.model_connections.codex._cli_path", lambda: cli)
    repo, _, store = _repository(tmp_path)

    class Gateway:
        def guard(self, **_kwargs: Any) -> tuple[Any, Any, None]:
            return (
                SimpleNamespace(action_hash="hash"),
                SimpleNamespace(decision=SimpleNamespace(value="allow"), lease=object()),
                None,
            )

        def consume(self, _lease: Any, _action: Any) -> None:
            pass

    def app() -> FastAPI:
        result = FastAPI()
        install_model_connection_routes(
            result,
            cast(Any, SimpleNamespace(store=store)),
            repo,
            action_gateway=cast(Any, Gateway()),
        )
        return result

    with TestClient(app(), base_url="http://127.0.0.1:4343") as client:
        started = client.post(
            "/v1/setup/oauth/start",
            json={"provider": "codex"},
            headers={"Idempotency-Key": "codex-start-lost-response"},
        )
        assert started.status_code == 200, started.text
        assert started.json()["authorization_url"] is None
        attempt_id = started.json()["attempt_id"]
        live = client.get("/v1/setup/connections/requests/codex-start-lost-response")
        assert live.json()["oauth_attempt"]["attempt_id"] == attempt_id
        competing = client.post(
            "/v1/setup/oauth/start",
            json={"provider": "codex"},
            headers={"Idempotency-Key": "codex-competing-start"},
        )
        assert competing.status_code == 400
        assert competing.json()["detail"]["code"] == "codex_login_in_progress"

    with TestClient(app(), base_url="http://127.0.0.1:4343") as client:
        recovered = client.get("/v1/setup/connections/requests/codex-start-lost-response")
        assert recovered.status_code == 200
        body = recovered.json()
        assert body["status"] == "completed"
        assert body["connection"]["provider"] == "codex"
        assert body["oauth_attempt"]["attempt_id"] == attempt_id
        assert body["oauth_attempt"]["status"] == "expired"
        repeated = client.post(
            "/v1/setup/oauth/start",
            json={"provider": "codex"},
            headers={"Idempotency-Key": "codex-start-lost-response"},
        )
        assert repeated.status_code == 200
        assert repeated.json()["attempt_id"] == attempt_id
    assert not [path for path in tmp_path.glob(".operant-codex-login-*") if path.is_dir()]


def test_codex_lost_cancel_response_uses_its_own_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = _fake_cli(tmp_path)
    monkeypatch.setattr("operant.model_connections.codex._cli_path", lambda: cli)
    repo, _, store = _repository(tmp_path)

    class Gateway:
        def guard(self, **_kwargs: Any) -> tuple[Any, Any, None]:
            return (
                SimpleNamespace(action_hash="hash"),
                SimpleNamespace(decision=SimpleNamespace(value="allow"), lease=object()),
                None,
            )

        def consume(self, _lease: Any, _action: Any) -> None:
            pass

    def app() -> FastAPI:
        result = FastAPI()
        install_model_connection_routes(
            result,
            cast(Any, SimpleNamespace(store=store)),
            repo,
            action_gateway=cast(Any, Gateway()),
        )
        return result

    with TestClient(app(), base_url="http://127.0.0.1:4343") as client:
        started = client.post(
            "/v1/setup/oauth/start",
            json={"provider": "codex"},
            headers={"Idempotency-Key": "codex-start-for-cancel"},
        )
        assert started.status_code == 200
        attempt_id = started.json()["attempt_id"]
        cancelled = client.delete(
            f"/v1/setup/oauth/{attempt_id}",
            headers={"Idempotency-Key": "codex-cancel-lost-response"},
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
    with TestClient(app(), base_url="http://127.0.0.1:4343") as client:
        recovered = client.get("/v1/setup/connections/requests/codex-cancel-lost-response")
        assert recovered.status_code == 200
        assert recovered.json()["status"] == "completed"
        assert recovered.json()["oauth_attempt"]["attempt_id"] == attempt_id
        assert recovered.json()["oauth_attempt"]["status"] == "cancelled"
        repeated = client.delete(
            f"/v1/setup/oauth/{attempt_id}",
            headers={"Idempotency-Key": "codex-cancel-lost-response"},
        )
        assert repeated.status_code == 200
        assert repeated.json()["status"] == "cancelled"
        repo.delete_connection(cancelled.json()["connection_id"])
        without_connection = client.get("/v1/setup/connections/requests/codex-cancel-lost-response")
        assert without_connection.json()["status"] == "completed"
        assert without_connection.json()["connection"] is None


@pytest.mark.parametrize("race", ["reauth", "disconnect", "none"])
def test_codex_select_rechecks_generation_under_connection_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, race: str
) -> None:
    repo, _, store = _repository(tmp_path)
    connection_id = "select-race"
    original_record = {
        "connection_id": connection_id,
        "name": "Codex",
        "provider": "codex",
        "auth_method": "oauth",
        "status": "connected",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "secret_ref": secret_ref(connection_id, "ACCESS_TOKEN"),
        "auth_generation": "old-generation",
        "model_ids": ["exact-model"],
        "model_names": {"exact-model": "Exact model"},
        "model_capabilities": {
            "exact-model": {
                "reasoning_levels": ["medium"],
                "default_reasoning_level": "medium",
            }
        },
        "profile_ids": [],
    }
    repo.save_connection(connection_id, original_record)
    created: list[str] = []

    class Service:
        def __init__(self) -> None:
            self.store = store

        def add_model_profile(self, profile: Any) -> Any:
            if race == "none":
                competing_fd = CredentialStore(tmp_path / ".env").try_acquire_lock(
                    "codex-refresh-selectrace"
                )
                assert competing_fd is None
            created.append(profile.id)
            return profile

    class Gateway:
        def guard(self, **_kwargs: Any) -> tuple[Any, Any, None]:
            return (
                SimpleNamespace(action_hash="hash"),
                SimpleNamespace(decision=SimpleNamespace(value="allow"), lease=object()),
                None,
            )

        def consume(self, _lease: Any, _action: Any) -> None:
            pass

    app = FastAPI()
    install_model_connection_routes(
        app, cast(Any, Service()), repo, action_gateway=cast(Any, Gateway())
    )
    original_lock = CredentialStore.try_acquire_lock
    mutated = False

    def race_before_lock(self: CredentialStore, name: str) -> int | None:
        nonlocal mutated
        if name == "codex-refresh-selectrace" and not mutated:
            mutated = True
            if race == "reauth":
                repo.save_connection(
                    connection_id,
                    {**original_record, "auth_generation": "new-generation", "model_ids": []},
                )
            elif race == "disconnect":
                repo.delete_connection(connection_id)
        return original_lock(self, name)

    monkeypatch.setattr(CredentialStore, "try_acquire_lock", race_before_lock)
    with TestClient(app, base_url="http://127.0.0.1:4343") as client:
        response = client.post(
            f"/v1/setup/connections/{connection_id}/profiles",
            json={"model_id": "exact-model"},
            headers={"Idempotency-Key": f"select-{race}"},
        )
    assert response.status_code == (200 if race == "none" else 409)
    assert len(created) == (1 if race == "none" else 0)
    current = repo.get_connection(connection_id)
    if race == "reauth":
        assert current is not None and current["auth_generation"] == "new-generation"
        assert current["profile_ids"] == []
    elif race == "disconnect":
        assert current is None
    else:
        assert current is not None and current["status"] == "ready"
        assert current["profile_ids"] == created


@pytest.mark.asyncio
@pytest.mark.parametrize("expires_in", [None, 172800, "172800", 1])
async def test_codex_refresh_uses_access_jwt_exp(
    tmp_path: Path, expires_in: int | str | None
) -> None:
    repo, credentials, _ = _repository(tmp_path)
    connection_id = "refresh-test"
    account = "acct_fixture"
    old_access = _jwt(account, int(time.time()) + 30)
    new_expires_at = int(time.time()) + 3600
    new_access = _jwt(account, new_expires_at)
    new_identity = _jwt(account, int(time.time()) + 1000)
    reference = secret_ref(connection_id, "ACCESS_TOKEN")
    credentials.put_many(
        {
            reference: old_access,
            secret_ref(connection_id, "REFRESH_TOKEN"): "old-refresh",
            secret_ref(connection_id, "ID_TOKEN"): "old-id",
        }
    )
    record = {
        "connection_id": connection_id,
        "provider": "codex",
        "status": "connected",
        "account_id": account,
        "expires_at": time.time() + 30,
    }
    repo.save_connection(connection_id, record)

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/oauth/token"
        payload: dict[str, Any] = {
            "access_token": new_access,
            "refresh_token": "rotated-refresh",
            "id_token": new_identity,
        }
        if expires_in is not None:
            payload["expires_in"] = expires_in
        return httpx.Response(
            200,
            json=payload,
        )

    manager = CodexConnections(repo, credentials, transport=httpx.MockTransport(handle))
    assert await manager.access_token(record) == (new_access, account)
    assert credentials.get(reference) == new_access
    assert credentials.get(secret_ref(connection_id, "REFRESH_TOKEN")) == "rotated-refresh"
    assert credentials.get(secret_ref(connection_id, "ID_TOKEN")) == new_identity
    assert repo.get_connection(connection_id)["expires_at"] == new_expires_at


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "body", "expected_status", "expected_error", "expected_copy"),
    [
        (
            400,
            {"error": "invalid_grant"},
            "needs_auth",
            "authentication_required",
            "Codex connection needs sign-in",
        ),
        (
            503,
            {"error": "temporarily_unavailable"},
            "connected",
            None,
            "Codex authorization could not be refreshed; try again",
        ),
    ],
)
async def test_codex_refresh_failure_keeps_safe_state(
    tmp_path: Path,
    status_code: int,
    body: dict[str, str],
    expected_status: str,
    expected_error: str | None,
    expected_copy: str,
) -> None:
    repo, credentials, _ = _repository(tmp_path)
    connection_id = "refresh-failure"
    reference = secret_ref(connection_id, "ACCESS_TOKEN")
    access = _jwt("acct_fixture", int(time.time()) + 30)
    credentials.put_many(
        {reference: access, secret_ref(connection_id, "REFRESH_TOKEN"): "old-refresh"}
    )
    record = {
        "connection_id": connection_id,
        "provider": "codex",
        "status": "connected",
        "account_id": "acct_fixture",
        "expires_at": time.time() + 30,
    }
    repo.save_connection(connection_id, record)
    manager = CodexConnections(
        repo,
        credentials,
        transport=httpx.MockTransport(lambda _request: httpx.Response(status_code, json=body)),
    )
    provider = CodexResponsesProvider(manager, repo, lambda _reference: record, lambda *_args: None)
    with pytest.raises(ProviderError, match=expected_copy):
        await provider.list_models(
            base_url="https://chatgpt.com/backend-api/codex", secret_ref=reference
        )
    current = repo.get_connection(connection_id)
    assert current is not None
    assert current["status"] == expected_status
    assert current.get("error") == expected_error
    assert credentials.get(reference) == access
    assert credentials.get(secret_ref(connection_id, "REFRESH_TOKEN")) == "old-refresh"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("upstream_code", "expected_error"),
    [
        ("usage_limit_reached", "usage_limit"),
        ("rate_limit_exceeded", "rate_limited"),
        ("unknown_limit", "limit_unconfirmed"),
    ],
)
async def test_codex_429_uses_only_known_limit_codes(
    tmp_path: Path, upstream_code: str, expected_error: str
) -> None:
    repo, credentials, _ = _repository(tmp_path)
    connection_id = "limit-test"
    reference = secret_ref(connection_id, "ACCESS_TOKEN")
    credentials.put(reference, _jwt("acct_fixture", int(time.time()) + 3600))
    repo.save_connection(
        connection_id,
        {
            "connection_id": connection_id,
            "provider": "codex",
            "auth_method": "oauth",
            "status": "connected",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "secret_ref": reference,
            "account_id": "acct_fixture",
            "auth_generation": "generation-1",
            "expires_at": time.time() + 3600,
        },
    )
    manager = CodexConnections(repo, credentials)
    router = ConnectionProviderRouter(
        repo, credentials, OAuthConnections(repo, credentials), codex=manager
    )
    router.codex = CodexResponsesProvider(
        manager,
        repo,
        router._connection,
        router._report_status,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                429, json={"error": {"type": upstream_code, "code": upstream_code}}
            )
        ),
    )
    with pytest.raises(ProviderError, match="HTTP 429"):
        await router.list_models(
            base_url="https://chatgpt.com/backend-api/codex", secret_ref=reference
        )
    assert repo.get_connection(connection_id)["error"] == expected_error


@pytest.mark.asyncio
async def test_codex_catalog_lite_stream_and_restart_reasoning(tmp_path: Path) -> None:
    repo, credentials, _ = _repository(tmp_path)
    connection_id, account = "stream-test", "acct_fixture"
    reference = secret_ref(connection_id, "ACCESS_TOKEN")
    credentials.put(reference, _jwt(account, int(time.time()) + 3600))
    record = {
        "connection_id": connection_id,
        "provider": "codex",
        "auth_method": "oauth",
        "status": "connected",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "secret_ref": reference,
        "account_id": account,
        "auth_generation": "generation-1",
        "expires_at": time.time() + 3600,
        "model_ids": [],
    }
    repo.save_connection(connection_id, record)
    requests: list[dict[str, Any]] = []
    model_id = "catalog-exact-model"

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["chatgpt-account-id"] == account
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "slug": model_id,
                            "display_name": "Catalog model",
                            "visibility": "list",
                            "input_modalities": ["text"],
                            "supported_reasoning_levels": [
                                {"effort": "low", "description": ""},
                                {"effort": "medium", "description": ""},
                                {"effort": "xhigh", "description": ""},
                            ],
                            "default_reasoning_level": "medium",
                            "use_responses_lite": True,
                            "context_window": 272000,
                        }
                    ]
                },
            )
        payload = json.loads(request.content)
        requests.append(payload)
        completed = {
            "type": "response.completed",
            "response": {
                "id": "response-1",
                "status": "completed",
                "output": [
                    {
                        "type": "reasoning",
                        "id": "reasoning-1",
                        "encrypted_content": "opaque-ciphertext",
                    },
                    {
                        "type": "function_call",
                        "call_id": "call-1",
                        "name": "read_file",
                        "arguments": '{"path":"/tmp/example"}',
                    },
                ],
                "usage": {"input_tokens": 10, "output_tokens": 3},
            },
        }
        return httpx.Response(200, text=f"data: {json.dumps(completed)}\n\ndata: [DONE]\n\n")

    manager = CodexConnections(repo, credentials)
    provider = CodexResponsesProvider(
        manager,
        repo,
        lambda _ref: repo.get_connection(connection_id),
        lambda _ref, _status, _reason: None,
        transport=httpx.MockTransport(handle),
    )
    assert await provider.list_models(base_url=record["base_url"], secret_ref=reference) == [
        model_id
    ]
    current = repo.get_connection(connection_id)
    assert current is not None
    assert current["model_capabilities"][model_id]["reasoning_levels"] == [
        "low",
        "medium",
        "xhigh",
    ]
    assert current["model_capabilities"][model_id]["context_window"] == 272000
    repo.save_connection(connection_id, {**current, "model_ids": [model_id]})
    snapshot = cast(
        RoleSnapshot,
        SimpleNamespace(
            provider="codex",
            model_id=model_id,
            model_profile_id="profile-1",
            base_url=record["base_url"],
            secret_ref=reference,
            effort=Effort.MEDIUM,
            provider_effort_value="medium",
        ),
    )
    user = Message(role=MessageRole.USER, content="Read this")
    tools = [ToolDefinition(name="read_file", description="Read", parameters={"type": "object"})]
    events = [
        event async for event in provider.stream(snapshot=snapshot, messages=[user], tools=tools)
    ]
    response = events[-1].response
    assert response is not None and response.tool_calls[0].id == "call-1"
    assert response.usage is not None and response.usage.total_tokens == 13
    assert requests[0]["input"][0]["type"] == "additional_tools"
    assert requests[0]["input"][1] == {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "Read this"}],
    }
    assert "opaque-ciphertext" not in json.dumps([event.model_dump() for event in events])
    restarted = CodexResponsesProvider(
        manager,
        repo,
        lambda _ref: repo.get_connection(connection_id),
        lambda _ref, _status, _reason: None,
        transport=httpx.MockTransport(handle),
    )
    assistant = Message(role=MessageRole.ASSISTANT, tool_calls=response.tool_calls)
    followup = restarted._input(
        snapshot,
        [user, assistant, Message(role=MessageRole.TOOL, tool_call_id="call-1", content="done")],
    )
    assert followup[1] == {
        "type": "reasoning",
        "id": "reasoning-1",
        "encrypted_content": "opaque-ciphertext",
    }
    assert followup[-1] == {"type": "function_call_output", "call_id": "call-1", "output": "done"}


@pytest.mark.asyncio
async def test_codex_incomplete_stream_fails_explicitly(tmp_path: Path) -> None:
    repo, credentials, _ = _repository(tmp_path)
    connection_id = "incomplete-test"
    reference = secret_ref(connection_id, "ACCESS_TOKEN")
    credentials.put(reference, _jwt("acct_fixture", int(time.time()) + 3600))
    repo.save_connection(
        connection_id,
        {
            "connection_id": connection_id,
            "provider": "codex",
            "status": "ready",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "secret_ref": reference,
            "account_id": "acct_fixture",
            "auth_generation": "generation-1",
            "expires_at": time.time() + 3600,
            "model_ids": ["exact-model"],
            "model_capabilities": {"exact-model": {"reasoning_levels": ["medium"]}},
        },
    )
    provider = CodexResponsesProvider(
        CodexConnections(repo, credentials),
        repo,
        lambda _ref: repo.get_connection(connection_id),
        lambda _ref, _status, _reason: None,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, text='data: {"type":"response.output_text.delta","delta":"partial"}\n\n'
            )
        ),
    )
    snapshot = cast(
        RoleSnapshot,
        SimpleNamespace(
            model_id="exact-model",
            model_profile_id="profile-1",
            base_url="https://chatgpt.com/backend-api/codex",
            secret_ref=reference,
            effort=Effort.MEDIUM,
            provider_effort_value="medium",
        ),
    )
    with pytest.raises(ProviderError, match="without response.completed"):
        _ = [
            event
            async for event in provider.stream(
                snapshot=snapshot,
                messages=[Message(role=MessageRole.USER, content="hello")],
                tools=[],
            )
        ]
