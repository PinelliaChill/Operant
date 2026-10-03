from __future__ import annotations

import pytest

from operant.application.graph import GraphCompilationError, GraphCompiler
from operant.domain.graph import (
    EdgeSpec,
    IdempotencyClass,
    NodeKind,
    NodeSpec,
    PortSpec,
    WorkflowDefinition,
)
from operant.domain.multiwriter import MergeNodePolicy, WriterIsolationKind, WriterNodePolicy


def _writer(key: str) -> NodeSpec:
    return NodeSpec(
        node_id=f"writer-{key}",
        node_kind=NodeKind.AGENT,
        output_ports=(PortSpec(name="out"),),
        writes_workspace=True,
        idempotency_class=IdempotencyClass.IDEMPOTENT,
        writer_policy=WriterNodePolicy(
            writer_key=key,
            isolation_kind=WriterIsolationKind.WORKTREE,
            isolation_ref=f"worktree:{key}",
            ownership_paths=(f"src/{key}",),
        ),
    )


def _definition(
    *, source_keys: tuple[str, ...], writer_keys: tuple[str, ...], connected: tuple[str, ...]
) -> WorkflowDefinition:
    return WorkflowDefinition(
        name="merge source contract",
        nodes=(
            *(_writer(key) for key in writer_keys),
            NodeSpec(
                node_id="merge",
                node_kind=NodeKind.MERGE,
                input_ports=(PortSpec(name="in"),),
                writes_workspace=True,
                idempotency_class=IdempotencyClass.IDEMPOTENT,
                merge_policy=MergeNodePolicy(source_writer_keys=source_keys),
            ),
        ),
        edges=tuple(
            EdgeSpec(
                edge_id=f"{key}-merge",
                source_node=f"writer-{key}",
                source_port="out",
                target_node="merge",
                target_port="in",
            )
            for key in connected
        ),
    )


@pytest.mark.parametrize(
    ("source_keys", "writer_keys", "connected", "expected_issue"),
    [
        (("a", "a"), ("a",), ("a",), "invalid_merge_sources"),
        (("a", "missing"), ("a",), ("a",), "merge_source_writer_missing"),
        (("a", "b"), ("a", "b"), ("a",), "writer_not_connected_to_merge"),
        (("a", "b"), (), (), "merge_source_writer_missing"),
    ],
)
def test_merge_requires_unique_reachable_typed_writer_sources(
    source_keys: tuple[str, ...],
    writer_keys: tuple[str, ...],
    connected: tuple[str, ...],
    expected_issue: str,
) -> None:
    definition = _definition(source_keys=source_keys, writer_keys=writer_keys, connected=connected)
    with pytest.raises(GraphCompilationError) as raised:
        GraphCompiler().compile(definition)
    assert expected_issue in {issue.code for issue in raised.value.issues}


def test_merge_accepts_two_distinct_reachable_typed_writers() -> None:
    definition = _definition(source_keys=("a", "b"), writer_keys=("a", "b"), connected=("a", "b"))
    compiled = GraphCompiler().compile(definition)
    assert compiled.terminal_node_ids == ("merge",)


def test_merge_rejects_ambiguous_typed_writer_key() -> None:
    definition = _definition(source_keys=("a", "b"), writer_keys=("a", "b"), connected=("a", "b"))
    duplicate = _writer("a").model_copy(update={"node_id": "writer-a-copy"})
    extra_edge = EdgeSpec(
        edge_id="a-copy-merge",
        source_node="writer-a-copy",
        source_port="out",
        target_node="merge",
        target_port="in",
    )
    definition = definition.model_copy(
        update={"nodes": (*definition.nodes, duplicate), "edges": (*definition.edges, extra_edge)}
    )
    with pytest.raises(GraphCompilationError) as raised:
        GraphCompiler().compile(definition)
    assert "merge_source_writer_ambiguous" in {issue.code for issue in raised.value.issues}
