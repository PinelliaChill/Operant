"""B2-5 public API integration with isolated data and the real Action Gateway."""

from __future__ import annotations

from fastapi.testclient import TestClient

from operant.api import create_app
from operant.persistence.sqlite import SQLiteStore


def setup_project(client, tmp_path, name="治理项目"):
    tmp_path.mkdir(parents=True, exist_ok=True)

    def execute(**body):
        response = client.post("/v1/b2-3/commands", json=body)
        assert response.status_code == 200, response.text
        return response.json()

    project = execute(action="project_create", name=name, workspace_path=str(tmp_path))["state"][
        "projects"
    ][-1]["project_id"]
    install = execute(
        action="plugin_install", plugin_id="memory-standard", mode="trusted_in_process"
    )["state"]["installations"][-1]["installation_id"]
    execute(action="binding_select", project_id=project, installation_id=install)
    return project


def exact(entry):
    p = entry["proposal"]
    return {
        "proposal_id": p["proposal_id"],
        "proposal_revision": p["proposal_revision"],
        "proposed_version": p["proposed_version"],
        "base_head_revision": p["base_head"]["revision"],
    }


def test_public_proposal_exact_review_and_replay(tmp_path):
    with TestClient(create_app(tmp_path / "api.sqlite3")) as client:
        project = setup_project(client, tmp_path)
        assert client.get("/v1/protocol/b2-5").json()["protocol_version"] == "b2-5.v1"
        body = {"action": "propose", "project_id": project, "content": "本项目统一使用中文注释"}
        response = client.post(
            "/v1/b2-5/commands", json=body, headers={"Idempotency-Key": "proposal-1"}
        )
        assert response.status_code == 200, response.text
        proposal = response.json()["state"]["proposals"][0]
        assert proposal["proposal"]["state"] == "pending"
        again = client.post(
            "/v1/b2-5/commands", json=body, headers={"Idempotency-Key": "proposal-1"}
        )
        assert again.status_code == 200, again.text
        assert again.json()["affected_ids"] == response.json()["affected_ids"]
        event_page = client.get(f"/v1/b2-5/projects/{project}/events")
        assert event_page.status_code == 200, event_page.text
        assert len(event_page.json()["events"]) == 1
        assert event_page.json()["events"][0]["action"] == "propose"
        assert "content" not in event_page.json()["events"][0]
        selected = exact(proposal)
        stale = {**selected, "proposal_revision": selected["proposal_revision"] + 1}
        failed = client.post(
            "/v1/b2-5/commands",
            json={
                "action": "review",
                "project_id": project,
                "decision": "accept",
                "selections": [stale],
            },
        )
        assert failed.status_code == 409, failed.text
        accepted = client.post(
            "/v1/b2-5/commands",
            json={
                "action": "review",
                "project_id": project,
                "decision": "accept",
                "selections": [selected],
            },
        )
        assert accepted.status_code == 200, accepted.text
        record = accepted.json()["state"]["records"][0]
        assert record["head"]["state"] == "published"
        assert record["currently_usable"] is True
        history = client.get(f"/v1/b2-5/projects/{project}/history", params={"query": "中文"})
        assert history.status_code == 200, history.text
        assert history.json()["perspective"] == "historical_fact"
        item_id = history.json()["items"][0]["item_id"]
        detail = client.get(f"/v1/b2-5/projects/{project}/history/{item_id}")
        assert detail.status_code == 200, detail.text
        assert "中文" in detail.json()["text"]


def test_v17_migration_preserves_frozen_history(tmp_path):
    store = SQLiteStore(tmp_path / "migration.sqlite3")
    store.migrate(16)
    previous = store.list_applied_migrations()
    store.migrate(17)
    assert store.schema_version() == 17
    assert store.list_applied_migrations()[:16] == previous
    with store._connect() as connection:
        assert connection.execute("SELECT count(*) FROM b25_commands").fetchone()[0] == 0


