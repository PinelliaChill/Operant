"""Opt-in Task 3 real HTTP/model/watch acceptance against an isolated Core.

The operator seeds a discovered ModelProfile/Team and supplies a JSON seed file.
The script only mutates the specified synthetic workspace and temporary Core.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from sdk.python_client import Phase23Client, Phase45Client


def prepare_boundary_flow(seed: dict[str, Any], graphs: Phase23Client) -> dict[str, Any]:
    """Prepare a formal real-model flow; human commands are performed by GUI/TUI."""
    from operant.domain.graph import (
        EdgeSpec,
        IdempotencyClass,
        NodeKind,
        NodeSpec,
        PortSpec,
        TimeoutPolicy,
        WorkflowDefinition,
    )
    from operant.domain.models import Budget

    def port(name: str, required: bool = True) -> PortSpec:
        return PortSpec(name=name, value_type="string", required=required)

    nodes = (
        NodeSpec(
            node_id="human",
            node_kind=NodeKind.HUMAN_INPUT,
            output_ports=(
                port("answer"),
                PortSpec(name="timeout", value_type="bool", required=False),
            ),
            timeout_policy=TimeoutPolicy(timeout_seconds=300, on_timeout_node_id="fallback"),
            metadata={"prompt": "Provide Task3 input"},
        ),
        NodeSpec(
            node_id="approve",
            node_kind=NodeKind.APPROVAL,
            input_ports=(port("answer"),),
            output_ports=(
                port("answer"),
                PortSpec(name="timeout", value_type="bool", required=False),
            ),
            timeout_policy=TimeoutPolicy(timeout_seconds=300, on_timeout_node_id="fallback"),
            metadata={"detail": "Continue the Task3 isolated model flow?"},
        ),
        NodeSpec(
            node_id="wait",
            node_kind=NodeKind.WAIT,
            input_ports=(port("answer"),),
            output_ports=(port("answer"),),
            metadata={"delay_seconds": 0.1},
        ),
        NodeSpec(
            node_id="agent",
            node_kind=NodeKind.AGENT,
            input_ports=(port("input"),),
            output_ports=(port("result"),),
            metadata={
                "role_id": seed["role_id"],
                "role_version": 1,
                "task": "Reply TASK3_COMBINED_OK. Use no tools.",
            },
        ),
        NodeSpec(
            node_id="child",
            node_kind=NodeKind.SUBWORKFLOW,
            subworkflow_id=seed["workflow_id"],
            subworkflow_version=seed["workflow_version"],
            input_ports=(port("input"),),
            output_ports=(port("result"),),
        ),
        NodeSpec(
            node_id="artifact",
            node_kind=NodeKind.ARTIFACT,
            idempotency_class=IdempotencyClass.IDEMPOTENT,
            input_ports=(port("content"),),
            output_ports=(port("artifact_id"),),
            metadata={
                "content": {"$input": "content"},
                "title": "Task3 combined result",
                "media_type": "text/plain",
            },
        ),
        NodeSpec(
            node_id="fallback",
            node_kind=NodeKind.JOIN,
            input_ports=(PortSpec(name="timeout", value_type="bool", required=False),),
        ),
    )
    pairs = (
        ("human", "answer", "approve", "answer"),
        ("approve", "answer", "wait", "answer"),
        ("wait", "answer", "agent", "input"),
        ("agent", "result", "child", "input"),
        ("child", "result", "artifact", "content"),
    )
    edges = tuple(
        EdgeSpec(
            edge_id=f"{a}-{c}",
            source_node=a,
            source_port=b,
            target_node=c,
            target_port=d,
            delivery_mode="value",
        )
        for a, b, c, d in pairs
    )
    edges += tuple(
        EdgeSpec(
            edge_id=f"{a}-timeout",
            source_node=a,
            source_port="timeout",
            target_node="fallback",
            target_port="timeout",
            join_mode="any",
            delivery_mode="value",
        )
        for a in ("human", "approve")
    )
    definition = WorkflowDefinition(
        workflow_id="task3-combined-" + uuid4().hex[:8],
        name="Task3 combined boundary",
        nodes=nodes,
        edges=edges,
        locked_role_versions={seed["role_id"]: 1},
        default_policy={
            "b24_executor": True,
            "team_id": seed["team_id"],
            "team_version": seed["team_version"],
        },
        default_budget=Budget(
            max_turns=6, timeout_seconds=300, max_output_tokens=8192, max_tool_calls=0
        ),
    )
    graphs.create_workflow_draft(definition.model_dump(mode="json"))
    compiled = graphs.compile_workflow_draft(definition.workflow_id, {"definition_version": 1})
    assert compiled["valid"], compiled
    published = graphs.publish_workflow(definition.workflow_id, {"draft_version": 1})
    version = int(published["resource_id"].rsplit(":v", 1)[1])
    receipt = graphs.start_graph_run(
        {
            "workflow_id": definition.workflow_id,
            "definition_version": version,
            "workspace_or_target": seed["workspace"],
            "input": {},
        }
    )
    return {
        "workflow_id": definition.workflow_id,
        "version": version,
        "run_id": receipt["resource_id"],
    }


def main() -> None:
    if os.environ.get("OPERANT_TASK3_REAL_ACCEPTANCE") != "1":
        raise RuntimeError("set OPERANT_TASK3_REAL_ACCEPTANCE=1 for isolated real acceptance")
    seed = json.loads(Path(os.environ["OPERANT_ACCEPTANCE_SEED_FILE"]).read_text())
    workspace = Path(seed["workspace"]).resolve(strict=True)
    if not workspace.is_relative_to(Path("/private/tmp")):
        raise RuntimeError("this acceptance only operates in /private/tmp")
    core_url = os.environ["OPERANT_ACCEPTANCE_CORE_URL"]
    evidence = Path(os.environ["OPERANT_ACCEPTANCE_EVIDENCE_DIR"])
    evidence.mkdir(parents=True, exist_ok=True)
    client = httpx.Client(base_url=core_url, timeout=120, trust_env=False)
    graphs = Phase23Client(core_url)
    schedules = Phase45Client(core_url)
    graphs.negotiate_protocol()
    schedules.negotiate_protocol()
    prefix = "task3-" + uuid4().hex[:10]

    def capture(name: str, value: Any) -> Any:
        (evidence / f"{name}.json").write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return value

    def wait_for(action: Any, condition: Any, *, seconds: int = 120) -> Any:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = action()
            if condition(value):
                return value
            time.sleep(0.5)
        raise AssertionError("formal Core projection did not reach the expected state")

    try:
        first = capture(
            "suggestion-create",
            graphs.suggest_workflow_draft(
                {
                    "instruction": (
                        "建立一个工作流，唯一节点是当前 Team 的 agent，任务是简短回答 TASK3_OK。"
                        "名称为 Task3 initial。不要添加其他节点或工具。"
                    ),
                    "team_id": seed["team_id"],
                    "team_version": seed["team_version"],
                }
            ),
        )
        assert first["model_id"] == seed["model_id"]
        assert first["definition"]["status"] == "draft"
        conversation_id = first["conversation_id"]
        second = capture(
            "suggestion-modify",
            graphs.suggest_workflow_draft(
                {
                    "instruction": (
                        "在上一轮候选基础上，仅把工作流名称改为 Task3 revised。"
                        "完整保留所有节点、连线、角色和任务。"
                    ),
                    "team_id": seed["team_id"],
                    "team_version": seed["team_version"],
                    "conversation_id": conversation_id,
                }
            ),
        )
        assert second["definition"]["name"] == "Task3 revised"
        assert second["conversation_id"] == conversation_id
        history = capture(
            "conversation", graphs.get_workflow_suggestion_conversation(conversation_id)
        )
        assert history["turn_count"] == 2
        assert len(history["turns"]) == 2
        candidate = second["definition"]
        missing = client.get(
            f"/v1/graph/workflows/{candidate['workflow_id']}/definitions/{candidate['version']}"
        )
        assert missing.status_code == 404, "suggestion must not silently save or publish"
        capture("draft-receipt", graphs.create_workflow_draft(candidate))
        compiled = capture(
            "compile",
            graphs.compile_workflow_draft(
                candidate["workflow_id"], {"definition_version": candidate["version"]}
            ),
        )
        assert compiled["valid"], compiled
        capture(
            "publish",
            graphs.publish_workflow(
                candidate["workflow_id"], {"draft_version": candidate["version"]}
            ),
        )

        watched = workspace / "signal.txt"
        repository = workspace / f"{prefix}-repo"
        repository.mkdir()

        def git(*args: str) -> str:
            return subprocess.check_output(["git", "-C", str(repository), *args], text=True).strip()

        git("init", "-q")
        git("config", "user.name", "Task3 Acceptance")
        git("config", "user.email", "task3@example.invalid")
        tracked = repository / "note.txt"
        tracked.write_text("baseline\n")
        git("add", "note.txt")
        git("commit", "-qm", "baseline")
        ids = [f"{prefix}-file", f"{prefix}-git"]
        for schedule_id, kind, path in zip(
            ids, ["file.changed", "git.head.changed"], [watched, repository], strict=True
        ):
            capture(
                schedule_id,
                schedules.create_schedule(
                    {
                        "id": schedule_id,
                        "name": schedule_id,
                        "trigger_kind": "hook",
                        "hook_event_type": kind,
                        "watch_path": str(path),
                        "timezone_name": "Asia/Shanghai",
                        "workflow_id": seed["workflow_id"],
                        "workflow_version": seed["workflow_version"],
                        "workflow_input": {"workspace_or_target": str(workspace)},
                        "max_attempts": 1,
                    }
                ),
            )
            status = wait_for(
                lambda sid=schedule_id: schedules.get_schedule_watch_status(sid),
                lambda value: value["initialized"],
            )
            assert status["error_code"] is None
        watched.write_text(f"{prefix} changed\n")
        tracked.write_text("committed change\n")
        git("add", "note.txt")
        git("commit", "-qm", "changed")
        for schedule_id in ids:

            def queue(sid: str = schedule_id) -> list[Any]:
                result = client.get("/v1/scheduler/queue", params={"limit": 200})
                result.raise_for_status()
                return [item for item in result.json()["items"] if item["schedule_id"] == sid]

            requests = wait_for(
                queue, lambda values: bool(values) and values[0]["status"] == "succeeded"
            )
            assert len(requests) == 1
            graph_run = capture(
                schedule_id + "-graph", graphs.get_graph_run(requests[0]["workflow_run_id"])
            )
            assert graph_run["status"] == "completed"
            capture(schedule_id + "-nodes", graphs.list_node_runs(graph_run["id"]))
            capture(schedule_id + "-queue", requests)
            assert schedules.get_schedule_watch_status(schedule_id)["generation"] == 1
            schedules.set_schedule_status(schedule_id, {"status": "paused", "expected_version": 1})
        capture(
            "result",
            {
                "model_id": seed["model_id"],
                "entry": "Phase23/Phase45 generated clients over loopback HTTP",
                "conversation_id": conversation_id,
                "schedules": ids,
                "status": "passed",
            },
        )
        print("TASK3_REAL_HTTP_MODEL_WATCH_OK", seed["model_id"])
    finally:
        client.close()


if __name__ == "__main__":
    main()
