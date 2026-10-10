"""Deterministic transport, persistence and permission-boundary checks."""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient

from operant.application.security import PolicyEngine, balanced_policy_bundle
from operant.application.skill_sources import SkillSourceOperationError
from operant.caller_pairing.api import install_caller_pairing_routes
from operant.caller_pairing.crypto import (
    COMMAND_PATH,
    PAIR_PATH,
    READBACK_PATH,
    CallerCryptoError,
    b64,
    canonical_bytes,
    decrypt,
    derive_key,
    device_id,
    encrypt,
    public_key_text,
    request_hash,
    sign,
    verify,
)
from operant.caller_pairing.runtime import CallerPairingError, CallerPairingRuntime
from operant.contracts.caller_pairing import (
    CallerCommand,
    CallerEncryptedReply,
    CallerPairingTicket,
    CallerPairRequest,
)
from operant.domain.security import (
    Capability,
    PolicyBundle,
    PolicyDecision,
    PolicyLayer,
    PolicyRule,
)
from operant.local_caller import canonical_proof
from operant.persistence.caller_pairing_schema import upgrade
from operant.persistence.phase45 import SQLitePhase45Repository
from operant.persistence.sqlite import MigrationError, SQLiteStore

NOW = 1_700_000_000


class FakeEffects:
    def __init__(self) -> None:
        self.add_calls = 0
        self.remove_calls = 0
        self.ask = False

    def list_sources(self) -> SimpleNamespace:
        return SimpleNamespace(model_dump=lambda **_kwargs: {"items": []})

    def add_source(self, path: str, key: str) -> SimpleNamespace:
        self.add_calls += 1
        if self.ask:
            raise SkillSourceOperationError(
                409, {"code": "approval_required", "approval_id": "approval_test"}
            )
        return SimpleNamespace(
            model_dump=lambda **_kwargs: {
                "path": path,
                "key_hash": hashlib.sha256(key.encode()).hexdigest(),
            }
        )

    def remove_source(self, root_ref: str, key: str) -> SimpleNamespace:
        self.remove_calls += 1
        return SimpleNamespace(model_dump=lambda **_kwargs: {"root_ref": root_ref})


def _runtime(tmp_path: Path) -> tuple[CallerPairingRuntime, FakeEffects]:
    store = SQLiteStore(tmp_path / "isolated.sqlite3")
    with store._connect() as db:
        upgrade(db)
    effects = FakeEffects()
    runtime = CallerPairingRuntime(
        store, effects_for_device=lambda _device: effects, clock=lambda: NOW
    )
    return runtime, effects


