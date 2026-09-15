from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import tracemalloc
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

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
    assert result["configuration_key"]["policy"] == benchmark.asdict(benchmark.POLICY)
    assert result["model_calls"] == 0
    assert result["quality"]["forbidden_hit_count"] == 0
    assert result["metrics"]["sample_count"] == 42
    assert "formal parent-process call without tracemalloc" in result["metrics"]["cpu_scope"]
    assert "independent full allocation pass" in result["metrics"]["allocation_scope"]
    assert result["observation_pass"]["rpc"]["frames"] == 0
    assert result["observation_pass"]["phase_timing"]["manager_search"]["calls"] == 42
    assert result["observation_pass"]["manager_search_calls"] == 42
    assert result["allocation_pass"]["returned_ids_match_timing"] is True
    # Host modes are intentionally omitted here, so the overall gate is not a
    # false pass from a partial run.
    assert report["gates"]["status"] == "not_passed"


def test_b24_trusted_stage_timing_exposes_package_validation_cost() -> None:
    benchmark = _benchmark_module()
    report = benchmark.run_benchmark(modes=("trusted_in_process",), repetitions=1)

    result = report["results"]["trusted_in_process"]
    assert result["status"] == "completed"
    assert result["configuration_key"]["policy"] == benchmark.asdict(benchmark.POLICY)
    assert result["model_calls"] == 0
    assert result["quality"]["forbidden_hit_count"] == 0
    phase = result["observation_pass"]["phase_timing"]
    assert phase["manager_search"]["calls"] == result["metrics"]["sample_count"]
    assert phase["host_invoke"]["calls"] == result["metrics"]["sample_count"]
    assert phase["registry_verify_package"]["calls"] >= phase["host_invoke"]["calls"]
    assert phase["registry_verify_package"]["total_ms"] > 0
    assert phase["host_minus_manager_ms"] > 0
    allocation = result["allocation_pass"]
    assert "phase_timing" not in allocation
    assert "phase_calls" not in allocation
    assert "rpc" not in allocation
    assert phase["registry_verify_package"]["calls"] == 7 * result["metrics"]["sample_count"]
    assert result["observation_pass"]["requests_match_timing"] is True
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


def test_b24_formal_timing_and_allocation_passes_are_separate() -> None:
    benchmark = _benchmark_module()
    calls: list[str] = []

    def formal() -> SimpleNamespace:
        calls.append("formal")
        return SimpleNamespace(
            candidates=(SimpleNamespace(ref=SimpleNamespace(record_id="formal")),)
        )

    def allocation() -> SimpleNamespace:
        calls.append("allocation")
        return SimpleNamespace(
            candidates=(SimpleNamespace(ref=SimpleNamespace(record_id="allocation")),)
        )

    timing = benchmark._sample_timing(formal, child_pid=None)
    allocation_sample = benchmark._sample_allocation(allocation)
    assert calls == ["formal", "allocation"]
    assert timing.returned_ids == ("formal",)
    assert timing.peak_alloc_kib == 0
    assert allocation_sample.returned_ids == ("allocation",)
    assert allocation_sample.wall_ms == 0
    assert allocation_sample.cpu_ms == 0

    async_calls: list[str] = []

    async def async_formal() -> SimpleNamespace:
        async_calls.append("formal")
        return formal()

    async def async_allocation() -> SimpleNamespace:
        async_calls.append("allocation")
        return allocation()

    async_timing = asyncio.run(benchmark._async_sample_timing(async_formal, child_pid=None))
    async_allocation_sample = asyncio.run(benchmark._async_sample_allocation(async_allocation))
    assert async_calls == ["formal", "allocation"]
    assert async_timing.returned_ids == ("formal",)
    assert async_timing.peak_alloc_kib == 0
    assert async_allocation_sample.returned_ids == ("allocation",)
    assert async_allocation_sample.wall_ms == 0
    assert async_allocation_sample.cpu_ms == 0


