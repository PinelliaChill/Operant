from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from operant.domain.models import (
    AgentInstance,
    AgentStatus,
    Event,
    ModelProfile,
    RolePreset,
    RoleSnapshot,
    RoleStatus,
    Session,
    SnapshotOverrides,
)


class NotFoundError(LookupError):
    pass


class ConflictError(ValueError):
    pass


class SQLiteStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS model_profiles (
                    id TEXT PRIMARY KEY,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS role_heads (
                    id TEXT PRIMARY KEY,
                    current_version INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS role_versions (
                    role_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (role_id, version),
                    FOREIGN KEY (role_id) REFERENCES role_heads(id)
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agents (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                );

                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT UNIQUE NOT NULL,
                    session_id TEXT NOT NULL,
                    agent_id TEXT,
                    event_type TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                );
                """
            )

    def add_model_profile(self, profile: ModelProfile) -> ModelProfile:
        with self._connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO model_profiles(id, body, created_at) VALUES (?, ?, ?)",
                    (profile.id, profile.model_dump_json(), profile.created_at.isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"model profile already exists: {profile.id}") from exc
        return profile

    def get_model_profile(self, profile_id: str) -> ModelProfile:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT body FROM model_profiles WHERE id = ?", (profile_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"model profile not found: {profile_id}")
        return ModelProfile.model_validate_json(row["body"])

    def list_model_profiles(self) -> list[ModelProfile]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT body FROM model_profiles ORDER BY created_at, id"
            ).fetchall()
        return [ModelProfile.model_validate_json(row["body"]) for row in rows]

    def get_model_profile_by_name(self, name: str) -> ModelProfile:
        for profile in self.list_model_profiles():
            if profile.name == name:
                return profile
        raise NotFoundError(f"model profile not found by name: {name}")

    def update_model_profile(self, profile_id: str, **changes: Any) -> ModelProfile:
        current = self.get_model_profile(profile_id)
        forbidden = {"id", "created_at"}
        attempted = forbidden.intersection(changes)
        if attempted:
            raise ValueError(f"cannot change model profile identity fields: {sorted(attempted)}")
        updated = ModelProfile.model_validate({**current.model_dump(), **changes})
        with self._connect() as connection:
            connection.execute(
                "UPDATE model_profiles SET body = ? WHERE id = ?",
                (updated.model_dump_json(), profile_id),
            )
        return updated

    def deactivate_model_profile(self, profile_id: str) -> ModelProfile:
        return self.update_model_profile(profile_id, enabled=False)

    def create_role(self, role: RolePreset) -> RolePreset:
        if role.version != 1:
            raise ValueError("a new role must start at version 1")
        self._validate_role_model(role)
        with self._connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO role_heads(id, current_version) VALUES (?, 1)", (role.id,)
                )
                connection.execute(
                    """
                    INSERT INTO role_versions(role_id, version, body, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (role.id, role.version, role.model_dump_json(), role.created_at.isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"role already exists: {role.id}") from exc
        return role

    def get_role(self, role_id: str, version: int | None = None) -> RolePreset:
        with self._connect() as connection:
            if version is None:
                head = connection.execute(
                    "SELECT current_version FROM role_heads WHERE id = ?", (role_id,)
                ).fetchone()
                if head is None:
                    raise NotFoundError(f"role not found: {role_id}")
                version = int(head["current_version"])
            row = connection.execute(
                "SELECT body FROM role_versions WHERE role_id = ? AND version = ?",
                (role_id, version),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"role version not found: {role_id}@{version}")
        return RolePreset.model_validate_json(row["body"])

    def get_role_by_name(self, name: str) -> RolePreset:
        for role in self.list_roles(include_inactive=True):
            if role.name == name:
                return role
        raise NotFoundError(f"role not found by name: {name}")

    def list_role_versions(self, role_id: str) -> list[RolePreset]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT body FROM role_versions
                WHERE role_id = ? ORDER BY version
                """,
                (role_id,),
            ).fetchall()
        if not rows:
            raise NotFoundError(f"role not found: {role_id}")
        return [RolePreset.model_validate_json(row["body"]) for row in rows]

    def update_role(self, role_id: str, **changes: Any) -> RolePreset:
        current = self.get_role(role_id)
        forbidden = {"id", "version", "created_at"}
        attempted = forbidden.intersection(changes)
        if attempted:
            raise ValueError(f"cannot change version identity fields: {sorted(attempted)}")
        updated = current.model_copy(
            update={
                **changes,
                "version": current.version + 1,
                "created_at": current.created_at.__class__.now(current.created_at.tzinfo),
            }
        )
        updated = RolePreset.model_validate(updated.model_dump())
        self._validate_role_model(updated)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO role_versions(role_id, version, body, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    updated.id,
                    updated.version,
                    updated.model_dump_json(),
                    updated.created_at.isoformat(),
                ),
            )
            connection.execute(
                "UPDATE role_heads SET current_version = ? WHERE id = ?",
                (updated.version, updated.id),
            )
        return updated

    def deactivate_role(self, role_id: str) -> RolePreset:
        return self.update_role(role_id, status=RoleStatus.INACTIVE)

    def copy_role(self, role_id: str, *, name: str) -> RolePreset:
        source = self.get_role(role_id)
        copied = RolePreset(
            name=name,
            system_prompt=source.system_prompt,
            model_profile_id=source.model_profile_id,
            effort=source.effort,
            tool_policy=source.tool_policy,
            budget=source.budget,
            memory_scope=source.memory_scope,
        )
        return self.create_role(copied)

    def create_session(
        self,
        role_id: str,
        *,
        model_profile_id: str | None = None,
        effort: str | None = None,
        budget_overrides: dict[str, Any] | None = None,
    ) -> Session:
        role = self.get_role(role_id)
        if role.status is RoleStatus.INACTIVE:
            raise ValueError("cannot create a session from an inactive role")
        selected_profile_id = model_profile_id or role.model_profile_id
        profile = self.get_model_profile(selected_profile_id)
        if not profile.enabled:
            raise ValueError(f"model profile is inactive: {profile.id}")

        selected_effort = role.effort if effort is None else type(role.effort)(effort)
        if selected_effort not in profile.supported_efforts:
            raise ValueError(
                f"effort {selected_effort.value!r} is not supported by {profile.name!r}"
            )

        budget = role.budget
        overridden_budget_fields: tuple[str, ...] = ()
        if budget_overrides:
            budget = type(role.budget).model_validate(
                {**role.budget.model_dump(), **budget_overrides}
            )
            overridden_budget_fields = tuple(sorted(budget_overrides))

        snapshot = RoleSnapshot(
            role_id=role.id,
            role_version=role.version,
            role_name=role.name,
            system_prompt=role.system_prompt,
            model_profile_id=profile.id,
            model_profile_name=profile.name,
            provider=profile.provider,
            model_id=profile.model_id,
            base_url=profile.base_url,
            secret_ref=profile.secret_ref,
            effort=selected_effort,
            provider_effort_parameter=profile.effort_parameter,
            provider_effort_value=profile.provider_effort_value(selected_effort),
            tool_policy=role.tool_policy,
            budget=budget,
            memory_scope=role.memory_scope,
            overrides=SnapshotOverrides(
                effort_overridden=effort is not None,
                model_profile_overridden=model_profile_id is not None,
                budget_fields=overridden_budget_fields,
            ),
        )
        session = Session(role_snapshot=snapshot)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO sessions(id, body, created_at) VALUES (?, ?, ?)",
                (session.id, session.model_dump_json(), session.created_at.isoformat()),
            )
        return session

    def get_session(self, session_id: str) -> Session:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT body FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"session not found: {session_id}")
        return Session.model_validate_json(row["body"])

    def create_agent(self, session_id: str) -> AgentInstance:
        session = self.get_session(session_id)
        agent = AgentInstance(session_id=session.id, role_snapshot=session.role_snapshot)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO agents(id, session_id, status, body, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    agent.id,
                    agent.session_id,
                    agent.status.value,
                    agent.model_dump_json(),
                    agent.created_at.isoformat(),
                ),
            )
        return agent

    def get_agent(self, agent_id: str) -> AgentInstance:
        with self._connect() as connection:
            row = connection.execute("SELECT body FROM agents WHERE id = ?", (agent_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"agent not found: {agent_id}")
        return AgentInstance.model_validate_json(row["body"])

    def update_agent_status(self, agent_id: str, status: AgentStatus) -> AgentInstance:
        current = self.get_agent(agent_id)
        updated = AgentInstance.model_validate({**current.model_dump(), "status": status})
        with self._connect() as connection:
            connection.execute(
                "UPDATE agents SET status = ?, body = ? WHERE id = ?",
                (status.value, updated.model_dump_json(), agent_id),
            )
        return updated

    def append_event(self, event: Event) -> Event:
        self.get_session(event.session_id)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO events(
                    id, session_id, agent_id, event_type, body, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.session_id,
                    event.agent_id,
                    event.event_type,
                    json.dumps(event.payload, ensure_ascii=False, separators=(",", ":")),
                    event.created_at.isoformat(),
                ),
            )
        return event

    def list_events(self, session_id: str) -> list[Event]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, session_id, agent_id, event_type, body, created_at
                FROM events WHERE session_id = ? ORDER BY sequence
                """,
                (session_id,),
            ).fetchall()
        return [
            Event(
                id=row["id"],
                session_id=row["session_id"],
                agent_id=row["agent_id"],
                event_type=row["event_type"],
                payload=json.loads(row["body"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def list_roles(self, *, include_inactive: bool = False) -> list[RolePreset]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT versions.body
                FROM role_heads AS heads
                JOIN role_versions AS versions
                  ON versions.role_id = heads.id
                 AND versions.version = heads.current_version
                ORDER BY versions.created_at, versions.role_id
                """
            ).fetchall()
        roles = [RolePreset.model_validate_json(row["body"]) for row in rows]
        if include_inactive:
            return roles
        return [role for role in roles if role.status is RoleStatus.ACTIVE]

    def _validate_role_model(self, role: RolePreset) -> None:
        profile = self.get_model_profile(role.model_profile_id)
        if role.effort not in profile.supported_efforts:
            raise ValueError(f"effort {role.effort.value!r} is not supported by {profile.name!r}")
