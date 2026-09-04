from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from operant.api import create_app
from operant.application.factory import AgentFactory
from operant.application.security import (
    ActionNormalizer,
    ApprovalReviewerAdapter,
    CapabilityBroker,
    DenialRemediator,
    PolicyDenied,
    PolicyEngine,
    SecretBroker,
    SecretUnavailable,
    balanced_policy_bundle,
)
from operant.application.service import _PersistentActionGateway
from operant.domain.models import (
    CommandExecutionPolicy,
    CommandRunnerType,
    ModelProfile,
    RolePreset,
    ToolPolicy,
)
from operant.domain.security import (
    Capability,
    PolicyDecision,
    SecurityAuditEvent,
)
from operant.persistence.security import SQLiteSecurityRepository
from operant.persistence.sqlite import ConflictError, MigrationError, SQLiteStore
from operant.tools.workspace import ToolError, WorkspaceTools


def _action(
    workspace: Path,
    *,
    capabilities: tuple[Capability, ...] = (Capability.WORKSPACE_WRITE,),
    key: str = "action-1",
):
    return ActionNormalizer().normalize(
        principal="agent:test",
        tool="apply_patch",
        operation="execute",
        arguments={"path": "result.txt", "old_text": "", "new_text": "ok"},
        requested_capabilities=capabilities,
        idempotency_key=key,
        policy_version="phase45.v1",
        workspace=workspace,
    )


def test_v10_migration_is_frozen_append_only_and_refuses_lossy_rollback(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "security.sqlite3")
    store.migrate(10)
    assert store.schema_version() == 10
    migration = next(item for item in store._migrations() if item.version == 10)
    assert migration.name == "phase4_security_control_plane"
    assert migration.checksum == SQLiteStore._FROZEN_MIGRATION_CHECKSUMS[10]

    repository = SQLiteSecurityRepository(store)
    action = repository.record_security_action(_action(tmp_path))
    event = repository.append_security_audit(
        SecurityAuditEvent(
            action_hash=action.action_hash,
            principal=action.principal,
            event_type="policy.evaluated",
            decision=PolicyDecision.ALLOW,
        )
    )
    with (
        sqlite3.connect(store.path) as connection,
        pytest.raises(sqlite3.IntegrityError, match="append-only"),
    ):
        connection.execute(
            "UPDATE security_audit_events SET event_type='changed' WHERE id=?",
            (event.event_id,),
        )
    with pytest.raises(MigrationError, match="refusing to roll back"):
        store.rollback(9, isolated=True)


def test_policy_is_per_capability_and_deny_ask_allow_precedence(tmp_path: Path) -> None:
    engine = PolicyEngine(balanced_policy_bundle())
    allowed = _action(tmp_path)
    assert engine.evaluate(allowed).decision is PolicyDecision.ALLOW

    asked = _action(
        tmp_path,
        capabilities=(Capability.WORKSPACE_WRITE, Capability.NETWORK_EGRESS),
        key="mixed-ask",
    )
    assert engine.evaluate(asked).decision is PolicyDecision.ASK

    denied = _action(
        tmp_path,
        capabilities=(Capability.WORKSPACE_WRITE, Capability.PRODUCTION_MUTATE),
        key="mixed-deny",
    )
    evaluation = engine.evaluate(denied)
    assert evaluation.decision is PolicyDecision.DENY
    assert evaluation.hard_deny is True


@pytest.mark.asyncio
async def test_reviewer_only_handles_ask_and_fails_closed_with_redacted_input(
    tmp_path: Path,
) -> None:
    action = _action(
        tmp_path,
        capabilities=(Capability.NETWORK_EGRESS,),
        key="review",
    )
    evaluation = PolicyEngine(balanced_policy_bundle()).evaluate(action)
    seen: list[dict[str, object]] = []

    async def broken(payload: dict[str, object]):
        seen.append(payload)
        raise RuntimeError("provider unavailable")

    decision = await ApprovalReviewerAdapter(broken, timeout_seconds=1).review(action, evaluation)
    assert decision.decision is PolicyDecision.DENY
    assert decision.reason_code == "reviewer_fail_closed"
    assert "normalized_arguments" not in seen[0]

    with pytest.raises(PolicyDenied):
        await ApprovalReviewerAdapter(broken).review(
            _action(tmp_path), PolicyEngine(balanced_policy_bundle()).evaluate(_action(tmp_path))
        )


