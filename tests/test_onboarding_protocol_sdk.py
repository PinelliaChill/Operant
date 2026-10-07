from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from operant.api import create_app
from sdk.protocol import generate_onboarding
from sdk.python_client import ONBOARDING_SCHEMA_DIGEST, OnboardingClient
from sdk.python_client.onboarding_generated import OnboardingError
from sdk.python_client.transport import TransportRequest, TransportResponse


def test_onboarding_schema_and_clients_are_deterministic_and_legacy_frozen(tmp_path: Path) -> None:
    paths = (
        generate_onboarding.SCHEMA_PATH,
        generate_onboarding.DIGEST_PATH,
        generate_onboarding.TS_PATH,
        generate_onboarding.PY_PATH,
    )
    legacy = tuple(
        path
        for directory, pattern in (
            (Path("sdk/protocol/schema"), "*"),
            (Path("sdk/typescript-client"), "*.generated.ts"),
            (Path("sdk/python_client"), "*_generated.py"),
        )
        for path in directory.glob(pattern)
        if path.is_file() and "onboarding" not in path.name
    )
    before = {path: path.read_bytes() for path in (*paths, *legacy)}
    assert (
        generate_onboarding.generate() == generate_onboarding.generate() == ONBOARDING_SCHEMA_DIGEST
    )
    assert before == {path: path.read_bytes() for path in before}
    schema = json.loads(generate_onboarding.SCHEMA_PATH.read_bytes())
    assert {
        op["operationId"] for item in schema["paths"].values() for op in item.values()
    } == generate_onboarding.EXPECTED_OPERATION_IDS
    for item in schema["paths"].values():
        for method, op in item.items():
            if method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
                assert any(
                    parameter["in"] == "header" and parameter["name"].lower() == "idempotency-key"
                    for parameter in op["parameters"]
                )
    with TestClient(create_app(tmp_path / "protocol.db", phase45_skill_roots={})) as client:
        response = client.get("/v1/protocol/onboarding")
    assert response.status_code == 200
    assert response.json()["schema_digest"] == ONBOARDING_SCHEMA_DIGEST


def test_onboarding_client_uses_formal_routes_and_safe_validation(tmp_path: Path) -> None:
    app = create_app(tmp_path / "client.db", phase45_skill_roots={})
    requests: list[TransportRequest] = []
    with TestClient(app, base_url="http://127.0.0.1") as http:

        def transport(request: TransportRequest) -> TransportResponse:
            requests.append(request)
            result = http.request(
                request.method,
                request.url,
                headers=dict(request.headers),
                content=request.body,
            )
            return TransportResponse(result.status_code, dict(result.headers), body=result.content)

        client = OnboardingClient("http://127.0.0.1", transport=transport)
        assert client.get_setup_state()["ready"] is False
        assert client.list_conversation_metadata() == {"items": []}
        assert client.list_model_connections() == {"items": []}
        marker = "never-return-this-credential"
        with pytest.raises(OnboardingError) as error:
            client.create_model_connection(
                {"provider": "invalid", "api_key": marker},  # type: ignore[typeddict-item]
                idempotency_key="validation-key",
            )
        assert error.value.code == "request_validation_failed"
        assert marker not in str(error.value.detail)
        assert requests[0].url.endswith("/v1/protocol/onboarding")
        assert requests[-1].headers["Idempotency-Key"] == "validation-key"
        digest = hashlib.sha256(generate_onboarding.SCHEMA_PATH.read_bytes().rstrip()).hexdigest()
        assert digest == ONBOARDING_SCHEMA_DIGEST


def test_setup_respects_configured_local_authorizer(tmp_path: Path) -> None:
    app = create_app(
        tmp_path / "restricted.db",
        phase45_skill_roots={},
        phase56_local_authorizer=lambda _request: False,
    )
    with TestClient(app) as client:
        for path in (
            "/v1/setup/state",
            "/v1/setup/connections",
            "/v1/setup/skill-sources",
            "/v1/setup/local-apps",
            "/v1/setup/team-templates",
        ):
            assert client.get(path).status_code == 403
