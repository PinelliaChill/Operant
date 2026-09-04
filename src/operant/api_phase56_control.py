from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from fastapi import FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from operant.application.phase45_gateway import Phase45ActionGateway
from operant.application.security import PolicyEngine
from operant.domain.remote_control import (
    EncryptedRemoteCommand,
    RelayEnvelope,
    RemoteCommandStatus,
    RemoteScope,
    RemoteTransportMode,
)
from operant.domain.security import Capability
from operant.mcp import GatewayDecision, McpError
from operant.persistence.remote_control import SQLiteRemoteControlRepository
from operant.persistence.security import SQLiteSecurityRepository
from operant.persistence.sqlite import ConflictError, IdempotencyConflictError, SQLiteStore
from operant.protocol import canonical_action_hash
from operant.remote_control.crypto import RemoteKeyStore
from operant.remote_control.runtime import (
    RelayService,
    RemoteCapabilityMap,
    RemoteControlError,
    RemoteControlService,
    RemoteExecutor,
    remote_control_policy_engine,
)

Authorizer = Callable[[Request], bool]
T = TypeVar("T")


class EnableRemoteHostBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host_id: str | None = Field(default=None, max_length=200)
    display_name: str = Field(min_length=1, max_length=200)
    core_version: str = Field(min_length=1, max_length=100)
    protocol_version: str = Field(min_length=1, max_length=100)
    capabilities: tuple[str, ...] = Field(default=(), max_length=64)
    enabled: bool = True


class PairingChallengeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host_id: str = Field(min_length=1, max_length=200)
    relay_url: str | None = Field(default=None, max_length=2_000)
    ttl_seconds: int = Field(default=120, ge=1, le=300)
    allowed_scopes: tuple[RemoteScope, ...] = (RemoteScope.OBSERVE,)


class ReconcileRemoteCommandBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: RemoteCommandStatus
    result_ref: str | None = Field(default=None, max_length=500)
    error_code: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def validate_terminal_status(self) -> ReconcileRemoteCommandBody:
        if self.status not in {RemoteCommandStatus.COMPLETED, RemoteCommandStatus.REJECTED}:
            raise ValueError("manual reconciliation requires a known terminal outcome")
        return self


class PairRemoteDeviceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    challenge_id: str = Field(min_length=1, max_length=200)
    one_time_code: str = Field(min_length=16, max_length=200)
    display_name: str = Field(min_length=1, max_length=200)
    signing_public_key: str = Field(min_length=32, max_length=500)
    exchange_public_key: str = Field(min_length=32, max_length=500)
    scopes: tuple[RemoteScope, ...] = (RemoteScope.OBSERVE,)


class CreateRemoteSessionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host_id: str = Field(min_length=1, max_length=200)
    device_id: str = Field(min_length=1, max_length=200)
    transport_mode: RemoteTransportMode
    protocol_version: str = Field(min_length=1, max_length=100)
    event_cursor: int = Field(default=0, ge=0, le=2**63 - 1)
    ttl_seconds: int = Field(default=900, ge=1, le=3600)


class RelayAckBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipient_ref: str = Field(min_length=1, max_length=300)


