from pathlib import Path

import pytest

from operant.domain.models import ToolPolicy
from operant.tools.workspace import ToolError, WorkspaceTools


@pytest.mark.asyncio
async def test_read_only_role_cannot_see_or_execute_patch(tmp_path: Path) -> None:
    target = tmp_path / "module.py"
    target.write_text("answer = 1\n", encoding="utf-8")
    tools = WorkspaceTools(
        tmp_path,
        policy=ToolPolicy(allowed_tools=("read_file", "search_files", "git_diff")),
    )

    assert {tool.name for tool in tools.definitions()} == {
        "read_file",
        "search_files",
        "git_diff",
    }
    with pytest.raises(ToolError, match="not allowed"):
        await tools.execute(
            "apply_patch",
            {"path": "module.py", "old_text": "1", "new_text": "2"},
        )
    assert target.read_text(encoding="utf-8") == "answer = 1\n"


@pytest.mark.asyncio
async def test_coder_can_modify_real_workspace_file(tmp_path: Path) -> None:
    target = tmp_path / "module.py"
    target.write_text("answer = 1\n", encoding="utf-8")
    tools = WorkspaceTools(
        tmp_path,
        policy=ToolPolicy(
            allowed_tools=("apply_patch",),
            workspace_write=True,
        ),
    )

    await tools.execute(
        "apply_patch",
        {"path": "module.py", "old_text": "answer = 1", "new_text": "answer = 42"},
    )

    assert target.read_text(encoding="utf-8") == "answer = 42\n"


def test_patch_with_identical_text_is_rejected_as_no_progress(tmp_path: Path) -> None:
    target = tmp_path / "module.py"
    target.write_text("answer = 1\n", encoding="utf-8")
    tools = WorkspaceTools(tmp_path)

    with pytest.raises(ToolError, match="would not change"):
        tools.apply_patch("module.py", "answer = 1", "answer = 1")
