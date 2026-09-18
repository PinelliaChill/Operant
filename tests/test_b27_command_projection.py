"""Large typed command projections must retain exact IDs and replay semantics."""

import pytest
from fastapi.testclient import TestClient

from operant.api import create_app
from operant.contracts.b2_5 import B25Result
from operant.contracts.b2_6 import B26Result


@pytest.mark.parametrize("version", ["b2-5", "b2-6"])
def test_large_governed_command_and_replay_preserve_shape_and_redaction(tmp_path, version):
    with TestClient(create_app(tmp_path / "core.sqlite3")) as client:

        def management(**body):
            response = client.post("/v1/b2-3/commands", json=body)
            assert response.status_code == 200, response.text
            return response.json()

        project = management(
            action="project_create", name="large projection", workspace_path=str(tmp_path)
        )["state"]["projects"][-1]["project_id"]
        installation = management(
            action="plugin_install", plugin_id="memory-standard", mode="trusted_in_process"
        )["state"]["installations"][-1]["installation_id"]
        management(action="binding_select", project_id=project, installation_id=installation)
        for index in range(16):
            management(
                action="memory_save",
                project_id=project,
                content=f"Synthetic record {index}; password=not-a-real-credential-{index}",
                confirmed=True,
            )
        body = {"action": "propose", "project_id": project, "content": "Synthetic next procedure"}
        model = B25Result
        if version == "b2-6":
            record = client.get(f"/v1/b2-5/projects/{project}/governance").json()["records"][0][
                "version"
            ]
            body.update(
                action="procedure_propose",
                sources=[
                    {
                        "source_type": "memory_version",
                        "source_id": record["ref"]["record_id"],
                        "revision": record["ref"]["version"],
                        "content_digest": record["ref"]["content_digest"],
                        "scope": record["scope"],
                        "permission_epoch": 0,
                        "availability": "available",
                    }
                ],
            )
            model = B26Result
        response = client.post(
            f"/v1/{version}/commands", json=body, headers={"Idempotency-Key": "large-command"}
        )
        assert response.status_code == 200, response.text
        parsed = model.model_validate(response.json())
        assert len(parsed.affected_ids) == 1
        assert "_truncated_items" not in response.text
        assert "not-a-real-credential" not in response.text
        replay = client.post(
            f"/v1/{version}/commands", json=body, headers={"Idempotency-Key": "large-command"}
        )
        assert replay.status_code == 200, replay.text
        assert model.model_validate(replay.json()).affected_ids == parsed.affected_ids
        assert replay.headers["Idempotent-Replayed"] == "true"
        assert "_truncated_items" not in replay.text
        assert "not-a-real-credential" not in replay.text
        table = {"b2-5": "b25_commands", "b2-6": "b26_commands"}[version]
        with client.app.state.operant_service.store._connect() as connection:
            connection.execute(
                f"UPDATE {table} SET state='outcome_unknown' WHERE command_id=?",
                ("large-command",),
            )
        unknown = client.post(
            f"/v1/{version}/commands",
            json=body,
            headers={"Idempotency-Key": "large-command"},
        )
        assert unknown.status_code == 409
        assert unknown.json()["error"]["code"] == "command_outcome_unknown"
        assert unknown.json()["error"]["recovery"] == "manual_reconcile"
        events = client.get(f"/v1/{version}/projects/{project}/events").json()["events"]
        assert len(events) == 1
