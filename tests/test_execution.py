import os
import shutil
import sys
from pathlib import Path

import pytest

from operant.application.defaults import default_role_presets
from operant.domain.models import CommandExecutionPolicy, CommandRunnerType, ToolPolicy
from operant.tools.execution import DockerCommandRunner
from operant.tools.workspace import ApprovalRequired, ToolError, WorkspaceTools


def test_docker_argv_has_network_resource_and_mount_boundaries(tmp_path: Path) -> None:
    policy = CommandExecutionPolicy(
        runner=CommandRunnerType.DOCKER,
        docker_image="example/operant-test:latest",
        cpu_limit=1.5,
        memory_limit_mb=768,
        pids_limit=128,
    )
    snapshot = tmp_path / "snapshot"
    argv = DockerCommandRunner.build_argv(
        argv=("python", "-m", "pytest", "-q"),
        snapshot=snapshot,
        relative_cwd=Path("nested"),
        cidfile=tmp_path / "container-id",
        workspace_write=True,
        policy=policy,
    )

    assert argv[:4] == ["docker", "run", "--rm", "--init"]
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--cpus") + 1] == "1.5"
    assert argv[argv.index("--memory") + 1] == "768m"
    assert argv[argv.index("--pids-limit") + 1] == "128"
    assert "--read-only" in argv
    assert argv[argv.index("--workdir") + 1] == "/workspace/nested"
    mount = argv[argv.index("--mount") + 1]
    assert f"src={snapshot.resolve()}" in mount
    assert "dst=/workspace" in mount
    assert "readonly" not in mount
    assert argv[-5:] == ["example/operant-test:latest", "python", "-m", "pytest", "-q"]

    readonly_argv = DockerCommandRunner.build_argv(
        argv=("python", "-m", "pytest", "-q"),
        snapshot=snapshot,
        relative_cwd=Path("."),
        cidfile=tmp_path / "readonly-container-id",
        workspace_write=False,
        policy=policy,
    )
    readonly_mount = readonly_argv[readonly_argv.index("--mount") + 1]
    assert "readonly" in readonly_mount


def test_default_coder_uses_docker_runner() -> None:
    roles = default_role_presets(
        planner_model_profile_id="model_planner",
        coder_model_profile_id="model_coder",
        reviewer_model_profile_id="model_reviewer",
    )
    coder = next(role for role in roles if role.id == "role_coder")

    assert coder.tool_policy.command_execution_policy.runner is CommandRunnerType.DOCKER


def test_docker_snapshot_excludes_secrets_and_local_runtime_data(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "visible.py").write_text("answer = 42\n", encoding="utf-8")
    (source / ".env").write_text("PRIVATE=value\n", encoding="utf-8")
    (source / ".ENV").write_text("PRIVATE=uppercase\n", encoding="utf-8")
    (source / ".NPMRC").write_text("TOKEN=uppercase\n", encoding="utf-8")
    (source / "ID_RSA").write_text("private key\n", encoding="utf-8")
    (source / "CREDENTIALS.JSON").write_text("{}\n", encoding="utf-8")
    (source / ".git").mkdir()
    (source / ".git" / "config").write_text("private git metadata\n", encoding="utf-8")
    lowercase = source / "lowercase"
    (lowercase / ".operant").mkdir(parents=True)
    (lowercase / ".operant" / "runtime.sqlite3").write_text("runtime data\n", encoding="utf-8")
    uppercase_runtime_names = (
        ".OPERANT",
        ".VENV",
        "NODE_MODULES",
        "__PYCACHE__",
        ".PYTEST_CACHE",
    )
    for name in uppercase_runtime_names:
        directory = source / name
        directory.mkdir()
        (directory / "must-not-copy.txt").write_text("local data\n", encoding="utf-8")
    snapshot = tmp_path / "snapshot"

    DockerCommandRunner._copy_workspace_snapshot(source, snapshot)

    assert (snapshot / "visible.py").is_file()
    assert not (snapshot / ".env").exists()
    assert not (snapshot / ".ENV").exists()
    assert not (snapshot / ".NPMRC").exists()
    assert not (snapshot / "ID_RSA").exists()
    assert not (snapshot / "CREDENTIALS.JSON").exists()
    assert not (snapshot / ".git").exists()
    assert not (snapshot / "lowercase" / ".operant").exists()
    for name in uppercase_runtime_names:
        assert not (snapshot / name).exists()


@pytest.mark.asyncio
async def test_shell_command_requires_approval_before_execution(tmp_path: Path) -> None:
    tools = WorkspaceTools(
        tmp_path,
        policy=ToolPolicy(allowed_tools=("run_command",), command_execution=True),
    )

    with pytest.raises(ApprovalRequired, match="shell"):
        await tools.run_command(["sh", "-c", "echo should-not-run"])


@pytest.mark.asyncio
async def test_host_runner_reports_timeout_and_output_truncation(tmp_path: Path) -> None:
    tools = WorkspaceTools(
        tmp_path,
        policy=ToolPolicy(allowed_tools=("run_command",), command_execution=True),
        output_limit=4,
    )

    result = await tools.run_command([sys.executable, "-c", "print('abcdefgh')"])
    assert result["stdout"] == "abcd"
    assert result["stdout_truncated"] is True

    with pytest.raises(ToolError, match="timed out"):
        await tools.run_command(
            [sys.executable, "-c", "import time; time.sleep(30)"], timeout_seconds=1
        )


@pytest.mark.asyncio
async def test_docker_runner_integration_when_an_image_is_explicitly_provided(
    tmp_path: Path,
) -> None:
    image = os.getenv("OPERANT_DOCKER_TEST_IMAGE")
    if shutil.which("docker") is None or not image:
        pytest.skip("requires Docker and OPERANT_DOCKER_TEST_IMAGE")

    policy = ToolPolicy(
        allowed_tools=("run_command",),
        command_execution=True,
        workspace_write=True,
        command_execution_policy=CommandExecutionPolicy(
            runner=CommandRunnerType.DOCKER,
            docker_image=image,
        ),
    )
    result = await WorkspaceTools(tmp_path, policy=policy).run_command(
        ["python", "-c", "print('isolated')"]
    )

    assert result["runner"] == "docker"
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "isolated"
