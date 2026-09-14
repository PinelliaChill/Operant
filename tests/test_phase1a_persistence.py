from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from operant.domain.actions import ApprovalRequest, ToolActionReceipt
from operant.domain.models import Event, ModelProfile, RolePreset
from operant.domain.threads import (
    AgentMessagePayload,
    ApprovalLinkPayload,
    Artifact,
    ArtifactRefPayload,
    ArtifactSensitivity,
    ArtifactSourceRef,
    ArtifactSourceType,
    ConversationThread,
    Item,
    SteeringPayload,
    SystemEventPayload,
    ThreadLegacyRef,
    ThreadStatus,
    ToolCallPayload,
    ToolResultRefPayload,
    Turn,
    UserMessagePayload,
)
from operant.domain.workflow import WorkflowRun
from operant.persistence.sqlite import (
    ConflictError,
    MigrationError,
    NotFoundError,
    SQLiteStore,
)


def _storage_key(content_hash: str) -> str:
    return f"sha256/{content_hash[:2]}/{content_hash[2:4]}/{content_hash}"


def _artifact(
    content: bytes,
    *,
    sources: tuple[ArtifactSourceRef, ...] = (),
    sensitivity: ArtifactSensitivity = ArtifactSensitivity.NORMAL,
) -> Artifact:
    return Artifact(
        content_hash=hashlib.sha256(content).hexdigest(),
        media_type="text/plain; charset=utf-8",
        size_bytes=len(content),
        sensitivity=sensitivity,
        source_refs=sources,
        retention_policy_ref="retention/project-default",
    )


def _initialized_store(path: Path) -> SQLiteStore:
    store = SQLiteStore(path)
    store.initialize()
    return store


def _seed_action_scope(store: SQLiteStore):
    profile = store.add_model_profile(
        ModelProfile(
            name="phase1a-model",
            model_id="phase1a-model",
            base_url="https://example.invalid/v1",
            secret_ref="OPERANT_PHASE1A_TEST_KEY",
        )
    )
    role = store.create_role(
        RolePreset(
            name="Phase 1A Coder",
            system_prompt="Test canonical history.",
            model_profile_id=profile.id,
        )
    )
    session = store.create_session(role.id)
    agent = store.create_agent(session.id)
    receipt, claimed = store.reserve_tool_action(
        ToolActionReceipt(
            scope="phase1a",
            session_id=session.id,
            agent_id=agent.id,
            idempotency_key="tool-call-approval",
            action_hash="a" * 64,
            command_name="run_command",
        )
    )
    assert claimed
    approval = store.create_approval_request(
        ApprovalRequest(
            session_id=session.id,
            agent_id=agent.id,
            tool_action_receipt_id=receipt.id,
            tool_call_id="tool-call-approval",
            action_hash="a" * 64,
            category="execute",
            detail_summary="safe test command",
        )
    )
    return session, agent, receipt, approval


@pytest.mark.parametrize("starting_version", [1, 2, 3, 4])
def test_v1_through_v4_upgrade_to_v5_preserves_legacy_rows(
    tmp_path: Path,
    starting_version: int,
) -> None:
    database = tmp_path / f"from-v{starting_version}.sqlite3"
    store = SQLiteStore(database)
    assert store.migrate(starting_version) == starting_version
    profile = store.add_model_profile(
        ModelProfile(
            id=f"model-from-v{starting_version}",
            name=f"from-v{starting_version}",
            model_id="legacy-model",
            base_url="https://example.invalid/v1",
            secret_ref="OPERANT_LEGACY_TEST_KEY",
        )
    )

    store.initialize()
    store.initialize()

    assert store.schema_version() == 15
    assert store.get_model_profile(profile.id) == profile
    assert store.list_threads() == []
    assert store.list_artifacts() == []


def test_v5_migration_failure_is_atomic_and_empty_rollback_is_explicit(
    tmp_path: Path,
) -> None:
    database = tmp_path / "atomic.sqlite3"
    SQLiteStore(database).migrate(4)

    class BrokenV5Store(SQLiteStore):
        def _upgrade_v5(self, connection: sqlite3.Connection) -> None:
            super()._upgrade_v5(connection)
            connection.execute("CREATE TABLE injected_failure_marker(id TEXT)")
            raise RuntimeError("injected v5 failure")

    with pytest.raises(RuntimeError, match="injected v5 failure"):
        BrokenV5Store(database).migrate()
    assert SQLiteStore(database).schema_version() == 4
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'threads'").fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'injected_failure_marker'"
            ).fetchone()
            is None
        )

    empty = SQLiteStore(tmp_path / "empty-rollback.sqlite3")
    empty.initialize()
    assert empty.rollback(4, isolated=True) == 4
    assert empty.migrate() == 15


