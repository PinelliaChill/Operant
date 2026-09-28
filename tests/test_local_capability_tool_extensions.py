from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import operant.cli as cli
from operant.application.service import ApplicationService
from operant.domain.models import ModelProfile, ToolPolicy
from operant.domain.security import Capability
from operant.persistence.sqlite import SQLiteStore
from operant.plugins.capability_registry import CapabilityPluginRegistry
from operant.providers.openai_compatible import OpenAICompatibleProvider
from operant.remote.local_worker import BROWSER_PLUGIN
from operant.remote.tool_extensions import local_capability_tool_extensions
from operant.tools.workspace import WorkspaceTools


def test_local_browser_tool_requires_install_grant_and_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ("TARGET_ID", "LEASE_ID", "LEASE_FENCING", "LEASE_TOKEN"):
        monkeypatch.delenv(f"OPERANT_BROWSER_{key}", raising=False)
    database = tmp_path / "core.sqlite3"
    policy = ToolPolicy(allowed_tools=("ext_browser_observe", "ext_browser_click"))
    assert local_capability_tool_extensions(database, policy) == {}
    registry = CapabilityPluginRegistry(tmp_path / "capability-plugins")
    registry.install(BROWSER_PLUGIN.plugin_id, ("http://127.0.0.1:8765",))
    registry.set_enabled(BROWSER_PLUGIN.plugin_id, True)
    assert local_capability_tool_extensions(database, policy) == {}
    monkeypatch.setenv("OPERANT_BROWSER_TARGET_ID", "target-test")
    monkeypatch.setenv("OPERANT_BROWSER_LEASE_ID", "lease-test")
    monkeypatch.setenv("OPERANT_BROWSER_LEASE_FENCING", "1")
    monkeypatch.setenv("OPERANT_BROWSER_LEASE_TOKEN", "test-lease-token-123456")
    extensions = local_capability_tool_extensions(database, policy)
    assert set(extensions) == {"ext_browser_observe", "ext_browser_click"}
    assert extensions["ext_browser_observe"].capabilities == (Capability.BROWSER_OBSERVE,)
    assert not extensions["ext_browser_observe"].side_effecting
    assert extensions["ext_browser_click"].capabilities == (Capability.BROWSER_SUBMIT,)
    assert extensions["ext_browser_click"].side_effecting
    tools = WorkspaceTools(tmp_path, policy=policy, extensions=extensions)
    assert {item.name for item in tools.definitions()} == set(extensions)
    assert "test-lease-token" not in repr(extensions)
    registry.set_enabled(BROWSER_PLUGIN.plugin_id, False)
    assert local_capability_tool_extensions(database, policy) == {}


def test_local_browser_tool_refuses_incomplete_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ("TARGET_ID", "LEASE_ID", "LEASE_FENCING", "LEASE_TOKEN"):
        monkeypatch.delenv(f"OPERANT_BROWSER_{key}", raising=False)
    registry = CapabilityPluginRegistry(tmp_path / "capability-plugins")
    registry.install(BROWSER_PLUGIN.plugin_id, ("http://127.0.0.1:8765",))
    registry.set_enabled(BROWSER_PLUGIN.plugin_id, True)
    monkeypatch.setenv("OPERANT_BROWSER_TARGET_ID", "target-test")
    with pytest.raises(ValueError, match="incomplete"):
        local_capability_tool_extensions(
            tmp_path / "core.sqlite3",
            ToolPolicy(allowed_tools=("ext_browser_observe",)),
        )


def test_role_cli_grants_and_revokes_versioned_extension_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = ApplicationService(
        SQLiteStore(tmp_path / "roles.sqlite3"), OpenAICompatibleProvider()
    )
    service.initialize()
    profile = service.add_model_profile(
        ModelProfile(
            name="test-model",
            model_id="test-model",
            base_url="https://example.invalid/v1",
            secret_ref="OPERANT_TEST_KEY",
        )
    )
    monkeypatch.setattr(cli, "_service", lambda: service)
    runner = CliRunner()
    added = runner.invoke(
        cli.app,
        [
            "role",
            "add",
            "--name",
            "Browser role",
            "--model-profile-id",
            profile.id,
            "--system-prompt",
            "Observe only.",
            "--extension-tool",
            "ext_browser_observe",
        ],
    )
    assert added.exit_code == 0, added.output
    role_id = added.output.strip()
    assert "ext_browser_observe" in service.get_role(role_id).tool_policy.allowed_tools
    updated = runner.invoke(
        cli.app,
        [
            "role",
            "update",
            role_id,
            "--disable-extension-tool",
            "ext_browser_observe",
        ],
    )
    assert updated.exit_code == 0, updated.output
    assert "ext_browser_observe" not in service.get_role(role_id).tool_policy.allowed_tools
    assert "ext_browser_observe" in service.get_role(role_id, 1).tool_policy.allowed_tools
    rejected = runner.invoke(
        cli.app,
        ["role", "update", role_id, "--enable-extension-tool", "run_command"],
    )
    assert rejected.exit_code != 0
