from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from operant.application.graph import GraphRuntime
from operant.application.graph_execution import BoundedGraphExecutor, GraphExecutionError
from operant.application.service import ApplicationService
from operant.application.team import TeamRuntime
from operant.domain.graph import (
    EdgeSpec,
    GraphLimits,
    GraphRunStatus,
    NodeKind,
    NodeRunStatus,
    NodeSpec,
    PortSpec,
    WorkflowDefinition,
    WorkflowDefinitionStatus,
)
from operant.domain.messages import ModelResponse, ModelUsage, ProviderEvent
from operant.domain.models import AgentStatus, Budget, ModelProfile, RolePreset
from operant.domain.team import (
    MessageAudience,
    MessageEnvelope,
    MessageKind,
    TaskBoardUpdate,
    TeamDefinition,
    TeamMember,
    TeamTask,
)
from operant.persistence.graph_team import SQLiteGraphRepository, SQLiteTeamRepository
from operant.persistence.sqlite import SQLiteStore


class RecordingProvider:
    def __init__(
        self,
        *,
        fail: bool = False,
        block: asyncio.Event | None = None,
        entered: asyncio.Event | None = None,
        unknown_usage: bool = False,
    ) -> None:
        self.fail = fail
        self.block = block
        self.entered = entered
        self.unknown_usage = unknown_usage
        self.active = 0
        self.max_active = 0
        self.calls: list[tuple[str, str, tuple[str, ...]]] = []

    async def list_models(self, *, base_url: str, secret_ref: str) -> list[str]:
        del base_url, secret_ref
        return ["graph-model"]

    async def stream(
        self,
        *,
        snapshot: Any,
        messages: Sequence[Any],
        tools: Sequence[Any],
    ) -> AsyncIterator[ProviderEvent]:
        self.calls.append(
            (
                snapshot.model_id,
                "\n".join(str(message.content or "") for message in messages),
                tuple(tool.name for tool in tools),
            )
        )
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        if self.entered is not None and self.active >= 2:
            self.entered.set()
        try:
            if self.block is not None:
                await self.block.wait()
            if self.fail:
                raise RuntimeError("provider fixture failure")
            yield ProviderEvent(
                event_type="model.completed",
                response=ModelResponse(
                    content=f"completed:{snapshot.role_id}",
                    usage=(
                        None
                        if self.unknown_usage
                        else ModelUsage(prompt_tokens=11, completion_tokens=3, total_tokens=14)
                    ),
                    finish_reason="stop",
                ),
            )
        finally:
            self.active -= 1


class RecordingService(ApplicationService):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.run_session_calls: list[tuple[str, dict[str, Any]]] = []

    async def run_session(self, session_id: str, **kwargs: Any) -> AsyncIterator[Any]:
        self.run_session_calls.append((session_id, dict(kwargs)))
        async for event in super().run_session(session_id, **kwargs):
            yield event


def _service(tmp_path: Path, provider: RecordingProvider) -> tuple[RecordingService, Any]:
    store = SQLiteStore(tmp_path / "graph.sqlite3")
    service = RecordingService(store, provider, artifact_root=tmp_path / "artifacts")
    service.initialize()
    profile = service.add_model_profile(
        ModelProfile(
            id="profile_graph",
            name="graph model",
            model_id="graph-model",
            base_url="https://provider.invalid/v1",
            secret_ref="OPERANT_GRAPH_TEST_KEY",
        )
    )
    role = service.create_role(
        RolePreset(
            id="role_graph",
            name="Graph Worker",
            system_prompt="Execute only the assigned task.",
            model_profile_id=profile.id,
        )
    )
    return service, role


def _definition(
    role_id: str,
    role_version: int,
    *,
    team_id: str,
    nodes: tuple[NodeSpec, ...],
    edges: tuple[EdgeSpec, ...] = (),
    parallel: int = 2,
    budget: Budget | None = None,
) -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id=f"graph.execution.{team_id}",
        version=1,
        name="Graph execution",
        nodes=nodes,
        edges=edges,
        graph_limits=GraphLimits(max_parallel_nodes=parallel),
        default_budget=budget or Budget(),
        default_policy={"team_id": team_id, "team_version": 1},
        locked_role_versions={role_id: role_version},
        status=WorkflowDefinitionStatus.PUBLISHED,
    )


