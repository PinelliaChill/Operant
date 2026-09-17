"""Focused B2-7 fault and migration coverage.

The existing B2-3 through B2-6 suites cover the normal lifecycle and many
authorization boundaries.  These tests exercise the remaining recovery seams
with synthetic registries and databases only.  The stdio crash test uses a
deterministic runner shim; the standalone B2-7 drill is the source of real
``sandbox-exec`` evidence.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import sys
from contextlib import suppress
from pathlib import Path

import pytest
from test_plugin_host import _cert, _package, _recall, _scope
from test_plugin_host_stdio import _patch_ps

from operant.contracts.b2_1 import CandidateBatch, LifecycleResult
from operant.memory_plugins.b26_schema import schema_contracts as b26_schema_contracts
from operant.persistence.sqlite import MigrationError, SQLiteStore
from operant.plugins import (
    HostBudget,
    PluginError,
    PluginHost,
    PluginRegistry,
    SandboxEvidence,
    SandboxProbe,
    compute_package_digest,
)

B26_TABLES = set(b26_schema_contracts()[0])


def _release_registry_lock(registry: PluginRegistry) -> None:
    """Leave durable state as a crashed writer would, without deleting it."""

    handle = registry._lock_handle  # noqa: SLF001 - crash fixture
    registry._lock_handle = None  # noqa: SLF001 - crash fixture
    assert handle is not None
    handle.close()


class _CompletePlugin:
    async def handle(self, operation, request, _host):
        if operation == "recall":
            return CandidateBatch(request_id=request.context.request_id, candidates=())
        return LifecycleResult(
            request_id=request.context.request_id,
            sdk_version="operant-memory-sdk.v1",
            state="ready",
            checkpoint_ref=None,
        )


class _StubbornPlugin:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def handle(self, operation, request, _host):
        self.entered.set()
        while not self.release.is_set():
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                # A trusted in-process implementation may ignore cancellation;
                # Host must retain the fence and require a safe restart.
                continue
        if operation == "recall":
            return CandidateBatch(request_id=request.context.request_id, candidates=())
        return LifecycleResult(
            request_id=request.context.request_id,
            sdk_version="operant-memory-sdk.v1",
            state="ready",
            checkpoint_ref=None,
        )


class _VersionedPlugin:
    def __init__(self, version: str) -> None:
        self.version = version

    async def handle(self, operation, request, _host):
        if operation == "recall":
            return CandidateBatch(request_id=request.context.request_id, candidates=())
        return LifecycleResult(
            request_id=request.context.request_id,
            sdk_version="operant-memory-sdk.v1",
            state="ready",
            checkpoint_ref=None,
        )


def _versioned_packages(root: Path):
    package_v1, manifest_v1 = _package(root / "source-v1", entrypoint=True)
    package_v2, manifest_v2 = _package(root / "source-v2", entrypoint=True)
    plugin_v2 = package_v2 / "plugin.py"
    plugin_v2.write_text(
        plugin_v2.read_text(encoding="utf-8").replace("PackagePlugin", "PackagePluginV2"),
        encoding="utf-8",
    )
    manifest_v2 = manifest_v2.model_copy(
        update={
            "plugin_version": "2.0.0",
            "package_digest": compute_package_digest(package_v2),
        }
    )
    certification_v1 = _cert(manifest_v1).model_copy(update={"certification_id": "cert.example.v1"})
    certification_v2 = _cert(manifest_v2).model_copy(update={"certification_id": "cert.example.v2"})
    return (
        package_v1,
        manifest_v1,
        certification_v1,
        package_v2,
        manifest_v2,
        certification_v2,
    )


@pytest.mark.asyncio
async def test_b27_registry_restart_expires_old_lease_and_requires_new_run(
    tmp_path: Path,
) -> None:
    package, manifest = _package(tmp_path / "source")
    managed = tmp_path / "managed"
    first = PluginRegistry(managed, trusted_issuers={"issuer.local"})
    installation = first.install(manifest, package, certification=_cert(manifest))
    binding = first.bind(installation.installation_id)
    first.enable(binding.binding_id)
    old_lease = first.create_run(
        binding.binding_id,
        run_id="old-run",
        scope=_scope("old-run"),
    )
    _release_registry_lock(first)

    recovered = PluginRegistry(managed, trusted_issuers={"issuer.local"})
    host = PluginHost(
        recovered,
        plugin_factories={manifest.plugin_id: lambda _record: _CompletePlugin()},
    )
    try:
        fenced = recovered.get_run(old_lease.lease_id)
        current_binding = recovered.get_binding(binding.binding_id)
        assert fenced.status == "expired"
        assert current_binding.active_run_ids == ()
        assert current_binding.binding_epoch == old_lease.binding_epoch + 1

        await host.start(installation.installation_id, mode="trusted_in_process")
        with pytest.raises(PluginError) as old_error:
            await host.invoke(old_lease, "recall", _recall(old_lease))
        assert old_error.value.code == "lease_expired"

        new_lease = host.start_run(
            binding.binding_id,
            run_id="new-run",
            scope=_scope("new-run"),
        )
        result = await host.invoke(new_lease, "recall", _recall(new_lease))
        assert isinstance(result, CandidateBatch)
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_b27_versioned_packages_coexist_and_replace_after_keep(tmp_path: Path) -> None:
    (
        package_v1,
        manifest_v1,
        certification_v1,
        package_v2,
        manifest_v2,
        certification_v2,
    ) = _versioned_packages(tmp_path)
    registry = PluginRegistry(tmp_path / "managed", trusted_issuers={"issuer.local"})
    old = registry.install(
        manifest_v1,
        package_v1,
        installation_id="b27-old-installation",
        certification=certification_v1,
    )
    old_binding = registry.bind(old.installation_id)
    registry.enable(old_binding.binding_id)
    old_dataset = old.dataset_id
    old_package_path = registry.package_path(old.installation_id)
    old_plugin = _VersionedPlugin("1.0.0")

    implementations: dict[str, _VersionedPlugin] = {old.installation_id: old_plugin}
    host = PluginHost(
        registry,
        plugin_factories={
            manifest_v1.plugin_id: lambda record: implementations[record.installation_id]
        },
    )
    try:
        await host.start(old.installation_id, mode="trusted_in_process")
        old_lease = host.start_run(
            old_binding.binding_id, run_id="b27-old-run", scope=_scope("b27-old-run")
        )
        old_result = await host.invoke(
            old_lease, "recall", _recall(old_lease, request_id="b27-old-before")
        )
        assert isinstance(old_result, CandidateBatch)
        assert old_lease.installation_id == old.installation_id
        assert registry.get_installation(old.installation_id).manifest.plugin_version == "1.0.0"

        # A new version may be installed as a separate immutable instance while
        # the old Run is active.  Its package and dataset have different owner
        # identities until the old dataset is explicitly retained.
        side = registry.install(
            manifest_v2,
            package_v2,
            installation_id="b27-new-side-installation",
            certification=certification_v2,
        )
        side_binding = registry.bind(side.installation_id)
        registry.enable(side_binding.binding_id)
        side_plugin = _VersionedPlugin("2.0.0")
        implementations[side.installation_id] = side_plugin
        await host.start(side.installation_id, mode="trusted_in_process")
        side_lease = host.start_run(
            side_binding.binding_id, run_id="b27-side-run", scope=_scope("b27-side-run")
        )
        side_result = await host.invoke(
            side_lease, "recall", _recall(side_lease, request_id="b27-side")
        )
        assert isinstance(side_result, CandidateBatch)
        assert side.dataset_id != old_dataset
        assert side.package_relative != old.package_relative
        assert registry.package_path(side.installation_id) != old_package_path
        assert "PackagePluginV2" in (
            registry.package_path(side.installation_id) / "plugin.py"
        ).read_text(encoding="utf-8")

        # Explicitly selecting/using the new binding does not mutate the old
        # lease or stop the old package's active Run.
        old_after_side = await host.invoke(
            old_lease, "recall", _recall(old_lease, request_id="b27-old-after-side")
        )
        assert isinstance(old_after_side, CandidateBatch)
        blocked = await host.uninstall(old.installation_id, data_policy="keep")
        assert blocked.state == "blocked"
        assert blocked.cleanup[0].reason == "active_run"

        registry.release_run(side_lease.lease_id)
        side_deleted = await host.uninstall(side.installation_id, data_policy="delete")
        assert side_deleted.state == "uninstalled"

        registry.release_run(old_lease.lease_id)
        kept = await host.uninstall(old.installation_id, data_policy="keep")
        assert kept.state == "uninstalled"
        assert not old_package_path.exists()
        assert registry.get_dataset(old_dataset).state == "retained"

        replacement = registry.install(
            manifest_v2,
            package_v2,
            installation_id="b27-new-replacement",
            dataset_id=old_dataset,
            certification=certification_v2,
        )
        replacement_binding = registry.bind(replacement.installation_id)
        registry.enable(replacement_binding.binding_id)
        replacement_plugin = _VersionedPlugin("2.0.0")
        implementations[replacement.installation_id] = replacement_plugin
        await host.start(replacement.installation_id, mode="trusted_in_process")
        replacement_lease = host.start_run(
            replacement_binding.binding_id,
            run_id="b27-new-run",
            scope=_scope("b27-new-run"),
        )
        replacement_result = await host.invoke(
            replacement_lease,
            "recall",
            _recall(replacement_lease, request_id="b27-new"),
        )
        assert isinstance(replacement_result, CandidateBatch)
        assert replacement.dataset_id == old_dataset
        assert (
            registry.get_installation(replacement.installation_id).manifest.plugin_version
            == "2.0.0"
        )
        assert replacement_lease.installation_id == replacement.installation_id
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_b27_missing_package_rejects_original_run_recovery(tmp_path: Path) -> None:
    package, manifest = _package(tmp_path / "source")
    managed = tmp_path / "managed"
    first = PluginRegistry(managed, trusted_issuers={"issuer.local"})
    installation = first.install(manifest, package, certification=_cert(manifest))
    binding = first.bind(installation.installation_id)
    first.enable(binding.binding_id)
    old_lease = first.create_run(
        binding.binding_id,
        run_id="missing-package-run",
        scope=_scope("missing-package-run"),
    )
    _release_registry_lock(first)
    shutil.rmtree(first.package_path(installation.installation_id))

    recovered = PluginRegistry(managed, trusted_issuers={"issuer.local"})
    host = PluginHost(recovered)
    try:
        assert recovered.get_run(old_lease.lease_id).status == "expired"
        with pytest.raises(PluginError) as error:
            await host.start(installation.installation_id, mode="trusted_in_process")
        assert error.value.code == "package_unavailable"
        assert not recovered.package_path(installation.installation_id).exists()
        assert len(recovered.list_installations()) == 1
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_b27_safe_restart_fences_one_installation_but_other_run_continues(
    tmp_path: Path,
) -> None:
    package_a, manifest_a = _package(tmp_path / "source-a")
    package_b, manifest_b = _package(tmp_path / "source-b")
    registry = PluginRegistry(tmp_path / "managed", trusted_issuers={"issuer.local"})
    certification = _cert(manifest_a)
    installation_a = registry.install(manifest_a, package_a, certification=certification)
    installation_b = registry.install(manifest_b, package_b, certification=certification)
    binding_a = registry.bind(installation_a.installation_id)
    binding_b = registry.bind(installation_b.installation_id)
    registry.enable(binding_a.binding_id)
    registry.enable(binding_b.binding_id)

    stubborn = _StubbornPlugin()
    implementations = {
        installation_a.installation_id: stubborn,
        installation_b.installation_id: _CompletePlugin(),
    }
    host = PluginHost(
        registry,
        plugin_factories={
            manifest_a.plugin_id: lambda record: implementations[record.installation_id]
        },
    )
    host._CALL_QUIESCE_TIMEOUT_SECONDS = 0.03  # noqa: SLF001 - bounded fault fixture
    pending: asyncio.Task[object] | None = None
    try:
        await host.start(installation_a.installation_id, mode="trusted_in_process")
        await host.start(installation_b.installation_id, mode="trusted_in_process")
        lease_a = host.start_run(binding_a.binding_id, run_id="run-a", scope=_scope("run-a"))
        lease_b = host.start_run(binding_b.binding_id, run_id="run-b", scope=_scope("run-b"))
        pending = asyncio.create_task(host.invoke(lease_a, "recall", _recall(lease_a)))
        await stubborn.entered.wait()

        stopped = await host.stop(
            installation_a.installation_id,
            stop_run_ids=(lease_a.run_id,),
        )
        assert stopped.state == "restart_required"
        assert registry.get_installation(installation_a.installation_id).state == "restart_required"

        other_result = await host.invoke(
            lease_b, "recall", _recall(lease_b, request_id="request-b")
        )
        assert isinstance(other_result, CandidateBatch)

        stubborn.release.set()
        with pytest.raises(PluginError) as old_error:
            await pending
        assert old_error.value.code in {"stale_epoch", "cancelled"}
        pending = None
    finally:
        stubborn.release.set()
        if pending is not None:
            with suppress(BaseException):
                await asyncio.wait_for(pending, 1)
        await host.close()


@pytest.mark.asyncio
async def test_b27_isolated_worker_crash_requires_explicit_respawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crashed stdio worker is fenced, then explicitly respawned on retry."""

    _patch_ps(monkeypatch)
    package, manifest = _package(tmp_path / "source")
    worker = tmp_path / "crash-once-worker.py"
    worker.write_text(
        "import json, os, pathlib, sys\n"
        "marker = pathlib.Path(sys.argv[1])\n"
        "for line in sys.stdin:\n"
        "    request = json.loads(line)\n"
        "    if not marker.exists():\n"
        "        marker.write_text('crashed', encoding='utf-8')\n"
        "        os._exit(47)\n"
        "    context = request['params']['context']\n"
        "    result = {'request_id': context['request_id'],\n"
        "              'sdk_version': 'operant-memory-sdk.v1',\n"
        "              'state': 'ready', 'checkpoint_ref': None}\n"
        "    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'],\n"
        "                      'result': result}), flush=True)\n",
        encoding="utf-8",
    )
    runner = tmp_path / "runner"
    runner.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "args = sys.argv\n"
        "index = args.index('-p')\n"
        "os.execv(args[index + 2], args[index + 3:])\n",
        encoding="utf-8",
    )
    runner.chmod(0o755)

    class DeterministicProbe(SandboxProbe):
        def check(self, **kwargs):
            return SandboxEvidence(
                evidence_ref="b27-test-runner-shim",
                profile="(version 1)\n(deny default)\n",
                runner=str(runner),
            )

    registry = PluginRegistry(tmp_path / "managed", trusted_issuers={"issuer.local"})
    installation = registry.install(manifest, package, certification=_cert(manifest))
    marker = registry.state_path_for(installation.installation_id) / "crash-once"
    host = PluginHost(
        registry,
        stdio_commands={
            installation.installation_id: (
                sys.executable,
                "-I",
                "-S",
                str(worker),
                str(marker),
            )
        },
        budget=HostBudget(timeout_seconds=5, max_idle_seconds=5),
        sandbox_probe=DeterministicProbe(),
    )
    binding = host.bind(installation.installation_id)
    host.enable(binding.binding_id)
    await host.start(installation.installation_id, mode="isolated")
    lease = host.start_run(binding.binding_id, run_id="crash-run", scope=_scope("crash-run"))
    try:
        with pytest.raises(PluginError) as error:
            await host.lifecycle(lease, operation="health")
        assert error.value.code in {"package_unavailable", "cancelled"}
        first_process = host._engines[installation.installation_id].engine.process  # noqa: SLF001
        assert marker.read_text(encoding="utf-8") == "crashed"

        result = await host.lifecycle(lease, operation="health")
        second_process = host._engines[installation.installation_id].engine.process  # noqa: SLF001
        assert result.state == "ready"
        assert second_process is not None
        if first_process is not None:
            assert first_process.returncode is not None
            assert second_process is not first_process
        assert second_process.returncode is None
    finally:
        await host.close()


