"""Independent caller SDK checks with an isolated Core runtime and no live service."""

from __future__ import annotations

import json
import os
import socket
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from operant.application.skill_sources import SkillSourceOperationError
from operant.caller_pairing.crypto import (
    COMMAND_PATH,
    PAIR_PATH,
    READBACK_PATH,
    b64,
    canonical_bytes,
    decrypt,
    derive_key,
    encrypt,
    sign,
)
from operant.caller_pairing.runtime import CallerPairingRuntime
from operant.contracts.caller_pairing import CallerCommand, CallerPairRequest
from operant.persistence.caller_pairing_schema import upgrade
from operant.persistence.sqlite import SQLiteStore
from sdk.python_client.caller_pairing import (
    TICKET_PREFIX,
    PairedSkillSourceClient,
    PairingClientError,
    _base_url,
    strict_loopback_transport,
)
from sdk.python_client.caller_pairing_generated import CALLER_PAIRING_SCHEMA_DIGEST
from sdk.python_client.transport import TransportRequest, TransportResponse


class Effects:
    def __init__(self) -> None:
        self.add_calls = 0
        self.ask = False

    def list_sources(self) -> SimpleNamespace:
        return SimpleNamespace(
            model_dump=lambda **_: {"items": [{"root_ref": "user-000000000001"}]}
        )

    def add_source(self, path: str, _key: str) -> SimpleNamespace:
        self.add_calls += 1
        if self.ask:
            raise SkillSourceOperationError(
                409, {"code": "approval_required", "approval_id": "approval_test"}
            )
        return SimpleNamespace(model_dump=lambda **_: {"path": path})

    def remove_source(self, root_ref: str, _key: str) -> SimpleNamespace:
        return SimpleNamespace(model_dump=lambda **_: {"root_ref": root_ref})


class FakeTransport:
    def __init__(self, runtime: CallerPairingRuntime) -> None:
        self.runtime = runtime
        self.lose_pair_once = False
        self.lose_add_once = False
        self.tamper_reply = False
        self.stale_reply = False
        self.invalid_grant = False
        self.pair_bodies: list[str] = []

    def __call__(self, request: TransportRequest) -> TransportResponse:
        path = urlsplit(request.url).path
        if path == "/v1/protocol/caller-pairing":
            result = {
                "protocol_version": "caller-pairing.v1",
                "schema_digest": CALLER_PAIRING_SCHEMA_DIGEST,
                "min_client_version": "caller-pairing.v1",
                "capabilities": [],
            }
        elif path == "/v1/local-callers/pair":
            assert request.body is not None
            self.pair_bodies.append(request.body)
            result = self.runtime.pair(
                CallerPairRequest.model_validate_json(request.body)
            ).model_dump()
            if self.lose_pair_once:
                self.lose_pair_once = False
                raise OSError("SENTINEL_PRIVATE_TRANSPORT_DETAIL")
        elif path in {"/v1/local-callers/commands", "/v1/local-callers/requests/readback"}:
            assert request.body is not None
            command = CallerCommand.model_validate_json(request.body)
            result = self.runtime.command(command, path=path).model_dump()
            if command.operation == "add" and self.lose_add_once:
                self.lose_add_once = False
                raise OSError("SENTINEL_PRIVATE_TRANSPORT_DETAIL")
        else:
            raise AssertionError(path)
        if self.tamper_reply and path != "/v1/protocol/caller-pairing":
            result["signature"] = "A" + result["signature"][1:]
        if self.invalid_grant and path == PAIR_PATH:
            request_value = json.loads(request.body or "{}")
            key = derive_key(
                self.runtime.keys.exchange,
                request_value["device_exchange_public_key"],
                epoch_id=result["core_epoch_id"],
                device_id_value=result["device_id"],
                purpose="pair-reply",
            )
            clear = decrypt(result, key, path=PAIR_PATH)
            clear["expires_at"] = True
            result["ciphertext"] = encrypt(clear, result, key, path=PAIR_PATH)
            result["signature"] = sign(result, self.runtime.keys.signing, path=PAIR_PATH)
        if self.stale_reply and path != "/v1/protocol/caller-pairing":
            result["issued_at"] -= 31
            result["signature"] = sign(
                result,
                self.runtime.keys.signing,
                path=PAIR_PATH
                if path == PAIR_PATH
                else READBACK_PATH
                if path == READBACK_PATH
                else COMMAND_PATH,
            )
        return TransportResponse(
            status=200, headers={"content-type": "application/json"}, body=canonical_bytes(result)
        )