def _node(node_id: str, role_id: str, role_version: int, *, task: str = "") -> NodeSpec:
    return NodeSpec(
        node_id=node_id,
        node_kind=NodeKind.AGENT,
        input_ports=(PortSpec(name="input", required=False),),
        output_ports=(PortSpec(name="result", value_type="string", required=False),),
        metadata={"role_id": role_id, "role_version": role_version, "task": task},
    )


def _team(team_id: str, role_id: str, node_ids: tuple[str, ...]) -> TeamDefinition:
    return TeamDefinition(
        team_id=team_id,
        version=1,
        members=tuple(
            TeamMember(
                member_id=node_id,
                agent_definition_id=role_id,
                role=f"Worker {node_id}",
                can_coordinate=index == 0,
            )
            for index, node_id in enumerate(node_ids)
        ),
        default_coordinator=node_ids[0],
    )


def _executor(
    service: ApplicationService,
) -> tuple[SQLiteGraphRepository, SQLiteTeamRepository, GraphRuntime, BoundedGraphExecutor]:
    graph_repository = SQLiteGraphRepository(service.store)
    team_repository = SQLiteTeamRepository(service.store)
    graph_runtime = GraphRuntime(graph_repository)
    return (
        graph_repository,
        team_repository,
        graph_runtime,
        BoundedGraphExecutor(service, graph_repository, graph_runtime, team_repository),
    )


def test_prepare_pins_roles_and_run_reuses_prepared_agents_and_threads(tmp_path: Path) -> None:
    provider = RecordingProvider()
    service, role = _service(tmp_path, provider)
    graph_repository, team_repository, runtime, executor = _executor(service)
    definition = _definition(
        role.id,
        role.version,
        team_id="team.execution",
        nodes=(
            _node("first", role.id, role.version, task="first task"),
            _node("second", role.id, role.version, task="second task"),
        ),
        edges=(
            EdgeSpec(
                edge_id="first-second",
                source_node="first",
                source_port="result",
                target_node="second",
                target_port="input",
            ),
        ),
        parallel=2,
    )
    team_repository.put_team_definition(_team("team.execution", role.id, ("first", "second")))
    run = runtime.create_run(
        definition,
        input={"task": "root task"},
        workspace_or_target=str(tmp_path),
    )
    prepared = executor.prepare(run.id)
    roster = team_repository.list_roster(prepared.team_run_id)
    assert len(roster) == 2
    first_agent = next(entry.agent_instance_id for entry in roster if entry.member_id == "first")
    second_agent = next(entry.agent_instance_id for entry in roster if entry.member_id == "second")
    TeamRuntime(team_repository).send_message(
        MessageEnvelope(
            workflow_run_id=run.id,
            team_run_id=prepared.team_run_id,
            sender_id=second_agent,
            recipient_ids=(first_agent,),
            message_kind=MessageKind.FINDING,
            payload={"marker": "private-to-first"},
            audience=MessageAudience.DIRECT,
        ),
        idempotency_key="message-first",
    )
    TeamRuntime(team_repository).update_task(
        TaskBoardUpdate(
            idempotency_key="task-first",
            task=TeamTask(
                team_run_id=prepared.team_run_id,
                title="first-only board task",
                assignee_ids=(first_agent,),
            ),
        )
    )
    with service.store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 2

    result = asyncio.run(executor.run(run.id))

    assert result.status is GraphRunStatus.COMPLETED
    assert [node.status.value for node in result.node_runs] == ["succeeded", "succeeded"]
    assert len(provider.calls) == 2
    assert len(service.run_session_calls) == 2
    assert all(
        call.get("memory_run_id") == graph_repository.get_run(run.id).id
        and "workflow_run_id" not in call
        for _session_id, call in service.run_session_calls
    )
    first_prompt = next(
        prompt for _model, prompt, _tools in provider.calls if '"node_id":"first"' in prompt
    )
    second_prompt = next(
        prompt for _model, prompt, _tools in provider.calls if '"node_id":"second"' in prompt
    )
    assert "private-to-first" in first_prompt
    assert "first-only board task" in first_prompt
    assert "private-to-first" not in second_prompt
    assert "first-only board task" not in second_prompt
    assert {entry.status.value for entry in team_repository.list_roster(prepared.team_run_id)} == {
        "completed"
    }
    with service.store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 2
    assert graph_repository.get_run(run.id).consumed_output_tokens == 6


