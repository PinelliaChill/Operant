"""Third-party Tools stay outside Core and require proven local isolation."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from operant import cli
from operant.application.factory import AgentFactory
from operant.application.service import _PersistentActionGateway
from operant.domain.models import ModelProfile, RolePreset, ToolPolicy
from operant.domain.security import Capability
from operant.persistence.sqlite import SQLiteStore
from operant.plugins.external_tool import (
    ExternalToolRegistry,
)
from operant.plugins.protocol import IsolationUnavailableError, SandboxEvidence, SandboxProbe
from operant.remote.tool_extensions import local_capability_tool_extensions
from operant.tools.workspace import WorkspaceTools


def _package(root: Path, script: str) -> Path:
    root.mkdir()
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "plugin_id": "sample_tool",
                "version": "1.0.0",
                "host_api_version": "operant-tool-extension.v1",
                "tools": [
                    {
                        "name": "ext_sample_tool_check",
                        "description": "Check isolated execution.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "outside_path": {"type": "string"},
                                "port": {"type": "integer"},
                            },
                            "required": ["outside_path", "port"],
                            "additionalProperties": False,
                        },
                    }
                ],
            }
        )
    )
    (root / "plugin.py").write_text(script)
    return root


def test_external_tool_install_requires_digest_isolation_and_role_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _package(tmp_path / "source", "print('{}')\n")
    root = tmp_path / "external-tools"
    registry = ExternalToolRegistry(root)
    with pytest.raises(ValueError, match="digest"):
        registry.install(source, expected_digest="0" * 64)
    assert registry.list() == ()
    record = registry.install(source)
    granted_name = record.granted_name("ext_sample_tool_check")
    assert record.state == "disabled"
    assert (
        local_capability_tool_extensions(
            tmp_path / "core.sqlite3", ToolPolicy(allowed_tools=(granted_name,))
        )
        == {}
    )
    with pytest.raises(ValueError, match="previous"):
        registry.install(source)

    def admitted(self: SandboxProbe, **_: object) -> SandboxEvidence:
        return SandboxEvidence(
            evidence_ref="sandbox-test", profile="(version 1)", runner="/bin/false"
        )

    monkeypatch.setattr(SandboxProbe, "check", admitted)
    enabled = registry.set_enabled("sample_tool", True)
    assert enabled.sandbox_evidence_ref == "sandbox-test"
    granted = local_capability_tool_extensions(
        tmp_path / "core.sqlite3", ToolPolicy(allowed_tools=(granted_name,))
    )
    assert tuple(granted) == (granted_name,)
    assert granted[granted_name].side_effecting
    assert granted[granted_name].plugin_version == f"1.0.0+{record.installation_id}"
    assert (
        local_capability_tool_extensions(
            tmp_path / "core.sqlite3", ToolPolicy(allowed_tools=("read_file",))
        )
        == {}
    )
    with pytest.raises(ValueError, match="disable"):
        registry.uninstall("sample_tool")
    (root / "packages" / "sample_tool" / "plugin.py").write_text("changed")
    with pytest.raises(PermissionError, match="source changed"):
        registry.get("sample_tool", require_enabled=True)


def test_external_tool_rejects_builtin_name_and_symlinked_script(tmp_path: Path) -> None:
    registry = ExternalToolRegistry(tmp_path / "external-tools")
    source = _package(tmp_path / "reserved", "print('{}')\n")
    manifest = json.loads((source / "manifest.json").read_text())
    manifest["plugin_id"] = "browser"
    manifest["tools"][0]["name"] = "ext_browser_observe"
    (source / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="namespace"):
        registry.install(source)
    source = _package(tmp_path / "symlinked", "print('{}')\n")
    (source / "plugin.py").unlink()
    (source / "plugin.py").symlink_to(tmp_path / "outside-script")
    (tmp_path / "outside-script").write_text("print('outside')\n")
    with pytest.raises(ValueError, match="invalid file"):
        registry.install(source)
    assert registry.list() == ()


def test_external_tool_source_directory_is_pinned_and_not_a_symlink(tmp_path: Path) -> None:
    source = _package(tmp_path / "source", "print('{}')\n")
    alias = tmp_path / "source-alias"
    alias.symlink_to(source, target_is_directory=True)
    registry = ExternalToolRegistry(tmp_path / "external-tools")
    with pytest.raises(ValueError, match="real directory"):
        registry.inspect(alias)
    with pytest.raises(ValueError, match="real directory"):
        registry.install(alias)
    with pytest.raises(ValueError, match="traverse directories"):
        registry.inspect(source / ".." / source.name)
    (source / "plugin.py").unlink()
    os.mkfifo(source / "plugin.py")
    with pytest.raises(ValueError, match="invalid file"):
        registry.inspect(source)
    (source / "plugin.py").unlink()
    (source / "plugin.py").write_text("print('{}')\n")
    source.chmod(0o777)
    with pytest.raises(PermissionError, match="not trusted"):
        registry.inspect(source)
    source.chmod(0o700)
    assert registry.list() == ()
    manifest, digest = registry.inspect(source)
    assert manifest.plugin_id == "sample_tool"
    assert registry.install(source, expected_digest=digest).package_digest == digest


def test_external_tool_rejects_unbound_package_files(tmp_path: Path) -> None:
    source = _package(tmp_path / "source", "print('{}')\n")
    registry = ExternalToolRegistry(tmp_path / "external-tools")
    registry.install(source)
    package = tmp_path / "external-tools" / "packages" / "sample_tool"
    (package / "helper.py").write_text("print('unbound')\n")
    with pytest.raises(PermissionError, match="unbound files"):
        registry.get("sample_tool")


def test_external_tool_cli_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _package(tmp_path / "source", "print('{}')\n")
    monkeypatch.setattr(cli, "database_path", lambda: tmp_path / "core.sqlite3")

    def admitted(self: SandboxProbe, **_: object) -> SandboxEvidence:
        return SandboxEvidence(
            evidence_ref="sandbox-test", profile="(version 1)", runner="/bin/false"
        )

    monkeypatch.setattr(SandboxProbe, "check", admitted)
    runner = CliRunner()
    inspected = runner.invoke(
        cli.app, ["capability-plugin", "inspect-external-tool", "--source", str(source)]
    )
    assert inspected.exit_code == 0, inspected.output
    digest = json.loads(inspected.output)["package_digest"]
    installed = runner.invoke(
        cli.app,
        [
            "capability-plugin",
            "install-external-tool",
            "--source",
            str(source),
            "--expected-sha256",
            digest,
        ],
    )
    assert installed.exit_code == 0, installed.output
    assert "当前禁用" in installed.output
    enabled = runner.invoke(cli.app, ["capability-plugin", "enable-external-tool", "sample_tool"])
    assert enabled.exit_code == 0, enabled.output
    listed = runner.invoke(cli.app, ["capability-plugin", "list-external-tools"])
    assert listed.exit_code == 0, listed.output
    assert '"state": "enabled"' in listed.output
    assert json.loads(listed.output)[0]["tools"][0].startswith("ext_sample_tool_check__")
    disabled = runner.invoke(cli.app, ["capability-plugin", "disable-external-tool", "sample_tool"])
    assert disabled.exit_code == 0, disabled.output
    removed = runner.invoke(
        cli.app, ["capability-plugin", "uninstall-external-tool", "sample_tool"]
    )
    assert removed.exit_code == 0, removed.output


def test_external_tool_enable_fails_closed_without_isolator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _package(tmp_path / "source", "print('{}')\n")
    registry = ExternalToolRegistry(tmp_path / "external-tools")
    registry.install(source)
    monkeypatch.setattr("operant.plugins.protocol.platform.system", lambda: "Linux")
    with pytest.raises(IsolationUnavailableError):
        registry.set_enabled("sample_tool", True)
    assert registry.get("sample_tool").state == "disabled"


def test_external_tool_reinstall_requires_fresh_role_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _package(tmp_path / "source", "print('{}')\n")
    root = tmp_path / "external-tools"
    registry = ExternalToolRegistry(root)

    def admitted(self: SandboxProbe, **_: object) -> SandboxEvidence:
        return SandboxEvidence(
            evidence_ref="sandbox-test", profile="(version 1)", runner="/bin/false"
        )

    monkeypatch.setattr(SandboxProbe, "check", admitted)
    first = registry.install(source)
    old_grant = first.granted_name("ext_sample_tool_check")
    registry.set_enabled("sample_tool", True)
    old_data = root / "data" / f"sample_tool-{first.installation_id}"
    (old_data / "retained.txt").write_text("previous installation data")
    previous_extension = local_capability_tool_extensions(
        tmp_path / "core.sqlite3", ToolPolicy(allowed_tools=(old_grant,))
    )[old_grant]
    registry.set_enabled("sample_tool", False)
    registry.uninstall("sample_tool")
    second = registry.install(source)
    new_grant = second.granted_name("ext_sample_tool_check")
    assert new_grant != old_grant
    registry.set_enabled("sample_tool", True)
    new_data = root / "data" / f"sample_tool-{second.installation_id}"
    assert old_data != new_data
    assert (old_data / "retained.txt").read_text() == "previous installation data"
    assert not (new_data / "retained.txt").exists()
    assert (
        local_capability_tool_extensions(
            tmp_path / "core.sqlite3", ToolPolicy(allowed_tools=(old_grant,))
        )
        == {}
    )
    assert new_grant in local_capability_tool_extensions(
        tmp_path / "core.sqlite3", ToolPolicy(allowed_tools=(new_grant,))
    )
    with pytest.raises(PermissionError, match="installation changed"):
        asyncio.run(previous_extension.execute({"outside_path": "test", "port": 8765}))


def test_external_tool_agent_action_uses_bounded_capability_and_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _package(tmp_path / "source", "print('{}')\n")
    registry = ExternalToolRegistry(tmp_path / "external-tools")
    record = registry.install(source)
    granted_name = record.granted_name("ext_sample_tool_check")

    def admitted(self: SandboxProbe, **_: object) -> SandboxEvidence:
        return SandboxEvidence(
            evidence_ref="sandbox-test", profile="(version 1)", runner="/bin/false"
        )

    monkeypatch.setattr(SandboxProbe, "check", admitted)
    registry.set_enabled("sample_tool", True)
    store = SQLiteStore(tmp_path / "core.sqlite3")
    store.initialize()
    profile = store.add_model_profile(
        ModelProfile(
            name="External test model",
            model_id="test-model",
            base_url="https://example.invalid/v1",
            secret_ref="OPERANT_TEST_KEY",
        )
    )
    role = store.create_role(
        RolePreset(
            name="External Tool role",
            system_prompt="Use the explicitly granted Tool.",
            model_profile_id=profile.id,
            tool_policy=ToolPolicy(allowed_tools=(granted_name,)),
        )
    )
    session = AgentFactory(store).create_session(role.id)
    agent = store.create_agent(session.id)
    extensions = local_capability_tool_extensions(store.path, role.tool_policy)
    tools = WorkspaceTools(tmp_path, policy=role.tool_policy, extensions=extensions)
    assert [item.name for item in tools.definitions()] == [granted_name]
    gateway = _PersistentActionGateway(
        store=store, session_id=session.id, agent_id=agent.id, tools=tools
    )
    claim = gateway.reserve_tool_action(
        tool_call_id="external-tool-check",
        name=granted_name,
        arguments={"outside_path": "private phrase", "port": 8765},
    )
    action, _ = gateway._security_claims[claim.receipt_id]
    assert action.requested_capabilities == (Capability.PROCESS_EXEC_NO_NETWORK,)
    assert action.normalized_arguments["plugin_id"] == "sample_tool"
    assert len(action.normalized_arguments["input_sha256"]) == 64
    assert "private phrase" not in json.dumps(action.model_dump(mode="json"))


@pytest.mark.asyncio
async def test_real_external_tool_cannot_read_outside_or_use_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.environ.get("OPERANT_EXTERNAL_TOOL_TEST") != "1":
        pytest.skip("set OPERANT_EXTERNAL_TOOL_TEST=1 for real sandbox acceptance")
    if sys.platform != "darwin":
        pytest.skip("macOS sandbox-exec is required")
    script = """import json, os, socket, sys
