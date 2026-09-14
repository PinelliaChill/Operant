from __future__ import annotations

import asyncio
import hashlib
import shutil
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from operant.contracts.b2_1 import (
    Certification,
    HostReadResult,
    LifecycleRequest,
    PluginManifest,
    RpcContext,
    RunScope,
    SourceBatch,
    SourceRef,
)
from operant.domain.models import utc_now
from operant.plugins import (
    HostBudget,
    HostCallbacks,
    PluginError,
    PluginHost,
    PluginRegistry,
    SandboxEvidence,
    SandboxProbe,
    compute_package_digest,
)


def _patch_ps(
    monkeypatch: pytest.MonkeyPatch, *, rss_kb: int = 1024, cpu: str = "00:00:00"
) -> None:
    """Keep the resource monitor deterministic on hosts that restrict ``ps``."""

    original = asyncio.create_subprocess_exec

    class ProbeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return f"{rss_kb} {cpu}\n".encode("ascii"), b""

        def kill(self) -> None:
            self.returncode = -9

        async def wait(self) -> int:
            return self.returncode

    async def create_subprocess_exec(*args, **kwargs):
        if args and args[0] == "/bin/ps":
            return ProbeProcess()
        return await original(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess_exec)


def _stdio_package(tmp_path: Path) -> tuple[Path, PluginManifest, Certification]:
    package = tmp_path / "package"
    package.mkdir()
    source_script = Path(__file__).parents[1] / "examples/plugins/stdio_memory_plugin.py"
    shutil.copyfile(source_script, package / "plugin.py")
    dependencies = b'{"stdlib_only":true}\n'
    permissions = b'{"capabilities":[]}\n'
    (package / "dependencies.json").write_bytes(dependencies)
    (package / "permissions.json").write_bytes(permissions)
    manifest = PluginManifest(
        plugin_id="example.stdio",
        plugin_version="1.0.0",
        sdk_version="operant-memory-sdk.v1",
        host_api_versions=("operant-memory-sdk.v1",),
        package_digest=compute_package_digest(package),
        dependencies_digest=hashlib.sha256(dependencies).hexdigest(),
        permissions_digest=hashlib.sha256(permissions).hexdigest(),
        entrypoint="plugin.py",
        config_schema_ref="config.v1",
        config_schema_digest="a" * 64,
        state_schema_version="state.v1",
        capabilities=("extract",),
        memory_mb=64,
        max_rpc_bytes=128_000,
        max_concurrency=1,
        export_supported=True,
        import_supported=True,
        recoverable=True,
    )
    now = utc_now()
    certification = Certification(
        certification_id="cert.stdio.v1",
        issuer_id="issuer.local",
        plugin_id=manifest.plugin_id,
        plugin_version=manifest.plugin_version,
        package_digest=manifest.package_digest,
        dependencies_digest=manifest.dependencies_digest,
        permissions_digest=manifest.permissions_digest,
        lifecycle_evidence_digest="b" * 64,
        allowed_modes=("trusted_in_process",),
        valid_from=now - timedelta(minutes=1),
        expires_at=now + timedelta(hours=1),
        revocation_epoch=0,
        state="valid",
        signature_ref="signature.stdio",
    )
    return package, manifest, certification


