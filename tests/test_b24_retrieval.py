from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from operant.contracts.b2_1 import (
    CandidateReference,
    DatasetOwner,
    MemoryConditions,
    MemoryVersion,
    MemoryVersionRef,
    RecallRequest,
    RpcContext,
    SourceRef,
    WorkspaceScope,
)
from operant.memory_plugins.ledger import MemoryLedger
from operant.memory_plugins.retrieval import (
    MemoryConditionContext,
    QueryPlan,
    RetrievalPolicy,
    build_query_plan,
    compile_fts_query,
    conditions_match,
    retrieve_memories,
)

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
SCOPE = WorkspaceScope(kind="workspace", project_id="project-a", workspace_id="workspace-a")


def _request(
    query: str,
    *,
    explicit_refs: tuple[MemoryVersionRef, ...] = (),
    knowledge_cutoff: str = "0",
    max_candidates: int = 5,
) -> RecallRequest:
    context = RpcContext(
        sdk_version="operant-memory-sdk.v1",
        request_id="request-b24",
        installation_id="installation-b24",
        dataset_id="dataset-b24",
        scope=SCOPE,
        deadline=NOW + timedelta(minutes=1),
        cancel_token="cancel-b24",
        idempotency_key="idempotency-b24",
        request_digest="a" * 64,
        binding_epoch=1,
        permission_epoch=2,
        lease_fencing=3,
    )
    return RecallRequest(
        context=context,
        query=query,
        explicit_refs=explicit_refs,
        knowledge_cutoff=knowledge_cutoff,
        max_candidates=max_candidates,
        token_budget=0,
    )


def _version(
    record_id: str,
    content: str,
    *,
    version: int = 1,
    scope: WorkspaceScope = SCOPE,
    source_id: str | None = None,
    role_ids: tuple[str, ...] = (),
    agent_ids: tuple[str, ...] = (),
    conditions: MemoryConditions | None = None,
    dataset_id: str = "dataset-b24",
    evidence: str = "tested",
) -> MemoryVersion:
    digest = hashlib.sha256(content.encode()).hexdigest()
    source = SourceRef(
        source_type="item",
        source_id=source_id or f"source-{record_id}",
        revision=1,
        content_digest=digest,
        scope=scope,
        permission_epoch=2,
        availability="available",
    )
    return MemoryVersion(
        ref=MemoryVersionRef(
            dataset_id=dataset_id,
            record_id=record_id,
            version=version,
            content_digest=digest,
        ),
        owner=DatasetOwner(
            kind="plugin_dataset",
            owner_namespace=f"dataset:{dataset_id}",
            dataset_id=dataset_id,
            principal_id="principal-b24",
        ),
        kind="project",
        content_type="fact",
        scope=scope,
        role_ids=role_ids,
        agent_ids=agent_ids,
        content=content,
        sources=(source,),
        evidence=evidence,  # type: ignore[arg-type]
        sensitivity="internal",
        retention_policy_id="retain-b24",
        conditions=conditions or _conditions(),
        recorded_at=NOW,
    )


def _conditions(**updates: Any) -> MemoryConditions:
    values: dict[str, Any] = {
        "commit_ref": None,
        "tree_digest": None,
        "file_fingerprints": {},
        "environment_digest": None,
        "tool_versions": {},
        "verified_at": NOW,
        "valid_from": NOW - timedelta(days=1),
        "valid_until": None,
    }
    values.update(updates)
    return MemoryConditions(**values)


def test_query_plan_keeps_identifiers_and_short_cjk_terms() -> None:
    plan = build_query_plan("  项目迁移 PersistentContextComposer uv-migration MAPLE-42  ")

    assert isinstance(plan, QueryPlan)
    assert plan.normalized_query == "项目迁移 PersistentContextComposer uv-migration MAPLE-42"
    assert plan.group_labels[0] == "identifier"
    assert "PersistentContextComposer" in plan.groups[0]
    assert "uv-migration" in plan.groups[0]
    assert "MAPLE-42" in plan.groups[0]
    assert "项目迁移" in plan.groups[1]
    assert "项目" in plan.groups[1]
    assert plan.normalized_query not in plan.queries
    assert len(plan.queries) <= 36


def test_fts_group_is_quoted_without_allowing_query_syntax() -> None:
    assert compile_fts_query(("MAPLE-42", 'a"b', "中文")) == '"MAPLE-42" OR "a""b" OR "中文"'


