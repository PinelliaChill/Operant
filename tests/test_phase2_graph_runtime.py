from __future__ import annotations

import pytest

from operant.application.graph import (
    GraphCompilationError,
    GraphCompiler,
    GraphConflictError,
    GraphRuntime,
    GraphStateError,
    InMemoryGraphRepository,
    coding_workflow_definition,
)
from operant.domain.graph import (
    AttemptResult,
    AttemptSideEffectState,
    BoundaryKind,
    BoundaryResolution,
    EdgeSpec,
    GraphLimits,
    GraphRunStatus,
    IdempotencyClass,
    JoinMode,
    LoopPolicy,
    NodeKind,
    NodeRunStatus,
    NodeSpec,
    PortSpec,
    RetryPolicy,
    TimeoutPolicy,
    WorkflowDefinition,
    WorkflowDefinitionStatus,
)
from operant.domain.models import Budget


def _sequential_definition(
    *,
    second_kind: NodeKind = NodeKind.ARTIFACT,
    second_idempotency: IdempotencyClass = IdempotencyClass.PURE,
) -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id="test.sequential",
        version=7,
        name="sequential",
        nodes=(
            NodeSpec(
                node_id="first",
                node_kind=NodeKind.AGENT,
                output_ports=(PortSpec(name="out", value_type="json"),),
            ),
            NodeSpec(
                node_id="second",
                node_kind=second_kind,
                input_ports=(PortSpec(name="in", value_type="json"),),
                idempotency_class=second_idempotency,
                writes_workspace=second_idempotency is IdempotencyClass.NON_IDEMPOTENT,
            ),
        ),
        edges=(
            EdgeSpec(
                edge_id="first-second",
                source_node="first",
                source_port="out",
                target_node="second",
                target_port="in",
            ),
        ),
        default_budget=Budget(max_output_tokens=100, max_cost_usd=2, max_tool_calls=3),
        status=WorkflowDefinitionStatus.PUBLISHED,
    )


def _node(repository: InMemoryGraphRepository, run_id: str, spec_id: str):
    return next(node for node in repository.list_node_runs(run_id) if node.node_id == spec_id)


def test_ir_round_trips_and_run_pins_definition_and_snapshots() -> None:
    definition = _sequential_definition()
    restored = WorkflowDefinition.model_validate_json(definition.model_dump_json())
    assert restored == definition

    repository = InMemoryGraphRepository()
    runtime = GraphRuntime(repository)
    run = runtime.create_run(definition, input={"task": "x"}, workspace_or_target="/tmp/x")

    assert (run.workflow_definition_id, run.workflow_definition_version) == (
        definition.workflow_id,
        7,
    )
    assert run.budget_snapshot == definition.default_budget
    assert _node(repository, run.id, "first").status is NodeRunStatus.READY
    assert _node(repository, run.id, "first").input_refs == {"task": "x"}
    assert _node(repository, run.id, "second").status is NodeRunStatus.PENDING


def test_compiler_rejects_multiple_writers_unsafe_retry_and_implicit_cycle() -> None:
    ports = (PortSpec(name="value", required=False),)
    definition = WorkflowDefinition(
        name="invalid",
        nodes=(
            NodeSpec(
                node_id="a",
                node_kind=NodeKind.TOOL,
                input_ports=ports,
                output_ports=ports,
                writes_workspace=True,
                idempotency_class=IdempotencyClass.NON_IDEMPOTENT,
                retry_policy=RetryPolicy(max_attempts=2),
            ),
            NodeSpec(
                node_id="b",
                node_kind=NodeKind.SCRIPT,
                input_ports=ports,
                output_ports=ports,
                writes_workspace=True,
                idempotency_class=IdempotencyClass.IDEMPOTENT,
            ),
        ),
        edges=(
            EdgeSpec(
                edge_id="ab",
                source_node="a",
                source_port="value",
                target_node="b",
                target_port="value",
            ),
            EdgeSpec(
                edge_id="ba",
                source_node="b",
                source_port="value",
                target_node="a",
                target_port="value",
            ),
        ),
    )

    with pytest.raises(GraphCompilationError) as raised:
        GraphCompiler().compile(definition)

    assert {issue.code for issue in raised.value.issues} >= {
        "multiple_writers",
        "unsafe_retry",
        "illegal_cycle",
    }


def test_compiler_requires_bounded_loop_and_durable_input_timeout() -> None:
    with pytest.raises(ValueError, match="loop_policy"):
        NodeSpec(node_id="loop", node_kind=NodeKind.LOOP)

    definition = WorkflowDefinition(
        name="approval-without-timeout",
        nodes=(NodeSpec(node_id="approval", node_kind=NodeKind.APPROVAL),),
    )
    with pytest.raises(GraphCompilationError) as raised:
        GraphCompiler().compile(definition)
    assert "missing_timeout_branch" in {issue.code for issue in raised.value.issues}


def test_compiler_requires_timeout_edge_and_consistent_join_mode() -> None:
    port = (PortSpec(name="value", required=False),)
    definition = WorkflowDefinition(
        name="invalid-routing",
        nodes=(
            NodeSpec(
                node_id="approval",
                node_kind=NodeKind.APPROVAL,
                output_ports=port,
                timeout_policy=TimeoutPolicy(timeout_seconds=1, on_timeout_node_id="fallback"),
            ),
            NodeSpec(node_id="other", node_kind=NodeKind.AGENT, output_ports=port),
            NodeSpec(node_id="fallback", node_kind=NodeKind.ARTIFACT, input_ports=port),
        ),
        edges=(
            EdgeSpec(
                edge_id="other-fallback",
                source_node="other",
                source_port="value",
                target_node="fallback",
                target_port="value",
                join_mode=JoinMode.ANY,
            ),
            EdgeSpec(
                edge_id="approval-other",
                source_node="approval",
                source_port="value",
                target_node="other",
                target_port="value",
                join_mode=JoinMode.ALL,
            ),
        ),
    )

    with pytest.raises(GraphCompilationError) as raised:
        GraphCompiler().compile(definition)

    assert "missing_timeout_edge" in {issue.code for issue in raised.value.issues}

    mixed = definition.model_copy(
        update={
            "edges": definition.edges
            + (
                EdgeSpec(
                    edge_id="approval-fallback",
                    source_node="approval",
                    source_port="value",
                    target_node="fallback",
                    target_port="value",
                    join_mode=JoinMode.ALL,
                ),
            )
        }
    )
    with pytest.raises(GraphCompilationError) as mixed_raised:
        GraphCompiler().compile(mixed)
    assert "mixed_join_mode" in {issue.code for issue in mixed_raised.value.issues}


