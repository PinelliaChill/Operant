from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from operant.application.service import ApplicationService
from operant.application.trace import summarize_session_trace
from operant.application.workflow import SequentialCodingWorkflow, WorkflowEvent
from operant.domain.actions import ToolActionReceipt, ToolActionReceiptStatus
from operant.domain.messages import (
    Message,
    ModelResponse,
    ModelUsage,
    ProviderEvent,
    ToolCall,
    ToolDefinition,
)
from operant.domain.models import (
    Budget,
    Event,
    ModelProfile,
    RolePreset,
    RoleSnapshot,
    Session,
    ToolPolicy,
    utc_now,
)
from operant.domain.workflow import WorkflowRun, WorkflowRunStatus, WorkflowStage
from operant.persistence.sqlite import (
    ActionOutcomeUnknownError,
    ConflictError,
    SQLiteStore,
    WorkflowExecutionLease,
)
from operant.runtime.loop import AgentLoop
from operant.tools.workspace import WorkspaceTools


def _snapshot(
    *,
    budget: Budget,
    policy: ToolPolicy | None = None,
    input_price: float | None = None,
    output_price: float | None = None,
) -> RoleSnapshot:
    return RoleSnapshot(
        role_id="role_budget",
        role_version=1,
        role_name="Budget",
        system_prompt="Respect the hard budget.",
        model_profile_id="model_budget",
        model_profile_name="Budget Model",
        provider="fake",
        model_id="budget-model",
        base_url="https://example.invalid/v1",
        secret_ref="OPERANT_BUDGET_KEY",
        input_usd_per_million_tokens=input_price,
        output_usd_per_million_tokens=output_price,
        effort="medium",
        provider_effort_parameter=None,
        provider_effort_value=None,
        tool_policy=policy or ToolPolicy(),
        budget=budget,
        memory_scope="session",
    )


class ScriptedBudgetProvider:
    def __init__(self, responses: Sequence[ModelResponse]) -> None:
        self.responses = list(responses)
        self.request_limits: list[int | None] = []

    async def list_models(self, *, base_url: str, secret_ref: str) -> list[str]:
        del base_url, secret_ref
        return []

    async def stream(
        self,
        *,
        snapshot: RoleSnapshot,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        del messages, tools
        self.request_limits.append(snapshot.budget.max_output_tokens)
        yield ProviderEvent(event_type="model.completed", response=self.responses.pop(0))


@pytest.mark.asyncio
async def test_output_budget_is_cumulative_and_provider_receives_remaining_limit(
    tmp_path: Path,
) -> None:
    (tmp_path / "hello.txt").write_text("hello", encoding="utf-8")
    provider = ScriptedBudgetProvider(
        (
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="read",
                        name="read_file",
                        arguments_json='{"path":"hello.txt"}',
                    ),
                ),
                usage=ModelUsage(prompt_tokens=2, completion_tokens=3, total_tokens=5),
            ),
            ModelResponse(
                content="done",
                usage=ModelUsage(prompt_tokens=4, completion_tokens=2, total_tokens=6),
            ),
        )
    )
    snapshot = _snapshot(
        budget=Budget(max_turns=2, max_output_tokens=5),
        policy=ToolPolicy(allowed_tools=("read_file",)),
    )
    events = [
        event
        async for event in AgentLoop(
            provider,
            WorkspaceTools(tmp_path, policy=snapshot.tool_policy),
        ).run(snapshot=snapshot, user_message="read")
    ]
    assert provider.request_limits == [5, 2]
    assert events[-1].event_type == "agent.completed"


@pytest.mark.asyncio
async def test_missing_usage_fails_closed_before_tool_started_or_side_effect(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.txt"
    target.write_text("before", encoding="utf-8")
    provider = ScriptedBudgetProvider(
        (
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="write",
                        name="apply_patch",
                        arguments_json=(
                            '{"path":"target.txt","old_text":"before","new_text":"after"}'
                        ),
                    ),
                ),
                usage=None,
            ),
        )
    )
    snapshot = _snapshot(
        budget=Budget(max_output_tokens=10),
        policy=ToolPolicy(allowed_tools=("apply_patch",), workspace_write=True),
    )
    events = [
        event
        async for event in AgentLoop(
            provider,
            WorkspaceTools(tmp_path, policy=snapshot.tool_policy),
        ).run(snapshot=snapshot, user_message="write")
    ]
    assert [event.event_type for event in events] == [
        "agent.started",
        "model.completed",
        "budget.exhausted",
    ]
    assert events[-1].payload["reason"] == "usage_unknown"
    assert events[-1].payload["observed"] is None
    assert target.read_text(encoding="utf-8") == "before"


