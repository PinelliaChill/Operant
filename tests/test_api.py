from pathlib import Path

from fastapi.testclient import TestClient

from operant.api import create_app


def test_health_and_empty_registries(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "api.sqlite3")) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/v1/models").json() == []
        assert client.get("/v1/roles").json() == []


def test_registry_crud_default_roles_and_snapshot_override(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "api.sqlite3")) as client:
        model_payload = {
            "name": "relay",
            "model_id": "planner-id",
            "base_url": "https://relay.example.com/v1",
            "secret_ref": "OPERANT_TEST_KEY",
        }
        model = client.post("/v1/models", json=model_payload)
        assert model.status_code == 201
        model_id = model.json()["id"]

        patched = client.patch(
            f"/v1/models/{model_id}",
            json={"name": "relay-updated"},
        )
        assert patched.status_code == 200
        assert patched.json()["name"] == "relay-updated"

        seeded = client.post(
            "/v1/roles/seed-defaults",
            json={
                "planner_model_profile_id": model_id,
                "coder_model_profile_id": model_id,
                "reviewer_model_profile_id": model_id,
            },
        )
        assert seeded.status_code == 200
        assert len(seeded.json()) == 5

        session = client.post(
            "/v1/sessions",
            json={
                "role_id": "role_planner",
                "effort": "high",
                "budget_overrides": {"timeout_seconds": 17},
            },
        )
        assert session.status_code == 201
        snapshot = session.json()["role_snapshot"]
        assert snapshot["role_id"] == "role_planner"
        assert snapshot["overrides"]["effort_overridden"]
        assert snapshot["budget"]["timeout_seconds"] == 17

        updated_role = client.patch(
            "/v1/roles/role_planner",
            json={"system_prompt": "A new prompt."},
        )
        assert updated_role.status_code == 200
        assert updated_role.json()["version"] == 2

        old_session = client.get(f"/v1/sessions/{session.json()['id']}").json()
        assert old_session["role_snapshot"]["role_version"] == 1
        assert old_session["role_snapshot"]["system_prompt"] != "A new prompt."
