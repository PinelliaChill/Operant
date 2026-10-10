"""Effects shared by local Skill source adapters after caller authentication."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from operant.application.phase45_gateway import Phase45ActionGateway
from operant.contracts.onboarding import SkillSourceList, SkillSourceView
from operant.domain.actions import CommandExecution, CommandExecutionStatus
from operant.domain.security import Capability
from operant.persistence.sqlite import ConflictError, SQLiteStore
from operant.protocol import canonical_action_hash

_SETTING_KEY = "skill_sources.v1"


class SkillSourceSettings(Protocol):
    def get_setting(self, key: str, default: Any = None) -> Any: ...

    def set_setting(self, key: str, value: Any) -> None: ...


def saved_sources(settings: SkillSourceSettings) -> dict[str, str]:
    saved = settings.get_setting(_SETTING_KEY, {})
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


class SkillSourceRefresh(Protocol):
    def __call__(
        self,
        workspace_ref: str | Path | None = None,
        *,
        discover: bool = False,
        gateway_override: Phase45ActionGateway | None = None,
    ) -> list[SkillSourceView]: ...


class SkillSourceOperationError(Exception):
    """A stable adapter boundary for a rejected or unknown source command."""

    def __init__(self, status_code: int, detail: str | dict[str, str | None]) -> None:
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


class SkillSourceEffects:
    """Run source changes through their original Gateway and command journal.

    Adapters must authenticate the caller before invoking these methods. The
    directory supplied by that caller remains unrestricted beyond the product's
    existing absolute-directory checks.
    """

    def __init__(
        self,
        *,
        store: SQLiteStore,
        settings: SkillSourceSettings,
        gateway: Callable[[], Phase45ActionGateway],
        refresh: SkillSourceRefresh,
        requested_path: Callable[[str | Path], Path],
        source_path: Callable[[str | Path], Path],
    ) -> None:
        self.store = store
        self.settings = settings
        self.gateway = gateway
        self.refresh = refresh
        self.requested_path = requested_path
        self.source_path = source_path

    def list_sources(self) -> SkillSourceList:
        return SkillSourceList(items=self.refresh())

    def add_source(self, raw_path: str | Path, key: str) -> SkillSourceView:
        requested = self.requested_path(raw_path)
        raw_guard_fingerprint = canonical_action_hash(
            {"operation": "skill_source_add", "path": str(requested)}
        )
        # Bind the lexical request before path lookup. The versioned security
        # key preserves older canonical-path actions and the original command
        # key still binds the canonical effect receipt.
        guard_key = "skill-source-add-v2:" + hashlib.sha256(key.encode()).hexdigest()
        self._guard(
            "add",
            hashlib.sha256(str(requested).encode()).hexdigest()[:32],
            raw_guard_fingerprint,
            guard_key,
        )
        path = self.source_path(requested)
        canonical_fingerprint = canonical_action_hash(
            {"operation": "skill_source_add", "path": str(path)}
        )
        prior = self._prior_result(key, canonical_fingerprint)
        if prior is not None:
            return SkillSourceView.model_validate(prior)
        if not path.is_dir() or path in {Path(path.anchor), Path.home().resolve()}:
            raise ValueError("choose an existing Skill directory")
        saved = saved_sources(self.settings)
        current = self.refresh()
        duplicate = next(
            (item for item in current if item.enabled and Path(item.path) == path), None
        )
        root_ref = "user-" + hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:12]
        if duplicate is None and root_ref in saved and saved[root_ref] != str(path):
            raise ValueError("Skill source reference collision")
        if duplicate is None and len(saved) >= 8:
            raise ValueError("too many additional Skill sources")
        command, created = self._reserve(key, canonical_fingerprint)
        if not created:
            replay = self._prior_result(key, canonical_fingerprint)
            assert replay is not None
            return SkillSourceView.model_validate(replay)
        if duplicate is None:
            saved[root_ref] = str(path)
            self.settings.set_setting(_SETTING_KEY, saved)
            result = next(item for item in self.refresh(discover=True) if item.root_ref == root_ref)
        else:
            result = duplicate
        self._finish(command, result)
        return result

    def remove_source(self, root_ref: str, key: str) -> SkillSourceList:
        fingerprint = canonical_action_hash(
            {"operation": "skill_source_remove", "root_ref": root_ref}
        )
        self._guard("remove", root_ref, fingerprint, key)
        prior = self._prior_result(key, fingerprint)
        if prior is not None:
            return SkillSourceList.model_validate(prior)
        saved = saved_sources(self.settings)
        if root_ref not in saved:
            raise SkillSourceOperationError(404, "added Skill source not found")
        command, created = self._reserve(key, fingerprint)
        if not created:
            replay = self._prior_result(key, fingerprint)
            assert replay is not None
            return SkillSourceList.model_validate(replay)
        del saved[root_ref]
        self.settings.set_setting(_SETTING_KEY, saved)
        result = SkillSourceList(items=self.refresh(discover=True))
        self._finish(command, result)
        return result

    def _guard(self, operation: str, target_id: str, fingerprint: str, key: str) -> None:
        gateway = self.gateway()
        try:
            action, decision, _ = gateway.guard(
                tool="skill_source",
                operation=operation,
                target_id=target_id,
                arguments={"request_hash": fingerprint},
                capabilities=(Capability.WORKSPACE_WRITE,),
                idempotency_key=key,
            )
        except ConflictError as exc:
            raise SkillSourceOperationError(409, "request identity changed") from exc
        if decision.decision.value != "allow" or decision.lease is None:
            if decision.decision.value == "ask":
                raise SkillSourceOperationError(
                    409,
                    {
                        "code": "approval_required",
                        "approval_id": decision.approval_id,
                        "reason_code": decision.reason_code,
                    },
                )
            raise SkillSourceOperationError(403, decision.reason_code)
        gateway.consume(decision.lease, action)

    def _prior_result(self, key: str, fingerprint: str) -> dict[str, Any] | None:
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT id FROM command_executions WHERE command_type=? AND idempotency_key=?",
                ("skill_source.change", key),
            ).fetchone()
        if row is None:
            return None
        command = self.store.get_command_execution(str(row["id"]))
        if command.action_hash != fingerprint:
            raise SkillSourceOperationError(409, "request identity changed")
        if command.status is CommandExecutionStatus.COMPLETED and command.response_json:
            return dict(json.loads(command.response_json))
        raise SkillSourceOperationError(
            409,
            {"code": "command_outcome_unknown", "message": "请刷新来源后核对结果"},
        )

    def _reserve(self, key: str, fingerprint: str) -> tuple[CommandExecution, bool]:
        try:
            return self.store.reserve_command_execution(
                CommandExecution(
                    command_type="skill_source.change",
                    idempotency_key=key,
                    action_hash=fingerprint,
                )
            )
        except ConflictError as exc:
            raise SkillSourceOperationError(409, "request identity changed") from exc

    def _finish(self, command: CommandExecution, result: SkillSourceView | SkillSourceList) -> None:
        self.store.complete_command_execution(
            command.id,
            response_json=result.model_dump_json(),
            http_status=200,
            resource_type="skill_source",
            resource_id=(result.root_ref if isinstance(result, SkillSourceView) else None),
        )
