from pathlib import Path

import pytest
from pydantic import ValidationError

from operant.domain.workflow import WorkflowRun


def test_workflow_run_normalizes_absolute_workspace(tmp_path: Path) -> None:
    run = WorkflowRun(
        task="Fix calculator",
        workspace=str(tmp_path / "project" / ".." / "project"),
        planner_role_id="role_planner",
        explorer_role_ids=("role_explorer",),
        coder_role_id="role_coder",
        reviewer_role_id="role_reviewer",
    )

    assert run.workspace == str((tmp_path / "project").resolve())


def test_workflow_run_rejects_relative_workspace_and_duplicate_explorers() -> None:
    base = {
        "task": "Fix calculator",
        "planner_role_id": "role_planner",
        "coder_role_id": "role_coder",
        "reviewer_role_id": "role_reviewer",
    }
    with pytest.raises(ValidationError, match="absolute path"):
        WorkflowRun(workspace="relative/project", **base)
    with pytest.raises(ValidationError, match="must not contain duplicates"):
        WorkflowRun(
            workspace="/tmp/project",
            explorer_role_ids=("role_explorer", "role_explorer"),
            **base,
        )
