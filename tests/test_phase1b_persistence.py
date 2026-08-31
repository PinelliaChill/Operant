from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from operant.domain.context import (
    PHASE1B_PROMPT_BLOCK_ORDER,
    Compaction,
    CompactionSourceType,
    CompactionSummary,
    ContextReferenceType,
    ContextRevision,
    ContextSourceRef,
    ContextSourceType,
    ContextVisibility,
    ContextWatermark,
    ContextWatermarkState,
    PromptBlock,
    PromptBlockType,
    PromptLayout,
    ReferenceBinding,
    ReferenceIncludeMode,
    compaction_coverage_hash,
)
from operant.domain.memory import Memory, MemoryKind, MemoryStatus
from operant.domain.messages import Message, MessageRole
from operant.domain.models import AgentInstance, Budget, ModelProfile, RolePreset, Session, new_id
from operant.domain.threads import (
    Artifact,
    ConversationThread,
    Item,
    ThreadStatus,
    Turn,
    UserMessagePayload,
)
from operant.persistence.sqlite import ConflictError, MigrationError, NotFoundError, SQLiteStore


def _scope(
    store: SQLiteStore,
) -> tuple[ModelProfile, RolePreset, Session, AgentInstance]:
    profile = store.add_model_profile(
        ModelProfile(
            name="phase1b-model",
            model_id="phase1b-model",
            base_url="https://example.invalid/v1",
            secret_ref="OPERANT_PHASE1B_TEST_KEY",
            context_window=16_384,
        )
    )
    role = store.create_role(
        RolePreset(
            name="Phase 1B",
            system_prompt="Use the composed Context.",
            model_profile_id=profile.id,
            budget=Budget(max_output_tokens=1_024),
        )
    )
    session = store.create_session(role.id)
    agent = store.create_agent(session.id)
    return profile, role, session, agent


def _revision(session_id: str, agent_id: str, *, ordinal: int = 1) -> ContextRevision:
    revision_id = new_id("context")
    role_content = "Use the composed Context."
    conversation = '[{"role":"user","content":"Inspect safely."}]'
    blocks = (
        PromptBlock(
            revision_id=revision_id,
            position=1,
            block_type=PromptBlockType.ROLE_INSTRUCTIONS,
            content=role_content,
            content_hash=hashlib.sha256(role_content.encode()).hexdigest(),
            source_refs=(
                ContextSourceRef(
                    source_type=ContextSourceType.SESSION,
                    source_id=session_id,
                    content_hash=hashlib.sha256(role_content.encode()).hexdigest(),
                ),
            ),
            token_estimate=6,
            cache_eligible=True,
        ),
        PromptBlock(
            revision_id=revision_id,
            position=2,
            block_type=PromptBlockType.TOOL_SCHEMA,
            content="[]",
            content_hash=hashlib.sha256(b"[]").hexdigest(),
            source_refs=(
                ContextSourceRef(
                    source_type=ContextSourceType.TOOL_SCHEMA,
                    source_id=session_id,
                    content_hash=hashlib.sha256(b"[]").hexdigest(),
                ),
            ),
            token_estimate=1,
            cache_eligible=True,
        ),
        PromptBlock(
            revision_id=revision_id,
            position=3,
            block_type=PromptBlockType.CONVERSATION,
            content=conversation,
            content_hash=hashlib.sha256(conversation.encode()).hexdigest(),
            source_refs=(
                ContextSourceRef(
                    source_type=ContextSourceType.AGENT,
                    source_id=agent_id,
                    content_hash=hashlib.sha256(conversation.encode()).hexdigest(),
                ),
            ),
            token_estimate=12,
        ),
    )
    return ContextRevision(
        id=revision_id,
        session_id=session_id,
        agent_id=agent_id,
        request_ordinal=ordinal,
        model_id="phase1b-model",
        prompt_layout=PromptLayout(),
        messages=(
            Message(role=MessageRole.SYSTEM, content=role_content),
            Message(role=MessageRole.USER, content="Inspect safely."),
        ),
        tools=(),
        blocks=blocks,
        watermark=ContextWatermark(
            state=ContextWatermarkState.GREEN,
            context_window=16_384,
            reserved_output_tokens=1_024,
            tool_schema_token_estimate=1,
            safety_margin_tokens=820,
            available_input_tokens=14_540,
            pre_compaction_token_estimate=19,
            input_token_estimate=19,
            estimation_method="utf8_bytes_ceil_div_4",
        ),
    )


def _item_body_hash(item: Item) -> str:
    return hashlib.sha256(
        item.model_copy(update={"cursor": None}).model_dump_json().encode("utf-8")
    ).hexdigest()


