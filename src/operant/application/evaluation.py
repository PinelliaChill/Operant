"""Evaluation Runner v1.

The runner is deliberately an application-layer coordinator: it expands a
persisted suite, creates one isolated artifact workspace per result, invokes
the existing Session or fixed coding Workflow runtime, performs *external*
verification, and persists only safe facts.  It is not a report generator for
already-finished runs.

This module keeps the filesystem and subprocess boundaries intentionally
small.  In particular, evaluation verification has its own conservative argv
allow-list in the domain layer and never invokes a shell.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import time
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from operant.application.workflow import SequentialCodingWorkflow
from operant.domain.evaluation import (
    ArtifactWorkspace,
    EnvironmentFact,
    EnvironmentSnapshot,
    EvaluationAggregate,
    EvaluationCase,
    EvaluationMetrics,
    EvaluationResult,
    EvaluationResultSnapshot,
    EvaluationResultStatus,
    EvaluationRoleSnapshot,
    EvaluationRun,
    EvaluationRunEvent,
    EvaluationRunStatus,
    EvaluationSuite,
    EvaluationVariant,
    EvaluationVariantKind,
    FailureAnalysis,
    FailureCategory,
    FailureEvidence,
    MemoryReference,
    MemorySnapshot,
    ModelPricing,
    VerificationCommand,
    VerificationOutcome,
    aggregate_evaluation_results,
    interrupted_evaluation_failure_analysis,
)
from operant.domain.memory import Memory, MemoryKind
from operant.domain.models import Event, RoleSnapshot, Session
from operant.domain.workflow import WorkflowRun, WorkflowRunEvent, WorkflowRunStatus

_EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
        ".operant",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".cache",
        ".tox",
        "node_modules",
        ".hypothesis",
        ".nox",
    }
)
_EXCLUDED_FILE_NAMES = frozenset(
    {
        ".env",
        ".env.local",
        "secrets.json",
        "secrets",
        ".secrets",
        ".ds_store",
    }
)
_LOCK_FILENAMES = (
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
    "requirements.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Cargo.lock",
    "go.sum",
)
_KNOWN_TOOL_NAMES = frozenset(
    {"read_file", "search_files", "apply_patch", "run_command", "git_diff"}
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _is_excluded(relative: Path, *, is_directory: bool) -> bool:
    """Return whether an item is excluded from an isolated evaluation copy."""

    lowered = relative.name.lower()
    if any(part.lower() in _EXCLUDED_DIRECTORY_NAMES for part in relative.parts):
        return True
    if lowered in _EXCLUDED_FILE_NAMES:
        return True
    if lowered.startswith(".env."):
        return True
    if lowered.endswith((".pyc", ".pyo")):
        return True
    return is_directory and lowered in _EXCLUDED_DIRECTORY_NAMES


def _safe_relative_path(path: Path) -> str:
    return path.as_posix()


@dataclass(frozen=True)
class IsolatedWorkspace:
    """One non-shared fixture copy plus facts needed to interpret its diff."""

    source: Path
    workspace: Path
    source_digest: str
    dependency_lock_digest: str | None
    git_head: str | None
    git_dirty: bool | None
    skipped_external_symlinks: tuple[str, ...]
    git_metadata_preserved: bool
    baseline_manifest: dict[str, str]


def copy_isolated_workspace(source: Path, destination: Path) -> IsolatedWorkspace:
    """Copy a fixture without credentials, runtime state, caches, or external links.

    ``shutil.copytree(..., symlinks=True)`` is not enough for an evaluator:
    an absolute symlink pointing at the original fixture would make the agent
    mutate the source, while a link escaping the source could expose unrelated
    local data.  This copier preserves only links whose resolved targets stay
    under ``source``.  Absolute in-tree targets are rebased to a relative link
    within the artifact copy.  Escaping links are skipped and recorded.
    """

    source_root = source.resolve(strict=True)
    if not source_root.is_dir():
        raise ValueError("evaluation fixture_ref must resolve to a directory")
    source_git_head = _git_head(source_root)
    source_git_dirty = _git_dirty(source_root)
    destination_root = destination.resolve(strict=False)
    if destination_root.exists():
        raise ValueError(f"evaluation artifact workspace already exists: {destination_root}")
    destination_root.parent.mkdir(parents=True, exist_ok=True)

    skipped: list[str] = []
    git_metadata_preserved = True

    def copy_directory(current_source: Path, current_destination: Path) -> None:
        nonlocal git_metadata_preserved
        current_destination.mkdir(mode=current_source.stat().st_mode & 0o777, exist_ok=False)
        with os.scandir(current_source) as entries:
            for entry in sorted(entries, key=lambda candidate: candidate.name):
                source_entry = Path(entry.path)
                relative = source_entry.relative_to(source_root)
                if _is_excluded(relative, is_directory=entry.is_dir(follow_symlinks=False)):
                    continue
                destination_entry = current_destination / entry.name
                if entry.is_symlink():
                    raw_target = os.readlink(source_entry)
                    resolved_target = source_entry.resolve(strict=False)
                    if not _within(resolved_target, source_root):
                        skipped.append(_safe_relative_path(relative))
                        if relative == Path(".git"):
                            git_metadata_preserved = False
                        continue
                    if os.path.isabs(raw_target):
                        rebased_target = destination_root / resolved_target.relative_to(source_root)
                        raw_target = os.path.relpath(rebased_target, destination_entry.parent)
                    os.symlink(raw_target, destination_entry)
                    continue
                if entry.is_dir(follow_symlinks=False):
                    copy_directory(source_entry, destination_entry)
                    continue
                if entry.is_file(follow_symlinks=False):
                    # A linked Git worktree uses a ``.git`` *file* that can
                    # point outside the fixture.  Do not retain that external
                    # control path in an evaluation artifact.
                    if relative == Path(".git"):
                        try:
                            first_line = source_entry.read_text(encoding="utf-8").splitlines()[0]
                        except (IndexError, OSError, UnicodeDecodeError):
                            first_line = ""
                        if first_line.startswith("gitdir:"):
                            gitdir = (source_entry.parent / first_line[7:].strip()).resolve(
                                strict=False
                            )
                            if not _within(gitdir, source_root):
                                skipped.append(_safe_relative_path(relative))
                                git_metadata_preserved = False
                                continue
                    shutil.copy2(source_entry, destination_entry, follow_symlinks=False)

    copy_directory(source_root, destination_root)
    baseline = workspace_manifest(destination_root)
    return IsolatedWorkspace(
        source=source_root,
        workspace=destination_root,
        source_digest=_manifest_digest(baseline),
        dependency_lock_digest=dependency_lock_digest(destination_root),
        git_head=source_git_head,
        git_dirty=source_git_dirty,
        skipped_external_symlinks=tuple(skipped),
        git_metadata_preserved=git_metadata_preserved,
        baseline_manifest=baseline,
    )


def workspace_manifest(root: Path) -> dict[str, str]:
    """Produce a deterministic, non-sensitive content manifest for a workspace."""

    resolved_root = root.resolve(strict=True)
    entries: dict[str, str] = {}

    def visit(directory: Path) -> None:
        with os.scandir(directory) as scandir_entries:
            for entry in sorted(scandir_entries, key=lambda candidate: candidate.name):
                candidate = Path(entry.path)
                relative = candidate.relative_to(resolved_root)
                # Git state is required for a normal ``git diff`` but must not
                # be treated as an agent patch.
                if relative.parts and relative.parts[0] == ".git":
                    continue
                if _is_excluded(relative, is_directory=entry.is_dir(follow_symlinks=False)):
                    continue
                key = _safe_relative_path(relative)
                if entry.is_symlink():
                    entries[key] = "link:" + _sha256_text(os.readlink(candidate))
                elif entry.is_dir(follow_symlinks=False):
                    visit(candidate)
                elif entry.is_file(follow_symlinks=False):
                    digest = hashlib.sha256()
                    with candidate.open("rb") as handle:
                        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                            digest.update(chunk)
                    entries[key] = "file:" + digest.hexdigest()

    visit(resolved_root)
    return entries


def _manifest_digest(manifest: dict[str, str]) -> str:
    canonical = "".join(f"{path}\0{digest}\n" for path, digest in sorted(manifest.items()))
    return _sha256_text(canonical)


def dependency_lock_digest(root: Path) -> str | None:
    """Hash the lockfiles that actually exist; no lockfile is an unknown fact."""

    digests: list[tuple[str, str]] = []
    for name in _LOCK_FILENAMES:
        candidate = root / name
        if candidate.is_file():
            digest = hashlib.sha256()
            with candidate.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            digests.append((name, digest.hexdigest()))
    if not digests:
        return None
    return _sha256_text("".join(f"{name}\0{digest}\n" for name, digest in digests))


def changed_paths(baseline: dict[str, str], workspace: Path) -> tuple[str, ...]:
    current = workspace_manifest(workspace)
    return tuple(
        path
        for path in sorted(set(baseline).union(current))
        if baseline.get(path) != current.get(path)
    )


def _git(command: Sequence[str], workspace: Path) -> subprocess.CompletedProcess[bytes] | None:
    try:
        return subprocess.run(
            ["git", "-C", str(workspace), *command],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def _git_head(workspace: Path) -> str | None:
    completed = _git(("rev-parse", "HEAD"), workspace)
    if completed is None or completed.returncode != 0:
        return None
    value = completed.stdout.decode("ascii", errors="ignore").strip()
    return value or None


def _git_dirty(workspace: Path) -> bool | None:
    completed = _git(("status", "--porcelain", "--untracked-files=all"), workspace)
    if completed is None or completed.returncode != 0:
        return None
    return bool(completed.stdout.strip())


@dataclass(frozen=True)
class VerificationObservation:
    """Safe external-verification fact.  Raw stdout/stderr is intentionally absent."""

    argv: tuple[str, ...]
    exit_code: int | None
    timed_out: bool
    duration_ms: int
    output_sha256: str | None
    output_chars: int | None
    output_truncated: bool | None


async def execute_verification(
    command: VerificationCommand,
    *,
    workspace: Path,
    default_timeout_seconds: int,
) -> VerificationObservation:
    """Run one domain-validated verification argv in a new process group."""

    timeout_seconds = command.timeout_seconds or default_timeout_seconds
    started = time.monotonic()
    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            *command.argv,
            cwd=workspace,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError:
        return VerificationObservation(
            argv=command.argv,
            exit_code=None,
            timed_out=False,
            duration_ms=_elapsed_ms(started),
            output_sha256=None,
            output_chars=None,
            output_truncated=None,
        )

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        await _terminate_process_group(process)
        return VerificationObservation(
            argv=command.argv,
            exit_code=None,
            timed_out=True,
            duration_ms=_elapsed_ms(started),
            output_sha256=None,
            output_chars=None,
            output_truncated=None,
        )
    except asyncio.CancelledError:
        await _terminate_process_group(process)
        raise
    output = (stdout or b"") + b"\0" + (stderr or b"")
    return VerificationObservation(
        argv=command.argv,
        exit_code=process.returncode,
        timed_out=False,
        duration_ms=_elapsed_ms(started),
        output_sha256=_sha256_bytes(output),
        output_chars=len(output),
        output_truncated=False,
    )


async def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except (PermissionError, ProcessLookupError):
            if process.returncode is None:
                process.kill()
    await process.communicate()


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1000))


def patch_contract_passed(case: EvaluationCase, paths: Sequence[str]) -> bool | None:
    """Evaluate allowed/expected patch boundaries without treating no rule as success."""

    if not case.allowed_changed_paths and not case.expected_changed_paths:
        return None
    if case.allowed_changed_paths and any(
        not any(PurePosixPath(path).match(pattern) for pattern in case.allowed_changed_paths)
        for path in paths
    ):
        return False
    return not (
        case.expected_changed_paths
        and any(
            not any(PurePosixPath(path).match(pattern) for path in paths)
            for pattern in case.expected_changed_paths
        )
    )


def patch_accuracy(case: EvaluationCase, paths: Sequence[str]) -> float | None:
    """A conservative path-contract score for Exp 20 patch-accuracy comparison."""

    if not case.allowed_changed_paths and not case.expected_changed_paths:
        return None
    if not paths:
        return 1.0 if not case.expected_changed_paths else 0.0
    allowed = sum(
        any(PurePosixPath(path).match(pattern) for pattern in case.allowed_changed_paths)
        for path in paths
    )
    allowed_score = 1.0 if not case.allowed_changed_paths else allowed / len(paths)
    expected_score = 1.0
    if case.expected_changed_paths:
        expected_score = sum(
            any(PurePosixPath(path).match(pattern) for path in paths)
            for pattern in case.expected_changed_paths
        ) / len(case.expected_changed_paths)
    return round((allowed_score + expected_score) / 2, 6)


@dataclass(frozen=True)
class TraceEvidence:
    event_id: str
    event_type: str
    source: str


@dataclass(frozen=True)
class FailureClassification:
    category: FailureCategory
    reason_code: str
    root_cause: str
    inflection: TraceEvidence | None
    evidence: tuple[TraceEvidence, ...]
    confidence: float


def classify_failure(
    *,
    session_events: Iterable[Event] = (),
    workflow_events: Iterable[WorkflowRunEvent] = (),
    verification: Iterable[VerificationObservation] = (),
    runtime_succeeded: bool | None = None,
    patch_contract_ok: bool | None = None,
) -> FailureClassification | None:
    """Deterministically classify the first actionable failure signal.

    The classifier examines raw in-process events only long enough to derive a
    reason code.  It returns IDs/types and never persists model/tool text.
    The fixed precedence makes Exp 24 reports reproducible.
    """

    events = sorted(
        (
            (event.created_at, TraceEvidence(event.id, event.event_type, "session"), event.payload)
            for event in session_events
        ),
        key=lambda item: (item[0], item[1].event_id),
    )
    workflow = sorted(
        (
            (
                event.created_at,
                TraceEvidence(event.id, event.event_type, "workflow"),
                event.payload,
            )
            for event in workflow_events
        ),
        key=lambda item: (item[0], item[1].event_id),
    )

    def first_session(predicate: Any) -> tuple[TraceEvidence, dict[str, Any]] | None:
        for _, evidence, payload in events:
            if predicate(evidence, payload):
                return evidence, payload
        return None

    def first_workflow(event_type: str) -> TraceEvidence | None:
        for _, evidence, _ in workflow:
            if evidence.event_type == event_type:
                return evidence
        return None

    no_progress = first_session(lambda evidence, _: evidence.event_type == "agent.no_progress")
    if no_progress is not None:
        evidence, _ = no_progress
        return FailureClassification(
            category=FailureCategory.ORCHESTRATION,
            reason_code="orchestration.repeated_test_no_progress",
            root_cause="Repeated test-failure signature reached the no-progress limit.",
            inflection=evidence,
            evidence=(evidence,),
            confidence=0.99,
        )

    rework_limit = first_workflow("workflow.rework_limit_reached")
    if rework_limit is not None:
        return FailureClassification(
            category=FailureCategory.ORCHESTRATION,
            reason_code="orchestration.rework_limit_reached",
            root_cause="Reviewer kept requesting rework after the configured bound.",
            inflection=rework_limit,
            evidence=(rework_limit,),
            confidence=0.99,
        )

    missing_verdict = first_workflow("workflow.review_verdict_missing")
    if missing_verdict is not None:
        return FailureClassification(
            category=FailureCategory.PROMPT_PROTOCOL,
            reason_code="prompt_protocol.review_verdict_missing",
            root_cause="Reviewer output did not provide the required explicit verdict.",
            inflection=missing_verdict,
            evidence=(missing_verdict,),
            confidence=0.98,
        )

    def tool_failure(predicate: Any) -> tuple[TraceEvidence, dict[str, Any]] | None:
        return first_session(
            lambda evidence, payload: evidence.event_type == "tool.failed" and predicate(payload)
        )

    patch_failure = tool_failure(
        lambda payload: (
            payload.get("name") == "apply_patch"
            and any(
                marker in str(payload.get("result", "")).lower()
                for marker in ("occur exactly once", "found 0", "found 2", "would not change")
            )
        )
    )
    if patch_failure is not None:
        evidence, _ = patch_failure
        return FailureClassification(
            category=FailureCategory.TOOL_CONTEXT,
            reason_code="tool_context.apply_patch_ambiguous",
            root_cause="apply_patch could not identify one unambiguous target occurrence.",
            inflection=evidence,
            evidence=(evidence,),
            confidence=0.98,
        )

    hallucinated_tool = tool_failure(
        lambda payload: (
            str(payload.get("name", "")) not in _KNOWN_TOOL_NAMES
            or any(
                marker in str(payload.get("result", "")).lower()
                for marker in ("unknown tool", "not a valid tool")
            )
        )
    )
    if hallucinated_tool is not None:
        evidence, _ = hallucinated_tool
        return FailureClassification(
            category=FailureCategory.MODEL,
            reason_code="model.hallucinated_tool_or_api",
            root_cause="The model requested a nonexistent tool or API.",
            inflection=evidence,
            evidence=(evidence,),
            confidence=0.97,
        )

    policy_or_executor_failure = tool_failure(
        lambda payload: (
            str(payload.get("name", "")) in _KNOWN_TOOL_NAMES
            and any(
                marker in str(payload.get("result", "")).lower()
                for marker in ("not allowed by this role", "toolerror", "workspace tool")
            )
        )
    )
    if policy_or_executor_failure is not None:
        evidence, _ = policy_or_executor_failure
        return FailureClassification(
            category=FailureCategory.TOOL_CONTEXT,
            reason_code="tool_context.declared_tool_execution_failed",
            root_cause="A declared workspace tool failed at its execution boundary.",
            inflection=evidence,
            evidence=(evidence,),
            confidence=0.9,
        )

    dependency_failure = first_session(
        lambda evidence, payload: (
            evidence.event_type in {"tool.failed", "test.failure_feedback"}
            and any(
                marker in str(payload.get("result", payload)).lower()
                for marker in (
                    "modulenotfounderror",
                    "no module named",
                    "command executable was not found",
                    "command not found",
                    "dependency",
                )
            )
        )
    )
    if dependency_failure is not None:
        evidence, _ = dependency_failure
        return FailureClassification(
            category=FailureCategory.ENVIRONMENT,
            reason_code="environment.dependency_missing",
            root_cause="The fixture environment is missing a required dependency or executable.",
            inflection=evidence,
            evidence=(evidence,),
            confidence=0.94,
        )

    model_failure = first_session(lambda evidence, _: evidence.event_type == "agent.failed")
    if model_failure is not None:
        evidence, _ = model_failure
        return FailureClassification(
            category=FailureCategory.MODEL,
            reason_code="model.provider_or_runtime_failure",
            root_cause=(
                "The model/provider runtime terminated before a successful agent completion."
            ),
            inflection=evidence,
            evidence=(evidence,),
            confidence=0.9,
        )

    timed_out = next((item for item in verification if item.timed_out), None)
    if timed_out is not None:
        return FailureClassification(
            category=FailureCategory.ENVIRONMENT,
            reason_code="verification.command_timed_out",
            root_cause="An external verification command exceeded its configured timeout.",
            inflection=None,
            evidence=(),
            confidence=1.0,
        )

    failing_verification = next(
        (item for item in verification if item.exit_code not in {0, None}), None
    )
    if failing_verification is not None:
        return FailureClassification(
            category=FailureCategory.ENVIRONMENT,
            reason_code="verification.command_failed",
            root_cause="An external verification command returned a non-zero exit status.",
            inflection=None,
            evidence=(),
            confidence=1.0,
        )

    if patch_contract_ok is False:
        return FailureClassification(
            category=FailureCategory.UNKNOWN,
            reason_code="verification.patch_contract_failed",
            root_cause="The resulting changed paths violate the case patch contract.",
            inflection=None,
            evidence=(),
            confidence=1.0,
        )

    if runtime_succeeded is False:
        return FailureClassification(
            category=FailureCategory.UNKNOWN,
            reason_code="unknown.runtime_unsuccessful",
            root_cause=(
                "The runtime did not complete successfully and no deterministic trace "
                "class matched."
            ),
            inflection=None,
            evidence=(),
            confidence=0.25,
        )
    return None


def _environment_facts(isolated: IsolatedWorkspace) -> tuple[EnvironmentFact, ...]:
    facts = [
        EnvironmentFact(name="python_version", value=platform.python_version()),
        EnvironmentFact(name="os", value=platform.system() or "unknown"),
        EnvironmentFact(name="architecture", value=platform.machine() or "unknown"),
        EnvironmentFact(name="execution_strategy", value="sequential"),
        EnvironmentFact(
            name="git_metadata_preserved",
            value=str(isolated.git_metadata_preserved).lower(),
        ),
    ]
    if isolated.skipped_external_symlinks:
        facts.append(
            EnvironmentFact(
                name="skipped_external_symlink_count",
                value=str(len(isolated.skipped_external_symlinks)),
            )
        )
    return tuple(facts)


def environment_snapshot(case: EvaluationCase, isolated: IsolatedWorkspace) -> EnvironmentSnapshot:
    """Build the actual per-result environment facts without copying secrets."""

    base = case.environment
    source_revision = isolated.git_head if isolated.git_head is not None else base.source_revision
    facts = {fact.name: fact for fact in base.facts}
    for fact in _environment_facts(isolated):
        facts.setdefault(fact.name, fact)
    return EnvironmentSnapshot(
        fixture_ref=base.fixture_ref,
        source_revision=source_revision,
        fixture_sha256=isolated.source_digest,
        container_image=base.container_image,
        dependency_lock_sha256=isolated.dependency_lock_digest,
        facts=tuple(facts[name] for name in sorted(facts)),
    )


def _role_prompt_digest(snapshot: RoleSnapshot) -> str:
    return _sha256_text(snapshot.system_prompt)


def _tool_policy_fingerprint(snapshot: RoleSnapshot) -> str:
    return _policy_fingerprint(snapshot.tool_policy)


def _policy_fingerprint(policy: Any) -> str:
    canonical = json.dumps(policy.model_dump(mode="json"), sort_keys=True)
    return _sha256_text(canonical)


def _pricing_for(
    snapshot: RoleSnapshot,
    prices: Iterable[ModelPricing | None],
) -> ModelPricing | None:
    return next(
        (
            pricing
            for pricing in prices
            if pricing is not None
            and pricing.model_profile_id == snapshot.model_profile_id
            and pricing.model_id == snapshot.model_id
        ),
        None,
    )


def project_role_snapshot(
    snapshot: RoleSnapshot,
    *,
    prices: Iterable[ModelPricing | None] = (),
) -> EvaluationRoleSnapshot:
    """Project a live RoleSnapshot into the secret-free evaluation snapshot."""

    from operant.domain.evaluation import ModelSnapshot, PromptSnapshot

    return EvaluationRoleSnapshot(
        role_id=snapshot.role_id,
        role_version=snapshot.role_version,
        role_name=snapshot.role_name,
        prompt=PromptSnapshot(
            id=f"prompt_{snapshot.role_id}",
            version=snapshot.role_version,
            content=snapshot.system_prompt,
            content_sha256=_role_prompt_digest(snapshot),
        ),
        model=ModelSnapshot(
            model_profile_id=snapshot.model_profile_id,
            provider=snapshot.provider,
            model_id=snapshot.model_id,
            effort=snapshot.effort,
            pricing=_pricing_for(snapshot, prices),
        ),
        tool_policy_fingerprint=_tool_policy_fingerprint(snapshot),
        memory_scope=snapshot.memory_scope,
        max_turns=snapshot.budget.max_turns,
        timeout_seconds=snapshot.budget.timeout_seconds,
    )


def project_role_preset_snapshot(
    role: Any,
    profile: Any,
    *,
    pricing: ModelPricing | None,
) -> EvaluationRoleSnapshot:
    """Project current registry facts before a workflow creates Sessions.

    This is deliberately separate from :func:`project_role_snapshot`: workflow
    preflight has to stop on a mutated role *before* any child session/model
    call is created, while still preserving the actual mutable registry fact.
    """

    from operant.domain.evaluation import ModelSnapshot, PromptSnapshot

    resolved_pricing = pricing
    if resolved_pricing is not None and (
        resolved_pricing.model_profile_id != profile.id
        or resolved_pricing.model_id != profile.model_id
    ):
        resolved_pricing = None
    return EvaluationRoleSnapshot(
        role_id=role.id,
        role_version=role.version,
        role_name=role.name,
        prompt=PromptSnapshot(
            id=f"prompt_{role.id}",
            version=role.version,
            content=role.system_prompt,
            content_sha256=_sha256_text(role.system_prompt),
        ),
        model=ModelSnapshot(
            model_profile_id=profile.id,
            provider=profile.provider,
            model_id=profile.model_id,
            effort=role.effort,
            context_window=profile.context_window,
            pricing=resolved_pricing,
        ),
        tool_policy_fingerprint=_policy_fingerprint(role.tool_policy),
        memory_scope=role.memory_scope,
        max_turns=role.budget.max_turns,
        timeout_seconds=role.budget.timeout_seconds,
    )


def role_snapshot_matches(expected: EvaluationRoleSnapshot, actual: RoleSnapshot) -> bool:
    """Fail closed when an evaluation's intended model/prompt selection drifted."""

    expected_prompt_digest = expected.prompt.content_sha256 or _sha256_text(expected.prompt.content)
    return (
        expected.role_id == actual.role_id
        and expected.role_version == actual.role_version
        and expected_prompt_digest == _role_prompt_digest(actual)
        and expected.model.model_profile_id == actual.model_profile_id
        and expected.model.model_id == actual.model_id
        and expected.model.effort == actual.effort
        and expected.tool_policy_fingerprint == _tool_policy_fingerprint(actual)
    )


