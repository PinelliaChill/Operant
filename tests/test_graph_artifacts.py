"""Graph output publication, provenance, restart, and blob integrity boundaries."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from test_b24_graph_execution import (
    RecordingProvider,
    RecordingService,
    _definition,
    _executor,
    _node,
    _service,
    _team,
)

from operant.application.graph_artifacts import GraphArtifactError, publish_graph_artifact
from operant.domain.graph import (
    EdgeSpec,
    GraphRunStatus,
    IdempotencyClass,
    NodeKind,
    NodeSpec,
    PortSpec,
)
from operant.domain.threads import ArtifactAccessLevel, ArtifactSensitivity
from operant.persistence.sqlite import ConflictError, SQLiteStore


def _prepared(tmp_path: Path, *, content: str = "published text"):
    provider = RecordingProvider()
    service, role = _service(tmp_path, provider)
    graphs, teams, runtime, executor = _executor(service)
    teams.put_team_definition(_team("artifact-team", role.id, ("agent",)))
    node = NodeSpec(
        node_id="publish",
        node_kind=NodeKind.ARTIFACT,
        idempotency_class=IdempotencyClass.IDEMPOTENT,
        input_ports=(PortSpec(name="content", required=False),),
        output_ports=(PortSpec(name="artifact_id", value_type="string"),),
        metadata={"content": content, "title": "Graph output"},
    )
    definition = _definition(
        role.id,
        role.version,
        team_id="artifact-team",
        nodes=(_node("agent", role.id, role.version), node),
    )
    run = runtime.create_run(definition, workspace_or_target=str(tmp_path))
    executor.prepare(run.id)
    runtime.start_run(run.id)
    run = graphs.get_run(run.id)
    node_run = next(item for item in graphs.list_node_runs(run.id) if item.node_id == "publish")
    roster = teams.list_roster(run.team_run_id)
    publisher = roster[0].agent_instance_id
    sessions = {service.store.get_agent(entry.agent_instance_id).session_id for entry in roster}
    arguments = {
        "service": service,
        "team_repository": teams,
        "run": run,
        "node_run": node_run,
        "node": node,
        "publisher_id": publisher,
        "session_ids": sessions,
    }
    return service, graphs, teams, runtime, executor, arguments


def test_publication_keeps_raw_bytes_and_reuses_fact_after_restart(tmp_path: Path) -> None:
    service, _graphs, teams, _runtime, _executor_value, args = _prepared(tmp_path)
    first = publish_graph_artifact(**args)
    repeated = publish_graph_artifact(**args)
    assert repeated == first
    artifact = service.get_artifact(first["artifact_id"], verify=True)
    assert artifact.size_bytes == len(b"published text")
    assert artifact.retention_policy_ref == "graph-node-output"
    capability = service.issue_artifact_capability(
        artifact.id, operation="read", access_level=ArtifactAccessLevel.NORMAL
    )
    assert (
        service.read_artifact_text(artifact.id, capability=capability)["content_text"]
        == "published text"
    )
    service.close()
    restarted = RecordingService(
        SQLiteStore(service.store.path), RecordingProvider(), artifact_root=tmp_path / "artifacts"
    )
    restarted.initialize()
    args["service"] = restarted
    # The replacement service reconstructs provenance from persisted Board/store facts.
    args["node"] = args["node"].model_copy(update={"metadata": {"artifact_id": artifact.id}})
    assert publish_graph_artifact(**args) == first
    board = teams.list_artifacts(
        team_run_id=args["run"].team_run_id, viewer_id=args["publisher_id"], owner_audit=True
    )
    assert len(board) == 1


def test_unrelated_reference_and_sensitivity_collision_fail_closed(tmp_path: Path) -> None:
    service, _graphs, _teams, _runtime, _executor_value, args = _prepared(tmp_path)
    unrelated, _created = service.create_artifact(
        content=b"unrelated",
        media_type="text/plain",
        source_refs=(),
        retention_policy_ref="default",
    )
    args["node"] = args["node"].model_copy(update={"metadata": {"artifact_id": unrelated.id}})
    with pytest.raises(GraphArtifactError, match="not bound"):
        publish_graph_artifact(**args)
    service.create_artifact(
        content=b"private data",
        media_type="text/plain; charset=utf-8",
        sensitivity=ArtifactSensitivity.RESTRICTED,
        retention_policy_ref="graph-node-output",
    )
    args["node"] = args["node"].model_copy(update={"metadata": {"content": "private data"}})
    with pytest.raises(ConflictError, match="different metadata"):
        publish_graph_artifact(**args)
    assert (
        service.store.get_artifact_by_hash(hashlib.sha256(b"private data").hexdigest()).sensitivity
        is ArtifactSensitivity.RESTRICTED
    )


@pytest.mark.asyncio
async def test_agent_output_is_published_and_committed_by_graph_executor(tmp_path: Path) -> None:
    provider = RecordingProvider()
    service, role = _service(tmp_path, provider)
    graphs, teams, runtime, executor = _executor(service)
    teams.put_team_definition(_team("combined-artifact", role.id, ("agent",)))
    definition = _definition(
        role.id,
        role.version,
        team_id="combined-artifact",
        nodes=(
            _node("agent", role.id, role.version),
            NodeSpec(
                node_id="publish",
                node_kind=NodeKind.ARTIFACT,
                idempotency_class=IdempotencyClass.IDEMPOTENT,
                input_ports=(PortSpec(name="content", value_type="string"),),
                output_ports=(PortSpec(name="artifact_id", value_type="string"),),
                metadata={"content": {"$input": "content"}},
            ),
        ),
        edges=(
            EdgeSpec(
                edge_id="agent-output",
                source_node="agent",
                source_port="result",
                target_node="publish",
                target_port="content",
                delivery_mode="value",
            ),
        ),
    )
    run = runtime.create_run(definition, workspace_or_target=str(tmp_path))
    result = await executor.run(run.id)
    assert result.status is GraphRunStatus.COMPLETED
    nodes = {node.node_id: node for node in result.node_runs}
    published = service.get_artifact(nodes["publish"].output_refs["artifact_id"], verify=True)
    assert published.size_bytes > 0
    assert len(graphs.list_attempts(nodes["publish"].id)) == 1
    assert len(provider.calls) == 1
    assert (await executor.run(run.id)).status is GraphRunStatus.COMPLETED
    assert len(provider.calls) == 1
    assert len(graphs.list_attempts(nodes["publish"].id)) == 1


def test_identical_content_is_scoped_to_each_graph_without_duplicate_blobs(tmp_path: Path) -> None:
    service, graphs, teams, runtime, executor, args = _prepared(tmp_path)
    first = publish_graph_artifact(**args)
    definition = graphs.get_definition(
        args["run"].workflow_definition_id, args["run"].workflow_definition_version
    )
    second_run = runtime.create_run(definition, workspace_or_target=str(tmp_path))
    executor.prepare(second_run.id)
    runtime.start_run(second_run.id)
    second_run = graphs.get_run(second_run.id)
    roster = teams.list_roster(second_run.team_run_id)
    args.update(
        run=second_run,
        node_run=next(
            item for item in graphs.list_node_runs(second_run.id) if item.node_id == "publish"
        ),
        publisher_id=roster[0].agent_instance_id,
        session_ids={
            service.store.get_agent(entry.agent_instance_id).session_id for entry in roster
        },
    )
    second = publish_graph_artifact(**args)
    assert second == first
    assert (
        len(
            teams.list_artifacts(
                team_run_id=second_run.team_run_id, viewer_id=args["publisher_id"], owner_audit=True
            )
        )
        == 1
    )
    with service.store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM artifact_board_items").fetchone()[0] == 2


def test_corrupted_published_body_is_not_accepted_as_a_graph_reference(tmp_path: Path) -> None:
    from operant.artifacts.store import ArtifactStore, ArtifactStoreError

    service, _graphs, _teams, _runtime, _executor_value, args = _prepared(tmp_path)
    published = publish_graph_artifact(**args)
    artifact = service.get_artifact(published["artifact_id"], verify=True)
    path = tmp_path / "artifacts" / ArtifactStore.storage_key_for_hash(artifact.content_hash)
    path.write_bytes(b"tampered body")
    args["node"] = args["node"].model_copy(update={"metadata": {"artifact_id": artifact.id}})
    with pytest.raises(ArtifactStoreError):
        publish_graph_artifact(**args)
