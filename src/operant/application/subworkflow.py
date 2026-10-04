"""Pinned subgraph admission and disjoint budget reservations, without a second runtime."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from operant.domain.graph import (
    GraphWorkflowRun,
    NodeKind,
    NodeSpec,
    WorkflowDefinition,
    WorkflowDefinitionStatus,
)
from operant.domain.models import Budget

if TYPE_CHECKING:
    from operant.application.graph import GraphRepository


def loop_multiplier(definition: WorkflowDefinition) -> int:
    result = 1
    for node in definition.nodes:
        if node.loop_policy is not None:
            result *= node.loop_policy.max_iterations
    return result


def reservation_slots(definition: WorkflowDefinition) -> int:
    """A child has a separate reservation for each admitted bounded occurrence."""
    members = sum(
        node.retry_policy.max_attempts
        for node in definition.nodes
        if node.node_kind is NodeKind.AGENT
    )
    children = sum(node.node_kind is NodeKind.SUBWORKFLOW for node in definition.nodes)
    return max(1, (members + children) * loop_multiplier(definition))


def action_allowance(definition: WorkflowDefinition) -> int:
    return sum(
        node.retry_policy.max_attempts
        for node in definition.nodes
        if node.node_kind in {NodeKind.TOOL, NodeKind.SCRIPT}
    ) * loop_multiplier(definition)


def child_budget(
    parent: GraphWorkflowRun,
    definition: WorkflowDefinition,
    node: NodeSpec,
    child: WorkflowDefinition,
) -> Budget:
    slots = reservation_slots(definition)
    limits: dict[str, Any] = parent.budget_snapshot.model_dump()
    limits["max_turns"] = limits["max_turns"] // slots
    if limits["max_turns"] < 1:
        raise ValueError("Graph budget cannot fund the declared subgraph reservations")
    elapsed = (
        0.0
        if parent.started_at is None
        else (datetime.now(timezone.utc) - parent.started_at).total_seconds()
    )
    limits["timeout_seconds"] = int(parent.budget_snapshot.timeout_seconds - elapsed)
    if limits["timeout_seconds"] < 1:
        raise ValueError("parent Graph timeout exhausted before child admission")
    for key in ("max_output_tokens", "max_tool_calls", "max_cost_usd"):
        value = limits[key]
        if value is None:
            continue
        if key == "max_tool_calls":
            value -= action_allowance(definition)
        limits[key] = value / slots if key == "max_cost_usd" else value // slots
        minimum = 0 if key == "max_tool_calls" else 1
        if key == "max_cost_usd":
            if limits[key] <= 0:
                raise ValueError("parent Graph cost reservation is exhausted")
        elif limits[key] < minimum:
            raise ValueError("parent Graph reservation cannot fund the child")
    for cap in (child.default_budget, node.budget):
        if cap is None:
            continue
        for key, value in cap.model_dump().items():
            if value is not None:
                limits[key] = value if limits[key] is None else min(limits[key], value)
    return Budget.model_validate(limits)


def root_run(repository: GraphRepository, run: GraphWorkflowRun) -> GraphWorkflowRun:
    seen: set[str] = set()
    while run.parent_run_id is not None:
        if run.id in seen:
            raise ValueError("persisted subgraph parent cycle")
        seen.add(run.id)
        if len(seen) > 32:
            raise ValueError("persisted subgraph depth exceeds the hard limit")
        run = repository.get_run(run.parent_run_id)
    return run


def validate_subgraph_tree(
    repository: GraphRepository,
    definition: WorkflowDefinition,
) -> None:
    """Reject recursion and grants that exceed the explicitly pinned parent roles."""

    from operant.application.graph import GraphCompiler

    compiler = GraphCompiler()
    visits = 0

    def visit(current: WorkflowDefinition, stack: tuple[tuple[str, int], ...]) -> int:
        nonlocal visits
        visits += 1
        if visits > 10_000:
            raise ValueError("Subworkflow admission exceeds the bounded definition limit")
        compiler.compile(current)
        identity = (current.workflow_id, current.version)
        if identity in stack:
            raise ValueError("recursive Subworkflow is not allowed")
        depth = len(stack) + 1
        if depth > min(32, definition.graph_limits.max_recursion_depth):
            raise ValueError("Subworkflow exceeds the root recursion depth")
        descendants = 0
        for node in current.nodes:
            if node.node_kind is not NodeKind.SUBWORKFLOW:
                continue
            child = repository.get_definition(
                str(node.subworkflow_id), int(node.subworkflow_version or 0)
            )
            if child.status is not WorkflowDefinitionStatus.PUBLISHED:
                raise ValueError("Subworkflow must pin a published definition")
            if any(
                current.locked_role_versions.get(role) != version
                for role, version in child.locked_role_versions.items()
            ):
                raise ValueError("Subworkflow role grant exceeds the parent definition")
            required = set(child.required_capabilities)
            for child_node in child.nodes:
                required.update(child_node.capability_requirements)
            if not required <= set(node.capability_requirements):
                raise ValueError("Subworkflow capability grant exceeds its parent node")
            if not set(child.required_plugins) <= set(current.required_plugins):
                raise ValueError("Subworkflow plugin grant exceeds the parent definition")
            members = sum(item.node_kind is NodeKind.AGENT for item in child.nodes)
            descendants += (members + visit(child, (*stack, identity))) * loop_multiplier(current)
            if descendants > definition.graph_limits.max_subagents:
                raise ValueError("Subworkflow exceeds the root sub-agent limit")
        return descendants

    if visit(definition, ()) > definition.graph_limits.max_subagents:
        raise ValueError("Subworkflow exceeds the root sub-agent limit")


def validate_budget_tree(
    repository: GraphRepository,
    run: GraphWorkflowRun,
    definition: WorkflowDefinition,
) -> None:
    # Recursion is checked separately. This preflight cannot write or call a model.
    if run.budget_snapshot.max_turns < reservation_slots(definition):
        raise ValueError("Graph turn budget cannot fund its declared reservations")
    if (
        run.budget_snapshot.max_tool_calls is not None
        and run.budget_snapshot.max_tool_calls < action_allowance(definition)
    ):
        raise ValueError("Graph action budget cannot fund the bounded actions")
    for node in definition.nodes:
        if node.node_kind is not NodeKind.SUBWORKFLOW:
            continue
        child = repository.get_definition(
            str(node.subworkflow_id), int(node.subworkflow_version or 0)
        )
        reserved = child_budget(run, definition, node, child)
        virtual = GraphWorkflowRun(
            workflow_definition_id=child.workflow_id,
            workflow_definition_version=child.version,
            budget_snapshot=reserved,
        )
        validate_budget_tree(repository, virtual, child)


def aggregate_usage(repository: GraphRepository, run: GraphWorkflowRun) -> dict[str, int | float]:
    usage: dict[str, int | float] = {
        "consumed_output_tokens": run.consumed_output_tokens,
        "consumed_cost_usd": run.consumed_cost_usd,
        "consumed_tool_calls": run.consumed_tool_calls,
    }
    seen = {run.id}
    pending = list(repository.list_child_runs(run.id))
    while pending:
        child = pending.pop()
        if child.id in seen:
            raise ValueError("persisted child graph cycle")
        seen.add(child.id)
        if len(seen) > 10_000:
            raise ValueError("graph family exceeds the bounded projection limit")
        for key in usage:
            usage[key] += getattr(child, key)
        pending.extend(repository.list_child_runs(child.id))
    return usage
