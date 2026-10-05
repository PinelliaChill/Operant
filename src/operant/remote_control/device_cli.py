"""Paired device CLI for the private Remote Control gateway."""

from __future__ import annotations

import base64
import json
import os
import ssl
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
import typer
from websockets.sync.client import connect
from websockets.typing import Origin, Subprotocol

from operant.application.protocol_metadata import PHASE56_PROTOCOL_VERSION
from operant.contracts.b2_6_query import B26EncryptedReply
from operant.domain.remote_control import EncryptedRemoteCommand, PairingTicket
from operant.memory_plugins.remote_query import decrypt_reply
from operant.remote_control.crypto import RemoteCrypto
from operant.remote_control.session_executor import REMOTE_SESSION_CAPABILITIES

device_app = typer.Typer(no_args_is_help=True, help="配对设备、发送签名加密远程命令。")


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _state(path: Path) -> dict[str, Any]:
    if not path.is_absolute() or path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError("device state must be an absolute private 0600 file")
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("version") != 1 or not isinstance(value.get("signing_private"), str):
        raise ValueError("invalid device state")
    return cast(dict[str, Any], value)


def _save(path: Path, value: dict[str, Any]) -> None:
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("device state path must be absolute and not a symlink")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _origin(value: str) -> str:
    url = httpx.URL(value)
    if (
        url.scheme != "https"
        or not url.host
        or url.path not in {"", "/"}
        or url.query
        or url.fragment
        or url.userinfo
    ):
        raise ValueError("Core origin must be a plain HTTPS origin")
    return str(url).rstrip("/")


def _ca() -> str:
    value = os.getenv("OPERANT_REMOTE_CA_FILE")
    if not value or not Path(value).is_file():
        raise ValueError("OPERANT_REMOTE_CA_FILE must point to a trusted Core CA file")
    return value


def _gateway_token() -> str:
    value = os.getenv("OPERANT_REMOTE_GATEWAY_TOKEN", "")
    if not 32 <= len(value) <= 512:
        raise ValueError("OPERANT_REMOTE_GATEWAY_TOKEN is required")
    return value


def _socket(core_origin: str):  # type: ignore[no-untyped-def]
    origin = os.getenv("OPERANT_REMOTE_ORIGIN", "")
    if not origin.startswith("https://"):
        raise ValueError("OPERANT_REMOTE_ORIGIN must match the Host allowlist")
    url = _origin(core_origin).replace("https://", "wss://", 1)
    return connect(
        f"{url}/v1/remote-control/gateway",
        ssl=ssl.create_default_context(cafile=_ca()),
        origin=Origin(origin),
        additional_headers={"Authorization": f"Bearer {_gateway_token()}"},
        subprotocols=[Subprotocol("operant.remote.v1")],
        proxy=None,
        open_timeout=10,
        max_size=1024 * 1024,
    )


def _hello(socket: Any, state: dict[str, Any]) -> dict[str, Any]:
    session_id = state.get("session_id")
    if not session_id:
        raise ValueError("bind a Remote Session before connecting")
    socket.send(
        json.dumps(
            {
                "type": "hello",
                "host_id": state["host_id"],
                "device_id": state["device_id"],
                "remote_session_id": session_id,
                "protocol_version": PHASE56_PROTOCOL_VERSION,
            }
        )
    )
    response = json.loads(socket.recv(timeout=15))
    if response.get("type") != "hello_ack":
        raise ValueError(f"gateway hello rejected: {response.get('code', 'unknown')}")
    cursor = state.get("cursor", 0)
    items: list[dict[str, Any]] = []
    while True:
        socket.send(json.dumps({"type": "cursor_sync", "after_cursor": cursor}))
        events = json.loads(socket.recv(timeout=15))
        if events.get("type") != "events" or not isinstance(events.get("items"), list):
            raise ValueError("gateway cursor synchronization failed")
        page = events["items"]
        next_cursor = events.get("next_cursor")
        if not isinstance(next_cursor, int) or next_cursor < cursor or len(page) > 200:
            raise ValueError("gateway cursor page is invalid")
        items.extend(page)
        if len(items) > 10_000:
            raise ValueError("gateway event backlog exceeds the device sync limit")
        if len(page) < 200:
            return {"type": "events", "items": items, "next_cursor": next_cursor}
        if next_cursor == cursor:
            raise ValueError("gateway cursor did not advance")
        cursor = next_cursor