@pytest.mark.asyncio
async def test_sync_reviewer_timeout_fails_closed_and_ignores_late_allow(tmp_path: Path) -> None:
    action = _action(
        tmp_path,
        capabilities=(Capability.NETWORK_EGRESS,),
        key="sync-review-timeout",
    )
    evaluation = PolicyEngine(balanced_policy_bundle()).evaluate(action)
    release = threading.Event()
    finished = threading.Event()

    def slow_allow(_payload: dict[str, object]) -> dict[str, str]:
        try:
            release.wait(timeout=1)
            return {
                "decision": "allow",
                "reason_code": "late-allow",
                "summary": "this result arrived after the reviewer deadline",
            }
        finally:
            finished.set()

    started_at = time.monotonic()
    decision = await ApprovalReviewerAdapter(slow_allow, timeout_seconds=0.02).review(
        action, evaluation
    )
    elapsed = time.monotonic() - started_at

    assert decision.decision is PolicyDecision.DENY
    assert decision.reason_code == "reviewer_fail_closed"
    assert elapsed < 0.5

    release.set()
    assert await asyncio.to_thread(finished.wait, 1)
    assert decision.decision is PolicyDecision.DENY


def test_secret_broker_is_exact_short_lived_and_redacts_material(tmp_path: Path) -> None:
    secret_action = ActionNormalizer().normalize(
        principal="agent:test",
        tool="remote_api",
        operation="send",
        arguments={"url": "https://example.invalid/path"},
        requested_capabilities=(Capability.SECRET_USE,),
        idempotency_key="secret",
        policy_version="phase45.v1",
        secret_refs=("OPERANT_TEST_SECRET",),
    )
    asked = PolicyEngine(balanced_policy_bundle()).evaluate(secret_action)
    allowed = asked.model_copy(update={"decision": PolicyDecision.ALLOW})
    broker = SecretBroker({"OPERANT_TEST_SECRET": "top-secret-value"}, max_ttl_seconds=10)
    material = broker.issue(secret_action, allowed, secret_ref="OPERANT_TEST_SECRET", ttl_seconds=5)
    assert material.lease.expires_at - material.lease.issued_at == timedelta(seconds=5)
    redacted = broker.redact_output("token=top-secret-value", material)
    assert "top-secret-value" not in redacted
    assert "[REDACTED]" in redacted
    with pytest.raises(SecretUnavailable):
        broker.issue(secret_action, asked, secret_ref="OPERANT_TEST_SECRET")


def test_capability_consume_is_atomic_and_exact_under_concurrency(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "leases.sqlite3")
    store.initialize()
    repository = SQLiteSecurityRepository(store)
    action = repository.record_security_action(_action(tmp_path))
    evaluation = PolicyEngine(balanced_policy_bundle()).evaluate(action)
    broker = CapabilityBroker(repository)
    lease = broker.issue(
        action,
        evaluation,
        Capability.WORKSPACE_WRITE,
        issued_by="test",
        max_uses=1,
    )

    def consume() -> bool:
        try:
            broker.consume(lease.lease_id, action=action, capability=Capability.WORKSPACE_WRITE)
        except ConflictError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _index: consume(), range(2))) == [False, True]


def test_denial_remediation_stops_repeated_identical_denials(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "denials.sqlite3")
    store.initialize()
    repository = SQLiteSecurityRepository(store)
    engine = PolicyEngine(balanced_policy_bundle())
    remediator = DenialRemediator(repository, no_progress_threshold=3)
    results = []
    for index in range(3):
        action = repository.record_security_action(
            _action(
                tmp_path,
                capabilities=(Capability.PRODUCTION_MUTATE,),
                key=f"denial-{index}",
            )
        )
        results.append(remediator.build(action, engine.evaluate(action)))
    assert [item.repeated_count for item in results] == [1, 2, 3]
    assert results[-1].no_progress is True
    assert results[-1].allowed_alternatives == ()


