from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import typer
from pydantic import BaseModel, ValidationError
from rich.console import Console
from rich.table import Table

from operant.application.evaluation import EvaluationRunner
from operant.application.service import ApplicationService
from operant.application.trace import session_trace_jsonl, summarize_session_trace
from operant.application.workflow import SequentialCodingWorkflow
from operant.domain.evaluation import EvaluationResult, EvaluationSuite
from operant.domain.memory import MemoryKind
from operant.domain.models import (
    Budget,
    CommandExecutionPolicy,
    CommandRunnerType,
    Effort,
    ModelProfile,
    RolePreset,
    RoleSnapshot,
    ToolPolicy,
)
from operant.domain.workflow import WorkflowRunStatus
from operant.persistence.sqlite import NotFoundError, SQLiteStore
from operant.providers.openai_compatible import OpenAICompatibleProvider
from operant.settings import database_path, load_local_env

app = typer.Typer(no_args_is_help=True, help="由角色预设驱动的多模型 Coding Agent Runtime。")
model_app = typer.Typer(no_args_is_help=True, help="管理 Model Profile。")
role_app = typer.Typer(no_args_is_help=True, help="管理 Role Preset。")
session_app = typer.Typer(no_args_is_help=True, help="创建、查看和运行 Session。")
workflow_app = typer.Typer(
    no_args_is_help=True,
    help="运行 Planner → 只读 Explorer → Coder → Reviewer 工作流。",
)
memory_app = typer.Typer(no_args_is_help=True, help="按 Session Role Snapshot 管理 Memory。")
evaluation_app = typer.Typer(no_args_is_help=True, help="管理并顺序运行可复现 Evaluation Suite。")
evaluation_suite_app = typer.Typer(no_args_is_help=True, help="创建和查询 Evaluation Suite。")
evaluation_result_app = typer.Typer(no_args_is_help=True, help="查询 Evaluation Result。")
app.add_typer(model_app, name="model")
app.add_typer(role_app, name="role")
app.add_typer(session_app, name="session")
app.add_typer(workflow_app, name="workflow")
app.add_typer(memory_app, name="memory")
evaluation_app.add_typer(evaluation_suite_app, name="suite")
evaluation_app.add_typer(evaluation_result_app, name="result")
app.add_typer(evaluation_app, name="evaluation")
console = Console()


def _service() -> ApplicationService:
    load_local_env()
    service = ApplicationService(
        SQLiteStore(database_path()),
        OpenAICompatibleProvider(),
    )
    service.initialize()
    return service


def _required_base_url(value: str | None) -> str:
    resolved = value or os.environ.get("OPERANT_BASE_URL")
    if not resolved:
        raise typer.BadParameter("请通过 --base-url 或 OPERANT_BASE_URL 提供中转站地址")
    return resolved


def _json_event(event: BaseModel) -> str:
    return json.dumps(event.model_dump(mode="json"), ensure_ascii=False)


def _print_evaluation_json(payload: object) -> None:
    console.print_json(json.dumps(payload, ensure_ascii=False))


def _safe_evaluation_result_payload(result: EvaluationResult) -> dict[str, Any]:
    """Keep local artifact paths local when rendering CLI JSON."""

    payload = result.model_dump(mode="json")
    artifact_workspace = payload.get("artifact_workspace")
    if isinstance(artifact_workspace, dict):
        artifact_workspace.pop("local_workspace_path", None)
    return payload


def _evaluation_artifact_root(value: Path | None) -> Path:
    """Resolve a caller-provided absolute root or the database-local default."""

    if value is None:
        return (database_path().expanduser().resolve().parent / "evaluations").resolve()
    candidate = value.expanduser()
    if not candidate.is_absolute():
        raise typer.BadParameter("--artifact-root 必须是绝对路径")
    return candidate.resolve(strict=False)


def _safe_evaluation_error_type(exc: Exception) -> str:
    if isinstance(exc, NotFoundError):
        return "not_found"
    if isinstance(exc, ValueError):
        return "validation_error"
    if isinstance(exc, OSError):
        return "io_error"
    return "runner_error"


