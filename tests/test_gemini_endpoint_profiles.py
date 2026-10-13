from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from operant.api_model_connections import install_model_connection_routes
from operant.application.service import ApplicationService
from operant.domain.models import ModelProfile, RolePreset
from operant.persistence.onboarding import UXRepository
from operant.persistence.sqlite import SQLiteStore
from operant.providers.openai_compatible import OpenAICompatibleProvider


class AllowFixtureGateway:
    def guard(self, **kwargs: Any) -> tuple[Any, Any, None]:
        del kwargs
        return (
            SimpleNamespace(action_hash="fixture-action"),
            SimpleNamespace(decision=SimpleNamespace(value="allow"), lease=object()),
            None,
        )

    def consume(self, lease: Any, action: Any) -> None:
        del lease, action


@pytest.mark.parametrize(
    "requested_endpoint",
    [None, "https://generativelanguage.googleapis.com/v1"],
)
def test_new_gemini_profiles_support_json_schema_without_rewriting_old_configuration(
    tmp_path: Path, requested_endpoint: str | None
) -> None:
    service = ApplicationService(SQLiteStore(tmp_path / "core.sqlite3"), OpenAICompatibleProvider())
    service.initialize()
    repo = UXRepository(service.store)
    app = FastAPI()
    install_model_connection_routes(
        app, service, repo, action_gateway=cast(Any, AllowFixtureGateway())
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        body = {"provider": "gemini", "api_key": "synthetic-key"}
        if requested_endpoint:
            body["base_url"] = requested_endpoint
        response = client.post(
            "/v1/setup/connections", json=body, headers={"Idempotency-Key": "new-connection"}
        )
        assert response.status_code == 201
        connection = response.json()
        assert connection["base_url"] == (
            requested_endpoint or "https://generativelanguage.googleapis.com/v1beta"
        )
        connection_id = connection["connection_id"]
        saved = repo.get_connection(connection_id)
        assert saved is not None
        old_profile = service.add_model_profile(
            ModelProfile(
                id="old-v1-profile",
                name="Old Gemini profile",
                provider="gemini",
                model_id="catalog-model",
                base_url="https://generativelanguage.googleapis.com/v1",
                secret_ref=saved["secret_ref"],
            )
        )
        service.create_role(
            RolePreset(
                name="Old role",
                id="old-role",
                system_prompt="Read only.",
                model_profile_id=old_profile.id,
            )
        )
        old_session = service.create_session("old-role")
        old_snapshot = old_session.role_snapshot.model_dump(mode="json")
        # Catalog fixture: this test checks profile persistence, not real discovery.
        repo.save_connection(
            connection_id, {**saved, "status": "connected", "model_ids": ["catalog-model"]}
        )
        selected = client.post(
            f"/v1/setup/connections/{connection_id}/profiles",
            json={"model_id": "catalog-model"},
            headers={"Idempotency-Key": "select-new-profile"},
        )
        assert selected.status_code == 200
        profile = service.store.get_model_profile(selected.json()["model_profile_id"])
        assert profile.base_url == "https://generativelanguage.googleapis.com/v1beta"
        original_profile = profile.model_dump(mode="json")
        replayed = client.post(
            f"/v1/setup/connections/{connection_id}/profiles",
            json={"model_id": "catalog-model"},
            headers={"Idempotency-Key": "select-new-profile"},
        )
        assert replayed.json() == selected.json()
        assert (
            service.store.get_model_profile(profile.id).model_dump(mode="json") == original_profile
        )
        assert repo.get_connection(connection_id)["base_url"] == connection["base_url"]
        assert service.get_model_profile(old_profile.id) == old_profile
        assert (
            service.store.get_session(old_session.id).role_snapshot.model_dump(mode="json")
            == old_snapshot
        )
