"""Pinned child execution shares parent grants, budgets, cancellation and recovery."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from test_b24_graph_execution import (
    RecordingProvider,
    RecordingService,
    _definition,
    _executor,
    _node,
    _service,
    _team,
)

from operant.application.graph import GraphStateError
from operant.application.graph_execution import GraphExecutionError
from operant.application.subworkflow import aggregate_usage, child_budget
from operant.domain.graph import (
    BoundaryKind,
    BoundaryResolution,
    EdgeSpec,
    GraphLimits,
    GraphRunStatus,
    LoopPolicy,
    NodeKind,
    NodeRunStatus,
    NodeSpec,
    PortSpec,
    TimeoutPolicy,
)
from operant.domain.models import Budget
from operant.persistence.sqlite import SQLiteStore


def _family(
    tmp_path: Path,
    *,
    human: bool = False,
    budget: Budget | None = None,
    child_capabilities: tuple[str, ...] = (),
):
    provider = RecordingProvider()
    service, role = _service(tmp_path, provider)
    graphs, teams, runtime, executor = _executor(service)
    teams.put_team_definition(_team("parent", role.id, ("root-agent",)))
    teams.put_team_definition(_team("child", role.id, ("child-agent",)))
    child_nodes = (_node("child-agent", role.id, role.version),)
    edges = ()
    if human:
        child_nodes = (
            NodeSpec(
                node_id="human",
                node_kind=NodeKind.HUMAN_INPUT,
                output_ports=(
                    PortSpec(name="answer", value_type="string"),
                    PortSpec(name="timeout", value_type="bool", required=False),
                ),
                timeout_policy=TimeoutPolicy(timeout_seconds=30, on_timeout_node_id="fallback"),
                metadata={"prompt": "Supply child input"},
            ),
            *child_nodes,
            NodeSpec(
                node_id="fallback",
                node_kind=NodeKind.JOIN,
                input_ports=(PortSpec(name="timeout", value_type="bool"),),
            ),
        )
        edges = (
            EdgeSpec(
                edge_id="human-agent",
                source_node="human",
                source_port="answer",
                target_node="child-agent",
                target_port="input",
            ),
            EdgeSpec(
                edge_id="human-timeout",
                source_node="human",
                source_port="timeout",
                target_node="fallback",
                target_port="timeout",
            ),
        )
    child = _definition(
        role.id,
        role.version,
        team_id="child",
        nodes=child_nodes,
        edges=edges,
        budget=Budget(max_turns=10, max_output_tokens=100),
    )
    child = child.model_copy(update={"required_capabilities": child_capabilities})
    graphs.put_definition(child)
    parent = _definition(
        role.id,
        role.version,
        team_id="parent",
        nodes=(
            _node("root-agent", role.id, role.version),
            NodeSpec(
                node_id="child",
                node_kind=NodeKind.SUBWORKFLOW,
                subworkflow_id=child.workflow_id,
                subworkflow_version=child.version,
                output_ports=(PortSpec(name="result", value_type="string"),),
            ),
        ),
        budget=budget or Budget(max_turns=8, max_output_tokens=20, max_tool_calls=0),
    )
    run = runtime.create_run(parent, workspace_or_target=str(tmp_path))
    return service, role, provider, graphs, teams, runtime, executor, parent, child, run


@pytest.mark.asyncio
async def test_child_uses_disjoint_budget_and_aggregate_is_restart_stable(tmp_path: Path):
    service, _role, provider, graphs, _teams, _runtime, executor, _parent, child_def, run = _family(
        tmp_path
    )
    result = await executor.run(run.id)
    assert result.run.status is GraphRunStatus.COMPLETED
    children = graphs.list_child_runs(run.id)
    assert len(children) == 1
    child = children[0]
    assert child.workflow_definition_id == child_def.workflow_id
    assert child.budget_snapshot.max_output_tokens == 10
    assert child.budget_snapshot.max_turns == 4
    assert child.parent_node_run_id is not None
    assert len(provider.calls) == 2
    assert aggregate_usage(graphs, graphs.get_run(run.id))["consumed_output_tokens"] == 6
    assert graphs.get_run(run.id).consumed_output_tokens == 3
    service.close()
    restarted = RecordingService(
        SQLiteStore(service.store.path), RecordingProvider(), artifact_root=tmp_path / "artifacts"
    )
    restarted.initialize()
    restarted_graphs, _teams, _runtime, second = _executor(restarted)
    assert (
        aggregate_usage(restarted_graphs, restarted_graphs.get_run(run.id))[
            "consumed_output_tokens"
        ]
        == 6
    )
    assert (await second.run(run.id)).run.status is GraphRunStatus.COMPLETED
    assert not restarted.provider.calls


def test_invalid_pinned_grants_and_reservations_fail_before_provider(tmp_path: Path):
    _service_value, _role, provider, graphs, teams, _runtime, executor, _parent, child, run = (
        _family(tmp_path, child_capabilities=("workspace.write",))
    )
    with pytest.raises(GraphExecutionError, match="capability grant"):
        executor.prepare(run.id)
    assert graphs.get_run(run.id).team_run_id is None
    assert not provider.calls
    assert teams.list_roster("missing") == ()


def test_child_budget_cannot_be_injected_or_enlarged(tmp_path: Path):
    _service_value, _role, _provider, graphs, _teams, runtime, executor, parent, child, run = (
        _family(tmp_path)
    )
    with pytest.raises(GraphStateError, match="root budget"):
        runtime.create_run(parent, budget_snapshot=Budget(max_turns=100))
    executor.prepare(run.id)
    runtime.start_run(run.id)
    node_run = next(item for item in graphs.list_node_runs(run.id) if item.node_id == "child")
    runtime.enter_boundary(node_run.id, BoundaryKind.SUBWORKFLOW)
    node_run = graphs.get_node_run(node_run.id)
    spec = next(item for item in parent.nodes if item.node_id == "child")
    maximum = child_budget(graphs.get_run(run.id), parent, spec, child)
    with pytest.raises(GraphStateError, match="budget exceeds"):
        runtime.create_run(
            child,
            workspace_or_target=str(tmp_path),
            parent_run_id=run.id,
            parent_node_run_id=node_run.id,
            budget_snapshot=maximum.model_copy(update={"max_output_tokens": 11}),
        )
    assert graphs.list_child_runs(run.id) == ()


async def _wait_child(graphs, run_id: str):
    for _ in range(100):
        children = graphs.list_child_runs(run_id)
        if children:
            nodes = graphs.list_node_runs(children[0].id)
            waiting = next(
                (item for item in nodes if item.node_id == "human" and item.wait_token), None
            )
            if waiting is not None:
                return children[0], waiting
        await asyncio.sleep(0.02)
    pytest.fail("child never reached persisted Human Input")


@pytest.mark.asyncio
async def test_parent_restart_retains_child_token_and_never_duplicates_child(tmp_path: Path):
    service, _role, provider, graphs, _teams, _runtime, executor, _parent, _child, run = _family(
        tmp_path, human=True
    )
    task = asyncio.create_task(executor.run(run.id))
    child, human = await _wait_child(graphs, run.id)
    for _ in range(100):
        root_agent = next(
            node for node in graphs.list_node_runs(run.id) if node.node_id == "root-agent"
        )
        if (
            root_agent.status is NodeRunStatus.SUCCEEDED
            and graphs.get_run(child.id).status is GraphRunStatus.WAITING_INPUT
        ):
            break
        await asyncio.sleep(0.02)
    else:
        task.cancel()
        pytest.fail("root Agent and child Human Input did not settle before restart")
    assert len(provider.calls) == 1
    old_child_task = executor._child_tasks.get(child.id)
    assert old_child_task is not None
    executor.interrupt(run.id)
    await task
    assert old_child_task.done(), "parent run returned before the child stopped"
    service.close()
    restarted = RecordingService(
        SQLiteStore(service.store.path), provider, artifact_root=tmp_path / "artifacts"
    )
    restarted.initialize()
    graphs, _teams, runtime, executor = _executor(restarted)
    assert graphs.get_node_run(human.id).wait_token == human.wait_token
    assert graphs.get_run(child.id).status is GraphRunStatus.INTERRUPTED
    task = asyncio.create_task(executor.run(run.id))
    for _ in range(100):
        if graphs.get_run(child.id).status is not GraphRunStatus.INTERRUPTED:
            break
        await asyncio.sleep(0.02)
    runtime.resolve_boundary(
        human.id, BoundaryResolution(wait_token=human.wait_token, payload={"answer": "go"})
    )
    result = await asyncio.wait_for(task, 5)
    assert result.run.status is GraphRunStatus.COMPLETED
    assert [item.id for item in graphs.list_child_runs(run.id)] == [child.id]
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_parent_interrupt_waits_for_delayed_child_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _service_value, _role, _provider, graphs, _teams, _runtime, executor, _parent, _child, run = (
        _family(tmp_path, human=True)
    )
    shutdown_requested = asyncio.Event()
    shutdown_entered = asyncio.Event()
    release_shutdown = asyncio.Event()
    original_run_locked = executor._run_locked

    async def delayed_child_shutdown(run_id: str):
        try:
            return await original_run_locked(run_id)
        finally:
            if run_id != run.id and shutdown_requested.is_set():
                shutdown_entered.set()
                await release_shutdown.wait()

    monkeypatch.setattr(executor, "_run_locked", delayed_child_shutdown)
    parent_task = asyncio.create_task(executor.run(run.id))
    child, _human = await _wait_child(graphs, run.id)
    for _ in range(100):
        root_agent = next(
            node for node in graphs.list_node_runs(run.id) if node.node_id == "root-agent"
        )
        if (
            root_agent.status is NodeRunStatus.SUCCEEDED
            and graphs.get_run(child.id).status is GraphRunStatus.WAITING_INPUT
        ):
            break
        await asyncio.sleep(0.02)
    else:
        parent_task.cancel()
        pytest.fail("root Agent did not settle before interruption")
    child_task = executor._child_tasks.get(child.id)
    assert child_task is not None
    assert not child_task.done()
    try:
        shutdown_requested.set()
        executor.interrupt(run.id)
        await asyncio.wait_for(shutdown_entered.wait(), 5)
        await asyncio.sleep(0)
        assert not parent_task.done(), "parent returned while child shutdown was still pending"
    finally:
        release_shutdown.set()
    result = await asyncio.wait_for(parent_task, 5)
    assert result.status is GraphRunStatus.INTERRUPTED
    assert child_task.done()
    assert graphs.get_run(child.id).status is GraphRunStatus.INTERRUPTED


@pytest.mark.asyncio
async def test_graph_timeout_persists_failed_run_on_python_310(tmp_path: Path):
    service, role = _service(tmp_path, RecordingProvider())
    graphs, teams, runtime, executor = _executor(service)
    teams.put_team_definition(_team("timeout", role.id, ("root-agent",)))
    definition = _definition(
        role.id,
        role.version,
        team_id="timeout",
        nodes=(
            _node("root-agent", role.id, role.version),
            NodeSpec(
                node_id="human",
                node_kind=NodeKind.HUMAN_INPUT,
                output_ports=(
                    PortSpec(name="answer", value_type="string"),
                    PortSpec(name="timeout", value_type="bool", required=False),
                ),
                timeout_policy=TimeoutPolicy(timeout_seconds=30, on_timeout_node_id="fallback"),
                metadata={"prompt": "Wait for input beyond the Graph budget"},
            ),
            NodeSpec(
                node_id="fallback",
                node_kind=NodeKind.JOIN,
                input_ports=(PortSpec(name="timeout", value_type="bool"),),
            ),
        ),
        edges=(
            EdgeSpec(
                edge_id="human-timeout",
                source_node="human",
                source_port="timeout",
                target_node="fallback",
                target_port="timeout",
            ),
        ),
        budget=Budget(
            max_turns=8,
            timeout_seconds=1,
            max_output_tokens=20,
            max_tool_calls=0,
        ),
    )
    run = runtime.create_run(definition, workspace_or_target=str(tmp_path))
    result = await asyncio.wait_for(executor.run(run.id), 5)
    assert result.status is GraphRunStatus.FAILED
    assert graphs.get_run(run.id).status is GraphRunStatus.FAILED
    assert all(node.status is not NodeRunStatus.WAITING_INPUT for node in result.node_runs)


@pytest.mark.asyncio
async def test_parent_cancel_stops_waiting_child_before_model_call(tmp_path: Path):
    _service_value, _role, provider, graphs, _teams, _runtime, executor, _parent, _child, run = (
        _family(tmp_path, human=True)
    )
    task = asyncio.create_task(executor.run(run.id))
    child, _human = await _wait_child(graphs, run.id)
    executor.cancel(run.id)
    result = await asyncio.wait_for(task, 5)
    assert result.run.status is GraphRunStatus.CANCELLED
    assert graphs.get_run(child.id).status is GraphRunStatus.CANCELLED
    # The root Agent may be fenced before admission; the waiting child never calls a model.
    assert len(provider.calls) <= 1


def test_undersized_family_budget_fails_before_roster_or_model(tmp_path: Path):
    _service_value, _role, provider, graphs, _teams, _runtime, executor, _parent, _child, run = (
        _family(tmp_path, budget=Budget(max_turns=1, max_output_tokens=20))
    )
    with pytest.raises(GraphExecutionError, match="turn budget"):
        executor.prepare(run.id)
    assert graphs.get_run(run.id).team_run_id is None
    assert not provider.calls


def test_model_profile_drift_cannot_change_an_existing_graph_family(tmp_path: Path):
    service, role, provider, graphs, _teams, _runtime, executor, _parent, _child, run = _family(
        tmp_path
    )
    executor.prepare(run.id)
    fingerprint = graphs.get_run(run.id).policy_snapshot["__operant_config_fingerprints"]
    service.update_model_profile(role.model_profile_id, model_id="replacement-model")
    with pytest.raises(GraphExecutionError, match="frozen Graph configuration changed"):
        executor.prepare(run.id)
    assert graphs.get_run(run.id).policy_snapshot["__operant_config_fingerprints"] == fingerprint
    assert not provider.calls


@pytest.mark.asyncio
async def test_loop_creates_distinct_pinned_child_runs_and_aggregates_each_budget(tmp_path: Path):
    service, role, provider, graphs, teams, runtime, executor, _parent, child, _run = _family(
        tmp_path, budget=Budget(max_turns=8, max_output_tokens=20, max_tool_calls=0)
    )
    subworkflow = NodeSpec(
        node_id="child",
        node_kind=NodeKind.SUBWORKFLOW,
        input_ports=(PortSpec(name="input", required=False),),
        output_ports=(PortSpec(name="result", value_type="string"),),
        subworkflow_id=child.workflow_id,
        subworkflow_version=child.version,
    )
    loop = NodeSpec(
        node_id="loop",
        node_kind=NodeKind.LOOP,
        input_ports=(PortSpec(name="value", value_type="string"),),
        output_ports=(PortSpec(name="value", value_type="string"),),
        loop_policy=LoopPolicy(
            max_iterations=2,
            max_wall_seconds=30,
            max_output_tokens=20,
            max_cost_usd=1,
            max_subagents=2,
            max_recursion_depth=2,
            exit_expression="iteration >= 2",
            on_limit_node_id="limit",
        ),
    )
    parent = _definition(
        role.id,
        role.version,
        team_id="parent",
        nodes=(
            _node("root-agent", role.id, role.version),
            subworkflow,
            loop,
            NodeSpec(
                node_id="limit",
                node_kind=NodeKind.JOIN,
                input_ports=(PortSpec(name="value", value_type="string"),),
            ),
        ),
        edges=(
            EdgeSpec(
                edge_id="root-child",
                source_node="root-agent",
                source_port="result",
                target_node="child",
                target_port="input",
            ),
            EdgeSpec(
                edge_id="child-loop",
                source_node="child",
                source_port="result",
                target_node="loop",
                target_port="value",
            ),
            EdgeSpec(
                edge_id="loop-child",
                source_node="loop",
                source_port="value",
                target_node="child",
                target_port="input",
                loop_back=True,
            ),
            EdgeSpec(
                edge_id="loop-limit",
                source_node="loop",
                source_port="value",
                target_node="limit",
                target_port="value",
            ),
        ),
        budget=Budget(max_turns=8, max_output_tokens=20, max_tool_calls=0),
    ).model_copy(update={"workflow_id": "graph.subworkflow.loop"})
    run = runtime.create_run(parent, workspace_or_target=str(tmp_path))
    result = await asyncio.wait_for(executor.run(run.id), 10)
    assert result.status is GraphRunStatus.COMPLETED
    children = graphs.list_child_runs(run.id)
    assert len(children) == 2
    assert len({item.id for item in children}) == 2
    assert {item.parent_node_iteration for item in children} == {0, 1}
    assert {item.workflow_definition_id for item in children} == {child.workflow_id}
    assert all(item.status is GraphRunStatus.COMPLETED for item in children)
    assert all(item.budget_snapshot.max_turns == 2 for item in children)
    assert all(item.budget_snapshot.max_output_tokens == 5 for item in children)
    child_node = next(item for item in result.node_runs if item.node_id == "child")
    assert child_node.iteration == 1
    assert next(item for item in result.node_runs if item.node_id == "loop").iteration == 2
    assert (
        next(item for item in result.node_runs if item.node_id == "limit").status
        is NodeRunStatus.SKIPPED
    )
    assert len(provider.calls) == 3  # one root Agent, one Agent per child Run
    assert result.run.consumed_output_tokens == 3
    assert sum(item.consumed_output_tokens for item in children) == 6
    assert aggregate_usage(graphs, graphs.get_run(run.id))["consumed_output_tokens"] == 9
    assert len(teams.list_roster(result.run.team_run_id or "")) == 1
    service.close()


def test_indirect_recursive_pinned_subworkflow_fails_before_provider(tmp_path: Path):
    _service_value, role, provider, graphs, _teams, runtime, executor, parent, child, _run = (
        _family(tmp_path)
    )
    child_v2 = child.model_copy(
        update={
            "version": 2,
            "nodes": (
                _node("child-agent", role.id, role.version),
                NodeSpec(
                    node_id="back-to-parent",
                    node_kind=NodeKind.SUBWORKFLOW,
                    subworkflow_id=parent.workflow_id,
                    subworkflow_version=2,
                ),
            ),
        }
    )
    parent_v2 = parent.model_copy(
        update={
            "version": 2,
            "nodes": (
                _node("root-agent", role.id, role.version),
                NodeSpec(
                    node_id="child",
                    node_kind=NodeKind.SUBWORKFLOW,
                    subworkflow_id=child_v2.workflow_id,
                    subworkflow_version=child_v2.version,
                ),
            ),
        }
    )
    graphs.put_definition(child_v2)
    run = runtime.create_run(parent_v2, workspace_or_target=str(tmp_path))
    with pytest.raises(GraphExecutionError, match="recursive Subworkflow"):
        executor.prepare(run.id)
    assert graphs.get_run(run.id).team_run_id is None
    assert not provider.calls


@pytest.mark.parametrize(
    ("limits", "expected"),
    [
        (GraphLimits(max_recursion_depth=2), "recursion depth"),
        (GraphLimits(max_subagents=1), "sub-agent limit"),
    ],
)
def test_nested_child_depth_and_agent_limits_fail_before_provider(
    tmp_path: Path, limits: GraphLimits, expected: str
):
    _service_value, role, provider, graphs, teams, runtime, executor, parent, child, _run = _family(
        tmp_path
    )
    teams.put_team_definition(_team("grandchild", role.id, ("grandchild-agent",)))
    grandchild = _definition(
        role.id,
        role.version,
        team_id="grandchild",
        nodes=(_node("grandchild-agent", role.id, role.version),),
    )
    graphs.put_definition(grandchild)
    child_v2 = child.model_copy(
        update={
            "version": 2,
            "nodes": (
                _node("child-agent", role.id, role.version),
                NodeSpec(
                    node_id="grandchild",
                    node_kind=NodeKind.SUBWORKFLOW,
                    subworkflow_id=grandchild.workflow_id,
                    subworkflow_version=grandchild.version,
                ),
            ),
        }
    )
    graphs.put_definition(child_v2)
    parent_v2 = parent.model_copy(
        update={
            "version": 2,
            "nodes": (
                _node("root-agent", role.id, role.version),
                NodeSpec(
                    node_id="child",
                    node_kind=NodeKind.SUBWORKFLOW,
                    subworkflow_id=child_v2.workflow_id,
                    subworkflow_version=child_v2.version,
                ),
            ),
            "graph_limits": limits,
        }
    )
    run = runtime.create_run(parent_v2, workspace_or_target=str(tmp_path))
    with pytest.raises(GraphExecutionError, match=expected):
        executor.prepare(run.id)
    assert graphs.get_run(run.id).team_run_id is None
    assert not provider.calls
