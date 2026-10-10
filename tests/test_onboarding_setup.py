from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from operant.application.onboarding import OnboardingService
from operant.application.service import ApplicationService
from operant.contracts.onboarding import ConversationStart, SetupBootstrap
from operant.domain.models import CommandRunnerType, Effort, ModelProfile
from operant.persistence.onboarding import UXRepository
from operant.persistence.sqlite import SQLiteStore


class _Settings:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}

    def get_setting(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def set_setting(self, key: str, value: Any) -> None:
        self.values[key] = value

    def list_connections(self) -> list[dict[str, Any]]:
        return []


@pytest.mark.asyncio
async def test_bootstrap_seeds_only_missing_defaults_and_bundled_skills(tmp_path: Path) -> None:
    service = ApplicationService(SQLiteStore(tmp_path / "core.sqlite"), object())  # type: ignore[arg-type]
    service.initialize()
    profile = service.add_model_profile(
        ModelProfile(
            name="ready",
            model_id="test-model",
            base_url="https://example.test/v1",
            secret_ref="TEST_MODEL_KEY",
            effort_parameter=None,
        )
    )
    settings = _Settings()
    onboarding = OnboardingService(service, settings)
    try:
        assert onboarding.state().ready is False
        first = await onboarding.bootstrap(SetupBootstrap(model_profile_id=profile.id))
        assert first.ready is True
        assert first.default_role_id == "role_general"
        assert first.default_workspace_id is not None
        assert {role.id for role in first.roles} >= {
            "role_general",
            "role_planner",
            "role_explorer",
            "role_coder",
            "role_reviewer",
        }
        assert {role.name for role in first.roles} >= {"规划", "探索", "编程", "审查"}
        general = service.get_role("role_general")
        assert {"apply_patch", "run_command"}.issubset(general.tool_policy.allowed_tools)
        assert general.tool_policy.command_execution_policy.runner is CommandRunnerType.HOST
        coder = service.get_role("role_coder")
        assert coder.tool_policy.command_execution_policy.runner is CommandRunnerType.HOST
        assert "可信工作区" in coder.system_prompt
        manager = service.memory_manager
        assert manager is not None
        assert len(manager.projection().skills) == 6

        second = await onboarding.bootstrap(SetupBootstrap(model_profile_id=profile.id))
        assert second.ready is True
        assert second.default_workspace_id == first.default_workspace_id
        assert len(manager.projection().projects) == 1
        assert len(manager.projection().skills) == 6

        installed_id = manager.projection().skills[0].skill_id
        manifest = manager.root / "skills" / installed_id / "SKILL.md"
        manifest.write_text(manifest.read_text(encoding="utf-8") + "\nchanged", encoding="utf-8")
        assert "skills" in onboarding.state().missing_steps
        service.deactivate_role("role_general")
        assert "assistant" in onboarding.state().missing_steps
    finally:
        if service.memory_manager is not None:
            await service.memory_manager.close()
        service.close()


@pytest.mark.asyncio
async def test_disabled_old_model_can_be_replaced_without_rewriting_role_or_session(
    tmp_path: Path,
) -> None:
    service = ApplicationService(SQLiteStore(tmp_path / "core.sqlite"), object())  # type: ignore[arg-type]
    service.initialize()
    onboarding = OnboardingService(service, UXRepository(service.store))
    first_profile = service.add_model_profile(
        ModelProfile(
            name="old",
            model_id="old-model",
            base_url="https://example.test/v1",
            secret_ref="OLD_MODEL_KEY",
            effort_parameter=None,
        )
    )
    try:
        assert (await onboarding.bootstrap(SetupBootstrap(model_profile_id=first_profile.id))).ready
        first_conversation = await onboarding.initialize_conversation(
            ConversationStart(), idempotency_key="before-model-change"
        )
        first_snapshot = service.get_session(first_conversation.session_id).role_snapshot
        original_role = service.get_role("role_general")

        service.deactivate_model_profile(first_profile.id)
        assert "model" in onboarding.state().missing_steps
        replacement = service.add_model_profile(
            ModelProfile(
                name="replacement",
                model_id="new-model",
                base_url="https://example.test/v1",
                secret_ref="NEW_MODEL_KEY",
                supported_efforts=(Effort.LOW,),
                default_effort=Effort.LOW,
                effort_parameter=None,
            )
        )
        new_state = await onboarding.bootstrap(SetupBootstrap(model_profile_id=replacement.id))
        assert new_state.ready
        assert new_state.default_model_profile_id == replacement.id
        assert service.get_role("role_general") == original_role
        second_conversation = await onboarding.initialize_conversation(
            ConversationStart(), idempotency_key="after-model-change"
        )
        second_snapshot = service.get_session(second_conversation.session_id).role_snapshot
        assert first_snapshot.model_profile_id == first_profile.id
        assert second_snapshot.model_profile_id == replacement.id
        assert second_snapshot.effort is Effort.LOW
    finally:
        if service.memory_manager is not None:
            await service.memory_manager.close()
        service.close()


@pytest.mark.asyncio
async def test_other_registered_workspace_gets_its_own_default_skill_binding(
    tmp_path: Path,
) -> None:
    service = ApplicationService(SQLiteStore(tmp_path / "core.sqlite"), object())  # type: ignore[arg-type]
    service.initialize()
    profile = service.add_model_profile(
        ModelProfile(
            name="ready",
            model_id="test-model",
            base_url="https://example.test/v1",
            secret_ref="TEST_MODEL_KEY",
            effort_parameter=None,
        )
    )
    onboarding = OnboardingService(service, UXRepository(service.store))
    try:
        default = await onboarding.bootstrap(SetupBootstrap(model_profile_id=profile.id))
        assert default.default_workspace_id is not None
        other_path = tmp_path / "another-workspace"
        other_path.mkdir()
        other, _ = service.initialize_workspace(other_path)
        conversation = await onboarding.initialize_conversation(
            ConversationStart(workspace_id=other.id, title="另一个项目"),
            idempotency_key="other-workspace-first",
        )
        assert conversation.workspace_id == other.id
        assert conversation.title == "另一个项目"
        session = service.get_session(conversation.session_id)
        assert session.role_snapshot.config_workspace_ref == str(other_path)
        assert len(session.role_snapshot.skill_ids) == 6
        assert session.role_snapshot.config_project_id == other.id
        manager = service.memory_manager
        assert manager is not None
        project = next(
            item for item in manager.projection().projects if item.workspace_id == other.id
        )
        enabled = {
            skill.skill_id
            for skill in manager.projection().skills
            if project.project_id in skill.project_ids
        }
        assert enabled == set(session.role_snapshot.skill_ids)
    finally:
        if service.memory_manager is not None:
            await service.memory_manager.close()
        service.close()
