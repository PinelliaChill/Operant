"""Core-owned local control sessions with fenced human takeover."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from operant.application.remote_execution import (
    RemoteExecutionController,
    observation_hash_for_job,
)
from operant.domain.remote_execution import (
    CapabilityActionRequest,
    CapabilityManifest,
    RemoteActionIdempotency,
    RemoteCapability,
    RemoteTargetLease,
    RemoteTargetRegistration,
    RemoteTargetStatus,
)
from operant.persistence.sqlite import ConflictError
from operant.plugins.capability_registry import CapabilityPluginRecord, CapabilityPluginRegistry
from operant.protocol import canonical_action_hash, redact_public_text
from operant.remote.connector import ConnectorOutcome, RemoteTargetConnector
from operant.remote.local_browser import (
    BrowserTargetPolicy,
    IsolatedChromeBrowser,
    LocalBrowserConnector,
)
from operant.remote.local_computer import ComputerTargetPolicy, LocalComputerConnector, MacComputer
from operant.remote.local_worker import BROWSER_PLUGIN, COMPUTER_PLUGIN
from operant.remote.operator import CapabilityLeaseBinding
from operant.remote.sealed_input import seal_browser_input
from operant.remote.tool_extensions import _BROWSER_NAMES, _COMPUTER_NAMES
from operant.remote.transient_artifacts import TransientCapabilityArtifacts


def _now() -> datetime:
    return datetime.now(timezone.utc)


def conversation_local_control_tools(kind: str) -> tuple[str, ...]:
    """Tools granted only to a new conversation with a verified local session."""
    if kind == "browser":
        return _BROWSER_NAMES
    if kind == "computer":
        return tuple(name for name in _COMPUTER_NAMES if "clipboard" not in name)
    raise ValueError("unknown local control capability kind")


def local_control_tool_names() -> frozenset[str]:
    return frozenset((*_BROWSER_NAMES, *_COMPUTER_NAMES))


_SNAPSHOT_BINDING_FIELDS = (
    "session_id",
    "plugin_id",
    "target_id",
    "scope_digest",
    "source_digest",
    "generation",
    "plugin_version",
    "target_identity",
)


@dataclass(frozen=True)
class LocalControlSnapshotBinding:
    """Non-secret identity frozen into one new conversation snapshot."""

    kind: Literal["browser", "computer"]
    session_id: str
    plugin_id: str
    target_id: str
    scope_digest: str
    source_digest: str
    generation: str
    plugin_version: str
    target_identity: str

    def config_sources(self) -> dict[str, str]:
        prefix = f"local_control.{self.kind}."
        return {f"{prefix}{field}": str(getattr(self, field)) for field in _SNAPSHOT_BINDING_FIELDS}


def snapshot_control_bindings(
    config_sources: Mapping[str, str],
) -> tuple[LocalControlSnapshotBinding, ...]:
    """Parse only complete Core-written markers; never infer a global binding."""
    marked = {
        key: value for key, value in config_sources.items() if key.startswith("local_control.")
    }
    if not marked:
        return ()
    bindings: list[LocalControlSnapshotBinding] = []
    expected_keys: set[str] = set()
    for kind in ("browser", "computer"):
        prefix = f"local_control.{kind}."
        if not any(key.startswith(prefix) for key in marked):
            continue
        keys = {f"{prefix}{field}" for field in _SNAPSHOT_BINDING_FIELDS}
        if not keys.issubset(marked):
            raise PermissionError("local control snapshot binding is incomplete")
        try:
            binding = LocalControlSnapshotBinding(
                kind=kind,
                session_id=marked[f"{prefix}session_id"],
                plugin_id=marked[f"{prefix}plugin_id"],
                target_id=marked[f"{prefix}target_id"],
                scope_digest=marked[f"{prefix}scope_digest"],
                source_digest=marked[f"{prefix}source_digest"],
                generation=marked[f"{prefix}generation"],
                plugin_version=marked[f"{prefix}plugin_version"],
                target_identity=marked[f"{prefix}target_identity"],
            )
        except (TypeError, ValueError) as exc:
            raise PermissionError("local control snapshot binding is invalid") from exc
        bindings.append(binding)
        expected_keys.update(keys)
    if set(marked) != expected_keys:
        raise PermissionError("local control snapshot has an unknown binding marker")
    return tuple(bindings)


@dataclass
class LocalControlSession:
    session_id: str
    plugin_id: str
    target_id: str
    identity: str
    lease: RemoteTargetLease
    connector: RemoteTargetConnector
    computer_bundle_id: str | None = None
    authorized_targets: tuple[str, ...] = ()
    conversation_only: bool = False
    state: str = "active"
    last_error: str | None = None
    last_job_id: str | None = None
    observation: dict[str, Any] | None = None
    observations: set[str] = field(default_factory=set, repr=False)
    # The lock covers an entire driver action; takeover returns only when that
    # action has settled and the old lease has been revoked.
    lock: RLock = field(default_factory=RLock, repr=False)
    requests: dict[str, tuple[str, CapabilityActionRequest | None, str | None]] = field(
        default_factory=dict, repr=False
    )

    def projection(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "target_id": self.target_id,
            "plugin_id": self.plugin_id,
            "computer_bundle_id": self.computer_bundle_id,
            "state": self.state,
            "last_error": self.last_error,
            "last_job_id": self.last_job_id,
            "observation": self.observation,
        }


ConnectorFactory = Callable[
    [CapabilityPluginRecord, RemoteTargetLease, RemoteTargetConnector | None], RemoteTargetConnector
]


def bundled_connector(
    record: CapabilityPluginRecord,
    lease: RemoteTargetLease,
    previous: RemoteTargetConnector | None,
    *,
    core_origin: str | None = None,
) -> RemoteTargetConnector:
    fields: dict[str, Any] = dict(
        target_id=lease.target_id,
        lease_id=lease.lease_id,
        lease_token=lease.token,
        lease_fencing=lease.fencing,
    )
    if record.plugin_id == BROWSER_PLUGIN.plugin_id:
        browser = (
            previous.browser
            if isinstance(previous, LocalBrowserConnector)
            else IsolatedChromeBrowser(
                Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                BrowserTargetPolicy(
                    frozenset(record.allowed_targets),
                    blocked_origins=frozenset({core_origin})
                    if core_origin and urlsplit(core_origin).hostname != "::1"
                    else frozenset(),
                ),
                visible=True,
                blocked_ports=frozenset({urlsplit(core_origin).port or 80})
                if core_origin
                else frozenset(),
            )
        )
        return LocalBrowserConnector(**fields, browser=browser)
    return LocalComputerConnector(
        **fields,
        computer=MacComputer(
            ComputerTargetPolicy(frozenset(record.allowed_targets)),
            target_bundle_id=record.allowed_targets[0],
        ),
    )


class _EvidenceConnector:
    def __init__(
        self, connector: RemoteTargetConnector, artifacts: TransientCapabilityArtifacts
    ) -> None:
        self.connector = connector
        self.artifacts = artifacts
        self.target_id = connector.target_id
        self.lease_id = connector.lease_id
        self.lease_token = connector.lease_token
        self.lease_fencing = connector.lease_fencing

    def execute(self, job: Any) -> ConnectorOutcome:
        outcome = self.connector.execute(job)
        if outcome.artifact_bytes is not None:
            media_type = (
                "text/plain" if job.capability is RemoteCapability.CLIPBOARD_READ else "image/png"
            )
            self.artifacts.put(job.job_id, outcome.artifact_bytes, media_type)
        return outcome

    def cancel(self, job_id: str) -> None:
        self.connector.cancel(job_id)


class LocalControlManager:
    def __init__(
        self,
        registry: CapabilityPluginRegistry,
        controller: RemoteExecutionController,
        artifacts: TransientCapabilityArtifacts,
        *,
        connector_factory: ConnectorFactory = bundled_connector,
    ) -> None:
        self.registry = registry
        self.controller = controller
        self.repository = controller.repository
        self.artifacts = artifacts
        self.connector_factory = connector_factory
        self.core_origin: str | None = None
        self.sessions: dict[str, LocalControlSession] = {}
        self._task: asyncio.Task[None] | None = None

    def pending_unknowns(self, plugin_id: str | None = None) -> list[dict[str, str]]:
        """Unknown actions stay blocked across sessions and Core restarts."""
        self.repository.reconcile_expired(now=_now())
        with self.repository.store._connect() as connection:
            rows = connection.execute(
                "SELECT j.job_id,j.target_id,j.operation,t.endpoint_ref AS plugin_id "
                "FROM remote_execution_jobs j JOIN remote_execution_targets t "
                "ON t.target_id=j.target_id "
                "WHERE j.status='manual_reconcile_required' "
                "AND t.policy_ref LIKE 'local-control:%' "
                "AND (? IS NULL OR t.endpoint_ref=?) "
                "AND NOT EXISTS (SELECT 1 FROM security_audit_events a "
                "WHERE a.event_type='local_control.unknown_checked' "
                "AND json_extract(a.detail,'$.job_id')=j.job_id) "
                "ORDER BY j.created_at,j.job_id",
                (plugin_id, plugin_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def open(
        self,
        plugin_id: str,
        key: str,
        computer_bundle_id: str | None = None,
        *,
        conversation_only: bool = False,
    ) -> LocalControlSession:
        record = self.registry.get(plugin_id, require_enabled=True)
        if plugin_id == COMPUTER_PLUGIN.plugin_id:
            if computer_bundle_id is None and len(record.allowed_targets) == 1:
                computer_bundle_id = record.allowed_targets[0]
            if computer_bundle_id not in record.allowed_targets:
                raise ValueError("select an approved computer App before opening the session")
            record = record.model_copy(update={"allowed_targets": (computer_bundle_id,)})
        elif computer_bundle_id is not None:
            raise ValueError("browser session cannot select a computer App")
        session_id = "local-control-" + hashlib.sha256(key.encode()).hexdigest()[:24]
        if session_id in self.sessions:
            existing = self.sessions[session_id]
            if (
                existing.plugin_id != plugin_id
                or existing.computer_bundle_id != computer_bundle_id
                or existing.conversation_only != conversation_only
            ):
                raise ValueError("local session idempotency key changed plugin")
            return existing
        if any(s.plugin_id == plugin_id and s.state != "closed" for s in self.sessions.values()):
            raise ValueError("close the existing plugin session before opening another")
        if self.pending_unknowns(plugin_id):
            raise ValueError("manually check the unknown action before opening another session")
        with self.repository.store._connect() as connection:
            unsettled = connection.execute(
                "SELECT 1 FROM remote_execution_jobs j JOIN remote_execution_targets t "
                "ON j.target_id=t.target_id WHERE t.endpoint_ref=? "
                "AND t.policy_ref LIKE 'local-control:%' "
                "AND j.status IN ('queued','leased','running') LIMIT 1",
                (plugin_id,),
            ).fetchone()
        if unsettled:
            raise ValueError(
                "prior local job is unsettled; wait for lease expiry and check its result"
            )
        if sum(s.state != "closed" for s in self.sessions.values()) >= 8:
            raise ValueError("close a local control session before opening another")
        plugin = BROWSER_PLUGIN if plugin_id == BROWSER_PLUGIN.plugin_id else COMPUTER_PLUGIN
        target_id = session_id
        identity = self._identity(session_id, record)
        target = RemoteTargetRegistration(
            target_id=target_id,
            display_name=plugin_id,
            endpoint_ref=plugin_id,
            identity_public_key=identity,
            credential_ref="LOCAL_CONTROL_LEASE",
            policy_ref=f"local-control:{record.source_digest}",
            artifact_namespace=session_id,
            capability_manifest=CapabilityManifest(
                version=plugin.protocol_version,
                capabilities=tuple(sorted(plugin.capabilities, key=lambda c: c.value)),
                supported_operations=tuple(sorted(plugin.operations)),
                platform="macOS",
            ),
        )
        self.repository.register_target(target)
        self.repository.heartbeat_target(target_id, identity_public_key=identity, now=_now())
        lease = self._lease(target_id)
        try:
            connector = self._connector(record, lease, None)
        except Exception:
            self._release(lease)
            raise
        session = LocalControlSession(session_id, plugin_id, target_id, identity, lease, connector)
        session.computer_bundle_id = computer_bundle_id
        session.authorized_targets = record.allowed_targets
        session.conversation_only = conversation_only
        self.sessions[session_id] = session
        return session

    def _connector(
        self,
        record: CapabilityPluginRecord,
        lease: RemoteTargetLease,
        previous: RemoteTargetConnector | None,
    ) -> RemoteTargetConnector:
        if self.connector_factory is bundled_connector:
            if self.core_origin is None:
                raise ValueError("local driver requires a verified loopback Core listener")
            return bundled_connector(record, lease, previous, core_origin=self.core_origin)
        return self.connector_factory(record, lease, previous)

    def _lease(self, target_id: str) -> RemoteTargetLease:
        self.repository.reconcile_expired(now=_now())
        return self.repository.acquire_lease(
            RemoteTargetLease(
                target_id=target_id,
                owner="core:local-control",
                token=secrets.token_urlsafe(32),
                fencing=1,
                workspace_ref="local-control",
                expires_at=_now() + timedelta(seconds=180),
            ),
            now=_now(),
        )

    def _release(self, lease: RemoteTargetLease) -> None:
        try:
            self.repository.release_lease(
                target_id=lease.target_id,
                lease_id=lease.lease_id,
                token=lease.token,
                fencing=lease.fencing,
                now=_now(),
            )
        except ConflictError:
            current = self.repository.get_lease(lease.lease_id)
            target = self.repository.get_target(lease.target_id)
            if (
                current.released_at is None
                and current.expires_at > _now()
                and target.fencing == lease.fencing
            ):
                raise

    @staticmethod
    def _identity(session_id: str, record: CapabilityPluginRecord) -> str:
        return canonical_action_hash(
            {
                "session_id": session_id,
                "source_digest": record.source_digest,
                "generation": record.generation,
                "allowed_targets": record.allowed_targets,
            }
        )

    def _authorized_record(self, session: LocalControlSession) -> CapabilityPluginRecord:
        try:
            record = self.registry.get(session.plugin_id, require_enabled=True)
            if session.computer_bundle_id is not None:
                if session.computer_bundle_id not in record.allowed_targets:
                    raise PermissionError("computer App authorization has been revoked")
                record = record.model_copy(
                    update={"allowed_targets": (session.computer_bundle_id,)}
                )
            if self._identity(session.session_id, record) != session.identity:
                raise PermissionError("local plugin authorization changed; close the old session")
            return record
        except (KeyError, ValueError, PermissionError):
            session.state = "failed"
            session.last_error = "local plugin authorization revoked or changed"
            session.observations.clear()
            session.observation = None
            self._release(session.lease)
            self.repository.reconcile_expired(now=_now())
            raise

    def get(self, session_id: str, *, active: bool = False) -> LocalControlSession:
        session = self.sessions[session_id]
        if active:
            with session.lock:
                if session.state != "active":
                    raise ValueError("local control is paused; resume and observe again")
                self._authorized_record(session)
        return session

    def transition(self, session_id: str, state: str) -> LocalControlSession:
        session = self.get(session_id)
        with session.lock:
            if session.state == state:
                return session
            if session.state == "failed" and state != "closed":
                raise ValueError(
                    "inspect the original failed job and close this session; do not replay it"
                )
            if state == "active":
                if session.state != "human_control":
                    raise ValueError("only a human-controlled session can resume")
                record = self._authorized_record(session)
                self.repository.heartbeat_target(
                    session.target_id, identity_public_key=session.identity, now=_now()
                )
                lease = self._lease(session.target_id)
                try:
                    connector = self._connector(record, lease, session.connector)
                except Exception:
                    self._release(lease)
                    raise
                session.lease, session.connector = lease, connector
                session.requests.clear()
                session.observations.clear()
                session.observation = None
                session.last_job_id = None
                session.last_error = None
            else:
                if session.state == "closed":
                    raise ValueError("local control session is closed")
                if session.state in {"active", "failed"}:
                    self._release(session.lease)
                    self.repository.reconcile_expired(now=_now())
                if state == "closed" and isinstance(session.connector, LocalBrowserConnector):
                    session.connector.browser.close()
            session.state = state
            return session

    def bindings(self) -> dict[str, CapabilityLeaseBinding]:
        bindings: dict[str, CapabilityLeaseBinding] = {}
        for session in self.sessions.values():
            with session.lock:
                if session.state != "active" or session.conversation_only:
                    continue
                try:
                    self._authorized_record(session)
                except (KeyError, ValueError, PermissionError):
                    continue
                lease = session.lease
                bindings[self.kind(session)] = CapabilityLeaseBinding(
                    lease.target_id, lease.lease_id, lease.token, lease.fencing
                )
        return bindings

    def verified_snapshot_binding(
        self,
        session_id: str,
        *,
        expected: LocalControlSnapshotBinding | None = None,
    ) -> tuple[LocalControlSnapshotBinding, CapabilityLeaseBinding]:
        """Validate one exact local session and its durable fenced lease."""
        try:
            session = self.get(session_id, active=True)
        except (KeyError, ValueError) as exc:
            raise PermissionError("selected local control session is unavailable") from exc
        with session.lock:
            self.get(session_id, active=True)
            if not session.conversation_only:
                raise PermissionError("choose a conversation-only local control session")
            record = self._authorized_record(session)
            lease = session.lease
            try:
                with self.repository.store._connect() as connection:
                    self.repository._require_live_lease(
                        connection,
                        target_id=session.target_id,
                        lease_id=lease.lease_id,
                        token=lease.token,
                        fencing=lease.fencing,
                        now=_now(),
                    )
                target = self.repository.get_target(session.target_id)
            except (KeyError, ConflictError) as exc:
                raise PermissionError("selected local control lease is no longer active") from exc
            plugin = (
                BROWSER_PLUGIN if session.plugin_id == BROWSER_PLUGIN.plugin_id else COMPUTER_PLUGIN
            )
            if (
                target.status is not RemoteTargetStatus.ONLINE
                or target.endpoint_ref != session.plugin_id
                or target.identity_public_key != session.identity
                or target.policy_ref != f"local-control:{record.source_digest}"
                or target.capability_manifest.version != plugin.protocol_version
                or set(target.capability_manifest.capabilities) != set(plugin.capabilities)
                or set(target.capability_manifest.supported_operations) != set(plugin.operations)
                or lease.target_id != session.target_id
            ):
                raise PermissionError("selected local control target changed")
            binding = LocalControlSnapshotBinding(
                kind=self.kind(session),
                session_id=session.session_id,
                plugin_id=session.plugin_id,
                target_id=session.target_id,
                scope_digest=canonical_action_hash({"allowed_targets": record.allowed_targets}),
                source_digest=record.source_digest,
                generation=record.generation,
                plugin_version=record.version,
                target_identity=session.identity,
            )
            if expected is not None and binding != expected:
                raise PermissionError(
                    "local control authorization changed; open a new conversation"
                )
            return binding, CapabilityLeaseBinding(
                lease.target_id, lease.lease_id, lease.token, lease.fencing
            )

    def observe(self, session_id: str, key: str) -> dict[str, Any]:
        session = self.get(session_id, active=True)
        with session.lock:
            self.get(session_id, active=True)
            prior = session.requests.get(key)
            if prior:
                if prior[0] != "observe":
                    raise ValueError("local operation idempotency key changed request")
                return {"job_id": prior[2]}
            self._room(session)
            job = self.controller.observe(
                kind=self.kind(session),
                target_id=session.target_id,
                lease_id=session.lease.lease_id,
                lease_token=session.lease.token,
                lease_fencing=session.lease.fencing,
                target_ref=session.target_id,
                idempotency_key=key,
                now=_now(),
            )
            session.requests[key] = ("observe", None, job.job_id)
            session.last_job_id = job.job_id
            return {"job_id": job.job_id}

    @staticmethod
    def kind(session: LocalControlSession) -> Literal["browser", "computer"]:
        return "browser" if session.plugin_id == BROWSER_PLUGIN.plugin_id else "computer"

    @staticmethod
    def _room(session: LocalControlSession) -> None:
        if len(session.requests) >= 256:
            raise ValueError("local control operation limit reached; close this session")

    def act(
        self,
        session_id: str,
        operation: str,
        observation_hash: str,
        arguments: dict[str, Any],
        key: str,
    ) -> dict[str, Any]:
        from operant.api_phase56_target import _validate_local_capability_action

        session = self.get(session_id, active=True)
        fingerprint = canonical_action_hash(
            {"operation": operation, "observation_hash": observation_hash, "arguments": arguments}
        )
        with session.lock:
            self.get(session_id, active=True)
            prior = session.requests.get(key)
            if prior and prior[0] != fingerprint:
                raise ValueError("local operation idempotency key changed request")
            if prior and prior[2]:
                return {"job_id": prior[2]}
            if observation_hash not in session.observations:
                raise ValueError("observe the current lease before acting")
            if prior and prior[1] is not None:
                action = prior[1]
            else:
                self._room(session)
                args = dict(arguments)
                kind = self.kind(session)
                capabilities = {
                    "browser": {
                        "navigate": RemoteCapability.BROWSER_NAVIGATE,
                        "fill": RemoteCapability.BROWSER_SUBMIT,
                        "click": RemoteCapability.BROWSER_SUBMIT,
                        "press_key": RemoteCapability.BROWSER_SUBMIT,
                        "capture_viewport": RemoteCapability.BROWSER_SCREENSHOT,
                    },
                    "computer": {
                        "click_button": RemoteCapability.COMPUTER_INPUT,
                        "type_text": RemoteCapability.COMPUTER_INPUT,
                        "press_key": RemoteCapability.COMPUTER_INPUT,
                        "capture_window": RemoteCapability.COMPUTER_SCREENSHOT,
                        "read_clipboard": RemoteCapability.CLIPBOARD_READ,
                        "write_clipboard": RemoteCapability.CLIPBOARD_WRITE,
                    },
                }
                if operation not in capabilities[kind]:
                    raise ValueError("unsupported local control operation")
                if operation in {"fill", "type_text", "write_clipboard"}:
                    value = args.pop("value", None)
                    if (
                        not isinstance(value, str)
                        or len(value) > 2000
                        or redact_public_text(value) != value
                    ):
                        raise ValueError("local input is invalid or credential-shaped")
                    selector = (
                        args.get("selector")
                        if operation == "fill"
                        else f"computer:type_text:{args.get('element_name')}"
                        if operation == "type_text"
                        else "computer:write_clipboard"
                    )
                    if not isinstance(selector, str):
                        raise ValueError("local input target is invalid")
                    lease = session.lease
                    args["value_sealed"] = seal_browser_input(
                        value,
                        token=lease.token,
                        target_id=session.target_id,
                        lease_id=lease.lease_id,
                        fencing=lease.fencing,
                        observation_hash=observation_hash,
                        selector=selector,
                        idempotency_key=key,
                    )
                action = CapabilityActionRequest(
                    target_id=session.target_id,
                    target_ref=session.target_id,
                    capability=capabilities[kind][operation],
                    operation=operation,
                    observation_hash=observation_hash,
                    arguments=args,
                    idempotency_key=key,
                    idempotency=RemoteActionIdempotency.NON_IDEMPOTENT,
                )
                _validate_local_capability_action(kind, action)
                session.requests[key] = (fingerprint, action, None)
            job, _receipt = self.controller.act(
                action,
                kind=self.kind(session),
                lease_id=session.lease.lease_id,
                lease_token=session.lease.token,
                lease_fencing=session.lease.fencing,
                now=_now(),
            )
            session.requests[key] = (fingerprint, action, job.job_id)
            session.last_job_id = job.job_id
            return {"job_id": job.job_id}

    def _dispatch(self) -> None:
        self.artifacts.expire()
        for session in tuple(self.sessions.values()):
            with session.lock:
                if session.state != "active":
                    continue
                try:
                    self._authorized_record(session)
                    lease = session.lease
                    self.repository.renew_lease(
                        target_id=session.target_id,
                        lease_id=lease.lease_id,
                        token=lease.token,
                        fencing=lease.fencing,
                        now=_now(),
                        expires_at=_now() + timedelta(seconds=180),
                    )
                    outcomes = self.controller.dispatch_available(
                        _EvidenceConnector(session.connector, self.artifacts), now=_now(), limit=1
                    )
                    for outcome in outcomes:
                        job = self.repository.get_job(outcome.job_id)
                        if outcome.status.value == "succeeded" and job.operation in {
                            "observe_browser",
                            "observe_computer",
                        }:
                            body = outcome.postcondition["observation"]
                            observation_hash = observation_hash_for_job(
                                job, target_ref=session.target_id, body=cast(dict[str, Any], body)
                            )
                            observed = self.repository.get_observation(
                                session.target_id, observation_hash
                            )
                            session.observations.add(observation_hash)
                            session.observation = observed.model_dump(mode="json")
                    if any(o.status.value == "manual_reconcile_required" for o in outcomes):
                        self._release(session.lease)
                        session.state = "failed"
                        session.last_error = (
                            "outcome_unknown; inspect the original job before continuing"
                        )
                except Exception as exc:
                    session.state = "failed"
                    session.last_error = type(exc).__name__
                    with suppress(Exception):
                        self._release(session.lease)

    async def start(self) -> None:
        async def run() -> None:
            while True:
                await asyncio.to_thread(self._dispatch)
                await asyncio.sleep(0.1)

        self._task = asyncio.create_task(run())

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        for session in tuple(self.sessions.values()):
            if session.state != "closed":
                await asyncio.to_thread(self.transition, session.session_id, "closed")
        self.artifacts.clear()
