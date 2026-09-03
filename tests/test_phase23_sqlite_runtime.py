from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from operant.application.graph import GraphConflictError, GraphRuntime
from operant.application.team import TeamRuntime
from operant.domain.graph import (
    AttemptSideEffectState,
    GraphRunStatus,
    IdempotencyClass,
    NodeKind,
    NodeRunStatus,
    NodeSpec,
    WorkflowDefinition,
    WorkflowDefinitionStatus,
)
from operant.domain.team import (
    MessageAck,
    MessageEnvelope,
    MessageKind,
    RosterEntry,
    TeamDefinition,
    TeamMember,
    TeamRun,
)
from operant.domain.threads import ConversationThread
from operant.persistence.graph_team import SQLiteGraphRepository, SQLiteTeamRepository
from operant.persistence.sqlite import SQLiteStore


def _published_definition(*, writer: bool = False) -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id="sqlite.graph",
        version=1,
        name="sqlite graph",
        nodes=(
            NodeSpec(
                node_id="node",
                node_kind=NodeKind.TOOL,
                idempotency_class=(
                    IdempotencyClass.NON_IDEMPOTENT if writer else IdempotencyClass.PURE
                ),
                writes_workspace=writer,
            ),
        ),
        status=WorkflowDefinitionStatus.PUBLISHED,
    )


def test_sqlite_graph_runtime_persists_and_recovers_unknown_side_effect(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "operant.db")
    store.initialize()
    repository = SQLiteGraphRepository(store)
    runtime = GraphRuntime(repository)
    run = runtime.create_run(_published_definition(writer=True))
    runtime.start_run(run.id)
    node = repository.list_node_runs(run.id)[0]
    attempt = runtime.start_attempt(node.id, idempotency_key="writer-1")
    runtime.mark_side_effect_started(attempt.id)

    restarted = GraphRuntime(SQLiteGraphRepository(SQLiteStore(store.path)))
    recovered = restarted.recover(run.id)

    assert recovered.status is GraphRunStatus.MANUAL_RECONCILE_REQUIRED
    assert repository.get_node_run(node.id).status is NodeRunStatus.MANUAL_RECONCILE_REQUIRED
    assert repository.get_attempt(attempt.id).side_effect_state is AttemptSideEffectState.UNKNOWN
    event = repository.list_events(run.id)[0]
    assert event["event_id"].startswith(f"graph_event_{run.id}_")
    assert event["run_sequence"] == 1
    assert event["schema_version"] == "phase23.v1"


def _seed_agent(store: SQLiteStore, agent_id: str) -> str:
    thread = store.create_thread(ConversationThread())
    now = datetime.now(timezone.utc).isoformat()
    with store._connect() as connection:
        connection.execute(
            "INSERT INTO sessions(id, body, created_at) VALUES (?, '{}', ?)",
            (f"session_{agent_id}", now),
        )
        connection.execute(
            """
            INSERT INTO agents(id, session_id, status, body, created_at)
            VALUES (?, ?, 'idle', '{}', ?)
            """,
            (agent_id, f"session_{agent_id}", now),
        )
    return thread.id


def test_sqlite_team_message_mailbox_and_ack_are_atomic_and_idempotent(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "operant.db")
    store.initialize()
    graph_repository = SQLiteGraphRepository(store)
    graph_runtime = GraphRuntime(graph_repository)
    graph_run = graph_runtime.create_run(_published_definition())
    sender_thread = _seed_agent(store, "agent_sender")
    recipient_thread = _seed_agent(store, "agent_recipient")

    team_repository = SQLiteTeamRepository(store)
    team_runtime = TeamRuntime(team_repository)
    definition = TeamDefinition(
        team_id="team.sqlite",
        members=(
            TeamMember(member_id="sender", agent_definition_id="role_sender", role="sender"),
            TeamMember(
                member_id="recipient",
                agent_definition_id="role_recipient",
                role="recipient",
            ),
        ),
        default_coordinator="sender",
    )
    team_repository.put_team_definition(definition)
    team_run = team_repository.put_team_run(
        TeamRun(
            team_id=definition.team_id,
            team_version=definition.version,
            workflow_run_id=graph_run.id,
        )
    )
    team_repository.put_roster_entry(
        RosterEntry(
            team_run_id=team_run.team_run_id,
            member_id="sender",
            agent_instance_id="agent_sender",
            thread_id=sender_thread,
        )
    )
    team_repository.put_roster_entry(
        RosterEntry(
            team_run_id=team_run.team_run_id,
            member_id="recipient",
            agent_instance_id="agent_recipient",
            thread_id=recipient_thread,
        )
    )
    message = MessageEnvelope(
        workflow_run_id=graph_run.id,
        team_run_id=team_run.team_run_id,
        sender_id="agent_sender",
        recipient_ids=("agent_recipient",),
        message_kind=MessageKind.FINDING,
        payload={"summary": "bounded"},
    )

    first = team_runtime.send_message(message, idempotency_key="send-1")
    replay = team_runtime.send_message(message, idempotency_key="send-1")
    assert first.created is True
    assert replay.created is False
    assert replay.deliveries == first.deliveries
    inbox = team_repository.list_inbox(
        team_run_id=team_run.team_run_id,
        recipient_id="agent_recipient",
    )
    assert len(inbox) == 1
    delivery, delivered_message = inbox[0]
    assert delivered_message == message
    assert delivery.cursor is not None
    ack = MessageAck(
        delivery_id=delivery.delivery_id,
        message_id=message.message_id,
        team_run_id=team_run.team_run_id,
        recipient_id="agent_recipient",
        idempotency_key="ack-1",
        cursor=delivery.cursor,
    )
    assert team_runtime.acknowledge(ack) == ack
    retry = ack.model_copy(update={"ack_id": "ack_retry_should_not_replace_first_fact"})
    assert team_runtime.acknowledge(retry) == ack

    with pytest.raises(ValueError, match="different input"):
        team_runtime.send_message(
            message.model_copy(update={"payload": {"summary": "changed"}}),
            idempotency_key="send-1",
        )

    events = team_repository.list_events(team_run.team_run_id)
    assert [event["run_sequence"] for event in events] == list(range(1, len(events) + 1))
    assert len({event["event_id"] for event in events}) == len(events)
    assert {event["schema_version"] for event in events} == {"phase23.v1"}