request = json.loads(sys.stdin.buffer.read())
outside = request['arguments']['outside_path']
port = request['arguments']['port']
try:
    open(outside, 'rb').read()
    file_denied = False
except OSError:
    file_denied = True
try:
    connection = socket.create_connection(('127.0.0.1', port), timeout=0.5)
    connection.close()
    network_denied = False
except OSError:
    network_denied = True
print(json.dumps({'result': {
    'file_denied': file_denied,
    'network_denied': network_denied,
    'secret_absent': 'OPERANT_TEST_SECRET' not in os.environ,
    'api_version': request['host_api_version'],
}}))
"""
    source = _package(tmp_path / "source", script)
    outside = tmp_path / "outside-private"
    outside.write_text("must stay outside the plugin")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    monkeypatch.setenv("OPERANT_TEST_SECRET", "private-test-value")
    registry = ExternalToolRegistry(tmp_path / "external-tools")
    try:
        record = registry.install(source)
        granted_name = record.granted_name("ext_sample_tool_check")
        assert record.state == "disabled"
        assert registry.set_enabled("sample_tool", True).state == "enabled"
        granted = local_capability_tool_extensions(
            tmp_path / "core.sqlite3", ToolPolicy(allowed_tools=(granted_name,))
        )
        result = await granted[granted_name].execute(
            {"outside_path": str(outside), "port": listener.getsockname()[1]}
        )
        assert result == {
            "plugin_id": "sample_tool",
            "trust": "untrusted",
            "result": {
                "file_denied": True,
                "network_denied": True,
                "secret_absent": True,
                "api_version": "operant-tool-extension.v1",
            },
        }
        registry.set_enabled("sample_tool", False)
        with pytest.raises(PermissionError, match="disabled"):
            await granted[granted_name].execute(
                {"outside_path": str(outside), "port": listener.getsockname()[1]}
            )
        registry.uninstall("sample_tool")
        assert not (tmp_path / "external-tools" / "packages" / "sample_tool").exists()
    finally:
        listener.close()
