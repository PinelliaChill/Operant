"""A small, repeatable path from model selection to an ordinary conversation."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from typing import Any

from operant.application.configuration import ConfigPatch, ConfigService
from operant.application.default_skill_pack import install_default_skill_pack
from operant.application.defaults import default_role_presets
from operant.application.service import ApplicationService
from operant.contracts.b2_3 import ManagementCommand
from operant.contracts.onboarding import (
    ConversationInitialization,
    ConversationMetadata,
    ConversationRename,
    ConversationStart,
    ModelOption,
    ProviderConnection,
    RoleOption,
    SetupBootstrap,
    SetupState,
)
from operant.domain.models import (
    CommandExecutionPolicy,
    CommandRunnerType,
    ModelProfile,
    RolePreset,
    RoleStatus,
    ToolPolicy,
)
from operant.domain.threads import ConversationThread, LegacySourceType, ThreadLegacyRef
from operant.memory_plugins.manager import MemoryManager
from operant.persistence.sqlite import ConflictError, NotFoundError
from operant.plugins.protocol import PluginError
from operant.protocol import canonical_action_hash

_GENERAL_ROLE_ID = "role_general"
_UNTITLED = "新对话"


class OnboardingService:
    def __init__(self, service: ApplicationService, repo: Any) -> None:
        self.service = service
        self.repo = repo
        self._setup_lock = asyncio.Lock()

    def state(self) -> SetupState:
        profiles = [profile for profile in self.service.list_model_profiles() if profile.enabled]
        profile_ids = {profile.id for profile in profiles}
        default_profile_id = self.repo.get_setting("default_model_profile_id")
        if default_profile_id not in profile_ids:
            default_profile_id = None
        workspace_id = self.repo.get_setting("default_workspace_id")
        if workspace_id is not None:
            try:
                workspace = self.service.store.get_workspace_initialization_by_id(workspace_id)
                if not Path(workspace.workspace_ref).is_dir():
                    workspace_id = None
            except (NotFoundError, OSError):
                workspace_id = None
        role_id = self.repo.get_setting("default_role_id")
        roles = [role for role in self.service.list_roles() if role.status is RoleStatus.ACTIVE]
        if role_id not in {role.id for role in roles}:
            role_id = None
        if role_id is not None and workspace_id is not None:
            try:
                workspace = self.service.store.get_workspace_initialization_by_id(workspace_id)
                if default_profile_id is None:
                    role_id = None
                else:
                    self._run_effort(
                        self.service.get_role(role_id),
                        self.service.get_model_profile(default_profile_id),
                        workspace_id,
                        workspace.workspace_ref,
                    )
            except (NotFoundError, ValueError, KeyError):
                role_id = None
        missing: list[str] = []
        if default_profile_id is None:
            missing.append("model")
        if workspace_id is None:
            missing.append("workspace")
        if role_id is None:
            missing.append("assistant")
        if not self._default_skills_ready(workspace_id, default_profile_id):
            missing.append("skills")
        connections = [
            ProviderConnection.model_validate(
                {key: record[key] for key in ProviderConnection.model_fields if key in record}
            )
            for record in self.repo.list_connections()
        ]
        return SetupState(
            ready=not missing,
            default_workspace_id=workspace_id,
            default_model_profile_id=default_profile_id,
            default_role_id=role_id,
            missing_steps=missing,
            connections=connections,
            roles=[
                RoleOption(
                    id=role.id,
                    name=role.name,
                    model_profile_id=role.model_profile_id,
                )
                for role in roles
            ],
            models=[
                ModelOption(id=profile.id, name=profile.name, model_id=profile.model_id)
                for profile in profiles
            ],
        )

    async def bootstrap(self, request: SetupBootstrap) -> SetupState:
        async with self._setup_lock:
            return await self._bootstrap(request)

    async def _bootstrap(self, request: SetupBootstrap) -> SetupState:
        profile_id = request.model_profile_id or self.repo.get_setting("default_model_profile_id")
        if profile_id is None:
            active = [profile for profile in self.service.list_model_profiles() if profile.enabled]
            if len(active) == 1:
                profile_id = active[0].id
        if profile_id is None:
            return self.state()
        profile = self.service.get_model_profile(profile_id)
        if not profile.enabled:
            raise ValueError("select an active model before setup")

        workspace_id = self.repo.get_setting("default_workspace_id")
        workspace = None
        if workspace_id is not None:
            with suppress(NotFoundError):
                existing = self.service.store.get_workspace_initialization_by_id(workspace_id)
                if Path(existing.workspace_ref).is_dir():
                    workspace = existing
        if workspace is None:
            path = self.service.store.path.absolute().parent / "workspace"
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            workspace, _ = self.service.initialize_workspace(path)
            self.repo.set_setting("default_workspace_id", workspace.id)

        self._ensure_default_roles(profile.id)
        selected_role = self.repo.get_setting("default_role_id")
        if not self._active_role(selected_role):
            self.repo.set_setting("default_role_id", _GENERAL_ROLE_ID)
        if (
            request.model_profile_id is not None
            or self.repo.get_setting("default_model_profile_id") is None
        ):
            self.repo.set_setting("default_model_profile_id", profile.id)

        manager = self._manager(create=True)
        assert manager is not None
        project = next(
            (
                item
                for item in manager.projection().projects
                if item.workspace_id == workspace.id and not item.archived
            ),
            None,
        )
        if project is None:
            result = await manager.execute(
                ManagementCommand(
                    action="project_create",
                    name="个人工作区",
                    workspace_path=workspace.workspace_ref,
                )
            )
            project = next(
                item
                for item in result.state.projects
                if item.workspace_id == workspace.id and not item.archived
            )
        skill_ids = await install_default_skill_pack(manager, project_id=project.project_id)
        self.repo.set_setting("default_skill_pack_ids", list(skill_ids))
        self._bind_default_skills(workspace.id, skill_ids)
        return self.state()

    async def initialize_conversation(
        self, body: ConversationStart, *, idempotency_key: str
    ) -> ConversationInitialization:
        current = self.state()
        workspace_id = body.workspace_id or current.default_workspace_id
        role_id = body.role_id or current.default_role_id
        profile_id = body.model_profile_id or current.default_model_profile_id
        if workspace_id is None or role_id is None or profile_id is None:
            raise ValueError("finish model and workspace setup before starting a conversation")
        workspace = self.service.store.get_workspace_initialization_by_id(workspace_id)
        role = self.service.get_role(role_id)
        profile = self.service.get_model_profile(profile_id)
        if role.status is RoleStatus.INACTIVE or not profile.enabled:
            raise ValueError("selected assistant or model is inactive")
        effort = self._run_effort(role, profile, workspace.id, workspace.workspace_ref)
        fingerprint = canonical_action_hash(
            {"operation": "initialize_conversation", "body": body.model_dump(mode="json")}
        )
        prior = self.repo.get_command(idempotency_key, fingerprint)
        if prior is not None:
            if not isinstance(prior.get("thread_id"), str) or not isinstance(
                prior.get("session_id"), str
            ):
                raise ConflictError("conversation result needs reconciliation")
            existing_thread = self.service.get_thread(prior["thread_id"])
            if existing_thread.workspace_ref != workspace.workspace_ref:
                raise ConflictError("conversation workspace changed")
            self._apply_requested_title(existing_thread.id, body.title)
            return ConversationInitialization(
                thread_id=existing_thread.id,
                session_id=prior["session_id"],
                workspace_id=workspace.id,
                title=self.conversation_metadata(existing_thread.id).title,
            )
        if body.thread_id is not None:
            thread = self.service.get_thread(body.thread_id)
            if thread.workspace_ref != workspace.workspace_ref:
                raise ConflictError("conversation belongs to another workspace")
            session_ref = next(
                (ref for ref in thread.legacy_refs if ref.source_type is LegacySourceType.SESSION),
                None,
            )
            if session_ref is not None:
                session = self.service.get_session(session_ref.source_id)
                if (
                    session.role_snapshot.role_id != role_id
                    or session.role_snapshot.model_profile_id != profile_id
                ):
                    raise ConflictError("conversation already uses another assistant or model")
            else:
                await self._ensure_project(workspace.id, workspace.workspace_ref)
                session = self.service.create_session(
                    role_id,
                    model_profile_id=profile_id,
                    effort=effort,
                    thread_id=thread.id,
                    workspace_ref=workspace.workspace_ref,
                    _onboarding_command=(idempotency_key, fingerprint),
                )
        else:
            await self._ensure_project(workspace.id, workspace.workspace_ref)
            thread = ConversationThread(workspace_ref=workspace.workspace_ref)
            session = self.service.create_session(
                role_id,
                model_profile_id=profile_id,
                effort=effort,
                workspace_ref=workspace.workspace_ref,
                _new_thread=thread,
                _onboarding_command=(idempotency_key, fingerprint),
                _onboarding_title=body.title,
            )
            thread = self.service.store.get_thread_by_legacy_ref(
                ThreadLegacyRef(source_type=LegacySourceType.SESSION, source_id=session.id)
            )
        if body.thread_id is not None:
            self._apply_requested_title(thread.id, body.title)
        metadata = self.conversation_metadata(thread.id)
        result = ConversationInitialization(
            thread_id=thread.id,
            session_id=session.id,
            workspace_id=workspace.id,
            title=metadata.title,
        )
        if body.thread_id is not None and session_ref is not None:
            self.repo.save_command(idempotency_key, fingerprint, result.model_dump(mode="json"))
        return result

    def conversation_metadata(self, thread_id: str) -> ConversationMetadata:
        self.service.get_thread(thread_id)
        metadata = self.repo.get_thread_metadata(thread_id)
        if metadata is None:
            return ConversationMetadata(
                thread_id=thread_id, title=_UNTITLED, title_source="default", revision=0
            )
        return ConversationMetadata.model_validate(metadata)

    def _apply_requested_title(self, thread_id: str, title: str | None) -> None:
        if title is None:
            return
        existing = self.conversation_metadata(thread_id)
        if existing.title_source == "manual":
            if existing.title != title:
                raise ConflictError("conversation already has a different manual name")
            return
        self.repo.put_thread_metadata(
            thread_id, title, "manual", expected_revision=existing.revision
        )

    def list_conversation_metadata(
        self, *, limit: int = 100, request_id: str | None = None
    ) -> list[ConversationMetadata]:
        if request_id is not None:
            metadata = self.repo.get_conversation_command_metadata(request_id)
            return [] if metadata is None else [metadata]
        return [
            self.conversation_metadata(thread.id)
            for thread in self.service.list_threads(limit=limit)
        ]

    def rename_conversation(
        self, thread_id: str, body: ConversationRename, *, idempotency_key: str
    ) -> ConversationMetadata:
        if body.expected_revision is None:
            raise ValueError("refresh the conversation name before renaming")
        fingerprint = canonical_action_hash(
            {
                "operation": "rename_conversation",
                "thread_id": thread_id,
                "body": body.model_dump(mode="json"),
            }
        )
        previous = self.repo.get_command(idempotency_key, fingerprint)
        if previous is not None:
            return ConversationMetadata.model_validate(previous)
        changed = ConversationMetadata.model_validate(
            self.repo.rename(thread_id, body.title, body.expected_revision)
        )
        self.repo.save_command(idempotency_key, fingerprint, changed.model_dump(mode="json"))
        return changed

    def _ensure_default_roles(self, profile_id: str) -> None:
        profile = self.service.get_model_profile(profile_id)
        presets = default_role_presets(
            planner_model_profile_id=profile_id,
            coder_model_profile_id=profile_id,
            reviewer_model_profile_id=profile_id,
        )
        general = RolePreset(
            id=_GENERAL_ROLE_ID,
            name="通用助手",
            system_prompt="你是通用任务助手。先理解用户目标，用清楚的语言回答，并遵守当前工具权限。",
            model_profile_id=profile_id,
            effort=profile.default_effort,
            tool_policy=ToolPolicy(
                allowed_tools=(
                    "read_file",
                    "search_files",
                    "apply_patch",
                    "run_command",
                    "git_diff",
                ),
                workspace_write=True,
                command_execution=True,
                command_execution_policy=CommandExecutionPolicy(
                    runner=CommandRunnerType.HOST,
                ),
            ),
            budget=presets[0].budget,
            memory_scope=presets[0].memory_scope,
        )
        localized = {
            "role_planner": "规划",
            "role_explorer": "探索",
            "role_coder": "编程",
            "role_reviewer": "审查",
        }
        for preset in (general, *presets[1:]):
            with suppress(NotFoundError):
                self.service.get_role(preset.id)
                continue
            if preset.id in localized:
                preset = preset.model_copy(update={"name": localized[preset.id]})
            if preset.id == "role_coder":
                preset = preset.model_copy(
                    update={
                        "system_prompt": preset.system_prompt.replace(
                            "命令默认在受限 Docker 快照中运行；",
                            "命令只在当前已登记的可信工作区按权限运行；",
                        ),
                        "tool_policy": preset.tool_policy.model_copy(
                            update={
                                "command_execution_policy": CommandExecutionPolicy(
                                    runner=CommandRunnerType.HOST
                                )
                            }
                        ),
                    }
                )
            if preset.effort not in profile.supported_efforts:
                preset = preset.model_copy(update={"effort": profile.default_effort})
            self.service.create_role(preset)

    def _active_role(self, role_id: str | None) -> bool:
        if role_id is None:
            return False
        try:
            return self.service.get_role(role_id).status is RoleStatus.ACTIVE
        except NotFoundError:
            return False

    def _manager(self, *, create: bool = False) -> MemoryManager | None:
        manager = self.service.memory_manager
        if manager is None and self.service.memory_manager_factory is not None:
            manager = self.service.memory_manager_factory()
            self.service.memory_manager = manager
        if manager is None and create:
            manager = MemoryManager(self.service)
            self.service.memory_manager = manager
        return manager

    async def _ensure_project(self, workspace_id: str, workspace_ref: str) -> None:
        async with self._setup_lock:
            manager = self._manager(create=True)
            assert manager is not None
            if any(
                project.workspace_id == workspace_id and not project.archived
                for project in manager.projection().projects
            ):
                project = next(
                    item
                    for item in manager.projection().projects
                    if item.workspace_id == workspace_id and not item.archived
                )
            else:
                result = await manager.execute(
                    ManagementCommand(
                        action="project_create", name="对话工作区", workspace_path=workspace_ref
                    )
                )
                project = next(
                    item
                    for item in result.state.projects
                    if item.workspace_id == workspace_id and not item.archived
                )
            skill_ids = await install_default_skill_pack(manager, project_id=project.project_id)
            self._bind_default_skills(workspace_id, skill_ids)

    def _bind_default_skills(self, project_id: str, skill_ids: tuple[str, ...]) -> None:
        config = ConfigService(self.service.store)
        project_scope = config.get_scope("project", project_id)
        # A user's explicit global or project selection wins. New projects have
        # no selection, so freeze the bundled set into ordinary Session snapshots.
        if project_scope.patch.skill_ids is not None:
            return
        if config.get_scope("global", "default").patch.skill_ids is not None:
            return
        config.put_scope(
            "project",
            project_id,
            patch={**project_scope.patch.explicit(), "skill_ids": skill_ids},
            expected_revision=project_scope.revision,
        )

    def _run_effort(
        self, role: RolePreset, profile: ModelProfile, workspace_id: str, workspace_ref: str
    ) -> str | None:
        config = ConfigService(self.service.store)
        try:
            config.effective(
                role,
                project_id=workspace_id,
                workspace_ref=workspace_ref,
                run_overrides=ConfigPatch(model_profile_id=profile.id),
            )
            return None
        except ValueError as exc:
            if "effort is unsupported" not in str(exc):
                raise
            config.effective(
                role,
                project_id=workspace_id,
                workspace_ref=workspace_ref,
                run_overrides=ConfigPatch(
                    model_profile_id=profile.id, effort=profile.default_effort
                ),
            )
            return profile.default_effort.value

    def _default_skills_ready(self, workspace_id: str | None, profile_id: str | None) -> bool:
        ids = self.repo.get_setting("default_skill_pack_ids", [])
        if workspace_id is None or profile_id is None or not isinstance(ids, list) or len(ids) != 6:
            return False
        manager = self._manager()
        if manager is None:
            return False
        project = next(
            (
                item
                for item in manager.projection().projects
                if item.workspace_id == workspace_id and not item.archived
            ),
            None,
        )
        if project is None:
            return False
        installed = {
            item.skill_id
            for item in manager.projection().skills
            if item.state == "installed" and project.project_id in item.project_ids
        }
        if not set(ids).issubset(installed):
            return False
        try:
            workspace = self.service.store.get_workspace_initialization_by_id(workspace_id)
            verified = {
                item["skill_id"] for item in manager.list_invocable_skills(workspace.workspace_ref)
            }
            role = self.service.get_role(_GENERAL_ROLE_ID)
            profile = self.service.get_model_profile(profile_id)
            effort = self._run_effort(role, profile, workspace_id, workspace.workspace_ref)
            effective = ConfigService(self.service.store).effective(
                role,
                project_id=workspace_id,
                workspace_ref=workspace.workspace_ref,
                run_overrides=ConfigPatch(
                    model_profile_id=profile_id,
                    effort=profile.default_effort if effort is not None else None,
                ),
            )
        except (NotFoundError, ValueError, OSError, PluginError):
            return False
        return set(ids).issubset(verified) and set(ids).issubset(effective.values["skill_ids"])
