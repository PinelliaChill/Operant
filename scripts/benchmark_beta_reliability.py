"""Daily local workload, using isolated SQLite and real Core paths (no model calls)."""

from __future__ import annotations

import argparse
import json
import platform
import resource
import statistics
import subprocess
import time
import tracemalloc
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from operant.api_workbench_context import WorkbenchReferenceRequest, context_view, create_reference
from operant.application.service import ApplicationService
from operant.domain.commands import ContextBaselineOperation
from operant.domain.models import ModelProfile, RolePreset, ToolPolicy
from operant.domain.threads import (
    AgentMessagePayload,
    ConversationThread,
    Item,
    Turn,
    UserMessagePayload,
)
from operant.persistence.sqlite import SQLiteStore
from operant.providers.openai_compatible import OpenAICompatibleProvider


def sample(operation: Callable[[], Any], repetitions: int) -> dict[str, float]:
    operation()  # Warm the same local path before recording samples.
    elapsed = []
    for _ in range(repetitions):
        started = time.perf_counter()
        operation()
        elapsed.append((time.perf_counter() - started) * 1000)
    values = sorted(elapsed)
    return {
        "median_ms": round(statistics.median(values), 3),
        "p95_ms": round(values[min(len(values) - 1, int(len(values) * 0.95))], 3),
        "max_ms": round(max(values), 3),
    }


def benchmark(repetitions: int) -> dict[str, Any]:
    with TemporaryDirectory(prefix="operant-beta2-load-") as directory:
        root = Path(directory).resolve()
        workspace = root / "workspace"
        workspace.mkdir()
        service = ApplicationService(
            SQLiteStore(root / "core.sqlite3"),
            OpenAICompatibleProvider(),
            artifact_root=root / "artifacts",
        )
        service.initialize()
        profile = service.add_model_profile(
            ModelProfile(
                name="local load fixture",
                model_id="local-fixture",
                base_url="https://example.invalid/v1",
                secret_ref="BETA_LOAD_UNUSED_KEY",
                context_window=32000,
            )
        )
        role = service.create_role(
            RolePreset(
                name="load fixture",
                model_profile_id=profile.id,
                system_prompt="Summarize.",
                tool_policy=ToolPolicy(allowed_tools=("read_file",)),
            )
        )
        threads = []
        sessions = []
        for _index in range(12):
            thread = service.create_thread(ConversationThread(workspace_ref=str(workspace)))
            session = service.create_session(role.id, thread_id=thread.id)
            threads.append(thread)
            sessions.append(session)
            for _turn_index in range(100):
                turn = service.create_turn(Turn(thread_id=thread.id))
                for payload in (
                    UserMessagePayload(text="请继续检查任务进度，保留既定约束。"),
                    AgentMessagePayload(text="Progress: " + "y" * 800, agent_id="fixture_agent"),
                ):
                    service.append_item(Item(thread_id=thread.id, turn_id=turn.id, payload=payload))
        thread, session = threads[0], sessions[0]
        for index in range(20):
            filename = f"source-{index}.txt"
            (workspace / filename).write_text("Synthetic local file. " * 200)
            create_reference(
                service, thread.id, WorkbenchReferenceRequest(kind="file", target=filename)
            )
        agent = service.store.create_agent(session.id)
        tracemalloc.start()
        metrics = {
            "thread_list": sample(lambda: service.list_threads(limit=100), repetitions),
            "history_page_100": sample(
                lambda: service.list_items(thread.id, limit=100), repetitions
            ),
            "context_details": sample(lambda: context_view(service, thread.id), repetitions),
            "manual_compaction_200_items": sample(
                lambda: service.append_context_baseline(
                    session_id=session.id,
                    thread_id=thread.id,
                    agent_id=agent.id,
                    operation=ContextBaselineOperation.COMPACT,
                ),
                repetitions,
            ),
        }
        resource_module = (
            Path(__import__("operant").__file__).parent / "application/resource_governance.py"
        )
        if resource_module.is_file():
            from operant.application.resource_governance import ResourceGovernanceService

            governance = ResourceGovernanceService(service)
            metrics["resource_inventory"] = sample(
                lambda: governance.inventory(thread.id), repetitions
            )
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        rss_bytes = rss if platform.system() == "Darwin" else rss * 1024
        source = Path(__import__("operant").__file__).resolve().parents[2]
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
        return {
            "source": str(source),
            "head": head,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "fixture": {
                "threads": 12,
                "turns_per_thread": 100,
                "items": 2400,
                "file_snapshots": 20,
            },
            "repetitions": repetitions,
            "metrics": metrics,
            "measurement_peak_python_bytes": peak,
            "process_peak_rss_bytes": rss_bytes,
            "database_bytes": service.store.path.stat().st_size,
            "interactive_budget_ms": 500,
            "within_interactive_budget": all(value["p95_ms"] < 500 for value in metrics.values()),
            "scope": "Local daily fixture; excludes Provider latency and unlimited workloads.",
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=10)
    arguments = parser.parse_args()
    if not 2 <= arguments.repetitions <= 100:
        parser.error("repetitions must be between 2 and 100")
    report = benchmark(arguments.repetitions)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
