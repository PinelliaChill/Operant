from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from operant.api import create_app
from operant.application.configuration import ConfigService, workspace_scope_id
from operant.domain.models import Budget, ModelProfile, RolePreset, ToolPolicy
from operant.domain.threads import ConversationThread


def test_config_sources_freeze_new_session_and_preserve_policy(tmp_path: Path) -> None:
    app = create_app(tmp_path / "config.sqlite3")
    service = app.state.operant_service
    service.add_model_profile(
        ModelProfile(
            id="model-config",
            name="Configured",
            model_id="model-config",
            base_url="https://provider.example/v1",
            secret_ref="TEST_CONFIG_KEY",
            supports_temperature=True,
        )
    )
    role = service.create_role(
        RolePreset(
            id="role-config",
            name="Config role",
            system_prompt="Role base",
            model_profile_id="model-config",
            tool_policy=ToolPolicy(
                allowed_tools=("read_file", "apply_patch"), workspace_write=True
            ),
            budget=Budget(max_turns=12, max_output_tokens=1000),
        )
    )
    thread = service.create_thread(ConversationThread(workspace_ref=str(tmp_path)))
    project, _ = service.initialize_workspace(tmp_path)
    config = ConfigService(service.store)
    config.put_scope(
        "global",
        "default",
        patch={"system_prompt": "Global rule", "budget": {"max_turns": 10}},
        expected_revision=0,
    )
    config.put_scope(
        "project",
        project.id,
        patch={"system_prompt": "Project rule", "temperature": 0.25},
        expected_revision=0,
    )
    config.put_scope(
        "workspace",
        workspace_scope_id(str(tmp_path)),
        patch={"budget": {"max_output_tokens": 500}},
        expected_revision=0,
    )
    config.put_scope(
        "role",
        role.id,
        patch={
            "tool_policy": ToolPolicy(
                allowed_tools=("read_file",), workspace_write=True
            ).model_dump(mode="json")
        },
        expected_revision=0,
    )
    with TestClient(app) as client:
        effective = client.get(
            "/v1/config/effective",
            params={
                "role_id": role.id,
                "project_id": project.id,
                "workspace_ref": str(tmp_path),
            },
        )
        assert effective.status_code == 200, effective.text
        body = effective.json()
        assert body["sources"]["temperature"] == {
            "scope_type": "project",
            "scope_id": project.id,
        }
        assert body["revisions"] == {"global": 1, "project": 1, "workspace": 1, "role": 1}
        assert body["values"]["budget"]["max_turns"] == 10
        assert body["values"]["budget"]["max_output_tokens"] == 500
        assert body["values"]["tool_policy"]["allowed_tools"] == ["read_file"]

        created = client.post(
            "/v1/sessions",
            json={
                "role_id": role.id,
                "thread_id": thread.id,
                "project_id": project.id,
            },
        )
        assert created.status_code == 201, created.text
        frozen = created.json()["role_snapshot"]
        assert frozen["temperature"] == 0.25
        assert frozen["budget"]["max_output_tokens"] == 500
        assert "Global rule" in frozen["system_prompt"]
        assert "Project rule" in frozen["system_prompt"]
        assert frozen["tool_policy"]["allowed_tools"] == ["read_file"]

        # Live Chat only supplies the Thread; Core must resolve its registered Project.
        another_thread = service.create_thread(ConversationThread(workspace_ref=str(tmp_path)))
        inferred = client.post(
            "/v1/sessions",
            json={"role_id": role.id, "thread_id": another_thread.id},
        )
        assert inferred.status_code == 201, inferred.text
        inferred_snapshot = inferred.json()["role_snapshot"]
        assert "Project rule" in inferred_snapshot["system_prompt"]
        assert inferred_snapshot["config_project_id"] == project.id
        workflow_style = service.create_session(role.id, workspace_ref=str(tmp_path))
        assert "Project rule" in workflow_style.role_snapshot.system_prompt
        assert workflow_style.role_snapshot.config_project_id == project.id

        widened = client.post(
            "/v1/sessions",
            json={"role_id": role.id, "budget_overrides": {"max_turns": 12}},
        )
        assert widened.status_code == 400, widened.text
        with pytest.raises(ValueError, match="budget cannot increase"):
            service.store.create_session(role.id, budget_overrides={"max_output_tokens": 2000})

        config.put_scope("project", project.id, patch={"temperature": 0.5}, expected_revision=1)
        saved = client.get(f"/v1/sessions/{created.json()['id']}").json()["role_snapshot"]
        assert saved["temperature"] == 0.25
        newer = service.create_session(role.id, project_id=project.id, workspace_ref=str(tmp_path))
        assert newer.role_snapshot.temperature == 0.5


def test_config_reset_cas_and_temperature_capability(tmp_path: Path) -> None:
    app = create_app(tmp_path / "config-unsupported.sqlite3")
    service = app.state.operant_service
    service.add_model_profile(
        ModelProfile(
            id="model-no-temperature",
            name="No temperature",
            model_id="model-no-temperature",
            base_url="https://provider.example/v1",
            secret_ref="TEST_CONFIG_KEY",
        )
    )
    service.create_role(
        RolePreset(
            id="role-no-temperature",
            name="No temperature",
            system_prompt="Base",
            model_profile_id="model-no-temperature",
        )
    )
    with TestClient(app) as client:
        initial = client.get("/v1/config/scopes/global/default")
        assert initial.json()["revision"] == 0
        changed = client.put(
            "/v1/config/scopes/global/default",
            json={"expected_revision": 0, "patch": {"temperature": 0.3}},
        )
        assert changed.status_code == 200
        invalid = client.get("/v1/config/effective", params={"role_id": "role-no-temperature"})
        assert invalid.status_code == 400
        stale = client.delete("/v1/config/scopes/global/default", params={"expected_revision": 0})
        assert stale.status_code == 409
        cleared = client.delete("/v1/config/scopes/global/default", params={"expected_revision": 1})
        assert cleared.status_code == 200
        effective = client.get(
            "/v1/config/effective", params={"role_id": "role-no-temperature"}
        ).json()
        assert "temperature" not in effective["values"]
