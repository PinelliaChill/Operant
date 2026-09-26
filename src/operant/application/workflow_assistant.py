"""Bounded, review-first Workflow suggestions from an existing ModelProfile."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from operant.application.graph import GraphCompilationError, GraphCompiler
from operant.domain.graph import NodeKind, WorkflowDefinition, WorkflowDefinitionStatus
from operant.domain.messages import Message, MessageRole
from operant.domain.models import Budget, RoleSnapshot, RoleStatus, ToolPolicy
from operant.domain.team import TeamDefinition
from operant.protocol import redact_public_text


class WorkflowSuggestionError(ValueError):
    """A model proposal cannot be presented as a usable Workflow draft."""


class WorkflowSuggestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instruction: str = Field(min_length=1, max_length=8_000)
    team_id: str = Field(min_length=1, max_length=300)
    team_version: int = Field(ge=1)
    base_workflow_id: str | None = Field(default=None, max_length=300)
    base_version: int | None = Field(default=None, ge=1)


class WorkflowSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    definition: WorkflowDefinition
    changes: tuple[str, ...]
    model_id: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    input_redacted: bool = False


_SUPPORTED_KINDS = frozenset(
    {
        NodeKind.AGENT,
        NodeKind.TOOL,
        NodeKind.SCRIPT,
        NodeKind.CONDITION,
        NodeKind.FAN_OUT,
        NodeKind.JOIN,
        NodeKind.LOOP,
        NodeKind.TIMER,
    }
)
_SYSTEM = """You design an Operant Workflow draft. Return a single JSON object only.
Keys: name, description, nodes, edges. Do not add Markdown or commentary.
Use only the provided Team Agent member ids, exactly once each, as agent nodes.
Each agent has metadata role_id and role_version from the supplied roster.
Allowed node kinds: agent, tool, script, condition, fan_out, join, loop, timer.
Tool metadata: tool_name, arguments, role_id, role_version.
Script metadata: argv (array), cwd, timeout_seconds, role_id, role_version.
Condition metadata: expression; use true/false output ports.
Timer metadata: delay_seconds. Loop needs a bounded loop_policy.
Every edge refers to declared source/output and target/input ports.
Use JSON-compatible values. Keep the graph small. Never include secrets.
The draft will only be saved and run after human confirmation."""


def _snapshot(role: Any, profile: Any) -> RoleSnapshot:
    if role.status is not RoleStatus.ACTIVE or not profile.enabled:
        raise WorkflowSuggestionError("coordinator role or model profile is inactive")
    if role.effort not in profile.supported_efforts:
        raise WorkflowSuggestionError("coordinator effort is unsupported by its model profile")
    limit = min(role.budget.max_output_tokens or 4096, 4096)
    return RoleSnapshot(
        role_id=role.id,
        role_version=role.version,
        role_name=role.name,
        system_prompt=_SYSTEM,
        model_profile_id=profile.id,
        model_profile_name=profile.name,
        provider=profile.provider,
        model_id=profile.model_id,
        base_url=profile.base_url,
        secret_ref=profile.secret_ref,
        context_window=profile.context_window,
        input_usd_per_million_tokens=profile.input_usd_per_million_tokens,
        output_usd_per_million_tokens=profile.output_usd_per_million_tokens,
        effort=role.effort,
        provider_effort_parameter=profile.effort_parameter,
        provider_effort_value=profile.provider_effort_value(role.effort),
        tool_policy=ToolPolicy(),
        budget=Budget(max_turns=1, timeout_seconds=90, max_output_tokens=limit, max_tool_calls=0),
        memory_scope="session",
    )


def _changes(base: WorkflowDefinition | None, draft: WorkflowDefinition) -> tuple[str, ...]:
    if base is None:
        return (f"新建 {len(draft.nodes)} 个节点、{len(draft.edges)} 条连线",)
    old_nodes = {node.node_id: node for node in base.nodes}
    new_nodes = {node.node_id: node for node in draft.nodes}
    old_edges = {edge.edge_id: edge for edge in base.edges}
    new_edges = {edge.edge_id: edge for edge in draft.edges}
    modified = sorted(
        key for key in old_nodes.keys() & new_nodes.keys() if old_nodes[key] != new_nodes[key]
    )
    result = [
        f"工作流名称：{'已修改' if base.name != draft.name else '未修改'}",
        f"描述：{'已修改' if base.description != draft.description else '未修改'}",
        f"新增节点：{', '.join(sorted(new_nodes.keys() - old_nodes.keys())) or '无'}",
        f"删除节点：{', '.join(sorted(old_nodes.keys() - new_nodes.keys())) or '无'}",
        f"修改节点：{', '.join(modified) or '无'}",
        f"新增连线：{', '.join(sorted(new_edges.keys() - old_edges.keys())) or '无'}",
        f"删除连线：{', '.join(sorted(old_edges.keys() - new_edges.keys())) or '无'}",
    ]
    return tuple(result)


async def suggest_workflow(
    *,
    request: WorkflowSuggestionRequest,
    service: Any,
    team: TeamDefinition,
    base: WorkflowDefinition | None,
    next_version: int,
) -> WorkflowSuggestion:
    """Return a validated candidate without writing a Definition or starting a Run."""
    coordinator = next(
        (member for member in team.members if member.member_id == team.default_coordinator),
        None,
    )
    if coordinator is None:
        raise WorkflowSuggestionError("Team has no coordinator")
    role = service.get_role(coordinator.agent_definition_id)
    profile = service.get_model_profile(role.model_profile_id)
    snapshot = _snapshot(role, profile)
    roster = []
    for member in team.members:
        member_role = service.get_role(member.agent_definition_id)
        if member_role.status is not RoleStatus.ACTIVE:
            raise WorkflowSuggestionError("Team contains an inactive role")
        roster.append(
            {
                "node_id": member.member_id,
                "role_id": member_role.id,
                "role_version": member_role.version,
            }
        )
    safe_instruction = redact_public_text(request.instruction, max_chars=8_000)
    base_context = None
    base_redacted = False
    if base is not None:
        safe_name = redact_public_text(base.name, max_chars=200)
        safe_description = redact_public_text(base.description, max_chars=1_000)
        safe_edges: list[dict[str, Any]] = [
            {
                **edge.model_dump(mode="json"),
                "condition": (
                    redact_public_text(edge.condition, max_chars=2_000)
                    if edge.condition is not None
                    else None
                ),
            }
            for edge in base.edges
        ]
        base_context = {
            "name": safe_name,
            "description": safe_description,
            "nodes": [
                {
                    "node_id": node.node_id,
                    "node_kind": node.node_kind.value,
                    "input_ports": [port.model_dump(mode="json") for port in node.input_ports],
                    "output_ports": [port.model_dump(mode="json") for port in node.output_ports],
                }
                for node in base.nodes
            ],
            "edges": safe_edges,
        }
        base_redacted = (
            safe_name != base.name
            or safe_description != base.description
            or any(
                item["condition"] != edge.condition
                for item, edge in zip(safe_edges, base.edges, strict=True)
            )
        )
    context = {
        "instruction": safe_instruction,
        "team": roster,
        "base_definition": base_context,
    }
    messages = (
        Message(role=MessageRole.SYSTEM, content=_SYSTEM),
        Message(role=MessageRole.USER, content=json.dumps(context, ensure_ascii=False)),
    )

    async def collect() -> tuple[str | None, int | None, int | None]:
        content: str | None = None
        prompt_tokens: int | None = None
        completion_tokens: int | None = None
        async for event in service.provider.stream(snapshot=snapshot, messages=messages, tools=()):
            if event.event_type != "model.completed" or event.response is None:
                continue
            content = event.response.content
            if event.response.tool_calls:
                raise WorkflowSuggestionError("model returned a tool call instead of a Workflow")
            usage = event.response.usage
            if usage is not None:
                prompt_tokens = usage.prompt_tokens
                completion_tokens = usage.completion_tokens
        return content, prompt_tokens, completion_tokens

    try:
        content, prompt_tokens, completion_tokens = await asyncio.wait_for(collect(), timeout=90)
    except asyncio.TimeoutError as exc:
        raise WorkflowSuggestionError("Workflow suggestion timed out") from exc
    if not content or len(content) > 70_000:
        raise WorkflowSuggestionError("model returned an empty or oversized Workflow")
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise WorkflowSuggestionError("model did not return a JSON Workflow") from exc
    if not isinstance(payload, dict) or set(payload) != {"name", "description", "nodes", "edges"}:
        raise WorkflowSuggestionError("model returned an unexpected Workflow shape")
    if not isinstance(payload["nodes"], list) or len(payload["nodes"]) > 32:
        raise WorkflowSuggestionError("model returned too many nodes")
    if not isinstance(payload["edges"], list) or len(payload["edges"]) > 96:
        raise WorkflowSuggestionError("model returned too many edges")
    agent_ids = {member["node_id"] for member in roster}
    roles = {member["node_id"]: member for member in roster}
    role_versions = {member["role_id"]: member["role_version"] for member in roster}
    for node in payload["nodes"]:
        if not isinstance(node, dict):
            raise WorkflowSuggestionError("model returned an invalid node")
        metadata = node.get("metadata")
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict):
            raise WorkflowSuggestionError("model returned invalid node metadata")
        for key in ("name", "label", "description"):
            if key in node:
                value = node.pop(key)
                if not isinstance(value, str):
                    raise WorkflowSuggestionError(f"node {key} must be text")
                metadata["label" if key == "name" else key] = value
        for key in (
            "role_id",
            "role_version",
            "task",
            "tool_name",
            "arguments",
            "argv",
            "cwd",
            "timeout_seconds",
            "expression",
            "delay_seconds",
            "outputs",
        ):
            if key in node:
                metadata[key] = node.pop(key)
        node["metadata"] = metadata
        if node.get("node_kind") == NodeKind.AGENT.value:
            binding = roles.get(node.get("node_id"))
            if binding is None:
                raise WorkflowSuggestionError("model invented an Agent outside the Team")
            node["metadata"] = {
                **metadata,
                "role_id": binding["role_id"],
                "role_version": binding["role_version"],
            }
            agent_role = service.get_role(binding["role_id"], binding["role_version"])
            writes = bool(
                set(agent_role.tool_policy.allowed_tools)
                - {"read_file", "search_files", "git_diff"}
            )
            node["writes_workspace"] = writes
            node["idempotency_class"] = "non_idempotent" if writes else "pure"
        elif node.get("node_kind") in {NodeKind.TOOL.value, NodeKind.SCRIPT.value}:
            if metadata.get("role_id") not in role_versions:
                raise WorkflowSuggestionError("action node must use a Team role")
            metadata["role_version"] = role_versions[metadata["role_id"]]
    if {
        node.get("node_id")
        for node in payload["nodes"]
        if node.get("node_kind") == NodeKind.AGENT.value
    } != agent_ids:
        raise WorkflowSuggestionError("Workflow must include every Team Agent exactly once")
    source = base.model_dump(mode="json") if base is not None else {}
    source.update(payload)
    source.update(
        workflow_id=base.workflow_id if base is not None else None,
        version=next_version,
        status=WorkflowDefinitionStatus.DRAFT,
        default_policy={
            "b24_executor": True,
            "team_id": team.team_id,
            "team_version": team.version,
        },
        locked_role_versions=role_versions,
        created_at=datetime.now(timezone.utc),
    )
    if source["workflow_id"] is None:
        source.pop("workflow_id")
    try:
        draft = WorkflowDefinition.model_validate(source)
        if any(node.node_kind not in _SUPPORTED_KINDS for node in draft.nodes):
            raise WorkflowSuggestionError("model proposed a node kind without an executor")
        GraphCompiler().compile(draft)
    except ValidationError as exc:
        locations = ", ".join(".".join(map(str, issue["loc"])) for issue in exc.errors()[:5])
        raise WorkflowSuggestionError(
            f"suggested Workflow has invalid fields: {locations}"
        ) from exc
    except GraphCompilationError as exc:
        codes = ", ".join(issue.code for issue in exc.issues[:5])
        raise WorkflowSuggestionError(
            f"suggested Workflow failed Compiler checks: {codes}"
        ) from exc
    return WorkflowSuggestion(
        definition=draft,
        changes=_changes(base, draft),
        model_id=snapshot.model_id,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        input_redacted=safe_instruction != request.instruction or base_redacted,
    )