def _tool_policy(
    writable: bool,
    *,
    command_runner: CommandRunnerType = CommandRunnerType.DOCKER,
    docker_image: str = "python:3.13-slim",
    cpu_limit: float = 1.0,
    memory_limit_mb: int = 512,
    pids_limit: int = 256,
) -> ToolPolicy:
    tools = (
        ("read_file", "search_files", "apply_patch", "run_command", "git_diff")
        if writable
        else ("read_file", "search_files", "git_diff")
    )
    return ToolPolicy(
        allowed_tools=tools,
        workspace_write=writable,
        command_execution=writable,
        command_execution_policy=CommandExecutionPolicy(
            runner=command_runner,
            docker_image=docker_image,
            cpu_limit=cpu_limit,
            memory_limit_mb=memory_limit_mb,
            pids_limit=pids_limit,
        ),
    )


@app.command("init", help="初始化本地 SQLite 数据库。")
def initialize() -> None:
    _service()
    console.print(f"Operant 数据库已初始化：{database_path()}")


@app.command("serve", help="启动仅限本机或私有网络的 Operant Core。")
def serve(
    host: str = typer.Option("127.0.0.1", help="仅允许 localhost、回环或明确的私有 IP。"),
    port: int = typer.Option(8000, min=1, max=65535),
    ssl_certfile: Path | None = typer.Option(None, help="TLS 证书绝对路径。"),
    ssl_keyfile: Path | None = typer.Option(None, help="TLS 私钥绝对路径。"),
    desktop: bool = typer.Option(False, help="仅在回环地址允许固定 Tauri Origin。"),
) -> None:
    from operant.server import ServerConfigurationError, run_server

    try:
        run_server(
            host=host,
            port=port,
            ssl_certfile=ssl_certfile,
            ssl_keyfile=ssl_keyfile,
            desktop=desktop,
        )
    except ServerConfigurationError as exc:
        raise typer.BadParameter(str(exc)) from exc


@model_app.command("add", help="添加一个 Model Profile。")
def add_model(
    name: str = typer.Option(..., help="Model Profile 名称。"),
    model_id: str = typer.Option(..., help="中转站使用的精确模型 ID。"),
    base_url: str | None = typer.Option(None, help="默认读取 OPERANT_BASE_URL。"),
    secret_ref: str = typer.Option("OPERANT_API_KEY", help="保存 API Key 的环境变量名。"),
    efforts: str = typer.Option("low,medium,high", help="支持的 effort，逗号分隔。"),
    effort_parameter: str | None = typer.Option(
        "reasoning_effort", help="Provider 的 effort 参数名。"
    ),
) -> None:
    profile = _service().add_model_profile(
        ModelProfile(
            name=name,
            model_id=model_id,
            base_url=_required_base_url(base_url),
            secret_ref=secret_ref,
            supported_efforts=tuple(Effort(item.strip()) for item in efforts.split(",")),
            effort_parameter=effort_parameter,
        )
    )
    console.print(profile.id)


@model_app.command("list", help="列出所有 Model Profile。")
def list_models() -> None:
    table = Table("ID", "名称", "模型 ID", "Provider", "启用")
    for profile in _service().list_model_profiles():
        table.add_row(
            profile.id,
            profile.name,
            profile.model_id,
            profile.provider,
            "是" if profile.enabled else "否",
        )
    console.print(table)


@model_app.command("show", help="查看一个 Model Profile。")
def show_model(profile_id: str = typer.Argument(help="Model Profile ID。")) -> None:
    console.print_json(_service().get_model_profile(profile_id).model_dump_json(indent=2))


@model_app.command("update", help="更新 Model Profile。")
def update_model(
    profile_id: str = typer.Argument(help="Model Profile ID。"),
    name: str | None = typer.Option(None),
    model_id: str | None = typer.Option(None),
    base_url: str | None = typer.Option(None),
    default_effort: Effort | None = typer.Option(None),
) -> None:
    changes = {
        key: value
        for key, value in {
            "name": name,
            "model_id": model_id,
            "base_url": base_url,
            "default_effort": default_effort,
        }.items()
        if value is not None
    }
    profile = _service().update_model_profile(profile_id, **changes)
    console.print_json(profile.model_dump_json(indent=2))


