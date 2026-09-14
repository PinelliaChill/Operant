"""One installed-engine recall chain for Session, Workflow and Graph Agent runs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from operant.contracts.b2_1 import (
    CandidateBatch,
    MemoryEnabled,
    MemoryPack,
    MemoryVersion,
    MemoryVersionRef,
    RecallRequest,
)
from operant.contracts.b2_4 import MemoryInspection, MemorySelection
from operant.domain.context import ContextReferenceType, ReferenceRequest
from operant.domain.memory import MemoryKind
from operant.domain.models import RoleSnapshot, utc_now
from operant.memory_plugins.retrieval import (
    MAXIMUM_RECALL_SENSITIVITY,
    memory_sensitivity_visible,
)

if TYPE_CHECKING:
    from operant.memory_plugins.manager import MemoryManager

POLICY_REVISION = 1
RESIDENT_CANDIDATE_LIMIT = 40
RESIDENT_SELECTION_LIMIT = 8


def publication_cutoff(manager: MemoryManager) -> str:
    with manager.store._connect() as c:
        return str(
            c.execute(
                "SELECT value FROM memory_ledger_meta WHERE key='publication_cursor'"
            ).fetchone()[0]
        )


def frozen_versions(
    manager: MemoryManager,
    dataset: str,
    cutoff: str,
    *,
    record_id: str | None = None,
    resident_only: bool = False,
    limit: int = 10000,
) -> list[MemoryVersion]:
    with manager.store._connect() as c:
        rows = c.execute(
            """SELECT v.body FROM b24_publications p
          JOIN memory_ledger_versions v ON v.dataset_id=p.dataset_id AND v.record_id=p.record_id
          AND v.version=p.version
          JOIN memory_ledger_heads h ON h.dataset_id=p.dataset_id AND h.record_id=p.record_id
          WHERE p.dataset_id=? AND (? IS NULL OR p.record_id=?) AND p.cursor=(SELECT
          MAX(q.cursor) FROM b24_publications q WHERE q.dataset_id=p.dataset_id AND
          q.record_id=p.record_id AND q.cursor<=?)
          AND p.state='published' AND h.state='published'
          AND NOT EXISTS(SELECT 1 FROM b24_publications revoked WHERE
          revoked.dataset_id=p.dataset_id AND revoked.record_id=p.record_id AND
          revoked.cursor>p.cursor AND revoked.state IN ('inactive','revoked','deleted'))
          AND (?=0 OR (json_extract(v.body,'$.content_type')='preference'
          AND json_extract(v.body,'$.evidence')='user_asserted'))
          ORDER BY p.record_id LIMIT ?""",
            (dataset, record_id, record_id, int(cutoff), int(resident_only), limit),
        ).fetchall()
    return [MemoryVersion.model_validate_json(r["body"]) for r in rows]


def history_publication_active(manager: MemoryManager, ref: MemoryVersionRef) -> bool:
    """A revoke/re-publish cannot rehabilitate previously derived Graph output."""
    head = manager.ledger.get_head(ref.dataset_id, ref.record_id)
    with manager.store._connect() as c:
        revoked = c.execute(
            """SELECT 1 FROM b24_publications WHERE dataset_id=? AND record_id=?
            AND state IN ('inactive','revoked','deleted') AND cursor >= (
            SELECT MIN(cursor) FROM b24_publications WHERE dataset_id=?
            AND record_id=? AND version=? AND state='published') LIMIT 1""",
            (ref.dataset_id, ref.record_id, ref.dataset_id, ref.record_id, ref.version),
        ).fetchone()
    return head.state == "published" and revoked is None


def load_manifest(manager: MemoryManager, run_id: str) -> dict[str, Any] | None:
    with manager.store._connect() as c:
        row = c.execute("SELECT body FROM b24_manifests WHERE run_id=?", (run_id,)).fetchone()
    return json.loads(row["body"]) if row else None


def save_manifest(
    manager: MemoryManager, run_id: str, session_id: str, dataset: str, state: dict[str, Any]
) -> None:
    # Parallel Graph members append evidence without replacing another member's
    # history or rolling back a newer explicit refresh.
    with manager.store._connect() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT body FROM b24_manifests WHERE run_id=?", (run_id,)).fetchone()
        previous = json.loads(row["body"]) if row else {}
        merged = dict(
            state if state.get("revision", 0) >= previous.get("revision", 0) else previous
        )

        def union(left: list[Any], right: list[Any]) -> list[Any]:
            return list({json.dumps(ref, sort_keys=True): ref for ref in [*left, *right]}.values())

        merged["used_refs"] = union(previous.get("used_refs", []), state.get("used_refs", []))
        members = dict(previous.get("used_by_session", {}))
        for member, refs in state.get("used_by_session", {}).items():
            members[member] = union(members.get(member, []), refs)
        merged["used_by_session"] = members
        c.execute(
            "INSERT INTO b24_manifests VALUES(?,?,?,?) "
            "ON CONFLICT(run_id) DO UPDATE SET body=excluded.body",
            (run_id, session_id, dataset, json.dumps(merged)),
        )
    state.clear()
    state.update(merged)


@dataclass
class MemoryRun:
    manager: MemoryManager
    project: dict[str, Any]
    lease: Any
    context: Any
    run_id: str
    session_id: str
    agent_id: str
    snapshot: RoleSnapshot
    state: dict[str, Any]
    candidates: list[MemoryVersion]
    explicit_ids: set[str]
    resident_ids: set[str] = field(default_factory=set)
    sent: bool = False

    def validate(self, version: MemoryVersion, *, historical: bool = False) -> bool:
        try:
            self.manager._installation(self.project)
            binding = self.manager.registry.get_binding(self.lease.binding_id)
            current_lease = self.manager.registry.get_run(self.lease.lease_id)
            if (
                current_lease != self.lease
                or current_lease.status != "active"
                or current_lease.expires_at <= utc_now()
                or not binding.enabled
                or not binding.global_enabled
                or binding.binding_epoch != self.lease.binding_epoch
                or binding.permission_epoch != self.lease.permission_epoch
            ):
                return False
            if historical:
                if not history_publication_active(self.manager, version.ref):
                    return False
            else:
                current = {
                    v.ref
                    for v in frozen_versions(
                        self.manager,
                        self.lease.dataset_id,
                        self.state["cutoff"],
                        record_id=version.ref.record_id,
                    )
                }
                if version.ref not in current:
                    return False
            # Preserve frozen content, but enforce any tightened access on the
            # current published head as well as the originally selected version.
            current_version = self.manager.ledger.get_version(
                version.ref.dataset_id, version.ref.record_id
            )
            for access_version in (version, current_version):
                if (
                    access_version.ref.dataset_id != self.lease.dataset_id
                    or access_version.scope != self.context.scope
                    or not memory_sensitivity_visible(access_version, MAXIMUM_RECALL_SENSITIVITY)
                ):
                    return False
                if (
                    access_version.role_ids
                    and self.snapshot.role_id not in access_version.role_ids
                    and self.snapshot.role_name not in access_version.role_ids
                ):
                    return False
                if access_version.agent_ids and self.agent_id not in access_version.agent_ids:
                    return False
            from operant.memory_plugins.retrieval import memory_conditions_match

            if not memory_conditions_match(version, None):
                return False
            return all(self.manager.authorize_source(self.context, s) for s in version.sources)
        except Exception:
            return False

    def inspection(self, budget: int, model_id: str) -> MemoryInspection:
        # Every send repeats live authorization. Derived messages cannot be safely
        # stripped after a revocation, so fail closed instead of reusing them.
        self.manager.registry.assert_lease(self.lease, self.context)
        # Revalidate evidence already present in this Session's derived history,
        # including entries omitted by a later Pack or explicit exclusion.
        state = load_manifest(self.manager, self.run_id) or self.state
        for ref in state.get("used_refs", []):
            if not history_publication_active(self.manager, MemoryVersionRef.model_validate(ref)):
                raise PermissionError(
                    "prior memory-derived history is revoked; start a clean Context Baseline"
                )
        for ref in state.get("used_by_session", {}).get(
            self.session_id,
            state.get("used_refs", []) if "used_by_session" not in state else [],
        ):
            old = MemoryVersionRef.model_validate(ref)
            version = self.manager.ledger.get_version(old.dataset_id, old.record_id, old.version)
            if not self.validate(version, historical=True):
                raise PermissionError(
                    "prior memory-derived history is revoked; start a clean Context Baseline"
                )
        budget = min(
            budget,
            self.manager.registry.get_binding(self.lease.binding_id).config.recall_token_budget,
        )
        selected = []
        used = 0
        for version in self.candidates:
            if not self.validate(version):
                if self.sent or version.ref.record_id in self.explicit_ids:
                    raise PermissionError(
                        "memory revoked or permission changed; start a clean Context Baseline"
                    )
                continue
            if version.ref.record_id in self.state.get("excluded", []):
                if version.ref.record_id in self.explicit_ids:
                    raise ValueError(
                        "explicit Memory is excluded from this run; "
                        "remove the reference or start a new run"
                    )
                continue
            rendered = json.dumps(
                version.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")
            )
            count = len(rendered.encode("utf-8")) + 32
            if used + count > budget:
                if version.ref.record_id in self.explicit_ids:
                    raise ValueError("explicit Memory exceeds shared Context budget")
                continue
            used += count
            selected.append(
                MemorySelection(
                    memory=version,
                    reason="explicit"
                    if version.ref.record_id in self.explicit_ids
                    else "resident_preference"
                    if version.ref.record_id in self.resident_ids
                    else "task_relevance",
                    token_count=count,
                )
            )
        i = self.manager.registry.get_installation(self.lease.installation_id)
        binding = MemoryEnabled(
            state="enabled",
            binding_id=self.lease.binding_id,
            binding_epoch=self.lease.binding_epoch,
            installation_id=i.installation_id,
            dataset_id=i.dataset_id,
            config_digest=hashlib.sha256(
                self.manager.registry.get_binding(self.lease.binding_id)
                .config.model_dump_json()
                .encode()
            ).hexdigest(),
            package_digest=i.manifest.package_digest,
            certification_id=i.certification_id or "isolated",
            index_generation="b24-trigram-v1",
            global_enabled=True,
        )
        body = {
            "run_id": self.run_id,
            "cutoff": self.state["cutoff"],
            "revision": self.state["revision"],
            "refs": [s.memory.ref.model_dump() for s in selected],
            "model_id": model_id,
        }
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        pack = MemoryPack(
            manifest_id="manifest_" + digest,
            manifest_digest=digest,
            scope=self.context.scope,
            run_id=self.run_id,
            agent_instance_id=self.agent_id,
            binding=binding,
            knowledge_cutoff=self.state["cutoff"],
            policy_revision=POLICY_REVISION,
            permission_epoch=self.lease.permission_epoch,
            selected=tuple(s.memory.ref for s in selected),
            total_token_budget=budget,
            token_count=used,
            selection="automatic_and_explicit",
        )
        self.sent = True
        combined = {
            json.dumps(ref, sort_keys=True, separators=(",", ":")): ref
            for ref in self.state.get("used_refs", [])
        }
        combined.update(
            {
                json.dumps(
                    s.memory.ref.model_dump(), sort_keys=True, separators=(",", ":")
                ): s.memory.ref.model_dump()
                for s in selected
            }
        )
        self.state["used_refs"] = list(combined.values())
        members = self.state.setdefault("used_by_session", {})
        own = {json.dumps(ref, sort_keys=True): ref for ref in members.get(self.session_id, [])}
        own.update(
            {
                json.dumps(s.memory.ref.model_dump(), sort_keys=True): s.memory.ref.model_dump()
                for s in selected
            }
        )
        members[self.session_id] = list(own.values())
        save_manifest(self.manager, self.run_id, self.session_id, i.dataset_id, self.state)
        return MemoryInspection(
            pack=pack,
            entries=selected,
            counting_method="utf8_bytes_upper_bound_with_wrapper",
            refresh="explicit_refresh" if self.state["revision"] > 1 else "frozen",
            excluded_record_ids=self.state.get("excluded", []),
        )


async def begin_memory_run(
    manager: MemoryManager,
    *,
    session_id: str,
    agent_id: str,
    run_id: str,
    workspace: str,
    snapshot: RoleSnapshot,
    query: str,
    references: tuple[ReferenceRequest, ...],
) -> MemoryRun | None:
    project = next(
        (
            p
            for p in manager._state["projects"]
            if manager.store.get_workspace_initialization_by_id(p["workspace_id"]).workspace_ref
            == workspace
        ),
        None,
    )
    if project is None:
        old_state = load_manifest(manager, run_id)
        if old_state and old_state.get("used_refs"):
            raise PermissionError("memory project unavailable; start a clean Context Baseline")
        return None
    explicit = {r.target_id for r in references if r.ref_type is ContextReferenceType.MEMORY}
    state = load_manifest(manager, run_id)
    try:
        installation = manager._installation(project)
        manager.service._authorize_memory(
            snapshot, MemoryKind.PROJECT, operation="read", project_scope=workspace
        )
    except Exception:
        if explicit or (state and state.get("used_refs")):
            raise PermissionError(
                "memory disabled or inaccessible; start a clean Context Baseline"
            ) from None
        return None
    if state is None:
        state = {
            "cutoff": publication_cutoff(manager),
            "revision": 1,
            "excluded": [],
            "used_refs": [],
            "used_by_session": {},
            "binding_id": installation.binding_id,
            "binding_epoch": installation.binding_epoch,
        }
    elif (
        state["binding_id"] != installation.binding_id
        or state["binding_epoch"] != installation.binding_epoch
    ):
        raise PermissionError("memory binding changed; explicitly refresh or start a clean session")
    await manager.host.start(
        installation.installation_id,
        mode=manager._state["modes"].get(installation.installation_id, "isolated"),
    )
    lease = manager.host.start_run(
        installation.binding_id,
        run_id=agent_id,
        scope=manager._scope(project),
        ttl_seconds=snapshot.budget.timeout_seconds + 30,
    )
    context = manager._context(lease)
    run = MemoryRun(
        manager,
        project,
        lease,
        context,
        run_id,
        session_id,
        agent_id,
        snapshot,
        state,
        [],
        explicit,
    )
    manager._recall_runs[context.request_id] = run
    try:

        def resolve(record_id: str) -> MemoryVersion | None:
            values = frozen_versions(
                manager, installation.dataset_id, state["cutoff"], record_id=record_id
            )
            return next((v for v in values if v.scope == context.scope), None)

        for ref in state.get("used_refs", []):
            if not history_publication_active(manager, MemoryVersionRef.model_validate(ref)):
                raise PermissionError(
                    "prior memory-derived history is revoked; start a clean Context Baseline"
                )
        for ref in state.get("used_by_session", {}).get(
            session_id, state.get("used_refs", []) if "used_by_session" not in state else []
        ):
            old = MemoryVersionRef.model_validate(ref)
            version = manager.ledger.get_version(old.dataset_id, old.record_id, old.version)
            # Previously sent versions keep their immutable identity even after a
            # deliberate refresh advances the cutoff to a newer publication.
            if not run.validate(version, historical=True):
                raise PermissionError(
                    "prior memory-derived history is revoked; start a clean Context Baseline"
                )
        residents = [
            v
            for v in frozen_versions(
                manager,
                installation.dataset_id,
                state["cutoff"],
                resident_only=True,
                limit=RESIDENT_CANDIDATE_LIMIT,
            )
            if run.validate(v)
        ][:RESIDENT_SELECTION_LIMIT]
        run.resident_ids = {v.ref.record_id for v in residents}
        available = {key: resolve(key) for key in explicit}
        if any(v is None or not run.validate(v) for v in available.values()):
            raise PermissionError("explicit memory is outside frozen authorized scope")
        result = CandidateBatch.model_validate(
            await manager.host.invoke(
                lease,
                "recall",
                RecallRequest(
                    context=context,
                    query=query[:10000] or "task",
                    explicit_refs=tuple(
                        dict.fromkeys(
                            [v.ref for k in sorted(explicit) if (v := available[k]) is not None]
                            + [v.ref for v in residents]
                        )
                    ),
                    knowledge_cutoff=state["cutoff"],
                    max_candidates=40,
                    token_budget=manager.registry.get_binding(
                        lease.binding_id
                    ).config.recall_token_budget,
                ),
            )
        )
        refs = (
            [v.ref for k in sorted(explicit) if (v := available[k]) is not None]
            + [v.ref for v in residents]
            + [c.ref for c in result.candidates]
        )
        seen = set()
        for ref in refs:
            if ref.record_id in seen:
                continue
            candidate_version = resolve(ref.record_id)
            if (
                candidate_version is None
                or candidate_version.ref != ref
                or not run.validate(candidate_version)
            ):
                continue
            seen.add(ref.record_id)
            run.candidates.append(candidate_version)
        save_manifest(manager, run_id, session_id, installation.dataset_id, state)
        return run
    except BaseException:
        manager._recall_runs.pop(context.request_id, None)
        manager.registry.release_run(lease.lease_id)
        raise