def test_exact_phase1a_preview_adds_reference_guards_and_updates_checksum(
    tmp_path: Path,
) -> None:
    database = tmp_path / "phase1a-preview.sqlite3"
    SQLiteStore(database).migrate(5)
    with sqlite3.connect(database) as connection:
        for trigger in (
            "artifact_source_refs_insert_guard",
            "items_tool_call_unique_guard",
            "items_tool_result_call_guard",
            "thread_legacy_refs_insert_guard",
        ):
            connection.execute(f'DROP TRIGGER "{trigger}"')
        connection.execute(
            "UPDATE schema_migrations SET checksum = ? WHERE version = 5",
            (SQLiteStore._PHASE1A_V5_PREVIEW_HISTORY[-1][2],),
        )

    restarted = SQLiteStore(database)
    restarted.initialize()
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT checksum FROM schema_migrations WHERE version = 5"
        ).fetchone()
        triggers = {
            str(entry[0])
            for entry in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
        }
    assert row == (SQLiteStore._FROZEN_MIGRATION_CHECKSUMS[5],)
    assert {
        "artifact_source_refs_insert_guard",
        "items_tool_call_unique_guard",
        "items_tool_result_call_guard",
        "thread_legacy_refs_insert_guard",
    }.issubset(triggers)


def test_v4_session_workflow_event_receipt_and_approval_remain_readable(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "preserve-v4.sqlite3")
    store.migrate(4)
    session, agent, receipt, approval = _seed_action_scope(store)
    event = store.append_event(
        Event(
            session_id=session.id,
            agent_id=agent.id,
            event_type="legacy.event",
            payload={"safe": True},
        )
    )
    workflow = store.create_workflow_run(
        WorkflowRun(
            task="Preserve the v4 workflow",
            workspace=str(tmp_path),
            planner_role_id="role-planner",
            coder_role_id="role-coder",
            reviewer_role_id="role-reviewer",
        )
    )

    assert store.migrate() == 15

    assert store.get_session(session.id) == session
    assert store.get_agent(agent.id) == agent
    assert store.list_events(session.id) == [event]
    assert store.get_workflow_run(workflow.id) == workflow
    assert store.get_tool_action_receipt(receipt.id) == receipt
    assert store.get_approval_request(session.id, approval.tool_call_id) == approval


def test_thread_parent_workspace_legacy_mapping_and_no_automatic_backfill(
    tmp_path: Path,
) -> None:
    store = _initialized_store(tmp_path / "threads.sqlite3")
    session, _agent, _receipt, _approval = _seed_action_scope(store)
    assert store.list_threads() == []

    parent = store.create_thread(ConversationThread(workspace_ref=str(tmp_path / "workspace")))
    child = store.create_thread(
        ConversationThread(
            parent_thread_id=parent.id,
            workspace_ref=parent.workspace_ref,
            legacy_refs=(ThreadLegacyRef(source_type="session", source_id=session.id),),
        )
    )

    assert child.cursor is not None and child.cursor > parent.cursor
    assert store.get_thread(child.id) == child
    assert store.get_thread_by_legacy_ref(child.legacy_refs[0]) == child
    assert store.list_threads(parent_thread_id=parent.id) == [child]
    assert store.list_threads(workspace_ref=parent.workspace_ref) == [parent, child]
    with pytest.raises(ConflictError):
        store.create_thread(
            ConversationThread(
                legacy_refs=(ThreadLegacyRef(source_type="session", source_id=session.id),)
            )
        )
    with pytest.raises(NotFoundError):
        store.create_thread(
            ConversationThread(
                legacy_refs=(ThreadLegacyRef(source_type="workflow_run", source_id="missing"),)
            )
        )


