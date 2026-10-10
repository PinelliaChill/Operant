"""Local Skill source settings. Discovery and installation stay separate actions."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Request, Response

from operant.api_model_connections import _trusted_local
from operant.application.service import ApplicationService
from operant.application.skill_sources import (
    SkillSourceEffects,
    SkillSourceOperationError,
)
from operant.application.skill_sources import (
    saved_sources as _saved_sources,
)
from operant.contracts.onboarding import SkillSourceInput, SkillSourceList, SkillSourceView
from operant.domain.security import Capability
from operant.persistence.phase45 import SQLitePhase45Repository
from operant.persistence.sqlite import NotFoundError
from operant.skills.discovery import SkillDiscovery, SkillDiscoveryLimits
from operant.skills.sources import default_skill_roots

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


def _requested_source_path(raw: str | Path) -> Path:
    supplied = str(raw)
    if len(supplied) > 4_096 or not supplied:
        raise ValueError("Skill source path must be a bounded absolute directory")
    if any(character in supplied for character in ("\x00", "\n", "\r")):
        raise ValueError("Skill source path contains a control character")
    path = Path(supplied)
    if not path.is_absolute() and not supplied.startswith("~"):
        raise ValueError("Skill source path must be a bounded absolute directory")
    return path


def _source_path(raw: str | Path) -> Path:
    try:
        return _requested_source_path(raw).expanduser().resolve(strict=False)
    except RuntimeError as exc:
        raise ValueError("Skill source home directory is unavailable") from exc


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
        saved_sources = _saved_sources(repo)
        definitions.update({key: Path(value) for key, value in saved_sources.items()})
        active: dict[str, Path] = {}
        seen: dict[Path, str] = {}
        views: list[SkillSourceView] = []
        for root_ref, raw_path in definitions.items():
            try:
                path = _source_path(raw_path)
                # Added sources are stored as their real paths. A later symlink
                # replacement must not silently authorize a different root.
                changed_source = root_ref in saved_sources and path != raw_path
                exists = not changed_source and path.is_dir()
                duplicate = seen.get(path) if exists else None
                if exists and duplicate is None and len(active) < max_roots:
                    seen[path] = root_ref
                    active[root_ref] = path
                issue = f"与 {duplicate} 指向同一目录" if duplicate else None
                if changed_source:
                    path = raw_path
                    issue = "目录已改变，请重新添加来源"
                elif not exists:
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
            removed = (set(previous_active) - set(active)) | {
                view.root_ref for view in views if not view.enabled
            }
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

    effects = SkillSourceEffects(
        store=service.store,
        settings=repo,
        gateway=lambda: app.state.phase45_action_gateway,
        refresh=refresh_skill_sources,
        requested_path=_requested_source_path,
        source_path=_source_path,
    )
    app.state.skill_source_effects = effects

    @app.get(
        "/v1/setup/skill-sources", operation_id="listSkillSources", response_model=SkillSourceList
    )
    def list_skill_sources(request: Request) -> SkillSourceList:
        require_local(request)
        return effects.list_sources()

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
            # Preserve the original lexical failure before assigning a command
            # key or looking up any part of the requested directory.
            requested = _requested_source_path(body.path)
            key = command_key(idempotency_key, response)
            return effects.add_source(requested, key)
        except SkillSourceOperationError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
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
        try:
            return effects.remove_source(root_ref, key)
        except SkillSourceOperationError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
