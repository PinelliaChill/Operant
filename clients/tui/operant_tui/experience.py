"""Keyboard-driven B2-6 projection and exact-object lifecycle actions."""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Input, Label, Select, Static

from .controller import ClientController, CommandKeys, error_view


def experience_objects(state: dict[str, Any]) -> list[tuple[str, str]]:
    objects = []
    for item in state["skills"].get("skills", []):
        objects.append(
            (
                f"技能 {item['version']['name']} v{item['version']['version']} "
                f"· {item['head']['state']}",
                "skill:" + item["skill_id"],
            )
        )
    for item in state["sharing"].get("writer_evidence", []):
        objects.append(
            (f"Writer {item['branch_ref']} · {item['state']}", "writer:" + item["evidence_id"])
        )
    for item in state["sharing"].get("grants", []):
        objects.append(
            (f"授权 {item['subject_id']} · {item['state']}", "grant:" + item["grant_id"])
        )
    for item in state["remote"].get("packs", []):
        objects.append(
            (f"远程包 {item['purpose']} · {item['status']}", "pack:" + item["package_id"])
        )
    return objects


def prepare_experience_command(
    state: dict[str, Any],
    selected: str,
    action: str,
    *,
    rollback_version: str = "",
    reason: str = "",
) -> dict[str, Any]:
    """Freeze the displayed exact version. Never fetch a newer object on submit."""
    project = state["project_id"]
    kind, _, identifier = selected.partition(":")
    command: dict[str, Any] = {"action": action, "project_id": project}
    if kind == "skill" and action in {
        "skill_validate",
        "skill_publish",
        "skill_disable",
        "skill_rollback",
    }:
        item = next(
            (s for s in state["skills"].get("skills", []) if s["skill_id"] == identifier), None
        )
        if item is None:
            raise ValueError("技能不在当前投影中，请刷新")
        command.update(
            skill_id=identifier,
            skill_version=item["version"]["version"],
            expected_head_revision=item["head"]["head_revision"],
            permission_epoch=item["head"]["permission_epoch"],
        )
        if action in {"skill_disable", "skill_rollback"}:
            if item["head"].get("published_version") is None:
                raise ValueError("该技能尚未发布，不能停用或回退")
            command["skill_version"] = item["head"]["published_version"]
        if action == "skill_rollback":
            try:
                version = int(rollback_version)
            except ValueError as exc:
                raise ValueError("回退需要填写目标版本号") from exc
            if version not in item.get("rollback_versions", []):
                raise ValueError("目标版本不在当前可回退列表中")
            command["rollback_to_version"] = version
    elif kind == "writer" and action in {"writer_memory_promote", "writer_memory_revoke"}:
        item = next(
            (
                s
                for s in state["sharing"].get("writer_evidence", [])
                if s["evidence_id"] == identifier
            ),
            None,
        )
        if item is None:
            raise ValueError("Writer 证据不在当前投影中")
        command.update(evidence_id=identifier, expected_revision=item["revision"])
    elif kind == "grant" and action == "grant_revoke":
        item = next(
            (s for s in state["sharing"].get("grants", []) if s["grant_id"] == identifier), None
        )
        if item is None:
            raise ValueError("授权不在当前投影中")
        command.update(grant_id=identifier, expected_revision=item["revision"])
    elif kind == "pack" and action == "remote_pack_revoke":
        item = next(
            (s for s in state["remote"].get("packs", []) if s["package_id"] == identifier), None
        )
        if item is None:
            raise ValueError("远程包不在当前投影中")
        command.update(
            package_id=identifier,
            package_digest=item["package_digest"],
            target_id=item["target_id"],
            purpose=item["purpose"],
        )
    else:
        raise ValueError("所选操作不适用于此对象")
    if reason and not action.startswith("remote_"):
        command["reason"] = reason
    return copy.deepcopy(command)


