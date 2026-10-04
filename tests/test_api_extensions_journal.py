from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_local_extensions import _source

from operant import api_extensions
from operant.domain.security import PolicyDecision
from operant.domain.threads import Turn
from operant.persistence.sqlite import SQLiteStore
from operant.plugins.external_tool import ExternalToolRegistry
from operant.plugins.protocol import SandboxEvidence, SandboxProbe


def test_extension_command_journal_replays_success_and_blocks_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = ExternalToolRegistry(tmp_path / "external-tools")
    record = registry.install(_source(tmp_path / "source"))

    def admitted(self: SandboxProbe, **_: object) -> SandboxEvidence:
        return SandboxEvidence(
            evidence_ref="sandbox-test", profile="(version 1)", runner="/bin/false"
        )

    monkeypatch.setattr(SandboxProbe, "check", admitted)
    registry.set_enabled("sample_ext", True, granted_categories=("command",))
    store = SQLiteStore(tmp_path / "core.sqlite3")
    store.initialize()
    events: list[Any] = []
    monkeypatch.setattr(
        store, "append_event", lambda event, **kwargs: events.append((event, kwargs))
    )
    service = SimpleNamespace(
        extension_registry=registry,
        store=store,
        create_turn=lambda turn: Turn(thread_id=turn.thread_id),
    )
    monkeypatch.setattr(
        api_extensions, "thread_session", lambda _service, _thread_id: SimpleNamespace(id="session")
    )

    class Gateway:
        decision = PolicyDecision.ASK
        calls = 0

        def guard(self, **_kwargs: Any) -> tuple[Any, Any, Any]:
            self.calls += 1
            return (
                SimpleNamespace(action_hash="a" * 64, policy_version="test"),
                SimpleNamespace(
                    decision=self.decision,
                    lease=object() if self.decision is PolicyDecision.ALLOW else None,
                    reason_code="test",
                    approval_id="approval-1",
                ),
                None,
            )

        def consume(self, _lease: Any, _action: Any) -> None:
            return None

    gateway = Gateway()
    app = FastAPI()
    api_extensions.install_extension_routes(
        app, service, action_gateway=gateway, local_authorizer=lambda _request: True
    )
    calls = 0

    def execute(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"ok": True}

    monkeypatch.setattr(api_extensions, "run_operation", execute)
    client = TestClient(app)
    url = "/v1/workbench/threads/thread/extension-commands"
    body = {
        "command": record.granted_name("ext_sample_ext_status"),
        "arguments": {"label": "test"},
        "idempotency_key": "same-key",
    }
    pending = client.post(url, json=body)
    assert pending.status_code == 409
    assert pending.json()["detail"]["code"] == "approval_required"
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM command_executions").fetchone()[0] == 0
    gateway.decision = PolicyDecision.ALLOW
    first = client.post(url, json=body)
    assert first.status_code == 200
    assert first.json()["result"] == {"ok": True}
    assert len(events) == 1
    assert events[0][0].event_type == "extension.command.completed"
    assert events[0][1]["history_item"].payload.summary.startswith("/")
    repeated = client.post(url, json=body)
    assert repeated.status_code == 200
    assert repeated.json() == first.json()
    assert calls == 1
    assert gateway.calls == 2

    def fail(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        raise RuntimeError("unknown outcome")

    monkeypatch.setattr(api_extensions, "run_operation", fail)
    unknown_body = {**body, "idempotency_key": "unknown-key"}
    unknown = client.post(url, json=unknown_body)
    assert unknown.status_code == 409
    assert unknown.json()["detail"]["code"] == "command_outcome_unknown"
    again = client.post(url, json=unknown_body)
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "command_outcome_unknown"
    assert calls == 2