@pytest.mark.asyncio
async def test_cost_budget_requires_exact_pricing_and_complete_usage(tmp_path: Path) -> None:
    response = ModelResponse(
        tool_calls=(ToolCall(id="read", name="read_file", arguments_json='{"path":"x"}'),),
        usage=ModelUsage(prompt_tokens=600_000, completion_tokens=500_000),
    )
    unknown_pricing = ScriptedBudgetProvider((response,))
    snapshot = _snapshot(budget=Budget(max_cost_usd=1.0))
    events = [
        event
        async for event in AgentLoop(
            unknown_pricing,
            WorkspaceTools(tmp_path, policy=snapshot.tool_policy),
        ).run(snapshot=snapshot, user_message="do not start")
    ]
    assert unknown_pricing.request_limits == []
    assert events[-1].payload["reason"] == "pricing_unknown"

    priced = ScriptedBudgetProvider((response,))
    priced_snapshot = _snapshot(
        budget=Budget(max_cost_usd=1.0),
        input_price=1.0,
        output_price=1.0,
    )
    events = [
        event
        async for event in AgentLoop(
            priced,
            WorkspaceTools(tmp_path, policy=priced_snapshot.tool_policy),
        ).run(snapshot=priced_snapshot, user_message="cost")
    ]
    assert events[-1].event_type == "budget.exhausted"
    assert events[-1].payload["observed"] == pytest.approx(1.1)
    assert "tool.started" not in [event.event_type for event in events]

    partial_usage = ScriptedBudgetProvider(
        (
            ModelResponse(
                tool_calls=response.tool_calls,
                usage=ModelUsage(prompt_tokens=1, completion_tokens=None, total_tokens=1),
            ),
        )
    )
    events = [
        event
        async for event in AgentLoop(
            partial_usage,
            WorkspaceTools(tmp_path, policy=priced_snapshot.tool_policy),
        ).run(snapshot=priced_snapshot, user_message="unknown cost")
    ]
    assert events[-1].event_type == "budget.exhausted"
    assert events[-1].payload["reason"] == "usage_unknown"
    assert events[-1].payload["observed"] is None
    assert "tool.started" not in [event.event_type for event in events]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("second_prompt_tokens", "expected_event"),
    (
        (2_000_000, "agent.completed"),
        (2_000_001, "budget.exhausted"),
    ),
)
async def test_cost_budget_compares_cumulative_decimal_cost_exactly(
    tmp_path: Path,
    second_prompt_tokens: int,
    expected_event: str,
) -> None:
    (tmp_path / "input.txt").write_text("input", encoding="utf-8")
    provider = ScriptedBudgetProvider(
        (
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="read",
                        name="read_file",
                        arguments_json='{"path":"input.txt"}',
                    ),
                ),
                usage=ModelUsage(prompt_tokens=1_000_000, completion_tokens=0),
            ),
            ModelResponse(
                content="done",
                usage=ModelUsage(prompt_tokens=second_prompt_tokens, completion_tokens=0),
            ),
        )
    )
    snapshot = _snapshot(
        budget=Budget(max_turns=2, max_cost_usd=0.3),
        policy=ToolPolicy(allowed_tools=("read_file",)),
        input_price=0.1,
        output_price=0.0,
    )
    events = [
        event
        async for event in AgentLoop(
            provider,
            WorkspaceTools(tmp_path, policy=snapshot.tool_policy),
        ).run(snapshot=snapshot, user_message="read")
    ]
    assert [event.event_type for event in events].count("tool.completed") == 1
    assert events[-1].event_type == expected_event
    if expected_event == "budget.exhausted":
        assert events[-1].payload["observed"] == pytest.approx(0.3000001)


