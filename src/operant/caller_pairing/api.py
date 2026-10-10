"""HTTP surface for the independent skill-source caller permission domain."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, Response

from operant.api_model_connections import _trusted_local
from operant.application.phase45_gateway import Phase45ActionGateway
from operant.application.service import ApplicationService
from operant.application.skill_sources import SkillSourceEffects
from operant.caller_pairing.crypto import COMMAND_PATH, READBACK_PATH
from operant.caller_pairing.runtime import CallerPairingError, CallerPairingRuntime
from operant.contracts.caller_pairing import (
    CallerChallengeInput,
    CallerCommand,
    CallerDeviceList,
    CallerEncryptedReply,
    CallerPairingTicket,
    CallerPairRequest,
    CallerReceiptView,
)
from operant.contracts.onboarding import SkillSourceView


def _base_url(request: Request) -> str:
    """Only the authenticated, numeric loopback origin can enter a ticket."""
    host = request.headers.get("host", "")
    try:
        parsed = urlsplit("http://" + host)
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="local caller required") from exc
    if (
        parsed.hostname not in {"127.0.0.1", "::1"}
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(status_code=403, detail="local caller required")
    scope_server = request.scope.get("server")
    if isinstance(scope_server, (list, tuple)) and len(scope_server) >= 2:
        server_host, server_port = scope_server[:2]
        if server_host in {"127.0.0.1", "::1"} and int(server_port) != port:
            raise HTTPException(status_code=403, detail="local caller required")
    return f"http://{host}"


def install_caller_pairing_routes(
    app: FastAPI,
    service: ApplicationService,
    *,
    local_authorizer: Callable[[Request], bool],
    confirmation_authorizer: Callable[[Request], bool],
) -> CallerPairingRuntime:
    """Install native and paired routes; parent wires v26 and outer proof guard."""
    native_effects: SkillSourceEffects = app.state.skill_source_effects
    root_gateway: Phase45ActionGateway = app.state.phase45_action_gateway

    def effects_for_device(device_id: str) -> SkillSourceEffects:
        # A paired device never borrows the native principal or its cached grant.
        gateway = Phase45ActionGateway(
            root_gateway.repository,
            root_gateway.phase_repository,
            root_gateway.engine,
            principal=f"paired-skill-source:{device_id}",
            on_approval_requested=root_gateway.on_approval_requested,
        )

        def refresh_for_device(
            workspace_ref: str | Path | None = None,
            *,
            discover: bool = False,
            gateway_override: Phase45ActionGateway | None = None,
        ) -> list[SkillSourceView]:
            if gateway_override is not None and gateway_override is not gateway:
                raise ValueError("paired discovery gateway changed")
            return native_effects.refresh(
                workspace_ref, discover=discover, gateway_override=gateway
            )

        return SkillSourceEffects(
            store=service.store,
            settings=native_effects.settings,
            gateway=lambda: gateway,
            refresh=refresh_for_device,
            requested_path=native_effects.requested_path,
            source_path=native_effects.source_path,
        )

    runtime = CallerPairingRuntime(service.store, effects_for_device=effects_for_device)
    app.state.caller_pairing_runtime = runtime

    def require_native(request: Request, *, confirmation: bool = False) -> None:
        if not _trusted_local(request, local_authorizer):
            raise HTTPException(status_code=403, detail="trusted native caller required")
        if confirmation and not confirmation_authorizer(request):
            raise HTTPException(status_code=403, detail="native confirmation required")

    def no_store(response: Response) -> None:
        response.headers["Cache-Control"] = "no-store"

    def require_paired_loopback(request: Request) -> None:
        if request.client is None or request.client.host not in {"127.0.0.1", "::1", "testclient"}:
            raise HTTPException(status_code=403, detail="local paired caller required")

    @app.post(
        "/v1/local-callers/challenges",
        operation_id="createCallerChallenge",
        response_model=CallerPairingTicket,
    )
    def create_challenge(
        body: CallerChallengeInput, request: Request, response: Response
    ) -> CallerPairingTicket:
        require_native(request, confirmation=True)
        no_store(response)
        try:
            return runtime.challenge(base_url=_base_url(request), ttl_seconds=body.ttl_seconds)
        except CallerPairingError as exc:
            raise HTTPException(
                status_code=exc.status_code, detail={"code": exc.code, "message": "配对票据不可用"}
            ) from exc

    @app.get(
        "/v1/local-callers/devices",
        operation_id="listCallerDevices",
        response_model=CallerDeviceList,
    )
    def list_devices(request: Request, response: Response) -> CallerDeviceList:
        require_native(request)
        no_store(response)
        return runtime.devices()

    @app.post(
        "/v1/local-callers/devices/{device_id}/revoke",
        operation_id="revokeCallerDevice",
        response_model=CallerDeviceList,
    )
    def revoke_device(device_id: str, request: Request, response: Response) -> CallerDeviceList:
        require_native(request)
        no_store(response)
        if not runtime.revoke(device_id):
            raise HTTPException(status_code=404, detail="device unavailable")
        return runtime.devices()

    def paired_error(exc: CallerPairingError) -> HTTPException:
        return HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": "配对请求未通过验证，请核对设备与请求状态"},
        )

    @app.post(
        "/v1/local-callers/pair",
        operation_id="pairLocalCaller",
        response_model=CallerEncryptedReply,
    )
    def pair(body: CallerPairRequest, request: Request, response: Response) -> CallerEncryptedReply:
        require_paired_loopback(request)
        no_store(response)
        try:
            return runtime.pair(body)
        except CallerPairingError as exc:
            raise paired_error(exc) from exc

    @app.post(
        COMMAND_PATH, operation_id="executeCallerCommand", response_model=CallerEncryptedReply
    )
    def command(body: CallerCommand, request: Request, response: Response) -> CallerEncryptedReply:
        require_paired_loopback(request)
        no_store(response)
        try:
            return runtime.command(body)
        except CallerPairingError as exc:
            raise paired_error(exc) from exc

    @app.post(READBACK_PATH, operation_id="readCallerRequest", response_model=CallerEncryptedReply)
    def paired_readback(
        body: CallerCommand, request: Request, response: Response
    ) -> CallerEncryptedReply:
        require_paired_loopback(request)
        no_store(response)
        try:
            return runtime.command(body, path=READBACK_PATH)
        except CallerPairingError as exc:
            raise paired_error(exc) from exc

    @app.get(
        "/v1/local-callers/requests/result",
        operation_id="getCallerRequest",
        response_model=CallerReceiptView,
    )
    def native_readback(
        device_id: str, request_id: str, request: Request, response: Response
    ) -> CallerReceiptView:
        require_native(request)
        no_store(response)
        try:
            return runtime.native_readback(device_id, request_id)
        except Exception as exc:
            # No private request or DB detail enters the native response.
            raise HTTPException(status_code=404, detail="request unavailable") from exc

    return runtime
