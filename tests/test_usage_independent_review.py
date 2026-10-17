"""Independent simulation checks for the U2/U4 usage fixes."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import operant.cli as cli
from operant.local_caller import LocalCallerAuthority
from operant.local_caller_middleware import LocalCallerProofMiddleware
from operant.persistence.sqlite import NotFoundError
from sdk.python_client.transport import Phase1EError, TransportResponse


def test_denial_is_parseable_and_never_reaches_write_while_public_read_survives() -> None:
    effects: list[str] = []
    app = FastAPI()

    @app.post("/v1/private/write")
    def write() -> dict[str, bool]:
        effects.append("written")
        return {"ok": True}

    @app.get("/v1/projects")
    def projects() -> dict[str, list[str]]:
        return {"items": ["public-project"]}

    app.add_middleware(
        LocalCallerProofMiddleware,
        authority=LocalCallerAuthority(b"r" * 32),
        protected_prefixes=("/v1/private",),
        public_exact_routes=frozenset(),
    )
    with TestClient(app) as client:
        for headers in ({}, {"X-Operant-Caller-Signature": "not-a-proof"}):
            response = client.post("/v1/private/write", headers=headers)
            assert response.status_code == 403
            assert response.headers["cache-control"] == "no-store"
            detail = Phase1EError.from_response(
                TransportResponse(status=response.status_code, headers={}, text=response.text)
            )
            assert detail.code == "local_caller_required"
            assert detail.retryable is False
            assert detail.recovery == "none"
        assert client.get("/v1/projects").json() == {"items": ["public-project"]}
    assert effects == []


@pytest.mark.parametrize("failure", [RuntimeError("internal bug"), ValueError("bad invariant")])
def test_unexpected_cli_failures_are_not_rewritten_as_user_errors(monkeypatch, failure) -> None:
    def service():
        raise failure

    monkeypatch.setattr(cli, "_service", service)
    result = CliRunner().invoke(cli.app, ["session", "show", "missing"])
    assert result.exit_code != 0
    assert result.exception is failure
    assert "未找到指定记录" not in result.output


@pytest.mark.parametrize("command", ["session", "model", "role", "workflow"])
def test_expected_cli_lookup_failure_is_short_nonzero_and_actionable(monkeypatch, command) -> None:
    def service():
        raise NotFoundError("private raw lookup details")

    monkeypatch.setattr(cli, "_service", service)
    result = CliRunner().invoke(cli.app, [command, "show", "missing"])
    assert result.exit_code != 0
    assert "未找到指定记录" in result.output
    hint = "operant session --help" if command == "session" else f"operant {command} list"
    assert hint in result.output
    assert "Traceback" not in result.output
    assert "private raw" not in result.output
