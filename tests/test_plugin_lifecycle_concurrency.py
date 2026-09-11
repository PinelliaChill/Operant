from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from test_plugin_cleanup_lifecycle import _CooperativePlugin, _recall, _running_host

from operant.plugins import PluginError


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operations",
    [
        ("stop", "uninstall"),
        ("uninstall", "stop"),
        ("close", "uninstall"),
        ("uninstall", "close"),
        ("stop", "close"),
        ("close", "stop"),
        ("close", "close"),
        ("stop", "stop"),
        ("uninstall", "uninstall"),
    ],
)
async def test_concurrent_lifecycle_requests_have_stable_results(tmp_path: Path, operations):
    plugin = _CooperativePlugin()
    host, registry, installation, lease = await _running_host(tmp_path, plugin)
    invoking = asyncio.create_task(host.invoke(lease, "recall", _recall(lease)))
    await plugin.entered.wait()

    async def run(operation):
        if operation == "close":
            return await host.close()
        if operation == "stop":
            return await host.stop(installation.installation_id, stop_run_ids=(lease.run_id,))
        return await host.uninstall(
            installation.installation_id, data_policy="delete", stop_run_ids=(lease.run_id,)
        )

    try:
        results = await asyncio.wait_for(asyncio.gather(*(run(op) for op in operations)), 3)
        assert all(
            result is None or result.state in {"disabled", "uninstalled", "restart_required"}
            for result in results
        )
        assert not host._engines
        assert not host._calls
        if "uninstall" in operations and "close" not in operations:
            assert registry.get_installation(installation.installation_id).state == "uninstalled"
            assert any(result.state == "uninstalled" for result in results)
        with pytest.raises(PluginError):
            await invoking
    finally:
        await host.close()
