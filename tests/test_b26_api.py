"""Public B2-6 boundary checks using an isolated real Core and ledger."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from operant.api import create_app
from operant.persistence.sqlite import MigrationError, SQLiteStore


def test_v18_preserves_prior_checksums_and_protects_unresolved_commands(tmp_path):
    store = SQLiteStore(tmp_path / "migration.sqlite3")
    store.migrate(17)
    prior = store.list_applied_migrations()
    store.migrate(18)
    assert store.list_applied_migrations()[:17] == prior
    assert store.rollback(17, isolated=True) == 17
    store.migrate(18)
    with store._connect() as connection:
        connection.execute(
            "INSERT INTO b26_commands VALUES(?,?,?,?,NULL)",
            ("command-pending", "project", "a" * 64, "outcome_unknown"),
        )
    with pytest.raises(MigrationError, match="experience evidence"):
        store.rollback(17, isolated=True)
    assert store.schema_version() == 18
    with store._connect() as connection:
        assert (
            connection.execute("SELECT state FROM b26_commands").fetchone()[0] == "outcome_unknown"
        )


def setup(client: TestClient, tmp_path):
    def command(**body):
        response = client.post("/v1/b2-3/commands", json=body)
        assert response.status_code == 200, response.text
        return response.json()

    project = command(action="project_create", name="B26 test", workspace_path=str(tmp_path))[
        "state"
    ]["projects"][-1]["project_id"]
    installation = command(
        action="plugin_install", plugin_id="memory-standard", mode="trusted_in_process"
    )["state"]["installations"][-1]["installation_id"]
    command(action="binding_select", project_id=project, installation_id=installation)
    command(
        action="memory_save",
        project_id=project,
        content="执行检查时先读取说明，再核对版本。",
        confirmed=True,
    )
    return project


def test_experience_projection_and_protocol(tmp_path):
    with TestClient(create_app(tmp_path / "core.sqlite3")) as client:
        project = setup(client, tmp_path)
        response = client.get(f"/v1/b2-6/projects/{project}/experience")
        assert response.status_code == 200, response.text
        state = response.json()
        assert state["project_id"] == project
        assert state["skills"]["skills"] == []
        assert state["sharing"]["grants"] == []
        assert state["remote"]["records"]
        assert client.get("/v1/protocol/b2-6").json()["protocol_version"] == "b2-6.v1"


def test_project_projection_excludes_foreign_dataset_ownership(tmp_path):
    first_workspace, second_workspace = tmp_path / "first", tmp_path / "second"
    first_workspace.mkdir()
    second_workspace.mkdir()
    with TestClient(create_app(tmp_path / "core.sqlite3")) as client:
        first = setup(client, first_workspace)
        second = setup(client, second_workspace)
        first_state = client.get(f"/v1/b2-6/projects/{first}/experience").json()
        second_state = client.get(f"/v1/b2-6/projects/{second}/experience").json()
        assert len(first_state["datasets"]) == len(second_state["datasets"]) == 1
        assert first_state["datasets"][0]["dataset_id"] == first_state["remote"]["dataset_id"]
        assert second_state["datasets"][0]["dataset_id"] != first_state["datasets"][0]["dataset_id"]


def test_procedure_candidate_then_skill_validation_and_publish(tmp_path):
    with TestClient(create_app(tmp_path / "core.sqlite3")) as client:
        project = setup(client, tmp_path)
        record = client.get(f"/v1/b2-5/projects/{project}/governance").json()["records"][0][
            "version"
        ]
        state = client.get(f"/v1/b2-6/projects/{project}/experience").json()
        source = dict(
            source_type="memory_version",
            source_id=record["ref"]["record_id"],
            revision=record["ref"]["version"],
            content_digest=record["ref"]["content_digest"],
            scope=record["scope"],
            permission_epoch=state["remote"]["permission_epoch"],
            availability="available",
        )

        def execute(body, key):
            response = client.post(
                "/v1/b2-6/commands",
                json={"project_id": project, **body},
                headers={"Idempotency-Key": key},
            )
            assert response.status_code == 200, response.text
            return response.json()

        proposed = execute(
            dict(
                action="procedure_propose", content="1. 读取说明。\n2. 核对版本。", sources=[source]
            ),
            "procedure-1",
        )
        assert proposed["status"] == "completed"
        inbox = client.get(f"/v1/b2-5/projects/{project}/governance").json()["proposals"]
        procedure = next(e for e in inbox if e["version"]["content_type"] == "procedure")
        p = procedure["proposal"]
        reviewed = client.post(
            "/v1/b2-5/commands",
            json=dict(
                action="review",
                project_id=project,
                decision="accept",
                selections=[
                    dict(
                        proposal_id=p["proposal_id"],
                        proposal_revision=p["proposal_revision"],
                        proposed_version=p["proposed_version"],
                        base_head_revision=p["base_head"]["revision"],
                    )
                ],
            ),
        )
        assert reviewed.status_code == 200, reviewed.text
        drafted = execute(
            dict(
                action="skill_draft",
                procedure_ref=procedure["version"]["ref"],
                name="check-version",
                description="检查前核对版本",
            ),
            "draft-1",
        )
        skill = drafted["state"]["skills"]["skills"][0]
        exact = dict(
            skill_id=skill["skill_id"],
            skill_version=skill["version"]["version"],
            expected_head_revision=skill["head"]["head_revision"],
            permission_epoch=skill["head"]["permission_epoch"],
        )
        execute(dict(action="skill_validate", **exact), "validate-1")
        published = execute(dict(action="skill_publish", **exact), "publish-1")
        assert published["state"]["skills"]["skills"][0]["head"]["state"] == "published"
        replay = execute(dict(action="skill_publish", **exact), "publish-1")
        assert replay["affected_ids"] == published["affected_ids"]
        events = client.get(f"/v1/b2-6/projects/{project}/events").json()["events"]
        assert len([e for e in events if e["action"] == "skill_publish"]) == 1
