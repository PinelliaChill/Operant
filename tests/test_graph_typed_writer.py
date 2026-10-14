"""Typed Graph writers execute in trusted Git worktrees before formal merge."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

from operant.api import create_app
from operant.application.graph import GraphRuntime
from operant.application.security import PolicyEngine
from operant.domain.graph import (
    EdgeSpec,
    GraphRunStatus,
    IdempotencyClass,
    NodeKind,
    NodeRunStatus,
    NodeSpec,
    PortSpec,
    WorkflowDefinition,
    WorkflowDefinitionStatus,
)
from operant.domain.messages import ModelResponse, ProviderEvent
from operant.domain.models import ModelProfile, RolePreset, RoleSnapshot, ToolPolicy
from operant.domain.multiwriter import (
    MergeNodePolicy,
    MergeRun,
    MergeRunStatus,
    MergeStrategy,
    WriterIsolationKind,
    WriterNodePolicy,
)
from operant.domain.security import PolicyBundle, PolicyDecision
from operant.domain.team import TeamDefinition, TeamMember
from operant.persistence.graph_team import SQLiteGraphRepository, SQLiteTeamRepository


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()


class _Provider:
    async def list_models(self, *, base_url: str, secret_ref: str) -> list[str]:
        return ["graph-writer-test-model"]

    async def stream(
        self, *, snapshot: RoleSnapshot, messages: Sequence[Any], tools: Sequence[Any]
    ) -> AsyncIterator[ProviderEvent]:
        yield ProviderEvent(
            event_type="model.completed",
            response=ModelResponse(content="source ready", finish_reason="stop"),
        )


def _fixture(tmp_path: Path, *, decision: PolicyDecision = PolicyDecision.ALLOW) -> dict[str, Any]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Graph Writer Test")
    _git(repository, "config", "user.email", "graph-writer@example.invalid")
    for key in ("a", "b"):
        path = repository / "src" / key / "value.txt"
        path.parent.mkdir(parents=True)
        path.write_text("base\n")
    _git(repository, "add", "src")
    _git(repository, "commit", "-m", "base")
    base = _git(repository, "rev-parse", "HEAD")
    roots = {key: tmp_path / f"writer-{key}" for key in ("a", "b")}
    for key, root in roots.items():
        _git(repository, "worktree", "add", "-b", f"writer-{key}", str(root), base)
    target = tmp_path / "target"
    _git(repository, "worktree", "add", "-b", "target", str(target), base)
    app = create_app(
        tmp_path / "core.sqlite3",
        artifact_root=tmp_path / "artifacts",
        phase45_policy_engine=PolicyEngine(
            PolicyBundle(
                bundle_id="graph-writer-test",
                version="graph-writer-test.v1",
                default_decision=decision,
                rules=(),
            )
        ),
        phase56_multiwriter_roots={
            "writer-a": roots["a"],
            "writer-b": roots["b"],
            "target": target,
        },
    )
    service = app.state.operant_service
    service.provider = _Provider()
    service.factory.provider = service.provider
    profile = service.add_model_profile(
        ModelProfile(
            name="Graph writer test",
            model_id="graph-writer-test-model",
            base_url="https://invalid.test/v1",
            secret_ref="GRAPH_WRITER_TEST_KEY",
        )
    )
    source_role = service.create_role(
        RolePreset(
            name="Source",
            system_prompt="Provide source signal.",
            model_profile_id=profile.id,
        )
    )
    script_role = service.create_role(
        RolePreset(
            name="Writer",
            system_prompt="Execute assigned script.",
            model_profile_id=profile.id,
            tool_policy=ToolPolicy(
                allowed_tools=("run_command",),
                workspace_write=True,
                command_execution=True,
            ),
        )
    )
    nodes = [
        NodeSpec(
            node_id="source",
            node_kind=NodeKind.AGENT,
            output_ports=(PortSpec(name="result", required=False),),
            metadata={"role_id": source_role.id, "role_version": source_role.version},
        )
    ]
    for key in ("a", "b"):
        script = (
            "from pathlib import Path; import subprocess; "
            f"Path('src/{key}/value.txt').write_text('{key} changed\\n'); "
            f"subprocess.run(['git', 'add', 'src/{key}/value.txt'], check=True); "
            f"subprocess.run(['git', 'commit', '-m', 'writer {key}'], check=True)"
        )
        nodes.append(
            NodeSpec(
                node_id=f"writer-{key}",
                node_kind=NodeKind.SCRIPT,
                input_ports=(PortSpec(name="input", required=False),),
                output_ports=(PortSpec(name="writer_artifact_id"),),
                writes_workspace=True,
                idempotency_class=IdempotencyClass.NON_IDEMPOTENT,
                writer_policy=WriterNodePolicy(
                    writer_key=key,
                    isolation_kind=WriterIsolationKind.WORKTREE,
                    isolation_ref=f"writer-{key}",
                    ownership_paths=(f"src/{key}",),
                ),
                metadata={
                    "role_id": script_role.id,
                    "role_version": script_role.version,
                    "argv": [sys.executable, "-c", script],
                },
            )
        )
    nodes.append(
        NodeSpec(
            node_id="merge",
            node_kind=NodeKind.MERGE,
            input_ports=(PortSpec(name="artifacts", required=False),),
            output_ports=(PortSpec(name="result_artifact_ref"),),
            writes_workspace=True,
            idempotency_class=IdempotencyClass.IDEMPOTENT,
            merge_policy=MergeNodePolicy(
                strategy=MergeStrategy.CHERRY_PICK,
                source_writer_keys=("a", "b"),
                require_review=False,
            ),
        )
    )
    definition = WorkflowDefinition(
        workflow_id="graph.typed.writer.test",
        name="Typed writers",
        status=WorkflowDefinitionStatus.PUBLISHED,
        nodes=tuple(nodes),
        edges=tuple(
            [
                EdgeSpec(
                    edge_id=f"source-{key}",
                    source_node="source",
                    source_port="result",
                    target_node=f"writer-{key}",
                    target_port="input",
                )
                for key in ("a", "b")
            ]
            + [
                EdgeSpec(
                    edge_id=f"{key}-merge",
                    source_node=f"writer-{key}",
                    source_port="writer_artifact_id",
                    target_node="merge",
                    target_port="artifacts",
                )
                for key in ("a", "b")
            ]
        ),
        default_policy={"team_id": "graph-writer-team", "team_version": 1},
        locked_role_versions={
            source_role.id: source_role.version,
            script_role.id: script_role.version,
        },
    )
    graph_repository = SQLiteGraphRepository(service.store)
    team_repository = SQLiteTeamRepository(service.store)
    team_repository.put_team_definition(
        TeamDefinition(
            team_id="graph-writer-team",
            members=(
                TeamMember(
                    member_id="source",
                    agent_definition_id=source_role.id,
                    role="Source",
                    can_coordinate=True,
                ),
            ),
            default_coordinator="source",
        )
    )
    graph_run = GraphRuntime(graph_repository).create_run(
        definition, workspace_or_target=str(repository)
    )
    return {
        "app": app,
        "service": service,
        "graphs": graph_repository,
        "definition": definition,
        "run": graph_run,
        "repository": repository,
        "roots": roots,
        "target": target,
        "base": base,
    }


async def _approve_scripts(context: dict[str, Any], task: asyncio.Task[Any]) -> None:
    graphs = context["graphs"]
    service = context["service"]
    run_id = context["run"].id
    approved: set[str] = set()
    for _ in range(150):
        for node in graphs.list_node_runs(run_id):
            if not node.node_id.startswith("writer-") or not node.active_attempt_id:
                continue
            attempt = graphs.get_attempt(node.active_attempt_id)
            if attempt.agent_instance_id is None or attempt.id in approved:
                continue
            session_id = service.store.get_agent(attempt.agent_instance_id).session_id
            if service.list_pending_approvals(session_id):
                assert service.decide_approval(session_id, attempt.id, approved=True)["accepted"]
                approved.add(attempt.id)
        if len(approved) == 2 or task.done():
            return
        await asyncio.sleep(0.05)
    task.cancel()
    pytest.fail("writer script approvals did not appear")


@pytest.mark.asyncio
async def test_two_script_writers_publish_commits_and_formal_merge(tmp_path: Path) -> None:
    context = _fixture(tmp_path)
    app = context["app"]
    run = context["run"]
    graphs = context["graphs"]
    executor = app.state.b24_graph_executor
    running = asyncio.create_task(executor.run(run.id))
    await _approve_scripts(context, running)
    for _ in range(150):
        states = {node.node_id: node for node in graphs.list_node_runs(run.id)}
        if states["merge"].status is NodeRunStatus.WAITING:
            break
        if running.done():
            break
        await asyncio.sleep(0.05)
    else:
        running.cancel()
        pytest.fail("Merge node did not enter its durable wait")
    assert not running.done()
    assert (context["repository"] / "src/a/value.txt").read_text() == "base\n"
    assert (context["repository"] / "src/b/value.txt").read_text() == "base\n"
    for key in ("a", "b"):
        assert (context["roots"][key] / f"src/{key}/value.txt").read_text() == f"{key} changed\n"
    writer_runtime = app.state.multiwriter_runtime
    artifacts = writer_runtime.repository.list_artifacts(run.id)
    assert len(artifacts) == 2
    assert {states[f"writer-{key}"].output_refs["writer_artifact_id"] for key in ("a", "b")} == {
        artifact.writer_artifact_id for artifact in artifacts
    }
    leases = tuple(
        writer_runtime.acquire_lease(
            artifact.writer_workspace_id, owner="graph-merge-test", ttl_seconds=300
        )
        for artifact in artifacts
    )
    merge = MergeRun(
        graph_run_id=run.id,
        merge_node_id="merge",
        artifact_ids=tuple(artifact.writer_artifact_id for artifact in artifacts),
        strategy=MergeStrategy.CHERRY_PICK,
        target_isolation_ref="target",
        base_revision=context["base"],
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        created = await client.post(
            "/v1/merge-runs",
            json={
                "merge_run": merge.model_dump(mode="json"),
                "leases": [lease.model_dump(mode="json") for lease in leases],
            },
        )
        assert created.status_code == 201, created.text
        finalized = await client.post(
            f"/v1/merge-runs/{merge.merge_run_id}/finalize",
            json={
                "leases": [lease.model_dump(mode="json") for lease in leases],
                "review_approved": True,
            },
        )
        assert finalized.status_code == 200, finalized.text
        assert finalized.json()["status"] == MergeRunStatus.SUCCEEDED.value
    result = await asyncio.wait_for(running, 10)
    assert result.status is GraphRunStatus.COMPLETED
    merge_node = next(node for node in result.node_runs if node.node_id == "merge")
    assert merge_node.output_refs["result_artifact_ref"] == finalized.json()["result_artifact_ref"]
    for key in ("a", "b"):
        assert (context["target"] / f"src/{key}/value.txt").read_text() == f"{key} changed\n"
    assert _git(context["target"], "status", "--porcelain") == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", [PolicyDecision.DENY, PolicyDecision.ASK])
async def test_writer_gateway_blocks_before_isolated_script(
    tmp_path: Path, decision: PolicyDecision
) -> None:
    context = _fixture(tmp_path, decision=decision)
    app = context["app"]
    run = context["run"]
    executor = app.state.b24_graph_executor
    running = asyncio.create_task(executor.run(run.id))
    if decision is PolicyDecision.ASK:
        for _ in range(100):
            with context["service"].store._connect() as connection:
                pending = connection.execute(
                    "SELECT COUNT(*) FROM phase45_approval_requests WHERE status = 'pending'"
                ).fetchone()[0]
            if pending:
                break
            await asyncio.sleep(0.05)
        else:
            running.cancel()
            pytest.fail("Gateway ASK approval did not appear")
        executor.cancel(run.id)
    result = await asyncio.wait_for(running, 10)
    assert result.status is not GraphRunStatus.COMPLETED
    assert app.state.multiwriter_runtime.repository.list_artifacts(run.id) == ()
    for key in ("a", "b"):
        assert (context["roots"][key] / f"src/{key}/value.txt").read_text() == "base\n"
    assert (context["repository"] / "src/a/value.txt").read_text() == "base\n"


@pytest.mark.asyncio
async def test_started_writer_with_unknown_result_is_not_replayed_after_restart(
    tmp_path: Path,
) -> None:
    context = _fixture(tmp_path)
    graphs = context["graphs"]
    run_id = context["run"].id
    runtime = GraphRuntime(graphs)
    runtime.start_run(run_id)
    nodes = {node.node_id: node for node in graphs.list_node_runs(run_id)}
    source_attempt = runtime.start_attempt(nodes["source"].id)
    runtime.complete_attempt(source_attempt, succeeded=True, output_refs={"result": "ready"})
    writer = next(node for node in graphs.list_node_runs(run_id) if node.node_id == "writer-a")
    assert writer.status is NodeRunStatus.READY
    attempt = runtime.start_attempt(writer.id)
    started = runtime.mark_side_effect_started(attempt.id)
    assert started.side_effect_state.value == "started"

    # Simulate a Core crash after dispatch but before a trustworthy result. A
    # new Core instance must retain the manual fence and never execute argv.
    context["service"].close()
    restarted = create_app(
        tmp_path / "core.sqlite3",
        artifact_root=tmp_path / "artifacts",
        phase45_policy_engine=PolicyEngine(
            PolicyBundle(
                bundle_id="graph-writer-test",
                version="graph-writer-test.v1",
                default_decision=PolicyDecision.ALLOW,
                rules=(),
            )
        ),
        phase56_multiwriter_roots={
            "writer-a": context["roots"]["a"],
            "writer-b": context["roots"]["b"],
            "target": context["target"],
        },
    )
    resumed_graphs = SQLiteGraphRepository(restarted.state.operant_service.store)
    recovered = GraphRuntime(resumed_graphs).recover(run_id)
    assert recovered.status is GraphRunStatus.MANUAL_RECONCILE_REQUIRED
    result = await restarted.state.b24_graph_executor.run(run_id)
    assert result.status is GraphRunStatus.MANUAL_RECONCILE_REQUIRED
    writer_after = resumed_graphs.get_node_run(writer.id)
    assert writer_after.status is NodeRunStatus.MANUAL_RECONCILE_REQUIRED
    assert len(resumed_graphs.list_attempts(writer.id)) == 1
    assert resumed_graphs.list_attempts(writer.id)[0].side_effect_state.value == "unknown"
    assert restarted.state.multiwriter_runtime.repository.list_artifacts(run_id) == ()
    for key in ("a", "b"):
        assert (context["roots"][key] / f"src/{key}/value.txt").read_text() == "base\n"
    restarted.state.operant_service.close()
