from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import timedelta
from pathlib import Path
from typing import Any, NoReturn

import pytest

from operant.contracts.b2_1 import (
    CandidateBatch,
    Certification,
    PluginManifest,
    RecallRequest,
    RpcContext,
    RunScope,
)
from operant.domain.models import utc_now
from operant.plugins import (
    CleanupPlanRecord,
    HostBudget,
    PluginError,
    PluginHost,
    PluginRegistry,
    RunLease,
)
from operant.plugins.protocol import RestrictedHostApi, compute_package_digest, path_under


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


def _recall(lease: RunLease, *, request_id: str = "request-1") -> RecallRequest:
    context = RpcContext(
        sdk_version="operant-memory-sdk.v1",
        request_id=request_id,
        installation_id=lease.installation_id,
        dataset_id=lease.dataset_id,
        scope=lease.scope,
        deadline=utc_now() + timedelta(seconds=30),
        cancel_token=f"cancel-{request_id}",
        idempotency_key=f"idem-{request_id}",
        request_digest="c" * 64,
        binding_epoch=lease.binding_epoch,
        permission_epoch=lease.permission_epoch,
        lease_fencing=lease.lease_fencing,
    )
    return RecallRequest(
        context=context,
        query="hello",
        explicit_refs=(),
        knowledge_cutoff="0",
        max_candidates=3,
        token_budget=0,  # Empty recall fixture requests no context allocation.
    )


def _registry(tmp_path: Path) -> tuple[PluginRegistry, str]:
    package, manifest = _package(tmp_path / "source")
    registry = PluginRegistry(tmp_path / "managed", trusted_issuers={"issuer.local"})
    installation = registry.install(
        manifest,
        package,
        certification=_certification(manifest),
    )
    binding = registry.bind(installation.installation_id)
    registry.enable(binding.binding_id)
    return registry, installation.installation_id