def test_compiler_rejects_callable_condition_syntax() -> None:
    definition = _sequential_definition().model_copy(
        update={
            "edges": (
                _sequential_definition()
                .edges[0]
                .model_copy(update={"condition": "dangerous(payload)"}),
            )
        }
    )
    with pytest.raises(GraphCompilationError) as raised:
        GraphCompiler().compile(definition)
    assert "invalid_condition" in {issue.code for issue in raised.value.issues}


def test_compiler_represents_but_rejects_timer_until_scheduler_exists() -> None:
    definition = WorkflowDefinition(
        name="timer-is-not-scheduler",
        nodes=(NodeSpec(node_id="timer", node_kind=NodeKind.TIMER),),
    )
    with pytest.raises(GraphCompilationError) as raised:
        GraphCompiler().compile(definition)
    assert "scheduler_out_of_scope" in {issue.code for issue in raised.value.issues}


def test_sequential_run_attempts_unlock_dependencies_and_complete() -> None:
    repository = InMemoryGraphRepository()
    runtime = GraphRuntime(repository)
    run = runtime.create_run(_sequential_definition())
    assert runtime.start_run(run.id).status is GraphRunStatus.RUNNING

    first = _node(repository, run.id, "first")
    first_attempt = runtime.start_attempt(first.id, worker_id="worker-1", lease_id="lease-1")
    runtime.complete_attempt(first_attempt, succeeded=True, output_refs={"out": "artifact:a"})
    assert _node(repository, run.id, "second").status is NodeRunStatus.READY

    second = _node(repository, run.id, "second")
    second_attempt = runtime.start_attempt(second.id)
    runtime.complete_attempt(second_attempt, succeeded=True, output_refs={"done": True})
    assert repository.get_run(run.id).status is GraphRunStatus.COMPLETED


def test_unknown_writer_outcome_is_never_replayed_automatically() -> None:
    repository = InMemoryGraphRepository()
    runtime = GraphRuntime(repository)
    run = runtime.create_run(
        _sequential_definition(second_idempotency=IdempotencyClass.NON_IDEMPOTENT)
    )
    runtime.start_run(run.id)
    first = _node(repository, run.id, "first")
    runtime.complete_attempt(
        runtime.start_attempt(first.id), succeeded=True, output_refs={"out": "ready"}
    )
    writer = _node(repository, run.id, "second")
    attempt = runtime.start_attempt(writer.id, idempotency_key="operation-1")
    runtime.mark_side_effect_started(attempt.id)

    recovered = runtime.recover(run.id)

    assert recovered.status is GraphRunStatus.MANUAL_RECONCILE_REQUIRED
    assert repository.get_node_run(writer.id).status is NodeRunStatus.MANUAL_RECONCILE_REQUIRED
    with pytest.raises(GraphStateError, match="not running"):
        runtime.start_attempt(writer.id)


def test_not_started_idempotent_attempt_can_resume_at_node_boundary() -> None:
    repository = InMemoryGraphRepository()
    runtime = GraphRuntime(repository)
    definition = _sequential_definition(second_idempotency=IdempotencyClass.IDEMPOTENT)
    run = runtime.create_run(definition)
    runtime.start_run(run.id)
    first = _node(repository, run.id, "first")
    runtime.complete_attempt(
        runtime.start_attempt(first.id), succeeded=True, output_refs={"out": "ready"}
    )
    second = _node(repository, run.id, "second")
    runtime.start_attempt(second.id, idempotency_key="safe-op")

    recovered = runtime.recover(run.id)

    assert recovered.status is GraphRunStatus.RUNNING
    assert repository.get_node_run(second.id).status is NodeRunStatus.READY


def test_started_idempotent_attempt_retries_with_same_key() -> None:
    repository = InMemoryGraphRepository()
    runtime = GraphRuntime(repository)
    run = runtime.create_run(_sequential_definition(second_idempotency=IdempotencyClass.IDEMPOTENT))
    runtime.start_run(run.id)
    first = _node(repository, run.id, "first")
    runtime.complete_attempt(
        runtime.start_attempt(first.id), succeeded=True, output_refs={"out": "ready"}
    )
    second = _node(repository, run.id, "second")
    attempt = runtime.start_attempt(second.id, idempotency_key="stable-safe-op")
    runtime.mark_side_effect_started(attempt.id)

    recovered = runtime.recover(run.id)
    retried = runtime.start_attempt(second.id)

    assert recovered.status is GraphRunStatus.RUNNING
    assert retried.idempotency_key == "stable-safe-op"
    assert repository.get_attempt(attempt.id).side_effect_state is AttemptSideEffectState.STARTED
    assert repository.get_attempt(attempt.id).result is AttemptResult.INTERRUPTED


def test_known_retry_of_idempotent_node_keeps_the_logical_action_key() -> None:
    base = _sequential_definition(second_idempotency=IdempotencyClass.IDEMPOTENT)
    definition = base.model_copy(
        update={
            "nodes": (
                base.nodes[0],
                base.nodes[1].model_copy(update={"retry_policy": RetryPolicy(max_attempts=2)}),
            )
        }
    )
    repository = InMemoryGraphRepository()
    runtime = GraphRuntime(repository)
    run = runtime.create_run(definition)
    runtime.start_run(run.id)
    runtime.complete_attempt(
        runtime.start_attempt(_node(repository, run.id, "first").id),
        succeeded=True,
        output_refs={"out": "ready"},
    )
    second = _node(repository, run.id, "second")
    first_attempt = runtime.start_attempt(second.id)
    runtime.complete_attempt(first_attempt, succeeded=False, failure_class="transient")

    retry = runtime.start_attempt(second.id, idempotency_key="must-not-replace-logical-key")

    assert retry.idempotency_key == first_attempt.idempotency_key


def test_reported_unknown_idempotent_effect_is_safe_to_retry_with_same_key() -> None:
    repository = InMemoryGraphRepository()
    runtime = GraphRuntime(repository)
    run = runtime.create_run(_sequential_definition(second_idempotency=IdempotencyClass.IDEMPOTENT))
    runtime.start_run(run.id)
    runtime.complete_attempt(
        runtime.start_attempt(_node(repository, run.id, "first").id),
        succeeded=True,
        output_refs={"out": "ready"},
    )
    second = _node(repository, run.id, "second")
    attempt = runtime.start_attempt(second.id, idempotency_key="safe-unknown")
    runtime.mark_side_effect_started(attempt.id)

    node = runtime.complete_attempt(
        attempt,
        succeeded=False,
        side_effect_state=AttemptSideEffectState.UNKNOWN,
    )
    retry = runtime.start_attempt(second.id)

    assert node.status is NodeRunStatus.READY
    assert repository.get_attempt(attempt.id).result is AttemptResult.INTERRUPTED
    assert repository.get_run(run.id).status is GraphRunStatus.RUNNING
    assert retry.idempotency_key == "safe-unknown"


