from __future__ import annotations

import asyncio
import hashlib
from datetime import timedelta
from pathlib import Path

import pytest

from operant.contracts.b2_1 import (
    CandidateBatch,
    Certification,
    PluginManifest,
    PrivateIndexRequest,
    RpcContext,
    RunScope,
)
from operant.domain.models import utc_now
from operant.plugins import (
    HostBudget,
    HostCallbacks,
    IsolationUnavailableError,
    PluginError,
    PluginHost,
    PluginRegistry,
    SandboxProbe,
    compute_package_digest,
)


def _package(root: Path, *, entrypoint: bool = False) -> tuple[Path, PluginManifest]:
    package = root / "package"
    package.mkdir(parents=True)
    dependencies = b'{"stdlib_only":true}\n'
    permissions = b'{"capabilities":[]}\n'
    (package / "dependencies.json").write_bytes(dependencies)
    (package / "permissions.json").write_bytes(permissions)
    if entrypoint:
        (package / "plugin.py").write_text(
            "from operant.contracts.b2_1 import CandidateBatch\n"
            "class PackagePlugin:\n"
            "    def handle(self, operation, request, host):\n"
            "        return CandidateBatch(request_id=request.context.request_id, candidates=())\n"
            "def create_plugin():\n"
            "    return PackagePlugin()\n",
            encoding="utf-8",
        )
    manifest = PluginManifest(
        plugin_id="example.plugin",
        plugin_version="1.0.0",
        sdk_version="operant-memory-sdk.v1",
        host_api_versions=("operant-memory-sdk.v1",),
        package_digest=compute_package_digest(package),
        dependencies_digest=hashlib.sha256(dependencies).hexdigest(),
        permissions_digest=hashlib.sha256(permissions).hexdigest(),
        entrypoint="plugin.py" if entrypoint else "entry",
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


def _cert(manifest: PluginManifest) -> Certification:
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


def _scope(run_id: str = "run-1") -> RunScope:
    return RunScope(
        kind="run",
        project_id="project-1",
        workspace_id="workspace-1",
        run_id=run_id,
        writer_id=None,
    )


def _recall(lease, *, request_id: str = "request-1") -> object:
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
    from operant.contracts.b2_1 import RecallRequest

    return RecallRequest(
        context=context,
        query="hello",
        explicit_refs=(),
        knowledge_cutoff="0",
        max_candidates=3,
        token_budget=0,  # Empty recall fixture requests no context allocation.
    )


class CallbackPlugin:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def handle(self, operation, request, host):
        self.calls.append(operation)
        host.check_cancelled()
        if operation == "recall":
            result = await host.search(request)
            assert result.request_id == request.context.request_id
            return result
        if operation == "extract":
            from operant.contracts.b2_1 import ProposalBatch

            return ProposalBatch(
                request_id=request.context.request_id,
                proposals=(),
                source_watermark=request.source_watermark,
            )
        raise ValueError(operation)


@pytest.mark.asyncio
async def test_install_bind_enable_and_restricted_callback_without_engine_before_enable(
    tmp_path: Path,
) -> None:
    package, manifest = _package(tmp_path)
    registry = PluginRegistry(tmp_path / "managed", trusted_issuers={"issuer.local"})
    certification = _cert(manifest)
    installation = registry.install(manifest, package, certification=certification)
    plugin = CallbackPlugin()
    called: list[str] = []

    def search(request):
        called.append(request.query)
        return CandidateBatch(request_id=request.context.request_id, candidates=())

    host = PluginHost(
        registry,
        plugin_factories={manifest.plugin_id: lambda _record: plugin},
        callbacks=HostCallbacks(search=search),
    )
    binding = host.bind(installation.installation_id)
    with pytest.raises(PluginError, match="explicitly enabled"):
        await host.start(installation.installation_id, mode="trusted_in_process")
    host.enable(binding.binding_id)
    admission = await host.start(installation.installation_id, mode="trusted_in_process")
    assert admission.certification_id == certification.certification_id
    lease = host.start_run(binding.binding_id, run_id="run-1", scope=_scope())
    result = await host.invoke(lease, "recall", _recall(lease))
    assert isinstance(result, CandidateBatch)
    assert called == ["hello"]
    assert plugin.calls == ["recall"]
    await host.close()


@pytest.mark.asyncio
async def test_trusted_mode_loads_verified_package_entrypoint(tmp_path: Path) -> None:
    package, manifest = _package(tmp_path, entrypoint=True)
    registry = PluginRegistry(tmp_path / "managed", trusted_issuers={"issuer.local"})
    installation = registry.install(manifest, package, certification=_cert(manifest))
    host = PluginHost(registry)
    binding = host.bind(installation.installation_id)
    host.enable(binding.binding_id)
    await host.start(installation.installation_id, mode="trusted_in_process")
    lease = host.start_run(binding.binding_id, run_id="run-1", scope=_scope())
    result = await host.invoke(lease, "recall", _recall(lease))
    assert isinstance(result, CandidateBatch)
    await host.close()


@pytest.mark.asyncio
async def test_private_index_is_host_owned_and_cas_fenced(tmp_path: Path) -> None:
    package, manifest = _package(tmp_path)
    registry = PluginRegistry(tmp_path / "managed", trusted_issuers={"issuer.local"})
    installation = registry.install(manifest, package, certification=_cert(manifest))

    class IndexPlugin:
        async def handle(self, operation, request, host):
            resource = host.register_resource(relative_path="data/example.idx")
            write = PrivateIndexRequest(
                context=request.context,
                resource=resource,
                operation="replace",
                expected_revision=0,
                content_digest=hashlib.sha256(b"owned").hexdigest(),
                payload="owned",
            )
            stored = host.private_index(write)
            assert stored.revision == 1
            return CandidateBatch(request_id=request.context.request_id, candidates=())

    host = PluginHost(
        registry,
        plugin_factories={manifest.plugin_id: lambda _record: IndexPlugin()},
    )
    binding = host.bind(installation.installation_id)
    host.enable(binding.binding_id)
    await host.start(installation.installation_id, mode="trusted_in_process")
    lease = host.start_run(binding.binding_id, run_id="run-1", scope=_scope())
    await host.invoke(lease, "recall", _recall(lease))
    resource = next(
        item
        for item in registry.resources_for(installation.installation_id)
        if item.relative_path == "data/example.idx"
    )
    assert (
        registry.installation_root(installation.installation_id) / resource.relative_path
    ).read_text() == "owned"
    await host.close()


@pytest.mark.asyncio
async def test_disable_fences_old_epoch_and_close_cancels_leases(tmp_path: Path) -> None:
    package, manifest = _package(tmp_path)
    registry = PluginRegistry(tmp_path / "managed", trusted_issuers={"issuer.local"})
    installation = registry.install(manifest, package, certification=_cert(manifest))
    host = PluginHost(
        registry, plugin_factories={manifest.plugin_id: lambda _record: CallbackPlugin()}
    )
    binding = host.bind(installation.installation_id)
    host.enable(binding.binding_id)
    await host.start(installation.installation_id, mode="trusted_in_process")
    lease = host.start_run(binding.binding_id, run_id="run-1", scope=_scope())
    blocked = await host.disable(binding.binding_id)
    assert blocked.state == "blocked"
    receipt = await host.disable(binding.binding_id, stop_run_ids=("run-1",))
    assert receipt.state == "disabled"
    with pytest.raises(PluginError, match="no longer active"):
        await host.invoke(lease, "recall", _recall(lease))
    await host.close()


@pytest.mark.asyncio
async def test_inflight_result_is_rejected_after_certification_revocation(tmp_path: Path) -> None:
    package, manifest = _package(tmp_path)
    certification = _cert(manifest)
    registry = PluginRegistry(tmp_path / "managed", trusted_issuers={"issuer.local"})
    installation = registry.install(manifest, package, certification=certification)
    entered = asyncio.Event()
    release = asyncio.Event()

    class SlowPlugin:
        async def handle(self, operation, request, host):
            entered.set()
            await release.wait()
            from operant.contracts.b2_1 import CandidateBatch

            return CandidateBatch(request_id=request.context.request_id, candidates=())

    host = PluginHost(registry, plugin_factories={manifest.plugin_id: lambda _record: SlowPlugin()})
    binding = host.bind(installation.installation_id)
    host.enable(binding.binding_id)
    await host.start(installation.installation_id, mode="trusted_in_process")
    lease = host.start_run(binding.binding_id, run_id="run-1", scope=_scope())
    pending = asyncio.create_task(host.invoke(lease, "recall", _recall(lease)))
    await entered.wait()
    registry.revoke_certification(certification.certification_id)
    release.set()
    with pytest.raises(PluginError, match="stale|disabled"):
        await pending
    await host.close()


@pytest.mark.asyncio
async def test_timeout_cancellation_and_log_budget_fail_closed(tmp_path: Path) -> None:
    package, manifest = _package(tmp_path)
    registry = PluginRegistry(tmp_path / "managed", trusted_issuers={"issuer.local"})
    installation = registry.install(manifest, package, certification=_cert(manifest))
    entered = asyncio.Event()

    class SlowPlugin:
        async def handle(self, operation, request, host):
            entered.set()
            host.log("this log is bounded")
            await asyncio.sleep(1)
            return CandidateBatch(request_id=request.context.request_id, candidates=())

    host = PluginHost(
        registry,
        plugin_factories={manifest.plugin_id: lambda _record: SlowPlugin()},
        budget=HostBudget(timeout_seconds=0.05, max_log_chars=5),
    )
    binding = host.bind(installation.installation_id)
    host.enable(binding.binding_id)
    await host.start(installation.installation_id, mode="trusted_in_process")
    lease = host.start_run(binding.binding_id, run_id="run-1", scope=_scope())
    with pytest.raises(PluginError, match="deadline|cancelled"):
        await host.invoke(lease, "recall", _recall(lease))
    assert entered.is_set()
    await host.close()


def test_certification_is_issuer_bound_and_package_tamper_is_rejected(tmp_path: Path) -> None:
    package, manifest = _package(tmp_path)
    registry = PluginRegistry(tmp_path / "managed", trusted_issuers={"issuer.local"})
    with pytest.raises(PluginError, match="issuer"):
        registry.register_certification(_cert(manifest).model_copy(update={"issuer_id": "self"}))
    installation = registry.install(manifest, package)
    (registry.package_path(installation.installation_id) / "permissions.json").write_text(
        "tampered", encoding="utf-8"
    )
    with pytest.raises(PluginError, match="digest"):
        registry.verify_package(installation.installation_id)
    registry.close()


def test_registry_restart_does_not_auto_instantiate_or_reinstall(tmp_path: Path) -> None:
    package, manifest = _package(tmp_path)
    managed = tmp_path / "managed"
    first = PluginRegistry(managed)
    installation = first.install(manifest, package)
    first.close()
    second = PluginRegistry(managed)
    assert second.list_installations()[0].installation_id == installation.installation_id
    assert second.list_bindings() == ()
    second.close()


def test_registry_recovery_fences_persisted_active_run(tmp_path: Path) -> None:
    package, manifest = _package(tmp_path)
    managed = tmp_path / "managed"
    first = PluginRegistry(managed)
    installation = first.install(manifest, package)
    binding = first.bind(installation.installation_id)
    first.enable(binding.binding_id)
    lease = first.create_run(binding.binding_id, run_id="run-crash", scope=_scope("run-crash"))
    assert lease.status == "active"
    # Simulate a process crash: the OS releases the lock without the Host's
    # normal close path, leaving a durable active lease behind.
    handle = first._lock_handle
    first._lock_handle = None
    assert handle is not None
    handle.close()
    recovered = PluginRegistry(managed)
    recovered_lease = recovered.get_run(lease.lease_id)
    assert recovered_lease.status == "expired"
    assert recovered.get_binding(binding.binding_id).binding_epoch == lease.binding_epoch + 1
    recovered.close()


@pytest.mark.asyncio
async def test_delete_uninstall_uses_host_inventory_without_cleanup_hook(tmp_path: Path) -> None:
    package, manifest = _package(tmp_path)
    registry = PluginRegistry(tmp_path / "managed", trusted_issuers={"issuer.local"})
    installation = registry.install(manifest, package, certification=_cert(manifest))
    resource = registry.create_resource(
        installation.installation_id,
        relative_path="data/fact.json",
        category="record",
        path_kind="file",
        reconstructible=False,
    )
    resource_path = registry.installation_root(installation.installation_id) / "data/fact.json"
    resource_path.write_text("owned", encoding="utf-8")
    assert resource.resource_id
    host = PluginHost(registry)
    binding = host.bind(installation.installation_id)
    host.enable(binding.binding_id)
    receipt = await host.uninstall(installation.installation_id, data_policy="delete")
    assert receipt.state == "uninstalled"
    assert not resource_path.exists()
    assert registry.state.datasets[0].state == "deleted"
    await host.close()


@pytest.mark.asyncio
async def test_cleanup_plan_resumes_after_interruption_and_keep_retains_data(
    tmp_path: Path,
) -> None:
    package, manifest = _package(tmp_path)
    managed = tmp_path / "managed"
    first = PluginRegistry(managed)
    installation = first.install(manifest, package)
    plan = first.begin_uninstall(installation.installation_id, data_policy="delete")
    # Leave all items pending, release the writer as if the process crashed.
    handle = first._lock_handle
    first._lock_handle = None
    assert handle is not None
    handle.close()
    recovered = PluginRegistry(managed)
    host = PluginHost(recovered)
    receipts = await host.resume_cleanup()
    assert [item.operation_id for item in receipts] == [plan.operation_id]
    assert recovered.cleanup_plan(plan.operation_id).state == "completed"
    await host.close()

    package2, manifest2 = _package(tmp_path / "keep-source")
    keep_registry = PluginRegistry(tmp_path / "keep-managed")
    keep_installation = keep_registry.install(manifest2, package2)
    keep_path = (
        keep_registry.installation_root(keep_installation.installation_id) / "data/keep.json"
    )
    keep_registry.create_resource(
        keep_installation.installation_id,
        relative_path="data/keep.json",
        category="record",
        path_kind="file",
        reconstructible=False,
    )
    keep_path.write_text("keep", encoding="utf-8")
    keep_host = PluginHost(keep_registry)
    keep_receipt = await keep_host.uninstall(keep_installation.installation_id, data_policy="keep")
    assert keep_receipt.state == "uninstalled"
    assert keep_path.read_text(encoding="utf-8") == "keep"
    assert keep_registry.state.datasets[0].state == "retained"
    await keep_host.close()


@pytest.mark.asyncio
async def test_untrusted_stdio_never_falls_back_when_sandbox_is_unavailable(tmp_path: Path) -> None:
    package, manifest = _package(tmp_path, entrypoint=True)
    registry = PluginRegistry(tmp_path / "managed")
    installation = registry.install(manifest, package)

    class RejectingProbe(SandboxProbe):
        def check(self, **kwargs):
            raise IsolationUnavailableError()

    host = PluginHost(registry, sandbox_probe=RejectingProbe())
    binding = host.bind(installation.installation_id)
    host.enable(binding.binding_id)
    with pytest.raises(PluginError, match="isolation"):
        await host.start(installation.installation_id, mode="isolated")
    assert host._engines == {}
    await host.close()


@pytest.mark.asyncio
async def test_inprocess_concurrency_is_admitted_before_plugin_execution(tmp_path: Path) -> None:
    import asyncio

    from operant.plugins import HostBudget

    package, manifest = _package(tmp_path)
    registry = PluginRegistry(tmp_path / "managed", trusted_issuers={"issuer.local"})
    installation = registry.install(manifest, package, certification=_cert(manifest))
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowPlugin:
        async def handle(self, operation, request, host):
            entered.set()
            await release.wait()
            return CandidateBatch(request_id=request.context.request_id, candidates=())

    host = PluginHost(
        registry,
        plugin_factories={manifest.plugin_id: lambda _: SlowPlugin()},
        budget=HostBudget(max_concurrency=1),
    )
    binding = host.bind(installation.installation_id)
    host.enable(binding.binding_id)
    await host.start(installation.installation_id, mode="trusted_in_process")
    lease = host.start_run(binding.binding_id, run_id="run-1", scope=_scope())
    first = asyncio.create_task(host.invoke(lease, "recall", _recall(lease)))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(PluginError) as error:
            await host.invoke(lease, "recall", _recall(lease, request_id="second"))
        assert error.value.code == "concurrency_limit"
    finally:
        release.set()
        await first
        await host.close()


@pytest.mark.asyncio
async def test_active_host_rejects_forged_scope_lease_and_expired_certification(
    tmp_path: Path, monkeypatch
) -> None:
    package, manifest = _package(tmp_path, entrypoint=True)
    registry = PluginRegistry(tmp_path / "managed", trusted_issuers={"issuer.local"})
    certification = _cert(manifest)
    installation = registry.install(manifest, package, certification=certification)
    host = PluginHost(registry)
    binding = host.bind(installation.installation_id)
    host.enable(binding.binding_id)
    await host.start(installation.installation_id, mode="trusted_in_process")
    lease = host.start_run(binding.binding_id, run_id="run-1", scope=_scope())
    request = _recall(lease)
    forged_context = request.context.model_copy(update={"scope": _scope("other-run")})
    with pytest.raises(PluginError, match="stale"):
        await host.invoke(lease, "recall", request.model_copy(update={"context": forged_context}))
    forged_lease = lease.model_copy(update={"scope": _scope("other-run")})
    with pytest.raises(PluginError, match="stale"):
        await host.invoke(forged_lease, "recall", _recall(forged_lease))
    active_api = host._host_api(lease, request.context, HostBudget())
    monkeypatch.setattr("operant.plugins.registry.utc_now", lambda: certification.expires_at)
    # Keep lease live at the controlled certification deadline, isolating this guard.
    monkeypatch.setattr(registry, "assert_lease", lambda *_args, **_kwargs: None)
    with pytest.raises(PluginError, match="current certification"):
        active_api.register_resource(relative_path="state/late.json")
    with pytest.raises(PluginError, match="current certification"):
        await host.invoke(lease, "recall", request)
    assert not (
        registry.installation_root(installation.installation_id) / "state/late.json"
    ).exists()
    # This test constructs the internal API without invoking an engine. Release
    # its synthetic call slot; real invocations retain their actual asyncio task.
    host._calls.pop(request.context.request_id)
    await host.close()


def test_reloaded_registry_requires_issuer_still_trusted(tmp_path: Path) -> None:
    package, manifest = _package(tmp_path)
    registry = PluginRegistry(tmp_path / "managed", trusted_issuers={"issuer.local"})
    installation = registry.install(manifest, package, certification=_cert(manifest))
    registry.close()
    reopened = PluginRegistry(tmp_path / "managed", trusted_issuers=())
    with pytest.raises(PluginError, match="current certification"):
        reopened.verify_certification(installation.installation_id, mode="trusted_in_process")
    reopened.close()


@pytest.mark.parametrize("operation", ["read", "write", "delete", "register"])
def test_managed_resource_rejects_parent_swap_after_path_validation(
    tmp_path: Path, operation: str
) -> None:
    from operant.plugins.protocol import (
        _delete_managed_file,
        _read_managed_file,
        _write_managed_file,
        ensure_managed_resource,
        path_under,
    )

    managed = tmp_path / "managed"
    parent = managed / "data" / "sub"
    parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "index"
    sentinel.write_text("sentinel")
    checked = path_under(managed, "data/sub/index")
    parent.rename(managed / "data" / "saved")
    parent.symlink_to(outside, target_is_directory=True)
    actions = {
        "read": lambda: _read_managed_file(checked),
        "write": lambda: _write_managed_file(checked, b"changed"),
        "delete": lambda: _delete_managed_file(checked),
        "register": lambda: ensure_managed_resource(checked),
    }
    with pytest.raises(PluginError):
        actions[operation]()
    assert sentinel.read_text() == "sentinel"


def test_managed_file_rejects_hardlink_before_truncation_and_bounds_reads(tmp_path: Path) -> None:
    import os

    from operant.plugins.protocol import _read_managed_file, _write_managed_file

    source = tmp_path / "source"
    source.write_text("unchanged")
    alias = tmp_path / "alias"
    os.link(source, alias)
    with pytest.raises(PluginError):
        _write_managed_file(alias, b"changed")
    assert source.read_text() == "unchanged"
    large = tmp_path / "large"
    large.write_bytes(b"x" * 100)
    with pytest.raises(PluginError) as error:
        _read_managed_file(large, max_bytes=10)
    assert error.value.code == "budget_exceeded"


def test_managed_write_keeps_opened_directory_after_concurrent_rename(
    tmp_path: Path, monkeypatch
) -> None:
    import os

    from operant.plugins.protocol import _write_managed_file, path_under

    managed = tmp_path / "managed"
    parent = managed / "data" / "sub"
    parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "index"
    sentinel.write_text("sentinel")
    checked = path_under(managed, "data/sub/index")
    original_open = os.open
    swapped = False

    def raced_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if path == "sub" and dir_fd is not None and not swapped:
            swapped = True
            parent.rename(managed / "data" / "saved")
            parent.symlink_to(outside, target_is_directory=True)
        return descriptor

    monkeypatch.setattr(os, "open", raced_open)
    _write_managed_file(checked, b"owned")
    assert swapped
    assert sentinel.read_text() == "sentinel"
    assert (managed / "data" / "saved" / "index").read_bytes() == b"owned"


def test_package_digest_cache_rechecks_same_size_restored_time_and_links(tmp_path: Path):
    import os

    package, manifest = _package(tmp_path, entrypoint=True)
    registry = PluginRegistry(tmp_path / "managed", trusted_issuers={"issuer.local"})
    installation = registry.install(manifest, package, certification=_cert(manifest))
    target = registry.package_path(installation.installation_id) / "plugin.py"
    original = target.read_bytes()
    before = target.stat()
    assert registry.verify_package(installation.installation_id) == manifest.package_digest
    assert registry.verify_package(installation.installation_id) == manifest.package_digest
    target.write_bytes(original.replace(b"PackagePlugin", b"ChangedPlugin"))
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(PluginError, match="digest changed"):
        registry.verify_package(installation.installation_id)
    target.write_bytes(original)
    registry.verify_package(installation.installation_id)
    replacement = target.with_suffix(".other")
    target.rename(replacement)
    target.symlink_to(replacement)
    with pytest.raises(PluginError, match="unsafe path"):
        registry.verify_package(installation.installation_id)


def test_package_signature_reuses_unchanged_tuple_and_rebuilds_after_first_difference(
    tmp_path: Path,
) -> None:
    package, _manifest = _package(tmp_path, entrypoint=True)

    initial = PluginRegistry._package_signature(package)
    assert PluginRegistry._package_signature(package, previous=initial) is initial

    target = package / "plugin.py"
    target.write_text(target.read_text(encoding="utf-8").replace("PackagePlugin", "ChangedPlugin"))
    updated = PluginRegistry._package_signature(package, previous=initial)

    assert updated is not initial
    assert updated[0] is initial[0]
    assert updated[1] is initial[1]
    assert updated[2] is initial[2]
    assert PluginRegistry._package_signature(package, previous=updated) is updated