class EvaluationService(Protocol):
    """The narrow service surface consumed by the runner.

    The concrete ApplicationService implements this protocol; it keeps the
    runner from bypassing SQLite through CLI/API code.
    """

    def get_evaluation_suite(self, suite_id: str) -> EvaluationSuite: ...

    def create_evaluation_run(self, run: EvaluationRun) -> EvaluationRun: ...

    def update_evaluation_run(
        self,
        evaluation_run_id: str,
        **changes: Any,
    ) -> EvaluationRun: ...

    def append_evaluation_event(self, event: EvaluationRunEvent) -> EvaluationRunEvent: ...

    def append_evaluation_result(self, result: EvaluationResult) -> EvaluationResult: ...

    def update_evaluation_result(
        self,
        result_id: str,
        **changes: Any,
    ) -> EvaluationResult: ...

    def list_evaluation_results(self, evaluation_run_id: str) -> list[EvaluationResult]: ...

    def create_session(
        self,
        role_id: str | None = None,
        *,
        new_role: Any = None,
        model_profile_id: str | None = None,
        effort: str | None = None,
        budget_overrides: dict[str, Any] | None = None,
    ) -> Session: ...

    def run_session(
        self,
        session_id: str,
        *,
        user_message: str,
        workspace: str | Path,
        approval_callback: Any = None,
    ) -> AsyncIterator[Any]: ...

    def get_session(self, session_id: str) -> Session: ...

    def list_events(self, session_id: str) -> list[Event]: ...

    def get_role(self, role_id: str, version: int | None = None) -> Any: ...

    def get_model_profile(self, profile_id: str) -> Any: ...

    def query_memories(
        self,
        query: str,
        *,
        snapshot: RoleSnapshot,
        session_id: str | None = None,
        project_scope: str | None = None,
        kinds: Any = None,
        include_candidates: bool = False,
        limit: int = 20,
    ) -> list[Memory]: ...

    def get_workflow_run(self, workflow_run_id: str) -> WorkflowRun: ...

    def list_workflow_events(self, workflow_run_id: str) -> list[WorkflowRunEvent]: ...

    def submit_approval(self, session_id: str, tool_call_id: str, *, approved: bool) -> bool: ...


