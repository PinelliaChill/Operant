from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from operant.application.context import PersistentContextComposer
from operant.domain.actions import CommandExecution
from operant.domain.commands import (
    BTWSidecarEvent,
    BTWSidecarRun,
    BTWSidecarStatus,
    ContextBaseline,
    ContextBaselineOperation,
    Phase1DCommandAuditEvent,
    ReviewRun,
    ReviewRunStatus,
    SlashCommandKind,
    WorkspaceInitialization,
)
from operant.domain.context import (
    Compaction,
    CompactionSourceType,
    CompactionSummary,
    ContextSourceRef,
    ContextSourceType,
    deterministic_compaction_id,
)
from operant.domain.messages import Message, MessageRole
from operant.domain.models import Budget, ModelProfile, RolePreset, utc_now
from operant.domain.threads import (
    ConversationThread,
    Item,
    SteeringPayload,
    Turn,
    UserMessagePayload,
)
from operant.persistence.sqlite import ConflictError, MigrationError, SQLiteStore

PHASE1D_WORKSPACE_REF = str(Path("/tmp/phase1d-workspace").resolve())


def _scope(store: SQLiteStore):
    profile = store.add_model_profile(
        ModelProfile(
            name="phase1d-model",
            model_id="phase1d-model",
            base_url="https://example.invalid/v1",
            secret_ref="OPERANT_PHASE1D_TEST_KEY",
            context_window=16_384,
        )
    )
    role = store.create_role(
        RolePreset(
            name="Phase 1D",
            system_prompt="Operate safely.",
            model_profile_id=profile.id,
            budget=Budget(max_output_tokens=1_024),
        )
    )
    session = store.create_session(role.id)
    agent = store.create_agent(session.id)
    thread = store.create_thread(ConversationThread(workspace_ref=PHASE1D_WORKSPACE_REF))
    return session, agent, thread


def _append_message(store: SQLiteStore, thread: ConversationThread, text: str) -> Item:
    turn = store.create_turn(Turn(thread_id=thread.id))
    return store.append_item(
        Item(
            thread_id=thread.id,
            turn_id=turn.id,
            payload=UserMessagePayload(text=text),
        )
    )


def _thread_compaction(session_id: str, agent_id: str, thread_id: str, items: list[Item]):
    refs = tuple(
        ContextSourceRef(
            source_type=ContextSourceType.ITEM,
            source_id=item.id,
            cursor=item.cursor,
            content_hash=hashlib.sha256(
                item.model_copy(update={"cursor": None}).model_dump_json().encode()
            ).hexdigest(),
        )
        for item in items
    )
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
            ).encode()
        )
        digest.update(b"\n")
    summary = CompactionSummary(
        active_goal="Continue safely.",
        completed_steps=(f"Covered {len(items)} canonical Items.",),
    )
    compaction = Compaction(
        session_id=session_id,
        agent_id=agent_id,
        thread_id=thread_id,
        source_type=CompactionSourceType.THREAD_ITEMS,
        source_cursor_start=refs[0].cursor or 1,
        source_cursor_end=refs[-1].cursor or 1,
        source_snapshot_hash=digest.hexdigest(),
        summary=summary,
        content_hash=hashlib.sha256(summary.model_dump_json().encode()).hexdigest(),
        covered_item_refs=refs,
    )
    return compaction.model_copy(update={"id": deterministic_compaction_id(compaction)})


@pytest.mark.parametrize("starting_version", range(1, 8))
def test_v1_through_v7_upgrade_to_v8_and_repeat_initialize(
    tmp_path: Path,
    starting_version: int,
) -> None:
    store = SQLiteStore(tmp_path / f"v{starting_version}.sqlite3")
    assert store.migrate(starting_version) == starting_version
    store.initialize()
    store.initialize()
    assert store.schema_version() == 13


