"""Workbench temporary resource inventory, policy and safe cleanup routes."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from typing import Literal, TypeVar

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from operant.application.resource_governance import ResourceGovernanceService
from operant.application.service import ApplicationService
from operant.domain.resource_governance import (
    ResourceCleanupPreview,
    ResourceCleanupResult,
    ResourceInventory,
    ResourceItem,
    ResourcePolicy,
)
from operant.persistence.sqlite import ConflictError, NotFoundError

LOG = logging.getLogger(__name__)
T = TypeVar("T")


class ResourcePolicyPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    completed_ttl_seconds: int = Field(ge=3600, le=31_536_000)
    unanswered_ttl_seconds: int = Field(ge=3600, le=31_536_000)


class ResourceConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    completed: bool


class ResourcePinRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pinned: bool


class ResourceCleanupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_ids: list[str] | None = Field(default=None, max_length=100)
    mode: Literal["manual", "automatic"] = "manual"
    after_artifact: int = Field(default=0, ge=0)


def install_workbench_resource_routes(app: FastAPI, service: ApplicationService) -> None:
    governance = ResourceGovernanceService(service)
    app.state.workbench_resources = governance

    async def call(action: Callable[[], T]) -> T:
        try:
            return await asyncio.to_thread(action)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ConflictError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="resource action is denied") from exc

    @app.get(
        "/v1/workbench/threads/{thread_id}/resources",
        operation_id="listWorkbenchResources",
        response_model=ResourceInventory,
    )
    async def list_resources(
        thread_id: str,
        after_artifact: int = Query(default=0, ge=0),
        after_revision: int = Query(default=0, ge=0),
        after_compaction: int = Query(default=0, ge=0),
    ) -> ResourceInventory:
        return await call(
            lambda: governance.inventory(
                thread_id,
                after_artifact=after_artifact,
                after_revision=after_revision,
                after_compaction=after_compaction,
            )
        )

    @app.patch(
        "/v1/workbench/threads/{thread_id}/resource-policy",
        operation_id="updateWorkbenchResourcePolicy",
        response_model=ResourcePolicy,
    )
    async def update_policy(thread_id: str, body: ResourcePolicyPatch) -> ResourcePolicy:
        return await call(lambda: governance.update_policy(thread_id, **body.model_dump()))

    @app.post(
        "/v1/workbench/threads/{thread_id}/resource-confirm",
        operation_id="confirmWorkbenchResources",
        response_model=ResourcePolicy,
    )
    async def confirm(thread_id: str, body: ResourceConfirmRequest) -> ResourcePolicy:
        return await call(lambda: governance.confirm(thread_id, completed=body.completed))

    @app.patch(
        "/v1/workbench/threads/{thread_id}/resources/{resource_id}",
        operation_id="setWorkbenchResourcePin",
        response_model=ResourceItem,
    )
    async def pin(thread_id: str, resource_id: str, body: ResourcePinRequest) -> ResourceItem:
        return await call(lambda: governance.pin(thread_id, resource_id, pinned=body.pinned))

    @app.post(
        "/v1/workbench/threads/{thread_id}/resource-cleanup-preview",
        operation_id="previewWorkbenchResourceCleanup",
        response_model=ResourceCleanupPreview,
    )
    async def preview(thread_id: str, body: ResourceCleanupRequest) -> ResourceCleanupPreview:
        return await call(
            lambda: governance.preview_cleanup(
                thread_id,
                resource_ids=body.resource_ids,
                mode=body.mode,
                after_artifact=body.after_artifact,
            )
        )

    @app.post(
        "/v1/workbench/threads/{thread_id}/resource-cleanup",
        operation_id="cleanupWorkbenchResources",
        response_model=ResourceCleanupResult,
    )
    async def cleanup(thread_id: str, body: ResourceCleanupRequest) -> ResourceCleanupResult:
        if body.resource_ids is None:
            raise HTTPException(status_code=422, detail="resource_ids is required")
        resource_ids = body.resource_ids
        return await call(
            lambda: governance.cleanup(thread_id, resource_ids=resource_ids, mode=body.mode)
        )

    stop = threading.Event()
    worker: threading.Thread | None = None

    def scan_loop() -> None:
        while not stop.is_set():
            try:
                governance.scan_due(thread_limit=25, resource_limit=25, stop_requested=stop.is_set)
            except Exception:
                LOG.exception("Workbench resource scan failed safely")
            stop.wait(300)

    async def start_worker() -> None:
        nonlocal worker
        stop.clear()
        worker = threading.Thread(target=scan_loop, name="workbench-resource-scan", daemon=True)
        worker.start()

    async def stop_worker() -> None:
        if worker is not None:
            stop.set()
            await asyncio.to_thread(worker.join)

    app.router.add_event_handler("startup", start_worker)
    app.router.on_shutdown.insert(0, stop_worker)