@pytest.mark.asyncio
async def test_tool_call_budget_stops_before_preparing_the_next_call(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    provider = ScriptedBudgetProvider(
        (
            ModelResponse(
                tool_calls=(
                    ToolCall(id="one", name="read_file", arguments_json='{"path":"a.txt"}'),
                    ToolCall(id="two", name="read_file", arguments_json='{"path":"a.txt"}'),
                )
            ),
        )
    )
    snapshot = _snapshot(
        budget=Budget(max_tool_calls=1),
        policy=ToolPolicy(allowed_tools=("read_file",)),
    )
    events = [
        event
        async for event in AgentLoop(
            provider,
            WorkspaceTools(tmp_path, policy=snapshot.tool_policy),
        ).run(snapshot=snapshot, user_message="read once")
    ]
    assert [event.event_type for event in events].count("tool.started") == 1
    assert events[-1].event_type == "budget.exhausted"
    assert events[-1].payload["kind"] == "tool_calls"


def _store_with_session(path: Path) -> tuple[SQLiteStore, str]:
    store = SQLiteStore(path)
    store.initialize()
    profile = store.add_model_profile(
        ModelProfile(
            name="lease",
            model_id="lease",
            base_url="https://example.invalid/v1",
            secret_ref="OPERANT_LEASE_KEY",
        )
    )
    role = store.create_role(
        RolePreset(
            name="lease",
            system_prompt="wait",
            model_profile_id=profile.id,
        )
    )
    return store, store.create_session(role.id).id


@pytest.mark.parametrize("heartbeat", (0.0, 5.0, 6.0))
def test_lease_heartbeat_must_be_positive_and_below_ttl(heartbeat: float) -> None:
    with pytest.raises(ValueError, match="below TTL"):
        ApplicationService(
            SQLiteStore(":memory:"),
            ScriptedBudgetProvider(()),
            session_lease_ttl_seconds=5,
            session_lease_heartbeat_seconds=heartbeat,
        )


def test_default_lease_heartbeat_supports_sub_millisecond_ttl() -> None:
    service = ApplicationService(
        SQLiteStore(":memory:"),
        ScriptedBudgetProvider(()),
        session_lease_ttl_seconds=0.001,
    )
    assert 0 < service.workflow_execution_heartbeat_seconds < 0.001


def test_session_lease_expiry_reclaim_fences_stale_owner(tmp_path: Path) -> None:
    store, session_id = _store_with_session(tmp_path / "lease.sqlite3")
    started = datetime(2026, 8, 27, tzinfo=timezone.utc)
    first = store.acquire_session_run_lease(
        session_id,
        owner_id="first",
        ttl_seconds=5,
        now=started,
    )
    assert first is not None
    assert (
        SQLiteStore(store.path).acquire_session_run_lease(
            session_id,
            owner_id="second",
            ttl_seconds=5,
            now=started + timedelta(seconds=1),
        )
        is None
    )
    second = SQLiteStore(store.path).acquire_session_run_lease(
        session_id,
        owner_id="second",
        ttl_seconds=5,
        now=started + timedelta(seconds=6),
    )
    assert second is not None
    assert second.generation == first.generation + 1
    assert (
        store.renew_session_run_lease(
            first,
            ttl_seconds=5,
            now=started + timedelta(seconds=6),
        )
        is None
    )
    assert store.release_session_run_lease(first, now=started + timedelta(seconds=6)) is False
    with pytest.raises(ConflictError, match="fenced"):
        store.assert_session_run_lease(first, now=started + timedelta(seconds=6))
    assert store.release_session_run_lease(second, now=started + timedelta(seconds=7)) is True


def test_concurrent_session_lease_acquire_has_exactly_one_winner(tmp_path: Path) -> None:
    store, session_id = _store_with_session(tmp_path / "concurrent-lease.sqlite3")
    started = datetime(2026, 8, 27, tzinfo=timezone.utc)

    def acquire(index: int) -> bool:
        return (
            SQLiteStore(store.path).acquire_session_run_lease(
                session_id,
                owner_id=f"process-{index}",
                ttl_seconds=5,
                now=started,
            )
            is not None
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(acquire, range(8)))
    assert results.count(True) == 1


def test_cancelled_lease_blocks_new_side_effect_reservation(tmp_path: Path) -> None:
    store, session_id = _store_with_session(tmp_path / "cancelled-action.sqlite3")
    started = datetime(2026, 8, 27, tzinfo=timezone.utc)
    lease = store.acquire_session_run_lease(
        session_id,
        owner_id="cancel-owner",
        ttl_seconds=5,
        now=started,
    )
    assert lease is not None
    agent = store.create_agent(session_id)
    lease = store.bind_session_run_lease_agent(lease, agent.id, now=started)
    assert store.cancel_session_run_lease(session_id, now=started + timedelta(seconds=1)) is True
    assert store.cancel_session_run_lease(session_id, now=started + timedelta(seconds=1)) is True
    with pytest.raises(ConflictError, match="cancelled"):
        store.reserve_tool_action(
            ToolActionReceipt(
                scope=f"session:{session_id}:agent:{agent.id}:attempt:1",
                session_id=session_id,
                agent_id=agent.id,
                idempotency_key="must-not-run",
                action_hash="c" * 64,
                command_name="apply_patch",
            ),
            lease=lease,
            now=started + timedelta(seconds=1),
        )
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM tool_action_receipts").fetchone()[0] == 0


def test_workflow_cancel_atomically_fences_every_unreleased_child_and_old_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, first_session_id = _store_with_session(tmp_path / "atomic-cancel.sqlite3")
    role_id = store.get_session(first_session_id).role_snapshot.role_id
    second_session_id = store.create_session(role_id).id
    workflow = store.create_workflow_run(
        WorkflowRun(
            task="cancel atomically",
            workspace=str(tmp_path),
            planner_role_id="planner",
            coder_role_id="coder",
            reviewer_role_id="reviewer",
        )
    )
    started = datetime(2026, 8, 28, tzinfo=timezone.utc)
    guard = store.acquire_workflow_execution_lease(
        workflow.id,
        owner_id="coordinator",
        ttl_seconds=60,
        now=started,
    )
    assert guard is not None
    assert store.activate_workflow_run(guard, now=started) is not None
    first_lease = store.acquire_session_run_lease(
        first_session_id,
        owner_id="first-child",
        ttl_seconds=60,
        workflow_run_id=workflow.id,
        workflow_execution_lease=guard,
        now=started,
    )
    second_lease = store.acquire_session_run_lease(
        second_session_id,
        owner_id="expired-child",
        ttl_seconds=1,
        workflow_run_id=workflow.id,
        workflow_execution_lease=guard,
        now=started,
    )
    assert first_lease is not None and second_lease is not None
    agent = store.create_agent(first_session_id)
    first_lease = store.bind_session_run_lease_agent(first_lease, agent.id, now=started)

    original_atomic_cancel = store.cancel_workflow_run_atomically

    def atomic_cancel_at_fixed_time(workflow_run_id: str) -> tuple[bool, tuple[str, ...]]:
        return original_atomic_cancel(
            workflow_run_id,
            now=started + timedelta(seconds=5),
        )

    def forbidden_old_step(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("legacy two-transaction cancellation path was called")

    monkeypatch.setattr(store, "cancel_workflow_run_atomically", atomic_cancel_at_fixed_time)
    monkeypatch.setattr(store, "update_workflow_run", forbidden_old_step)
    monkeypatch.setattr(store, "cancel_workflow_run_leases", forbidden_old_step)
    service = ApplicationService(store, ScriptedBudgetProvider(()))

    assert service.cancel_workflow_run(workflow.id) is True
    assert service.cancel_workflow_run(workflow.id) is False
    with store._connect() as connection:
        connection.execute("BEGIN")
        run_row = connection.execute(
            "SELECT body, status FROM workflow_runs WHERE id = ?", (workflow.id,)
        ).fetchone()
        lease_rows = connection.execute(
            """
            SELECT session_id, cancel_requested, released_at
            FROM session_run_leases WHERE workflow_run_id = ? ORDER BY session_id
            """,
            (workflow.id,),
        ).fetchall()
    assert run_row is not None
    snapshot_run = WorkflowRun.model_validate_json(run_row["body"])
    assert snapshot_run.status is WorkflowRunStatus.CANCELLED
    assert snapshot_run.last_error_type == "cancelled"
    assert run_row["status"] == WorkflowRunStatus.CANCELLED.value
    assert {row["session_id"] for row in lease_rows} == {
        first_session_id,
        second_session_id,
    }
    assert all(row["cancel_requested"] == 1 for row in lease_rows)
    assert all(row["released_at"] is None for row in lease_rows)
    with pytest.raises(ConflictError, match="cancelled"):
        store.reserve_tool_action(
            ToolActionReceipt(
                scope=f"session:{first_session_id}:agent:{agent.id}:attempt:1",
                session_id=first_session_id,
                agent_id=agent.id,
                idempotency_key="after-cancel",
                action_hash="d" * 64,
                command_name="apply_patch",
            ),
            lease=first_lease,
            now=started + timedelta(seconds=5),
        )


def test_session_lease_owner_and_agent_binding_are_fenced(tmp_path: Path) -> None:
    store, session_id = _store_with_session(tmp_path / "lease-owner.sqlite3")
    role_id = store.get_session(session_id).role_snapshot.role_id
    other_session_id = store.create_session(role_id).id
    started = datetime(2026, 8, 28, tzinfo=timezone.utc)
    lease = store.acquire_session_run_lease(
        session_id,
        owner_id="real-owner",
        ttl_seconds=30,
        now=started,
    )
    assert lease is not None
    agent = store.create_agent(session_id)
    other_agent = store.create_agent(other_session_id)
    forged_owner = replace(lease, owner_id="forged-owner")
    with pytest.raises(ConflictError, match="current"):
        store.bind_session_run_lease_agent(forged_owner, agent.id, now=started)
    with pytest.raises(ConflictError, match="another session"):
        store.bind_session_run_lease_agent(lease, other_agent.id, now=started)
    assert (
        store.renew_session_run_lease(
            forged_owner,
            ttl_seconds=30,
            now=started + timedelta(seconds=1),
        )
        is None
    )
    with pytest.raises(ConflictError, match="fenced"):
        store.assert_session_run_lease(forged_owner, now=started + timedelta(seconds=1))
    assert store.release_session_run_lease(forged_owner, now=started) is False
    assert store.bind_session_run_lease_agent(lease, agent.id, now=started).agent_id == agent.id


def test_workflow_child_lease_requires_execution_guard(tmp_path: Path) -> None:
    store, session_id = _store_with_session(tmp_path / "required-workflow-guard.sqlite3")
    workflow = store.create_workflow_run(
        WorkflowRun(
            task="guard required",
            workspace=str(tmp_path),
            planner_role_id="planner",
            coder_role_id="coder",
            reviewer_role_id="reviewer",
            status=WorkflowRunStatus.RUNNING,
        )
    )
    with pytest.raises(ConflictError, match="required"):
        store.acquire_session_run_lease(
            session_id,
            owner_id="unguarded",
            ttl_seconds=30,
            workflow_run_id=workflow.id,
        )
    assert (
        store.acquire_session_run_lease(
            session_id,
            owner_id="independent",
            ttl_seconds=30,
        )
        is not None
    )


def test_service_stale_release_cannot_release_reclaimed_lease(tmp_path: Path) -> None:
    store, session_id = _store_with_session(tmp_path / "aba.sqlite3")
    service = ApplicationService(store, ScriptedBudgetProvider(()), session_lease_ttl_seconds=5)
    assert service.admit_session_run(session_id) is True
    first = service.admitted_session_run_lease(session_id)
    assert first is not None
    assert service.admit_session_run(session_id) is False
    second = SQLiteStore(store.path).acquire_session_run_lease(
        session_id,
        owner_id="other-process",
        ttl_seconds=5,
        now=first.expires_at + timedelta(microseconds=1),
    )
    assert second is not None
    service.release_session_run(session_id, first)
    current = store.get_session_run_lease(session_id)
    assert current.lease_token == second.lease_token
    assert current.released_at is None


def test_expired_lease_with_unknown_write_requires_manual_reconcile(tmp_path: Path) -> None:
    store, session_id = _store_with_session(tmp_path / "unknown.sqlite3")
    started = datetime(2026, 8, 27, tzinfo=timezone.utc)
    lease = store.acquire_session_run_lease(
        session_id,
        owner_id="crashed",
        ttl_seconds=5,
        now=started,
    )
    assert lease is not None
    agent = store.create_agent(session_id)
    lease = store.bind_session_run_lease_agent(lease, agent.id, now=started)
    receipt, _ = store.reserve_tool_action(
        ToolActionReceipt(
            scope=f"session:{session_id}:agent:{agent.id}:attempt:1",
            session_id=session_id,
            agent_id=agent.id,
            idempotency_key="unknown-write",
            action_hash="a" * 64,
            command_name="apply_patch",
        ),
        lease=lease,
        now=started,
    )
    with pytest.raises(ActionOutcomeUnknownError, match="manual reconciliation"):
        SQLiteStore(store.path).acquire_session_run_lease(
            session_id,
            owner_id="restarted",
            ttl_seconds=5,
            now=started + timedelta(seconds=6),
        )
    assert (
        store.get_tool_action_receipt(receipt.id).status is ToolActionReceiptStatus.OUTCOME_UNKNOWN
    )


def test_v3_unknown_session_write_blocks_first_v4_lease(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "v3-unknown-write.sqlite3")
    assert store.migrate(target_version=3) == 3
    profile = store.add_model_profile(
        ModelProfile(
            name="legacy action",
            model_id="legacy-action",
            base_url="https://example.invalid/v1",
            secret_ref="OPERANT_LEGACY_ACTION_KEY",
        )
    )
    role = store.create_role(
        RolePreset(
            name="legacy action",
            system_prompt="write",
            model_profile_id=profile.id,
        )
    )
    session = store.create_session(role.id)
    agent = store.create_agent(session.id)
    receipt, _ = store.reserve_tool_action(
        ToolActionReceipt(
            scope=f"session:{session.id}:agent:{agent.id}:attempt:1",
            session_id=session.id,
            agent_id=agent.id,
            idempotency_key="legacy-in-progress",
            action_hash="e" * 64,
            command_name="apply_patch",
        )
    )
    assert receipt.status is ToolActionReceiptStatus.IN_PROGRESS

    store.initialize()

    assert (
        store.get_tool_action_receipt(receipt.id).status is ToolActionReceiptStatus.OUTCOME_UNKNOWN
    )
    with pytest.raises(ActionOutcomeUnknownError, match="manual reconciliation"):
        store.acquire_session_run_lease(
            session.id,
            owner_id="restarted",
            ttl_seconds=30,
        )
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM session_run_leases").fetchone()[0] == 0


def test_second_initialize_preserves_active_workflow_and_tool_receipt(tmp_path: Path) -> None:
    store, session_id = _store_with_session(tmp_path / "active-initialize.sqlite3")
    workflow = store.create_workflow_run(
        WorkflowRun(
            task="active",
            workspace=str(tmp_path),
            planner_role_id="planner",
            coder_role_id="coder",
            reviewer_role_id="reviewer",
        )
    )
    workflow_guard = store.acquire_workflow_execution_lease(
        workflow.id,
        owner_id="live-process",
        ttl_seconds=60,
    )
    assert workflow_guard is not None
    store.update_workflow_run(workflow.id, status=WorkflowRunStatus.RUNNING)
    lease = store.acquire_session_run_lease(
        session_id,
        owner_id="live-process",
        ttl_seconds=60,
        workflow_run_id=workflow.id,
        workflow_execution_lease=workflow_guard,
    )
    assert lease is not None
    agent = store.create_agent(session_id)
    lease = store.bind_session_run_lease_agent(lease, agent.id)
    receipt, _ = store.reserve_tool_action(
        ToolActionReceipt(
            scope=f"session:{session_id}:agent:{agent.id}:attempt:1",
            session_id=session_id,
            agent_id=agent.id,
            idempotency_key="active-write",
            action_hash="b" * 64,
            command_name="apply_patch",
        ),
        lease=lease,
    )

    SQLiteStore(store.path).initialize()

    assert store.get_workflow_run(workflow.id).status is WorkflowRunStatus.RUNNING
    assert store.get_workflow_execution_lease(workflow.id).released_at is None
    assert store.get_tool_action_receipt(receipt.id).status is ToolActionReceiptStatus.IN_PROGRESS


def test_workflow_guard_fences_stale_token_and_generation(tmp_path: Path) -> None:
    store, _session_id = _store_with_session(tmp_path / "workflow-guard-aba.sqlite3")
    workflow = store.create_workflow_run(
        WorkflowRun(
            task="guard fencing",
            workspace=str(tmp_path),
            planner_role_id="planner",
            coder_role_id="coder",
            reviewer_role_id="reviewer",
        )
    )
    started = datetime(2026, 8, 27, tzinfo=timezone.utc)
    first = store.acquire_workflow_execution_lease(
        workflow.id,
        owner_id="first",
        ttl_seconds=5,
        now=started,
    )
    assert first is not None
    assert (
        SQLiteStore(store.path).acquire_workflow_execution_lease(
            workflow.id,
            owner_id="second",
            ttl_seconds=5,
            now=started + timedelta(seconds=1),
        )
        is None
    )
    assert store.release_workflow_execution_lease(first, now=started + timedelta(seconds=1))
    second = store.acquire_workflow_execution_lease(
        workflow.id,
        owner_id="second",
        ttl_seconds=5,
        now=started + timedelta(seconds=1),
    )
    assert second is not None
    assert second.generation == first.generation + 1
    assert (
        store.renew_workflow_execution_lease(
            replace(second, lease_token=first.lease_token),
            ttl_seconds=5,
            now=started + timedelta(seconds=2),
        )
        is None
    )
    assert (
        store.renew_workflow_execution_lease(
            replace(second, generation=first.generation),
            ttl_seconds=5,
            now=started + timedelta(seconds=2),
        )
        is None
    )
    assert (
        store.release_workflow_execution_lease(first, now=started + timedelta(seconds=2)) is False
    )


@pytest.mark.asyncio
async def test_cancelled_created_workflow_cannot_be_activated_or_create_children(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, session_id = _store_with_session(tmp_path / "cancel-before-activation.sqlite3")
    role_id = store.get_session(session_id).role_snapshot.role_id
    service = ApplicationService(store, ScriptedBudgetProvider(()))
    workflow = SequentialCodingWorkflow(service)
    original_activate = service.activate_workflow_run

    def cancel_before_activation(lease: WorkflowExecutionLease) -> WorkflowRun:
        workflow_run_id = lease.workflow_run_id
        assert SQLiteStore(store.path).cancel_workflow_run_atomically(workflow_run_id)[0]
        return original_activate(lease)

    monkeypatch.setattr(service, "activate_workflow_run", cancel_before_activation)
    with store._connect() as connection:
        session_count_before = connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    streamed = workflow.run(
        task="cancel before activate",
        workspace=tmp_path,
        planner_role_id=role_id,
        coder_role_id=role_id,
        reviewer_role_id=role_id,
    )

    with pytest.raises(ConflictError, match="cancelled"):
        await anext(streamed)

    run = store.list_workflow_runs()[0]
    guard = store.get_workflow_execution_lease(run.id)
    assert run.status is WorkflowRunStatus.CANCELLED
    assert guard.released_at is not None
    with store._connect() as connection:
        session_count_after = connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    assert session_count_after == session_count_before
    assert store.list_workflow_events(run.id) == []


def test_late_workflow_stage_and_completion_cannot_revive_cancelled_run(
    tmp_path: Path,
) -> None:
    store, _session_id = _store_with_session(tmp_path / "late-workflow-event.sqlite3")
    run = store.create_workflow_run(
        WorkflowRun(
            task="late event",
            workspace=str(tmp_path),
            planner_role_id="planner",
            coder_role_id="coder",
            reviewer_role_id="reviewer",
            status=WorkflowRunStatus.RUNNING,
        )
    )
    service = ApplicationService(store, ScriptedBudgetProvider(()))
    workflow = SequentialCodingWorkflow(service)
    assert service.cancel_workflow_run(run.id) is True

    assert workflow._advance_workflow_run(
        run.id,
        WorkflowEvent(
            workflow_run_id=run.id,
            role="planner",
            session_id="",
            event_type="agent.started",
        ),
    )
    assert workflow._advance_workflow_run(
        run.id,
        WorkflowEvent(
            workflow_run_id=run.id,
            role="workflow",
            session_id="",
            event_type="workflow.completed",
            payload={"verdict": "PASS"},
        ),
    )
    persisted = store.get_workflow_run(run.id)
    assert persisted.status is WorkflowRunStatus.CANCELLED
    assert persisted.current_stage is WorkflowStage.CREATED
    assert persisted.final_verdict is None


def test_workflow_guard_expiry_recovers_crashed_running_workflow(tmp_path: Path) -> None:
    store, _session_id = _store_with_session(tmp_path / "workflow-guard-expiry.sqlite3")
    workflow = store.create_workflow_run(
        WorkflowRun(
            task="crashed coordinator",
            workspace=str(tmp_path),
            planner_role_id="planner",
            coder_role_id="coder",
            reviewer_role_id="reviewer",
        )
    )
    started = datetime(2026, 8, 27, tzinfo=timezone.utc)
    guard = store.acquire_workflow_execution_lease(
        workflow.id,
        owner_id="crashed",
        ttl_seconds=5,
        now=started,
    )
    assert guard is not None
    store.update_workflow_run(workflow.id, status=WorkflowRunStatus.RUNNING)

    with store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        SQLiteStore._interrupt_running_workflows(
            connection,
            now=started + timedelta(seconds=4),
        )
    assert store.get_workflow_run(workflow.id).status is WorkflowRunStatus.RUNNING

    with store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        SQLiteStore._interrupt_running_workflows(
            connection,
            now=started + timedelta(seconds=6),
        )
    assert store.get_workflow_run(workflow.id).status is WorkflowRunStatus.INTERRUPTED


def test_trace_keeps_each_missing_usage_counter_unknown() -> None:
    snapshot = _snapshot(budget=Budget())
    session = Session(role_snapshot=snapshot)
    events = [
        Event(
            session_id=session.id,
            event_type="model.completed",
            payload={
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": None,
                    "total_tokens": 5,
                }
            },
            created_at=utc_now(),
        ),
        Event(
            session_id=session.id,
            event_type="model.completed",
            payload={
                "usage": {
                    "prompt_tokens": 7,
                    "completion_tokens": 3,
                    "total_tokens": None,
                }
            },
            created_at=utc_now(),
        ),
    ]
    summary = summarize_session_trace(session, events)
    assert summary.prompt_tokens == 12
    assert summary.completion_tokens is None
    assert summary.total_tokens is None


class RoleAwareBlockingProvider:
    def __init__(self) -> None:
        self.explorer_started = 0

    async def list_models(self, *, base_url: str, secret_ref: str) -> list[str]:
        del base_url, secret_ref
        return []

    async def stream(
        self,
        *,
        snapshot: RoleSnapshot,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        del messages, tools
        if snapshot.role_name == "planner":
            yield ProviderEvent(
                event_type="model.completed",
                response=ModelResponse(content="plan"),
            )
            return
        if snapshot.role_name.startswith("explorer"):
            self.explorer_started += 1
        await asyncio.Event().wait()
        if False:  # pragma: no cover - keep this an async generator
            yield ProviderEvent(event_type="model.completed", response=ModelResponse())


class AlwaysBlockingProvider:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def list_models(self, *, base_url: str, secret_ref: str) -> list[str]:
        del base_url, secret_ref
        return []

    async def stream(
        self,
        *,
        snapshot: RoleSnapshot,
        messages: Sequence[Message],
        tools: Sequence[ToolDefinition],
    ) -> AsyncIterator[ProviderEvent]:
        del snapshot, messages, tools
        self.started.set()
        await asyncio.Event().wait()
        if False:  # pragma: no cover - keep this an async generator
            yield ProviderEvent(event_type="model.completed", response=ModelResponse())


def _service_with_blocking_role(
    tmp_path: Path,
    provider: AlwaysBlockingProvider,
) -> tuple[ApplicationService, str]:
    service = ApplicationService(
        SQLiteStore(tmp_path / "blocking-session.sqlite3"),
        provider,
        session_lease_ttl_seconds=1,
        session_lease_heartbeat_seconds=0.01,
    )
    service.initialize()
    profile = service.add_model_profile(
        ModelProfile(
            name="blocking session",
            model_id="blocking-session",
            base_url="https://example.invalid/v1",
            secret_ref="OPERANT_BLOCKING_SESSION_KEY",
        )
    )
    role = service.create_role(
        RolePreset(
            name="blocking role",
            system_prompt="wait",
            model_profile_id=profile.id,
        )
    )
    return service, role.id


@pytest.mark.asyncio
async def test_external_cancelled_error_persists_cancelled_agent_and_releases_lease(
    tmp_path: Path,
) -> None:
    provider = AlwaysBlockingProvider()
    service, role_id = _service_with_blocking_role(tmp_path, provider)
    session = service.create_session(role_id)
    streamed = service.run_session(
        session.id,
        user_message="wait",
        workspace=tmp_path,
    )
    assert (await anext(streamed)).event_type == "agent.started"
    pending = asyncio.create_task(anext(streamed))
    await asyncio.wait_for(provider.started.wait(), timeout=2)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await streamed.aclose()

    with service.store._connect() as connection:
        agent_status = connection.execute(
            "SELECT status FROM agents WHERE session_id = ?",
            (session.id,),
        ).fetchone()["status"]
    assert agent_status == "cancelled"
    assert service.list_events(session.id)[-1].event_type == "agent.cancelled"
    assert service.store.get_session_run_lease(session.id).released_at is not None


@pytest.mark.asyncio
async def test_cross_instance_cancel_fences_session_during_long_provider_wait(
    tmp_path: Path,
) -> None:
    provider = AlwaysBlockingProvider()
    service, role_id = _service_with_blocking_role(tmp_path, provider)
    other = ApplicationService(
        SQLiteStore(service.store.path),
        provider,
        session_lease_ttl_seconds=1,
        session_lease_heartbeat_seconds=0.01,
    )
    other.initialize()
    session = service.create_session(role_id)
    streamed = service.run_session(
        session.id,
        user_message="wait",
        workspace=tmp_path,
    )
    assert (await anext(streamed)).event_type == "agent.started"
    pending = asyncio.create_task(anext(streamed))
    await asyncio.wait_for(provider.started.wait(), timeout=2)

    assert other.cancel_session(session.id) is True
    assert (await asyncio.wait_for(pending, timeout=2)).event_type == "agent.cancelled"
    await streamed.aclose()
    assert service.store.get_session_run_lease(session.id).released_at is not None


@pytest.mark.asyncio
async def test_workflow_guard_loss_interrupts_long_provider_and_releases_child(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "workflow-guard-loss.sqlite3")
    provider = AlwaysBlockingProvider()
    service = ApplicationService(
        store,
        provider,
        session_lease_ttl_seconds=1,
        session_lease_heartbeat_seconds=0.01,
    )
    service.initialize()
    profile = service.add_model_profile(
        ModelProfile(
            name="workflow guard",
            model_id="workflow-guard",
            base_url="https://example.invalid/v1",
            secret_ref="OPERANT_WORKFLOW_GUARD_KEY",
        )
    )

    def role(name: str, *, writable: bool = False) -> str:
        return service.create_role(
            RolePreset(
                name=name,
                system_prompt=name,
                model_profile_id=profile.id,
                tool_policy=(
                    ToolPolicy(allowed_tools=("apply_patch",), workspace_write=True)
                    if writable
                    else ToolPolicy()
                ),
            )
        ).id

    events = []
    streamed = SequentialCodingWorkflow(service).run(
        task="lose guard while provider waits",
        workspace=tmp_path,
        planner_role_id=role("planner"),
        coder_role_id=role("coder", writable=True),
        reviewer_role_id=role("reviewer"),
    )

    async def consume() -> None:
        async for event in streamed:
            events.append(event)

    consumer = asyncio.create_task(consume())
    await asyncio.wait_for(provider.started.wait(), timeout=2)
    workflow_id = events[0].workflow_run_id
    guard = SQLiteStore(store.path).get_workflow_execution_lease(workflow_id)
    assert SQLiteStore(store.path).release_workflow_execution_lease(guard) is True

    await asyncio.wait_for(consumer, timeout=2)
    recovered = store.get_workflow_run(workflow_id)
    assert recovered.status is WorkflowRunStatus.INTERRUPTED
    assert recovered.last_error_type == "workflow_execution_lease_lost"
    with store._connect() as connection:
        leases = connection.execute(
            "SELECT cancel_requested, released_at FROM session_run_leases"
        ).fetchall()
        agent_statuses = connection.execute("SELECT status FROM agents").fetchall()
        receipt_count = connection.execute("SELECT COUNT(*) FROM tool_action_receipts").fetchone()[
            0
        ]
    assert leases and all(row["released_at"] is not None for row in leases)
    assert [row["status"] for row in agent_statuses] == ["cancelled"]
    assert receipt_count == 0


@pytest.mark.asyncio
async def test_budget_exhaustion_fails_workflow_before_next_role_starts(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "workflow-budget.sqlite3")
    provider = ScriptedBudgetProvider((ModelResponse(content="unmetered plan", usage=None),))
    service = ApplicationService(store, provider)
    service.initialize()
    profile = service.add_model_profile(
        ModelProfile(
            name="workflow budget",
            model_id="workflow-budget",
            base_url="https://example.invalid/v1",
            secret_ref="OPERANT_WORKFLOW_BUDGET_KEY",
        )
    )

    def role(name: str, *, writable: bool = False, metered: bool = False) -> str:
        return service.create_role(
            RolePreset(
                name=name,
                system_prompt=name,
                model_profile_id=profile.id,
                tool_policy=(
                    ToolPolicy(allowed_tools=("apply_patch",), workspace_write=True)
                    if writable
                    else ToolPolicy()
                ),
                budget=Budget(max_output_tokens=10) if metered else Budget(),
            )
        ).id

    events = [
        event
        async for event in SequentialCodingWorkflow(service).run(
            task="fail closed on unknown usage",
            workspace=tmp_path,
            planner_role_id=role("planner", metered=True),
            coder_role_id=role("coder", writable=True),
            reviewer_role_id=role("reviewer"),
        )
    ]
    workflow_id = events[0].workflow_run_id
    assert any(event.event_type == "budget.exhausted" for event in events)
    assert events[-1].event_type == "workflow.failed"
    assert service.get_workflow_run(workflow_id).status is WorkflowRunStatus.FAILED
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_cross_instance_workflow_cancel_stops_all_parallel_explorers(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "workflow-cancel.sqlite3")
    provider = RoleAwareBlockingProvider()
    # This checks cancellation while leases are live, not one-second expiry
    # during another instance's synchronous schema initialization on slow hosts.
    service = ApplicationService(
        store,
        provider,
        session_lease_heartbeat_seconds=0.01,
    )
    service.initialize()
    profile = service.add_model_profile(
        ModelProfile(
            name="workflow",
            model_id="workflow",
            base_url="https://example.invalid/v1",
            secret_ref="OPERANT_WORKFLOW_KEY",
        )
    )

    def role(name: str, *, writable: bool = False) -> str:
        policy = (
            ToolPolicy(allowed_tools=("apply_patch",), workspace_write=True)
            if writable
            else ToolPolicy()
        )
        return service.create_role(
            RolePreset(
                name=name,
                system_prompt=name,
                model_profile_id=profile.id,
                tool_policy=policy,
            )
        ).id

    planner = role("planner")
    explorers = (role("explorer-1"), role("explorer-2"))
    coder = role("coder", writable=True)
    reviewer = role("reviewer")
    workflow = SequentialCodingWorkflow(service)
    streamed = workflow.run(
        task="cancel explorers",
        workspace=tmp_path,
        planner_role_id=planner,
        explorer_role_ids=explorers,
        coder_role_id=coder,
        reviewer_role_id=reviewer,
        max_parallel_explorers=2,
    )
    events = []

    async def consume() -> None:
        async for event in streamed:
            events.append(event)

    consumer = asyncio.create_task(consume())
    for _ in range(200):
        if provider.explorer_started == 2:
            break
        await asyncio.sleep(0.005)
    assert provider.explorer_started == 2
    workflow_id = events[0].workflow_run_id
    other = ApplicationService(
        SQLiteStore(store.path),
        provider,
        session_lease_heartbeat_seconds=0.01,
    )
    other.initialize()
    assert other.get_workflow_run(workflow_id).status is WorkflowRunStatus.RUNNING
    assert other.cancel_workflow_run(workflow_id) is True
    assert other.cancel_workflow_run(workflow_id) is False
    await asyncio.wait_for(consumer, timeout=2)
    assert service.get_workflow_run(workflow_id).status is WorkflowRunStatus.CANCELLED
    with store._connect() as connection:
        active = connection.execute(
            """
            SELECT COUNT(*) FROM session_run_leases
            WHERE workflow_run_id = ? AND released_at IS NULL
            """,
            (workflow_id,),
        ).fetchone()[0]
        coder_agents = connection.execute(
            """
            SELECT COUNT(*) FROM agents
            WHERE session_id IN (
                SELECT id FROM sessions WHERE body LIKE '%\"role_name\":\"coder\"%'
            )
            """
        ).fetchone()[0]
        cancelled_agents = connection.execute(
            "SELECT COUNT(*) FROM agents WHERE status = 'cancelled'"
        ).fetchone()[0]
    assert active == 0
    assert coder_agents == 0
    assert cancelled_agents == 2


def test_cancelled_workflow_cannot_acquire_a_new_child_lease(tmp_path: Path) -> None:
    store, session_id = _store_with_session(tmp_path / "cancel-race.sqlite3")
    workflow = store.create_workflow_run(
        WorkflowRun(
            task="cancel",
            workspace=str(tmp_path),
            planner_role_id="planner",
            coder_role_id="coder",
            reviewer_role_id="reviewer",
        )
    )
    store.update_workflow_run(workflow.id, status=WorkflowRunStatus.CANCELLED)
    with pytest.raises(ConflictError, match="not running"):
        store.acquire_session_run_lease(
            session_id,
            owner_id="late-child",
            ttl_seconds=5,
            workflow_run_id=workflow.id,
        )
