from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import operant.api as api_module
from operant.api import (
    MAX_ARTIFACT_REQUEST_BODY_BYTES,
    CreateArtifactRequest,
    create_app,
)
from operant.domain.workflow import WorkflowRun
from operant.persistence.sqlite import SQLiteStore


def _command_count(database: Path) -> int:
    with sqlite3.connect(database) as connection:
        row = connection.execute("SELECT COUNT(*) FROM command_executions").fetchone()
    assert row is not None
    return int(row[0])


def test_thread_history_receipts_cursor_replay_and_archive_boundary(tmp_path: Path) -> None:
    database = tmp_path / "thread-api.sqlite3"
    with TestClient(create_app(database)) as client:
        key = "create-parent-thread"
        parent_response = client.post(
            "/v1/threads",
            headers={"Idempotency-Key": key},
            json={"workspace_ref": "workspace:alpha"},
        )
        assert parent_response.status_code == 201
        parent = parent_response.json()
        assert parent["status"] == "active"
        assert parent["cursor"] >= 1

        replay = client.post(
            "/v1/threads",
            headers={"Idempotency-Key": key},
            json={"workspace_ref": "workspace:alpha"},
        )
        assert replay.status_code == 201
        assert replay.json() == parent
        assert replay.headers["Idempotency-Replayed"] == "true"

        conflict = client.post(
            "/v1/threads",
            headers={"Idempotency-Key": key},
            json={"workspace_ref": "workspace:different"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "idempotency_key_conflict"

        cannot_select_terminal_status = client.post(
            "/v1/threads",
            json={"workspace_ref": "workspace:bad", "status": "archived"},
        )
        assert cannot_select_terminal_status.status_code == 422

        child_response = client.post(
            "/v1/threads",
            json={
                "parent_thread_id": parent["id"],
                "workspace_ref": "workspace:alpha",
            },
        )
        assert child_response.status_code == 201
        child = child_response.json()
        assert child["parent_thread_id"] == parent["id"]

        children = client.get(
            "/v1/threads",
            params={"parent_thread_id": parent["id"], "limit": 1},
        )
        assert children.status_code == 200
        assert [thread["id"] for thread in children.json()] == [child["id"]]

        turn_response = client.post(f"/v1/threads/{child['id']}/turns", json={})
        assert turn_response.status_code == 201
        turn = turn_response.json()
        assert turn["position"] == 1

        secret = "super-secret-phase1a-value"
        first_item_response = client.post(
            f"/v1/threads/{child['id']}/turns/{turn['id']}/items",
            headers={"Idempotency-Key": "append-first-thread-item"},
            json={
                "payload": {
                    "type": "user_message",
                    "text": f"DATABASE_PASSWORD={secret}",
                }
            },
        )
        assert first_item_response.status_code == 201
        first_item = first_item_response.json()
        assert first_item["position"] == 1
        assert secret not in first_item_response.text
        assert "[REDACTED]" in first_item["payload"]["text"]

        replayed_item = client.post(
            f"/v1/threads/{child['id']}/turns/{turn['id']}/items",
            headers={"Idempotency-Key": "append-first-thread-item"},
            json={
                "payload": {
                    "type": "user_message",
                    "text": f"DATABASE_PASSWORD={secret}",
                }
            },
        )
        assert replayed_item.status_code == 201
        assert replayed_item.json() == first_item
        assert replayed_item.headers["Idempotency-Replayed"] == "true"

        second_item_response = client.post(
            f"/v1/threads/{child['id']}/turns/{turn['id']}/items",
            json={
                "payload": {
                    "type": "system_event",
                    "event_type": "test.completed",
                    "summary": "second canonical item",
                }
            },
        )
        assert second_item_response.status_code == 201
        second_item = second_item_response.json()
        assert second_item["position"] == 2
        assert second_item["cursor"] > first_item["cursor"]

        first_page = client.get(
            f"/v1/threads/{child['id']}/items",
            params={"limit": 1},
        ).json()
        second_page = client.get(
            f"/v1/threads/{child['id']}/items",
            params={"after_cursor": first_page[0]["cursor"], "limit": 1},
        ).json()
        assert [item["id"] for item in first_page + second_page] == [
            first_item["id"],
            second_item["id"],
        ]

        replay_stream = client.get(
            f"/v1/threads/{child['id']}/items/stream",
            headers={"Last-Event-ID": str(first_item["cursor"])},
        )
        assert replay_stream.status_code == 200
        assert f"id: {second_item['cursor']}\n" in replay_stream.text
        assert "event: thread.item.appended\n" in replay_stream.text
        assert first_item["id"] not in replay_stream.text
        assert second_item["id"] in replay_stream.text

        invalid_cursor = client.get(
            f"/v1/threads/{child['id']}/items/stream",
            headers={"Last-Event-ID": "not-a-cursor"},
        )
        assert invalid_cursor.status_code == 400
        assert invalid_cursor.json()["error"]["code"] == "invalid_event_cursor"

        archived = client.post(f"/v1/threads/{child['id']}/archive")
        assert archived.status_code == 200
        assert archived.json()["status"] == "archived"
        assert archived.json()["archived_at"] is not None
        repeated_archive = client.post(f"/v1/threads/{child['id']}/archive")
        assert repeated_archive.status_code == 200
        assert repeated_archive.json()["archived_at"] == archived.json()["archived_at"]

        rejected_turn = client.post(f"/v1/threads/{child['id']}/turns", json={})
        assert rejected_turn.status_code == 409
        rejected_item = client.post(
            f"/v1/threads/{child['id']}/turns/{turn['id']}/items",
            json={"payload": {"type": "user_message", "text": "too late"}},
        )
        assert rejected_item.status_code == 409

    assert _command_count(database) >= 8


def test_artifact_metadata_dedup_integrity_and_source_validation(tmp_path: Path) -> None:
    database = tmp_path / "artifact-api.sqlite3"
    artifact_root = tmp_path / "private-artifact-store"
    with TestClient(create_app(database, artifact_root=artifact_root)) as client:
        thread = client.post(
            "/v1/threads",
            json={"workspace_ref": "workspace:artifact-source"},
        ).json()
        payload = {
            "content_text": "artifact bytes only",
            "media_type": "text/plain",
            "sensitivity": "sensitive",
            "source_refs": [{"source_type": "thread", "source_id": thread["id"]}],
            "retention_policy_ref": "retention:project",
        }
        created = client.post(
            "/v1/artifacts",
            headers={"Idempotency-Key": "artifact-create"},
            json=payload,
        )
        assert created.status_code == 201
        artifact = created.json()
        assert artifact["size_bytes"] == len(payload["content_text"].encode("utf-8"))
        assert artifact["sensitivity"] == "sensitive"
        assert "content_text" not in created.text
        assert "content_base64" not in created.text
        assert "storage_key" not in created.text
        assert str(artifact_root) not in created.text

        receipt_replay = client.post(
            "/v1/artifacts",
            headers={"Idempotency-Key": "artifact-create"},
            json=payload,
        )
        assert receipt_replay.status_code == 201
        assert receipt_replay.json() == artifact
        assert receipt_replay.headers["Idempotency-Replayed"] == "true"

        deduplicated = client.post("/v1/artifacts", json=payload)
        assert deduplicated.status_code == 200
        assert deduplicated.json() == artifact

        sensitivity_downgrade = client.post(
            "/v1/artifacts",
            json={**payload, "sensitivity": "normal"},
        )
        assert sensitivity_downgrade.status_code == 409
        metadata_conflict = client.post(
            "/v1/artifacts",
            json={**payload, "media_type": "application/octet-stream"},
        )
        assert metadata_conflict.status_code == 409

        invalid_source = client.post(
            "/v1/artifacts",
            json={
                **payload,
                "content_text": "different bytes",
                "source_refs": [{"source_type": "thread", "source_id": "thread_missing"}],
            },
        )
        assert invalid_source.status_code == 404

        malformed_base64 = client.post(
            "/v1/artifacts",
            json={"content_base64": "not*base64", "media_type": "application/octet-stream"},
        )
        assert malformed_base64.status_code == 400
        assert "not*base64" not in malformed_base64.text

        oversized_envelope = client.post(
            "/v1/artifacts",
            headers={"Content-Length": str(MAX_ARTIFACT_REQUEST_BODY_BYTES + 1)},
            json={"content_text": "small", "media_type": "text/plain"},
        )
        assert oversized_envelope.status_code == 413
        assert oversized_envelope.json()["error"]["code"] == "artifact_request_too_large"

        listed = client.get("/v1/artifacts", params={"sensitivity": "sensitive"})
        assert listed.status_code == 200
        assert [entry["id"] for entry in listed.json()] == [artifact["id"]]
        fetched = client.get(f"/v1/artifacts/{artifact['id']}")
        assert fetched.status_code == 200
        assert fetched.json() == artifact
        assert str(artifact_root) not in fetched.text

        digest = artifact["content_hash"]
        blob_path = artifact_root / "sha256" / digest[:2] / digest[2:4] / digest
        blob_path.write_bytes(b"x" * artifact["size_bytes"])
        corrupted = client.get(f"/v1/artifacts/{artifact['id']}")
        assert corrupted.status_code == 409
        assert corrupted.json()["error"]["code"] == "artifact_integrity_failed"
        assert str(blob_path) not in corrupted.text


def test_artifact_text_limit_counts_utf8_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_module, "MAX_ARTIFACT_UPLOAD_BYTES", 4)
    request = CreateArtifactRequest(content_text="汉字", media_type="text/plain")
    with pytest.raises(ValueError, match="API size limit"):
        request.decoded_content()


@pytest.mark.parametrize("content_length", [None, b"1"])
def test_artifact_body_limit_stops_chunked_or_falsely_small_stream_early(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content_length: bytes | None,
) -> None:
    monkeypatch.setattr(api_module, "MAX_ARTIFACT_REQUEST_BODY_BYTES", 10)
    app = create_app(tmp_path / f"bounded-{content_length!r}.sqlite3")
    headers = [(b"content-type", b"application/json")]
    if content_length is None:
        headers.append((b"transfer-encoding", b"chunked"))
    else:
        headers.append((b"content-length", content_length))
    receive_calls = 0
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        nonlocal receive_calls
        receive_calls += 1
        return {"type": "http.request", "body": b"xxxx", "more_body": True}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/artifacts",
        "raw_path": b"/v1/artifacts",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "root_path": "",
    }
    asyncio.run(app(scope, receive, send))  # type: ignore[arg-type]

    response_start = next(message for message in sent if message["type"] == "http.response.start")
    response_body = b"".join(
        message.get("body", b"")  # type: ignore[arg-type]
        for message in sent
        if message["type"] == "http.response.body"
    )
    assert response_start["status"] == 413
    assert json.loads(response_body)["error"]["code"] == "artifact_request_too_large"
    assert receive_calls == 3
    assert receive_calls < 100