@model_app.command("deactivate", help="停用 Model Profile。")
def deactivate_model(profile_id: str = typer.Argument(help="Model Profile ID。")) -> None:
    profile = _service().deactivate_model_profile(profile_id)
    console.print(f"{profile.id}: 已停用")


@model_app.command("discover", help="查询中转站提供的精确模型 ID。")
def discover_models(
    base_url: str | None = typer.Option(None, help="默认读取 OPERANT_BASE_URL。"),
    secret_ref: str = typer.Option("OPERANT_API_KEY", help="保存 API Key 的环境变量名。"),
) -> None:
    model_ids = asyncio.run(
        _service().discover_models(
            base_url=_required_base_url(base_url),
            secret_ref=secret_ref,
        )
    )
    for model_id in model_ids:
        console.print(model_id)


@model_app.command("health", help="检查 Profile 中的模型 ID 是否存在于中转站。")
def model_health(profile_id: str = typer.Argument(help="Model Profile ID。")) -> None:
    result = asyncio.run(_service().check_model_profile(profile_id))
    console.print_json(json.dumps(result, ensure_ascii=False))


@role_app.command("add", help="创建 Role Preset。")
def add_role(
    name: str = typer.Option(..., help="角色名称。"),
    model_profile_id: str = typer.Option(..., help="绑定的 Model Profile ID。"),
    system_prompt: str = typer.Option(..., help="角色的 System Prompt。"),
    effort: Effort = typer.Option(Effort.MEDIUM, help="reasoning effort。"),
    writable: bool = typer.Option(False, help="是否允许修改 workspace。"),
    command_runner: CommandRunnerType = typer.Option(
        CommandRunnerType.DOCKER,
        help="可写角色的命令 Runner；host 只适用于可信 workspace。",
    ),
    docker_image: str = typer.Option("python:3.13-slim", help="Docker Runner 使用的镜像。"),
    cpu_limit: float = typer.Option(1.0, min=0.1, max=64),
    memory_limit_mb: int = typer.Option(512, min=64, max=262_144),
    pids_limit: int = typer.Option(256, min=16, max=65_536),
    timeout_seconds: int = typer.Option(300, min=1, max=3600),
) -> None:
    role = _service().create_role(
        RolePreset(
            name=name,
            model_profile_id=model_profile_id,
            system_prompt=system_prompt,
            effort=effort,
            tool_policy=_tool_policy(
                writable,
                command_runner=command_runner,
                docker_image=docker_image,
                cpu_limit=cpu_limit,
                memory_limit_mb=memory_limit_mb,
                pids_limit=pids_limit,
            ),
            budget=Budget(timeout_seconds=timeout_seconds),
        )
    )
    console.print(role.id)


@role_app.command("list", help="列出 Role Preset 当前版本。")
def list_roles(include_inactive: bool = typer.Option(False)) -> None:
    table = Table("ID", "版本", "名称", "Model Profile", "状态")
    for role in _service().list_roles(include_inactive=include_inactive):
        table.add_row(
            role.id,
            str(role.version),
            role.name,
            role.model_profile_id,
            role.status.value,
        )
    console.print(table)


@role_app.command("show", help="查看 Role Preset 或指定历史版本。")
def show_role(
    role_id: str = typer.Argument(help="Role ID。"),
    version: int | None = typer.Option(None, min=1),
) -> None:
    console.print_json(_service().get_role(role_id, version).model_dump_json(indent=2))


@role_app.command("versions", help="列出 Role Preset 的不可变历史版本。")
def role_versions(role_id: str = typer.Argument(help="Role ID。")) -> None:
    for role in _service().list_role_versions(role_id):
        console.print_json(role.model_dump_json())