def _session_key(state: dict[str, Any]) -> bytes:
    return RemoteCrypto.derive_session_key(
        _decode(state["exchange_private"]),
        state["host_exchange_public"],
        session_id=state["session_id"],
        host_id=state["host_id"],
        device_id=state["device_id"],
    )


def _signed_session_command(
    state: dict[str, Any], operation: str, target_id: str, arguments: dict[str, Any]
) -> EncryptedRemoteCommand:
    capabilities = REMOTE_SESSION_CAPABILITIES.get(("session", operation))
    if capabilities is None:
        raise ValueError("unsupported remote CLI operation")
    now = datetime.now(timezone.utc)
    unsigned = EncryptedRemoteCommand(
        command_id=f"remote_command_{uuid4().hex}",
        idempotency_key=f"remote-session:{uuid4().hex}",
        host_id=state["host_id"],
        device_id=state["device_id"],
        remote_session_id=state["session_id"],
        protocol_version=PHASE56_PROTOCOL_VERSION,
        issued_at=now,
        expires_at=now + timedelta(seconds=120),
        nonce="0" * 16,
        ciphertext="pending",
        signature="0" * 32,
    )
    return RemoteCrypto.encrypt_command_payload(
        unsigned,
        {
            "tool": "session",
            "operation": operation,
            "target_id": target_id,
            "arguments": arguments,
            "capabilities": [item.value for item in capabilities],
        },
        _session_key(state),
        _decode(state["signing_private"]),
    )


def _send_pending(state_file: Path, core_origin: str, state: dict[str, Any]) -> dict[str, Any]:
    pending = state.get("pending")
    if not isinstance(pending, dict):
        raise ValueError("no pending signed command")
    command = EncryptedRemoteCommand.model_validate(pending)
    if command.expires_at <= datetime.now(timezone.utc):
        raise ValueError("pending command expired; inspect the Host receipt manually")
    with _socket(core_origin) as socket:
        events = _hello(socket, state)
        state["cursor"] = events["next_cursor"]
        terminal = next(
            (
                item
                for item in events["items"]
                if item.get("command_id") == command.command_id
                and item.get("status") in {"completed", "outcome_unknown"}
            ),
            None,
        )
        if terminal is not None:
            state.pop("pending")
            _save(state_file, state)
            return {"type": "recovered_event", "event": terminal}
        _save(state_file, state)
        socket.send(json.dumps({"type": "command", "command": pending}))
        response = json.loads(socket.recv(timeout=300))
    if (
        response.get("type") != "host_ack"
        or response.get("receipt", {}).get("command_id") != command.command_id
    ):
        raise ValueError("Host acknowledgement missing; original command remains pending")
    receipt = response["receipt"]
    if (
        receipt.get("status") in {"completed", "rejected"}
        and receipt.get("error_code") != "remote.approval_required"
    ):
        state.pop("pending")
    _save(state_file, state)
    return cast(dict[str, Any], response)


@device_app.command("init")
def init(state_file: Path = typer.Option(..., help="绝对路径，私有设备状态文件。")) -> None:
    if state_file.exists():
        raise typer.BadParameter("device state already exists")
    signing_public, signing_private = RemoteCrypto.create_signing_keypair()
    exchange_public, exchange_private = RemoteCrypto.create_exchange_keypair()
    _save(
        state_file,
        {
            "version": 1,
            "signing_public": signing_public,
            "signing_private": _encode(signing_private),
            "exchange_public": exchange_public,
            "exchange_private": _encode(exchange_private),
            "cursor": 0,
        },
    )
    typer.echo(
        json.dumps(
            {
                "state_file": str(state_file),
                "signing_public_key": signing_public,
                "exchange_public_key": exchange_public,
            }
        )
    )