def test_turn_item_history_is_ordered_append_only_and_cursor_paginated(
    tmp_path: Path,
) -> None:
    store = _initialized_store(tmp_path / "history.sqlite3")
    thread = store.create_thread(ConversationThread(workspace_ref="workspace-a"))
    other = store.create_thread(ConversationThread(workspace_ref="workspace-b"))
    first_turn = store.create_turn(Turn(thread_id=thread.id))
    store.create_turn(Turn(thread_id=other.id))
    second_turn = store.create_turn(Turn(thread_id=thread.id))
    assert [turn.position for turn in store.list_turns(thread.id)] == [1, 2]
    assert store.list_turns(thread.id, after_cursor=first_turn.cursor) == [second_turn]

    first = store.append_item(
        Item(
            thread_id=thread.id,
            turn_id=first_turn.id,
            payload=UserMessagePayload(text="first"),
        )
    )
    store.append_item(
        Item(
            thread_id=other.id,
            turn_id=store.list_turns(other.id)[0].id,
            payload=SystemEventPayload(event_type="other", summary="other thread"),
        )
    )
    second = store.append_item(
        Item(
            thread_id=thread.id,
            turn_id=second_turn.id,
            payload=AgentMessagePayload(text="second", agent_id="agent-canonical"),
        )
    )
    assert [item.position for item in store.list_items(thread.id)] == [1, 2]
    assert store.list_items(thread.id, after_cursor=first.cursor, limit=1) == [second]
    assert store.get_item(first.id) == first

    with sqlite3.connect(store.path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE items SET body = '{}' WHERE id = ?", (first.id,))
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM turns WHERE id = ?", (first_turn.id,))


def test_all_item_kinds_and_durable_references_are_validated(tmp_path: Path) -> None:
    store = _initialized_store(tmp_path / "item-types.sqlite3")
    _session, _agent, _receipt, approval = _seed_action_scope(store)
    thread = store.create_thread(ConversationThread())
    turn = store.create_turn(Turn(thread_id=thread.id))
    artifact = _artifact(
        b"tool output",
        sources=(ArtifactSourceRef(source_type="thread", source_id=thread.id),),
    )
    artifact, _created = store.register_artifact(
        artifact,
        storage_key=_storage_key(artifact.content_hash),
    )
    with pytest.raises(NotFoundError, match="tool call reference"):
        store.append_item(
            Item(
                thread_id=thread.id,
                turn_id=turn.id,
                payload=ToolResultRefPayload(
                    tool_call_id="tool-missing",
                    artifact_id=artifact.id,
                    outcome="completed",
                ),
            )
        )
    payloads = (
        UserMessagePayload(text="user"),
        AgentMessagePayload(text="agent", agent_id="agent-1"),
        ToolCallPayload(tool_call_id="tool-1", tool_name="read_file"),
        ToolResultRefPayload(
            tool_call_id="tool-1",
            artifact_id=artifact.id,
            outcome="completed",
        ),
        ArtifactRefPayload(artifact_id=artifact.id),
        ApprovalLinkPayload(approval_id=approval.id),
        SteeringPayload(text="focus on the boundary"),
        SystemEventPayload(event_type="history.checkpoint", summary="saved"),
    )
    appended = [
        store.append_item(Item(thread_id=thread.id, turn_id=turn.id, payload=payload))
        for payload in payloads
    ]
    assert [item.item_type.value for item in appended] == [payload.type for payload in payloads]
    with pytest.raises(ConflictError, match="tool call already exists"):
        store.append_item(
            Item(
                thread_id=thread.id,
                turn_id=turn.id,
                payload=ToolCallPayload(tool_call_id="tool-1", tool_name="duplicate"),
            )
        )
    with pytest.raises(NotFoundError):
        store.append_item(
            Item(
                thread_id=thread.id,
                turn_id=turn.id,
                payload=ArtifactRefPayload(artifact_id="artifact-missing"),
            )
        )
    with pytest.raises(NotFoundError):
        store.append_item(
            Item(
                thread_id=thread.id,
                turn_id=turn.id,
                payload=ApprovalLinkPayload(approval_id="approval-missing"),
            )
        )


def test_terminal_thread_fences_new_history_and_status_cannot_rewind(tmp_path: Path) -> None:
    store = _initialized_store(tmp_path / "terminal.sqlite3")
    thread = store.create_thread(ConversationThread())
    turn = store.create_turn(Turn(thread_id=thread.id))
    completed = store.set_thread_status(thread.id, ThreadStatus.COMPLETED)
    assert completed.status is ThreadStatus.COMPLETED
    with pytest.raises(ConflictError, match="not active"):
        store.create_turn(Turn(thread_id=thread.id))
    with pytest.raises(ConflictError, match="not active"):
        store.append_item(
            Item(
                thread_id=thread.id,
                turn_id=turn.id,
                payload=UserMessagePayload(text="too late"),
            )
        )
    with pytest.raises(ConflictError, match="invalid thread status"):
        store.set_thread_status(thread.id, ThreadStatus.ACTIVE)
    archived = store.archive_thread(thread.id)
    assert archived.status is ThreadStatus.ARCHIVED
    assert archived.archived_at is not None
    assert store.archive_thread(thread.id) == archived

    with sqlite3.connect(store.path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="invalid thread status"):
            connection.execute("UPDATE threads SET status = 'active' WHERE id = ?", (thread.id,))
        with pytest.raises(sqlite3.IntegrityError, match="body metadata mismatch"):
            connection.execute("UPDATE threads SET body = '{}' WHERE id = ?", (thread.id,))


def test_concurrent_turn_and_item_positions_are_unique_and_monotonic(tmp_path: Path) -> None:
    store = _initialized_store(tmp_path / "concurrent-history.sqlite3")
    thread = store.create_thread(ConversationThread())

    def create_turn(index: int) -> Turn:
        return store.create_turn(Turn(id=f"turn-concurrent-{index}", thread_id=thread.id))

    with ThreadPoolExecutor(max_workers=8) as executor:
        turns = list(executor.map(create_turn, range(24)))
    listed_turns = store.list_turns(thread.id, limit=100)
    assert [turn.position for turn in listed_turns] == list(range(1, 25))
    assert len({turn.cursor for turn in turns}) == 24

    target_turn = listed_turns[0]

    def create_item(index: int) -> Item:
        return store.append_item(
            Item(
                id=f"item-concurrent-{index}",
                thread_id=thread.id,
                turn_id=target_turn.id,
                payload=UserMessagePayload(text=f"message {index}"),
            )
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        items = list(executor.map(create_item, range(32)))
    listed_items = store.list_items(thread.id, limit=100)
    assert [item.position for item in listed_items] == list(range(1, 33))
    assert len({item.cursor for item in items}) == 32


def test_artifact_metadata_is_atomic_deduplicated_and_path_free(tmp_path: Path) -> None:
    store = _initialized_store(tmp_path / "artifacts.sqlite3")
    thread = store.create_thread(ConversationThread())
    source = ArtifactSourceRef(source_type=ArtifactSourceType.THREAD, source_id=thread.id)
    requested = _artifact(b"same content", sources=(source,))
    storage_key = _storage_key(requested.content_hash)

    created, was_created = store.register_artifact(requested, storage_key=storage_key)
    assert was_created
    duplicate, was_created = store.register_artifact(
        _artifact(b"same content", sources=(source,)),
        storage_key=storage_key,
    )
    assert not was_created
    assert duplicate == created
    assert store.get_artifact(created.id) == created
    assert store.get_artifact_by_hash(created.content_hash) == created
    assert "storage_key" not in created.model_dump()
    assert "storage_key" not in json.loads(created.model_dump_json())

    with pytest.raises(ConflictError, match="different metadata"):
        store.register_artifact(
            _artifact(
                b"same content",
                sources=(source,),
                sensitivity=ArtifactSensitivity.RESTRICTED,
            ),
            storage_key=storage_key,
        )
    with pytest.raises(ValueError, match="storage key"):
        store.register_artifact(requested, storage_key="../../outside")
    with pytest.raises(NotFoundError, match="source reference"):
        missing_source = _artifact(
            b"another",
            sources=(ArtifactSourceRef(source_type="item", source_id="missing"),),
        )
        store.register_artifact(
            missing_source,
            storage_key=_storage_key(missing_source.content_hash),
        )


def test_direct_sql_rejects_forged_tool_results_and_source_refs(tmp_path: Path) -> None:
    store = _initialized_store(tmp_path / "direct-reference-guards.sqlite3")
    thread = store.create_thread(ConversationThread())
    turn = store.create_turn(Turn(thread_id=thread.id))
    artifact = _artifact(b"guarded output")
    artifact, _created = store.register_artifact(
        artifact,
        storage_key=_storage_key(artifact.content_hash),
    )

    forged_result = Item(
        id="item-forged-result",
        thread_id=thread.id,
        turn_id=turn.id,
        position=1,
        payload=ToolResultRefPayload(
            tool_call_id="tool-bogus",
            artifact_id=artifact.id,
            outcome="completed",
        ),
    )
    with sqlite3.connect(store.path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="tool call reference"):
            connection.execute(
                """
                INSERT INTO items(id, thread_id, turn_id, position, item_type, body, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    forged_result.id,
                    forged_result.thread_id,
                    forged_result.turn_id,
                    forged_result.position,
                    forged_result.item_type.value,
                    forged_result.model_dump_json(),
                    forged_result.created_at.isoformat(),
                ),
            )

        with pytest.raises(sqlite3.IntegrityError, match="artifact source reference"):
            connection.execute(
                """
                INSERT INTO artifact_source_refs(artifact_id, ordinal, source_type, source_id)
                VALUES (?, 1, 'thread', 'thread-missing')
                """,
                (artifact.id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="artifact source reference"):
            connection.execute(
                """
                INSERT INTO artifact_source_refs(artifact_id, ordinal, source_type, source_id)
                VALUES (?, 1, 'tool_call', 'tool-bogus')
                """,
                (artifact.id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="legacy source"):
            connection.execute(
                """
                INSERT INTO thread_legacy_refs(thread_id, source_type, source_id, created_at)
                VALUES (?, 'session', 'session-missing', '2026-08-28T00:00:00+00:00')
                """,
                (thread.id,),
            )

    tool_call = store.append_item(
        Item(
            thread_id=thread.id,
            turn_id=turn.id,
            payload=ToolCallPayload(tool_call_id="tool-real", tool_name="read_file"),
        )
    )
    duplicate_call = Item(
        id="item-duplicate-tool-call",
        thread_id=thread.id,
        turn_id=turn.id,
        position=2,
        payload=ToolCallPayload(tool_call_id="tool-real", tool_name="read_file"),
    )
    with (
        sqlite3.connect(store.path) as connection,
        pytest.raises(sqlite3.IntegrityError, match="duplicate tool call"),
    ):
        connection.execute(
            """
            INSERT INTO items(id, thread_id, turn_id, position, item_type, body, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                duplicate_call.id,
                duplicate_call.thread_id,
                duplicate_call.turn_id,
                duplicate_call.position,
                duplicate_call.item_type.value,
                duplicate_call.model_dump_json(),
                duplicate_call.created_at.isoformat(),
            ),
        )
    assert tool_call.position == 1


def test_concurrent_artifact_registration_creates_one_logical_record(tmp_path: Path) -> None:
    store = _initialized_store(tmp_path / "artifact-concurrency.sqlite3")
    content = b"deduplicate me"
    content_hash = hashlib.sha256(content).hexdigest()
    key = _storage_key(content_hash)

    def register(index: int) -> tuple[Artifact, bool]:
        return store.register_artifact(
            Artifact(
                id=f"artifact-concurrent-{index}",
                content_hash=content_hash,
                media_type="application/octet-stream",
                size_bytes=len(content),
                retention_policy_ref="retention/default",
            ),
            storage_key=key,
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(register, range(20)))
    assert sum(created for _artifact_result, created in results) == 1
    assert len({artifact.id for artifact, _created in results}) == 1
    assert len(store.list_artifacts()) == 1


def test_v5_manifest_rejects_trigger_and_table_drift(tmp_path: Path) -> None:
    trigger_drift = _initialized_store(tmp_path / "trigger-drift.sqlite3")
    with sqlite3.connect(trigger_drift.path) as connection:
        connection.execute("DROP TRIGGER items_no_update")
    with pytest.raises(MigrationError, match="managed objects"):
        trigger_drift.schema_version()

    extra_table = _initialized_store(tmp_path / "extra-table.sqlite3")
    with sqlite3.connect(extra_table.path) as connection:
        connection.execute("CREATE TABLE unexpected_phase1a_table(id TEXT)")
    with pytest.raises(MigrationError, match="managed objects"):
        extra_table.schema_version()