@dataclass(frozen=True)
class _RuntimeFacts:
    """Non-persisted facts accumulated while one evaluation row executes."""

    succeeded: bool
    session_ids: tuple[str, ...]
    workflow_run_id: str | None
    session_events: tuple[Event, ...]
    workflow_events: tuple[WorkflowRunEvent, ...]
    roles: tuple[EvaluationRoleSnapshot, ...]
    memory: MemorySnapshot
    pricing_by_session: dict[str, ModelPricing | None]


class _RoleSnapshotDriftError(ValueError):
    """A safe preflight stop that retains the actual selected role snapshots."""

    def __init__(self, runtime: _RuntimeFacts) -> None:
        super().__init__("evaluation role snapshot drifted before execution")
        self.runtime = runtime


class EvaluationRunner:
    """Sequential, isolated Evaluation Runner v1.

    v1 intentionally runs the expanded rows in order.  Each row receives an
    independent copy, preserving the existing one-writer safety model and
    avoiding cross-variant contamination.  The fact is persisted on the Run
    by the caller/domain rather than claimed as parallel throughput.
    """

    def __init__(self, service: EvaluationService) -> None:
        self.service = service

    async def run_suite(
        self,
        suite_id: str,
        *,
        artifact_root: str | Path,
    ) -> AsyncIterator[EvaluationRunEvent]:
        """Expand and run a suite; status becomes interrupted on cancellation."""

        suite = self.service.get_evaluation_suite(suite_id)
        requested_root = Path(artifact_root)
        if not requested_root.is_absolute():
            raise ValueError("evaluation artifact_root must be absolute")
        root = requested_root.resolve(strict=False)
        run = self.service.create_evaluation_run(EvaluationRun(suite_id=suite.id))
        run = self.service.update_evaluation_run(
            run.id,
            status=EvaluationRunStatus.RUNNING,
            started_at=_utc_now(),
            last_error_type=None,
        )
        yield self.service.append_evaluation_event(
            EvaluationRunEvent(
                evaluation_run_id=run.id,
                event_type="evaluation.run_started",
                payload={
                    "execution_strategy": "sequential",
                    "expected_results": suite.expanded_result_count,
                },
            )
        )

        terminal = False
        try:
            for case, variant, repetition in self._expanded(suite):
                async for event in self._run_one(
                    run=run,
                    case=case,
                    variant=variant,
                    repetition=repetition,
                    artifact_root=root,
                ):
                    yield event
            results = self.service.list_evaluation_results(run.id)
            had_error = any(result.status is EvaluationResultStatus.ERROR for result in results)
            final_status = (
                EvaluationRunStatus.FAILED if had_error else EvaluationRunStatus.COMPLETED
            )
            completed_run = self.service.update_evaluation_run(
                run.id,
                status=final_status,
                finished_at=_utc_now(),
                last_error_type="result_error" if had_error else None,
                aggregate=aggregate_evaluation_results(
                    results,
                    suite_id=suite.id,
                    run_id=run.id,
                    expected_result_count=suite.expanded_result_count,
                ),
            )
            terminal = True
            yield self.service.append_evaluation_event(
                EvaluationRunEvent(
                    evaluation_run_id=run.id,
                    event_type="evaluation.run_finished",
                    payload={
                        "status": final_status.value,
                        "result_count": completed_run.aggregate.result_count
                        if completed_run.aggregate is not None
                        else 0,
                    },
                )
            )
        except asyncio.CancelledError:
            self._interrupt_pending_results(run)
            self.service.update_evaluation_run(
                run.id,
                status=EvaluationRunStatus.INTERRUPTED,
                finished_at=_utc_now(),
                last_error_type="cancelled",
                aggregate=self._interrupted_aggregate(run, suite),
            )
            terminal = True
            raise
        finally:
            if not terminal:
                self._interrupt_pending_results(run)
                self.service.update_evaluation_run(
                    run.id,
                    status=EvaluationRunStatus.INTERRUPTED,
                    finished_at=_utc_now(),
                    last_error_type="stream_interrupted",
                    aggregate=self._interrupted_aggregate(run, suite),
                )

    @staticmethod
    def _expanded(
        suite: EvaluationSuite,
    ) -> Iterable[tuple[EvaluationCase, EvaluationVariant, int]]:
        for case in suite.cases:
            for variant in suite.variants:
                for repetition in range(1, suite.repetitions + 1):
                    yield case, variant, repetition

    async def _run_one(
        self,
        *,
        run: EvaluationRun,
        case: EvaluationCase,
        variant: EvaluationVariant,
        repetition: int,
        artifact_root: Path,
    ) -> AsyncIterator[EvaluationRunEvent]:
        """Run one row from source copy through external verification.

        The unique Pending row is persisted before any fixture copy, Session,
        or Workflow work.  A clean terminal fact then replaces that same row;
        a process death leaves a recoverable Pending record rather than
        silently erasing a scheduled comparison.
        """

        artifact_workspace = artifact_root / run.id / case.id / variant.id / str(repetition)
        result_created_at = _utc_now()
        pending = self.service.append_evaluation_result(
            EvaluationResult(
                run_id=run.id,
                case_id=case.id,
                variant_id=variant.id,
                repetition=repetition,
                status=EvaluationResultStatus.PENDING,
                artifact_workspace=self._reserved_artifact_workspace(
                    artifact_workspace,
                    run,
                    case,
                    variant,
                    repetition,
                ),
                created_at=result_created_at,
            )
        )
        yield self.service.append_evaluation_event(
            EvaluationRunEvent(
                evaluation_run_id=run.id,
                result_id=pending.id,
                event_type="evaluation.result_started",
                payload={"case_id": case.id, "variant_id": variant.id, "repetition": repetition},
            )
        )

        started = time.monotonic()
        isolated: IsolatedWorkspace | None = None
        runtime = self._empty_runtime(variant)
        observations: list[VerificationObservation] = []
        paths: tuple[str, ...] = ()
        contract_ok: bool | None = None
        actual_environment = case.environment
        extra_facts: list[EnvironmentFact] = []

        try:
            isolated = copy_isolated_workspace(
                Path(case.environment.fixture_ref), artifact_workspace
            )
            actual_environment = environment_snapshot(case, isolated)
            if variant.kind is EvaluationVariantKind.SESSION:
                runtime = await self._run_session_variant(
                    case=case,
                    variant=variant,
                    workspace=isolated.workspace,
                    source_scope=isolated.source,
                )
            else:
                runtime = await self._run_workflow_variant(
                    case=case,
                    variant=variant,
                    workspace=isolated.workspace,
                    source_scope=isolated.source,
                )

            for command in case.verification_commands:
                observations.append(
                    await execute_verification(
                        command,
                        workspace=isolated.workspace,
                        default_timeout_seconds=case.validation_timeout_seconds,
                    )
                )

            paths = changed_paths(isolated.baseline_manifest, isolated.workspace)
            contract_ok = patch_contract_passed(case, paths)
            verification_ok = self._verification_succeeded(observations)
            task_succeeded = (
                runtime.succeeded is True and verification_ok and contract_ok is not False
            )
            classification = classify_failure(
                session_events=runtime.session_events,
                workflow_events=runtime.workflow_events,
                verification=observations,
                runtime_succeeded=runtime.succeeded,
                patch_contract_ok=contract_ok,
            )
            result = self._build_result(
                result_id=pending.id,
                run=run,
                case=case,
                variant=variant,
                repetition=repetition,
                created_at=result_created_at,
                status=(
                    EvaluationResultStatus.PASSED
                    if task_succeeded
                    else EvaluationResultStatus.FAILED
                ),
                runtime=runtime,
                isolated=isolated,
                artifact_workspace=artifact_workspace,
                environment=actual_environment,
                observations=observations,
                changed=paths,
                task_succeeded=task_succeeded,
                latency_ms=_elapsed_ms(started),
                classification=classification,
                extra_facts=extra_facts,
            )
        except asyncio.CancelledError:
            self._interrupt_pending_result(pending)
            raise
        except Exception as exc:
            if isinstance(exc, _RoleSnapshotDriftError):
                runtime = exc.runtime
            extra_facts.append(EnvironmentFact(name="runner_error_type", value=type(exc).__name__))
            failure = self._runner_failure(exc)
            result = self._build_result(
                result_id=pending.id,
                run=run,
                case=case,
                variant=variant,
                repetition=repetition,
                created_at=result_created_at,
                status=EvaluationResultStatus.ERROR,
                runtime=runtime,
                isolated=isolated,
                artifact_workspace=artifact_workspace,
                environment=actual_environment,
                observations=observations,
                changed=self._safe_changed_paths(isolated, paths),
                task_succeeded=False,
                latency_ms=_elapsed_ms(started),
                classification=failure,
                extra_facts=extra_facts,
            )

        persisted = self._update_pending_result(pending, result)
        yield self.service.append_evaluation_event(
            EvaluationRunEvent(
                evaluation_run_id=run.id,
                result_id=persisted.id,
                event_type="evaluation.result_finished",
                payload={
                    "status": persisted.status.value,
                    "verification_count": len(persisted.verification),
                    "changed_path_count": len(persisted.changed_paths),
                },
            )
        )

    def _empty_runtime(self, variant: EvaluationVariant) -> _RuntimeFacts:
        return _RuntimeFacts(
            succeeded=False,
            session_ids=(),
            workflow_run_id=None,
            session_events=(),
            workflow_events=(),
            roles=self._declared_roles(variant),
            memory=variant.memory,
            pricing_by_session={},
        )

    def _interrupted_aggregate(
        self,
        run: EvaluationRun,
        suite: EvaluationSuite,
    ) -> EvaluationAggregate:
        return aggregate_evaluation_results(
            self.service.list_evaluation_results(run.id),
            suite_id=suite.id,
            run_id=run.id,
            expected_result_count=suite.expanded_result_count,
        )

    def _interrupt_pending_results(self, run: EvaluationRun) -> None:
        """Close every row this runner already scheduled but could not finish."""

        for result in self.service.list_evaluation_results(run.id):
            if result.status is EvaluationResultStatus.PENDING:
                self._interrupt_pending_result(result)

    def _update_pending_result(
        self,
        pending: EvaluationResult,
        terminal: EvaluationResult,
    ) -> EvaluationResult:
        if (
            pending.id != terminal.id
            or pending.run_id != terminal.run_id
            or pending.case_id != terminal.case_id
            or pending.variant_id != terminal.variant_id
            or pending.repetition != terminal.repetition
        ):
            raise ValueError("terminal evaluation result must retain its pending row identity")
        changes = terminal.model_dump(
            exclude={
                "id",
                "run_id",
                "case_id",
                "variant_id",
                "repetition",
                "created_at",
                "updated_at",
            }
        )
        return self.service.update_evaluation_result(pending.id, **changes)

    def _interrupt_pending_result(self, pending: EvaluationResult) -> EvaluationResult:
        if pending.artifact_workspace is None:
            raise ValueError("pending evaluation result is missing its reserved artifact workspace")
        return self.service.update_evaluation_result(
            pending.id,
            status=EvaluationResultStatus.INTERRUPTED,
            metrics=EvaluationMetrics(),
            changed_paths=(),
            snapshot=None,
            artifact_workspace=pending.artifact_workspace,
            verification=(),
            execution_facts=(),
            failure_analysis=interrupted_evaluation_failure_analysis(),
            trace_session_ids=(),
            trace_workflow_run_id=None,
            finished_at=_utc_now(),
        )

    @staticmethod
    def _runner_failure(exc: Exception) -> FailureClassification:
        """Classify local coordinator failures without persisting exception detail."""

        message = str(exc).lower()
        # Only the dedicated preflight exception is allowed to use the special
        # role-drift contract relaxation.  A generic exception merely *saying*
        # "role snapshot drift" has not proved that it retained the actual
        # selected snapshots, so it must not gain that exemption.
        if isinstance(exc, _RoleSnapshotDriftError):
            return FailureClassification(
                category=FailureCategory.ORCHESTRATION,
                reason_code="orchestration.role_snapshot_drift",
                root_cause="The live role selection drifted from the declared evaluation snapshot.",
                inflection=None,
                evidence=(),
                confidence=1.0,
            )
        if "memory" in message and "drift" in message:
            return FailureClassification(
                category=FailureCategory.ORCHESTRATION,
                reason_code="orchestration.memory_snapshot_drift",
                root_cause=(
                    "The active project Memory changed from the declared evaluation snapshot."
                ),
                inflection=None,
                evidence=(),
                confidence=1.0,
            )
        if "fixture_ref" in message or "artifact workspace" in message:
            return FailureClassification(
                category=FailureCategory.ENVIRONMENT,
                reason_code="environment.isolation_workspace_failed",
                root_cause="The evaluator could not prepare its isolated fixture workspace.",
                inflection=None,
                evidence=(),
                confidence=0.9,
            )
        return FailureClassification(
            category=FailureCategory.UNKNOWN,
            reason_code="unknown.evaluation_runner_error",
            root_cause="Evaluation runner could not complete this isolated result.",
            inflection=None,
            evidence=(),
            confidence=0.2,
        )

    @staticmethod
    def _declared_roles(variant: EvaluationVariant) -> tuple[EvaluationRoleSnapshot, ...]:
        if variant.kind is EvaluationVariantKind.SESSION:
            if variant.session_role is None:
                raise ValueError("session evaluation variant is missing its role snapshot")
            return (variant.session_role,)
        if variant.workflow is None:
            raise ValueError("workflow evaluation variant is missing its workflow snapshot")
        return variant.workflow.roles

    async def _run_session_variant(
        self,
        *,
        case: EvaluationCase,
        variant: EvaluationVariant,
        workspace: Path,
        source_scope: Path,
    ) -> _RuntimeFacts:
        expected = self._declared_roles(variant)[0]
        session = self.service.create_session(
            expected.role_id,
            model_profile_id=expected.model.model_profile_id,
            effort=expected.model.effort.value,
        )
        actual_role = project_role_snapshot(
            session.role_snapshot,
            prices=(expected.model.pricing,),
        )
        if not role_snapshot_matches(expected, session.role_snapshot):
            raise _RoleSnapshotDriftError(
                _RuntimeFacts(
                    succeeded=False,
                    session_ids=(session.id,),
                    workflow_run_id=None,
                    session_events=(),
                    workflow_events=(),
                    roles=(actual_role,),
                    memory=variant.memory,
                    pricing_by_session={session.id: actual_role.model.pricing},
                )
            )

        memory = MemorySnapshot(enabled=False)
        message = case.task
        if variant.memory.enabled:
            memory, context = self._memory_context(session, source_scope)
            self._assert_declared_memory(variant.memory, memory)
            if context:
                message = f"{message}\n\n已确认的项目 Memory（评测快照）：\n{context}"

        async for _event in self.service.run_session(
            session.id,
            user_message=message,
            workspace=workspace,
            approval_callback=self._reject_approval,
        ):
            pass

        events = tuple(self.service.list_events(session.id))
        actual = self.service.get_session(session.id)
        if not role_snapshot_matches(expected, actual.role_snapshot):
            raise ValueError("evaluation role snapshot drifted during session execution")
        return _RuntimeFacts(
            succeeded=_session_succeeded(events),
            session_ids=(session.id,),
            workflow_run_id=None,
            session_events=events,
            workflow_events=(),
            roles=(project_role_snapshot(actual.role_snapshot, prices=(expected.model.pricing,)),),
            memory=memory,
            pricing_by_session={session.id: expected.model.pricing},
        )

    async def _run_workflow_variant(
        self,
        *,
        case: EvaluationCase,
        variant: EvaluationVariant,
        workspace: Path,
        source_scope: Path,
    ) -> _RuntimeFacts:
        workflow_config = _workflow_slots(variant)
        self._preflight_workflow_snapshots(variant)
        coordinator = SequentialCodingWorkflow(self.service)  # type: ignore[arg-type]
        workflow_run_id: str | None = None
        async for event in coordinator.run(
            task=case.task,
            workspace=workspace,
            main_role_id=workflow_config.main_role_id,
            planner_role_id=workflow_config.planner_role_id,
            explorer_role_ids=workflow_config.explorer_role_ids,
            coder_role_id=workflow_config.coder_role_id,
            reviewer_role_id=workflow_config.reviewer_role_id,
            max_parallel_explorers=workflow_config.max_parallel_explorers,
            max_rework_rounds=workflow_config.max_rework_rounds,
            memory_enabled=variant.memory.enabled,
            memory_project_scope=source_scope,
            persist_memory_candidates=False,
        ):
            if event.workflow_run_id:
                workflow_run_id = event.workflow_run_id
            if event.event_type == "tool.approval_required" and event.session_id:
                tool_call_id = event.payload.get("tool_call_id")
                if isinstance(tool_call_id, str):
                    self.service.submit_approval(
                        event.session_id,
                        tool_call_id,
                        approved=False,
                    )

        if workflow_run_id is None:
            raise RuntimeError("workflow did not expose a persisted workflow run id")
        workflow_run = self.service.get_workflow_run(workflow_run_id)
        self._verify_workflow_configuration(workflow_run, workflow_config)
        workflow_events = tuple(self.service.list_workflow_events(workflow_run_id))
        session_ids = _workflow_session_ids(workflow_events)
        session_events = tuple(
            event for session_id in session_ids for event in self.service.list_events(session_id)
        )
        roles, pricing_by_session = self._verify_workflow_actual_snapshots(variant, session_ids)
        memory = self._workflow_memory_snapshot(
            session_ids,
            source_scope=source_scope,
            enabled=variant.memory.enabled,
        )
        self._assert_declared_memory(variant.memory, memory)
        return _RuntimeFacts(
            succeeded=(
                workflow_run.status is WorkflowRunStatus.COMPLETED
                and workflow_run.final_verdict == "APPROVED"
            ),
            session_ids=session_ids,
            workflow_run_id=workflow_run_id,
            session_events=session_events,
            workflow_events=workflow_events,
            roles=roles,
            memory=memory,
            pricing_by_session=pricing_by_session,
        )

    @staticmethod
    async def _reject_approval(_tool_call_id: str, _category: str, _detail: str) -> bool:
        """Evaluation has no human approval authority; record a deliberate denial."""

        return False

    def _memory_context(self, session: Session, source_scope: Path) -> tuple[MemorySnapshot, str]:
        memories = self._read_project_memories(session, source_scope)
        references = tuple(
            MemoryReference(
                memory_id=memory.id,
                version=memory.version,
                content_sha256=_sha256_text(memory.content),
            )
            for memory in memories
        )
        context = "\n".join(f"- {memory.content[:1_500]}" for memory in memories)[:6_000]
        return MemorySnapshot(enabled=True, references=references), context

    def _read_project_memories(self, session: Session, source_scope: Path) -> list[Memory]:
        try:
            return self.service.query_memories(
                "",
                snapshot=session.role_snapshot,
                session_id=session.id,
                project_scope=str(source_scope.resolve(strict=False)),
                kinds=(MemoryKind.PROJECT,),
                limit=5,
            )
        except (PermissionError, ValueError):
            return []

    def _workflow_memory_snapshot(
        self,
        session_ids: Sequence[str],
        *,
        source_scope: Path,
        enabled: bool,
    ) -> MemorySnapshot:
        if not enabled:
            return MemorySnapshot(enabled=False)
        references: dict[str, MemoryReference] = {}
        for session_id in session_ids:
            session = self.service.get_session(session_id)
            for memory in self._read_project_memories(session, source_scope):
                reference = MemoryReference(
                    memory_id=memory.id,
                    version=memory.version,
                    content_sha256=_sha256_text(memory.content),
                )
                existing = references.get(reference.memory_id)
                if existing is not None and existing != reference:
                    raise ValueError("evaluation memory changed during workflow execution")
                references[reference.memory_id] = reference
        return MemorySnapshot(
            enabled=True,
            references=tuple(references[key] for key in sorted(references)),
        )

    @staticmethod
    def _assert_declared_memory(declared: MemorySnapshot, actual: MemorySnapshot) -> None:
        if declared.enabled != actual.enabled:
            raise ValueError("evaluation memory snapshot drifted")
        if not declared.references:
            return
        actual_by_id = {reference.memory_id: reference for reference in actual.references}
        for expected in declared.references:
            captured = actual_by_id.get(expected.memory_id)
            if captured is None or captured.version != expected.version:
                raise ValueError("evaluation memory reference snapshot drifted")
            if (
                expected.content_sha256 is not None
                and expected.content_sha256 != captured.content_sha256
            ):
                raise ValueError("evaluation memory reference snapshot drifted")

    def _preflight_workflow_snapshots(self, variant: EvaluationVariant) -> None:
        actual_roles: list[EvaluationRoleSnapshot] = []
        drifted = False
        for expected in _workflow_slots(variant).roles:
            role = self.service.get_role(expected.role_id)
            profile = self.service.get_model_profile(role.model_profile_id)
            actual = project_role_preset_snapshot(
                role,
                profile,
                pricing=expected.model.pricing,
            )
            actual_roles.append(actual)
            if not _role_preset_matches(expected, role, profile):
                drifted = True
        if drifted:
            raise _RoleSnapshotDriftError(
                _RuntimeFacts(
                    succeeded=False,
                    session_ids=(),
                    workflow_run_id=None,
                    session_events=(),
                    workflow_events=(),
                    roles=tuple(actual_roles),
                    memory=variant.memory,
                    pricing_by_session={},
                )
            )

    def _verify_workflow_actual_snapshots(
        self,
        variant: EvaluationVariant,
        session_ids: Sequence[str],
    ) -> tuple[tuple[EvaluationRoleSnapshot, ...], dict[str, ModelPricing | None]]:
        expected_roles = self._declared_roles(variant)
        expected_by_role = {role.role_id: role for role in expected_roles}
        actual_by_role: dict[str, EvaluationRoleSnapshot] = {}
        pricing_by_session: dict[str, ModelPricing | None] = {}
        for session_id in session_ids:
            actual = self.service.get_session(session_id).role_snapshot
            expected = expected_by_role.get(actual.role_id)
            if expected is None or not role_snapshot_matches(expected, actual):
                raise ValueError("evaluation workflow role snapshot drifted during execution")
            projected = project_role_snapshot(actual, prices=(expected.model.pricing,))
            existing = actual_by_role.get(actual.role_id)
            if existing is not None and existing != projected:
                raise ValueError("evaluation workflow role snapshot changed between attempts")
            actual_by_role[actual.role_id] = projected
            pricing_by_session[session_id] = expected.model.pricing
        if set(actual_by_role) != set(expected_by_role):
            raise ValueError("workflow did not execute every declared role")
        return (
            tuple(actual_by_role[role.role_id] for role in expected_roles),
            pricing_by_session,
        )

    @staticmethod
    def _verify_workflow_configuration(workflow_run: WorkflowRun, config: _WorkflowSlots) -> None:
        if (
            workflow_run.main_role_id != config.main_role_id
            or workflow_run.planner_role_id != config.planner_role_id
            or workflow_run.explorer_role_ids != config.explorer_role_ids
            or workflow_run.coder_role_id != config.coder_role_id
            or workflow_run.reviewer_role_id != config.reviewer_role_id
            or workflow_run.max_parallel_explorers != config.max_parallel_explorers
            or workflow_run.max_rework_rounds != config.max_rework_rounds
        ):
            raise ValueError("evaluation workflow configuration drifted during execution")

    @staticmethod
    def _verification_succeeded(observations: Sequence[VerificationObservation]) -> bool:
        return bool(observations) and all(
            item.exit_code == 0 and item.timed_out is False for item in observations
        )

    @staticmethod
    def _safe_changed_paths(
        isolated: IsolatedWorkspace | None,
        previous: tuple[str, ...],
    ) -> tuple[str, ...]:
        if isolated is None:
            return previous
        try:
            return changed_paths(isolated.baseline_manifest, isolated.workspace)
        except (OSError, ValueError):
            return previous

    def _build_result(
        self,
        *,
        result_id: str,
        run: EvaluationRun,
        case: EvaluationCase,
        variant: EvaluationVariant,
        repetition: int,
        created_at: datetime,
        status: EvaluationResultStatus,
        runtime: _RuntimeFacts,
        isolated: IsolatedWorkspace | None,
        artifact_workspace: Path,
        environment: EnvironmentSnapshot,
        observations: Sequence[VerificationObservation],
        changed: tuple[str, ...],
        task_succeeded: bool,
        latency_ms: int,
        classification: FailureClassification | None,
        extra_facts: Sequence[EnvironmentFact],
    ) -> EvaluationResult:
        snapshot = self._result_snapshot(
            case=case,
            variant=variant,
            runtime=runtime,
            environment=environment,
            allow_role_drift=(
                status is EvaluationResultStatus.ERROR
                and classification is not None
                and classification.reason_code == "orchestration.role_snapshot_drift"
            ),
        )
        return EvaluationResult(
            id=result_id,
            run_id=run.id,
            case_id=case.id,
            variant_id=variant.id,
            repetition=repetition,
            status=status,
            metrics=self._metrics(
                session_ids=runtime.session_ids,
                session_events=runtime.session_events,
                workflow_events=runtime.workflow_events,
                verification=observations,
                task_succeeded=task_succeeded,
                contract_accuracy=patch_accuracy(case, changed),
                latency_ms=latency_ms,
                pricing_by_session=runtime.pricing_by_session,
            ),
            changed_paths=changed,
            snapshot=snapshot,
            artifact_workspace=self._artifact_workspace(
                isolated,
                artifact_workspace,
                run,
                case,
                variant,
                repetition,
            ),
            verification=self._verification_outcomes(case, observations),
            execution_facts=self._execution_facts(isolated, extra_facts),
            failure_analysis=(
                None if classification is None else self._failure_analysis(classification)
            ),
            trace_session_ids=runtime.session_ids,
            trace_workflow_run_id=runtime.workflow_run_id,
            created_at=created_at,
            finished_at=_utc_now(),
        )

    def _result_snapshot(
        self,
        *,
        case: EvaluationCase,
        variant: EvaluationVariant,
        runtime: _RuntimeFacts,
        environment: EnvironmentSnapshot,
        allow_role_drift: bool,
    ) -> EvaluationResultSnapshot:
        declared_roles = self._declared_roles(variant)
        if allow_role_drift:
            if not _same_role_ids(declared_roles, runtime.roles):
                raise ValueError("role drift result did not retain selected actual role snapshots")
            roles = runtime.roles
        elif _roles_compatible(declared_roles, runtime.roles):
            roles = runtime.roles
        else:
            roles = declared_roles
        memory = (
            runtime.memory if _memory_compatible(variant.memory, runtime.memory) else variant.memory
        )
        compatible_environment = (
            environment
            if _environment_compatible(case.environment, environment)
            else case.environment
        )
        return EvaluationResultSnapshot(
            roles=roles,
            memory=memory,
            environment=compatible_environment,
            workflow=variant.workflow,
            execution=variant.execution,
        )

    @staticmethod
    def _reserved_artifact_workspace(
        workspace: Path,
        run: EvaluationRun,
        case: EvaluationCase,
        variant: EvaluationVariant,
        repetition: int,
    ) -> ArtifactWorkspace:
        """Reserve the stable artifact namespace before any execution side effect."""

        return ArtifactWorkspace(
            local_workspace_path=str(workspace.resolve(strict=False)),
            artifact_ref=f"evaluation/{run.id}/{case.id}/{variant.id}/{repetition}",
        )

    @classmethod
    def _artifact_workspace(
        cls,
        isolated: IsolatedWorkspace | None,
        workspace: Path,
        run: EvaluationRun,
        case: EvaluationCase,
        variant: EvaluationVariant,
        repetition: int,
    ) -> ArtifactWorkspace:
        digest: str | None = None
        if isolated is not None:
            try:
                digest = _manifest_digest(workspace_manifest(isolated.workspace))
            except (OSError, ValueError):
                digest = None
        return cls._reserved_artifact_workspace(
            workspace,
            run,
            case,
            variant,
            repetition,
        ).model_copy(update={"workspace_sha256": digest})

    @staticmethod
    def _verification_outcomes(
        case: EvaluationCase,
        observations: Sequence[VerificationObservation],
    ) -> tuple[VerificationOutcome, ...]:
        outcomes: list[VerificationOutcome] = []
        for index, command in enumerate(case.verification_commands):
            observation = observations[index] if index < len(observations) else None
            outcomes.append(
                VerificationOutcome(
                    argv=command.argv,
                    exit_code=None if observation is None else observation.exit_code,
                    timed_out=None if observation is None else observation.timed_out,
                    duration_ms=None if observation is None else observation.duration_ms,
                    output_sha256=None if observation is None else observation.output_sha256,
                    output_chars=None if observation is None else observation.output_chars,
                    output_truncated=(
                        None if observation is None else observation.output_truncated
                    ),
                )
            )
        return tuple(outcomes)

    @staticmethod
    def _execution_facts(
        isolated: IsolatedWorkspace | None,
        extra_facts: Sequence[EnvironmentFact],
    ) -> tuple[EnvironmentFact, ...]:
        facts: list[EnvironmentFact] = list(extra_facts)
        if isolated is not None:
            facts.extend(
                (
                    EnvironmentFact(name="source_fixture_sha256", value=isolated.source_digest),
                    EnvironmentFact(
                        name="artifact_git_metadata_preserved",
                        value=str(isolated.git_metadata_preserved).lower(),
                    ),
                    EnvironmentFact(name="python_version", value=platform.python_version()),
                    EnvironmentFact(name="os", value=platform.system() or "unknown"),
                    EnvironmentFact(
                        name="architecture",
                        value=platform.machine() or "unknown",
                    ),
                )
            )
            if isolated.dependency_lock_digest is not None:
                facts.append(
                    EnvironmentFact(
                        name="dependency_lock_sha256",
                        value=isolated.dependency_lock_digest,
                    )
                )
            if isolated.git_head is not None:
                facts.append(EnvironmentFact(name="git_head", value=isolated.git_head))
            if isolated.git_dirty is not None:
                facts.append(
                    EnvironmentFact(
                        name="git_dirty",
                        value=str(isolated.git_dirty).lower(),
                    )
                )
            if isolated.skipped_external_symlinks:
                facts.append(
                    EnvironmentFact(
                        name="skipped_external_symlink_count",
                        value=str(len(isolated.skipped_external_symlinks)),
                    )
                )
        unique: dict[str, EnvironmentFact] = {}
        for fact in facts:
            unique.setdefault(fact.name, fact)
        return tuple(unique[name] for name in sorted(unique))

    def _metrics(
        self,
        *,
        session_ids: Sequence[str],
        session_events: Sequence[Event],
        workflow_events: Sequence[WorkflowRunEvent],
        verification: Sequence[VerificationObservation],
        task_succeeded: bool,
        contract_accuracy: float | None,
        latency_ms: int,
        pricing_by_session: dict[str, ModelPricing | None],
    ) -> EvaluationMetrics:
        model_completed = [
            event for event in session_events if event.event_type == "model.completed"
        ]
        usages = [event.payload.get("usage") for event in model_completed]
        tokens_known = bool(model_completed) and all(isinstance(usage, dict) for usage in usages)
        prompt_tokens = _sum_usage(usages, "prompt_tokens") if tokens_known else None
        completion_tokens = _sum_usage(usages, "completion_tokens") if tokens_known else None
        total_tokens = _sum_usage(usages, "total_tokens") if tokens_known else None
        approval_requests = sum(
            event.event_type == "tool.approval_required" for event in session_events
        )
        approvals_approved = sum(
            event.event_type == "tool.approval_decided" and event.payload.get("approved") is True
            for event in session_events
        )
        approvals_denied = sum(
            event.event_type == "tool.approval_decided" and event.payload.get("approved") is False
            for event in session_events
        )
        rework_rounds = sum(
            event.event_type == "workflow.rework_started" for event in workflow_events
        )
        repair_turns = sum(event.event_type == "test.failure_feedback" for event in session_events)
        verification_passed = bool(verification) and all(
            item.exit_code == 0 and item.timed_out is False for item in verification
        )
        return EvaluationMetrics(
            task_succeeded=task_succeeded,
            tests_passed=verification_passed,
            first_attempt_succeeded=(task_succeeded and repair_turns == 0 and rework_rounds == 0),
            verification_passed=verification_passed,
            patch_accuracy=contract_accuracy,
            repair_turns=repair_turns,
            rework_rounds=rework_rounds,
            model_calls=len(model_completed),
            tool_calls=sum(event.event_type == "tool.started" for event in session_events),
            tool_failures=sum(event.event_type == "tool.failed" for event in session_events),
            approval_requests=approval_requests,
            approvals_approved=approvals_approved,
            approvals_denied=approvals_denied,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost_usd=_cost_usd(
                session_ids=session_ids,
                model_events=model_completed,
                pricing_by_session=pricing_by_session,
            ),
            latency_ms=latency_ms,
        )

    @staticmethod
    def _failure_analysis(classification: FailureClassification) -> FailureAnalysis:
        evidence_by_key: dict[tuple[str, str], FailureEvidence] = {}
        for item in classification.evidence:
            evidence_by_key.setdefault(
                (item.event_type, item.event_id),
                FailureEvidence(
                    event_type=item.event_type,
                    event_id=item.event_id,
                    reason_code=classification.reason_code,
                ),
            )
        evidence = tuple(evidence_by_key.values())
        return FailureAnalysis(
            category=classification.category,
            reason_code=classification.reason_code,
            root_cause=classification.root_cause,
            evidence_event_types=tuple(dict.fromkeys(item.event_type for item in evidence)),
            evidence=evidence,
            inflection_event_id=(
                None if classification.inflection is None else classification.inflection.event_id
            ),
            inflection_event_type=(
                None if classification.inflection is None else classification.inflection.event_type
            ),
            confidence=classification.confidence,
        )


