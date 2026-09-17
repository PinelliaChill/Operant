from __future__ import annotations

import hashlib
import secrets
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from cryptography.exceptions import InvalidSignature, InvalidTag
from pydantic import BaseModel, ConfigDict, Field

from operant.application.phase45_gateway import Phase45ActionGateway
from operant.application.security import PolicyEngine, balanced_policy_bundle
from operant.domain.remote_control import (
    EncryptedRemoteCommand,
    HostInstance,
    PairingChallenge,
    PairingTicket,
    RelayEnvelope,
    RemoteCommandReceipt,
    RemoteCommandStatus,
    RemoteDevice,
    RemoteHostOnlineState,
    RemoteScope,
    RemoteSession,
    RemoteSessionState,
    RemoteTransportMode,
    new_remote_id,
)
from operant.domain.security import (
    ActionRequest,
    Capability,
    PolicyBundle,
    PolicyDecision,
    PolicyLayer,
    PolicyRule,
    RiskLevel,
)
from operant.mcp import GatewayDecision
from operant.persistence.phase45 import SQLitePhase45Repository
from operant.persistence.remote_control import SQLiteRemoteControlRepository
from operant.persistence.security import SQLiteSecurityRepository
from operant.persistence.sqlite import ConflictError, SQLiteStore
from operant.remote_control.crypto import RemoteCrypto, RemoteKeyStore

MAX_PAIRING_TTL_SECONDS = 300
MAX_SESSION_TTL_SECONDS = 3600
MAX_COMMAND_TTL_SECONDS = 300
MAX_CLOCK_SKEW_SECONDS = 60
MAX_RELAY_TTL_SECONDS = 120
MAX_RELAY_CIPHERTEXT_CHARS = 350_000
DEFAULT_COMMAND_EXECUTION_LEASE_SECONDS = 30


def _now() -> datetime:
    return datetime.now(timezone.utc)


class RemoteControlError(Exception):
    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class RemoteActionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str = Field(min_length=1, max_length=200)
    operation: str = Field(min_length=1, max_length=200)
    target_id: str = Field(min_length=1, max_length=500)
    arguments: dict[str, Any] = Field(default_factory=dict)
    capabilities: tuple[Capability, ...] = Field(min_length=1, max_length=16)
    workspace: str | None = Field(default=None, max_length=4096)
    secret_refs: tuple[str, ...] = Field(default=(), max_length=16)


RemoteExecutor = Callable[[RemoteActionPayload, ActionRequest], str | None]
RemoteCapabilityMap = Mapping[tuple[str, str], tuple[Capability, ...]]


DEFAULT_REMOTE_OPERATION_CAPABILITIES: dict[tuple[str, str], tuple[Capability, ...]] = {
    # MP-5.4 memory operations cross the existing Remote Control envelope.
    # The Memory Core still performs project/session/source checks; this map
    # only binds the transport operation to its single remote capability.
    ("memory", "query"): (Capability.REMOTE_CONTROL_OBSERVE,),
    ("memory", "command"): (Capability.REMOTE_CONTROL_COMMAND,),
    ("remote_projection", "read"): (
        Capability.REMOTE_CONTROL_OBSERVE,
        Capability.WORKSPACE_READ,
    ),
    ("workspace", "write"): (
        Capability.REMOTE_CONTROL_COMMAND,
        Capability.WORKSPACE_WRITE,
    ),
    ("approval", "decide"): (Capability.REMOTE_CONTROL_APPROVE,),
    ("browser", "observe"): (
        Capability.REMOTE_CONTROL_OBSERVE,
        Capability.BROWSER_OBSERVE,
    ),
    ("browser", "navigate"): (
        Capability.REMOTE_CONTROL_COMMAND,
        Capability.BROWSER_NAVIGATE,
    ),
    ("browser", "submit"): (
        Capability.REMOTE_CONTROL_COMMAND,
        Capability.BROWSER_SUBMIT,
    ),
    ("computer", "observe"): (
        Capability.REMOTE_CONTROL_OBSERVE,
        Capability.COMPUTER_OBSERVE,
    ),
    ("computer", "clipboard_read"): (
        Capability.REMOTE_CONTROL_OBSERVE,
        Capability.COMPUTER_CLIPBOARD_READ,
    ),
    ("computer", "input"): (
        Capability.REMOTE_CONTROL_COMMAND,
        Capability.COMPUTER_INPUT,
    ),
    ("computer", "clipboard_write"): (
        Capability.REMOTE_CONTROL_COMMAND,
        Capability.COMPUTER_CLIPBOARD_WRITE,
    ),
    ("settings", "update"): (
        Capability.REMOTE_CONTROL_COMMAND,
        Capability.POLICY_MODIFY,
    ),
}