class ExperienceScreen(Screen[None]):
    BINDINGS = [("escape", "app.pop_screen", "返回")]
    CSS = """
    ExperienceScreen { padding: 1 2; }
    #experience-state { height: auto; min-height: 6; border: solid $primary; padding: 1; }
    #experience-notice { height: auto; min-height: 2; }
    Input, Select { margin-bottom: 1; }
    """

    def __init__(self, controller: ClientController) -> None:
        super().__init__()
        self.controller = controller
        self.state: dict[str, Any] | None = None
        self.pending: dict[str, Any] | None = None
        self.keys = CommandKeys()

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Label("经验技能与授权协作 · Esc 返回")
            yield Input(placeholder="项目 ID", id="experience-project")
            yield Button("刷新项目知识状态", id="experience-refresh")
            yield Static("尚未读取。", id="experience-state", markup=False)
            yield Select([], prompt="选择当前投影中的对象", id="experience-object")
            yield Select(
                [
                    ("验证技能", "skill_validate"),
                    ("发布技能", "skill_publish"),
                    ("停用技能", "skill_disable"),
                    ("回退技能", "skill_rollback"),
                    ("晋级 Writer 知识", "writer_memory_promote"),
                    ("撤销 Writer 知识", "writer_memory_revoke"),
                    ("撤销共享授权", "grant_revoke"),
                    ("撤销远程包", "remote_pack_revoke"),
                ],
                prompt="选择操作",
                id="experience-action",
            )
            yield Input(placeholder="仅回退时：目标版本号", id="experience-version")
            yield Input(placeholder="操作理由", id="experience-reason")
            with Horizontal():
                yield Button("核对操作", id="experience-prepare", disabled=True)
                yield Button(
                    "确认所列对象与版本", id="experience-confirm", disabled=True, variant="warning"
                )
            yield Static(
                "撤销只能阻止后续使用，不能收回已发送内容。", id="experience-notice", markup=False
            )

    def notice(self, text: str) -> None:
        self.query_one("#experience-notice", Static).update(text)

    @on(Button.Pressed, "#experience-refresh")
    async def refresh_state(self) -> None:
        self.pending = None
        self.query_one("#experience-confirm", Button).disabled = True
        self.query_one("#experience-prepare", Button).disabled = True
        project = self.query_one("#experience-project", Input).value.strip()
        try:
            state = await asyncio.to_thread(self.controller.experience, project)
        except Exception as exc:
            self.notice(error_view(exc).message + "\n当前投影只读；请修复连接后刷新。")
            return
        if project != self.query_one("#experience-project", Input).value.strip():
            return
        self.state = state
        objects = experience_objects(state)
        self.query_one("#experience-object", Select).set_options(objects)
        lines = [label + " · " + identity for label, identity in objects]
        for preference in state["sharing"].get("personal_preferences", []):
            lines.append(
                f"个人偏好 {preference['ref']['record_id']} v{preference['ref']['version']}："
                f"{preference['content']}；跨项目使用需明确授权"
            )
        for skill in state["skills"].get("skills", []):
            lines.append(
                f"来源 {skill['version']['procedure_ref']}；"
                f"资源 {skill['version']['artifact']['content_hash']}；"
                f"可回退 {skill.get('rollback_versions', [])}"
            )
        for writer in state["sharing"].get("writer_evidence", []):
            lines.append(
                f"Writer {writer['evidence_id']} 验证 {writer['verification_status']}；"
                f"目标 tree {writer.get('target_tree_digest')}"
            )
        for grant in state["sharing"].get("grants", []):
            lines.append(
                f"授权 {grant['grant_id']} 用途 {grant['purpose']}；"
                f"范围 {grant['target_scope']}；期限 {grant['expires_at']}"
            )
        for pack in state["remote"].get("packs", []):
            lines.append(
                f"包 {pack['package_id']} 目标 {pack['target_id']}；"
                f"用途 {pack['purpose']}；期限 {pack['expires_at']}"
            )
        self.query_one("#experience-state", Static).update(
            "\n".join(lines) or "当前没有经验技能、晋级证据或授权。"
        )
        self.query_one("#experience-prepare", Button).disabled = not objects
        unknown = state.get("unresolved_command_ids", [])
        self.notice(
            "Core 投影已刷新。" + (f" 待人工核对命令：{', '.join(unknown)}" if unknown else "")
        )

    @on(Button.Pressed, "#experience-prepare")
    def prepare(self) -> None:
        self.pending = None
        self.query_one("#experience-confirm", Button).disabled = True
        if (
            self.state is None
            or self.state["project_id"]
            != self.query_one("#experience-project", Input).value.strip()
        ):
            self.notice("项目已改变，请先刷新。")
            return
        try:
            self.pending = prepare_experience_command(
                self.state,
                str(self.query_one("#experience-object", Select).value),
                str(self.query_one("#experience-action", Select).value),
                rollback_version=self.query_one("#experience-version", Input).value,
                reason=self.query_one("#experience-reason", Input).value.strip(),
            )
        except ValueError as exc:
            self.notice(str(exc))
            return
        details = "\n".join(f"{key}: {value}" for key, value in self.pending.items())
        self.notice("请核对以下精确操作，确认后由 Core 再次校验：\n" + details)
        self.query_one("#experience-confirm", Button).disabled = False

    @on(Button.Pressed, "#experience-confirm")
    async def confirm(self) -> None:
        command = self.pending
        if (
            command is None
            or command["project_id"] != self.query_one("#experience-project", Input).value.strip()
        ):
            self.notice("项目或核对对象已失效，请重新核对。")
            return
        self.pending = None
        self.query_one("#experience-confirm", Button).disabled = True
        self.query_one("#experience-prepare", Button).disabled = True
        logical = json.dumps(command, sort_keys=True)
        try:
            result = await asyncio.to_thread(
                self.controller.experience_command, command, idempotency_key=self.keys.get(logical)
            )
        except Exception as exc:
            self.notice(error_view(exc).message + "\n未确认成功，不自动重发；请刷新核对。")
            return
        self.keys.release(logical)
        await self.refresh_state()
        self.notice(str(result["message"]))
