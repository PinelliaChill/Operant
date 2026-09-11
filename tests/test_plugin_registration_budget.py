from pathlib import Path

import pytest
from test_plugin_host import _cert, _package, _recall, _scope

from operant.plugins import HostBudget, PluginError, PluginHost, PluginRegistry


@pytest.mark.asyncio
async def test_registration_budget_denies_before_creating_extra_resource(tmp_path: Path):
    package, manifest = _package(tmp_path)
    registry = PluginRegistry(tmp_path / "managed", trusted_issuers={"issuer.local"})
    installation = registry.install(manifest, package, certification=_cert(manifest))

    class Plugin:
        async def handle(self, operation, request, api):
            api.register_resource(relative_path="indexes/first.json")
            api.register_resource(relative_path="indexes/excess.json")
            raise AssertionError("registration exceeded its call budget")

    host = PluginHost(
        registry,
        plugin_factories={manifest.plugin_id: lambda _: Plugin()},
        budget=HostBudget(max_calls=1),
    )
    binding = host.bind(installation.installation_id)
    host.enable(binding.binding_id)
    await host.start(installation.installation_id, mode="trusted_in_process")
    lease = host.start_run(binding.binding_id, run_id="run-1", scope=_scope())
    before = len(registry.resources_for(installation.installation_id))
    try:
        with pytest.raises(PluginError) as error:
            await host.invoke(lease, "recall", _recall(lease))
        assert error.value.code == "budget_exceeded"
        resources = registry.resources_for(installation.installation_id)
        assert len(resources) == before + 1
        assert all(item.relative_path != "indexes/excess.json" for item in resources)
    finally:
        await host.close()
