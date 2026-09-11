from __future__ import annotations

from pathlib import Path

import pytest
from test_plugin_host import _cert, _package, _recall, _scope

from operant.contracts.b2_1 import (
    CandidateBatch,
    CandidateReference,
    HostReadRequest,
    IndexEvent,
    IndexReceipt,
    MaintenanceInput,
    MemoryHead,
    MemoryVersionRef,
    ModelProxyRequest,
    ProposalBatch,
    SourceBatch,
    SourceRef,
)
from operant.plugins import HostCallbacks, PluginError, PluginHost, PluginRegistry


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["extract", "recall", "maintain", "on_index_event"])
async def test_host_invocation_enforces_direct_payload_authorization(
    tmp_path: Path, operation: str
):
    package, manifest = _package(tmp_path)
    registry = PluginRegistry(tmp_path / "managed", trusted_issuers={"issuer.local"})
    installation = registry.install(manifest, package, certification=_cert(manifest))
    invoked = []

    class Plugin:
        async def handle(self, op, request, api):
            invoked.append(op)
            if op == "recall":
                return CandidateBatch(
                    request_id=request.context.request_id,
                    candidates=(CandidateReference(ref=foreign_ref, score=1),),
                )
            if op == "maintain":
                # A valid empty result with another request's watermark must not be accepted.
                return ProposalBatch(
                    request_id=request.context.request_id, proposals=(), source_watermark="99"
                )
            if op == "on_index_event":
                return IndexReceipt(
                    event_id=request.event_id,
                    index_generation="gen",
                    applied_cursor="1",
                    outcome="applied",
                )
            return ProposalBatch(
                request_id=request.context.request_id,
                proposals=(),
                source_watermark=request.source_watermark,
            )

    host = PluginHost(
        registry,
        plugin_factories={manifest.plugin_id: lambda _: Plugin()},
        callbacks=HostCallbacks(
            authorize_source=lambda context, source: True,
            authorize_memory_ref=lambda context, ref: True,
            authorize_head=lambda context, head: True,
        ),
    )
    binding = host.bind(
        installation.installation_id,
        config=installation.config.model_copy(update={"maintenance_enabled": True}),
    )
    host.enable(binding.binding_id)
    await host.start(installation.installation_id, mode="trusted_in_process")
    lease = host.start_run(binding.binding_id, run_id="run-1", scope=_scope())
    context = _recall(lease).context
    foreign_ref = MemoryVersionRef(
        dataset_id="foreign", record_id="record", version=1, content_digest="d" * 64
    )
    requests = {
        "extract": SourceBatch(
            context=context,
            sources=(
                SourceRef(
                    source_type="item",
                    source_id="source",
                    revision=1,
                    content_digest="d" * 64,
                    scope=_scope("foreign-run"),
                    permission_epoch=context.permission_epoch,
                    availability="available",
                ),
            ),
            source_watermark="1",
        ),
        "recall": _recall(lease),
        "maintain": MaintenanceInput(
            context=context, job_id="job", action="reindex", source_watermark="1", budget_tokens=1
        ),
        "on_index_event": IndexEvent(
            context=context,
            event_id="event",
            head=MemoryHead(
                dataset_id="foreign",
                record_id="record",
                revision=0,
                publication_cursor="0",
                permission_epoch=context.permission_epoch,
                state="unpublished",
                published_version=None,
            ),
        ),
    }
    try:
        with pytest.raises(PluginError) as error:
            await host.invoke(lease, operation, requests[operation])
        assert error.value.code == (
            "protocol_mismatch" if operation == "maintain" else "permission_denied"
        )
        assert invoked == ([operation] if operation in {"recall", "maintain"} else [])
    finally:
        await host.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "capability,nested",
    [
        ("extract", "search"),
        ("on_index_event", "read_source"),
        ("on_index_event", "model"),
        ("maintain", "model"),
    ],
)
async def test_host_denies_nested_api_outside_manifest_capability(
    tmp_path: Path, capability: str, nested: str
):
    package, original = _package(tmp_path)
    manifest = original.model_copy(update={"capabilities": (capability,)})
    registry = PluginRegistry(tmp_path / "managed", trusted_issuers={"issuer.local"})
    installation = registry.install(manifest, package, certification=_cert(manifest))
    entered = []

    class Plugin:
        async def handle(self, op, request, api):
            entered.append(op)
            if nested == "search":
                await api.search(_recall(lease))
            elif nested == "read_source":
                await api.read_source(
                    HostReadRequest(context=request.context, source=source, max_bytes=1)
                )
            else:
                await api.model(
                    ModelProxyRequest(
                        context=request.context,
                        model_profile_id="configured",
                        input_source_refs=(),
                        instruction="test",
                        max_output_tokens=1,
                    )
                )
            raise AssertionError("nested API unexpectedly accepted")

    host = PluginHost(
        registry,
        plugin_factories={manifest.plugin_id: lambda _: Plugin()},
        callbacks=HostCallbacks(
            authorize_source=lambda context, candidate: candidate == source,
            authorize_head=lambda context, candidate: candidate == head,
        ),
    )
    binding = host.bind(
        installation.installation_id,
        config=installation.config.model_copy(
            update={
                "extraction_model_profile_id": "configured",
                "rerank_model_profile_id": "configured",
                "maintenance_enabled": True,
            }
        ),
    )
    host.enable(binding.binding_id)
    await host.start(installation.installation_id, mode="trusted_in_process")
    lease = host.start_run(binding.binding_id, run_id="run-1", scope=_scope())
    context = _recall(lease).context
    source = SourceRef(
        source_type="item",
        source_id="source",
        revision=1,
        content_digest="d" * 64,
        scope=context.scope,
        permission_epoch=context.permission_epoch,
        availability="available",
    )
    head = MemoryHead(
        dataset_id=context.dataset_id,
        record_id="record",
        revision=0,
        publication_cursor="0",
        permission_epoch=context.permission_epoch,
        state="unpublished",
        published_version=None,
    )
    request = (
        SourceBatch(context=context, sources=(source,), source_watermark="1")
        if capability == "extract"
        else IndexEvent(context=context, event_id="event", head=head)
        if capability == "on_index_event"
        else MaintenanceInput(
            context=context, job_id="job", action="reindex", source_watermark="1", budget_tokens=1
        )
    )
    try:
        with pytest.raises(PluginError) as error:
            await host.invoke(lease, capability, request)
        assert error.value.code == "permission_denied"
        assert entered == [capability]
    finally:
        await host.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["extract", "recall"])
