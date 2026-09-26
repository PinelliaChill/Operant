"""Durable, source-labelled configuration for new execution snapshots."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from operant.application.approval_review import ReviewerConfig
from operant.domain.models import Budget, Effort, RolePreset, ToolPolicy, utc_now
from operant.persistence.sqlite import ConflictError, SQLiteStore

ScopeType = Literal["global", "project", "workspace", "role"]
_ORDER: tuple[ScopeType, ...] = ("global", "project", "workspace", "role")


class ConfigPatch(BaseModel):
    """Only fields explicitly present in a scope change an inherited value."""

    model_config = ConfigDict(extra="forbid")

    system_prompt: str | None = Field(default=None, max_length=20_000)
    model_profile_id: str | None = Field(default=None, min_length=1, max_length=300)
    effort: Effort | None = None
    tool_policy: ToolPolicy | None = None
    budget: dict[str, int | float | None] | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    skill_ids: tuple[str, ...] | None = None
    mcp_server_ids: tuple[str, ...] | None = None
    approval_reviewer: ReviewerConfig | None = None

    @field_validator("budget")
    @classmethod
    def validate_budget(
        cls, value: dict[str, int | float | None] | None
    ) -> dict[str, int | float | None] | None:
        if value is not None:
            unknown = set(value) - set(Budget.model_fields)
            if unknown:
                raise ValueError(f"unknown budget fields: {sorted(unknown)}")
            Budget.model_validate({**Budget().model_dump(), **value})
        return value

    @field_validator("skill_ids", "mcp_server_ids")
    @classmethod
    def validate_bindings(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is not None and (
            len(value) > 100
            or len(set(value)) != len(value)
            or any(not item or len(item) > 300 for item in value)
        ):
            raise ValueError("bindings must be unique, bounded non-empty IDs")
        return value

    def explicit(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_unset=True, exclude_none=True)


class ScopeRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_type: ScopeType
    scope_id: str
    patch: ConfigPatch = Field(default_factory=ConfigPatch)
    revision: int = 0
    updated_at: str | None = None


class ConfigSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_type: str
    scope_id: str


class EffectiveConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    values: dict[str, Any]
    sources: dict[str, ConfigSource]
    prompt_sources: list[ConfigSource]
    scopes: dict[str, ScopeRecord]
    revisions: dict[str, int]
    applies_to: Literal["new_sessions"] = "new_sessions"


def workspace_scope_id(workspace_ref: str) -> str:
    root = Path(workspace_ref).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("workspace_ref must be a directory")
    return hashlib.sha256(str(root).encode("utf-8")).hexdigest()


def _narrow_policy(base: ToolPolicy, requested: ToolPolicy) -> ToolPolicy:
    current = base.model_dump(mode="json")
    chosen = requested.model_dump(mode="json")
    command = current["command_execution_policy"]
    requested_command = chosen["command_execution_policy"]
    # A scope can remove authority. It cannot switch a container run to Host.
    if command["runner"] == "docker" and requested_command["runner"] == "host":
        requested_command["runner"] = "docker"
    for key in ("cpu_limit", "memory_limit_mb", "pids_limit"):
        requested_command[key] = min(command[key], requested_command[key])
    if command["runner"] == "docker":
        requested_command["docker_image"] = command["docker_image"]
    chosen.update(
        allowed_tools=tuple(sorted(set(current["allowed_tools"]) & set(chosen["allowed_tools"]))),
        workspace_write=current["workspace_write"] and chosen["workspace_write"],
        command_execution=current["command_execution"] and chosen["command_execution"],
        approval_required=tuple(
            sorted(set(current["approval_required"]) | set(chosen["approval_required"]))
        ),
        command_execution_policy=requested_command,
    )
    return ToolPolicy.model_validate(chosen)


def _narrow_budget(base: Budget, requested: dict[str, int | float | None]) -> Budget:
    values = base.model_dump()
    for key, value in requested.items():
        previous = values[key]
        if previous is None:
            values[key] = value
        elif value is not None:
            values[key] = min(previous, value)
    return Budget.model_validate(values)


class ConfigService:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    @staticmethod
    def _check_scope(scope_type: str, scope_id: str) -> ScopeType:
        if scope_type not in _ORDER or not scope_id or len(scope_id) > 300:
            raise ValueError("invalid configuration scope")
        if scope_type == "global" and scope_id != "default":
            raise ValueError("global configuration uses scope_id 'default'")
        return scope_type

    def get_scope(self, scope_type: str, scope_id: str) -> ScopeRecord:
        checked = self._check_scope(scope_type, scope_id)
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT body_json, revision, updated_at FROM scope_configs "
                "WHERE scope_type = ? AND scope_id = ?",
                (checked, scope_id),
            ).fetchone()
        if row is None:
            return ScopeRecord(scope_type=checked, scope_id=scope_id)
        return ScopeRecord(
            scope_type=checked,
            scope_id=scope_id,
            patch=ConfigPatch.model_validate_json(row["body_json"]),
            revision=int(row["revision"]),
            updated_at=str(row["updated_at"]),
        )

    def put_scope(
        self,
        scope_type: str,
        scope_id: str,
        *,
        patch: dict[str, Any],
        expected_revision: int,
    ) -> ScopeRecord:
        checked = self._check_scope(scope_type, scope_id)
        if expected_revision < 0:
            raise ValueError("expected_revision must be nonnegative")
        if checked != "global" and "approval_reviewer" in patch:
            raise ValueError("approval reviewer is a global safety setting")
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT body_json, revision FROM scope_configs "
                "WHERE scope_type = ? AND scope_id = ?",
                (checked, scope_id),
            ).fetchone()
            actual = 0 if row is None else int(row["revision"])
            if actual != expected_revision:
                raise ConflictError("configuration revision changed")
            # PUT replaces this scope's overrides; omitted fields inherit again.
            validated = ConfigPatch.model_validate(
                {key: value for key, value in patch.items() if value is not None}
            )
            stamp = utc_now().isoformat()
            connection.execute(
                "INSERT INTO scope_configs(scope_type,scope_id,body_json,revision,updated_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(scope_type,scope_id) DO UPDATE SET "
                "body_json=excluded.body_json,revision=excluded.revision,updated_at=excluded.updated_at",
                (
                    checked,
                    scope_id,
                    json.dumps(validated.explicit(), sort_keys=True),
                    actual + 1,
                    stamp,
                ),
            )
        return ScopeRecord(
            scope_type=checked,
            scope_id=scope_id,
            patch=validated,
            revision=actual + 1,
            updated_at=stamp,
        )

    def delete_scope(self, scope_type: str, scope_id: str, *, expected_revision: int) -> None:
        checked = self._check_scope(scope_type, scope_id)
        with self.store._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT revision FROM scope_configs WHERE scope_type = ? AND scope_id = ?",
                (checked, scope_id),
            ).fetchone()
            actual = 0 if row is None else int(row["revision"])
            if actual != expected_revision:
                raise ConflictError("configuration revision changed")
            connection.execute(
                "DELETE FROM scope_configs WHERE scope_type = ? AND scope_id = ?",
                (checked, scope_id),
            )

    def effective(
        self,
        role: RolePreset,
        *,
        project_id: str | None = None,
        workspace_ref: str | None = None,
        run_overrides: ConfigPatch | None = None,
    ) -> EffectiveConfig:
        scopes = [self.get_scope("global", "default")]
        if project_id:
            scopes.append(self.get_scope("project", project_id))
        if workspace_ref:
            scopes.append(self.get_scope("workspace", workspace_scope_id(workspace_ref)))
        scopes.append(self.get_scope("role", role.id))

        values: dict[str, Any] = {
            "system_prompt": role.system_prompt,
            "model_profile_id": role.model_profile_id,
            "effort": role.effort.value,
            "tool_policy": role.tool_policy.model_dump(mode="json"),
            "budget": role.budget.model_dump(mode="json"),
            "temperature": None,
            "skill_ids": [],
            "mcp_server_ids": [],
            "approval_reviewer": ReviewerConfig().model_dump(mode="json"),
        }
        sources = {key: ConfigSource(scope_type="role_base", scope_id=role.id) for key in values}
        prompt_sources = [sources["system_prompt"]]
        all_layers: list[tuple[ConfigSource, ConfigPatch]] = [
            (ConfigSource(scope_type=item.scope_type, scope_id=item.scope_id), item.patch)
            for item in scopes
        ]
        if run_overrides is not None:
            all_layers.append((ConfigSource(scope_type="run", scope_id="new"), run_overrides))
        for source, patch in all_layers:
            for key, value in patch.explicit().items():
                if key == "system_prompt":
                    values[key] = f"{values[key]}\n\n{value}" if value else values[key]
                    prompt_sources.append(source)
                elif key == "tool_policy":
                    values[key] = _narrow_policy(
                        ToolPolicy.model_validate(values[key]), ToolPolicy.model_validate(value)
                    ).model_dump(mode="json")
                elif key == "budget":
                    if source.scope_type == "run":
                        values[key] = (
                            Budget.model_validate(values[key])
                            .narrowed(**value)
                            .model_dump(mode="json")
                        )
                    else:
                        values[key] = _narrow_budget(
                            Budget.model_validate(values[key]), value
                        ).model_dump(mode="json")
                else:
                    values[key] = value
                sources[key] = source
        profile = self.store.get_model_profile(str(values["model_profile_id"]))
        if not profile.enabled:
            raise ValueError("effective model profile is inactive")
        selected_effort = Effort(values["effort"])
        if selected_effort not in profile.supported_efforts:
            raise ValueError("effective effort is unsupported by the selected model")
        if values["temperature"] is not None and not profile.supports_temperature:
            raise ValueError("selected model profile does not support temperature")
        if not profile.supports_temperature:
            values.pop("temperature")
            sources.pop("temperature")
        if values["mcp_server_ids"]:
            from operant.persistence.phase45 import SQLitePhase45Repository

            registered = {
                item["server_id"] for item in SQLitePhase45Repository(self.store).list_mcp_servers()
            }
            if set(values["mcp_server_ids"]) - registered:
                raise ValueError("bound MCP server is not registered")
        return EffectiveConfig(
            values=values,
            sources=sources,
            prompt_sources=prompt_sources,
            scopes={item.scope_type: item for item in scopes},
            revisions={item.scope_type: item.revision for item in scopes},
        )
