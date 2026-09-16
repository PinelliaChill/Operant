from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from operant.contracts.b2_1 import Scope, SourceRef, WorkspaceScope
from operant.contracts.b2_5 import HistoryEntry, HistoryPage
from operant.domain.threads import ConversationThread, LegacySourceType, ThreadLegacyRef
from operant.memory_plugins.maintenance import (
    CanonicalSourceProvider,
    MaintenanceBudget,
    MaintenanceExecutor,
    MaintenanceExtraction,
    MaintenanceSnapshot,
    MaintenanceState,
    PreparedCandidate,
    SQLiteMaintenanceCommitter,
    StaticSourceProvider,
    _digest,
)

UTC = timezone.utc


def _scope() -> Scope:
    return WorkspaceScope(kind="workspace", project_id="project-a", workspace_id="workspace-a")


def _source(index: int, *, scope: Scope | None = None) -> SourceRef:
    return SourceRef(
        source_type="item",
        source_id=f"item-{index}",
        revision=index,
        content_digest=f"{index:064x}",
        scope=scope or _scope(),
        permission_epoch=1,
        availability="available",
    )


def _snapshot(source: SourceRef, *, database_key: str = "key-a") -> MaintenanceSnapshot:
    return MaintenanceSnapshot(
        job_id=f"maintenance-{database_key}",
        idempotency_key=database_key,
        project_id="project-a",
        dataset_id="dataset-a",
        installation_id="installation-a",
        binding_id="binding-a",
        scope=_scope(),
        source_cursor=source.revision,
        expected_processed_cursor=0,
        source_digest=_digest([source.model_dump(mode="json")]),
        workflow_id="maintenance",
        workflow_version=1,
        plugin_id="plugin-a",
        plugin_version="1.0.0",
        package_digest="a" * 64,
        config_id="config-a",
        config_revision=1,
        config_digest="b" * 64,
        model_profile_id="profile-a",
        model_id="model-a",
        model_digest="c" * 64,
        prompt_version="role:maintenance:v1",
        budget=MaintenanceBudget(max_sources=3, max_output_tokens=128, max_attempts=2),
    )


def _committer(path: Path, seen: list[tuple[str, ...]]) -> SQLiteMaintenanceCommitter:
    def propose(connection, snapshot, candidates, sources):
        assert connection.in_transaction
        assert snapshot.source_cursor == sources[-1].revision
        ids = tuple(f"proposal-{index}" for index, _ in enumerate(candidates, start=1))
        seen.append(ids)
        return ids

    value = SQLiteMaintenanceCommitter(path, propose_in_transaction=propose)
    value.initialize()
    return value


class _Extractor:
    input_tokens = 12
    output_tokens = 8
    cost_usd = 0.0003

    def __init__(self, extraction: MaintenanceExtraction) -> None:
        self.extraction = extraction
        self.calls = 0

    async def extract(self, snapshot, sources, *, cancel_event):
        self.calls += 1
        return self.extraction


@pytest.mark.asyncio
async def test_sqlite_commit_is_atomic_and_idempotent(tmp_path: Path) -> None:
    source = _source(1)
    snapshot = _snapshot(source)
    seen: list[tuple[str, ...]] = []
    committer = _committer(tmp_path / "maintenance.sqlite3", seen)
    extraction = MaintenanceExtraction(
        request_id=snapshot.job_id,
        source_watermark=snapshot.source_cursor,
        candidates=(PreparedCandidate(content="candidate", source_indices=(0,)),),
        input_tokens=12,
        output_tokens=8,
        cost_usd=0.0003,
    )

    result = committer.commit_batch(
        snapshot,
        extraction,
        (source,),
        processed_cursor=1,
        attempt=1,
    )
    replay = committer.commit_batch(
        snapshot,
        extraction,
        (source,),
        processed_cursor=1,
        attempt=2,
    )

    assert result.state is MaintenanceState.SUCCEEDED
    assert result.proposal_ids == ("proposal-1",)
    assert result.input_tokens == 12
    assert result.output_tokens == 8
    assert result.cost_usd == pytest.approx(0.0003)
    assert replay == result
    assert seen == [("proposal-1",)]
    assert committer.get_watermark("dataset-a", "project_history") == 1


