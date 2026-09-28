from __future__ import annotations

from pathlib import Path

import pytest

from operant.plugins.capability_registry import CapabilityPluginRegistry
from operant.remote.local_worker import BROWSER_PLUGIN, COMPUTER_PLUGIN


def test_bundled_capability_install_enable_disable_and_reopen(tmp_path: Path) -> None:
    registry = CapabilityPluginRegistry(tmp_path / "capability-plugins")
    installed = registry.install(
        BROWSER_PLUGIN.plugin_id, ("https://example.com", "https://example.com")
    )
    assert installed.state == "disabled"
    assert installed.allowed_targets == ("https://example.com:443",)
    with pytest.raises(PermissionError, match="disabled"):
        registry.get(BROWSER_PLUGIN.plugin_id, require_enabled=True)
    enabled = registry.set_enabled(BROWSER_PLUGIN.plugin_id, True)
    assert enabled.state == "enabled"
    reopened = CapabilityPluginRegistry(tmp_path / "capability-plugins")
    assert reopened.get(BROWSER_PLUGIN.plugin_id, require_enabled=True) == enabled
    with pytest.raises(ValueError, match="disable"):
        reopened.uninstall(BROWSER_PLUGIN.plugin_id)
    reopened.set_enabled(BROWSER_PLUGIN.plugin_id, False)
    reopened.uninstall(BROWSER_PLUGIN.plugin_id)
    assert reopened.list() == ()


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
