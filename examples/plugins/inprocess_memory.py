"""A tiny trusted in-process plugin used by the MP-1 smoke tests."""

from __future__ import annotations

from pydantic import BaseModel

from operant.contracts.b2_1 import (
    CandidateBatch,
    LifecycleRequest,
    LifecycleResult,
    ProposalBatch,
)
from operant.plugins.protocol import RestrictedHostApi


class ExampleMemoryPlugin:
    """Returns typed empty results while exercising the real Host boundary."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def handle(self, operation: str, request: BaseModel, host: RestrictedHostApi) -> BaseModel:
        host.check_cancelled()
        self.calls.append(operation)
        if operation == "recall":
            return CandidateBatch(request_id=request.context.request_id, candidates=())  # type: ignore[attr-defined]
        if operation in {"extract", "maintain"}:
            return ProposalBatch(
                request_id=request.context.request_id,  # type: ignore[attr-defined]
                proposals=(),
                source_watermark=request.source_watermark,  # type: ignore[attr-defined]
            )
        if operation == "lifecycle":
            lifecycle_request = request
            assert isinstance(lifecycle_request, LifecycleRequest)
            state = "cancelled" if lifecycle_request.operation == "cancel" else "ready"
            return LifecycleResult(
                request_id=lifecycle_request.context.request_id,
                sdk_version="operant-memory-sdk.v1",
                state=state,
                checkpoint_ref=lifecycle_request.checkpoint_ref,
            )
        raise ValueError(f"unsupported operation: {operation}")

    def close(self) -> None:
        self.calls.append("close")


def create_plugin() -> ExampleMemoryPlugin:
    """Package entrypoint consumed by PluginHost's verified loader."""

    return ExampleMemoryPlugin()
