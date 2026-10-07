"""Local Skill source settings. Discovery and installation stay separate actions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Request, Response

from operant.api_model_connections import _trusted_local
from operant.application.service import ApplicationService
from operant.contracts.onboarding import SkillSourceInput, SkillSourceList, SkillSourceView
from operant.domain.actions import CommandExecution, CommandExecutionStatus
from operant.domain.security import Capability
from operant.persistence.phase45 import SQLitePhase45Repository
from operant.persistence.sqlite import ConflictError, NotFoundError
from operant.protocol import canonical_action_hash
from operant.skills.discovery import SkillDiscovery, SkillDiscoveryLimits
from operant.skills.sources import default_skill_roots

_SETTING_KEY = "skill_sources.v1"
_DEFAULT_LABELS = {
    "operant-default": "Operant 内置技能",
    "home-agents": "本机 Agents 技能",
    "home-codex": "本机 Codex 技能",
    "home-claude": "本机 Claude 技能",
    "workspace-agents": "工作区 Agents 技能",
    "workspace-codex": "工作区 Codex 技能",
    "workspace-claude": "工作区 Claude 技能",
    "workspace-operant": "工作区 Operant 技能",
}


def _source_path(raw: str | Path) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute() or len(str(path)) > 4_096:
        raise ValueError("Skill source path must be a bounded absolute directory")
    if any(character in str(path) for character in ("\x00", "\n", "\r")):
        raise ValueError("Skill source path contains a control character")
    return path.resolve(strict=False)


def _saved_sources(repo: Any) -> dict[str, str]:
    saved = repo.get_setting(_SETTING_KEY, {})
    if not isinstance(saved, dict):
        return {}
    return {
        key: value
        for key, value in saved.items()
        if isinstance(key, str)
        and key.startswith("user-")
        and len(key) == 17
        and isinstance(value, str)
    }


def install_skill_source_routes(
    app: FastAPI,
    service: ApplicationService,
    repo: Any,
    *,
    local_authorizer: Callable[[Request], bool] | None = None,
) -> None:
    """Register local source management; saved paths never imply installation."""
    base_roots = dict(getattr(app.state, "b23_skill_roots", {}) or {})
    use_defaults = bool(getattr(app.state, "skill_source_defaults_enabled", True))
    workspace = Path(getattr(app.state, "skill_source_workspace", Path.cwd()))
    if use_defaults:
        default_workspace_id = repo.get_setting("default_workspace_id")
        if isinstance(default_workspace_id, str):
            try:
                registered = service.store.get_workspace_initialization_by_id(default_workspace_id)
                restored = Path(registered.workspace_ref).resolve(strict=True)
                if (
                    registered.readable
                    and restored.is_dir()
                    and str(restored) == registered.workspace_ref
                ):
                    workspace = restored
            except (NotFoundError, OSError, ValueError):
                pass
    max_roots = SkillDiscoveryLimits().max_roots
    previous_active: dict[str, Path] = {}
    scan_issues: dict[str, str] = {}
    phase_repository = SQLitePhase45Repository(service.store)

    def refresh_skill_sources(
        workspace_ref: str | Path | None = None, *, discover: bool = False
    ) -> list[SkillSourceView]:
        nonlocal workspace
        if workspace_ref is not None:
            chosen = _source_path(workspace_ref)
            discover = discover or chosen != workspace
            workspace = chosen
        definitions: dict[str, Path] = {key: Path(value) for key, value in base_roots.items()}
        if use_defaults:
            definitions.update(default_skill_roots(workspace))
        definitions.update({key: Path(value) for key, value in _saved_sources(repo).items()})
        active: dict[str, Path] = {}
        seen: dict[Path, str] = {}
        views: list[SkillSourceView] = []
        for root_ref, raw_path in definitions.items():
            try:
                path = _source_path(raw_path)
                exists = path.is_dir()
                duplicate = seen.get(path) if exists else None
                if exists and duplicate is None and len(active) < max_roots:
                    seen[path] = root_ref
                    active[root_ref] = path
                issue = f"与 {duplicate} 指向同一目录" if duplicate else None
                if not exists:
                    issue = "目录不存在或不可读取"
                elif duplicate is None and root_ref not in active:
                    issue = "技能来源数量已达上限"
            except (OSError, ValueError) as exc:
                path = Path(raw_path)
                exists = False
                issue = str(exc)[:200]
            views.append(
                SkillSourceView(
                    root_ref=root_ref,
                    path=str(path),
                    label=_DEFAULT_LABELS.get(root_ref, "已添加的技能目录"),
                    exists=exists,
                    enabled=root_ref in active,
                    issue=issue,
                )
            )
        current = getattr(app.state, "b23_skill_roots", None)
        if not isinstance(current, dict):
            current = {}
            app.state.b23_skill_roots = current
        current.clear()
        current.update(active)
        manager = service.memory_manager
        if manager is not None:
            manager.skill_roots = dict(active)
        discover = discover or active != previous_active
        if discover:
            changed = {
                root_ref
                for root_ref, path in active.items()
                if previous_active.get(root_ref) != path
            }
            removed = set(previous_active) - set(active)
            for root_ref in removed:
                phase_repository.replace_skill_candidates(root_ref, ())
                scan_issues.pop(root_ref, None)
            if changed:
                for root_ref in changed:
                    scan_issues.pop(root_ref, None)
                ordered_refs = tuple(active)
                try:
                    gateway = app.state.phase45_action_gateway
                    arguments = {
                        "root_refs": list(ordered_refs),
                        "path_hashes": [
                            hashlib.sha256(str(active[ref]).encode()).hexdigest()
                            for ref in ordered_refs
                        ],
                    }
                    scan_key = f"skill-auto-discovery:{uuid4().hex}"
                    proposal = gateway.normalizer.normalize(
                        principal=gateway.principal,
                        tool="skill_discovery",
                        operation="read",
                        arguments=arguments,
                        requested_capabilities=(Capability.WORKSPACE_READ,),
                        idempotency_key=scan_key,
                        policy_version=gateway.engine.bundle.version,
                        workspace_id="skill-roots",
                    )
                    if gateway.engine.evaluate(proposal).decision.value != "allow":
                        raise PermissionError("技能目录扫描未获授权")
                    action, decision, _ = gateway.guard(
                        tool="skill_discovery",
                        operation="read",
                        target_id="skill-roots",
                        arguments=arguments,
                        capabilities=(Capability.WORKSPACE_READ,),
                        idempotency_key=scan_key,
                    )
                    if decision.decision.value != "allow" or decision.lease is None:
                        raise PermissionError("技能目录扫描未获授权")
                    gateway.consume(decision.lease, action)
                    result = SkillDiscovery(tuple(active.values())).discover()
                except (OSError, ValueError, RuntimeError, AttributeError, PermissionError) as exc:
                    for view in views:
                        if view.root_ref in changed:
                            scan_issues[view.root_ref] = f"扫描失败：{str(exc)[:150]}"
                else:
                    for index, root_ref in enumerate(ordered_refs):
                        if root_ref in changed:
                            phase_repository.replace_skill_candidates(
                                root_ref,
                                tuple(
                                    item for item in result.candidates if item.root_index == index
                                ),
                            )
                    for scan_issue in result.issues:
                        ref = ordered_refs[scan_issue.root_index]
                        scan_issues.setdefault(
                            ref,
                            f"{scan_issue.relative_directory}: {scan_issue.message}"[:200],
                        )
            previous_active.clear()
            previous_active.update(active)
        for view in views:
            if view.issue is None:
                view.issue = scan_issues.get(view.root_ref)
        return views

    app.state.refresh_skill_sources = refresh_skill_sources
    refresh_skill_sources(discover=True)

    def require_local(request: Request) -> None:
        if not _trusted_local(request, local_authorizer):
            raise HTTPException(status_code=403, detail="Skill sources require a local client")

    def command_key(supplied: str | None, response: Response) -> str:
        key = supplied or uuid4().hex
        response.headers["Idempotency-Key"] = key
        return key

    def guard(operation: str, target_id: str, fingerprint: str, key: str) -> None:
        try:
            action, decision, _ = app.state.phase45_action_gateway.guard(
                tool="skill_source",
                operation=operation,
                target_id=target_id,
                arguments={"request_hash": fingerprint},
                capabilities=(Capability.WORKSPACE_WRITE,),
                idempotency_key=key,
            )
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail="request identity changed") from exc
        if decision.decision.value != "allow" or decision.lease is None:
            if decision.decision.value == "ask":
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "approval_required",
                        "approval_id": decision.approval_id,
                        "reason_code": decision.reason_code,
                    },
                )
            raise HTTPException(status_code=403, detail=decision.reason_code)
        app.state.phase45_action_gateway.consume(decision.lease, action)

    def prior_result(key: str, fingerprint: str) -> dict[str, Any] | None:
        with service.store._connect() as connection:
            row = connection.execute(
                "SELECT id FROM command_executions WHERE command_type=? AND idempotency_key=?",
                ("skill_source.change", key),
            ).fetchone()
        if row is None:
            return None
        command = service.store.get_command_execution(str(row["id"]))
        if command.action_hash != fingerprint:
            raise HTTPException(status_code=409, detail="request identity changed")
        if command.status is CommandExecutionStatus.COMPLETED and command.response_json:
            return dict(json.loads(command.response_json))
        raise HTTPException(
            status_code=409,
            detail={"code": "command_outcome_unknown", "message": "请刷新来源后核对结果"},
        )

    def reserve(key: str, fingerprint: str) -> tuple[CommandExecution, bool]:
        try:
            command, created = service.store.reserve_command_execution(
                CommandExecution(
                    command_type="skill_source.change",
                    idempotency_key=key,
                    action_hash=fingerprint,
                )
            )
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail="request identity changed") from exc
        return command, created

    def finish(command: CommandExecution, result: SkillSourceView | SkillSourceList) -> None:
        service.store.complete_command_execution(
            command.id,
            response_json=result.model_dump_json(),
            http_status=200,
            resource_type="skill_source",
            resource_id=(result.root_ref if isinstance(result, SkillSourceView) else None),
        )

    @app.get(
        "/v1/setup/skill-sources", operation_id="listSkillSources", response_model=SkillSourceList
    )
    def list_skill_sources(request: Request) -> SkillSourceList:
        require_local(request)
        return SkillSourceList(items=refresh_skill_sources())

    @app.post(
        "/v1/setup/skill-sources", operation_id="addSkillSource", response_model=SkillSourceView
    )
    def add_skill_source(
        body: SkillSourceInput,
        request: Request,
        response: Response,
        idempotency_key: str | None = Header(default=None, min_length=1, max_length=300),
    ) -> SkillSourceView:
        require_local(request)
        try:
            path = _source_path(body.path)
            if not path.is_dir() or path in {Path(path.anchor), Path.home().resolve()}:
                raise ValueError("choose an existing Skill directory")
            key = command_key(idempotency_key, response)
            fingerprint = canonical_action_hash(
                {"operation": "skill_source_add", "path": str(path)}
            )
            guard("add", hashlib.sha256(str(path).encode()).hexdigest()[:32], fingerprint, key)
            prior = prior_result(key, fingerprint)
            if prior is not None:
                return SkillSourceView.model_validate(prior)
            saved = _saved_sources(repo)
            current = refresh_skill_sources()
            duplicate = next(
                (item for item in current if item.enabled and Path(item.path) == path), None
            )
            root_ref = "user-" + hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:12]
            if duplicate is None and root_ref in saved and saved[root_ref] != str(path):
                raise ValueError("Skill source reference collision")
            if duplicate is None and len(saved) >= 8:
                raise ValueError("too many additional Skill sources")
            command, created = reserve(key, fingerprint)
            if not created:
                replay = prior_result(key, fingerprint)
                assert replay is not None
                return SkillSourceView.model_validate(replay)
            if duplicate is None:
                saved[root_ref] = str(path)
                repo.set_setting(_SETTING_KEY, saved)
                result = next(
                    item
                    for item in refresh_skill_sources(discover=True)
                    if item.root_ref == root_ref
                )
            else:
                result = duplicate
            finish(command, result)
            return result
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.delete(
        "/v1/setup/skill-sources/{root_ref}",
        operation_id="removeSkillSource",
        response_model=SkillSourceList,
    )
    def remove_skill_source(
        root_ref: str,
        request: Request,
        response: Response,
        idempotency_key: str | None = Header(default=None, min_length=1, max_length=300),
    ) -> SkillSourceList:
        require_local(request)
        key = command_key(idempotency_key, response)
        fingerprint = canonical_action_hash(
            {"operation": "skill_source_remove", "root_ref": root_ref}
        )
        guard("remove", root_ref, fingerprint, key)
        prior = prior_result(key, fingerprint)
        if prior is not None:
            return SkillSourceList.model_validate(prior)
        saved = _saved_sources(repo)
        if root_ref not in saved:
            raise HTTPException(status_code=404, detail="added Skill source not found")
        command, created = reserve(key, fingerprint)
        if not created:
            replay = prior_result(key, fingerprint)
            assert replay is not None
            return SkillSourceList.model_validate(replay)
        del saved[root_ref]
        repo.set_setting(_SETTING_KEY, saved)
        result = SkillSourceList(items=refresh_skill_sources(discover=True))
        finish(command, result)
        return result