@pytest.mark.parametrize("use_other_profile", [False, True])
async def test_host_pairs_model_profile_with_current_operation(
    tmp_path: Path, operation: str, use_other_profile: bool
):
    from operant.contracts.b2_1 import ModelProxyResult

    package, manifest = _package(tmp_path)
    registry = PluginRegistry(tmp_path / "managed", trusted_issuers={"issuer.local"})
    installation = registry.install(manifest, package, certification=_cert(manifest))
    calls = []
    selected = (
        ("rerank" if operation == "extract" else "extraction")
        if use_other_profile
        else ("extraction" if operation == "extract" else "rerank")
    )

    def model(request):
        calls.append(request.model_profile_id)
        return ModelProxyResult(
            request_id=request.context.request_id,
            output="ok",
            usage_tokens=1,
            usage_status="reported",
        )

    class Plugin:
        async def handle(self, op, request, api):
            await api.model(
                ModelProxyRequest(
                    context=request.context,
                    model_profile_id=selected,
                    input_source_refs=(),
                    instruction="test",
                    max_output_tokens=1,
                )
            )
            if op == "extract":
                return ProposalBatch(
                    request_id=request.context.request_id, proposals=(), source_watermark="1"
                )
            return CandidateBatch(request_id=request.context.request_id, candidates=())

    host = PluginHost(
        registry,
        plugin_factories={manifest.plugin_id: lambda _: Plugin()},
        callbacks=HostCallbacks(model=model, authorize_source=lambda context, ref: ref == source),
    )
    binding = host.bind(
        installation.installation_id,
        config=installation.config.model_copy(
            update={
                "extraction_model_profile_id": "extraction",
                "rerank_model_profile_id": "rerank",
            }
        ),
    )
    host.enable(binding.binding_id)
    await host.start(installation.installation_id, mode="trusted_in_process")
    lease = host.start_run(binding.binding_id, run_id="run-1", scope=_scope())
    context = _recall(lease).context
    source = SourceRef(
        source_type="item",
        source_id="source",
        revision=1,
        content_digest="d" * 64,
        scope=context.scope,
        permission_epoch=context.permission_epoch,
        availability="available",
    )
    request = (
        SourceBatch(context=context, sources=(source,), source_watermark="1")
        if operation == "extract"
        else _recall(lease)
    )
    try:
        if use_other_profile:
            with pytest.raises(PluginError) as error:
                await host.invoke(lease, operation, request)
            assert error.value.code == "permission_denied"
            assert calls == []
        else:
            await host.invoke(lease, operation, request)
            assert calls == [selected]
    finally:
        await host.close()