def test_v8_failure_is_atomic_and_rollback_requires_empty_phase1d_tables(
    tmp_path: Path,
) -> None:
    database = tmp_path / "atomic.sqlite3"
    SQLiteStore(database).migrate(7)

    class BrokenV8Store(SQLiteStore):
        def _upgrade_v8(self, connection: sqlite3.Connection) -> None:
            super()._upgrade_v8(connection)
            connection.execute("CREATE TABLE injected_phase1d_failure(id TEXT)")
            raise RuntimeError("injected v8 failure")

    with pytest.raises(RuntimeError, match="injected v8 failure"):
        BrokenV8Store(database).migrate()
    assert SQLiteStore(database).schema_version() == 7
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'context_baselines'"
            ).fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'injected_phase1d_failure'"
            ).fetchone()
            is None
        )

    empty = SQLiteStore(tmp_path / "empty-rollback.sqlite3")
    empty.initialize()
    _v8_objects, v8_ddl = empty._canonical_schema_objects(8)
    _v7_objects, v7_ddl = empty._canonical_schema_objects(7)
    assert empty.rollback(7, isolated=True) == 7
    with empty._connect() as connection:
        rolled_back_ddl = {
            row["name"]: empty._normalize_schema_sql(row["sql"])
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
            if row["sql"] is not None
        }
    assert (
        rolled_back_ddl["context_revisions_scope_guard"] == v7_ddl["context_revisions_scope_guard"]
    )
    assert (
        rolled_back_ddl["prompt_blocks_source_refs_guard"]
        == v7_ddl["prompt_blocks_source_refs_guard"]
    )
    assert empty.migrate() == 13
    with empty._connect() as connection:
        upgraded_ddl = {
            row["name"]: empty._normalize_schema_sql(row["sql"])
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
            if row["sql"] is not None
        }
    assert upgraded_ddl["context_revisions_scope_guard"] == v8_ddl["context_revisions_scope_guard"]
    assert (
        upgraded_ddl["prompt_blocks_source_refs_guard"] == v8_ddl["prompt_blocks_source_refs_guard"]
    )

    populated = SQLiteStore(tmp_path / "populated-rollback.sqlite3")
    populated.initialize()
    populated.register_workspace(
        WorkspaceInitialization(
            workspace_ref="/tmp/populated",
            workspace_hash=hashlib.sha256(b"/tmp/populated").hexdigest(),
            readable=True,
            writable=True,
        )
    )
    with pytest.raises(MigrationError, match="contain data"):
        populated.rollback(7, isolated=True)


@pytest.mark.parametrize(
    ("preview_history", "strict_context_guard"),
    (
        (SQLiteStore._PHASE1D_V8_PREVIEW_HISTORY, True),
        (SQLiteStore._PHASE1D_V8_CONTEXT_GUARD_PREVIEW_HISTORY, False),
        (SQLiteStore._PHASE1D_V8_RUN_SCOPE_PREVIEW_HISTORY, False),
    ),
)
def test_exact_phase1d_previews_upgrade_both_context_provenance_guards(
    tmp_path: Path,
    preview_history: tuple[tuple[int, str, str], ...],
    strict_context_guard: bool,
) -> None:
    v7 = SQLiteStore(tmp_path / "v7-trigger-source.sqlite3")
    v7.migrate(7)
    with v7._connect() as connection:
        strict_context_sql = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'trigger' AND name = 'context_revisions_scope_guard'"
        ).fetchone()["sql"]

    database = tmp_path / f"preview-{int(strict_context_guard)}.sqlite3"
    preview = SQLiteStore(database)
    preview.migrate(target_version=8)
    with preview._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        preview._replace_phase1d_prompt_compaction_agent_guard(
            connection,
            allow_thread_items_cross_agent=False,
        )
        if strict_context_guard:
            connection.execute("DROP TRIGGER context_revisions_scope_guard")
            connection.execute(strict_context_sql)
        for trigger in (
            "review_runs_scope_guard",
            "review_runs_identity_guard",
            "btw_sidecar_runs_scope_guard",
            "btw_sidecar_runs_identity_guard",
        ):
            connection.execute(f'DROP TRIGGER "{trigger}"')
        for version, name, checksum in preview_history:
            connection.execute(
                "UPDATE schema_migrations SET name = ?, checksum = ? WHERE version = ?",
                (name, checksum, version),
            )

    upgraded = SQLiteStore(database)
    upgraded.initialize()
    assert upgraded.schema_version() == 13
    applied_v8 = next(item for item in upgraded.list_applied_migrations() if item["version"] == 8)
    assert applied_v8["checksum"] == SQLiteStore._FROZEN_MIGRATION_CHECKSUMS[8]
    _objects, expected = upgraded._canonical_schema_objects(8)
    with upgraded._connect() as connection:
        actual = {
            row["name"]: upgraded._normalize_schema_sql(row["sql"])
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
            if row["sql"] is not None
        }
    assert actual["context_revisions_scope_guard"] == expected["context_revisions_scope_guard"]
    assert actual["prompt_blocks_source_refs_guard"] == expected["prompt_blocks_source_refs_guard"]