def _workflow_session_ids(events: Sequence[WorkflowRunEvent]) -> tuple[str, ...]:
    identifiers: list[str] = []
    for event in events:
        if event.session_id and event.session_id not in identifiers:
            identifiers.append(event.session_id)
    return tuple(identifiers)


def _role_preset_matches(
    expected: EvaluationRoleSnapshot,
    role: Any,
    profile: Any,
) -> bool:
    prompt_digest = expected.prompt.content_sha256 or _sha256_text(expected.prompt.content)
    policy = getattr(role, "tool_policy", None)
    if policy is None:
        return False
    policy_digest = _sha256_text(json.dumps(policy.model_dump(mode="json"), sort_keys=True))
    return (
        role.id == expected.role_id
        and role.version == expected.role_version
        and _sha256_text(role.system_prompt) == prompt_digest
        and profile.id == expected.model.model_profile_id
        and profile.model_id == expected.model.model_id
        and role.effort == expected.model.effort
        and policy_digest == expected.tool_policy_fingerprint
        and role.memory_scope == expected.memory_scope
        and role.budget.max_turns == expected.max_turns
        and role.budget.timeout_seconds == expected.timeout_seconds
    )


def _roles_compatible(
    declared: Sequence[EvaluationRoleSnapshot],
    actual: Sequence[EvaluationRoleSnapshot],
) -> bool:
    if len(declared) != len(actual):
        return False
    declared_by_id = {role.role_id: role for role in declared}
    actual_by_id = {role.role_id: role for role in actual}
    if set(declared_by_id) != set(actual_by_id):
        return False
    for role_id, expected in declared_by_id.items():
        captured = actual_by_id[role_id]
        if (
            expected.role_version != captured.role_version
            or expected.prompt.content_sha256 != captured.prompt.content_sha256
            or expected.model.model_profile_id != captured.model.model_profile_id
            or expected.model.model_id != captured.model.model_id
            or expected.model.effort != captured.model.effort
        ):
            return False
    return True


