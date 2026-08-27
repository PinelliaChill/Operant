"""Immutable, secret-safe specifications and facts for Evaluation Runner v1.

The runner itself deliberately lives outside this module.  These objects are
the durable inputs and outputs needed to compare a fixed session or a fixed
workflow without turning missing telemetry into a reassuring-looking zero.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from datetime import datetime, timezone
from enum import Enum
from fnmatch import fnmatchcase
from uuid import uuid4

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from operant.domain.models import Effort

MAX_EVALUATION_CASES = 100
MAX_EVALUATION_VARIANTS = 32
MAX_EVALUATION_REPETITIONS = 20
MAX_EVALUATION_EXPANDED_RESULTS = 1_000
MAX_VERIFICATION_COMMANDS = 12


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


_SECRET_VALUE = re.compile(
    r"(?ix)(?:"
    r"\b(?:api[_-]?key|access[_-]?token|token|secret|password|passwd|authorization)\b\s*[:=]\s*\S+"
    r"|\bbearer\s+[a-z0-9._~+\-/=]{8,}"
    r"|\bsk-[a-z0-9_-]{16,}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r")"
)
_URL = re.compile(r"(?i)\b(?:https?|ftp)://")
_SHELL_META = re.compile(r"[|;&<>`]|\$\(|\$\{")
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]+$")


def _ensure_secret_safe(value: str, field: str) -> str:
    """Reject values that look like credential material rather than metadata."""

    if _SECRET_VALUE.search(value):
        raise ValueError(f"{field} must not contain credential material")
    return value


def _normalize_text(value: str, field: str, *, required: bool = True) -> str:
    normalized = value.strip()
    if required and not normalized:
        raise ValueError(f"{field} must not be blank")
    return _ensure_secret_safe(normalized, field)


def _validate_identifier(value: str, field: str) -> str:
    normalized = _normalize_text(value, field)
    if not _IDENTIFIER.fullmatch(normalized):
        raise ValueError(f"{field} contains unsupported characters")
    return normalized


def _validate_relative_path(value: str, field: str, *, allow_glob: bool = False) -> str:
    normalized = _normalize_text(value, field)
    if "\\" in normalized or normalized.startswith(("/", "~")):
        raise ValueError(f"{field} must be a relative POSIX path")
    if any(part == ".." for part in normalized.split("/")):
        raise ValueError(f"{field} must not escape the evaluation workspace")
    if normalized in {".git", ".env", ".env.local", "secrets.json"} or normalized.startswith(
        (".git/", ".env/", ".env.local/", "secrets.json/")
    ):
        raise ValueError(f"{field} must not target repository metadata or secret files")
    if not allow_glob and any(token in normalized for token in "*?[]"):
        raise ValueError(f"{field} must not contain a glob")
    return normalized


def _unique_texts(values: tuple[str, ...], field: str) -> tuple[str, ...]:
    if len(values) != len(set(values)):
        raise ValueError(f"{field} must not contain duplicates")
    return values


class EvaluationExperiment(str, Enum):
    """The six Week 4 experiments supported by Evaluation Runner v1."""

    EXP_19 = "exp_19_single_vs_multi_model"
    EXP_20 = "exp_20_model_ablation"
    EXP_21 = "exp_21_role_prompt_ablation"
    EXP_22 = "exp_22_effort_ablation"
    EXP_23 = "exp_23_memory_ablation"
    EXP_24 = "exp_24_trace_failure_analysis"


class EvaluationSuiteStatus(str, Enum):
    DRAFT = "draft"
    READY = "ready"
    ARCHIVED = "archived"


class EvaluationVariantKind(str, Enum):
    SESSION = "session"
    WORKFLOW = "workflow"


class EvaluationRunStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EvaluationResultStatus(str, Enum):
    PENDING = "pending"
    # The evaluator scheduled this row, but its host stopped before an outcome
    # could be committed.  It is deliberately distinct from a known failure.
    INTERRUPTED = "interrupted"
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


class FailureCategory(str, Enum):
    MODEL = "model"
    PROMPT_PROTOCOL = "prompt_protocol"
    TOOL_CONTEXT = "tool_context"
    ENVIRONMENT = "environment"
    ORCHESTRATION = "orchestration"
    UNKNOWN = "unknown"


class ExecutionRunner(str, Enum):
    HOST = "host"
    DOCKER = "docker"


class EvaluationExecutionStrategy(str, Enum):
    """v1 intentionally expands results one at a time for reproducibility."""

    SEQUENTIAL = "sequential"


class ModelPricing(BaseModel):
    """Known per-million-token prices for one exact model, or ``None`` when unknown."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model_profile_id: str = Field(min_length=1, max_length=200)
    model_id: str = Field(min_length=1, max_length=200)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    input_usd_per_million_tokens: float | None = Field(default=None, ge=0)
    output_usd_per_million_tokens: float | None = Field(default=None, ge=0)
    reasoning_usd_per_million_tokens: float | None = Field(default=None, ge=0)
    effective_from: datetime | None = None

    @field_validator("model_profile_id", "model_id")
    @classmethod
    def validate_model_identity(cls, value: str) -> str:
        return _validate_identifier(value, "model pricing identity")

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        normalized = _normalize_text(value, "currency").upper()
        if not normalized.isalpha():
            raise ValueError("currency must contain only letters")
        return normalized


