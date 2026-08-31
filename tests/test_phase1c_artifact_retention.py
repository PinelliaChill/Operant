from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import operant.artifacts.export as export_module
from operant.api import create_app
from operant.artifacts import ArtifactStore
from operant.domain.messages import ModelUsage
from operant.domain.models import ModelProfile, RolePreset
from operant.domain.threads import (
    ArtifactAccessLevel,
    CacheHitStatus,
    CacheObservation,
    RetentionLifecycle,
)
from operant.persistence.sqlite import ConflictError, MigrationError, SQLiteStore
from operant.runtime.loop import RuntimeEvent

_CAPABILITY_SECRET = b"phase1c-test-capability-secret!!"
_PHYSICAL_AUTHORIZATION = "phase1c-physical-delete-test-auth"


def _artifact_headers(token: str, *, idempotency_key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Artifact {token}"}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _create_artifact(
    client: TestClient,
    *,
    content: str,
    sensitivity: str = "normal",
    retention_policy_ref: str = "default",
) -> dict[str, Any]:
    response = client.post(
        "/v1/artifacts",
        json={
            "content_text": content,
            "media_type": "text/plain",
            "sensitivity": sensitivity,
            "retention_policy_ref": retention_policy_ref,
        },
    )
    assert response.status_code == 201
    return response.json()


def _issue(
    service: Any,
    artifact_id: str,
    operation: str,
    *,
    access_level: ArtifactAccessLevel = ArtifactAccessLevel.NORMAL,
    workspace_root: Path | None = None,
    relative_path: str | None = None,
) -> str:
    return service.issue_artifact_capability(
        artifact_id,
        operation=operation,
        access_level=access_level,
        workspace_root=workspace_root,
        relative_path=relative_path,
    )