def test_b24_timing_pass_rejects_external_tracemalloc() -> None:
    benchmark = _benchmark_module()
    tracemalloc.start()
    try:
        with pytest.raises(RuntimeError, match="tracemalloc to be disabled"):
            benchmark._sample_timing(lambda: SimpleNamespace(candidates=()), child_pid=None)
        with pytest.raises(RuntimeError, match="tracemalloc to be disabled"):
            asyncio.run(benchmark._async_sample_timing(lambda: _empty_awaitable(), child_pid=None))
    finally:
        tracemalloc.stop()


async def _empty_awaitable() -> SimpleNamespace:
    return SimpleNamespace(candidates=())


@pytest.mark.parametrize("mode", ["trusted_in_process", "isolated"])
@pytest.mark.parametrize("pass_kind", ["timing", "allocation"])
def test_clean_host_pass_keeps_production_checks_without_observers(
    monkeypatch, tmp_path, pass_kind, mode
):
    benchmark = _benchmark_module()
    fixture = benchmark.load_fixture()
    inside_sample = False
    calls = {name: 0 for name in ("assert_lease", "verify_package", "verify_certification")}
    for name in calls:
        original = getattr(benchmark.PluginRegistry, name)

        def checked(self, *args, _name=name, _original=original, **kwargs):
            if inside_sample:
                calls[_name] += 1
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(benchmark.PluginRegistry, name, checked)

    def unexpected(*args, **kwargs):
        raise AssertionError("benchmark observer entered a clean pass")

    monkeypatch.setattr(benchmark, "RpcCounter", unexpected)
    monkeypatch.setattr(benchmark, "encode_rpc", unexpected)
    sampler_name = "_async_sample_timing" if pass_kind == "timing" else "_async_sample_allocation"
    original_sample = getattr(benchmark, sampler_name)

    async def sample(*args, **kwargs):
        nonlocal inside_sample
        inside_sample = True
        try:
            return await original_sample(*args, **kwargs)
        finally:
            inside_sample = False

    monkeypatch.setattr(benchmark, sampler_name, sample)
    try:
        result = asyncio.run(
            benchmark._host_mode_pass(
                fixture, fixture.cases[:1], tmp_path, 1, mode, pass_kind=pass_kind
            )
        )
    except benchmark.IsolationUnavailableError as exc:
        pytest.skip(str(exc))
    assert calls == {name: 7 if mode == "trusted_in_process" else 4 for name in calls}
    assert result["rpc"] is None
    assert result["phase_timing"] is None
    assert result["host_startup"]["rpc"] is None


@pytest.mark.parametrize(
    "mismatch", ["request_keys", "configuration_key", "quality", "result_ids", "sample_count"]
)
def test_three_pass_assembly_rejects_drift(mismatch):
    benchmark = _benchmark_module()
    import copy

    sample = benchmark.Sample(
        wall_ms=2,
        cpu_ms=1,
        peak_alloc_kib=3,
        rss_kib=4,
        child_cpu_ms=None,
        child_rss_kib=None,
        returned_ids=("same",),
    )
    timing = {
        "configuration_key": {"permission_epoch": 1},
        "request_keys": [{"query": "same"}],
        "quality": {"forbidden_hit_count": 0},
        "samples": [sample],
    }
    allocation = copy.deepcopy(timing)
    observation = copy.deepcopy(timing)
    if mismatch == "result_ids":
        from dataclasses import replace

        observation["samples"][0] = replace(sample, returned_ids=("different",))
    elif mismatch == "sample_count":
        observation["samples"] = []
    else:
        observation[mismatch] = {"changed": True}
    with pytest.raises(RuntimeError, match="different"):
        benchmark._assemble_passes(timing, allocation, observation)


def test_forbidden_hit_in_earlier_repetition_cannot_be_hidden():
    benchmark = _benchmark_module()
    fixture = benchmark.load_fixture()
    case = next(case for case in fixture.cases if case.forbidden_ids)

    def sample(ids):
        return benchmark.Sample(
            wall_ms=0,
            cpu_ms=0,
            peak_alloc_kib=0,
            rss_kib=0,
            child_cpu_ms=None,
            child_rss_kib=None,
            returned_ids=ids,
        )

    result = benchmark._quality([case], [sample((case.forbidden_ids[0],)), sample(())], 2)
    assert result["forbidden_hit_count"] > 0