class Client:
    def __init__(self, *, now: int = NOW) -> None:
        self.signing = ec.generate_private_key(ec.SECP256R1())
        self.exchange = ec.generate_private_key(ec.SECP256R1())
        self.signing_public = public_key_text(self.signing.public_key())
        self.exchange_public = public_key_text(self.exchange.public_key())
        self.device_id = device_id(self.signing_public)
        self.now = now

    def pair_request(
        self, ticket: CallerPairingTicket, *, request_id: str = "pair-1"
    ) -> CallerPairRequest:
        envelope = {
            "protocol_version": "caller-pairing.v1",
            "pair_request_id": request_id,
            "ticket_id": ticket.ticket_id,
            "core_epoch_id": ticket.core_epoch_id,
            "device_id": self.device_id,
            "device_signing_public_key": self.signing_public,
            "device_exchange_public_key": self.exchange_public,
            "scope": "skill_source.manage",
            "issued_at": self.now,
            "expires_at": self.now + 60,
            "nonce": b64(b"pair-nonce12"),
        }
        key = derive_key(
            self.exchange,
            ticket.core_exchange_public_key,
            epoch_id=ticket.core_epoch_id,
            device_id_value=self.device_id,
            purpose="pair-request",
        )
        envelope["ciphertext"] = encrypt(
            {"one_time_code": ticket.one_time_code, "display_name": "Local test"},
            envelope,
            key,
            path=PAIR_PATH,
        )
        envelope["signature"] = sign(envelope, self.signing, path=PAIR_PATH)
        return CallerPairRequest.model_validate(envelope)

    def pair(self, runtime: CallerPairingRuntime, *, request_id: str = "pair-1") -> tuple[Any, Any]:
        ticket = runtime.challenge(base_url="http://127.0.0.1:18778", ttl_seconds=120)
        body = self.pair_request(ticket, request_id=request_id)
        reply = runtime.pair(body)
        verify(reply.model_dump(), ticket.core_signing_public_key, path=PAIR_PATH)
        reply_key = derive_key(
            self.exchange,
            ticket.core_exchange_public_key,
            epoch_id=ticket.core_epoch_id,
            device_id_value=self.device_id,
            purpose="pair-reply",
        )
        clear = decrypt(reply.model_dump(), reply_key, path=PAIR_PATH)
        assert clear == {
            "device_id": self.device_id,
            "scope": "skill_source.manage",
            "core_epoch_id": ticket.core_epoch_id,
            "expires_at": self.now + 8 * 3600,
        }
        return body, reply

    def command(
        self,
        runtime: CallerPairingRuntime,
        operation: str,
        payload: dict[str, Any],
        *,
        request_id: str,
        nonce: bytes,
        path: str = COMMAND_PATH,
    ) -> CallerCommand:
        envelope = {
            "protocol_version": "caller-pairing.v1",
            "core_epoch_id": runtime.keys.epoch_id,
            "device_id": self.device_id,
            "request_id": request_id,
            "operation": operation,
            "issued_at": self.now,
            "expires_at": self.now + 60,
            "nonce": b64(nonce),
        }
        key = derive_key(
            self.exchange,
            runtime.keys.exchange_public,
            epoch_id=runtime.keys.epoch_id,
            device_id_value=self.device_id,
            purpose="command-request",
        )
        envelope["ciphertext"] = encrypt(payload, envelope, key, path=path)
        envelope["signature"] = sign(envelope, self.signing, path=path)
        return CallerCommand.model_validate(envelope)

    def read_reply(
        self, runtime: CallerPairingRuntime, reply: Any, *, path: str = COMMAND_PATH
    ) -> dict[str, Any]:
        verify(reply.model_dump(), runtime.keys.signing_public, path=path)
        key = derive_key(
            self.exchange,
            runtime.keys.exchange_public,
            epoch_id=runtime.keys.epoch_id,
            device_id_value=self.device_id,
            purpose="command-reply",
        )
        return decrypt(reply.model_dump(), key, path=path)


def test_pair_signature_aead_and_exact_recovery(tmp_path: Path) -> None:
    runtime, _effects = _runtime(tmp_path)
    client = Client()
    body, reply = client.pair(runtime)
    assert runtime.pair(body) == reply
    changed = body.model_copy(update={"pair_request_id": "different"})
    with pytest.raises(CallerPairingError):
        runtime.pair(changed)
    assert runtime.devices().items[0].state == "active"


def test_command_idempotency_nonce_and_readback(tmp_path: Path) -> None:
    runtime, effects = _runtime(tmp_path)
    client = Client()
    client.pair(runtime)
    body = client.command(
        runtime, "add", {"path": "/tmp/skills"}, request_id="add-1", nonce=b"first-nonce1"
    )
    first = client.read_reply(runtime, runtime.command(body))["receipt"]
    assert first["state"] == "completed"
    assert effects.add_calls == 1
    with pytest.raises(CallerPairingError) as replay:
        runtime.command(body)
    assert replay.value.code == "nonce_replayed"
    retry = client.command(
        runtime, "add", {"path": "/tmp/skills"}, request_id="add-1", nonce=b"other-nonce1"
    )
    assert client.read_reply(runtime, runtime.command(retry))["receipt"] == first
    assert effects.add_calls == 1
    mismatch = client.command(
        runtime, "add", {"path": "/tmp/other"}, request_id="add-1", nonce=b"third-nonce1"
    )
    with pytest.raises(CallerPairingError) as conflict:
        runtime.command(mismatch)
    assert conflict.value.code == "request_identity_changed"
    readback = client.command(
        runtime,
        "readback",
        {"original_request_id": "add-1"},
        request_id="lookup-1",
        nonce=b"read-nonce12",
        path=READBACK_PATH,
    )
    reply = runtime.command(readback, path=READBACK_PATH)
    assert client.read_reply(runtime, reply, path=READBACK_PATH)["receipt"] == first