def test_b27_v18_upgrade_failure_is_atomic(tmp_path: Path) -> None:
    class BrokenV18Store(SQLiteStore):
        def _upgrade_v18(self, connection: sqlite3.Connection) -> None:
            super()._upgrade_v18(connection)
            connection.execute("CREATE TABLE b27_injected_failure(id TEXT)")
            raise RuntimeError("injected B2-7 v18 failure")

    database = tmp_path / "upgrade-failure.sqlite3"
    assert SQLiteStore(database).migrate(target_version=17) == 17
    with pytest.raises(RuntimeError, match="injected B2-7 v18 failure"):
        BrokenV18Store(database).migrate()

    store = SQLiteStore(database)
    assert store.schema_version() == 17
    assert [row["version"] for row in store.list_applied_migrations()] == list(range(1, 18))
    with sqlite3.connect(database) as connection:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert B26_TABLES.isdisjoint(tables)
    assert "b27_injected_failure" not in tables


def test_b27_v18_rollback_requires_isolated_empty_b2_6_database(tmp_path: Path) -> None:
    non_isolated = SQLiteStore(tmp_path / "non-isolated.sqlite3")
    non_isolated.initialize()
    with pytest.raises(MigrationError, match="explicitly isolated"):
        non_isolated.rollback(17)

    empty = SQLiteStore(tmp_path / "empty.sqlite3")
    empty.initialize()
    assert empty.rollback(17, isolated=True) == 17
    assert empty.schema_version() == 17
    with sqlite3.connect(empty.path) as connection:
        remaining = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert B26_TABLES.isdisjoint(remaining)

    populated = SQLiteStore(tmp_path / "populated.sqlite3")
    populated.initialize()
    with populated._connect() as connection:  # noqa: SLF001 - isolated fixture readback
        connection.execute(
            "INSERT INTO b26_commands(command_id, project_id, request_digest, state, result) "
            "VALUES (?, ?, ?, ?, ?)",
            ("b27-command", "b27-project", "a" * 64, "completed", json.dumps({})),
        )
    with pytest.raises(MigrationError, match="B2-6"):
        populated.rollback(17, isolated=True)
    assert populated.schema_version() == 18
