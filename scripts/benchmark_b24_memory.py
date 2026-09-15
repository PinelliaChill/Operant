"""Benchmark the B2-4 memory recall chain on the frozen B2-1 fixture.

The benchmark keeps the old baseline as a quality and task-latency reference,
then runs the same typed request set through:

* direct Core retrieval using ``MemoryManager.search`` and the B2-4 FTS index;
* a real trusted in-process ``PluginHost``; and
* a real isolated stdio ``PluginHost`` when the macOS sandbox probe admits it.

No user database, environment file, network, or model provider is used.  The
fixture is loaded at runtime, and expected/forbidden IDs come only from the
fixture itself.  Host overhead is reported separately from the task algorithm;
frozen B2-1 gates are imported from ``benchmark_memory_baseline.py`` rather
than copied or relaxed here.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import math
import platform
import resource
import subprocess
import sys
import time
import tracemalloc
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
from typing import Any, Literal, cast

from operant.contracts.b2_1 import (
    CandidateBatch,
    Certification,
    DatasetOwner,
    MemoryConditions,
    MemoryVersion,
    MemoryVersionRef,
    PluginConfig,
    RecallRequest,
    RpcContext,
    SourceRef,
    WorkspaceScope,
)
from operant.domain.models import utc_now
from operant.memory_plugins.ledger import MemoryLedger
from operant.memory_plugins.manager import MemoryManager
from operant.memory_plugins.retrieval import RetrievalPolicy
from operant.plugins import HostCallbacks, IsolationUnavailableError, PluginHost, PluginRegistry
from operant.plugins.protocol import StdioPluginEngine, encode_rpc

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = ROOT / "tests/fixtures/b2_1/memory_baseline.json"
OLD_BENCHMARK = ROOT / "scripts/benchmark_memory_baseline.py"
DATASET_ID = "b2_1_memory_baseline"
ROLE_ID = "role_b24_benchmark_reader"
ROLE_NAME = "B2-4 benchmark reader"
AGENT_ID = "agent_b24_benchmark"
REPETITIONS_DEFAULT = 2
POLICY = RetrievalPolicy()
MODES = ("direct", "trusted_in_process", "isolated")


@dataclass(frozen=True)
class BenchmarkCase:
    id: str
    split: str
    category: str
    query: str
    project_scope: str
    expected_ids: tuple[str, ...]
    forbidden_ids: tuple[str, ...]
    limit: int


@dataclass(frozen=True)
class FixtureData:
    dataset_id: str
    version: int
    memories: tuple[Mapping[str, Any], ...]
    cases: tuple[BenchmarkCase, ...]
    file_sha256: str


@dataclass(frozen=True)
class Sample:
    wall_ms: float
    cpu_ms: float
    peak_alloc_kib: float
    rss_kib: float
    child_cpu_ms: float | None
    child_rss_kib: float | None
    returned_ids: tuple[str, ...]


@dataclass
class RpcCounter:
    """Counters for actual transport frames and nested Host API calls."""

    outgoing_frames: int = 0
    outgoing_bytes: int = 0
    incoming_frames: int = 0
    incoming_bytes: int = 0
    callback_requests: int = 0
    callback_request_bytes: int = 0
    host_api_calls: int = 0
    manager_search_calls: int = 0
    manager_search_ms: float = 0.0
    registry_assert_lease_calls: int = 0
    registry_assert_lease_ms: float = 0.0
    registry_verify_package_calls: int = 0
    registry_verify_package_ms: float = 0.0
    registry_verify_certification_calls: int = 0
    registry_verify_certification_ms: float = 0.0

    @property
    def rpc_frames(self) -> int:
        return self.outgoing_frames + self.incoming_frames + self.callback_requests

    @property
    def rpc_bytes(self) -> int:
        return self.outgoing_bytes + self.incoming_bytes + self.callback_request_bytes

    def phase_report(self, *, host_total_ms: float) -> dict[str, Any]:
        return {
            "manager_search": {
                "calls": self.manager_search_calls,
                "total_ms": round(self.manager_search_ms, 4),
                "average_ms": round(self.manager_search_ms / self.manager_search_calls, 4)
                if self.manager_search_calls
                else None,
            },
            "host_invoke": {
                "calls": self.host_api_calls,
                "total_ms": round(host_total_ms, 4),
            },
            "registry_assert_lease": {
                "calls": self.registry_assert_lease_calls,
                "total_ms": round(self.registry_assert_lease_ms, 4),
            },
            "registry_verify_package": {
                "calls": self.registry_verify_package_calls,
                "total_ms": round(self.registry_verify_package_ms, 4),
            },
            "registry_verify_certification": {
                "calls": self.registry_verify_certification_calls,
                "total_ms": round(self.registry_verify_certification_ms, 4),
            },
            "host_minus_manager_ms": round(host_total_ms - self.manager_search_ms, 4),
            "interpretation": (
                "registry package digest work is included in verify_package; host_minus_manager "
                "also includes typed validation, callbacks, and transport; wall/CPU come from "
                "the timing pass and allocation is measured in an independent pass"
            ),
        }


@dataclass
class BenchmarkManager:
    """Minimal Core object exposing the production ``MemoryManager.search``."""

    store: MemoryLedger
    _recall_runs: dict[str, Any]

    def search(self, request: RecallRequest) -> CandidateBatch:
        # ``MemoryManager.search`` is the production callback.  This wrapper
        # supplies only its documented store/run registry dependencies.
        return MemoryManager.search(self, request)  # type: ignore[arg-type]


@dataclass
class BenchmarkRun:
    manager: BenchmarkManager
    cutoff: str
    snapshot: Any
    agent_id: str

    def validate(self, version: MemoryVersion) -> bool:
        from operant.memory_plugins.recall import frozen_versions

        current = frozen_versions(
            cast(MemoryManager, self.manager),
            version.ref.dataset_id,
            self.cutoff,
            record_id=version.ref.record_id,
        )
        return (
            any(item.ref == version.ref and item.scope == version.scope for item in current)
            and (
                not version.role_ids
                or self.snapshot.role_id in version.role_ids
                or self.snapshot.role_name in version.role_ids
            )
            and (not version.agent_ids or self.agent_id in version.agent_ids)
        )


def _load_old_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("b24_old_memory_benchmark", OLD_BENCHMARK)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load benchmark module: {OLD_BENCHMARK}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_fixture(path: Path = DEFAULT_FIXTURE) -> FixtureData:
    """Load and validate the fixed fixture through the old validator."""

    old = _load_old_module()
    fixture = old.load_fixture(path)
    cases = tuple(
        BenchmarkCase(
            id=case.id,
            split=case.split,
            category=case.category,
            query=case.query,
            project_scope=case.project_scope,
            expected_ids=case.expected_ids,
            forbidden_ids=case.forbidden_ids,
            limit=case.limit,
        )
        for case in fixture.cases
    )
    return FixtureData(
        dataset_id=fixture.dataset_id,
        version=fixture.version,
        memories=fixture.memories,
        cases=cases,
        file_sha256=fixture.file_sha256,
    )


def _scope(workspace_id: str) -> WorkspaceScope:
    return WorkspaceScope(
        kind="workspace",
        project_id="b24-benchmark-project",
        workspace_id=workspace_id,
    )


def _memory_version(record: Mapping[str, Any], fixture: FixtureData) -> MemoryVersion:
    content = str(record["content"])
    recorded_at = datetime.fromisoformat(str(record["created_at"]))
    scope = _scope(str(record["project_scope"]))
    content_digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    source = SourceRef(
        source_type="item",
        source_id=str(record.get("source_session_id") or f"source-{record['id']}"),
        revision=1,
        content_digest=content_digest,
        scope=scope,
        permission_epoch=1,
        availability="available",
    )
    roles = tuple(str(value) for value in (record.get("role_scope") or ()))
    return MemoryVersion(
        ref=MemoryVersionRef(
            dataset_id=fixture.dataset_id,
            record_id=str(record["id"]),
            version=1,
            content_digest=content_digest,
        ),
        owner=DatasetOwner(
            kind="plugin_dataset",
            owner_namespace=f"dataset:{fixture.dataset_id}",
            dataset_id=fixture.dataset_id,
            principal_id="b24-benchmark-user",
        ),
        kind="project",
        content_type="fact",
        scope=scope,
        role_ids=roles,
        agent_ids=(),
        content=content,
        sources=(source,),
        evidence="user_asserted",
        sensitivity="internal",
        retention_policy_id="b24-benchmark-retain",
        conditions=MemoryConditions(
            commit_ref=None,
            tree_digest=None,
            file_fingerprints={},
            environment_digest=None,
            tool_versions={},
            verified_at=recorded_at,
            valid_from=recorded_at,
            valid_until=None,
        ),
        recorded_at=recorded_at,
    )


def build_ledger(path: Path, fixture: FixtureData) -> tuple[MemoryLedger, dict[str, MemoryVersion]]:
    """Materialize fixture state into one isolated v15/v16-compatible ledger."""

    from operant.memory_plugins.recall_schema import SCHEMA_SQL

    ledger = MemoryLedger(path)
    with ledger._connect() as connection:  # noqa: SLF001 - benchmark setup only
        connection.executescript(SCHEMA_SQL)
    versions: dict[str, MemoryVersion] = {}
    for record in fixture.memories:
        version = _memory_version(record, fixture)
        versions[version.ref.record_id] = version
        status = str(record.get("status"))
        if status == "candidate":
            ledger.propose(version)
            continue
        ledger.confirm(ledger.propose(version))
        if status == "inactive":
            head = ledger.get_head(fixture.dataset_id, version.ref.record_id)
            ledger.deactivate(
                fixture.dataset_id,
                version.ref.record_id,
                expected_head_revision=head.revision,
            )
    return ledger, versions


def publication_cutoff(ledger: MemoryLedger) -> str:
    with ledger._connect() as connection:  # noqa: SLF001 - benchmark read only
        row = connection.execute(
            "SELECT value FROM memory_ledger_meta WHERE key='publication_cursor'"
        ).fetchone()
    if row is None:
        raise RuntimeError("publication cursor is missing")
    return str(row["value"])


def _request(
    case: BenchmarkCase,
    *,
    fixture: FixtureData,
    cutoff: str,
    installation_id: str,
    ordinal: int,
) -> RecallRequest:
    request_id = f"b24-benchmark-request-{ordinal}"
    context = RpcContext(
        sdk_version="operant-memory-sdk.v1",
        request_id=request_id,
        installation_id=installation_id,
        dataset_id=fixture.dataset_id,
        scope=_scope(case.project_scope),
        deadline=utc_now() + timedelta(minutes=2),
        cancel_token=f"b24-benchmark-cancel-{ordinal}",
        idempotency_key=f"b24-benchmark-idempotency-{ordinal}",
        request_digest=hashlib.sha256(request_id.encode()).hexdigest(),
        binding_epoch=1,
        permission_epoch=1,
        lease_fencing=1,
    )
    return RecallRequest(
        context=context,
        query=case.query,
        explicit_refs=(),
        knowledge_cutoff=cutoff,
        max_candidates=case.limit,
        token_budget=0,
    )


def _rss_kib() -> float:
    value = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return round(value / 1024.0, 2) if sys.platform == "darwin" else round(value, 2)


def _cpu_seconds() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


def _parse_ps_time(value: str) -> float:
    parts = value.strip().split(":")
    try:
        numbers = [float(part) for part in parts]
    except ValueError:
        return 0.0
    seconds = 0.0
    for number in numbers:
        seconds = seconds * 60.0 + number
    return seconds


def _child_metrics(pid: int | None) -> tuple[float | None, float | None]:
    if pid is None:
        return None, None
    try:
        result = subprocess.run(
            ["/bin/ps", "-o", "rss=,time=", "-p", str(pid)],
            capture_output=True,
            check=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    fields = result.stdout.split()
    if len(fields) != 2:
        return None, None
    try:
        rss = float(fields[0])
    except ValueError:
        rss = None
    return _parse_ps_time(fields[1]) * 1000.0, rss


def _sample_timing(call: Callable[[], CandidateBatch], *, child_pid: int | None) -> Sample:
    """Measure one formal call with tracemalloc disabled."""

    if tracemalloc.is_tracing():
        raise RuntimeError("formal timing pass requires tracemalloc to be disabled")
    child_cpu_before, _ = _child_metrics(child_pid)
    before_cpu = _cpu_seconds()
    started = time.perf_counter_ns()
    result = call()
    elapsed = (time.perf_counter_ns() - started) / 1_000_000.0
    after_cpu = _cpu_seconds()
    child_cpu_after, child_rss = _child_metrics(child_pid)
    child_cpu = (
        None
        if child_cpu_before is None or child_cpu_after is None
        else max(0.0, child_cpu_after - child_cpu_before)
    )
    rss_kib = _rss_kib()
    return Sample(
        wall_ms=elapsed,
        cpu_ms=max(0.0, after_cpu - before_cpu) * 1000.0,
        peak_alloc_kib=0.0,
        rss_kib=rss_kib,
        child_cpu_ms=child_cpu,
        child_rss_kib=child_rss,
        returned_ids=tuple(item.ref.record_id for item in result.candidates),
    )


def _sample_allocation(call: Callable[[], CandidateBatch]) -> Sample:
    """Measure one allocation-only call in the independent allocation pass."""

    tracemalloc.start()
    before_alloc, _ = tracemalloc.get_traced_memory()
    try:
        result = call()
        _, peak_alloc = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return Sample(
        wall_ms=0.0,
        cpu_ms=0.0,
        peak_alloc_kib=max(0.0, peak_alloc - before_alloc) / 1024.0,
        rss_kib=_rss_kib(),
        child_cpu_ms=None,
        child_rss_kib=None,
        returned_ids=tuple(item.ref.record_id for item in result.candidates),
    )


async def _async_sample_timing(
    call: Callable[[], Awaitable[Any]], *, child_pid: int | None
) -> Sample:
    """Measure one awaitable formal call with tracemalloc disabled."""

    if tracemalloc.is_tracing():
        raise RuntimeError("formal timing pass requires tracemalloc to be disabled")
    child_cpu_before, _ = _child_metrics(child_pid)
    before_cpu = _cpu_seconds()
    started = time.perf_counter_ns()
    result = await call()
    elapsed = (time.perf_counter_ns() - started) / 1_000_000.0
    after_cpu = _cpu_seconds()
    child_cpu_after, child_rss = _child_metrics(child_pid)
    child_cpu = (
        None
        if child_cpu_before is None or child_cpu_after is None
        else max(0.0, child_cpu_after - child_cpu_before)
    )
    rss_kib = _rss_kib()
    return Sample(
        wall_ms=elapsed,
        cpu_ms=max(0.0, after_cpu - before_cpu) * 1000.0,
        peak_alloc_kib=0.0,
        rss_kib=rss_kib,
        child_cpu_ms=child_cpu,
        child_rss_kib=child_rss,
        returned_ids=tuple(item.ref.record_id for item in result.candidates),
    )


async def _async_sample_allocation(call: Callable[[], Awaitable[Any]]) -> Sample:
    """Measure one allocation-only awaitable call in the allocation pass."""

    tracemalloc.start()
    before_alloc, _ = tracemalloc.get_traced_memory()
    try:
        result = await call()
        _, peak_alloc = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return Sample(
        wall_ms=0.0,
        cpu_ms=0.0,
        peak_alloc_kib=max(0.0, peak_alloc - before_alloc) / 1024.0,
        rss_kib=_rss_kib(),
        child_cpu_ms=None,
        child_rss_kib=None,
        returned_ids=tuple(item.ref.record_id for item in result.candidates),
    )


# Keep the short names as timing-only aliases for focused callers.  They no
# longer accept an allocation callback because allocation is a separate pass.
_sample = _sample_timing
_async_sample = _async_sample_timing


def percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
    return round(ordered[index], 4)


def _quality(
    cases: Sequence[BenchmarkCase], samples: Sequence[Sample], repetitions: int
) -> dict[str, Any]:
    final = samples[-len(cases) :] if cases else ()
    precisions: list[float] = []
    recalls: list[float] = []
    forbidden_hits: dict[str, list[str]] = {}
    for case, sample in zip(cases, final, strict=True):
        actual = set(sample.returned_ids)
        expected = set(case.expected_ids)
        relevant = actual.intersection(expected)
        bad = sorted(actual.intersection(case.forbidden_ids))
        if bad:
            forbidden_hits[case.id] = bad
        precisions.append(
            1.0 if not actual and not expected else len(relevant) / len(actual) if actual else 0.0
        )
        recalls.append(
            1.0
            if not expected and not actual
            else len(relevant) / len(expected)
            if expected
            else 0.0
        )
    split_quality = {}
    for split in ("development", "holdout"):
        indexes = [index for index, case in enumerate(cases) if case.split == split]
        split_precisions = [precisions[index] for index in indexes if index < len(precisions)]
        split_recalls = [recalls[index] for index in indexes if index < len(recalls)]
        split_quality[split] = {
            "case_count": len(indexes),
            "macro_precision": round(sum(split_precisions) / len(split_precisions), 4)
            if split_precisions
            else 0.0,
            "macro_recall": round(sum(split_recalls) / len(split_recalls), 4)
            if split_recalls
            else 0.0,
        }
    return {
        "case_count": len(cases),
        "repetitions": repetitions,
        "macro_precision": round(sum(precisions) / len(precisions), 4) if precisions else 0.0,
        "macro_recall": round(sum(recalls) / len(recalls), 4) if recalls else 0.0,
        "forbidden_hits": forbidden_hits,
        "forbidden_hit_count": sum(len(values) for values in forbidden_hits.values()),
        **split_quality,
    }


def _metric_summary(samples: Sequence[Sample]) -> dict[str, Any]:
    wall = [sample.wall_ms for sample in samples]
    rss = [sample.rss_kib for sample in samples]
    child_cpu = [sample.child_cpu_ms for sample in samples if sample.child_cpu_ms is not None]
    child_rss = [sample.child_rss_kib for sample in samples if sample.child_rss_kib is not None]
    warm = samples[1:] or samples
    warm_wall = [sample.wall_ms for sample in warm]
    warm_cpu = [sample.cpu_ms for sample in warm]
    warm_alloc = [sample.peak_alloc_kib for sample in warm]
    return {
        "sample_count": len(samples),
        "first_query_ms": round(wall[0], 4) if wall else None,
        "warm_wall_p50_ms": percentile(warm_wall, 0.50),
        "warm_wall_p95_ms": percentile(warm_wall, 0.95),
        "warm_cpu_p50_ms": percentile(warm_cpu, 0.50),
        "warm_cpu_p95_ms": percentile(warm_cpu, 0.95),
        "warm_peak_alloc_p50_kib": percentile(warm_alloc, 0.50),
        "warm_peak_alloc_p95_kib": percentile(warm_alloc, 0.95),
        "parent_rss_high_water_kib": max(rss) if rss else None,
        "child_cpu_p50_ms": percentile(child_cpu, 0.50) if child_cpu else None,
        "child_cpu_p95_ms": percentile(child_cpu, 0.95) if child_cpu else None,
        "child_rss_high_water_kib": max(child_rss) if child_rss else None,
        "rss_scope": (
            "parent-process high-water at formal timing sample boundary; "
            "child metrics separate when available"
        ),
        "cpu_scope": "formal parent-process call without tracemalloc; child metrics separate",
        "allocation_scope": (
            "parent-process tracemalloc in an independent full allocation pass; child "
            "allocation unknown; allocation wall/CPU/RPC/quality are reported separately"
        ),
    }


def _old_baseline(fixture_path: Path, repetitions: int) -> dict[str, Any]:
    old = _load_old_module()
    return cast(
        dict[str, Any], old.run_benchmark(fixture_path=fixture_path, repetitions=repetitions)
    )


def _prepare_manager(
    ledger: MemoryLedger,
    versions: Mapping[str, MemoryVersion],
    cutoff: str,
) -> BenchmarkManager:
    manager = BenchmarkManager(store=ledger, _recall_runs={})
    manager._benchmark_versions = versions  # type: ignore[attr-defined]
    manager._benchmark_cutoff = cutoff  # type: ignore[attr-defined]
    return manager


def _register_run(manager: BenchmarkManager, request: RecallRequest, cutoff: str) -> None:
    manager._recall_runs[request.context.request_id] = BenchmarkRun(
        manager=manager,
        cutoff=cutoff,
        snapshot=SimpleNamespace(role_id=ROLE_ID, role_name=ROLE_NAME),
        agent_id=AGENT_ID,
    )


def _merge_samples(
    timing_samples: Sequence[Sample], allocation_samples: Sequence[Sample]
) -> tuple[list[Sample], bool]:
    """Join two complete passes while proving that result IDs are stable."""

    if len(timing_samples) != len(allocation_samples):
        raise RuntimeError("timing and allocation passes returned different sample counts")
    returned_ids_match = all(
        timing.returned_ids == allocation.returned_ids
        for timing, allocation in zip(timing_samples, allocation_samples, strict=True)
    )
    if not returned_ids_match:
        raise RuntimeError("timing and allocation passes returned different result IDs")
    return [
        Sample(
            wall_ms=timing.wall_ms,
            cpu_ms=timing.cpu_ms,
            peak_alloc_kib=allocation.peak_alloc_kib,
            rss_kib=timing.rss_kib,
            child_cpu_ms=timing.child_cpu_ms,
            child_rss_kib=timing.child_rss_kib,
            returned_ids=timing.returned_ids,
        )
        for timing, allocation in zip(timing_samples, allocation_samples, strict=True)
    ], returned_ids_match


def _direct_mode_pass(
    fixture: FixtureData,
    cases: Sequence[BenchmarkCase],
    root: Path,
    repetitions: int,
    *,
    pass_kind: Literal["timing", "allocation"],
) -> dict[str, Any]:
    allocation = pass_kind == "allocation"
    setup_started = time.perf_counter_ns()
    ledger, versions = build_ledger(root / f"direct-{pass_kind}.sqlite3", fixture)
    cutoff = publication_cutoff(ledger)
    manager = _prepare_manager(ledger, versions, cutoff)
    setup_ms = (time.perf_counter_ns() - setup_started) / 1_000_000.0
    samples: list[Sample] = []
    returned: list[tuple[str, ...]] = []
    manager_search_ms = 0.0
    for repetition in range(repetitions):
        for index, case in enumerate(cases):
            ordinal = repetition * len(cases) + index
            request = _request(
                case,
                fixture=fixture,
                cutoff=cutoff,
                installation_id="b24-direct",
                ordinal=ordinal,
            )
            _register_run(manager, request, cutoff)

            def search_one(request: RecallRequest = request) -> CandidateBatch:
                return manager.search(request)

            sample = (
                _sample_allocation(search_one)
                if allocation
                else _sample_timing(search_one, child_pid=None)
            )
            manager_search_ms += sample.wall_ms
            manager._recall_runs.pop(request.context.request_id, None)
            samples.append(sample)
            returned.append(sample.returned_ids)
    return {
        "mode": "direct",
        "pass_kind": pass_kind,
        "status": "completed",
        "algorithm": "MemoryManager.search -> b24_fts + frozen_versions -> retrieve_memories",
        "setup_ms": round(setup_ms, 4),
        "metrics": _metric_summary(samples),
        "quality": _quality(cases, samples, repetitions),
        "model_calls": 0,
        "model_usage": "unknown",
        "token_usage": "unknown; token_budget=0 and no provider call",
        "cost_usd": "unknown",
        "rpc": {
            "frames": 0,
            "bytes": 0,
            "nested_host_api_calls": 0,
            "measurement": "no Host transport",
        },
        "phase_timing": {
            "manager_search": {
                "calls": len(samples),
                "total_ms": round(manager_search_ms, 4),
                "average_ms": round(manager_search_ms / len(samples), 4) if samples else None,
            },
            "host_invoke": {"calls": 0, "total_ms": 0.0},
            "host_minus_manager_ms": 0.0,
            "registry_assert_lease": {"calls": 0, "total_ms": 0.0},
            "registry_verify_package": {"calls": 0, "total_ms": 0.0},
            "registry_verify_certification": {"calls": 0, "total_ms": 0.0},
            "interpretation": (
                "direct reference executes the production MemoryManager.search entrypoint"
            ),
        },
        "returned_ids": returned,
        "samples": samples,
        "manager_search_calls": len(samples),
        "manager_search_ms": manager_search_ms,
    }


def _direct_mode(
    fixture: FixtureData,
    cases: Sequence[BenchmarkCase],
    root: Path,
    repetitions: int,
) -> dict[str, Any]:
    timing_pass = _direct_mode_pass(fixture, cases, root, repetitions, pass_kind="timing")
    allocation_pass = _direct_mode_pass(fixture, cases, root, repetitions, pass_kind="allocation")
    samples, returned_ids_match = _merge_samples(timing_pass["samples"], allocation_pass["samples"])
    result = dict(timing_pass)
    result.pop("pass_kind", None)
    result.pop("samples", None)
    result["metrics"] = _metric_summary(samples)
    result["quality"] = timing_pass["quality"]
    result["returned_ids"] = timing_pass["returned_ids"]
    result["allocation_pass"] = {
        "setup_ms": allocation_pass["setup_ms"],
        "sample_count": len(allocation_pass["samples"]),
        "manager_search_calls": allocation_pass["manager_search_calls"],
        "rpc": allocation_pass["rpc"],
        "returned_ids_match_timing": returned_ids_match,
        "scope": (
            "independent temporary SQLite and full fixture pass under tracemalloc; "
            "allocation wall/CPU/quality are excluded from the timing pass"
        ),
    }
    return result


async def _host_mode_pass(
    fixture: FixtureData,
    cases: Sequence[BenchmarkCase],
    root: Path,
    repetitions: int,
    mode: Literal["trusted_in_process", "isolated"],
    *,
    pass_kind: Literal["timing", "allocation"],
) -> dict[str, Any]:
    allocation = pass_kind == "allocation"
    setup_started = time.perf_counter_ns()
    ledger, versions = build_ledger(root / f"{mode}-{pass_kind}.sqlite3", fixture)
    cutoff = publication_cutoff(ledger)
    manager = _prepare_manager(ledger, versions, cutoff)
    package = ROOT / "plugins/memory-standard"
    manifest_path = package / "manifest.py"
    spec = importlib.util.spec_from_file_location(f"b24_manifest_{mode}_{pass_kind}", manifest_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("memory-standard manifest cannot be loaded")
    manifest_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(manifest_module)
    manifest = manifest_module.plugin_manifest()
    now = utc_now()
    certification = Certification(
        certification_id=f"cert.b24.benchmark.{mode}.{pass_kind}",
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
        signature_ref="signature.b24.benchmark",
    )
    config = PluginConfig(
        schema_version="operant-memory-config.v1",
        config_id=f"config-b24-benchmark-{mode}-{pass_kind}",
        revision=0,
        extraction_model_profile_id=None,
        rerank_model_profile_id=None,
        recall_token_budget=0,
        maintenance_enabled=False,
        scheduler_definition_id=None,
        secret_refs={},
        effective_at=now,
    )
    registry = PluginRegistry(
        root / f"managed-{mode}-{pass_kind}", trusted_issuers={"issuer.local"}
    )
    installation = registry.install(
        manifest,
        package.resolve(),
        dataset_id=fixture.dataset_id,
        principal_id="b24-benchmark-user",
        certification=certification,
        config=config,
        installation_id=f"installation-b24-{mode}-{pass_kind}",
    )
    counter = RpcCounter()

    def authorize_ref(context: RpcContext, ref: MemoryVersionRef) -> bool:
        version = versions.get(ref.record_id)
        return (
            version is not None
            and version.ref == ref
            and version.scope == context.scope
            and version.ref.dataset_id == context.dataset_id
        )

    def search_callback(request: RecallRequest) -> CandidateBatch:
        counter.host_api_calls += 1
        if mode == "isolated":
            counter.callback_requests += 1
            request_frame = {
                "jsonrpc": "2.0",
                "id": counter.callback_requests,
                "method": "host.search",
                "params": request.model_dump(mode="json"),
            }
            counter.callback_request_bytes += len(encode_rpc(request_frame, max_bytes=1_000_000))
        started = time.perf_counter_ns()
        try:
            return manager.search(request)
        finally:
            counter.manager_search_calls += 1
            counter.manager_search_ms += (time.perf_counter_ns() - started) / 1_000_000.0

    host = PluginHost(
        registry,
        callbacks=HostCallbacks(
            authorize_memory_ref=authorize_ref,
            search=search_callback,
        ),
    )
    binding = host.bind(installation.installation_id)
    host.enable(binding.binding_id)
    spawn_started = time.perf_counter_ns()
    try:
        admission = await host.start(installation.installation_id, mode=mode)
    except IsolationUnavailableError:
        with suppress(Exception):
            await host.close()
        with suppress(Exception):
            registry.close()
        raise
    spawn_ms = (time.perf_counter_ns() - spawn_started) / 1_000_000.0

    def instrument_registry_method(name: str, counter_name: str, time_name: str) -> None:
        original = getattr(registry, name)

        def timed(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter_ns()
            try:
                return original(*args, **kwargs)
            finally:
                setattr(
                    counter,
                    counter_name,
                    int(getattr(counter, counter_name)) + 1,
                )
                setattr(
                    counter,
                    time_name,
                    float(getattr(counter, time_name))
                    + (time.perf_counter_ns() - started) / 1_000_000.0,
                )

        setattr(registry, name, timed)

    instrument_registry_method(
        "assert_lease", "registry_assert_lease_calls", "registry_assert_lease_ms"
    )
    instrument_registry_method(
        "verify_package", "registry_verify_package_calls", "registry_verify_package_ms"
    )
    instrument_registry_method(
        "verify_certification",
        "registry_verify_certification_calls",
        "registry_verify_certification_ms",
    )

    child_pid: int | None = None
    if mode == "isolated":
        slot = host._engines[installation.installation_id]  # noqa: SLF001 - benchmark instrumentation
        engine = cast(StdioPluginEngine, slot.engine)
        child_pid = getattr(engine.process, "pid", None)
        original_write = engine._write  # noqa: SLF001
        original_read = engine._read_until_response  # noqa: SLF001

        async def counted_write(payload: Mapping[str, Any], *, deadline: Any = None) -> None:
            encoded = encode_rpc(payload, max_bytes=slot.limits.max_request_bytes)
            counter.outgoing_frames += 1
            counter.outgoing_bytes += len(encoded)
            await original_write(payload, deadline=deadline)

        async def counted_read(rpc_id: int, host_api: Any) -> dict[str, Any]:
            result = await original_read(rpc_id, host_api)
            counter.incoming_frames += 1
            counter.incoming_bytes += len(
                encode_rpc(result, max_bytes=slot.limits.max_response_bytes)
            )
            return result

        engine._write = counted_write
        engine._read_until_response = counted_read

    samples: list[Sample] = []
    returned: list[tuple[str, ...]] = []
    leases: dict[str, Any] = {}
    for workspace in {case.project_scope for case in cases}:
        leases[workspace] = host.start_run(
            binding.binding_id,
            run_id=f"b24-benchmark-{mode}-{workspace}",
            scope=_scope(workspace),
        )
    # Host.start establishes the process; the typed health response proves that
    # the installed engine has finished loading. Charge both to bootstrap, not
    # to the first retrieval query. No fixture query is run as a warm-up.
    readiness_started = time.perf_counter_ns()
    try:
        readiness = await host.lifecycle(next(iter(leases.values())), operation="health")
        if readiness.state != "ready":
            raise RuntimeError("installed engine health handshake did not report ready")
    except BaseException:
        with suppress(Exception):
            await host.close()
        registry.close()
        raise
    readiness_ms = (time.perf_counter_ns() - readiness_started) / 1_000_000.0
    setup_ms = (time.perf_counter_ns() - setup_started) / 1_000_000.0
    startup_rpc = {"frames": counter.rpc_frames, "bytes": counter.rpc_bytes}
    # The callbacks close over this variable. Keep startup work separate from
    # the subsequent query counters, while retaining its RPC cost in the report.
    counter = RpcCounter()
    try:
        for repetition in range(repetitions):
            for index, case in enumerate(cases):
                ordinal = repetition * len(cases) + index
                request = _request(
                    case,
                    fixture=fixture,
                    cutoff=cutoff,
                    installation_id=installation.installation_id,
                    ordinal=ordinal,
                )
                _register_run(manager, request, cutoff)
                lease = leases[case.project_scope]
                request = request.model_copy(
                    update={
                        "context": request.context.model_copy(
                            update={
                                "binding_epoch": lease.binding_epoch,
                                "permission_epoch": lease.permission_epoch,
                                "lease_fencing": lease.lease_fencing,
                            }
                        )
                    }
                )

                # Registration uses the original immutable request ID, so the
                # copied context still points at the same run entry.
                async def invoke_one(
                    request: RecallRequest = request, lease: Any = lease
                ) -> CandidateBatch:
                    return cast(CandidateBatch, await host.invoke(lease, "recall", request))

                sample = await (
                    _async_sample_allocation(invoke_one)
                    if allocation
                    else _async_sample_timing(invoke_one, child_pid=child_pid)
                )
                samples.append(sample)
                returned.append(sample.returned_ids)
                manager._recall_runs.pop(request.context.request_id, None)
    finally:
        with suppress(Exception):
            await host.close()
        with suppress(Exception):
            registry.close()
    return {
        "mode": mode,
        "pass_kind": pass_kind,
        "status": "completed",
        "algorithm": (
            "PluginHost -> memory-standard -> Host.search -> MemoryManager.search -> b24_fts"
        ),
        "admission": {
            "mode": admission.mode,
            "certification_id": admission.certification_id,
            "isolation_evidence_ref": admission.isolation_evidence_ref,
        },
        "setup_ms": round(setup_ms, 4),
        "host_startup": {
            "spawn_ms": round(spawn_ms, 4),
            "health_handshake_ms": round(readiness_ms, 4),
            "total_ms": round(spawn_ms + readiness_ms, 4),
            "response": readiness.model_dump(mode="json"),
            "rpc": startup_rpc,
        },
        "metrics": _metric_summary(samples),
        "phase_timing": counter.phase_report(
            host_total_ms=sum(sample.wall_ms for sample in samples)
        ),
        "quality": _quality(cases, samples, repetitions),
        "model_calls": 0,
        "model_usage": "unknown",
        "token_usage": "unknown; token_budget=0 and no provider call",
        "cost_usd": "unknown",
        "rpc": {
            "frames": counter.rpc_frames,
            "bytes": counter.rpc_bytes,
            "outgoing_frames": counter.outgoing_frames,
            "outgoing_bytes": counter.outgoing_bytes,
            "incoming_frames": counter.incoming_frames,
            "incoming_bytes": counter.incoming_bytes,
            "callback_request_frames": counter.callback_requests,
            "callback_request_bytes": counter.callback_request_bytes,
            "nested_host_api_calls": counter.host_api_calls,
            "measurement": (
                "actual stdio JSON-RPC frames; callback request bytes "
                "reconstructed from typed payload"
                if mode == "isolated"
                else "in-process calls are not RPC frames"
            ),
        },
        "returned_ids": returned,
        "samples": samples,
        "manager_search_calls": counter.manager_search_calls,
        "manager_search_ms": counter.manager_search_ms,
    }


async def _host_mode(
    fixture: FixtureData,
    cases: Sequence[BenchmarkCase],
    root: Path,
    repetitions: int,
    mode: Literal["trusted_in_process", "isolated"],
) -> dict[str, Any]:
    """Run complete timing and allocation passes on independent Host state."""

    timing_pass = await _host_mode_pass(
        fixture,
        cases,
        root,
        repetitions,
        mode,
        pass_kind="timing",
    )
    allocation_pass = await _host_mode_pass(
        fixture,
        cases,
        root,
        repetitions,
        mode,
        pass_kind="allocation",
    )
    samples, returned_ids_match = _merge_samples(timing_pass["samples"], allocation_pass["samples"])
    result = dict(timing_pass)
    result.pop("pass_kind", None)
    result.pop("samples", None)
    result["metrics"] = _metric_summary(samples)
    result["quality"] = timing_pass["quality"]
    result["returned_ids"] = timing_pass["returned_ids"]
    result["allocation_pass"] = {
        "setup_ms": allocation_pass["setup_ms"],
        "host_startup": allocation_pass["host_startup"],
        "sample_count": len(allocation_pass["samples"]),
        "manager_search_calls": allocation_pass["manager_search_calls"],
        "rpc": allocation_pass["rpc"],
        "phase_calls": {
            name: entry["calls"]
            for name, entry in allocation_pass["phase_timing"].items()
            if isinstance(entry, dict) and "calls" in entry
        },
        "returned_ids_match_timing": returned_ids_match,
        "scope": (
            "independent temporary SQLite, registry, and Host pass under tracemalloc; "
            "allocation wall/CPU/RPC/quality are reported separately"
        ),
    }
    return result


async def _run_host_mode(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return await _host_mode(*args, **kwargs)


def _ratio(actual: float | None, reference: float | None) -> float | None:
    if actual is None or reference is None or reference <= 0:
        return None
    return round(actual / reference, 4)


def _bounded_gate(
    actual: float | None,
    reference: float | None,
    ratio_limit: float,
    *,
    slack: float = 0.0,
    unit: str,
) -> dict[str, Any]:
    """Return a transparent ratio-plus-slack gate result."""

    if actual is None or reference is None or reference <= 0:
        return {
            "actual": actual,
            "reference": reference,
            "ratio": None,
            "ratio_limit": ratio_limit,
            "slack": slack,
            "unit": unit,
            "allowed_actual": None,
            "passed": False,
            "status": "unavailable",
        }
    allowed = reference * ratio_limit + slack
    return {
        "actual": round(actual, 4),
        "reference": round(reference, 4),
        "ratio": _ratio(actual, reference),
        "ratio_limit": ratio_limit,
        "slack": slack,
        "unit": unit,
        "allowed_actual": round(allowed, 4),
        "passed": actual <= allowed,
        "status": "completed",
    }


def _all_passed(checks: Mapping[str, Any]) -> bool:
    return all(
        value is True or (isinstance(value, Mapping) and value.get("passed") is True)
        for value in checks.values()
    )


def evaluate_gates(
    old_report: Mapping[str, Any],
    results: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Evaluate frozen gates without changing their thresholds."""

    old_direct = cast(Mapping[str, Any], old_report["strategies"]["old_direct_query"])
    old_timing = cast(Mapping[str, Any], old_direct["timing"])
    old_quality = cast(Mapping[str, Any], old_direct["quality"])
    gates_module = _load_old_module()
    task_gates = cast(Mapping[str, float], gates_module.TASK_PERFORMANCE_GATES)
    host_gates = cast(Mapping[str, Mapping[str, float]], gates_module.HOST_PERFORMANCE_GATES)
    direct_metrics = cast(Mapping[str, Any], results["direct"]["metrics"])
    direct_quality = cast(Mapping[str, Any], results["direct"]["quality"])
    old_holdout = cast(Mapping[str, Any], old_quality["holdout"])
    new_holdout = cast(Mapping[str, Any], direct_quality["holdout"])
    direct_checks = {
        "holdout_recall_not_below_old_direct": new_holdout["macro_recall"]
        >= old_holdout["macro_recall"],
        "forbidden_hits_zero": direct_quality["forbidden_hit_count"] == 0,
        "warm_wall_p95": _bounded_gate(
            direct_metrics["warm_wall_p95_ms"],
            old_timing["warm_wall_p95_ms"],
            task_gates["warm_wall_p95_max_regression_ratio"],
            slack=task_gates["absolute_wall_slack_ms"],
            unit="ms",
        ),
        "first_query_wall": _bounded_gate(
            direct_metrics["first_query_ms"],
            old_timing["first_query_ms"],
            task_gates["first_query_wall_max_regression_ratio"],
            slack=task_gates["absolute_wall_slack_ms"],
            unit="ms",
        ),
        "warm_cpu_p95": _bounded_gate(
            direct_metrics["warm_cpu_p95_ms"],
            old_timing["warm_cpu_p95_ms"],
            task_gates["warm_cpu_p95_max_regression_ratio"],
            unit="ms",
        ),
        "warm_peak_alloc_p95": _bounded_gate(
            direct_metrics["warm_peak_alloc_p95_kib"],
            old_timing["warm_peak_alloc_p95_kib"],
            task_gates["warm_peak_alloc_p95_max_regression_ratio"],
            slack=task_gates["absolute_peak_alloc_slack_kib"],
            unit="KiB",
        ),
    }
    direct_passed = _all_passed(direct_checks)
    host_checks: dict[str, Any] = {}
    for mode, key in (
        ("trusted_in_process", "future_host_in_process"),
        ("isolated", "future_host_isolated"),
    ):
        result = results.get(mode)
        if result is None or result.get("status") != "completed":
            host_checks[mode] = {"status": "unavailable_or_not_run", "passed": False}
            continue
        metrics = cast(Mapping[str, Any], result["metrics"])
        limits = host_gates[key]
        quality = cast(Mapping[str, Any], result["quality"])
        host_checks_for_mode = {
            "holdout_recall_not_below_old_direct": quality["holdout"]["macro_recall"]
            >= old_holdout["macro_recall"],
            "forbidden_hits_zero": quality["forbidden_hit_count"] == 0,
            "bootstrap_wall": _bounded_gate(
                result.get("setup_ms"),
                results["direct"].get("setup_ms"),
                limits["bootstrap_wall_max_regression_ratio"],
                unit="ms",
            ),
            "warm_wall_p95": _bounded_gate(
                metrics["warm_wall_p95_ms"],
                direct_metrics["warm_wall_p95_ms"],
                limits["warm_wall_p95_max_regression_ratio"],
                unit="ms",
            ),
            "first_query_wall": _bounded_gate(
                metrics["first_query_ms"],
                direct_metrics["first_query_ms"],
                limits["first_query_wall_max_regression_ratio"],
                unit="ms",
            ),
            "warm_cpu_p95": _bounded_gate(
                metrics["warm_cpu_p95_ms"],
                direct_metrics["warm_cpu_p95_ms"],
                limits["warm_cpu_p95_max_regression_ratio"],
                unit="ms",
            ),
            "warm_peak_alloc_p95": _bounded_gate(
                metrics["warm_peak_alloc_p95_kib"],
                direct_metrics["warm_peak_alloc_p95_kib"],
                limits["warm_peak_alloc_p95_max_regression_ratio"],
                unit="KiB",
            ),
        }
        host_checks[mode] = {
            "reference": "new_direct",
            "checks": host_checks_for_mode,
            "passed": _all_passed(host_checks_for_mode),
        }
    return {
        "status": "not_passed"
        if not direct_passed or not all(item.get("passed") for item in host_checks.values())
        else "passed",
        "frozen_task_gates": dict(task_gates),
        "frozen_host_gates": {key: dict(value) for key, value in host_gates.items()},
        "direct": {"passed": direct_passed, "checks": direct_checks},
        "host": host_checks,
        "quality_reference": {
            "old_direct_holdout_recall": old_holdout["macro_recall"],
            "new_direct_holdout_recall": new_holdout["macro_recall"],
        },
    }


