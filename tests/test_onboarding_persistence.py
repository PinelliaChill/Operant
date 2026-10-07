"""Setup identity, title and credential boundaries on the actual SQLite store."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from operant.application.service import ApplicationService
from operant.domain.models import ModelProfile, RolePreset
from operant.domain.threads import (
    ConversationThread,
    Item,
    ThreadLegacyRef,
    Turn,
    UserMessagePayload,
)
from operant.persistence.onboarding import UXRepository
from operant.persistence.sqlite import ConflictError, MigrationError, SQLiteStore
from operant.protocol import canonical_action_hash


def _user_message(store: SQLiteStore, thread_id: str, text: str) -> None:
    turn = store.create_turn(Turn(thread_id=thread_id))
    store.append_item(
        Item(thread_id=thread_id, turn_id=turn.id, payload=UserMessagePayload(text=text))
    )


def _service(tmp_path: Path) -> tuple[ApplicationService, str, str]:
    service = ApplicationService(SQLiteStore(tmp_path / "db.sqlite3"), object())  # type: ignore[arg-type]
    service.initialize()
    profile = service.add_model_profile(
        ModelProfile(
            name="Available",
            model_id="fixture-model",
            base_url="https://fixture.test/v1",
            secret_ref="FIXTURE_KEY",
            effort_parameter=None,
        )
    )
    role = service.create_role(
        RolePreset(name="Assistant", system_prompt="Assist.", model_profile_id=profile.id)
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    initialized, _ = service.initialize_workspace(workspace)
    return service, role.id, initialized.workspace_ref


def test_upgrade_names_old_conversation_from_first_message(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "old.sqlite3")
    store.migrate(23)
    old = store.create_thread(ConversationThread())
    _user_message(store, old.id, "最早的用户任务")
    frozen = store.list_applied_migrations()
    store.migrate()
    assert store.list_applied_migrations()[:23] == frozen
    repo = UXRepository(store)
    assert repo.get_metadata(old.id).title == "最早的用户任务"
    _user_message(store, old.id, "后来继续讨论")
    assert UXRepository(SQLiteStore(store.path)).get_metadata(old.id).title == "最早的用户任务"


def test_manual_name_survives_messages_and_conflicting_client(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "titles.sqlite3")
    store.initialize()
    thread = store.create_thread(ConversationThread())
    repo = UXRepository(store)
    first = repo.rename(thread.id, "自定义名称", expected_revision=0)
    with pytest.raises(ConflictError):
        repo.rename(thread.id, "另一窗口的旧草稿", expected_revision=0)
    _user_message(store, thread.id, "这条消息不能替换手动名称")
    reread = UXRepository(SQLiteStore(store.path)).get_metadata(thread.id)
    assert reread.title == "自定义名称"
    assert reread.revision == first.revision
    assert reread.title_source == "manual"


def test_concurrent_new_conversation_has_one_identity(tmp_path: Path) -> None:
    service, role_id, workspace = _service(tmp_path)
    fingerprint = canonical_action_hash({"operation": "conversation_create"})

    def create(_: int) -> str:
        return service.create_session(
            role_id,
            workspace_ref=workspace,
            _new_thread=ConversationThread(workspace_ref=workspace),
            _onboarding_command=("same-click", fingerprint),
            _onboarding_title="我的任务",
        ).id

    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(create, range(3)))
    assert len(set(results)) == 1
    thread = service.store.get_thread_by_legacy_ref(
        ThreadLegacyRef(source_type="session", source_id=results[0])
    )
    assert UXRepository(service.store).get_metadata(thread.id).title == "我的任务"
    with service.store._connect() as connection:
        assert connection.execute("SELECT count(*) FROM threads").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1
    with pytest.raises(ConflictError):
        service.create_session(
            role_id,
            workspace_ref=workspace,
            _new_thread=ConversationThread(workspace_ref=workspace),
            _onboarding_command=("same-click", canonical_action_hash({"operation": "other"})),
        )


def test_private_provider_state_persists_but_secrets_cannot_enter_sqlite(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "private.sqlite3")
    store.initialize()
    repo = UXRepository(store)
    repo.save_connection("account", {"connection_id": "account", "secret_ref": "REF_ONLY"})
    repo.save_provider_metadata(
        "part",
        {
            "provider": "gemini",
            "connection_id": "account",
            "tool_call_id": "call",
            "thought_signature": "opaque",
        },
    )
    assert UXRepository(SQLiteStore(store.path)).get_provider_metadata("part") is not None
    for key in ("api_key", "refresh_token", "access_token", "client_secret"):
        with pytest.raises(ValueError):
            repo.save_connection("unsafe", {key: "must-not-persist"})
    for record in (
        {"provider": "gemini", "connection_id": "account", "parts": [{"thought": "hidden"}]},
        {
            "provider": "gemini",
            "connection_id": "account",
            "segments": [{"length": 5, "thought_signature": "opaque", "text": "hidden"}],
        },
        {
            "provider": "chatgpt",
            "connection_id": "account",
            "items": [{"type": "reasoning", "encrypted_content": "opaque", "summary": "hidden"}],
        },
    ):
        with pytest.raises(ValueError):
            repo.save_provider_metadata("unsafe", record)
    repo.delete_connection("account")
    assert repo.get_provider_metadata("part") is None
    assert b"must-not-persist" not in store.path.read_bytes()
    store.migrate(23, _isolated_rollback=True)
    assert store.schema_version() == 23


def test_rollback_refuses_to_remove_names(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "keep.sqlite3")
    store.initialize()
    thread = store.create_thread(ConversationThread())
    UXRepository(store).rename(thread.id, "要保留的历史")
    with pytest.raises(MigrationError):
        store.rollback(23, isolated=True)
    assert store.schema_version() == 24
