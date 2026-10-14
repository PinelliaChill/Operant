"""Local Remote Control and Target diagnostics over the generated clients."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from textual import on
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Input, Label, Select, Static

from .controller import ClientController, error_view

SCOPES = (
    "remote.control.observe",
    "remote.control.command",
    "remote.control.approve",
    "remote.control.browser",
    "remote.control.computer",
    "remote.control.settings",
)

MUTATION_BUTTONS = (
    "remote-pair",
    "remote-session-create",
    "remote-session-prepare",
    "remote-session-confirm",
    "remote-revoke-prepare",
    "remote-revoke-confirm",
    "target-register",
    "target-lease",
    "target-release",
    "target-job-submit",
    "target-cancel-prepare",
    "target-cancel-confirm",
)


def parse_scopes(value: str) -> list[str]:
    scopes = [part.strip() for part in value.split(",") if part.strip()]
    if not scopes or len(scopes) != len(set(scopes)) or "remote.control.observe" not in scopes:
        raise ValueError("Scope 不能重复，且必须包含 remote.control.observe")
    if any(scope not in SCOPES for scope in scopes):
        raise ValueError("存在未知 Scope；请使用页面列出的权限名")
    return scopes


class RemoteScreen(Screen[None]):
    CSS = """
    RemoteScreen { layout: vertical; }
    #remote-body { height: 1fr; padding: 0 1; }
    #remote-status, #remote-ticket, #remote-projection, #remote-detail {
        height: auto; min-height: 2;
    }
    Input, Select, Button { margin-bottom: 1; }
    """
    BINDINGS = [("escape", "close", "返回"), ("ctrl+r", "refresh", "刷新")]

    def __init__(self, controller: ClientController) -> None:
        super().__init__()
        self.controller = controller
        self.projection: dict[str, Any] = {}
        self.pending_revoke = ""
        self.pending_close = ""
        self.busy = False
        self.lease: dict[str, Any] | None = None
        self.lease_target_id = ""
        self.pending_cancel_job = ""

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="remote-body"):
            yield Label("Remote Control · 本机 Host 与设备授权")
            yield Static("正在读取 Core 投影…", id="remote-status", markup=False)
            yield Button("刷新 Host / 会话 / Gateway / Target", id="remote-refresh")
            yield Static("", id="remote-projection", markup=False)
            yield Label("配对权限（逗号分隔；创建票据即本机确认）")
            yield Static("、".join(SCOPES), markup=False)
            yield Input(value="remote.control.observe", id="remote-scopes")
            yield Button("创建一次性配对票据", id="remote-pair", variant="primary")
            yield Static("", id="remote-ticket", markup=False)
            yield Label("设备与会话")
            yield Input(placeholder="Device ID", id="remote-device")
            yield Select(
                [("直连", "direct"), ("自托管 Relay", "relay")], value="direct", id="remote-mode"
            )
            yield Button("创建会话", id="remote-session-create")
            yield Input(placeholder="Remote Session ID", id="remote-session")
            yield Button("准备关闭会话", id="remote-session-prepare")
            yield Button(
                "确认关闭会话", id="remote-session-confirm", variant="error", disabled=True
            )
            yield Button("准备撤销设备", id="remote-revoke-prepare")
            yield Button("确认撤销设备", id="remote-revoke-confirm", variant="error", disabled=True)
            yield Label("按 ID 回读 Host Ack 与 Target Job")
            yield Input(placeholder="Command ID", id="remote-command")
            yield Button("读取 Host 命令回执", id="remote-command-read")
            yield Input(placeholder="Job ID", id="remote-job")
            yield Button("读取 Target 结果", id="remote-job-read")
            yield Static("", id="remote-detail", markup=False)
            yield Label("Remote Execution Target · 登记、租约、作业")
            yield Input(placeholder="Target ID", id="target-id")
            yield Input(placeholder="显示名称", id="target-name")
            yield Input(placeholder="HTTPS Endpoint", id="target-endpoint")
            yield Input(placeholder="Target 签名公钥", id="target-public-key")
            yield Input(placeholder="Bearer 环境变量名（不要填 Token）", id="target-credential-ref")
            yield Input(placeholder="Policy Ref", id="target-policy-ref")
            yield Input(placeholder="Artifact Namespace", id="target-artifact-namespace")
            yield Button("登记 Target", id="target-register")
            yield Input(placeholder="目标 Workspace Ref", id="target-workspace")
            yield Button("申请 5 分钟租约", id="target-lease")
            yield Static("", id="target-lease-detail", markup=False)
            yield Button("清除屏幕 Token", id="target-hide-token")
            yield Button("释放租约", id="target-release")
            yield Select(
                [("只读文本", "remote.target.read"), ("运行允许命令", "remote.target.exec")],
                value="remote.target.read",
                id="target-capability",
            )
            yield Input(value="read_text", id="target-operation")
            yield Input(value='{"path":"README.md"}', id="target-arguments")
            yield Select(
                [("可安全去重", "idempotent"), ("可能有一次性副作用", "non_idempotent")],
                value="idempotent",
                id="target-idempotency",
            )
            yield Button("提交 Target 作业", id="target-job-submit", variant="primary")
            yield Button("准备取消 Job ID", id="target-cancel-prepare")
            yield Button(
                "确认取消 Job ID", id="target-cancel-confirm", variant="error", disabled=True
            )
        yield Footer()

    async def on_mount(self) -> None:
        await self.refresh_projection()

    def action_close(self) -> None:
        self.query_one("#remote-ticket", Static).update("")
        self.query_one("#target-lease-detail", Static).update("")
        self.lease = None
        self.lease_target_id = ""
        self.dismiss(None)

    async def action_refresh(self) -> None:
        await self.refresh_projection()

    def _show_error(self, exc: Exception) -> None:
        view = error_view(exc)
        if view.code != "invalid_input":
            for name in MUTATION_BUTTONS:
                self.query_one(f"#{name}", Button).disabled = True
        self.query_one("#remote-status", Static).update(
            f"{view.code}: {view.message}；{view.projection_trust}"
        )

    async def refresh_projection(self) -> None:
        if self.busy:
            return
        self.busy = True
        try:
            projection = await asyncio.to_thread(self.controller.remote_projection)
            self.projection = projection
            lines = [
                f"Host {len(projection['hosts'])} · Device {len(projection['devices'])}"
                f" · Session {len(projection['sessions'])}"
                f" · Target {len(projection['targets'])} · Job {len(projection['jobs'])}"
            ]
            for host in projection["hosts"]:
                lines.append(
                    f"Host {host['host_id']} · {host['online_state']} · {host['protocol_version']}"
                )
            for device in projection["devices"]:
                lines.append(
                    f"Device {device['device_id']} · {device['display_name']}"
                    f" · {','.join(device['scopes'])}"
                    f" · revoked={bool(device.get('revoked_at'))}"
                )
            for session in projection["sessions"]:
                connection = next(
                    (
                        item
                        for item in projection["connections"]
                        if item["remote_session_id"] == session["remote_session_id"]
                        and item["status"] != "closed"
                    ),
                    None,
                )
                cursor = connection["event_cursor"] if connection else session["event_cursor"]
                lines.append(
                    f"Session {session['remote_session_id']} · {session['connection_state']}"
                    f" · Gateway {connection['status'] if connection else '未连接'}"
                    f" · Cursor {cursor}"
                    f" · expires {session['expires_at']}"
                )
                if connection and connection.get("error_code"):
                    lines.append(f"  Gateway 错误：{connection['error_code']}")
            for event in projection["events"][-10:]:
                lines.append(
                    f"Command {event['command_id']} · {event['status']}"
                    f" · Host {'已确认' if event.get('host_acknowledged_at') else '未确认'}"
                    f" · Cursor {event['cursor']}"
                )
            for target in projection["targets"]:
                lines.append(
                    f"Target {target['target_id']} · {target['status']}"
                    f" · heartbeat {target.get('last_seen_at') or '尚无'}"
                )
            for job in projection["jobs"][-10:]:
                lines.append(f"Job {job['job_id']} · {job['capability']} · {job['status']}")
            self.query_one("#remote-projection", Static).update("\n".join(lines))
            for name in MUTATION_BUTTONS:
                if name not in {
                    "remote-session-confirm",
                    "remote-revoke-confirm",
                    "target-cancel-confirm",
                }:
                    self.query_one(f"#{name}", Button).disabled = False
            self.query_one("#remote-status", Static).update(
                "已从 Core 刷新。断线后用原 Session/Cursor 重连；"
                "过期后再建会话。结果不明先人工核对，不能自动重发。"
            )
        except Exception as exc:
            self._show_error(exc)
        finally:
            self.busy = False

    @on(Button.Pressed)
    async def on_button(self, event: Button.Pressed) -> None:
        name = event.button.id or ""
        if name == "remote-refresh":
            await self.refresh_projection()
            return
        if self.busy:
            return
        self.busy = True
        try:
            if name == "remote-pair":
                host = next(
                    (item for item in self.projection.get("hosts", []) if item.get("enabled")), None
                )
                if not host:
                    raise ValueError("先在本机 GUI 启用 Host")
                scopes = parse_scopes(self.query_one("#remote-scopes", Input).value)
                ticket = await asyncio.to_thread(
                    self.controller.create_remote_pairing, host["host_id"], scopes
                )
                self.query_one("#remote-ticket", Static).update(
                    "仅本屏显示的完整票据 JSON；远端 remote-device pair --ticket-file 可读取：\n"
                    + json.dumps(ticket, ensure_ascii=False, indent=2)
                )
            elif name == "remote-session-create":
                device_id = self.query_one("#remote-device", Input).value.strip()
                device = next(
                    (
                        item
                        for item in self.projection.get("devices", [])
                        if item["device_id"] == device_id and not item.get("revoked_at")
                    ),
                    None,
                )
                if not device:
                    raise ValueError("请选择已配对且未撤销的 Device ID")
                mode = str(self.query_one("#remote-mode", Select).value)
                session = await asyncio.to_thread(
                    self.controller.create_remote_session, device["host_id"], device_id, mode
                )
                self.query_one("#remote-detail", Static).update(
                    f"会话已创建：{session['remote_session_id']}；请在远端以此 ID 连接 Gateway。"
                )
            elif name == "remote-session-prepare":
                self.pending_close = self.query_one("#remote-session", Input).value.strip()
                if not any(
                    item["remote_session_id"] == self.pending_close
                    for item in self.projection.get("sessions", [])
                ):
                    raise ValueError("会话 ID 不在当前 Core 投影中")
                self.query_one("#remote-session-confirm", Button).disabled = False
                self.query_one("#remote-status", Static).update(
                    f"待确认关闭会话：{self.pending_close}"
                )
            elif name == "remote-session-confirm":
                if self.pending_close != self.query_one("#remote-session", Input).value.strip():
                    raise ValueError("会话 ID 已变化，请重新准备")
                await asyncio.to_thread(self.controller.close_remote_session, self.pending_close)
                self.pending_close = ""
                self.query_one("#remote-session-confirm", Button).disabled = True
            elif name == "remote-revoke-prepare":
                self.pending_revoke = self.query_one("#remote-device", Input).value.strip()
                if not any(
                    item["device_id"] == self.pending_revoke and not item.get("revoked_at")
                    for item in self.projection.get("devices", [])
                ):
                    raise ValueError("设备 ID 不在当前有效投影中")
                self.query_one("#remote-revoke-confirm", Button).disabled = False
                self.query_one("#remote-status", Static).update(
                    f"待确认撤销设备：{self.pending_revoke}；关联会话会关闭"
                )
            elif name == "remote-revoke-confirm":
                if self.pending_revoke != self.query_one("#remote-device", Input).value.strip():
                    raise ValueError("设备 ID 已变化，请重新准备")
                await asyncio.to_thread(self.controller.revoke_remote_device, self.pending_revoke)
                self.pending_revoke = ""
                self.query_one("#remote-revoke-confirm", Button).disabled = True
            elif name == "remote-command-read":
                receipt = await asyncio.to_thread(
                    self.controller.remote_command, self.query_one("#remote-command", Input).value
                )
                self.query_one("#remote-detail", Static).update(
                    f"Host Command {receipt['command_id']} · {receipt['status']}"
                    f" · Host Ack {receipt.get('host_acknowledged_at') or '未确认'}"
                    f" · Result {receipt.get('result_ref') or '无'}"
                    f" · Error {receipt.get('error_code') or '无'}"
                )
            elif name == "remote-job-read":
                result = await asyncio.to_thread(
                    self.controller.remote_job_result, self.query_one("#remote-job", Input).value
                )
                self.query_one("#remote-detail", Static).update(
                    f"Target Job {result.get('job_id')} · {result.get('status')}"
                    f" · Result {result.get('result') or '尚无'}"
                    f" · Error {result.get('error_code') or '无'}"
                )
            elif name == "target-register":

                def target_input(field: str) -> str:
                    return self.query_one(f"#target-{field}", Input).value.strip()

                target_id = target_input("id")
                if not target_id:
                    raise ValueError("Target ID 不能为空")
                request = {
                    "target_id": target_id,
                    "display_name": target_input("name"),
                    "endpoint_ref": target_input("endpoint"),
                    "identity_public_key": target_input("public-key"),
                    "credential_ref": target_input("credential-ref"),
                    "policy_ref": target_input("policy-ref"),
                    "artifact_namespace": target_input("artifact-namespace"),
                    "capability_manifest": {
                        "version": "1",
                        "platform": "linux",
                        "capabilities": ["remote.target.read", "remote.target.exec"],
                        "supported_operations": ["read_text", "run_allowlisted"],
                    },
                }
                await asyncio.to_thread(self.controller.register_remote_target, request)
                self.query_one("#remote-detail", Static).update(
                    f"Target {target_id} 已登记；请核对远端身份与心跳。"
                )
            elif name == "target-lease":
                if self.lease is not None:
                    raise ValueError("当前页面已有租约，先释放或等待过期")
                target_id = self.query_one("#target-id", Input).value.strip()
                workspace = self.query_one("#target-workspace", Input).value.strip()
                lease = await asyncio.to_thread(
                    self.controller.acquire_remote_target_lease, target_id, workspace
                )
                self.lease = lease
                self.lease_target_id = target_id
                self.query_one("#target-lease-detail", Static).update(
                    f"租约 {lease['lease_id']} · fencing {lease['fencing']}"
                    f" · expires {lease['expires_at']}\n"
                    f"OPERANT_REMOTE_TARGET_LEASE_TOKEN={lease['token']}\n"
                    "仅本屏内存持有；远端 serve 与本机 dispatch 使用同一 Token。"
                    "离开后丢失时，等待最多 5 分钟过期再申请。"
                )
            elif name == "target-hide-token":
                self.query_one("#target-lease-detail", Static).update("Token 已从屏幕隐藏。")
            elif name == "target-release":
                if not self.lease:
                    raise ValueError("当前页面没有可释放的租约")
                await asyncio.to_thread(
                    self.controller.release_remote_target_lease, self.lease_target_id, self.lease
                )
                self.lease = None
                self.lease_target_id = ""
                self.query_one("#target-lease-detail", Static).update("租约已释放，Token 已清除。")
            elif name == "target-job-submit":
                if not self.lease:
                    raise ValueError("先申请短时租约")
                if datetime.fromisoformat(self.lease["expires_at"]) <= datetime.now(timezone.utc):
                    raise ValueError("租约已过期，不能提交作业")
                target_id = self.query_one("#target-id", Input).value.strip()
                if target_id != self.lease_target_id:
                    raise ValueError("Target ID 已变化，请恢复原租约 Target ID")
                capability = str(self.query_one("#target-capability", Select).value)
                operation = self.query_one("#target-operation", Input).value.strip()
                arguments = json.loads(self.query_one("#target-arguments", Input).value)
                if not isinstance(arguments, dict):
                    raise ValueError("作业参数必须是 JSON 对象")
                idempotency = str(self.query_one("#target-idempotency", Select).value)
                job = await asyncio.to_thread(
                    self.controller.create_remote_target_job,
                    target_id,
                    self.lease,
                    capability,
                    operation,
                    arguments,
                    idempotency,
                )
                self.query_one("#remote-detail", Static).update(
                    f"Target Job {job['job_id']} 已排队。远端正式 dispatch 后按 ID 回读结果。"
                )
            elif name == "target-cancel-prepare":
                job_id = self.query_one("#remote-job", Input).value.strip()
                if not any(item["job_id"] == job_id for item in self.projection.get("jobs", [])):
                    raise ValueError("Job ID 不在当前 Core 投影中")
                self.pending_cancel_job = job_id
                self.query_one("#target-cancel-confirm", Button).disabled = False
                self.query_one("#remote-status", Static).update(
                    f"待确认取消 {job_id}；远端可能已开始执行，取消后需回读终态"
                )
            elif name == "target-cancel-confirm":
                job_id = self.query_one("#remote-job", Input).value.strip()
                if job_id != self.pending_cancel_job:
                    raise ValueError("Job ID 已变化，请重新准备")
                await asyncio.to_thread(self.controller.cancel_remote_target_job, job_id)
                self.pending_cancel_job = ""
                self.query_one("#target-cancel-confirm", Button).disabled = True
                self.query_one("#remote-detail", Static).update(
                    f"取消请求已提交：{job_id}；请回读结果。"
                )
            else:
                return
        except Exception as exc:
            self._show_error(exc)
        finally:
            self.busy = False
        if name in {
            "remote-session-create",
            "remote-session-confirm",
            "remote-revoke-confirm",
            "target-register",
            "target-job-submit",
            "target-cancel-confirm",
        }:
            await self.refresh_projection()
