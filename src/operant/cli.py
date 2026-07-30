from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import typer
from pydantic import BaseModel
from rich.console import Console
from rich.table import Table

from operant.application.service import ApplicationService
from operant.application.workflow import SequentialCodingWorkflow
from operant.domain.models import (
    Budget,
    Effort,
    ModelProfile,
    RolePreset,
    ToolPolicy,
)
from operant.persistence.sqlite import SQLiteStore
from operant.providers.openai_compatible import OpenAICompatibleProvider
from operant.settings import database_path, load_local_env

app = typer.Typer(no_args_is_help=True, help="由角色预设驱动的多模型 Coding Agent Runtime。")
model_app = typer.Typer(no_args_is_help=True, help="管理 Model Profile。")
role_app = typer.Typer(no_args_is_help=True, help="管理 Role Preset。")
session_app = typer.Typer(no_args_is_help=True, help="创建、查看和运行 Session。")
workflow_app = typer.Typer(no_args_is_help=True, help="运行 Planner → Coder → Reviewer 工作流。")
app.add_typer(model_app, name="model")
app.add_typer(role_app, name="role")
app.add_typer(session_app, name="session")
app.add_typer(workflow_app, name="workflow")
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


def _tool_policy(writable: bool) -> ToolPolicy:
    tools = (
        ("read_file", "search_files", "apply_patch", "run_command", "git_diff")
        if writable
        else ("read_file", "search_files", "git_diff")
    )
    return ToolPolicy(
        allowed_tools=tools,
        workspace_write=writable,
        command_execution=writable,
    )


@app.command("init", help="初始化本地 SQLite 数据库。")
def initialize() -> None:
    _service()
    console.print(f"Operant 数据库已初始化：{database_path()}")


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
    timeout_seconds: int = typer.Option(300, min=1, max=3600),
) -> None:
    role = _service().create_role(
        RolePreset(
            name=name,
            model_profile_id=model_profile_id,
            system_prompt=system_prompt,
            effort=effort,
            tool_policy=_tool_policy(writable),
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
            tool_policy=_tool_policy(writable),
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


@workflow_app.command("run", help="运行 Planner → Coder → Reviewer 顺序工作流。")
def run_workflow(
    task: str = typer.Option(..., help="编码任务。"),
    workspace: Path = typer.Option(
        Path("."),
        exists=True,
        file_okay=False,
        resolve_path=True,
    ),
    planner_role_id: str = typer.Option("role_planner"),
    coder_role_id: str = typer.Option("role_coder"),
    reviewer_role_id: str = typer.Option("role_reviewer"),
) -> None:
    service = _service()
    workflow = SequentialCodingWorkflow(service)

    async def run() -> None:
        async for event in workflow.run(
            task=task,
            workspace=workspace,
            planner_role_id=planner_role_id,
            coder_role_id=coder_role_id,
            reviewer_role_id=reviewer_role_id,
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


if __name__ == "__main__":
    app()