def test_persistent_gateway_enforces_policy_and_consumes_exact_lease(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "gateway.sqlite3")
    store.initialize()
    profile = store.add_model_profile(
        ModelProfile(
            id="model-security-gateway",
            name="security-gateway",
            model_id="security-gateway",
            base_url="https://example.invalid/v1",
            secret_ref="OPERANT_TEST_KEY",
        )
    )
    role = store.create_role(
        RolePreset(
            id="role-security-gateway",
            name="Security Gateway",
            system_prompt="Exercise security gateway.",
            model_profile_id=profile.id,
            tool_policy=ToolPolicy(allowed_tools=("apply_patch",), workspace_write=True),
        )
    )
    session = AgentFactory(store).create_session(role.id)
    agent = store.create_agent(session.id)
    tools = WorkspaceTools(
        tmp_path,
        policy=role.tool_policy,
    )
    gateway = _PersistentActionGateway(
        store=store,
        session_id=session.id,
        agent_id=agent.id,
        tools=tools,
    )
    claim = gateway.reserve_tool_action(
        tool_call_id="patch-1",
        name="apply_patch",
        arguments={"path": "safe.txt", "old_text": "", "new_text": "safe"},
    )
    assert gateway.approval_requirement(claim) is None
    gateway.authorize_tool_action(claim)
    with store._connect() as connection:
        lease = connection.execute(
            "SELECT capability, uses, max_uses FROM capability_leases"
        ).fetchone()
    assert tuple(lease) == ("workspace.write", 1, 1)

    command_tools = WorkspaceTools(
        tmp_path,
        policy=ToolPolicy(
            allowed_tools=("run_command",),
            command_execution=True,
            command_execution_policy=CommandExecutionPolicy(runner=CommandRunnerType.HOST),
        ),
    )
    denied_gateway = _PersistentActionGateway(
        store=store,
        session_id=session.id,
        agent_id=agent.id,
        tools=command_tools,
    )
    with pytest.raises(ToolError, match="system.hard"):
        denied_gateway.reserve_tool_action(
            tool_call_id="sudo-1",
            name="run_command",
            arguments={"argv": ["sudo", "true"], "cwd": "."},
        )


def test_phase45_security_api_is_additive_and_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "api.sqlite3"
    with TestClient(create_app(database)) as client:
        metadata = client.get("/v1/protocol/phase45")
        assert metadata.status_code == 200
        assert metadata.json()["protocol_version"] == "phase45.v1"
        body = {
            "principal": "gui:user",
            "tool": "apply_patch",
            "operation": "execute",
            "arguments": {"path": "file.txt", "old_text": "", "new_text": "safe"},
            "requested_capabilities": ["workspace.write"],
            "idempotency_key": "gui-action-1",
            "workspace": str(tmp_path),
        }
        checked = client.post("/v1/security/policy/check", json=body)
        assert checked.status_code == 200
        assert checked.json()["evaluation"]["decision"] == "allow"
        assert client.post("/v1/security/policy/check", json=body).status_code == 200
        changed = {**body, "arguments": {**body["arguments"], "new_text": "changed"}}
        assert client.post("/v1/security/policy/check", json=changed).status_code == 409


def test_action_normalizer_rejects_workspace_escape(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="escapes the workspace"):
        ActionNormalizer().normalize(
            principal="agent:test",
            tool="apply_patch",
            operation="execute",
            arguments={"path": "../escape", "old_text": "", "new_text": "bad"},
            requested_capabilities=(Capability.WORKSPACE_WRITE,),
            idempotency_key="escape",
            policy_version="phase45.v1",
            workspace=tmp_path,
        )