@pytest.mark.asyncio
async def test_isolated_stdio_reuses_process_and_calls_restricted_host_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_ps(monkeypatch)
    package, manifest, certification = _stdio_package(tmp_path)
    registry = PluginRegistry(tmp_path / "managed", trusted_issuers={"issuer.local"})
    installation = registry.install(manifest, package, certification=certification)
    calls: list[str] = []

    def read_source(request):
        calls.append(request.source.source_id)
        return HostReadResult(source=request.source, text="source body", truncated=False)

    # Unit test transport with a deterministic runner shim.  Platform sandbox
    # admission itself is covered by SandboxProbe and is exercised separately
    # on macOS with the real /usr/bin/sandbox-exec binary.
    runner = tmp_path / "sandbox-runner"
    runner.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "args = sys.argv\n"
        "index = args.index('-p')\n"
        "os.execv(args[index + 2], args[index + 3:])\n",
        encoding="utf-8",
    )
    runner.chmod(0o755)

    class TestProbe(SandboxProbe):
        def check(self, **kwargs):
            return SandboxEvidence(
                evidence_ref="test-sandbox", profile="ignored", runner=str(runner)
            )

    host = PluginHost(
        registry,
        callbacks=HostCallbacks(
            read_source=read_source,
            authorize_source=lambda context, ref: ref == source and context.scope == lease.scope,
        ),
        sandbox_probe=TestProbe(),
    )
    binding = host.bind(installation.installation_id)
    host.enable(binding.binding_id)
    admission = await host.start(installation.installation_id, mode="isolated")
    assert admission.mode == "isolated"
    lease = host.start_run(
        binding.binding_id,
        run_id="run-stdio",
        scope=RunScope(
            kind="run",
            project_id="project-1",
            workspace_id="workspace-1",
            run_id="run-stdio",
            writer_id=None,
        ),
    )
    context = RpcContext(
        sdk_version="operant-memory-sdk.v1",
        request_id="stdio-request",
        installation_id=lease.installation_id,
        dataset_id=lease.dataset_id,
        scope=lease.scope,
        deadline=utc_now() + timedelta(seconds=30),
        cancel_token="stdio-cancel",
        idempotency_key="stdio-idempotency",
        request_digest="c" * 64,
        binding_epoch=lease.binding_epoch,
        permission_epoch=lease.permission_epoch,
        lease_fencing=lease.lease_fencing,
    )
    source = SourceRef(
        source_type="item",
        source_id="source-1",
        revision=1,
        content_digest="d" * 64,
        scope=lease.scope,
        permission_epoch=lease.permission_epoch,
        availability="available",
    )
    result = await host.invoke(
        lease,
        "extract",
        SourceBatch(context=context, sources=(source,), source_watermark="1"),
    )
    assert result.request_id == "stdio-request"
    assert calls == ["source-1"]
    await host.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["memory", "cpu"])
