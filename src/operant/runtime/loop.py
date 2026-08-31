from __future__ import annotations

import json
import time
from collections.abc import AsyncGenerator, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from operant.domain.context import ContextRevision
from operant.domain.messages import Message, MessageRole, ModelResponse, ToolDefinition
from operant.domain.models import RoleSnapshot
from operant.protocol import redact_public_text
from operant.providers.base import ModelProvider
from operant.runtime.feedback import NoProgressDetector, test_failure_feedback
from operant.tools.workspace import (
    ApprovalCallback,
    ApprovalRequired,
    ToolError,
    WorkspaceTools,
)


class RuntimeEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_type: str
    turn: int = Field(ge=0)
    cursor: int | None = Field(default=None, ge=1)
    payload: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class ToolActionClaim:
    receipt_id: str
    action_hash: str
    replay_result: str | None = None
    replay_is_error: bool = False


class ActionGateway(Protocol):
    """Persistence-neutral contract used by the runtime around side effects."""

    def reserve_tool_action(
        self,
        *,
        tool_call_id: str,
        name: str,
        arguments: dict[str, Any],
    ) -> ToolActionClaim: ...

    def complete_tool_action(self, claim: ToolActionClaim, result: str) -> None: ...

    def fail_tool_action(
        self,
        claim: ToolActionClaim,
        *,
        error_code: str,
        result: str,
    ) -> None: ...

    def request_approval(
        self,
        claim: ToolActionClaim,
        *,
        tool_call_id: str,
        category: str,
        detail: str,
    ) -> Mapping[str, Any]: ...

    def verify_approval(
        self,
        claim: ToolActionClaim,
        *,
        tool_call_id: str,
        name: str,
        arguments: dict[str, Any],
    ) -> None: ...

    def verify_execution(self) -> None: ...


class ComposedContext(Protocol):
    @property
    def messages(self) -> Sequence[Message]: ...

    @property
    def tools(self) -> Sequence[ToolDefinition]: ...

    @property
    def revision(self) -> ContextRevision: ...


class ContextComposer(Protocol):
    def compose(
        self,
        *,
        snapshot: RoleSnapshot,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
        request_ordinal: int,
    ) -> ComposedContext: ...


