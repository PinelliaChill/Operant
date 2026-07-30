from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from operant.domain.messages import Message, MessageRole, ModelResponse
from operant.domain.models import RoleSnapshot
from operant.providers.base import ModelProvider
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
    payload: dict[str, Any] = Field(default_factory=dict)


class AgentLoop:
    def __init__(self, provider: ModelProvider, tools: WorkspaceTools) -> None:
        self.provider = provider
        self.tools = tools

    async def run(
        self,
        *,
        snapshot: RoleSnapshot,
        user_message: str,
        approval_callback: ApprovalCallback | None = None,
    ) -> AsyncGenerator[RuntimeEvent, None]:
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
            },
        )

        for turn in range(1, snapshot.budget.max_turns + 1):
            completed: ModelResponse | None = None
            async for event in self.provider.stream(
                snapshot=snapshot,
                messages=messages,
                tools=self.tools.definitions(),
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
            yield RuntimeEvent(
                event_type="model.completed",
                turn=turn,
                payload={
                    "content": completed.content,
                    "finish_reason": completed.finish_reason,
                    "tool_calls": [call.model_dump() for call in completed.tool_calls],
                },
            )

            if not completed.tool_calls:
                yield RuntimeEvent(
                    event_type="agent.completed",
                    turn=turn,
                    payload={"content": completed.content or ""},
                )
                return

            for call in completed.tool_calls:
                yield RuntimeEvent(
                    event_type="tool.started",
                    turn=turn,
                    payload={"tool_call_id": call.id, "name": call.name},
                )
                try:
                    result = await self.tools.execute(call.name, call.arguments())
                    event_type = "tool.completed"
                    is_error = False
                except ApprovalRequired as exc:
                    yield RuntimeEvent(
                        event_type="tool.approval_required",
                        turn=turn,
                        payload={
                            "tool_call_id": call.id,
                            "name": call.name,
                            "category": exc.category,
                            "detail": exc.detail,
                        },
                    )
                    approved = (
                        False
                        if approval_callback is None
                        else await approval_callback(call.id, exc.category, exc.detail)
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
                            result = await self.tools.execute(
                                call.name,
                                call.arguments(),
                                approved_categories=frozenset({exc.category}),
                            )
                            event_type = "tool.completed"
                            is_error = False
                        except (ToolError, ValueError, json.JSONDecodeError) as retry_exc:
                            result = json.dumps(
                                {
                                    "error": type(retry_exc).__name__,
                                    "message": str(retry_exc),
                                }
                            )
                            event_type = "tool.failed"
                            is_error = True
                    else:
                        result = json.dumps(
                            {
                                "error": "approval_denied",
                                "category": exc.category,
                                "detail": exc.detail,
                            }
                        )
                        event_type = "tool.failed"
                        is_error = True
                except (ToolError, ValueError, json.JSONDecodeError) as exc:
                    result = json.dumps({"error": type(exc).__name__, "message": str(exc)})
                    event_type = "tool.failed"
                    is_error = True
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
                    },
                )

        yield RuntimeEvent(
            event_type="agent.max_turns",
            turn=snapshot.budget.max_turns,
            payload={"max_turns": snapshot.budget.max_turns},
        )