def _runtime(tmp_path: Path) -> tuple[CallerPairingRuntime, Effects]:
    store = SQLiteStore(tmp_path / "core.sqlite3")
    with store._connect() as connection:
        upgrade(connection)
    effects = Effects()
    return CallerPairingRuntime(store, effects_for_device=lambda _device: effects), effects


def _ticket(runtime: CallerPairingRuntime) -> str:
    ticket = runtime.challenge(base_url="http://127.0.0.1:18778", ttl_seconds=120)
    return TICKET_PREFIX + b64(canonical_bytes(ticket.model_dump()))


def test_pair_pins_core_and_private_keys_then_lists_and_adds(tmp_path: Path) -> None:
    runtime, effects = _runtime(tmp_path)
    transport = FakeTransport(runtime)
    directory = tmp_path / "client"
    client = PairedSkillSourceClient(directory, transport=transport)
    paired = client.pair_ticket(_ticket(runtime), "TUI 测试")
    assert paired["scope"] == "skill_source.manage"
    assert (directory / ".env").stat().st_mode & 0o777 == 0o600
    assert stat.S_ISREG((directory / ".env").stat().st_mode)
    assert "PRIVATE KEY" not in (directory / "state.json").read_text()
    assert client.list_sources()["state"] == "completed"
    assert client.add_source("/tmp/skills")["state"] == "completed"
    assert effects.add_calls == 1
    assert client.pending_request() is None
    assert client.remove_source("user-000000000001")["state"] == "completed"


def test_lost_pair_response_requires_explicit_same_envelope_retry(tmp_path: Path) -> None:
    runtime, _ = _runtime(tmp_path)
    transport = FakeTransport(runtime)
    transport.lose_pair_once = True
    client = PairedSkillSourceClient(tmp_path / "client", transport=transport)
    with pytest.raises(PairingClientError, match="结果未知") as error:
        client.pair_ticket(_ticket(runtime), "Local test")
    assert error.value.code == "outcome_unknown"
    assert client.retry_pair_same_envelope()["scope"] == "skill_source.manage"
    assert len(transport.pair_bodies) == 2
    assert transport.pair_bodies[0] == transport.pair_bodies[1]


def test_lost_write_persists_only_identity_and_readback_never_replays(tmp_path: Path) -> None:
    runtime, effects = _runtime(tmp_path)
    transport = FakeTransport(runtime)
    directory = tmp_path / "client"
    client = PairedSkillSourceClient(directory, transport=transport)
    client.pair_ticket(_ticket(runtime), "Local test")
    transport.lose_add_once = True
    with pytest.raises(PairingClientError) as error:
        client.add_source("/tmp/skills")
    assert error.value.code == "outcome_unknown"
    pending = json.loads((directory / "pending.json").read_text())
    assert set(pending) == {"device_id", "operation", "request_id", "payload_hash"}
    assert "/tmp/skills" not in (directory / "pending.json").read_text()
    with pytest.raises(PairingClientError):
        client.add_source("/tmp/other")
    reopened = PairedSkillSourceClient(directory, transport=transport)
    assert reopened.readback(pending["request_id"])["state"] == "completed"
    assert reopened.pending_request() is None
    assert effects.add_calls == 1


