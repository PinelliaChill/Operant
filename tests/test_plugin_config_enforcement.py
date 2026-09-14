from __future__ import annotations

from pathlib import Path

import pytest
from test_plugin_host import _cert, _package, _recall, _scope

from operant.contracts.b2_1 import CandidateBatch, MaintenanceInput, ProposalBatch
from operant.plugins import HostCallbacks, PluginError, PluginHost, PluginRegistry


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,configured,requested,allowed",
    [
        ("maintain", False, 0, False),
        ("maintain", True, 0, True),
        ("recall", 2, 2, True),
        ("recall", 2, 3, False),
        ("search", 2, 2, True),
        ("search", 2, 3, False),
    ],
)
async def test_binding_config_is_enforced_at_host_boundary(
    tmp_path: Path, operation, configured, requested, allowed
):
    package, manifest = _package(tmp_path)
    registry = PluginRegistry(tmp_path / "managed", trusted_issuers={"issuer.local"})
    installation = registry.install(manifest, package, certification=_cert(manifest))
    entered = []
    searched = []

    def search(request):
        searched.append(request.query)
        return CandidateBatch(request_id=request.context.request_id, candidates=())

    class Plugin:
        async def handle(self, op, request, api):
            entered.append(op)
            if operation == "search":
                return await api.search(request.model_copy(update={"token_budget": requested}))
            if op == "maintain":
                return ProposalBatch(
                    request_id=request.context.request_id,
                    proposals=(),
                    source_watermark=request.source_watermark,
                )
            return CandidateBatch(request_id=request.context.request_id, candidates=())

    config = installation.config.model_copy(
        update={
            "maintenance_enabled": bool(configured) if operation == "maintain" else False,
            "recall_token_budget": int(configured) if operation != "maintain" else 0,
        }
    )
    host = PluginHost(
        registry,
        plugin_factories={manifest.plugin_id: lambda _: Plugin()},
        callbacks=HostCallbacks(search=search),
    )
    binding = host.bind(installation.installation_id, config=config)
    host.enable(binding.binding_id)
    await host.start(installation.installation_id, mode="trusted_in_process")
    lease = host.start_run(binding.binding_id, run_id="run-1", scope=_scope())
    recall = _recall(lease)
    request = (
        MaintenanceInput(
            context=recall.context,
            job_id="job",
            action="reindex",
            source_watermark="1",
            budget_tokens=0,
        )
        if operation == "maintain"
        else recall.model_copy(update={"token_budget": requested if operation == "recall" else 0})
    )
    try:
        if allowed:
            await host.invoke(lease, "maintain" if operation == "maintain" else "recall", request)
            assert entered
            if operation == "search":
                assert searched
        else:
            with pytest.raises(PluginError) as error:
                await host.invoke(
                    lease, "maintain" if operation == "maintain" else "recall", request
                )
            assert error.value.code == (
                "permission_denied" if operation == "maintain" else "budget_exceeded"
            )
            assert not searched
            if operation != "search":
                assert not entered
    finally:
        await host.close()
