"""Additive onboarding.v1 public contract; credential values are request-only."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class OnboardingModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


ProviderKind = Literal["openai-compatible", "chatgpt", "gemini"]


class ProviderConnection(OnboardingModel):
    connection_id: str
    name: str
    provider: ProviderKind
    auth_method: Literal["api_key", "oauth"]
    status: Literal["needs_auth", "connected", "ready", "error"]
    base_url: str
    model_ids: list[str] = Field(default_factory=list)
    model_names: dict[str, str] = Field(default_factory=dict)
    profile_ids: list[str] = Field(default_factory=list)
    account_label: str | None = None
    project_id: str | None = None
    error: str | None = None


class ProviderConnectionList(OnboardingModel):
    items: list[ProviderConnection]


class ConnectionCreate(OnboardingModel):
    provider: ProviderKind = "openai-compatible"
    name: str | None = Field(default=None, min_length=1, max_length=100)
    base_url: str | None = Field(default=None, max_length=2048)
    api_key: SecretStr | None = None
    project_id: str | None = Field(default=None, max_length=200)
    client_id: str | None = Field(default=None, max_length=500)
    client_secret: SecretStr | None = None


class ConnectionModels(OnboardingModel):
    connection_id: str
    model_ids: list[str]
    model_names: dict[str, str] = Field(default_factory=dict)


class ConnectionModelSelection(OnboardingModel):
    model_id: str = Field(min_length=1, max_length=200)
    name: str | None = Field(default=None, min_length=1, max_length=100)


class ConnectionProfile(OnboardingModel):
    connection_id: str
    model_profile_id: str
    model_id: str


class OAuthStart(OnboardingModel):
    provider: Literal["chatgpt", "gemini"]
    connection_id: str | None = Field(default=None, max_length=200)
    project_id: str | None = Field(default=None, max_length=200)
    client_id: str | None = Field(default=None, max_length=500)
    client_secret: SecretStr | None = None


class OAuthAttempt(OnboardingModel):
    attempt_id: str
    connection_id: str
    provider: Literal["chatgpt", "gemini"]
    status: Literal["pending", "connected", "ready", "cancelled", "expired", "error"]
    authorization_url: str | None = None
    message: str | None = None
    expires_at: datetime


class ConnectionDeleted(OnboardingModel):
    connection_id: str
    disconnected: bool = True


class RoleOption(OnboardingModel):
    id: str
    name: str
    model_profile_id: str


class ModelOption(OnboardingModel):
    id: str
    name: str
    model_id: str


class SetupState(OnboardingModel):
    ready: bool
    default_workspace_id: str | None = None
    default_model_profile_id: str | None = None
    default_role_id: str | None = None
    missing_steps: list[str] = Field(default_factory=list)
    connections: list[ProviderConnection] = Field(default_factory=list)
    roles: list[RoleOption] = Field(default_factory=list)
    models: list[ModelOption] = Field(default_factory=list)


class SetupBootstrap(OnboardingModel):
    model_profile_id: str | None = Field(default=None, max_length=200)


class ConversationStart(OnboardingModel):
    workspace_id: str | None = Field(default=None, max_length=200)
    thread_id: str | None = Field(default=None, max_length=200)
    role_id: str | None = Field(default=None, max_length=200)
    model_profile_id: str | None = Field(default=None, max_length=200)
    title: str | None = Field(default=None, min_length=1, max_length=100)
    local_control_session_ids: tuple[
        Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_-]+$")], ...
    ] = Field(default=(), max_length=2)

    @field_validator("local_control_session_ids")
    @classmethod
    def unique_control_sessions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("本机控制会话不能重复")
        return tuple(sorted(value))

    def command_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json")
        if not self.local_control_session_ids:
            payload.pop("local_control_session_ids")
        return payload

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str | None) -> str | None:
        return None if value is None else validate_title(value)


def validate_title(value: str) -> str:
    value = value.strip()
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("名称不能为空或包含控制字符")
    return value


class ConversationInitialization(OnboardingModel):
    thread_id: str
    session_id: str
    workspace_id: str
    title: str


class ConversationMetadata(OnboardingModel):
    thread_id: str
    title: str
    title_source: Literal["manual", "auto", "default"]
    revision: int = Field(ge=0)


class ConversationMetadataList(OnboardingModel):
    items: list[ConversationMetadata]


class ConversationRename(OnboardingModel):
    title: str = Field(min_length=1, max_length=100)
    expected_revision: int | None = Field(default=None, ge=0)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        return validate_title(value)


class SkillSourceView(OnboardingModel):
    root_ref: str
    path: str
    label: str
    exists: bool
    enabled: bool
    issue: str | None = None


class SkillSourceList(OnboardingModel):
    items: list[SkillSourceView]


class SkillSourceInput(OnboardingModel):
    path: str = Field(min_length=1, max_length=4096)


class LocalApplication(OnboardingModel):
    bundle_id: str
    name: str


class LocalApplicationList(OnboardingModel):
    items: list[LocalApplication]


class ConversationLocalControlOpen(OnboardingModel):
    plugin_id: Literal["operant.chrome.browser", "operant.macos.computer"]
    computer_bundle_id: str | None = Field(default=None, max_length=253)


class ConversationLocalControlSession(OnboardingModel):
    session_id: str
    plugin_id: Literal["operant.chrome.browser", "operant.macos.computer"]
    target_id: str
    computer_bundle_id: str | None = None
    allowed_targets: tuple[str, ...] = Field(max_length=100)
    state: Literal["active", "human_control", "closed", "failed"]
    last_error: str | None = None


class ConversationLocalControlSessionList(OnboardingModel):
    items: list[ConversationLocalControlSession]


class CollaborationTemplateView(OnboardingModel):
    template_id: str
    name: str
    description: str
    member_count: int = Field(ge=1)


class CollaborationTemplateList(OnboardingModel):
    items: list[CollaborationTemplateView]


class CollaborationStart(OnboardingModel):
    template_id: str = "basic"
    workspace_id: str | None = Field(default=None, max_length=200)
    task: str = Field(min_length=1, max_length=4000)


class CollaborationStarted(OnboardingModel):
    workflow_definition_id: str
    graph_run_id: str
    team_run_id: str | None = None
    status: str
