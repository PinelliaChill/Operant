from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/b27_real_evaluation.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("b27_real_evaluation_test", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_b27_fixture_is_fixed_and_split() -> None:
    evaluation = _module()
    fixture = evaluation.load_fixture()

    assert fixture.dataset_id == "b2_7_memory_evaluation"
    assert 30 <= len(fixture.cases) <= 50
    assert len(fixture.memories) == 29
    assert {case.split for case in fixture.cases} == {"development", "holdout"}
    assert {case.project_scope for case in fixture.cases if case.split == "holdout"} == {"gamma"}
    memory_ids = {str(record["id"]) for record in fixture.memories}
    assert all(set(case.expected_ids).issubset(memory_ids) for case in fixture.cases)
    assert all(
        not set(case.expected_ids).intersection(case.forbidden_ids) for case in fixture.cases
    )


def test_b27_deterministic_comparison_uses_real_host_and_freezes_learning(tmp_path: Path) -> None:
    evaluation = _module()
    report = asyncio.run(
        evaluation.run_deterministic_baseline(run_root=tmp_path / "isolated-evaluation")
    )

    assert report["schema"] == "operant.b2_7.memory-evaluation-report.v1"
    assert report["status"] == "completed"
    assert report["results"]["new_fts"]["quality"]["forbidden_hit_count"] == 0
    assert report["results"]["new_fts"]["host"]["invokes"] > 0
    assert (
        report["results"]["new_fts"]["quality"]["holdout"]["macro_recall"]
        > report["results"]["old_recent_entries"]["quality"]["holdout"]["macro_recall"]
    )
    learning = report["cross_task_learning"]
    assert learning["status"] == "completed"
    assert learning["reuse"]["formed_record_selected"] is True
    assert learning["reuse"]["cutoff_matches_freeze"] is True
    json.dumps(report, ensure_ascii=False)


def test_b27_real_without_provider_is_explicitly_blocked(tmp_path: Path, monkeypatch) -> None:
    evaluation = _module()
    monkeypatch.setattr(evaluation, "_find_env_file", lambda: None)
    monkeypatch.delenv("OPERANT_BASE_URL", raising=False)
    monkeypatch.delenv("OPERANT_API_KEY", raising=False)
    report = asyncio.run(
        evaluation.run_real_evaluation(
            run_root=tmp_path / "isolated-real",
            include_learning=False,
            sample_case_count=4,
        )
    )

    assert report["status"] == "blocked"
    assert report["error_code"] == "provider_base_url_missing"
    assert "OPERANT_API_KEY" not in json.dumps(report)


def test_b27_measurement_reads_actual_pack_when_convenience_refs_are_empty(tmp_path: Path) -> None:
    evaluation = _module()
    path = tmp_path / "measurement.sqlite3"

    def connect():
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        return connection

    with connect() as connection:
        connection.execute("CREATE TABLE b24_context_memory(revision_id TEXT, body TEXT)")
        connection.execute(
            "INSERT INTO b24_context_memory VALUES (?, ?)",
            (
                "r1",
                json.dumps(
                    {"pack": {"selected": [{"record_id": "actual"}], "knowledge_cutoff": "7"}}
                ),
            ),
        )
    service = SimpleNamespace(store=SimpleNamespace(_connect=connect))
    ids, cutoff = evaluation._context_pack_selection(
        service, [SimpleNamespace(id="r1", memory_refs=())]
    )
    assert ids == ("actual",)
    assert cutoff == "7"


def test_b27_missing_learning_usage_is_not_counted_as_zero() -> None:
    evaluation = _module()
    known = {"state": "known", "prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}
    assert evaluation._aggregate_usage(known, {"state": "unknown"})["state"] == "unknown"
    assert evaluation._aggregate_usage(known, known)["total_tokens"] == 60
