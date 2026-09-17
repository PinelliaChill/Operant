"""Actual Git conditions must be proved before a Writer result can be recalled."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

from operant.memory_plugins.worktree_knowledge import workspace_facts


def test_registered_git_facts_reject_dirty_or_different_tree(tmp_path):
    def git(*args):
        return subprocess.run(
            ["git", "-C", str(tmp_path), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.name", "B26 Test")
    git("config", "user.email", "test@example.invalid")
    (tmp_path / "check.txt").write_text("verified")
    git("add", "check.txt")
    git("commit", "-m", "verified tree")
    manager = SimpleNamespace(
        store=SimpleNamespace(
            get_workspace_initialization_by_id=lambda _: SimpleNamespace(
                workspace_ref=str(tmp_path)
            )
        )
    )
    original = workspace_facts(manager, {"workspace_id": "workspace"})
    assert original.commit_ref == f"git:{git('rev-parse', 'HEAD')}"
    assert original.tree_digest
    (tmp_path / "check.txt").write_text("unverified change")
    assert workspace_facts(manager, {"workspace_id": "workspace"}).tree_digest is None
    git("add", "check.txt")
    git("commit", "-m", "new tree")
    assert (
        workspace_facts(manager, {"workspace_id": "workspace"}).tree_digest != original.tree_digest
    )
    (tmp_path / "untracked.txt").write_text("unreviewed addition")
    assert workspace_facts(manager, {"workspace_id": "workspace"}).tree_digest is None