def remote_control_policy_engine() -> PolicyEngine:
    base = balanced_policy_bundle()
    return PolicyEngine(
        PolicyBundle(
            bundle_id="balanced-remote-control",
            version="phase5b.remote.v1",
            default_decision=base.default_decision,
            rules=base.rules
            + (
                PolicyRule(
                    rule_id="remote.control.observe",
                    layer=PolicyLayer.SESSION,
                    decision=PolicyDecision.ALLOW,
                    capabilities=(Capability.REMOTE_CONTROL_OBSERVE,),
                    reason="paired device observation is allowed within its scope",
                    risk_level=RiskLevel.LOW,
                ),
                PolicyRule(
                    rule_id="remote.control.command",
                    layer=PolicyLayer.SESSION,
                    decision=PolicyDecision.ASK,
                    capabilities=(Capability.REMOTE_CONTROL_COMMAND,),
                    reason="remote side effects require exact local approval",
                    risk_level=RiskLevel.HIGH,
                ),
                PolicyRule(
                    rule_id="remote.control.approve",
                    layer=PolicyLayer.SESSION,
                    decision=PolicyDecision.ASK,
                    capabilities=(Capability.REMOTE_CONTROL_APPROVE,),
                    reason="remote approval requires exact local policy review",
                    risk_level=RiskLevel.HIGH,
                ),
            ),
        )
    )


