"""Focused B2-5 governance checks for history, source revocation and exact CAS."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from operant.api import create_app
from operant.contracts.b2_1 import SourceRef
from operant.contracts.b2_3 import ManagementCommand
from operant.contracts.b2_5 import ExactProposal, MemoryRelationship
from operant.domain.threads import ConversationThread, Item, Turn, UserMessagePayload
from operant.memory_plugins.governance import (
    GovernanceError,
    GovernancePermissionError,
    GovernanceService,
    PreparedCandidate,
)
from operant.memory_plugins.ledger import (
    LedgerConflictError,
    LedgerNotFoundError,
    LedgerValidationError,
)
from operant.memory_plugins.maintenance import MaintenanceBudget, MaintenanceSnapshot
from operant.memory_plugins.manager import MemoryManager


@pytest_asyncio.fixture
async def governance(tmp_path: Path):
    app = create_app(tmp_path / "core.sqlite3")
    service = app.state.operant_service
    manager = MemoryManager(service)
    project_result = await manager.execute(
        ManagementCommand(
            action="project_create",
            name="B2-5 测试项目",
            workspace_path=str(tmp_path),
        )
    )
    project_id = project_result.state.projects[-1].project_id
    installed = await manager.execute(
        ManagementCommand(
            action="plugin_install", plugin_id="memory-standard", mode="trusted_in_process"
        )
    )
    installation_id = installed.state.installations[-1].installation_id
    await manager.execute(
        ManagementCommand(
            action="binding_select",
            project_id=project_id,
            installation_id=installation_id,
        )
    )
    # ``initialize_schema`` is harmless after v17 and keeps this fixture useful
    # when it is run against an isolated store created at v16.
    service_governance = GovernanceService(manager, initialize_schema=True)
    yield service_governance, manager, service, project_id, installation_id
    await manager.close()
    service.close()


def _scope(manager: MemoryManager, project_id: str):
    return manager._scope(manager._project(project_id))  # noqa: SLF001 - focused domain fixture


def _source(manager: MemoryManager, project_id: str, item: Item) -> SourceRef:
    binding = manager.registry.get_binding(
        manager.registry.get_installation(
            manager._project(project_id)["installation_id"]  # noqa: SLF001
        ).binding_id
    )
    text = item.payload.text if isinstance(item.payload, UserMessagePayload) else item.payload.type
    return SourceRef(
        source_type="item",
        source_id=item.id,
        revision=item.cursor or 0,
        content_digest=hashlib.sha256(text.encode()).hexdigest(),
        scope=_scope(manager, project_id),
        permission_epoch=binding.permission_epoch,
        availability="available",
    )


def _history_item(service: Any, manager: MemoryManager, project_id: str) -> Item:
    project = manager._project(project_id)  # noqa: SLF001
    workspace = service.store.get_workspace_initialization_by_id(project["workspace_id"])
    thread = service.create_thread(ConversationThread(workspace_ref=workspace.workspace_ref))
    turn = service.create_turn(Turn(thread_id=thread.id))
    return service.append_item(
        Item(
            thread_id=thread.id,
            turn_id=turn.id,
            payload=UserMessagePayload(text="canonical user fact for governance"),
        )
    )


def _maintenance_snapshot(
    manager: MemoryManager,
    project_id: str,
    installation_id: str,
    source: SourceRef,
    *,
    job_id: str = "maintenance-test-job",
) -> MaintenanceSnapshot:
    installation = manager.registry.get_installation(installation_id)
    binding = manager.registry.get_binding(installation.binding_id)
    digest = hashlib.sha256(
        json.dumps(
            [source.model_dump(mode="json")],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return MaintenanceSnapshot(
        job_id=job_id,
        project_id=project_id,
        dataset_id=installation.dataset_id,
        installation_id=installation_id,
        binding_id=binding.binding_id,
        scope=source.scope,
        source_cursor=source.revision,
        expected_processed_cursor=0,
        source_digest=digest,
        workflow_id="memory-maintenance",
        workflow_version=1,
        plugin_id=installation.manifest.plugin_id,
        plugin_version=installation.manifest.plugin_version,
        package_digest=installation.manifest.package_digest,
        config_id="memory-config",
        config_revision=0,
        config_digest="a" * 64,
        model_profile_id="maintenance-profile",
        model_id="maintenance-model",
        model_digest="b" * 64,
        prompt_version="maintenance-prompt.v1",
        budget=MaintenanceBudget(max_sources=5),
    )


def _exact(entry) -> ExactProposal:
    proposal = entry.proposal
    return ExactProposal(
        proposal_id=proposal.proposal_id,
        proposal_revision=proposal.proposal_revision,
        proposed_version=proposal.proposed_version,
        base_head_revision=proposal.base_head.revision,
    )


def _publish(governance: GovernanceService, project_id: str, entry):
    (published,) = governance.review(project_id, _exact(entry), decision="accept")
    return published


@pytest.mark.asyncio
async def test_history_is_canonical_and_cutoff_is_historical(governance):
    service_governance, manager, service, project_id, _installation_id = governance
    first = _history_item(service, manager, project_id)
    second = _history_item(service, manager, project_id)

    page = service_governance.search_history(project_id, query="canonical")
    assert [item.item_id for item in page.items] == [first.id, second.id]
    first_page = service_governance.search_history(project_id, query="canonical", limit=1)
    assert first_page.next_cursor == first.cursor
    second_page = service_governance.search_history(
        project_id,
        query="canonical",
        after_cursor=first_page.next_cursor,
        limit=1,
    )
    assert [item.item_id for item in second_page.items] == [second.id]
    old_page = service_governance.search_history(
        project_id,
        query="canonical",
        cutoff_cursor=first.cursor,
    )
    assert [item.item_id for item in old_page.items] == [first.id]
    detail = service_governance.history_detail(project_id, first.id, cutoff_cursor=first.cursor)
    assert detail.text == "canonical user fact for governance"
    assert detail.perspective == "historical_fact"


@pytest.mark.asyncio
async def test_proposal_inbox_deduplicates_same_source_and_exact_batch_is_atomic(governance):
    service_governance, manager, service, project_id, installation_id = governance
    item = _history_item(service, manager, project_id)
    source = _source(manager, project_id, item)
    first = service_governance.propose(
        project_id,
        content="first candidate",
        record_id="record-first",
        sources=(source, source),
    )
    second = service_governance.propose(
        project_id,
        content="second candidate",
        record_id="record-second",
        sources=(source,),
    )
    assert first.independent_evidence_count == 1
    assert second.independent_evidence_count == 1

    stale = ExactProposal(
        proposal_id=second.proposal.proposal_id,
        proposal_revision=second.proposal.proposal_revision,
        proposed_version=second.proposal.proposed_version,
        base_head_revision=99,
    )
    exact_first = ExactProposal(
        proposal_id=first.proposal.proposal_id,
        proposal_revision=first.proposal.proposal_revision,
        proposed_version=first.proposal.proposed_version,
        base_head_revision=first.proposal.base_head.revision,
    )
    binding = manager.registry.get_binding(
        manager.registry.get_installation(installation_id).binding_id
    )
    with pytest.raises(LedgerConflictError, match="base head revision"):
        manager.ledger.confirm_proposals(
            [exact_first, stale],
            dataset_id=first.proposal.owner.dataset_id,
            permission_epoch=binding.permission_epoch,
        )
    assert manager.ledger.get_proposal(first.proposal.proposal_id).state == "pending"
    assert manager.ledger.get_proposal(second.proposal.proposal_id).state == "pending"
    assert manager.ledger.get_head(first.proposal.owner.dataset_id, "record-first").revision == 0


@pytest.mark.asyncio
async def test_source_revocation_blocks_recursive_derived_recall(governance):
    service_governance, manager, service, project_id, _installation_id = governance
    item = _history_item(service, manager, project_id)
    source = _source(manager, project_id, item)
    parent = service_governance.propose(
        project_id,
        content="parent fact",
        record_id="record-parent",
        sources=(source,),
    )
    (parent_entry,) = service_governance.review(
        project_id,
        [
            ExactProposal(
                proposal_id=parent.proposal.proposal_id,
                proposal_revision=parent.proposal.proposal_revision,
                proposed_version=parent.proposal.proposed_version,
                base_head_revision=parent.proposal.base_head.revision,
            )
        ],
        decision="accept",
    )
    parent_ref = parent_entry.version.ref
    derived_source = SourceRef(
        source_type="memory_version",
        source_id=parent_ref.record_id,
        revision=parent_ref.version,
        content_digest=parent_ref.content_digest,
        scope=parent_entry.version.scope,
        permission_epoch=source.permission_epoch,
        availability="available",
    )
    child = service_governance.propose(
        project_id,
        content="derived fact",
        record_id="record-child",
        sources=(derived_source,),
    )
    assert child.independent_evidence_count == 1
    service_governance.review(
        project_id,
        [
            ExactProposal(
                proposal_id=child.proposal.proposal_id,
                proposal_revision=child.proposal.proposal_revision,
                proposed_version=child.proposal.proposed_version,
                base_head_revision=child.proposal.base_head.revision,
            )
        ],
        decision="accept",
    )
    affected = service_governance.revoke_source(project_id, source)
    assert "record-parent@1" in affected
    assert "record-child@1" in affected
    assert not service_governance.is_recallable(project_id, parent_ref)
    child_ref = child.proposal.proposed_version
    assert not service_governance.is_recallable(project_id, child_ref)
    assert service_governance.search_history(project_id).items == []


@pytest.mark.asyncio
async def test_review_rechecks_closed_binding_and_never_self_confirms(governance):
    service_governance, manager, service, project_id, _installation_id = governance
    item = _history_item(service, manager, project_id)
    proposal = service_governance.propose(
        project_id,
        content="candidate awaiting user",
        sources=(_source(manager, project_id, item),),
    )
    exact = ExactProposal(
        proposal_id=proposal.proposal.proposal_id,
        proposal_revision=proposal.proposal.proposal_revision,
        proposed_version=proposal.proposal.proposed_version,
        base_head_revision=proposal.proposal.base_head.revision,
    )
    with pytest.raises(GovernancePermissionError, match="self-confirm"):
        service_governance.review(
            project_id,
            exact,
            decision="accept",
            reviewer_kind="system",
        )
    manager._state["global_enabled"] = False  # noqa: SLF001 - close barrier fixture
    with pytest.raises(GovernancePermissionError, match="disabled"):
        service_governance.review(project_id, exact, decision="accept")


@pytest.mark.asyncio
async def test_maintenance_commit_is_atomic_and_duplicate_event_is_idempotent(governance):
    service_governance, manager, service, project_id, installation_id = governance
    item = _history_item(service, manager, project_id)
    source = _source(manager, project_id, item)
    snapshot = _maintenance_snapshot(manager, project_id, installation_id, source)
    extraction = service_governance.prepare_extraction(
        source_cursor=source.revision,
        sources=(source,),
        candidates=(PreparedCandidate(content="bounded inferred fact", source_indexes=(0,)),),
    )
    with (
        pytest.raises(RuntimeError, match="rollback probe"),
        manager.store._connect() as connection,
    ):
        connection.execute("BEGIN IMMEDIATE")
        service_governance.commit_prepared_extraction(connection, snapshot, extraction)
        raise RuntimeError("rollback probe")
    assert manager.ledger.list_proposals(snapshot.dataset_id) == []
    with manager.store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        first = service_governance.commit_prepared_extraction(connection, snapshot, extraction)
    with manager.store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        second = service_governance.commit_prepared_extraction(connection, snapshot, extraction)
    assert first == second and len(first) == 1
    assert len(manager.ledger.list_proposals(snapshot.dataset_id)) == 1
    with manager.store._connect() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM memory_ledger_versions WHERE dataset_id=?",
                (snapshot.dataset_id,),
            ).fetchone()[0]
            == 1
        )


@pytest.mark.asyncio
async def test_expired_candidate_rejects_accept_but_allows_exact_reject(governance):
    service_governance, manager, service, project_id, _installation_id = governance
    item = _history_item(service, manager, project_id)
    source = _source(manager, project_id, item)
    expired = service_governance.propose(
        project_id,
        content="过期候选",
        sources=(source,),
        review_due_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    exact = _exact(expired)
    with pytest.raises(LedgerConflictError, match="deadline"):
        service_governance.review(project_id, exact, decision="accept")
    assert manager.ledger.get_proposal(expired.proposal.proposal_id).state == "pending"
    assert manager.ledger.get_head(
        expired.proposal.owner.dataset_id, expired.version.ref.record_id
    ).state == ("unpublished")
    rejected = service_governance.review(project_id, exact, decision="reject")
    assert rejected[0].proposal.state == "rejected"
    with manager.store._connect() as connection:
        assert connection.execute("SELECT count(*) FROM b25_governance_reviews").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT decision FROM b25_governance_reviews WHERE proposal_id=?",
                (expired.proposal.proposal_id,),
            ).fetchone()[0]
            == "reject"
        )


@pytest.mark.asyncio
async def test_supersedes_atomically_inactivates_old_head(governance):
    service_governance, manager, service, project_id, _installation_id = governance
    item = _history_item(service, manager, project_id)
    source = _source(manager, project_id, item)
    old = service_governance.propose(
        project_id,
        content="旧项目约定",
        record_id="record-old-agreement",
        sources=(source,),
    )
    old_published = _publish(service_governance, project_id, old)
    replacement = service_governance.propose(
        project_id,
        content="新项目约定",
        record_id="record-new-agreement",
        sources=(source,),
        relationships=(
            MemoryRelationship(relation="supersedes", target=old_published.version.ref),
        ),
    )
    (new_published,) = service_governance.review(
        project_id,
        _exact(replacement),
        decision="accept",
    )
    dataset = old_published.version.ref.dataset_id
    old_head = manager.ledger.get_head(dataset, old_published.version.ref.record_id)
    new_head = manager.ledger.get_head(dataset, new_published.version.ref.record_id)
    assert old_head.state == "inactive"
    assert new_head.state == "published"
    assert not service_governance.is_recallable(project_id, old_published.version.ref)
    assert service_governance.is_recallable(project_id, new_published.version.ref)
    usable = {
        record.version.ref.record_id
        for record in service_governance.current_records(project_id)
        if record.currently_usable
    }
    assert usable == {new_published.version.ref.record_id}


@pytest.mark.asyncio
async def test_conflicting_candidate_cannot_be_accepted(governance):
    service_governance, manager, service, project_id, _installation_id = governance
    item = _history_item(service, manager, project_id)
    source = _source(manager, project_id, item)
    old = service_governance.propose(
        project_id,
        content="现行约定",
        record_id="record-current-agreement",
        sources=(source,),
    )
    old_published = _publish(service_governance, project_id, old)
    conflict = service_governance.propose(
        project_id,
        content="互相冲突的候选",
        record_id="record-conflict-agreement",
        sources=(source,),
        relationships=(
            MemoryRelationship(relation="conflicts_with", target=old_published.version.ref),
        ),
    )
    with pytest.raises(GovernanceError, match="conflict"):
        service_governance.review(project_id, _exact(conflict), decision="accept")
    dataset = old_published.version.ref.dataset_id
    assert (
        manager.ledger.get_head(dataset, old_published.version.ref.record_id).state == "published"
    )
    assert manager.ledger.get_proposal(conflict.proposal.proposal_id).state == "pending"
    with manager.store._connect() as connection:
        assert connection.execute("SELECT count(*) FROM b25_governance_reviews").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_mixed_conflict_and_supersedes_batch_rolls_back_everything(governance):
    service_governance, manager, service, project_id, _installation_id = governance
    item = _history_item(service, manager, project_id)
    source = _source(manager, project_id, item)
    old = service_governance.propose(
        project_id,
        content="批量处理前的约定",
        record_id="record-batch-old",
        sources=(source,),
    )
    old_published = _publish(service_governance, project_id, old)
    superseding = service_governance.propose(
        project_id,
        content="批量替代候选",
        record_id="record-batch-new",
        sources=(source,),
        relationships=(
            MemoryRelationship(relation="supersedes", target=old_published.version.ref),
        ),
    )
    conflicting = service_governance.propose(
        project_id,
        content="批量冲突候选",
        record_id="record-batch-conflict",
        sources=(source,),
        relationships=(
            MemoryRelationship(relation="conflicts_with", target=old_published.version.ref),
        ),
    )
    with pytest.raises(GovernanceError, match="conflict"):
        service_governance.review(
            project_id,
            [_exact(superseding), _exact(conflicting)],
            decision="accept",
        )
    dataset = old_published.version.ref.dataset_id
    assert (
        manager.ledger.get_head(dataset, old_published.version.ref.record_id).state == "published"
    )
    assert (
        manager.ledger.get_head(dataset, superseding.version.ref.record_id).state == "unpublished"
    )
    assert (
        manager.ledger.get_head(dataset, conflicting.version.ref.record_id).state == "unpublished"
    )
    assert manager.ledger.get_proposal(superseding.proposal.proposal_id).state == "pending"
    assert manager.ledger.get_proposal(conflicting.proposal.proposal_id).state == "pending"
    with manager.store._connect() as connection:
        assert connection.execute("SELECT count(*) FROM b25_governance_reviews").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_propose_metadata_failure_rolls_back_ledger_and_governance_rows(
    governance, monkeypatch
):
    service_governance, manager, service, project_id, _installation_id = governance
    item = _history_item(service, manager, project_id)
    source = _source(manager, project_id, item)

    def fail_metadata(*_args, **_kwargs):
        raise RuntimeError("governance metadata failure")

    monkeypatch.setattr(
        service_governance,
        "_proposal_metadata_in_connection",
        fail_metadata,
    )
    with pytest.raises(RuntimeError, match="metadata failure"):
        service_governance.propose(
            project_id,
            content="must roll back as one transaction",
            record_id="record-atomic-propose",
            sources=(source,),
        )
    installation = manager.registry.get_installation(
        manager._project(project_id)["installation_id"]  # noqa: SLF001
    )
    dataset = installation.dataset_id
    assert manager.ledger.list_proposals(dataset) == []
    with pytest.raises(LedgerNotFoundError):
        manager.ledger.get_head(dataset, "record-atomic-propose")
    with manager.store._connect() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM memory_ledger_versions WHERE dataset_id=?",
                (dataset,),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM b25_governance_proposals WHERE dataset_id=?",
                (dataset,),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM b25_governance_dependencies WHERE dataset_id=?",
                (dataset,),
            ).fetchone()[0]
            == 0
        )


@pytest.mark.asyncio
async def test_ledger_rejects_content_digest_mismatch_before_publishing(governance):
    service_governance, manager, service, project_id, _installation_id = governance
    item = _history_item(service, manager, project_id)
    source = _source(manager, project_id, item)
    seed = service_governance.propose(
        project_id,
        content="valid first version",
        record_id="record-digest-integrity",
        sources=(source,),
    )
    bad = seed.version.model_copy(
        update={
            "content": "正文与引用 digest 不一致",
            "ref": seed.version.ref.model_copy(update={"version": 2, "content_digest": "a" * 64}),
        }
    )
    with pytest.raises(LedgerValidationError, match="content digest"):
        service_governance.propose(
            project_id,
            version=bad,
            expected_head_revision=seed.proposal.base_head.revision,
        )
    dataset = seed.version.ref.dataset_id
    assert len(manager.ledger.list_versions(dataset, "record-digest-integrity")) == 1
    assert manager.ledger.get_proposal(seed.proposal.proposal_id).state == "pending"


@pytest.mark.asyncio
async def test_history_rejects_future_cutoff_cursor(governance):
    service_governance, manager, service, project_id, _installation_id = governance
    first = _history_item(service, manager, project_id)
    second = _history_item(service, manager, project_id)
    assert first.cursor is not None and second.cursor is not None
    with pytest.raises(ValueError, match="future"):
        service_governance.search_history(project_id, cutoff_cursor=second.cursor + 1)


@pytest.mark.asyncio
async def test_projection_recursively_blocks_child_after_legacy_parent_deactivate(governance):
    service_governance, manager, service, project_id, _installation_id = governance
    item = _history_item(service, manager, project_id)
    source = _source(manager, project_id, item)
    parent = service_governance.propose(
        project_id,
        content="parent that will be deactivated",
        record_id="record-legacy-parent",
        sources=(source,),
    )
    parent_published = _publish(service_governance, project_id, parent)
    parent_ref = parent_published.version.ref
    derived_source = SourceRef(
        source_type="memory_version",
        source_id=parent_ref.record_id,
        revision=parent_ref.version,
        content_digest=parent_ref.content_digest,
        scope=parent_published.version.scope,
        permission_epoch=source.permission_epoch,
        availability="available",
    )
    child = service_governance.propose(
        project_id,
        content="child derived from a stopped parent",
        record_id="record-legacy-child",
        sources=(derived_source,),
    )
    child_published = _publish(service_governance, project_id, child)
    manager.ledger.deactivate(
        parent_ref.dataset_id,
        parent_ref.record_id,
        expected_head_revision=parent_published.current_head.revision,
    )
    projected = {
        record.version.ref.record_id: record
        for record in service_governance.current_records(project_id)
    }
    assert projected[child_published.version.ref.record_id].currently_usable is False
    assert projected[child_published.version.ref.record_id].blocked_reason == "source_unavailable"
    assert not service_governance.is_recallable(project_id, child_published.version.ref)
