"""Current local facts for applying verified Writer knowledge."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any

from operant.memory_plugins.retrieval import MemoryConditionContext


def workspace_facts(manager: Any, project: dict[str, Any]) -> MemoryConditionContext:
    """Read the registered workspace; unproved or dirty trees remain unavailable."""
    try:
        workspace = manager.store.get_workspace_initialization_by_id(project["workspace_id"])
        root = Path(workspace.workspace_ref).resolve(strict=True)

        def git(*args: str) -> str:
            return subprocess.run(
                ["git", "--no-optional-locks", "-C", str(root), *args],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            ).stdout.strip()

        if Path(git("rev-parse", "--show-toplevel")).resolve() != root:
            return MemoryConditionContext()
        if git("status", "--porcelain", "--untracked-files=normal"):
            return MemoryConditionContext()
        commit, tree = git("rev-parse", "HEAD", "HEAD^{tree}").splitlines()
        return MemoryConditionContext(
            commit_ref=f"git:{commit}",
            tree_digest=hashlib.sha256(f"git-tree:{tree}".encode("ascii")).hexdigest(),
        )
    except (OSError, ValueError, LookupError, subprocess.SubprocessError):
        return MemoryConditionContext()


def publication_active(manager: Any, ref: Any) -> bool:
    """A revoked promotion/registration cannot keep authorizing its publication."""
    with manager.store._connect() as connection:
        if (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='b26_writer_memory_evidence'"
            ).fetchone()
            is None
        ):
            return True
        rows = connection.execute(
            "SELECT e.*, r.state AS registration_state, "
            "r.permission_epoch AS registration_epoch FROM b26_writer_memory_evidence e "
            "LEFT JOIN b26_worktree_registrations r ON r.project_id=e.project_id "
            "AND r.worktree_id=e.worktree_id WHERE e.dataset_id=? "
            "AND json_extract(e.published_memory_ref_json,'$.record_id')=? "
            "AND json_extract(e.published_memory_ref_json,'$.version')=?",
            (ref.dataset_id, ref.record_id, ref.version),
        ).fetchall()
    return all(
        row["state"] == "published"
        and row["verification_status"] == "passed"
        and row["registration_state"] == "active"
        and row["registration_epoch"] == row["permission_epoch"]
        for row in rows
    )
