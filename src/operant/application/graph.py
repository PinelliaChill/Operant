from __future__ import annotations

import ast
import io
import math
import tokenize
from collections import defaultdict, deque
from collections.abc import Iterable
from threading import RLock
from typing import Any, Protocol
from uuid import uuid4

from operant.domain.graph import (
    AttemptResult,
    AttemptSideEffectState,
    BoundaryKind,
    BoundaryResolution,
    CompilationIssue,
    CompiledWorkflow,
    EdgeSpec,
    FailurePolicy,
    GraphLimits,
    GraphRunStatus,
    GraphWorkflowRun,
    IdempotencyClass,
    JoinMode,
    LoopPolicy,
    NodeAttempt,
    NodeKind,
    NodeRun,
    NodeRunStatus,
    NodeSpec,
    PortSpec,
    WorkflowDefinition,
    WorkflowDefinitionStatus,
    new_graph_id,
    utc_now,
)


class GraphCompilationError(ValueError):
    def __init__(self, issues: Iterable[CompilationIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__("; ".join(issue.message for issue in self.issues))


class GraphConflictError(RuntimeError):
    pass


class GraphStateError(RuntimeError):
    pass


_TERMINAL_GRAPH_STATUSES = frozenset(
    {
        GraphRunStatus.COMPLETED,
        GraphRunStatus.FAILED,
        GraphRunStatus.CANCELLED,
        GraphRunStatus.MANUAL_RECONCILE_REQUIRED,
    }
)
_ACTIVE_NODE_STATUSES = frozenset(
    {NodeRunStatus.READY, NodeRunStatus.RUNNING, NodeRunStatus.RETRY_WAIT}
)
_NONTERMINAL_NODE_STATUSES = frozenset(
    {
        NodeRunStatus.PENDING,
        NodeRunStatus.READY,
        NodeRunStatus.RUNNING,
        NodeRunStatus.WAITING_APPROVAL,
        NodeRunStatus.WAITING_INPUT,
        NodeRunStatus.WAITING,
        NodeRunStatus.RETRY_WAIT,
    }
)

_PORT_TYPE_ALIASES = {
    "any": "any",
    "json": "json",
    "string": "string",
    "str": "string",
    "integer": "integer",
    "int": "integer",
    "number": "number",
    "float": "number",
    "boolean": "boolean",
    "bool": "boolean",
    "object": "object",
    "dict": "object",
    "array": "array",
    "list": "array",
    "null": "null",
    "none": "null",
}


class GraphRepository(Protocol):
    """Persistence boundary. Implementations must commit before returning."""

    def put_definition(self, definition: WorkflowDefinition) -> None: ...

    def get_definition(self, workflow_id: str, version: int) -> WorkflowDefinition: ...

    def create_run(self, run: GraphWorkflowRun, node_runs: tuple[NodeRun, ...]) -> None: ...

    def get_run(self, run_id: str) -> GraphWorkflowRun: ...

    def update_run(self, run: GraphWorkflowRun, *, expected_revision: int) -> None: ...

    def list_node_runs(self, run_id: str) -> tuple[NodeRun, ...]: ...

    def get_node_run(self, node_run_id: str) -> NodeRun: ...

    def update_node_run(self, node_run: NodeRun, *, expected_revision: int) -> None: ...

    def start_node_attempt(
        self, node_run: NodeRun, attempt: NodeAttempt, *, expected_node_revision: int
    ) -> None: ...

    def get_attempt(self, attempt_id: str) -> NodeAttempt: ...

    def finish_node_attempt(
        self, node_run: NodeRun, attempt: NodeAttempt, *, expected_node_revision: int
    ) -> None: ...

    def update_attempt(self, attempt: NodeAttempt) -> None: ...

    def list_attempts(self, node_run_id: str) -> tuple[NodeAttempt, ...]: ...


class InMemoryGraphRepository:
    """Reference repository for deterministic tests; not a persistence claim."""

    def __init__(self) -> None:
        self._definitions: dict[tuple[str, int], WorkflowDefinition] = {}
        self._runs: dict[str, GraphWorkflowRun] = {}
        self._node_runs: dict[str, NodeRun] = {}
        self._attempts: dict[str, NodeAttempt] = {}
        self._lock = RLock()

    def put_definition(self, definition: WorkflowDefinition) -> None:
        key = (definition.workflow_id, definition.version)
        with self._lock:
            existing = self._definitions.get(key)
            if existing is not None and existing != definition:
                raise GraphConflictError("published definition revisions are immutable")
            self._definitions[key] = definition

    def get_definition(self, workflow_id: str, version: int) -> WorkflowDefinition:
        return self._definitions[(workflow_id, version)]

    def create_run(self, run: GraphWorkflowRun, node_runs: tuple[NodeRun, ...]) -> None:
        with self._lock:
            if run.id in self._runs:
                raise GraphConflictError(f"run {run.id!r} already exists")
            if any(node.id in self._node_runs for node in node_runs):
                raise GraphConflictError("node run already exists")
            self._runs[run.id] = run
            self._node_runs.update({node.id: node for node in node_runs})

    def get_run(self, run_id: str) -> GraphWorkflowRun:
        return self._runs[run_id]

    def update_run(self, run: GraphWorkflowRun, *, expected_revision: int) -> None:
        with self._lock:
            current = self._runs[run.id]
            if current.revision != expected_revision or run.revision != expected_revision + 1:
                raise GraphConflictError("workflow run revision conflict")
            self._runs[run.id] = run

    def list_node_runs(self, run_id: str) -> tuple[NodeRun, ...]:
        return tuple(node for node in self._node_runs.values() if node.workflow_run_id == run_id)

    def get_node_run(self, node_run_id: str) -> NodeRun:
        return self._node_runs[node_run_id]

    def update_node_run(self, node_run: NodeRun, *, expected_revision: int) -> None:
        with self._lock:
            current = self._node_runs[node_run.id]
            if current.revision != expected_revision or node_run.revision != expected_revision + 1:
                raise GraphConflictError("node run revision conflict")
            self._node_runs[node_run.id] = node_run

    def start_node_attempt(
        self, node_run: NodeRun, attempt: NodeAttempt, *, expected_node_revision: int
    ) -> None:
        with self._lock:
            current = self._node_runs[node_run.id]
            if (
                attempt.id in self._attempts
                or current.revision != expected_node_revision
                or node_run.revision != expected_node_revision + 1
            ):
                raise GraphConflictError("node attempt start conflict")
            self._attempts[attempt.id] = attempt
            self._node_runs[node_run.id] = node_run

    def get_attempt(self, attempt_id: str) -> NodeAttempt:
        return self._attempts[attempt_id]

    def update_attempt(self, attempt: NodeAttempt) -> None:
        with self._lock:
            current = self._attempts[attempt.id]
            if current.result is not AttemptResult.RUNNING:
                raise GraphConflictError("terminal attempts are immutable")
            self._attempts[attempt.id] = attempt

    def finish_node_attempt(
        self, node_run: NodeRun, attempt: NodeAttempt, *, expected_node_revision: int
    ) -> None:
        with self._lock:
            current_node = self._node_runs[node_run.id]
            current_attempt = self._attempts[attempt.id]
            if (
                current_node.revision != expected_node_revision
                or node_run.revision != expected_node_revision + 1
                or current_attempt.result is not AttemptResult.RUNNING
            ):
                raise GraphConflictError("node attempt finish conflict")
            self._attempts[attempt.id] = attempt
            self._node_runs[node_run.id] = node_run

    def list_attempts(self, node_run_id: str) -> tuple[NodeAttempt, ...]:
        attempts = [a for a in self._attempts.values() if a.node_run_id == node_run_id]
        return tuple(sorted(attempts, key=lambda item: item.attempt_number))


class GraphCompiler:
    def compile(self, definition: WorkflowDefinition) -> CompiledWorkflow:
        issues: list[CompilationIssue] = []
        nodes: dict[str, NodeSpec] = {}
        edges: dict[str, EdgeSpec] = {}
        for node in definition.nodes:
            if node.node_id in nodes:
                issues.append(
                    CompilationIssue(
                        code="duplicate_node",
                        message=f"duplicate node {node.node_id}",
                        node_id=node.node_id,
                    )
                )
            nodes[node.node_id] = node
        for edge in definition.edges:
            if edge.edge_id in edges:
                issues.append(
                    CompilationIssue(
                        code="duplicate_edge",
                        message=f"duplicate edge {edge.edge_id}",
                        edge_id=edge.edge_id,
                    )
                )
            edges[edge.edge_id] = edge
        if not nodes:
            issues.append(
                CompilationIssue(code="empty_graph", message="workflow must contain a node")
            )
        if len(nodes) > definition.graph_limits.max_nodes:
            issues.append(CompilationIssue(code="node_limit", message="workflow exceeds max_nodes"))
        if len(edges) > definition.graph_limits.max_edges:
            issues.append(CompilationIssue(code="edge_limit", message="workflow exceeds max_edges"))
        if definition.required_plugins:
            issues.append(
                CompilationIssue(
                    code="plugins_out_of_scope",
                    message="Phase 2 graph definitions cannot require plugins",
                )
            )

        incoming: dict[str, list[EdgeSpec]] = defaultdict(list)
        outgoing: dict[str, list[EdgeSpec]] = defaultdict(list)
        for edge in edges.values():
            source = nodes.get(edge.source_node)
            target = nodes.get(edge.target_node)
            if source is None:
                issues.append(
                    CompilationIssue(
                        code="missing_source",
                        message=f"edge {edge.edge_id} source does not exist",
                        edge_id=edge.edge_id,
                    )
                )
            if target is None:
                issues.append(
                    CompilationIssue(
                        code="missing_target",
                        message=f"edge {edge.edge_id} target does not exist",
                        edge_id=edge.edge_id,
                    )
                )
            if source is None or target is None:
                continue
            source_ports = {port.name: port for port in source.output_ports}
            target_ports = {port.name: port for port in target.input_ports}
            if edge.source_port not in source_ports:
                issues.append(
                    CompilationIssue(
                        code="missing_source_port",
                        message=f"edge {edge.edge_id} source port does not exist",
                        edge_id=edge.edge_id,
                    )
                )
            if edge.target_port not in target_ports:
                issues.append(
                    CompilationIssue(
                        code="missing_target_port",
                        message=f"edge {edge.edge_id} target port does not exist",
                        edge_id=edge.edge_id,
                    )
                )
            if edge.source_port in source_ports and edge.target_port in target_ports:
                source_type = _PORT_TYPE_ALIASES.get(
                    source_ports[edge.source_port].value_type.lower()
                )
                target_type = _PORT_TYPE_ALIASES.get(
                    target_ports[edge.target_port].value_type.lower()
                )
                compatible = (
                    source_type is None
                    or target_type is None
                    or "any" in {source_type, target_type}
                    or source_type == target_type
                    or (source_type == "integer" and target_type in {"number", "json"})
                    or target_type == "json"
                )
                if not compatible:
                    issues.append(
                        CompilationIssue(
                            code="port_type_mismatch",
                            message=f"edge {edge.edge_id} has incompatible port types",
                            edge_id=edge.edge_id,
                        )
                    )
            if edge.loop_back and source.node_kind is not NodeKind.LOOP:
                issues.append(
                    CompilationIssue(
                        code="invalid_loop_back",
                        message=f"edge {edge.edge_id} loop_back must originate at a LoopNode",
                        edge_id=edge.edge_id,
                    )
                )
            if edge.condition is not None and not _is_safe_expression(edge.condition):
                issues.append(
                    CompilationIssue(
                        code="invalid_condition",
                        message=f"edge {edge.edge_id} uses an invalid condition expression",
                        edge_id=edge.edge_id,
                    )
                )
            incoming[target.node_id].append(edge)
            outgoing[source.node_id].append(edge)

        for node in nodes.values():
            for port in (*node.input_ports, *node.output_ports):
                if port.value_type.lower() not in _PORT_TYPE_ALIASES:
                    issues.append(
                        CompilationIssue(
                            code="unsupported_port_type",
                            message=(
                                f"node {node.node_id} port {port.name} uses unsupported "
                                f"value type {port.value_type!r}"
                            ),
                            node_id=node.node_id,
                        )
                    )
            if node.node_kind is NodeKind.TIMER:
                issues.append(
                    CompilationIssue(
                        code="scheduler_out_of_scope",
                        message=(
                            f"timer node {node.node_id} cannot run until Scheduler is implemented"
                        ),
                        node_id=node.node_id,
                    )
                )
            delivered = {edge.target_port for edge in incoming[node.node_id] if not edge.loop_back}
            missing = [
                port.name
                for port in node.input_ports
                if port.required and port.name not in delivered
            ]
            if incoming[node.node_id] and missing:
                issues.append(
                    CompilationIssue(
                        code="missing_required_input",
                        message=f"node {node.node_id} has unbound required inputs: {missing}",
                        node_id=node.node_id,
                    )
                )
            if node.writes_workspace and node.idempotency_class is IdempotencyClass.PURE:
                issues.append(
                    CompilationIssue(
                        code="writer_marked_pure",
                        message=f"writer node {node.node_id} cannot be pure",
                        node_id=node.node_id,
                    )
                )
            if (
                node.retry_policy.max_attempts > 1
                and node.idempotency_class is IdempotencyClass.NON_IDEMPOTENT
            ):
                issues.append(
                    CompilationIssue(
                        code="unsafe_retry",
                        message=f"node {node.node_id} retries a non-idempotent side effect",
                        node_id=node.node_id,
                    )
                )
            if node.node_kind in {NodeKind.APPROVAL, NodeKind.HUMAN_INPUT}:
                timeout = node.timeout_policy
                if timeout is None or timeout.on_timeout_node_id is None:
                    issues.append(
                        CompilationIssue(
                            code="missing_timeout_branch",
                            message=f"node {node.node_id} requires a timeout branch",
                            node_id=node.node_id,
                        )
                    )
                elif timeout.on_timeout_node_id not in nodes:
                    issues.append(
                        CompilationIssue(
                            code="missing_timeout_target",
                            message=f"node {node.node_id} timeout target does not exist",
                            node_id=node.node_id,
                        )
                    )
                elif not any(
                    edge.target_node == timeout.on_timeout_node_id
                    for edge in outgoing[node.node_id]
                ):
                    issues.append(
                        CompilationIssue(
                            code="missing_timeout_edge",
                            message=(
                                f"node {node.node_id} timeout target "
                                f"{timeout.on_timeout_node_id} requires an outgoing edge"
                            ),
                            node_id=node.node_id,
                        )
                    )
            if node.node_kind is NodeKind.LOOP and node.loop_policy is not None:
                if not _is_safe_expression(node.loop_policy.exit_expression):
                    issues.append(
                        CompilationIssue(
                            code="invalid_loop_exit",
                            message=f"loop {node.node_id} has an invalid exit expression",
                            node_id=node.node_id,
                        )
                    )
                for target_id in (
                    node.loop_policy.on_limit_node_id,
                    node.loop_policy.on_manual_reconcile_node_id,
                ):
                    if target_id is not None and target_id not in nodes:
                        issues.append(
                            CompilationIssue(
                                code="missing_loop_branch",
                                message=(
                                    f"loop {node.node_id} branch target {target_id} does not exist"
                                ),
                                node_id=node.node_id,
                            )
                        )
                    elif target_id is not None and not any(
                        edge.target_node == target_id and not edge.loop_back
                        for edge in outgoing[node.node_id]
                    ):
                        issues.append(
                            CompilationIssue(
                                code="missing_loop_branch_edge",
                                message=(
                                    f"loop {node.node_id} branch target {target_id} "
                                    "requires an outgoing edge"
                                ),
                                node_id=node.node_id,
                            )
                        )
                if (
                    node.loop_policy.max_subagents > definition.graph_limits.max_subagents
                    or node.loop_policy.max_recursion_depth
                    > definition.graph_limits.max_recursion_depth
                ):
                    issues.append(
                        CompilationIssue(
                            code="loop_limit_escalation",
                            message=f"loop {node.node_id} exceeds graph limits",
                            node_id=node.node_id,
                        )
                    )
            if node.node_kind is NodeKind.SUBWORKFLOW:
                requested = set(node.metadata.get("required_capabilities", ()))
                granted = set(node.capability_requirements)
                if not requested.issubset(granted):
                    issues.append(
                        CompilationIssue(
                            code="subworkflow_permission_escalation",
                            message=f"subworkflow {node.node_id} exceeds its capability grant",
                            node_id=node.node_id,
                        )
                    )
            if (
                node.model_override is not None
                and node.model_override not in definition.locked_provider_versions
            ):
                issues.append(
                    CompilationIssue(
                        code="unlocked_provider_version",
                        message=f"node {node.node_id} model override is not version locked",
                        node_id=node.node_id,
                    )
                )

        for target_id, target_edges in incoming.items():
            join_modes = {edge.join_mode for edge in target_edges if not edge.loop_back}
            if len(join_modes) > 1:
                issues.append(
                    CompilationIssue(
                        code="mixed_join_mode",
                        message=f"node {target_id} mixes ALL and ANY incoming edges",
                        node_id=target_id,
                    )
                )

        writers = [node.node_id for node in nodes.values() if node.writes_workspace]
        if len(writers) > 1:
            issues.append(
                CompilationIssue(
                    code="multiple_writers",
                    message=f"only one workspace Writer is allowed, found {writers}",
                )
            )

        indegree = {node_id: 0 for node_id in nodes}
        for edge in edges.values():
            if not edge.loop_back and edge.source_node in nodes and edge.target_node in nodes:
                indegree[edge.target_node] += 1
        entries = tuple(sorted(node_id for node_id, degree in indegree.items() if degree == 0))
        queue = deque(entries)
        order: list[str] = []
        mutable_indegree = dict(indegree)
        while queue:
            node_id = queue.popleft()
            order.append(node_id)
            for edge in outgoing[node_id]:
                if edge.loop_back:
                    continue
                mutable_indegree[edge.target_node] -= 1
                if mutable_indegree[edge.target_node] == 0:
                    queue.append(edge.target_node)
        if len(order) != len(nodes):
            issues.append(
                CompilationIssue(
                    code="illegal_cycle", message="cycles must be explicit bounded loop_back edges"
                )
            )
        if entries:
            reachable: set[str] = set()
            pending = list(entries)
            while pending:
                node_id = pending.pop()
                if node_id in reachable:
                    continue
                reachable.add(node_id)
                pending.extend(edge.target_node for edge in outgoing[node_id])
            for node_id in sorted(set(nodes).difference(reachable)):
                issues.append(
                    CompilationIssue(
                        code="unreachable_node",
                        message=f"node {node_id} is unreachable",
                        node_id=node_id,
                    )
                )
        if issues:
            raise GraphCompilationError(issues)
        return CompiledWorkflow(
            definition=definition,
            entry_node_ids=entries,
            topological_node_ids=tuple(order),
            terminal_node_ids=tuple(sorted(node_id for node_id in nodes if not outgoing[node_id])),
            incoming_edge_ids={
                node_id: tuple(edge.edge_id for edge in incoming[node_id]) for node_id in nodes
            },
            outgoing_edge_ids={
                node_id: tuple(edge.edge_id for edge in outgoing[node_id]) for node_id in nodes
            },
        )


_SAFE_EXPRESSION_NODES = (
    ast.Expression,
    ast.BoolOp,
    ast.UnaryOp,
    ast.Compare,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.Subscript,
    ast.And,
    ast.Or,
    ast.Not,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
)


def _is_safe_expression(expression: str) -> bool:
    """Finite condition DSL: names, literals, lookups and boolean comparisons only."""
    try:
        parsed = ast.parse(_normalize_expression(expression), mode="eval")
    except (SyntaxError, tokenize.TokenError):
        return False
    return all(isinstance(node, _SAFE_EXPRESSION_NODES) for node in ast.walk(parsed))


def _normalize_expression(expression: str) -> str:
    """Translate DSL booleans without touching quoted string contents."""

    tokens = tokenize.generate_tokens(io.StringIO(expression).readline)
    normalized: list[tokenize.TokenInfo] = []
    for token in tokens:
        replacement = {"true": "True", "false": "False"}.get(token.string)
        if token.type == tokenize.NAME and replacement is not None:
            token = tokenize.TokenInfo(
                token.type,
                replacement,
                token.start,
                token.end,
                token.line,
            )
        normalized.append(token)
    return tokenize.untokenize(normalized)


def _is_json_value(value: Any, *, seen: set[int] | None = None, remaining_depth: int = 64) -> bool:
    if remaining_depth < 0:
        return False
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if not isinstance(value, (list, dict)):
        return False
    seen = set() if seen is None else seen
    identity = id(value)
    if identity in seen:
        return False
    seen.add(identity)
    try:
        if isinstance(value, list):
            return all(
                _is_json_value(item, seen=seen, remaining_depth=remaining_depth - 1)
                for item in value
            )
        return all(
            isinstance(key, str)
            and _is_json_value(item, seen=seen, remaining_depth=remaining_depth - 1)
            for key, item in value.items()
        )
    finally:
        seen.remove(identity)


def _matches_port_type(value: Any, value_type: str) -> bool:
    canonical = _PORT_TYPE_ALIASES.get(value_type.lower())
    if canonical in {"any", "json"}:
        return _is_json_value(value)
    if canonical == "string":
        return isinstance(value, str)
    if canonical == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if canonical == "number":
        return (
            isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        )
    if canonical == "boolean":
        return isinstance(value, bool)
    if canonical == "object":
        return isinstance(value, dict) and _is_json_value(value)
    if canonical == "array":
        return isinstance(value, list) and _is_json_value(value)
    if canonical == "null":
        return value is None
    return False


def _validate_port_values(
    ports: tuple[PortSpec, ...],
    values: dict[str, Any],
    *,
    context: str,
    require_required: bool = True,
) -> None:
    if not _is_json_value(values):
        raise GraphStateError(f"{context} must contain only finite JSON values")
    missing = [
        port.name
        for port in ports
        if require_required and port.required and port.name not in values
    ]
    if missing:
        raise GraphStateError(f"{context} is missing required ports: {missing}")
    for port in ports:
        if port.name in values and not _matches_port_type(values[port.name], port.value_type):
            raise GraphStateError(
                f"{context} port {port.name!r} does not match value type {port.value_type!r}"
            )


def _expression_value(node: ast.AST, values: dict[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _expression_value(node.body, values)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return values[node.id]
    if isinstance(node, ast.Subscript):
        container = _expression_value(node.value, values)
        key = _expression_value(node.slice, values)
        return container[key]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not bool(_expression_value(node.operand, values))
    if isinstance(node, ast.BoolOp):
        operands = [_expression_value(value, values) for value in node.values]
        if isinstance(node.op, ast.And):
            return all(bool(value) for value in operands)
        if isinstance(node.op, ast.Or):
            return any(bool(value) for value in operands)
    if isinstance(node, ast.Compare):
        left = _expression_value(node.left, values)
        for operator, comparator in zip(node.ops, node.comparators, strict=True):
            right = _expression_value(comparator, values)
            if isinstance(operator, ast.Eq):
                matched = left == right
            elif isinstance(operator, ast.NotEq):
                matched = left != right
            elif isinstance(operator, ast.Lt):
                matched = left < right
            elif isinstance(operator, ast.LtE):
                matched = left <= right
            elif isinstance(operator, ast.Gt):
                matched = left > right
            elif isinstance(operator, ast.GtE):
                matched = left >= right
            elif isinstance(operator, ast.In):
                matched = left in right
            elif isinstance(operator, ast.NotIn):
                matched = left not in right
            else:  # pragma: no cover - guarded by _SAFE_EXPRESSION_NODES
                raise ValueError("unsupported graph condition operator")
            if not matched:
                return False
            left = right
        return True
    raise ValueError("unsupported graph condition expression")


def _evaluate_condition(expression: str, values: dict[str, Any]) -> bool:
    """Evaluate a compiler-validated expression against persisted source outputs."""
    try:
        parsed = ast.parse(_normalize_expression(expression), mode="eval")
        if not all(isinstance(node, _SAFE_EXPRESSION_NODES) for node in ast.walk(parsed)):
            return False
        return bool(_expression_value(parsed, values))
    except (KeyError, SyntaxError, TypeError, ValueError, tokenize.TokenError):
        return False


class GraphRuntime:
    """Deterministic coordinator; Agents never mutate graph state directly."""

    def __init__(self, repository: GraphRepository, compiler: GraphCompiler | None = None) -> None:
        self.repository = repository
        self.compiler = compiler or GraphCompiler()

    def create_run(
        self,
        definition: WorkflowDefinition,
        *,
        input: dict[str, Any] | None = None,
        workspace_or_target: str | None = None,
        legacy_workflow_run_id: str | None = None,
        run_id: str | None = None,
    ) -> GraphWorkflowRun:
        compiled = self.validate_run(definition, input=input)
        run_input = input or {}
        run = GraphWorkflowRun(
            id=run_id or new_graph_id("graph_run"),
            workflow_definition_id=definition.workflow_id,
            workflow_definition_version=definition.version,
            input=run_input,
            workspace_or_target=workspace_or_target,
            legacy_workflow_run_id=legacy_workflow_run_id,
            budget_snapshot=definition.default_budget,
            policy_snapshot=definition.default_policy,
        )
        self.repository.put_definition(definition)
        entries = set(compiled.entry_node_ids)
        node_runs = tuple(
            NodeRun(
                workflow_run_id=run.id,
                node_id=node.node_id,
                status=NodeRunStatus.READY if node.node_id in entries else NodeRunStatus.PENDING,
                input_refs=run.input if node.node_id in entries else {},
            )
            for node in definition.nodes
        )
        self.repository.create_run(run, node_runs)
        return run

    def validate_run(
        self, definition: WorkflowDefinition, *, input: dict[str, Any] | None = None
    ) -> CompiledWorkflow:
        """Run every deterministic definition/input check without persistence."""
        compiled = self.compiler.compile(definition)
        if definition.status is not WorkflowDefinitionStatus.PUBLISHED:
            raise GraphStateError("only published definitions can run")
        run_input = input or {}
        nodes_by_id = {node.node_id: node for node in definition.nodes}
        for entry_node_id in compiled.entry_node_ids:
            _validate_port_values(
                nodes_by_id[entry_node_id].input_ports,
                run_input,
                context=f"entry node {entry_node_id} input",
            )
        return compiled

    def start_run(self, run_id: str) -> GraphWorkflowRun:
        run = self.repository.get_run(run_id)
        if run.status not in {
            GraphRunStatus.CREATED,
            GraphRunStatus.QUEUED,
            GraphRunStatus.INTERRUPTED,
        }:
            raise GraphStateError(f"cannot start run in {run.status.value}")
        return self._save_run(
            run, status=GraphRunStatus.RUNNING, started_at=run.started_at or utc_now()
        )

    def start_attempt(
        self,
        node_run_id: str,
        *,
        worker_id: str | None = None,
        lease_id: str | None = None,
        agent_instance_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> NodeAttempt:
        node_run = self.repository.get_node_run(node_run_id)
        self._require_running_run(node_run.workflow_run_id)
        if node_run.status not in {NodeRunStatus.READY, NodeRunStatus.RETRY_WAIT}:
            raise GraphStateError(f"cannot start node in {node_run.status.value}")
        definition, spec = self._definition_and_node(node_run)
        _validate_port_values(
            spec.input_ports,
            node_run.input_refs,
            context=f"node {spec.node_id} input",
        )
        active_nodes = sum(
            node.status is NodeRunStatus.RUNNING
            for node in self.repository.list_node_runs(node_run.workflow_run_id)
        )
        if active_nodes >= definition.graph_limits.max_parallel_nodes:
            raise GraphStateError("graph max_parallel_nodes limit reached")
        side_effect = (
            AttemptSideEffectState.NOT_STARTED
            if spec.idempotency_class is not IdempotencyClass.PURE
            else AttemptSideEffectState.NONE
        )
        attempt = NodeAttempt(
            node_run_id=node_run.id,
            attempt_number=len(self.repository.list_attempts(node_run.id)) + 1,
            agent_instance_id=agent_instance_id,
            side_effect_state=side_effect,
            idempotency_key=self._attempt_idempotency_key(
                node_run, spec, side_effect, requested=idempotency_key
            ),
        )
        updated_node = node_run.model_copy(
            update={
                "status": NodeRunStatus.RUNNING,
                "active_attempt_id": attempt.id,
                "worker_id": worker_id,
                "lease_id": lease_id,
                "revision": node_run.revision + 1,
                "updated_at": utc_now(),
            }
        )
        self.repository.start_node_attempt(
            updated_node, attempt, expected_node_revision=node_run.revision
        )
        return attempt

    def complete_attempt(
        self,
        attempt: NodeAttempt,
        *,
        succeeded: bool,
        output_refs: dict[str, Any] | None = None,
        failure_class: str | None = None,
        side_effect_state: AttemptSideEffectState | None = None,
    ) -> NodeRun:
        stored = self._attempt(attempt.id)
        if stored.result is not AttemptResult.RUNNING:
            raise GraphStateError("attempt is already terminal")
        node_run = self.repository.get_node_run(stored.node_run_id)
        self._require_running_run(node_run.workflow_run_id)
        if node_run.active_attempt_id != stored.id or node_run.status is not NodeRunStatus.RUNNING:
            raise GraphStateError("attempt is not active")
        _, spec = self._definition_and_node(node_run)
        effect = side_effect_state or stored.side_effect_state
        payload = output_refs or {}
        _validate_port_values(
            spec.output_ports,
            payload,
            context=f"node {spec.node_id} output",
            require_required=succeeded and effect is not AttemptSideEffectState.UNKNOWN,
        )
        result = AttemptResult.SUCCEEDED if succeeded else AttemptResult.FAILED
        terminal_attempt = stored.model_copy(
            update={
                "result": result,
                "result_payload": payload,
                "failure_class": failure_class,
                "side_effect_state": effect,
                "completed_at": utc_now(),
            }
        )
        if (
            effect is AttemptSideEffectState.UNKNOWN
            and spec.idempotency_class is IdempotencyClass.NON_IDEMPOTENT
        ):
            updated = self._finish_node_attempt(
                node_run,
                terminal_attempt,
                status=NodeRunStatus.MANUAL_RECONCILE_REQUIRED,
                active_attempt_id=None,
                output_refs=payload,
            )
            self._mark_run_manual_reconcile(node_run.workflow_run_id)
            return updated
        if effect is AttemptSideEffectState.UNKNOWN:
            interrupted_attempt = terminal_attempt.model_copy(
                update={
                    "result": AttemptResult.INTERRUPTED,
                    "failure_class": failure_class or "side_effect_outcome_unknown",
                }
            )
            return self._finish_node_attempt(
                node_run,
                interrupted_attempt,
                status=NodeRunStatus.READY,
                active_attempt_id=None,
                worker_id=None,
                lease_id=None,
            )
        if succeeded:
            updated = self._finish_node_attempt(
                node_run,
                terminal_attempt,
                status=NodeRunStatus.SUCCEEDED,
                active_attempt_id=None,
                output_refs=payload,
                worker_id=None,
                lease_id=None,
            )
            self._propagate_dependencies(node_run.workflow_run_id)
            self._finish_if_terminal(node_run.workflow_run_id)
            self._refresh_run_activity_status(node_run.workflow_run_id)
            return updated
        if (
            spec.retry_policy.max_attempts > stored.attempt_number
            and spec.idempotency_class is not IdempotencyClass.NON_IDEMPOTENT
        ):
            return self._finish_node_attempt(
                node_run,
                terminal_attempt,
                status=NodeRunStatus.RETRY_WAIT,
                active_attempt_id=None,
                retry_count=node_run.retry_count + 1,
                worker_id=None,
                lease_id=None,
            )
        if spec.failure_policy is FailurePolicy.SKIP:
            updated = self._finish_node_attempt(
                node_run,
                terminal_attempt,
                status=NodeRunStatus.SKIPPED,
                active_attempt_id=None,
                output_refs=output_refs or {},
                worker_id=None,
                lease_id=None,
            )
            self._propagate_dependencies(node_run.workflow_run_id)
            self._finish_if_terminal(node_run.workflow_run_id)
            self._refresh_run_activity_status(node_run.workflow_run_id)
            return updated
        if spec.failure_policy is FailurePolicy.MANUAL_RECONCILE:
            updated = self._finish_node_attempt(
                node_run,
                terminal_attempt.model_copy(
                    update={"side_effect_state": AttemptSideEffectState.UNKNOWN}
                ),
                status=NodeRunStatus.MANUAL_RECONCILE_REQUIRED,
                active_attempt_id=None,
                output_refs=output_refs or {},
                worker_id=None,
                lease_id=None,
            )
            self._mark_run_manual_reconcile(node_run.workflow_run_id)
            return updated
        updated = self._finish_node_attempt(
            node_run,
            terminal_attempt,
            status=NodeRunStatus.FAILED,
            active_attempt_id=None,
            worker_id=None,
            lease_id=None,
        )
        manual = self._terminalize_active_nodes(
            node_run.workflow_run_id, safe_status=NodeRunStatus.CANCELLED
        )
        self._save_run(
            self.repository.get_run(node_run.workflow_run_id),
            status=(GraphRunStatus.MANUAL_RECONCILE_REQUIRED if manual else GraphRunStatus.FAILED),
            completed_at=None if manual else utc_now(),
        )
        return updated

    def mark_side_effect_started(self, attempt_id: str) -> NodeAttempt:
        """Persist this before dispatching an operation to the Action Gateway."""
        attempt = self.repository.get_attempt(attempt_id)
        if attempt.result is not AttemptResult.RUNNING:
            raise GraphStateError("attempt is already terminal")
        if attempt.side_effect_state is AttemptSideEffectState.NONE:
            raise GraphStateError("pure attempts have no side-effect boundary")
        node_run = self.repository.get_node_run(attempt.node_run_id)
        self._require_running_run(node_run.workflow_run_id)
        updated = attempt.model_copy(update={"side_effect_state": AttemptSideEffectState.STARTED})
        self.repository.update_attempt(updated)
        return updated

    def select_condition(
        self,
        node_run_id: str,
        *,
        selected_port: str,
        output_refs: dict[str, Any] | None = None,
    ) -> NodeRun:
        node_run = self.repository.get_node_run(node_run_id)
        self._require_running_run(node_run.workflow_run_id)
        definition, spec = self._definition_and_node(node_run)
        if spec.node_kind is not NodeKind.CONDITION or node_run.status is not NodeRunStatus.READY:
            raise GraphStateError("condition node is not ready")
        _validate_port_values(
            spec.input_ports,
            node_run.input_refs,
            context=f"node {spec.node_id} input",
        )
        if selected_port not in {port.name for port in spec.output_ports}:
            raise GraphStateError("condition selected an unknown output port")
        selected_output = dict(output_refs or {})
        selected_output.setdefault(selected_port, True)
        _validate_port_values(
            spec.output_ports,
            selected_output,
            context=f"node {spec.node_id} output",
            require_required=False,
        )
        updated = self._save_node(
            node_run,
            status=NodeRunStatus.SUCCEEDED,
            output_refs={**selected_output, "selected_port": selected_port},
        )
        self._propagate_dependencies(node_run.workflow_run_id)
        self._finish_if_terminal(node_run.workflow_run_id)
        self._refresh_run_activity_status(node_run.workflow_run_id)
        return updated

    def enter_boundary(self, node_run_id: str, boundary: BoundaryKind) -> NodeRun:
        node_run = self.repository.get_node_run(node_run_id)
        self._require_running_run(node_run.workflow_run_id)
        if node_run.status is not NodeRunStatus.READY:
            raise GraphStateError("only ready nodes may enter a durable boundary")
        _, spec = self._definition_and_node(node_run)
        _validate_port_values(
            spec.input_ports,
            node_run.input_refs,
            context=f"node {spec.node_id} input",
        )
        expected = {
            BoundaryKind.APPROVAL: NodeKind.APPROVAL,
            BoundaryKind.HUMAN_INPUT: NodeKind.HUMAN_INPUT,
            BoundaryKind.WAIT: NodeKind.WAIT,
            BoundaryKind.SUBWORKFLOW: NodeKind.SUBWORKFLOW,
        }[boundary]
        if spec.node_kind is not expected:
            raise GraphStateError(f"node is not a {expected.value} boundary")
        status = {
            BoundaryKind.APPROVAL: NodeRunStatus.WAITING_APPROVAL,
            BoundaryKind.HUMAN_INPUT: NodeRunStatus.WAITING_INPUT,
            BoundaryKind.WAIT: NodeRunStatus.WAITING,
            BoundaryKind.SUBWORKFLOW: NodeRunStatus.WAITING,
        }[boundary]
        updated = self._save_node(node_run, status=status, wait_token=f"wait_{uuid4().hex}")
        self._refresh_run_activity_status(node_run.workflow_run_id)
        return updated

    def resolve_boundary(self, node_run_id: str, resolution: BoundaryResolution) -> NodeRun:
        node_run = self.repository.get_node_run(node_run_id)
        if node_run.status in {NodeRunStatus.SUCCEEDED, NodeRunStatus.FAILED}:
            if node_run.wait_token == resolution.wait_token:
                return node_run
            raise GraphConflictError("stale or conflicting boundary resolution")
        if node_run.status not in {
            NodeRunStatus.WAITING_APPROVAL,
            NodeRunStatus.WAITING_INPUT,
            NodeRunStatus.WAITING,
        }:
            raise GraphStateError("node is not waiting")
        run = self.repository.get_run(node_run.workflow_run_id)
        if run.status in _TERMINAL_GRAPH_STATUSES:
            raise GraphStateError(f"cannot resolve a boundary for terminal run {run.status.value}")
        if run.status not in {
            GraphRunStatus.RUNNING,
            GraphRunStatus.WAITING_APPROVAL,
            GraphRunStatus.WAITING_INPUT,
        }:
            raise GraphStateError(f"cannot resolve a boundary for run {run.status.value}")
        if node_run.wait_token != resolution.wait_token:
            raise GraphConflictError("stale or duplicate boundary resolution")
        _, spec = self._definition_and_node(node_run)
        _validate_port_values(
            spec.output_ports,
            resolution.payload,
            context=f"node {spec.node_id} boundary output",
            require_required=resolution.accepted,
        )
        status = NodeRunStatus.SUCCEEDED if resolution.accepted else NodeRunStatus.FAILED
        updated = self._save_node(node_run, status=status, output_refs=resolution.payload)
        if resolution.accepted:
            self._propagate_dependencies(run.id)
            self._finish_if_terminal(run.id)
            self._refresh_run_activity_status(run.id)
        else:
            manual = self._terminalize_active_nodes(run.id, safe_status=NodeRunStatus.CANCELLED)
            self._save_run(
                self.repository.get_run(run.id),
                status=(
                    GraphRunStatus.MANUAL_RECONCILE_REQUIRED if manual else GraphRunStatus.FAILED
                ),
                completed_at=None if manual else utc_now(),
            )
        return updated

    def timeout_boundary(self, node_run_id: str, *, wait_token: str) -> NodeRun:
        node_run = self.repository.get_node_run(node_run_id)
        if node_run.status not in {
            NodeRunStatus.WAITING_APPROVAL,
            NodeRunStatus.WAITING_INPUT,
            NodeRunStatus.WAITING,
        }:
            raise GraphStateError("node is not waiting")
        run = self.repository.get_run(node_run.workflow_run_id)
        if run.status in _TERMINAL_GRAPH_STATUSES:
            raise GraphStateError(f"cannot timeout a boundary for terminal run {run.status.value}")
        if node_run.wait_token != wait_token:
            raise GraphConflictError("stale boundary timeout")
        _, spec = self._definition_and_node(node_run)
        timeout_target = spec.timeout_policy.on_timeout_node_id if spec.timeout_policy else None
        if timeout_target is None:
            raise GraphStateError("boundary has no compiled timeout branch")
        updated = self._save_node(
            node_run,
            status=NodeRunStatus.SKIPPED,
            output_refs={"timeout": True},
        )
        self._propagate_dependencies(node_run.workflow_run_id)
        self._finish_if_terminal(node_run.workflow_run_id)
        self._refresh_run_activity_status(node_run.workflow_run_id)
        return updated

    def advance_loop(
        self,
        node_run_id: str,
        *,
        continue_loop: bool,
        progress_signature: str,
        output_refs: dict[str, Any] | None = None,
        elapsed_seconds: float = 0,
        output_tokens: int = 0,
        cost_usd: float = 0,
        subagents: int = 0,
        recursion_depth: int = 1,
    ) -> NodeRun:
        if (
            isinstance(elapsed_seconds, bool)
            or not isinstance(elapsed_seconds, (int, float))
            or not math.isfinite(elapsed_seconds)
            or elapsed_seconds < 0
            or isinstance(cost_usd, bool)
            or not isinstance(cost_usd, (int, float))
            or not math.isfinite(cost_usd)
            or cost_usd < 0
            or isinstance(output_tokens, bool)
            or not isinstance(output_tokens, int)
            or output_tokens < 0
            or isinstance(subagents, bool)
            or not isinstance(subagents, int)
            or subagents < 0
            or isinstance(recursion_depth, bool)
            or not isinstance(recursion_depth, int)
            or recursion_depth < 0
        ):
            raise ValueError(
                "loop time/cost must be finite non-negative numbers and "
                "token/subagent/recursion usage must be non-negative integers"
            )
        node_run = self.repository.get_node_run(node_run_id)
        self._require_running_run(node_run.workflow_run_id)
        _, spec = self._definition_and_node(node_run)
        policy = spec.loop_policy
        if spec.node_kind is not NodeKind.LOOP or policy is None:
            raise GraphStateError("node is not a bounded loop")
        if node_run.status not in {NodeRunStatus.READY, NodeRunStatus.RUNNING}:
            raise GraphStateError("loop node is not active")
        _validate_port_values(
            spec.input_ports,
            node_run.input_refs,
            context=f"node {spec.node_id} input",
        )
        repeats = (
            node_run.no_progress_repeats + 1
            if node_run.no_progress_signature == progress_signature
            else 0
        )
        iteration = node_run.iteration + 1
        limit_reasons: list[str] = []
        if iteration > policy.max_iterations:
            limit_reasons.append("max_iterations")
        if repeats >= policy.max_no_progress_repeats:
            limit_reasons.append("no_progress")
        if elapsed_seconds > policy.max_wall_seconds:
            limit_reasons.append("max_wall_seconds")
        if output_tokens > policy.max_output_tokens:
            limit_reasons.append("max_output_tokens")
        if cost_usd > policy.max_cost_usd:
            limit_reasons.append("max_cost_usd")
        if subagents > policy.max_subagents:
            limit_reasons.append("max_subagents")
        if recursion_depth > policy.max_recursion_depth:
            limit_reasons.append("max_recursion_depth")
        limit_hit = bool(limit_reasons)
        _validate_port_values(
            spec.output_ports,
            output_refs or {},
            context=f"node {spec.node_id} loop output",
            require_required=not continue_loop and not limit_hit,
        )
        if limit_hit:
            updated = self._save_node(
                node_run,
                status=NodeRunStatus.SKIPPED,
                iteration=iteration,
                no_progress_signature=progress_signature,
                no_progress_repeats=repeats,
                output_refs={
                    **(output_refs or {}),
                    "limit_reasons": limit_reasons,
                },
            )
            self._propagate_dependencies(node_run.workflow_run_id)
            self._finish_if_terminal(node_run.workflow_run_id)
            self._refresh_run_activity_status(node_run.workflow_run_id)
            return updated
        if not continue_loop:
            updated = self._save_node(
                node_run,
                status=NodeRunStatus.SUCCEEDED,
                iteration=iteration,
                no_progress_signature=progress_signature,
                no_progress_repeats=repeats,
                output_refs=output_refs or {},
            )
            self._propagate_dependencies(node_run.workflow_run_id, exclude_loop_back=True)
            self._finish_if_terminal(node_run.workflow_run_id)
            self._refresh_run_activity_status(node_run.workflow_run_id)
            return updated
        updated = self._save_node(
            node_run,
            status=NodeRunStatus.READY,
            iteration=iteration,
            no_progress_signature=progress_signature,
            no_progress_repeats=repeats,
            output_refs=output_refs or {},
        )
        self._unlock_loop_body(node_run.workflow_run_id, spec.node_id)
        return updated

    def record_budget(
        self, run_id: str, *, output_tokens: int = 0, cost_usd: float = 0, tool_calls: int = 0
    ) -> GraphWorkflowRun:
        if (
            isinstance(output_tokens, bool)
            or not isinstance(output_tokens, int)
            or isinstance(tool_calls, bool)
            or not isinstance(tool_calls, int)
            or isinstance(cost_usd, bool)
            or not isinstance(cost_usd, (int, float))
            or output_tokens < 0
            or cost_usd < 0
            or tool_calls < 0
            or not math.isfinite(cost_usd)
        ):
            raise ValueError("budget usage values must be finite and non-negative")
        run = self.repository.get_run(run_id)
        if (
            isinstance(run.consumed_output_tokens, bool)
            or not isinstance(run.consumed_output_tokens, int)
            or isinstance(run.consumed_tool_calls, bool)
            or not isinstance(run.consumed_tool_calls, int)
            or isinstance(run.consumed_cost_usd, bool)
            or not isinstance(run.consumed_cost_usd, (int, float))
            or run.consumed_output_tokens < 0
            or run.consumed_tool_calls < 0
            or run.consumed_cost_usd < 0
            or not math.isfinite(run.consumed_cost_usd)
        ):
            raise GraphStateError("persisted budget usage is invalid")
        if run.status not in {
            GraphRunStatus.RUNNING,
            GraphRunStatus.WAITING_APPROVAL,
            GraphRunStatus.WAITING_INPUT,
        }:
            raise GraphStateError(f"cannot record budget for run {run.status.value}")
        tokens = run.consumed_output_tokens + output_tokens
        cost = run.consumed_cost_usd + cost_usd
        calls = run.consumed_tool_calls + tool_calls
        budget = run.budget_snapshot
        exceeded = (
            (budget.max_output_tokens is not None and tokens > budget.max_output_tokens)
            or (budget.max_cost_usd is not None and cost > budget.max_cost_usd)
            or (budget.max_tool_calls is not None and calls > budget.max_tool_calls)
        )
        if exceeded:
            manual = self._terminalize_active_nodes(run_id, safe_status=NodeRunStatus.CANCELLED)
            current = self.repository.get_run(run_id)
            return self._save_run(
                current,
                consumed_output_tokens=tokens,
                consumed_cost_usd=cost,
                consumed_tool_calls=calls,
                status=(
                    GraphRunStatus.MANUAL_RECONCILE_REQUIRED if manual else GraphRunStatus.FAILED
                ),
                completed_at=None if manual else utc_now(),
            )
        return self._save_run(
            run,
            consumed_output_tokens=tokens,
            consumed_cost_usd=cost,
            consumed_tool_calls=calls,
        )

    def cancel_run(self, run_id: str) -> GraphWorkflowRun:
        run = self.repository.get_run(run_id)
        if run.status in _TERMINAL_GRAPH_STATUSES:
            return run
        manual = self._terminalize_active_nodes(run_id, safe_status=NodeRunStatus.CANCELLED)
        return self._save_run(
            self.repository.get_run(run_id),
            status=(
                GraphRunStatus.MANUAL_RECONCILE_REQUIRED if manual else GraphRunStatus.CANCELLED
            ),
            completed_at=None if manual else utc_now(),
        )

    def interrupt_run(self, run_id: str) -> GraphWorkflowRun:
        """Persist a coordinator interruption without replaying unknown writes."""

        run = self.repository.get_run(run_id)
        if run.status in _TERMINAL_GRAPH_STATUSES:
            return run
        manual = self._reset_interrupted_nodes(run_id)
        return self._save_run(
            self.repository.get_run(run_id),
            status=(
                GraphRunStatus.MANUAL_RECONCILE_REQUIRED if manual else GraphRunStatus.INTERRUPTED
            ),
        )

    def fail_run(self, run_id: str) -> GraphWorkflowRun:
        """Record a terminal coordinator failure not owned by one node attempt."""

        run = self.repository.get_run(run_id)
        if run.status in _TERMINAL_GRAPH_STATUSES:
            return run
        manual = self._terminalize_active_nodes(run_id, safe_status=NodeRunStatus.CANCELLED)
        return self._save_run(
            self.repository.get_run(run_id),
            status=(GraphRunStatus.MANUAL_RECONCILE_REQUIRED if manual else GraphRunStatus.FAILED),
            completed_at=None if manual else utc_now(),
        )

    def recover(self, run_id: str) -> GraphWorkflowRun:
        run = self.repository.get_run(run_id)
        if run.status in _TERMINAL_GRAPH_STATUSES:
            return run
        manual = self._reset_interrupted_nodes(run_id)
        if manual:
            return self._save_run(
                self.repository.get_run(run_id),
                status=GraphRunStatus.MANUAL_RECONCILE_REQUIRED,
            )
        self._reset_derived_dependency_states(run_id)
        self._propagate_dependencies(run_id)
        self._finish_if_terminal(run_id)
        run = self.repository.get_run(run_id)
        if run.status is GraphRunStatus.COMPLETED:
            return run
        return self._refresh_run_activity_status(run_id)

    def _reset_interrupted_nodes(self, run_id: str) -> bool:
        manual = False
        for node_run in self.repository.list_node_runs(run_id):
            if node_run.status is NodeRunStatus.MANUAL_RECONCILE_REQUIRED:
                manual = True
                continue
            if node_run.status is not NodeRunStatus.RUNNING:
                continue
            _, spec = self._definition_and_node(node_run)
            attempts = self.repository.list_attempts(node_run.id)
            attempt = attempts[-1] if attempts else None
            unknown_write = self._unknown_non_idempotent_write(spec, attempt)
            if attempt is not None and attempt.result is AttemptResult.SUCCEEDED:
                self._save_node(
                    node_run,
                    status=NodeRunStatus.SUCCEEDED,
                    active_attempt_id=None,
                    output_refs=attempt.result_payload,
                    worker_id=None,
                    lease_id=None,
                )
                continue
            if attempt is not None and attempt.result is AttemptResult.RUNNING:
                self.repository.update_attempt(
                    attempt.model_copy(
                        update={
                            "result": AttemptResult.INTERRUPTED,
                            "side_effect_state": (
                                AttemptSideEffectState.UNKNOWN
                                if unknown_write
                                else attempt.side_effect_state
                            ),
                            "completed_at": utc_now(),
                        }
                    )
                )
            self._save_node(
                node_run,
                status=(
                    NodeRunStatus.MANUAL_RECONCILE_REQUIRED
                    if unknown_write
                    else NodeRunStatus.READY
                ),
                active_attempt_id=None,
                worker_id=None,
                lease_id=None,
            )
            manual = manual or unknown_write
        return manual

    def _definition_and_node(self, node_run: NodeRun) -> tuple[WorkflowDefinition, NodeSpec]:
        run = self.repository.get_run(node_run.workflow_run_id)
        definition = self.repository.get_definition(
            run.workflow_definition_id, run.workflow_definition_version
        )
        return definition, next(
            node for node in definition.nodes if node.node_id == node_run.node_id
        )

    def _attempt(self, attempt_id: str) -> NodeAttempt:
        return self.repository.get_attempt(attempt_id)

    def _attempt_idempotency_key(
        self,
        node_run: NodeRun,
        spec: NodeSpec,
        side_effect: AttemptSideEffectState,
        *,
        requested: str | None,
    ) -> str | None:
        attempts = self.repository.list_attempts(node_run.id)
        if attempts and spec.idempotency_class is IdempotencyClass.IDEMPOTENT:
            previous = attempts[-1]
            if previous.idempotency_key:
                return previous.idempotency_key
        if requested is not None or side_effect is AttemptSideEffectState.NONE:
            return requested
        return f"{node_run.workflow_run_id}:{node_run.node_id}:{node_run.retry_count}"

    def _require_running_run(self, run_id: str) -> None:
        run = self.repository.get_run(run_id)
        if run.status is not GraphRunStatus.RUNNING:
            raise GraphStateError(f"graph run is not running: {run.status.value}")

    def _refresh_run_activity_status(self, run_id: str) -> GraphWorkflowRun:
        """Project one aggregate Run status without freezing parallel runnable nodes."""

        run = self.repository.get_run(run_id)
        if run.status in _TERMINAL_GRAPH_STATUSES:
            return run
        if run.started_at is None:
            desired = GraphRunStatus.CREATED
        else:
            statuses = {node.status for node in self.repository.list_node_runs(run_id)}
            if NodeRunStatus.MANUAL_RECONCILE_REQUIRED in statuses:
                desired = GraphRunStatus.MANUAL_RECONCILE_REQUIRED
            elif NodeRunStatus.FAILED in statuses:
                desired = GraphRunStatus.FAILED
            elif statuses.intersection(_ACTIVE_NODE_STATUSES):
                desired = GraphRunStatus.RUNNING
            elif NodeRunStatus.WAITING_APPROVAL in statuses:
                desired = GraphRunStatus.WAITING_APPROVAL
            elif statuses.intersection({NodeRunStatus.WAITING_INPUT, NodeRunStatus.WAITING}):
                desired = GraphRunStatus.WAITING_INPUT
            elif statuses and statuses.issubset({NodeRunStatus.SUCCEEDED, NodeRunStatus.SKIPPED}):
                desired = GraphRunStatus.COMPLETED
            else:
                desired = run.status
        if desired is run.status:
            return run
        return self._save_run(
            run,
            status=desired,
            completed_at=(
                utc_now()
                if desired
                in {GraphRunStatus.COMPLETED, GraphRunStatus.FAILED, GraphRunStatus.CANCELLED}
                else None
            ),
        )

    def _terminalize_active_nodes(self, run_id: str, *, safe_status: NodeRunStatus) -> bool:
        """Stop unfinished nodes while preserving unknown non-idempotent effects."""

        manual = False
        for node in self.repository.list_node_runs(run_id):
            if node.status not in _NONTERMINAL_NODE_STATUSES:
                continue
            unknown_write = False
            if node.active_attempt_id is not None:
                attempt = self.repository.get_attempt(node.active_attempt_id)
                if attempt.result is AttemptResult.RUNNING:
                    _, spec = self._definition_and_node(node)
                    unknown_write = self._unknown_non_idempotent_write(spec, attempt)
                    self.repository.update_attempt(
                        attempt.model_copy(
                            update={
                                "result": (
                                    AttemptResult.INTERRUPTED
                                    if unknown_write
                                    else AttemptResult.CANCELLED
                                ),
                                "side_effect_state": (
                                    AttemptSideEffectState.UNKNOWN
                                    if unknown_write
                                    else attempt.side_effect_state
                                ),
                                "completed_at": utc_now(),
                            }
                        )
                    )
                elif attempt.result is AttemptResult.SUCCEEDED:
                    self._save_node(
                        node,
                        status=NodeRunStatus.SUCCEEDED,
                        active_attempt_id=None,
                        output_refs=attempt.result_payload,
                        worker_id=None,
                        lease_id=None,
                    )
                    continue
            elif node.status is NodeRunStatus.RUNNING:
                _, spec = self._definition_and_node(node)
                unknown_write = self._unknown_non_idempotent_write(spec, None)
            self._save_node(
                node,
                status=(NodeRunStatus.MANUAL_RECONCILE_REQUIRED if unknown_write else safe_status),
                active_attempt_id=None,
                worker_id=None,
                lease_id=None,
            )
            manual = manual or unknown_write
        return manual

    @staticmethod
    def _unknown_non_idempotent_write(spec: NodeSpec, attempt: NodeAttempt | None) -> bool:
        return spec.idempotency_class is IdempotencyClass.NON_IDEMPOTENT and (
            attempt is None
            or attempt.side_effect_state
            in {AttemptSideEffectState.STARTED, AttemptSideEffectState.UNKNOWN}
        )

    def _save_node(self, node: NodeRun, **updates: Any) -> NodeRun:
        updated = node.model_copy(
            update={**updates, "revision": node.revision + 1, "updated_at": utc_now()}
        )
        self.repository.update_node_run(updated, expected_revision=node.revision)
        return updated

    def _finish_node_attempt(self, node: NodeRun, attempt: NodeAttempt, **updates: Any) -> NodeRun:
        updated = node.model_copy(
            update={**updates, "revision": node.revision + 1, "updated_at": utc_now()}
        )
        self.repository.finish_node_attempt(updated, attempt, expected_node_revision=node.revision)
        return updated

    def _save_run(self, run: GraphWorkflowRun, **updates: Any) -> GraphWorkflowRun:
        updated = run.model_copy(
            update={**updates, "revision": run.revision + 1, "updated_at": utc_now()}
        )
        self.repository.update_run(updated, expected_revision=run.revision)
        return updated

    def _mark_run_manual_reconcile(self, run_id: str) -> None:
        self._save_run(
            self.repository.get_run(run_id), status=GraphRunStatus.MANUAL_RECONCILE_REQUIRED
        )

    def _reset_derived_dependency_states(self, run_id: str) -> None:
        """Discard cached routing decisions so recovery can rebuild them from facts."""
        run = self.repository.get_run(run_id)
        definition = self.repository.get_definition(
            run.workflow_definition_id, run.workflow_definition_version
        )
        has_incoming = {edge.target_node for edge in definition.edges if not edge.loop_back}
        for node in self.repository.list_node_runs(run_id):
            if node.node_id not in has_incoming:
                continue
            attempts = self.repository.list_attempts(node.id)
            latest = attempts[-1] if attempts else None
            persisted_skip = node.status is NodeRunStatus.SKIPPED and (
                node.output_refs.get("timeout") is True
                or "limit_reasons" in node.output_refs
                or (latest is not None and latest.result is AttemptResult.FAILED)
            )
            if node.status is NodeRunStatus.READY or (
                node.status is NodeRunStatus.SKIPPED and not persisted_skip
            ):
                self._save_node(node, status=NodeRunStatus.PENDING, input_refs={})

    def _propagate_dependencies(self, run_id: str, *, exclude_loop_back: bool = True) -> None:
        run = self.repository.get_run(run_id)
        definition = self.repository.get_definition(
            run.workflow_definition_id, run.workflow_definition_version
        )
        incoming: dict[str, list[EdgeSpec]] = defaultdict(list)
        for edge in definition.edges:
            if exclude_loop_back and edge.loop_back:
                continue
            incoming[edge.target_node].append(edge)
        while True:
            changed = False
            by_spec = {node.node_id: node for node in self.repository.list_node_runs(run_id)}
            for spec_id, node_run in by_spec.items():
                edges = incoming[spec_id]
                if node_run.status is not NodeRunStatus.PENDING or not edges:
                    continue
                states = [self._edge_state(edge, by_spec, definition, run.input) for edge in edges]
                active_edges = [
                    edge for edge, state in zip(edges, states, strict=True) if state == "active"
                ]
                unresolved = any(state == "unresolved" for state in states)
                disabled = any(state == "disabled" for state in states)
                mode = edges[0].join_mode
                target_spec = next(item for item in definition.nodes if item.node_id == spec_id)
                if mode is JoinMode.ANY:
                    ready = bool(active_edges)
                    skipped = not active_edges and not unresolved
                elif target_spec.node_kind is NodeKind.JOIN:
                    # ALL joins aggregate every branch that actually ran. A
                    # skipped branch is absent, not a reason to deadlock.
                    ready = not unresolved
                    skipped = False
                else:
                    ready = not unresolved and not disabled
                    skipped = disabled
                if not ready and not skipped:
                    continue
                input_refs = self._input_refs(active_edges, by_spec) if ready else {}
                self._save_node(
                    node_run,
                    status=NodeRunStatus.READY if ready else NodeRunStatus.SKIPPED,
                    input_refs=input_refs,
                )
                changed = True
            if not changed:
                return

    @staticmethod
    def _input_refs(active_edges: list[EdgeSpec], by_spec: dict[str, NodeRun]) -> dict[str, Any]:
        refs: dict[str, Any] = {}
        for edge in active_edges:
            source_refs = by_spec[edge.source_node].output_refs
            value = source_refs.get(edge.source_port)
            existing = refs.get(edge.target_port)
            if edge.target_port not in refs:
                refs[edge.target_port] = value
            elif isinstance(existing, list):
                existing.append(value)
            else:
                refs[edge.target_port] = [existing, value]
        return refs

    @staticmethod
    def _edge_state(
        edge: EdgeSpec,
        by_spec: dict[str, NodeRun],
        definition: WorkflowDefinition,
        run_input: dict[str, Any],
    ) -> str:
        source = by_spec[edge.source_node]
        condition_values = {**run_input, **source.output_refs}
        if source.status is NodeRunStatus.SUCCEEDED:
            source_spec = next(
                node for node in definition.nodes if node.node_id == edge.source_node
            )
            selected_port = source.output_refs.get("selected_port")
            if source_spec.node_kind is NodeKind.CONDITION and selected_port is not None:
                if edge.source_port != selected_port:
                    return "disabled"
                if edge.source_port not in source.output_refs:
                    return "disabled"
                if edge.condition is None:
                    return "active"
                return (
                    "active"
                    if _evaluate_condition(edge.condition, condition_values)
                    else "disabled"
                )
            if edge.source_port not in source.output_refs:
                return "disabled"
            if edge.condition is None:
                return "active"
            return "active" if _evaluate_condition(edge.condition, condition_values) else "disabled"
        if source.status is NodeRunStatus.SKIPPED:
            source_spec = next(
                node for node in definition.nodes if node.node_id == edge.source_node
            )
            timeout_target = (
                source_spec.timeout_policy.on_timeout_node_id
                if source_spec.timeout_policy is not None
                else None
            )
            if (
                source.output_refs.get("timeout") is True
                and edge.target_node == timeout_target
                and (
                    edge.condition is None or _evaluate_condition(edge.condition, condition_values)
                )
            ):
                return "active"
            loop_limit_target = (
                source_spec.loop_policy.on_limit_node_id
                if source_spec.loop_policy is not None
                else None
            )
            if (
                "limit_reasons" in source.output_refs
                and edge.target_node == loop_limit_target
                and (
                    edge.condition is None or _evaluate_condition(edge.condition, condition_values)
                )
            ):
                return "active"
            return "disabled"
        if source.status in {
            NodeRunStatus.FAILED,
            NodeRunStatus.CANCELLED,
            NodeRunStatus.MANUAL_RECONCILE_REQUIRED,
        }:
            return "disabled"
        return "unresolved"

    def _unlock_loop_body(self, run_id: str, loop_node_id: str) -> None:
        run = self.repository.get_run(run_id)
        definition = self.repository.get_definition(
            run.workflow_definition_id, run.workflow_definition_version
        )
        targets = {
            edge.target_node
            for edge in definition.edges
            if edge.source_node == loop_node_id and edge.loop_back
        }
        if not targets:
            targets = {
                edge.target_node for edge in definition.edges if edge.source_node == loop_node_id
            }
        cycle_nodes = self._loop_cycle_nodes(definition, loop_node_id, targets)
        by_spec = {node.node_id: node for node in self.repository.list_node_runs(run_id)}
        branch_targets: set[str] = set()
        for edge in definition.edges:
            if edge.source_node in cycle_nodes and edge.target_node not in cycle_nodes:
                branch_targets.add(edge.target_node)
        for node_id in sorted((cycle_nodes - targets).union(branch_targets)):
            node = by_spec.get(node_id)
            if node is not None and node.status in {
                NodeRunStatus.SUCCEEDED,
                NodeRunStatus.SKIPPED,
                NodeRunStatus.FAILED,
            }:
                self._save_node(node, status=NodeRunStatus.PENDING, active_attempt_id=None)
        for target in targets:
            self._ready_node_by_spec_id(run_id, target, allow_reset=True)

    @staticmethod
    def _loop_cycle_nodes(
        definition: WorkflowDefinition, loop_node_id: str, targets: set[str]
    ) -> set[str]:
        outgoing: dict[str, set[str]] = defaultdict(set)
        incoming: dict[str, set[str]] = defaultdict(set)
        for edge in definition.edges:
            outgoing[edge.source_node].add(edge.target_node)
            incoming[edge.target_node].add(edge.source_node)

        reachable = set(targets)
        pending = list(targets)
        while pending:
            current = pending.pop()
            for target in outgoing[current]:
                if target not in reachable:
                    reachable.add(target)
                    pending.append(target)

        reaches_loop = {loop_node_id}
        pending = [loop_node_id]
        while pending:
            current = pending.pop()
            for source in incoming[current]:
                if source not in reaches_loop:
                    reaches_loop.add(source)
                    pending.append(source)
        return reachable.intersection(reaches_loop)

    def _ready_node_by_spec_id(
        self, run_id: str, node_id: str, *, allow_reset: bool = False
    ) -> None:
        node = next(
            item for item in self.repository.list_node_runs(run_id) if item.node_id == node_id
        )
        if node.status is NodeRunStatus.PENDING or (
            allow_reset
            and node.status
            in {NodeRunStatus.SUCCEEDED, NodeRunStatus.SKIPPED, NodeRunStatus.FAILED}
        ):
            self._save_node(node, status=NodeRunStatus.READY, active_attempt_id=None)

    def _finish_if_terminal(self, run_id: str) -> None:
        run = self.repository.get_run(run_id)
        nodes = self.repository.list_node_runs(run_id)
        if nodes and all(
            node.status in {NodeRunStatus.SUCCEEDED, NodeRunStatus.SKIPPED} for node in nodes
        ):
            self._save_run(run, status=GraphRunStatus.COMPLETED, completed_at=utc_now())


def coding_workflow_definition(
    *,
    planner_role_id: str,
    explorer_role_ids: tuple[str, ...],
    coder_role_id: str,
    reviewer_role_id: str,
    main_role_id: str | None = None,
    max_parallel_explorers: int = 2,
    max_rework_rounds: int = 1,
) -> WorkflowDefinition:
    """Compatibility mapping for the frozen phase1e.v1 coding workflow."""
    if len(explorer_role_ids) > 4 or not 1 <= max_parallel_explorers <= 4:
        raise ValueError("coding workflow supports at most four explorers")
    if len(explorer_role_ids) != len(set(explorer_role_ids)):
        raise ValueError("explorer roles must be unique")
    if not 0 <= max_rework_rounds <= 3:
        raise ValueError("max_rework_rounds must be between 0 and 3")
    any_in = (PortSpec(name="in", required=False),)
    nodes: list[NodeSpec] = [
        NodeSpec(
            node_id="planner",
            node_kind=NodeKind.AGENT,
            agent_or_action_ref=planner_role_id,
            output_ports=(PortSpec(name="subtask", required=False),),
        ),
        NodeSpec(
            node_id="explorer_fanout",
            node_kind=NodeKind.FAN_OUT,
            input_ports=any_in,
            output_ports=(PortSpec(name="dispatched", required=False),),
            metadata={
                "role_ids": explorer_role_ids,
                "max_parallel": max_parallel_explorers,
                "readonly": True,
            },
        ),
    ]
    nodes.extend(
        NodeSpec(
            node_id=f"explorer_{index}",
            node_kind=NodeKind.AGENT,
            input_ports=any_in,
            output_ports=(PortSpec(name="subtask", required=False),),
            agent_or_action_ref=role_id,
            failure_policy=FailurePolicy.SKIP,
            metadata={"readonly": True, "legacy_role": "explorer", "role_id": role_id},
        )
        for index, role_id in enumerate(explorer_role_ids, start=1)
    )
    nodes.extend(
        [
            NodeSpec(
                node_id="explorer_join",
                node_kind=NodeKind.JOIN,
                input_ports=any_in,
                output_ports=(PortSpec(name="joined", required=False),),
            ),
            NodeSpec(
                node_id="coder",
                node_kind=NodeKind.AGENT,
                input_ports=any_in,
                output_ports=(PortSpec(name="subtask", required=False),),
                agent_or_action_ref=coder_role_id,
                idempotency_class=IdempotencyClass.NON_IDEMPOTENT,
                writes_workspace=True,
                failure_policy=FailurePolicy.MANUAL_RECONCILE,
            ),
            NodeSpec(
                node_id="reviewer",
                node_kind=NodeKind.AGENT,
                input_ports=any_in,
                output_ports=(PortSpec(name="subtask", required=False),),
                agent_or_action_ref=reviewer_role_id,
                metadata={"readonly": True, "verdicts": ["approved", "rework", "missing"]},
            ),
            NodeSpec(
                node_id="verdict",
                node_kind=NodeKind.CONDITION,
                input_ports=any_in,
                output_ports=(
                    PortSpec(name="approved", required=False),
                    PortSpec(name="rework", required=False),
                    PortSpec(name="missing", required=False),
                ),
            ),
            NodeSpec(
                node_id="rework_loop",
                node_kind=NodeKind.LOOP,
                input_ports=any_in,
                output_ports=(PortSpec(name="out", required=False),),
                loop_policy=LoopPolicy(
                    max_iterations=max(max_rework_rounds, 1),
                    max_wall_seconds=3_600,
                    max_output_tokens=1_000_000,
                    max_cost_usd=1_000,
                    max_subagents=0,
                    max_recursion_depth=1,
                    exit_expression="verdict != 'rework'",
                    on_limit_node_id="failed",
                ),
            ),
            NodeSpec(
                node_id="failed",
                node_kind=NodeKind.ARTIFACT,
                input_ports=any_in,
                output_ports=(PortSpec(name="out", required=False),),
            ),
        ]
    )
    if main_role_id is not None:
        nodes.append(
            NodeSpec(
                node_id="main",
                node_kind=NodeKind.AGENT,
                input_ports=any_in,
                output_ports=(PortSpec(name="out", required=False),),
                agent_or_action_ref=main_role_id,
                metadata={"readonly": True},
            )
        )
    else:
        nodes.append(
            NodeSpec(
                node_id="main",
                node_kind=NodeKind.ARTIFACT,
                input_ports=any_in,
                output_ports=(PortSpec(name="out", required=False),),
            )
        )
    explorer_edges = tuple(
        EdgeSpec(
            edge_id=f"fanout-explorer-{index}",
            source_node="explorer_fanout",
            source_port="dispatched",
            target_node=f"explorer_{index}",
            target_port="in",
        )
        for index, _role_id in enumerate(explorer_role_ids, start=1)
    ) + tuple(
        EdgeSpec(
            edge_id=f"explorer-{index}-join",
            source_node=f"explorer_{index}",
            source_port="subtask",
            target_node="explorer_join",
            target_port="in",
        )
        for index, _role_id in enumerate(explorer_role_ids, start=1)
    )
    if not explorer_role_ids:
        explorer_edges = (
            EdgeSpec(
                edge_id="fanout-join",
                source_node="explorer_fanout",
                source_port="dispatched",
                target_node="explorer_join",
                target_port="in",
            ),
        )
    edges = (
        EdgeSpec(
            edge_id="planner-fanout",
            source_node="planner",
            source_port="subtask",
            target_node="explorer_fanout",
            target_port="in",
        ),
        *explorer_edges,
        EdgeSpec(
            edge_id="join-coder",
            source_node="explorer_join",
            source_port="joined",
            target_node="coder",
            target_port="in",
        ),
        EdgeSpec(
            edge_id="coder-reviewer",
            source_node="coder",
            source_port="subtask",
            target_node="reviewer",
            target_port="in",
        ),
        EdgeSpec(
            edge_id="reviewer-verdict",
            source_node="reviewer",
            source_port="subtask",
            target_node="verdict",
            target_port="in",
        ),
        EdgeSpec(
            edge_id="approved-main",
            source_node="verdict",
            source_port="approved",
            target_node="main",
            target_port="in",
            condition="verdict == 'approved'",
        ),
        EdgeSpec(
            edge_id="rework-loop",
            source_node="verdict",
            source_port="rework",
            target_node="rework_loop",
            target_port="in",
            condition="verdict == 'rework'",
        ),
        EdgeSpec(
            edge_id="loop-coder",
            source_node="rework_loop",
            source_port="out",
            target_node="coder",
            target_port="in",
            loop_back=True,
        ),
        EdgeSpec(
            edge_id="loop-limit-failed",
            source_node="rework_loop",
            source_port="out",
            target_node="failed",
            target_port="in",
            condition="limit_reasons",
            join_mode=JoinMode.ANY,
        ),
        EdgeSpec(
            edge_id="missing-failed",
            source_node="verdict",
            source_port="missing",
            target_node="failed",
            target_port="in",
            condition="verdict == 'missing'",
            join_mode=JoinMode.ANY,
        ),
    )
    return WorkflowDefinition(
        workflow_id="builtin.coding-review",
        version=1,
        name="coding-review",
        description="Frozen phase1e.v1 compatibility graph",
        nodes=tuple(nodes),
        edges=edges,
        graph_limits=GraphLimits(
            max_parallel_nodes=max_parallel_explorers + 1,
            max_subagents=len(explorer_role_ids),
            max_recursion_depth=1,
        ),
        locked_role_versions={
            role_id: 1
            for role_id in (
                planner_role_id,
                *explorer_role_ids,
                coder_role_id,
                reviewer_role_id,
                *((main_role_id,) if main_role_id else ()),
            )
        },
        status=WorkflowDefinitionStatus.PUBLISHED,
    )
