from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from operant.application.service import ApplicationService


class WorkflowEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: str
    session_id: str
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)


@dataclass
class RoleRunCapture:
    final_content: str = ""


class SequentialCodingWorkflow:
    """Planner → Coder → Reviewer orchestration with isolated sessions."""

    def __init__(self, service: ApplicationService) -> None:
        self.service = service

    async def run(
        self,
        *,
        task: str,
        workspace: str | Path,
        planner_role_id: str,
        coder_role_id: str,
        reviewer_role_id: str,
    ) -> AsyncIterator[WorkflowEvent]:
        planner = RoleRunCapture()
        async for event in self._stream_role(
            role="planner",
            role_id=planner_role_id,
            message=task,
            workspace=workspace,
            capture=planner,
        ):
            yield event

        coder = RoleRunCapture()
        coder_message = (
            f"原始任务：\n{task}\n\nPlanner 输出：\n{planner.final_content}\n\n"
            "请在 workspace 中完成任务，运行相关测试并检查 Git diff。"
        )
        async for event in self._stream_role(
            role="coder",
            role_id=coder_role_id,
            message=coder_message,
            workspace=workspace,
            capture=coder,
        ):
            yield event

        reviewer = RoleRunCapture()
        reviewer_message = (
            f"原始任务：\n{task}\n\nPlanner 输出：\n{planner.final_content}\n\n"
            f"Coder 输出：\n{coder.final_content}\n\n"
            "请只读检查 workspace 的 Git diff 和测试结果，不要修改文件。"
        )
        async for event in self._stream_role(
            role="reviewer",
            role_id=reviewer_role_id,
            message=reviewer_message,
            workspace=workspace,
            capture=reviewer,
        ):
            yield event

    async def _stream_role(
        self,
        *,
        role: str,
        role_id: str,
        message: str,
        workspace: str | Path,
        capture: RoleRunCapture,
    ) -> AsyncGenerator[WorkflowEvent, None]:
        session = self.service.create_session(role_id)
        async for event in self.service.run_session(
            session.id,
            user_message=message,
            workspace=workspace,
        ):
            if event.event_type == "agent.completed":
                capture.final_content = str(event.payload.get("content", ""))
            yield WorkflowEvent(
                role=role,
                session_id=session.id,
                event_type=event.event_type,
                payload=event.payload,
            )