def test_cancel_keeps_unknown_non_idempotent_effect_in_manual_reconcile() -> None:
    repository = InMemoryGraphRepository()
    runtime = GraphRuntime(repository)
    run = runtime.create_run(
        _sequential_definition(second_idempotency=IdempotencyClass.NON_IDEMPOTENT)
    )
    runtime.start_run(run.id)
    runtime.complete_attempt(
        runtime.start_attempt(_node(repository, run.id, "first").id),
        succeeded=True,
        output_refs={"out": "ready"},
    )
    writer = _node(repository, run.id, "second")
    attempt = runtime.start_attempt(writer.id, idempotency_key="write-once")
    runtime.mark_side_effect_started(attempt.id)

    cancelled = runtime.cancel_run(run.id)

    assert cancelled.status is GraphRunStatus.MANUAL_RECONCILE_REQUIRED
    assert repository.get_node_run(writer.id).status is NodeRunStatus.MANUAL_RECONCILE_REQUIRED
    stored_attempt = repository.get_attempt(attempt.id)
    assert stored_attempt.result is AttemptResult.INTERRUPTED
    assert stored_attempt.side_effect_state is AttemptSideEffectState.UNKNOWN


def test_node_execution_requires_started_graph_run() -> None:
    repository = InMemoryGraphRepository()
    runtime = GraphRuntime(repository)
    run = runtime.create_run(_sequential_definition())

    with pytest.raises(GraphStateError, match="not running"):
        runtime.start_attempt(_node(repository, run.id, "first").id)


def test_recover_promotes_persisted_success_and_recomputes_downstream() -> None:
    repository = InMemoryGraphRepository()
    runtime = GraphRuntime(repository)
    run = runtime.create_run(_sequential_definition())
    runtime.start_run(run.id)
    first = _node(repository, run.id, "first")
    attempt = runtime.start_attempt(first.id)
    repository.update_attempt(
        attempt.model_copy(
            update={
                "result": AttemptResult.SUCCEEDED,
                "result_payload": {"out": "artifact:survived"},
            }
        )
    )

    recovered = runtime.recover(run.id)

    assert recovered.status is GraphRunStatus.RUNNING
    assert repository.get_node_run(first.id).status is NodeRunStatus.SUCCEEDED
    second = _node(repository, run.id, "second")
    assert second.status is NodeRunStatus.READY
    assert second.input_refs == {"in": "artifact:survived"}


def test_unselected_condition_branch_is_skipped_to_fixed_point() -> None:
    port = (PortSpec(name="value", required=False),)
    definition = WorkflowDefinition(
        name="condition-chain",
        nodes=(
            NodeSpec(
                node_id="choice",
                node_kind=NodeKind.CONDITION,
                output_ports=(
                    PortSpec(name="yes", required=False),
                    PortSpec(name="no", required=False),
                ),
            ),
            NodeSpec(
                node_id="yes-step", node_kind=NodeKind.AGENT, input_ports=port, output_ports=port
            ),
            NodeSpec(
                node_id="no-step", node_kind=NodeKind.AGENT, input_ports=port, output_ports=port
            ),
            NodeSpec(
                node_id="no-tail", node_kind=NodeKind.AGENT, input_ports=port, output_ports=port
            ),
            NodeSpec(node_id="join", node_kind=NodeKind.JOIN, input_ports=port),
        ),
        edges=(
            EdgeSpec(
                edge_id="yes",
                source_node="choice",
                source_port="yes",
                target_node="yes-step",
                target_port="value",
            ),
            EdgeSpec(
                edge_id="no",
                source_node="choice",
                source_port="no",
                target_node="no-step",
                target_port="value",
            ),
            EdgeSpec(
                edge_id="no-tail",
                source_node="no-step",
                source_port="value",
                target_node="no-tail",
                target_port="value",
            ),
            EdgeSpec(
                edge_id="yes-join",
                source_node="yes-step",
                source_port="value",
                target_node="join",
                target_port="value",
                join_mode=JoinMode.ANY,
            ),
            EdgeSpec(
                edge_id="no-join",
                source_node="no-tail",
                source_port="value",
                target_node="join",
                target_port="value",
                join_mode=JoinMode.ANY,
            ),
        ),
        status=WorkflowDefinitionStatus.PUBLISHED,
    )
    repository = InMemoryGraphRepository()
    runtime = GraphRuntime(repository)
    run = runtime.create_run(definition)
    runtime.start_run(run.id)

    runtime.select_condition(
        _node(repository, run.id, "choice").id,
        selected_port="yes",
        output_refs={"yes": "chosen"},
    )

    assert _node(repository, run.id, "yes-step").status is NodeRunStatus.READY
    assert _node(repository, run.id, "no-step").status is NodeRunStatus.SKIPPED
    assert _node(repository, run.id, "no-tail").status is NodeRunStatus.SKIPPED
    assert _node(repository, run.id, "join").status is NodeRunStatus.PENDING

    yes_step = _node(repository, run.id, "yes-step")
    runtime.complete_attempt(
        runtime.start_attempt(yes_step.id),
        succeeded=True,
        output_refs={"value": "joined"},
    )
    join = _node(repository, run.id, "join")
    assert join.status is NodeRunStatus.READY
    assert join.input_refs == {"value": "joined"}