@device_app.command("pair")
def pair(
    state_file: Path = typer.Option(...),
    ticket_file: Path = typer.Option(...),
    core_origin: str = typer.Option(...),
    display_name: str = typer.Option(...),
) -> None:
    state = _state(state_file)
    if state.get("device_id"):
        raise typer.BadParameter("device is already paired")
    ticket = PairingTicket.model_validate_json(ticket_file.read_text(encoding="utf-8"))
    if ticket.expires_at <= datetime.now(timezone.utc):
        raise ValueError("pairing ticket expired")
    with httpx.Client(verify=_ca(), timeout=10, follow_redirects=False, trust_env=False) as client:
        response = client.post(
            f"{_origin(core_origin)}/v1/remote-control/devices/pair",
            json={
                "challenge_id": ticket.challenge_id,
                "one_time_code": ticket.one_time_code,
                "display_name": display_name,
                "signing_public_key": state["signing_public"],
                "exchange_public_key": state["exchange_public"],
                "scopes": [scope.value for scope in ticket.allowed_scopes],
            },
        )
        response.raise_for_status()
        device = response.json()
    if (
        device["host_id"] != ticket.host_id
        or device["signing_public_key"] != state["signing_public"]
    ):
        raise ValueError("pairing response binding changed")
    state.update(
        {
            "host_id": ticket.host_id,
            "device_id": device["device_id"],
            "host_exchange_public": ticket.host_exchange_public_key,
            "host_signing_public": ticket.host_signing_public_key,
            "scopes": device["scopes"],
        }
    )
    _save(state_file, state)
    typer.echo(
        json.dumps(
            {
                "host_id": state["host_id"],
                "device_id": state["device_id"],
                "scopes": state["scopes"],
            }
        )
    )


@device_app.command("bind-session")
def bind_session(state_file: Path = typer.Option(...), session_id: str = typer.Option(...)) -> None:
    state = _state(state_file)
    if not state.get("device_id") or not session_id.startswith("remote_session_"):
        raise ValueError("pair device and supply a Remote Session ID")
    state["session_id"] = session_id
    state["cursor"] = 0
    state.pop("pending", None)
    _save(state_file, state)
    typer.echo(json.dumps({"session_id": session_id}))


@device_app.command("sync")
def sync(state_file: Path = typer.Option(...), core_origin: str = typer.Option(...)) -> None:
    state = _state(state_file)
    with _socket(core_origin) as socket:
        events = _hello(socket, state)
    state["cursor"] = events["next_cursor"]
    _save(state_file, state)
    typer.echo(json.dumps(events, ensure_ascii=False))


@device_app.command("command")
def command(
    state_file: Path = typer.Option(...),
    core_origin: str = typer.Option(...),
    operation: str = typer.Option(..., help="create/run/status/cancel（Session）"),
    target_id: str = typer.Option(...),
    arguments_file: Path = typer.Option(...),
) -> None:
    state = _state(state_file)
    if state.get("pending"):
        raise ValueError("an unacknowledged command exists; inspect it before another write")
    arguments = json.loads(arguments_file.read_text(encoding="utf-8"))
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be a JSON object")
    signed = _signed_session_command(state, operation, target_id, arguments)
    state["pending"] = signed.model_dump(mode="json")
    _save(state_file, state)
    typer.echo(json.dumps(_send_pending(state_file, core_origin, state), ensure_ascii=False))


@device_app.command("retry-pending")
def retry_pending(
    state_file: Path = typer.Option(...), core_origin: str = typer.Option(...)
) -> None:
    """Sync durable events, then resend only the same unexpired signed frame."""
    typer.echo(
        json.dumps(_send_pending(state_file, core_origin, _state(state_file)), ensure_ascii=False)
    )


@device_app.command("pending")
def pending(state_file: Path = typer.Option(...)) -> None:
    state = _state(state_file)
    item = state.get("pending")
    typer.echo(
        json.dumps(
            {
                "command_id": None if item is None else item["command_id"],
                "expires_at": None if item is None else item["expires_at"],
            }
        )
    )


@device_app.command("query-result")
def query_result(
    state_file: Path = typer.Option(...),
    core_origin: str = typer.Option(...),
    session_id: str = typer.Option(...),
) -> None:
    state = _state(state_file)
    signed = _signed_session_command(state, "status", session_id, {})
    with httpx.Client(verify=_ca(), timeout=30, follow_redirects=False, trust_env=False) as client:
        response = client.post(
            f"{_origin(core_origin)}/v1/remote-control/session-query",
            json=signed.model_dump(mode="json"),
            headers={"Idempotency-Key": signed.idempotency_key},
        )
        response.raise_for_status()
    reply = B26EncryptedReply.model_validate(response.json())
    if reply.command_id != signed.command_id or reply.device_id != state["device_id"]:
        raise ValueError("Session result reply binding changed")
    typer.echo(json.dumps(decrypt_reply(reply, _session_key(state)), ensure_ascii=False))
