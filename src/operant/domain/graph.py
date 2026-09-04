from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from operant.domain.models import Budget
from operant.domain.multiwriter import MergeNodePolicy, WriterNodePolicy


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_graph_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class WorkflowDefinitionStatus(str, Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    DEPRECATED = "deprecated"


class NodeKind(str, Enum):
    AGENT = "agent"
    TOOL = "tool"
    SCRIPT = "script"
    CONDITION = "condition"
    FAN_OUT = "fan_out"
    JOIN = "join"
    LOOP = "loop"
    HUMAN_INPUT = "human_input"
    APPROVAL = "approval"
    WAIT = "wait"
    TIMER = "timer"
    SUBWORKFLOW = "subworkflow"
    ARTIFACT = "artifact"
    MERGE = "merge"


class IdempotencyClass(str, Enum):
    PURE = "pure"
    IDEMPOTENT = "idempotent"
    NON_IDEMPOTENT = "non_idempotent"


class FailurePolicy(str, Enum):
    FAIL_WORKFLOW = "fail_workflow"
    SKIP = "skip"
    MANUAL_RECONCILE = "manual_reconcile"


class JoinMode(str, Enum):
    ALL = "all"
    ANY = "any"


class DeliveryMode(str, Enum):
    VALUE = "value"
    REFERENCE = "reference"


class GraphRunStatus(str, Enum):
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    WAITING_APPROVAL = "waiting_approval"
    INTERRUPTED = "interrupted"
    MANUAL_RECONCILE_REQUIRED = "manual_reconcile_required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class NodeRunStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_INPUT = "waiting_input"
    WAITING = "waiting"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    MANUAL_RECONCILE_REQUIRED = "manual_reconcile_required"


class AttemptSideEffectState(str, Enum):
    NONE = "none"
    NOT_STARTED = "not_started"
    STARTED = "started"
    COMMITTED = "committed"
    UNKNOWN = "unknown"


class AttemptResult(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class PortSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=100)
    value_type: str = Field(default="any", min_length=1, max_length=200)
    required: bool = True


class RetryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_attempts: int = Field(default=1, ge=1, le=20)
    delay_seconds: float = Field(default=0, ge=0, le=86_400)


class TimeoutPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    timeout_seconds: int = Field(default=300, ge=1, le=86_400)
    on_timeout_node_id: str | None = None


class LoopPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_iterations: int = Field(ge=1, le=1_000)
    max_wall_seconds: int = Field(ge=1, le=86_400)
    max_output_tokens: int = Field(ge=1)
    max_cost_usd: float = Field(gt=0)
    max_subagents: int = Field(ge=0, le=100)
    max_recursion_depth: int = Field(ge=1, le=32)
    max_no_progress_repeats: int = Field(default=2, ge=1, le=20)
    exit_expression: str = Field(min_length=1, max_length=2_000)
    on_limit_node_id: str
    on_manual_reconcile_node_id: str | None = None


class GraphLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_nodes: int = Field(default=256, ge=1, le=10_000)
    max_edges: int = Field(default=1_024, ge=0, le=50_000)
    max_parallel_nodes: int = Field(default=8, ge=1, le=256)
    max_subagents: int = Field(default=8, ge=0, le=256)
    max_recursion_depth: int = Field(default=4, ge=1, le=32)


class NodeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str = Field(min_length=1, max_length=200)
    node_kind: NodeKind
    input_ports: tuple[PortSpec, ...] = ()
    output_ports: tuple[PortSpec, ...] = ()
    agent_or_action_ref: str | None = None
    model_override: str | None = None
    context_policy: dict[str, Any] = Field(default_factory=dict)
    capability_requirements: tuple[str, ...] = ()
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    timeout_policy: TimeoutPolicy | None = None
    budget: Budget | None = None
    idempotency_class: IdempotencyClass = IdempotencyClass.PURE
    workspace_or_target: str | None = None
    failure_policy: FailurePolicy = FailurePolicy.FAIL_WORKFLOW
    writes_workspace: bool = False
    writer_policy: WriterNodePolicy | None = None
    merge_policy: MergeNodePolicy | None = None
    loop_policy: LoopPolicy | None = None
    subworkflow_id: str | None = None
    subworkflow_version: int | None = Field(default=None, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_kind_specific_fields(self) -> NodeSpec:
        if len({port.name for port in self.input_ports}) != len(self.input_ports):
            raise ValueError("input port names must be unique")
        if len({port.name for port in self.output_ports}) != len(self.output_ports):
            raise ValueError("output port names must be unique")
        if self.node_kind is NodeKind.LOOP and self.loop_policy is None:
            raise ValueError("loop nodes require a loop_policy")
        if self.node_kind is not NodeKind.LOOP and self.loop_policy is not None:
            raise ValueError("loop_policy is only valid for loop nodes")
        if self.node_kind is NodeKind.SUBWORKFLOW:
            if self.subworkflow_id is None or self.subworkflow_version is None:
                raise ValueError("subworkflow nodes must pin an id and version")
        elif self.subworkflow_id is not None or self.subworkflow_version is not None:
            raise ValueError("subworkflow fields are only valid for subworkflow nodes")
        if self.writer_policy is not None and not self.writes_workspace:
            raise ValueError("writer_policy requires writes_workspace")
        if self.node_kind is NodeKind.MERGE:
            if self.merge_policy is None or not self.writes_workspace:
                raise ValueError("merge nodes require merge_policy and writes_workspace")
        elif self.merge_policy is not None:
            raise ValueError("merge_policy is only valid for merge nodes")
        return self


class EdgeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    edge_id: str = Field(min_length=1, max_length=200)
    source_node: str
    source_port: str
    target_node: str
    target_port: str
    condition: str | None = Field(default=None, max_length=2_000)
    join_mode: JoinMode = JoinMode.ALL
    delivery_mode: DeliveryMode = DeliveryMode.REFERENCE
    loop_back: bool = False


class WorkflowDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_id: str = Field(default_factory=lambda: new_graph_id("definition"))
    version: int = Field(default=1, ge=1)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=10_000)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    nodes: tuple[NodeSpec, ...]
    edges: tuple[EdgeSpec, ...] = ()
    graph_limits: GraphLimits = Field(default_factory=GraphLimits)
    default_budget: Budget = Field(default_factory=Budget)
    default_policy: dict[str, Any] = Field(default_factory=dict)
    required_plugins: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    locked_role_versions: dict[str, int] = Field(default_factory=dict)
    locked_provider_versions: dict[str, str] = Field(default_factory=dict)
    status: WorkflowDefinitionStatus = WorkflowDefinitionStatus.DRAFT
    created_at: datetime = Field(default_factory=utc_now)


class GraphWorkflowRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_graph_id("graph_run"))
    workflow_definition_id: str
    workflow_definition_version: int = Field(ge=1)
    input: dict[str, Any] = Field(default_factory=dict)
    workspace_or_target: str | None = None
    team_run_id: str | None = None
    legacy_workflow_run_id: str | None = None
    status: GraphRunStatus = GraphRunStatus.CREATED
    budget_snapshot: Budget
    policy_snapshot: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    consumed_output_tokens: int = Field(default=0, ge=0)
    consumed_cost_usd: float = Field(default=0, ge=0)
    consumed_tool_calls: int = Field(default=0, ge=0)
    revision: int = Field(default=1, ge=1)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class NodeRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_graph_id("node_run"))
    workflow_run_id: str
    node_id: str
    status: NodeRunStatus = NodeRunStatus.PENDING
    input_refs: dict[str, Any] = Field(default_factory=dict)
    output_refs: dict[str, Any] = Field(default_factory=dict)
    active_attempt_id: str | None = None
    retry_count: int = Field(default=0, ge=0)
    iteration: int = Field(default=0, ge=0)
    no_progress_signature: str | None = None
    no_progress_repeats: int = Field(default=0, ge=0)
    worker_id: str | None = None
    lease_id: str | None = None
    wait_token: str | None = None
    revision: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class NodeAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: new_graph_id("attempt"))
    node_run_id: str
    attempt_number: int = Field(ge=1)
    agent_instance_id: str | None = None
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    result: AttemptResult = AttemptResult.RUNNING
    result_payload: dict[str, Any] = Field(default_factory=dict)
    failure_class: str | None = None
    side_effect_state: AttemptSideEffectState = AttemptSideEffectState.NONE
    idempotency_key: str | None = None

    @model_validator(mode="after")
    def require_idempotency_key_for_side_effect(self) -> NodeAttempt:
        if self.side_effect_state is not AttemptSideEffectState.NONE and not self.idempotency_key:
            raise ValueError("side-effecting attempts require an idempotency_key")
        return self


class BoundaryKind(str, Enum):
    APPROVAL = "approval"
    HUMAN_INPUT = "human_input"
    WAIT = "wait"
    SUBWORKFLOW = "subworkflow"


class BoundaryResolution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    wait_token: str
    accepted: bool = True
    payload: dict[str, Any] = Field(default_factory=dict)


class CompiledWorkflow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    definition: WorkflowDefinition
    entry_node_ids: tuple[str, ...]
    topological_node_ids: tuple[str, ...]
    terminal_node_ids: tuple[str, ...]
    incoming_edge_ids: dict[str, tuple[str, ...]]
    outgoing_edge_ids: dict[str, tuple[str, ...]]


class CompilationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str
    node_id: str | None = None
    edge_id: str | None = None
    severity: Literal["error"] = "error"