def test_conditions_fail_closed_for_missing_facts_and_expiry() -> None:
    conditions = _conditions(
        commit_ref="abc123",
        file_fingerprints={"src.app.py": "b" * 64},
        tool_versions={"uv": "0.9"},
        valid_until=NOW + timedelta(hours=1),
    )
    current = MemoryConditionContext(
        commit_ref="abc123",
        file_fingerprints={"src.app.py": "b" * 64},
        tool_versions={"uv": "0.9"},
        at=NOW,
    )

    assert conditions_match(conditions, current)
    assert not conditions_match(conditions, MemoryConditionContext(commit_ref="abc123", at=NOW))
    assert not conditions_match(conditions, current, now=NOW + timedelta(hours=1))


def test_retrieval_merges_terms_filters_scope_and_deduplicates_records() -> None:
    current = _version(
        "record-current",
        "当前 PersistentContextComposer 使用 uv。",
        source_id="source-shared",
    )
    older = _version(
        "record-current",
        "旧 PersistentContextComposer 使用 poetry。",
        version=1,
        source_id="source-shared",
    )
    other_source = _version(
        "record-other",
        "项目迁移后运行 uv。",
        source_id="source-other",
    )
    foreign = _version(
        "record-foreign",
        "PersistentContextComposer 使用 uv。",
        scope=WorkspaceScope(kind="workspace", project_id="project-b", workspace_id="workspace-b"),
        source_id="source-foreign",
    )
    restricted = _version(
        "record-restricted",
        "PersistentContextComposer 使用 uv。",
        role_ids=("role-private",),
        source_id="source-private",
    )
    values = (current, older, other_source, foreign, restricted)
    calls: list[str] = []

    def search(
        dataset_id: str, text: str | None = None, *, scope: Any, limit: int
    ) -> list[MemoryVersion]:
        assert dataset_id == "dataset-b24"
        assert scope == SCOPE
        calls.append(text or "")
        return list(values)

    result = retrieve_memories(
        _request("项目迁移 PersistentContextComposer"),
        search,
        policy=RetrievalPolicy(diversity_limit=1),
    )

    assert [item.ref.record_id for item in result] == ["record-current", "record-other"]
    assert all(isinstance(item, CandidateReference) for item in result)
    assert "PersistentContextComposer" in calls
    assert "项目迁移" in calls or "项目" in calls


def test_explicit_reference_can_be_resolved_and_is_ranked_first() -> None:
    selected = _version("record-explicit", "内容与查询无关，但由用户显式引用。")
    reference = selected.ref

    def empty_search(*_args: Any, **_kwargs: Any) -> list[MemoryVersion]:
        return []

    result = retrieve_memories(
        _request("does-not-exist", explicit_refs=(reference,)),
        empty_search,
        resolve=lambda candidate: selected if candidate == reference else None,
    )

    assert [item.ref for item in result] == [reference]
    assert result[0].score == 1.0


def test_nonzero_knowledge_cutoff_requires_and_uses_resolver() -> None:
    before = _version("record-before", "保留 uv 迁移事实。")
    after = _version("record-after", "保留 uv 迁移事实。")

    def search(*_args: Any, **_kwargs: Any) -> list[MemoryVersion]:
        return [before, after]

    result = retrieve_memories(
        _request("uv", knowledge_cutoff="3"),
        search,
        cutoff_resolver=lambda version: {"record-before": "3", "record-after": "4"}[
            version.ref.record_id
        ],
    )
    assert [item.ref.record_id for item in result] == ["record-before"]
    assert retrieve_memories(_request("uv", knowledge_cutoff="3"), search) == ()


def test_ledger_search_signature_is_supported_without_mutating_ledger(tmp_path: Path) -> None:
    ledger = MemoryLedger(tmp_path / "memory.sqlite3")
    first = _version("record-ledger", "Alpha 项目使用 uv run pytest。")
    second = _version("record-second", "Alpha 项目执行 ruff。", source_id="source-second")
    ledger.confirm(ledger.propose(first))
    ledger.confirm(ledger.propose(second))

    request = _request("项目 uv")
    result = retrieve_memories(request, ledger.search)

    assert [item.ref.record_id for item in result] == ["record-ledger", "record-second"]
    assert ledger.get_head("dataset-b24", "record-ledger").state == "published"