class ModelSnapshot(BaseModel):
    """Exact model selection with no endpoint credential or credential value."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model_profile_id: str = Field(min_length=1, max_length=200)
    provider: str = Field(min_length=1, max_length=100)
    model_id: str = Field(min_length=1, max_length=200)
    effort: Effort
    context_window: int | None = Field(default=None, ge=1)
    pricing: ModelPricing | None = None

    @field_validator("model_profile_id", "provider", "model_id")
    @classmethod
    def validate_identity(cls, value: str) -> str:
        return _validate_identifier(value, "model snapshot identity")

    @model_validator(mode="after")
    def validate_pricing_identity(self) -> ModelSnapshot:
        if self.pricing is not None and (
            self.pricing.model_profile_id != self.model_profile_id
            or self.pricing.model_id != self.model_id
        ):
            raise ValueError("pricing must identify the same model as the model snapshot")
        return self


class PromptSnapshot(BaseModel):
    """Versioned prompt text.  Prompts containing credential-shaped values are rejected."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    id: str = Field(default_factory=lambda: _new_id("prompt"), min_length=1, max_length=200)
    version: int = Field(default=1, ge=1)
    content: str = Field(
        min_length=1,
        max_length=100_000,
        validation_alias=AliasChoices("content", "prompt", "system_prompt"),
    )
    content_sha256: str | None = Field(default=None, min_length=64, max_length=64)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _validate_identifier(value, "prompt id")

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        # Prompt whitespace is execution-relevant.  Verify that it is not
        # blank without normalizing it, then hash exactly the bytes the role
        # will receive.
        if not value.strip():
            raise ValueError("prompt content must not be blank")
        return _ensure_secret_safe(value, "prompt content")

    @field_validator("content_sha256")
    @classmethod
    def validate_digest(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not re.fullmatch(r"[0-9a-f]{64}", value.lower()):
            raise ValueError("content_sha256 must be a SHA-256 hex digest")
        return value.lower()

    @model_validator(mode="after")
    def ensure_content_digest(self) -> PromptSnapshot:
        calculated = hashlib.sha256(self.content.encode("utf-8")).hexdigest()
        if self.content_sha256 is None:
            object.__setattr__(self, "content_sha256", calculated)
            return self
        if self.content_sha256 != calculated:
            raise ValueError("content_sha256 must match the prompt content")
        return self


class EvaluationRoleSnapshot(BaseModel):
    """Evaluation-safe projection of a RoleSnapshot.

    It intentionally records the selected model and prompt but omits ``base_url``
    and ``secret_ref`` because neither is part of a benchmark result.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    role_id: str = Field(min_length=1, max_length=200)
    role_version: int = Field(ge=1)
    role_name: str = Field(min_length=1, max_length=200)
    prompt: PromptSnapshot
    model: ModelSnapshot
    tool_policy_fingerprint: str = Field(min_length=1, max_length=128)
    memory_scope: str = Field(default="session", min_length=1, max_length=200)
    max_turns: int | None = Field(default=None, ge=1, le=100)
    timeout_seconds: int | None = Field(default=None, ge=1, le=3_600)

    @field_validator("role_id", "tool_policy_fingerprint")
    @classmethod
    def validate_role_identifier(cls, value: str) -> str:
        return _validate_identifier(value, "role snapshot identity")

    @field_validator("role_name", "memory_scope")
    @classmethod
    def validate_role_text(cls, value: str) -> str:
        return _normalize_text(value, "role snapshot text")


class MemoryReference(BaseModel):
    """Immutable memory reference; versioned storage provides reproducibility."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    memory_id: str = Field(min_length=1, max_length=200)
    version: int = Field(ge=1)
    content_sha256: str | None = Field(default=None, min_length=64, max_length=64)

    @field_validator("memory_id")
    @classmethod
    def validate_memory_id(cls, value: str) -> str:
        return _validate_identifier(value, "memory id")

    @field_validator("content_sha256")
    @classmethod
    def validate_digest(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not re.fullmatch(r"[0-9a-f]{64}", value.lower()):
            raise ValueError("content_sha256 must be a SHA-256 hex digest")
        return value.lower()


class MemorySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    references: tuple[MemoryReference, ...] = ()

    @model_validator(mode="after")
    def validate_enabled_shape(self) -> MemorySnapshot:
        ids = tuple(reference.memory_id for reference in self.references)
        _unique_texts(ids, "memory references")
        if not self.enabled and self.references:
            raise ValueError("disabled memory must not carry memory references")
        return self


class EnvironmentFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=1_000)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _validate_identifier(value, "environment fact name")

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: str) -> str:
        return _normalize_text(value, "environment fact value")


