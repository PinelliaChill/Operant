from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from operant.api import create_app
from operant.application.workflow import CodingWorkflowGraphBridge
from operant.domain.threads import ConversationThread
from operant.domain.workflow import WorkflowRun


def _headers(key: str) -> dict[str, str]:
    return {"Idempotency-Key": key}


def _graph_definition() -> dict[str, object]:
    return {
        "workflow_id": "api.graph",
        "version": 1,
        "name": "API Graph",
        "nodes": [
            {
                "node_id": "artifact",
                "node_kind": "artifact",
                "input_ports": [],
                "output_ports": [],
            }
        ],
        "status": "draft",
    }


def _seed_agent(client: TestClient, agent_id: str) -> str:
    store = client.app.state.operant_service.store
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


def test_phase23_graph_protocol_compile_publish_run_and_receipt_replay(tmp_path) -> None:
    app = create_app(tmp_path / "operant.db", artifact_root=tmp_path / "artifacts")
    with TestClient(app) as client:
        protocol = client.get("/v1/protocol/phase23")
        assert protocol.status_code == 200
        assert protocol.json()["protocol_version"] == "phase23.v1"

        created = client.post(
            "/v1/graph/workflows/drafts",
            headers=_headers("draft-1"),
            json=_graph_definition(),
        )
        assert created.status_code == 202
        replay = client.post(
            "/v1/graph/workflows/drafts",
            headers=_headers("draft-1"),
            json=_graph_definition(),
        )
        assert replay.status_code == 202
        assert replay.headers["Idempotency-Replayed"] == "true"
        assert replay.json() == created.json()

        compiled = client.post(
            "/v1/graph/workflows/api.graph/compile",
            headers=_headers("compile-1"),
            json={"definition_version": 1},
        )
        assert compiled.status_code == 200
        assert compiled.json()["valid"] is True
        published = client.post(
            "/v1/graph/workflows/api.graph/publish",
            headers=_headers("publish-1"),
            json={"draft_version": 1},
        )
        assert published.status_code == 202

        started = client.post(
            "/v1/graph/runs",
            headers=_headers("run-1"),
            json={
                "workflow_id": "api.graph",
                "definition_version": 2,
                "input": {"task": "bounded"},
            },
        )
        assert started.status_code == 202
        run_id = started.json()["resource_id"]
        projection = client.get(f"/v1/graph/runs/{run_id}")
        assert projection.status_code == 200
        assert projection.json()["status"] == "running"
        cancelled = client.post(f"/v1/graph/runs/{run_id}/cancel", headers=_headers("cancel-1"))
        assert cancelled.status_code == 202
        assert client.get(f"/v1/graph/runs/{run_id}").json()["status"] == "cancelled"