@role_app.command("update", help="创建 Role Preset 的新版本。")
def update_role(
    role_id: str = typer.Argument(help="Role ID。"),
    name: str | None = typer.Option(None),
    system_prompt: str | None = typer.Option(None),
    model_profile_id: str | None = typer.Option(None),
    effort: Effort | None = typer.Option(None),
) -> None:
    changes: dict[str, Any] = {
        key: value
        for key, value in {
            "name": name,
            "system_prompt": system_prompt,
            "model_profile_id": model_profile_id,
            "effort": effort,
        }.items()
        if value is not None
    }
    role = _service().update_role(role_id, **changes)
    console.print_json(role.model_dump_json(indent=2))


@role_app.command("copy", help="复制 Role Preset 为新的角色。")
def copy_role(
    role_id: str = typer.Argument(help="源 Role ID。"),
    name: str = typer.Option(..., help="新角色名称。"),
) -> None:
    console.print(_service().copy_role(role_id, name=name).id)


@role_app.command("deactivate", help="停用 Role Preset。")
def deactivate_role(role_id: str = typer.Argument(help="Role ID。")) -> None:
    role = _service().deactivate_role(role_id)
    console.print(f"{role.id}@{role.version}: 已停用")


@role_app.command("seed-defaults", help="创建 main/planner/explorer/coder/reviewer 默认角色。")
def seed_default_roles(
    planner_model_profile_id: str = typer.Option(...),
    coder_model_profile_id: str = typer.Option(...),
    reviewer_model_profile_id: str = typer.Option(...),
) -> None:
    roles = _service().seed_default_roles(
        planner_model_profile_id=planner_model_profile_id,
        coder_model_profile_id=coder_model_profile_id,
        reviewer_model_profile_id=reviewer_model_profile_id,
    )
    for role in roles:
        console.print(f"{role.id}@{role.version}")


@session_app.command("create", help="从已有角色或即时新角色创建 Session。")
def create_session(
    role_id: str | None = typer.Option(None, help="已有 Role Preset ID。"),
    new_role_name: str | None = typer.Option(None, help="即时创建的新角色名称。"),
    system_prompt: str | None = typer.Option(None, help="即时角色 System Prompt。"),
    role_model_profile_id: str | None = typer.Option(None, help="即时角色绑定的 Model Profile。"),
    model_profile_id: str | None = typer.Option(None, help="会话级模型覆盖。"),
    effort: Effort | None = typer.Option(None, help="会话级 effort 覆盖。"),
    timeout_seconds: int | None = typer.Option(None, min=1, max=3600),
    writable: bool = typer.Option(False, help="即时角色是否可修改 workspace。"),
    command_runner: CommandRunnerType = typer.Option(
        CommandRunnerType.DOCKER,
        help="即时可写角色的命令 Runner；host 只适用于可信 workspace。",
    ),
    docker_image: str = typer.Option("python:3.13-slim", help="Docker Runner 使用的镜像。"),
    cpu_limit: float = typer.Option(1.0, min=0.1, max=64),
    memory_limit_mb: int = typer.Option(512, min=64, max=262_144),
    pids_limit: int = typer.Option(256, min=16, max=65_536),
) -> None:
    new_role: RolePreset | None = None
    if new_role_name is not None:
        if system_prompt is None or role_model_profile_id is None:
            raise typer.BadParameter(
                "--new-role-name 需要同时提供 --system-prompt 和 --role-model-profile-id"
            )
        new_role = RolePreset(
            name=new_role_name,
            system_prompt=system_prompt,
            model_profile_id=role_model_profile_id,
            tool_policy=_tool_policy(
                writable,
                command_runner=command_runner,
                docker_image=docker_image,
                cpu_limit=cpu_limit,
                memory_limit_mb=memory_limit_mb,
                pids_limit=pids_limit,
            ),
        )
    overrides = None if timeout_seconds is None else {"timeout_seconds": timeout_seconds}
    session = _service().create_session(
        role_id,
        new_role=new_role,
        model_profile_id=model_profile_id,
        effort=None if effort is None else effort.value,
        budget_overrides=overrides,
    )
    console.print(session.id)


@session_app.command("show", help="查看 Session 及其不可变 Role Snapshot。")
def show_session(session_id: str = typer.Argument(help="Session ID。")) -> None:
    console.print_json(_service().get_session(session_id).model_dump_json(indent=2))