def gate_limitations(gates: Mapping[str, Any]) -> list[str]:
    """Render failure statements from measured gate results."""

    limitations: list[str] = []
    if gates.get("status") == "passed":
        return limitations
    direct = cast(Mapping[str, Any], gates.get("direct", {}))
    for name, check in cast(Mapping[str, Any], direct.get("checks", {})).items():
        if isinstance(check, Mapping) and check.get("passed") is False:
            limitations.append(
                "direct gate failed: "
                f"{name} actual={check.get('actual')} {check.get('unit', '')} "
                f"allowed={check.get('allowed_actual')} {check.get('unit', '')}"
            )
        elif check is False:
            limitations.append(f"direct gate failed: {name}")
    for mode, value in cast(Mapping[str, Any], gates.get("host", {})).items():
        if not isinstance(value, Mapping) or value.get("passed") is not False:
            continue
        checks = value.get("checks")
        failed = [
            name
            for name, check in cast(Mapping[str, Any], checks or {}).items()
            if check is False or (isinstance(check, Mapping) and check.get("passed") is False)
        ]
        reason = ", ".join(failed) if failed else str(value.get("status", "unknown"))
        limitations.append(f"{mode} Host gate failed: {reason}")
    return limitations


def _clean_for_json(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_clean_for_json(item) for item in value]
    if isinstance(value, list):
        return [_clean_for_json(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _clean_for_json(item) for key, item in value.items()}
    return value


def run_benchmark(
    *,
    fixture_path: Path = DEFAULT_FIXTURE,
    repetitions: int = REPETITIONS_DEFAULT,
    modes: Sequence[str] = MODES,
) -> dict[str, Any]:
    """Run the fixed comparison in temporary databases and return JSON data."""

    if repetitions < 1:
        raise ValueError("repetitions must be positive")
    unknown = set(modes).difference(MODES)
    if unknown:
        raise ValueError(f"unknown benchmark mode: {sorted(unknown)}")
    fixture = load_fixture(fixture_path)
    old_report = _old_baseline(fixture_path, repetitions)
    results: dict[str, Mapping[str, Any]] = {}
    with TemporaryDirectory(prefix="operant-b24-memory-benchmark-") as temp_dir:
        root = Path(temp_dir)
        # Direct retrieval is the frozen task reference for both Host modes,
        # so it is always collected even when the CLI limits a run to one
        # requested Host mode.
        results["direct"] = _direct_mode(fixture, fixture.cases, root, repetitions)
        for mode in ("trusted_in_process", "isolated"):
            if mode not in modes:
                continue
            try:
                results[mode] = asyncio.run(
                    _run_host_mode(fixture, fixture.cases, root, repetitions, mode)
                )
            except IsolationUnavailableError as exc:
                results[mode] = {
                    "mode": mode,
                    "status": "unavailable",
                    "reason": str(exc),
                    "model_calls": 0,
                    "model_usage": "unknown",
                }
    gate_results = evaluate_gates(old_report, results)
    limitations = [
        "The fixed fixture is synthetic and cannot establish production semantic quality.",
        "The run makes zero model calls; provider usage and end-to-end task cost are unknown.",
        "Direct and Host parent CPU/RSS/allocation scopes differ from isolated child metrics.",
        "Isolated callback request bytes are reconstructed from typed payloads; "
        "other stdio frames are counted at transport boundaries.",
    ]
    limitations.extend(gate_limitations(gate_results))
    report = {
        "schema": "operant.b2_4.memory-performance-report.v1",
        "implementation": {
            "source_root": str(ROOT),
            "head": _git_head(ROOT),
            "retrieval_policy_version": POLICY.version,
            "manager_entrypoint": "MemoryManager.search",
        },
        "dataset": {
            "fixture_path": str(fixture_path.resolve()),
            "file_sha256": fixture.file_sha256,
            "dataset_id": fixture.dataset_id,
            "version": fixture.version,
            "memory_count": len(fixture.memories),
            "case_count": len(fixture.cases),
            "split_counts": {
                split: sum(case.split == split for case in fixture.cases)
                for split in ("development", "holdout")
            },
        },
        "run": {
            "repetitions": repetitions,
            "requested_modes": list(modes),
            "modes": list(results),
            "database": "temporary isolated SQLite per mode",
            "user_database_read": False,
            "environment_file_read": False,
            "network": False,
            "model_calls": 0,
            "model_usage": "unknown",
            "python": platform.python_version(),
            "platform": platform.platform(aliased=True),
        },
        "policy": {
            "version": POLICY.version,
            "candidate_limit": POLICY.candidate_limit,
            "diversity_limit": POLICY.diversity_limit,
            "max_query_groups": POLICY.max_query_groups,
            "max_terms_per_group": POLICY.max_terms_per_group,
        },
        "old_baseline": {
            "implementation": old_report["implementation"],
            "quality": {
                mode: old_report["strategies"][mode]["quality"]
                for mode in ("old_direct_query", "old_recent_entries")
            },
            "timing": {
                mode: old_report["strategies"][mode]["timing"]
                for mode in ("old_direct_query", "old_recent_entries")
            },
            "model_calls": 0,
            "model_usage": "unknown",
        },
        "results": _clean_for_json(dict(results)),
        "gates": _clean_for_json(gate_results),
        "limitations": limitations,
    }
    return report


def _git_head(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            check=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return result.stdout.strip() or "unknown"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--repetitions", type=int, default=REPETITIONS_DEFAULT)
    parser.add_argument(
        "--mode",
        action="append",
        choices=MODES,
        dest="modes",
        help="limit the run to one or more modes; default runs all",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    modes = tuple(args.modes) if args.modes else MODES
    try:
        report = run_benchmark(
            fixture_path=args.fixture,
            repetitions=args.repetitions,
            modes=modes,
        )
    except (OSError, ValueError, RuntimeError, AssertionError) as exc:
        print(f"benchmark failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return 0 if report["gates"]["status"] == "passed" else 3


if __name__ == "__main__":
    raise SystemExit(main())