def _same_role_ids(
    declared: Sequence[EvaluationRoleSnapshot],
    actual: Sequence[EvaluationRoleSnapshot],
) -> bool:
    return len(declared) == len(actual) and {role.role_id for role in declared} == {
        role.role_id for role in actual
    }


def _memory_compatible(declared: MemorySnapshot, actual: MemorySnapshot) -> bool:
    return declared.enabled == actual.enabled and (
        not declared.references or declared.references == actual.references
    )


def _environment_compatible(
    declared: EnvironmentSnapshot,
    actual: EnvironmentSnapshot,
) -> bool:
    if declared.fixture_ref.rstrip("/") != actual.fixture_ref.rstrip("/"):
        return False
    for field in (
        "source_revision",
        "fixture_sha256",
        "container_image",
        "dependency_lock_sha256",
    ):
        expected = getattr(declared, field)
        if expected is not None and getattr(actual, field) != expected:
            return False
    actual_facts = {fact.name: fact.value for fact in actual.facts}
    return all(actual_facts.get(fact.name) == fact.value for fact in declared.facts)


def _cost_usd(
    *,
    session_ids: Sequence[str],
    model_events: Sequence[Event],
    pricing_by_session: dict[str, ModelPricing | None],
) -> float | None:
    """Price every live session conservatively, leaving missing facts unknown."""

    if not session_ids:
        return None
    events_by_session: dict[str, list[Event]] = {session_id: [] for session_id in session_ids}
    for event in model_events:
        if event.session_id in events_by_session:
            events_by_session[event.session_id].append(event)
    total = 0.0
    for session_id in session_ids:
        price = pricing_by_session.get(session_id)
        events = events_by_session[session_id]
        if (
            not events
            or price is None
            or price.input_usd_per_million_tokens is None
            or price.output_usd_per_million_tokens is None
            or price.reasoning_usd_per_million_tokens is not None
        ):
            return None
        prompt = _sum_usage([event.payload.get("usage") for event in events], "prompt_tokens")
        completion = _sum_usage(
            [event.payload.get("usage") for event in events],
            "completion_tokens",
        )
        if prompt is None or completion is None:
            return None
        total += (prompt * price.input_usd_per_million_tokens) / 1_000_000
        total += (completion * price.output_usd_per_million_tokens) / 1_000_000
    return round(total, 12)


