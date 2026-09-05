from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from operant.api import create_app
from operant.application.graph import GraphRuntime
from operant.domain.graph import (
    EdgeSpec,
    IdempotencyClass,
    NodeKind,
    NodeSpec,
    PortSpec,
    WorkflowDefinition,
    WorkflowDefinitionStatus,
)
from operant.domain.multiwriter import (
    MergeNodePolicy,
    WriterIsolationKind,
    WriterNodePolicy,
    WriterWorkspace,
)
from operant.multiwriter.container import (
    ContainerLifecycleStatus,
    ContainerOutcomeUnknown,
    ContainerWriterSpec,
)
from operant.persistence.graph_team import SQLiteGraphRepository


def _definition(root_ref: str) -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id="beta-container",
        version=1,
        name="Beta container",
        status=WorkflowDefinitionStatus.PUBLISHED,
        nodes=(
            NodeSpec(
                node_id="writer",
                node_kind=NodeKind.AGENT,
                output_ports=(PortSpec(name="artifact"),),
                writes_workspace=True,
                idempotency_class=IdempotencyClass.IDEMPOTENT,
                writer_policy=WriterNodePolicy(
                    writer_key="container",
                    isolation_kind=WriterIsolationKind.CONTAINER,
                    isolation_ref=root_ref,
                    ownership_paths=("src",),
                ),
            ),
            NodeSpec(
                node_id="merge",
                node_kind=NodeKind.MERGE,
                input_ports=(PortSpec(name="artifacts"),),
                writes_workspace=True,
                idempotency_class=IdempotencyClass.IDEMPOTENT,
                merge_policy=MergeNodePolicy(source_writer_keys=("container", "review")),
            ),
            NodeSpec(
                node_id="review",
                node_kind=NodeKind.AGENT,
                output_ports=(PortSpec(name="artifact"),),
                writes_workspace=True,
                idempotency_class=IdempotencyClass.IDEMPOTENT,
                writer_policy=WriterNodePolicy(
                    writer_key="review",
                    isolation_kind=WriterIsolationKind.WORKTREE,
                    isolation_ref="worktree:review",
                    ownership_paths=("review",),
                ),
            ),
        ),
        edges=(
            EdgeSpec(
                edge_id="writer-merge",
                source_node="writer",
                source_port="artifact",
                target_node="merge",
                target_port="artifacts",
            ),
            EdgeSpec(
                edge_id="review-merge",
                source_node="review",
                source_port="artifact",
                target_node="merge",
                target_port="artifacts",
            ),
        ),
    )


class FakeContainerLifecycle:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.status = ContainerLifecycleStatus.ABSENT
        self.fail_stop = False

    @staticmethod
    def container_name(workspace_id: str) -> str:
        return f"operant-writer-{workspace_id}"

    def workspace_path(self, isolation_ref: str) -> Path:
        assert isolation_ref == "container:beta"
        return self.root

    def create(self, spec: ContainerWriterSpec) -> ContainerLifecycleStatus:
        assert spec.user_uid == 1000
        assert spec.user_gid == 1000
        self.status = ContainerLifecycleStatus.CREATED
        return self.status

    def start(self, workspace_id: str) -> ContainerLifecycleStatus:
        assert workspace_id
        self.status = ContainerLifecycleStatus.RUNNING
        return self.status

    def stop(self, workspace_id: str) -> ContainerLifecycleStatus:
        assert workspace_id
        if self.fail_stop:
            raise ContainerOutcomeUnknown("test unknown stop")
        self.status = ContainerLifecycleStatus.STOPPED
        return self.status

    def remove(self, workspace_id: str) -> ContainerLifecycleStatus:
        assert workspace_id
        self.status = ContainerLifecycleStatus.REMOVED
        return self.status

    def inspect(self, workspace_id: str) -> ContainerLifecycleStatus:
        assert workspace_id
        return self.status


