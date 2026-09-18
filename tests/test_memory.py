from pathlib import Path

import pytest

from operant.application.service import ApplicationService
from operant.domain.memory import Memory, MemoryKind, MemoryStatus
from operant.domain.models import ModelProfile, RolePreset
from operant.persistence.sqlite import SQLiteStore
from operant.providers.openai_compatible import OpenAICompatibleProvider


def service_with_memory_role(tmp_path: Path) -> tuple[ApplicationService, str]:
    store = SQLiteStore(tmp_path / "memory.sqlite3")
    service = ApplicationService(store, OpenAICompatibleProvider())
    service.initialize()
    profile = service.add_model_profile(
        ModelProfile(
            id="memory_model",
            name="memory-test-model",
            model_id="memory-test-model",
            base_url="https://relay.example.com/v1",
            secret_ref="OPERANT_TEST_KEY",
        )
    )
    role = service.create_role(
        RolePreset(
            id="role_memory",
            name="Memory Role",
            system_prompt="Save and retrieve verified project knowledge.",
            model_profile_id=profile.id,
            memory_scope="read: [project, episodic]; write: [project, episodic]",
        )
    )
    return service, role.id


def test_legacy_memory_writes_have_one_upgrade_path(tmp_path: Path) -> None:
    service, role_id = service_with_memory_role(tmp_path)
    session = service.create_session(role_id)

    # No rollout marker can re-enable the removed Core writer.
    with pytest.raises(ValueError, match="schema_upgrade_required"):
        service.save_memory(
            snapshot=session.role_snapshot,
            session_id=session.id,
            kind=MemoryKind.PROJECT,
            content="legacy writer",
            project_scope=str(tmp_path),
        )
    with pytest.raises(ValueError, match="schema_upgrade_required"):
        service.create_memory(
            snapshot=session.role_snapshot,
            session_id=session.id,
            kind=MemoryKind.PROJECT,
            content="legacy writer",
            project_scope=str(tmp_path),
        )

    legacy = service.store.create_memory(
        Memory(
            id="legacy-memory",
            kind=MemoryKind.PROJECT,
            content="historical migration row",
            project_scope=str(tmp_path),
            source_session_id=session.id,
            status=MemoryStatus.ACTIVE,
        )
    )
    for operation in (
        lambda: service.update_memory(
            legacy.id,
            snapshot=session.role_snapshot,
            session_id=session.id,
            content="attempted rewrite",
        ),
        lambda: service.confirm_memory(
            legacy.id,
            snapshot=session.role_snapshot,
            session_id=session.id,
        ),
        lambda: service.activate_memory(
            legacy.id,
            snapshot=session.role_snapshot,
            session_id=session.id,
        ),
        lambda: service.deactivate_memory(
            legacy.id,
            snapshot=session.role_snapshot,
            session_id=session.id,
        ),
    ):
        with pytest.raises(ValueError, match="schema_upgrade_required"):
            operation()

    assert service.store.get_memory(legacy.id) == legacy


def test_legacy_explicit_reads_are_kept_but_search_requires_plugin_governance(
    tmp_path: Path,
) -> None:
    service, role_id = service_with_memory_role(tmp_path)
    session = service.create_session(role_id)
    legacy = service.store.create_memory(
        Memory(
            id="legacy-read",
            kind=MemoryKind.PROJECT,
            content="historical migration row",
            project_scope=str(tmp_path),
            status=MemoryStatus.ACTIVE,
        )
    )

    assert (
        service.get_memory(
            legacy.id,
            snapshot=session.role_snapshot,
            session_id=session.id,
            project_scope=str(tmp_path),
        )
        == legacy
    )
    with pytest.raises(PermissionError, match="memory plugin is not installed or selected"):
        service.query_memories(
            "historical",
            snapshot=session.role_snapshot,
            session_id=session.id,
            project_scope=str(tmp_path),
        )