class AgentLoop:
    def __init__(
        self,
        provider: ModelProvider,
        tools: WorkspaceTools,
        *,
        action_gateway: ActionGateway | None = None,
        context_composer: ContextComposer | None = None,
    ) -> None:
        self.provider = provider
        self.tools = tools
        self.action_gateway = action_gateway
        self.context_composer = context_composer

    async def run(
        self,
        *,
        snapshot: RoleSnapshot,
        user_message: str,
        approval_callback: ApprovalCallback | None = None,
    ) -> AsyncGenerator[RuntimeEvent, None]:
        run_started = time.monotonic()
        messages = [
            Message(role=MessageRole.SYSTEM, content=snapshot.system_prompt),
            Message(role=MessageRole.USER, content=user_message),
        ]
        yield RuntimeEvent(
            event_type="agent.started",
            turn=0,
            payload={
                "role_id": snapshot.role_id,
                "role_version": snapshot.role_version,
                "model_profile_id": snapshot.model_profile_id,
                "model_id": snapshot.model_id,
                "provider": snapshot.provider,
                "effort": snapshot.effort.value,
            },
        )
        no_progress = NoProgressDetector(snapshot.budget.max_consecutive_test_failures)
        completion_tokens_used = 0
        cost_usd_used = Decimal(0)
        tool_calls_used = 0

        if snapshot.budget.max_cost_usd is not None and (
            snapshot.input_usd_per_million_tokens is None
            or snapshot.output_usd_per_million_tokens is None
        ):
            yield self._budget_exhausted(
                turn=0,
                kind="cost",
                reason="pricing_unknown",
                limit=snapshot.budget.max_cost_usd,
                observed=None,
            )
            return

        for turn in range(1, snapshot.budget.max_turns + 1):
            remaining_output_tokens = None
            if snapshot.budget.max_output_tokens is not None:
                remaining_output_tokens = snapshot.budget.max_output_tokens - completion_tokens_used
                if remaining_output_tokens <= 0:
                    yield self._budget_exhausted(
                        turn=turn,
                        kind="output_tokens",
                        reason="limit_reached",
                        limit=snapshot.budget.max_output_tokens,
                        observed=completion_tokens_used,
                    )
                    return
            request_snapshot = snapshot
            if remaining_output_tokens is not None:
                request_snapshot = snapshot.model_copy(
                    update={
                        "budget": snapshot.budget.model_copy(
                            update={"max_output_tokens": remaining_output_tokens}
                        )
                    }
                )
            tool_definitions = self.tools.definitions()
            request_messages: Sequence[Message] = messages
            request_tools: Sequence[ToolDefinition] = tool_definitions
            context_revision_id: str | None = None
            if self.context_composer is not None:
                composed = self.context_composer.compose(
                    snapshot=request_snapshot,
                    messages=messages,
                    tools=tool_definitions,
                    request_ordinal=turn,
                )
                request_messages = composed.messages
                request_tools = composed.tools
                context_revision_id = composed.revision.id
            model_started = time.monotonic()
            completed: ModelResponse | None = None
            async for event in self.provider.stream(
                snapshot=request_snapshot,
                messages=request_messages,
                tools=request_tools,
            ):
                if event.event_type == "model.delta":
                    yield RuntimeEvent(
                        event_type="model.delta",
                        turn=turn,
                        payload={"delta": event.delta or ""},
                    )
                elif event.event_type == "model.completed":
                    completed = event.response

            if completed is None:
                raise RuntimeError("provider stream ended without a completed response")
            messages.append(
                Message(
                    role=MessageRole.ASSISTANT,
                    content=completed.content,
                    tool_calls=completed.tool_calls,
                )
            )
            completed_payload: dict[str, Any] = {
                "content": completed.content,
                "finish_reason": completed.finish_reason,
                "tool_calls": [call.model_dump() for call in completed.tool_calls],
                "usage": (
                    None if completed.usage is None else completed.usage.model_dump(mode="json")
                ),
                "duration_ms": self._elapsed_ms(model_started),
                "provider_request_id": completed.provider_request_id,
            }
            if context_revision_id is not None:
                completed_payload["context_revision_id"] = context_revision_id
            yield RuntimeEvent(
                event_type="model.completed",
                turn=turn,
                payload=completed_payload,
            )

            usage = completed.usage
            if snapshot.budget.max_output_tokens is not None:
                if usage is None or usage.completion_tokens is None:
                    yield self._budget_exhausted(
                        turn=turn,
                        kind="output_tokens",
                        reason="usage_unknown",
                        limit=snapshot.budget.max_output_tokens,
                        observed=None,
                    )
                    return
                completion_tokens_used += usage.completion_tokens
                if completion_tokens_used > snapshot.budget.max_output_tokens or (
                    completion_tokens_used == snapshot.budget.max_output_tokens
                    and completed.tool_calls
                ):
                    yield self._budget_exhausted(
                        turn=turn,
                        kind="output_tokens",
                        reason="limit_reached",
                        limit=snapshot.budget.max_output_tokens,
                        observed=completion_tokens_used,
                    )
                    return

            if snapshot.budget.max_cost_usd is not None:
                if usage is None or usage.prompt_tokens is None or usage.completion_tokens is None:
                    yield self._budget_exhausted(
                        turn=turn,
                        kind="cost",
                        reason="usage_unknown",
                        limit=snapshot.budget.max_cost_usd,
                        observed=None,
                    )
                    return
                assert snapshot.input_usd_per_million_tokens is not None
                assert snapshot.output_usd_per_million_tokens is not None
                cost_limit_usd = Decimal(str(snapshot.budget.max_cost_usd))
                cost_usd_used += (
                    Decimal(usage.prompt_tokens)
                    * Decimal(str(snapshot.input_usd_per_million_tokens))
                    + Decimal(usage.completion_tokens)
                    * Decimal(str(snapshot.output_usd_per_million_tokens))
                ) / Decimal(1_000_000)
                if cost_usd_used > cost_limit_usd or (
                    cost_usd_used >= cost_limit_usd and completed.tool_calls
                ):
                    yield self._budget_exhausted(
                        turn=turn,
                        kind="cost",
                        reason="limit_reached",
                        limit=snapshot.budget.max_cost_usd,
                        observed=float(cost_usd_used),
                    )
                    return

            if not completed.tool_calls:
                yield RuntimeEvent(
                    event_type="agent.completed",
                    turn=turn,
                    payload={
                        "content": completed.content or "",
                        "duration_ms": self._elapsed_ms(run_started),
                    },
                )
                return

            for call in completed.tool_calls:
                if (
                    snapshot.budget.max_tool_calls is not None
                    and tool_calls_used >= snapshot.budget.max_tool_calls
                ):
                    yield self._budget_exhausted(
                        turn=turn,
                        kind="tool_calls",
                        reason="limit_reached",
                        limit=snapshot.budget.max_tool_calls,
                        observed=tool_calls_used,
                    )
                    return
                tool_calls_used += 1
                tool_started = time.monotonic()
                arguments: dict[str, Any] = {}
                claim: ToolActionClaim | None = None
                action_gateway = self.action_gateway
                yield RuntimeEvent(
                    event_type="tool.started",
                    turn=turn,
                    payload={"tool_call_id": call.id, "name": call.name},
                )
                try:
                    arguments = call.arguments()
                    if action_gateway is not None and self.tools.is_side_effecting(call.name):
                        claim = action_gateway.reserve_tool_action(
                            tool_call_id=call.id,
                            name=call.name,
                            arguments=arguments,
                        )
                    if claim is not None and claim.replay_result is not None:
                        result = self._safe_tool_result(claim.replay_result)
                        is_error = claim.replay_is_error
                        event_type = "tool.failed" if is_error else "tool.completed"
                    else:
                        if claim is not None:
                            assert action_gateway is not None
                            verify_execution = getattr(action_gateway, "verify_execution", None)
                            if verify_execution is not None:
                                verify_execution()
                        result = self._safe_tool_result(
                            await self.tools.execute(call.name, arguments)
                        )
                        event_type = "tool.completed"
                        is_error = False
                        if claim is not None:
                            assert action_gateway is not None
                            action_gateway.complete_tool_action(claim, result)
                except ApprovalRequired as exc:
                    safe_approval_detail = redact_public_text(exc.detail, max_chars=500)
                    approval_payload: Mapping[str, Any] = {}
                    if claim is not None:
                        assert action_gateway is not None
                        approval_payload = action_gateway.request_approval(
                            claim,
                            tool_call_id=call.id,
                            category=exc.category,
                            detail=safe_approval_detail,
                        )
                    yield RuntimeEvent(
                        event_type="tool.approval_required",
                        turn=turn,
                        payload={
                            "tool_call_id": call.id,
                            "name": call.name,
                            "category": exc.category,
                            "detail": safe_approval_detail,
                            **approval_payload,
                        },
                    )
                    approved = (
                        False
                        if approval_callback is None
                        else await approval_callback(call.id, exc.category, safe_approval_detail)
                    )
                    yield RuntimeEvent(
                        event_type="tool.approval_decided",
                        turn=turn,
                        payload={
                            "tool_call_id": call.id,
                            "name": call.name,
                            "category": exc.category,
                            "approved": approved,
                        },
                    )
                    if approved:
                        try:
                            if claim is not None:
                                assert action_gateway is not None
                                action_gateway.verify_approval(
                                    claim,
                                    tool_call_id=call.id,
                                    name=call.name,
                                    arguments=arguments,
                                )
                                verify_execution = getattr(action_gateway, "verify_execution", None)
                                if verify_execution is not None:
                                    verify_execution()
                            result = self._safe_tool_result(
                                await self.tools.execute(
                                    call.name,
                                    arguments,
                                    approved_categories=frozenset({exc.category}),
                                )
                            )
                            event_type = "tool.completed"
                            is_error = False
                            if claim is not None:
                                assert action_gateway is not None
                                action_gateway.complete_tool_action(claim, result)
                        except (ToolError, ValueError, json.JSONDecodeError) as retry_exc:
                            result = self._safe_failure_result(retry_exc)
                            event_type = "tool.failed"
                            is_error = True
                            if claim is not None:
                                assert action_gateway is not None
                                action_gateway.fail_tool_action(
                                    claim,
                                    error_code=type(retry_exc).__name__,
                                    result=result,
                                )
                    else:
                        result = json.dumps(
                            {
                                "error": "approval_denied",
                                "category": exc.category,
                                "detail": safe_approval_detail,
                            }
                        )
                        event_type = "tool.failed"
                        is_error = True
                        if claim is not None:
                            assert action_gateway is not None
                            action_gateway.fail_tool_action(
                                claim,
                                error_code="approval_denied",
                                result=result,
                            )
                except (ToolError, ValueError, json.JSONDecodeError) as exc:
                    result = self._safe_failure_result(exc)
                    event_type = "tool.failed"
                    is_error = True
                    if claim is not None:
                        assert action_gateway is not None
                        action_gateway.fail_tool_action(
                            claim,
                            error_code=type(exc).__name__,
                            result=result,
                        )
                feedback: dict[str, Any] | None = None
                result = self._safe_tool_result(result)
                if not is_error and call.name == "run_command":
                    raw_argv = arguments.get("argv")
                    result, feedback = self._attach_test_failure_feedback(
                        result,
                        argv=(
                            raw_argv
                            if isinstance(raw_argv, list)
                            and all(isinstance(item, str) for item in raw_argv)
                            else None
                        ),
                    )
                messages.append(
                    Message(
                        role=MessageRole.TOOL,
                        content=result,
                        tool_call_id=call.id,
                        name=call.name,
                    )
                )
                yield RuntimeEvent(
                    event_type=event_type,
                    turn=turn,
                    payload={
                        "tool_call_id": call.id,
                        "name": call.name,
                        "result": result,
                        "is_error": is_error,
                        "duration_ms": self._elapsed_ms(tool_started),
                    },
                )
                if feedback is not None:
                    yield RuntimeEvent(
                        event_type="test.failure_feedback",
                        turn=turn,
                        payload=feedback,
                    )
                    if no_progress.observe(feedback):
                        yield RuntimeEvent(
                            event_type="agent.no_progress",
                            turn=turn,
                            payload={
                                "reason": "repeated_test_failure",
                                "signature": feedback["signature"],
                                "consecutive_failures": no_progress.count,
                                "max_consecutive_test_failures": (
                                    snapshot.budget.max_consecutive_test_failures
                                ),
                                "duration_ms": self._elapsed_ms(run_started),
                            },
                        )
                        return

        yield RuntimeEvent(
            event_type="agent.max_turns",
            turn=snapshot.budget.max_turns,
            payload={
                "max_turns": snapshot.budget.max_turns,
                "duration_ms": self._elapsed_ms(run_started),
            },
        )

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return max(0, round((time.monotonic() - started) * 1000))

    @staticmethod
    def _budget_exhausted(
        *,
        turn: int,
        kind: str,
        reason: str,
        limit: int | float,
        observed: int | float | None,
    ) -> RuntimeEvent:
        return RuntimeEvent(
            event_type="budget.exhausted",
            turn=turn,
            payload={
                "kind": kind,
                "reason": reason,
                "limit": limit,
                "observed": observed,
                "usage_state": "unknown" if observed is None else "known",
            },
        )

    @staticmethod
    def _safe_tool_result(result: str) -> str:
        return redact_public_text(result)

    @staticmethod
    def _safe_failure_result(exc: Exception) -> str:
        return json.dumps(
            {
                "error": type(exc).__name__,
                "message": redact_public_text(str(exc)),
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _attach_test_failure_feedback(
        result: str,
        *,
        argv: list[str] | None = None,
    ) -> tuple[str, dict[str, Any] | None]:
        try:
            payload = json.loads(result)
        except json.JSONDecodeError:
            return result, None
        if not isinstance(payload, dict):
            return result, None
        feedback = test_failure_feedback(payload, argv=argv)
        if feedback is None:
            return result, None
        payload["test_failure"] = feedback
        return json.dumps(payload, ensure_ascii=False), feedback
