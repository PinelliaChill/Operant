from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest
from test_plugin_cleanup_lifecycle import _running_host


@pytest.mark.asyncio
async def test_sync_close_cannot_block_lifecycle_deadline(tmp_path: Path):
    class Plugin:
        def __init__(self):
            self.release = threading.Event()
            self.finished = threading.Event()

        def close(self):
            try:
                self.release.wait(3)
            finally:
                self.finished.set()

    plugin = Plugin()
    host, registry, installation, lease = await _running_host(tmp_path, plugin)
    host._CALL_QUIESCE_TIMEOUT_SECONDS = 0.05
    try:
        receipt = await asyncio.wait_for(
            host.stop(installation.installation_id, stop_run_ids=(lease.run_id,)), 1
        )
        assert receipt.state == "restart_required"
        assert installation.installation_id in host._engines
        assert not plugin.finished.is_set()
        assert registry.installation_root(installation.installation_id).exists()
    finally:
        plugin.release.set()
        await asyncio.to_thread(plugin.finished.wait, 1)
        await host.close()


@pytest.mark.asyncio
async def test_host_shutdown_and_reentry_keep_running_code_fenced(tmp_path: Path):
    from test_plugin_cleanup_lifecycle import _recall, _StubbornPlugin

    from operant.plugins import PluginError

    plugin = _StubbornPlugin()
    host, registry, installation, lease = await _running_host(tmp_path, plugin)
    host._CALL_QUIESCE_TIMEOUT_SECONDS = 0.02
    task = asyncio.create_task(host.invoke(lease, "recall", _recall(lease)))
    await plugin.entered.wait()
    try:
        with pytest.raises(PluginError) as error:
            await asyncio.wait_for(host.close(), 1)
        assert error.value.code == "restart_required"
        assert installation.installation_id in host._engines
        assert registry.installation_root(installation.installation_id).exists()
        with pytest.raises(PluginError):
            host.enable(lease.binding_id)
        with pytest.raises(PluginError):
            await host.start(installation.installation_id)
    finally:
        plugin.release.set()
        with pytest.raises(PluginError):
            await task
        await host.close()
