from __future__ import annotations

import json
from pathlib import Path

import pytest

import operant.cli as cli
from operant.plugins.capability_registry import CapabilityPluginRegistry
from operant.remote.local_worker import BROWSER_PLUGIN, COMPUTER_PLUGIN


def test_bundled_capability_install_enable_disable_and_reopen(tmp_path: Path) -> None:
    registry = CapabilityPluginRegistry(tmp_path / "capability-plugins")
    installed = registry.install(
        BROWSER_PLUGIN.plugin_id, ("https://example.com", "https://example.com")
    )
    assert installed.state == "disabled"
    assert installed.allowed_targets == ("https://example.com:443",)
    assert len(installed.generation) == 32
    with pytest.raises(PermissionError, match="disabled"):
        registry.get(BROWSER_PLUGIN.plugin_id, require_enabled=True)
    enabled = registry.set_enabled(BROWSER_PLUGIN.plugin_id, True)
    assert enabled.state == "enabled"
    assert enabled.generation != installed.generation
    assert registry.set_enabled(BROWSER_PLUGIN.plugin_id, True).generation == enabled.generation
    reopened = CapabilityPluginRegistry(tmp_path / "capability-plugins")
    assert reopened.get(BROWSER_PLUGIN.plugin_id, require_enabled=True) == enabled
    with pytest.raises(ValueError, match="disable"):
        reopened.uninstall(BROWSER_PLUGIN.plugin_id)
    disabled = reopened.set_enabled(BROWSER_PLUGIN.plugin_id, False)
    assert disabled.generation != enabled.generation
    assert reopened.set_enabled(BROWSER_PLUGIN.plugin_id, False).generation == disabled.generation
    reenabled = reopened.set_enabled(BROWSER_PLUGIN.plugin_id, True)
    assert reenabled.generation not in {installed.generation, enabled.generation}
    reopened.set_enabled(BROWSER_PLUGIN.plugin_id, False)
    reopened.uninstall(BROWSER_PLUGIN.plugin_id)
    assert reopened.list() == ()


def test_legacy_record_and_reinstall_receive_fresh_generation(tmp_path: Path) -> None:
    registry = CapabilityPluginRegistry(tmp_path / "capability-plugins")
    original = registry.install(BROWSER_PLUGIN.plugin_id, ("https://example.com",))
    payload = json.loads(registry.state_path.read_text())
    del payload["plugins"][BROWSER_PLUGIN.plugin_id]["generation"]
    registry.state_path.write_text(json.dumps(payload))
    legacy = registry.get(BROWSER_PLUGIN.plugin_id)
    assert legacy.generation == "legacy"
    assert registry.set_enabled(BROWSER_PLUGIN.plugin_id, False).generation == "legacy"
    enabled = registry.set_enabled(BROWSER_PLUGIN.plugin_id, True)
    assert enabled.generation != "legacy"
    registry.set_enabled(BROWSER_PLUGIN.plugin_id, False)
    replacement = registry.install(BROWSER_PLUGIN.plugin_id, ("https://example.com",))
    assert replacement.generation not in {"legacy", original.generation, enabled.generation}


def test_cli_worker_stops_after_quick_disable_enable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = CapabilityPluginRegistry(tmp_path / "capability-plugins")
    registry.install(BROWSER_PLUGIN.plugin_id, ("http://127.0.0.1:8765",))
    registry.set_enabled(BROWSER_PLUGIN.plugin_id, True)
    monkeypatch.setenv("OPERANT_TARGET_LEASE_TOKEN", "test-lease-token-123456")
    monkeypatch.setattr(cli, "_capability_registry", lambda: registry)
    observed = {"stale_rejected": False, "closed": False}

    class FakeWorker:
        lifecycle_check = None

        def run(self) -> None:
            assert self.lifecycle_check is not None
            self.lifecycle_check()
            registry.set_enabled(BROWSER_PLUGIN.plugin_id, False)
            registry.set_enabled(BROWSER_PLUGIN.plugin_id, True)
            try:
                self.lifecycle_check()
            except PermissionError:
                observed["stale_rejected"] = True
                raise

        def close(self) -> None:
            observed["closed"] = True

    monkeypatch.setattr(
        "operant.remote.local_worker.browser_worker", lambda **_kwargs: FakeWorker()
    )
    cli.capability_worker(
        kind="browser",
        core_origin="http://127.0.0.1:8000",
        target_id="target",
        lease_id="lease",
        lease_fencing=1,
        chrome_path=Path("/unused"),
        visible=False,
    )
    assert observed == {"stale_rejected": True, "closed": True}


def test_registry_rejects_unbundled_package_and_tampered_digest(tmp_path: Path) -> None:
    registry = CapabilityPluginRegistry(tmp_path / "capability-plugins")
    with pytest.raises(ValueError, match="bundled"):
        registry.install("external.python", ("https://example.com",))
    installed = registry.install(BROWSER_PLUGIN.plugin_id, ("https://example.com",))
    registry.state_path.write_text(
        registry.state_path.read_text().replace(installed.source_digest, "0" * 64)
    )
    with pytest.raises(ValueError, match="source changed"):
        registry.get(BROWSER_PLUGIN.plugin_id)
    with pytest.raises(ValueError, match="source changed"):
        registry.set_enabled(BROWSER_PLUGIN.plugin_id, True)
    assert registry.set_enabled(BROWSER_PLUGIN.plugin_id, False).state == "disabled"
    registry.uninstall(BROWSER_PLUGIN.plugin_id)
    assert registry.list() == ()


def test_registry_refuses_symlinked_state(tmp_path: Path) -> None:
    registry = CapabilityPluginRegistry(tmp_path / "capability-plugins")
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    registry.state_path.symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        registry.list()
    assert outside.read_text() == "{}"


def test_registry_refuses_protected_computer_app(tmp_path: Path) -> None:
    registry = CapabilityPluginRegistry(tmp_path / "capability-plugins")
    with pytest.raises(ValueError, match="protected App"):
        registry.install(COMPUTER_PLUGIN.plugin_id, ("com.apple.Terminal",))
    assert registry.list() == ()
