"""Private Target service and Core-side dispatch commands."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn, cast

import typer
import uvicorn

from operant.application.remote_execution import RemoteExecutionController
from operant.persistence.remote_execution import SQLiteRemoteExecutionRepository
from operant.persistence.sqlite import SQLiteStore
from operant.remote.http_connector import HttpRemoteTargetConfig, HttpRemoteTargetConnector
from operant.remote.target_service import TargetServiceConfig, create_target_app
from operant.remote_control.crypto import RemoteCrypto
from operant.settings import database_path

target_app = typer.Typer(no_args_is_help=True, help="受限远端 Target 与本机分发。")


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _private_state(path: Path) -> dict[str, Any]:
    if not path.is_absolute() or path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError("Target state must be a private 0600 absolute file")
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("version") != 1 or not isinstance(value.get("private_key"), str):
        raise ValueError("invalid Target identity")
    return cast(dict[str, Any], value)


class _DispatchOnlyAuthorization:
    def authorize(self, **_kwargs: Any) -> NoReturn:
        raise PermissionError("dispatch CLI cannot authorize a new remote action")


@target_app.command("init")
def init(state_file: Path = typer.Option(...), target_id: str = typer.Option(...)) -> None:
    if not state_file.is_absolute() or state_file.exists() or state_file.is_symlink():
        raise typer.BadParameter("state file must be a new absolute path")
    public, private = RemoteCrypto.create_signing_keypair()
    state_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(state_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(
            {
                "version": 1,
                "target_id": target_id,
                "public_key": public,
                "private_key": _encode(private),
            },
            stream,
        )
        stream.flush()
        os.fsync(stream.fileno())
    typer.echo(json.dumps({"target_id": target_id, "identity_public_key": public}))


@target_app.command("serve")
def serve(
    state_file: Path = typer.Option(...),
    ledger_path: Path = typer.Option(...),
    workspace: Path = typer.Option(...),
    lease_id: str = typer.Option(...),
    fencing: int = typer.Option(...),
    expires_at: str = typer.Option(...),
    allow_argv_file: Path = typer.Option(...),
    core_origin: str = typer.Option(..., help="受限 Gateway 的 HTTPS loopback origin。"),
    core_ca_file: Path = typer.Option(...),
    tls_certfile: Path = typer.Option(...),
    tls_keyfile: Path = typer.Option(...),
    port: int = typer.Option(..., min=1, max=65535),
) -> None:
    if not tls_certfile.is_file() or not tls_keyfile.is_file():
        raise ValueError("Target TLS certificate and key are required")
    state = _private_state(state_file)
    lease_token = os.getenv("OPERANT_REMOTE_TARGET_LEASE_TOKEN", "")
    bearer = os.getenv("OPERANT_REMOTE_TARGET_BEARER_TOKEN", "")
    allow_argv = json.loads(allow_argv_file.read_text(encoding="utf-8"))
    if not isinstance(allow_argv, list) or not all(
        isinstance(item, list) and all(isinstance(part, str) for part in item)
        for item in allow_argv
    ):
        raise ValueError("allowed argv must be a JSON array of literal argv arrays")
    config = TargetServiceConfig(
        target_id=state["target_id"],
        lease_id=lease_id,
        lease_token=lease_token,
        lease_fencing=fencing,
        lease_expires_at=datetime.fromisoformat(expires_at),
        bearer_token=bearer,
        signing_private_key=_decode(state["private_key"]),
        workspace=workspace,
        ledger_path=ledger_path,
        allowed_argv=tuple(tuple(item) for item in allow_argv),
        core_origin=core_origin,
        core_ca_file=core_ca_file,
    )
    uvicorn.run(
        create_target_app(config),
        host="127.0.0.1",
        port=port,
        access_log=False,
        ssl_certfile=str(tls_certfile),
        ssl_keyfile=str(tls_keyfile),
    )


@target_app.command("dispatch")
def dispatch(
    target_id: str = typer.Option(...),
    lease_id: str = typer.Option(...),
    fencing: int = typer.Option(...),
    db_path: Path | None = typer.Option(None),
    ca_file: Path = typer.Option(...),
) -> None:
    """Dispatch an already authorized Core Job once; never creates a new action."""
    store = SQLiteStore(db_path or database_path())
    if not store.path.is_file():
        raise ValueError("Core database must already be initialized at schema v23")
    with sqlite3.connect(f"{store.path.resolve().as_uri()}?mode=ro", uri=True) as db:
        row = db.execute(
            "SELECT version,name,checksum FROM schema_migrations ORDER BY version DESC LIMIT 1"
        ).fetchone()
    expected = store._migrations()[-1]
    if row != (expected.version, expected.name, expected.checksum):
        raise ValueError("Core database must already be initialized at schema v23")
    repository = SQLiteRemoteExecutionRepository(store)
    target = repository.get_target(target_id)
    lease = repository.get_lease(lease_id)
    if (
        lease.target_id != target_id
        or lease.fencing != fencing
        or lease.released_at is not None
        or lease.expires_at <= datetime.now(timezone.utc)
    ):
        raise ValueError("Target Lease is inactive or mismatched")
    lease_token = os.getenv("OPERANT_REMOTE_TARGET_LEASE_TOKEN", "")
    if not 16 <= len(lease_token) <= 300:
        raise ValueError("OPERANT_REMOTE_TARGET_LEASE_TOKEN is required")
    bearer = os.getenv(target.credential_ref, "")
    if not bearer:
        raise ValueError("registered Target credential_ref is unavailable")
    if not ca_file.is_file():
        raise ValueError("Target CA file is unavailable")
    endpoint = (
        os.getenv(target.endpoint_ref, "") if target.endpoint_ref.isupper() else target.endpoint_ref
    )
    if not endpoint:
        raise ValueError("registered Target endpoint_ref is unavailable")
    connector = HttpRemoteTargetConnector(
        target_id=target_id,
        lease_id=lease_id,
        lease_token=lease_token,
        lease_fencing=fencing,
        config=HttpRemoteTargetConfig(
            endpoint=endpoint,
            bearer_token=bearer,
            identity_public_key=target.identity_public_key,
            ca_file=str(ca_file),
        ),
    )
    controller = RemoteExecutionController(repository, _DispatchOnlyAuthorization())
    results = controller.dispatch_available(connector, now=datetime.now(timezone.utc), limit=8)
    typer.echo(
        json.dumps(
            {"items": [item.model_dump(mode="json") for item in results]}, ensure_ascii=False
        )
    )