@pytest.mark.asyncio
async def test_independent_agent_nodes_run_in_parallel_and_dependents_wait(tmp_path: Path) -> None:
    both_started = asyncio.Event()
    release = asyncio.Event()
    provider = RecordingProvider(block=release, entered=both_started)
    service, role = _service(tmp_path, provider)
    graph_repository, team_repository, runtime, executor = _executor(service)
    nodes = (
        _node("left", role.id, role.version, task="left"),
        _node("right", role.id, role.version, task="right"),
        _node("dependent", role.id, role.version, task="dependent"),
    )
    definition = _definition(
        role.id,
        role.version,
        team_id="team.parallel",
        nodes=nodes,
        edges=(
            EdgeSpec(
                edge_id="left-dependent",
                source_node="left",
                source_port="result",
                target_node="dependent",
                target_port="input",
            ),
        ),
        parallel=2,
    )
    team_repository.put_team_definition(
        _team("team.parallel", role.id, ("left", "right", "dependent"))
    )
    run = runtime.create_run(definition, workspace_or_target=str(tmp_path))
    executor.prepare(run.id)

    execution = asyncio.create_task(executor.run(run.id))
    await asyncio.wait_for(both_started.wait(), timeout=2)
    assert provider.max_active >= 2
    release.set()
    result = await execution

    assert result.status is GraphRunStatus.COMPLETED
    assert len(provider.calls) == 3
    dependent_prompt = next(
        prompt for _model, prompt, _tools in provider.calls if '"node_id":"dependent"' in prompt
    )
    assert "completed:role_graph" in dependent_prompt


@pytest.mark.asyncio
async def test_failure_does_not_mark_node_or_graph_success(tmp_path: Path) -> None:
    provider = RecordingProvider(fail=True)
    service, role = _service(tmp_path, provider)
    graph_repository, team_repository, runtime, executor = _executor(service)
    definition = _definition(
        role.id,
        role.version,
        team_id="team.failure",
        nodes=(_node("failed", role.id, role.version),),
        parallel=1,
    )
    team_repository.put_team_definition(_team("team.failure", role.id, ("failed",)))
    run = runtime.create_run(definition, workspace_or_target=str(tmp_path))

    result = await executor.run(run.id)

    assert result.status is GraphRunStatus.FAILED
    assert result.completed is False
    assert result.node_runs[0].status is not NodeRunStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_cancel_signals_running_session_and_persists_cancelled_graph(tmp_path: Path) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingProvider(RecordingProvider):
        async def stream(
            self,
            *,
            snapshot: Any,
            messages: Sequence[Any],
            tools: Sequence[Any],
        ) -> AsyncIterator[ProviderEvent]:
            started.set()
            async for event in super().stream(snapshot=snapshot, messages=messages, tools=tools):
                yield event

    provider = BlockingProvider(block=release)
    service, role = _service(tmp_path, provider)
    graph_repository, team_repository, runtime, executor = _executor(service)
    definition = _definition(
        role.id,
        role.version,
        team_id="team.cancel",
        nodes=(_node("cancelled", role.id, role.version),),
        parallel=1,
    )
    team_repository.put_team_definition(_team("team.cancel", role.id, ("cancelled",)))
    run = runtime.create_run(definition, workspace_or_target=str(tmp_path))
    task = asyncio.create_task(executor.run(run.id))
    await started.wait()

    cancelled = executor.cancel(run.id)
    release.set()
    result = await task

    assert cancelled.status is GraphRunStatus.CANCELLED
    assert result.status is GraphRunStatus.CANCELLED
    assert result.completed is False
    team = team_repository.get_team_run(result.run.team_run_id)
    assert all(
        entry.status.value != "active" for entry in team_repository.list_roster(team.team_run_id)
    )
    assert all(
        task.status.value in {"cancelled", "blocked"}
        for task in team_repository.list_tasks(team.team_run_id)
    )


