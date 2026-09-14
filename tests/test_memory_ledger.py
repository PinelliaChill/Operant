from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from operant.contracts.b2_1 import (
    DatasetOwner,
    MemoryConditions,
    MemoryVersion,
    MemoryVersionRef,
    SessionScope,
    SourceRef,
    WorkspaceScope,
)
from operant.memory_plugins import (
    IdempotencyConflictError,
    LedgerConflictError,
    LedgerNotFoundError,
    LedgerValidationError,
    MemoryLedger,
)


def owner(dataset_id: str = "data-a") -> DatasetOwner:
    return DatasetOwner(
        kind="plugin_dataset",
        owner_namespace=f"dataset:{dataset_id}",
        dataset_id=dataset_id,
        principal_id="user-a",
    )


def workspace_scope(workspace_id: str = "workspace-a") -> WorkspaceScope:
    return WorkspaceScope(kind="workspace", project_id="project-a", workspace_id=workspace_id)


def source(scope: WorkspaceScope) -> SourceRef:
    return SourceRef(
        source_type="item",
        source_id="item-a",
        revision=1,
        content_digest="a" * 64,
        scope=scope,
        permission_epoch=3,
        availability="available",
    )


def version(
    *,
    dataset_id: str = "data-a",
    record_id: str = "record-a",
    number: int = 1,
    content: str = "Run the focused tests after changing the memory adapter.",
    scope: WorkspaceScope | SessionScope | None = None,
    evidence: str = "user_asserted",
) -> MemoryVersion:
    actual_scope = scope or workspace_scope()
    return MemoryVersion(
        ref=MemoryVersionRef(
            dataset_id=dataset_id,
            record_id=record_id,
            version=number,
            content_digest=hashlib.sha256(content.encode()).hexdigest(),
        ),
        owner=owner(dataset_id),
        kind="project" if isinstance(actual_scope, WorkspaceScope) else "working",
        content_type="fact",
        scope=actual_scope,
        role_ids=(),
        agent_ids=(),
        content=content,
        sources=(source(workspace_scope()),) if evidence != "legacy_unverified" else (),
        evidence=evidence,
        sensitivity="internal",
        retention_policy_id="retain-default",
        conditions=MemoryConditions(
            commit_ref=None,
            tree_digest=None,
            file_fingerprints={},
            environment_digest=None,
            tool_versions={},
            verified_at=None,
            valid_from=datetime.now(timezone.utc),
            valid_until=None,
        ),
        recorded_at=datetime.now(timezone.utc),
    )


def test_schema_is_namespaced_and_version_is_immutable(tmp_path: Path) -> None:
    ledger = MemoryLedger(tmp_path / "memory.sqlite3")
    saved = ledger.save_version(version())

    assert "memory_ledger_versions" in MemoryLedger.schema_sql()
    assert "CREATE TABLE IF NOT EXISTS memories" not in MemoryLedger.schema_sql()
    with (
        pytest.raises(sqlite3.DatabaseError, match="immutable"),
        ledger._connect() as connection,  # noqa: SLF001 - direct guard verification
    ):
        connection.execute(
            "UPDATE memory_ledger_versions SET body = ? WHERE dataset_id = ?",
            ("{}", saved.ref.dataset_id),
        )
    with (
        pytest.raises(sqlite3.DatabaseError, match="immutable"),
        ledger._connect() as connection,  # noqa: SLF001 - direct guard verification
    ):
        connection.execute(
            "DELETE FROM memory_ledger_versions WHERE dataset_id = ?",
            (saved.ref.dataset_id,),
        )


def test_candidate_does_not_move_head_and_confirm_uses_cas(tmp_path: Path) -> None:
    ledger = MemoryLedger(tmp_path / "memory.sqlite3")
    first = version()
    proposal = ledger.propose(first)
    assert ledger.get_head("data-a", "record-a").published_version is None
    assert ledger.query("data-a") == []
    assert ledger.query("data-a", include_candidates=True) == [first]

    published = ledger.confirm_proposal(proposal, expected_head_revision=0)
    assert published.published_version == first.ref
    assert ledger.authorize_ref(first.ref, dataset_id="data-a") == first

    second = version(number=2, content="Use the new focused test command.")
    pending = ledger.propose(second, expected_head_revision=published.revision)
    assert ledger.get_head("data-a", "record-a") == published
    with pytest.raises(LedgerConflictError, match="head revision"):
        ledger.confirm_proposal(pending, expected_head_revision=0)
    assert ledger.get_proposal(pending.proposal_id).state == "conflict"