def test_invalid_ticket_pin_reply_and_secret_permissions_fail_closed(tmp_path: Path) -> None:
    runtime, _ = _runtime(tmp_path)
    transport = FakeTransport(runtime)
    client = PairedSkillSourceClient(tmp_path / "client", transport=transport)
    ticket = _ticket(runtime)
    with pytest.raises(PairingClientError):
        client.pair_ticket(ticket + "A", "Local test")
    transport.tamper_reply = True
    with pytest.raises(PairingClientError) as error:
        client.pair_ticket(ticket, "Local test")
    assert error.value.code == "invalid_reply"
    transport.tamper_reply = False
    client.pair_ticket(_ticket(runtime), "Local test")
    os.chmod(tmp_path / "client" / ".env", 0o644)
    with pytest.raises(PairingClientError) as unsafe:
        client.list_sources()
    assert unsafe.value.code == "unsafe_storage"


def test_symlink_storage_and_signed_stale_reply_fail_closed(tmp_path: Path) -> None:
    runtime, _ = _runtime(tmp_path)
    transport = FakeTransport(runtime)
    client = PairedSkillSourceClient(tmp_path / "client", transport=transport)
    transport.stale_reply = True
    with pytest.raises(PairingClientError) as stale:
        client.pair_ticket(_ticket(runtime), "Local test")
    assert stale.value.code == "invalid_reply"
    transport.stale_reply = False
    client.pair_ticket(_ticket(runtime), "Local test")
    key_path = tmp_path / "client" / ".env"
    outside = tmp_path / "outside.env"
    outside.write_bytes(key_path.read_bytes())
    key_path.unlink()
    key_path.symlink_to(outside)
    with pytest.raises(PairingClientError) as linked:
        client.list_sources()
    assert linked.value.code == "unsafe_storage"
    assert outside.read_bytes()


def test_signed_pair_reply_with_boolean_grant_is_rejected(tmp_path: Path) -> None:
    runtime, _ = _runtime(tmp_path)
    transport = FakeTransport(runtime)
    transport.invalid_grant = True
    client = PairedSkillSourceClient(tmp_path / "client", transport=transport)
    with pytest.raises(PairingClientError) as rejected:
        client.pair_ticket(_ticket(runtime), "Local test")
    assert rejected.value.code == "invalid_reply"
    assert not (tmp_path / "client" / "state.json").exists()


def test_tampered_public_pin_and_state_directory_symlink_fail_closed(tmp_path: Path) -> None:
    runtime, _ = _runtime(tmp_path)
    transport = FakeTransport(runtime)
    directory = tmp_path / "client"
    client = PairedSkillSourceClient(directory, transport=transport)
    client.pair_ticket(_ticket(runtime), "Local test")
    (tmp_path / "other").mkdir()
    other, _ = _runtime(tmp_path / "other")
    state_file = directory / "state.json"
    state = json.loads(state_file.read_text())
    state["core_signing_public_key"] = other.keys.signing_public
    state_file.write_bytes(canonical_bytes(state))
    with pytest.raises(PairingClientError) as pin:
        client.list_sources()
    assert pin.value.code == "invalid_reply"
    linked = tmp_path / "linked"
    linked.symlink_to(directory, target_is_directory=True)
    with pytest.raises(PairingClientError) as unsafe:
        PairedSkillSourceClient(linked, transport=transport).list_sources()
    assert unsafe.value.code == "unsafe_storage"


def test_second_window_cannot_overwrite_unknown_write(tmp_path: Path) -> None:
    runtime, effects = _runtime(tmp_path)
    transport = FakeTransport(runtime)
    directory = tmp_path / "client"
    first = PairedSkillSourceClient(directory, transport=transport)
    first.pair_ticket(_ticket(runtime), "Local test")
    transport.lose_add_once = True
    with pytest.raises(PairingClientError):
        first.add_source("/tmp/skills")
    original = (directory / "pending.json").read_bytes()
    second = PairedSkillSourceClient(directory, transport=transport)
    with pytest.raises(PairingClientError) as blocked:
        second.remove_source("user-000000000001")
    assert blocked.value.code == "outcome_unknown"
    assert (directory / "pending.json").read_bytes() == original
    assert effects.add_calls == 1