def test_all_and_any_joins_use_active_edges_and_populate_inputs() -> None:
    port = (PortSpec(name="value", required=False),)

    def definition(mode: JoinMode) -> WorkflowDefinition:
        return WorkflowDefinition(
            name=f"{mode.value}-join",
            nodes=(
                NodeSpec(node_id="a", node_kind=NodeKind.AGENT, output_ports=port),
                NodeSpec(node_id="b", node_kind=NodeKind.AGENT, output_ports=port),
                NodeSpec(
                    node_id="join",
                    node_kind=NodeKind.JOIN,
                    input_ports=(
                        PortSpec(name="from_a", required=False),
                        PortSpec(name="from_b", required=False),
                    ),
                ),
            ),
            edges=(
                EdgeSpec(
                    edge_id="a-join",
                    source_node="a",
                    source_port="value",
                    target_node="join",
                    target_port="from_a",
                    join_mode=mode,
                ),
                EdgeSpec(
                    edge_id="b-join",
                    source_node="b",
                    source_port="value",
                    target_node="join",
                    target_port="from_b",
                    join_mode=mode,
                ),
            ),
            status=WorkflowDefinitionStatus.PUBLISHED,
        )

    all_repository = InMemoryGraphRepository()
    all_runtime = GraphRuntime(all_repository)
    all_run = all_runtime.create_run(definition(JoinMode.ALL))
    all_runtime.start_run(all_run.id)
    all_runtime.complete_attempt(
        all_runtime.start_attempt(_node(all_repository, all_run.id, "a").id),
        succeeded=True,
        output_refs={"value": "A"},
    )
    assert _node(all_repository, all_run.id, "join").status is NodeRunStatus.PENDING
    all_runtime.complete_attempt(
        all_runtime.start_attempt(_node(all_repository, all_run.id, "b").id),
        succeeded=True,
        output_refs={"value": "B"},
    )
    all_join = _node(all_repository, all_run.id, "join")
    assert all_join.status is NodeRunStatus.READY
    assert all_join.input_refs == {"from_a": "A", "from_b": "B"}

    any_repository = InMemoryGraphRepository()
    any_runtime = GraphRuntime(any_repository)
    any_run = any_runtime.create_run(definition(JoinMode.ANY))
    any_runtime.start_run(any_run.id)
    any_runtime.complete_attempt(
        any_runtime.start_attempt(_node(any_repository, any_run.id, "a").id),
        succeeded=True,
        output_refs={"value": "A"},
    )
    any_join = _node(any_repository, any_run.id, "join")
    assert any_join.status is NodeRunStatus.READY
    assert any_join.input_refs == {"from_a": "A"}


def test_approval_boundary_is_persisted_and_resolution_is_idempotent() -> None:
    definition = WorkflowDefinition(
        name="approval",
        nodes=(
            NodeSpec(
                node_id="approval",
                node_kind=NodeKind.APPROVAL,
                output_ports=(PortSpec(name="out", required=False),),
                timeout_policy=TimeoutPolicy(timeout_seconds=60, on_timeout_node_id="timeout"),
            ),
            NodeSpec(
                node_id="timeout",
                node_kind=NodeKind.ARTIFACT,
                input_ports=(PortSpec(name="in", required=False),),
            ),
        ),
        edges=(
            EdgeSpec(
                edge_id="timeout-edge",
                source_node="approval",
                source_port="out",
                target_node="timeout",
                target_port="in",
                condition="timeout",
            ),
        ),
        status=WorkflowDefinitionStatus.PUBLISHED,
    )
    repository = InMemoryGraphRepository()
    runtime = GraphRuntime(repository)
    run = runtime.create_run(definition)
    runtime.start_run(run.id)
    waiting = runtime.enter_boundary(
        _node(repository, run.id, "approval").id, BoundaryKind.APPROVAL
    )
    assert repository.get_run(run.id).status is GraphRunStatus.WAITING_APPROVAL
    assert waiting.wait_token is not None

    resolution = BoundaryResolution(wait_token=waiting.wait_token, payload={"approved": True})
    decided = runtime.resolve_boundary(waiting.id, resolution)
    assert runtime.resolve_boundary(waiting.id, resolution) == decided
    with pytest.raises(GraphConflictError):
        runtime.resolve_boundary(
            waiting.id,
            BoundaryResolution(wait_token="wait_stale", payload={"approved": True}),
        )


def test_waiting_parallel_branch_does_not_freeze_runnable_or_running_sibling() -> None:
    definition = WorkflowDefinition(
        name="parallel-input",
        nodes=(
            NodeSpec(
                node_id="input",
                node_kind=NodeKind.HUMAN_INPUT,
                output_ports=(PortSpec(name="out", required=False),),
                timeout_policy=TimeoutPolicy(timeout_seconds=60, on_timeout_node_id="timeout"),
            ),
            NodeSpec(node_id="worker", node_kind=NodeKind.AGENT),
            NodeSpec(
                node_id="timeout",
                node_kind=NodeKind.ARTIFACT,
                input_ports=(PortSpec(name="in", required=False),),
            ),
        ),
        edges=(
            EdgeSpec(
                edge_id="timeout-edge",
                source_node="input",
                source_port="out",
                target_node="timeout",
                target_port="in",
                condition="timeout == true",
            ),
        ),
        status=WorkflowDefinitionStatus.PUBLISHED,
    )
    repository = InMemoryGraphRepository()
    runtime = GraphRuntime(repository)
    run = runtime.create_run(definition)
    runtime.start_run(run.id)

    waiting = runtime.enter_boundary(
        _node(repository, run.id, "input").id, BoundaryKind.HUMAN_INPUT
    )
    assert repository.get_run(run.id).status is GraphRunStatus.RUNNING
    worker_attempt = runtime.start_attempt(_node(repository, run.id, "worker").id)
    runtime.complete_attempt(worker_attempt, succeeded=True)
    assert repository.get_run(run.id).status is GraphRunStatus.WAITING_INPUT

    assert waiting.wait_token is not None
    runtime.resolve_boundary(
        waiting.id,
        BoundaryResolution(wait_token=waiting.wait_token, payload={"value": "continue"}),
    )
    assert repository.get_run(run.id).status is GraphRunStatus.COMPLETED


def test_budget_failure_cancels_waiting_nodes_and_late_input_cannot_revive_run() -> None:
    definition = WorkflowDefinition(
        name="budget-wait",
        nodes=(
            NodeSpec(
                node_id="input",
                node_kind=NodeKind.HUMAN_INPUT,
                output_ports=(PortSpec(name="out", required=False),),
                timeout_policy=TimeoutPolicy(timeout_seconds=60, on_timeout_node_id="timeout"),
            ),
            NodeSpec(
                node_id="timeout",
                node_kind=NodeKind.ARTIFACT,
                input_ports=(PortSpec(name="in", required=False),),
            ),
        ),
        edges=(
            EdgeSpec(
                edge_id="timeout-edge",
                source_node="input",
                source_port="out",
                target_node="timeout",
                target_port="in",
            ),
        ),
        default_budget=Budget(max_output_tokens=1),
        status=WorkflowDefinitionStatus.PUBLISHED,
    )
    repository = InMemoryGraphRepository()
    runtime = GraphRuntime(repository)
    run = runtime.create_run(definition)
    runtime.start_run(run.id)
    waiting = runtime.enter_boundary(
        _node(repository, run.id, "input").id, BoundaryKind.HUMAN_INPUT
    )
    assert waiting.wait_token is not None

    failed = runtime.record_budget(run.id, output_tokens=2)

    assert failed.status is GraphRunStatus.FAILED
    assert repository.get_node_run(waiting.id).status is NodeRunStatus.CANCELLED
    with pytest.raises(GraphStateError, match="not waiting"):
        runtime.resolve_boundary(
            waiting.id,
            BoundaryResolution(wait_token=waiting.wait_token, payload={"value": "late"}),
        )
    assert repository.get_run(run.id).status is GraphRunStatus.FAILED


