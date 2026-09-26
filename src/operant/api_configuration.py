"""Core-owned configuration inheritance and edit routes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from operant.application.configuration import (
    ConfigPatch,
    ConfigService,
    EffectiveConfig,
    ScopeRecord,
)
from operant.application.service import ApplicationService
from operant.package_resources import protocol_schema_path
from operant.persistence.sqlite import ConflictError, NotFoundError
from operant.providers.openai_compatible import ProviderError


class PutScopeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    patch: dict[str, Any]
    expected_revision: int = Field(ge=0)


def _view(record: ScopeRecord) -> dict[str, Any]:
    return {
        "scope_type": record.scope_type,
        "scope_id": record.scope_id,
        "patch": record.patch.explicit(),
        "revision": record.revision,
        "updated_at": record.updated_at,
    }


def install_configuration_routes(app: FastAPI, service: ApplicationService) -> ConfigService:
    config = ConfigService(service.store)

    @app.get("/v1/protocol/phase3", operation_id="negotiatePhase3")
    def negotiate_phase3() -> dict[str, Any]:
        digest_path = protocol_schema_path("operant-phase3.openapi.sha256")
        if not digest_path.is_file():
            raise HTTPException(status_code=503, detail="Phase 3 schema digest unavailable")
        return {
            "protocol_version": "phase3.v1",
            "schema_digest": digest_path.read_text(encoding="utf-8").split()[0],
            "min_client_version": "phase3.v1",
            "capabilities": ["configuration", "approval_review", "goal_plan_control"],
        }

    @app.get("/v1/config/effective", operation_id="getEffectiveConfig")
    def effective_config(
        role_id: str = Query(min_length=1),
        project_id: str | None = None,
        workspace_ref: str | None = None,
    ) -> dict[str, Any]:
        try:
            if project_id is not None:
                project = service.store.get_workspace_initialization_by_id(project_id)
                project_workspace = str(Path(project.workspace_ref).resolve(strict=True))
                if workspace_ref is not None and (
                    str(Path(workspace_ref).resolve(strict=True)) != project_workspace
                ):
                    raise ValueError("project and workspace configuration scopes differ")
                workspace_ref = project_workspace
            result: EffectiveConfig = config.effective(
                service.get_role(role_id),
                project_id=project_id,
                workspace_ref=workspace_ref,
            )
            value = result.model_dump(mode="json")
            value["scopes"] = {key: _view(item) for key, item in result.scopes.items()}
            return value
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/v1/config/scopes/{scope_type}/{scope_id}", operation_id="getConfigScope")
    def get_scope(scope_type: str, scope_id: str) -> dict[str, Any]:
        try:
            return _view(config.get_scope(scope_type, scope_id))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.put("/v1/config/scopes/{scope_type}/{scope_id}", operation_id="putConfigScope")
    async def put_scope(scope_type: str, scope_id: str, body: PutScopeRequest) -> dict[str, Any]:
        try:
            patch = ConfigPatch.model_validate(body.patch)
            if patch.approval_reviewer is not None and patch.approval_reviewer.mode == "auto":
                profile_id = patch.approval_reviewer.profile_id
                assert profile_id is not None
                profile = service.get_model_profile(profile_id)
                if not profile.enabled:
                    raise ValueError("approval reviewer model profile is inactive")
                available = await service.discover_models(
                    base_url=profile.base_url, secret_ref=profile.secret_ref
                )
                if profile.model_id not in available:
                    raise ValueError("approval reviewer model is not available from Discovery")
            return _view(
                config.put_scope(
                    scope_type,
                    scope_id,
                    patch=patch.explicit(),
                    expected_revision=body.expected_revision,
                )
            )
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ProviderError as exc:
            raise HTTPException(
                status_code=503, detail="approval model Discovery is unavailable"
            ) from exc
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/v1/config/scopes/{scope_type}/{scope_id}", operation_id="deleteConfigScope")
    def delete_scope(
        scope_type: str,
        scope_id: str,
        expected_revision: int = Query(ge=0),
    ) -> dict[str, bool]:
        try:
            config.delete_scope(scope_type, scope_id, expected_revision=expected_revision)
            return {"deleted": True}
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return config
