"""Pinned experience inputs with a current authorization check before every send."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from operant.contracts.b2_1 import MemoryVersionRef
from operant.contracts.b2_6_skills import SkillRunSnapshot
from operant.domain.memory import MemoryKind
from operant.domain.models import RoleSnapshot
from operant.memory_plugins.experience_skills import ExperienceSkillService
from operant.memory_plugins.governance import GovernanceService
from operant.memory_plugins.retrieval import memory_conditions_match
from operant.memory_plugins.sharing import SharingService
from operant.memory_plugins.worktree_knowledge import workspace_facts


@dataclass
class ExperienceRun:
    manager: Any
    project: dict[str, Any]
    session_id: str
    agent_id: str
    snapshot: RoleSnapshot
    workspace: str
    pinned: dict[str, Any]
    enabled: bool

    def _check(self, value: dict[str, Any], *, originating_agent_id: str) -> None:
        m = self.manager
        if not self.enabled:
            raise PermissionError("experience history is unavailable while memory is disabled")
        m._installation(self.project)
        m.service._authorize_memory(
            self.snapshot, MemoryKind.PROJECT, operation="read", project_scope=self.workspace
        )
        if value["project_id"] != self.project["project_id"]:
            raise PermissionError("experience history belongs to another project")
        skills = ExperienceSkillService(m)
        for raw in value.get("skills", []):
            skill = SkillRunSnapshot.model_validate(raw)
            skills.assert_snapshot_usable(
                self.project["project_id"],
                skill,
                session_id=self.session_id,
                role_id=self.snapshot.role_id,
                model_profile_id=self.snapshot.model_profile_id,
                agent_id=originating_agent_id,
            )
            version = skills._snapshot_version(skill)
            if version.agent_ids and self.agent_id not in version.agent_ids:
                raise PermissionError("experience history is restricted to another agent")
        for entry in value.get("shared", []):
            self._shared_version(entry)

    def _shared_version(self, entry: dict[str, Any]) -> Any:
        m = self.manager
        sharing = SharingService(m)
        ref = MemoryVersionRef.model_validate(entry["ref"])
        grant = sharing._grant_by_id(entry["grant_id"])
        source = m._project(grant.project_id)
        installation = m._installation(source)
        binding = m.registry.get_binding(installation.binding_id)
        if installation.dataset_id != ref.dataset_id:
            raise PermissionError("shared source binding changed")
        # The grant is additionally bound to this immutable local role/model.
        if grant.subject_id not in {self.snapshot.role_id, self.snapshot.model_profile_id}:
            raise PermissionError("sharing grant does not authorize this role/model")
        decision = sharing.authorize(
            ref,
            target_scope=m._scope(self.project),
            subject_id=grant.subject_id,
            permission_epoch=binding.permission_epoch,
            grant_id=grant.grant_id,
        )
        if not decision.allowed:
            raise PermissionError("shared memory authorization was revoked or expired")
        head = m.ledger.get_head(ref.dataset_id, ref.record_id)
        if head.state != "published" or head.published_version != ref:
            raise PermissionError("shared memory is no longer published")
        original = m.ledger.get_version(ref.dataset_id, ref.record_id, ref.version)
        current = m.ledger.get_version(
            ref.dataset_id, ref.record_id, head.published_version.version
        )
        for version in (original, current):
            if version.sensitivity not in {"public", "internal"}:
                raise PermissionError("shared memory sensitivity exceeds model clearance")
            if version.role_ids and not {
                self.snapshot.role_id,
                self.snapshot.role_name,
            }.intersection(version.role_ids):
                raise PermissionError("shared memory is restricted to another role")
            if version.agent_ids and self.agent_id not in version.agent_ids:
                raise PermissionError("shared memory is restricted to another agent")
            facts = (
                workspace_facts(m, source)
                if version.conditions.commit_ref or version.conditions.tree_digest
                else None
            )
            if not memory_conditions_match(version, facts):
                raise PermissionError("shared memory conditions no longer apply")
            if not GovernanceService(m).version_dependencies_valid(version.ref):
                raise PermissionError("shared memory source was revoked")
        if original.ref != ref:
            raise PermissionError("shared version digest changed")
        return original

    def guard(self) -> None:
        with self.manager.store._connect() as c:
            prior = c.execute(
                "SELECT agent_id,body FROM b26_run_context WHERE session_id=?", (self.session_id,)
            ).fetchall()
        # Previously sent content remains historical, but cannot be sent again
        # after revocation even if the next Run selects a different Skill set.
        for row in prior:
            # History belongs to its original Agent. A new Agent receives a new
            # snapshot and may reuse same-Session history only after both the
            # original identity and its own current access are checked.
            self._check(json.loads(row["body"]), originating_agent_id=row["agent_id"])
        if self.pinned["skills"] or self.pinned["shared"]:
            self._check(self.pinned, originating_agent_id=self.agent_id)
            with self.manager.store._connect() as c:
                c.execute(
                    "INSERT OR IGNORE INTO b26_run_context VALUES(?,?,?,?)",
                    (
                        self.session_id,
                        self.agent_id,
                        self.project["project_id"],
                        json.dumps(self.pinned),
                    ),
                )

    def content(self) -> str:
        chunks = [
            f"经验技能 {s['skill_id']} v{s['skill_version']}\n{s['content']}"
            for s in self.pinned["skills"]
        ]
        for entry in self.pinned["shared"]:
            version = self._shared_version(entry)
            chunks.append(
                f"授权共享知识 {version.ref.record_id} v{version.ref.version}\n{version.content}"
            )
        return "\n\n".join(chunks)


def begin_experience_run(
    manager: Any,
    *,
    workspace: str,
    session_id: str,
    agent_id: str,
    snapshot: RoleSnapshot,
    enabled: bool,
) -> ExperienceRun | None:
    project = next(
        (
            p
            for p in manager._state["projects"]
            if not p["archived"]
            and manager.store.get_workspace_initialization_by_id(p["workspace_id"]).workspace_ref
            == workspace
        ),
        None,
    )
    with manager.store._connect() as c:
        has_history = c.execute(
            "SELECT 1 FROM b26_run_context WHERE session_id=? LIMIT 1", (session_id,)
        ).fetchone()
    if project is None:
        if has_history:
            raise PermissionError("experience project is unavailable; start a clean session")
        return None
    pinned: dict[str, Any] = {"project_id": project["project_id"], "skills": [], "shared": []}
    run = ExperienceRun(
        manager, project, session_id, agent_id, snapshot, workspace, pinned, enabled
    )
    try:
        if not enabled:
            raise PermissionError("memory disabled")
        manager._installation(project)
        manager.service._authorize_memory(
            snapshot, MemoryKind.PROJECT, operation="read", project_scope=workspace
        )
    except Exception:
        if has_history:
            run.guard()
        return None
    remaining = 24000  # Conservative UTF-8 byte bound; Composer also accounts the full request.
    for skill in ExperienceSkillService(manager).snapshot_for_run(
        project["project_id"],
        session_id=session_id,
        role_id=snapshot.role_id,
        agent_id=agent_id,
        model_profile_id=snapshot.model_profile_id,
    ):
        size = len(skill.content.encode())
        if size <= remaining:
            pinned["skills"].append(skill.model_dump(mode="json"))
            remaining -= size
    sharing = SharingService(manager)
    with manager.store._connect() as c:
        rows = c.execute(
            "SELECT grant_id FROM b26_sharing_grants WHERE state='active' "
            "ORDER BY grant_id LIMIT 500"
        ).fetchall()
    seen = set()
    for row in rows:
        grant = sharing._grant_by_id(row["grant_id"])
        if grant.target_scope != manager._scope(project) or grant.subject_id not in {
            snapshot.role_id,
            snapshot.model_profile_id,
        }:
            continue
        for ref in grant.memory_refs:
            entry = {"grant_id": grant.grant_id, "ref": ref.model_dump(mode="json")}
            try:
                version = run._shared_version(entry)
            except (ValueError, RuntimeError, LookupError, PermissionError):
                continue
            size = len(version.content.encode())
            if ref not in seen and len(pinned["shared"]) < 8 and size <= remaining:
                pinned["shared"].append(entry)
                seen.add(ref)
                remaining -= size
    run.guard()
    return run