def test_budget_failure_preserves_unknown_non_idempotent_attempt_boundary() -> None:
    definition = WorkflowDefinition(
        name="budget-writer",
        nodes=(
            NodeSpec(
                node_id="writer",
                node_kind=NodeKind.AGENT,
                idempotency_class=IdempotencyClass.NON_IDEMPOTENT,
                writes_workspace=True,
            ),
        ),
        default_budget=Budget(max_output_tokens=1),
        status=WorkflowDefinitionStatus.PUBLISHED,
    )
    repository = InMemoryGraphRepository()
    runtime = GraphRuntime(repository)
    run = runtime.create_run(definition)
    runtime.start_run(run.id)
    attempt = runtime.start_attempt(_node(repository, run.id, "writer").id)
    runtime.mark_side_effect_started(attempt.id)

    stopped = runtime.record_budget(run.id, output_tokens=2)

    assert stopped.status is GraphRunStatus.MANUAL_RECONCILE_REQUIRED
    assert _node(repository, run.id, "writer").status is NodeRunStatus.MANUAL_RECONCILE_REQUIRED
    stored_attempt = repository.get_attempt(attempt.id)
    assert stored_attempt.result is AttemptResult.INTERRUPTED
    assert stored_attempt.side_effect_state is AttemptSideEffectState.UNKNOWN


def test_boundary_timeout_follows_compiled_persistent_branch() -> None:
    definition = WorkflowDefinition(
        name="human-input-timeout",
        nodes=(
            NodeSpec(
                node_id="input",
                node_kind=NodeKind.HUMAN_INPUT,
                output_ports=(PortSpec(name="out", required=False),),
                timeout_policy=TimeoutPolicy(timeout_seconds=1, on_timeout_node_id="fallback"),
            ),
            NodeSpec(
                node_id="fallback",
                node_kind=NodeKind.ARTIFACT,
                input_ports=(PortSpec(name="in", required=False),),
            ),
        ),
        edges=(
            EdgeSpec(
                edge_id="fallback",
                source_node="input",
                source_port="out",
                target_node="fallback",
                target_port="in",
                condition="timeout == true",
            ),
        ),
        status=WorkflowDefinitionStatus.PUBLISHED,
    )
    repository = InMemoryGraphRepository()
    runtime = GraphRuntime(repository)
    run = runtime.create_run(definition)
    runtime.start_run(run.id)
    waiting = runtime.enter_boundary(
        _node(repository, run.id, "input").id, BoundaryKind.HUMAN_INPUT
    )
    assert waiting.wait_token is not None
    timed_out = runtime.timeout_boundary(waiting.id, wait_token=waiting.wait_token)
    assert timed_out.status is NodeRunStatus.SKIPPED
    assert _node(repository, run.id, "fallback").status is NodeRunStatus.READY


def test_loop_stops_on_no_progress_and_budget_dimensions() -> None:
    definition = WorkflowDefinition(
        name="loop",
        nodes=(
            NodeSpec(
                node_id="loop",
                node_kind=NodeKind.LOOP,
                output_ports=(PortSpec(name="out", required=False),),
                loop_policy=LoopPolicy(
                    max_iterations=10,
                    max_wall_seconds=5,
                    max_output_tokens=20,
                    max_cost_usd=1,
                    max_subagents=1,
                    max_recursion_depth=1,
                    max_no_progress_repeats=1,
                    exit_expression="done == true",
                    on_limit_node_id="limited",
                ),
            ),
            NodeSpec(
                node_id="limited",
                node_kind=NodeKind.ARTIFACT,
                input_ports=(PortSpec(name="in", required=False),),
            ),
        ),
        edges=(
            EdgeSpec(
                edge_id="limit-path",
                source_node="loop",
                source_port="out",
                target_node="limited",
                target_port="in",
            ),
        ),
        status=WorkflowDefinitionStatus.PUBLISHED,
    )
    repository = InMemoryGraphRepository()
    runtime = GraphRuntime(repository)
    run = runtime.create_run(definition)
    runtime.start_run(run.id)
    loop = _node(repository, run.id, "loop")
    runtime.advance_loop(loop.id, continue_loop=True, progress_signature="same")
    limited = runtime.advance_loop(
        loop.id,
        continue_loop=True,
        progress_signature="same",
        output_tokens=21,
        elapsed_seconds=6,
    )
    assert limited.status is NodeRunStatus.SKIPPED
    assert set(limited.output_refs["limit_reasons"]) == {
        "no_progress",
        "max_wall_seconds",
        "max_output_tokens",
    }
    assert _node(repository, run.id, "limited").status is NodeRunStatus.READY
    limit_attempt = runtime.start_attempt(_node(repository, run.id, "limited").id)
    runtime.complete_attempt(limit_attempt, succeeded=True)
    assert repository.get_run(run.id).status is GraphRunStatus.COMPLETED


def _required_output_loop_runtime() -> tuple[GraphRuntime, InMemoryGraphRepository, str, str]:
    definition = WorkflowDefinition(
        name="required-loop-output",
        nodes=(
            NodeSpec(
                node_id="loop",
                node_kind=NodeKind.LOOP,
                output_ports=(PortSpec(name="out", value_type="string"),),
                loop_policy=LoopPolicy(
                    max_iterations=2,
                    max_wall_seconds=10,
                    max_output_tokens=10,
                    max_cost_usd=1,
                    max_subagents=1,
                    max_recursion_depth=1,
                    exit_expression="true",
                    on_limit_node_id="limited",
                ),
            ),
            NodeSpec(
                node_id="limited",
                node_kind=NodeKind.ARTIFACT,
                input_ports=(PortSpec(name="in", required=False),),
            ),
        ),
        edges=(
            EdgeSpec(
                edge_id="loop-limited",
                source_node="loop",
                source_port="out",
                target_node="limited",
                target_port="in",
            ),
        ),
        status=WorkflowDefinitionStatus.PUBLISHED,
    )
    repository = InMemoryGraphRepository()
    runtime = GraphRuntime(repository)
    run = runtime.create_run(definition)
    runtime.start_run(run.id)
    return runtime, repository, run.id, _node(repository, run.id, "loop").id


def test_loop_requires_declared_output_only_when_it_will_succeed() -> None:
    runtime, repository, run_id, loop_id = _required_output_loop_runtime()
    before_node = repository.get_node_run(loop_id)
    before_run = repository.get_run(run_id)

    with pytest.raises(GraphStateError, match="missing required ports"):
        runtime.advance_loop(
            loop_id,
            continue_loop=False,
            progress_signature="complete-without-output",
        )
    assert repository.get_node_run(loop_id) == before_node
    assert repository.get_run(run_id) == before_run

    intermediate = runtime.advance_loop(
        loop_id,
        continue_loop=True,
        progress_signature="intermediate-without-output",
    )
    assert intermediate.status is NodeRunStatus.READY

    limited = runtime.advance_loop(
        loop_id,
        continue_loop=False,
        progress_signature="limit-without-output",
        elapsed_seconds=11,
    )
    assert limited.status is NodeRunStatus.SKIPPED
    assert "max_wall_seconds" in limited.output_refs["limit_reasons"]


