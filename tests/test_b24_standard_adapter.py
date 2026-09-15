"""Keep trusted recall adaptation equivalent to the standard wire path."""

from __future__ import annotations

import asyncio
import importlib.util
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from operant.contracts.b2_1 import (
    CandidateBatch,
    CandidateReference,
    MemoryVersionRef,
    RecallRequest,
    RpcContext,
    WorkspaceScope,
)
from operant.domain.models import utc_now


@pytest.mark.parametrize(
    "corruption", ["none", "copied_result", "constructed_result", "constructed_request"]
)
@pytest.mark.parametrize("query", ["MemoryPack", "  中文标识符  ", "   "])
def test_standard_typed_recall_preserves_wire_validation(
    monkeypatch: pytest.MonkeyPatch, query: str, corruption: str
) -> None:
    path = Path(__file__).resolve().parents[1] / "plugins/memory-standard/plugin.py"
    spec = importlib.util.spec_from_file_location("b24_standard_adapter_test", path)
    assert spec is not None and spec.loader is not None
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)
    monkeypatch.setattr(plugin, "load_config", lambda: plugin.validate_config())
    context = RpcContext(
        sdk_version="operant-memory-sdk.v1",
        request_id="request-test",
        installation_id="installation-test",
        dataset_id="dataset-test",
        scope=WorkspaceScope(kind="workspace", project_id="project-test", workspace_id="ws-test"),
        deadline=utc_now() + timedelta(seconds=30),
        cancel_token="cancel-test",
        idempotency_key="idem-test",
        request_digest="a" * 64,
        binding_epoch=1,
        permission_epoch=1,
        lease_fencing=1,
    )
    request = RecallRequest(
        context=context,
        query=query,
        explicit_refs=(),
        knowledge_cutoff="0",
        max_candidates=5,
        token_budget=0,
    )
    candidate = CandidateReference(
        ref=MemoryVersionRef(
            dataset_id=context.dataset_id,
            record_id="record-test",
            version=1,
            content_digest="b" * 64,
        ),
        score=0.5,
    )
    if corruption == "copied_result":
        candidate = candidate.model_copy(update={"score": 2.0})
    elif corruption == "constructed_result":
        candidate = CandidateReference.model_construct(ref=candidate.ref, score=2.0)
    elif corruption == "constructed_request":
        values = dict(request.__dict__)
        values["max_candidates"] = 101
        request = RecallRequest.model_construct(**values)
    result = CandidateBatch(request_id=context.request_id, candidates=(candidate,))
    if corruption == "constructed_result":
        result = CandidateBatch.model_construct(
            request_id=context.request_id, candidates=(candidate,)
        )

    class Host:
        calls: list[Any]

        def __init__(self) -> None:
            self.calls = []

        def check_cancelled(self) -> None:
            self.calls.append("cancel-check")

        async def search(self, value: RecallRequest) -> CandidateBatch:
            self.calls.append(value)
            return result

    typed_host, wire_host = Host(), Host()

    async def wire() -> CandidateBatch:
        payload = await plugin._handle(
            "recall", request.model_dump(mode="json"), plugin._HostAdapter(wire_host)
        )
        return CandidateBatch.model_validate(payload)

    if corruption != "none" or not query.strip():
        with pytest.raises(ValueError):
            asyncio.run(wire())
        with pytest.raises(ValueError):
            asyncio.run(plugin.StandardMemoryPlugin().handle("recall", request, typed_host))
    else:
        actual = asyncio.run(plugin.StandardMemoryPlugin().handle("recall", request, typed_host))
        assert actual == asyncio.run(wire())
        assert typed_host.calls == wire_host.calls
        assert typed_host.calls[-1].query == query.strip()
        assert request.query == query