@pytest.mark.asyncio
async def test_interrupted_run_with_terminal_prepared_agent_stays_interrupted(
    tmp_path: Path,
) -> None:
    provider = RecordingProvider()
    service, role = _service(tmp_path, provider)
    graph_repository, team_repository, runtime, executor = _executor(service)
    definition = _definition(
        role.id,
        role.version,
        team_id="team.resume",
        nodes=(_node("resume", role.id, role.version),),
        parallel=1,
    )
    team_repository.put_team_definition(_team("team.resume", role.id, ("resume",)))
    run = runtime.create_run(definition, workspace_or_target=str(tmp_path))
    executor.prepare(run.id)
    runtime.start_run(run.id)
    node = graph_repository.list_node_runs(run.id)[0]
    runtime.start_attempt(node.id)
    runtime.interrupt_run(run.id)
    team_run_id = graph_repository.get_run(run.id).team_run_id
    assert team_run_id is not None
    entry = team_repository.list_roster(team_run_id)[0]
    service.store.update_agent_status(entry.agent_instance_id, AgentStatus.CANCELLED)

    with pytest.raises(GraphExecutionError, match="cannot resume"):
        await executor.run(run.id)

    assert graph_repository.get_run(run.id).status is GraphRunStatus.INTERRUPTED
    assert provider.calls == []


@pytest.mark.asyncio
async def test_interrupted_run_with_reusable_agent_recovers_with_new_attempt(
    tmp_path: Path,
) -> None:
    provider = RecordingProvider()
    service, role = _service(tmp_path, provider)
    graph_repository, team_repository, runtime, executor = _executor(service)
    definition = _definition(
        role.id,
        role.version,
        team_id="team.resume.created",
        nodes=(_node("resume", role.id, role.version),),
        parallel=1,
    )
    team_repository.put_team_definition(_team("team.resume.created", role.id, ("resume",)))
    run = runtime.create_run(definition, workspace_or_target=str(tmp_path))
    prepared = executor.prepare(run.id)
    entry = team_repository.list_roster(prepared.team_run_id)[0]
    runtime.start_run(run.id)
    node = graph_repository.list_node_runs(run.id)[0]
    first_attempt = runtime.start_attempt(node.id, agent_instance_id=entry.agent_instance_id)
    runtime.interrupt_run(run.id)

    result = await executor.run(run.id)

    attempts = graph_repository.list_attempts(node.id)
    assert result.status is GraphRunStatus.COMPLETED
    assert len(attempts) == 2
    assert attempts[0].result.value == "interrupted"
    assert attempts[1].result.value == "succeeded"
    assert attempts[0].agent_instance_id == first_attempt.agent_instance_id
    assert attempts[1].agent_instance_id == entry.agent_instance_id
    assert len(provider.calls) == 1
    with service.store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_graph_output_budget_is_aggregate_across_agent_nodes(tmp_path: Path) -> None:
    provider = RecordingProvider()
    service, role = _service(tmp_path, provider)
    graph_repository, team_repository, runtime, executor = _executor(service)
    definition = _definition(
        role.id,
        role.version,
        team_id="team.budget",
        nodes=(
            _node("first", role.id, role.version),
            _node("second", role.id, role.version),
        ),
        parallel=2,
        budget=Budget(max_output_tokens=5),
    )
    team_repository.put_team_definition(_team("team.budget", role.id, ("first", "second")))
    run = runtime.create_run(definition, workspace_or_target=str(tmp_path))

    team_run = executor.prepare(run.id)
    roster = team_repository.list_roster(team_run.team_run_id)
    reserved = [
        service.get_session(
            service.store.get_agent(entry.agent_instance_id).session_id
        ).role_snapshot.budget.max_output_tokens
        for entry in roster
    ]
    assert all(value is not None for value in reserved)
    assert sum(reserved) <= 5
    result = await executor.run(run.id)

    # This deliberately nonconforming Provider reports 3 tokens even when its
    # reserved share is 2. Detect actual overuse without claiming success.
    assert result.status is GraphRunStatus.FAILED
    assert result.completed is False
    assert result.run.consumed_output_tokens == 6


@pytest.mark.asyncio
async def test_hard_graph_budget_rejects_unknown_usage_without_false_success(
    tmp_path: Path,
) -> None:
    provider = RecordingProvider(unknown_usage=True)
    service, role = _service(tmp_path, provider)
    graph_repository, team_repository, runtime, executor = _executor(service)
    definition = _definition(
        role.id,
        role.version,
        team_id="team.budget.unknown",
        nodes=(_node("unknown", role.id, role.version),),
        parallel=1,
        budget=Budget(max_output_tokens=5),
    )
    team_repository.put_team_definition(_team("team.budget.unknown", role.id, ("unknown",)))
    run = runtime.create_run(definition, workspace_or_target=str(tmp_path))

    result = await executor.run(run.id)

    assert result.status is GraphRunStatus.FAILED
    assert result.node_runs[0].status is NodeRunStatus.FAILED
    assert result.run.consumed_output_tokens == 0