@pytest.mark.parametrize(
    ("usage", "value"),
    (
        ("elapsed_seconds", True),
        ("elapsed_seconds", -1),
        ("elapsed_seconds", float("nan")),
        ("elapsed_seconds", float("inf")),
        ("elapsed_seconds", "1"),
        ("cost_usd", True),
        ("cost_usd", -1),
        ("cost_usd", float("nan")),
        ("cost_usd", float("inf")),
        ("cost_usd", "1"),
        ("output_tokens", True),
        ("output_tokens", -1),
        ("output_tokens", 1.5),
        ("output_tokens", float("nan")),
        ("output_tokens", "1"),
        ("subagents", True),
        ("subagents", -1),
        ("subagents", 1.5),
        ("subagents", float("inf")),
        ("subagents", "1"),
        ("recursion_depth", True),
        ("recursion_depth", -1),
        ("recursion_depth", 1.5),
        ("recursion_depth", float("nan")),
        ("recursion_depth", "1"),
    ),
)
def test_loop_usage_rejects_invalid_values_without_state_writes(usage: str, value: object) -> None:
    runtime, repository, run_id, loop_id = _required_output_loop_runtime()
    before_node = repository.get_node_run(loop_id)
    before_run = repository.get_run(run_id)
    kwargs = {
        "continue_loop": True,
        "progress_signature": "invalid-usage",
        "output_refs": {"out": "unused"},
        usage: value,
    }

    with pytest.raises(ValueError, match="finite non-negative numbers"):
        runtime.advance_loop(loop_id, **kwargs)  # type: ignore[arg-type]
    assert repository.get_node_run(loop_id) == before_node
    assert repository.get_run(run_id) == before_run


def test_parallel_node_limit_is_enforced_at_attempt_start() -> None:
    port = (PortSpec(name="value", required=False),)
    definition = WorkflowDefinition(
        name="parallel-limit",
        nodes=(
            NodeSpec(node_id="a", node_kind=NodeKind.AGENT, output_ports=port),
            NodeSpec(node_id="b", node_kind=NodeKind.AGENT, output_ports=port),
        ),
        graph_limits=GraphLimits(max_parallel_nodes=1),
        status=WorkflowDefinitionStatus.PUBLISHED,
    )
    repository = InMemoryGraphRepository()
    runtime = GraphRuntime(repository)
    run = runtime.create_run(definition)
    runtime.start_run(run.id)

    runtime.start_attempt(_node(repository, run.id, "a").id)
    with pytest.raises(GraphStateError, match="max_parallel_nodes"):
        runtime.start_attempt(_node(repository, run.id, "b").id)


def test_recover_rebuilds_downstream_after_success_commit_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = InMemoryGraphRepository()
    runtime = GraphRuntime(repository)
    run = runtime.create_run(_sequential_definition())
    runtime.start_run(run.id)
    first_attempt = runtime.start_attempt(_node(repository, run.id, "first").id)
    original = runtime._propagate_dependencies

    def crash_after_commit(run_id: str, *, exclude_loop_back: bool = True) -> None:
        del run_id, exclude_loop_back
        raise RuntimeError("coordinator crashed after attempt commit")

    monkeypatch.setattr(runtime, "_propagate_dependencies", crash_after_commit)
    with pytest.raises(RuntimeError, match="after attempt commit"):
        runtime.complete_attempt(
            first_attempt,
            succeeded=True,
            output_refs={"out": "artifact:committed"},
        )

    assert _node(repository, run.id, "first").status is NodeRunStatus.SUCCEEDED
    assert _node(repository, run.id, "second").status is NodeRunStatus.PENDING
    monkeypatch.setattr(runtime, "_propagate_dependencies", original)

    recovered = runtime.recover(run.id)

    assert recovered.status is GraphRunStatus.RUNNING
    assert _node(repository, run.id, "second").status is NodeRunStatus.READY
    assert _node(repository, run.id, "second").input_refs == {"in": "artifact:committed"}


def test_coding_workflow_mapping_preserves_phase1e_constraints() -> None:
    definition = coding_workflow_definition(
        planner_role_id="planner",
        explorer_role_ids=("explorer-1", "explorer-2"),
        coder_role_id="coder",
        reviewer_role_id="reviewer",
        main_role_id="main",
        max_parallel_explorers=2,
        max_rework_rounds=1,
    )
    compiled = GraphCompiler().compile(definition)
    nodes = {node.node_id: node for node in definition.nodes}
    assert definition.workflow_id == "builtin.coding-review"
    assert nodes["coder"].writes_workspace
    assert nodes["coder"].idempotency_class is IdempotencyClass.NON_IDEMPOTENT
    assert nodes["reviewer"].metadata["verdicts"] == ["approved", "rework", "missing"]
    assert nodes["rework_loop"].loop_policy is not None
    assert compiled.entry_node_ids == ("planner",)


def test_entry_required_inputs_are_present_and_match_port_types() -> None:
    definition = WorkflowDefinition(
        name="typed-entry",
        nodes=(
            NodeSpec(
                node_id="entry",
                node_kind=NodeKind.AGENT,
                input_ports=(PortSpec(name="count", value_type="integer"),),
            ),
        ),
        status=WorkflowDefinitionStatus.PUBLISHED,
    )
    runtime = GraphRuntime(InMemoryGraphRepository())

    with pytest.raises(GraphStateError, match="missing required ports"):
        runtime.create_run(definition)
    with pytest.raises(GraphStateError, match="does not match value type"):
        runtime.create_run(definition, input={"count": True})

    run = runtime.create_run(definition, input={"count": 3})
    assert run.input == {"count": 3}


def test_successful_attempt_requires_typed_outputs_before_commit() -> None:
    definition = WorkflowDefinition(
        name="typed-output",
        nodes=(
            NodeSpec(
                node_id="producer",
                node_kind=NodeKind.AGENT,
                output_ports=(PortSpec(name="count", value_type="integer"),),
            ),
        ),
        status=WorkflowDefinitionStatus.PUBLISHED,
    )
    repository = InMemoryGraphRepository()
    runtime = GraphRuntime(repository)
    run = runtime.create_run(definition)
    runtime.start_run(run.id)
    attempt = runtime.start_attempt(_node(repository, run.id, "producer").id)

    with pytest.raises(GraphStateError, match="missing required ports"):
        runtime.complete_attempt(attempt, succeeded=True)
    with pytest.raises(GraphStateError, match="does not match value type"):
        runtime.complete_attempt(attempt, succeeded=True, output_refs={"count": True})

    completed = runtime.complete_attempt(attempt, succeeded=True, output_refs={"count": 2})
    assert completed.status is NodeRunStatus.SUCCEEDED


