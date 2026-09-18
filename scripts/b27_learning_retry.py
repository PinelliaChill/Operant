"""Retry only a read-only reuse task against an already frozen synthetic memory."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

from b27_real_evaluation import (
    GOVERNANCE_ROOT,
    ROLE_ID,
    EvaluationCase,
    ProjectContext,
    _aggregate_usage,
    _run_service_case,
)

from operant.memory_plugins.manager import MemoryManager
from operant.settings import load_local_env


async def run(args: argparse.Namespace) -> dict:
    previous = json.loads(args.report.read_text())
    learning = previous["cross_task_learning"]
    assert learning["reuse"]["formed_record_selected"] is True
    assert learning["reuse"]["task_succeeded"] is False
    assert previous["configuration"]["budget"]["max_tool_calls"] == 0
    root = args.run_root.resolve(strict=True)
    os.environ["OPERANT_DB_PATH"] = str(root / "core.sqlite3")
    load_local_env(GOVERNANCE_ROOT / ".env")
    from operant.api import create_app
    from operant.contracts.b2_3 import ManagementCommand

    app = create_app(root / "core.sqlite3")
    service = app.state.operant_service
    manager = MemoryManager(service)
    service.memory_manager = manager
    try:
        dataset = learning["freeze"]["dataset_id"]
        record = manager.ledger.get_version(dataset, learning["formed_record_id"])
        assert record.ref.content_digest == learning["formed_content_sha256"]
        project = next(
            p
            for p in manager._state["projects"]
            if manager.registry.get_installation(p["installation_id"]).dataset_id == dataset
        )
        await manager.execute(
            ManagementCommand(action="plugin_enable", installation_id=project["installation_id"])
        )
        workspace = service.store.get_workspace_initialization_by_id(
            project["workspace_id"]
        ).workspace_ref
        role = service.get_role(ROLE_ID)
        assert not role.tool_policy.allowed_tools and role.budget.max_tool_calls == 0
        harness = SimpleNamespace(
            service=service,
            manager=manager,
            role=role,
            projects={
                "alpha": ProjectContext(
                    "alpha",
                    project["project_id"],
                    Path(workspace),
                    project["installation_id"],
                    dataset,
                )
            },
            versions={"alpha": {record.ref.record_id: record}},
        )
        case = EvaluationCase(
            "cross-task-reuse",
            "development",
            "cross_task_learning",
            "B27跨任务验证应按什么顺序运行哪些命令？",
            "alpha",
            (record.ref.record_id,),
            (),
            ("uv run pytest", "uv run ruff check"),
        )
        result = await _run_service_case(harness, case, "new_fts")
        cutoff_matches = (
            result.usage.get("manifest_cutoff") == learning["freeze"]["knowledge_cutoff"]
        )
        complete = (
            result.task_succeeded is True
            and result.answer_success is True
            and result.answer_order_ok is True
            and record.ref.record_id in result.selected_ids
            and cutoff_matches
        )
        return {
            "completed": complete,
            "evidence_head": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], text=True
            ).strip(),
            "entry": "new ApplicationService.run_session / unchanged role, budget, query and frozen record",
            "prior_report": str(args.report),
            "database": str(root / "core.sqlite3"),
            "freeze": learning["freeze"],
            "record_digest": record.ref.content_digest,
            "formation_reused": True,
            "selected_ids": result.selected_ids,
            "cutoff_matches": cutoff_matches,
            "task_succeeded": result.task_succeeded,
            "answer_success": result.answer_success,
            "answer_order_ok": result.answer_order_ok,
            "answer_sha256": result.answer_sha256,
            "reuse_model_completed_calls": result.model_calls,
            "reuse_usage": result.usage,
            "successful_chain_usage": _aggregate_usage(learning["formation_usage"], result.usage),
            "failed_attempt_usage": learning["reuse"]["usage"],
            "total_cost_usd": None,
            "cost_state": "unknown",
            "limitations": [
                "Earlier read-only reuse timed out; no tool/write is replayed.",
                "Prior timed-out request usage is unknown and is not counted as zero.",
            ],
        }
    finally:
        await manager.close()
        service.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(run(args))
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False))
    if not result["completed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