def test_sqlite_team_creation_atomically_binds_graph_and_rolls_back_after_cas_failure(
    tmp_path, monkeypatch
) -> None:
    store = SQLiteStore(tmp_path / "operant.db")
    store.initialize()
    graph_repository = SQLiteGraphRepository(store)
    graph_run = GraphRuntime(graph_repository).create_run(_published_definition())
    thread_id = _seed_agent(store, "agent_atomic")
    team_repository = SQLiteTeamRepository(store)
    definition = TeamDefinition(
        team_id="team.atomic",
        members=(TeamMember(member_id="worker", agent_definition_id="role_worker", role="worker"),),
        default_coordinator="worker",
    )
    team_repository.put_team_definition(definition)
    team_run = TeamRun(
        team_id=definition.team_id,
        team_version=definition.version,
        workflow_run_id=graph_run.id,
    )
    entries = (
        RosterEntry(
            team_run_id=team_run.team_run_id,
            member_id="worker",
            agent_instance_id="agent_atomic",
            thread_id=thread_id,
        ),
    )

    def fail_graph_event(*args, **kwargs) -> None:
        raise RuntimeError("injected graph event failure")

    monkeypatch.setattr(
        SQLiteGraphRepository,
        "_append_event",
        staticmethod(fail_graph_event),
    )
    with pytest.raises(RuntimeError, match="injected graph event failure"):
        team_repository.bind_graph_and_put_team_run_with_roster(team_run, entries)

    unchanged_graph = graph_repository.get_run(graph_run.id)
    assert unchanged_graph.team_run_id is None
    assert unchanged_graph.revision == graph_run.revision
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM team_runs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM team_roster").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM team_run_events").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM graph_run_events").fetchone()[0] == 1


def test_sqlite_team_binding_concurrent_cas_has_one_winner(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "operant.db")
    store.initialize()
    graph_repository = SQLiteGraphRepository(store)
    graph_run = GraphRuntime(graph_repository).create_run(_published_definition())
    thread_id = _seed_agent(store, "agent_cas")
    team_repository = SQLiteTeamRepository(store)
    definition = TeamDefinition(
        team_id="team.cas",
        members=(TeamMember(member_id="worker", agent_definition_id="role_worker", role="worker"),),
        default_coordinator="worker",
    )
    team_repository.put_team_definition(definition)
    candidates = tuple(
        TeamRun(
            team_id=definition.team_id,
            team_version=definition.version,
            workflow_run_id=graph_run.id,
        )
        for _ in range(2)
    )

    def bind(candidate: TeamRun) -> str:
        repository = SQLiteTeamRepository(SQLiteStore(store.path))
        repository.bind_graph_and_put_team_run_with_roster(
            candidate,
            (
                RosterEntry(
                    team_run_id=candidate.team_run_id,
                    member_id="worker",
                    agent_instance_id="agent_cas",
                    thread_id=thread_id,
                ),
            ),
        )
        return candidate.team_run_id

    winners: list[str] = []
    conflicts = 0
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(bind, candidate) for candidate in candidates]
        for future in futures:
            try:
                winners.append(future.result())
            except GraphConflictError as exc:
                assert str(exc) == "Graph Run is already bound to a different Team Run"
                conflicts += 1

    assert len(winners) == 1
    assert conflicts == 1
    bound_graph = graph_repository.get_run(graph_run.id)
    assert bound_graph.team_run_id == winners[0]
    assert bound_graph.revision == graph_run.revision + 1
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM team_runs").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM team_roster").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM team_run_events").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM graph_run_events").fetchone()[0] == 2