def _session_succeeded(events: Sequence[Event]) -> bool:
    return bool(events and events[-1].event_type == "agent.completed")


def _sum_usage(usages: Sequence[Any], key: str) -> int | None:
    values: list[int] = []
    for usage in usages:
        if not isinstance(usage, dict):
            return None
        value = usage.get(key)
        if not isinstance(value, int) or value < 0:
            return None
        values.append(value)
    return sum(values)


@dataclass(frozen=True)
class _WorkflowSlots:
    main_role_id: str | None
    planner_role_id: str
    explorer_role_ids: tuple[str, ...]
    coder_role_id: str
    reviewer_role_id: str
    max_parallel_explorers: int
    max_rework_rounds: int
    roles: tuple[EvaluationRoleSnapshot, ...]


def _workflow_slots(variant: EvaluationVariant) -> _WorkflowSlots:
    """Resolve explicit domain role bindings; no role-name inference is allowed."""

    workflow = variant.workflow
    if workflow is None:
        raise ValueError("workflow evaluation variant is missing its workflow snapshot")
    return _WorkflowSlots(
        main_role_id=workflow.main_role_id,
        planner_role_id=workflow.planner_role_id,
        explorer_role_ids=workflow.explorer_role_ids,
        coder_role_id=workflow.coder_role_id,
        reviewer_role_id=workflow.reviewer_role_id,
        max_parallel_explorers=workflow.max_parallel_explorers,
        max_rework_rounds=workflow.max_rework_rounds,
        roles=workflow.roles,
    )
