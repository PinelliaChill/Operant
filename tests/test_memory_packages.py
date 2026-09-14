"""Real-package tests for the MP-2 memory engine examples."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from operant.contracts.b2_1 import (
    CandidateBatch,
    CandidateReference,
    Certification,
    HostReadResult,
    IndexEvent,
    MemoryHead,
    MemoryVersionRef,
    PluginConfig,
    RecallRequest,
    RpcContext,
    RunScope,
    SourceBatch,
    SourceRef,
)
from operant.domain.models import utc_now
from operant.plugins import (
    HostBudget,
    HostCallbacks,
    PluginHost,
    PluginRegistry,
    SandboxEvidence,
    SandboxProbe,
)
from operant.plugins.protocol import compute_package_digest
from sdk.memory_plugin.package import package_digest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAMES = ("memory-standard", "memory-notebook")


def _manifest(name: str) -> Any:
    path = ROOT / "plugins" / name / "manifest.py"
    spec = importlib.util.spec_from_file_location(f"{name.replace('-', '_')}_manifest_test", path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.plugin_manifest()


def _certification(
    manifest: Any, *, modes: tuple[str, ...] = ("trusted_in_process",)
) -> Certification:
    return Certification(
        certification_id=f"cert.{manifest.plugin_id}.v1",
        issuer_id="issuer.local",
        plugin_id=manifest.plugin_id,
        plugin_version=manifest.plugin_version,
        package_digest=manifest.package_digest,
        dependencies_digest=manifest.dependencies_digest,
        permissions_digest=manifest.permissions_digest,
        lifecycle_evidence_digest="b" * 64,
        allowed_modes=modes,
        valid_from=utc_now() - timedelta(minutes=1),
        expires_at=utc_now() + timedelta(hours=1),
        revocation_epoch=0,
        state="valid",
        signature_ref=f"signature.{manifest.plugin_id}",
    )


def _scope() -> RunScope:
    return RunScope(
        kind="run",
        project_id="project-1",
        workspace_id="workspace-1",
        run_id="run-1",
        writer_id=None,
    )


def _source(context: RpcContext, text: str, *, source_id: str = "source-1") -> SourceRef:
    return SourceRef(
        source_type="item",
        source_id=source_id,
        revision=1,
        content_digest=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        scope=context.scope,
        permission_epoch=context.permission_epoch,
        availability="available",
    )


def _context(lease: Any, request_id: str = "request-1") -> RpcContext:
    return RpcContext(
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


def test_packages_have_distinct_manifests_and_host_digest_identity() -> None:
    manifests = {name: _manifest(name) for name in PACKAGE_NAMES}
    assert set(manifests) == set(PACKAGE_NAMES)
    assert manifests["memory-standard"].plugin_id != manifests["memory-notebook"].plugin_id
    assert (
        manifests["memory-standard"].config_schema_digest
        != manifests["memory-notebook"].config_schema_digest
    )
    for name, manifest in manifests.items():
        package = ROOT / "plugins" / name
        assert manifest.package_digest == compute_package_digest(package)
        assert manifest.package_digest == package_digest(package)
        assert manifest.sdk_version == "operant-memory-sdk.v1"
        assert json.loads((package / "dependencies.json").read_text()) == {
            "python": "3.10+",
            "stdlib_only": True,
        }
        assert (
            json.loads((package / "config.schema.json").read_text())["additionalProperties"]
            is False
        )


@pytest.mark.parametrize("name", PACKAGE_NAMES)
def test_installed_package_runs_real_extract_and_recall_boundary(tmp_path: Path, name: str) -> None:
    manifest = _manifest(name)
    registry = PluginRegistry(tmp_path / "managed", trusted_issuers={"issuer.local"})
    installation = registry.install(
        manifest,
        (ROOT / "plugins" / name).resolve(),
        certification=_certification(manifest),
        config=PluginConfig(
            schema_version="operant-memory-config.v1",
            config_id="config-1",
            revision=0,
            extraction_model_profile_id=None,
            rerank_model_profile_id=None,
            recall_token_budget=1000,
            maintenance_enabled=True,
            scheduler_definition_id=None,
            secret_refs={},
            effective_at=utc_now(),
        ),
    )
    scope = _scope()
    body = json.dumps(
        {
            "content": "user.name = Alice\nuser.role: admin"
            if name == "memory-standard"
            else "user.name = Alice",
            "version": 1,
        }
    )
    calls: list[tuple[str, str]] = []

    def read_source(request: Any) -> HostReadResult:
        calls.append(("read_source", request.source.source_id))
        return HostReadResult(source=request.source, text=body, truncated=False)

    def search(request: Any) -> CandidateBatch:
        calls.append(("search", request.query))
        return CandidateBatch(request_id=request.context.request_id, candidates=())

    def allow(*_args: Any) -> bool:
        return True

    host = PluginHost(
        registry,
        callbacks=HostCallbacks(
            read_source=read_source,
            search=search,
            authorize_source=allow,
            authorize_memory_ref=allow,
            authorize_proposal=allow,
        ),
    )
    binding = host.bind(installation.installation_id)
    host.enable(binding.binding_id)

    async def exercise() -> None:
        await host.start(installation.installation_id, mode="trusted_in_process")
        lease = host.start_run(binding.binding_id, run_id="run-1", scope=scope)
        context = _context(lease)
        source = _source(context, body)
        extracted = await host.invoke(
            lease,
            "extract",
            SourceBatch(context=context, sources=(source,), source_watermark="1"),
        )
        assert len(extracted.proposals) >= 1
        assert all(item.state == "pending" for item in extracted.proposals)
        assert all(item.source_refs == (source,) for item in extracted.proposals)

        recalled = await host.invoke(
            lease,
            "recall",
            RecallRequest(
                context=_context(lease, "request-2"),
                query="Alice",
                explicit_refs=(),
                knowledge_cutoff="0",
                max_candidates=5,
                token_budget=0,
            ),
        )
        assert recalled.request_id == "request-2"
        if name == "memory-notebook":
            assert recalled.candidates == ()
        await host.close()

    asyncio.run(exercise())
    assert ("read_source", "source-1") in calls
    if name == "memory-standard":
        assert ("search", "Alice") in calls
    else:
        assert ("search", "Alice") in calls


def test_notebook_config_is_loaded_from_installed_core_config(tmp_path: Path) -> None:
    name = "memory-notebook"
    manifest = _manifest(name)
    registry = PluginRegistry(tmp_path / "managed", trusted_issuers={"issuer.local"})
    installation = registry.install(
        manifest,
        (ROOT / "plugins" / name).resolve(),
        certification=_certification(manifest),
        config=PluginConfig(
            schema_version="operant-memory-config.v1",
            config_id="config-2",
            revision=0,
            extraction_model_profile_id=None,
            rerank_model_profile_id=None,
            recall_token_budget=1000,
            maintenance_enabled=True,
            scheduler_definition_id=None,
            secret_refs={},
            effective_at=utc_now(),
        ),
    )
    settings = registry.installation_root(installation.installation_id) / "config" / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "schema_version": "memory-notebook-config.v1",
                "match_mode": "exact",
                "case_sensitive": False,
                "separator": ":",
                "max_pairs_per_source": 10,
            }
        ),
        encoding="utf-8",
    )
    scope = _scope()
    body = json.dumps({"content": "User.Name: Alice", "version": 1})

    def allow(*_args: Any) -> bool:
        return True

    async def exercise() -> None:
        host = PluginHost(
            registry,
            callbacks=HostCallbacks(
                read_source=lambda request: HostReadResult(
                    source=request.source, text=body, truncated=False
                ),
                search=lambda request: CandidateBatch(
                    request_id=request.context.request_id, candidates=()
                ),
                authorize_source=allow,
                authorize_memory_ref=allow,
                authorize_head=allow,
                authorize_proposal=allow,
            ),
        )
        binding = host.bind(installation.installation_id)
        host.enable(binding.binding_id)
        await host.start(installation.installation_id, mode="trusted_in_process")
        lease = host.start_run(binding.binding_id, run_id="run-1", scope=scope)
        context = _context(lease)
        source = _source(context, body)
        extracted = await host.invoke(
            lease,
            "extract",
            SourceBatch(context=context, sources=(source,), source_watermark="1"),
        )
        assert len(extracted.proposals) == 1
        published = extracted.proposals[0].proposed_version
        await host.invoke(
            lease,
            "on_index_event",
            IndexEvent(
                context=_context(lease, "request-index"),
                event_id="event-config-publish",
                head=MemoryHead(
                    dataset_id=lease.dataset_id,
                    record_id=published.record_id,
                    published_version=published,
                    revision=1,
                    publication_cursor="1",
                    state="published",
                    permission_epoch=lease.permission_epoch,
                ),
            ),
        )
        recalled = await host.invoke(
            lease,
            "recall",
            RecallRequest(
                context=_context(lease, "request-2"),
                query="user.name",
                explicit_refs=(),
                knowledge_cutoff="0",
                max_candidates=5,
                token_budget=0,
            ),
        )
        assert len(recalled.candidates) == 1
        assert recalled.candidates[0].score == 1
        await host.close()

    asyncio.run(exercise())


def test_notebook_respects_core_identity_and_only_promotes_on_index_event(tmp_path: Path) -> None:
    manifest = _manifest("memory-notebook")
    registry = PluginRegistry(tmp_path / "managed", trusted_issuers={"issuer.local"})
    installation = registry.install(
        manifest,
        (ROOT / "plugins" / "memory-notebook").resolve(),
        certification=_certification(manifest),
        config=PluginConfig(
            schema_version="operant-memory-config.v1",
            config_id="config-3",
            revision=0,
            extraction_model_profile_id=None,
            rerank_model_profile_id=None,
            recall_token_budget=1000,
            maintenance_enabled=True,
            scheduler_definition_id=None,
            secret_refs={},
            effective_at=utc_now(),
        ),
    )
    scope = _scope()
    calls: list[str] = []
    body_holder = {"value": ""}

    def allow(*_args: Any) -> bool:
        return True

    def search(request: Any) -> CandidateBatch:
        calls.append(request.query)
        return CandidateBatch(request_id=request.context.request_id, candidates=())

    async def exercise() -> None:
        host = PluginHost(
            registry,
            callbacks=HostCallbacks(
                read_source=lambda request: HostReadResult(
                    source=request.source, text=body_holder["value"], truncated=False
                ),
                search=search,
                authorize_source=allow,
                authorize_memory_ref=allow,
                authorize_head=allow,
                authorize_proposal=allow,
            ),
        )
        binding = host.bind(installation.installation_id)
        host.enable(binding.binding_id)
        await host.start(installation.installation_id, mode="trusted_in_process")
        lease = host.start_run(binding.binding_id, run_id="run-1", scope=scope)
        context = _context(lease)
        body_holder["value"] = json.dumps(
            {
                "content": "account.email = alice@example.test",
                "version": 7,
                "record_id": "record-core",
                "proposal": {
                    "operation": "modify",
                    "proposal_revision": 3,
                    "base_head": {
                        "dataset_id": lease.dataset_id,
                        "record_id": "record-core",
                        "published_version": None,
                        "revision": 4,
                        "publication_cursor": "4",
                        "state": "unpublished",
                        "permission_epoch": lease.permission_epoch,
                    },
                },
            }
        )
        source = _source(context, body_holder["value"])
        proposal_batch = await host.invoke(
            lease,
            "extract",
            SourceBatch(context=context, sources=(source,), source_watermark="7"),
        )
        proposal = proposal_batch.proposals[0]
        assert proposal.proposed_version.record_id == "record-core"
        assert proposal.proposed_version.version == 7
        assert proposal.base_head.record_id == "record-core"
        assert proposal.base_head.revision == 4

        before_publish = await host.invoke(
            lease,
            "recall",
            RecallRequest(
                context=_context(lease, "request-before-publish"),
                query="account.email",
                explicit_refs=(),
                knowledge_cutoff="0",
                max_candidates=5,
                token_budget=0,
            ),
        )
        assert before_publish.candidates == ()
        assert calls == ["account.email"]

        head = MemoryHead(
            dataset_id=lease.dataset_id,
            record_id="record-core",
            published_version=proposal.proposed_version,
            revision=5,
            publication_cursor="5",
            state="published",
            permission_epoch=lease.permission_epoch,
        )
        await host.invoke(
            lease,
            "on_index_event",
            IndexEvent(
                context=_context(lease, "request-index"),
                event_id="event-publish",
                head=head,
            ),
        )
        after_publish = await host.invoke(
            lease,
            "recall",
            RecallRequest(
                context=_context(lease, "request-after-publish"),
                query="account.email",
                explicit_refs=(),
                knowledge_cutoff="0",
                max_candidates=5,
                token_budget=0,
            ),
        )
        assert len(after_publish.candidates) == 1
        assert after_publish.candidates[0].ref == proposal.proposed_version
        assert calls == ["account.email"]
        await host.close()

    asyncio.run(exercise())


def test_notebook_fallback_requires_an_exact_key_in_trusted_mode(tmp_path: Path) -> None:
    manifest = _manifest("memory-notebook")
    registry = PluginRegistry(tmp_path / "managed", trusted_issuers={"issuer.local"})
    installation = registry.install(
        manifest,
        (ROOT / "plugins" / "memory-notebook").resolve(),
        certification=_certification(manifest),
        config=PluginConfig(
            schema_version="operant-memory-config.v1",
            config_id="config-fallback",
            revision=0,
            extraction_model_profile_id=None,
            rerank_model_profile_id=None,
            recall_token_budget=1000,
            maintenance_enabled=True,
            scheduler_definition_id=None,
            secret_refs={},
            effective_at=utc_now(),
        ),
    )
    ref = MemoryVersionRef(
        dataset_id=installation.dataset_id,
        record_id="record-exact",
        version=2,
        content_digest=hashlib.sha256("验收标记=MAPLE-42".encode()).hexdigest(),
    )
    calls: list[tuple[str, str]] = []

    def allow(*_args: Any) -> bool:
        return True

    def search(request: Any) -> CandidateBatch:
        calls.append(("search", request.query))
        return CandidateBatch(
            request_id=request.context.request_id,
            candidates=(CandidateReference(ref=ref, score=1.0),),
        )

    def read_source(request: Any) -> HostReadResult:
        calls.append(("read", request.source.source_type))
        assert request.source.source_type == "memory_version"
        assert request.source.source_id == ref.record_id
        assert request.source.revision == ref.version
        assert request.source.content_digest == ref.content_digest
        return HostReadResult(source=request.source, text="验收标记=MAPLE-42", truncated=False)

    async def exercise() -> None:
        host = PluginHost(
            registry,
            callbacks=HostCallbacks(
                search=search,
                read_source=read_source,
                authorize_source=allow,
                authorize_memory_ref=allow,
                authorize_head=allow,
                authorize_proposal=allow,
            ),
        )
        binding = host.bind(installation.installation_id)
        host.enable(binding.binding_id)
        await host.start(installation.installation_id, mode="trusted_in_process")
        lease = host.start_run(binding.binding_id, run_id="run-fallback", scope=_scope())
        for request_id, query, expected in (
            ("request-partial", "验收", 0),
            ("request-value", "MAPLE-42", 0),
            ("request-exact", "验收标记", 1),
        ):
            recalled = await host.invoke(
                lease,
                "recall",
                RecallRequest(
                    context=_context(lease, request_id),
                    query=query,
                    explicit_refs=(),
                    knowledge_cutoff="0",
                    max_candidates=5,
                    token_budget=0,
                ),
            )
            assert len(recalled.candidates) == expected
        assert calls == [
            ("search", "验收"),
            ("read", "memory_version"),
            ("search", "MAPLE-42"),
            ("read", "memory_version"),
            ("search", "验收标记"),
            ("read", "memory_version"),
        ]
        await host.close()

    asyncio.run(exercise())


def test_notebook_fallback_requires_an_exact_key_in_isolated_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The installed stdio Host path applies the same exact-key filter."""

    original_create_process = asyncio.create_subprocess_exec

    class ProbeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"1024 00:00:00\n", b""

        def kill(self) -> None:
            self.returncode = -9

        async def wait(self) -> int:
            return self.returncode

    async def create_process(*args: Any, **kwargs: Any) -> Any:
        if args and args[0] == "/bin/ps":
            return ProbeProcess()
        return await original_create_process(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    manifest = _manifest("memory-notebook")
    registry = PluginRegistry(tmp_path / "managed", trusted_issuers={"issuer.local"})
    installation = registry.install(
        manifest,
        (ROOT / "plugins" / "memory-notebook").resolve(),
        certification=_certification(manifest, modes=("isolated",)),
        config=PluginConfig(
            schema_version="operant-memory-config.v1",
            config_id="config-isolated-fallback",
            revision=0,
            extraction_model_profile_id=None,
            rerank_model_profile_id=None,
            recall_token_budget=1000,
            maintenance_enabled=True,
            scheduler_definition_id=None,
            secret_refs={},
            effective_at=utc_now(),
        ),
    )
    ref = MemoryVersionRef(
        dataset_id=installation.dataset_id,
        record_id="record-exact",
        version=2,
        content_digest=hashlib.sha256("验收标记=MAPLE-42".encode()).hexdigest(),
    )
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
    calls: list[tuple[str, str]] = []

    class Probe(SandboxProbe):
        def check(self, **kwargs: Any) -> SandboxEvidence:
            return SandboxEvidence(
                evidence_ref="test-isolated-fallback", profile="ignored", runner=str(runner)
            )

    def allow(*_args: Any) -> bool:
        return True

    def search(request: Any) -> CandidateBatch:
        calls.append(("search", request.query))
        return CandidateBatch(
            request_id=request.context.request_id,
            candidates=(CandidateReference(ref=ref, score=1.0),),
        )

    def read_source(request: Any) -> HostReadResult:
        calls.append(("read", request.source.source_type))
        assert request.source.source_type == "memory_version"
        return HostReadResult(source=request.source, text="验收标记=MAPLE-42", truncated=False)

    async def exercise() -> None:
        host = PluginHost(
            registry,
            budget=HostBudget(max_idle_seconds=5),
            callbacks=HostCallbacks(
                search=search,
                read_source=read_source,
                authorize_source=allow,
                authorize_memory_ref=allow,
                authorize_head=allow,
                authorize_proposal=allow,
            ),
            sandbox_probe=Probe(),
        )
        binding = host.bind(installation.installation_id)
        host.enable(binding.binding_id)
        await host.start(installation.installation_id, mode="isolated")
        lease = host.start_run(binding.binding_id, run_id="run-isolated-fallback", scope=_scope())
        for request_id, query, expected in (
            ("isolated-partial", "验收", 0),
            ("isolated-value", "MAPLE-42", 0),
            ("isolated-exact", "验收标记", 1),
        ):
            recalled = await host.invoke(
                lease,
                "recall",
                RecallRequest(
                    context=_context(lease, request_id),
                    query=query,
                    explicit_refs=(),
                    knowledge_cutoff="0",
                    max_candidates=5,
                    token_budget=0,
                ),
            )
            assert len(recalled.candidates) == expected
        assert calls == [
            ("search", "验收"),
            ("read", "memory_version"),
            ("search", "MAPLE-42"),
            ("read", "memory_version"),
            ("search", "验收标记"),
            ("read", "memory_version"),
        ]
        await host.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("name", PACKAGE_NAMES)
def test_isolated_entrypoint_uses_stdlib_bidirectional_rpc(name: str) -> None:
    """Drive the actual ``-I -S`` package without a Core or filesystem bypass."""

    package = (ROOT / "plugins" / name).resolve()
    context = {
        "sdk_version": "operant-memory-sdk.v1",
        "request_id": "stdio-extract",
        "installation_id": "install-1",
        "dataset_id": "dataset-1",
        "scope": {
            "kind": "run",
            "project_id": "project-1",
            "workspace_id": "workspace-1",
            "run_id": "run-1",
            "writer_id": None,
        },
        "deadline": "2099-01-01T00:00:00Z",
        "cancel_token": "cancel-1",
        "idempotency_key": "stdio-idem",
        "request_digest": "c" * 64,
        "binding_epoch": 0,
        "permission_epoch": 0,
        "lease_fencing": 0,
    }
    source = {
        "source_type": "item",
        "source_id": "source-1",
        "revision": 1,
        "content_digest": "d" * 64,
        "scope": context["scope"],
        "permission_epoch": 0,
        "availability": "available",
    }
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "extract",
        "params": {"context": context, "sources": [source], "source_watermark": "1"},
    }
    process = subprocess.Popen(
        [sys.executable, "-I", "-S", str(package / "plugin.py")],
        cwd=package,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdin is not None and process.stdout is not None
    try:
        process.stdin.write(json.dumps(request) + "\n")
        process.stdin.flush()
        callback = json.loads(process.stdout.readline())
        assert callback["method"] == "host.read_source"
        assert callback["params"]["source"]["source_id"] == "source-1"
        callback_content = "Alice" if name == "memory-standard" else "topic: Alice"
        process.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": callback["id"],
                    "result": {
                        "source": source,
                        "text": json.dumps({"content": callback_content}),
                        "truncated": False,
                    },
                }
            )
            + "\n"
        )
        process.stdin.flush()
        response = json.loads(process.stdout.readline())
        assert response["id"] == 1
        assert response["result"]["request_id"] == "stdio-extract"
        assert response["result"]["proposals"]
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_notebook_isolated_fallback_reads_and_filters_exact_key() -> None:
    """The real ``-I -S`` package filters substring search via Host reads."""

    package = (ROOT / "plugins" / "memory-notebook").resolve()
    context = {
        "sdk_version": "operant-memory-sdk.v1",
        "request_id": "stdio-recall-1",
        "installation_id": "install-1",
        "dataset_id": "dataset-1",
        "scope": {
            "kind": "run",
            "project_id": "project-1",
            "workspace_id": "workspace-1",
            "run_id": "run-1",
            "writer_id": None,
        },
        "deadline": "2099-01-01T00:00:00Z",
        "cancel_token": "cancel-1",
        "idempotency_key": "stdio-recall-idem",
        "request_digest": "c" * 64,
        "binding_epoch": 0,
        "permission_epoch": 0,
        "lease_fencing": 0,
    }
    ref = {
        "dataset_id": "dataset-1",
        "record_id": "record-exact",
        "version": 2,
        "content_digest": hashlib.sha256("验收标记=MAPLE-42".encode()).hexdigest(),
    }
    process = subprocess.Popen(
        [sys.executable, "-I", "-S", str(package / "plugin.py")],
        cwd=package,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdin is not None and process.stdout is not None
    try:
        for rpc_id, request_id, query, expected in (
            (1, "stdio-recall-1", "验收", []),
            (2, "stdio-recall-2", "验收标记", [ref]),
        ):
            request_context = {**context, "request_id": request_id}
            process.stdin.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": rpc_id,
                        "method": "recall",
                        "params": {
                            "context": request_context,
                            "query": query,
                            "explicit_refs": [],
                            "knowledge_cutoff": "0",
                            "max_candidates": 5,
                            "token_budget": 0,
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            process.stdin.flush()
            search_callback = json.loads(process.stdout.readline())
            assert search_callback["method"] == "host.search"
            assert search_callback["params"]["query"] == query
            process.stdin.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": search_callback["id"],
                        "result": {
                            "request_id": request_id,
                            "candidates": [{"ref": ref, "score": 1.0}],
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            process.stdin.flush()
            read_callback = json.loads(process.stdout.readline())
            assert read_callback["method"] == "host.read_source"
            assert read_callback["params"]["source"] == {
                "source_type": "memory_version",
                "source_id": "record-exact",
                "revision": 2,
                "content_digest": ref["content_digest"],
                "scope": request_context["scope"],
                "permission_epoch": 0,
                "availability": "available",
            }
            process.stdin.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": read_callback["id"],
                        "result": {
                            "source": read_callback["params"]["source"],
                            "text": "验收标记=MAPLE-42",
                            "truncated": False,
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            process.stdin.flush()
            response = json.loads(process.stdout.readline())
            assert response["id"] == rpc_id
            assert response["result"]["request_id"] == request_id
            assert [item["ref"] for item in response["result"]["candidates"]] == expected
    finally:
        process.terminate()
        process.wait(timeout=5)