class RemoteControlService:
    def __init__(
        self,
        store: SQLiteStore,
        key_store: RemoteKeyStore,
        policy_engine: PolicyEngine,
        *,
        executor: RemoteExecutor | None = None,
        operation_capabilities: RemoteCapabilityMap | None = None,
        execution_owner_id: str | None = None,
        execution_lease_seconds: int = DEFAULT_COMMAND_EXECUTION_LEASE_SECONDS,
    ) -> None:
        if not 3 <= execution_lease_seconds <= MAX_COMMAND_TTL_SECONDS:
            raise ValueError("remote command execution lease must be between 3 and 300 seconds")
        self.store = store
        self.repository = SQLiteRemoteControlRepository(store)
        self.key_store = key_store
        self.policy_engine = policy_engine
        self.executor = executor or self._unsupported_executor
        source = (
            DEFAULT_REMOTE_OPERATION_CAPABILITIES
            if operation_capabilities is None
            else operation_capabilities
        )
        self.operation_capabilities = {
            key: tuple(dict.fromkeys(value)) for key, value in source.items()
        }
        self.execution_owner_id = execution_owner_id or f"remote-core:{uuid4().hex}"
        self.execution_lease_seconds = execution_lease_seconds
        self.repository.reconcile_incomplete_commands(now=_now())

    def enable_host(
        self,
        *,
        display_name: str,
        core_version: str,
        protocol_version: str,
        capabilities: Sequence[str] = (),
        host_id: str | None = None,
        enabled: bool = True,
        now: datetime | None = None,
    ) -> HostInstance:
        at = now or _now()
        if host_id is not None:
            try:
                existing = self.repository.get_host(host_id)
            except KeyError as exc:
                raise RemoteControlError(
                    "remote.host_not_found", "remote host was not found", status_code=404
                ) from exc
            host = existing.model_copy(
                update={
                    "display_name": display_name,
                    "core_version": core_version,
                    "protocol_version": protocol_version,
                    "capabilities": tuple(sorted(set(capabilities))),
                    "enabled": enabled,
                    "online_state": (
                        RemoteHostOnlineState.ONLINE if enabled else RemoteHostOnlineState.OFFLINE
                    ),
                    "last_seen_at": at if enabled else existing.last_seen_at,
                    "updated_at": at,
                }
            )
            persisted = self.repository.put_host(host)
            if not enabled:
                for session in self.repository.list_sessions(host_id):
                    if session.connection_state is not RemoteSessionState.CLOSED:
                        closed = self.repository.close_session(session.remote_session_id, now=at)
                        self.key_store.delete(closed.session_key_ref)
            return persisted
        signing_public, signing_private = RemoteCrypto.create_signing_keypair()
        exchange_public, exchange_private = RemoteCrypto.create_exchange_keypair()
        host = HostInstance(
            display_name=display_name,
            signing_public_key=signing_public,
            exchange_public_key=exchange_public,
            core_version=core_version,
            protocol_version=protocol_version,
            capabilities=tuple(sorted(set(capabilities))),
            enabled=enabled,
            online_state=(
                RemoteHostOnlineState.ONLINE if enabled else RemoteHostOnlineState.OFFLINE
            ),
            last_seen_at=at if enabled else None,
            created_at=at,
            updated_at=at,
        )
        self.key_store.put(f"host:{host.host_id}:signing", signing_private)
        self.key_store.put(f"host:{host.host_id}:exchange", exchange_private)
        return self.repository.put_host(host)

    def create_pairing_challenge(
        self,
        host_id: str,
        *,
        relay_url: str | None = None,
        ttl_seconds: int = 120,
        allowed_scopes: Sequence[RemoteScope] = (RemoteScope.OBSERVE,),
        now: datetime | None = None,
    ) -> PairingTicket:
        if not 1 <= ttl_seconds <= MAX_PAIRING_TTL_SECONDS:
            raise RemoteControlError("remote.invalid_pairing_ttl", "pairing TTL is out of range")
        at = now or _now()
        host = self._active_host(host_id)
        approved_scopes = tuple(dict.fromkeys(allowed_scopes))
        if not approved_scopes or RemoteScope.OBSERVE not in approved_scopes:
            raise RemoteControlError("remote.invalid_scope", "pairing must allow the observe scope")
        code = secrets.token_urlsafe(24)
        challenge = PairingChallenge(
            host_id=host_id,
            code_hash=hashlib.sha256(code.encode()).hexdigest(),
            allowed_scopes=approved_scopes,
            expires_at=at + timedelta(seconds=ttl_seconds),
            created_at=at,
        )
        self.repository.create_challenge(challenge)
        return PairingTicket(
            challenge_id=challenge.challenge_id,
            host_id=host_id,
            one_time_code=code,
            expires_at=challenge.expires_at,
            allowed_scopes=challenge.allowed_scopes,
            relay_url=relay_url,
            host_signing_public_key=host.signing_public_key,
            host_exchange_public_key=host.exchange_public_key,
        )

    def pair_device(
        self,
        *,
        challenge_id: str,
        one_time_code: str,
        display_name: str,
        signing_public_key: str,
        exchange_public_key: str,
        scopes: Sequence[RemoteScope] = (RemoteScope.OBSERVE,),
        now: datetime | None = None,
    ) -> RemoteDevice:
        at = now or _now()
        requested = tuple(dict.fromkeys(scopes))
        if not requested or RemoteScope.OBSERVE not in requested:
            raise RemoteControlError(
                "remote.invalid_scope", "new devices require the observe scope"
            )
        challenge = self.repository.get_challenge(challenge_id)
        if not set(requested).issubset(challenge.allowed_scopes):
            raise RemoteControlError(
                "remote.scope_not_approved",
                "requested device scopes exceed the locally approved pairing scopes",
                status_code=403,
            )
        challenge = self.repository.consume_challenge(
            challenge_id,
            code_hash=hashlib.sha256(one_time_code.encode()).hexdigest(),
            now=at,
        )
        self._active_host(challenge.host_id)
        device = RemoteDevice(
            host_id=challenge.host_id,
            display_name=display_name,
            signing_public_key=signing_public_key,
            exchange_public_key=exchange_public_key,
            scopes=requested,
            paired_at=at,
        )
        return self.repository.put_device(device)

    def revoke_device(self, device_id: str, *, now: datetime | None = None) -> RemoteDevice:
        device = self.repository.revoke_device(device_id, now=now or _now())
        for session in self.repository.list_sessions(device.host_id):
            if session.device_id == device_id:
                self.key_store.delete(session.session_key_ref)
        return device

    def create_session(
        self,
        *,
        host_id: str,
        device_id: str,
        transport_mode: RemoteTransportMode,
        protocol_version: str,
        event_cursor: int = 0,
        ttl_seconds: int = 900,
        now: datetime | None = None,
    ) -> RemoteSession:
        if not 1 <= ttl_seconds <= MAX_SESSION_TTL_SECONDS:
            raise RemoteControlError("remote.invalid_session_ttl", "session TTL is out of range")
        at = now or _now()
        host = self._active_host(host_id)
        device = self._active_device(device_id, host_id=host_id)
        if protocol_version != host.protocol_version:
            raise RemoteControlError(
                "remote.protocol_mismatch",
                "remote protocol version does not match",
                status_code=409,
            )
        session_id = new_remote_id("remote_session")
        key_ref = f"remote-session:{session_id}"
        session_key = RemoteCrypto.derive_session_key(
            self.key_store.get(f"host:{host_id}:exchange"),
            device.exchange_public_key,
            session_id=session_id,
            host_id=host_id,
            device_id=device_id,
        )
        self.key_store.put(key_ref, session_key)
        session = RemoteSession(
            remote_session_id=session_id,
            host_id=host_id,
            device_id=device_id,
            transport_mode=transport_mode,
            protocol_version=protocol_version,
            event_cursor=event_cursor,
            connection_state=RemoteSessionState.CONNECTED,
            session_key_ref=key_ref,
            expires_at=at + timedelta(seconds=ttl_seconds),
            connected_at=at,
            created_at=at,
        )
        return self.repository.put_session(session)

    def close_session(self, session_id: str, *, now: datetime | None = None) -> RemoteSession:
        session = self.repository.close_session(session_id, now=now or _now())
        self.key_store.delete(session.session_key_ref)
        return session

    def submit_command(
        self, command: EncryptedRemoteCommand, *, now: datetime | None = None
    ) -> RemoteCommandReceipt:
        at = now or _now()
        self.repository.reconcile_incomplete_commands(now=at)
        self._validate_command_time(command, at)
        host = self._active_host(command.host_id)
        device = self._active_device(command.device_id, host_id=command.host_id)
        session = self._active_session(command.remote_session_id, at=at)
        if session.device_id != device.device_id or session.host_id != host.host_id:
            raise RemoteControlError(
                "remote.command_binding_invalid", "remote command binding is invalid"
            )
        if command.protocol_version != session.protocol_version:
            raise RemoteControlError(
                "remote.protocol_mismatch", "remote protocol version does not match"
            )
        try:
            RemoteCrypto.verify_command(command, device.signing_public_key)
            payload_data = RemoteCrypto.decrypt_command(
                command, self.key_store.get(session.session_key_ref)
            )
        except (InvalidSignature, InvalidTag, ValueError, KeyError) as exc:
            raise RemoteControlError(
                "remote.command_authentication_failed",
                "remote command authentication failed",
                status_code=403,
            ) from exc
        payload = RemoteActionPayload.model_validate(payload_data)
        self._require_scope(device, payload, self.operation_capabilities)
        gateway = Phase45ActionGateway(
            SQLiteSecurityRepository(self.store),
            SQLitePhase45Repository(self.store),
            self.policy_engine,
            principal=f"remote-device:{device.device_id}",
        )
        action, guarded, _evaluation = gateway.guard(
            tool=payload.tool,
            operation=payload.operation,
            target_id=payload.target_id,
            arguments=payload.arguments,
            capabilities=payload.capabilities,
            idempotency_key=command.idempotency_key,
            secret_refs=payload.secret_refs,
            workspace=payload.workspace,
            network_profile=(
                "egress" if Capability.NETWORK_EGRESS in payload.capabilities else "none"
            ),
        )
        payload_hash = hashlib.sha256(command.ciphertext.encode("ascii")).hexdigest()
        receipt, created = self.repository.reserve_command(
            command_id=command.command_id,
            idempotency_key=command.idempotency_key,
            action_hash=action.action_hash,
            host_id=command.host_id,
            device_id=command.device_id,
            session_id=command.remote_session_id,
            payload_hash=payload_hash,
            nonce=command.nonce,
            signature=command.signature,
            execution_owner_id=self.execution_owner_id,
            execution_lease_expires_at=at + timedelta(seconds=self.execution_lease_seconds),
            now=at,
        )
        if not created and receipt.status is not RemoteCommandStatus.REJECTED:
            return receipt
        if not created and receipt.error_code != "remote.approval_required":
            return receipt
        if guarded.decision is not GatewayDecision.ALLOW or guarded.lease is None:
            if not created:
                return receipt
            code = (
                "remote.approval_required"
                if guarded.decision is GatewayDecision.ASK
                else "remote.policy_denied"
            )
            return self.repository.set_command_status(
                command.command_id,
                RemoteCommandStatus.REJECTED,
                expected_status=RemoteCommandStatus.RECEIVED,
                execution_owner_id=self.execution_owner_id,
                now=at,
                result_ref=guarded.approval_id,
                error_code=code,
                host_ack=True,
            )
        gateway.consume(guarded.lease, action)
        self.repository.claim_command_execution(
            command.command_id,
            execution_owner_id=self.execution_owner_id,
            execution_lease_expires_at=at + timedelta(seconds=self.execution_lease_seconds),
            now=at,
        )
        with self._command_execution_heartbeat(command.command_id):
            try:
                result_ref = self.executor(payload, action)
            except RemoteControlError as exc:
                return self._finish_owned_command(
                    command.command_id,
                    RemoteCommandStatus.REJECTED,
                    error_code=exc.code,
                )
            except Exception:
                status = (
                    RemoteCommandStatus.REJECTED
                    if action.idempotency_level.value == "read_only"
                    else RemoteCommandStatus.OUTCOME_UNKNOWN
                )
                return self._finish_owned_command(
                    command.command_id,
                    status,
                    error_code=(
                        "remote.execution_failed"
                        if status is RemoteCommandStatus.REJECTED
                        else "remote.outcome_unknown"
                    ),
                )
        return self._finish_owned_command(
            command.command_id,
            RemoteCommandStatus.COMPLETED,
            result_ref=result_ref,
        )

    def _finish_owned_command(
        self,
        command_id: str,
        status: RemoteCommandStatus,
        *,
        result_ref: str | None = None,
        error_code: str | None = None,
    ) -> RemoteCommandReceipt:
        try:
            return self.repository.set_command_status(
                command_id,
                status,
                expected_status=RemoteCommandStatus.ACCEPTED,
                execution_owner_id=self.execution_owner_id,
                now=_now(),
                result_ref=result_ref,
                error_code=error_code,
            )
        except ConflictError:
            # An expired owner may have been conservatively closed and reconciled.
            # The old executor must never overwrite that newer authoritative state.
            return self.repository.get_command(command_id)

    @contextmanager
    def _command_execution_heartbeat(self, command_id: str) -> Iterator[None]:
        stop = threading.Event()

        def renew() -> None:
            interval = max(1.0, self.execution_lease_seconds / 3)
            while not stop.wait(interval):
                at = _now()
                try:
                    self.repository.renew_command_execution_lease(
                        command_id,
                        execution_owner_id=self.execution_owner_id,
                        execution_lease_expires_at=at
                        + timedelta(seconds=self.execution_lease_seconds),
                        now=at,
                    )
                except ConflictError:
                    return

        thread = threading.Thread(
            target=renew,
            name=f"operant-remote-command-{command_id[:32]}",
            daemon=True,
        )
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=1.0)

    def reconcile_command(
        self,
        command_id: str,
        *,
        status: RemoteCommandStatus,
        result_ref: str | None = None,
        error_code: str | None = None,
        now: datetime | None = None,
    ) -> RemoteCommandReceipt:
        if status not in {RemoteCommandStatus.COMPLETED, RemoteCommandStatus.REJECTED}:
            raise RemoteControlError(
                "remote.invalid_reconciliation",
                "manual reconciliation requires a known terminal outcome",
            )
        try:
            return self.repository.reconcile_command(
                command_id,
                status=status,
                result_ref=result_ref,
                error_code=error_code,
                now=now or _now(),
            )
        except ConflictError as exc:
            raise RemoteControlError(
                "remote.reconciliation_conflict",
                "only outcome-unknown commands can be reconciled",
                status_code=409,
            ) from exc

    def list_events(
        self,
        host_id: str,
        *,
        session_id: str | None = None,
        after_cursor: int = 0,
        limit: int = 100,
        device_id: str | None = None,
    ) -> list[dict[str, Any]]:
        self.repository.get_host(host_id)
        if session_id is not None:
            session = self.repository.get_session(session_id)
            if session.host_id != host_id:
                raise RemoteControlError(
                    "remote.session_binding_invalid",
                    "remote event cursor session belongs to another host",
                    status_code=409,
                )
            if device_id is not None and session.device_id != device_id:
                raise RemoteControlError(
                    "remote.session_binding_invalid",
                    "remote event cursor session belongs to another device",
                    status_code=409,
                )
        events = self.repository.list_command_events(
            host_id,
            after_cursor=after_cursor,
            limit=limit,
            session_id=session_id,
            device_id=device_id,
        )
        if session_id is not None and events:
            self.repository.advance_cursor(
                session_id, max(int(event["cursor"]) for event in events)
            )
        return events

    def validate_gateway_binding(
        self,
        host_id: str,
        device_id: str,
        session_id: str,
        protocol_version: str,
    ) -> None:
        host = self._active_host(host_id)
        device = self._active_device(device_id, host_id=host_id)
        session = self._active_session(session_id, at=_now())
        if (
            session.host_id != host.host_id
            or session.device_id != device.device_id
            or session.protocol_version != protocol_version
            or host.protocol_version != protocol_version
        ):
            raise RemoteControlError(
                "remote.session_binding_invalid",
                "remote gateway binding is invalid",
                status_code=403,
            )

    def _active_host(self, host_id: str) -> HostInstance:
        try:
            host = self.repository.get_host(host_id)
        except KeyError as exc:
            raise RemoteControlError(
                "remote.host_not_found", "remote host was not found", status_code=404
            ) from exc
        if not host.enabled or host.online_state is RemoteHostOnlineState.OFFLINE:
            raise RemoteControlError(
                "remote.host_offline", "remote host is disabled or offline", status_code=409
            )
        return host

    def _active_device(self, device_id: str, *, host_id: str) -> RemoteDevice:
        try:
            device = self.repository.get_device(device_id)
        except KeyError as exc:
            raise RemoteControlError(
                "remote.device_not_found", "remote device was not found", status_code=404
            ) from exc
        if device.host_id != host_id or device.revoked_at is not None:
            raise RemoteControlError(
                "remote.device_revoked", "remote device is revoked", status_code=403
            )
        return device

    def _active_session(self, session_id: str, *, at: datetime) -> RemoteSession:
        try:
            session = self.repository.get_session(session_id)
        except KeyError as exc:
            raise RemoteControlError(
                "remote.session_not_found", "remote session was not found", status_code=404
            ) from exc
        if session.connection_state is not RemoteSessionState.CONNECTED or session.expires_at <= at:
            raise RemoteControlError(
                "remote.session_inactive", "remote session is not active", status_code=409
            )
        return session

    @staticmethod
    def _validate_command_time(command: EncryptedRemoteCommand, at: datetime) -> None:
        if command.issued_at > at + timedelta(seconds=MAX_CLOCK_SKEW_SECONDS):
            raise RemoteControlError(
                "remote.command_from_future", "remote command issue time is invalid"
            )
        if command.expires_at <= at:
            raise RemoteControlError("remote.command_expired", "remote command has expired")
        if command.expires_at - command.issued_at > timedelta(seconds=MAX_COMMAND_TTL_SECONDS):
            raise RemoteControlError(
                "remote.command_ttl_exceeded", "remote command TTL is too long"
            )

    @staticmethod
    def _require_scope(
        device: RemoteDevice,
        payload: RemoteActionPayload,
        operation_capabilities: RemoteCapabilityMap,
    ) -> None:
        scopes = set(device.scopes)
        capabilities = set(payload.capabilities)
        expected = operation_capabilities.get((payload.tool, payload.operation))
        if expected is None:
            raise RemoteControlError(
                "remote.operation_not_registered",
                "remote operation has no trusted capability registration",
                status_code=403,
            )
        if capabilities != set(expected):
            raise RemoteControlError(
                "remote.capability_mismatch",
                "remote capabilities do not match the trusted operation registration",
                status_code=403,
            )
        remote_markers = capabilities.intersection(
            {
                Capability.REMOTE_CONTROL_OBSERVE,
                Capability.REMOTE_CONTROL_COMMAND,
                Capability.REMOTE_CONTROL_APPROVE,
            }
        )
        if len(remote_markers) != 1:
            raise RemoteControlError(
                "remote.capability_mismatch",
                "remote operation must have exactly one trusted remote capability",
                status_code=403,
            )
        if Capability.REMOTE_CONTROL_OBSERVE in capabilities and capabilities.intersection(
            {
                Capability.WORKSPACE_WRITE,
                Capability.WORKSPACE_DELETE,
                Capability.PROCESS_EXEC,
                Capability.PROCESS_EXEC_NO_NETWORK,
                Capability.NETWORK_EGRESS,
                Capability.SECRET_USE,
                Capability.GIT_COMMIT,
                Capability.GIT_PUSH,
                Capability.BROWSER_NAVIGATE,
                Capability.BROWSER_SUBMIT,
                Capability.COMPUTER_INPUT,
                Capability.COMPUTER_CLIPBOARD_WRITE,
                Capability.EXTERNAL_MESSAGE_SEND,
                Capability.PRODUCTION_MUTATE,
                Capability.POLICY_MODIFY,
            }
        ):
            raise RemoteControlError(
                "remote.observe_side_effect_mixed",
                "observe scope cannot be mixed with side-effect capabilities",
                status_code=403,
            )
        if Capability.REMOTE_CONTROL_OBSERVE in capabilities and RemoteScope.OBSERVE not in scopes:
            raise RemoteControlError(
                "remote.scope_denied", "device lacks observe scope", status_code=403
            )
        if Capability.REMOTE_CONTROL_COMMAND in capabilities and RemoteScope.COMMAND not in scopes:
            raise RemoteControlError(
                "remote.scope_denied", "device lacks command scope", status_code=403
            )
        if Capability.REMOTE_CONTROL_APPROVE in capabilities and RemoteScope.APPROVE not in scopes:
            raise RemoteControlError(
                "remote.scope_denied", "device lacks approval scope", status_code=403
            )
        if (
            capabilities.intersection(
                {Capability.BROWSER_OBSERVE, Capability.BROWSER_NAVIGATE, Capability.BROWSER_SUBMIT}
            )
            and RemoteScope.BROWSER not in scopes
        ):
            raise RemoteControlError(
                "remote.scope_denied", "device lacks browser scope", status_code=403
            )
        if (
            capabilities.intersection(
                {
                    Capability.COMPUTER_OBSERVE,
                    Capability.COMPUTER_INPUT,
                    Capability.COMPUTER_CLIPBOARD_READ,
                    Capability.COMPUTER_CLIPBOARD_WRITE,
                }
            )
            and RemoteScope.COMPUTER not in scopes
        ):
            raise RemoteControlError(
                "remote.scope_denied", "device lacks computer scope", status_code=403
            )
        if payload.tool == "settings" and RemoteScope.SETTINGS not in scopes:
            raise RemoteControlError(
                "remote.scope_denied", "device lacks settings scope", status_code=403
            )

    @staticmethod
    def _unsupported_executor(_payload: RemoteActionPayload, _action: ActionRequest) -> None:
        raise RemoteControlError(
            "remote.operation_unavailable",
            "this Core has no executor registered for the remote operation",
            status_code=501,
        )


