#!/usr/bin/env python3
"""Exercise the production TLS/WSS gateway on a temporary local Core."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import ssl
import subprocess
import time
from pathlib import Path

import httpx
import websockets

from operant.domain.remote_control import RemoteScope, RemoteTransportMode
from operant.persistence.sqlite import SQLiteStore
from operant.remote_control.crypto import RemoteCrypto, RemoteKeyStore
from operant.remote_control.runtime import RemoteControlService, remote_control_policy_engine


def _available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


async def _exercise_gateway(
    *, port: int, token: str, host_id: str, device_id: str, session_id: str
) -> None:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    async with websockets.connect(
        f"wss://127.0.0.1:{port}/v1/remote-control/gateway",
        origin="https://control.example",
        subprotocols=["operant.remote.v1"],
        additional_headers={"Authorization": f"Bearer {token}"},
        ssl=context,
        proxy=None,
        max_size=512 * 1024,
        max_queue=16,
    ) as websocket:
        await websocket.send(
            json.dumps(
                {
                    "type": "hello",
                    "host_id": host_id,
                    "device_id": device_id,
                    "remote_session_id": session_id,
                    "protocol_version": "phase5b.v1",
                }
            )
        )
        assert json.loads(await websocket.recv())["type"] == "hello_ack"
        await websocket.send(json.dumps({"type": "ping"}))
        assert json.loads(await websocket.recv()) == {"type": "pong"}
        await websocket.send(json.dumps({"type": "cursor_sync", "after_cursor": 0}))
        events = json.loads(await websocket.recv())
        assert events["type"] == "events" and isinstance(events["items"], list)


def _prepare_identity(work: Path) -> tuple[str, str, str]:
    store = SQLiteStore(work / "operant.sqlite3")
    store.initialize()
    signing_public, _ = RemoteCrypto.create_signing_keypair()
    exchange_public, _ = RemoteCrypto.create_exchange_keypair()
    service = RemoteControlService(
        store,
        RemoteKeyStore(work / "remote-control-keys.json"),
        remote_control_policy_engine(),
    )
    host = service.enable_host(
        display_name="WSS smoke Core",
        core_version="0.1.0",
        protocol_version="phase5b.v1",
    )
    challenge = service.create_pairing_challenge(
        host.host_id,
        allowed_scopes=(RemoteScope.OBSERVE,),
    )
    device = service.pair_device(
        challenge_id=challenge.challenge_id,
        one_time_code=challenge.one_time_code,
        display_name="WSS smoke device",
        signing_public_key=signing_public,
        exchange_public_key=exchange_public,
        scopes=(RemoteScope.OBSERVE,),
    )
    session = service.create_session(
        host_id=host.host_id,
        device_id=device.device_id,
        transport_mode=RemoteTransportMode.DIRECT,
        protocol_version="phase5b.v1",
    )
    return host.host_id, device.device_id, session.remote_session_id


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--cert", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    args = parser.parse_args()
    work = args.work.absolute()
    cert = args.cert.absolute()
    key = args.key.absolute()
    if not work.is_dir() or not cert.is_file() or not key.is_file():
        raise SystemExit("work, certificate, and key must already exist")

    host_id, device_id, session_id = _prepare_identity(work)
    port = _available_port()
    token = "temporary-wss-smoke-token-that-is-long-enough"
    environment = os.environ.copy()
    environment.update(
        {
            "OPERANT_DB_PATH": str(work / "operant.sqlite3"),
            "OPERANT_REMOTE_GATEWAY_TOKEN_REF": "OPERANT_WSS_SMOKE_TOKEN",
            "OPERANT_WSS_SMOKE_TOKEN": token,
            "OPERANT_REMOTE_GATEWAY_ALLOWED_ORIGINS_JSON": '["https://control.example"]',
        }
    )
    process = subprocess.Popen(
        [
            str(Path.cwd() / ".venv/bin/operant"),
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--ssl-certfile",
            str(cert),
            "--ssl-keyfile",
            str(key),
        ],
        cwd=Path.cwd(),
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        with httpx.Client(verify=False, trust_env=False, timeout=0.5) as client:
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError("Core exited before WSS acceptance")
                try:
                    if client.get(f"https://127.0.0.1:{port}/healthz").json() == {"status": "ok"}:
                        break
                except (httpx.HTTPError, ValueError):
                    time.sleep(0.05)
            else:
                raise RuntimeError("Core did not become healthy")

            asyncio.run(
                _exercise_gateway(
                    port=port,
                    token=token,
                    host_id=host_id,
                    device_id=device_id,
                    session_id=session_id,
                )
            )
            for _ in range(60):
                rows = client.get(
                    f"https://127.0.0.1:{port}/v1/remote-control/gateway/connections"
                ).json()["items"]
                if rows and rows[0]["status"] == "closed":
                    break
                time.sleep(0.05)
            else:
                raise AssertionError("durable Gateway close projection was not observed")
        print(
            json.dumps(
                {
                    "tls": True,
                    "wss": True,
                    "subprotocol": "operant.remote.v1",
                    "durable_connection_status": rows[0]["status"],
                },
                sort_keys=True,
            )
        )
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


if __name__ == "__main__":
    main()