def install_phase56_control_routes(
    app: FastAPI,
    store: SQLiteStore,
    *,
    local_authorizer: Authorizer,
    relay_authorizer: Authorizer,
    action_gateway: Phase45ActionGateway,
    key_store_path: str | Path | None = None,
    policy_engine: PolicyEngine | None = None,
    executor: RemoteExecutor | None = None,
    operation_capabilities: RemoteCapabilityMap | None = None,
) -> RemoteControlService:
    """Install opt-in routes only when explicit admin and Relay auth gates are supplied."""
    key_path = (
        store.path.parent.absolute() / "remote-control-keys.json"
        if key_store_path is None
        else Path(key_store_path)
    )
    service = RemoteControlService(
        store,
        RemoteKeyStore(key_path),
        policy_engine or remote_control_policy_engine(),
        executor=executor,
        operation_capabilities=operation_capabilities,
    )
    relay = RelayService(SQLiteRemoteControlRepository(store))
    app.state.remote_control_service = service
    app.state.relay_service = relay

    def require(request: Request, authorizer: Authorizer) -> None:
        if not authorizer(request):
            raise HTTPException(status_code=401, detail="remote authorization required")

    def invoke(call: Callable[[], T]) -> T:
        try:
            return call()
        except RemoteControlError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="remote resource was not found") from exc
        except IdempotencyConflictError as exc:
            raise HTTPException(
                status_code=409, detail="remote idempotency binding conflicts"
            ) from exc
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail="remote state conflicts") from exc

    def guard_reconciliation(command_id: str, body: ReconcileRemoteCommandBody) -> None:
        receipt = service.repository.get_command(command_id)
        original = SQLiteSecurityRepository(store).get_security_action(receipt.action_hash)
        arguments = {
            "original_action_hash": original.action_hash,
            "original_target": original.normalized_target.model_dump(mode="json"),
            "original_workspace_id": original.workspace_id,
            "current_status": receipt.status.value,
            "reconciliation": body.model_dump(mode="json"),
        }
        action, guarded, _evaluation = action_gateway.guard(
            tool="remote_control",
            operation="reconcile_command",
            target_id=command_id,
            arguments=arguments,
            capabilities=(Capability.REMOTE_CONTROL_COMMAND,),
            idempotency_key=(
                f"remote-control:reconcile:{command_id}:{canonical_action_hash(arguments)}"
            ),
        )
        if guarded.decision is not GatewayDecision.ALLOW or guarded.lease is None:
            if guarded.decision is GatewayDecision.ASK:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "approval_required",
                        "approval_id": guarded.approval_id,
                        "action_hash": action.action_hash,
                    },
                )
            raise HTTPException(status_code=403, detail="policy_denied")
        try:
            action_gateway.consume(guarded.lease, action)
        except McpError as exc:
            raise HTTPException(status_code=409, detail=exc.code) from exc

    @app.post("/v1/remote-control/hosts/enable", operation_id="enableRemoteHost")
    def enable_remote_host(body: EnableRemoteHostBody, request: Request) -> dict[str, Any]:
        require(request, local_authorizer)
        return invoke(lambda: service.enable_host(**body.model_dump())).model_dump(mode="json")

    @app.get("/v1/remote-control/hosts/{host_id}", operation_id="getRemoteHost")
    def get_remote_host(host_id: str, request: Request) -> dict[str, Any]:
        require(request, local_authorizer)
        return invoke(lambda: service.repository.get_host(host_id)).model_dump(mode="json")

    @app.get("/v1/remote-control/hosts", operation_id="listRemoteHosts")
    def list_remote_hosts(
        request: Request, limit: int = Query(default=200, ge=1, le=500)
    ) -> dict[str, Any]:
        require(request, local_authorizer)
        hosts = invoke(lambda: service.repository.list_hosts(limit=limit))
        return {"items": [item.model_dump(mode="json") for item in hosts]}

    @app.post("/v1/remote-control/pairing-challenges", operation_id="createPairingChallenge")
    def create_pairing_challenge(
        body: PairingChallengeBody, request: Request, response: Response
    ) -> dict[str, Any]:
        require(request, local_authorizer)
        response.headers["x-operant-sensitive-response"] = "one-time"
        response.headers["Cache-Control"] = "no-store"
        return invoke(lambda: service.create_pairing_challenge(**body.model_dump())).model_dump(
            mode="json"
        )

    @app.post("/v1/remote-control/devices/pair", operation_id="pairRemoteDevice")
    def pair_remote_device(body: PairRemoteDeviceBody) -> dict[str, Any]:
        return invoke(lambda: service.pair_device(**body.model_dump())).model_dump(mode="json")

    @app.get("/v1/remote-control/devices", operation_id="listRemoteDevices")
    def list_remote_devices(
        request: Request, host_id: str = Query(min_length=1, max_length=200)
    ) -> dict[str, Any]:
        require(request, local_authorizer)
        devices = invoke(lambda: service.repository.list_devices(host_id))
        return {"items": [item.model_dump(mode="json") for item in devices]}

    @app.post("/v1/remote-control/devices/{device_id}/revoke", operation_id="revokeRemoteDevice")
    def revoke_remote_device(device_id: str, request: Request) -> dict[str, Any]:
        require(request, local_authorizer)
        return invoke(lambda: service.revoke_device(device_id)).model_dump(mode="json")

    @app.post("/v1/remote-control/sessions", operation_id="createRemoteSession")
    def create_remote_session(body: CreateRemoteSessionBody, request: Request) -> dict[str, Any]:
        require(request, local_authorizer)
        return invoke(lambda: service.create_session(**body.model_dump())).model_dump(mode="json")

    @app.post("/v1/remote-control/sessions/{session_id}/close", operation_id="closeRemoteSession")
    def close_remote_session(session_id: str, request: Request) -> dict[str, Any]:
        require(request, local_authorizer)
        return invoke(lambda: service.close_session(session_id)).model_dump(mode="json")

    @app.get("/v1/remote-control/sessions", operation_id="listRemoteSessions")
    def list_remote_sessions(
        request: Request, host_id: str = Query(min_length=1, max_length=200)
    ) -> dict[str, Any]:
        require(request, local_authorizer)
        sessions = invoke(lambda: service.repository.list_sessions(host_id))
        return {"items": [item.model_dump(mode="json") for item in sessions]}

    @app.post("/v1/remote-control/commands", operation_id="submitRemoteCommand")
    def submit_remote_command(command: EncryptedRemoteCommand) -> dict[str, Any]:
        # Device signature, session key, nonce and expiry authenticate this route.
        return invoke(lambda: service.submit_command(command)).model_dump(mode="json")

    @app.get("/v1/remote-control/commands/{command_id}", operation_id="getRemoteCommand")
    def get_remote_command(command_id: str, request: Request) -> dict[str, Any]:
        require(request, local_authorizer)
        return invoke(lambda: service.repository.get_command(command_id)).model_dump(mode="json")

    @app.post(
        "/v1/remote-control/commands/{command_id}/reconcile",
        operation_id="reconcileRemoteCommand",
    )
    def reconcile_remote_command(
        command_id: str, body: ReconcileRemoteCommandBody, request: Request
    ) -> dict[str, Any]:
        require(request, local_authorizer)
        invoke(lambda: guard_reconciliation(command_id, body))
        return invoke(
            lambda: service.reconcile_command(
                command_id,
                status=body.status,
                result_ref=body.result_ref,
                error_code=body.error_code,
            )
        ).model_dump(mode="json")

    @app.get("/v1/remote-control/events", operation_id="listRemoteControlEvents")
    def list_remote_control_events(
        request: Request,
        host_id: str = Query(min_length=1, max_length=200),
        session_id: str | None = Query(default=None, min_length=1, max_length=200),
        after_cursor: int = Query(default=0, ge=0, le=2**63 - 1),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        require(request, local_authorizer)
        events = invoke(
            lambda: service.list_events(
                host_id, session_id=session_id, after_cursor=after_cursor, limit=limit
            )
        )
        return {"items": events, "next_cursor": events[-1]["cursor"] if events else after_cursor}

    @app.post("/v1/relay/envelopes", operation_id="publishRelayEnvelope")
    def publish_relay_envelope(envelope: RelayEnvelope, request: Request) -> dict[str, Any]:
        require(request, relay_authorizer)
        return invoke(lambda: relay.publish(envelope)).model_dump(mode="json")

    @app.get("/v1/relay/envelopes", operation_id="pullRelayEnvelopes")
    def pull_relay_envelopes(
        request: Request,
        route_ref: str = Query(min_length=1, max_length=300),
        recipient_ref: str = Query(min_length=1, max_length=300),
        limit: int = Query(default=50, ge=1, le=50),
    ) -> dict[str, Any]:
        require(request, relay_authorizer)
        items = invoke(lambda: relay.pull(route_ref, recipient_ref, limit=limit))
        return {"items": [item.model_dump(mode="json") for item in items]}

    @app.post(
        "/v1/relay/envelopes/{envelope_id}/acknowledge", operation_id="acknowledgeRelayEnvelope"
    )
    def acknowledge_relay_envelope(
        envelope_id: str, body: RelayAckBody, request: Request
    ) -> dict[str, Any]:
        require(request, relay_authorizer)
        return invoke(lambda: relay.acknowledge(envelope_id, body.recipient_ref)).model_dump(
            mode="json"
        )

    @app.get("/v1/relay/health", operation_id="getRelayHealth")
    def get_relay_health(request: Request) -> dict[str, int | str]:
        require(request, relay_authorizer)
        from datetime import datetime, timezone

        return service.repository.relay_health(now=datetime.now(timezone.utc))

    return service