async def test_process_resource_overrun_is_stopped_without_an_rpc(
    tmp_path: Path, kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real resource enforcement; sandbox transport shim is not isolation evidence."""

    from operant.plugins import (
        SandboxEvidence,
        SandboxProbe,
        StdioPluginEngine,
    )

    runner = tmp_path / "runner"
    runner.write_text(
        "#!/usr/bin/env python3\nimport os,sys\n"
        "a=sys.argv; i=a.index('-p'); os.execv(a[i+2],a[i+3:])\n"
    )
    runner.chmod(0o755)
    worker = tmp_path / "worker.py"
    worker.write_text(
        {
            "memory": "import time\ndata=bytearray(64*1024*1024)\ntime.sleep(30)\n",
            "cpu": "while True: pass\n",
            "idle": "import time\ntime.sleep(30)\n",
        }[kind]
    )

    class Probe(SandboxProbe):
        def check(self, **kwargs):
            return SandboxEvidence(
                evidence_ref="unit-resource-probe", profile="ignored", runner=str(runner)
            )

    _patch_ps(
        monkeypatch,
        rss_kb=64 * 1024 if kind == "memory" else 1024,
        cpu="00:00:01" if kind == "cpu" else "00:00:00",
    )
    engine = StdioPluginEngine(
        argv=[sys.executable, "-I", "-S", str(worker)],
        package_root=tmp_path,
        mode="isolated",
        sandbox_probe=Probe(),
        data_root=tmp_path,
        state_root=tmp_path,
        logs_root=tmp_path,
        tmp_root=tmp_path,
        limits=HostBudget(
            memory_mb=24,
            max_cpu_seconds=0.01 if kind == "cpu" else 5,
            max_idle_seconds=0.1 if kind == "idle" else 5,
        ),
    )
    await engine.start()
    process = engine.process
    assert process is not None
    try:
        await asyncio.wait_for(process.wait(), 5)
        assert process.returncode != 0
        with pytest.raises(PluginError) as error:
            await engine.start()
        assert error.value.code == "budget_exceeded"
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_isolated_stdio_idle_process_survives_until_explicit_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An idle reusable engine is not a pending request timeout."""

    _patch_ps(monkeypatch)
    package, manifest, certification = _stdio_package(tmp_path)
    registry = PluginRegistry(tmp_path / "managed", trusted_issuers={"issuer.local"})
    installation = registry.install(manifest, package, certification=certification)
    runner = tmp_path / "sandbox-runner"
    runner.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "args = sys.argv\n"
        "index = args.index('-p')\n"
        "os.execv(args[index + 2], args[index + 3:])\n",
        encoding="utf-8",
    )
    runner.chmod(0o755)

    class Probe(SandboxProbe):
        def check(self, **kwargs):
            return SandboxEvidence(
                evidence_ref="test-sandbox", profile="ignored", runner=str(runner)
            )

    host = PluginHost(
        registry,
        budget=HostBudget(max_idle_seconds=0.05),
        sandbox_probe=Probe(),
    )
    binding = host.bind(installation.installation_id)
    host.enable(binding.binding_id)
    await host.start(installation.installation_id, mode="isolated")
    try:
        await asyncio.sleep(0.45)
        engine = host._engines[installation.installation_id].engine
        assert engine.process is not None
        assert engine.process.returncode is None
        assert engine._resource_error is None
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_isolated_stdio_pending_request_obeys_context_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blocked response still expires on the active request deadline."""

    _patch_ps(monkeypatch)
    package, manifest, certification = _stdio_package(tmp_path)
    worker = tmp_path / "unresponsive.py"
    worker.write_text(
        "import time, sys\nfor _line in sys.stdin:\n    time.sleep(10)\n",
        encoding="utf-8",
    )
    certification = certification.model_copy(update={"allowed_modes": ("isolated",)})
    registry = PluginRegistry(tmp_path / "managed", trusted_issuers={"issuer.local"})
    installation = registry.install(manifest, package, certification=certification)
    runner = tmp_path / "sandbox-runner"
    runner.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "args = sys.argv\n"
        "index = args.index('-p')\n"
        "os.execv(args[index + 2], args[index + 3:])\n",
        encoding="utf-8",
    )
    runner.chmod(0o755)

    class Probe(SandboxProbe):
        def check(self, **kwargs):
            return SandboxEvidence(
                evidence_ref="test-sandbox", profile="ignored", runner=str(runner)
            )

    host = PluginHost(
        registry,
        stdio_commands={installation.installation_id: (sys.executable, "-I", "-S", str(worker))},
        budget=HostBudget(timeout_seconds=1, max_idle_seconds=5),
        sandbox_probe=Probe(),
    )
    binding = host.bind(installation.installation_id)
    host.enable(binding.binding_id)
    await host.start(installation.installation_id, mode="isolated")
    lease = host.start_run(
        binding.binding_id,
        run_id="run-deadline",
        scope=RunScope(
            kind="run",
            project_id="project-1",
            workspace_id="workspace-1",
            run_id="run-deadline",
            writer_id=None,
        ),
    )
    context = RpcContext(
        sdk_version="operant-memory-sdk.v1",
        request_id="deadline-request",
        installation_id=lease.installation_id,
        dataset_id=lease.dataset_id,
        scope=lease.scope,
        deadline=utc_now() + timedelta(seconds=0.08),
        cancel_token="deadline-cancel",
        idempotency_key="deadline-idempotency",
        request_digest="c" * 64,
        binding_epoch=lease.binding_epoch,
        permission_epoch=lease.permission_epoch,
        lease_fencing=lease.lease_fencing,
    )
    try:
        with pytest.raises(PluginError) as error:
            await host.invoke(
                lease,
                "lifecycle",
                LifecycleRequest(context=context, operation="health", checkpoint_ref=None),
            )
        assert error.value.code in {"deadline_exceeded", "cancelled"}
        assert host._engines[installation.installation_id].engine.process is None
    finally:
        await host.close()