def test_idempotency_digest_binds_cas_and_proposal_fields(tmp_path: Path) -> None:
    ledger = MemoryLedger(tmp_path / "memory.sqlite3")
    first = version()
    ledger.save_version(
        first,
        expected_head_revision=0,
        permission_epoch=3,
        idempotency_key="save-request",
    )
    with pytest.raises(IdempotencyConflictError):
        ledger.save_version(
            first,
            expected_head_revision=99,
            permission_epoch=3,
            idempotency_key="save-request",
        )
    with pytest.raises(IdempotencyConflictError):
        ledger.save_version(
            first,
            expected_head_revision=0,
            permission_epoch=4,
            idempotency_key="save-request",
        )

    proposal = ledger.propose(
        first,
        expected_head_revision=0,
        operation="create",
        reason="first reason",
        extractor_version="extractor-a",
        idempotency_key="proposal-request",
    )
    assert proposal.reason == "first reason"
    with pytest.raises(IdempotencyConflictError):
        ledger.propose(
            first,
            expected_head_revision=99,
            operation="create",
            reason="first reason",
            extractor_version="extractor-a",
            idempotency_key="proposal-request",
        )
    with pytest.raises(IdempotencyConflictError):
        ledger.propose(
            first,
            expected_head_revision=0,
            operation="create",
            reason="second reason",
            extractor_version="extractor-a",
            idempotency_key="proposal-request",
        )


def test_new_record_save_checks_expected_head_revision(tmp_path: Path) -> None:
    ledger = MemoryLedger(tmp_path / "memory.sqlite3")

    with pytest.raises(LedgerConflictError, match="head revision"):
        ledger.save_version(version(), expected_head_revision=99)

    with pytest.raises(LedgerNotFoundError, match="memory head not found"):
        ledger.get_head("data-a", "record-a")

    saved = ledger.save_version(version(), expected_head_revision=0)
    assert saved.ref.version == 1
    assert ledger.get_head("data-a", "record-a").revision == 0


def test_proposal_owner_and_sources_match_stored_version(tmp_path: Path) -> None:
    ledger = MemoryLedger(tmp_path / "memory.sqlite3")
    saved = ledger.save_version(version())
    head = ledger.get_head("data-a", "record-a")

    forged_owner = owner().model_copy(update={"principal_id": "attacker"})
    forged = {
        "proposal_id": "forged-owner",
        "proposal_revision": 0,
        "owner": forged_owner,
        "operation": "create",
        "base_head": head,
        "proposed_version": saved.ref,
        "source_refs": saved.sources,
        "extractor_version": "plugin",
        "reason": "forged principal",
        "state": "pending",
    }
    with pytest.raises(LedgerConflictError, match="owner"):
        ledger.propose(proposal=forged, expected_head_revision=0)

    source_refs = saved.sources
    assert source_refs
    without_source = dict(forged)
    without_source.update(
        proposal_id="missing-source",
        owner=saved.owner,
        source_refs=(),
        reason="removed source",
    )
    with pytest.raises(LedgerConflictError, match="source references"):
        ledger.propose(proposal=without_source, expected_head_revision=0)

    reordered_source = source_refs[0].model_copy(update={"source_id": "item-b"})
    reordered = dict(forged)
    reordered.update(
        proposal_id="forged-source",
        owner=saved.owner,
        source_refs=(reordered_source,),
        reason="forged source",
    )
    with pytest.raises(LedgerConflictError, match="source references"):
        ledger.propose(proposal=reordered, expected_head_revision=0)


def test_correction_deactivation_sources_scope_and_count(tmp_path: Path) -> None:
    ledger = MemoryLedger(tmp_path / "memory.sqlite3")
    proposal = ledger.propose(version())
    head = ledger.confirm(proposal)
    corrected = ledger.correct(
        "data-a",
        "record-a",
        content="The corrected focused test command is authoritative.",
        expected_head_revision=head.revision,
    )
    corrected_head = ledger.confirm(corrected, expected_head_revision=head.revision)

    assert corrected_head.revision == 2
    assert ledger.get_sources("data-a", "record-a")
    assert ledger.count("data-a", scope=workspace_scope()) == 1
    assert ledger.count("data-a", scope=workspace_scope("other-workspace")) == 0
    inactive = ledger.deactivate("data-a", "record-a", expected_head_revision=2)
    assert inactive.state == "inactive"
    assert ledger.query("data-a") == []
    assert ledger.query("data-a", include_inactive=True)[0].content.startswith("The corrected")


