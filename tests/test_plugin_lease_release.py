from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path

import pytest

from operant.contracts.b2_1 import Certification, PluginManifest, RunScope
from operant.domain.models import utc_now
from operant.plugins import PluginHost, PluginRegistry, compute_package_digest


def _package(root: Path) -> tuple[Path, PluginManifest]:
    package = root / "package"
    package.mkdir(parents=True)
    dependencies = b'{"stdlib_only":true}\n'
    permissions = b'{"capabilities":[]}\n'
    (package / "dependencies.json").write_bytes(dependencies)
    (package / "permissions.json").write_bytes(permissions)
    manifest = PluginManifest(
        plugin_id="example.plugin",
        plugin_version="1.0.0",
        sdk_version="operant-memory-sdk.v1",
        host_api_versions=("operant-memory-sdk.v1",),
        package_digest=compute_package_digest(package),
        dependencies_digest=hashlib.sha256(dependencies).hexdigest(),
        permissions_digest=hashlib.sha256(permissions).hexdigest(),
        entrypoint="entry",
        config_schema_ref="config.v1",
        config_schema_digest="a" * 64,
        state_schema_version="state.v1",
        capabilities=("recall", "extract", "maintain", "on_index_event"),
        memory_mb=64,
        max_rpc_bytes=128_000,
        max_concurrency=2,
        export_supported=True,
        import_supported=True,
        recoverable=True,
    )
    return package, manifest


def _certification(manifest: PluginManifest) -> Certification:
    now = utc_now()
    return Certification(
        certification_id="cert.example.v1",
        issuer_id="issuer.local",
        plugin_id=manifest.plugin_id,
        plugin_version=manifest.plugin_version,
        package_digest=manifest.package_digest,
        dependencies_digest=manifest.dependencies_digest,
        permissions_digest=manifest.permissions_digest,
        lifecycle_evidence_digest="b" * 64,
        allowed_modes=("trusted_in_process", "isolated"),
        valid_from=now - timedelta(minutes=1),
        expires_at=now + timedelta(hours=1),
        revocation_epoch=0,
        state="valid",
        signature_ref="signature.example",
    )


def _scope(run_id: str) -> RunScope:
    return RunScope(
        kind="run",
        project_id="project-1",
        workspace_id="workspace-1",
        run_id=run_id,
        writer_id=None,
    )


def _enabled_registry(tmp_path: Path) -> tuple[PluginRegistry, str]:
    package, manifest = _package(tmp_path / "source")
    registry = PluginRegistry(tmp_path / "managed", trusted_issuers={"issuer.local"})
    installation = registry.install(
        manifest,
        package,
        certification=_certification(manifest),
    )
    binding = registry.bind(installation.installation_id)
    registry.enable(binding.binding_id)
    return registry, binding.binding_id


def test_release_run_clears_its_marker_and_is_idempotent(tmp_path: Path) -> None:
    registry, binding_id = _enabled_registry(tmp_path)
    lease = registry.create_run(binding_id, run_id="run-1", scope=_scope("run-1"))

    released = registry.release_run(lease.lease_id, status="completed")
    assert released.status == "completed"
    assert registry.get_binding(binding_id).active_run_ids == ()

    retried = registry.release_run(lease.lease_id, status="cancelled")
    assert retried == released
    assert registry.get_run(lease.lease_id).status == "completed"
    assert registry.get_binding(binding_id).active_run_ids == ()
    registry.close()


def test_late_release_cannot_clear_reused_run_marker(tmp_path: Path) -> None:
    registry, binding_id = _enabled_registry(tmp_path)
    old_lease = registry.create_run(binding_id, run_id="run-reused", scope=_scope("run-reused"))
    registry.release_run(old_lease.lease_id)
    new_lease = registry.create_run(binding_id, run_id="run-reused", scope=_scope("run-reused"))

    late = registry.release_run(old_lease.lease_id, status="expired")
    assert late.status == "completed"
    assert registry.get_run(new_lease.lease_id).status == "active"
    assert registry.get_binding(binding_id).active_run_ids == ("run-reused",)
    registry.close()


@pytest.mark.asyncio
async def test_stop_and_uninstall_stay_blocked_by_reused_run_after_late_release(
    tmp_path: Path,
) -> None:
    registry, binding_id = _enabled_registry(tmp_path)
    installation_id = registry.get_binding(binding_id).installation_id
    old_lease = registry.create_run(binding_id, run_id="run-reused", scope=_scope("run-reused"))
    registry.release_run(old_lease.lease_id)
    registry.create_run(binding_id, run_id="run-reused", scope=_scope("run-reused"))
    registry.release_run(old_lease.lease_id)
    host = PluginHost(registry)

    stopped = await host.stop(installation_id)
    assert stopped.state == "blocked"
    assert stopped.cleanup[0].reason == "active_run"
    assert stopped.cleanup[0].blocker_ids == ("run-reused",)

    uninstalled = await host.uninstall(installation_id, data_policy="delete")
    assert uninstalled.state == "blocked"
    assert uninstalled.cleanup[0].reason == "active_run"
    assert uninstalled.cleanup[0].blocker_ids == ("run-reused",)
    await host.close()