def test_missing_optional_source_port_does_not_deliver_none_downstream() -> None:
    definition = WorkflowDefinition(
        name="missing-optional-source",
        nodes=(
            NodeSpec(
                node_id="source",
                node_kind=NodeKind.AGENT,
                output_ports=(PortSpec(name="maybe", value_type="string", required=False),),
            ),
            NodeSpec(
                node_id="target",
                node_kind=NodeKind.AGENT,
                input_ports=(PortSpec(name="value", value_type="string"),),
            ),
        ),
        edges=(
            EdgeSpec(
                edge_id="source-target",
                source_node="source",
                source_port="maybe",
                target_node="target",
                target_port="value",
            ),
        ),
        status=WorkflowDefinitionStatus.PUBLISHED,
    )
    repository = InMemoryGraphRepository()
    runtime = GraphRuntime(repository)
    run = runtime.create_run(definition)
    runtime.start_run(run.id)
    runtime.complete_attempt(
        runtime.start_attempt(_node(repository, run.id, "source").id), succeeded=True
    )

    target = _node(repository, run.id, "target")
    assert target.status is NodeRunStatus.SKIPPED
    assert target.input_refs == {}


def test_condition_dsl_booleans_do_not_rewrite_string_literals() -> None:
    definition = WorkflowDefinition(
        name="literal-boolean-text",
        nodes=(
            NodeSpec(
                node_id="source",
                node_kind=NodeKind.AGENT,
                output_ports=(PortSpec(name="label", value_type="string"),),
            ),
            NodeSpec(
                node_id="target",
                node_kind=NodeKind.AGENT,
                input_ports=(PortSpec(name="label", value_type="string"),),
            ),
        ),
        edges=(
            EdgeSpec(
                edge_id="literal-condition",
                source_node="source",
                source_port="label",
                target_node="target",
                target_port="label",
                condition="label == 'true' and true and 'false' == 'false'",
            ),
        ),
        status=WorkflowDefinitionStatus.PUBLISHED,
    )
    repository = InMemoryGraphRepository()
    runtime = GraphRuntime(repository)
    run = runtime.create_run(definition)
    runtime.start_run(run.id)
    runtime.complete_attempt(
        runtime.start_attempt(_node(repository, run.id, "source").id),
        succeeded=True,
        output_refs={"label": "true"},
    )

    assert _node(repository, run.id, "target").status is NodeRunStatus.READY


@pytest.mark.parametrize(
    ("usage", "value"),
    (
        ("output_tokens", True),
        ("output_tokens", 1.5),
        ("output_tokens", -1),
        ("tool_calls", True),
        ("tool_calls", 1.5),
        ("tool_calls", -1),
        ("cost_usd", True),
        ("cost_usd", -1),
        ("cost_usd", float("nan")),
        ("cost_usd", float("inf")),
    ),
)
def test_record_budget_rejects_invalid_usage(usage: str, value: object) -> None:
    repository = InMemoryGraphRepository()
    runtime = GraphRuntime(repository)
    run = runtime.create_run(_sequential_definition())
    runtime.start_run(run.id)
    kwargs = {"output_tokens": 0, "cost_usd": 0, "tool_calls": 0}
    kwargs[usage] = value

    with pytest.raises(ValueError, match="finite and non-negative"):
        runtime.record_budget(run.id, **kwargs)  # type: ignore[arg-type]


def test_record_budget_rejects_invalid_persisted_usage_from_model_copy() -> None:
    repository = InMemoryGraphRepository()
    runtime = GraphRuntime(repository)
    run = runtime.start_run(runtime.create_run(_sequential_definition()).id)
    repository.update_run(
        run.model_copy(update={"consumed_output_tokens": True, "revision": run.revision + 1}),
        expected_revision=run.revision,
    )

    with pytest.raises(GraphStateError, match="persisted budget usage is invalid"):
        runtime.record_budget(run.id)


def test_compiler_rejects_unknown_port_value_type() -> None:
    definition = WorkflowDefinition(
        name="unknown-port-type",
        nodes=(
            NodeSpec(
                node_id="entry",
                node_kind=NodeKind.AGENT,
                output_ports=(PortSpec(name="unsafe", value_type="python-expression"),),
            ),
        ),
    )

    with pytest.raises(GraphCompilationError) as raised:
        GraphCompiler().compile(definition)
    assert "unsupported_port_type" in {issue.code for issue in raised.value.issues}


def test_any_ports_reject_values_that_cannot_be_persisted_as_finite_json() -> None:
    input_definition = WorkflowDefinition(
        name="json-safe-any-input",
        nodes=(
            NodeSpec(
                node_id="entry",
                node_kind=NodeKind.AGENT,
                input_ports=(PortSpec(name="payload"),),
            ),
        ),
        status=WorkflowDefinitionStatus.PUBLISHED,
    )
    runtime = GraphRuntime(InMemoryGraphRepository())
    recursive: dict[str, object] = {}
    recursive["self"] = recursive

    for value in ({"not-json"}, float("nan"), recursive):
        with pytest.raises(GraphStateError, match="finite JSON"):
            runtime.create_run(input_definition, input={"payload": value})

    output_definition = WorkflowDefinition(
        name="json-safe-any-output",
        nodes=(
            NodeSpec(
                node_id="producer",
                node_kind=NodeKind.AGENT,
                output_ports=(PortSpec(name="payload"),),
            ),
        ),
        status=WorkflowDefinitionStatus.PUBLISHED,
    )
    repository = InMemoryGraphRepository()
    output_runtime = GraphRuntime(repository)
    run = output_runtime.create_run(output_definition)
    output_runtime.start_run(run.id)
    attempt = output_runtime.start_attempt(_node(repository, run.id, "producer").id)

    with pytest.raises(GraphStateError, match="finite JSON"):
        output_runtime.complete_attempt(
            attempt,
            succeeded=True,
            output_refs={"payload": {"not-json"}},
        )
    assert repository.get_attempt(attempt.id).result is AttemptResult.RUNNING


