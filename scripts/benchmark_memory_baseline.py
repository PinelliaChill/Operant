"""Run the B2-1 MP-0.5 synthetic Memory baseline.

The benchmark deliberately exercises the existing ``ApplicationService``
Memory query path against an isolated SQLite database.  It does not load
environment secrets, open a user database, call a model, or implement a new
index.  The recent-entry variant is the empty-query form used by the current
workflow Memory context path.  The no-memory control returns an empty result
without constructing a Service or opening SQLite.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import resource
import subprocess
import sys
import tempfile
import time
import tracemalloc
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from operant.application.service import ApplicationService
from operant.domain.memory import Memory, MemoryKind, MemoryStatus
from operant.domain.messages import Message, ToolDefinition
from operant.domain.models import ModelProfile, RolePreset, RoleSnapshot
from operant.persistence.sqlite import SQLiteStore

DEFAULT_FIXTURE = (
    Path(__file__).resolve().parent.parent / "tests/fixtures/b2_1/memory_baseline.json"
)
DATASET_SCHEMA = "operant.b2_1.memory-baseline.v1"
BASE_IMPLEMENTATION_HEAD = "ecb00437e9a44a5e79d54e8cf4944fd0d456bf02"
STRATEGY_DIRECT = "old_direct_query"
STRATEGY_RECENT = "old_recent_entries"
STRATEGY_NO_MEMORY = "no_memory"
STRATEGIES = (STRATEGY_NO_MEMORY, STRATEGY_DIRECT, STRATEGY_RECENT)

# These values are frozen for future task-retrieval candidate comparisons.
# They do not mix task-algorithm quality with Host overhead.
TASK_PERFORMANCE_GATES: Mapping[str, float] = {
    "warm_wall_p95_max_regression_ratio": 1.25,
    "first_query_wall_max_regression_ratio": 1.50,
    "warm_cpu_p95_max_regression_ratio": 1.25,
    "warm_peak_alloc_p95_max_regression_ratio": 1.50,
    "absolute_wall_slack_ms": 2.0,
    "absolute_peak_alloc_slack_kib": 256.0,
}

# Future Host modes use the same direct retrieval algorithm and compare only
# execution overhead against ``old_direct_query``.  The isolated mode has a
# larger, separately frozen allowance for process/RPC overhead.
HOST_PERFORMANCE_GATES: Mapping[str, Mapping[str, float]] = {
    "future_host_in_process": {
        "warm_wall_p95_max_regression_ratio": 1.25,
        "bootstrap_wall_max_regression_ratio": 1.50,
        "first_query_wall_max_regression_ratio": 1.50,
        "warm_cpu_p95_max_regression_ratio": 1.25,
        "warm_peak_alloc_p95_max_regression_ratio": 1.50,
    },
    "future_host_isolated": {
        "warm_wall_p95_max_regression_ratio": 2.00,
        "bootstrap_wall_max_regression_ratio": 3.00,
        "first_query_wall_max_regression_ratio": 3.00,
        "warm_cpu_p95_max_regression_ratio": 2.00,
        "warm_peak_alloc_p95_max_regression_ratio": 2.00,
    },
}


class NoModelProvider:
    """Provider sentinel that makes accidental model use fail closed."""

    async def list_models(self, *, base_url: str, secret_ref: str) -> list[str]:
        del base_url, secret_ref
        raise AssertionError("B2-1 baseline must not perform model discovery")

    def stream(
        self,
        *,
        snapshot: RoleSnapshot,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> Any:
        del snapshot, messages, tools
        raise AssertionError("B2-1 baseline must not call a model")


@dataclass(frozen=True)
class BaselineCase:
    id: str
    split: str
    category: str
    query: str
    project_scope: str
    expected_ids: tuple[str, ...]
    forbidden_ids: tuple[str, ...]
    limit: int = 5


@dataclass(frozen=True)
class Fixture:
    dataset_id: str
    version: int
    memories: tuple[Mapping[str, Any], ...]
    cases: tuple[BaselineCase, ...]
    sha256: str
    file_sha256: str


@dataclass(frozen=True)
class Sample:
    returned_ids: tuple[str, ...]
    wall_ms: float
    cpu_ms: float
    peak_alloc_kib: float


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _as_string(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"fixture {field} must be a non-empty string")
    return value


def _as_string_tuple(value: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"fixture {field} must be a list of strings")
    return tuple(value)


def _forbidden_scope_ids(
    records: Sequence[Mapping[str, Any]],
    *,
    project_scope: str,
) -> set[str]:
    """Return every fixture row unavailable to the fixed baseline reader."""

    forbidden: set[str] = set()
    for record in records:
        roles = record.get("role_scope") or ()
        if not isinstance(roles, (list, tuple)):
            raise ValueError(f"memory {record.get('id')} role_scope must be a list")
        visible = (
            record.get("project_scope") == project_scope
            and record.get("status") == MemoryStatus.ACTIVE.value
            and (
                not roles
                or "role_b2_1_baseline" in roles
                or "B2-1 baseline reader" in roles
                or "*" in roles
            )
        )
        if not visible:
            forbidden.add(_as_string(record.get("id"), field="memory.id"))
    return forbidden


def load_fixture(path: Path = DEFAULT_FIXTURE) -> Fixture:
    """Load and validate the fixed synthetic dataset and return its digest."""

    fixture_bytes = path.read_bytes()
    raw = json.loads(fixture_bytes.decode("utf-8"))
    if not isinstance(raw, dict) or raw.get("schema") != DATASET_SCHEMA:
        raise ValueError(f"unsupported B2-1 fixture schema in {path}")
    raw_memories = raw.get("memories")
    raw_cases = raw.get("cases")
    if not isinstance(raw_memories, list) or not isinstance(raw_cases, list):
        raise ValueError("fixture memories and cases must be lists")
    memory_ids: set[str] = set()
    for record in raw_memories:
        if not isinstance(record, dict):
            raise ValueError("fixture memory entries must be objects")
        memory_id = _as_string(record.get("id"), field="memory.id")
        if memory_id in memory_ids:
            raise ValueError(f"duplicate fixture memory id: {memory_id}")
        memory_ids.add(memory_id)
        _as_string(record.get("kind"), field="memory.kind")
        _as_string(record.get("content"), field="memory.content")
        _as_string(record.get("created_at"), field="memory.created_at")
    if not 30 <= len(raw_cases) <= 50:
        raise ValueError("B2-1 fixture must contain 30-50 cases")
    cases: list[BaselineCase] = []
    case_ids: set[str] = set()
    for raw_case in raw_cases:
        if not isinstance(raw_case, dict):
            raise ValueError("fixture case entries must be objects")
        case_id = _as_string(raw_case.get("id"), field="case.id")
        if case_id in case_ids:
            raise ValueError(f"duplicate fixture case id: {case_id}")
        case_ids.add(case_id)
        split = _as_string(raw_case.get("split"), field="case.split")
        if split not in {"development", "holdout"}:
            raise ValueError(f"case {case_id} has an invalid split: {split}")
        expected_ids = _as_string_tuple(raw_case.get("expected_ids"), field="case.expected_ids")
        forbidden_ids = _as_string_tuple(raw_case.get("forbidden_ids"), field="case.forbidden_ids")
        if set(expected_ids).intersection(forbidden_ids):
            raise ValueError(f"case {case_id} marks one ID as both expected and forbidden")
        unknown_ids = (set(expected_ids) | set(forbidden_ids)).difference(memory_ids)
        if unknown_ids:
            raise ValueError(f"case {case_id} references unknown memories: {sorted(unknown_ids)}")
        project_scope = _as_string(raw_case.get("project_scope"), field="case.project_scope")
        expected_forbidden = _forbidden_scope_ids(raw_memories, project_scope=project_scope)
        if set(forbidden_ids) != expected_forbidden:
            missing = sorted(expected_forbidden.difference(forbidden_ids))
            extra = sorted(set(forbidden_ids).difference(expected_forbidden))
            raise ValueError(
                f"case {case_id} must list complete forbidden scope IDs; "
                f"missing={missing}, extra={extra}"
            )
        limit = raw_case.get("limit", 5)
        if not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError(f"case {case_id} limit must be between 1 and 100")
        cases.append(
            BaselineCase(
                id=case_id,
                split=split,
                category=_as_string(raw_case.get("category"), field="case.category"),
                query=_as_string(raw_case.get("query"), field="case.query"),
                project_scope=project_scope,
                expected_ids=expected_ids,
                forbidden_ids=forbidden_ids,
                limit=limit,
            )
        )
    split_counts = {
        split: sum(case.split == split for case in cases) for split in ("development", "holdout")
    }
    if split_counts["development"] == 0 or split_counts["holdout"] == 0:
        raise ValueError("fixture must contain both development and holdout cases")
    holdout_scopes = {case.project_scope for case in cases if case.split == "holdout"}
    if holdout_scopes != {"workspace-gamma"}:
        raise ValueError("holdout cases must use the independent workspace-gamma project")
    gamma_records = [
        record for record in raw_memories if record.get("project_scope") == "workspace-gamma"
    ]
    if not gamma_records or any(
        not _as_string(
            record.get("source_session_id"), field="memory.source_session_id"
        ).startswith("source-gamma-")
        or _as_string(record.get("created_at"), field="memory.created_at")
        < "2026-02-01T00:00:00+00:00"
        for record in gamma_records
    ):
        raise ValueError("workspace-gamma holdout records must use source-gamma-* after the cutoff")
    fixture_hash = hashlib.sha256(_canonical_json(raw)).hexdigest()
    return Fixture(
        dataset_id=_as_string(raw.get("dataset_id"), field="dataset_id"),
        version=int(raw.get("version", 0)),
        memories=tuple(raw_memories),
        cases=tuple(cases),
        sha256=fixture_hash,
        file_sha256=hashlib.sha256(fixture_bytes).hexdigest(),
    )


def _memory_from_record(record: Mapping[str, Any]) -> Memory:
    role_scope = record.get("role_scope", ())
    if not isinstance(role_scope, (list, tuple)):
        raise ValueError(f"memory {record.get('id')} role_scope must be a list")
    return Memory(
        id=_as_string(record.get("id"), field="memory.id"),
        kind=MemoryKind(_as_string(record.get("kind"), field="memory.kind")),
        content=_as_string(record.get("content"), field="memory.content"),
        project_scope=record.get("project_scope"),
        role_scope=tuple(str(item) for item in role_scope),
        source_session_id=record.get("source_session_id"),
        source_task=record.get("source_task"),
        confidence=float(record.get("confidence", 0.5)),
        status=MemoryStatus(_as_string(record.get("status"), field="memory.status")),
        created_at=datetime.fromisoformat(
            _as_string(record.get("created_at"), field="memory.created_at")
        ),
    )


def _build_service(db_path: Path, fixture: Fixture) -> tuple[ApplicationService, str, RoleSnapshot]:
    store = SQLiteStore(db_path)
    service = ApplicationService(store, NoModelProvider())
    service.initialize()
    service.add_model_profile(
        ModelProfile(
            id="model_b2_1_baseline",
            name="B2-1 baseline sentinel",
            model_id="b2-1-no-model",
            base_url="https://b2-1.invalid/v1",
            secret_ref="B2_1_UNUSED_SECRET_REF",
        )
    )
    role = service.create_role(
        RolePreset(
            id="role_b2_1_baseline",
            name="B2-1 baseline reader",
            system_prompt="Synthetic baseline role; no provider call is permitted.",
            model_profile_id="model_b2_1_baseline",
            memory_scope="read: [project, episodic]; write: []",
        )
    )
    session = service.create_session(role.id)
    for record in fixture.memories:
        service.store.create_memory(_memory_from_record(record))
    return service, session.id, session.role_snapshot


def _invoke_strategy(
    strategy: str,
    service: ApplicationService | None,
    *,
    session_id: str,
    snapshot: RoleSnapshot | None,
    case: BaselineCase,
) -> list[Memory]:
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown baseline strategy: {strategy}")
    if strategy == STRATEGY_NO_MEMORY:
        return []
    if service is None or snapshot is None:
        raise RuntimeError(f"{strategy} requires the isolated Service fixture")
    # ``old_recent_entries`` intentionally uses the empty query employed by
    # SequentialCodingWorkflow._memory_context.  Both branches remain calls
    # into the existing service/store implementation; no new FTS is built here.
    query = case.query if strategy == STRATEGY_DIRECT else ""
    return service.query_memories(
        query,
        snapshot=snapshot,
        session_id=session_id,
        project_scope=case.project_scope,
        kinds=(MemoryKind.PROJECT,),
        include_candidates=False,
        limit=case.limit,
    )


def _resource_cpu_ms(usage: resource.struct_rusage) -> float:
    return (usage.ru_utime + usage.ru_stime) * 1000.0


def _measure_timing(call: Callable[[], list[Memory]]) -> Sample:
    """Measure a formal call with tracemalloc disabled."""

    if tracemalloc.is_tracing():
        raise RuntimeError("formal timing pass requires tracemalloc to be disabled")
    before_cpu = _resource_cpu_ms(resource.getrusage(resource.RUSAGE_SELF))
    started = time.perf_counter_ns()
    values = call()
    elapsed_ns = time.perf_counter_ns() - started
    after_cpu = _resource_cpu_ms(resource.getrusage(resource.RUSAGE_SELF))
    return Sample(
        returned_ids=tuple(memory.id for memory in values),
        wall_ms=elapsed_ns / 1_000_000.0,
        cpu_ms=max(0.0, after_cpu - before_cpu),
        peak_alloc_kib=0.0,
    )


def _measure_allocation(call: Callable[[], list[Memory]]) -> Sample:
    """Run one allocation-only pass; wall/CPU are deliberately excluded."""

    tracemalloc.start()
    before_alloc, _ = tracemalloc.get_traced_memory()
    try:
        values = call()
        _, peak_alloc = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return Sample(
        returned_ids=tuple(memory.id for memory in values),
        wall_ms=0.0,
        cpu_ms=0.0,
        peak_alloc_kib=max(0.0, peak_alloc - before_alloc) / 1024.0,
    )


def _measure(call: Callable[[], list[Memory]]) -> Sample:
    """Backward-compatible alias for the formal timing pass."""

    return _measure_timing(call)


def _percentile(values: Iterable[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    if not 0.0 < fraction <= 1.0:
        raise ValueError("percentile fraction must be in (0, 1]")
    index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
    return round(ordered[index], 4)


def _rss_high_water_kib() -> float:
    value = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    # macOS reports bytes; Linux and the other Unix implementations normally
    # report KiB.  The report names the source so this remains auditable.
    if sys.platform == "darwin":
        return round(value / 1024.0, 2)
    return round(value, 2)


def _git_head(root: Path) -> str:
    """Return the checked-out implementation revision without reading project data."""

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    head = result.stdout.strip()
    return head if head else "unknown"


def _quality_summary(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not records:
        return {
            "case_count": 0,
            "macro_precision": 0.0,
            "macro_recall": 0.0,
            "security_violations": [],
        }
    precisions = [float(record["precision"]) for record in records]
    recalls = [float(record["recall"]) for record in records]
    violations = [
        {"case_id": record["case_id"], "forbidden_hits": record["forbidden_hits"]}
        for record in records
        if record["forbidden_hits"]
    ]
    return {
        "case_count": len(records),
        "macro_precision": round(sum(precisions) / len(precisions), 4),
        "macro_recall": round(sum(recalls) / len(recalls), 4),
        "expected_items": sum(len(record["expected_ids"]) for record in records),
        "retrieved_items": sum(len(record["returned_ids"]) for record in records),
        "relevant_items": sum(len(record["relevant_ids"]) for record in records),
        "security_violations": violations,
    }


def _run_strategy_pass(
    strategy: str,
    fixture: Fixture,
    *,
    repetitions: int,
    db_path: Path,
    allocation: bool,
) -> dict[str, Any]:
    service: ApplicationService | None = None
    session_id = ""
    snapshot: RoleSnapshot | None = None
    bootstrap_started = time.perf_counter_ns()
    if strategy != STRATEGY_NO_MEMORY:
        service, session_id, snapshot = _build_service(db_path, fixture)
    bootstrap_ms = (time.perf_counter_ns() - bootstrap_started) / 1_000_000.0
    samples: list[Sample] = []
    measure = _measure_allocation if allocation else _measure_timing
    try:
        for case in fixture.cases:
            for _ in range(repetitions):
                samples.append(
                    measure(
                        lambda case=case: _invoke_strategy(
                            strategy,
                            service,
                            session_id=session_id,
                            snapshot=snapshot,
                            case=case,
                        )
                    )
                )
    finally:
        if service is not None:
            service.close()
    return {
        "samples": samples,
        "bootstrap_ms": bootstrap_ms,
        "call_count": len(samples),
        "rss_high_water_kib": _rss_high_water_kib(),
    }


def _run_strategy(
    strategy: str,
    fixture: Fixture,
    *,
    repetitions: int,
    temp_root: Path,
) -> dict[str, Any]:
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown baseline strategy: {strategy}")
    timing_pass = _run_strategy_pass(
        strategy,
        fixture,
        repetitions=repetitions,
        db_path=temp_root / f"{strategy}-timing.sqlite3",
        allocation=False,
    )
    allocation_pass = _run_strategy_pass(
        strategy,
        fixture,
        repetitions=repetitions,
        db_path=temp_root / f"{strategy}-allocation.sqlite3",
        allocation=True,
    )
    timing_samples = timing_pass["samples"]
    allocation_samples = allocation_pass["samples"]
    if len(timing_samples) != len(allocation_samples):
        raise RuntimeError("timing and allocation passes returned different sample counts")
    returned_ids_match = all(
        timing.returned_ids == allocation.returned_ids
        for timing, allocation in zip(timing_samples, allocation_samples, strict=True)
    )
    if not returned_ids_match:
        raise RuntimeError("timing and allocation passes returned different result IDs")
    samples = [
        Sample(
            returned_ids=timing.returned_ids,
            wall_ms=timing.wall_ms,
            cpu_ms=timing.cpu_ms,
            peak_alloc_kib=allocation.peak_alloc_kib,
        )
        for timing, allocation in zip(timing_samples, allocation_samples, strict=True)
    ]
    case_records: list[dict[str, Any]] = []
    offset = 0
    for case in fixture.cases:
        case_samples = samples[offset : offset + repetitions]
        offset += repetitions
        returned_ids = list(case_samples[-1].returned_ids)
        expected = set(case.expected_ids)
        forbidden = set(case.forbidden_ids)
        returned = set(returned_ids)
        relevant = sorted(returned.intersection(expected))
        forbidden_hits = sorted(returned.intersection(forbidden))
        precision = (
            1.0
            if not returned and not expected
            else len(relevant) / len(returned)
            if returned
            else 0.0
        )
        recall = (
            1.0
            if not expected and not returned
            else len(relevant) / len(expected)
            if expected
            else 0.0
        )
        case_records.append(
            {
                "case_id": case.id,
                "split": case.split,
                "category": case.category,
                "query": case.query,
                "project_scope": case.project_scope,
                "expected_ids": list(case.expected_ids),
                "forbidden_ids": list(case.forbidden_ids),
                "returned_ids": returned_ids,
                "relevant_ids": relevant,
                "forbidden_hits": forbidden_hits,
                "unexpected_ids": sorted(returned.difference(expected)),
                "precision": round(precision, 4),
                "recall": round(recall, 4),
                "samples": [
                    {
                        "wall_ms": round(item.wall_ms, 4),
                        "cpu_ms": round(item.cpu_ms, 4),
                        "peak_alloc_kib": round(item.peak_alloc_kib, 4),
                    }
                    for item in case_samples
                ],
            }
        )
    first_query = samples[0]
    warm = samples[1:] or samples
    quality_by_split = {
        split: _quality_summary([record for record in case_records if record["split"] == split])
        for split in ("development", "holdout")
    }
    service_calls = 0 if strategy == STRATEGY_NO_MEMORY else timing_pass["call_count"]
    allocation_service_calls = (
        0 if strategy == STRATEGY_NO_MEMORY else allocation_pass["call_count"]
    )
    return {
        "strategy": strategy,
        "implementation": (
            "deterministic empty result; no Service, SQLite, or retrieval call"
            if strategy == STRATEGY_NO_MEMORY
            else "ApplicationService.query_memories(query=case.query) -> "
            "existing SQLiteStore.search_memories"
            if strategy == STRATEGY_DIRECT
            else "ApplicationService.query_memories(query='') -> current recent-entry path"
        ),
        "cases": case_records,
        "quality": {
            "all": _quality_summary(case_records),
            **quality_by_split,
        },
        "timing": {
            "bootstrap_ms": round(timing_pass["bootstrap_ms"], 4),
            "bootstrap_scope": (
                "no_service_or_sqlite"
                if strategy == STRATEGY_NO_MEMORY
                else (
                    "Service construction, SQLite initialize/migrations, role/session, "
                    "fixture writes; not process startup"
                )
            ),
            "first_query_ms": round(first_query.wall_ms, 4),
            "formal_timing_scope": "wall/CPU measured with tracemalloc disabled",
            "allocation_scope": (
                "separate tracemalloc allocation pass in an independent temporary store; "
                "wall/CPU/quality come only from the timing pass"
            ),
            "wall_p50_ms": _percentile((sample.wall_ms for sample in samples), 0.50),
            "wall_p95_ms": _percentile((sample.wall_ms for sample in samples), 0.95),
            "warm_wall_p50_ms": _percentile((sample.wall_ms for sample in warm), 0.50),
            "warm_wall_p95_ms": _percentile((sample.wall_ms for sample in warm), 0.95),
            "warm_cpu_p50_ms": _percentile((sample.cpu_ms for sample in warm), 0.50),
            "warm_cpu_p95_ms": _percentile((sample.cpu_ms for sample in warm), 0.95),
            "warm_peak_alloc_p50_kib": _percentile(
                (sample.peak_alloc_kib for sample in warm), 0.50
            ),
            "warm_peak_alloc_p95_kib": _percentile(
                (sample.peak_alloc_kib for sample in warm), 0.95
            ),
            "rss_high_water_kib": timing_pass["rss_high_water_kib"],
            "rss_scope": (
                "timing-pass process high-water; allocation pass uses a separate temporary store"
            ),
            "sample_count": len(samples),
            "repetitions_per_case": repetitions,
        },
        "allocation_pass": {
            "bootstrap_ms": round(allocation_pass["bootstrap_ms"], 4),
            "sample_count": len(allocation_samples),
            "call_count": allocation_pass["call_count"],
            "service_query_calls": allocation_service_calls,
            "returned_ids_match_timing": returned_ids_match,
            "rss_high_water_kib": allocation_pass["rss_high_water_kib"],
            "scope": "independent temporary store and full fixture pass under tracemalloc",
        },
        "cost": {
            "model_calls": 0,
            "model_usage": "unknown",
            "token_cost_usd": "unknown",
            "service_query_calls": service_calls,
            "allocation_pass_service_query_calls": allocation_service_calls,
            "sqlite_sql_calls": "unknown",
            "host_rpc_calls": 0,
            "notes": (
                "No retrieval or storage path was invoked."
                if strategy == STRATEGY_NO_MEMORY
                else (
                    "Formal timing pass query-call count only; allocation pass uses an "
                    "independent temporary store; actual SQLite SQL statement count is unknown."
                )
            ),
        },
    }


def run_benchmark(
    *,
    fixture_path: Path = DEFAULT_FIXTURE,
    repetitions: int = 3,
) -> dict[str, Any]:
    """Run the no-memory control and both old strategies in isolation."""

    if repetitions < 1:
        raise ValueError("repetitions must be positive")
    fixture_load_started = time.perf_counter_ns()
    fixture = load_fixture(fixture_path)
    fixture_load_ms = (time.perf_counter_ns() - fixture_load_started) / 1_000_000.0
    with tempfile.TemporaryDirectory(prefix="operant-b2-1-memory-") as temp_dir:
        temp_root = Path(temp_dir)
        results = {
            strategy: _run_strategy(
                strategy,
                fixture,
                repetitions=repetitions,
                temp_root=temp_root,
            )
            for strategy in STRATEGIES
        }
    violations = {
        strategy: result["quality"]["all"]["security_violations"]
        for strategy, result in results.items()
        if result["quality"]["all"]["security_violations"]
    }
    split_counts = {
        split: sum(case.split == split for case in fixture.cases)
        for split in ("development", "holdout")
    }
    return {
        "schema": "operant.b2_1.memory-baseline-report.v2",
        "implementation": {
            "worktree": str(Path.cwd().resolve()),
            "source_root": str(Path(__file__).resolve().parent.parent),
            "base_head": BASE_IMPLEMENTATION_HEAD,
            "head": _git_head(Path(__file__).resolve().parent.parent),
        },
        "dataset": {
            "id": fixture.dataset_id,
            "version": fixture.version,
            "sha256": fixture.sha256,
            "file_sha256": fixture.file_sha256,
            "fixture_path": str(fixture_path.resolve()),
            "memory_count": len(fixture.memories),
            "case_count": len(fixture.cases),
            "split_counts": split_counts,
        },
        "run": {
            "strategies": list(STRATEGIES),
            "repetitions": repetitions,
            "database": "temporary isolated SQLite per strategy; no_memory bypasses store",
            "fixture_load_ms": round(fixture_load_ms, 4),
            "process_startup_measured": False,
            "rss_scope": "whole_process_high_water_non_comparable_across_sequential_strategies",
            "user_database_read": False,
            "environment_file_read": False,
            "network": False,
            "model_calls": 0,
            "model_usage": "unknown",
            "python": platform.python_version(),
            "platform": platform.platform(aliased=True),
        },
        "performance_gates": {
            "status": "frozen_pending_future_candidate",
            "task_retrieval": {
                "status": "baseline_characterization_only",
                "quality_comparison_strategies": [STRATEGY_DIRECT, STRATEGY_RECENT],
                "regression_reference": STRATEGY_DIRECT,
                "thresholds": dict(TASK_PERFORMANCE_GATES),
            },
            "host_overhead": {
                "status": "frozen_pending_future_host",
                "same_algorithm_reference": STRATEGY_DIRECT,
                "thresholds": {
                    mode: dict(thresholds) for mode, thresholds in HOST_PERFORMANCE_GATES.items()
                },
            },
            "security_hard_gate": "forbidden_hits == 0 for every case and strategy",
        },
        "strategies": results,
        "safety": {
            "passed": not violations,
            "hard_gate": (
                "No cross-project, role-restricted, inactive, revoked, or candidate ID "
                "may be returned."
            ),
            "violations": violations,
        },
        "pending_measurements": [
            "future_host_in_process",
            "future_host_isolated",
            "future_task_fts",
            "real_model_usage_and_end_to_end_cost",
        ],
        "limits": [
            "Synthetic fixed data cannot establish production quality or semantic recall.",
            "Timing and memory metrics are local process observations and should be "
            "compared on the same host profile.",
            "Provider usage is unknown because this run makes zero model calls.",
        ],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--output", type=Path, help="write JSON report to this path")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = run_benchmark(fixture_path=args.fixture, repetitions=args.repetitions)
    except (OSError, ValueError, AssertionError) as exc:
        print(f"benchmark failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        report["run"]["report_output_path"] = str(args.output.resolve())
        encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    else:
        report["run"]["report_output_path"] = "stdout"
        encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        print(encoded, end="")
    if not report["safety"]["passed"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