def test_unsupported_nodes_and_role_drift_fail_before_roster_creation(tmp_path: Path) -> None:
    provider = RecordingProvider()
    service, role = _service(tmp_path, provider)
    graph_repository, team_repository, runtime, executor = _executor(service)
    unsupported = _definition(
        role.id,
        role.version,
        team_id="team.unsupported",
        nodes=(NodeSpec(node_id="tool", node_kind=NodeKind.TOOL),),
        parallel=1,
    )
    team_repository.put_team_definition(_team("team.unsupported", role.id, ("tool",)))
    unsupported_run = runtime.create_run(unsupported, workspace_or_target=str(tmp_path))
    with pytest.raises(GraphExecutionError, match="only AGENT"):
        executor.prepare(unsupported_run.id)
    with service.store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 0

    pinned = _definition(
        role.id,
        role.version,
        team_id="team.drift",
        nodes=(_node("drift", role.id, role.version),),
        parallel=1,
    )
    team_repository.put_team_definition(_team("team.drift", role.id, ("drift",)))
    drift_run = runtime.create_run(pinned, workspace_or_target=str(tmp_path))
    service.update_role(role.id, system_prompt="new role version")
    with pytest.raises(GraphExecutionError, match="changed after Graph publication"):
        executor.prepare(drift_run.id)
    with service.store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_retry_uses_new_agent_and_preserves_reserved_budget_and_history(tmp_path):
    from operant.domain.graph import RetryPolicy

    class FlakyProvider(RecordingProvider):
        async def stream(self, **kwargs):
            if not hasattr(self, "first_failed"):
                self.first_failed = True
                raise RuntimeError("transient provider failure")
            self.assert_retry_board()
            async for event in super().stream(**kwargs):
                yield event

    provider = FlakyProvider()
    service, role = _service(tmp_path, provider)
    graphs, teams, runtime, executor = _executor(service)
    node = _node("worker", role.id, role.version).model_copy(
        update={
            "retry_policy": RetryPolicy(max_attempts=2, delay_seconds=0.02),
        }
    )
    definition = _definition(role.id, role.version, team_id="retry-team", nodes=(node,))
    teams.put_team_definition(_team("retry-team", role.id, ("worker",)))
    run = runtime.create_run(definition, workspace_or_target=str(tmp_path))
    first = executor.prepare(run.id)
    old = teams.list_roster(first.team_run_id)[0]

    def assert_retry_board():
        current = executor._roster_entry(graphs.get_run(run.id), "worker")
        board = teams.list_tasks(first.team_run_id)[0]
        assert board.assignee_ids == (current.agent_instance_id,)
        assert board.status.value == "in_progress"

    provider.assert_retry_board = assert_retry_board
    result = await executor.run(run.id)
    assert result.completed
    history = teams.list_roster(first.team_run_id)
    assert len(history) == 2
    current = executor._roster_entry(result.run, "worker")
    assert current.agent_instance_id != old.agent_instance_id
    before = service.store.get_agent(old.agent_instance_id)
    after = service.store.get_agent(current.agent_instance_id)
    assert before.status is AgentStatus.FAILED and after.status is AgentStatus.COMPLETED
    assert before.session_id == after.session_id
    assert after.role_snapshot.budget.max_turns < before.role_snapshot.budget.max_turns
    assert len(graphs.list_attempts(result.node_runs[0].id)) == 2
    task = teams.list_tasks(first.team_run_id)[0]
    assert task.status.value == "completed" and task.assignee_ids == (current.agent_instance_id,)