@pytest.mark.asyncio
async def test_executor_yields_to_foreground_before_model_or_commit(tmp_path: Path) -> None:
    source = _source(1)
    snapshot = _snapshot(source)
    seen: list[tuple[str, ...]] = []
    committer = _committer(tmp_path / "maintenance.sqlite3", seen)
    extractor = _Extractor(
        MaintenanceExtraction(
            request_id=snapshot.job_id,
            source_watermark=1,
            candidates=(PreparedCandidate(content="candidate", source_indices=(0,)),),
        )
    )
    executor = MaintenanceExecutor(
        source_provider=StaticSourceProvider((source,)),
        extractor=extractor,
        committer=committer,
        foreground_active=lambda: True,
    )

    result = await executor.execute(snapshot)

    assert result.state is MaintenanceState.RETRY_WAIT
    assert result.error_code == "foreground_priority"
    assert extractor.calls == 0
    assert seen == []


@pytest.mark.asyncio
async def test_executor_rejects_candidate_source_index_and_never_calls_governance(
    tmp_path: Path,
) -> None:
    source = _source(1)
    snapshot = _snapshot(source)
    seen: list[tuple[str, ...]] = []
    committer = _committer(tmp_path / "maintenance.sqlite3", seen)
    extractor = _Extractor(
        MaintenanceExtraction(
            request_id=snapshot.job_id,
            source_watermark=1,
            candidates=(PreparedCandidate(content="forged", source_indices=(9,)),),
        )
    )
    executor = MaintenanceExecutor(
        source_provider=StaticSourceProvider((source,)),
        extractor=extractor,
        committer=committer,
    )

    result = await executor.execute(snapshot)

    assert result.state is MaintenanceState.DEAD_LETTER
    assert result.error_code == "maintenance.source_claim"
    assert seen == []


def test_canonical_provider_pages_past_non_user_items_and_marks_maintenance_thread() -> None:
    scope = _scope()
    sources = (_source(1, scope=scope), _source(2, scope=scope), _source(3, scope=scope))
    marker = ConversationThread(
        id="thread-maintenance",
        workspace_ref="workspace:/tmp/project",
        legacy_refs=(
            ThreadLegacyRef(
                source_type=LegacySourceType.SESSION,
                source_id="maintenance:job",
            ),
        ),
    )

    class Store:
        def get_thread(self, thread_id: str) -> ConversationThread:
            if thread_id == "thread-maintenance":
                return marker
            return ConversationThread(id=thread_id, workspace_ref="workspace:/tmp/project")

    class Governance:
        store = Store()

        def search_history(self, project_id, *, cutoff_cursor, after_cursor, limit):
            del project_id, limit
            return HistoryPage(
                project_id="project-a",
                cutoff_cursor=cutoff_cursor,
                items=[
                    HistoryEntry(
                        item_id="agent-1",
                        thread_id="thread-user",
                        cursor=1,
                        occurred_at=datetime.now(UTC),
                        kind="agent_message",
                        excerpt="agent",
                        source=None,
                    ),
                    HistoryEntry(
                        item_id="user-1",
                        thread_id="thread-user",
                        cursor=1,
                        occurred_at=datetime.now(UTC),
                        kind="user_message",
                        excerpt="user",
                        source=sources[0],
                    ),
                    HistoryEntry(
                        item_id="maintenance-1",
                        thread_id="thread-maintenance",
                        cursor=2,
                        occurred_at=datetime.now(UTC),
                        kind="user_message",
                        excerpt="prompt",
                        source=sources[1],
                    ),
                    HistoryEntry(
                        item_id="user-2",
                        thread_id="thread-user",
                        cursor=3,
                        occurred_at=datetime.now(UTC),
                        kind="user_message",
                        excerpt="user two",
                        source=sources[2],
                    ),
                ],
                next_cursor=None,
            )

    snapshot = _snapshot(sources[2])
    provider = CanonicalSourceProvider(Governance())
    selected = provider.list_sources(snapshot, limit=2)

    assert selected == (sources[0], sources[2])
    assert provider.last_source_cursor == 3