def test_any_to_typed_input_mismatch_fails_before_attempt_creation() -> None:
    definition = WorkflowDefinition(
        name="runtime-input-type-check",
        nodes=(
            NodeSpec(
                node_id="source",
                node_kind=NodeKind.AGENT,
                output_ports=(PortSpec(name="value"),),
            ),
            NodeSpec(
                node_id="target",
                node_kind=NodeKind.TOOL,
                input_ports=(PortSpec(name="value", value_type="integer"),),
                idempotency_class=IdempotencyClass.NON_IDEMPOTENT,
            ),
        ),
        edges=(
            EdgeSpec(
                edge_id="source-target",
                source_node="source",
                source_port="value",
                target_node="target",
                target_port="value",
            ),
        ),
        status=WorkflowDefinitionStatus.PUBLISHED,
    )
    repository = InMemoryGraphRepository()
    runtime = GraphRuntime(repository)
    run = runtime.create_run(definition)
    runtime.start_run(run.id)
    runtime.complete_attempt(
        runtime.start_attempt(_node(repository, run.id, "source").id),
        succeeded=True,
        output_refs={"value": "not-an-integer"},
    )
    target = _node(repository, run.id, "target")

    with pytest.raises(GraphStateError, match="does not match value type"):
        runtime.start_attempt(target.id)
    assert repository.list_attempts(target.id) == ()
    assert repository.get_node_run(target.id).status is NodeRunStatus.READY


def test_multi_edge_aggregation_type_mismatch_fails_before_join_attempt() -> None:
    definition = WorkflowDefinition(
        name="runtime-aggregate-type-check",
        nodes=(
            NodeSpec(
                node_id="left",
                node_kind=NodeKind.AGENT,
                output_ports=(PortSpec(name="value", value_type="string"),),
            ),
            NodeSpec(
                node_id="right",
                node_kind=NodeKind.AGENT,
                output_ports=(PortSpec(name="value", value_type="string"),),
            ),
            NodeSpec(
                node_id="join",
                node_kind=NodeKind.JOIN,
                input_ports=(PortSpec(name="value", value_type="string"),),
            ),
        ),
        edges=(
            EdgeSpec(
                edge_id="left-join",
                source_node="left",
                source_port="value",
                target_node="join",
                target_port="value",
            ),
            EdgeSpec(
                edge_id="right-join",
                source_node="right",
                source_port="value",
                target_node="join",
                target_port="value",
            ),
        ),
        status=WorkflowDefinitionStatus.PUBLISHED,
    )
    repository = InMemoryGraphRepository()
    runtime = GraphRuntime(repository)
    run = runtime.create_run(definition)
    runtime.start_run(run.id)
    for node_id in ("left", "right"):
        runtime.complete_attempt(
            runtime.start_attempt(_node(repository, run.id, node_id).id),
            succeeded=True,
            output_refs={"value": node_id},
        )
    join = _node(repository, run.id, "join")
    assert join.input_refs == {"value": ["left", "right"]}

    with pytest.raises(GraphStateError, match="does not match value type"):
        runtime.start_attempt(join.id)
    assert repository.list_attempts(join.id) == ()


def test_non_attempt_execution_entries_validate_actual_inputs_before_state_changes() -> None:
    def prepared_runtime(
        target: NodeSpec,
        *,
        extra_nodes: tuple[NodeSpec, ...] = (),
        extra_edges: tuple[EdgeSpec, ...] = (),
    ) -> tuple[GraphRuntime, InMemoryGraphRepository, str]:
        definition = WorkflowDefinition(
            name=f"invalid-{target.node_id}-input",
            nodes=(
                NodeSpec(
                    node_id="source",
                    node_kind=NodeKind.AGENT,
                    output_ports=(PortSpec(name="value"),),
                ),
                target,
                *extra_nodes,
            ),
            edges=(
                EdgeSpec(
                    edge_id=f"source-{target.node_id}",
                    source_node="source",
                    source_port="value",
                    target_node=target.node_id,
                    target_port="value",
                ),
                *extra_edges,
            ),
            status=WorkflowDefinitionStatus.PUBLISHED,
        )
        repository = InMemoryGraphRepository()
        runtime = GraphRuntime(repository)
        run = runtime.create_run(definition)
        runtime.start_run(run.id)
        runtime.complete_attempt(
            runtime.start_attempt(_node(repository, run.id, "source").id),
            succeeded=True,
            output_refs={"value": "not-an-integer"},
        )
        return runtime, repository, run.id

    condition_runtime, condition_repository, condition_run_id = prepared_runtime(
        NodeSpec(
            node_id="condition",
            node_kind=NodeKind.CONDITION,
            input_ports=(PortSpec(name="value", value_type="integer"),),
            output_ports=(PortSpec(name="yes", required=False),),
        )
    )
    condition = _node(condition_repository, condition_run_id, "condition")
    with pytest.raises(GraphStateError, match="does not match value type"):
        condition_runtime.select_condition(condition.id, selected_port="yes")
    assert condition_repository.get_node_run(condition.id).status is NodeRunStatus.READY

    boundary_runtime, boundary_repository, boundary_run_id = prepared_runtime(
        NodeSpec(
            node_id="boundary",
            node_kind=NodeKind.WAIT,
            input_ports=(PortSpec(name="value", value_type="integer"),),
        )
    )
    boundary = _node(boundary_repository, boundary_run_id, "boundary")
    with pytest.raises(GraphStateError, match="does not match value type"):
        boundary_runtime.enter_boundary(boundary.id, BoundaryKind.WAIT)
    assert boundary_repository.get_node_run(boundary.id).wait_token is None

    loop_runtime, loop_repository, loop_run_id = prepared_runtime(
        NodeSpec(
            node_id="loop",
            node_kind=NodeKind.LOOP,
            input_ports=(PortSpec(name="value", value_type="integer"),),
            output_ports=(PortSpec(name="out", required=False),),
            loop_policy=LoopPolicy(
                max_iterations=1,
                max_wall_seconds=10,
                max_output_tokens=10,
                max_cost_usd=1,
                max_subagents=0,
                max_recursion_depth=1,
                exit_expression="true",
                on_limit_node_id="limit",
            ),
        ),
        extra_nodes=(
            NodeSpec(
                node_id="limit",
                node_kind=NodeKind.ARTIFACT,
                input_ports=(PortSpec(name="in", required=False),),
            ),
        ),
        extra_edges=(
            EdgeSpec(
                edge_id="loop-limit",
                source_node="loop",
                source_port="out",
                target_node="limit",
                target_port="in",
            ),
        ),
    )
    loop = _node(loop_repository, loop_run_id, "loop")
    with pytest.raises(GraphStateError, match="does not match value type"):
        loop_runtime.advance_loop(
            loop.id,
            continue_loop=False,
            progress_signature="invalid-input",
        )
    assert loop_repository.get_node_run(loop.id).iteration == 0