def test_phase23_rejects_conflicting_graph_team_bindings_without_partial_writes(
    tmp_path,
) -> None:
    app = create_app(tmp_path / "operant.db", artifact_root=tmp_path / "artifacts")
    with TestClient(app) as client:
        assert (
            client.post(
                "/v1/graph/workflows/drafts",
                headers=_headers("binding-draft"),
                json=_graph_definition(),
            ).status_code
            == 202
        )
        assert (
            client.post(
                "/v1/graph/workflows/api.graph/publish",
                headers=_headers("binding-publish"),
                json={"draft_version": 1},
            ).status_code
            == 202
        )
        graph_response = client.post(
            "/v1/graph/runs",
            headers=_headers("binding-graph-1"),
            json={"workflow_id": "api.graph", "definition_version": 2},
        )
        assert graph_response.status_code == 202
        graph_run_id = graph_response.json()["resource_id"]

        thread_id = _seed_agent(client, "agent_binding")
        team_definition = {
            "team_id": "api.binding-team",
            "version": 1,
            "members": [
                {
                    "member_id": "worker",
                    "agent_definition_id": "role_worker",
                    "role": "worker",
                }
            ],
            "default_coordinator": "worker",
        }
        assert (
            client.post(
                "/v1/teams/definitions",
                headers=_headers("binding-team-definition"),
                json=team_definition,
            ).status_code
            == 202
        )
        team_body = {
            "team_id": "api.binding-team",
            "team_version": 1,
            "workflow_run_id": graph_run_id,
            "roster": [
                {
                    "member_id": "worker",
                    "agent_instance_id": "agent_binding",
                    "thread_id": thread_id,
                }
            ],
        }
        team_response = client.post(
            "/v1/teams/runs",
            headers=_headers("binding-team-1"),
            json=team_body,
        )
        assert team_response.status_code == 202
        team_run_id = team_response.json()["resource_id"]
        graph_projection = client.get(f"/v1/graph/runs/{graph_run_id}")
        assert graph_projection.status_code == 200
        assert graph_projection.json()["team_run_id"] == team_run_id
        bound_graph = graph_projection.json()

        store = client.app.state.operant_service.store
        with store._connect() as connection:
            graph_counts_before = (
                connection.execute("SELECT COUNT(*) FROM graph_workflow_runs").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM node_runs").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM graph_run_events").fetchone()[0],
            )

        unsupported_reverse_binding = client.post(
            "/v1/graph/runs",
            headers=_headers("binding-graph-team-input"),
            json={
                "workflow_id": "api.graph",
                "definition_version": 2,
                "team_run_id": team_run_id,
            },
        )
        assert unsupported_reverse_binding.status_code == 422
        with store._connect() as connection:
            assert (
                connection.execute("SELECT COUNT(*) FROM graph_workflow_runs").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM node_runs").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM graph_run_events").fetchone()[0],
            ) == graph_counts_before

        with store._connect() as connection:
            team_counts_before = (
                connection.execute("SELECT COUNT(*) FROM team_runs").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM team_roster").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM team_run_events").fetchone()[0],
            )

        conflicting_team = client.post(
            "/v1/teams/runs",
            headers=_headers("binding-team-conflict"),
            json=team_body,
        )
        assert conflicting_team.status_code == 409
        assert conflicting_team.json()["detail"] == (
            "Graph Run is already bound to a different Team Run"
        )
        assert client.get(f"/v1/graph/runs/{graph_run_id}").json() == bound_graph
        with store._connect() as connection:
            assert (
                connection.execute("SELECT COUNT(*) FROM team_runs").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM team_roster").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM team_run_events").fetchone()[0],
            ) == team_counts_before


def test_phase23_resolves_graph_run_linked_to_frozen_coding_workflow(tmp_path) -> None:
    app = create_app(tmp_path / "operant.db", artifact_root=tmp_path / "artifacts")
    with TestClient(app) as client:
        service = client.app.state.operant_service
        legacy = service.create_workflow_run(
            WorkflowRun(
                task="compatibility projection",
                workspace=str(tmp_path),
                planner_role_id="planner",
                coder_role_id="coder",
                reviewer_role_id="reviewer",
            )
        )
        bridge = CodingWorkflowGraphBridge.create(
            client.app.state.graph_runtime,
            legacy_workflow_run_id=legacy.id,
            task=legacy.task,
            workspace=tmp_path,
            planner_role_id=legacy.planner_role_id,
            explorer_role_ids=legacy.explorer_role_ids,
            coder_role_id=legacy.coder_role_id,
            reviewer_role_id=legacy.reviewer_role_id,
            main_role_id=legacy.main_role_id,
            max_parallel_explorers=legacy.max_parallel_explorers,
            max_rework_rounds=legacy.max_rework_rounds,
            resumed_from_id=None,
        )

        projection = client.get(f"/v1/graph/runs/by-legacy/{legacy.id}")

        assert projection.status_code == 200
        assert projection.json()["id"] == bridge.graph_run_id
        assert projection.json()["legacy_workflow_run_id"] == legacy.id


