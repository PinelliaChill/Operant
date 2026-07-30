from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from operant.application.defaults import default_role_presets
from operant.application.factory import AgentFactory
from operant.domain.models import (
    AgentStatus,
    Event,
    ModelProfile,
    RolePreset,
    Session,
)
from operant.persistence.sqlite import NotFoundError, SQLiteStore
from operant.providers.base import ModelProvider
from operant.runtime.loop import AgentLoop, RuntimeEvent
from operant.tools.workspace import ApprovalCallback, WorkspaceTools


class ApplicationService:
    """Use-case layer shared by CLI, API, and workflows."""

    def __init__(self, store: SQLiteStore, provider: ModelProvider) -> None:
        self.store = store
        self.provider = provider
        self.factory = AgentFactory(store)
        self._cancellations: dict[str, asyncio.Event] = {}
        self._approval_futures: dict[tuple[str, str], asyncio.Future[bool]] = {}
        self._approval_details: dict[tuple[str, str], dict[str, str]] = {}

    def initialize(self) -> None:
        self.store.initialize()

    # Model Registry

    def add_model_profile(self, profile: ModelProfile) -> ModelProfile:
        return self.store.add_model_profile(profile)

    def get_model_profile(self, profile_id: str) -> ModelProfile:
        return self.store.get_model_profile(profile_id)

    def list_model_profiles(self) -> list[ModelProfile]:
        return self.store.list_model_profiles()

    def update_model_profile(self, profile_id: str, **changes: Any) -> ModelProfile:
        return self.store.update_model_profile(profile_id, **changes)

    def deactivate_model_profile(self, profile_id: str) -> ModelProfile:
        return self.store.deactivate_model_profile(profile_id)

    async def discover_models(self, *, base_url: str, secret_ref: str) -> list[str]:
        return await self.provider.list_models(base_url=base_url, secret_ref=secret_ref)

    async def check_model_profile(self, profile_id: str) -> dict[str, object]:
        profile = self.get_model_profile(profile_id)
        model_ids = await self.discover_models(
            base_url=profile.base_url,
            secret_ref=profile.secret_ref,
        )
        return {
            "profile_id": profile.id,
            "model_id": profile.model_id,
            "available": profile.model_id in model_ids,
            "discovered_models": len(model_ids),
        }

    # Role Registry

    def create_role(self, role: RolePreset) -> RolePreset:
        return self.store.create_role(role)

    def get_role(self, role_id: str, version: int | None = None) -> RolePreset:
        return self.store.get_role(role_id, version)

    def list_roles(self, *, include_inactive: bool = False) -> list[RolePreset]:
        return self.store.list_roles(include_inactive=include_inactive)

    def list_role_versions(self, role_id: str) -> list[RolePreset]:
        return self.store.list_role_versions(role_id)

    def update_role(self, role_id: str, **changes: Any) -> RolePreset:
        return self.store.update_role(role_id, **changes)

    def copy_role(self, role_id: str, *, name: str) -> RolePreset:
        return self.store.copy_role(role_id, name=name)

    def deactivate_role(self, role_id: str) -> RolePreset:
        return self.store.deactivate_role(role_id)

    def seed_default_roles(
        self,
        *,
        planner_model_profile_id: str,
        coder_model_profile_id: str,
        reviewer_model_profile_id: str,
    ) -> list[RolePreset]:
        for profile_id in {
            planner_model_profile_id,
            coder_model_profile_id,
            reviewer_model_profile_id,
        }:
            profile = self.get_model_profile(profile_id)
            if not profile.enabled:
                raise ValueError(f"model profile is inactive: {profile_id}")

        seeded: list[RolePreset] = []
        for role in default_role_presets(
            planner_model_profile_id=planner_model_profile_id,
            coder_model_profile_id=coder_model_profile_id,
            reviewer_model_profile_id=reviewer_model_profile_id,
        ):
            try:
                seeded.append(self.get_role(role.id))
            except NotFoundError:
                seeded.append(self.create_role(role))
        return seeded

    # Session and Agent Factory

    def create_session(
        self,
        role_id: str | None = None,
        *,
        new_role: RolePreset | None = None,
        model_profile_id: str | None = None,
        effort: str | None = None,
        budget_overrides: dict[str, Any] | None = None,
    ) -> Session:
        if (role_id is None) == (new_role is None):
            raise ValueError("provide exactly one of role_id or new_role")
        if new_role is not None:
            role_id = self.create_role(new_role).id
        assert role_id is not None
        return self.factory.create_session(
            role_id,
            model_profile_id=model_profile_id,
            effort=effort,
            budget_overrides=budget_overrides,
        )

    def get_session(self, session_id: str) -> Session:
        return self.store.get_session(session_id)

    def list_events(self, session_id: str) -> list[Event]:
        self.get_session(session_id)
        return self.store.list_events(session_id)

    # Runtime control

    async def run_session(
        self,
        session_id: str,
        *,
        user_message: str,
        workspace: str | Path,
        approval_callback: ApprovalCallback | None = None,
    ) -> AsyncIterator[RuntimeEvent]:
        session = self.get_session(session_id)
        agent = self.factory.create_agent(session.id)
        self.store.update_agent_status(agent.id, AgentStatus.RUNNING)
        cancellation = asyncio.Event()
        self._cancellations[session.id] = cancellation
        loop = AgentLoop(
            self.provider,
            WorkspaceTools(
                workspace,
                policy=session.role_snapshot.tool_policy,
            ),
        )

        async def wait_for_approval(tool_call_id: str, category: str, detail: str) -> bool:
            if approval_callback is not None:
                return await approval_callback(tool_call_id, category, detail)
            key = (session.id, tool_call_id)
            future = self._approval_futures.get(key)
            if future is None:
                future = asyncio.get_running_loop().create_future()
                self._approval_futures[key] = future
                self._approval_details[key] = {
                    "category": category,
                    "detail": detail,
                }
            return await future

        iterator = loop.run(
            snapshot=session.role_snapshot,
            user_message=user_message,
            approval_callback=wait_for_approval,
        )
        deadline = asyncio.get_running_loop().time() + session.role_snapshot.budget.timeout_seconds
        final_status = AgentStatus.FAILED

        try:
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    timeout_event = RuntimeEvent(
                        event_type="agent.timed_out",
                        turn=0,
                        payload={"timeout_seconds": (session.role_snapshot.budget.timeout_seconds)},
                    )
                    self._persist_runtime_event(session.id, agent.id, timeout_event)
                    yield timeout_event
                    final_status = AgentStatus.TIMED_OUT
                    break

                next_event: asyncio.Future[RuntimeEvent] = asyncio.ensure_future(anext(iterator))
                cancelled = asyncio.create_task(cancellation.wait())
                waiters: set[asyncio.Future[Any]] = {
                    next_event,
                    cancelled,
                }
                done, pending = await asyncio.wait(
                    waiters,
                    timeout=remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)

                if not done:
                    next_event.cancel()
                    cancelled.cancel()
                    await asyncio.gather(
                        next_event,
                        cancelled,
                        return_exceptions=True,
                    )
                    timeout_event = RuntimeEvent(
                        event_type="agent.timed_out",
                        turn=0,
                        payload={"timeout_seconds": (session.role_snapshot.budget.timeout_seconds)},
                    )
                    self._persist_runtime_event(session.id, agent.id, timeout_event)
                    yield timeout_event
                    final_status = AgentStatus.TIMED_OUT
                    break

                if cancelled in done and cancelled.result():
                    next_event.cancel()
                    await asyncio.gather(next_event, return_exceptions=True)
                    cancel_event = RuntimeEvent(
                        event_type="agent.cancelled",
                        turn=0,
                        payload={},
                    )
                    self._persist_runtime_event(session.id, agent.id, cancel_event)
                    yield cancel_event
                    final_status = AgentStatus.CANCELLED
                    break

                try:
                    runtime_event = next_event.result()
                except StopAsyncIteration:
                    final_status = self._status_from_events(self.store.list_events(session.id))
                    break

                if runtime_event.event_type == "tool.approval_required":
                    tool_call_id = str(runtime_event.payload["tool_call_id"])
                    key = (session.id, tool_call_id)
                    if key not in self._approval_futures:
                        self._approval_futures[key] = asyncio.get_running_loop().create_future()
                        self._approval_details[key] = {
                            "category": str(runtime_event.payload["category"]),
                            "detail": str(runtime_event.payload["detail"]),
                        }

                self._persist_runtime_event(session.id, agent.id, runtime_event)
                yield runtime_event
        except BaseException:
            final_status = AgentStatus.FAILED
            raise
        finally:
            await iterator.aclose()
            self.store.update_agent_status(agent.id, final_status)
            self._cancellations.pop(session.id, None)
            self._clear_session_approvals(session.id)

    def cancel_session(self, session_id: str) -> bool:
        self.get_session(session_id)
        cancellation = self._cancellations.get(session_id)
        if cancellation is None:
            return False
        cancellation.set()
        return True

    def submit_approval(self, session_id: str, tool_call_id: str, *, approved: bool) -> bool:
        self.get_session(session_id)
        key = (session_id, tool_call_id)
        future = self._approval_futures.get(key)
        if future is None or future.done():
            return False
        future.set_result(approved)
        return True

    def list_pending_approvals(self, session_id: str) -> list[dict[str, str]]:
        self.get_session(session_id)
        pending: list[dict[str, str]] = []
        for (candidate_session_id, tool_call_id), details in self._approval_details.items():
            if candidate_session_id == session_id:
                pending.append({"tool_call_id": tool_call_id, **details})
        return pending

    def _persist_runtime_event(self, session_id: str, agent_id: str, event: RuntimeEvent) -> None:
        self.store.append_event(
            Event(
                session_id=session_id,
                agent_id=agent_id,
                event_type=event.event_type,
                payload={"turn": event.turn, **event.payload},
            )
        )

    def _clear_session_approvals(self, session_id: str) -> None:
        keys = [key for key in self._approval_futures if key[0] == session_id]
        for key in keys:
            future = self._approval_futures.pop(key)
            if not future.done():
                future.cancel()
            self._approval_details.pop(key, None)

    @staticmethod
    def _status_from_events(events: list[Event]) -> AgentStatus:
        if events and events[-1].event_type == "agent.completed":
            return AgentStatus.COMPLETED
        if events and events[-1].event_type == "agent.cancelled":
            return AgentStatus.CANCELLED
        if events and events[-1].event_type == "agent.timed_out":
            return AgentStatus.TIMED_OUT
        return AgentStatus.FAILED