def test_workspace_registration_is_immutable_and_cursor_paged(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "workspace.sqlite3")
    store.initialize()
    first = WorkspaceInitialization(
        workspace_ref="/tmp/workspace-one",
        workspace_hash=hashlib.sha256(b"/tmp/workspace-one").hexdigest(),
        readable=True,
        writable=False,
    )
    stored, created = store.register_workspace(first)
    replay, replay_created = store.register_workspace(first.model_copy(update={"id": "other"}))
    assert created is True and replay_created is False
    assert replay == stored
    assert store.get_workspace_initialization(first.workspace_hash) == stored
    assert store.list_workspace_initializations(after_cursor=stored.cursor) == []
    with pytest.raises(ConflictError):
        store.register_workspace(first.model_copy(update={"writable": True}))


def test_clear_and_compact_baselines_are_append_only_and_atomic(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "baseline.sqlite3")
    store.initialize()
    session, agent, thread = _scope(store)
    zero = store.append_context_baseline(
        ContextBaseline(
            session_id=session.id,
            thread_id=thread.id,
            item_cursor_end=0,
            operation=ContextBaselineOperation.CLEAR,
        )
    )
    items = [_append_message(store, thread, f"message-{index}") for index in range(3)]
    compaction = _thread_compaction(session.id, agent.id, thread.id, items)
    compacted = store.append_context_baseline(
        ContextBaseline(
            session_id=session.id,
            thread_id=thread.id,
            item_cursor_end=items[-1].cursor or 0,
            operation=ContextBaselineOperation.COMPACT,
            compaction_id=compaction.id,
            previous_baseline_id=zero.id,
        ),
        compaction=compaction,
    )
    assert compacted.previous_baseline_id == zero.id
    assert store.get_active_context_baseline(session.id, thread.id) == compacted
    assert store.get_compaction(compaction.id).covered_item_refs == compaction.covered_item_refs
    assert [item.id for item in store.list_items(thread.id)] == [item.id for item in items]


def test_concurrent_clear_baselines_form_one_serial_predecessor_chain(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "baseline-concurrent.sqlite3")
    store.initialize()
    session, _agent, thread = _scope(store)

    def append(index: int) -> ContextBaseline:
        return store.append_context_baseline(
            ContextBaseline(
                id=f"baseline-{index}",
                session_id=session.id,
                thread_id=thread.id,
                item_cursor_end=0,
                operation=ContextBaselineOperation.CLEAR,
            )
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(append, range(8)))
    persisted = store.list_context_baselines(session.id, thread.id)
    assert len(results) == len(persisted) == 8
    assert persisted[0].previous_baseline_id is None
    assert [row.previous_baseline_id for row in persisted[1:]] == [row.id for row in persisted[:-1]]


def test_review_and_sidecar_running_rows_fail_closed_after_restart(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "restart.sqlite3")
    store.initialize()
    session, agent, thread = _scope(store)
    review = store.create_review_run(
        ReviewRun(
            session_id=session.id,
            thread_id=thread.id,
            workspace_ref=PHASE1D_WORKSPACE_REF,
        )
    )
    sidecar = store.create_btw_sidecar_run(
        BTWSidecarRun(
            session_id=session.id,
            agent_id=agent.id,
            thread_id=thread.id,
            workspace_ref=PHASE1D_WORKSPACE_REF,
            source_item_cursor_end=0,
            prompt="Explain this state.",
            prompt_hash=hashlib.sha256(b"Explain this state.").hexdigest(),
        )
    )
    store.initialize()
    assert store.get_review_run(review.id).status is ReviewRunStatus.FAILED
    assert store.get_review_run(review.id).error_code == "process_interrupted"
    assert store.get_btw_sidecar_run(sidecar.id).status is BTWSidecarStatus.FAILED
    events = store.list_btw_sidecar_events(sidecar.id)
    assert [event.event_type for event in events] == ["btw.failed"]


