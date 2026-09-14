"""Core integration of generic persistence, installed engines and lifecycle fences."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
from contextlib import nullcontext
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from operant.application.service import ApplicationService
from operant.contracts.b2_1 import (
    CandidateBatch,
    CandidateReference,
    Certification,
    HostReadRequest,
    HostReadResult,
    IndexEvent,
    MemoryConditions,
    MemoryHead,
    MemoryProposal,
    MemoryVersion,
    MemoryVersionRef,
    PluginManifest,
    ProposalBatch,
    RecallRequest,
    RpcContext,
    SourceBatch,
    SourceRef,
    WorkspaceScope,
)
from operant.contracts.b2_3 import (
    ManagedMemory,
    ManagedProposal,
    ManagementCommand,
    ManagementResult,
    ManagementState,
)
from operant.domain.memory import Memory, MemoryStatus
from operant.domain.models import utc_now
from operant.domain.threads import ConversationThread, Item, Turn, UserMessagePayload
from operant.memory_plugins.ledger import MemoryLedger
from operant.plugins.host import PluginHost
from operant.plugins.protocol import HostCallbacks, PluginError
from operant.plugins.registry import PluginRegistry


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class MemoryManager:
    def __init__(
        self,
        service: ApplicationService,
        *,
        host: PluginHost | None = None,
        skill_roots: dict[str, str | Path] | None = None,
    ) -> None:
        self.skill_roots = {key: Path(value) for key, value in (skill_roots or {}).items()}
        self.service = service
        self.store = service.store
        self.ledger = MemoryLedger(self.store.path, initialize=False)
        self.root = self.store.path.absolute().parent / "memory-plugins"
        self.registry = (
            host.registry
            if host
            else PluginRegistry(self.root, trusted_issuers=("operant-bundled",))
        )
        self.host = host or PluginHost(self.registry)
        self.host.callbacks = HostCallbacks(
            authorize_source=self.authorize_source,
            read_source=self.read_source,
            authorize_memory_ref=self.authorize_ref,
            search=self.search,
            authorize_head=self.authorize_head,
            authorize_proposal=self.authorize_proposal,
        )
        self._lock = asyncio.Lock()
        self._pending_refs: dict[str, MemoryVersionRef] = {}
        self._active_skill_runs: dict[str, set[str]] = {}
        self._owns_host = host is None
        self.catalog_root = Path(__file__).resolve().parents[3] / "plugins"
        self._state = self._load()
        self._state.setdefault("skill_digests", {})
        history = self._state.setdefault("setting_history", {})
        for setting in self._project_settings():
            identity = f"{setting['scope']}:{setting['key']}"
            history.setdefault(
                identity,
                {
                    "fingerprint": digest(json.dumps(setting, sort_keys=True)),
                    "effective_at": "unknown",
                },
            )

    def _load(self) -> dict[str, Any]:
        with self.store._connect() as c:
            row = c.execute("SELECT body FROM b23_management WHERE key='state'").fetchone()
        return (
            json.loads(row["body"])
            if row
            else {
                "projects": [],
                "global_enabled": True,
                "modes": {},
                "plugin_config": {},
                "skills": [],
                "dataset_plugins": {},
                "cleanup": {},
                "effective_at": utc_now().isoformat(),
                "setting_history": {
                    "global:memory_enabled": {
                        "fingerprint": digest(
                            json.dumps(
                                {
                                    "key": "memory_enabled",
                                    "value": True,
                                    "source": "global",
                                    "scope": "global",
                                },
                                sort_keys=True,
                            )
                        ),
                        "effective_at": utc_now().isoformat(),
                    }
                },
            }
        )

    def _save(self) -> None:
        self._state["effective_at"] = utc_now().isoformat()
        history = self._state["setting_history"]
        current = set()
        for setting in self._project_settings():
            identity = f"{setting['scope']}:{setting['key']}"
            current.add(identity)
            fingerprint = digest(json.dumps(setting, sort_keys=True))
            if history.get(identity, {}).get("fingerprint") != fingerprint:
                history[identity] = {
                    "fingerprint": fingerprint,
                    "effective_at": self._state["effective_at"],
                }
        for identity in set(history) - current:
            del history[identity]
        with self.store._connect() as c:
            c.execute(
                "INSERT INTO b23_management(key,body) VALUES('state',?) "
                "ON CONFLICT(key) DO UPDATE SET body=excluded.body",
                (json.dumps(self._state),),
            )

    def _project(self, project_id: str | None) -> dict[str, Any]:
        for p in self._state["projects"]:
            if p["project_id"] == project_id:
                return cast(dict[str, Any], p)
        raise ValueError("project not found")

    @staticmethod
    def _required(value: Any, label: str) -> Any:
        if value is None or value == "":
            raise ValueError(f"{label} is required")
        return value

    def _scope(self, p: dict[str, Any]) -> WorkspaceScope:
        return WorkspaceScope(
            kind="workspace", project_id=p["project_id"], workspace_id=p["workspace_id"]
        )

    def _installation(self, p: dict[str, Any], *, enabled: bool = True) -> Any:
        if p["archived"]:
            raise PluginError("permission_denied", "project archived")
        installation = self.registry.get_installation(
            self._required(p["installation_id"], "selected plugin")
        )
        if enabled and (
            not self._state["global_enabled"]
            or not p["memory_enabled"]
            or installation.state != "enabled"
        ):
            raise PluginError("memory_disabled", "memory is disabled for this project")
        return installation

    def _catalog(self) -> list[dict[str, Any]]:
        result = []
        for path in sorted(self.catalog_root.glob("*/config.schema.json")):
            schema = json.loads(path.read_text())
            result.append(
                {
                    "plugin_id": path.parent.name,
                    "name": schema.get("title", path.parent.name),
                    "description": schema.get("description", "独立记忆插件"),
                    "config_schema": schema,
                }
            )
        return result

    def _records(self, project: dict[str, Any], query: str | None = None) -> list[ManagedMemory]:
        iid = project["installation_id"]
        if not iid:
            return []
        installation = self.registry.get_installation(iid)
        versions = self.ledger.query(
            installation.dataset_id,
            query,
            scope=self._scope(project),
            include_candidates=True,
            include_inactive=True,
            include_legacy=True,
        )
        result = []
        seen_records: set[str] = set()
        for version in versions:
            if version.ref.record_id in seen_records:
                continue
            seen_records.add(version.ref.record_id)
            head = self.ledger.get_head(installation.dataset_id, version.ref.record_id)
            if head.published_version is not None:
                version = self.ledger.get_version(
                    installation.dataset_id, head.record_id, head.published_version.version
                )
            else:
                version = max(
                    self.ledger.list_versions(installation.dataset_id, head.record_id),
                    key=lambda item: item.ref.version,
                )
            proposals = self.ledger.list_proposals(
                installation.dataset_id, record_id=version.ref.record_id
            )
            result.append(
                ManagedMemory(
                    record_id=version.ref.record_id,
                    dataset_id=version.ref.dataset_id,
                    project_id=project["project_id"],
                    content=version.content,
                    state=head.state,
                    revision=head.revision,
                    evidence=version.evidence,
                    version=version.ref.version,
                    sources=[s.model_dump(mode="json") for s in version.sources],
                    proposals=[
                        ManagedProposal(
                            proposal_id=q.proposal_id,
                            content=self.ledger.get_version(
                                q.proposed_version.dataset_id,
                                q.proposed_version.record_id,
                                q.proposed_version.version,
                            ).content,
                            state=q.state,
                            expected_revision=q.base_head.revision,
                        )
                        for q in proposals
                    ],
                )
            )
        return result

    def _project_settings(self) -> list[dict[str, Any]]:
        settings = [
            {
                "key": "memory_enabled",
                "value": self._state["global_enabled"],
                "source": "global",
                "scope": "global",
            }
        ]
        for p in self._state["projects"]:
            settings.append(
                {
                    "key": "memory_enabled",
                    "value": bool(self._state["global_enabled"] and p["memory_enabled"]),
                    "source": "project" if self._state["global_enabled"] else "global",
                    "scope": p["project_id"],
                }
            )
        for p in self._state["projects"]:
            if p["installation_id"]:
                installation = self.registry.get_installation(p["installation_id"])
                for key, value in (
                    ("memory_engine", installation.manifest.plugin_id),
                    ("dataset_id", installation.dataset_id),
                    (
                        "plugin_config",
                        self._state["plugin_config"].get(installation.installation_id, {}),
                    ),
                ):
                    settings.append(
                        {
                            "key": key,
                            "value": value,
                            "source": "project binding",
                            "scope": p["project_id"],
                        }
                    )
        return settings

    def projection(self) -> ManagementState:
        datasets = []
        for d in self.registry.state.datasets:
            with self.store._connect() as c:
                count = c.execute(
                    "SELECT count(*) FROM memory_ledger_heads WHERE dataset_id=? AND state != ?",
                    (d.dataset_id, "deleted"),
                ).fetchone()[0]
            datasets.append(
                {
                    "dataset_id": d.dataset_id,
                    "plugin_id": self._state["dataset_plugins"].get(d.dataset_id, ""),
                    "installation_id": d.installation_id,
                    "state": d.state,
                    "record_count": count,
                    "exceptions": [
                        "历史会话及已发送上下文保留",
                        "独立导出、Skill、备份及外部副本不受此操作影响",
                        "删除仅清理专属资源，不是磁盘安全擦除",
                    ],
                }
            )
        installations = []
        for i in self.registry.list_installations():
            installations.append(
                {
                    "installation_id": i.installation_id,
                    "plugin_id": i.manifest.plugin_id,
                    "dataset_id": i.dataset_id,
                    "binding_id": i.binding_id,
                    "state": i.state,
                    "mode": self._state["modes"].get(i.installation_id, "isolated"),
                    "certification_status": "valid"
                    if self.registry.certification_is_current(i)
                    else "uncertified",
                    "config": self._state["plugin_config"].get(i.installation_id, {}),
                }
            )
        settings = self._project_settings()
        for setting in settings:
            identity = f"{setting['scope']}:{setting['key']}"
            setting["effective_at"] = self._state["setting_history"][identity]["effective_at"]
        for role in self.service.list_roles():
            for key, value in (
                ("model_profile_id", role.model_profile_id),
                ("prompt", role.system_prompt),
                ("budget", role.budget.model_dump(mode="json")),
                ("effort", role.effort.value),
            ):
                settings.append(
                    {
                        "key": key,
                        "value": value,
                        "source": "role; next Session snapshot",
                        "scope": role.id,
                        "effective_at": role.created_at.isoformat(),
                    }
                )
        return ManagementState.model_validate(
            {
                "projects": self._state["projects"],
                "global_enabled": self._state["global_enabled"],
                "catalog": self._catalog(),
                "installations": installations,
                "datasets": datasets,
                "settings": settings,
                "skills": self._state["skills"],
                "skill_catalog": self._skill_catalog(),
                "artifacts": self._artifacts(),
                "records": [
                    r.model_dump() for p in self._state["projects"] for r in self._records(p)
                ],
            }
        )

    def _skill_catalog(self) -> list[dict[str, str]]:
        from operant.persistence.phase45 import SQLitePhase45Repository

        return [
            {"package_ref": c["candidate_id"], "name": c["name"]}
            for c in SQLitePhase45Repository(self.store).list_skill_candidates()
        ]

    async def execute(
        self, command: ManagementCommand, *, idempotency_key: str | None = None
    ) -> ManagementResult:
        key = idempotency_key or uuid4().hex
        request_digest = digest(command.model_dump_json())
        closing = command.action in {
            "plugin_disable",
            "plugin_uninstall",
            "project_archive",
            "project_detach",
        } or (command.action == "memory_switch" and command.enabled is False)
        async with nullcontext() if closing else self._lock:
            with self.store._connect() as c:
                old = c.execute("SELECT * FROM b23_commands WHERE command_id=?", (key,)).fetchone()
                if old:
                    if old["request_digest"] != request_digest:
                        raise PluginError("revision_conflict", "idempotency key payload differs")
                    if old["state"] != "completed":
                        raise PluginError(
                            "manual_reconcile_required",
                            "previous result is unknown; inspect state before a new action",
                        )
                    cached = json.loads(old["result"])
                    if command.action == "memory_search":
                        self._installation(self._project(command.project_id))
                    payload = cached.get("payload", cached)
                    if (
                        command.action in ("memory_search", "dataset_export")
                        and "payload" not in cached
                    ):
                        raise PluginError(
                            "result_unavailable", "the original query result was not retained"
                        )
                    return ManagementResult(**payload, state=self.projection())
                c.execute(
                    "INSERT INTO b23_commands VALUES(?,?,?,NULL)", (key, request_digest, "pending")
                )
            try:
                status, message, export, records = await self._execute(command)
                result = ManagementResult(
                    status=status,
                    message=message,
                    state=self.projection(),
                    export_data=export,
                    records=records,
                )
                with self.store._connect() as c:
                    c.execute(
                        "UPDATE b23_commands SET state='completed',result=? WHERE command_id=?",
                        (
                            json.dumps(
                                {
                                    "payload": result.model_dump(mode="json", exclude={"state"}),
                                    "dataset_ids": sorted(
                                        {r.dataset_id for r in (result.records or [])}
                                        | (
                                            {command.dataset_id}
                                            if command.action == "dataset_export"
                                            and command.dataset_id
                                            else set()
                                        )
                                    ),
                                }
                            ),
                            key,
                        ),
                    )
                return result
            except Exception:
                # Unknown mutation outcomes are never automatically replayed.
                raise

    async def _execute(self, cmd: ManagementCommand) -> tuple[str, str, Any, Any]:
        action = cmd.action
        if action == "project_create":
            initialization, _ = self.service.initialize_workspace(
                self._required(cmd.workspace_path, "workspace_path")
            )
            if any(
                p["workspace_id"] == initialization.id and not p["archived"]
                for p in self._state["projects"]
            ):
                raise ValueError("workspace already belongs to a project")
            self._state["projects"].append(
                {
                    "project_id": "project_" + uuid4().hex,
                    "name": self._required(cmd.name, "name"),
                    "workspace_id": initialization.id,
                    "archived": False,
                    "memory_enabled": True,
                    "installation_id": None,
                }
            )
        elif action in ("project_update", "project_archive", "project_detach"):
            p = self._project(cmd.project_id)
            if action == "project_update":
                p["name"] = self._required(cmd.name, "name")
            else:
                await self._stop_project(p)
                if action == "project_archive":
                    p["archived"] = True
                else:
                    p["installation_id"] = None
        elif action == "plugin_install":
            await self._install(cmd)
        elif action in ("plugin_enable", "plugin_disable", "plugin_uninstall"):
            i = self.registry.get_installation(
                self._required(cmd.installation_id, "installation_id")
            )
            if action == "plugin_enable":
                if not self._state["global_enabled"]:
                    raise PluginError("memory_disabled", "global memory switch is off")
                if not i.binding_id:
                    raise ValueError("installation has no binding")
                self.host.enable(i.binding_id)
                await self.host.start(
                    i.installation_id, mode=self._state["modes"].get(i.installation_id, "isolated")
                )
            elif action == "plugin_disable":
                receipt = await self.host.stop(
                    i.installation_id, stop_run_ids=self._management_runs(i.installation_id)
                )
                return (
                    (receipt.ack if receipt.ack == "completed" else receipt.state),
                    (receipt.ack if receipt.ack == "completed" else receipt.state),
                    None,
                    None,
                )
            else:
                return await self._uninstall(i, self._required(cmd.data_policy, "data_policy"))
        elif action == "binding_select":
            p = self._project(cmd.project_id)
            new = self.registry.get_installation(
                self._required(cmd.installation_id, "installation_id")
            )
            if new.state == "uninstalled":
                raise ValueError("plugin is uninstalled")
            if p["installation_id"] and p["installation_id"] != new.installation_id:
                await self._stop_project(p)
            # Project binding does not grant implicit dataset sharing.
            # A different project needs a separate installation.
            if any(
                other["project_id"] != p["project_id"]
                and other["installation_id"] == new.installation_id
                for other in self._state["projects"]
            ):
                raise PluginError(
                    "shared_consumer", "installation is already selected by another project"
                )
            p["installation_id"] = new.installation_id
        elif action == "memory_switch":
            enabled = self._required(cmd.enabled, "enabled")
            if cmd.project_id:
                p = self._project(cmd.project_id)
                p["memory_enabled"] = enabled
                self._save()
                if not enabled:
                    await self._stop_project(p)
            else:
                self._state["global_enabled"] = enabled
                self._save()
                for binding in self.registry.list_bindings():
                    self.registry.set_global_enabled(binding.binding_id, enabled)
                if not enabled:
                    outcomes = []
                    for i in self.registry.list_installations():
                        if i.state in ("enabled", "disabling", "failed", "restart_required"):
                            receipt = await self.host.stop(
                                i.installation_id,
                                stop_run_ids=self._management_runs(i.installation_id),
                            )
                            if receipt.ack != "completed":
                                outcomes.append(receipt.state)
                    if outcomes:
                        return (
                            ("restart_required" if "restart_required" in outcomes else "blocked"),
                            "全局访问已关闭；部分插件仍受活动 Run 或重启屏障阻断",
                            None,
                            None,
                        )
        elif action in (
            "memory_save",
            "memory_propose",
            "memory_confirm",
            "memory_deactivate",
            "memory_search",
        ):
            return await self._memory(cmd)
        elif action == "memory_migrate":
            p = self._project(cmd.project_id)
            i = self._installation(p)
            with self.store._connect() as c:
                # Migrate only the explicitly registered project scope.
                workspace = self.store.get_workspace_initialization_by_id(p["workspace_id"])
                rows = [
                    dict(r)
                    for r in c.execute(
                        "SELECT v.* FROM memory_versions v "
                        "JOIN memories m ON m.id=v.memory_id "
                        "WHERE v.project_scope=? ORDER BY v.memory_id,v.version",
                        (workspace.workspace_ref,),
                    )
                ]
            migration = self.ledger.migrate_legacy(rows, owner=i.owner, scope=self._scope(p))
            return (
                "completed",
                f"迁移 {migration.imported_count} 条；旧证据保持 legacy_unverified，未自动发布",
                None,
                None,
            )
        elif action == "dataset_export":
            dataset_id = self._required(cmd.dataset_id, "dataset_id")
            self._dataset(dataset_id)
            return (
                "completed",
                "已导出数据集；独立副本需自行管理",
                self.ledger.export_dataset(dataset_id),
                None,
            )
        elif action == "dataset_delete":
            return await self._delete_retained(self._required(cmd.dataset_id, "dataset_id"))
        elif action == "cleanup_resume":
            await self.host.resume_cleanup()
            for dataset_id in tuple(self._state["cleanup"]):
                await self._finish_delete(dataset_id)
        elif action == "plugin_configure":
            i = self.registry.get_installation(
                self._required(cmd.installation_id, "installation_id")
            )
            if i.state != "disabled":
                raise PluginError("revision_conflict", "disable the plugin before configuring it")
            config = self._required(cmd.config, "config")
            self._validate_config(i, config)
            self._state["plugin_config"][i.installation_id] = config
            self.registry.configure(i.installation_id, config)
        elif action in (
            "skill_discover",
            "skill_install",
            "skill_disable",
            "skill_enable",
            "skill_uninstall",
        ):
            self._skill(cmd)
        elif action.startswith("artifact_"):
            if action == "artifact_audit":
                return (
                    "completed",
                    "工件审计完成；未执行清理",
                    {"artifact_audit": self.service.audit_artifacts().model_dump(mode="json")},
                    None,
                )
            self._artifact_command(cmd)
        else:
            raise ValueError("unsupported management action")
        self._save()
        return "completed", "已保存，状态来自 Core", None, None

    def _management_runs(self, installation_id: str) -> tuple[str, ...]:
        return tuple(
            run.run_id
            for run in self.registry.state.runs
            if run.installation_id == installation_id
            and run.status == "active"
            and run.run_id.startswith("management_")
        )

    async def _stop_project(self, p: dict[str, Any]) -> None:
        if p["installation_id"]:
            receipt = await self.host.stop(
                p["installation_id"], stop_run_ids=self._management_runs(p["installation_id"])
            )
            if (receipt.ack if receipt.ack == "completed" else receipt.state) not in (
                "completed",
                "disabled",
            ):
                raise PluginError(
                    (receipt.ack if receipt.ack == "completed" else receipt.state),
                    "project change blocked by active plugin work",
                )

    def _dataset(self, dataset_id: str) -> Any:
        for d in self.registry.state.datasets:
            if d.dataset_id == dataset_id:
                return d
        raise ValueError("dataset not found")

    async def _uninstall(
        self, i: Any, policy: Literal["keep", "delete"]
    ) -> tuple[str, str, Any, Any]:
        if policy == "delete":
            self._state["cleanup"][i.dataset_id] = i.installation_id
            self._save()
        receipt = await self.host.uninstall(
            i.installation_id,
            data_policy=policy,
            stop_run_ids=self._management_runs(i.installation_id),
        )
        if (receipt.ack if receipt.ack == "completed" else receipt.state) == "completed":
            if policy == "delete":
                self._state["cleanup"][i.dataset_id] = i.installation_id
                self._save()
                await self._finish_delete(i.dataset_id)
            self._save()
        return (
            (receipt.ack if receipt.ack == "completed" else receipt.state),
            "卸载状态："
            + (receipt.ack if receipt.ack == "completed" else receipt.state)
            + "；历史会话、独立导出与备份保留",
            None,
            None,
        )

    async def _finish_delete(self, dataset_id: str) -> None:
        # MP-2 delete is an explicit lifecycle command, never ordinary record
        # deactivation. The durable plan survives interruption; keep does not
        # create this authorization. No user workspace files are included.
        if dataset_id not in self._state["cleanup"]:
            raise PluginError("cleanup_blocked", "no explicit dataset deletion plan")
        for installation in self.registry.list_installations():
            if installation.dataset_id != dataset_id:
                continue
            if self.registry.resources_for(installation.installation_id):
                receipt = await self.host.uninstall(
                    installation.installation_id, data_policy="delete"
                )
                if receipt.ack != "completed":
                    raise PluginError(
                        "cleanup_blocked", "dataset still has protected or active resources"
                    )
        self.ledger.delete_dataset(dataset_id)
        dataset = self._dataset(dataset_id)
        if dataset.state != "deleted" or dataset.installation_id is not None:
            raise PluginError("cleanup_blocked", "Host resources have not finished deletion")
        # Keep the immutable-write guard for all ordinary ledger operations.
        # Only this Core-owned transaction, after the Host completion barrier,
        # removes rows in the exact plugin dataset namespace. Canonical Items,
        # Context history, Artifacts, Skills and external copies are untouched.
        with self.ledger._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            self.ledger._enable_purge(c)
            for table in (
                "memory_ledger_versions",
                "memory_ledger_proposals",
                "memory_ledger_legacy_imports",
                "memory_ledger_idempotency",
                "memory_ledger_heads",
                "b23_sources",
            ):
                c.execute(f"DELETE FROM {table} WHERE dataset_id=?", (dataset_id,))
            self.ledger._disable_purge(c)
            for row in c.execute(
                "SELECT command_id,result FROM b23_commands WHERE result IS NOT NULL"
            ).fetchall():
                cached = json.loads(row["result"])
                if dataset_id in cached.get("dataset_ids", []):
                    c.execute(
                        "UPDATE b23_commands SET state='purged',result=NULL WHERE command_id=?",
                        (row["command_id"],),
                    )
        self._state["cleanup"].pop(dataset_id, None)
        self._save()

    async def _delete_retained(self, dataset_id: str) -> tuple[str, str, Any, Any]:
        d = self._dataset(dataset_id)
        if d.state != "retained":
            raise PluginError(
                "cleanup_blocked", "only retained uninstalled datasets may be deleted here"
            )
        for i in self.registry.list_installations():
            if i.dataset_id == dataset_id:
                # Host inventories cover retained paths even when code is absent.
                result = await self._uninstall(i, "delete")
                if result[0] != "completed":
                    return result
        if dataset_id in self._state["cleanup"]:
            await self._finish_delete(dataset_id)
        return "completed", "专属数据已删除；来源历史与独立副本保留，不是磁盘安全擦除", None, None

    def _validate_config(self, i: Any, config: dict[str, Any]) -> None:
        schema = json.loads(
            (self.registry.package_path(i.installation_id) / "config.schema.json").read_text()
        )
        properties = schema.get("properties", {})
        if set(config) - set(properties):
            raise ValueError("unknown plugin configuration field")
        for name, value in config.items():
            rule = properties[name]
            expected = rule.get("type")
            if expected == "string" and not isinstance(value, str):
                raise ValueError("configuration type mismatch")
            if expected == "boolean" and not isinstance(value, bool):
                raise ValueError("configuration type mismatch")
            if expected == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
                raise ValueError("configuration type mismatch")
            if "enum" in rule and value not in rule["enum"]:
                raise ValueError("configuration value is not supported")
            if isinstance(value, int) and (
                value < rule.get("minimum", value) or value > rule.get("maximum", value)
            ):
                raise ValueError("configuration outside supported range")

    async def _install(self, cmd: ManagementCommand) -> None:
        plugin_id = self._required(cmd.plugin_id, "plugin_id")
        if plugin_id not in {c["plugin_id"] for c in self._catalog()}:
            raise ValueError("unknown configured package")
        package = self.catalog_root / plugin_id
        spec = importlib.util.spec_from_file_location(
            "_manifest_" + uuid4().hex, package / "manifest.py"
        )
        if spec is None or spec.loader is None:
            raise ValueError("package manifest missing")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        manifest = PluginManifest.model_validate(module.build_manifest())
        mode = cmd.mode or "isolated"
        certification = None
        if mode == "trusted_in_process":
            # Only the operator-controlled bundled catalog can receive this local trust record.
            certification = Certification(
                certification_id="cert_" + uuid4().hex,
                issuer_id="operant-bundled",
                plugin_id=manifest.plugin_id,
                plugin_version=manifest.plugin_version,
                package_digest=manifest.package_digest,
                dependencies_digest=manifest.dependencies_digest,
                permissions_digest=manifest.permissions_digest,
                lifecycle_evidence_digest=digest(
                    "operant-bundled-install:" + manifest.package_digest
                ),
                allowed_modes=("trusted_in_process", "isolated"),
                valid_from=utc_now() - timedelta(seconds=1),
                expires_at=utc_now() + timedelta(days=30),
                revocation_epoch=0,
                state="valid",
                signature_ref="local-bundled-trust",
            )
        if cmd.dataset_id:
            d = self._dataset(cmd.dataset_id)
            if (
                d.state != "retained"
                or self._state["dataset_plugins"].get(d.dataset_id) != plugin_id
            ):
                raise PluginError(
                    "unknown_owner", "retained dataset must belong to the same plugin"
                )
        i = self.registry.install(
            manifest, package.absolute(), dataset_id=cmd.dataset_id, certification=certification
        )
        binding = self.host.bind(i.installation_id, global_enabled=self._state["global_enabled"])
        self._state["modes"][i.installation_id] = mode
        self._state["dataset_plugins"][i.dataset_id] = plugin_id
        self._state["plugin_config"][i.installation_id] = {}
        self._save()
        if self._state["global_enabled"]:
            self.host.enable(binding.binding_id)
            await self.host.start(i.installation_id, mode=mode)

    def _skill(self, cmd: ManagementCommand) -> None:
        from operant.persistence.phase45 import SQLitePhase45Repository
        from operant.plugins.protocol import compute_package_digest, copy_package
        from operant.skills import SkillDiscovery

        repository = SQLitePhase45Repository(self.store)
        if cmd.action == "skill_discover":
            if not self.skill_roots:
                raise ValueError("尚未配置可信 Skill 根目录")
            for root_ref, root in self.skill_roots.items():
                discovered = SkillDiscovery((root,)).discover()
                repository.replace_skill_candidates(root_ref, discovered.candidates)
            return
        if cmd.action in ("skill_enable", "skill_disable", "skill_uninstall"):
            skill = next((s for s in self._state["skills"] if s["skill_id"] == cmd.skill_id), None)
            if skill is None or skill["state"] == "uninstalled":
                raise ValueError("installed skill not found")
            if cmd.action == "skill_uninstall":
                if any(skill["skill_id"] in ids for ids in self._active_skill_runs.values()):
                    raise PluginError("cleanup_blocked", "active Run holds this Skill snapshot")
                from operant.plugins.protocol import _remove_managed_path

                _remove_managed_path(self.root / "skills" / skill["skill_id"], directory=True)
                skill.update(state="uninstalled", project_ids=[])
            elif cmd.project_id:
                self._project(cmd.project_id)
                enabled_projects = set(skill.get("project_ids", []))
                if cmd.action == "skill_enable":
                    enabled_projects.add(cmd.project_id)
                    skill["state"] = "installed"
                else:
                    enabled_projects.discard(cmd.project_id)
                skill["project_ids"] = sorted(enabled_projects)
            else:
                skill["state"] = "installed" if cmd.action == "skill_enable" else "disabled"
            return
        candidate = next(
            (s for s in repository.list_skill_candidates() if s["candidate_id"] == cmd.package_ref),
            None,
        )
        if candidate is None:
            raise ValueError("discover the existing Skill package before installing")
        selected_root = self.skill_roots.get(candidate["root_ref"])
        if selected_root is None:
            raise PermissionError("configured Skill root is unavailable")
        current = next(
            (
                s
                for s in SkillDiscovery((selected_root,)).discover().candidates
                if s.relative_directory == candidate["relative_directory"]
            ),
            None,
        )
        if current is None or current.manifest_sha256 != candidate["manifest_sha256"]:
            raise PluginError("revision_conflict", "Skill package changed after discovery")
        if any(
            s["package_ref"] == cmd.package_ref and s["state"] == "installed"
            for s in self._state["skills"]
        ):
            raise ValueError("Skill is already installed")
        source = selected_root / current.relative_directory
        before = compute_package_digest(source)
        skill_id = "skill_" + uuid4().hex
        destination = self.root / "skills" / skill_id
        destination.parent.mkdir(exist_ok=True)
        copy_package(source, destination)
        if compute_package_digest(destination) != before:
            raise PluginError("revision_conflict", "Skill copy integrity mismatch")
        self._state["skill_digests"][skill_id] = before
        self._state["skills"].append(
            {
                "skill_id": skill_id,
                "name": current.name,
                "state": "installed",
                "package_ref": candidate["candidate_id"],
                "project_ids": [],
                "trust_status": "user_installed",
            }
        )

    async def close(self) -> None:
        if self._owns_host:
            await self.host.close()
            self.registry.close()

    def authorize_source(self, context: RpcContext, source: SourceRef) -> bool:
        if source.source_type == "memory_version":
            if (
                source.scope != context.scope
                or source.permission_epoch != context.permission_epoch
                or source.availability != "available"
                or source.revision < 1
            ):
                return False
            try:
                version = self.ledger.get_version(
                    context.dataset_id, source.source_id, source.revision
                )
                head = self.ledger.get_head(context.dataset_id, source.source_id)
                return (
                    version.scope == context.scope
                    and version.ref.content_digest == source.content_digest
                    and head.state == "published"
                    and head.published_version == version.ref
                )
            except Exception:
                return False
        with self.store._connect() as c:
            row = c.execute(
                "SELECT * FROM b23_sources WHERE source_id=?", (source.source_id,)
            ).fetchone()
        if row is None or row["dataset_id"] != context.dataset_id:
            return False
        if (
            source != SourceRef.model_validate_json(row["ref"])
            or source.scope != context.scope
            or source.availability != "available"
        ):
            return False
        # Real canonical user history is the authority, not plugin-authored provenance.
        try:
            item = self.store.get_item(source.source_id)
        except Exception:
            return False
        if not isinstance(item.payload, UserMessagePayload):
            return False
        return (
            digest(item.payload.text) == source.content_digest
            and source.permission_epoch == context.permission_epoch
        )

    def read_source(self, request: HostReadRequest) -> HostReadResult:
        if not self.authorize_source(request.context, request.source):
            raise PermissionError("source is not authorized")
        if request.source.source_type == "memory_version":
            text = self.ledger.get_version(
                request.context.dataset_id, request.source.source_id, request.source.revision
            ).content
        else:
            with self.store._connect() as c:
                row = c.execute(
                    "SELECT body FROM b23_sources WHERE source_id=?", (request.source.source_id,)
                ).fetchone()
            text = row["body"]
        if len(text.encode()) > request.max_bytes:
            raise ValueError("source exceeds plugin read budget")
        return HostReadResult(source=request.source, text=text, truncated=False)

    def authorize_ref(self, context: RpcContext, ref: MemoryVersionRef) -> bool:
        if ref.dataset_id != context.dataset_id:
            return False
        try:
            version = self.ledger.get_version(ref.dataset_id, ref.record_id, ref.version)
            head = self.ledger.get_head(ref.dataset_id, ref.record_id)
            return (
                version.ref == ref
                and version.scope == context.scope
                and head.state not in ("deleted", "revoked")
                and (
                    (head.state == "published" and head.published_version == ref)
                    or self._pending_refs.get(context.request_id) == ref
                )
            )
        except Exception:
            return False

    def authorize_head(self, context: RpcContext, head: MemoryHead) -> bool:
        try:
            return (
                head.dataset_id == context.dataset_id
                and self.ledger.get_head(head.dataset_id, head.record_id) == head
            )
        except Exception:
            return False

    def authorize_proposal(self, context: RpcContext, proposal: MemoryProposal) -> bool:
        try:
            version = self.ledger.get_version(
                context.dataset_id,
                proposal.proposed_version.record_id,
                proposal.proposed_version.version,
            )
            installation = self.registry.get_installation(context.installation_id)
            return (
                proposal.owner == installation.owner == version.owner
                and proposal.source_refs == version.sources
                and self.authorize_head(context, proposal.base_head)
                and self.authorize_ref(context, proposal.proposed_version)
                and all(self.authorize_source(context, source) for source in proposal.source_refs)
            )
        except Exception:
            return False

    def search(self, request: RecallRequest) -> CandidateBatch:
        versions = self.ledger.query(
            request.context.dataset_id,
            request.query,
            scope=request.context.scope,
            limit=request.max_candidates,
        )
        return CandidateBatch(
            request_id=request.context.request_id,
            candidates=tuple(CandidateReference(ref=v.ref, score=1.0) for v in versions),
        )

    def _context(self, lease: Any) -> RpcContext:
        return RpcContext(
            sdk_version="operant-memory-sdk.v1",
            request_id="rpc_" + uuid4().hex,
            installation_id=lease.installation_id,
            dataset_id=lease.dataset_id,
            scope=lease.scope,
            deadline=utc_now() + timedelta(seconds=30),
            cancel_token="cancel_" + uuid4().hex,
            idempotency_key="request_" + uuid4().hex,
            request_digest=digest(uuid4().hex),
            binding_epoch=lease.binding_epoch,
            permission_epoch=lease.permission_epoch,
            lease_fencing=lease.lease_fencing,
        )

    async def _memory(self, cmd: ManagementCommand) -> tuple[str, str, Any, Any]:
        p = self._project(cmd.project_id)
        i = self._installation(p)
        await self.host.start(
            i.installation_id, mode=self._state["modes"].get(i.installation_id, "isolated")
        )
        lease = self.host.start_run(
            i.binding_id, run_id="management_" + uuid4().hex, scope=self._scope(p)
        )
        context = self._context(lease)
        try:
            if cmd.action == "memory_search":
                request = RecallRequest(
                    context=context,
                    query=self._required(cmd.query, "query"),
                    explicit_refs=(),
                    knowledge_cutoff="0",
                    max_candidates=100,
                    token_budget=2000,
                )
                result = await self.host.invoke(lease, "recall", request)
                self.registry.assert_lease(lease, context)
                refs = {c.ref.record_id for c in CandidateBatch.model_validate(result).candidates}
                return (
                    "completed",
                    "插件查询完成",
                    None,
                    [r for r in self._records(p) if r.record_id in refs],
                )
            if cmd.action == "memory_deactivate":
                head = self.ledger.get_head(
                    i.dataset_id, self._required(cmd.record_id, "record_id")
                )
                if not self.authorize_ref(
                    context, self.ledger.get_version(i.dataset_id, head.record_id).ref
                ):
                    raise PermissionError("record scope mismatch")
                self.registry.assert_lease(lease, context)
                head = self.ledger.deactivate(
                    i.dataset_id,
                    head.record_id,
                    expected_head_revision=self._required(
                        cmd.expected_revision, "expected_revision"
                    ),
                )
            elif cmd.action == "memory_confirm":
                proposal = self.ledger.get_proposal(self._required(cmd.proposal_id, "proposal_id"))
                self._pending_refs[context.request_id] = proposal.proposed_version
                if not self.authorize_proposal(context, proposal):
                    raise PermissionError("proposal scope or provenance is not current")
                self.registry.assert_lease(lease, context)
                head = self.ledger.confirm_proposal(
                    proposal,
                    dataset_id=i.dataset_id,
                    expected_head_revision=self._required(
                        cmd.expected_revision, "expected_revision"
                    ),
                )
            else:
                content = self._required(cmd.content, "content").strip()
                if not content:
                    raise ValueError("content is empty")
                if cmd.action == "memory_propose":
                    record_id = self._required(cmd.record_id, "record_id")
                    previous = self.ledger.get_version(i.dataset_id, record_id)
                    if previous.scope != context.scope:
                        raise PermissionError("record scope mismatch")
                    base = self.ledger.get_head(i.dataset_id, record_id)
                    if base.revision != self._required(cmd.expected_revision, "expected_revision"):
                        raise PluginError("revision_conflict", "memory head changed")
                    version_number = (
                        max(
                            v.ref.version
                            for v in self.ledger.list_versions(i.dataset_id, record_id)
                        )
                        + 1
                    )
                else:
                    record_id = "memory_" + uuid4().hex
                    version_number = 1
                    base = None
                workspace = self.store.get_workspace_initialization_by_id(p["workspace_id"])
                thread = self.service.create_thread(
                    ConversationThread(workspace_ref=workspace.workspace_ref)
                )
                turn = self.service.create_turn(Turn(thread_id=thread.id))
                item = self.service.append_item(
                    Item(
                        thread_id=thread.id,
                        turn_id=turn.id,
                        payload=UserMessagePayload(
                            text=content, author_ref="user:memory-management"
                        ),
                    )
                )
                if not isinstance(item.payload, UserMessagePayload):
                    raise ValueError("canonical source type mismatch")
                content = item.payload.text
                source = SourceRef(
                    source_type="item",
                    source_id=item.id,
                    revision=1,
                    content_digest=digest(content),
                    scope=context.scope,
                    permission_epoch=context.permission_epoch,
                    availability="available",
                )
                version = MemoryVersion(
                    ref=MemoryVersionRef(
                        dataset_id=i.dataset_id,
                        record_id=record_id,
                        version=version_number,
                        content_digest=digest(content),
                    ),
                    owner=i.owner,
                    kind="project",
                    content_type="fact",
                    scope=context.scope,
                    role_ids=(),
                    agent_ids=(),
                    content=content,
                    sources=(source,),
                    evidence="user_asserted",
                    sensitivity="internal",
                    retention_policy_id="default",
                    conditions=MemoryConditions(
                        commit_ref=None,
                        tree_digest=None,
                        file_fingerprints={},
                        environment_digest=None,
                        tool_versions={},
                        verified_at=None,
                        valid_from=utc_now(),
                        valid_until=None,
                    ),
                    recorded_at=utc_now(),
                )
                self.ledger.save_version(
                    version,
                    expected_head_revision=base.revision if base else 0,
                    permission_epoch=context.permission_epoch,
                )
                base = self.ledger.get_head(i.dataset_id, record_id)
                envelope = {
                    "content": content,
                    "record_id": record_id,
                    "version": version_number,
                    "operation": "modify" if cmd.action == "memory_propose" else "create",
                    "proposal": {"base_head": base.model_dump(mode="json")},
                    "confirmed": cmd.confirmed,
                    "config": self._state["plugin_config"].get(i.installation_id, {}),
                }
                with self.store._connect() as c:
                    c.execute(
                        "INSERT INTO b23_sources VALUES(?,?,?,?,?)",
                        (
                            item.id,
                            i.dataset_id,
                            context.scope.model_dump_json(),
                            source.model_dump_json(),
                            json.dumps(envelope),
                        ),
                    )
                self._pending_refs[context.request_id] = version.ref
                result = await self.host.invoke(
                    lease,
                    "extract",
                    SourceBatch(
                        context=context, sources=(source,), source_watermark=str(item.cursor)
                    ),
                )
                self.registry.assert_lease(lease, context)
                result_batch = ProposalBatch.model_validate(result)
                if len(result_batch.proposals) != 1:
                    raise PluginError(
                        "proposal_rejected", "plugin did not accept this memory format"
                    )
                proposal = result_batch.proposals[0]
                if proposal.proposed_version != version.ref or proposal.base_head != base:
                    raise PluginError(
                        "proposal_rejected",
                        "plugin proposal does not match the current user source and head",
                    )
                proposal = self.ledger.propose(
                    proposal=proposal, expected_head_revision=base.revision
                )
                if not cmd.confirmed or cmd.action == "memory_propose":
                    return "completed", "已保存候选；尚未替代正式知识", None, None
                head = self.ledger.confirm_proposal(
                    proposal, dataset_id=i.dataset_id, expected_head_revision=base.revision
                )
            self.registry.assert_lease(lease, context)
            await self.host.invoke(
                lease,
                "on_index_event",
                IndexEvent(context=context, event_id="index_" + uuid4().hex, head=head),
            )
            return "completed", "记忆状态已保存", None, None
        finally:
            self._pending_refs.pop(context.request_id, None)
            self.registry.release_run(lease.lease_id)

    def compat_query(
        self, query: str, workspace: str | None, snapshot: Any, include_candidates: bool, limit: int
    ) -> list[Memory]:
        from operant.domain.memory import MemoryKind

        for p in self._state["projects"]:
            initialization = self.store.get_workspace_initialization_by_id(p["workspace_id"])
            if initialization.workspace_ref != workspace:
                continue
            try:
                i = self._installation(p)
            except PluginError:
                return []
            self.service._authorize_memory(
                snapshot, MemoryKind.PROJECT, operation="read", project_scope=workspace
            )
            versions = self.ledger.query(
                i.dataset_id,
                query or None,
                scope=self._scope(p),
                include_candidates=include_candidates,
                limit=limit,
            )
            return [self._compat(v, workspace) for v in versions]
        return []

    def compat_get(
        self, record_id: str, workspace: str | None, snapshot: Any, version: int | None
    ) -> Memory:
        for p in self._state["projects"]:
            initialization = self.store.get_workspace_initialization_by_id(p["workspace_id"])
            if initialization.workspace_ref != workspace:
                continue
            i = self._installation(p)
            head = self.ledger.get_head(i.dataset_id, record_id)
            if head.state != "published" or not head.published_version:
                raise PermissionError("memory has no current published version")
            if version is not None and version != head.published_version.version:
                raise PermissionError("historical memory version is not currently active")
            value = self.ledger.get_version(i.dataset_id, record_id, head.published_version.version)
            if value.scope != self._scope(p):
                raise PermissionError("memory scope mismatch")
            result = self._compat(value, workspace)
            self.service._authorize_memory(
                snapshot, result.kind, operation="read", memory=result, project_scope=workspace
            )
            return result
        raise PermissionError("no active memory binding for workspace")

    def _compat(self, version: Any, workspace: str | None) -> Memory:
        from operant.domain.memory import Memory

        head = self.ledger.get_head(version.ref.dataset_id, version.ref.record_id)
        return Memory(
            id=version.ref.record_id,
            version=version.ref.version,
            kind=version.kind,
            content=version.content,
            project_scope=workspace,
            confidence=0.0,
            status=MemoryStatus.ACTIVE if head.state == "published" else MemoryStatus.CANDIDATE,
        )

    def _artifacts(self) -> list[dict[str, Any]]:
        result = []
        for artifact in self.service.list_artifacts(limit=100):
            retention = self.service.get_artifact_retention_state(artifact.id)
            result.append(
                {
                    "artifact_id": artifact.id,
                    "content_hash": artifact.content_hash,
                    "size_bytes": artifact.size_bytes,
                    "lifecycle": retention.lifecycle.value,
                    "pinned": retention.pinned,
                    "blocked": bool(self.store.artifact_deletion_blockers(artifact.id)),
                }
            )
        return result

    def _artifact_command(self, cmd: ManagementCommand) -> None:
        from operant.domain.threads import ArtifactAccessLevel

        artifact_id = self._required(cmd.artifact_id, "artifact_id")
        operations = {
            "artifact_pin": "retention_pin",
            "artifact_archive": "retention_archive",
            "artifact_schedule": "retention_schedule",
            "artifact_trash": "retention_trash",
            "artifact_restore": "retention_restore",
        }
        # This method is called only after the management Action Gateway. It
        # issues no reusable public token or file-content capability. Sensitive
        # and restricted artifacts still require their separately trusted flow.
        token = self.service.issue_artifact_capability(
            artifact_id,
            operation=operations[cmd.action],
            access_level=ArtifactAccessLevel.NORMAL,
            ttl_seconds=30,
        )
        if cmd.action == "artifact_pin":
            self.service.set_artifact_pin(
                artifact_id, pinned=self._required(cmd.enabled, "enabled"), capability=token
            )
        elif cmd.action == "artifact_archive":
            self.service.archive_artifact(artifact_id, capability=token)
        elif cmd.action == "artifact_schedule":
            self.service.schedule_artifact_deletion(artifact_id, capability=token)
        elif cmd.action == "artifact_trash":
            self.service.trash_artifact(artifact_id, capability=token)
        elif cmd.action == "artifact_restore":
            self.service.restore_artifact(artifact_id, capability=token)

    def begin_skill_run(self, workspace: str, run_id: str) -> str:
        from operant.plugins.protocol import compute_package_digest
        from operant.skills import SkillDiscovery

        project = next(
            (
                p
                for p in self._state["projects"]
                if not p["archived"]
                and self.store.get_workspace_initialization_by_id(p["workspace_id"]).workspace_ref
                == workspace
            ),
            None,
        )
        if project is None:
            return ""
        selected = [
            s
            for s in self._state["skills"]
            if s["state"] == "installed" and project["project_id"] in s.get("project_ids", [])
        ]
        content = []
        snapshot_ids = set()
        for skill in selected:
            if compute_package_digest(self.root / "skills" / skill["skill_id"]) != self._state[
                "skill_digests"
            ].get(skill["skill_id"]):
                raise PluginError("package_unavailable", "installed Skill contents changed")
            candidates = SkillDiscovery((self.root / "skills",)).discover().candidates
            candidate = next(
                (c for c in candidates if c.relative_directory == skill["skill_id"]), None
            )
            if candidate is None:
                raise PluginError("package_unavailable", "installed Skill snapshot unavailable")
            content.append(f"技能 {candidate.name} ({candidate.manifest_sha256})\n{candidate.body}")
            snapshot_ids.add(skill["skill_id"])
        self._active_skill_runs[run_id] = snapshot_ids
        return "\n\n".join(content)

    def release_skill_run(self, run_id: str) -> None:
        self._active_skill_runs.pop(run_id, None)