def test_scope_epoch_and_unknown_effect_do_not_replay(tmp_path: Path) -> None:
    runtime, effects = _runtime(tmp_path)
    client = Client()
    client.pair(runtime)
    effects.add_source = lambda _path, _key: (_ for _ in ()).throw(
        RuntimeError("after-effect unknown")
    )  # type: ignore[method-assign]
    body = client.command(
        runtime, "add", {"path": "/tmp/skills"}, request_id="unknown", nonce=b"unknownnonce"
    )
    receipt = client.read_reply(runtime, runtime.command(body))["receipt"]
    assert receipt["state"] == "unconfirmed"
    assert receipt["error_code"] == "manual_reconcile_required"
    fresh = CallerPairingRuntime(
        runtime.repo.store, effects_for_device=lambda _device: effects, clock=lambda: NOW
    )
    stale = client.command(runtime, "list", {}, request_id="stale", nonce=b"stale-nonce1")
    with pytest.raises(CallerPairingError) as changed:
        fresh.command(stale)
    assert changed.value.code == "epoch_changed"
    client.pair(fresh, request_id="pair-new")
    readback = client.command(
        fresh,
        "readback",
        {"original_request_id": "unknown"},
        request_id="lookup-new",
        nonce=b"newreadnonce",
        path=READBACK_PATH,
    )
    assert (
        client.read_reply(fresh, fresh.command(readback, path=READBACK_PATH), path=READBACK_PATH)[
            "receipt"
        ]["state"]
        == "unconfirmed"
    )


def test_crypto_vectors_are_canonical_and_bound_to_route() -> None:
    envelope = {
        "device_id": "caller_" + "a" * 32,
        "nonce": b64(b"123456789012"),
        "operation": "list",
    }
    assert canonical_bytes({"z": 1, "a": "中"}) == b'{"a":"\xe4\xb8\xad","z":1}'
    assert request_hash(envelope, path=COMMAND_PATH) != request_hash(envelope, path=READBACK_PATH)
    assert len(request_hash(envelope, path=COMMAND_PATH)) == 64
    key = b"k" * 32
    envelope["protocol_version"] = "caller-pairing.v1"
    envelope["ciphertext"] = encrypt({}, envelope, key, path=COMMAND_PATH)
    assert decrypt(envelope, key, path=COMMAND_PATH) == {}
    with pytest.raises(CallerCryptoError):
        decrypt(envelope, key, path=READBACK_PATH)


def test_webcrypto_pair_vector_is_stable() -> None:
    core = ec.derive_private_key(1, ec.SECP256R1())
    client = ec.derive_private_key(2, ec.SECP256R1())
    core_public = public_key_text(core.public_key())
    client_public = public_key_text(client.public_key())
    assert core_public == (
        "BGsX0fLhLEJH-Lzm5WOkQPJ3A32BLeszoPShOUXYmMKWT-NC4v4af5uO5-tKfA-eFivOM1drMV7Oy7ZAaDe_UfU"
    )
    assert client_public == (
        "BHzyexiNA09-ilI4AwS1GsPAiWnid_IbNaYLSPxHZpl4B3dVENuO0EApPZrGn3Qw27p9reY86YIpngS3nSJ4c9E"
    )
    epoch = "core_" + "1" * 32
    device = "caller_" + "2" * 32
    key = derive_key(
        client,
        core_public,
        epoch_id=epoch,
        device_id_value=device,
        purpose="pair-request",
    )
    assert key.hex() == "43cfb2be42dffaf5d1e051cec27444b6c4b4fdeb829e97df58f4baa06c6e9ad5"
    envelope = {
        "protocol_version": "caller-pairing.v1",
        "pair_request_id": "vector-1",
        "ticket_id": "ticket_" + "3" * 32,
        "core_epoch_id": epoch,
        "device_id": device,
        "device_signing_public_key": client_public,
        "device_exchange_public_key": client_public,
        "scope": "skill_source.manage",
        "issued_at": NOW,
        "expires_at": NOW + 60,
        "nonce": b64(bytes.fromhex("000102030405060708090a0b")),
    }
    envelope["ciphertext"] = encrypt(
        {"one_time_code": "A" * 43, "display_name": "Vector"},
        envelope,
        key,
        path=PAIR_PATH,
    )
    assert envelope["ciphertext"] == (
        "vla3a7eeZRMartdoWsWLEs8Phn3sXEijOIQTPPOVuRpncLgeaF00bIdO_SvQA58rylYi-rY4fER_fuEWR-"
        "FtuG2WxB0qvEDrIlL3ShPg3RirdTgIaWohTprBTvfwmJUbV9cdKT-O4A"
    )
    assert request_hash(envelope, path=PAIR_PATH) == (
        "f663f198c48ec1da3f2648f5f565a94c64d1d9c95c05aea7b9c4348cd49ee912"
    )