def test_dataset_isolation_export_and_logical_tombstone(tmp_path: Path) -> None:
    ledger = MemoryLedger(tmp_path / "memory.sqlite3")
    ledger.confirm(ledger.propose(version(dataset_id="data-a")))
    other = version(dataset_id="data-b", content="Private data B")
    ledger.confirm(ledger.propose(other))

    assert [item.content for item in ledger.query("data-a")] == [
        "Run the focused tests after changing the memory adapter."
    ]
    assert ledger.count("data-b") == 1
    exported = ledger.export_dataset("data-a")
    assert exported["dataset_id"] == "data-a"
    assert all(item["owner"]["dataset_id"] == "data-a" for item in exported["versions"])

    tombstone = ledger.delete_record("data-a", "record-a", expected_head_revision=1)
    assert tombstone.state == "deleted"
    assert ledger.get_tombstone("data-a", "record-a") == tombstone
    assert ledger.count("data-a") == 0
    # The logical delete keeps history until the manager selects its explicit
    # data deletion policy; a late append remains blocked by the tombstone.
    with pytest.raises(LedgerConflictError, match="tombstoned"):
        ledger.save_version(version(dataset_id="data-a", content="late write"))

    ledger.delete_dataset("data-a")
    assert ledger.get_tombstone("data-a").record_id is None
    with pytest.raises(LedgerConflictError, match="tombstoned"):
        ledger.save_version(version(dataset_id="data-a", record_id="new-record"))


def test_legacy_import_is_explicitly_scoped_and_never_published(tmp_path: Path) -> None:
    ledger = MemoryLedger(tmp_path / "memory.sqlite3")
    old = {
        "id": "legacy-record",
        "version": 1,
        "content": "Imported old project fact",
        "kind": "project",
        "status": "active",
        "confidence": 0.99,
        "source_task": "old task text",
    }
    with pytest.raises(LedgerValidationError, match="explicit registered scope"):
        ledger.migrate_legacy([old], owner=owner())

    result = ledger.migrate_legacy([old], owner=owner(), scope=workspace_scope())
    assert result.imported_count == 1
    assert len(result.proposals) == 1
    assert result.proposals[0].state == "pending"
    assert ledger.get_head("data-a", "legacy-record").published_version is None
    assert ledger.query("data-a", include_candidates=True) == []
    assert (
        ledger.query("data-a", include_candidates=True, include_legacy=True)[0].evidence
        == "legacy_unverified"
    )

    repeated = ledger.migrate_legacy([old], owner=owner(), scope=workspace_scope())
    assert repeated.skipped_versions == 1
    assert repeated.imported_count == 1


def test_legacy_payload_scope_cannot_override_registered_scope(tmp_path: Path) -> None:
    ledger = MemoryLedger(tmp_path / "memory.sqlite3")
    old = {
        "id": "legacy-scoped-record",
        "version": 1,
        "content": "Imported old scoped fact",
        "kind": "project",
        "scope": {
            "kind": "workspace",
            "project_id": "other-project",
            "workspace_id": "other-workspace",
        },
    }
    with pytest.raises(LedgerConflictError, match="conflicts"):
        ledger.migrate_legacy([old], owner=owner(), scope=workspace_scope())

    old["scope"] = workspace_scope().model_dump(mode="json")
    result = ledger.migrate_legacy([old], owner=owner(), scope=workspace_scope())
    assert result.imported_versions[0].scope == workspace_scope()


def test_legacy_sqlite_source_is_read_only_and_export_is_json_serializable(tmp_path: Path) -> None:
    old_path = tmp_path / "old.sqlite3"
    with sqlite3.connect(old_path) as connection:
        connection.execute(
            "CREATE TABLE memory_versions (memory_id TEXT, version INTEGER, body TEXT)"
        )
        connection.execute(
            "INSERT INTO memory_versions VALUES (?, ?, ?)",
            (
                "legacy-sqlite",
                1,
                json.dumps(
                    {
                        "id": "legacy-sqlite",
                        "version": 1,
                        "content": "Read old rows without writing the source database.",
                        "kind": "project",
                    }
                ),
            ),
        )

    ledger = MemoryLedger(tmp_path / "new.sqlite3")
    result = ledger.migrate_legacy(old_path, owner=owner(), scope=workspace_scope())
    assert result.record_count == 1
    assert json.loads(ledger.export_json("data-a"))["dataset_id"] == "data-a"
    with sqlite3.connect(old_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_versions").fetchone()[0] == 1
