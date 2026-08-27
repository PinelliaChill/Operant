from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from operant.domain.evaluation import (
    ArtifactWorkspace,
    EvaluationCase,
    EvaluationMetrics,
    EvaluationResult,
    EvaluationResultStatus,
    EvaluationRun,
    EvaluationRunStatus,
    EvaluationSuite,
    EvaluationSuiteStatus,
    EvaluationVariant,
    aggregate_evaluation_results,
    assert_evaluation_result_contract,
    interrupted_evaluation_failure_analysis,
)
from operant.domain.memory import Memory, MemoryKind, MemorySource, MemoryStatus
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
    utc_now,
)
from operant.domain.workflow import WorkflowRun, WorkflowRunEvent, WorkflowRunStatus


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

                CREATE TABLE IF NOT EXISTS workflow_runs (
                    id TEXT PRIMARY KEY,
                    body TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_stage TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS workflow_run_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT UNIQUE NOT NULL,
                    workflow_run_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    session_id TEXT,
                    event_type TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (workflow_run_id) REFERENCES workflow_runs(id)
                );

                CREATE INDEX IF NOT EXISTS idx_workflow_run_events_run_sequence
                    ON workflow_run_events(workflow_run_id, sequence);

                CREATE TABLE IF NOT EXISTS evaluation_suites (
                    id TEXT PRIMARY KEY,
                    body TEXT NOT NULL,
                    experiment TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS evaluation_cases (
                    id TEXT PRIMARY KEY,
                    suite_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(suite_id, ordinal),
                    FOREIGN KEY (suite_id) REFERENCES evaluation_suites(id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS evaluation_variants (
                    id TEXT PRIMARY KEY,
                    suite_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(suite_id, ordinal),
                    FOREIGN KEY (suite_id) REFERENCES evaluation_suites(id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS evaluation_runs (
                    id TEXT PRIMARY KEY,
                    suite_id TEXT NOT NULL,
                    body TEXT NOT NULL,
                    status TEXT NOT NULL,
                    execution_strategy TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (suite_id) REFERENCES evaluation_suites(id)
                );

                CREATE TABLE IF NOT EXISTS evaluation_results (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    variant_id TEXT NOT NULL,
                    repetition INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(run_id, case_id, variant_id, repetition),
                    FOREIGN KEY (run_id) REFERENCES evaluation_runs(id),
                    FOREIGN KEY (case_id) REFERENCES evaluation_cases(id),
                    FOREIGN KEY (variant_id) REFERENCES evaluation_variants(id)
                );

                CREATE INDEX IF NOT EXISTS idx_evaluation_cases_suite_ordinal
                    ON evaluation_cases(suite_id, ordinal);
                CREATE INDEX IF NOT EXISTS idx_evaluation_variants_suite_ordinal
                    ON evaluation_variants(suite_id, ordinal);
                CREATE INDEX IF NOT EXISTS idx_evaluation_runs_suite_status_created
                    ON evaluation_runs(suite_id, status, created_at);

                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    current_version INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memory_versions (
                    memory_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    body TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    project_scope TEXT,
                    role_scope TEXT NOT NULL,
                    source_session_id TEXT,
                    source_task TEXT,
                    confidence REAL NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (memory_id, version),
                    FOREIGN KEY (memory_id) REFERENCES memories(id)
                );

                CREATE INDEX IF NOT EXISTS idx_memory_versions_source_session
                    ON memory_versions(source_session_id);
                CREATE INDEX IF NOT EXISTS idx_memory_versions_project_scope
                    ON memory_versions(project_scope);

                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                    memory_id UNINDEXED,
                    version UNINDEXED,
                    content,
                    source_task,
                    project_scope
                );
                """
            )
            # Schema DDL above may commit on SQLite.  Recovery itself must be
            # atomic: a run cannot become interrupted while one of its
            # scheduled rows remains pending.
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_evaluation_result_columns(connection)
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_evaluation_results_run_status_created
                ON evaluation_results(run_id, status, created_at)
                """
            )
            self._interrupt_running_workflows(connection)
            self._interrupt_running_evaluations(connection)

    @staticmethod
    def _ensure_evaluation_result_columns(connection: sqlite3.Connection) -> None:
        """Keep initialize idempotent for databases created before result updates existed."""

        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(evaluation_results)").fetchall()
        }
        if "updated_at" not in columns:
            connection.execute("ALTER TABLE evaluation_results ADD COLUMN updated_at TEXT")
            connection.execute(
                "UPDATE evaluation_results SET updated_at = created_at WHERE updated_at IS NULL"
            )

    @staticmethod
    def _interrupt_running_workflows(connection: sqlite3.Connection) -> None:
        """Make runs left in ``running`` state safe to inspect after restart."""

        rows = connection.execute(
            "SELECT id, body FROM workflow_runs WHERE status = ?",
            (WorkflowRunStatus.RUNNING.value,),
        ).fetchall()
        if not rows:
            return
        interrupted_at = utc_now()
        for row in rows:
            workflow_run = WorkflowRun.model_validate_json(row["body"])
            updated = workflow_run.model_copy(
                update={
                    "status": WorkflowRunStatus.INTERRUPTED,
                    "updated_at": interrupted_at,
                }
            )
            connection.execute(
                """
                UPDATE workflow_runs
                SET body = ?, status = ?, current_stage = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    updated.model_dump_json(),
                    updated.status.value,
                    updated.current_stage.value,
                    updated.updated_at.isoformat(),
                    row["id"],
                ),
            )

    @staticmethod
    def _interrupt_running_evaluations(connection: sqlite3.Connection) -> None:
        """Atomically preserve unknown scheduled rows after an evaluator restart."""

        rows = connection.execute(
            "SELECT id, suite_id, body FROM evaluation_runs WHERE status = ?",
            (EvaluationRunStatus.RUNNING.value,),
        ).fetchall()
        if not rows:
            return
        interrupted_at = utc_now()
        for row in rows:
            evaluation_run = EvaluationRun.model_validate_json(row["body"])
            suite_row = connection.execute(
                "SELECT body FROM evaluation_suites WHERE id = ?", (row["suite_id"],)
            ).fetchone()
            if suite_row is None:
                raise NotFoundError(f"evaluation suite not found: {row['suite_id']}")
            suite = SQLiteStore._load_evaluation_suite(connection, suite_row["body"])
            cases = {case.id: case for case in suite.cases}
            variants = {variant.id: variant for variant in suite.variants}
            pending_rows = connection.execute(
                "SELECT id, body FROM evaluation_results WHERE run_id = ? AND status = ?",
                (evaluation_run.id, EvaluationResultStatus.PENDING.value),
            ).fetchall()
            for pending_row in pending_rows:
                pending = EvaluationResult.model_validate_json(pending_row["body"])
                artifact_workspace = pending.artifact_workspace
                if artifact_workspace is None:
                    # Pre-v1 recovery rows did not reserve the namespace.  A
                    # deterministic ref preserves a safe identifier without
                    # pretending that an artifact was actually captured.
                    artifact_workspace = ArtifactWorkspace(
                        artifact_ref=(
                            f"evaluation/{pending.run_id}/{pending.case_id}/"
                            f"{pending.variant_id}/{pending.repetition}"
                        )
                    )
                interrupted = EvaluationResult.model_validate(
                    {
                        **pending.model_dump(),
                        "status": EvaluationResultStatus.INTERRUPTED,
                        "metrics": EvaluationMetrics(),
                        "changed_paths": (),
                        "snapshot": None,
                        "artifact_workspace": artifact_workspace,
                        "verification": (),
                        "execution_facts": (),
                        "failure_analysis": interrupted_evaluation_failure_analysis(),
                        "trace_session_ids": (),
                        "trace_workflow_run_id": None,
                        "updated_at": interrupted_at,
                        "finished_at": interrupted_at,
                    }
                )
                case = cases.get(interrupted.case_id)
                variant = variants.get(interrupted.variant_id)
                if case is None or variant is None:
                    raise NotFoundError("evaluation result references a missing case or variant")
                assert_evaluation_result_contract(case, variant, interrupted)
                connection.execute(
                    """
                    UPDATE evaluation_results
                    SET status = ?, body = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        interrupted.status.value,
                        interrupted.model_dump_json(),
                        interrupted.updated_at.isoformat(),
                        pending_row["id"],
                    ),
                )

            result_rows = connection.execute(
                "SELECT body FROM evaluation_results WHERE run_id = ?",
                (evaluation_run.id,),
            ).fetchall()
            results = tuple(
                EvaluationResult.model_validate_json(result_row["body"])
                for result_row in result_rows
            )
            updated = evaluation_run.model_copy(
                update={
                    "status": EvaluationRunStatus.INTERRUPTED,
                    "finished_at": interrupted_at,
                    "updated_at": interrupted_at,
                    "last_error_type": "process_interrupted",
                    "aggregate": aggregate_evaluation_results(
                        results,
                        suite_id=suite.id,
                        run_id=evaluation_run.id,
                        expected_result_count=suite.expanded_result_count,
                    ),
                }
            )
            connection.execute(
                """
                UPDATE evaluation_runs
                SET body = ?, status = ?, execution_strategy = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    updated.model_dump_json(),
                    updated.status.value,
                    updated.execution_strategy.value,
                    updated.updated_at.isoformat(),
                    row["id"],
                ),
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

    # Workflow persistence

    def create_workflow_run(self, workflow_run: WorkflowRun) -> WorkflowRun:
        with self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO workflow_runs(
                        id, body, status, current_stage, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        workflow_run.id,
                        workflow_run.model_dump_json(),
                        workflow_run.status.value,
                        workflow_run.current_stage.value,
                        workflow_run.created_at.isoformat(),
                        workflow_run.updated_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"workflow run already exists: {workflow_run.id}") from exc
        return workflow_run

    def get_workflow_run(self, workflow_run_id: str) -> WorkflowRun:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT body FROM workflow_runs WHERE id = ?", (workflow_run_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"workflow run not found: {workflow_run_id}")
        return WorkflowRun.model_validate_json(row["body"])

    def list_workflow_runs(
        self,
        *,
        status: WorkflowRunStatus | str | None = None,
        limit: int | None = None,
    ) -> list[WorkflowRun]:
        parameters: list[Any] = []
        conditions: list[str] = []
        if status is not None:
            normalized_status = (
                status if isinstance(status, WorkflowRunStatus) else WorkflowRunStatus(status)
            )
            conditions.append("status = ?")
            parameters.append(normalized_status.value)
        query = "SELECT body FROM workflow_runs"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at, id"
        if limit is not None:
            if limit < 1:
                raise ValueError("workflow run list limit must be positive")
            query += " LIMIT ?"
            parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [WorkflowRun.model_validate_json(row["body"]) for row in rows]

    def update_workflow_run(self, workflow_run_id: str, **changes: Any) -> WorkflowRun:
        forbidden = {"id", "created_at"}
        attempted = forbidden.intersection(changes)
        if attempted:
            raise ValueError(f"cannot change workflow run identity fields: {sorted(attempted)}")
        current = self.get_workflow_run(workflow_run_id)
        updated_data = current.model_dump()
        updated_data.update(changes)
        updated_data["updated_at"] = utc_now()
        updated = WorkflowRun.model_validate(updated_data)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE workflow_runs
                SET body = ?, status = ?, current_stage = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    updated.model_dump_json(),
                    updated.status.value,
                    updated.current_stage.value,
                    updated.updated_at.isoformat(),
                    workflow_run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise NotFoundError(f"workflow run not found: {workflow_run_id}")
        return updated

    def append_workflow_event(self, event: WorkflowRunEvent) -> WorkflowRunEvent:
        self.get_workflow_run(event.workflow_run_id)
        with self._connect() as connection:
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO workflow_run_events(
                        id, workflow_run_id, role, session_id, event_type, body, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.id,
                        event.workflow_run_id,
                        event.role,
                        event.session_id,
                        event.event_type,
                        json.dumps(event.payload, ensure_ascii=False, separators=(",", ":")),
                        event.created_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"workflow event already exists: {event.id}") from exc
            if cursor.lastrowid is None:
                raise RuntimeError("SQLite did not return a workflow event sequence")
            sequence = cursor.lastrowid
        return event.model_copy(update={"sequence": sequence})

    def list_workflow_events(self, workflow_run_id: str) -> list[WorkflowRunEvent]:
        self.get_workflow_run(workflow_run_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT sequence, id, workflow_run_id, role, session_id,
                       event_type, body, created_at
                FROM workflow_run_events
                WHERE workflow_run_id = ?
                ORDER BY sequence
                """,
                (workflow_run_id,),
            ).fetchall()
        return [
            WorkflowRunEvent(
                id=row["id"],
                workflow_run_id=row["workflow_run_id"],
                sequence=int(row["sequence"]),
                role=row["role"],
                session_id=row["session_id"],
                event_type=row["event_type"],
                payload=json.loads(row["body"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    # Explicit aliases keep the workflow-level API discoverable to callers
    # that use "workflow" rather than the persisted "workflow run" name.

    def create_workflow(self, workflow_run: WorkflowRun) -> WorkflowRun:
        return self.create_workflow_run(workflow_run)

    def get_workflow(self, workflow_run_id: str) -> WorkflowRun:
        return self.get_workflow_run(workflow_run_id)

    def list_workflows(
        self,
        *,
        status: WorkflowRunStatus | str | None = None,
        limit: int | None = None,
    ) -> list[WorkflowRun]:
        return self.list_workflow_runs(status=status, limit=limit)

    def update_workflow(self, workflow_run_id: str, **changes: Any) -> WorkflowRun:
        return self.update_workflow_run(workflow_run_id, **changes)

    def append_workflow_run_event(self, event: WorkflowRunEvent) -> WorkflowRunEvent:
        return self.append_workflow_event(event)

    def list_workflow_run_events(self, workflow_run_id: str) -> list[WorkflowRunEvent]:
        return self.list_workflow_events(workflow_run_id)

    # Evaluation persistence

    def create_evaluation_suite(self, suite: EvaluationSuite) -> EvaluationSuite:
        """Persist an immutable suite and its ordered cases/variants atomically."""

        with self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO evaluation_suites(id, body, experiment, status, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        suite.id,
                        suite.model_dump_json(),
                        suite.experiment.value if suite.experiment is not None else None,
                        suite.status.value,
                        suite.created_at.isoformat(),
                    ),
                )
                for ordinal, case in enumerate(suite.cases, start=1):
                    connection.execute(
                        """
                        INSERT INTO evaluation_cases(id, suite_id, ordinal, body, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            case.id,
                            suite.id,
                            ordinal,
                            case.model_dump_json(),
                            suite.created_at.isoformat(),
                        ),
                    )
                for ordinal, variant in enumerate(suite.variants, start=1):
                    connection.execute(
                        """
                        INSERT INTO evaluation_variants(id, suite_id, ordinal, body, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            variant.id,
                            suite.id,
                            ordinal,
                            variant.model_dump_json(),
                            suite.created_at.isoformat(),
                        ),
                    )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(
                    f"evaluation suite or child already exists: {suite.id}"
                ) from exc
        return suite

    def get_evaluation_suite(self, suite_id: str) -> EvaluationSuite:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT body FROM evaluation_suites WHERE id = ?", (suite_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"evaluation suite not found: {suite_id}")
            return self._load_evaluation_suite(connection, row["body"])

    def list_evaluation_suites(
        self,
        *,
        status: EvaluationSuiteStatus | str | None = None,
        limit: int | None = None,
    ) -> list[EvaluationSuite]:
        parameters: list[Any] = []
        query = "SELECT body FROM evaluation_suites"
        if status is not None:
            normalized_status = (
                status
                if isinstance(status, EvaluationSuiteStatus)
                else EvaluationSuiteStatus(status)
            )
            query += " WHERE status = ?"
            parameters.append(normalized_status.value)
        query += " ORDER BY created_at, id"
        if limit is not None:
            if limit < 1:
                raise ValueError("evaluation suite list limit must be positive")
            query += " LIMIT ?"
            parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
            return [self._load_evaluation_suite(connection, row["body"]) for row in rows]

    @staticmethod
    def _load_evaluation_suite(
        connection: sqlite3.Connection,
        serialized_suite: str,
    ) -> EvaluationSuite:
        data = json.loads(serialized_suite)
        suite_id = data["id"]
        case_rows = connection.execute(
            "SELECT body FROM evaluation_cases WHERE suite_id = ? ORDER BY ordinal", (suite_id,)
        ).fetchall()
        variant_rows = connection.execute(
            "SELECT body FROM evaluation_variants WHERE suite_id = ? ORDER BY ordinal", (suite_id,)
        ).fetchall()
        data["cases"] = [json.loads(row["body"]) for row in case_rows]
        data["variants"] = [json.loads(row["body"]) for row in variant_rows]
        return EvaluationSuite.model_validate(data)

    def create_evaluation_run(self, evaluation_run: EvaluationRun) -> EvaluationRun:
        self.get_evaluation_suite(evaluation_run.suite_id)
        with self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO evaluation_runs(
                        id, suite_id, body, status, execution_strategy, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        evaluation_run.id,
                        evaluation_run.suite_id,
                        evaluation_run.model_dump_json(),
                        evaluation_run.status.value,
                        evaluation_run.execution_strategy.value,
                        evaluation_run.created_at.isoformat(),
                        evaluation_run.updated_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"evaluation run already exists: {evaluation_run.id}") from exc
        return evaluation_run

    def get_evaluation_run(self, evaluation_run_id: str) -> EvaluationRun:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT body FROM evaluation_runs WHERE id = ?", (evaluation_run_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"evaluation run not found: {evaluation_run_id}")
        return EvaluationRun.model_validate_json(row["body"])

    def list_evaluation_runs(
        self,
        *,
        suite_id: str | None = None,
        status: EvaluationRunStatus | str | None = None,
        limit: int | None = None,
    ) -> list[EvaluationRun]:
        conditions: list[str] = []
        parameters: list[Any] = []
        if suite_id is not None:
            conditions.append("suite_id = ?")
            parameters.append(suite_id)
        if status is not None:
            normalized_status = (
                status if isinstance(status, EvaluationRunStatus) else EvaluationRunStatus(status)
            )
            conditions.append("status = ?")
            parameters.append(normalized_status.value)
        query = "SELECT body FROM evaluation_runs"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at, id"
        if limit is not None:
            if limit < 1:
                raise ValueError("evaluation run list limit must be positive")
            query += " LIMIT ?"
            parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [EvaluationRun.model_validate_json(row["body"]) for row in rows]

    def update_evaluation_run(self, evaluation_run_id: str, **changes: Any) -> EvaluationRun:
        forbidden = {"id", "suite_id", "created_at"}
        attempted = forbidden.intersection(changes)
        if attempted:
            raise ValueError(f"cannot change evaluation run identity fields: {sorted(attempted)}")
        current = self.get_evaluation_run(evaluation_run_id)
        updated_data = current.model_dump()
        updated_data.update(changes)
        updated_data["updated_at"] = utc_now()
        updated = EvaluationRun.model_validate(updated_data)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE evaluation_runs
                SET body = ?, status = ?, execution_strategy = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    updated.model_dump_json(),
                    updated.status.value,
                    updated.execution_strategy.value,
                    updated.updated_at.isoformat(),
                    evaluation_run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise NotFoundError(f"evaluation run not found: {evaluation_run_id}")
        return updated

    def append_evaluation_result(self, result: EvaluationResult) -> EvaluationResult:
        """Create one unique scheduled result after proving its suite membership."""

        with self._connect() as connection:
            run_row = connection.execute(
                "SELECT suite_id FROM evaluation_runs WHERE id = ?", (result.run_id,)
            ).fetchone()
            if run_row is None:
                raise NotFoundError(f"evaluation run not found: {result.run_id}")
            suite_id = run_row["suite_id"]
            suite_row = connection.execute(
                "SELECT body FROM evaluation_suites WHERE id = ?", (suite_id,)
            ).fetchone()
            if suite_row is None:
                raise NotFoundError(f"evaluation suite not found: {suite_id}")
            suite = self._load_evaluation_suite(connection, suite_row["body"])
            if result.repetition > suite.repetitions:
                raise ValueError(
                    "evaluation result repetition exceeds the evaluation suite repetition count"
                )
            case_row = connection.execute(
                "SELECT body FROM evaluation_cases WHERE id = ? AND suite_id = ?",
                (result.case_id, suite_id),
            ).fetchone()
            if case_row is None:
                raise NotFoundError(f"evaluation case not found in suite: {result.case_id}")
            variant_row = connection.execute(
                "SELECT body FROM evaluation_variants WHERE id = ? AND suite_id = ?",
                (result.variant_id, suite_id),
            ).fetchone()
            if variant_row is None:
                raise NotFoundError(f"evaluation variant not found in suite: {result.variant_id}")
            case = EvaluationCase.model_validate_json(case_row["body"])
            variant = EvaluationVariant.model_validate_json(variant_row["body"])
            assert_evaluation_result_contract(case, variant, result)
            try:
                connection.execute(
                    """
                    INSERT INTO evaluation_results(
                        id, run_id, case_id, variant_id, repetition,
                        status, body, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        result.id,
                        result.run_id,
                        result.case_id,
                        result.variant_id,
                        result.repetition,
                        result.status.value,
                        result.model_dump_json(),
                        result.created_at.isoformat(),
                        result.updated_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(
                    "evaluation result already exists for this run, case, variant, and repetition"
                ) from exc
        return result

    def get_evaluation_result(self, result_id: str) -> EvaluationResult:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT body FROM evaluation_results WHERE id = ?", (result_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"evaluation result not found: {result_id}")
        return EvaluationResult.model_validate_json(row["body"])

    def update_evaluation_result(self, result_id: str, **changes: Any) -> EvaluationResult:
        """Atomically advance one persisted result from pending to its terminal fact."""

        forbidden = {
            "id",
            "run_id",
            "case_id",
            "variant_id",
            "repetition",
            "created_at",
            "updated_at",
        }
        terminal_statuses = {
            EvaluationResultStatus.PASSED,
            EvaluationResultStatus.FAILED,
            EvaluationResultStatus.ERROR,
            EvaluationResultStatus.SKIPPED,
            EvaluationResultStatus.INTERRUPTED,
        }

        with self._connect() as connection:
            # Keep the persisted body/status/updated-at tuple coherent even if
            # two in-process runners happen to finish the same result together.
            connection.execute("BEGIN IMMEDIATE")
            result_row = connection.execute(
                "SELECT body FROM evaluation_results WHERE id = ?", (result_id,)
            ).fetchone()
            if result_row is None:
                raise NotFoundError(f"evaluation result not found: {result_id}")
            current = EvaluationResult.model_validate_json(result_row["body"])
            if current.status is not EvaluationResultStatus.PENDING:
                raise ValueError(
                    "evaluation results may only be updated once from pending to a terminal status"
                )
            attempted = forbidden.intersection(changes)
            if attempted:
                raise ValueError(
                    f"cannot change evaluation result identity fields: {sorted(attempted)}"
                )
            updated_data = current.model_dump()
            updated_data.update(changes)
            updated_data["updated_at"] = utc_now()
            updated = EvaluationResult.model_validate(updated_data)
            if updated.status not in terminal_statuses:
                raise ValueError(
                    "evaluation results must transition from pending to a terminal status"
                )
            run_row = connection.execute(
                "SELECT suite_id FROM evaluation_runs WHERE id = ?", (updated.run_id,)
            ).fetchone()
            if run_row is None:
                raise NotFoundError(f"evaluation run not found: {updated.run_id}")
            suite_row = connection.execute(
                "SELECT body FROM evaluation_suites WHERE id = ?", (run_row["suite_id"],)
            ).fetchone()
            if suite_row is None:
                raise NotFoundError(f"evaluation suite not found: {run_row['suite_id']}")
            suite = self._load_evaluation_suite(connection, suite_row["body"])
            if updated.repetition > suite.repetitions:
                raise ValueError(
                    "evaluation result repetition exceeds the evaluation suite repetition count"
                )
            case_row = connection.execute(
                "SELECT body FROM evaluation_cases WHERE id = ? AND suite_id = ?",
                (updated.case_id, suite.id),
            ).fetchone()
            variant_row = connection.execute(
                "SELECT body FROM evaluation_variants WHERE id = ? AND suite_id = ?",
                (updated.variant_id, suite.id),
            ).fetchone()
            if case_row is None or variant_row is None:
                raise NotFoundError("evaluation result references a missing case or variant")
            if updated.status in terminal_statuses:
                assert_evaluation_result_contract(
                    EvaluationCase.model_validate_json(case_row["body"]),
                    EvaluationVariant.model_validate_json(variant_row["body"]),
                    updated,
                )
            cursor = connection.execute(
                """
                UPDATE evaluation_results
                SET status = ?, body = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    updated.status.value,
                    updated.model_dump_json(),
                    updated.updated_at.isoformat(),
                    result_id,
                ),
            )
            if cursor.rowcount != 1:
                raise NotFoundError(f"evaluation result not found: {result_id}")
        return updated

    def list_evaluation_results(self, evaluation_run_id: str) -> list[EvaluationResult]:
        self.get_evaluation_run(evaluation_run_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT body FROM evaluation_results
                WHERE run_id = ?
                ORDER BY case_id, variant_id, repetition, created_at, id
                """,
                (evaluation_run_id,),
            ).fetchall()
        return [EvaluationResult.model_validate_json(row["body"]) for row in rows]

    # Memory Store

    def create_memory(self, memory: Memory) -> Memory:
        """Persist the first version of a Memory and add it to the FTS index."""

        if memory.version != 1:
            raise ValueError("a new memory must start at version 1")
        with self._connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO memories(id, current_version, created_at) VALUES (?, ?, ?)",
                    (memory.id, memory.version, memory.created_at.isoformat()),
                )
                self._insert_memory_version(connection, memory)
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"memory already exists: {memory.id}") from exc
        return memory

    def get_memory(self, memory_id: str, version: int | None = None) -> Memory:
        with self._connect() as connection:
            if version is None:
                head = connection.execute(
                    "SELECT current_version FROM memories WHERE id = ?", (memory_id,)
                ).fetchone()
                if head is None:
                    raise NotFoundError(f"memory not found: {memory_id}")
                version = int(head["current_version"])
            row = connection.execute(
                """
                SELECT body FROM memory_versions
                WHERE memory_id = ? AND version = ?
                """,
                (memory_id, version),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"memory version not found: {memory_id}@{version}")
        return Memory.model_validate_json(row["body"])

    def list_memory_versions(self, memory_id: str) -> list[Memory]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT body FROM memory_versions
                WHERE memory_id = ? ORDER BY version
                """,
                (memory_id,),
            ).fetchall()
        if not rows:
            raise NotFoundError(f"memory not found: {memory_id}")
        return [Memory.model_validate_json(row["body"]) for row in rows]

    def update_memory(self, memory_id: str, **changes: Any) -> Memory:
        """Create a new immutable version of a Memory.

        Identity and version metadata are controlled by the store.  All other
        fields are validated by the domain model before the transaction is
        committed.
        """

        forbidden = {"id", "version", "created_at"}
        attempted = forbidden.intersection(changes)
        if attempted:
            raise ValueError(f"cannot change memory identity fields: {sorted(attempted)}")
        current = self.get_memory(memory_id)
        updated_data = current.model_dump()
        updated_data.update(changes)
        updated_data["version"] = current.version + 1
        updated_data["created_at"] = utc_now()
        updated = Memory.model_validate(updated_data)
        with self._connect() as connection:
            head = connection.execute(
                "SELECT current_version FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
            if head is None:
                raise NotFoundError(f"memory not found: {memory_id}")
            if int(head["current_version"]) != current.version:
                raise ConflictError(f"memory changed while updating: {memory_id}")
            self._insert_memory_version(connection, updated)
            connection.execute(
                "UPDATE memories SET current_version = ? WHERE id = ?",
                (updated.version, memory_id),
            )
        return updated

    def deactivate_memory(self, memory_id: str) -> Memory:
        return self.update_memory(memory_id, status=MemoryStatus.INACTIVE)

    def trace_memory_source(self, memory_id: str, version: int | None = None) -> MemorySource:
        return self.get_memory(memory_id, version).source

    def list_memories_by_source(
        self,
        source_session_id: str,
        *,
        source_task: str | None = None,
        include_inactive: bool = False,
    ) -> list[Memory]:
        conditions = [
            "v.source_session_id = ?",
            "h.current_version = v.version",
        ]
        parameters: list[Any] = [source_session_id]
        if source_task is not None:
            conditions.append("v.source_task = ?")
            parameters.append(source_task)
        if not include_inactive:
            conditions.append("v.status != ?")
            parameters.append(MemoryStatus.INACTIVE.value)
        query = (
            """
            SELECT v.body
            FROM memories AS h
            JOIN memory_versions AS v ON v.memory_id = h.id
            WHERE """
            + " AND ".join(conditions)
            + " ORDER BY v.created_at, v.memory_id"
        )
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [Memory.model_validate_json(row["body"]) for row in rows]

    def search_memories(
        self,
        query: str,
        *,
        project_scope: str | None = None,
        source_session_id: str | None = None,
        kinds: Collection[MemoryKind | str] | None = None,
        role_id: str | None = None,
        role_name: str | None = None,
        include_candidates: bool = False,
        limit: int = 20,
    ) -> list[Memory]:
        """Search current, accessible versions through SQLite FTS5.

        Scope-sensitive working and project filtering is intentionally applied
        in SQL before the small role-scope post-filter.  Historical versions
        remain in the FTS table for source/version inspection but the head join
        ensures normal queries only return the current version.
        """

        if not 1 <= limit <= 100:
            raise ValueError("memory search limit must be between 1 and 100")
        normalized_kinds: tuple[str, ...] = ()
        if kinds is not None:
            normalized_kinds = tuple(
                kind.value if isinstance(kind, MemoryKind) else MemoryKind(kind).value
                for kind in kinds
            )
            if not normalized_kinds:
                return []

        conditions = [
            "h.current_version = v.version",
            "fts.memory_id = v.memory_id",
            "fts.version = v.version",
        ]
        parameters: list[Any] = []
        if normalized_kinds:
            placeholders = ", ".join("?" for _ in normalized_kinds)
            conditions.append(f"v.kind IN ({placeholders})")
            parameters.extend(normalized_kinds)
        if include_candidates:
            conditions.append("v.status IN (?, ?)")
            parameters.extend([MemoryStatus.ACTIVE.value, MemoryStatus.CANDIDATE.value])
        else:
            conditions.append("v.status = ?")
            parameters.append(MemoryStatus.ACTIVE.value)

        # Project knowledge must never leak across project boundaries.  A
        # missing project scope therefore excludes project items rather than
        # treating them as globally visible.
        if project_scope is None:
            conditions.append("v.kind != ?")
            parameters.append(MemoryKind.PROJECT.value)
        else:
            conditions.append("(v.kind != ? OR v.project_scope = ?)")
            parameters.extend([MemoryKind.PROJECT.value, project_scope])

        # Working memory is always bound to one source session.
        if source_session_id is None:
            conditions.append("v.kind != ?")
            parameters.append(MemoryKind.WORKING.value)
        else:
            conditions.append("(v.kind != ? OR v.source_session_id = ?)")
            parameters.extend([MemoryKind.WORKING.value, source_session_id])

        fts_query = self._build_fts_query(query)
        match_clause = ""
        if fts_query:
            match_clause = " AND memory_fts MATCH ?"
            parameters.append(fts_query)
        sql = (
            "SELECT v.body, bm25(memory_fts) AS rank "
            "FROM memory_fts AS fts "
            "JOIN memories AS h ON h.id = fts.memory_id "
            "JOIN memory_versions AS v ON v.memory_id = fts.memory_id "
            "WHERE "
            + " AND ".join(conditions)
            + match_clause
            + " ORDER BY rank, v.created_at DESC LIMIT ?"
        )
        parameters.append(min(limit * 10, 1000))

        try:
            with self._connect() as connection:
                rows = connection.execute(sql, parameters).fetchall()
        except sqlite3.OperationalError as exc:
            raise ValueError("invalid memory full-text query") from exc

        memories: list[Memory] = []
        for row in rows:
            memory = Memory.model_validate_json(row["body"])
            if memory.role_scope and not self._role_scope_matches(
                memory,
                role_id=role_id,
                role_name=role_name,
            ):
                continue
            memories.append(memory)
            if len(memories) >= limit:
                break
        return memories

    @staticmethod
    def _insert_memory_version(connection: sqlite3.Connection, memory: Memory) -> None:
        connection.execute(
            """
            INSERT INTO memory_versions(
                memory_id, version, body, kind, content, project_scope,
                role_scope, source_session_id, source_task, confidence,
                status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                memory.id,
                memory.version,
                memory.model_dump_json(),
                memory.kind.value,
                memory.content,
                memory.project_scope,
                json.dumps(list(memory.role_scope), ensure_ascii=False, separators=(",", ":")),
                memory.source_session_id,
                memory.source_task,
                memory.confidence,
                memory.status.value,
                memory.created_at.isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO memory_fts(memory_id, version, content, source_task, project_scope)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                memory.id,
                memory.version,
                memory.content,
                memory.source_task or "",
                memory.project_scope or "",
            ),
        )

    @staticmethod
    def _build_fts_query(value: str) -> str:
        tokens = re.findall(r"[^\W_]+", value, flags=re.UNICODE)
        parts: list[str] = []
        for token in tokens:
            escaped = token.replace('"', '""')
            parts.append(f'"{escaped}"')
        return " AND ".join(parts)

    @staticmethod
    def _role_scope_matches(
        memory: Memory,
        *,
        role_id: str | None,
        role_name: str | None,
    ) -> bool:
        return bool(
            set(memory.role_scope).intersection(
                candidate for candidate in (role_id, role_name) if candidate is not None
            )
            or "*" in memory.role_scope
        )

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