def test_ask_requires_explicit_bound_continuation(tmp_path: Path) -> None:
    runtime, effects = _runtime(tmp_path)
    client = Client()
    client.pair(runtime)
    action_hash = "a" * 64
    approval_status = {"status": "pending"}
    runtime.phase_repo = SimpleNamespace(  # type: ignore[assignment]
        get_phase45_approval=lambda _approval_id: {
            "action_hash": action_hash,
            "status": approval_status["status"],
        }
    )
    effects.ask = True
    original = client.command(
        runtime,
        "add",
        {"path": "/tmp/skills"},
        request_id="requires-ask",
        nonce=b"ask-nonce-12",
    )
    first = client.read_reply(runtime, runtime.command(original))["receipt"]
    assert first["state"] == "awaiting_approval"
    assert first["approval_id"] == "approval_test"
    assert first["action_hash"] == action_hash
    assert effects.add_calls == 1
    # An ordinary retry only reads the durable receipt and never re-runs effects.
    retry = client.command(
        runtime,
        "add",
        {"path": "/tmp/skills"},
        request_id="requires-ask",
        nonce=b"ask-nonce-13",
    )
    assert client.read_reply(runtime, runtime.command(retry))["receipt"] == first
    assert effects.add_calls == 1
    continue_payload = {
        "original_request_id": "requires-ask",
        "payload_hash": first["payload_hash"],
        "approval_id": "approval_test",
        "action_hash": action_hash,
    }
    wrong = client.command(
        runtime,
        "continue",
        {**continue_payload, "action_hash": "b" * 64},
        request_id="continue-wrong",
        nonce=b"ask-nonce-14",
    )
    with pytest.raises(CallerPairingError) as mismatch:
        runtime.command(wrong)
    assert mismatch.value.code == "approval_binding_changed"
    pending = client.command(
        runtime,
        "continue",
        continue_payload,
        request_id="continue-pending",
        nonce=b"ask-nonce-15",
    )
    with pytest.raises(CallerPairingError) as not_granted:
        runtime.command(pending)
    assert not_granted.value.code == "approval_not_granted"
    approval_status["status"] = "approved"
    effects.ask = False
    approved = client.command(
        runtime,
        "continue",
        continue_payload,
        request_id="continue-approved",
        nonce=b"ask-nonce-16",
    )
    completed = client.read_reply(runtime, runtime.command(approved))["receipt"]
    assert completed["state"] == "completed"
    assert effects.add_calls == 2
    again = client.command(
        runtime,
        "continue",
        continue_payload,
        request_id="continue-again",
        nonce=b"ask-nonce-17",
    )
    with pytest.raises(CallerPairingError):
        runtime.command(again)
    assert effects.add_calls == 2


def test_revoke_and_authentication_fail_before_effect(tmp_path: Path) -> None:
    runtime, effects = _runtime(tmp_path)
    client = Client()
    client.pair(runtime)
    valid = client.command(
        runtime,
        "add",
        {"path": "/tmp/skills"},
        request_id="authorized",
        nonce=b"validnonce12",
    )
    tampered = valid.model_copy(update={"ciphertext": valid.ciphertext[:-1] + "A"})
    with pytest.raises(CallerPairingError):
        runtime.command(tampered)
    assert effects.add_calls == 0
    assert runtime.repo.get_receipt(client.device_id, "authorized") is None
    assert runtime.revoke(client.device_id)
    with pytest.raises(CallerPairingError) as revoked:
        runtime.command(valid)
    assert revoked.value.code == "device_unavailable"
    assert effects.add_calls == 0


def test_encrypted_payload_cannot_smuggle_extra_permission(tmp_path: Path) -> None:
    runtime, effects = _runtime(tmp_path)
    client = Client()
    client.pair(runtime)
    body = client.command(
        runtime,
        "add",
        {"path": "/tmp/skills", "secret_refs": ["MODEL_KEY"]},
        request_id="smuggle",
        nonce=b"smug-nonce12",
    )
    with pytest.raises(CallerPairingError) as rejected:
        runtime.command(body)
    assert rejected.value.code == "command_rejected"
    assert runtime.repo.get_receipt(client.device_id, "smuggle") is None
    assert effects.add_calls == 0