def test_artifact_capabilities_redacted_read_download_and_scoped_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "artifact-access.sqlite3"
    artifact_root = tmp_path / "private-artifacts"
    app = create_app(
        database,
        artifact_root=artifact_root,
        artifact_capability_secret=_CAPABILITY_SECRET,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        body = "DATABASE_PASSWORD=ordinary-confidential-value"
        artifact = _create_artifact(client, content=body, sensitivity="sensitive")
        service = app.state.operant_service

        with pytest.raises(PermissionError, match="clearance"):
            _issue(service, artifact["id"], "read")

        read_token = _issue(
            service,
            artifact["id"],
            "read",
            access_level=ArtifactAccessLevel.SENSITIVE,
        )
        read_response = client.get(
            f"/v1/artifacts/{artifact['id']}/content",
            headers=_artifact_headers(read_token),
        )
        assert read_response.status_code == 200
        assert "ordinary-confidential-value" not in read_response.text
        assert "[REDACTED]" in read_response.json()["content_text"]
        assert "storage_key" not in read_response.text
        assert str(artifact_root) not in read_response.text

        wrong_operation = client.get(
            f"/v1/artifacts/{artifact['id']}/download",
            headers=_artifact_headers(read_token),
        )
        assert wrong_operation.status_code == 403
        download_token = _issue(
            service,
            artifact["id"],
            "download",
            access_level=ArtifactAccessLevel.SENSITIVE,
        )
        downloaded = client.get(
            f"/v1/artifacts/{artifact['id']}/download",
            headers=_artifact_headers(download_token),
        )
        assert downloaded.status_code == 200
        assert downloaded.content == body.encode()
        assert downloaded.headers["content-disposition"] == 'attachment; filename="artifact.bin"'
        assert downloaded.headers["x-content-type-options"] == "nosniff"

        workspace = tmp_path / "workspace"
        export_directory = workspace / "exports"
        export_directory.mkdir(parents=True)
        export_token = _issue(
            service,
            artifact["id"],
            "export",
            access_level=ArtifactAccessLevel.SENSITIVE,
            workspace_root=workspace,
            relative_path="exports/result.txt",
        )
        export_body = {
            "workspace_root": str(workspace),
            "relative_path": "exports/result.txt",
        }
        exported = client.post(
            f"/v1/artifacts/{artifact['id']}/export",
            headers=_artifact_headers(export_token, idempotency_key="export-once"),
            json=export_body,
        )
        assert exported.status_code == 200
        assert exported.json()["exported"] is True
        assert (export_directory / "result.txt").read_bytes() == body.encode()
        assert str(workspace) not in exported.text

        replayed = client.post(
            f"/v1/artifacts/{artifact['id']}/export",
            headers=_artifact_headers(export_token, idempotency_key="export-once"),
            json=export_body,
        )
        assert replayed.status_code == 200
        assert replayed.headers["Idempotency-Replayed"] == "true"
        assert replayed.json() == exported.json()

        changed_request = client.post(
            f"/v1/artifacts/{artifact['id']}/export",
            headers=_artifact_headers(export_token, idempotency_key="export-once"),
            json={**export_body, "relative_path": "exports/other.txt"},
        )
        assert changed_request.status_code == 409
        assert changed_request.json()["error"]["code"] == "idempotency_key_conflict"

        replacement = tmp_path / "replacement-workspace"
        (replacement / "exports").mkdir(parents=True)
        replacement_token = _issue(
            service,
            artifact["id"],
            "export",
            access_level=ArtifactAccessLevel.SENSITIVE,
            workspace_root=replacement,
            relative_path="exports/result.txt",
        )
        detached = tmp_path / "detached-workspace"
        replacement.rename(detached)
        (replacement / "exports").mkdir(parents=True)
        replaced_scope = client.post(
            f"/v1/artifacts/{artifact['id']}/export",
            headers=_artifact_headers(replacement_token, idempotency_key="replaced-scope"),
            json={
                "workspace_root": str(replacement),
                "relative_path": "exports/result.txt",
            },
        )
        assert replaced_scope.status_code == 403
        assert not (replacement / "exports" / "result.txt").exists()

        prepublish = tmp_path / "prepublish-workspace"
        (prepublish / "exports").mkdir(parents=True)
        prepublish_token = _issue(
            service,
            artifact["id"],
            "export",
            access_level=ArtifactAccessLevel.SENSITIVE,
            workspace_root=prepublish,
            relative_path="exports/result.txt",
        )
        detached_prepublish = tmp_path / "detached-prepublish-workspace"
        original_authorized = service._authorized_artifact_content

        def authorize_then_replace(*args: Any, **kwargs: Any) -> Any:
            authorized = original_authorized(*args, **kwargs)
            prepublish.rename(detached_prepublish)
            (prepublish / "exports").mkdir(parents=True)
            return authorized

        with monkeypatch.context() as prepublish_patch:
            prepublish_patch.setattr(
                service,
                "_authorized_artifact_content",
                authorize_then_replace,
            )
            prepublish_replaced = client.post(
                f"/v1/artifacts/{artifact['id']}/export",
                headers=_artifact_headers(
                    prepublish_token,
                    idempotency_key="prepublish-replaced",
                ),
                json={
                    "workspace_root": str(prepublish),
                    "relative_path": "exports/result.txt",
                },
            )
        assert prepublish_replaced.status_code == 409
        assert not (prepublish / "exports" / "result.txt").exists()
        assert not (detached_prepublish / "exports" / "result.txt").exists()

        racing = tmp_path / "racing-workspace"
        (racing / "exports").mkdir(parents=True)
        racing_token = _issue(
            service,
            artifact["id"],
            "export",
            access_level=ArtifactAccessLevel.SENSITIVE,
            workspace_root=racing,
            relative_path="exports/result.txt",
        )
        detached_racing = tmp_path / "detached-racing-workspace"
        original_link = export_module.os.link

        def link_then_replace(*args: Any, **kwargs: Any) -> None:
            original_link(*args, **kwargs)
            racing.rename(detached_racing)
            (racing / "exports").mkdir(parents=True)

        with monkeypatch.context() as race_patch:
            race_patch.setattr(export_module.os, "link", link_then_replace)
            unknown_export = client.post(
                f"/v1/artifacts/{artifact['id']}/export",
                headers=_artifact_headers(racing_token, idempotency_key="racing-export"),
                json={
                    "workspace_root": str(racing),
                    "relative_path": "exports/result.txt",
                },
            )
        assert unknown_export.status_code == 500
        assert (detached_racing / "exports" / "result.txt").read_bytes() == body.encode()
        assert not (racing / "exports" / "result.txt").exists()
        with sqlite3.connect(database) as connection:
            receipt = connection.execute(
                "SELECT status FROM command_executions WHERE idempotency_key = ?",
                ("racing-export",),
            ).fetchone()
        assert receipt == ("manual_reconcile_required",)


def _prepare_trashed_artifact(
    client: TestClient,
    service: Any,
    *,
    content: str,
    policy_id: str,
) -> dict[str, Any]:
    artifact = _create_artifact(
        client,
        content=content,
        retention_policy_ref=policy_id,
    )
    for operation, suffix in (
        ("retention_schedule", "schedule-deletion"),
        ("retention_trash", "trash"),
    ):
        token = _issue(service, artifact["id"], operation)
        response = client.post(
            f"/v1/artifacts/{artifact['id']}/retention/{suffix}",
            headers=_artifact_headers(token),
        )
        assert response.status_code == 200
    assert (
        service.get_artifact_retention_state(artifact["id"]).lifecycle is RetentionLifecycle.TRASHED
    )
    return artifact


def test_retention_pin_grace_delete_crash_and_explicit_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "retention.sqlite3"
    artifact_root = tmp_path / "retention-artifacts"
    app = create_app(
        database,
        artifact_root=artifact_root,
        artifact_capability_secret=_CAPABILITY_SECRET,
        physical_delete_enabled=True,
        physical_delete_authorization=_PHYSICAL_AUTHORIZATION,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        service = app.state.operant_service
        policy = client.post(
            "/v1/retention-policies",
            headers={"Idempotency-Key": "create-delete-policy"},
            json={
                "id": "delete-after-zero-grace",
                "grace_period_seconds": 0,
                "allow_physical_delete": True,
            },
        )
        assert policy.status_code == 201

        pinned = _create_artifact(
            client,
            content="pinned-content",
            retention_policy_ref="delete-after-zero-grace",
        )
        pin_token = _issue(service, pinned["id"], "retention_pin")
        assert (
            client.post(
                f"/v1/artifacts/{pinned['id']}/retention/pin",
                headers=_artifact_headers(pin_token),
                json={"pinned": True},
            ).status_code
            == 200
        )
        schedule_token = _issue(service, pinned["id"], "retention_schedule")
        blocked = client.post(
            f"/v1/artifacts/{pinned['id']}/retention/schedule-deletion",
            headers=_artifact_headers(schedule_token),
        )
        assert blocked.status_code == 409
        unpin_token = _issue(service, pinned["id"], "retention_pin")
        assert (
            client.post(
                f"/v1/artifacts/{pinned['id']}/retention/pin",
                headers=_artifact_headers(unpin_token),
                json={"pinned": False},
            ).status_code
            == 200
        )
        archive_token = _issue(service, pinned["id"], "retention_archive")
        archived = client.post(
            f"/v1/artifacts/{pinned['id']}/retention/archive",
            headers=_artifact_headers(archive_token),
        )
        assert archived.status_code == 200
        assert archived.json()["lifecycle"] == "archived"

        artifact = _prepare_trashed_artifact(
            client,
            service,
            content="unknown-delete-outcome",
            policy_id="delete-after-zero-grace",
        )
        read_token = _issue(service, artifact["id"], "read")
        original_update = service.store.update_artifact_retention_state

        def fail_after_unlink(*args: Any, **kwargs: Any) -> Any:
            if kwargs.get("event_type") == "retention.physical_delete_completed":
                raise RuntimeError("injected database failure after unlink")
            return original_update(*args, **kwargs)

        monkeypatch.setattr(service.store, "update_artifact_retention_state", fail_after_unlink)
        delete_token = service.issue_physical_mutation_capability(
            bootstrap_authorization=_PHYSICAL_AUTHORIZATION,
            operation="physical_delete",
            resource_id=artifact["id"],
        )
        failed_delete = client.post(
            f"/v1/artifacts/{artifact['id']}/retention/physical-delete",
            headers=_artifact_headers(delete_token, idempotency_key="crashing-delete"),
        )
        assert failed_delete.status_code == 500
        monkeypatch.setattr(service.store, "update_artifact_retention_state", original_update)

        with sqlite3.connect(database) as connection:
            receipt = connection.execute(
                """
                SELECT id, action_hash, status FROM command_executions
                WHERE idempotency_key = 'crashing-delete'
                """
            ).fetchone()
        assert receipt is not None
        command_id, action_hash, status = receipt
        assert status == "manual_reconcile_required"
        assert (
            service.get_artifact_retention_state(artifact["id"]).lifecycle
            is RetentionLifecycle.TRASHED
        )
        missing_content = client.get(
            f"/v1/artifacts/{artifact['id']}/content",
            headers=_artifact_headers(read_token),
        )
        assert missing_content.status_code == 409

        reconcile_token = service.issue_physical_mutation_capability(
            bootstrap_authorization=_PHYSICAL_AUTHORIZATION,
            operation="reconcile_delete",
            resource_id=artifact["id"],
            operation_scope_hash=command_id,
        )
        reconciled = client.post(
            f"/v1/artifacts/{artifact['id']}/retention/reconcile-physical-delete",
            headers=_artifact_headers(reconcile_token, idempotency_key="reconcile-delete"),
            json={
                "prior_command_id": command_id,
                "prior_action_hash": action_hash,
            },
        )
        assert reconciled.status_code == 200
        assert reconciled.json()["lifecycle"] == "deleted"
        assert reconciled.json()["deleted_at"] is not None
        deleted_content = client.get(
            f"/v1/artifacts/{artifact['id']}/content",
            headers=_artifact_headers(read_token),
        )
        assert deleted_content.status_code == 410


def test_artifact_audit_is_read_only_and_orphan_repair_is_explicit(tmp_path: Path) -> None:
    database = tmp_path / "audit.sqlite3"
    artifact_root = tmp_path / "audit-artifacts"
    app = create_app(
        database,
        artifact_root=artifact_root,
        artifact_capability_secret=_CAPABILITY_SECRET,
        physical_delete_enabled=True,
        physical_delete_authorization=_PHYSICAL_AUTHORIZATION,
    )
    with TestClient(app) as client:
        with sqlite3.connect(database) as connection:
            audit_rows_before = connection.execute(
                "SELECT COUNT(*) FROM artifact_retention_audit_events"
            ).fetchone()
        assert audit_rows_before == (0,)
        empty_audit = client.get("/v1/artifact-audits")
        assert empty_audit.status_code == 200
        assert empty_audit.json()["findings"] == []
        assert not artifact_root.exists()

        orphan_content = b"phase1c-orphan"
        with ArtifactStore(artifact_root) as blob_store:
            orphan = blob_store.put_bytes(orphan_content)
        audit = client.get("/v1/artifact-audits")
        assert audit.status_code == 200
        finding = next(
            entry for entry in audit.json()["findings"] if entry["finding_type"] == "orphan_blob"
        )
        assert finding["content_hash"] == orphan.content_hash
        assert finding["repairable"] is True
        assert str(artifact_root) not in audit.text

        with sqlite3.connect(database) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM artifact_retention_audit_events"
            ).fetchone() == (0,)

        service = app.state.operant_service
        repair_token = service.issue_physical_mutation_capability(
            bootstrap_authorization=_PHYSICAL_AUTHORIZATION,
            operation="repair_orphan",
            resource_id=orphan.content_hash,
            operation_scope_hash=finding["finding_hash"],
        )
        repaired = client.post(
            "/v1/artifact-repairs/orphan-blob",
            headers=_artifact_headers(repair_token, idempotency_key="repair-orphan-once"),
            json={
                "content_hash": orphan.content_hash,
                "finding_hash": finding["finding_hash"],
            },
        )
        assert repaired.status_code == 200
        assert repaired.json()["repaired"] is True
        replayed = client.post(
            "/v1/artifact-repairs/orphan-blob",
            headers=_artifact_headers(repair_token, idempotency_key="repair-orphan-once"),
            json={
                "content_hash": orphan.content_hash,
                "finding_hash": finding["finding_hash"],
            },
        )
        assert replayed.status_code == 200
        assert replayed.headers["Idempotency-Replayed"] == "true"
        assert replayed.json() == repaired.json()
        assert client.get("/v1/artifact-audits").json()["findings"] == []

        registered = _create_artifact(client, content="registered-content")
        digest = registered["content_hash"]
        blob_path = artifact_root / "sha256" / digest[:2] / digest[2:4] / digest
        blob_path.write_bytes(b"x" * registered["size_bytes"])
        corrupt = client.get("/v1/artifact-audits")
        assert any(
            item["finding_type"] == "corrupt_blob" and item["artifact_id"] == registered["id"]
            for item in corrupt.json()["findings"]
        )
        blob_path.unlink()
        missing = client.get("/v1/artifact-audits")
        assert any(
            item["finding_type"] == "missing_blob" and item["artifact_id"] == registered["id"]
            for item in missing.json()["findings"]
        )
        (artifact_root / "sha256" / "AA").mkdir()
        unsafe = client.get("/v1/artifact-audits")
        assert any(item["finding_type"] == "unsafe_entry" for item in unsafe.json()["findings"])
        assert str(artifact_root) not in unsafe.text
        with sqlite3.connect(database) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM artifact_retention_audit_events"
            ).fetchone() == (1,)


