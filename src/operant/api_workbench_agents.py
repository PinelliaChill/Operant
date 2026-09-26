"""Bounded parent/child Session execution and private directed mailbox."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import suppress
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from operant.api_workbench_context import thread_session
from operant.domain.models import Budget, RoleSnapshot, RoleStatus, ToolPolicy, new_id, utc_now
from operant.domain.threads import LegacySourceType, ThreadLegacyRef, ThreadStatus
from operant.persistence.sqlite import ConflictError, NotFoundError
from operant.protocol import redact_public_text
from operant.tools.workspace import ToolError

if TYPE_CHECKING:
    from operant.application.service import ApplicationService

MAX_CHILDREN = 4
MAX_DEPTH = 2


class CreateChildAgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1, max_length=12000)
    role_id: str | None = None
    model_profile_id: str | None = None
    effort: str | None = None
    budget_overrides: dict[str, Any] | None = None


class ChildAgentView(BaseModel):
    thread_id: str
    parent_thread_id: str
    session_id: str
    role_id: str
    model_profile_id: str
    workspace_ref: str
    tool_policy: ToolPolicy
    budget: Budget
    status: Literal["queued", "running", "completed", "failed", "cancelled", "interrupted"]
    task: str
    created_at: datetime
    result: str | None = None
    recovery: str | None = None


class SendAgentMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipient_thread_id: str
    body: str = Field(min_length=1, max_length=12000)
    reply_to: str | None = None
    idempotency_key: str = Field(min_length=1, max_length=300)

    @field_validator("reply_to", mode="before")
    @classmethod
    def normalize_optional_reply(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip() or None
        return value


class WorkbenchAgentMessage(BaseModel):
    message_id: str
    sender_thread_id: str
    recipient_thread_id: str
    body: str
    reply_to: str | None = None
    created_at: datetime
    cursor: int
    delivery_status: Literal["delivered", "consumed"]
    consumed_at: datetime | None = None
    wake_status: Literal["pending", "scheduled", "active_run", "limit_reached", "consumed"] = (
        "pending"
    )


class WorkbenchRuntime:
    def __init__(self, service: ApplicationService) -> None:
        self.service = service
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.wake_tasks: dict[str, asyncio.Task[None]] = {}
        self.completion_checks: set[asyncio.Task[None]] = set()
        self.inbox_projected_cursor: dict[str, int] = {}
        self.run_inbox: dict[str, dict[str, str]] = {}

    def _view(self, row: sqlite3.Row) -> ChildAgentView:
        session = self.service.get_session(str(row["session_id"]))
        thread = self.service.get_thread(str(row["thread_id"]))
        snapshot = session.role_snapshot
        return ChildAgentView(
            thread_id=thread.id,
            parent_thread_id=str(row["parent_thread_id"]),
            session_id=session.id,
            role_id=snapshot.role_id,
            model_profile_id=snapshot.model_profile_id,
            workspace_ref=thread.workspace_ref or "",
            tool_policy=snapshot.tool_policy,
            budget=snapshot.budget,
            status=row["status"],
            task=row["task"],
            created_at=datetime.fromisoformat(row["created_at"]),
            result=row["result"],
            recovery=row["recovery"],
        )

    def get_child(self, thread_id: str) -> ChildAgentView:
        with self.service.store._connect() as connection:
            row = connection.execute(
                "SELECT * FROM workbench_children WHERE thread_id=?", (thread_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"child agent not found: {thread_id}")
        view = self._view(row)
        if view.status in {"queued", "running"} and thread_id not in self.tasks:
            try:
                lease = self.service.store.get_session_run_lease(view.session_id)
            except NotFoundError:
                lease = None
            if lease is not None and lease.released_at is None and lease.expires_at > utc_now():
                return view
            # A fresh Core never replays an unknown in-flight model or tool call.
            with self.service.store._connect() as connection:
                connection.execute(
                    "UPDATE workbench_children SET status='interrupted',"
                    "recovery='manual_reconcile',"
                    "updated_at=? WHERE thread_id=? AND status IN ('queued','running')",
                    (utc_now().isoformat(), thread_id),
                )
            return self.get_child(thread_id)
        return view

    def list_children(self, parent_thread_id: str) -> list[ChildAgentView]:
        self.service.get_thread(parent_thread_id)
        with self.service.store._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM workbench_children WHERE parent_thread_id=? "
                "ORDER BY created_at,thread_id",
                (parent_thread_id,),
            ).fetchall()
        return [self.get_child(str(row["thread_id"])) for row in rows]

    def _snapshot(self, parent: RoleSnapshot, body: CreateChildAgentRequest) -> RoleSnapshot:
        selected = parent
        if body.role_id is not None:
            role = self.service.get_role(body.role_id)
            if role.status is RoleStatus.INACTIVE:
                raise ValueError("child role is inactive")
            if not set(role.tool_policy.allowed_tools).issubset(parent.tool_policy.allowed_tools):
                raise PermissionError("child role cannot gain tools")
            if role.tool_policy.workspace_write and not parent.tool_policy.workspace_write:
                raise PermissionError("child role cannot gain workspace write")
            if role.tool_policy.command_execution and not parent.tool_policy.command_execution:
                raise PermissionError("child role cannot gain command execution")
            if not set(parent.tool_policy.approval_required).issubset(
                role.tool_policy.approval_required
            ):
                raise PermissionError("child role cannot remove required approvals")
            if (
                role.tool_policy.command_execution_policy
                != parent.tool_policy.command_execution_policy
            ):
                raise PermissionError("child role cannot change the command execution boundary")
            parent.budget.narrowed(**role.budget.model_dump())
            selected = parent.model_copy(
                update={
                    "role_id": role.id,
                    "role_version": role.version,
                    "role_name": role.name,
                    "system_prompt": role.system_prompt,
                    "tool_policy": role.tool_policy,
                    "budget": role.budget,
                }
            )
        if body.model_profile_id is not None:
            profile = self.service.get_model_profile(body.model_profile_id)
            if not profile.enabled:
                raise ValueError("child model profile is inactive")
            if selected.effort not in profile.supported_efforts and body.effort is None:
                raise ValueError("child model does not support inherited effort")
            selected = selected.model_copy(
                update={
                    "model_profile_id": profile.id,
                    "model_profile_name": profile.name,
                    "provider": profile.provider,
                    "model_id": profile.model_id,
                    "base_url": profile.base_url,
                    "secret_ref": profile.secret_ref,
                    "context_window": profile.context_window,
                    "input_usd_per_million_tokens": profile.input_usd_per_million_tokens,
                    "output_usd_per_million_tokens": profile.output_usd_per_million_tokens,
                    "provider_effort_parameter": profile.effort_parameter,
                    "provider_effort_value": profile.provider_effort_value(
                        selected.effort
                        if body.effort is None
                        else type(selected.effort)(body.effort)
                    ),
                }
            )
        if body.effort is not None:
            from operant.domain.models import Effort

            effort = Effort(body.effort)
            profile = self.service.get_model_profile(selected.model_profile_id)
            if effort not in profile.supported_efforts:
                raise ValueError("child model does not support requested effort")
            selected = selected.model_copy(
                update={
                    "effort": effort,
                    "provider_effort_parameter": profile.effort_parameter,
                    "provider_effort_value": profile.provider_effort_value(effort),
                }
            )
        if body.budget_overrides:
            selected = selected.model_copy(
                update={"budget": selected.budget.narrowed(**body.budget_overrides)}
            )
        parent.budget.narrowed(**selected.budget.model_dump())
        return selected

    async def create_child(
        self, parent_thread_id: str, body: CreateChildAgentRequest
    ) -> ChildAgentView:
        parent = self.service.get_thread(parent_thread_id)
        if parent.status is not ThreadStatus.ACTIVE or not parent.workspace_ref:
            raise ConflictError("parent thread must be active and bound to a workspace")
        parent_session = thread_session(self.service, parent_thread_id)
        depth = 0
        cursor = parent
        while cursor.parent_thread_id:
            depth += 1
            cursor = self.service.get_thread(cursor.parent_thread_id)
        if depth >= MAX_DEPTH:
            raise ConflictError("child agent depth limit reached")
        snapshot = self._snapshot(parent_session.role_snapshot, body)
        thread, session = self.service.store.create_workbench_child(
            parent_thread_id=parent_thread_id,
            workspace_ref=parent.workspace_ref,
            snapshot=snapshot,
            task=body.task,
            limit=MAX_CHILDREN,
        )
        task = asyncio.create_task(
            self._run_child(thread.id, session.id, body.task, parent.workspace_ref)
        )
        self.tasks[thread.id] = task
        task.add_done_callback(
            lambda completed: (
                self.tasks.pop(thread.id, None) if self.tasks.get(thread.id) is completed else None
            )
        )
        return self.get_child(thread.id)

    async def _run_child(
        self, thread_id: str, session_id: str, prompt: str, workspace: str
    ) -> None:
        status = "interrupted"
        result: str | None = None
        recovery: str | None = "manual_reconcile"
        with self.service.store._connect() as connection:
            connection.execute(
                "UPDATE workbench_children SET status='running',updated_at=? "
                "WHERE thread_id=? AND status='queued'",
                (utc_now().isoformat(), thread_id),
            )
        try:
            async for event in self.service.run_session(
                session_id, user_message=prompt, workspace=workspace, thread_id=thread_id
            ):
                if event.event_type == "agent.completed":
                    status = "completed"
                    content = event.payload.get("content")
                    result = content if isinstance(content, str) else None
                    recovery = None
                elif event.event_type == "agent.cancelled":
                    status, recovery = "cancelled", None
                elif event.event_type in {"agent.failed", "agent.timed_out", "budget.exhausted"}:
                    status, recovery = "failed", "inspect_events"
                elif event.event_type == "agent.stream_error":
                    status, recovery = "interrupted", "manual_reconcile"
        except asyncio.CancelledError:
            # Only explicit cancel_tree commits cancelled. Unknown shutdown
            # remains interrupted and requires inspection before any replay.
            with self.service.store._connect() as connection:
                row = connection.execute(
                    "SELECT status FROM workbench_children WHERE thread_id=?", (thread_id,)
                ).fetchone()
            if row is not None and row["status"] == "cancelled":
                status, recovery = "cancelled", None
            raise
        except Exception:
            status, recovery = "interrupted", "manual_reconcile"
        finally:
            with self.service.store._connect() as connection:
                connection.execute(
                    "UPDATE workbench_children SET status=?,result=?,recovery=?,updated_at=? "
                    "WHERE thread_id=? AND status NOT IN "
                    "('cancelled','completed','failed','interrupted')",
                    (status, result, recovery, utc_now().isoformat(), thread_id),
                )
            if status == "cancelled":
                with suppress(ConflictError):
                    self.service.set_thread_status(thread_id, ThreadStatus.CANCELLED)

    def cancel_tree(self, thread_id: str) -> ChildAgentView | None:
        thread = self.service.get_thread(thread_id)
        for child in self.list_children(thread_id):
            self.cancel_tree(child.thread_id)
        child_view: ChildAgentView | None = None
        with self.service.store._connect() as connection:
            connection.execute(
                "UPDATE workbench_children SET status='cancelled',recovery=NULL,updated_at=? "
                "WHERE thread_id=? AND status IN ('queued','running')",
                (utc_now().isoformat(), thread_id),
            )
        try:
            session = thread_session(self.service, thread_id)
            self.service.cancel_session(session.id)
        except ValueError:
            pass
        if thread.status is ThreadStatus.ACTIVE:
            self.service.set_thread_status(thread_id, ThreadStatus.CANCELLED)
        if thread_id in self.tasks:
            self.tasks[thread_id].cancel()
        with self.service.store._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM workbench_children WHERE thread_id=?", (thread_id,)
            ).fetchone()
        if exists:
            child_view = self.get_child(thread_id)
        return child_view

    def cancel_session_children(self, session_id: str) -> None:
        try:
            thread = self.service.store.get_thread_by_legacy_ref(
                ThreadLegacyRef(source_type=LegacySourceType.SESSION, source_id=session_id)
            )
        except NotFoundError:
            return
        for child in self.list_children(thread.id):
            self.cancel_tree(child.thread_id)

    def _message(self, row: sqlite3.Row) -> WorkbenchAgentMessage:
        recipient_id = str(row["recipient_thread_id"])
        wake_status: Literal["pending", "scheduled", "active_run", "limit_reached", "consumed"]
        if row["consumed_at"] is not None:
            wake_status = "consumed"
        elif recipient_id in self.tasks or recipient_id in self.wake_tasks:
            wake_status = "active_run"
        else:
            wake_status = "pending"
            with self.service.store._connect() as connection:
                count_row = connection.execute(
                    "SELECT wake_count FROM workbench_wake_counts WHERE thread_id=?",
                    (recipient_id,),
                ).fetchone()
            if count_row is not None:
                limit = min(
                    4, thread_session(self.service, recipient_id).role_snapshot.budget.max_turns
                )
                if int(count_row["wake_count"]) >= limit:
                    wake_status = "limit_reached"
        return WorkbenchAgentMessage(
            message_id=row["id"],
            sender_thread_id=row["sender_thread_id"],
            recipient_thread_id=row["recipient_thread_id"],
            body=row["body"],
            reply_to=row["reply_to"],
            created_at=datetime.fromisoformat(row["created_at"]),
            cursor=row["sequence"],
            delivery_status="consumed" if row["consumed_at"] is not None else "delivered",
            consumed_at=(
                datetime.fromisoformat(row["consumed_at"])
                if row["consumed_at"] is not None
                else None
            ),
            wake_status=wake_status,
        )

    def send(self, sender_thread_id: str, body: SendAgentMessageRequest) -> WorkbenchAgentMessage:
        message, _created = self.send_once(sender_thread_id, body)
        return message

    def send_once(
        self, sender_thread_id: str, body: SendAgentMessageRequest
    ) -> tuple[WorkbenchAgentMessage, bool]:
        sender = self.service.get_thread(sender_thread_id)
        if body.recipient_thread_id == sender_thread_id:
            parent_hint = (
                f" Parent Thread ID: {sender.parent_thread_id}."
                if sender.parent_thread_id is not None
                else ""
            )
            raise PermissionError(
                "recipient_thread_id must name another related Agent Thread, not this Thread."
                + parent_hint
            )
        recipient = self.service.get_thread(body.recipient_thread_id)
        if sender.status is not ThreadStatus.ACTIVE or recipient.status is not ThreadStatus.ACTIVE:
            raise ConflictError("message sender and recipient must be active")
        if sender.workspace_ref != recipient.workspace_ref or not sender.workspace_ref:
            raise PermissionError("private messages require one shared workspace")
        thread_session(self.service, body.recipient_thread_id)
        if not (
            sender.parent_thread_id == recipient.id
            or recipient.parent_thread_id == sender.id
            or (
                sender.parent_thread_id is not None
                and sender.parent_thread_id == recipient.parent_thread_id
            )
        ):
            raise PermissionError("private messages require related agent threads")
        with self.service.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM workbench_messages WHERE sender_thread_id=? AND idempotency_key=?",
                (sender_thread_id, body.idempotency_key),
            ).fetchone()
            if existing is not None:
                if (existing["recipient_thread_id"], existing["body"], existing["reply_to"]) != (
                    body.recipient_thread_id,
                    body.body,
                    body.reply_to,
                ):
                    raise ConflictError("message idempotency key reused with different content")
                return self._message(existing), False
            if body.reply_to is not None:
                previous = connection.execute(
                    "SELECT sender_thread_id,recipient_thread_id "
                    "FROM workbench_messages WHERE id=?",
                    (body.reply_to,),
                ).fetchone()
                if previous is None or {
                    previous["sender_thread_id"],
                    previous["recipient_thread_id"],
                } != {sender_thread_id, body.recipient_thread_id}:
                    raise PermissionError(
                        "reply_to must be an existing message_id exchanged by these Threads; "
                        "omit reply_to for a new message. A Thread ID is not a message ID."
                    )
            message_id = new_id("message")
            connection.execute(
                "INSERT INTO workbench_messages("
                "id,sender_thread_id,recipient_thread_id,body,"
                "reply_to,idempotency_key,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    message_id,
                    sender_thread_id,
                    body.recipient_thread_id,
                    body.body,
                    body.reply_to,
                    body.idempotency_key,
                    utc_now().isoformat(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM workbench_messages WHERE id=?", (message_id,)
            ).fetchone()
            assert row is not None
            return self._message(row), True

    async def wake(self, recipient_thread_id: str) -> str:
        if recipient_thread_id in self.tasks or recipient_thread_id in self.wake_tasks:
            return "active_run"
        thread = self.service.get_thread(recipient_thread_id)
        if thread.status is not ThreadStatus.ACTIVE or not thread.workspace_ref:
            return "pending"
        try:
            child = self.get_child(recipient_thread_id)
        except NotFoundError:
            child = None
        if child is not None and child.status != "completed":
            # get_child marks a stale queued/running child interrupted. A
            # directed message must not silently replay that uncertain run.
            return "active_run" if child.status in {"queued", "running"} else "pending"
        workspace_ref = thread.workspace_ref
        session = thread_session(self.service, recipient_thread_id)
        try:
            lease = self.service.store.get_session_run_lease(session.id)
        except NotFoundError:
            lease = None
        if lease is not None and lease.released_at is None:
            # An expired but unreleased lease represents unknown execution,
            # even when the thread's last projection said completed.
            return "active_run" if lease.expires_at > utc_now() else "pending"
        limit = min(4, session.role_snapshot.budget.max_turns)
        if not self.service.store.reserve_workbench_wake(recipient_thread_id, limit=limit):
            return "limit_reached"

        with self.service.store._connect() as connection:
            child_row = connection.execute(
                "SELECT status FROM workbench_children WHERE thread_id=?",
                (recipient_thread_id,),
            ).fetchone()
            if child_row is not None and child_row["status"] == "completed":
                connection.execute(
                    "UPDATE workbench_children SET status='queued',result=NULL,recovery=NULL,"
                    "updated_at=? WHERE thread_id=? AND status='completed'",
                    (utc_now().isoformat(), recipient_thread_id),
                )
                task = asyncio.create_task(
                    self._run_child(
                        recipient_thread_id,
                        session.id,
                        "A private Agent message arrived. Read it and continue your task.",
                        workspace_ref,
                    )
                )
                self.tasks[recipient_thread_id] = task
                task.add_done_callback(
                    lambda completed: (
                        self.tasks.pop(recipient_thread_id, None)
                        if self.tasks.get(recipient_thread_id) is completed
                        else None
                    )
                )
                return "scheduled"

        async def run() -> None:
            async for _ in self.service.run_session(
                session.id,
                user_message="A private Agent message arrived. Read it and continue your task.",
                workspace=workspace_ref,
                thread_id=recipient_thread_id,
            ):
                pass

        task = asyncio.create_task(run())
        self.wake_tasks[recipient_thread_id] = task
        task.add_done_callback(
            lambda completed: (
                self.wake_tasks.pop(recipient_thread_id, None)
                if self.wake_tasks.get(recipient_thread_id) is completed
                else None
            )
        )
        return "scheduled"

    def schedule_pending_wake(self, thread_id: str) -> None:
        """After a successful run, inspect mail after its task mapping disappears."""

        active = self.tasks.get(thread_id) or self.wake_tasks.get(thread_id)
        if active is not None:
            active.add_done_callback(lambda _task: self._start_completion_check(thread_id))
        else:
            self._start_completion_check(thread_id)

    def _start_completion_check(self, thread_id: str) -> None:
        check = asyncio.create_task(self._wake_if_pending(thread_id))
        self.completion_checks.add(check)
        check.add_done_callback(self.completion_checks.discard)

    async def _wake_if_pending(self, thread_id: str) -> None:
        with self.service.store._connect() as connection:
            pending = connection.execute(
                "SELECT 1 FROM workbench_messages "
                "WHERE recipient_thread_id=? AND consumed_at IS NULL LIMIT 1",
                (thread_id,),
            ).fetchone()
        if pending is not None:
            await self.wake(thread_id)

    def list_messages(self, thread_id: str) -> list[WorkbenchAgentMessage]:
        self.service.get_thread(thread_id)
        with self.service.store._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM workbench_messages "
                "WHERE sender_thread_id=? OR recipient_thread_id=? "
                "ORDER BY sequence LIMIT 500",
                (thread_id, thread_id),
            ).fetchall()
        return [self._message(row) for row in rows]

    def peek_inbox(self, thread_id: str) -> str:
        with self.service.store._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM workbench_messages "
                "WHERE recipient_thread_id=? AND consumed_at IS NULL "
                "ORDER BY sequence LIMIT 100",
                (thread_id,),
            ).fetchall()
        self.inbox_projected_cursor[thread_id] = max(
            (int(row["sequence"]) for row in rows),
            default=self.inbox_projected_cursor.get(thread_id, 0),
        )
        projected = self.run_inbox.setdefault(thread_id, {})
        for row in rows:
            projected[str(row["id"])] = (
                f"Private message {row['id']} from {row['sender_thread_id']}: {row['body']}"
            )
        return "\n".join(projected.values())

    def acknowledge_inbox(self, thread_id: str) -> None:
        cursor = self.inbox_projected_cursor.get(thread_id, 0)
        if cursor <= 0:
            return
        with self.service.store._connect() as connection:
            connection.execute(
                "UPDATE workbench_messages SET consumed_at=? "
                "WHERE recipient_thread_id=? AND sequence<=? AND consumed_at IS NULL",
                (utc_now().isoformat(), thread_id, cursor),
            )

    def clear_run_inbox(self, thread_id: str) -> None:
        self.run_inbox.pop(thread_id, None)
        self.inbox_projected_cursor.pop(thread_id, None)

    def tools_for(self, thread_id: str) -> WorkbenchCollaborationTools:
        return WorkbenchCollaborationTools(self, thread_id)


class WorkbenchCollaborationTools:
    def __init__(self, runtime: WorkbenchRuntime, thread_id: str) -> None:
        self.runtime = runtime
        self.thread_id = thread_id

    def delegate_options(self) -> dict[str, list[tuple[str, str]]]:
        service = self.runtime.service
        parent = thread_session(service, self.thread_id).role_snapshot
        profiles = [
            (profile.id, f"{profile.name} / {profile.model_id}")
            for profile in service.list_model_profiles()
            if profile.enabled and parent.effort in profile.supported_efforts
        ]
        roles: list[tuple[str, str]] = []
        for role in service.list_roles():
            try:
                self.runtime._snapshot(
                    parent, CreateChildAgentRequest(task="candidate", role_id=role.id)
                )
            except (NotFoundError, ValueError, PermissionError):
                continue
            roles.append((role.id, role.name))
        return {"model_profile_id": profiles[:32], "role_id": roles[:32]}

    def message_scope(self) -> dict[str, Any]:
        service = self.runtime.service
        current = service.get_thread(self.thread_id)
        targets: list[tuple[str, str]] = []
        if current.parent_thread_id is not None:
            parent = service.get_thread(current.parent_thread_id)
            if parent.status is ThreadStatus.ACTIVE:
                targets.append((parent.id, "parent"))
            siblings = service.list_threads(parent_thread_id=parent.id, limit=1000)
            targets.extend(
                (sibling.id, "sibling")
                for sibling in siblings
                if sibling.id != current.id and sibling.status is ThreadStatus.ACTIVE
            )
        children = service.list_threads(parent_thread_id=current.id, limit=1000)
        targets.extend(
            (child.id, "child") for child in children if child.status is ThreadStatus.ACTIVE
        )
        return {
            "current_thread_id": current.id,
            "parent_thread_id": current.parent_thread_id,
            "targets": targets,
        }

    @staticmethod
    def _tool_error(exc: Exception) -> ToolError:
        return ToolError(redact_public_text(str(exc), max_chars=600))

    async def delegate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            body = CreateChildAgentRequest.model_validate(arguments)
        except ValidationError as exc:
            raise self._tool_error(exc) from exc
        try:
            child = await self.runtime.create_child(self.thread_id, body)
        except NotFoundError as exc:
            if body.model_profile_id is not None and "model profile not found" in str(exc):
                available = ", ".join(
                    identity for identity, _ in self.delegate_options()["model_profile_id"]
                )
                raise ToolError(
                    "model_profile_id must be a configured ModelProfile ID, not a model name; "
                    f"omit it to inherit the parent model. Available IDs: {available or 'none'}"
                ) from exc
            raise self._tool_error(exc) from exc
        except (ConflictError, ValueError, PermissionError) as exc:
            raise self._tool_error(exc) from exc
        return child.model_dump(mode="json")

    async def send(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            body = SendAgentMessageRequest.model_validate(arguments)
            message, created = self.runtime.send_once(self.thread_id, body)
            wake_status = (
                await self.runtime.wake(message.recipient_thread_id)
                if created
                else message.wake_status
            )
        except (NotFoundError, ConflictError, ValidationError, ValueError, PermissionError) as exc:
            raise self._tool_error(exc) from exc
        message = message.model_copy(update={"wake_status": wake_status})
        return message.model_dump(mode="json")

    async def wait(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            child_id = str(arguments["child_thread_id"])
            seconds = min(300, max(1, int(arguments.get("timeout_seconds", 60))))
            child = self.runtime.get_child(child_id)
            if child.parent_thread_id != self.thread_id:
                raise PermissionError("can only wait for direct child Agents")
        except (KeyError, NotFoundError, ValueError, PermissionError) as exc:
            raise self._tool_error(exc) from exc
        deadline = asyncio.get_running_loop().time() + seconds
        while (
            child.status in {"queued", "running"} and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.2)
            child = self.runtime.get_child(child_id)
        return child.model_dump(mode="json")


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, NotFoundError):
        return HTTPException(404, str(exc))
    if isinstance(exc, ConflictError):
        return HTTPException(409, str(exc))
    if isinstance(exc, PermissionError):
        return HTTPException(403, str(exc))
    return HTTPException(400, str(exc))


def install_workbench_agent_routes(app: FastAPI, service: ApplicationService) -> WorkbenchRuntime:
    runtime = service.workbench
    if runtime is None:
        runtime = WorkbenchRuntime(service)
        service.workbench = runtime

    @app.get(
        "/v1/workbench/threads/{parent_thread_id}/children",
        operation_id="listWorkbenchChildAgents",
        response_model=list[ChildAgentView],
    )
    def list_children(parent_thread_id: str) -> list[ChildAgentView]:
        try:
            return runtime.list_children(parent_thread_id)
        except (NotFoundError, ValueError) as exc:
            raise _error(exc) from exc

    @app.post(
        "/v1/workbench/threads/{parent_thread_id}/children",
        operation_id="createWorkbenchChildAgent",
        response_model=ChildAgentView,
        status_code=201,
    )
    async def create_child(parent_thread_id: str, body: CreateChildAgentRequest) -> ChildAgentView:
        try:
            return await runtime.create_child(parent_thread_id, body)
        except (NotFoundError, ConflictError, ValueError, PermissionError) as exc:
            raise _error(exc) from exc

    @app.post(
        "/v1/workbench/threads/{thread_id}/cancel",
        operation_id="cancelWorkbenchThread",
        response_model=ChildAgentView | None,
    )
    def cancel(thread_id: str) -> ChildAgentView | None:
        try:
            return runtime.cancel_tree(thread_id)
        except (NotFoundError, ConflictError, ValueError) as exc:
            raise _error(exc) from exc

    @app.get(
        "/v1/workbench/threads/{thread_id}/messages",
        operation_id="listWorkbenchAgentMessages",
        response_model=list[WorkbenchAgentMessage],
    )
    def list_messages(thread_id: str) -> list[WorkbenchAgentMessage]:
        try:
            return runtime.list_messages(thread_id)
        except NotFoundError as exc:
            raise _error(exc) from exc

    @app.post(
        "/v1/workbench/threads/{thread_id}/messages",
        operation_id="sendWorkbenchAgentMessage",
        response_model=WorkbenchAgentMessage,
        status_code=201,
    )
    async def send(thread_id: str, body: SendAgentMessageRequest) -> WorkbenchAgentMessage:
        try:
            message, created = runtime.send_once(thread_id, body)
            wake_status = (
                await runtime.wake(message.recipient_thread_id) if created else message.wake_status
            )
            return message.model_copy(update={"wake_status": wake_status})
        except (NotFoundError, ConflictError, ValueError, PermissionError) as exc:
            raise _error(exc) from exc

    return runtime