@pytest.mark.parametrize(
    "status", [AgentStatus.FAILED, AgentStatus.CANCELLED, AgentStatus.TIMED_OUT]
)
def test_retry_roster_accepts_failed_terminal_states_without_losing_history(tmp_path, status):
    service, role = _service(tmp_path, RecordingProvider())
    graphs, teams, runtime, executor = _executor(service)
    definition = _definition(
        role.id,
        role.version,
        team_id="terminal-retry",
        nodes=(_node("worker", role.id, role.version),),
    )
    teams.put_team_definition(_team("terminal-retry", role.id, ("worker",)))
    run = runtime.create_run(definition, workspace_or_target=str(tmp_path))
    team = executor.prepare(run.id)
    original = teams.list_roster(team.team_run_id)[0]
    service.store.update_agent_status(original.agent_instance_id, status)
    replacement = executor._replace_failed_agent(original)
    assert replacement.agent_instance_id != original.agent_instance_id
    assert len(teams.list_roster(team.team_run_id)) == 2
    assert service.store.get_agent(original.agent_instance_id).status is status


def test_graph_keeps_model_profile_default_output_budget(tmp_path):
    service, role = _service(tmp_path, RecordingProvider())
    profile = service.get_model_profile(role.model_profile_id)
    service.update_model_profile(profile.id, default_token_budget=77)
    graphs, teams, runtime, executor = _executor(service)
    definition = _definition(
        role.id,
        role.version,
        team_id="default-budget",
        nodes=(_node("worker", role.id, role.version),),
    )
    teams.put_team_definition(_team("default-budget", role.id, ("worker",)))
    run = runtime.create_run(definition, workspace_or_target=str(tmp_path))
    team = executor.prepare(run.id)
    entry = teams.list_roster(team.team_run_id)[0]
    assert (
        service.store.get_agent(entry.agent_instance_id).role_snapshot.budget.max_output_tokens
        == 77
    )


@pytest.mark.asyncio
async def test_mail_received_during_tool_turn_reaches_only_next_recipient_context(tmp_path):
    from operant.domain.messages import ToolCall

    class MailProvider(RecordingProvider):
        async def stream(self, **kwargs):
            body = "\n".join(str(message.content or "") for message in kwargs["messages"])
            self.observed.append(body)
            if '"node_id":"first"' in body and not self.sent:
                self.sent = True
                assert "arrived-during-request" not in body
                self.send_mail()
                yield ProviderEvent(
                    event_type="model.completed",
                    response=ModelResponse(
                        tool_calls=(
                            ToolCall(
                                id="read-next",
                                name="read_file",
                                arguments_json='{"path":"sample.txt"}',
                            ),
                        ),
                        usage=ModelUsage(prompt_tokens=10, completion_tokens=3, total_tokens=13),
                        finish_reason="tool_calls",
                    ),
                )
                return
            async for event in super().stream(**kwargs):
                yield event

    (tmp_path / "sample.txt").write_text("sample evidence")
    provider = MailProvider()
    provider.sent = False
    provider.observed = []
    service, role = _service(tmp_path, provider)
    graphs, teams, runtime, executor = _executor(service)
    definition = _definition(
        role.id,
        role.version,
        team_id="turn-mail",
        nodes=(_node("first", role.id, role.version), _node("second", role.id, role.version)),
        edges=(
            EdgeSpec(
                edge_id="ordered",
                source_node="first",
                source_port="result",
                target_node="second",
                target_port="input",
            ),
        ),
    )
    teams.put_team_definition(_team("turn-mail", role.id, ("first", "second")))
    run = runtime.create_run(definition, workspace_or_target=str(tmp_path))
    prepared = executor.prepare(run.id)
    roster = {entry.member_id: entry for entry in teams.list_roster(prepared.team_run_id)}

    def send_mail():
        TeamRuntime(teams).send_message(
            MessageEnvelope(
                workflow_run_id=run.id,
                team_run_id=prepared.team_run_id,
                sender_id=roster["second"].agent_instance_id,
                recipient_ids=(roster["first"].agent_instance_id,),
                message_kind=MessageKind.FINDING,
                payload={"marker": "arrived-during-request"},
                audience=MessageAudience.DIRECT,
            ),
            idempotency_key="during-request",
        )

    provider.send_mail = send_mail
    result = await executor.run(run.id)
    assert result.completed
    first = [body for body in provider.observed if '"node_id":"first"' in body]
    second = [body for body in provider.observed if '"node_id":"second"' in body]
    assert len(first) == 2 and len(second) == 1
    assert "arrived-during-request" not in first[0]
    assert "arrived-during-request" in first[1]
    assert "arrived-during-request" not in second[0]
