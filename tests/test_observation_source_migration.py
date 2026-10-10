"""Additive provenance migration preserves old observations without trusting them."""

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from operant.domain.remote_execution import (
    CapabilityManifest,
    CapabilityObservation,
    RemoteActionIdempotency,
    RemoteCapability,
    RemoteExecutionJob,
    RemoteTargetLease,
    RemoteTargetRegistration,
)
from operant.domain.security import ActionRequest, Capability, NormalizedTarget
from operant.persistence.remote_execution import SQLiteRemoteExecutionRepository
from operant.persistence.security import SQLiteSecurityRepository
from operant.persistence.sqlite import MigrationError, SQLiteStore


def test_v25_preserves_v24_data_and_frozen_history(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "upgrade.sqlite3")
    store.migrate(24)
    now = datetime.now(timezone.utc)
    repository = SQLiteRemoteExecutionRepository(store)
    target = repository.register_target(
        RemoteTargetRegistration(
            display_name="old local fixture",
            endpoint_ref="http://127.0.0.1:18780",
            identity_public_key="i" * 32,
            credential_ref="FIXTURE_REF",
            policy_ref="local-control:fixture",
            artifact_namespace="fixture",
            capability_manifest=CapabilityManifest(
                version="1",
                capabilities=(RemoteCapability.BROWSER_OBSERVE,),
                supported_operations=("observe",),
                platform="fixture",
            ),
        )
    )
    observation = CapabilityObservation(
        target_id=target.target_id,
        capability=RemoteCapability.BROWSER_OBSERVE,
        target_ref="http://127.0.0.1:18780",
        observation_hash="a" * 64,
        body={"url": "http://127.0.0.1:18780/"},
        created_at=now,
        expires_at=now + timedelta(seconds=60),
    )
    repository.record_observation(observation)
    history = store.list_applied_migrations()
    assert store.migrate(25) == 25
    assert store.list_applied_migrations()[:24] == history
    assert repository.get_observation(target.target_id, observation.observation_hash) == observation
    assert repository.get_observation_source(observation.observation_id) is None
    for version in range(1, 26):
        manifest = SQLiteStore._migration_manifest(version)
        assert (
            hashlib.sha256(manifest.encode()).hexdigest()
            == (SQLiteStore._FROZEN_MANIFEST_SHA256[version])
        )
    with store._connect() as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_v25_empty_rollback_and_missing_table_fail_closed(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "empty.sqlite3")
    store.migrate(25)
    assert store.rollback(24, isolated=True) == 24
    assert store.migrate(25) == 25
    with store._connect() as connection:
        connection.execute("DROP TABLE capability_observation_sources")
    with pytest.raises(MigrationError, match="capability_observation_sources"):
        store.initialize()


def test_v25_provenance_foreign_keys_checks_and_nonempty_rollback(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "sources.sqlite3")
    store.migrate(25)
    repository = SQLiteRemoteExecutionRepository(store)
    now = datetime.now(timezone.utc)
    target = repository.register_target(
        RemoteTargetRegistration(
            display_name="migration fixture",
            endpoint_ref="http://127.0.0.1:18780",
            identity_public_key="i" * 32,
            credential_ref="FIXTURE_REF",
            policy_ref="local-control:fixture",
            artifact_namespace="fixture",
            capability_manifest=CapabilityManifest(
                version="1",
                capabilities=(RemoteCapability.BROWSER_OBSERVE,),
                supported_operations=("observe",),
                platform="fixture",
            ),
        )
    )
    repository.heartbeat_target(
        target.target_id, identity_public_key=target.identity_public_key, now=now
    )
    lease = repository.acquire_lease(
        RemoteTargetLease(
            target_id=target.target_id,
            owner="fixture",
            token="fixture-lease-token",
            fencing=1,
            workspace_ref="fixture",
            expires_at=now + timedelta(minutes=5),
        ),
        now=now,
    )
    SQLiteSecurityRepository(store).record_security_action(
        ActionRequest(
            principal="fixture",
            tool="fixture",
            operation="observe",
            normalized_target=NormalizedTarget(
                target_type="remote_target", target_id=target.target_id
            ),
            requested_capabilities=(Capability.BROWSER_OBSERVE,),
            idempotency_key="fixture",
            action_hash="b" * 64,
            policy_version="fixture",
        )
    )
    job = repository.create_job(
        RemoteExecutionJob(
            target_id=target.target_id,
            lease_id=lease.lease_id,
            lease_fencing=lease.fencing,
            capability=RemoteCapability.BROWSER_OBSERVE,
            operation="observe",
            action_hash="b" * 64,
            idempotency_key="fixture",
            idempotency=RemoteActionIdempotency.IDEMPOTENT,
        ),
        lease_token=lease.token,
        now=now,
    )
    observation = repository.record_observation(
        CapabilityObservation(
            target_id=target.target_id,
            capability=RemoteCapability.BROWSER_OBSERVE,
            target_ref="fixture",
            observation_hash="a" * 64,
            body={},
            created_at=now,
            expires_at=now + timedelta(seconds=60),
        )
    )
    values = (observation.observation_id, job.job_id, lease.lease_id, lease.fencing, "c" * 64)
    for index in range(3):
        invalid = list(values)
        invalid[index] = "missing-parent"
        with (
            pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"),
            store._connect() as connection,
        ):
            connection.execute(
                "INSERT INTO capability_observation_sources VALUES (?,?,?,?,?)", invalid
            )
    for fencing, digest in ((0, "c" * 64), (1, "short"), (1, "z" * 64)):
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"), store._connect() as connection:
            connection.execute(
                "INSERT INTO capability_observation_sources VALUES (?,?,?,?,?)",
                (*values[:3], fencing, digest),
            )
    with store._connect() as connection:
        connection.execute("INSERT INTO capability_observation_sources VALUES (?,?,?,?,?)", values)
    with pytest.raises(MigrationError, match="observation v25 contains data"):
        store.rollback(24, isolated=True)
    assert store.schema_version() == 25
    with store._connect() as connection:
        assert (
            connection.execute("SELECT count(*) FROM capability_observation_sources").fetchone()[0]
            == 1
        )