def test_cache_observation_keeps_missing_usage_unknown_and_pages_by_cursor(
    tmp_path: Path,
) -> None:
    app = create_app(
        tmp_path / "cache.sqlite3",
        artifact_root=tmp_path / "cache-artifacts",
    )
    with TestClient(app) as client:
        service = app.state.operant_service
        profile = service.add_model_profile(
            ModelProfile(
                name="cache-model",
                model_id="cache-model",
                base_url="https://example.invalid/v1",
                secret_ref="OPERANT_PHASE1C_CACHE_KEY",
            )
        )
        role = service.create_role(
            RolePreset(
                name="Cache Observer",
                system_prompt="Observe cache facts only.",
                model_profile_id=profile.id,
            )
        )
        session = service.create_session(role.id)

        unknown = service._record_runtime_cache_observation(
            session,
            RuntimeEvent(
                event_type="model.completed",
                turn=1,
                payload={
                    "usage": ModelUsage(prompt_tokens=11).model_dump(mode="json"),
                    "provider_request_id": "API_KEY=plain-cache-secret",
                },
            ),
        )
        assert unknown.hit_status is CacheHitStatus.UNKNOWN
        assert unknown.cache_read_tokens is None
        assert unknown.completion_tokens is None
        assert "plain-cache-secret" not in (unknown.request_id or "")

        miss = service._record_runtime_cache_observation(
            session,
            RuntimeEvent(
                event_type="model.completed",
                turn=2,
                payload={
                    "usage": ModelUsage(
                        prompt_tokens=11,
                        completion_tokens=2,
                        cache_read_tokens=0,
                    ).model_dump(mode="json")
                },
            ),
        )
        assert miss.hit_status is CacheHitStatus.MISS
        assert miss.cache_read_tokens == 0
        assert unknown.cursor is not None and miss.cursor is not None
        page = client.get(
            "/v1/cache-observations",
            params={"after_cursor": unknown.cursor, "limit": 1},
        )
        assert page.status_code == 200
        assert [entry["id"] for entry in page.json()] == [miss.id]
        assert "plain-cache-secret" not in page.text

        direct = service.record_cache_observation(
            CacheObservation(
                provider="Provider",
                model="model",
                hit_status=CacheHitStatus.UNKNOWN,
            )
        )
        assert direct.prompt_tokens is None
        assert direct.cache_read_tokens is None