def _thread_item_refs_hash(refs: tuple[ContextSourceRef, ...]) -> str:
    digest = hashlib.sha256()
    for ref in refs:
        digest.update(
            json.dumps(
                {
                    "id": ref.source_id,
                    "cursor": ref.cursor,
                    "content_hash": ref.content_hash,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _thread_item_compaction(
    session_id: str,
    agent_id: str,
    thread_id: str,
    refs: tuple[ContextSourceRef, ...],
) -> Compaction:
    summary = CompactionSummary(active_goal="Preserve exact canonical Item evidence.")
    summary_json = summary.model_dump_json()
    assert refs[0].cursor is not None and refs[-1].cursor is not None
    return Compaction(
        session_id=session_id,
        agent_id=agent_id,
        thread_id=thread_id,
        source_type=CompactionSourceType.THREAD_ITEMS,
        source_cursor_start=refs[0].cursor,
        source_cursor_end=refs[-1].cursor,
        source_snapshot_hash=_thread_item_refs_hash(refs),
        summary=summary,
        content_hash=hashlib.sha256(summary_json.encode("utf-8")).hexdigest(),
        covered_item_refs=refs,
    )


def _revision_with_compaction(
    session_id: str,
    agent_id: str,
    thread_id: str,
    compaction: Compaction,
    *,
    ordinal: int = 1,
) -> ContextRevision:
    base = _revision(session_id, agent_id, ordinal=ordinal)
    compaction_content = compaction.summary.model_dump_json()
    compaction_block = PromptBlock(
        revision_id=base.id,
        position=3,
        block_type=PromptBlockType.COMPACTION,
        content=compaction_content,
        content_hash=hashlib.sha256(compaction_content.encode("utf-8")).hexdigest(),
        source_refs=(
            ContextSourceRef(
                source_type=ContextSourceType.COMPACTION,
                source_id=compaction.id,
                content_hash=compaction.content_hash,
            ),
        ),
        token_estimate=1,
    )
    blocks = (
        base.blocks[0],
        base.blocks[1],
        compaction_block,
        base.blocks[2].model_copy(update={"position": 4}),
    )
    return ContextRevision.model_validate(
        {
            **base.model_dump(mode="python"),
            "thread_id": thread_id,
            "blocks": blocks,
            "compaction_id": compaction.id,
            "compaction_refs": (compaction.id,),
            "source_item_ids": tuple(ref.source_id for ref in compaction.covered_item_refs),
            "source_cursor_start": compaction.source_cursor_start,
            "source_cursor_end": compaction.source_cursor_end,
            "source_cursor_namespace": "items.sequence",
        }
    )


def _revision_with_memory(
    session_id: str,
    agent_id: str,
    memory: Memory,
    *,
    ordinal: int = 1,
    workspace_ref: str | None = None,
    with_binding: bool = True,
) -> ContextRevision:
    base = _revision(session_id, agent_id, ordinal=ordinal)
    memory_hash = hashlib.sha256(memory.model_dump_json().encode("utf-8")).hexdigest()
    memory_source = ContextSourceRef(
        source_type=ContextSourceType.MEMORY,
        source_id=memory.id,
        cursor=memory.version,
        content_hash=memory_hash,
    )
    blocks = tuple(
        block.model_copy(update={"source_refs": (*block.source_refs, memory_source)})
        if block.block_type is PromptBlockType.CONVERSATION
        else block
        for block in base.blocks
    )
    bindings = (
        (
            ReferenceBinding(
                revision_id=base.id,
                position=1,
                ref_type=ContextReferenceType.MEMORY,
                resolved_target=memory.id,
                source_snapshot_hash=memory_hash,
                include_mode=ReferenceIncludeMode.INLINE,
                visibility=ContextVisibility.SESSION,
            ),
        )
        if with_binding
        else ()
    )
    return base.model_copy(
        update={
            "workspace_ref": workspace_ref,
            "blocks": blocks,
            "reference_bindings": bindings,
            "memory_refs": (memory.id,),
        }
    )


def _compaction_scope(
    store: SQLiteStore,
    workspace: Path,
) -> tuple[Session, AgentInstance, ConversationThread, tuple[Item, ...], Compaction]:
    _profile, _role, session, agent = _scope(store)
    thread = store.create_thread(ConversationThread(workspace_ref=str(workspace.resolve())))
    turn = store.create_turn(Turn(thread_id=thread.id))
    items = tuple(
        store.append_item(
            Item(
                thread_id=thread.id,
                turn_id=turn.id,
                payload=UserMessagePayload(text=f"canonical-{index}"),
            )
        )
        for index in range(3)
    )
    refs = tuple(
        ContextSourceRef(
            source_type=ContextSourceType.ITEM,
            source_id=item.id,
            cursor=item.cursor,
            content_hash=_item_body_hash(item),
        )
        for item in items[:2]
    )
    return (
        session,
        agent,
        thread,
        items,
        _thread_item_compaction(session.id, agent.id, thread.id, refs),
    )


@pytest.mark.parametrize("starting_version", [1, 2, 3, 4, 5])
def test_v1_through_v5_upgrade_to_v6_preserves_rows(
    tmp_path: Path,
    starting_version: int,
) -> None:
    database = tmp_path / f"from-v{starting_version}.sqlite3"
    store = SQLiteStore(database)
    assert store.migrate(starting_version) == starting_version
    profile = store.add_model_profile(
        ModelProfile(
            name=f"from-v{starting_version}",
            model_id="legacy-model",
            base_url="https://example.invalid/v1",
            secret_ref="OPERANT_PHASE1B_LEGACY_KEY",
        )
    )

    store.initialize()
    store.initialize()

    assert store.schema_version() == 6
    assert store.get_model_profile(profile.id) == profile
    applied_v6 = store.list_applied_migrations()[-1]
    assert applied_v6["name"] == "phase1b_context_composer"
    assert applied_v6["checksum"] == SQLiteStore._FROZEN_MIGRATION_CHECKSUMS[6]
    manifest = store._migration_manifest(6)
    assert (
        hashlib.sha256(manifest.encode("utf-8")).hexdigest()
        == (SQLiteStore._FROZEN_MANIFEST_SHA256[6])
    )
    with pytest.raises(NotFoundError):
        store.list_context_revisions("missing")


def test_unrecognized_v6_preview_history_is_rejected_instead_of_adopted(tmp_path: Path) -> None:
    database = tmp_path / "unknown-v6-preview.sqlite3"
    store = SQLiteStore(database)
    assert store.migrate(5) == 5
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO schema_migrations(version, name, checksum, applied_at)
            VALUES (6, 'phase1b_context_composer', ?, '2026-08-29T00:00:00+00:00')
            """,
            ("0" * 64,),
        )

    with pytest.raises(MigrationError, match="checksum mismatch at version 6"):
        store.initialize()
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'context_revisions'"
            ).fetchone()
            is None
        )


def test_v6_failure_is_atomic_and_empty_rollback_is_explicit(tmp_path: Path) -> None:
    database = tmp_path / "atomic.sqlite3"
    SQLiteStore(database).migrate(5)

    class BrokenV6Store(SQLiteStore):
        def _upgrade_v6(self, connection: sqlite3.Connection) -> None:
            super()._upgrade_v6(connection)
            connection.execute("CREATE TABLE injected_phase1b_failure(id TEXT)")
            raise RuntimeError("injected v6 failure")

    with pytest.raises(RuntimeError, match="injected v6 failure"):
        BrokenV6Store(database).migrate()
    assert SQLiteStore(database).schema_version() == 5
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'context_revisions'"
            ).fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'injected_phase1b_failure'"
            ).fetchone()
            is None
        )

    empty = SQLiteStore(tmp_path / "empty.sqlite3")
    empty.initialize()
    assert empty.rollback(5, isolated=True) == 5
    assert empty.migrate() == 6


def test_concurrent_v6_initialization_and_context_request_identity(tmp_path: Path) -> None:
    database = tmp_path / "concurrent.sqlite3"

    with ThreadPoolExecutor(max_workers=3) as executor:
        versions = list(executor.map(lambda _value: SQLiteStore(database).migrate(), range(3)))
    assert versions == [6, 6, 6]

    store = SQLiteStore(database)
    _profile, _role, session, agent = _scope(store)
    revision = _revision(session.id, agent.id)

    def append() -> str:
        try:
            return SQLiteStore(database).append_context_revision(revision).id
        except ConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _value: append(), range(2)))
    assert outcomes == [revision.id, revision.id]
    assert store.list_context_revisions(session.id) == [store.get_context_revision(revision.id)]

    conflicting = revision.model_copy(update={"model_id": "different-model"})
    with pytest.raises(ConflictError, match="different evidence"):
        store.append_context_revision(conflicting)


def test_context_revision_and_children_are_immutable_and_ordered(tmp_path: Path) -> None:
    database = tmp_path / "immutable.sqlite3"
    store = SQLiteStore(database)
    store.initialize()
    _profile, _role, session, agent = _scope(store)
    revision = _revision(session.id, agent.id)
    persisted = store.append_context_revision(revision)

    assert persisted.cursor is not None
    assert persisted.messages == revision.messages
    assert persisted.blocks == revision.blocks
    assert persisted.watermark.input_token_estimate == 19
    with sqlite3.connect(database) as connection:
        for statement in (
            "UPDATE context_revisions SET model_id='forged' WHERE id=?",
            "DELETE FROM context_revisions WHERE id=?",
            "UPDATE prompt_blocks SET content='forged' WHERE revision_id=?",
            "DELETE FROM prompt_blocks WHERE revision_id=?",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement, (revision.id,))


def test_phase1b_prompt_layout_rejects_custom_versions_and_relative_block_order() -> None:
    with pytest.raises(ValueError, match="only supports PromptLayout"):
        PromptLayout(version="custom.v1")
    with pytest.raises(ValueError, match="block order is frozen"):
        PromptLayout(block_order=tuple(reversed(PHASE1B_PROMPT_BLOCK_ORDER)))

    revision = _revision("session_layout", "agent_layout")
    role_block, tool_block, conversation_block = revision.blocks
    wrong_order = (
        role_block,
        conversation_block.model_copy(update={"position": 2}),
        tool_block.model_copy(update={"position": 3}),
    )
    with pytest.raises(ValueError, match="frozen PromptLayout"):
        ContextRevision.model_validate(
            {
                **revision.model_dump(mode="python"),
                "blocks": wrong_order,
            }
        )


def test_store_and_sql_triggers_reject_forged_item_provenance(tmp_path: Path) -> None:
    database = tmp_path / "forged-provenance.sqlite3"
    store = SQLiteStore(database)
    store.initialize()
    _profile, _role, session, agent = _scope(store)
    thread = store.create_thread(ConversationThread(workspace_ref=str(tmp_path.resolve())))
    turn = store.create_turn(Turn(thread_id=thread.id))
    item = store.append_item(
        Item(
            thread_id=thread.id,
            turn_id=turn.id,
            payload=UserMessagePayload(text="authorized item"),
        )
    )
    other_thread = store.create_thread(ConversationThread(workspace_ref=str(tmp_path.resolve())))
    other_turn = store.create_turn(Turn(thread_id=other_thread.id))
    other_item = store.append_item(
        Item(
            thread_id=other_thread.id,
            turn_id=other_turn.id,
            payload=UserMessagePayload(text="cross-thread item"),
        )
    )
    assert item.cursor is not None and other_item.cursor is not None
    valid = store.append_context_revision(
        _revision(session.id, agent.id).model_copy(update={"thread_id": thread.id})
    )

    cross_thread_cursor = _revision(session.id, agent.id, ordinal=2).model_copy(
        update={
            "thread_id": thread.id,
            "source_item_ids": (other_item.id,),
            "source_cursor_start": other_item.cursor,
            "source_cursor_end": other_item.cursor,
            "source_cursor_namespace": "items.sequence",
        }
    )
    with pytest.raises(ConflictError, match="cursor coverage|outside its Thread"):
        store.append_context_revision(cross_thread_cursor)

    missing_cursor = other_item.cursor + 10_000
    no_item_cursor = _revision(session.id, agent.id, ordinal=2).model_copy(
        update={
            "thread_id": thread.id,
            "source_cursor_start": missing_cursor,
            "source_cursor_end": missing_cursor,
            "source_cursor_namespace": "items.sequence",
        }
    )
    with pytest.raises(ConflictError, match="cursor coverage"):
        store.append_context_revision(no_item_cursor)

    forged_source = ContextSourceRef(
        source_type=ContextSourceType.ITEM,
        source_id=other_item.id,
        cursor=other_item.cursor,
        content_hash=hashlib.sha256(other_item.model_dump_json().encode("utf-8")).hexdigest(),
    )
    typed_source_base = _revision(session.id, agent.id, ordinal=2)
    typed_source_revision = typed_source_base.model_copy(
        update={
            "thread_id": thread.id,
            "blocks": tuple(
                block.model_copy(update={"source_refs": (*block.source_refs, forged_source)})
                if block.block_type is PromptBlockType.CONVERSATION
                else block
                for block in typed_source_base.blocks
            ),
        }
    )
    with pytest.raises(ConflictError, match="Item source is invalid"):
        store.append_context_revision(typed_source_revision)

    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM context_revisions WHERE id = ?",
            (valid.id,),
        ).fetchone()
        assert row is not None
        columns = [
            str(column["name"])
            for column in connection.execute("PRAGMA table_info(context_revisions)").fetchall()
            if column["name"] != "sequence"
        ]
        raw_revision = {column: row[column] for column in columns}
        raw_revision.update(
            {
                "id": "context_raw_missing_cursor",
                "request_ordinal": 99,
                "source_cursor_start": missing_cursor,
                "source_cursor_end": missing_cursor,
                "source_cursor_namespace": "items.sequence",
            }
        )
        placeholders = ", ".join("?" for _ in columns)
        with pytest.raises(sqlite3.IntegrityError, match="source scope is invalid"):
            connection.execute(
                f"INSERT INTO context_revisions({', '.join(columns)}) VALUES ({placeholders})",
                tuple(raw_revision[column] for column in columns),
            )
        for suffix, cursor_start, cursor_end in (
            ("start", None, missing_cursor),
            ("end", missing_cursor, None),
        ):
            partial_range = {
                **raw_revision,
                "id": f"context_raw_partial_cursor_{suffix}",
                "request_ordinal": 100 if suffix == "start" else 101,
                "source_cursor_start": cursor_start,
                "source_cursor_end": cursor_end,
                "source_cursor_namespace": "items.sequence",
            }
            with pytest.raises(sqlite3.IntegrityError, match="source scope is invalid"):
                connection.execute(
                    f"INSERT INTO context_revisions({', '.join(columns)}) VALUES ({placeholders})",
                    tuple(partial_range[column] for column in columns),
                )

        forged_content = "forged typed source"
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(
                """
                INSERT INTO prompt_blocks(
                    revision_id, position, id, block_type, content, content_hash,
                    source_refs_json, stable_until, visibility, token_estimate,
                    cache_eligible
                ) VALUES (?, 4, ?, 'conversation', ?, ?, ?, NULL, 'role_private', 1, 0)
                """,
                (
                    valid.id,
                    "ctxblock_raw_cross_thread",
                    forged_content,
                    hashlib.sha256(forged_content.encode("utf-8")).hexdigest(),
                    json.dumps(
                        [forged_source.model_dump(mode="json")],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            )


@pytest.mark.parametrize("source_type", tuple(ContextSourceType), ids=lambda item: item.value)
def test_store_rejects_forged_typed_prompt_block_sources(
    tmp_path: Path,
    source_type: ContextSourceType,
) -> None:
    database = tmp_path / f"forged-store-{source_type.value}.sqlite3"
    store = SQLiteStore(database)
    store.initialize()
    _profile, _role, session, agent = _scope(store)
    thread = store.create_thread(ConversationThread(workspace_ref=str(tmp_path.resolve())))
    turn = store.create_turn(Turn(thread_id=thread.id))
    store.append_item(
        Item(
            thread_id=thread.id,
            turn_id=turn.id,
            payload=UserMessagePayload(text="authorized item"),
        )
    )
    other_thread = store.create_thread(ConversationThread(workspace_ref=str(tmp_path.resolve())))
    other_turn = store.create_turn(Turn(thread_id=other_thread.id))
    other_item = store.append_item(
        Item(
            thread_id=other_thread.id,
            turn_id=other_turn.id,
            payload=UserMessagePayload(text="cross-thread item"),
        )
    )
    inactive_memory = store.create_memory(
        Memory(
            kind=MemoryKind.PROJECT,
            content="not active",
            project_scope=str(tmp_path.resolve()),
        )
    )
    assert other_thread.cursor is not None and other_item.cursor is not None

    base = _revision(session.id, agent.id, ordinal=2).model_copy(update={"thread_id": thread.id})
    block_index = 2
    if source_type is ContextSourceType.SESSION:
        block_index = 0
        source = ContextSourceRef(
            source_type=source_type,
            source_id=session.id,
            cursor=1,
            content_hash=base.blocks[block_index].content_hash,
        )
    elif source_type is ContextSourceType.AGENT:
        source = ContextSourceRef(
            source_type=source_type,
            source_id=agent.id,
            cursor=1,
            content_hash=base.blocks[block_index].content_hash,
        )
    elif source_type is ContextSourceType.TOOL_SCHEMA:
        block_index = 1
        source = ContextSourceRef(
            source_type=source_type,
            source_id=session.id,
            cursor=1,
            content_hash=base.blocks[block_index].content_hash,
        )
    elif source_type is ContextSourceType.ITEM:
        source = ContextSourceRef(
            source_type=source_type,
            source_id=other_item.id,
            cursor=other_item.cursor,
            content_hash=hashlib.sha256(other_item.model_dump_json().encode("utf-8")).hexdigest(),
        )
    elif source_type is ContextSourceType.THREAD:
        source = ContextSourceRef(
            source_type=source_type,
            source_id=other_thread.id,
            cursor=other_thread.cursor,
            content_hash="a" * 64,
        )
    elif source_type is ContextSourceType.ARTIFACT:
        source = ContextSourceRef(
            source_type=source_type,
            source_id="artifact_missing",
            cursor=999_999,
            content_hash="a" * 64,
        )
    elif source_type is ContextSourceType.MEMORY:
        source = ContextSourceRef(
            source_type=source_type,
            source_id=inactive_memory.id,
            content_hash=hashlib.sha256(
                inactive_memory.model_dump_json().encode("utf-8")
            ).hexdigest(),
        )
    else:
        assert source_type is ContextSourceType.COMPACTION
        source = ContextSourceRef(
            source_type=source_type,
            source_id="compaction_missing",
            content_hash="a" * 64,
        )

    blocks = list(base.blocks)
    blocks[block_index] = blocks[block_index].model_copy(update={"source_refs": (source,)})
    forged = base.model_copy(update={"blocks": tuple(blocks)})

    with pytest.raises(ConflictError, match="Prompt Block .* source is invalid"):
        store.append_context_revision(forged)


@pytest.mark.parametrize(
    "source_type",
    (*tuple(ContextSourceType), "unknown"),
    ids=lambda item: item.value if isinstance(item, ContextSourceType) else item,
)
def test_sql_trigger_rejects_forged_typed_prompt_block_sources(
    tmp_path: Path,
    source_type: ContextSourceType | str,
) -> None:
    source_label = source_type.value if isinstance(source_type, ContextSourceType) else source_type
    database = tmp_path / f"forged-sql-{source_label}.sqlite3"
    store = SQLiteStore(database)
    store.initialize()
    _profile, _role, session, agent = _scope(store)
    thread = store.create_thread(ConversationThread(workspace_ref=str(tmp_path.resolve())))
    other_thread = store.create_thread(ConversationThread(workspace_ref=str(tmp_path.resolve())))
    other_turn = store.create_turn(Turn(thread_id=other_thread.id))
    other_item = store.append_item(
        Item(
            thread_id=other_thread.id,
            turn_id=other_turn.id,
            payload=UserMessagePayload(text="cross-thread item"),
        )
    )
    inactive_memory = store.create_memory(
        Memory(
            kind=MemoryKind.PROJECT,
            content="not active",
            project_scope=str(tmp_path.resolve()),
        )
    )
    assert other_thread.cursor is not None and other_item.cursor is not None
    valid = store.append_context_revision(
        _revision(session.id, agent.id).model_copy(update={"thread_id": thread.id})
    )
    forged_content = f"forged {source_label} source"
    block_hash = hashlib.sha256(forged_content.encode("utf-8")).hexdigest()

    if source_type is ContextSourceType.SESSION:
        raw_source = {
            "source_type": source_label,
            "source_id": session.id,
            "cursor": 1,
            "content_hash": block_hash,
        }
    elif source_type is ContextSourceType.AGENT:
        raw_source = {
            "source_type": source_label,
            "source_id": agent.id,
            "cursor": 1,
            "content_hash": block_hash,
        }
    elif source_type is ContextSourceType.TOOL_SCHEMA:
        raw_source = {
            "source_type": source_label,
            "source_id": session.id,
            "cursor": 1,
            "content_hash": block_hash,
        }
    elif source_type is ContextSourceType.ITEM:
        raw_source = {
            "source_type": source_label,
            "source_id": other_item.id,
            "cursor": other_item.cursor,
            "content_hash": hashlib.sha256(
                other_item.model_dump_json().encode("utf-8")
            ).hexdigest(),
        }
    elif source_type is ContextSourceType.THREAD:
        raw_source = {
            "source_type": source_label,
            "source_id": other_thread.id,
            "cursor": other_thread.cursor,
            "content_hash": "a" * 64,
        }
    elif source_type is ContextSourceType.ARTIFACT:
        raw_source = {
            "source_type": source_label,
            "source_id": "artifact_missing",
            "cursor": 999_999,
            "content_hash": "a" * 64,
        }
    elif source_type is ContextSourceType.MEMORY:
        raw_source = {
            "source_type": source_label,
            "source_id": inactive_memory.id,
            "cursor": None,
            "content_hash": hashlib.sha256(
                inactive_memory.model_dump_json().encode("utf-8")
            ).hexdigest(),
        }
    else:
        raw_source = {
            "source_type": source_label,
            "source_id": "compaction_missing",
            "cursor": None,
            "content_hash": "a" * 64,
        }

    with (
        sqlite3.connect(database) as connection,
        pytest.raises(sqlite3.DatabaseError),
    ):
        connection.execute(
            """
            INSERT INTO prompt_blocks(
                revision_id, position, id, block_type, content, content_hash,
                source_refs_json, stable_until, visibility, token_estimate,
                cache_eligible
            ) VALUES (?, 4, ?, 'conversation', ?, ?, ?, NULL, 'role_private', 1, 0)
            """,
            (
                valid.id,
                f"ctxblock_raw_{source_label}",
                forged_content,
                block_hash,
                json.dumps([raw_source], sort_keys=True, separators=(",", ":")),
            ),
        )


@pytest.mark.parametrize("source_type", tuple(ContextSourceType), ids=lambda item: item.value)
def test_sql_trigger_rejects_wrong_hash_for_every_prompt_source(
    tmp_path: Path,
    source_type: ContextSourceType,
) -> None:
    store = SQLiteStore(tmp_path / f"wrong-hash-{source_type.value}.sqlite3")
    store.initialize()
    session, agent, thread, items, compaction = _compaction_scope(store, tmp_path)
    revision = store.append_context_revision(
        _revision_with_compaction(session.id, agent.id, thread.id, compaction),
        compaction=compaction,
    )
    artifact_bytes = b"canonical artifact bytes"
    artifact_hash = hashlib.sha256(artifact_bytes).hexdigest()
    artifact, _created = store.register_artifact(
        Artifact(
            content_hash=artifact_hash,
            media_type="text/plain",
            size_bytes=len(artifact_bytes),
            retention_policy_ref="phase1b-test",
        ),
        storage_key=store._artifact_storage_key(artifact_hash),
    )
    memory = store.create_memory(
        Memory(
            kind=MemoryKind.PROJECT,
            content="canonical active memory",
            project_scope=str(tmp_path.resolve()),
            status=MemoryStatus.ACTIVE,
        )
    )
    wrong_hash = "f" * 64
    source_ids = {
        ContextSourceType.SESSION: (session.id, None),
        ContextSourceType.AGENT: (agent.id, None),
        ContextSourceType.THREAD: (thread.id, thread.cursor),
        ContextSourceType.ITEM: (items[0].id, items[0].cursor),
        ContextSourceType.ARTIFACT: (artifact.id, artifact.cursor),
        ContextSourceType.MEMORY: (memory.id, None),
        ContextSourceType.TOOL_SCHEMA: (session.id, None),
        ContextSourceType.COMPACTION: (compaction.id, None),
    }
    source_id, cursor = source_ids[source_type]
    raw_source = {
        "source_type": source_type.value,
        "source_id": source_id,
        "cursor": cursor,
        "content_hash": wrong_hash,
    }
    content = f"wrong hash probe for {source_type.value}"
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    with (
        store._connect() as connection,
        pytest.raises(
            sqlite3.IntegrityError,
            match="source reference is invalid",
        ),
    ):
        connection.execute(
            """
            INSERT INTO prompt_blocks(
                revision_id, position, id, block_type, content, content_hash,
                source_refs_json, stable_until, visibility, token_estimate,
                cache_eligible
            ) VALUES (?, 5, ?, 'conversation', ?, ?, ?, NULL, 'role_private', 1, 0)
            """,
            (
                revision.id,
                f"ctxblock_wrong_hash_{source_type.value}",
                content,
                content_hash,
                json.dumps([raw_source], sort_keys=True, separators=(",", ":")),
            ),
        )


@pytest.mark.parametrize(
    "mutation",
    ("empty", "wrong_item_hash", "duplicate", "unsorted", "range", "digest"),
)
def test_store_rejects_invalid_thread_item_compaction_evidence(
    tmp_path: Path,
    mutation: str,
) -> None:
    store = SQLiteStore(tmp_path / f"store-compaction-{mutation}.sqlite3")
    store.initialize()
    session, agent, thread, items, valid = _compaction_scope(store, tmp_path)
    refs = valid.covered_item_refs
    if mutation == "empty":
        forged = valid.model_copy(update={"covered_item_refs": ()})
    elif mutation == "wrong_item_hash":
        forged_refs = (
            refs[0].model_copy(update={"content_hash": "f" * 64}),
            refs[1],
        )
        forged = valid.model_copy(
            update={
                "covered_item_refs": forged_refs,
                "source_snapshot_hash": _thread_item_refs_hash(forged_refs),
            }
        )
    elif mutation == "duplicate":
        forged_refs = (refs[0], refs[0])
        forged = valid.model_copy(
            update={
                "covered_item_refs": forged_refs,
                "source_cursor_end": refs[0].cursor,
                "source_snapshot_hash": _thread_item_refs_hash(forged_refs),
            }
        )
    elif mutation == "unsorted":
        forged_refs = tuple(reversed(refs))
        forged = valid.model_copy(
            update={
                "covered_item_refs": forged_refs,
                "source_snapshot_hash": _thread_item_refs_hash(forged_refs),
            }
        )
    elif mutation == "range":
        assert items[2].cursor is not None
        forged = valid.model_copy(update={"source_cursor_end": items[2].cursor})
    else:
        forged = valid.model_copy(update={"source_snapshot_hash": "f" * 64})
    revision = _revision_with_compaction(session.id, agent.id, thread.id, valid)

    with pytest.raises(ConflictError):
        store.append_context_revision(revision, compaction=forged)


@pytest.mark.parametrize(
    "mutation",
    (
        "not_array",
        "empty",
        "extra_field",
        "wrong_item_hash",
        "duplicate",
        "unsorted",
        "range",
        "digest",
    ),
)
def test_sql_trigger_rejects_invalid_thread_item_compaction_evidence(
    tmp_path: Path,
    mutation: str,
) -> None:
    store = SQLiteStore(tmp_path / f"sql-compaction-{mutation}.sqlite3")
    store.initialize()
    session, agent, thread, items, valid = _compaction_scope(store, tmp_path)
    raw_refs = [ref.model_dump(mode="json") for ref in valid.covered_item_refs]
    cursor_start = valid.source_cursor_start
    cursor_end = valid.source_cursor_end
    if mutation == "not_array":
        raw_coverage: object = raw_refs[0]
    elif mutation == "empty":
        raw_coverage = []
    elif mutation == "extra_field":
        raw_coverage = [{**raw_refs[0], "extra": True}, raw_refs[1]]
    elif mutation == "wrong_item_hash":
        raw_coverage = [{**raw_refs[0], "content_hash": "f" * 64}, raw_refs[1]]
    elif mutation == "duplicate":
        raw_coverage = [raw_refs[0], raw_refs[0]]
        cursor_end = cursor_start
    elif mutation == "unsorted":
        raw_coverage = list(reversed(raw_refs))
    elif mutation == "range":
        raw_coverage = raw_refs
        assert items[2].cursor is not None
        cursor_end = items[2].cursor
    else:
        raw_coverage = raw_refs
    covered_json = json.dumps(raw_coverage, sort_keys=True, separators=(",", ":"))
    source_snapshot_hash = (
        "f" * 64
        if mutation == "digest"
        else _thread_item_refs_hash(
            tuple(ContextSourceRef.model_validate(ref) for ref in raw_coverage)
        )
        if isinstance(raw_coverage, list)
        and raw_coverage
        and all(
            isinstance(ref, dict)
            and set(ref)
            == {
                "source_type",
                "source_id",
                "cursor",
                "content_hash",
            }
            for ref in raw_coverage
        )
        else valid.source_snapshot_hash
    )

    with store._connect() as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO compactions(
                id, session_id, agent_id, thread_id, source_type,
                source_cursor_start, source_cursor_end, source_snapshot_hash,
                summary_json, content_hash, covered_item_refs_json, created_at
            ) VALUES (?, ?, ?, ?, 'thread_items', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"compaction_raw_{mutation}",
                session.id,
                agent.id,
                thread.id,
                cursor_start,
                cursor_end,
                source_snapshot_hash,
                valid.summary.model_dump_json(),
                valid.content_hash,
                covered_json,
                valid.created_at.isoformat(),
            ),
        )


def test_thread_item_compaction_roundtrip_and_readback_revalidate_digest(tmp_path: Path) -> None:
    database = tmp_path / "compaction-readback.sqlite3"
    store = SQLiteStore(database)
    store.initialize()
    session, agent, thread, _items, compaction = _compaction_scope(store, tmp_path)
    revision = _revision_with_compaction(session.id, agent.id, thread.id, compaction)

    persisted_revision = store.append_context_revision(revision, compaction=compaction)
    persisted_compaction = store.get_compaction(compaction.id)
    assert persisted_compaction.covered_item_refs == compaction.covered_item_refs
    assert store.get_context_revision(revision.id) == persisted_revision

    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER compactions_no_update")
        connection.execute(
            "UPDATE compactions SET source_snapshot_hash = ? WHERE id = ?",
            ("f" * 64, compaction.id),
        )

    with pytest.raises(ConflictError, match="snapshot hash"):
        store.get_compaction(compaction.id)
    with pytest.raises(ConflictError, match="snapshot hash"):
        store.get_context_revision(revision.id)


def test_context_revision_readback_revalidates_typed_prompt_block_sources(
    tmp_path: Path,
) -> None:
    database = tmp_path / "tampered-readback.sqlite3"
    store = SQLiteStore(database)
    store.initialize()
    _profile, _role, session, agent = _scope(store)
    valid = store.append_context_revision(_revision(session.id, agent.id))
    role_block = valid.blocks[0]
    tampered_source = ContextSourceRef(
        source_type=ContextSourceType.SESSION,
        source_id=session.id,
        cursor=1,
        content_hash=role_block.content_hash,
    )

    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER prompt_blocks_no_update")
        connection.execute(
            "UPDATE prompt_blocks SET source_refs_json = ? WHERE id = ?",
            (
                json.dumps(
                    [tampered_source.model_dump(mode="json")],
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                role_block.id,
            ),
        )

    with pytest.raises(ConflictError, match="Prompt Block Session source is invalid"):
        store.get_context_revision(valid.id)


def test_context_revision_readback_rejects_missing_blocks_and_source_snapshot_tampering(
    tmp_path: Path,
) -> None:
    database = tmp_path / "context-readback-integrity.sqlite3"
    store = SQLiteStore(database)
    store.initialize()
    _profile, _role, session, agent = _scope(store)
    valid = store.append_context_revision(_revision(session.id, agent.id))

    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM context_revisions WHERE id = ?",
            (valid.id,),
        ).fetchone()
        assert row is not None
        columns = [
            str(column["name"])
            for column in connection.execute("PRAGMA table_info(context_revisions)").fetchall()
            if column["name"] != "sequence"
        ]
        raw_revision = {column: row[column] for column in columns}
        raw_revision.update(
            {
                "id": "context_raw_missing_required_blocks",
                "request_ordinal": 99,
            }
        )
        placeholders = ", ".join("?" for _ in columns)
        connection.execute(
            f"INSERT INTO context_revisions({', '.join(columns)}) VALUES ({placeholders})",
            tuple(raw_revision[column] for column in columns),
        )

    with pytest.raises(ConflictError, match="missing required Prompt Blocks"):
        store.get_context_revision("context_raw_missing_required_blocks")

    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER context_revisions_no_update")
        connection.execute(
            "UPDATE context_revisions SET source_snapshots_json = '[]' WHERE id = ?",
            (valid.id,),
        )

    with pytest.raises(ConflictError, match="source snapshots verification"):
        store.get_context_revision(valid.id)


def test_context_revision_store_requires_bidirectional_compaction_evidence(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "compaction-bidirectional.sqlite3")
    store.initialize()
    session, agent, thread, _items, compaction = _compaction_scope(store, tmp_path)
    valid = _revision_with_compaction(session.id, agent.id, thread.id, compaction)

    def persisted_counts() -> tuple[int, int]:
        with sqlite3.connect(store.path) as connection:
            revision_count = connection.execute(
                "SELECT COUNT(*) FROM context_revisions"
            ).fetchone()[0]
            compaction_count = connection.execute("SELECT COUNT(*) FROM compactions").fetchone()[0]
        return int(revision_count), int(compaction_count)

    assert persisted_counts() == (0, 0)

    detached_id = valid.model_copy(update={"compaction_id": None})
    with pytest.raises(ConflictError, match="Compaction source is invalid"):
        store.append_context_revision(detached_id, compaction=compaction)
    assert persisted_counts() == (0, 0)

    detached_ref = valid.model_copy(update={"compaction_refs": ()})
    with pytest.raises(ConflictError, match="compaction_refs"):
        store.append_context_revision(detached_ref, compaction=compaction)
    assert persisted_counts() == (0, 0)

    detached_block = valid.model_copy(
        update={
            "blocks": tuple(
                block
                for block in valid.blocks
                if block.block_type is not PromptBlockType.COMPACTION
            )
        }
    )
    with pytest.raises(ConflictError, match="Compaction source is invalid"):
        store.append_context_revision(detached_block, compaction=compaction)
    assert persisted_counts() == (0, 0)

    plain = _revision(session.id, agent.id, ordinal=2)
    for candidate in (
        plain.model_copy(update={"blocks": ()}),
        plain.model_copy(update={"blocks": plain.blocks[:2]}),
    ):
        with pytest.raises(ValueError):
            store.append_context_revision(candidate)
        assert persisted_counts() == (0, 0)


def test_context_revision_readback_rejects_duplicate_compaction_blocks(
    tmp_path: Path,
) -> None:
    database = tmp_path / "compaction-readback-integrity.sqlite3"
    store = SQLiteStore(database)
    store.initialize()
    session, agent, thread, _items, compaction = _compaction_scope(store, tmp_path)
    revision = _revision_with_compaction(session.id, agent.id, thread.id, compaction)
    persisted = store.append_context_revision(revision, compaction=compaction)
    content = compaction.summary.model_dump_json()
    source = ContextSourceRef(
        source_type=ContextSourceType.COMPACTION,
        source_id=compaction.id,
        content_hash=compaction.content_hash,
    )

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO prompt_blocks(
                revision_id, position, id, block_type, content, content_hash,
                source_refs_json, stable_until, visibility, token_estimate,
                cache_eligible
            ) VALUES (?, 5, ?, 'compaction', ?, ?, ?, NULL, 'role_private', 1, 0)
            """,
            (
                persisted.id,
                "ctxblock_raw_duplicate_compaction",
                content,
                hashlib.sha256(content.encode()).hexdigest(),
                json.dumps([source.model_dump(mode="json")], separators=(",", ":")),
            ),
        )

    with pytest.raises(ConflictError, match="Compaction Prompt evidence is incomplete"):
        store.get_context_revision(persisted.id)


def test_memory_source_freezes_historical_version_but_rejects_stale_writes(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "memory-source-versioning.sqlite3")
    store.initialize()
    _profile, _role, session, agent = _scope(store)
    memory = store.create_memory(
        Memory(
            kind=MemoryKind.WORKING,
            content="version one",
            source_session_id=session.id,
            status=MemoryStatus.ACTIVE,
        )
    )
    memory_ref = ContextSourceRef(
        source_type=ContextSourceType.MEMORY,
        source_id=memory.id,
        cursor=memory.version,
        content_hash=hashlib.sha256(memory.model_dump_json().encode("utf-8")).hexdigest(),
    )
    base = _revision(session.id, agent.id)
    revision = base.model_copy(
        update={
            "memory_refs": (memory.id,),
            "blocks": tuple(
                block.model_copy(update={"source_refs": (*block.source_refs, memory_ref)})
                if block.block_type is PromptBlockType.CONVERSATION
                else block
                for block in base.blocks
            ),
        }
    )
    persisted = store.append_context_revision(revision)
    updated = store.update_memory(memory.id, content="version two")
    assert updated.version == 2
    assert store.get_context_revision(persisted.id) == persisted

    stale_base = _revision(session.id, agent.id, ordinal=2)
    stale = stale_base.model_copy(
        update={
            "memory_refs": (memory.id,),
            "blocks": tuple(
                block.model_copy(update={"source_refs": (*block.source_refs, memory_ref)})
                if block.block_type is PromptBlockType.CONVERSATION
                else block
                for block in stale_base.blocks
            ),
        }
    )
    with pytest.raises(ConflictError, match="active head"):
        store.append_context_revision(stale)


def test_memory_reference_binding_store_validation_is_scope_and_snapshot_bound(
    tmp_path: Path,
) -> None:
    database = tmp_path / "memory-binding-store.sqlite3"
    store = SQLiteStore(database)
    store.initialize()
    _profile, _role, session, agent = _scope(store)
    workspace = str(tmp_path.resolve())
    memory = store.create_memory(
        Memory(
            kind=MemoryKind.WORKING,
            content="current working memory",
            source_session_id=session.id,
            status=MemoryStatus.ACTIVE,
        )
    )
    current = _revision_with_memory(session.id, agent.id, memory, workspace_ref=workspace)
    store.append_context_revision(current)

    other_session = store.create_session(_role.id)
    other_memory = store.create_memory(
        Memory(
            kind=MemoryKind.WORKING,
            content="other session memory",
            source_session_id=other_session.id,
            status=MemoryStatus.ACTIVE,
        )
    )
    role_mismatch = store.create_memory(
        Memory(
            kind=MemoryKind.WORKING,
            content="role restricted memory",
            source_session_id=session.id,
            role_scope=("role-not-current",),
            status=MemoryStatus.ACTIVE,
        )
    )
    project_role = store.create_role(
        RolePreset(
            name="Phase 1B Project Reader",
            system_prompt="Read project memory.",
            model_profile_id=_profile.id,
            memory_scope="read: [project]; write: []",
        )
    )
    project_session = store.create_session(project_role.id)
    project_agent = store.create_agent(project_session.id)
    foreign_project = store.create_memory(
        Memory(
            kind=MemoryKind.PROJECT,
            content="foreign project memory",
            project_scope=str((tmp_path / "other").resolve()),
            status=MemoryStatus.ACTIVE,
        )
    )
    updated = store.update_memory(memory.id, content="current working memory v2")
    current_v2 = _revision_with_memory(
        session.id,
        agent.id,
        updated,
        ordinal=6,
        workspace_ref=workspace,
    )

    old_head = _revision_with_memory(
        session.id,
        agent.id,
        memory,
        ordinal=2,
        workspace_ref=workspace,
    )
    bad_hash = "f" * 64
    bad_hash_source = ContextSourceRef(
        source_type=ContextSourceType.MEMORY,
        source_id=updated.id,
        cursor=updated.version,
        content_hash=bad_hash,
    )
    bad_hash_revision = _revision_with_memory(
        session.id,
        agent.id,
        updated,
        ordinal=3,
        workspace_ref=workspace,
    )
    bad_hash_blocks = tuple(
        block.model_copy(update={"source_refs": (*block.source_refs[:-1], bad_hash_source)})
        if block.block_type is PromptBlockType.CONVERSATION
        else block
        for block in bad_hash_revision.blocks
    )
    bad_hash_revision = bad_hash_revision.model_copy(
        update={
            "blocks": bad_hash_blocks,
            "reference_bindings": tuple(
                binding.model_copy(update={"source_snapshot_hash": bad_hash})
                for binding in bad_hash_revision.reference_bindings
            ),
        }
    )
    candidates = (
        (
            "other session working memory",
            _revision_with_memory(session.id, agent.id, other_memory, ordinal=4),
        ),
        (
            "role mismatch",
            _revision_with_memory(session.id, agent.id, role_mismatch, ordinal=5),
        ),
        (
            "foreign project",
            _revision_with_memory(
                project_session.id,
                project_agent.id,
                foreign_project,
                ordinal=1,
                workspace_ref=workspace,
            ),
        ),
        ("wrong version", old_head),
        ("wrong hash", bad_hash_revision),
        (
            "foreign memory_refs",
            current_v2.model_copy(update={"memory_refs": ("memory_foreign",)}),
        ),
        (
            "binding target",
            current_v2.model_copy(
                update={
                    "reference_bindings": tuple(
                        binding.model_copy(update={"resolved_target": "memory_foreign"})
                        for binding in current.reference_bindings
                    ),
                }
            ),
        ),
    )
    for _label, candidate in candidates:
        with pytest.raises(ConflictError):
            store.append_context_revision(candidate)
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT COUNT(*) FROM context_revisions").fetchone() == (1,)
            assert connection.execute("SELECT COUNT(*) FROM reference_bindings").fetchone() == (1,)


def test_memory_reference_binding_sql_trigger_and_historical_readback_are_consistent(
    tmp_path: Path,
) -> None:
    database = tmp_path / "memory-binding-sql.sqlite3"
    store = SQLiteStore(database)
    store.initialize()
    _profile, _role, session, agent = _scope(store)
    memory = store.create_memory(
        Memory(
            kind=MemoryKind.WORKING,
            content="SQL-bound memory",
            source_session_id=session.id,
            status=MemoryStatus.ACTIVE,
        )
    )
    revision = store.append_context_revision(
        _revision_with_memory(session.id, agent.id, memory, with_binding=False)
    )
    source_hash = hashlib.sha256(memory.model_dump_json().encode("utf-8")).hexdigest()
    binding = ReferenceBinding(
        revision_id=revision.id,
        position=1,
        ref_type=ContextReferenceType.MEMORY,
        resolved_target=memory.id,
        source_snapshot_hash=source_hash,
        include_mode=ReferenceIncludeMode.INLINE,
        visibility=ContextVisibility.SESSION,
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO reference_bindings(
                revision_id, position, id, ref_type, user_text,
                resolved_target, source_snapshot_hash, include_mode,
                max_tokens, visibility, resolved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                binding.revision_id,
                binding.position,
                binding.id,
                binding.ref_type.value,
                binding.user_text,
                binding.resolved_target,
                binding.source_snapshot_hash,
                binding.include_mode.value,
                binding.max_tokens,
                binding.visibility.value,
                binding.resolved_at.isoformat(),
            ),
        )
    assert store.get_context_revision(revision.id).reference_bindings == (binding,)

    with sqlite3.connect(database) as connection:
        columns = [
            str(row[1])
            for row in connection.execute("PRAGMA table_info(context_revisions)").fetchall()
        ]
        persisted_row = connection.execute(
            "SELECT * FROM context_revisions WHERE id = ?",
            (revision.id,),
        ).fetchone()
        assert persisted_row is not None
        raw_row = list(persisted_row)
        sequence_index = columns.index("sequence")
        source_snapshots_index = columns.index("source_snapshots_json")
        memory_refs_index = columns.index("memory_refs_json")
        raw_source_snapshots = json.loads(raw_row[source_snapshots_index])
        memory_source = next(
            source
            for source in raw_source_snapshots
            if source["source_type"] == ContextSourceType.MEMORY.value
        )
        extra_memory = store.create_memory(
            Memory(
                kind=MemoryKind.WORKING,
                content="second SQL-bound memory",
                source_session_id=session.id,
                status=MemoryStatus.ACTIVE,
            )
        )
        extra_source = {
            "source_type": ContextSourceType.MEMORY.value,
            "source_id": extra_memory.id,
            "cursor": extra_memory.version,
            "content_hash": hashlib.sha256(
                extra_memory.model_dump_json().encode("utf-8")
            ).hexdigest(),
        }
        insert_columns = ", ".join(columns)
        placeholders = ", ".join("?" for _ in columns)

        def raw_revision(
            revision_id: str,
            request_ordinal: int,
            memory_refs: list[str],
            source_snapshots: list[dict[str, object]],
        ) -> list[object]:
            candidate = list(raw_row)
            candidate[sequence_index] = None
            candidate[columns.index("id")] = revision_id
            candidate[columns.index("request_ordinal")] = request_ordinal
            candidate[memory_refs_index] = json.dumps(memory_refs, separators=(",", ":"))
            candidate[source_snapshots_index] = json.dumps(
                source_snapshots,
                sort_keys=True,
                separators=(",", ":"),
            )
            return candidate

        def insert_raw(candidate: list[object]) -> None:
            connection.execute(
                f"INSERT INTO context_revisions({insert_columns}) VALUES ({placeholders})",
                candidate,
            )

        for label, memory_refs, source_snapshots in (
            (
                "foreign_refs",
                ["memory_foreign"],
                raw_source_snapshots,
            ),
            (
                "foreign_both",
                ["memory_foreign"],
                [
                    *raw_source_snapshots[:-1],
                    {**memory_source, "source_id": "memory_foreign"},
                ],
            ),
            (
                "duplicate_refs",
                [memory.id, memory.id],
                raw_source_snapshots,
            ),
            (
                "missing_ref",
                [],
                raw_source_snapshots,
            ),
            (
                "extra_ref",
                [memory.id, extra_memory.id],
                raw_source_snapshots,
            ),
            (
                "duplicate_source",
                [memory.id],
                [*raw_source_snapshots, dict(memory_source)],
            ),
            (
                "wrong_order",
                [extra_memory.id, memory.id],
                [*raw_source_snapshots, extra_source],
            ),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                insert_raw(
                    raw_revision(
                        f"context_memory_refs_sql_{label}",
                        100 + len(label),
                        memory_refs,
                        source_snapshots,
                    )
                )
            assert connection.execute("SELECT COUNT(*) FROM context_revisions").fetchone() == (1,)

        valid_candidate = raw_revision(
            "context_memory_refs_sql_valid",
            200,
            [memory.id, extra_memory.id],
            [*raw_source_snapshots, extra_source],
        )
        connection.execute("SAVEPOINT valid_memory_refs")
        insert_raw(valid_candidate)
        assert connection.execute("SELECT COUNT(*) FROM context_revisions").fetchone() == (2,)
        connection.execute("ROLLBACK TO valid_memory_refs")
        connection.execute("RELEASE valid_memory_refs")
        assert connection.execute("SELECT COUNT(*) FROM context_revisions").fetchone() == (1,)

    stale_revision = store.append_context_revision(
        _revision_with_memory(session.id, agent.id, memory, ordinal=2, with_binding=False)
    )
    with sqlite3.connect(database) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="Context reference target is invalid"):
            connection.execute(
                """
                INSERT INTO reference_bindings(
                    revision_id, position, id, ref_type, user_text,
                    resolved_target, source_snapshot_hash, include_mode,
                    max_tokens, visibility, resolved_at
                ) VALUES (?, 1, ?, 'memory', NULL, ?, ?, 'inline', NULL, 'session', ?)
                """,
                (
                    stale_revision.id,
                    "refbind_wrong_target",
                    "memory_foreign",
                    source_hash,
                    binding.resolved_at.isoformat(),
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="Context reference target is invalid"):
            connection.execute(
                """
                INSERT INTO reference_bindings(
                    revision_id, position, id, ref_type, user_text,
                    resolved_target, source_snapshot_hash, include_mode,
                    max_tokens, visibility, resolved_at
                ) VALUES (?, 1, ?, 'memory', NULL, ?, ?, 'inline', NULL, 'session', ?)
                """,
                (
                    stale_revision.id,
                    "refbind_wrong_hash",
                    memory.id,
                    "f" * 64,
                    binding.resolved_at.isoformat(),
                ),
            )

    store.update_memory(memory.id, content="SQL-bound memory v2")
    with (
        sqlite3.connect(database) as connection,
        pytest.raises(sqlite3.IntegrityError, match="Context reference target is invalid"),
    ):
        connection.execute(
            """
            INSERT INTO reference_bindings(
                revision_id, position, id, ref_type, user_text,
                resolved_target, source_snapshot_hash, include_mode,
                max_tokens, visibility, resolved_at
            ) VALUES (?, 1, ?, 'memory', NULL, ?, ?, 'inline', NULL, 'session', ?)
            """,
            (
                stale_revision.id,
                "refbind_stale_head",
                memory.id,
                source_hash,
                binding.resolved_at.isoformat(),
            ),
        )
    assert store.get_context_revision(revision.id).reference_bindings == (binding,)
    assert store.get_context_revision(stale_revision.id).reference_bindings == ()

    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER context_revisions_no_update")
        connection.execute(
            "UPDATE context_revisions SET memory_refs_json = ? WHERE id = ?",
            (json.dumps(["memory_foreign"]), revision.id),
        )
    with pytest.raises(ConflictError, match="memory_refs"):
        store.get_context_revision(revision.id)


def test_memory_reference_binding_readback_rejects_binding_snapshot_tampering(
    tmp_path: Path,
) -> None:
    database = tmp_path / "memory-binding-readback.sqlite3"
    store = SQLiteStore(database)
    store.initialize()
    _profile, _role, session, agent = _scope(store)
    memory = store.create_memory(
        Memory(
            kind=MemoryKind.WORKING,
            content="readback memory",
            source_session_id=session.id,
            status=MemoryStatus.ACTIVE,
        )
    )
    revision = store.append_context_revision(_revision_with_memory(session.id, agent.id, memory))
    store.update_memory(memory.id, content="readback memory v2")
    store.deactivate_memory(memory.id)
    assert store.get_context_revision(revision.id) == revision

    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER reference_bindings_no_update")
        connection.execute(
            "UPDATE reference_bindings SET source_snapshot_hash = ? WHERE revision_id = ?",
            ("f" * 64, revision.id),
        )
    with pytest.raises(ConflictError, match="Reference Binding Memory source is invalid"):
        store.get_context_revision(revision.id)


def test_thread_source_freezes_historical_status_but_rejects_stale_writes(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "thread-source-status.sqlite3")
    store.initialize()
    _profile, _role, session, agent = _scope(store)
    workspace = str(tmp_path.resolve())
    thread = store.create_thread(ConversationThread(workspace_ref=workspace))
    assert thread.cursor is not None
    thread_ref = ContextSourceRef(
        source_type=ContextSourceType.THREAD,
        source_id=thread.id,
        cursor=thread.cursor,
        content_hash=store.context_source_body_hash(
            ContextSourceType.THREAD,
            thread.id,
            cursor=thread.cursor,
        ),
    )
    base = _revision(session.id, agent.id)
    revision = base.model_copy(
        update={
            "thread_id": thread.id,
            "workspace_ref": workspace,
            "blocks": tuple(
                block.model_copy(update={"source_refs": (*block.source_refs, thread_ref)})
                if block.block_type is PromptBlockType.CONVERSATION
                else block
                for block in base.blocks
            ),
        }
    )
    persisted = store.append_context_revision(revision)
    completed = store.set_thread_status(thread.id, ThreadStatus.COMPLETED)
    assert completed.status is ThreadStatus.COMPLETED
    assert store.get_context_revision(persisted.id) == persisted

    stale_base = _revision(session.id, agent.id, ordinal=2)
    stale = stale_base.model_copy(
        update={
            "thread_id": thread.id,
            "workspace_ref": workspace,
            "blocks": tuple(
                block.model_copy(update={"source_refs": (*block.source_refs, thread_ref)})
                if block.block_type is PromptBlockType.CONVERSATION
                else block
                for block in stale_base.blocks
            ),
        }
    )
    with pytest.raises(ConflictError, match="Thread source hash"):
        store.append_context_revision(stale)


def test_forged_reference_rolls_back_compaction_and_revision_atomically(tmp_path: Path) -> None:
    database = tmp_path / "reference-atomic.sqlite3"
    store = SQLiteStore(database)
    store.initialize()
    _profile, _role, session, agent = _scope(store)
    base = store.append_context_revision(_revision(session.id, agent.id))
    assert base.cursor is not None
    next_revision = _revision(session.id, agent.id, ordinal=2)
    summary = CompactionSummary(active_goal="Keep Canonical History.")
    summary_json = summary.model_dump_json()
    compaction = Compaction(
        session_id=session.id,
        agent_id=agent.id,
        source_type=CompactionSourceType.CONTEXT_REVISIONS,
        source_cursor_start=base.cursor,
        source_cursor_end=base.cursor,
        source_snapshot_hash=compaction_coverage_hash(summary),
        summary=summary,
        content_hash=hashlib.sha256(summary_json.encode()).hexdigest(),
    )
    binding = ReferenceBinding(
        revision_id=next_revision.id,
        position=1,
        ref_type=ContextReferenceType.ARTIFACT,
        resolved_target="artifact_missing",
        source_snapshot_hash="b" * 64,
        include_mode=ReferenceIncludeMode.METADATA,
        visibility=ContextVisibility.SESSION,
    )
    forged = next_revision.model_copy(
        update={
            "reference_bindings": (binding,),
            "compaction_id": compaction.id,
        }
    )

    with pytest.raises(ConflictError):
        store.append_context_revision(forged, compaction=compaction)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM compactions").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM context_revisions").fetchone() == (1,)


def test_compaction_rejects_nonexistent_or_future_context_revision_cursors(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "compaction-cursors.sqlite3")
    store.initialize()
    _profile, _role, session, agent = _scope(store)
    first = store.append_context_revision(_revision(session.id, agent.id, ordinal=1))
    assert first.cursor is not None
    future_agent = store.create_agent(session.id)
    future = store.append_context_revision(_revision(session.id, future_agent.id, ordinal=3))
    assert future.cursor is not None

    summary = CompactionSummary(active_goal="Only committed prior requests are covered.")
    summary_json = summary.model_dump_json()
    for cursor in (future.cursor, future.cursor + 100):
        compaction = Compaction(
            session_id=session.id,
            agent_id=future_agent.id,
            source_type=CompactionSourceType.CONTEXT_REVISIONS,
            source_cursor_start=cursor,
            source_cursor_end=cursor,
            source_snapshot_hash=compaction_coverage_hash(summary),
            summary=summary,
            content_hash=hashlib.sha256(summary_json.encode()).hexdigest(),
        )
        second = _revision(session.id, future_agent.id, ordinal=2).model_copy(
            update={"compaction_id": compaction.id}
        )
        with pytest.raises(ConflictError):
            store.append_context_revision(second, compaction=compaction)


def test_nonempty_phase1b_tables_refuse_rollback(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "rollback.sqlite3")
    store.initialize()
    _profile, _role, session, agent = _scope(store)
    store.append_context_revision(_revision(session.id, agent.id))

    with pytest.raises(MigrationError, match="Phase 1B"):
        store.rollback(5, isolated=True)