def test_governance_cannot_be_confirmed_through_old_command_and_source_revocation(tmp_path):
    with TestClient(create_app(tmp_path / "api.sqlite3")) as client:
        project = setup_project(client, tmp_path)
        made = client.post(
            "/v1/b2-5/commands",
            json={"action": "propose", "project_id": project, "content": "撤销后不能作为约定召回"},
        )
        assert made.status_code == 200, made.text
        entry = made.json()["state"]["proposals"][0]
        bypass = client.post(
            "/v1/b2-3/commands",
            json={
                "action": "memory_confirm",
                "project_id": project,
                "proposal_id": entry["proposal"]["proposal_id"],
                "expected_revision": 0,
            },
        )
        assert bypass.status_code == 409, bypass.text
        assert bypass.json()["detail"]["code"] == "exact_review_required"
        accepted = client.post(
            "/v1/b2-5/commands",
            json={
                "action": "review",
                "project_id": project,
                "decision": "accept",
                "selections": [exact(entry)],
            },
        )
        assert accepted.status_code == 200, accepted.text
        source = entry["version"]["sources"][0]
        revoked = client.post(
            "/v1/b2-5/commands",
            json={"action": "source_revoke", "project_id": project, "source": source},
        )
        assert revoked.status_code == 200, revoked.text
        assert not any(r["currently_usable"] for r in revoked.json()["state"]["records"])
        detail = client.get(f"/v1/b2-5/projects/{project}/history/{source['source_id']}")
        assert detail.status_code == 403, detail.text
        result = client.post(
            "/v1/b2-3/commands",
            json={"action": "memory_search", "project_id": project, "query": "撤销"},
        )
        assert result.status_code == 200, result.text
        assert result.json()["records"] == []


def test_history_rejects_cross_project_expansion(tmp_path):
    with TestClient(create_app(tmp_path / "api.sqlite3")) as client:
        first = setup_project(client, tmp_path / "first", "甲项目")
        second = setup_project(client, tmp_path / "second", "乙项目")
        made = client.post(
            "/v1/b2-5/commands",
            json={"action": "propose", "project_id": first, "content": "甲项目私有测试内容"},
        )
        assert made.status_code == 200, made.text
        source = made.json()["state"]["proposals"][0]["version"]["sources"][0]
        result = client.get(f"/v1/b2-5/projects/{second}/history/{source['source_id']}")
        assert result.status_code == 403, result.text
        result = client.get(f"/v1/b2-5/projects/{second}/history", params={"query": "私有测试"})
        assert result.status_code == 200, result.text
        assert result.json()["items"] == []


def test_committed_mutation_with_lost_receipt_recovers_as_unknown_without_replay(
    tmp_path, monkeypatch
):
    import operant.api_b2_5 as api

    database = tmp_path / "crash.sqlite3"
    app = create_app(database)
    original = api._record_completion
    with TestClient(app, raise_server_exceptions=False) as client:
        project = setup_project(client, tmp_path)

        def crash(*args, **kwargs):
            raise RuntimeError("injected crash after business commit")

        monkeypatch.setattr(api, "_record_completion", crash)
        request = {
            "action": "propose",
            "project_id": project,
            "content": "已提交但回执丢失的合成事实",
        }
        headers = {"Idempotency-Key": "unknown-command"}
        response = client.post("/v1/b2-5/commands", json=request, headers=headers)
        assert response.status_code == 500
        state = client.get(f"/v1/b2-5/projects/{project}/governance").json()
        assert len(state["proposals"]) == 1
        assert state["unresolved_command_ids"] == ["unknown-command"]
        monkeypatch.setattr(api, "_record_completion", original)
    with TestClient(create_app(database)) as client:
        state = client.get(f"/v1/b2-5/projects/{project}/governance").json()
        assert len(state["proposals"]) == 1
        assert state["unresolved_command_ids"] == ["unknown-command"]
        events = client.get(f"/v1/b2-5/projects/{project}/events").json()["events"]
        assert len(events) == 1
        assert events[0]["action"] == "outcome_unknown"
        assert events[0]["affected_ids"] == ["unknown-command"]
        response = client.post("/v1/b2-5/commands", json=request, headers=headers)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "command_outcome_unknown"
    with TestClient(create_app(database)) as client:
        # A second restart does not duplicate the uncertainty notification.
        events = client.get(f"/v1/b2-5/projects/{project}/events").json()["events"]
        assert len(events) == 1
