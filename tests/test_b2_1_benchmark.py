from __future__ import annotations

import hashlib
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts/benchmark_memory_baseline.py"
_SCRIPT_SPEC = importlib.util.spec_from_file_location("b2_1_memory_baseline", _SCRIPT_PATH)
assert _SCRIPT_SPEC is not None and _SCRIPT_SPEC.loader is not None
_SCRIPT_MODULE = importlib.util.module_from_spec(_SCRIPT_SPEC)
sys.modules[_SCRIPT_SPEC.name] = _SCRIPT_MODULE
_SCRIPT_SPEC.loader.exec_module(_SCRIPT_MODULE)

from b2_1_memory_baseline import (  # noqa: E402
    BASE_IMPLEMENTATION_HEAD,
    DEFAULT_FIXTURE,
    HOST_PERFORMANCE_GATES,
    STRATEGY_DIRECT,
    STRATEGY_NO_MEMORY,
    STRATEGY_RECENT,
    TASK_PERFORMANCE_GATES,
    load_fixture,
    run_benchmark,
)


def test_b2_1_fixture_is_fixed_split_and_covers_mp05_cases() -> None:
    fixture = load_fixture(DEFAULT_FIXTURE)

    assert fixture.dataset_id == "b2_1_memory_baseline"
    assert len(fixture.memories) == 31
    assert len(fixture.cases) == 42
    assert sum(case.split == "development" for case in fixture.cases) == 30
    assert sum(case.split == "holdout" for case in fixture.cases) == 12
    assert {case.project_scope for case in fixture.cases if case.split == "holdout"} == {
        "workspace-gamma"
    }
    gamma_records = [
        record for record in fixture.memories if record["project_scope"] == "workspace-gamma"
    ]
    assert gamma_records
    assert all(
        str(record["source_session_id"]).startswith("source-gamma-")
        and str(record["created_at"]) >= "2026-02-01T00:00:00+00:00"
        for record in gamma_records
    )
    categories = {case.category for case in fixture.cases}
    assert {
        "chinese_rewrite",
        "identifier",
        "branch_fact",
        "malicious_memory",
        "private_mailbox",
        "no_related_memory",
        "revocation",
        "compression_contamination",
        "late_event",
        "cross_task_learning",
        "cross_workspace",
    }.issubset(categories)


def test_b2_1_benchmark_uses_isolated_old_paths_and_no_model(tmp_path: Path) -> None:
    report = run_benchmark(fixture_path=DEFAULT_FIXTURE, repetitions=2)

    assert report["run"]["user_database_read"] is False
    assert report["run"]["environment_file_read"] is False
    assert report["run"]["model_calls"] == 0
    assert report["run"]["model_usage"] == "unknown"
    assert report["run"]["process_startup_measured"] is False
    assert report["run"]["fixture_load_ms"] >= 0
    assert report["run"]["rss_scope"] == (
        "whole_process_high_water_non_comparable_across_sequential_strategies"
    )
    actual_head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
    ).strip()
    assert report["implementation"]["base_head"] == BASE_IMPLEMENTATION_HEAD
    assert report["implementation"]["head"] == actual_head
    assert re.fullmatch(r"[0-9a-f]{40}", report["implementation"]["head"])
    assert report["implementation"]["source_root"] == str(Path(__file__).resolve().parents[1])
    assert report["dataset"]["fixture_path"].endswith("tests/fixtures/b2_1/memory_baseline.json")
    assert (
        report["dataset"]["file_sha256"] == hashlib.sha256(DEFAULT_FIXTURE.read_bytes()).hexdigest()
    )
    assert report["pending_measurements"] == [
        "future_host_in_process",
        "future_host_isolated",
        "future_task_fts",
        "real_model_usage_and_end_to_end_cost",
    ]
    assert set(report["strategies"]) == {
        STRATEGY_NO_MEMORY,
        STRATEGY_DIRECT,
        STRATEGY_RECENT,
    }
    assert report["safety"]["passed"] is True
    assert report["safety"]["violations"] == {}
    no_memory = report["strategies"][STRATEGY_NO_MEMORY]
    assert no_memory["cost"]["service_query_calls"] == 0
    assert no_memory["cost"]["sqlite_sql_calls"] == "unknown"
    assert all(not record["returned_ids"] for record in no_memory["cases"])
    for strategy in (STRATEGY_NO_MEMORY, STRATEGY_DIRECT, STRATEGY_RECENT):
        result = report["strategies"][strategy]
        assert result["cost"]["model_calls"] == 0
        assert result["cost"]["model_usage"] == "unknown"
        assert result["timing"]["bootstrap_ms"] >= 0
        assert result["timing"]["first_query_ms"] >= 0
        assert result["timing"]["rss_scope"] == (
            "whole_process_high_water_non_comparable_across_sequential_strategies"
        )
    for strategy in (STRATEGY_DIRECT, STRATEGY_RECENT):
        result = report["strategies"][strategy]
        assert result["cost"]["service_query_calls"] == 84
        assert result["cost"]["sqlite_sql_calls"] == "unknown"
        assert result["timing"]["sample_count"] == 84
        assert result["timing"]["warm_wall_p95_ms"] >= result["timing"]["warm_wall_p50_ms"]
        assert result["timing"]["warm_cpu_p95_ms"] >= result["timing"]["warm_cpu_p50_ms"]
        assert (
            result["timing"]["warm_peak_alloc_p95_kib"]
            >= result["timing"]["warm_peak_alloc_p50_kib"]
        )


def test_b2_1_hard_gate_and_performance_tolerances_are_frozen() -> None:
    assert TASK_PERFORMANCE_GATES["warm_wall_p95_max_regression_ratio"] == 1.25
    assert TASK_PERFORMANCE_GATES["first_query_wall_max_regression_ratio"] == 1.50
    assert TASK_PERFORMANCE_GATES["warm_cpu_p95_max_regression_ratio"] == 1.25
    assert TASK_PERFORMANCE_GATES["warm_peak_alloc_p95_max_regression_ratio"] == 1.50
    assert (
        HOST_PERFORMANCE_GATES["future_host_in_process"]["warm_wall_p95_max_regression_ratio"]
        == 1.25
    )
    assert (
        HOST_PERFORMANCE_GATES["future_host_in_process"]["bootstrap_wall_max_regression_ratio"]
        == 1.50
    )
    assert (
        HOST_PERFORMANCE_GATES["future_host_isolated"]["bootstrap_wall_max_regression_ratio"]
        == 3.00
    )

    report = run_benchmark(repetitions=1)
    assert report["performance_gates"]["status"] == "frozen_pending_future_candidate"
    assert report["performance_gates"]["security_hard_gate"] == (
        "forbidden_hits == 0 for every case and strategy"
    )
    assert report["performance_gates"]["host_overhead"]["same_algorithm_reference"] == (
        STRATEGY_DIRECT
    )
    assert report["performance_gates"]["task_retrieval"]["quality_comparison_strategies"] == [
        STRATEGY_DIRECT,
        STRATEGY_RECENT,
    ]
    for strategy in (STRATEGY_NO_MEMORY, STRATEGY_DIRECT, STRATEGY_RECENT):
        records = report["strategies"][strategy]["cases"]
        assert any(record["category"] == "private_mailbox" for record in records)
        assert all(not record["forbidden_hits"] for record in records)
