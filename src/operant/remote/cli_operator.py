"""Local user commands for the versioned browser capability protocol."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import typer
from rich.console import Console

from operant.plugins.capability_registry import CapabilityPluginRegistry
from operant.protocol import redact_public_text
from operant.remote.local_browser import BrowserTargetPolicy
from operant.remote.local_worker import BROWSER_PLUGIN, COMPUTER_PLUGIN, _validate_core_origin
from operant.remote.operator import (
    BrowserCapabilityOperator,
    CapabilityLeaseBinding,
    CapabilityOperationError,
    ComputerCapabilityOperator,
)
from operant.settings import database_path
from sdk.python_client.phase56_generated import Phase56Client
from sdk.python_client.transport import Phase56Error, TransportRequest, TransportResponse

browser_app = typer.Typer(no_args_is_help=True, help="通过正式 Core 协议操作已配对的专用浏览器。")
computer_app = typer.Typer(no_args_is_help=True, help="通过正式 Core 协议操作已配对的 macOS App。")
console = Console()


@contextmanager
def _client(core_origin: str) -> Iterator[Phase56Client]:
    with httpx.Client(timeout=15, trust_env=False) as connection:

        def transport(request: TransportRequest) -> TransportResponse:
            response = connection.request(
                request.method,
                request.url,
                headers=dict(request.headers),
                content=request.body,
            )
            return TransportResponse(
                status=response.status_code,
                headers=dict(response.headers),
                body=response.content,
            )

        yield Phase56Client(_validate_core_origin(core_origin), transport=transport)


@contextmanager
def _operator(
    core_origin: str,
    target_id: str,
    lease_id: str,
    lease_fencing: int,
) -> Iterator[BrowserCapabilityOperator]:
    root = database_path().expanduser().resolve().parent / "capability-plugins"
    record = CapabilityPluginRegistry(root).get(BROWSER_PLUGIN.plugin_id, require_enabled=True)
    binding = CapabilityLeaseBinding(
        target_id=target_id,
        lease_id=lease_id,
        token=os.environ.get("OPERANT_TARGET_LEASE_TOKEN", ""),
        fencing=lease_fencing,
    )
    with _client(core_origin) as client:
        yield BrowserCapabilityOperator(
            client,
            binding,
            BrowserTargetPolicy(frozenset(record.allowed_targets)),
        )


@contextmanager
def _computer_operator(
    core_origin: str,
    target_id: str,
    lease_id: str,
    lease_fencing: int,
) -> Iterator[ComputerCapabilityOperator]:
    root = database_path().expanduser().resolve().parent / "capability-plugins"
    record = CapabilityPluginRegistry(root).get(COMPUTER_PLUGIN.plugin_id, require_enabled=True)
    binding = CapabilityLeaseBinding(
        target_id=target_id,
        lease_id=lease_id,
        token=os.environ.get("OPERANT_TARGET_LEASE_TOKEN", ""),
        fencing=lease_fencing,
    )
    with _client(core_origin) as client:
        yield ComputerCapabilityOperator(client, binding, frozenset(record.allowed_targets))


def _emit(value: object) -> None:
    console.print_json(json.dumps(value, ensure_ascii=False))


def _fail(exc: Exception) -> None:
    if isinstance(exc, CapabilityOperationError):
        console.print(
            f"浏览器任务 {exc.job_id}：{exc.status}（{exc.code}）。结果不明时按原 Job ID 人工核对。"
        )
    elif isinstance(exc, Phase56Error):
        console.print(f"Core 拒绝操作：{redact_public_text(str(exc), max_chars=300)}")
    else:
        console.print(f"浏览器操作失败：{redact_public_text(str(exc), max_chars=300)}")
    raise typer.Exit(code=1) from exc


@browser_app.command("observe", help="观察专用浏览器，返回短期观察哈希。")
def observe(
    target_id: str = typer.Option(..., envvar="OPERANT_BROWSER_TARGET_ID"),
    lease_id: str = typer.Option(..., envvar="OPERANT_BROWSER_LEASE_ID"),
    lease_fencing: int = typer.Option(..., envvar="OPERANT_BROWSER_LEASE_FENCING", min=1),
    core_origin: str = typer.Option("http://127.0.0.1:8000", envvar="OPERANT_CORE_ORIGIN"),
) -> None:
    try:
        with _operator(core_origin, target_id, lease_id, lease_fencing) as operator:
            _emit(operator.observe())
    except (CapabilityOperationError, Phase56Error, ValueError, PermissionError, OSError) as exc:
        _fail(exc)


@browser_app.command("navigate", help="按观察哈希导航到已安装插件允许的精确来源。")
def navigate(
    url: str = typer.Option(...),
    observation_hash: str = typer.Option(...),
    idempotency_key: str = typer.Option(...),
    target_id: str = typer.Option(..., envvar="OPERANT_BROWSER_TARGET_ID"),
    lease_id: str = typer.Option(..., envvar="OPERANT_BROWSER_LEASE_ID"),
    lease_fencing: int = typer.Option(..., envvar="OPERANT_BROWSER_LEASE_FENCING", min=1),
    core_origin: str = typer.Option("http://127.0.0.1:8000", envvar="OPERANT_CORE_ORIGIN"),
) -> None:
    try:
        with _operator(core_origin, target_id, lease_id, lease_fencing) as operator:
            _emit(
                operator.act(
                    "navigate",
                    url=url,
                    observation_hash=observation_hash,
                    idempotency_key=idempotency_key,
                )
            )
    except (CapabilityOperationError, Phase56Error, ValueError, PermissionError, OSError) as exc:
        _fail(exc)


@browser_app.command("fill", help="从标准输入读取文本并填入非密码字段。")
def fill(
    selector: str = typer.Option(...),
    observation_hash: str = typer.Option(...),
    idempotency_key: str = typer.Option(...),
    target_id: str = typer.Option(..., envvar="OPERANT_BROWSER_TARGET_ID"),
    lease_id: str = typer.Option(..., envvar="OPERANT_BROWSER_LEASE_ID"),
    lease_fencing: int = typer.Option(..., envvar="OPERANT_BROWSER_LEASE_FENCING", min=1),
    core_origin: str = typer.Option("http://127.0.0.1:8000", envvar="OPERANT_CORE_ORIGIN"),
) -> None:
    try:
        if sys.stdin.isatty():
            console.print("请输入非密码文本，结束时按 Ctrl+D。")
        value = sys.stdin.read(2_001)
        with _operator(core_origin, target_id, lease_id, lease_fencing) as operator:
            _emit(
                operator.act(
                    "fill",
                    selector=selector,
                    value=value,
                    observation_hash=observation_hash,
                    idempotency_key=idempotency_key,
                )
            )
    except (CapabilityOperationError, Phase56Error, ValueError, PermissionError, OSError) as exc:
        _fail(exc)


@browser_app.command("click", help="只点击最近观察中列出的页面元素。")
def click(
    selector: str = typer.Option(...),
    observation_hash: str = typer.Option(...),
    idempotency_key: str = typer.Option(...),
    target_id: str = typer.Option(..., envvar="OPERANT_BROWSER_TARGET_ID"),
    lease_id: str = typer.Option(..., envvar="OPERANT_BROWSER_LEASE_ID"),
    lease_fencing: int = typer.Option(..., envvar="OPERANT_BROWSER_LEASE_FENCING", min=1),
    core_origin: str = typer.Option("http://127.0.0.1:8000", envvar="OPERANT_CORE_ORIGIN"),
) -> None:
    try:
        with _operator(core_origin, target_id, lease_id, lease_fencing) as operator:
            _emit(
                operator.act(
                    "click",
                    selector=selector,
                    observation_hash=observation_hash,
                    idempotency_key=idempotency_key,
                )
            )
    except (CapabilityOperationError, Phase56Error, ValueError, PermissionError, OSError) as exc:
        _fail(exc)


@browser_app.command("status", help="按原 Job ID 查询持久结果，便于结果不明时核对。")
def status(
    job_id: str = typer.Argument(...),
    core_origin: str = typer.Option("http://127.0.0.1:8000", envvar="OPERANT_CORE_ORIGIN"),
) -> None:
    try:
        with _client(core_origin) as client:
            _emit(client.get_remote_target_job_result(job_id))
    except (Phase56Error, ValueError, OSError) as exc:
        _fail(exc)


@computer_app.command("observe", help="观察白名单内的前台 App 与按钮。")
def observe_computer(
    target_id: str = typer.Option(..., envvar="OPERANT_COMPUTER_TARGET_ID"),
    lease_id: str = typer.Option(..., envvar="OPERANT_COMPUTER_LEASE_ID"),
    lease_fencing: int = typer.Option(..., envvar="OPERANT_COMPUTER_LEASE_FENCING", min=1),
    core_origin: str = typer.Option("http://127.0.0.1:8000", envvar="OPERANT_CORE_ORIGIN"),
) -> None:
    try:
        with _computer_operator(core_origin, target_id, lease_id, lease_fencing) as operator:
            _emit(operator.observe())
    except (CapabilityOperationError, Phase56Error, ValueError, PermissionError, OSError) as exc:
        _fail(exc)


@computer_app.command("click-button", help="按最近观察哈希点击前台 App 的唯一同名按钮。")
def click_computer_button(
    button_name: str = typer.Option(...),
    observation_hash: str = typer.Option(...),
    idempotency_key: str = typer.Option(...),
    target_id: str = typer.Option(..., envvar="OPERANT_COMPUTER_TARGET_ID"),
    lease_id: str = typer.Option(..., envvar="OPERANT_COMPUTER_LEASE_ID"),
    lease_fencing: int = typer.Option(..., envvar="OPERANT_COMPUTER_LEASE_FENCING", min=1),
    core_origin: str = typer.Option("http://127.0.0.1:8000", envvar="OPERANT_CORE_ORIGIN"),
) -> None:
    try:
        with _computer_operator(core_origin, target_id, lease_id, lease_fencing) as operator:
            _emit(
                operator.click_button(
                    button_name=button_name,
                    observation_hash=observation_hash,
                    idempotency_key=idempotency_key,
                )
            )
    except (CapabilityOperationError, Phase56Error, ValueError, PermissionError, OSError) as exc:
        _fail(exc)


@computer_app.command("status", help="按原 Job ID 查询持久结果。")
def computer_status(
    job_id: str = typer.Argument(...),
    core_origin: str = typer.Option("http://127.0.0.1:8000", envvar="OPERANT_CORE_ORIGIN"),
) -> None:
    status(job_id, core_origin)