def test_ask_requires_explicit_bound_continue(tmp_path: Path) -> None:
    runtime, effects = _runtime(tmp_path)
    approval = {"status": "pending"}
    runtime.phase_repo = SimpleNamespace(  # type: ignore[assignment]
        get_phase45_approval=lambda _id: {"action_hash": "a" * 64, "status": approval["status"]}
    )
    effects.ask = True
    client = PairedSkillSourceClient(tmp_path / "client", transport=FakeTransport(runtime))
    client.pair_ticket(_ticket(runtime), "Local test")
    first = client.add_source("/tmp/skills")
    assert first["state"] == "awaiting_approval"
    assert client.pending_request() is not None
    with pytest.raises(PairingClientError) as mismatch:
        client.continue_approved(first["approval_id"], "b" * 64)
    assert mismatch.value.code == "approval_mismatch"
    assert effects.add_calls == 1
    approval["status"] = "approved"
    effects.ask = False
    done = client.continue_approved(first["approval_id"], first["action_hash"])
    assert done["state"] == "completed"
    assert effects.add_calls == 2
    assert client.pending_request() is None


def test_numeric_loopback_only_and_redirect_rejected(tmp_path: Path) -> None:
    for value in [
        "https://127.0.0.1:8000",
        "http://localhost:8000",
        "http://example.com:8000",
        "http://127.0.0.2:8000",
        "http://127.0.0.1:8000/path",
        "http://127.0.0.1:8000#fragment",
    ]:
        with pytest.raises(PairingClientError):
            _base_url(value)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    import threading

    def serve() -> None:
        connection, _ = listener.accept()
        connection.recv(4096)
        connection.sendall(
            b"HTTP/1.1 302 Found\r\nLocation: http://example.com\r\nContent-Length: 0\r\n\r\n"
        )
        connection.close()
        listener.close()

    thread = threading.Thread(target=serve)
    thread.start()
    with pytest.raises(PairingClientError) as error:
        strict_loopback_transport(
            TransportRequest(
                method="GET", url=f"http://127.0.0.1:{port}/v1/protocol/caller-pairing", headers={}
            )
        )
    thread.join()
    assert error.value.code == "redirect_rejected"
    with pytest.raises(PairingClientError) as unsigned:
        strict_loopback_transport(
            TransportRequest(
                method="GET", url=f"http://127.0.0.1:{port}/v1/setup/state", headers={}
            )
        )
    assert unsigned.value.code == "invalid_request"


def test_real_loopback_http_transport_pairs_and_manages_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, effects = _runtime(tmp_path)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            pass

        def _reply(self, value: dict[str, object]) -> None:
            body = canonical_bytes(value)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            assert self.path == "/v1/protocol/caller-pairing"
            self._reply(
                {
                    "protocol_version": "caller-pairing.v1",
                    "schema_digest": CALLER_PAIRING_SCHEMA_DIGEST,
                    "min_client_version": "caller-pairing.v1",
                    "capabilities": [],
                }
            )

        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            if self.path == PAIR_PATH:
                value = runtime.pair(CallerPairRequest.model_validate_json(raw)).model_dump()
            elif self.path in {COMMAND_PATH, READBACK_PATH}:
                value = runtime.command(
                    CallerCommand.model_validate_json(raw), path=self.path
                ).model_dump()
            else:
                raise AssertionError(self.path)
            self._reply(value)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        ticket = runtime.challenge(
            base_url=f"http://127.0.0.1:{server.server_port}", ttl_seconds=120
        )
        client = PairedSkillSourceClient(tmp_path / "http-client")
        paired = client.pair_ticket(
            TICKET_PREFIX + b64(canonical_bytes(ticket.model_dump())), "HTTP"
        )
        assert paired["scope"] == "skill_source.manage"
        assert client.list_sources()["state"] == "completed"
        assert client.add_source("/tmp/skills")["state"] == "completed"
        assert effects.add_calls == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
