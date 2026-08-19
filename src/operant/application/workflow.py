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
    session_id: str = ""


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
        max_rework_rounds: int = 1,
    ) -> AsyncIterator[WorkflowEvent]:
        if not 0 <= max_rework_rounds <= 3:
            raise ValueError("max_rework_rounds must be between 0 and 3")

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
        async for event in self._stream_role(
            role="reviewer",
            role_id=reviewer_role_id,
            message=self._reviewer_message(
                task=task,
                planner_output=planner.final_content,
                coder_output=coder.final_content,
            ),
            workspace=workspace,
            capture=reviewer,
        ):
            yield event

        verdict = self._review_verdict(reviewer.final_content)
        if verdict is None:
            yield WorkflowEvent(
                role="workflow",
                session_id=reviewer.session_id,
                event_type="workflow.review_verdict_missing",
                payload={"expected": "VERDICT: APPROVED or VERDICT: REWORK"},
            )
            return

        for round_number in range(1, max_rework_rounds + 1):
            if verdict != "REWORK":
                return

            yield WorkflowEvent(
                role="workflow",
                session_id=reviewer.session_id,
                event_type="workflow.rework_started",
                payload={"round": round_number, "max_rework_rounds": max_rework_rounds},
            )
            previous_coder_output = coder.final_content
            coder = RoleRunCapture()
            rework_message = (
                f"原始任务：\n{task}\n\nPlanner 输出：\n{planner.final_content}\n\n"
                f"上一轮 Coder 输出：\n{previous_coder_output}\n\n"
                f"Reviewer 反馈：\n{reviewer.final_content}\n\n"
                "请只处理 Reviewer 明确指出的问题，在 workspace 中完成返工，"
                "运行相关测试并检查 Git diff。"
            )
            async for event in self._stream_role(
                role="coder",
                role_id=coder_role_id,
                message=rework_message,
                workspace=workspace,
                capture=coder,
            ):
                yield event

            reviewer = RoleRunCapture()
            async for event in self._stream_role(
                role="reviewer",
                role_id=reviewer_role_id,
                message=self._reviewer_message(
                    task=task,
                    planner_output=planner.final_content,
                    coder_output=coder.final_content,
                ),
                workspace=workspace,
                capture=reviewer,
            ):
                yield event
            verdict = self._review_verdict(reviewer.final_content)
            if verdict is None:
                yield WorkflowEvent(
                    role="workflow",
                    session_id=reviewer.session_id,
                    event_type="workflow.review_verdict_missing",
                    payload={"expected": "VERDICT: APPROVED or VERDICT: REWORK"},
                )
                return

        if verdict == "REWORK":
            yield WorkflowEvent(
                role="workflow",
                session_id=reviewer.session_id,
                event_type="workflow.rework_limit_reached",
                payload={"max_rework_rounds": max_rework_rounds},
            )

    @staticmethod
    def _reviewer_message(*, task: str, planner_output: str, coder_output: str) -> str:
        return (
            f"原始任务：\n{task}\n\nPlanner 输出：\n{planner_output}\n\n"
            f"Coder 输出：\n{coder_output}\n\n"
            "请只读检查 workspace 的 Git diff 和测试结果，不要修改文件。"
            "最终必须单独以 `VERDICT: APPROVED` 或 `VERDICT: REWORK` 结束；"
            "只有确实需要 Coder 继续修改时才使用 REWORK。"
        )

    @staticmethod
    def _review_verdict(content: str) -> str | None:
        for line in reversed(content.splitlines()):
            normalized = line.strip().upper()
            if normalized == "VERDICT: APPROVED":
                return "APPROVED"
            if normalized == "VERDICT: REWORK":
                return "REWORK"
        return None

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
        capture.session_id = session.id
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