def test_revoke_waits_for_inflight_effect_then_blocks_later_write(tmp_path: Path) -> None:
    runtime, effects = _runtime(tmp_path)
    client = Client()
    client.pair(runtime)
    entered = threading.Event()
    release = threading.Event()
    revoke_started = threading.Event()

    def held_effect(path: str, _key: str) -> SimpleNamespace:
        effects.add_calls += 1
        entered.set()
        assert release.wait(2)
        return SimpleNamespace(model_dump=lambda **_kwargs: {"path": path})

    effects.add_source = held_effect  # type: ignore[method-assign]
    first = client.command(
        runtime,
        "add",
        {"path": "/tmp/skills"},
        request_id="inflight",
        nonce=b"inflightnc12",
    )

    def revoke() -> bool:
        revoke_started.set()
        return runtime.revoke(client.device_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(runtime.command, first)
        assert entered.wait(2)
        revoke_future = pool.submit(revoke)
        assert revoke_started.wait(2)
        assert not revoke_future.done()
        release.set()
        assert (
            client.read_reply(runtime, first_future.result(timeout=2))["receipt"]["state"]
            == "completed"
        )
        assert revoke_future.result(timeout=2)
    later = client.command(
        runtime,
        "add",
        {"path": "/tmp/skills"},
        request_id="after-revoke",
        nonce=b"afternonce12",
    )
    with pytest.raises(CallerPairingError) as denied:
        runtime.command(later)
    assert denied.value.code == "device_unavailable"
    assert effects.add_calls == 1


def test_native_endpoints_require_proof_and_challenge_confirmation(tmp_path: Path) -> None:
    runtime, _effects = _runtime(tmp_path)
    app = FastAPI()
    app.state.skill_source_effects = SimpleNamespace()
    app.state.phase45_action_gateway = SimpleNamespace()
    app.state.setup_allowed_origins = frozenset()
    installed = install_caller_pairing_routes(
        app,
        SimpleNamespace(store=runtime.repo.store),  # type: ignore[arg-type]
        local_authorizer=lambda request: request.headers.get("x-native-proof") == "yes",
        confirmation_authorizer=lambda request: request.headers.get("x-confirm") == "yes",
    )
    client = TestClient(app, base_url="http://127.0.0.1:18778")
    assert client.post("/v1/local-callers/challenges", json={"ttl_seconds": 120}).status_code == 403
    assert (
        client.post(
            "/v1/local-callers/challenges",
            json={"ttl_seconds": 120},
            headers={"x-native-proof": "yes"},
        ).status_code
        == 403
    )
    issued = client.post(
        "/v1/local-callers/challenges",
        json={"ttl_seconds": 120},
        headers={"x-native-proof": "yes", "x-confirm": "yes"},
    )
    assert issued.status_code == 200
    assert issued.headers["cache-control"] == "no-store"
    assert issued.json()["base_url"] == "http://127.0.0.1:18778"
    assert (
        installed.repo.get_challenge(issued.json()["ticket_id"])["code_hash"]
        != issued.json()["one_time_code"]
    )
    assert client.get("/v1/local-callers/devices").status_code == 403
    assert (
        client.get("/v1/local-callers/devices", headers={"x-native-proof": "yes"}).status_code
        == 200
    )
    assert (
        client.get(
            "/v1/local-callers/requests/result?device_id=caller_" + "a" * 32 + "&request_id=a%2Fb",
            headers={"x-native-proof": "yes"},
        ).status_code
        == 404
    )


def test_desktop_outer_proof_precedes_source_journal_and_native_ticket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # create_app reads .env relative to cwd; a fresh empty directory ensures
    # this integration never inspects a real credential file.
    monkeypatch.chdir(tmp_path)
    from operant.server import build_server_config

    secret = bytes(range(32))
    config = build_server_config(
        host="127.0.0.1",
        port=8765,
        ssl_certfile=None,
        ssl_keyfile=None,
        desktop=True,
        local_caller_secret=secret,
        environment={"OPERANT_DB_PATH": str(tmp_path / "core.sqlite3")},
    )
    app = config.app
    with TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 12000)) as client:
        for method, path, body in (
            ("GET", "/v1/setup/skill-sources", None),
            ("POST", "/v1/setup/skill-sources", {"path": str(tmp_path)}),
            ("DELETE", "/v1/setup/skill-sources/user-fixture", None),
        ):
            response = client.request(
                method, path, json=body, headers={"Idempotency-Key": "blocked"}
            )
            assert response.status_code == 403
        with app.state.skill_source_effects.store._connect() as db:
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM command_executions WHERE idempotency_key='blocked'"
                ).fetchone()[0]
                == 0
            )
            assert (
                db.execute("SELECT COUNT(*) FROM ux_commands WHERE key='blocked'").fetchone()[0]
                == 0
            )
        path = "/v1/local-callers/challenges"
        body = b'{"ttl_seconds":120}'

        def headers(sequence: int, *, confirm: bool) -> dict[str, str]:
            timestamp = str(int(time.time()))
            nonce = f"{sequence:032x}"
            digest = hashlib.sha256(body).hexdigest()
            proof = canonical_proof(
                method=b"POST",
                target=path.encode(),
                timestamp=timestamp.encode(),
                nonce=nonce.encode(),
                idempotency_key=b"ticket",
                body_digest=digest.encode(),
            )
            signed = {
                "X-Operant-Caller-Protocol": "local-caller.v1",
                "X-Operant-Caller-Timestamp": timestamp,
                "X-Operant-Caller-Nonce": nonce,
                "X-Operant-Caller-Signature": hmac.new(secret, proof, hashlib.sha256).hexdigest(),
                "Idempotency-Key": "ticket",
                "Content-Type": "application/json",
            }
            if confirm:
                message = b"\n".join(
                    (
                        b"local-caller.confirm.v1",
                        b"POST",
                        path.encode(),
                        nonce.encode(),
                        digest.encode(),
                    )
                )
                signed["X-Operant-Native-Confirmation"] = hmac.new(
                    secret, message, hashlib.sha256
                ).hexdigest()
            return signed

        assert client.post(path, content=body, headers=headers(1, confirm=False)).status_code == 403
        with app.state.skill_source_effects.store._connect() as db:
            assert db.execute("SELECT COUNT(*) FROM caller_pairing_challenges").fetchone()[0] == 0
        issued = client.post(path, content=body, headers=headers(2, confirm=True))
        assert issued.status_code == 200
        assert issued.headers["cache-control"] == "no-store"
        assert issued.json()["scope"] == "skill_source.manage"
        runtime = app.state.caller_pairing_runtime
        paired = Client(now=int(time.time()))
        ticket = CallerPairingTicket.model_validate(issued.json())
        pair_body = paired.pair_request(ticket)
        paired_response = client.post(PAIR_PATH, json=pair_body.model_dump())
        assert paired_response.status_code == 200
        assert paired_response.headers["cache-control"] == "no-store"
        pair_reply = CallerEncryptedReply.model_validate(paired_response.json())
        verify(pair_reply.model_dump(), ticket.core_signing_public_key, path=PAIR_PATH)
        pair_key = derive_key(
            paired.exchange,
            ticket.core_exchange_public_key,
            epoch_id=ticket.core_epoch_id,
            device_id_value=paired.device_id,
            purpose="pair-reply",
        )
        assert (
            decrypt(pair_reply.model_dump(), pair_key, path=PAIR_PATH)["scope"]
            == "skill_source.manage"
        )
        with app.state.skill_source_effects.store._connect() as db:
            startup_scan_sequence = db.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM security_action_requests "
                "WHERE tool='skill_discovery'"
            ).fetchone()[0]

        list_body = paired.command(
            runtime,
            "list",
            {},
            request_id="list-native-independent",
            nonce=b"list-nonce12",
        )
        listed = client.post(COMMAND_PATH, json=list_body.model_dump())
        assert listed.status_code == 200
        list_receipt = paired.read_reply(
            runtime, CallerEncryptedReply.model_validate(listed.json())
        )["receipt"]
        assert list_receipt["state"] == "completed"
        assert list_receipt["operation"] == "list"

        skill_root = tmp_path / "paired-skills"
        skill_root.mkdir()
        (skill_root / "SKILL.md").write_text(
            "---\nname: paired-fixture\ndescription: Paired test source\n---\nUse this skill.\n",
            encoding="utf-8",
        )
        base_policy = balanced_policy_bundle()
        app.state.phase45_action_gateway.engine = PolicyEngine(
            PolicyBundle(
                bundle_id="paired-ask",
                version="paired-ask.v1",
                default_decision=base_policy.default_decision,
                rules=base_policy.rules
                + (
                    PolicyRule(
                        rule_id="paired-source-ask",
                        layer=PolicyLayer.SYSTEM,
                        decision=PolicyDecision.ASK,
                        capabilities=(Capability.WORKSPACE_WRITE,),
                        tools=("skill_source",),
                        reason="paired source needs approval",
                    ),
                ),
            )
        )
        add_body = paired.command(
            runtime,
            "add",
            {"path": str(skill_root)},
            request_id="paired-add",
            nonce=b"add-nonce-12",
        )
        asked_response = client.post(COMMAND_PATH, json=add_body.model_dump())
        assert asked_response.status_code == 200
        asked = paired.read_reply(
            runtime, CallerEncryptedReply.model_validate(asked_response.json())
        )["receipt"]
        assert asked["state"] == "awaiting_approval"
        assert isinstance(asked["approval_id"], str)
        assert isinstance(asked["action_hash"], str)
        with app.state.skill_source_effects.store._connect() as db:
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM command_executions "
                    "WHERE command_type='skill_source.change'"
                ).fetchone()[0]
                == 0
            )
        SQLitePhase45Repository(app.state.skill_source_effects.store).decide_phase45_approval(
            asked["approval_id"],
            approved=True,
            decided_by="user",
            reason_code=None,
        )
        continuation = paired.command(
            runtime,
            "continue",
            {
                "original_request_id": "paired-add",
                "payload_hash": asked["payload_hash"],
                "approval_id": asked["approval_id"],
                "action_hash": asked["action_hash"],
            },
            request_id="paired-continue",
            nonce=b"cont-nonce12",
        )
        resumed = client.post(COMMAND_PATH, json=continuation.model_dump())
        assert resumed.status_code == 200
        completed = paired.read_reply(runtime, CallerEncryptedReply.model_validate(resumed.json()))[
            "receipt"
        ]
        assert completed["state"] == "completed"
        assert completed["request_id"] == "paired-add"
        assert runtime.repo.get_receipt(paired.device_id, "paired-add")["state"] == "completed"
        with app.state.skill_source_effects.store._connect() as db:
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM command_executions "
                    "WHERE command_type='skill_source.change'"
                ).fetchone()[0]
                == 1
            )
            scan_principals = {
                row[0]
                for row in db.execute(
                    "SELECT principal FROM security_action_requests "
                    "WHERE tool='skill_discovery' AND sequence>?",
                    (startup_scan_sequence,),
                )
            }
            assert f"paired-skill-source:{paired.device_id}" in scan_principals
            assert "core:phase45" not in scan_principals

        # A durable reservation with no terminal result simulates a crash in
        # the action window. Both authenticated readback paths must say unknown.
        unknown_args = {"path": str(skill_root)}
        unknown_hash = hashlib.sha256(
            canonical_bytes({"operation": "add", "args": unknown_args})
        ).hexdigest()
        _unknown, created = runtime.repo.reserve_command(
            device_id=paired.device_id,
            epoch_id=runtime.keys.epoch_id,
            nonce=b64(b"crashnonce12"),
            envelope_hash="f" * 64,
            request_id="crash/reserved",
            operation="add",
            payload_hash=unknown_hash,
            original_args=unknown_args,
            now=runtime._now(),
            expires_at=runtime._now() + 60,
        )
        assert created
        readback_body = paired.command(
            runtime,
            "readback",
            {"original_request_id": "crash/reserved"},
            request_id="read-crash",
            nonce=b"readcrashnce",
            path=READBACK_PATH,
        )
        readback_response = client.post(READBACK_PATH, json=readback_body.model_dump())
        assert readback_response.status_code == 200
        unknown_receipt = paired.read_reply(
            runtime,
            CallerEncryptedReply.model_validate(readback_response.json()),
            path=READBACK_PATH,
        )["receipt"]
        assert unknown_receipt["state"] == "unconfirmed"
        assert unknown_receipt["error_code"] == "manual_reconcile_required"
        assert unknown_receipt["result"] is None
        assert (
            runtime.repo.get_receipt(paired.device_id, "crash/reserved")["state"] == "in_progress"
        )

        native_path = (
            "/v1/local-callers/requests/result?device_id="
            + paired.device_id
            + "&request_id=crash%2Freserved"
        )
        native_timestamp = str(int(time.time()))
        native_nonce = f"{4:032x}"
        native_proof = canonical_proof(
            method=b"GET",
            target=native_path.encode(),
            timestamp=native_timestamp.encode(),
            nonce=native_nonce.encode(),
            idempotency_key=b"",
            body_digest=hashlib.sha256(b"").hexdigest().encode(),
        )
        native_headers = {
            "X-Operant-Caller-Protocol": "local-caller.v1",
            "X-Operant-Caller-Timestamp": native_timestamp,
            "X-Operant-Caller-Nonce": native_nonce,
            "X-Operant-Caller-Signature": hmac.new(
                secret, native_proof, hashlib.sha256
            ).hexdigest(),
        }
        native_unknown = client.get(native_path, headers=native_headers)
        assert native_unknown.status_code == 200
        assert native_unknown.headers["cache-control"] == "no-store"
        assert native_unknown.json() == unknown_receipt
        # A lost command reply is recovered with a fresh nonce and identical
        # request identity; no second Gateway/effect execution follows.
        assert client.post(COMMAND_PATH, json=add_body.model_dump()).status_code == 409
        recovered_body = paired.command(
            runtime,
            "add",
            {"path": str(skill_root)},
            request_id="paired-add",
            nonce=b"retry-nonce1",
        )
        recovered = client.post(COMMAND_PATH, json=recovered_body.model_dump())
        assert recovered.status_code == 200
        assert (
            paired.read_reply(runtime, CallerEncryptedReply.model_validate(recovered.json()))[
                "receipt"
            ]
            == completed
        )
        with app.state.skill_source_effects.store._connect() as db:
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM command_executions "
                    "WHERE command_type='skill_source.change'"
                ).fetchone()[0]
                == 1
            )

        revoke_path = f"/v1/local-callers/devices/{paired.device_id}/revoke"
        revoke_nonce = f"{3:032x}"
        timestamp = str(int(time.time()))
        revoke_proof = canonical_proof(
            method=b"POST",
            target=revoke_path.encode(),
            timestamp=timestamp.encode(),
            nonce=revoke_nonce.encode(),
            idempotency_key=b"revoke",
            body_digest=hashlib.sha256(b"").hexdigest().encode(),
        )
        revoke_headers = {
            "X-Operant-Caller-Protocol": "local-caller.v1",
            "X-Operant-Caller-Timestamp": timestamp,
            "X-Operant-Caller-Nonce": revoke_nonce,
            "X-Operant-Caller-Signature": hmac.new(
                secret, revoke_proof, hashlib.sha256
            ).hexdigest(),
            "Idempotency-Key": "revoke",
        }
        revoked = client.post(revoke_path, headers=revoke_headers)
        assert revoked.status_code == 200
        assert revoked.json()["items"][0]["state"] == "revoked"
        after_revoke = paired.command(
            runtime,
            "add",
            {"path": str(skill_root)},
            request_id="after-revoke",
            nonce=b"revokednonce",
        )
        blocked = client.post(COMMAND_PATH, json=after_revoke.model_dump())
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["code"] == "device_unavailable"
        assert runtime.repo.get_receipt(paired.device_id, "after-revoke") is None


def test_v26_additive_migration_preserves_history_and_blocks_data_loss(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "migration.sqlite3")
    assert store.migrate(25) == 25
    prior_history = store.list_applied_migrations()
    assert store.migrate(26) == 26
    assert store.list_applied_migrations()[:25] == prior_history
    for version in range(1, 27):
        manifest = SQLiteStore._migration_manifest(version)
        assert (
            hashlib.sha256(manifest.encode()).hexdigest()
            == (SQLiteStore._FROZEN_MANIFEST_SHA256[version])
        )
    with store._connect() as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    runtime = CallerPairingRuntime(
        store, effects_for_device=lambda _device: FakeEffects(), clock=lambda: NOW
    )
    runtime.challenge(base_url="http://127.0.0.1:18778", ttl_seconds=120)
    with pytest.raises(MigrationError, match="caller pairing v26 contains data"):
        store.rollback(25, isolated=True)
    assert store.schema_version() == 26
    empty = SQLiteStore(tmp_path / "empty-migration.sqlite3")
    assert empty.migrate(26) == 26
    assert empty.rollback(25, isolated=True) == 25
    assert empty.migrate(26) == 26