def test_review_and_sidecar_scope_is_rejected_by_store_and_sqlite(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "run-scope.sqlite3")
    store.initialize()
    session, agent, thread = _scope(store)
    other_session = store.create_session(session.role_snapshot.role_id)
    other_agent = store.create_agent(other_session.id)
    other_thread = store.create_thread(
        ConversationThread(workspace_ref="/tmp/phase1d-other-workspace")
    )
    now = utc_now().isoformat()

    with pytest.raises(ConflictError, match="workspace scope"):
        store.create_review_run(
            ReviewRun(
                session_id=session.id,
                thread_id=other_thread.id,
                workspace_ref=thread.workspace_ref or "",
            )
        )
    with pytest.raises(ConflictError, match="Agent session"):
        store.create_btw_sidecar_run(
            BTWSidecarRun(
                session_id=session.id,
                agent_id=other_agent.id,
                thread_id=thread.id,
                workspace_ref=thread.workspace_ref or "",
                source_item_cursor_end=0,
                prompt="scope",
                prompt_hash=hashlib.sha256(b"scope").hexdigest(),
            )
        )
    with pytest.raises(ConflictError, match="Thread workspace"):
        store.create_btw_sidecar_run(
            BTWSidecarRun(
                session_id=other_session.id,
                agent_id=other_agent.id,
                thread_id=other_thread.id,
                workspace_ref=thread.workspace_ref or "",
                source_item_cursor_end=0,
                prompt="scope",
                prompt_hash=hashlib.sha256(b"scope").hexdigest(),
            )
        )

    with (
        store._connect() as connection,
        pytest.raises(
            sqlite3.IntegrityError,
            match="Review run scope",
        ),
    ):
        connection.execute(
            """
            INSERT INTO review_runs(
                id, session_id, thread_id, workspace_ref, scope, status,
                artifact_id, error_code, created_at, updated_at
            ) VALUES ('bad-review', ?, ?, ?, 'scope', 'running', NULL, NULL, ?, ?)
            """,
            (session.id, other_thread.id, thread.workspace_ref, now, now),
        )
    with (
        store._connect() as connection,
        pytest.raises(
            sqlite3.IntegrityError,
            match="BTW Sidecar run scope",
        ),
    ):
        connection.execute(
            """
            INSERT INTO btw_sidecar_runs(
                id, session_id, agent_id, thread_id, workspace_ref,
                source_item_cursor_end, prompt, prompt_hash, status, response,
                response_hash, context_revision_id, promoted_turn_id,
                promoted_item_id, error_code, created_at, updated_at
            ) VALUES (
                'bad-sidecar', ?, ?, ?, ?, 0, 'scope', ?, 'running', NULL,
                NULL, NULL, NULL, NULL, NULL, ?, ?
            )
            """,
            (
                session.id,
                other_agent.id,
                thread.id,
                thread.workspace_ref,
                hashlib.sha256(b"scope").hexdigest(),
                now,
                now,
            ),
        )

    valid_review = store.create_review_run(
        ReviewRun(
            session_id=session.id,
            thread_id=thread.id,
            workspace_ref=thread.workspace_ref or "",
        )
    )
    valid_sidecar = store.create_btw_sidecar_run(
        BTWSidecarRun(
            session_id=session.id,
            agent_id=agent.id,
            thread_id=thread.id,
            workspace_ref=thread.workspace_ref or "",
            source_item_cursor_end=0,
            prompt="valid",
            prompt_hash=hashlib.sha256(b"valid").hexdigest(),
        )
    )
    with store._connect() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="identity is immutable"):
            connection.execute(
                "UPDATE review_runs SET workspace_ref = '/tmp/other' WHERE id = ?",
                (valid_review.id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="identity is immutable"):
            connection.execute(
                "UPDATE btw_sidecar_runs SET session_id = ? WHERE id = ?",
                (other_session.id, valid_sidecar.id),
            )


def test_completed_sidecar_promotes_exactly_one_steering_item(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "promotion.sqlite3")
    store.initialize()
    session, agent, thread = _scope(store)
    source = _append_message(store, thread, "canonical source")
    composer = PersistentContextComposer(
        store=store,
        session=session,
        agent_id=agent.id,
        workspace=PHASE1D_WORKSPACE_REF,
        thread_id=thread.id,
        memory_resolver=lambda _memory_id: (_ for _ in ()).throw(AssertionError()),
        artifact_reader=lambda _artifact_id: b"",
        artifact_writer=lambda _content: (_ for _ in ()).throw(AssertionError()),
    )
    revision = composer.compose(
        snapshot=session.role_snapshot,
        messages=(
            Message(role=MessageRole.SYSTEM, content="Operate safely."),
            Message(role=MessageRole.USER, content="Explain."),
        ),
        tools=(),
        request_ordinal=1,
    ).revision
    prompt = "Side question"
    response = "Use the explicit safe boundary."
    run = store.create_btw_sidecar_run(
        BTWSidecarRun(
            session_id=session.id,
            agent_id=agent.id,
            thread_id=thread.id,
            workspace_ref=PHASE1D_WORKSPACE_REF,
            source_item_cursor_end=source.cursor or 0,
            prompt=prompt,
            prompt_hash=hashlib.sha256(prompt.encode()).hexdigest(),
        )
    )
    completed = store.update_btw_sidecar_run(
        run.id,
        status=BTWSidecarStatus.COMPLETED,
        response=response,
        response_hash=hashlib.sha256(response.encode()).hexdigest(),
        context_revision_id=revision.id,
    )
    turn = Turn(thread_id=thread.id)
    item = Item(
        thread_id=thread.id,
        turn_id=turn.id,
        payload=SteeringPayload(text=response),
    )
    promoted, stored_turn, stored_item, created = store.promote_btw_sidecar(
        completed.id,
        turn=turn,
        steering_item=item,
    )
    replay = store.promote_btw_sidecar(completed.id, turn=turn, steering_item=item)
    assert promoted.status is BTWSidecarStatus.PROMOTED
    assert created is True
    assert replay == (promoted, stored_turn, stored_item, False)
    assert len(store.list_items(thread.id)) == 2
    other_turn = Turn(thread_id=thread.id)
    concurrent_replay = store.promote_btw_sidecar(
        completed.id,
        turn=other_turn,
        steering_item=Item(
            thread_id=thread.id,
            turn_id=other_turn.id,
            payload=SteeringPayload(text=response),
        ),
    )
    assert concurrent_replay == (promoted, stored_turn, stored_item, False)
    assert [event.event_type for event in store.list_btw_sidecar_events(completed.id)] == [
        "btw.promoted"
    ]


def test_sidecar_events_and_command_audit_are_cursor_replayable(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "events.sqlite3")
    store.initialize()
    session, agent, thread = _scope(store)
    run = store.create_btw_sidecar_run(
        BTWSidecarRun(
            session_id=session.id,
            agent_id=agent.id,
            thread_id=thread.id,
            workspace_ref=PHASE1D_WORKSPACE_REF,
            source_item_cursor_end=0,
            prompt="Question",
            prompt_hash=hashlib.sha256(b"Question").hexdigest(),
        )
    )
    event = store.append_btw_sidecar_event(
        BTWSidecarEvent(
            sidecar_run_id=run.id,
            event_type="btw.started",
            payload={"resource_id": run.id},
        )
    )
    assert store.list_btw_sidecar_events(run.id, after_cursor=event.cursor) == []
    command, claimed = store.reserve_command_execution(
        CommandExecution(
            command_type="phase1d.context.clear",
            idempotency_key="audit-test",
            action_hash="a" * 64,
        )
    )
    assert claimed
    audit = store.append_phase1d_command_audit_event(
        Phase1DCommandAuditEvent(
            command_execution_id=command.id,
            command_kind=SlashCommandKind.CONTEXT_CLEAR,
            event_type="context.baseline_appended",
            resource_type="context_baseline",
            resource_id="baseline-safe",
            detail={"item_cursor_end": 0},
        )
    )
    assert store.list_phase1d_command_audit_events(command.id) == [audit]
