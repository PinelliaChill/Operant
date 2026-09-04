from __future__ import annotations

import fcntl
import hashlib
import subprocess
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from operant.application.multiwriter import MergeOutcomeUnknownError
from operant.domain.multiwriter import (
    MergeRun,
    MergeStrategy,
    PatchCommitArtifact,
    WriterArtifactKind,
    WriterConflict,
    WriterIsolationKind,
    WriterWorkspace,
)


@dataclass(frozen=True)
class _VerifiedArtifact:
    commit: str | None = None
    patch: bytes | None = None


class TrustedGitMultiWriterAdapter:
    """Verify and merge artifacts only inside administrator-mapped Git worktrees."""

    def __init__(
        self,
        isolation_roots: Mapping[str, str | Path],
        *,
        command_timeout_seconds: int = 60,
        max_patch_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        if not isolation_roots:
            raise ValueError("at least one trusted Git isolation root is required")
        if not 1 <= command_timeout_seconds <= 600:
            raise ValueError("Git command timeout must be between 1 and 600 seconds")
        if not 1 <= max_patch_bytes <= 64 * 1024 * 1024:
            raise ValueError("Git patch limit must be between 1 byte and 64 MiB")
        self.command_timeout_seconds = command_timeout_seconds
        self.max_patch_bytes = max_patch_bytes
        self._roots: dict[str, Path] = {}
        self._rollback_heads: dict[str, str] = {}
        for reference, configured in isolation_roots.items():
            root = Path(configured)
            if not reference or len(reference) > 500 or not root.is_absolute():
                raise ValueError(
                    "trusted Git isolation roots require bounded refs and absolute paths"
                )
            resolved = root.resolve(strict=True)
            if not resolved.is_dir() or self._git_text(
                resolved, "rev-parse", "--show-toplevel"
            ) != str(resolved):
                raise ValueError(f"trusted Git isolation root is not a worktree: {reference}")
            self._roots[reference] = resolved
        if len(set(self._roots.values())) != len(self._roots):
            raise ValueError("trusted Git isolation refs must map to distinct worktrees")

    def verify(self, workspace: WriterWorkspace, artifact: PatchCommitArtifact) -> None:
        self._verify(workspace, artifact)

    def _verify(
        self, workspace: WriterWorkspace, artifact: PatchCommitArtifact
    ) -> _VerifiedArtifact:
        if workspace.isolation_kind is not WriterIsolationKind.WORKTREE:
            raise ValueError("Git adapter only accepts worktree writer isolation")
        root = self._root(workspace.isolation_ref)
        base = self._commit(root, artifact.base_revision)
        if base != self._commit(root, workspace.base_revision):
            raise ValueError("writer artifact base does not match its isolated workspace")
        changed_paths: tuple[str, ...]
        content: bytes
        verified: _VerifiedArtifact
        if artifact.artifact_kind is WriterArtifactKind.COMMIT:
            if artifact.result_revision is None:
                raise ValueError("commit artifact requires result revision")
            result = self._commit(root, artifact.result_revision)
            if artifact.artifact_ref != f"git:{result}":
                raise ValueError("commit artifact_ref must bind the exact verified commit")
            self._git_text(root, "merge-base", "--is-ancestor", base, result)
            changed_paths = self._changed_paths(root, base, result)
            content = self._git_bytes(root, "diff", "--binary", base, result)
            verified = _VerifiedArtifact(commit=result)
        else:
            patch = self._patch_path(root, artifact.artifact_ref)
            assert patch is not None
            content = self._read_patch_snapshot(patch)
            self._git_input_text(root, content, "apply", "--check", "-")
            changed_paths = self._patch_paths(root, content)
            verified = _VerifiedArtifact(patch=content)
        if hashlib.sha256(content).hexdigest() != artifact.artifact_sha256:
            raise ValueError("writer artifact checksum does not match")
        if tuple(sorted(artifact.changed_paths)) != changed_paths:
            raise ValueError("writer artifact changed_paths do not match Git evidence")
        if any(not self._owned(path, workspace.ownership_paths) for path in changed_paths):
            raise ValueError("writer artifact changes a path outside its ownership")
        return verified

    def workspace_path(self, isolation_ref: str) -> Path:
        """Resolve an opaque isolation ref only through the administrator mapping."""
        return self._root(isolation_ref)

    def merge(
        self,
        *,
        merge: MergeRun,
        artifacts: tuple[PatchCommitArtifact, ...],
        workspaces: tuple[WriterWorkspace, ...],
        resolutions: tuple[WriterConflict, ...],
    ) -> str:
        del resolutions
        if len(artifacts) != len(workspaces):
            raise RuntimeError("merge artifacts and writer workspaces are not aligned")
        workspace_by_id = {item.writer_workspace_id: item for item in workspaces}
        if len(workspace_by_id) != len(workspaces):
            raise RuntimeError("merge writer workspaces must be unique")
        verified_by_id: dict[str, _VerifiedArtifact] = {}
        for artifact in artifacts:
            try:
                workspace = workspace_by_id[artifact.writer_workspace_id]
            except KeyError as exc:
                raise RuntimeError("merge artifact has no trusted writer workspace") from exc
            verified_by_id[artifact.writer_artifact_id] = self._verify(workspace, artifact)
        target = self._root(merge.target_isolation_ref)
        with self._target_lock(target):
            if self._git_text(target, "status", "--porcelain", "--untracked-files=all"):
                raise RuntimeError("merge target worktree must be clean")
            baseline = self._commit(target, "HEAD")
            expected = self._commit(target, merge.base_revision)
            if baseline != expected:
                raise RuntimeError("merge target HEAD does not match the frozen base revision")
            expected_tree = self._expected_merge_tree(
                target,
                merge=merge,
                artifacts=artifacts,
                verified_by_id=verified_by_id,
            )
            self._rollback_heads[merge.merge_run_id] = baseline
            try:
                self._apply_verified_artifacts(
                    target,
                    strategy=merge.strategy,
                    artifacts=artifacts,
                    verified_by_id=verified_by_id,
                )
                self._assert_target_identity(target, baseline)
                if self._git_text(target, "write-tree") != expected_tree:
                    raise RuntimeError(
                        "merge target staged tree differs from the verified artifact result"
                    )
                parents = [baseline]
                if merge.strategy is MergeStrategy.THREE_WAY:
                    parents.extend(
                        revision
                        for artifact in artifacts
                        if (revision := verified_by_id[artifact.writer_artifact_id].commit)
                        is not None
                    )
                commit_arguments = ["commit-tree", expected_tree]
                for parent in parents:
                    commit_arguments.extend(("-p", parent))
                commit_arguments.extend(("-m", f"operant merge {merge.merge_run_id}"))
                result = self._git_text(
                    target,
                    "-c",
                    "user.name=Operant Merge Node",
                    "-c",
                    "user.email=operant@localhost",
                    *commit_arguments,
                )
                self._assert_target_identity(target, baseline)
                self._git_text(target, "update-ref", "HEAD", result, baseline)
                self._git_text(target, "merge", "--quit", check=False)
                self._git_text(target, "cherry-pick", "--quit", check=False)
            except Exception as exc:
                self._rollback_heads.pop(merge.merge_run_id, None)
                raise MergeOutcomeUnknownError(
                    "merge target changed after isolated verification; inspect it before retrying"
                ) from exc
            else:
                self._rollback_heads.pop(merge.merge_run_id, None)
                return f"git:{result}"

    def _expected_merge_tree(
        self,
        target: Path,
        *,
        merge: MergeRun,
        artifacts: tuple[PatchCommitArtifact, ...],
        verified_by_id: Mapping[str, _VerifiedArtifact],
    ) -> str:
        """Build the expected tree in a private checkout, never from target staging state."""
        with tempfile.TemporaryDirectory(prefix="operant-merge-verify-") as directory:
            isolated = Path(directory) / "checkout"
            self._git_text(
                target,
                "clone",
                "--shared",
                "--no-checkout",
                "--quiet",
                str(target),
                str(isolated),
            )
            self._git_text(isolated, "checkout", "--detach", "--quiet", merge.base_revision)
            self._apply_verified_artifacts(
                isolated,
                strategy=merge.strategy,
                artifacts=artifacts,
                verified_by_id=verified_by_id,
            )
            return self._git_text(isolated, "write-tree")

    def _apply_verified_artifacts(
        self,
        root: Path,
        *,
        strategy: MergeStrategy,
        artifacts: tuple[PatchCommitArtifact, ...],
        verified_by_id: Mapping[str, _VerifiedArtifact],
    ) -> None:
        if strategy is MergeStrategy.APPLY_PATCH:
            if any(item.artifact_kind is not WriterArtifactKind.PATCH for item in artifacts):
                raise RuntimeError("apply_patch merge requires only patch artifacts")
            for artifact in artifacts:
                patch = verified_by_id[artifact.writer_artifact_id].patch
                if patch is None:
                    raise RuntimeError("verified patch snapshot is missing")
                self._git_input_text(root, patch, "apply", "--3way", "-")
            return
        if any(item.artifact_kind is not WriterArtifactKind.COMMIT for item in artifacts):
            raise RuntimeError("Git commit merge requires only commit artifacts")
        revisions = [verified_by_id[item.writer_artifact_id].commit for item in artifacts]
        if any(revision is None for revision in revisions):
            raise RuntimeError("verified commit identity is missing")
        if strategy is MergeStrategy.CHERRY_PICK:
            for revision in revisions:
                assert revision is not None
                self._git_text(root, "cherry-pick", "--no-commit", revision)
            return
        self._git_text(
            root,
            "merge",
            "--no-ff",
            "--no-commit",
            *(revision for revision in revisions if revision is not None),
        )

    def rollback(self, *, merge: MergeRun) -> None:
        target = self._root(merge.target_isolation_ref)
        baseline = self._rollback_heads.pop(merge.merge_run_id, None)
        if baseline is None:
            return
        with self._target_lock(target):
            self._rollback_target(target, baseline)

    def _root(self, reference: str) -> Path:
        try:
            return self._roots[reference]
        except KeyError as exc:
            raise ValueError("unknown trusted Git isolation reference") from exc

    def _patch_path(self, root: Path, reference: str, *, required: bool = True) -> Path | None:
        if not reference.startswith("patch:"):
            if required:
                raise ValueError("patch artifact_ref must use patch:<relative-path>")
            return None
        relative = reference.removeprefix("patch:")
        candidate = (root / relative).resolve()
        if (
            not relative
            or not candidate.is_relative_to(root)
            or (required and not candidate.is_file())
        ):
            if required:
                raise ValueError("patch artifact_ref escapes or is missing from its worktree")
            return None
        return candidate if candidate.is_file() else None

    def _patch_paths(self, root: Path, patch: bytes) -> tuple[str, ...]:
        output = self._git_input_text(root, patch, "apply", "--numstat", "-")
        paths: list[str] = []
        for line in output.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3 or any(character in parts[2] for character in "\r\n\t"):
                raise ValueError("patch contains an unsupported path encoding")
            paths.append(parts[2])
        return tuple(sorted(paths))

    def _read_patch_snapshot(self, patch: Path) -> bytes:
        with patch.open("rb") as handle:
            content = handle.read(self.max_patch_bytes + 1)
        if len(content) > self.max_patch_bytes:
            raise ValueError("writer patch exceeds the configured size limit")
        return content

    @contextmanager
    def _target_lock(self, target: Path) -> Iterator[None]:
        lock_ref = Path(self._git_text(target, "rev-parse", "--git-path", "operant-merge.lock"))
        lock_path = lock_ref if lock_ref.is_absolute() else (target / lock_ref).resolve()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as handle:
            self._acquire_lock(handle)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _acquire_lock(handle: BinaryIO) -> None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("merge target is locked by another merge") from exc

    def _assert_target_identity(self, target: Path, baseline: str) -> None:
        if self._git_text(target, "rev-parse", "--show-toplevel") != str(target):
            raise MergeOutcomeUnknownError("merge target identity changed during merge")
        if self._commit(target, "HEAD") != baseline:
            raise MergeOutcomeUnknownError("merge target HEAD changed during merge")

    def _rollback_target(self, target: Path, baseline: str) -> None:
        self._git_text(target, "merge", "--abort", check=False)
        self._git_text(target, "cherry-pick", "--abort", check=False)
        self._git_text(target, "reset", "--hard", baseline)
        self._git_text(target, "clean", "-fd")

    def _changed_paths(self, root: Path, base: str, result: str) -> tuple[str, ...]:
        output = self._git_bytes(root, "diff", "--name-only", "-z", base, result)
        return tuple(sorted(item.decode("utf-8") for item in output.split(b"\0") if item))

    @staticmethod
    def _owned(path: str, ownership: tuple[str, ...]) -> bool:
        return any(path == root or path.startswith(f"{root}/") for root in ownership)

    def _commit(self, root: Path, revision: str) -> str:
        return self._git_text(root, "rev-parse", "--verify", f"{revision}^{{commit}}")

    def _git_text(self, root: Path, *arguments: str, check: bool = True) -> str:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=self.command_timeout_seconds,
        )
        if check and completed.returncode != 0:
            detail = completed.stderr.strip()[-2_000:]
            raise RuntimeError(f"isolated Git operation failed: {detail}")
        return completed.stdout.strip()

    def _git_bytes(self, root: Path, *arguments: str) -> bytes:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=False,
            capture_output=True,
            timeout=self.command_timeout_seconds,
        )
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()[-2_000:]
            raise RuntimeError(f"isolated Git operation failed: {detail}")
        return completed.stdout

    def _git_input_text(self, root: Path, payload: bytes, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=False,
            capture_output=True,
            input=payload,
            timeout=self.command_timeout_seconds,
        )
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()[-2_000:]
            raise RuntimeError(f"isolated Git operation failed: {detail}")
        return completed.stdout.decode("utf-8", errors="strict").strip()
