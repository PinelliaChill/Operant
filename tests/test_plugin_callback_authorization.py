from __future__ import annotations

from pathlib import Path

import pytest
from test_plugin_host import _cert, _package, _recall, _scope

from operant.contracts.b2_1 import (
    CandidateBatch,
    CandidateReference,
    HostReadRequest,
    HostReadResult,
    MemoryVersionRef,
    ModelProxyRequest,
    ModelProxyResult,
    SourceRef,
)
from operant.plugins import HostCallbacks, PluginError, PluginHost, PluginRegistry


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attack",
    [
        "scope",
        "epoch",
        "unavailable",
        "forged_id",
        "missing_authorizer",
        "dataset",
        "profile",
        "model_source",
        "revoked_during_read",
        "wrong_result",
        "search_result",
    ],
)
async def test_host_denies_callback_authority_forgery(tmp_path: Path, attack: str) -> None:
    package, manifest = _package(tmp_path)
    registry = PluginRegistry(tmp_path / "managed", trusted_issuers={"issuer.local"})
    installation = registry.install(manifest, package, certification=_cert(manifest))
    calls: list[str] = []
    available = True

    async def read(request):
        nonlocal available
        calls.append("read")
        if attack == "revoked_during_read":
            available = False
        return HostReadResult(
            source=source.model_copy(update={"source_id": "wrong"})
            if attack == "wrong_result"
            else source,
            text="ok",
            truncated=False,
        )

    def search(request):
        calls.append("search")
        return CandidateBatch(
            request_id=request.context.request_id,
            candidates=(CandidateReference(ref=foreign_ref, score=1),),
        )

    def model(request):
        calls.append("model")
        return ModelProxyResult(
            request_id=request.context.request_id,
            output="ok",
            usage_tokens=1,
            usage_status="reported",
        )

    class Plugin:
        async def handle(self, operation, request, api):
            if attack in {"dataset", "search_result"}:
                return await api.search(
                    request.model_copy(
                        update={"explicit_refs": (foreign_ref,) if attack == "dataset" else ()}
                    )
                )
            if attack in {"profile", "model_source"}:
                await api.model(
                    ModelProxyRequest(
                        context=request.context,
                        model_profile_id="other-profile"
                        if attack == "profile"
                        else "allowed-profile",
                        input_source_refs=(source.model_copy(update={"source_id": "forged"}),)
                        if attack == "model_source"
                        else (),
                        instruction="test",
                        max_output_tokens=1,
                    )
                )
            else:
                updates = {
                    "scope": {"scope": _scope("other-run")},
                    "epoch": {"permission_epoch": source.permission_epoch + 1},
                    "unavailable": {"availability": "revoked"},
                    "forged_id": {"source_id": "forged"},
                }.get(attack, {})
                await api.read_source(
                    HostReadRequest(
                        context=request.context,
                        source=source.model_copy(update=updates),
                        max_bytes=100,
                    )
                )
            return CandidateBatch(request_id=request.context.request_id, candidates=())

    callbacks = HostCallbacks(
        read_source=read,
        search=search,
        model=model,
        authorize_source=None
        if attack == "missing_authorizer"
        else lambda context, ref: available and ref == source,
        authorize_memory_ref=lambda context, ref: True,  # Host must still reject foreign datasets.
    )
    host = PluginHost(
        registry, plugin_factories={manifest.plugin_id: lambda _: Plugin()}, callbacks=callbacks
    )
    assert installation.config is not None
    binding = host.bind(
        installation.installation_id,
        config=installation.config.model_copy(
            update={
                "extraction_model_profile_id": "allowed-profile",
                "rerank_model_profile_id": "allowed-profile",
            }
        ),
    )
    host.enable(binding.binding_id)
    await host.start(installation.installation_id, mode="trusted_in_process")
    lease = host.start_run(binding.binding_id, run_id="run-1", scope=_scope())
    source = SourceRef(
        source_type="item",
        source_id="source-1",
        revision=1,
        content_digest="d" * 64,
        scope=lease.scope,
        permission_epoch=lease.permission_epoch,
        availability="available",
    )
    foreign_ref = MemoryVersionRef(
        dataset_id="other-dataset", record_id="record", version=1, content_digest="d" * 64
    )
    try:
        with pytest.raises(PluginError) as error:
            await host.invoke(lease, "recall", _recall(lease))
        assert error.value.code == (
            "protocol_mismatch" if attack == "wrong_result" else "permission_denied"
        )
        assert calls == (
            ["read"]
            if attack in {"revoked_during_read", "wrong_result"}
            else ["search"]
            if attack == "search_result"
            else []
        )
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_host_allows_exact_authorized_sources_refs_and_configured_model(
    tmp_path: Path,
) -> None:
    package, manifest = _package(tmp_path)
    registry = PluginRegistry(tmp_path / "managed", trusted_issuers={"issuer.local"})
    installation = registry.install(manifest, package, certification=_cert(manifest))
    calls = []

    def read(request):
        calls.append("read")
        return HostReadResult(source=source, text="ok", truncated=False)

    def search(request):
        calls.append("search")
        return CandidateBatch(
            request_id=request.context.request_id,
            candidates=(CandidateReference(ref=ref, score=1),),
        )

    def model(request):
        calls.append("model")
        return ModelProxyResult(
            request_id=request.context.request_id,
            output="ok",
            usage_tokens=1,
            usage_status="reported",
        )

    class Plugin:
        async def handle(self, operation, request, api):
            await api.read_source(
                HostReadRequest(context=request.context, source=source, max_bytes=2)
            )
            await api.model(
                ModelProxyRequest(
                    context=request.context,
                    model_profile_id="allowed-profile",
                    input_source_refs=(source,),
                    instruction="test",
                    max_output_tokens=1,
                )
            )
            return await api.search(request.model_copy(update={"explicit_refs": (ref,)}))

    host = PluginHost(
        registry,
        plugin_factories={manifest.plugin_id: lambda _: Plugin()},
        callbacks=HostCallbacks(
            read_source=read,
            search=search,
            model=model,
            authorize_source=lambda context, candidate: candidate == source,
            authorize_memory_ref=lambda context, candidate: candidate == ref,
        ),
    )
    assert installation.config is not None
    binding = host.bind(
        installation.installation_id,
        config=installation.config.model_copy(
            update={"rerank_model_profile_id": "allowed-profile"}
        ),
    )
    host.enable(binding.binding_id)
    await host.start(installation.installation_id, mode="trusted_in_process")
    lease = host.start_run(binding.binding_id, run_id="run-1", scope=_scope())
    source = SourceRef(
        source_type="item",
        source_id="source-1",
        revision=1,
        content_digest="d" * 64,
        scope=lease.scope,
        permission_epoch=lease.permission_epoch,
        availability="available",
    )
    ref = MemoryVersionRef(
        dataset_id=lease.dataset_id, record_id="record", version=1, content_digest="d" * 64
    )
    try:
        result = await host.invoke(lease, "recall", _recall(lease))
        assert result.candidates[0].ref == ref
        assert calls == ["read", "model", "search"]
    finally:
        await host.close()
