from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/benchmark_b24_memory.py"


def _benchmark_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("b24_memory_benchmark_test", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_b24_benchmark_reads_fixed_split_without_fixture_constants() -> None:
    benchmark = _benchmark_module()
    fixture = benchmark.load_fixture()

    assert fixture.dataset_id == "b2_1_memory_baseline"
    assert (
        fixture.file_sha256
        == benchmark.hashlib.sha256(benchmark.DEFAULT_FIXTURE.read_bytes()).hexdigest()
    )
    assert len(fixture.memories) >= 30
    assert {case.split for case in fixture.cases} == {"development", "holdout"}
    assert len({case.id for case in fixture.cases}) == len(fixture.cases)
    memory_ids = {str(record["id"]) for record in fixture.memories}
    assert all(set(case.expected_ids).issubset(memory_ids) for case in fixture.cases)
    assert all(set(case.forbidden_ids).issubset(memory_ids) for case in fixture.cases)


def test_b24_direct_report_has_quality_safety_and_real_manager_entrypoint() -> None:
    benchmark = _benchmark_module()
    report = benchmark.run_benchmark(modes=("direct",), repetitions=1)

    assert report["schema"] == "operant.b2_4.memory-performance-report.v1"
    assert report["implementation"]["manager_entrypoint"] == "MemoryManager.search"
    result = report["results"]["direct"]
    assert result["status"] == "completed"
    assert result["model_calls"] == 0
    assert result["quality"]["forbidden_hit_count"] == 0
    assert result["metrics"]["sample_count"] == 42
    assert result["rpc"]["frames"] == 0
    assert result["phase_timing"]["manager_search"]["calls"] == 42
    # Host modes are intentionally omitted here, so the overall gate is not a
    # false pass from a partial run.
    assert report["gates"]["status"] == "not_passed"


def test_b24_trusted_stage_timing_exposes_package_validation_cost() -> None:
    benchmark = _benchmark_module()
    report = benchmark.run_benchmark(modes=("trusted_in_process",), repetitions=1)

    result = report["results"]["trusted_in_process"]
    assert result["status"] == "completed"
    assert result["model_calls"] == 0
    assert result["quality"]["forbidden_hit_count"] == 0
    phase = result["phase_timing"]
    assert phase["manager_search"]["calls"] == result["metrics"]["sample_count"]
    assert phase["host_invoke"]["calls"] == result["metrics"]["sample_count"]
    assert phase["registry_verify_package"]["calls"] >= phase["host_invoke"]["calls"]
    assert phase["registry_verify_package"]["total_ms"] > 0
    assert phase["host_minus_manager_ms"] > 0
    assert report["gates"]["host"]["trusted_in_process"]["checks"]["bootstrap_wall"]


def test_b24_gate_thresholds_are_loaded_from_frozen_old_baseline() -> None:
    benchmark = _benchmark_module()
    old = benchmark._load_old_module()
    assert old.TASK_PERFORMANCE_GATES["warm_wall_p95_max_regression_ratio"] == 1.25
    assert old.TASK_PERFORMANCE_GATES["first_query_wall_max_regression_ratio"] == 1.50
    assert old.TASK_PERFORMANCE_GATES["absolute_wall_slack_ms"] == 2.0
    assert old.TASK_PERFORMANCE_GATES["absolute_peak_alloc_slack_kib"] == 256.0
    assert (
        old.HOST_PERFORMANCE_GATES["future_host_in_process"]["warm_wall_p95_max_regression_ratio"]
        == 1.25
    )
    assert (
        old.HOST_PERFORMANCE_GATES["future_host_isolated"]["bootstrap_wall_max_regression_ratio"]
        == 3.00
    )


def test_b24_gate_applies_fixed_absolute_slack() -> None:
    benchmark = _benchmark_module()
    old_report = {
        "strategies": {
            "old_direct_query": {
                "timing": {
                    "warm_wall_p95_ms": 10.0,
                    "first_query_ms": 10.0,
                    "warm_cpu_p95_ms": 10.0,
                    "warm_peak_alloc_p95_kib": 100.0,
                },
                "quality": {"holdout": {"macro_recall": 1.0}},
            }
        }
    }
    direct = {
        "setup_ms": 1.0,
        "metrics": {
            "warm_wall_p95_ms": 14.4,
            "first_query_ms": 16.0,
            "warm_cpu_p95_ms": 12.0,
            "warm_peak_alloc_p95_kib": 405.0,
        },
        "quality": {
            "holdout": {"macro_recall": 1.0},
            "forbidden_hit_count": 0,
        },
    }
    result = benchmark.evaluate_gates(old_report, {"direct": direct})
    checks = result["direct"]["checks"]
    assert checks["warm_wall_p95"]["passed"] is True
    assert checks["warm_wall_p95"]["allowed_actual"] == 14.5
    assert checks["warm_peak_alloc_p95"]["passed"] is True
    assert checks["warm_peak_alloc_p95"]["allowed_actual"] == 406.0


def test_b24_report_is_json_serializable() -> None:
    benchmark = _benchmark_module()
    report = benchmark.run_benchmark(modes=("direct",), repetitions=1)
    encoded = json.dumps(report, ensure_ascii=False)
    assert "operant.b2_4.memory-performance-report.v1" in encoded