class EnvironmentSnapshot(BaseModel):
    """Replay-relevant environment metadata, containing no environment values."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fixture_ref: str = Field(min_length=1, max_length=500)
    source_revision: str | None = Field(default=None, max_length=200)
    fixture_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    container_image: str | None = Field(default=None, max_length=300)
    dependency_lock_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    facts: tuple[EnvironmentFact, ...] = ()

    @field_validator("fixture_ref", "source_revision", "container_image")
    @classmethod
    def validate_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalize_text(value, "environment snapshot text")

    @field_validator("fixture_sha256", "dependency_lock_sha256")
    @classmethod
    def validate_digest(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not re.fullmatch(r"[0-9a-f]{64}", value.lower()):
            raise ValueError("environment digest must be a SHA-256 hex digest")
        return value.lower()

    @model_validator(mode="after")
    def validate_fact_names(self) -> EnvironmentSnapshot:
        _unique_texts(tuple(fact.name for fact in self.facts), "environment fact names")
        return self


class ExecutionSnapshot(BaseModel):
    """Execution policy facts that are sufficient to reproduce a run boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    runner: ExecutionRunner = ExecutionRunner.DOCKER
    docker_image: str | None = Field(default="python:3.13-slim", max_length=300)
    cpu_limit: float | None = Field(default=None, gt=0, le=64)
    memory_limit_mb: int | None = Field(default=None, ge=64, le=262_144)
    pids_limit: int | None = Field(default=None, ge=16, le=65_536)
    seed: int | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    timeout_seconds: int = Field(default=300, ge=1, le=3_600)

    @field_validator("docker_image")
    @classmethod
    def validate_image(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalize_text(value, "docker image")

    @model_validator(mode="after")
    def validate_runner_shape(self) -> ExecutionSnapshot:
        if self.runner is ExecutionRunner.HOST and self.docker_image is not None:
            raise ValueError("host execution must not declare a docker image")
        if self.runner is ExecutionRunner.DOCKER and not self.docker_image:
            raise ValueError("docker execution requires a docker image")
        return self


class WorkflowSnapshot(BaseModel):
    """Fixed Workflow selection for a workflow-style evaluation variant."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_name: str = Field(min_length=1, max_length=200)
    workflow_version: int = Field(default=1, ge=1)
    roles: tuple[EvaluationRoleSnapshot, ...] = Field(min_length=1, max_length=8)
    main_role_id: str | None = Field(default=None, max_length=200)
    planner_role_id: str = Field(min_length=1, max_length=200)
    explorer_role_ids: tuple[str, ...] = Field(default=(), max_length=4)
    coder_role_id: str = Field(min_length=1, max_length=200)
    reviewer_role_id: str = Field(min_length=1, max_length=200)
    max_parallel_explorers: int = Field(default=1, ge=1, le=4)
    max_rework_rounds: int = Field(default=1, ge=0, le=3)

    @field_validator("workflow_name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _normalize_text(value, "workflow name")

    @field_validator("main_role_id", "planner_role_id", "coder_role_id", "reviewer_role_id")
    @classmethod
    def validate_slot_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_identifier(value, "workflow slot role id")

    @field_validator("explorer_role_ids")
    @classmethod
    def validate_explorer_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_validate_identifier(item, "explorer role id") for item in value)
        return _unique_texts(normalized, "explorer role ids")

    @model_validator(mode="after")
    def validate_roles(self) -> WorkflowSnapshot:
        role_ids = tuple(role.role_id for role in self.roles)
        _unique_texts(role_ids, "workflow role ids")
        selected = tuple(
            role_id
            for role_id in (
                self.main_role_id,
                self.planner_role_id,
                *self.explorer_role_ids,
                self.coder_role_id,
                self.reviewer_role_id,
            )
            if role_id is not None
        )
        _unique_texts(selected, "workflow slot role ids")
        missing = set(selected).difference(role_ids)
        if missing:
            raise ValueError(f"workflow slot references missing role snapshots: {sorted(missing)}")
        return self


def validate_verification_argv(argv: Iterable[str]) -> tuple[str, ...]:
    """Validate a verification command before an EvaluationCase can persist it.

    The list is intentionally much narrower than the normal ``run_command``
    interface: an evaluation specification can only request test/static checks.
    """

    normalized = tuple(argv)
    if not normalized or len(normalized) > 20:
        raise ValueError("verification argv must contain between 1 and 20 arguments")
    for item in normalized:
        if not isinstance(item, str) or not item or item != item.strip() or "\x00" in item:
            raise ValueError("verification argv must contain non-blank string arguments")
        _ensure_secret_safe(item, "verification argv")
        if _URL.search(item) or _SHELL_META.search(item) or _ENV_ASSIGNMENT.match(item):
            raise ValueError(
                "verification argv must not contain URLs, shell syntax, or env assignments"
            )
        if item.startswith(("/", "~")) or "\\" in item:
            raise ValueError("verification argv must use relative workspace arguments")
        if re.search(r"(?:^|[=/])\.\.(?:/|$)", item):
            raise ValueError("verification argv must not escape the evaluation workspace")

    if normalized[0] == "pytest":
        return normalized
    if (
        normalized[0] in {"python", "python3"}
        and len(normalized) >= 3
        and normalized[1] == "-m"
        and normalized[2] in {"pytest", "unittest"}
    ):
        return normalized
    if normalized[:2] == ("uv", "run"):
        return ("uv", "run", *validate_verification_argv(normalized[2:]))
    if normalized[0] == "ruff" and len(normalized) >= 2:
        if normalized[1] == "check":
            return normalized
        if normalized[1] == "format" and "--check" in normalized[2:]:
            return normalized
    if normalized[0] == "mypy" and len(normalized) >= 2:
        return normalized
    if normalized == ("git", "diff", "--check"):
        return normalized
    raise ValueError("verification argv must be an approved test or static-check command")


class VerificationCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    argv: tuple[str, ...] = Field(min_length=1, max_length=20)
    timeout_seconds: int | None = Field(default=None, ge=1, le=3_600)

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return validate_verification_argv(value)


class EvaluationCase(BaseModel):
    """One fixed task/fixture and its non-negotiable verification contract."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    id: str = Field(default_factory=lambda: _new_id("eval_case"), min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    task: str = Field(min_length=1, max_length=100_000)
    environment: EnvironmentSnapshot
    verification_commands: tuple[VerificationCommand, ...] = Field(
        min_length=1,
        max_length=MAX_VERIFICATION_COMMANDS,
        validation_alias=AliasChoices("verification_commands", "verification"),
    )
    allowed_changed_paths: tuple[str, ...] = Field(default=(), max_length=100)
    expected_changed_paths: tuple[str, ...] = Field(default=(), max_length=100)
    validation_timeout_seconds: int = Field(default=300, ge=1, le=3_600)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _validate_identifier(value, "evaluation case id")

    @field_validator("name", "task")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return _normalize_text(value, "evaluation case text")

    @field_validator("allowed_changed_paths", "expected_changed_paths")
    @classmethod
    def validate_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(
            _validate_relative_path(path, "changed path", allow_glob=True) for path in value
        )
        return _unique_texts(normalized, "changed paths")

    @model_validator(mode="after")
    def validate_expected_paths(self) -> EvaluationCase:
        if self.allowed_changed_paths:
            for expected in self.expected_changed_paths:
                if not any(
                    fnmatchcase(expected, allowed) or fnmatchcase(allowed, expected)
                    for allowed in self.allowed_changed_paths
                ):
                    raise ValueError("expected changed paths must be allowed changed paths")
        return self


class EvaluationVariant(BaseModel):
    """Exactly one execution shape: a Session or a Workflow, never both."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    id: str = Field(default_factory=lambda: _new_id("eval_variant"), min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    kind: EvaluationVariantKind = Field(
        validation_alias=AliasChoices("kind", "mode", "variant_type"),
    )
    session_role: EvaluationRoleSnapshot | None = None
    workflow: WorkflowSnapshot | None = None
    memory: MemorySnapshot = Field(default_factory=MemorySnapshot)
    execution: ExecutionSnapshot

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _validate_identifier(value, "evaluation variant id")

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _normalize_text(value, "evaluation variant name")

    @model_validator(mode="after")
    def validate_shape(self) -> EvaluationVariant:
        if self.kind is EvaluationVariantKind.SESSION:
            if self.session_role is None or self.workflow is not None:
                raise ValueError("session variants require session_role and forbid workflow")
        elif self.workflow is None or self.session_role is not None:
            raise ValueError("workflow variants require workflow and forbid session_role")
        return self


class EvaluationSuite(BaseModel):
    """Bounded immutable benchmark definition.

    ``expanded_result_count`` is the exact number of case/variant/repetition
    rows that one evaluation run will create.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: _new_id("eval_suite"), min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    experiment: EvaluationExperiment | None = None
    description: str | None = Field(default=None, max_length=10_000)
    cases: tuple[EvaluationCase, ...] = Field(min_length=1, max_length=MAX_EVALUATION_CASES)
    variants: tuple[EvaluationVariant, ...] = Field(
        min_length=1, max_length=MAX_EVALUATION_VARIANTS
    )
    repetitions: int = Field(default=1, ge=1, le=MAX_EVALUATION_REPETITIONS)
    status: EvaluationSuiteStatus = EvaluationSuiteStatus.DRAFT
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _validate_identifier(value, "evaluation suite id")

    @field_validator("name", "description")
    @classmethod
    def validate_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalize_text(value, "evaluation suite text")

    @model_validator(mode="after")
    def validate_bounds_and_ids(self) -> EvaluationSuite:
        _unique_texts(tuple(case.id for case in self.cases), "evaluation case ids")
        _unique_texts(tuple(variant.id for variant in self.variants), "evaluation variant ids")
        if self.expanded_result_count > MAX_EVALUATION_EXPANDED_RESULTS:
            raise ValueError(
                "evaluation suite expands beyond the maximum number of evaluation results"
            )
        return self

    @property
    def expanded_result_count(self) -> int:
        return len(self.cases) * len(self.variants) * self.repetitions


class EvaluationRun(BaseModel):
    """Persisted lifecycle record for a single expansion of an EvaluationSuite."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: _new_id("eval_run"), min_length=1, max_length=200)
    suite_id: str = Field(min_length=1, max_length=200)
    status: EvaluationRunStatus = EvaluationRunStatus.CREATED
    execution_strategy: EvaluationExecutionStrategy = EvaluationExecutionStrategy.SEQUENTIAL
    environment: EnvironmentSnapshot | None = None
    aggregate: EvaluationAggregate | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    last_error_type: str | None = Field(default=None, max_length=200)

    @field_validator("id", "suite_id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _validate_identifier(value, "evaluation run identity")

    @field_validator("last_error_type")
    @classmethod
    def validate_error_type(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_identifier(value, "evaluation error type")

    @model_validator(mode="after")
    def validate_timestamps(self) -> EvaluationRun:
        if self.finished_at is not None and self.started_at is None:
            raise ValueError("finished evaluation run requires started_at")
        if (
            self.started_at is not None
            and self.finished_at is not None
            and self.finished_at < self.started_at
        ):
            raise ValueError("finished_at must not precede started_at")
        return self


class EvaluationRunEvent(BaseModel):
    """Immutable evaluation event persisted before it is exposed to a client."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(
        default_factory=lambda: _new_id("evaluation_event"),
        min_length=1,
        max_length=200,
    )
    evaluation_run_id: str = Field(min_length=1, max_length=200)
    cursor: int | None = Field(default=None, ge=1)
    event_type: str = Field(min_length=1, max_length=100)
    result_id: str | None = Field(default=None, max_length=200)
    payload: dict[str, object] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class EvaluationMetrics(BaseModel):
    """Metrics recorded as facts; every unavailable value remains ``None``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_succeeded: bool | None = None
    tests_passed: bool | None = None
    first_attempt_succeeded: bool | None = None
    verification_passed: bool | None = None
    patch_accuracy: float | None = Field(default=None, ge=0, le=1)
    repair_turns: int | None = Field(default=None, ge=0)
    rework_rounds: int | None = Field(default=None, ge=0)
    model_calls: int | None = Field(default=None, ge=0)
    tool_calls: int | None = Field(default=None, ge=0)
    tool_failures: int | None = Field(default=None, ge=0)
    approval_requests: int | None = Field(default=None, ge=0)
    approvals_approved: int | None = Field(default=None, ge=0)
    approvals_denied: int | None = Field(default=None, ge=0)
    approval_wait_ms: int | None = Field(default=None, ge=0)
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    latency_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_approval_counts(self) -> EvaluationMetrics:
        if (
            self.approval_requests is not None
            and self.approvals_approved is not None
            and self.approvals_denied is not None
            and self.approvals_approved + self.approvals_denied > self.approval_requests
        ):
            raise ValueError("approval decisions cannot exceed approval requests")
        return self


class FailureEvidence(BaseModel):
    """Safe pointer to one Trace event; it never stores provider or tool output."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_type: str = Field(min_length=1, max_length=100)
    event_id: str | None = Field(default=None, max_length=200)
    summary_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    reason_code: str | None = Field(default=None, max_length=200)

    @field_validator("event_type", "reason_code")
    @classmethod
    def validate_event_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_identifier(value, "failure evidence text")

    @field_validator("event_id")
    @classmethod
    def validate_event_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_identifier(value, "failure evidence event id")

    @field_validator("summary_sha256")
    @classmethod
    def validate_digest(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not re.fullmatch(r"[0-9a-f]{64}", value.lower()):
            raise ValueError("failure evidence summary_sha256 must be a SHA-256 hex digest")
        return value.lower()


class FailureAnalysis(BaseModel):
    """Trace-backed five-dimensional RCA for Exp 24 and failed attempts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    category: FailureCategory
    reason_code: str = Field(min_length=1, max_length=200)
    root_cause: str = Field(min_length=1, max_length=10_000)
    evidence_event_types: tuple[str, ...] = Field(default=(), max_length=100)
    evidence: tuple[FailureEvidence, ...] = Field(default=(), max_length=100)
    inflection_event_id: str | None = Field(default=None, max_length=200)
    inflection_event_type: str | None = Field(default=None, max_length=100)
    confidence: float | None = Field(default=None, ge=0, le=1)

    @field_validator("reason_code", "inflection_event_type")
    @classmethod
    def validate_reason_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_identifier(value, "failure analysis code")

    @field_validator("root_cause")
    @classmethod
    def validate_root_cause(cls, value: str) -> str:
        return _normalize_text(value, "failure root cause")

    @field_validator("evidence_event_types")
    @classmethod
    def validate_event_types(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(
            _validate_identifier(item, "failure evidence event type") for item in value
        )
        return _unique_texts(normalized, "failure evidence event types")

    @field_validator("inflection_event_id")
    @classmethod
    def validate_inflection_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_identifier(value, "inflection event id")

    @model_validator(mode="after")
    def validate_evidence_types(self) -> FailureAnalysis:
        evidence_types = tuple(dict.fromkeys(evidence.event_type for evidence in self.evidence))
        if not self.evidence_event_types and evidence_types:
            object.__setattr__(self, "evidence_event_types", evidence_types)
        if self.evidence_event_types and set(evidence_types).difference(self.evidence_event_types):
            raise ValueError(
                "failure evidence event types must be declared in evidence_event_types"
            )
        return self


class VerificationOutcome(BaseModel):
    """A safe result for one pre-approved verification command."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    argv: tuple[str, ...] = Field(min_length=1, max_length=20)
    exit_code: int | None = None
    timed_out: bool | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    output_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    output_chars: int | None = Field(default=None, ge=0)
    output_truncated: bool | None = None

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return validate_verification_argv(value)

    @field_validator("output_sha256")
    @classmethod
    def validate_digest(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not re.fullmatch(r"[0-9a-f]{64}", value.lower()):
            raise ValueError("output_sha256 must be a SHA-256 hex digest")
        return value.lower()


class ArtifactWorkspace(BaseModel):
    """Local-only artifact location.  It must be excluded from sanitized exports."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    local_workspace_path: str | None = Field(default=None, max_length=4_096)
    artifact_ref: str | None = Field(default=None, max_length=500)
    workspace_sha256: str | None = Field(default=None, min_length=64, max_length=64)

    @field_validator("local_workspace_path")
    @classmethod
    def validate_local_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip() or "\x00" in value:
            raise ValueError("local_workspace_path must be non-blank and contain no NUL")
        return value

    @field_validator("artifact_ref")
    @classmethod
    def validate_artifact_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalize_text(value, "artifact ref")

    @field_validator("workspace_sha256")
    @classmethod
    def validate_digest(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not re.fullmatch(r"[0-9a-f]{64}", value.lower()):
            raise ValueError("workspace_sha256 must be a SHA-256 hex digest")
        return value.lower()

    @model_validator(mode="after")
    def validate_reference(self) -> ArtifactWorkspace:
        if self.local_workspace_path is None and self.artifact_ref is None:
            raise ValueError("artifact workspace requires a local workspace path or artifact ref")
        return self


class EvaluationResultSnapshot(BaseModel):
    """Actual execution fact, kept separately from the suite declaration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    roles: tuple[EvaluationRoleSnapshot, ...] = Field(min_length=1, max_length=8)
    memory: MemorySnapshot
    environment: EnvironmentSnapshot
    workflow: WorkflowSnapshot | None = None
    execution: ExecutionSnapshot

    @model_validator(mode="after")
    def validate_roles(self) -> EvaluationResultSnapshot:
        _unique_texts(tuple(role.role_id for role in self.roles), "actual role snapshot ids")
        return self


class EvaluationResult(BaseModel):
    """One case/variant/repetition fact, unique within an EvaluationRun."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(default_factory=lambda: _new_id("eval_result"), min_length=1, max_length=200)
    run_id: str = Field(min_length=1, max_length=200)
    case_id: str = Field(min_length=1, max_length=200)
    variant_id: str = Field(min_length=1, max_length=200)
    repetition: int = Field(ge=1, le=MAX_EVALUATION_REPETITIONS)
    status: EvaluationResultStatus = EvaluationResultStatus.PENDING
    metrics: EvaluationMetrics = Field(default_factory=EvaluationMetrics)
    changed_paths: tuple[str, ...] = Field(default=(), max_length=1_000)
    snapshot: EvaluationResultSnapshot | None = Field(
        default=None,
        validation_alias=AliasChoices("snapshot", "actual_snapshot", "result_snapshot"),
    )
    artifact_workspace: ArtifactWorkspace | None = None
    verification: tuple[VerificationOutcome, ...] = Field(
        default=(), max_length=MAX_VERIFICATION_COMMANDS
    )
    execution_facts: tuple[EnvironmentFact, ...] = Field(default=(), max_length=100)
    failure_analysis: FailureAnalysis | None = None
    trace_session_ids: tuple[str, ...] = Field(default=(), max_length=100)
    trace_workflow_run_id: str | None = Field(default=None, max_length=200)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None

    @field_validator("id", "run_id", "case_id", "variant_id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _validate_identifier(value, "evaluation result identity")

    @field_validator("changed_paths")
    @classmethod
    def validate_changed_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_validate_relative_path(path, "changed path") for path in value)
        return _unique_texts(normalized, "changed paths")

    @field_validator("trace_session_ids")
    @classmethod
    def validate_trace_sessions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_validate_identifier(item, "trace session id") for item in value)
        return _unique_texts(normalized, "trace session ids")

    @field_validator("trace_workflow_run_id")
    @classmethod
    def validate_trace_workflow(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_identifier(value, "trace workflow run id")

    @field_validator("execution_facts")
    @classmethod
    def validate_execution_facts(
        cls, value: tuple[EnvironmentFact, ...]
    ) -> tuple[EnvironmentFact, ...]:
        _unique_texts(tuple(fact.name for fact in value), "execution fact names")
        return value

    @model_validator(mode="after")
    def validate_result_shape(self) -> EvaluationResult:
        if self.status is EvaluationResultStatus.PASSED and self.failure_analysis is not None:
            raise ValueError("a passed result must not carry a failure analysis")
        if self.status in {
            EvaluationResultStatus.PASSED,
            EvaluationResultStatus.FAILED,
            EvaluationResultStatus.ERROR,
        } and (self.snapshot is None or self.artifact_workspace is None):
            raise ValueError("terminal evaluation results require snapshot and artifact_workspace")
        if self.status is EvaluationResultStatus.INTERRUPTED:
            if self.artifact_workspace is None:
                raise ValueError("interrupted evaluation results require artifact_workspace")
            if self.snapshot is not None:
                raise ValueError("interrupted evaluation results must not claim an actual snapshot")
            if any(value is not None for value in self.metrics.model_dump().values()):
                raise ValueError("interrupted evaluation results must keep metrics unknown")
            if self.finished_at is None:
                raise ValueError("interrupted evaluation results require finished_at")
        if self.finished_at is not None and self.finished_at < self.created_at:
            raise ValueError("result finished_at must not precede created_at")
        return self


class RoleSnapshotDrift(BaseModel):
    """One non-sensitive difference between declared and captured role facts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role_id: str = Field(min_length=1, max_length=200)
    field: str = Field(min_length=1, max_length=200)
    expected: str | None = Field(default=None, max_length=500)
    actual: str | None = Field(default=None, max_length=500)

    @field_validator("role_id", "field", "expected", "actual")
    @classmethod
    def validate_values(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalize_text(value, "role snapshot drift")


def role_snapshot_drifts(
    declared: Iterable[EvaluationRoleSnapshot],
    actual: Iterable[EvaluationRoleSnapshot],
) -> tuple[RoleSnapshotDrift, ...]:
    """Compare reproducibility fields without exposing prompt bodies.

    The comparison intentionally covers the role version, computed prompt hash,
    model profile/model ID and effort.  A caller can persist the returned safe
    drift facts or call :func:`assert_role_snapshot_contract` to fail preflight.
    """

    declared_items = tuple(declared)
    actual_items = tuple(actual)
    _unique_texts(tuple(item.role_id for item in declared_items), "declared role snapshot ids")
    _unique_texts(tuple(item.role_id for item in actual_items), "actual role snapshot ids")
    declared_by_id = {item.role_id: item for item in declared_items}
    actual_by_id = {item.role_id: item for item in actual_items}
    drifts: list[RoleSnapshotDrift] = []
    for role_id in sorted(set(declared_by_id).difference(actual_by_id)):
        drifts.append(RoleSnapshotDrift(role_id=role_id, field="role_presence", expected="present"))
    for role_id in sorted(set(actual_by_id).difference(declared_by_id)):
        drifts.append(
            RoleSnapshotDrift(role_id=role_id, field="role_presence", actual="unexpected")
        )
    comparisons = (
        ("role_version", lambda item: str(item.role_version)),
        ("prompt_sha256", lambda item: item.prompt.content_sha256),
        ("model_profile_id", lambda item: item.model.model_profile_id),
        ("model_id", lambda item: item.model.model_id),
        ("effort", lambda item: item.model.effort.value),
    )
    for role_id in sorted(set(declared_by_id).intersection(actual_by_id)):
        expected_role = declared_by_id[role_id]
        actual_role = actual_by_id[role_id]
        for field, accessor in comparisons:
            expected = accessor(expected_role)
            captured = accessor(actual_role)
            if expected != captured:
                drifts.append(
                    RoleSnapshotDrift(
                        role_id=role_id,
                        field=field,
                        expected=expected,
                        actual=captured,
                    )
                )
    return tuple(drifts)


def assert_role_snapshot_contract(
    declared: Iterable[EvaluationRoleSnapshot],
    actual: Iterable[EvaluationRoleSnapshot],
) -> None:
    drifts = role_snapshot_drifts(declared, actual)
    if drifts:
        fields = ", ".join(f"{drift.role_id}:{drift.field}" for drift in drifts)
        raise ValueError(f"evaluation role snapshot drift: {fields}")


def assert_evaluation_result_contract(
    case: EvaluationCase,
    variant: EvaluationVariant,
    result: EvaluationResult,
) -> None:
    """Fail when a captured result cannot prove it ran the declared variant."""

    if result.case_id != case.id:
        raise ValueError("evaluation result case identity does not match its contract")
    if result.variant_id != variant.id:
        raise ValueError("evaluation result variant identity does not match its contract")
    if result.status is EvaluationResultStatus.INTERRUPTED:
        _assert_interrupted_result_contract(result)
        return
    snapshot = result.snapshot
    if snapshot is None:
        if result.status in {EvaluationResultStatus.PENDING, EvaluationResultStatus.SKIPPED}:
            return
        raise ValueError("terminal evaluation result is missing its actual snapshot")
    expected_roles = (
        (variant.session_role,)
        if variant.kind is EvaluationVariantKind.SESSION
        else variant.workflow.roles
        if variant.workflow is not None
        else ()
    )
    selected_roles = tuple(role for role in expected_roles if role is not None)
    if _is_role_snapshot_drift_error(result):
        _assert_role_drift_error_scope(selected_roles, snapshot.roles)
    else:
        assert_role_snapshot_contract(selected_roles, snapshot.roles)
    if snapshot.memory.enabled != variant.memory.enabled:
        raise ValueError("evaluation memory snapshot drift")
    if variant.memory.references and snapshot.memory.references != variant.memory.references:
        raise ValueError("evaluation memory reference snapshot drift")
    _assert_environment_snapshot_compatible(case.environment, snapshot.environment)
    if snapshot.execution != variant.execution:
        raise ValueError("evaluation execution snapshot drift")
    if snapshot.workflow != variant.workflow:
        raise ValueError("evaluation workflow snapshot drift")
    _assert_changed_paths_compatible(case, result)
    _assert_verification_contract(case, result)


def _is_role_snapshot_drift_error(result: EvaluationResult) -> bool:
    return (
        result.status is EvaluationResultStatus.ERROR
        and result.failure_analysis is not None
        and result.failure_analysis.reason_code == "orchestration.role_snapshot_drift"
    )


def interrupted_evaluation_failure_analysis() -> FailureAnalysis:
    """Return the fixed, non-sensitive cause used when a scheduled row is unknown."""

    return FailureAnalysis(
        category=FailureCategory.ORCHESTRATION,
        reason_code="orchestration.evaluation_interrupted",
        root_cause=(
            "Evaluation process stopped after scheduling this result; its outcome is unknown."
        ),
        confidence=1.0,
    )


def _assert_interrupted_result_contract(result: EvaluationResult) -> None:
    """Keep interruption evidence honest: a namespace exists, execution facts do not."""

    if result.artifact_workspace is None:
        raise ValueError("interrupted evaluation result is missing its artifact workspace")
    if result.snapshot is not None:
        raise ValueError("interrupted evaluation result must not claim an actual snapshot")
    if any(value is not None for value in result.metrics.model_dump().values()):
        raise ValueError("interrupted evaluation result must keep metrics unknown")
    if result.changed_paths or result.verification:
        raise ValueError("interrupted evaluation result must not claim patch or verification facts")
    if result.failure_analysis is None or (
        result.failure_analysis.reason_code != "orchestration.evaluation_interrupted"
    ):
        raise ValueError("interrupted evaluation result requires the interruption failure analysis")
    if result.finished_at is None:
        raise ValueError("interrupted evaluation result is missing finished_at")


def _assert_role_drift_error_scope(
    selected_roles: tuple[EvaluationRoleSnapshot, ...],
    actual_roles: tuple[EvaluationRoleSnapshot, ...],
) -> None:
    """Permit actual drift evidence only for the selected evaluation role slots.

    A role-drift ERROR is precisely the one case where rejecting the current
    role version/prompt/model would erase the fact that caused the run to
    stop.  It is not a general relaxation: the captured role IDs must still
    be exactly those selected by the Session variant or fixed Workflow slots.
    ``snapshot.workflow`` remains checked by the caller, so slot assignment
    cannot silently change either.
    """

    expected_ids = tuple(role.role_id for role in selected_roles)
    actual_ids = tuple(role.role_id for role in actual_roles)
    if len(expected_ids) != len(actual_ids) or set(expected_ids) != set(actual_ids):
        raise ValueError("role snapshot drift error must retain exactly the selected role ids")


def _assert_environment_snapshot_compatible(
    declared: EnvironmentSnapshot,
    actual: EnvironmentSnapshot,
) -> None:
    """Allow captured environment facts to add detail without overriding a pin."""

    if declared.fixture_ref.rstrip("/") != actual.fixture_ref.rstrip("/"):
        raise ValueError("evaluation environment snapshot drift")
    for field in (
        "source_revision",
        "fixture_sha256",
        "container_image",
        "dependency_lock_sha256",
    ):
        expected = getattr(declared, field)
        if expected is not None and getattr(actual, field) != expected:
            raise ValueError(f"evaluation environment snapshot drift: {field}")
    actual_facts = {fact.name: fact.value for fact in actual.facts}
    for fact in declared.facts:
        if actual_facts.get(fact.name) != fact.value:
            raise ValueError(f"evaluation environment snapshot drift: fact {fact.name}")


def _assert_changed_paths_compatible(case: EvaluationCase, result: EvaluationResult) -> None:
    # Failed/error attempts must retain the actual diff, including an out-of-
    # contract path, so patch-accuracy and root-cause analysis do not erase the
    # very fact that made the attempt fail.  A result may claim PASSED only
    # when every changed path obeys the declaration and every expected path is
    # present.
    if result.status is EvaluationResultStatus.PASSED and case.allowed_changed_paths:
        for changed_path in result.changed_paths:
            if not any(
                fnmatchcase(changed_path, allowed) for allowed in case.allowed_changed_paths
            ):
                raise ValueError(f"changed path is not allowed by evaluation case: {changed_path}")
    if result.status is EvaluationResultStatus.PASSED:
        for expected_path in case.expected_changed_paths:
            if not any(
                fnmatchcase(changed_path, expected_path) or fnmatchcase(expected_path, changed_path)
                for changed_path in result.changed_paths
            ):
                raise ValueError(
                    f"evaluation result is missing expected changed path: {expected_path}"
                )


def _assert_verification_contract(case: EvaluationCase, result: EvaluationResult) -> None:
    terminal_statuses = {
        EvaluationResultStatus.PASSED,
        EvaluationResultStatus.FAILED,
        EvaluationResultStatus.ERROR,
    }
    if result.status not in terminal_statuses:
        return
    expected_argv = tuple(command.argv for command in case.verification_commands)
    actual_argv = tuple(outcome.argv for outcome in result.verification)
    if actual_argv != expected_argv:
        raise ValueError("evaluation verification outcomes must match case verification commands")
    if result.status is EvaluationResultStatus.PASSED:
        metrics = result.metrics
        if not (
            metrics.task_succeeded is True
            and metrics.tests_passed is True
            and metrics.verification_passed is True
        ):
            raise ValueError(
                "passed evaluation results require successful task and verification metrics"
            )
        if any(
            outcome.exit_code != 0 or outcome.timed_out is not False
            for outcome in result.verification
        ):
            raise ValueError("passed evaluation results require successful verification outcomes")


def _rate(values: Iterable[bool | None]) -> tuple[float | None, int]:
    observed = [value for value in values if value is not None]
    if not observed:
        return None, 0
    return sum(observed) / len(observed), len(observed)


def _complete_int_sum(values: Iterable[int | None]) -> int | None:
    observed = tuple(values)
    if not observed or any(value is None for value in observed):
        return None
    return sum(value for value in observed if value is not None)


def _complete_float_sum(values: Iterable[float | None]) -> float | None:
    observed = tuple(values)
    if not observed or any(value is None for value in observed):
        return None
    return sum(value for value in observed if value is not None)


def _complete_mean(values: Iterable[int | float | None]) -> float | None:
    observed = tuple(values)
    if not observed or any(value is None for value in observed):
        return None
    return sum(value for value in observed if value is not None) / len(observed)


class EvaluationAggregate(BaseModel):
    """Conservative aggregate of result facts.

    Rates use their explicit observed denominator.  Totals and means return
    ``None`` unless every included result supplied the underlying metric.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    suite_id: str | None = None
    run_id: str | None = None
    # The immutable Suite expansion is passed separately because a process may
    # stop before it schedules every row.  ``result_count`` remains the number
    # of rows actually persisted in SQLite.
    expected_result_count: int = Field(default=0, ge=0)
    result_count: int = Field(default=0, ge=0)
    # Keep the existing field for clients that already consumed it, while
    # exposing lifecycle details needed to distinguish missing outcomes from a
    # real failed result.
    completed_result_count: int = Field(default=0, ge=0)
    finished_result_count: int = Field(default=0, ge=0)
    interrupted_result_count: int = Field(default=0, ge=0)
    pending_result_count: int = Field(default=0, ge=0)
    # Planned combinations that never reached a persisted row.  Together with
    # finished/interrupted/pending this is a disjoint view of the Suite matrix.
    unknown_result_count: int = Field(default=0, ge=0)
    task_success_rate: float | None = Field(default=None, ge=0, le=1)
    task_success_observed: int = Field(default=0, ge=0)
    test_pass_rate: float | None = Field(default=None, ge=0, le=1)
    test_pass_observed: int = Field(default=0, ge=0)
    first_attempt_success_rate: float | None = Field(default=None, ge=0, le=1)
    first_attempt_observed: int = Field(default=0, ge=0)
    mean_patch_accuracy: float | None = Field(default=None, ge=0, le=1)
    mean_repair_turns: float | None = Field(default=None, ge=0)
    mean_rework_rounds: float | None = Field(default=None, ge=0)
    total_prompt_tokens: int | None = Field(default=None, ge=0)
    total_completion_tokens: int | None = Field(default=None, ge=0)
    total_reasoning_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    total_cost_usd: float | None = Field(default=None, ge=0)
    total_latency_ms: int | None = Field(default=None, ge=0)
    total_tool_failures: int | None = Field(default=None, ge=0)
    total_approval_requests: int | None = Field(default=None, ge=0)
    total_approvals_approved: int | None = Field(default=None, ge=0)
    total_approvals_denied: int | None = Field(default=None, ge=0)

    @field_validator("suite_id", "run_id")
    @classmethod
    def validate_optional_identity(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_identifier(value, "evaluation aggregate identity")

    @classmethod
    def from_results(
        cls,
        results: Iterable[EvaluationResult],
        *,
        suite_id: str | None = None,
        run_id: str | None = None,
        expected_result_count: int | None = None,
    ) -> EvaluationAggregate:
        materialized = tuple(results)
        expected_count = (
            len(materialized) if expected_result_count is None else expected_result_count
        )
        if expected_count < len(materialized):
            raise ValueError(
                "expected evaluation result count cannot be below persisted result count"
            )
        metrics = tuple(result.metrics for result in materialized)
        task_success_rate, task_success_observed = _rate(
            metric.task_succeeded for metric in metrics
        )
        test_pass_rate, test_pass_observed = _rate(metric.tests_passed for metric in metrics)
        first_attempt_rate, first_attempt_observed = _rate(
            metric.first_attempt_succeeded for metric in metrics
        )
        finished_count = sum(
            result.status
            in {
                EvaluationResultStatus.PASSED,
                EvaluationResultStatus.FAILED,
                EvaluationResultStatus.ERROR,
                EvaluationResultStatus.SKIPPED,
            }
            for result in materialized
        )
        interrupted_count = sum(
            result.status is EvaluationResultStatus.INTERRUPTED for result in materialized
        )
        pending_count = sum(
            result.status is EvaluationResultStatus.PENDING for result in materialized
        )
        return cls(
            suite_id=suite_id,
            run_id=run_id,
            expected_result_count=expected_count,
            result_count=len(materialized),
            completed_result_count=finished_count,
            finished_result_count=finished_count,
            interrupted_result_count=interrupted_count,
            pending_result_count=pending_count,
            unknown_result_count=expected_count - len(materialized),
            task_success_rate=task_success_rate,
            task_success_observed=task_success_observed,
            test_pass_rate=test_pass_rate,
            test_pass_observed=test_pass_observed,
            first_attempt_success_rate=first_attempt_rate,
            first_attempt_observed=first_attempt_observed,
            mean_patch_accuracy=_complete_mean(metric.patch_accuracy for metric in metrics),
            mean_repair_turns=_complete_mean(metric.repair_turns for metric in metrics),
            mean_rework_rounds=_complete_mean(metric.rework_rounds for metric in metrics),
            total_prompt_tokens=_complete_int_sum(metric.prompt_tokens for metric in metrics),
            total_completion_tokens=_complete_int_sum(
                metric.completion_tokens for metric in metrics
            ),
            total_reasoning_tokens=_complete_int_sum(metric.reasoning_tokens for metric in metrics),
            total_tokens=_complete_int_sum(metric.total_tokens for metric in metrics),
            total_cost_usd=_complete_float_sum(metric.cost_usd for metric in metrics),
            total_latency_ms=_complete_int_sum(metric.latency_ms for metric in metrics),
            total_tool_failures=_complete_int_sum(metric.tool_failures for metric in metrics),
            total_approval_requests=_complete_int_sum(
                metric.approval_requests for metric in metrics
            ),
            total_approvals_approved=_complete_int_sum(
                metric.approvals_approved for metric in metrics
            ),
            total_approvals_denied=_complete_int_sum(metric.approvals_denied for metric in metrics),
        )


def aggregate_evaluation_results(
    results: Iterable[EvaluationResult],
    *,
    suite_id: str | None = None,
    run_id: str | None = None,
    expected_result_count: int | None = None,
) -> EvaluationAggregate:
    """Convenience entry point for callers that do not need the model classmethod."""

    return EvaluationAggregate.from_results(
        results,
        suite_id=suite_id,
        run_id=run_id,
        expected_result_count=expected_result_count,
    )


EvaluationRun.model_rebuild()