def _setup(tmp_path: Path):
    root = tmp_path / "writer"
    root.mkdir()
    adapter = FakeContainerLifecycle(root)
    app = create_app(
        tmp_path / "beta.sqlite3",
        phase56_container_lifecycle=adapter,  # type: ignore[arg-type]
    )
    graph_repository = SQLiteGraphRepository(app.state.operant_service.store)
    definition = _definition("container:beta")
    graph_repository.put_definition(definition)
    run = GraphRuntime(graph_repository).create_run(definition)
    node_run = next(
        item for item in graph_repository.list_node_runs(run.id) if item.node_id == "writer"
    )
    runtime = app.state.multiwriter_runtime
    workspace = runtime.create_workspace(
        WriterWorkspace(
            graph_run_id=run.id,
            node_run_id=node_run.id,
            writer_key="container",
            isolation_kind=WriterIsolationKind.CONTAINER,
            isolation_ref="container:beta",
            base_revision="abcdef0",
            ownership_paths=("src",),
        )
    )
    lease = runtime.acquire_lease(workspace.writer_workspace_id, owner="container-owner")
    return app, adapter, workspace, lease


def test_container_lifecycle_is_durable_fenced_and_explicitly_reconciled(
    tmp_path: Path,
) -> None:
    app, adapter, workspace, lease = _setup(tmp_path)
    path = f"/v1/writer-workspaces/{workspace.writer_workspace_id}/container"
    create_body = {
        "image": f"sha256:{'a' * 64}",
        "command": ["python", "-m", "pytest"],
        "environment": {"OPERANT_WRITER_MODE": "test"},
        "user_uid": 1000,
        "user_gid": 1000,
        "resources": {"cpus": 1, "memory_bytes": 268435456, "pids": 64},
        "lease": lease.model_dump(mode="json"),
    }
    with TestClient(app) as client:
        created = client.post(
            path,
            headers={"Idempotency-Key": "container-create"},
            json=create_body,
        )
        assert created.status_code == 201, created.text
        assert created.json()["status"] == "created"

        started = client.post(
            f"{path}/start",
            headers={"Idempotency-Key": "container-start"},
            json={"lease": lease.model_dump(mode="json")},
        )
        assert started.status_code == 200, started.text
        assert started.json()["status"] == "running"

        adapter.fail_stop = True
        unknown = client.post(
            f"{path}/stop",
            headers={"Idempotency-Key": "container-stop"},
            json={"lease": lease.model_dump(mode="json")},
        )
        assert unknown.status_code == 409, unknown.text
        assert client.get(path).json()["status"] == "outcome_unknown"

        adapter.fail_stop = False
        adapter.status = ContainerLifecycleStatus.STOPPED
        reconciled = client.post(
            f"{path}/reconcile",
            headers={"Idempotency-Key": "container-reconcile"},
            json={"lease": lease.model_dump(mode="json")},
        )
        assert reconciled.status_code == 200, reconciled.text
        assert reconciled.json()["status"] == "stopped"
        assert reconciled.json()["revision"] == 7

    store = app.state.operant_service.store
    with store._connect() as connection:
        rows = connection.execute(
            "SELECT revision, event_type FROM writer_container_lifecycle_events "
            "WHERE writer_workspace_id = ? ORDER BY revision",
            (workspace.writer_workspace_id,),
        ).fetchall()
        assert [row["revision"] for row in rows] == list(range(1, 8))
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE writer_container_lifecycle_events SET event_type = 'changed' "
                "WHERE writer_workspace_id = ?",
                (workspace.writer_workspace_id,),
            )


def test_container_routes_are_local_and_side_effects_are_opt_in(tmp_path: Path) -> None:
    app = create_app(tmp_path / "disabled.sqlite3")
    document = app.openapi()
    assert (
        document["paths"]["/v1/writer-workspaces/{workspace_id}/container"]["post"]["operationId"]
        == "createContainerWriter"
    )
    assert app.state.container_writer_lifecycle is None