def test_v7_migration_failure_is_atomic_and_nonempty_rollback_is_refused(
    tmp_path: Path,
) -> None:
    database = tmp_path / "v7-atomic.sqlite3"
    assert SQLiteStore(database).migrate(6) == 6

    class BrokenV7Store(SQLiteStore):
        def _upgrade_v7(self, connection: sqlite3.Connection) -> None:
            super()._upgrade_v7(connection)
            connection.execute("CREATE TABLE injected_phase1c_failure(id TEXT)")
            raise RuntimeError("injected v7 failure")

    with pytest.raises(RuntimeError, match="injected v7 failure"):
        BrokenV7Store(database).migrate()
    assert SQLiteStore(database).schema_version() == 6
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'retention_policies'"
            ).fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'injected_phase1c_failure'"
            ).fetchone()
            is None
        )

    upgraded = SQLiteStore(database)
    upgraded.initialize()
    upgraded.add_cache_observation(
        CacheObservation(
            provider="provider",
            model="model",
            hit_status=CacheHitStatus.UNKNOWN,
        )
    )
    with pytest.raises(MigrationError, match="contain data"):
        upgraded.rollback(6, isolated=True)


def test_completed_receipts_do_not_masquerade_as_unknown_artifact_writes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "receipt-blockers.sqlite3"
    app = create_app(database, artifact_root=tmp_path / "receipt-artifacts")
    with TestClient(app) as client:
        artifact = _create_artifact(client, content="completed-command-reference")
        assert app.state.operant_service.store.artifact_deletion_blockers(artifact["id"]) == ()

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            UPDATE command_executions
            SET status = 'manual_reconcile_required', response_json = ?
            WHERE id = (SELECT id FROM command_executions ORDER BY created_at LIMIT 1)
            """,
            (f'{{"artifact_id":"{artifact["id"]}"}}',),
        )
    blockers = SQLiteStore(database).artifact_deletion_blockers(artifact["id"])
    assert "unknown_command_outcome" in blockers


def test_retention_compare_and_swap_rejects_one_concurrent_writer(tmp_path: Path) -> None:
    database = tmp_path / "retention-cas.sqlite3"
    app = create_app(database, artifact_root=tmp_path / "retention-cas-artifacts")
    with TestClient(app) as client:
        artifact = _create_artifact(client, content="concurrent-retention")
        store = app.state.operant_service.store
        current = store.get_artifact_retention_state(artifact["id"])
        updates = (
            current.model_copy(
                update={
                    "pinned": True,
                    "updated_at": current.updated_at + timedelta(microseconds=1),
                }
            ),
            current.model_copy(
                update={
                    "lifecycle": RetentionLifecycle.ARCHIVED,
                    "updated_at": current.updated_at + timedelta(microseconds=2),
                }
            ),
        )

        def write(index: int) -> str:
            try:
                store.update_artifact_retention_state(
                    updates[index],
                    expected_updated_at=current.updated_at,
                    event_type=("retention.pin_changed" if index == 0 else "retention.archived"),
                    action_hash=str(index + 1) * 64,
                )
            except ConflictError:
                return "conflict"
            return "updated"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = sorted(executor.map(write, range(2)))
        assert outcomes == ["conflict", "updated"]
