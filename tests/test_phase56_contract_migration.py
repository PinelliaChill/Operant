from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from operant.domain.graph import NodeKind, NodeSpec
from operant.domain.multiwriter import MergeNodePolicy, WriterIsolationKind, WriterNodePolicy
from operant.domain.remote_control import EncryptedRemoteCommand
from operant.domain.remote_execution import (
    CapabilityManifest,
    RemoteCapability,
    RemoteTargetRegistration,
)
from operant.domain.security import Capability
from operant.persistence.sqlite import MigrationError, SQLiteStore


def test_remote_control_and_execution_are_distinct_contracts() -> None:
    now = datetime.now(timezone.utc)
    command = EncryptedRemoteCommand(
        idempotency_key="remote-command-1",
        host_id="host-1",
        device_id="device-1",
        remote_session_id="session-1",
        protocol_version="phase56.v1",
        issued_at=now,
        expires_at=now + timedelta(seconds=30),
        nonce="n" * 16,
        ciphertext="opaque",
        signature="s" * 32,
    )
    target = RemoteTargetRegistration(
        display_name="build host",
        endpoint_ref="https://target.invalid",
        identity_public_key="i" * 32,
        credential_ref="OPERANT_REMOTE_TARGET_TOKEN",
        policy_ref="policy:remote-target",
        artifact_namespace="target-artifacts",
        capability_manifest=CapabilityManifest(
            version="1",
            capabilities=(RemoteCapability.TARGET_EXEC,),
            supported_operations=("run_command",),
            platform="linux-amd64",
        ),
    )

    assert command.host_id == "host-1"
    assert target.target_id.startswith("remote_target_")
    assert "device_id" not in RemoteTargetRegistration.model_fields
    assert "credential_ref" not in EncryptedRemoteCommand.model_fields
    assert Capability.REMOTE_CONTROL_COMMAND.value == "remote.control.command"
    assert Capability.REMOTE_TARGET_EXEC.value == "remote.target.exec"
    assert Capability.BROWSER_SUBMIT.value == "browser.submit"
    assert Capability.COMPUTER_INPUT.value == "computer.input"


def test_remote_command_rejects_invalid_expiry() -> None:
    now = datetime.now(timezone.utc)
    with pytest.raises(ValidationError, match="expiry must follow issuance"):
        EncryptedRemoteCommand(
            idempotency_key="remote-command-1",
            host_id="host-1",
            device_id="device-1",
            remote_session_id="session-1",
            protocol_version="phase56.v1",
            issued_at=now,
            expires_at=now,
            nonce="n" * 16,
            ciphertext="opaque",
            signature="s" * 32,
        )


def test_graph_contract_types_writer_and_merge_nodes() -> None:
    writer = NodeSpec(
        node_id="writer-a",
        node_kind=NodeKind.AGENT,
        writes_workspace=True,
        writer_policy=WriterNodePolicy(
            writer_key="writer-a",
            isolation_kind=WriterIsolationKind.WORKTREE,
            isolation_ref="worktree:writer-a",
            ownership_paths=("src/a",),
        ),
    )
    merge = NodeSpec(
        node_id="merge",
        node_kind=NodeKind.MERGE,
        writes_workspace=True,
        merge_policy=MergeNodePolicy(source_writer_keys=("writer-a", "writer-b")),
    )

    assert writer.writer_policy is not None
    assert merge.merge_policy is not None
    with pytest.raises(ValidationError, match="merge nodes require"):
        NodeSpec(node_id="bad-merge", node_kind=NodeKind.MERGE)


def test_v13_manifest_is_frozen_and_additive(tmp_path: Path) -> None:
    database = tmp_path / "phase56.sqlite3"
    store = SQLiteStore(database)
    assert store.migrate(target_version=12) == 12
    with sqlite3.connect(database) as connection:
        before = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    v12_manifest = SQLiteStore._migration_manifest(12)

    assert store.migrate(target_version=13) == 13
    assert store.schema_version() == 13
    assert SQLiteStore._migration_manifest(12) == v12_manifest
    manifest = SQLiteStore._migration_manifest(13)
    assert (
        hashlib.sha256(manifest.encode()).hexdigest() == (SQLiteStore._FROZEN_MANIFEST_SHA256[13])
    )
    checksum = hashlib.sha256(
        "\n".join(("13", "phase5b_remote_phase6_multiwriter", manifest)).encode()
    ).hexdigest()
    assert checksum == SQLiteStore._FROZEN_MIGRATION_CHECKSUMS[13]

    with sqlite3.connect(database) as connection:
        after = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert before < after
    assert {
        "remote_control_hosts",
        "remote_execution_targets",
        "writer_workspaces",
        "merge_runs",
    }.issubset(after)


def test_v13_rollback_requires_empty_phase56_tables(tmp_path: Path) -> None:
    empty = SQLiteStore(tmp_path / "empty.sqlite3")
    empty.initialize()
    assert empty.rollback(12, isolated=True) == 12

    populated = SQLiteStore(tmp_path / "populated.sqlite3")
    populated.initialize()
    with populated._connect() as connection:
        connection.execute(
            """
            INSERT INTO remote_control_hosts(
                host_id, display_name, signing_public_key, exchange_public_key,
                core_version, protocol_version, capabilities_json, enabled,
                online_state, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, '[]', 0, 'offline', ?, ?)
            """,
            (
                "host-1",
                "local",
                "s" * 32,
                "e" * 32,
                "1",
                "phase56.v1",
                datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    with pytest.raises(MigrationError, match="contain data"):
        populated.rollback(12, isolated=True)
