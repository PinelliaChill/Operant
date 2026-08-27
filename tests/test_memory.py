from pathlib import Path

import pytest

from operant.application.service import ApplicationService
from operant.domain.memory import MemoryKind, MemoryStatus
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


def test_task_a_saves_verification_command_and_task_b_reuses_it(tmp_path: Path) -> None:
    service, role_id = service_with_memory_role(tmp_path)
    task_a = service.create_session(role_id)
    task_b = service.create_session(role_id)

    saved = service.save_memory(
        snapshot=task_a.role_snapshot,
        session_id=task_a.id,
        kind=MemoryKind.PROJECT,
        content="Run pytest tests/test_calculator.py after every change.",
        project_scope="calculator",
        source_session_id=task_a.id,
        source_task="Task A verification command",
        confidence=0.96,
        allow_conservative_activation=True,
    )

    assert saved.status is MemoryStatus.ACTIVE
    reused = service.query_memories(
        "pytest calculator",
        snapshot=task_b.role_snapshot,
        session_id=task_b.id,
        project_scope="calculator",
    )

    assert [memory.content for memory in reused] == [saved.content]
    assert (
        service.trace_memory_source(
            saved.id,
            snapshot=task_b.role_snapshot,
            session_id=task_b.id,
            project_scope="calculator",
        ).source_session_id
        == task_a.id
    )


def test_candidate_requires_confirmation_and_updates_are_versioned(tmp_path: Path) -> None:
    service, role_id = service_with_memory_role(tmp_path)
    session = service.create_session(role_id)

    candidate = service.save_memory(
        snapshot=session.role_snapshot,
        session_id=session.id,
        kind=MemoryKind.EPISODIC,
        content="The first attempt needed an extra fixture.",
        source_session_id=session.id,
        source_task="Task A observation",
        confidence=0.5,
    )
    assert candidate.status is MemoryStatus.CANDIDATE
    with pytest.raises(PermissionError, match="confirmation"):
        service.activate_memory(
            candidate.id,
            snapshot=session.role_snapshot,
            session_id=session.id,
        )

    active = service.confirm_memory(
        candidate.id,
        snapshot=session.role_snapshot,
        session_id=session.id,
    )
    updated = service.update_memory(
        candidate.id,
        snapshot=session.role_snapshot,
        session_id=session.id,
        content="The confirmed attempt needed an extra fixture.",
    )
    versions = service.list_memory_versions(
        candidate.id,
        snapshot=session.role_snapshot,
        session_id=session.id,
    )

    assert active.version == 2
    assert updated.version == 3
    assert [memory.status for memory in versions] == [
        MemoryStatus.CANDIDATE,
        MemoryStatus.ACTIVE,
        MemoryStatus.ACTIVE,
    ]
    assert versions[0].source_session_id == session.id


def test_memory_scope_prevents_cross_kind_and_cross_project_access(tmp_path: Path) -> None:
    service, role_id = service_with_memory_role(tmp_path)
    session = service.create_session(role_id)

    with pytest.raises(PermissionError, match="does not allow: working"):
        service.save_memory(
            snapshot=session.role_snapshot,
            session_id=session.id,
            kind=MemoryKind.WORKING,
            content="session-only context",
        )

    saved = service.save_memory(
        snapshot=session.role_snapshot,
        session_id=session.id,
        kind=MemoryKind.PROJECT,
        content="Project-specific pytest command.",
        project_scope="project-a",
        source_session_id=session.id,
        source_task="Task A",
        confidence=0.96,
        allow_conservative_activation=True,
    )
    assert saved.status is MemoryStatus.ACTIVE
    assert (
        service.query_memories(
            "pytest",
            snapshot=session.role_snapshot,
            session_id=session.id,
            project_scope="project-b",
        )
        == []
    )