@session_app.command("events", help="查看 Session 的持久化事件。")
def session_events(session_id: str = typer.Argument(help="Session ID。")) -> None:
    for event in _service().list_events(session_id):
        console.print(_json_event(event))


@session_app.command("trace", help="查看 Session Trace 摘要或导出脱敏 JSONL。")
def session_trace(
    session_id: str = typer.Argument(help="Session ID。"),
    jsonl: bool = typer.Option(False, "--jsonl", help="逐行输出脱敏 JSONL Trace。"),
) -> None:
    service = _service()
    session = service.get_session(session_id)
    events = service.list_events(session_id)
    if jsonl:
        for line in session_trace_jsonl(session, events):
            console.print(line, markup=False, highlight=False)
        return
    console.print_json(summarize_session_trace(session, events).model_dump_json(indent=2))


@session_app.command("run", help="使用真实模型运行 Session；高风险工具会交互确认。")
def run_session(
    session_id: str = typer.Option(..., help="Session ID。"),
    message: str = typer.Option(..., help="发送给 Agent 的任务。"),
    workspace: Path = typer.Option(
        Path("."),
        exists=True,
        file_okay=False,
        resolve_path=True,
        help="允许 Agent 操作的 workspace。",
    ),
) -> None:
    service = _service()

    async def approve(tool_call_id: str, category: str, detail: str) -> bool:
        del tool_call_id
        return await asyncio.to_thread(
            typer.confirm,
            f"允许 {category} 操作？\n{detail}",
            default=False,
        )

    async def run() -> None:
        async for event in service.run_session(
            session_id,
            user_message=message,
            workspace=workspace,
            approval_callback=approve,
        ):
            console.print(_json_event(event))

    asyncio.run(run())


@workflow_app.command("list", help="列出已持久化的 Workflow。")
def list_workflows(
    status: WorkflowRunStatus | None = typer.Option(None, help="按 Workflow 状态过滤。"),
    limit: int | None = typer.Option(None, min=1, help="最多返回的 Workflow 数量。"),
) -> None:
    table = Table("ID", "状态", "阶段", "任务长度", "更新时间")
    for workflow_run in _service().list_workflow_runs(status=status, limit=limit):
        table.add_row(
            workflow_run.id,
            workflow_run.status.value,
            workflow_run.current_stage.value,
            str(len(workflow_run.task)),
            workflow_run.updated_at.isoformat(),
        )
    console.print(table)


@workflow_app.command("show", help="查看一个已持久化的 Workflow。")
def show_workflow(workflow_run_id: str = typer.Argument(help="Workflow Run ID。")) -> None:
    console.print_json(_service().get_workflow_run(workflow_run_id).model_dump_json(indent=2))


@workflow_app.command("events", help="查看 Workflow 的持久化事件。")
def workflow_events(workflow_run_id: str = typer.Argument(help="Workflow Run ID。")) -> None:
    for event in _service().list_workflow_events(workflow_run_id):
        console.print(_json_event(event), markup=False, highlight=False)


@workflow_app.command("trace", help="查看 Workflow Trace 摘要或导出脱敏 JSONL。")
def workflow_trace(
    workflow_run_id: str = typer.Argument(help="Workflow Run ID。"),
    jsonl: bool = typer.Option(False, "--jsonl", help="逐行输出脱敏 JSONL Trace。"),
) -> None:
    service = _service()
    if jsonl:
        for line in service.export_workflow_trace_jsonl(workflow_run_id):
            console.print(line, markup=False, highlight=False, soft_wrap=True)
        return
    console.print_json(service.get_workflow_trace(workflow_run_id).model_dump_json(indent=2))