def test_phase23_local_team_mailbox_and_ack(tmp_path) -> None:
    app = create_app(tmp_path / "operant.db", artifact_root=tmp_path / "artifacts")
    with TestClient(app) as client:
        client.post(
            "/v1/graph/workflows/drafts",
            headers=_headers("draft-team"),
            json=_graph_definition(),
        )
        client.post(
            "/v1/graph/workflows/api.graph/publish",
            headers=_headers("publish-team"),
            json={"draft_version": 1},
        )
        graph_response = client.post(
            "/v1/graph/runs",
            headers=_headers("run-team"),
            json={"workflow_id": "api.graph", "definition_version": 2, "input": {}},
        )
        graph_run_id = graph_response.json()["resource_id"]
        sender_thread = _seed_agent(client, "agent_sender")
        recipient_thread = _seed_agent(client, "agent_recipient")
        observer_thread = _seed_agent(client, "agent_observer")
        overflow_thread = _seed_agent(client, "agent_overflow")
        _seed_agent(client, "agent_outsider")

        definition = {
            "team_id": "api.team",
            "version": 1,
            "members": [
                {
                    "member_id": "sender",
                    "agent_definition_id": "role_sender",
                    "role": "sender",
                },
                {
                    "member_id": "recipient",
                    "agent_definition_id": "role_recipient",
                    "role": "recipient",
                },
                {
                    "member_id": "observer",
                    "agent_definition_id": "role_observer",
                    "role": "observer",
                },
                {
                    "member_id": "overflow",
                    "agent_definition_id": "role_overflow",
                    "role": "overflow",
                },
            ],
            "default_coordinator": "sender",
            "max_active_agents": 3,
        }
        assert (
            client.post(
                "/v1/teams/definitions",
                headers=_headers("team-definition"),
                json=definition,
            ).status_code
            == 202
        )
        empty_roster = client.post(
            "/v1/teams/runs",
            headers=_headers("team-run-empty"),
            json={
                "team_id": "api.team",
                "team_version": 1,
                "workflow_run_id": graph_run_id,
                "roster": [],
            },
        )
        assert empty_roster.status_code == 422
        oversized_roster = client.post(
            "/v1/teams/runs",
            headers=_headers("team-run-overflow"),
            json={
                "team_id": "api.team",
                "team_version": 1,
                "workflow_run_id": graph_run_id,
                "roster": [
                    {
                        "member_id": "sender",
                        "agent_instance_id": "agent_sender",
                        "thread_id": sender_thread,
                    },
                    {
                        "member_id": "recipient",
                        "agent_instance_id": "agent_recipient",
                        "thread_id": recipient_thread,
                    },
                    {
                        "member_id": "observer",
                        "agent_instance_id": "agent_observer",
                        "thread_id": observer_thread,
                    },
                    {
                        "member_id": "overflow",
                        "agent_instance_id": "agent_overflow",
                        "thread_id": overflow_thread,
                    },
                ],
            },
        )
        assert oversized_roster.status_code == 422
        invalid_roster = client.post(
            "/v1/teams/runs",
            headers=_headers("team-run-missing-agent"),
            json={
                "team_id": "api.team",
                "team_version": 1,
                "workflow_run_id": graph_run_id,
                "roster": [
                    {
                        "member_id": "sender",
                        "agent_instance_id": "agent_sender",
                        "thread_id": sender_thread,
                    },
                    {
                        "member_id": "recipient",
                        "agent_instance_id": "missing_agent",
                        "thread_id": recipient_thread,
                    },
                    {
                        "member_id": "observer",
                        "agent_instance_id": "agent_observer",
                        "thread_id": observer_thread,
                    },
                ],
            },
        )
        assert invalid_roster.status_code == 422
        duplicate_agent = client.post(
            "/v1/teams/runs",
            headers=_headers("team-run-duplicate-agent"),
            json={
                "team_id": "api.team",
                "team_version": 1,
                "workflow_run_id": graph_run_id,
                "roster": [
                    {
                        "member_id": "sender",
                        "agent_instance_id": "agent_sender",
                        "thread_id": sender_thread,
                    },
                    {
                        "member_id": "recipient",
                        "agent_instance_id": "agent_sender",
                        "thread_id": recipient_thread,
                    },
                    {
                        "member_id": "observer",
                        "agent_instance_id": "agent_observer",
                        "thread_id": observer_thread,
                    },
                ],
            },
        )
        assert duplicate_agent.status_code == 422
        with client.app.state.operant_service.store._connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM team_runs").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM team_roster").fetchone()[0] == 0
        assert client.get(f"/v1/graph/runs/{graph_run_id}").json()["team_run_id"] is None
        team_response = client.post(
            "/v1/teams/runs",
            headers=_headers("team-run"),
            json={
                "team_id": "api.team",
                "team_version": 1,
                "workflow_run_id": graph_run_id,
                "roster": [
                    {
                        "member_id": "sender",
                        "agent_instance_id": "agent_sender",
                        "thread_id": sender_thread,
                    },
                    {
                        "member_id": "recipient",
                        "agent_instance_id": "agent_recipient",
                        "thread_id": recipient_thread,
                    },
                    {
                        "member_id": "observer",
                        "agent_instance_id": "agent_observer",
                        "thread_id": observer_thread,
                    },
                ],
            },
        )
        assert team_response.status_code == 202
        team_run_id = team_response.json()["resource_id"]

        outside_message = client.post(
            f"/v1/teams/runs/{team_run_id}/messages",
            headers=_headers("outside-message"),
            json={
                "sender_id": "agent_outsider",
                "recipient_ids": ["agent_recipient"],
                "message_kind": "Finding",
                "payload": {"summary": "must not project"},
            },
        )
        assert outside_message.status_code == 422
        assert client.get(f"/v1/teams/runs/{team_run_id}/mailbox/agent_outsider").status_code == 422
        invalid_task = client.post(
            f"/v1/teams/runs/{team_run_id}/tasks/task-invalid",
            headers=_headers("outside-task"),
            json={
                "title": "must not persist",
                "assignee_ids": ["agent_outsider"],
                "expected_revision": 0,
            },
        )
        assert invalid_task.status_code == 422
        invalid_message_ref = client.post(
            f"/v1/teams/runs/{team_run_id}/messages",
            headers=_headers("invalid-message-ref"),
            json={
                "sender_id": "agent_sender",
                "recipient_ids": ["agent_recipient"],
                "message_kind": "Finding",
                "payload": {},
                "artifact_refs": ["artifact_missing"],
            },
        )
        assert invalid_message_ref.status_code == 422
        with client.app.state.operant_service.store._connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM team_messages").fetchone()[0] == 0
            assert connection.execute("SELECT COUNT(*) FROM team_tasks").fetchone()[0] == 0

        message_response = client.post(
            f"/v1/teams/runs/{team_run_id}/messages",
            headers=_headers("team-message"),
            json={
                "sender_id": "agent_sender",
                "recipient_ids": ["agent_recipient"],
                "message_kind": "Finding",
                "payload": {"summary": "result"},
            },
        )
        assert message_response.status_code == 202
        assert client.get(f"/v1/teams/runs/{team_run_id}/messages").status_code == 422
        assert (
            client.get(
                f"/v1/teams/runs/{team_run_id}/messages",
                params={"viewer_id": "agent_outsider"},
            ).status_code
            == 422
        )
        observer_messages = client.get(
            f"/v1/teams/runs/{team_run_id}/messages",
            params={"viewer_id": "agent_observer"},
        )
        assert observer_messages.status_code == 200
        assert observer_messages.json() == []
        for viewer_id in ("agent_sender", "agent_recipient"):
            visible = client.get(
                f"/v1/teams/runs/{team_run_id}/messages",
                params={"viewer_id": viewer_id},
            )
            assert visible.status_code == 200
            assert [item["payload"] for item in visible.json()] == [{"summary": "result"}]
        mailbox = client.get(f"/v1/teams/runs/{team_run_id}/mailbox/agent_recipient")
        assert mailbox.status_code == 200
        delivery = mailbox.json()["deliveries"][0]
        acked = client.post(
            f"/v1/teams/runs/{team_run_id}/mailbox/agent_recipient/{delivery['delivery_id']}/ack",
            headers=_headers("team-ack"),
        )
        assert acked.status_code == 202
        ack_replay = client.post(
            f"/v1/teams/runs/{team_run_id}/mailbox/agent_recipient/{delivery['delivery_id']}/ack",
            headers=_headers("team-ack"),
        )
        assert ack_replay.status_code == 202
        assert ack_replay.headers["Idempotency-Replayed"] == "true"
        refreshed_delivery = client.get(
            f"/v1/teams/runs/{team_run_id}/mailbox/agent_recipient"
        ).json()["deliveries"][0]
        assert refreshed_delivery["status"] == "acked"
        assert refreshed_delivery["acked_at"] is not None
