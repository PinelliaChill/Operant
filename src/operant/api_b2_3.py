"""B2-3 typed commands and management projection."""

from __future__ import annotations

import hashlib
import json
import threading
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from operant.package_resources import protocol_schema_path

if TYPE_CHECKING:
    from operant.memory_plugins.manager import MemoryManager

from fastapi import FastAPI, Header, HTTPException, Response

from operant.application.service import ApplicationService
from operant.contracts.b2_3 import ManagementCommand, ManagementResult, ManagementState
from operant.domain.security import Capability
from operant.memory_plugins.ledger import LedgerError
from operant.persistence.sqlite import ConflictError
from operant.plugins.protocol import PluginError
from operant.protocol import is_sensitive_key, redact_public_text


def _public_projection(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if is_sensitive_key(key) else _public_projection(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_public_projection(item) for item in value]
    return redact_public_text(value, max_chars=100_000) if isinstance(value, str) else value


def _bounded_projection(model: Any) -> Any:
    value = model.model_dump(mode="json")
    if len(json.dumps(value).encode()) > 4_000_000:
        raise HTTPException(413, "management projection exceeds response budget")
    return type(model).model_validate(_public_projection(value))


def install_b2_3_routes(app: FastAPI, service: ApplicationService) -> None:
    manager_lock = threading.Lock()

    def manager() -> MemoryManager:
        from operant.memory_plugins.manager import MemoryManager

        with manager_lock:
            if not hasattr(app.state, "memory_manager"):
                app.state.memory_manager = MemoryManager(
                    service, host=app.state.plugin_host, skill_roots=app.state.b23_skill_roots
                )
                service.memory_manager = app.state.memory_manager
            return cast("MemoryManager", app.state.memory_manager)

    async def close() -> None:
        if hasattr(app.state, "memory_manager"):
            await app.state.memory_manager.close()

    service.memory_manager_factory = manager
    app.router.add_event_handler("shutdown", close)

    @app.get("/v1/protocol/b2-3", operation_id="negotiateB23")
    def negotiate() -> dict[str, Any]:
        path = protocol_schema_path("operant-b2-3.openapi.sha256")
        if not path.is_file():
            raise HTTPException(503, "B2-3 schema digest unavailable")
        return {
            "protocol_version": "b2-3.v1",
            "schema_digest": path.read_text().split()[0],
            "min_client_version": "b2-3.v1",
            "capabilities": ["plugin_lifecycle", "memory_management", "project_settings"],
        }

    @app.get("/v1/b2-3/management", response_model=ManagementState, operation_id="getB23Management")
    def state() -> ManagementState:
        return cast(ManagementState, _bounded_projection(manager().projection()))

    @app.post(
        "/v1/b2-3/commands",
        response_model=ManagementResult,
        operation_id="executeB23ManagementCommand",
    )
    async def execute(
        body: ManagementCommand,
        response: Response,
        idempotency_key: str | None = Header(default=None, min_length=1, max_length=300),
    ) -> ManagementResult:
        idempotency_key = idempotency_key or uuid4().hex
        response.headers["Idempotency-Key"] = idempotency_key
        try:
            gateway = app.state.phase45_action_gateway
            action, result, _ = gateway.guard(
                tool="memory_management",
                operation=body.action,
                target_id=body.dataset_id
                or body.installation_id
                or body.project_id
                or "management",
                arguments={
                    "action": body.action,
                    "request_digest": hashlib.sha256(body.model_dump_json().encode()).hexdigest(),
                },
                capabilities=(Capability.WORKSPACE_WRITE,),
                idempotency_key=idempotency_key,
            )
            if result.decision.value != "allow" or result.lease is None:
                raise HTTPException(403, {"code": "policy_denied", "message": result.reason_code})
            gateway.consume(result.lease, action)
            return cast(
                ManagementResult,
                _bounded_projection(await manager().execute(body, idempotency_key=idempotency_key)),
            )
        except (LedgerError, ConflictError) as exc:
            raise HTTPException(409, {"code": "revision_conflict", "message": str(exc)}) from exc
        except PluginError as exc:
            raise HTTPException(409, {"code": exc.code, "message": str(exc)}) from exc
        except (ValueError, KeyError) as exc:
            raise HTTPException(422, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