def test_cleanup_pins_directory_descriptor_for_file_after_parent_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, installation_id = _registry(tmp_path)
    relative_path = "data/sub/owned.txt"
    registry.create_resource(
        installation_id,
        relative_path=relative_path,
        category="record",
        path_kind="file",
        reconstructible=False,
    )
    root = registry.installation_root(installation_id)
    owned = root / "data/sub/owned.txt"
    owned.write_text("owned", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "owned.txt"
    sentinel.write_text("sentinel", encoding="utf-8")
    checked = path_under(root, relative_path)
    parent = root / "data/sub"
    original_open = os.open
    swapped = False

    def raced_open(
        path: str | bytes, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        nonlocal swapped
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if path == "sub" and dir_fd is not None and not swapped:
            swapped = True
            parent.rename(root / "data/saved")
            parent.symlink_to(outside, target_is_directory=True)
        return descriptor

    monkeypatch.setattr(os, "open", raced_open)
    PluginHost._remove_owned_path(checked, "file")

    assert swapped
    assert not (root / "data/saved/owned.txt").exists()
    assert sentinel.read_text(encoding="utf-8") == "sentinel"
    registry.close()


def test_cleanup_removes_directory_tree_without_following_symlinks(tmp_path: Path) -> None:
    registry, installation_id = _registry(tmp_path)
    relative_path = "data/tree"
    registry.create_resource(
        installation_id,
        relative_path=relative_path,
        category="state",
        path_kind="directory",
        reconstructible=False,
    )
    root = registry.installation_root(installation_id)
    tree = root / relative_path
    nested = tree / "nested"
    nested.mkdir()
    (nested / "value").write_text("owned", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("sentinel", encoding="utf-8")
    (tree / "link").symlink_to(outside, target_is_directory=True)

    PluginHost._remove_owned_path(path_under(root, relative_path), "directory")

    assert not tree.exists()
    assert sentinel.read_text(encoding="utf-8") == "sentinel"
    registry.close()


@pytest.mark.asyncio
async def test_cleanup_path_swap_returns_failure_receipt_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, installation_id = _registry(tmp_path)
    relative_path = "data/sub/owned.txt"
    registry.create_resource(
        installation_id,
        relative_path=relative_path,
        category="record",
        path_kind="file",
        reconstructible=False,
    )
    root = registry.installation_root(installation_id)
    (root / relative_path).write_text("owned", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("sentinel", encoding="utf-8")
    data = root / "data"
    host = PluginHost(registry)
    original_apply = host._apply_cleanup
    swapped = False

    async def raced_apply(plan: CleanupPlanRecord) -> CleanupPlanRecord:
        nonlocal swapped
        if not swapped:
            swapped = True
            data.rename(root / "data-saved")
            data.symlink_to(outside, target_is_directory=True)
        return await original_apply(plan)

    monkeypatch.setattr(host, "_apply_cleanup", raced_apply)

    failed = await host.uninstall(installation_id, data_policy="delete")
    assert failed.state == "blocked"
    assert failed.ack == "failed"
    assert any(
        item.outcome == "blocked" and item.reason == "directory_identity_changed"
        for item in failed.cleanup
    )
    assert registry.cleanup_plan(failed.operation_id).state == "blocked"
    binding = registry.get_binding(registry.get_installation(installation_id).binding_id or "")
    assert not binding.enabled
    with pytest.raises(PluginError):
        host.start_run(binding.binding_id, run_id="new-run", scope=_scope("new-run"))
    assert sentinel.read_text(encoding="utf-8") == "sentinel"

    data.unlink()
    (root / "data-saved").rename(data)
    resumed = await host.resume_cleanup()
    assert len(resumed) == 1
    assert resumed[0].state == "uninstalled"
    assert registry.cleanup_plan(failed.operation_id).state == "completed"
    await host.close()


async def _running_host(
    tmp_path: Path,
    implementation: Any,
    *,
    budget: HostBudget | None = None,
) -> tuple[PluginHost, PluginRegistry, Any, RunLease]:
    package, manifest = _package(tmp_path / "source")
    registry = PluginRegistry(tmp_path / "managed", trusted_issuers={"issuer.local"})
    installation = registry.install(
        manifest,
        package,
        certification=_certification(manifest),
    )
    host = PluginHost(
        registry,
        plugin_factories={manifest.plugin_id: lambda _record: implementation},
        budget=budget,
    )
    binding = host.bind(installation.installation_id)
    host.enable(binding.binding_id)
    await host.start(installation.installation_id, mode="trusted_in_process")
    lease = host.start_run(binding.binding_id, run_id="run-1", scope=_scope("run-1"))
    return host, registry, installation, lease


class _CooperativePlugin:
    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def handle(
        self, _operation: str, _request: RecallRequest, host: RestrictedHostApi
    ) -> NoReturn:
        self.entered.set()
        while True:
            host.check_cancelled()
            await asyncio.sleep(0)


class _StubbornCleanupPlugin:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()

    async def handle(
        self, _operation: str, request: RecallRequest, _host: RestrictedHostApi
    ) -> CandidateBatch:
        return CandidateBatch(request_id=request.context.request_id, candidates=())

    async def cleanup(self) -> None:
        self.entered.set()
        await self.release.wait()
        self.finished.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["stop", "uninstall"])
async def test_stop_and_uninstall_wait_for_cooperative_inflight_call(
    tmp_path: Path, operation: str
) -> None:
    plugin = _CooperativePlugin()
    host, registry, installation, lease = await _running_host(tmp_path, plugin)
    invoke_task = asyncio.create_task(host.invoke(lease, "recall", _recall(lease)))
    await plugin.entered.wait()

    if operation == "stop":
        receipt = await host.stop(installation.installation_id, stop_run_ids=(lease.run_id,))
    else:
        receipt = await host.uninstall(
            installation.installation_id,
            data_policy="delete",
            stop_run_ids=(lease.run_id,),
        )

    assert receipt.state in {"disabled", "uninstalled"}
    with pytest.raises(PluginError) as error:
        await invoke_task
    assert error.value.code in {"stale_epoch", "cancelled"}
    assert not host._calls
    await host.close()


@pytest.mark.asyncio
async def test_uninstall_fails_closed_when_trusted_cleanup_hook_will_not_quiesce(
    tmp_path: Path,
) -> None:
    plugin = _StubbornCleanupPlugin()
    host, registry, installation, lease = await _running_host(tmp_path, plugin)

    receipt = await host.uninstall(
        installation.installation_id,
        data_policy="delete",
        stop_run_ids=(lease.run_id,),
    )

    assert receipt.state == "restart_required"
    assert receipt.ack == "failed"
    assert plugin.entered.is_set()
    assert registry.get_installation(installation.installation_id).state == "failed"
    assert installation.installation_id in host._engines
    assert registry.installation_root(installation.installation_id).exists()

    plugin.release.set()
    await asyncio.wait_for(plugin.finished.wait(), 1)
    await host.close()


class _StubbornPlugin:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def handle(
        self, _operation: str, request: RecallRequest, _host: RestrictedHostApi
    ) -> CandidateBatch:
        self.entered.set()
        while not self.release.is_set():
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                # Model a trusted implementation that ignores task
                # cancellation; lifecycle must keep the call fenced.
                continue
        return CandidateBatch(request_id=request.context.request_id, candidates=())


@pytest.mark.asyncio
async def test_invoke_timeout_keeps_uncooperative_call_tracked_until_it_finishes(
    tmp_path: Path,
) -> None:
    plugin = _StubbornPlugin()
    host, _registry, _installation, lease = await _running_host(
        tmp_path,
        plugin,
        budget=HostBudget(timeout_seconds=0.01),
    )
    invoke_task = asyncio.create_task(host.invoke(lease, "recall", _recall(lease)))
    await plugin.entered.wait()

    with pytest.raises(PluginError) as error:
        await invoke_task
    assert error.value.code == "restart_required"
    assert host._calls

    plugin.release.set()
    while host._calls:
        await asyncio.sleep(0)
    await host.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["stop", "uninstall"])
async def test_stop_and_uninstall_fail_closed_when_inflight_call_will_not_quiesce(
    tmp_path: Path, operation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = _StubbornPlugin()
    host, registry, installation, lease = await _running_host(tmp_path, plugin)
    invoke_task = asyncio.create_task(host.invoke(lease, "recall", _recall(lease)))
    await plugin.entered.wait()

    quiesce_entered = asyncio.Event()
    original_quiesce = host._quiesce_calls

    async def observed_quiesce(target: str) -> bool:
        quiesce_entered.set()
        return await original_quiesce(target)

    monkeypatch.setattr(host, "_quiesce_calls", observed_quiesce)
    if operation == "stop":
        lifecycle_task = asyncio.create_task(
            host.stop(installation.installation_id, stop_run_ids=(lease.run_id,))
        )
    else:
        lifecycle_task = asyncio.create_task(
            host.uninstall(
                installation.installation_id,
                data_policy="delete",
                stop_run_ids=(lease.run_id,),
            )
        )
    await quiesce_entered.wait()
    binding_id = registry.get_installation(installation.installation_id).binding_id
    assert binding_id is not None
    with pytest.raises(PluginError) as start_error:
        host.start_run(binding_id, run_id="run-2", scope=_scope("run-2"))
    assert start_error.value.code == "stale_epoch"
    receipt = await lifecycle_task

    assert receipt.state == "restart_required"
    assert receipt.ack == "failed"
    assert not invoke_task.done()
    assert registry.get_installation(installation.installation_id).state in {
        "failed",
        "restart_required",
    }
    assert installation.installation_id in host._engines

    plugin.release.set()
    with pytest.raises(PluginError) as error:
        await invoke_task
    assert error.value.code in {"stale_epoch", "cancelled"}
    await host.close()