def test_invalid_artifact_source_creates_no_blob_temp_or_metadata(tmp_path: Path) -> None:
    database = tmp_path / "invalid-source.sqlite3"
    artifact_root = tmp_path / "must-not-be-created"
    with TestClient(create_app(database, artifact_root=artifact_root)) as client:
        response = client.post(
            "/v1/artifacts",
            json={
                "content_text": "must never be published",
                "media_type": "text/plain",
                "source_refs": [{"source_type": "thread", "source_id": "thread_missing"}],
            },
        )
        assert response.status_code == 404

    assert not artifact_root.exists()
    with sqlite3.connect(database) as connection:
        counts = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM artifact_blobs),
                (SELECT COUNT(*) FROM artifacts),
                (SELECT COUNT(*) FROM artifact_source_refs)
            """
        ).fetchone()
    assert counts == (0, 0, 0)


def test_thread_legacy_mapping_is_explicit_and_does_not_rewrite_workflow(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy-mapping.sqlite3"
    store = SQLiteStore(database)
    store.initialize()
    workflow = store.create_workflow_run(
        WorkflowRun(
            task="legacy task remains authoritative in its existing table",
            workspace=str(tmp_path / "legacy-workspace"),
            planner_role_id="role_planner",
            coder_role_id="role_coder",
            reviewer_role_id="role_reviewer",
        )
    )

    with TestClient(create_app(database)) as client:
        assert client.get("/v1/threads").json() == []
        mapped = client.post(
            "/v1/threads",
            json={
                "workspace_ref": "workspace:legacy-explicit",
                "legacy_refs": [{"source_type": "workflow_run", "source_id": workflow.id}],
            },
        )
        assert mapped.status_code == 201
        assert mapped.json()["legacy_refs"] == [
            {"source_type": "workflow_run", "source_id": workflow.id}
        ]

        duplicate_mapping = client.post(
            "/v1/threads",
            json={"legacy_refs": [{"source_type": "workflow_run", "source_id": workflow.id}]},
        )
        assert duplicate_mapping.status_code == 409
        missing_mapping = client.post(
            "/v1/threads",
            json={
                "legacy_refs": [{"source_type": "workflow_run", "source_id": "workflow_missing"}]
            },
        )
        assert missing_mapping.status_code == 404

        legacy_readback = client.get(f"/v1/tasks/{workflow.id}")
        assert legacy_readback.status_code == 200
        assert legacy_readback.json()["task"] == workflow.task