@workflow_app.command("resume", help="从已持久化的中断边界恢复 Workflow。")
def resume_workflow(
    workflow_run_id: str = typer.Argument(help="需要恢复的 Workflow Run ID。"),
    allow_coder_replay: bool = typer.Option(
        False,
        "--allow-coder-replay",
        help="确认已检查 workspace 后，允许重放结果未知的 Coder 阶段。",
    ),
) -> None:
    service = _service()
    workflow = SequentialCodingWorkflow(service)

    async def run() -> None:
        async for event in workflow.resume(
            workflow_run_id,
            allow_coder_replay=allow_coder_replay,
        ):
            console.print(_json_event(event), markup=False, highlight=False)
            if event.event_type == "tool.approval_required":
                approved = await asyncio.to_thread(
                    typer.confirm,
                    (
                        f"[{event.role}] 允许 {event.payload['category']} 操作？\n"
                        f"{event.payload['detail']}"
                    ),
                    default=False,
                )
                service.submit_approval(
                    event.session_id,
                    str(event.payload["tool_call_id"]),
                    approved=approved,
                )

    try:
        asyncio.run(run())
    except (LookupError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc


@workflow_app.command("cancel", help="取消一个正在运行或可恢复的 Workflow。")
def cancel_workflow(workflow_run_id: str = typer.Argument(help="Workflow Run ID。")) -> None:
    cancelled = _service().cancel_workflow_run(workflow_run_id)
    console.print("已取消" if cancelled else "Workflow 已处于终态，未执行取消")


@workflow_app.command(
    "run",
    help="运行含只读并行探索和有限返工的多 Agent 工作流。",
)
def run_workflow(
    task: str = typer.Option(..., help="编码任务。"),
    workspace: Path = typer.Option(
        Path("."),
        exists=True,
        file_okay=False,
        resolve_path=True,
    ),
    main_role_id: str = typer.Option(
        "role_main",
        help="最终汇总使用的只读 Main Role。",
    ),
    planner_role_id: str = typer.Option("role_planner"),
    explorer_role_id: list[str] | None = typer.Option(
        None,
        "--explorer-role-id",
        help="可重复指定，最多 4 个；省略时使用 role_explorer。",
    ),
    coder_role_id: str = typer.Option("role_coder"),
    reviewer_role_id: str = typer.Option("role_reviewer"),
    max_parallel_explorers: int = typer.Option(
        2,
        min=1,
        max=4,
        help="只读 Explorer 的最大并行数。",
    ),
    max_rework_rounds: int = typer.Option(
        1,
        min=0,
        max=3,
        help="Reviewer 返回 VERDICT: REWORK 时，最多允许 Coder 返工的轮数。",
    ),
) -> None:
    service = _service()
    workflow = SequentialCodingWorkflow(service)
    explorer_role_ids = tuple(explorer_role_id or ("role_explorer",))
    try:
        workflow.validate_configuration(
            planner_role_id=planner_role_id,
            explorer_role_ids=explorer_role_ids,
            coder_role_id=coder_role_id,
            reviewer_role_id=reviewer_role_id,
            max_parallel_explorers=max_parallel_explorers,
            main_role_id=main_role_id,
        )
    except (LookupError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc

    async def run() -> None:
        async for event in workflow.run(
            task=task,
            workspace=workspace,
            main_role_id=main_role_id,
            planner_role_id=planner_role_id,
            explorer_role_ids=explorer_role_ids,
            coder_role_id=coder_role_id,
            reviewer_role_id=reviewer_role_id,
            max_parallel_explorers=max_parallel_explorers,
            max_rework_rounds=max_rework_rounds,
        ):
            console.print(_json_event(event))
            if event.event_type == "tool.approval_required":
                approved = await asyncio.to_thread(
                    typer.confirm,
                    (
                        f"[{event.role}] 允许 {event.payload['category']} 操作？\n"
                        f"{event.payload['detail']}"
                    ),
                    default=False,
                )
                service.submit_approval(
                    event.session_id,
                    str(event.payload["tool_call_id"]),
                    approved=approved,
                )

    asyncio.run(run())


@evaluation_suite_app.command("add", help="从 JSON 文件创建一个 immutable Evaluation Suite。")
def add_evaluation_suite(
    file: Path = typer.Option(
        ...,
        "--file",
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        help="EvaluationSuite JSON 文件。",
    ),
) -> None:
    try:
        payload = json.loads(file.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("evaluation suite JSON must be an object")
        suite = EvaluationSuite.model_validate(payload)
        created = _service().create_evaluation_suite(suite)
    except (OSError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise typer.BadParameter("Evaluation Suite JSON 无效或已存在") from exc
    _print_evaluation_json(created.model_dump(mode="json"))


@evaluation_suite_app.command("list", help="列出已持久化的 Evaluation Suite。")
def list_evaluation_suites(
    status: str | None = typer.Option(None, help="按 suite 状态过滤。"),
    limit: int | None = typer.Option(None, min=1, help="最多返回的 Suite 数量。"),
) -> None:
    try:
        suites = _service().list_evaluation_suites(status=status, limit=limit)
    except ValueError as exc:
        raise typer.BadParameter("Evaluation Suite 查询参数无效") from exc
    _print_evaluation_json([suite.model_dump(mode="json") for suite in suites])


@evaluation_suite_app.command("show", help="查看一个 Evaluation Suite。")
def show_evaluation_suite(suite_id: str = typer.Argument(help="Evaluation Suite ID。")) -> None:
    try:
        suite = _service().get_evaluation_suite(suite_id)
    except NotFoundError as exc:
        raise typer.BadParameter("Evaluation Suite 不存在") from exc
    _print_evaluation_json(suite.model_dump(mode="json"))


@evaluation_app.command("run", help="运行一个 Suite，或使用 run list/run show 查询已持久化 Run。")
def run_evaluation_suite(
    suite_id_or_action: str = typer.Argument(
        ...,
        help="要运行的 Evaluation Suite ID，或 list/show。",
    ),
    run_id: str | None = typer.Argument(None, help="run show 时的 Evaluation Run ID。"),
    artifact_root: Path | None = typer.Option(
        None,
        "--artifact-root",
        help="绝对 artifact 根目录；省略时使用数据库父目录下的 evaluations。",
    ),
    status: str | None = typer.Option(None, help="run list 时按状态过滤。"),
    limit: int | None = typer.Option(None, min=1, help="run list 时最多返回的数量。"),
) -> None:
    """Consume the async runner without allowing a shell or raw failure text."""

    if suite_id_or_action == "list":
        if run_id is not None or artifact_root is not None:
            raise typer.BadParameter("run list 不接受 Run ID 或 --artifact-root")
        try:
            runs = _service().list_evaluation_runs(status=status, limit=limit)
        except ValueError as exc:
            raise typer.BadParameter("Evaluation Run 查询参数无效") from exc
        _print_evaluation_json([run.model_dump(mode="json") for run in runs])
        return
    if suite_id_or_action == "show":
        if run_id is None:
            raise typer.BadParameter("run show 需要提供 Evaluation Run ID")
        if artifact_root is not None or status is not None or limit is not None:
            raise typer.BadParameter("run show 只接受 Evaluation Run ID")
        try:
            run = _service().get_evaluation_run(run_id)
        except NotFoundError as exc:
            raise typer.BadParameter("Evaluation Run 不存在") from exc
        _print_evaluation_json(run.model_dump(mode="json"))
        return
    if run_id is not None or status is not None or limit is not None:
        raise typer.BadParameter("运行 Suite 时只接受 Suite ID 和 --artifact-root")
    suite_id = suite_id_or_action
    root = _evaluation_artifact_root(artifact_root)
    service = _service()
    try:
        service.get_evaluation_suite(suite_id)
    except NotFoundError as exc:
        raise typer.BadParameter("Evaluation Suite 不存在") from exc
    evaluation_runner = EvaluationRunner(service)

    async def consume_events() -> None:
        try:
            async for event in evaluation_runner.run_suite(suite_id, artifact_root=root):
                console.print(_json_event(event), markup=False, highlight=False)
        except Exception as exc:
            _print_evaluation_json(
                {
                    "event_type": "evaluation.error",
                    "error_type": _safe_evaluation_error_type(exc),
                    "message": "evaluation runner stopped before completion",
                }
            )
            raise typer.Exit(code=1) from exc

    asyncio.run(consume_events())


@evaluation_result_app.command("list", help="列出一个 Evaluation Run 的安全 Result JSON。")
def list_evaluation_results(run_id: str = typer.Argument(help="Evaluation Run ID。")) -> None:
    try:
        results = _service().list_evaluation_results(run_id)
    except NotFoundError as exc:
        raise typer.BadParameter("Evaluation Run 不存在") from exc
    _print_evaluation_json([_safe_evaluation_result_payload(result) for result in results])


def _memory_snapshot(
    service: ApplicationService,
    session_id: str,
) -> RoleSnapshot:
    """Load the immutable Role Snapshot used for every Memory permission check."""

    return service.get_session(session_id).role_snapshot


@memory_app.command("add", help="通过 Session Role Snapshot 保存一条 Memory。")
def add_memory(
    kind: MemoryKind = typer.Option(..., help="Memory 类型：working、episodic 或 project。"),
    content: str = typer.Option(..., help="要保存的知识内容。"),
    session_id: str = typer.Option(..., help="用于判定 Memory 读写权限的 Session ID。"),
    project_scope: str | None = typer.Option(None, help="project Memory 的项目作用域。"),
    source_task: str | None = typer.Option(None, help="产生这条 Memory 的任务描述。"),
    role_scope: str | None = typer.Option(None, help="可选的角色作用域，逗号分隔。"),
    confidence: float = typer.Option(0.5, min=0.0, max=1.0),
    confirm: bool = typer.Option(False, help="显式确认候选知识并立即激活。"),
    allow_conservative_activation: bool = typer.Option(
        False,
        help="允许通过保守来源和验证规则激活候选知识。",
    ),
) -> None:
    service = _service()
    memory = service.save_memory(
        snapshot=_memory_snapshot(service, session_id),
        session_id=session_id,
        kind=kind,
        content=content,
        project_scope=project_scope,
        source_task=source_task,
        role_scope=role_scope,
        confidence=confidence,
        confirm=confirm,
        allow_conservative_activation=allow_conservative_activation,
    )
    console.print_json(memory.model_dump_json(indent=2))


@memory_app.command("search", help="通过 Session Role Snapshot 检索可读 Memory。")
def search_memory(
    query: str = typer.Argument(help="全文检索关键词。"),
    session_id: str = typer.Option(..., help="用于判定 Memory 读取权限的 Session ID。"),
    project_scope: str | None = typer.Option(None, help="限制 project Memory 的项目作用域。"),
    kind: list[MemoryKind] | None = typer.Option(None, "--kind", help="可重复指定 Memory 类型。"),
    include_candidates: bool = typer.Option(False, help="是否包含尚未确认的候选知识。"),
    limit: int = typer.Option(20, min=1, max=100),
) -> None:
    service = _service()
    memories = service.query_memories(
        query,
        snapshot=_memory_snapshot(service, session_id),
        session_id=session_id,
        project_scope=project_scope,
        kinds=kind,
        include_candidates=include_candidates,
        limit=limit,
    )
    for memory in memories:
        console.print(memory.model_dump_json(), markup=False, highlight=False)


@memory_app.command("confirm", help="确认并激活候选 Memory。")
def confirm_memory(
    memory_id: str = typer.Argument(help="Memory ID。"),
    session_id: str = typer.Option(..., help="用于判定 Memory 写入权限的 Session ID。"),
    project_scope: str | None = typer.Option(None, help="project Memory 的项目作用域。"),
) -> None:
    service = _service()
    memory = service.confirm_memory(
        memory_id,
        snapshot=_memory_snapshot(service, session_id),
        session_id=session_id,
        project_scope=project_scope,
    )
    console.print_json(memory.model_dump_json(indent=2))


@memory_app.command("deactivate", help="停用一条 Memory，并创建新的停用版本。")
def deactivate_memory(
    memory_id: str = typer.Argument(help="Memory ID。"),
    session_id: str = typer.Option(..., help="用于判定 Memory 写入权限的 Session ID。"),
    project_scope: str | None = typer.Option(None, help="project Memory 的项目作用域。"),
) -> None:
    service = _service()
    memory = service.deactivate_memory(
        memory_id,
        snapshot=_memory_snapshot(service, session_id),
        session_id=session_id,
        project_scope=project_scope,
    )
    console.print_json(memory.model_dump_json(indent=2))


if __name__ == "__main__":
    app()