class RelayService:
    def __init__(self, repository: SQLiteRemoteControlRepository) -> None:
        self.repository = repository

    def publish(self, envelope: RelayEnvelope, *, now: datetime | None = None) -> RelayEnvelope:
        at = now or _now()
        if len(envelope.ciphertext) > MAX_RELAY_CIPHERTEXT_CHARS:
            raise RemoteControlError(
                "relay.envelope_too_large", "relay envelope is too large", status_code=413
            )
        if envelope.expires_at <= at or envelope.expires_at - at > timedelta(
            seconds=MAX_RELAY_TTL_SECONDS
        ):
            raise RemoteControlError("relay.invalid_ttl", "relay envelope TTL is invalid")
        return self.repository.publish_envelope(envelope)

    def pull(
        self, route_ref: str, recipient_ref: str, *, limit: int = 50, now: datetime | None = None
    ) -> list[RelayEnvelope]:
        if not 1 <= limit <= 50:
            raise RemoteControlError("relay.invalid_limit", "relay pull limit is invalid")
        return self.repository.pull_envelopes(
            route_ref, recipient_ref, now=now or _now(), limit=limit
        )

    def acknowledge(
        self, envelope_id: str, recipient_ref: str, *, now: datetime | None = None
    ) -> RelayEnvelope:
        return self.repository.acknowledge_envelope(
            envelope_id, recipient_ref=recipient_ref, now=now or _now()
        )


class HostConnector:
    """Pull opaque Relay envelopes and submit authenticated commands to the local Core."""

    def __init__(self, service: RemoteControlService, relay: RelayService) -> None:
        self.service = service
        self.relay = relay

    def poll_once(self, session_id: str, *, limit: int = 50) -> list[RemoteCommandReceipt]:
        session = self.service.repository.get_session(session_id)
        envelopes = self.relay.pull(session_id, session.host_id, limit=limit)
        receipts: list[RemoteCommandReceipt] = []
        for envelope in envelopes:
            try:
                plaintext = RemoteCrypto.decrypt_relay_payload(
                    envelope.ciphertext,
                    envelope.nonce,
                    self.service.key_store.get(session.session_key_ref),
                    route_ref=envelope.route_ref,
                )
                command = EncryptedRemoteCommand.model_validate_json(plaintext)
                receipt = self.service.submit_command(command)
            except Exception:
                # Invalid ciphertext is deliberately not acknowledged, so expiry bounds retention.
                continue
            receipts.append(receipt)
            self.relay.acknowledge(envelope.envelope_id, session.host_id)
        return receipts
