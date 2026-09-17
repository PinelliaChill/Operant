"""Focused MP-5.4 Remote Memory package and Target boundary checks."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from operant.api import create_app
from operant.application.phase45_gateway import Phase45ActionGateway
from operant.application.remote_execution import RemoteAuthorization, RemoteExecutionController
from operant.application.security import PolicyEngine
from operant.contracts.b2_3 import ManagementCommand
from operant.contracts.b2_6_remote import (
    RemoteMemoryCandidate,
    RemoteMemoryCommand,
    RemoteMemoryPack,
    RemoteMemoryUpload,
)
from operant.domain.models import ModelProfile, RolePreset
from operant.domain.remote_control import (
    EncryptedRemoteCommand,
    RelayEnvelope,
    RemoteDevice,
    RemoteScope,
    RemoteTransportMode,
)
from operant.domain.remote_execution import (
    CapabilityManifest,
    RemoteActionIdempotency,
    RemoteCapability,
    RemoteExecutionJob,
    RemoteTargetRegistration,
)
from operant.domain.security import (
    ActionRequest,
    Capability,
    PolicyBundle,
    PolicyDecision,
    PolicyLayer,
    PolicyRule,
)
from operant.domain.threads import ConversationThread
from operant.memory_plugins.manager import MemoryManager
from operant.memory_plugins.remote_memory import (
    RemoteMemoryError,
    RemoteMemoryService,
    RemoteMemoryTargetAdapter,
    RemoteMemoryTargetRuntime,
)
from operant.persistence.phase45 import SQLitePhase45Repository
from operant.persistence.remote_execution import SQLiteRemoteExecutionRepository
from operant.persistence.security import SQLiteSecurityRepository
from operant.remote_control.crypto import RemoteCrypto, RemoteKeyStore
from operant.remote_control.runtime import (
    DEFAULT_REMOTE_OPERATION_CAPABILITIES,
    MAX_RELAY_TTL_SECONDS,
    RelayService,
    RemoteActionPayload,
    RemoteControlService,
    remote_control_policy_engine,
)

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)


def _allow_engine() -> PolicyEngine:
    return PolicyEngine(
        PolicyBundle(
            bundle_id="b26-remote-test",
            version="b26.remote.test.v1",
            default_decision=PolicyDecision.DENY,
            rules=(
                PolicyRule(
                    rule_id="b26.remote.test.allow",
                    layer=PolicyLayer.SYSTEM,
                    decision=PolicyDecision.ALLOW,
                    reason="focused remote memory test",
                ),
            ),
        )
    )


class _Authorization(RemoteAuthorization):
    def __init__(self, gateway: Phase45ActionGateway) -> None:
        self.gateway = gateway

    def authorize(
        self,
        *,
        tool: str,
        operation: str,
        target_id: str,
        arguments: dict[str, Any],
        capabilities: Sequence[Capability],
        idempotency_key: str,
    ) -> ActionRequest:
        action, decision, _ = self.gateway.guard(
            tool=tool,
            operation=operation,
            target_id=target_id,
            arguments=arguments,
            capabilities=capabilities,
            idempotency_key=idempotency_key,
        )
        assert decision.decision.value == PolicyDecision.ALLOW.value
        assert decision.lease is not None
        self.gateway.consume(decision.lease, action)
        return action


def _target_controller(manager: MemoryManager) -> tuple[RemoteExecutionController, Any]:
    gateway = Phase45ActionGateway(
        SQLiteSecurityRepository(manager.store),
        SQLitePhase45Repository(manager.store),
        _allow_engine(),
        principal="b26:remote-target-test",
    )
    controller = RemoteExecutionController(
        SQLiteRemoteExecutionRepository(manager.store), _Authorization(gateway)
    )
    target = RemoteTargetRegistration(
        target_id="b26-target",
        display_name="B2-6 target",
        endpoint_ref="REMOTE_TARGET_ENDPOINT",
        identity_public_key="public-key-" + "x" * 32,
        credential_ref="REMOTE_TARGET_CREDENTIAL",
        policy_ref="balanced",
        artifact_namespace="b26-target-artifacts",
        capability_manifest=CapabilityManifest(
            version="1",
            capabilities=(RemoteCapability.TARGET_EXEC,),
            supported_operations=("memory.consume",),
            max_concurrent_jobs=2,
            platform="test",
        ),
        created_at=NOW,
        updated_at=NOW,
    )
    controller.register_target(target)
    controller.heartbeat_target(
        target.target_id,
        identity_public_key=target.identity_public_key,
        now=NOW,
    )
    lease = controller.acquire_lease(
        target.target_id,
        owner="b26:connector",
        workspace_ref="b26-workspace",
        ttl_seconds=180,
        now=NOW,
        idempotency_key="b26-target-lease",
    )
    return controller, lease


@pytest_asyncio.fixture
async def memory_project(tmp_path: Path):
    app = create_app(tmp_path / "core.sqlite3")
    service = app.state.operant_service
    manager = MemoryManager(service)
    service.memory_manager = manager

    async def command(**kwargs: Any):
        return await manager.execute(ManagementCommand(**kwargs))

    project = (
        (
            await command(
                action="project_create",
                name="B2-6 remote memory project",
                workspace_path=str(tmp_path),
            )
        )
        .state.projects[-1]
        .project_id
    )
    installation = (
        (
            await command(
                action="plugin_install",
                plugin_id="memory-standard",
                mode="trusted_in_process",
            )
        )
        .state.installations[-1]
        .installation_id
    )
    await command(action="binding_select", project_id=project, installation_id=installation)
    saved = await command(
        action="memory_save",
        project_id=project,
        content="Remote package source fact",
        confirmed=True,
    )
    assert saved.state.records
    yield manager, project
    await manager.close()
    service.close()


@pytest.mark.asyncio
async def test_state_works_without_connector_and_pack_creation_is_blocked(memory_project) -> None:
    manager, project = memory_project
    service = RemoteMemoryService(manager, clock=lambda: NOW)

    state = service.state(project)
    assert state.status == "ready"
    assert state.records
    assert state.affected_ids == ()

    with pytest.raises(RemoteMemoryError, match="package creation is blocked"):
        service.execute(
            RemoteMemoryCommand(
                action="remote_pack_create",
                project_id=project,
                target_id="arbitrary-target",
            )
        )


@pytest.mark.asyncio
async def test_registered_target_lease_package_consumption_and_local_upload_review(
    memory_project,
) -> None:
    manager, project = memory_project
    controller, lease = _target_controller(manager)
    from operant.remote import InMemoryRemoteTargetConnector

    connector = InMemoryRemoteTargetConnector(
        target_id="b26-target",
        lease_id=lease.lease_id,
        lease_token=lease.token,
        lease_fencing=lease.fencing,
    )
    runtime = RemoteMemoryTargetRuntime(controller, connector)
    service = RemoteMemoryService(manager, runtime, clock=lambda: NOW)
    created = service.execute(
        RemoteMemoryCommand(
            action="remote_pack_create",
            project_id=project,
            target_id="b26-target",
            purpose="remote_execution",
            ttl_seconds=60,
            idempotency_key="b26-pack-1",
        )
    )
    assert created.status == "completed"
    assert created.pack is not None
    assert created.affected_ids[0] == created.pack.package_id

    target = RemoteMemoryTargetAdapter("b26-target", clock=lambda: NOW)
    consumed = target.consume(service.target_payload(created.pack))
    assert consumed.package_id == created.pack.package_id
    with pytest.raises(RemoteMemoryError, match="another Target"):
        RemoteMemoryTargetAdapter("other-target", clock=lambda: NOW).consume(created.pack)

    entry = created.pack.entries[0]
    upload = RemoteMemoryUpload(
        package_id=created.pack.package_id,
        package_digest=created.pack.package_digest,
        project_id=project,
        dataset_id=created.pack.dataset_id,
        target_id=created.pack.target_id,
        purpose=created.pack.purpose,
        candidates=(
            RemoteMemoryCandidate(
                content="Target derived candidate",
                source_refs=entry.source_refs,
                reason="derived from the received package",
            ),
        ),
        submitted_at=NOW,
    )
    upload = upload.model_copy(update={"source_digest": upload.calculated_source_digest()})
    reviewed = service.execute(
        RemoteMemoryCommand(
            action="remote_upload_review",
            project_id=project,
            target_id="b26-target",
            upload=upload,
            idempotency_key="b26-upload-1",
        )
    )
    assert reviewed.status == "pending_review"
    assert reviewed.upload_id == upload.upload_id
    assert reviewed.affected_ids
    assert all(
        manager.ledger.get_proposal(proposal_id).state == "pending"
        for proposal_id in reviewed.affected_ids
    )


@pytest.mark.asyncio
async def test_multi_candidate_upload_rolls_back_as_one_batch(memory_project, monkeypatch) -> None:
    manager, project = memory_project
    controller, lease = _target_controller(manager)
    from operant.remote import InMemoryRemoteTargetConnector

    runtime = RemoteMemoryTargetRuntime(
        controller,
        InMemoryRemoteTargetConnector(
            target_id="b26-target",
            lease_id=lease.lease_id,
            lease_token=lease.token,
            lease_fencing=lease.fencing,
        ),
    )
    service = RemoteMemoryService(manager, runtime, clock=lambda: NOW)
    created = service.execute(
        RemoteMemoryCommand(
            action="remote_pack_create",
            project_id=project,
            target_id="b26-target",
            ttl_seconds=60,
            idempotency_key="b26-atomic-pack",
        )
    )
    assert created.pack is not None and created.pack.entries
    entry = created.pack.entries[0]
    record_ids = ("b26-atomic-a", "b26-atomic-b")
    upload = RemoteMemoryUpload(
        upload_id="b26-atomic-upload",
        package_id=created.pack.package_id,
        package_digest=created.pack.package_digest,
        project_id=project,
        dataset_id=created.pack.dataset_id,
        target_id=created.pack.target_id,
        purpose=created.pack.purpose,
        candidates=tuple(
            RemoteMemoryCandidate(
                record_id=record_id,
                content=f"atomic candidate {record_id}",
                source_refs=entry.source_refs,
                reason="atomic batch rollback check",
            )
            for record_id in record_ids
        ),
        submitted_at=NOW,
    )
    upload = upload.model_copy(update={"source_digest": upload.calculated_source_digest()})

    original_store_idempotency = manager.ledger._store_idempotency
    calls = 0

    def fail_mid_batch(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("injected mid-batch failure")
        original_store_idempotency(*args, **kwargs)

    monkeypatch.setattr(manager.ledger, "_store_idempotency", fail_mid_batch)
    with pytest.raises(RuntimeError, match="mid-batch"):
        service.execute(
            RemoteMemoryCommand(
                action="remote_upload_review",
                project_id=project,
                target_id=created.pack.target_id,
                upload=upload,
            )
        )
    assert calls == 3

    with manager.store._connect() as connection:
        for table in ("memory_ledger_heads", "memory_ledger_versions"):
            row = connection.execute(
                f"SELECT COUNT(*) AS count FROM {table} WHERE record_id IN (?, ?)",
                record_ids,
            ).fetchone()
            assert row is not None and int(row["count"]) == 0
        row = connection.execute(
            "SELECT COUNT(*) AS count FROM memory_ledger_proposals WHERE record_id IN (?, ?)",
            record_ids,
        ).fetchone()
        assert row is not None and int(row["count"]) == 0
        row = connection.execute(
            "SELECT 1 FROM b26_memory_uploads WHERE upload_id=?",
            (upload.upload_id,),
        ).fetchone()
        assert row is None

    # A retry after the injected failure is a normal first submission: the
    # transaction left no partial ledger rows or upload receipt behind.
    monkeypatch.undo()
    reviewed = service.execute(
        RemoteMemoryCommand(
            action="remote_upload_review",
            project_id=project,
            target_id=created.pack.target_id,
            upload=upload,
        )
    )
    assert reviewed.status == "pending_review"
    assert len(reviewed.affected_ids) == len(record_ids)


@pytest.mark.asyncio
async def test_expired_revoked_and_tampered_packages_or_uploads_fail_closed(
    memory_project,
) -> None:
    manager, project = memory_project
    controller, lease = _target_controller(manager)
    from operant.remote import InMemoryRemoteTargetConnector

    connector = InMemoryRemoteTargetConnector(
        target_id="b26-target",
        lease_id=lease.lease_id,
        lease_token=lease.token,
        lease_fencing=lease.fencing,
    )
    runtime = RemoteMemoryTargetRuntime(controller, connector)
    service = RemoteMemoryService(manager, runtime, clock=lambda: NOW)
    created = service.execute(
        RemoteMemoryCommand(
            action="remote_pack_create",
            project_id=project,
            target_id="b26-target",
            ttl_seconds=30,
            idempotency_key="b26-negative-pack",
        )
    )
    assert created.pack is not None
    pack = created.pack
    expired_target = RemoteMemoryTargetAdapter(
        "b26-target", clock=lambda: NOW + timedelta(seconds=31)
    )
    with pytest.raises(RemoteMemoryError, match="expired"):
        expired_target.consume(pack)

    tampered = pack.model_copy(update={"purpose": "tampered"})
    with pytest.raises(RemoteMemoryError, match="digest"):
        RemoteMemoryTargetAdapter("b26-target", clock=lambda: NOW).consume(tampered)

    bad_digest_upload = RemoteMemoryUpload(
        package_id=pack.package_id,
        package_digest=pack.package_digest,
        project_id=project,
        dataset_id=pack.dataset_id,
        target_id=pack.target_id,
        purpose=pack.purpose,
        source_digest="0" * 64,
        submitted_at=NOW,
    )
    with pytest.raises(RemoteMemoryError, match="source digest"):
        service.execute(
            RemoteMemoryCommand(
                action="remote_upload_review",
                project_id=project,
                target_id=pack.target_id,
                upload=bad_digest_upload,
            )
        )

    target_mismatch_upload = RemoteMemoryUpload(
        package_id=pack.package_id,
        package_digest=pack.package_digest,
        project_id=project,
        dataset_id=pack.dataset_id,
        target_id="other-target",
        purpose=pack.purpose,
        submitted_at=NOW,
    )
    with pytest.raises(RemoteMemoryError, match="Target or purpose"):
        service.execute(
            RemoteMemoryCommand(
                action="remote_upload_review",
                project_id=project,
                target_id="other-target",
                upload=target_mismatch_upload,
            )
        )

    purpose_mismatch_upload = target_mismatch_upload.model_copy(
        update={"target_id": pack.target_id, "purpose": "another-purpose"}
    )
    purpose_mismatch_upload = purpose_mismatch_upload.model_copy(
        update={"source_digest": purpose_mismatch_upload.calculated_source_digest()}
    )
    with pytest.raises(RemoteMemoryError, match="Target or purpose"):
        service.execute(
            RemoteMemoryCommand(
                action="remote_upload_review",
                project_id=project,
                target_id=pack.target_id,
                upload=purpose_mismatch_upload,
            )
        )

    entry = pack.entries[0]
    assert entry.source_refs
    stale_source = entry.source_refs[0].model_copy(
        update={"permission_epoch": entry.source_refs[0].permission_epoch + 1}
    )
    unauthorized_upload = RemoteMemoryUpload(
        package_id=pack.package_id,
        package_digest=pack.package_digest,
        project_id=project,
        dataset_id=pack.dataset_id,
        target_id=pack.target_id,
        purpose=pack.purpose,
        candidates=(
            RemoteMemoryCandidate(
                content="candidate with stale source",
                source_refs=(stale_source,),
                reason="stale source negative case",
            ),
        ),
        submitted_at=NOW,
    )
    unauthorized_upload = unauthorized_upload.model_copy(
        update={"source_digest": unauthorized_upload.calculated_source_digest()}
    )
    with pytest.raises(RemoteMemoryError, match="source"):
        service.execute(
            RemoteMemoryCommand(
                action="remote_upload_review",
                project_id=project,
                target_id=pack.target_id,
                upload=unauthorized_upload,
            )
        )

    revoked = service.execute(
        RemoteMemoryCommand(
            action="remote_pack_revoke",
            project_id=project,
            target_id=pack.target_id,
            package_id=pack.package_id,
            package_digest=pack.package_digest,
        )
    )
    assert revoked.status == "revoked"
    assert revoked.pack is not None
    target = RemoteMemoryTargetAdapter("b26-target", clock=lambda: NOW)
    target.revoke(pack)
    with pytest.raises(RemoteMemoryError, match="revoked or expired"):
        target.consume(pack)
    revoked_upload = RemoteMemoryUpload(
        package_id=pack.package_id,
        package_digest=pack.package_digest,
        project_id=project,
        dataset_id=pack.dataset_id,
        target_id=pack.target_id,
        purpose=pack.purpose,
        submitted_at=NOW,
    )
    with pytest.raises(RemoteMemoryError, match="expired or revoked"):
        service.execute(
            RemoteMemoryCommand(
                action="remote_upload_review",
                project_id=project,
                target_id=pack.target_id,
                upload=revoked_upload,
            )
        )


@pytest.mark.asyncio
async def test_role_and_agent_restrictions_apply_before_pack_emission(memory_project) -> None:
    manager, project = memory_project
    application = manager.service
    profile = application.add_model_profile(
        ModelProfile(
            name="B2-6 restriction model",
            model_id="b26-restriction-model",
            base_url="https://example.invalid/v1",
            secret_ref="B26_RESTRICTION_KEY",
        )
    )
    allowed_role = application.create_role(
        RolePreset(
            name="B2-6 allowed role",
            system_prompt="test",
            model_profile_id=profile.id,
            memory_scope="project",
        )
    )
    project_row = manager._project(project)
    workspace_ref = manager.store.get_workspace_initialization_by_id(
        project_row["workspace_id"]
    ).workspace_ref
    allowed_thread = application.create_thread(ConversationThread(workspace_ref=workspace_ref))
    allowed_session = application.create_session(allowed_role.id, thread_id=allowed_thread.id)
    allowed_agent = application.factory.create_agent(allowed_session.id)
    other_role = application.create_role(
        RolePreset(
            name="B2-6 other role",
            system_prompt="test",
            model_profile_id=profile.id,
            memory_scope="project",
        )
    )
    other_thread = application.create_thread(ConversationThread(workspace_ref=workspace_ref))
    other_session = application.create_session(other_role.id, thread_id=other_thread.id)
    other_agent = application.factory.create_agent(other_session.id)

    project_row = manager._project(project)
    installation = manager.registry.get_installation(project_row["installation_id"])
    binding = manager.registry.get_binding(installation.binding_id)
    current = manager.ledger.get_version(
        installation.dataset_id, manager._records(project_row)[0].record_id
    )
    current_head = manager.ledger.get_head(installation.dataset_id, current.ref.record_id)
    restricted_content = "Role and agent restricted remote fact"
    restricted = current.model_copy(
        update={
            "ref": current.ref.model_copy(
                update={
                    "version": current.ref.version + 1,
                    "content_digest": hashlib.sha256(
                        restricted_content.encode("utf-8")
                    ).hexdigest(),
                }
            ),
            "content": restricted_content,
            "role_ids": (allowed_role.id,),
            "agent_ids": (allowed_agent.id,),
        }
    )
    manager.ledger.save_version(
        restricted,
        expected_head_revision=current_head.revision,
        permission_epoch=binding.permission_epoch,
        idempotency_key="b26-restriction-save",
    )
    proposal = manager.ledger.propose(
        restricted,
        operation="modify",
        source_refs=restricted.sources,
        extractor_version="b26-test",
        reason="restriction test",
        expected_head_revision=current_head.revision,
        idempotency_key="b26-restriction-propose",
        permission_epoch=binding.permission_epoch,
    )
    manager.ledger.confirm_proposal(
        proposal,
        dataset_id=installation.dataset_id,
        expected_head_revision=current_head.revision,
        permission_epoch=binding.permission_epoch,
        idempotency_key="b26-restriction-confirm",
    )

    controller, lease = _target_controller(manager)
    from operant.remote import InMemoryRemoteTargetConnector

    runtime = RemoteMemoryTargetRuntime(
        controller,
        InMemoryRemoteTargetConnector(
            target_id="b26-target",
            lease_id=lease.lease_id,
            lease_token=lease.token,
            lease_fencing=lease.fencing,
        ),
    )
    service = RemoteMemoryService(manager, runtime, clock=lambda: NOW)

    unbound_session = application.create_session(allowed_role.id)
    with pytest.raises(RemoteMemoryError, match="Thread binding"):
        service.execute(
            RemoteMemoryCommand(
                action="remote_pack_create",
                project_id=project,
                target_id="b26-target",
                session_id=unbound_session.id,
                ttl_seconds=30,
                idempotency_key="b26-restriction-unbound-session",
            )
        )

    allowed = service.execute(
        RemoteMemoryCommand(
            action="remote_pack_create",
            project_id=project,
            target_id="b26-target",
            session_id=allowed_session.id,
            agent_instance_id=allowed_agent.id,
            ttl_seconds=30,
            idempotency_key="b26-restriction-allowed",
        )
    )
    assert allowed.pack is not None
    assert any(item.content == restricted_content for item in allowed.pack.entries)

    wrong_role = service.execute(
        RemoteMemoryCommand(
            action="remote_pack_create",
            project_id=project,
            target_id="b26-target",
            session_id=other_session.id,
            agent_instance_id=other_agent.id,
            ttl_seconds=30,
            idempotency_key="b26-restriction-role-denied",
        )
    )
    assert wrong_role.pack is not None
    assert all(item.content != restricted_content for item in wrong_role.pack.entries)

    wrong_agent = application.factory.create_agent(allowed_session.id)
    wrong_agent_result = service.execute(
        RemoteMemoryCommand(
            action="remote_pack_create",
            project_id=project,
            target_id="b26-target",
            session_id=allowed_session.id,
            agent_instance_id=wrong_agent.id,
            ttl_seconds=30,
            idempotency_key="b26-restriction-agent-denied",
        )
    )
    assert wrong_agent_result.pack is not None
    assert all(item.content != restricted_content for item in wrong_agent_result.pack.entries)


def test_target_runtime_rejects_short_lease_and_target_job_adapter() -> None:
    # Contract-level package validation also keeps Target jobs from accepting
    # a package whose TTL outlives the active lease.
    issued = NOW
    pack = RemoteMemoryPack(
        project_id="project",
        dataset_id="dataset",
        target_id="target",
        purpose="remote_execution",
        issued_at=issued,
        expires_at=issued + timedelta(seconds=30),
        binding_epoch=1,
        permission_epoch=1,
        revocation_epoch=1,
        entries=(),
        token_count=0,
        byte_count=0,
        max_tokens=100,
        max_bytes=1024,
    )
    pack = pack.model_copy(update={"package_digest": pack.calculated_digest()})
    job = RemoteExecutionJob(
        target_id="target",
        lease_id="lease",
        lease_fencing=1,
        capability=RemoteCapability.TARGET_EXEC,
        operation="memory.consume",
        arguments={"remote_memory_pack": pack.model_dump(mode="json")},
        action_hash="a" * 64,
        idempotency_key="job-1",
        idempotency=RemoteActionIdempotency.IDEMPOTENT,
        created_at=issued,
    )
    consumed = RemoteMemoryTargetAdapter("target", clock=lambda: NOW).consume_job(job)
    assert consumed.package_id == pack.package_id


def test_remote_control_registers_memory_query_and_command_capabilities() -> None:
    device = RemoteDevice(
        host_id="host",
        display_name="remote memory device",
        signing_public_key="s" * 32,
        exchange_public_key="e" * 32,
        scopes=(RemoteScope.OBSERVE, RemoteScope.COMMAND),
    )
    query = RemoteActionPayload(
        tool="memory",
        operation="query",
        target_id="project",
        capabilities=DEFAULT_REMOTE_OPERATION_CAPABILITIES[("memory", "query")],
    )
    command = RemoteActionPayload(
        tool="memory",
        operation="command",
        target_id="project",
        capabilities=DEFAULT_REMOTE_OPERATION_CAPABILITIES[("memory", "command")],
    )
    RemoteControlService._require_scope(device, query, DEFAULT_REMOTE_OPERATION_CAPABILITIES)
    RemoteControlService._require_scope(device, command, DEFAULT_REMOTE_OPERATION_CAPABILITIES)


@pytest.mark.asyncio
async def test_encrypted_remote_control_query_calls_core_and_relay_stays_opaque(
    memory_project,
) -> None:
    manager, project = memory_project
    remote = RemoteMemoryService(manager, clock=lambda: NOW)
    seen: list[RemoteActionPayload] = []

    def executor(payload: RemoteActionPayload, _action: ActionRequest) -> str:
        seen.append(payload)
        return remote.execute_remote_payload(payload)

    control = RemoteControlService(
        manager.store,
        RemoteKeyStore((manager.store.path.parent / "remote-keys.json").absolute()),
        remote_control_policy_engine(),
        executor=executor,
    )
    signing_public, signing_private = RemoteCrypto.create_signing_keypair()
    exchange_public, exchange_private = RemoteCrypto.create_exchange_keypair()
    host = control.enable_host(
        display_name="B2-6 Core",
        core_version="0.1.0",
        protocol_version="phase5b.v1",
    )
    ticket = control.create_pairing_challenge(
        host.host_id,
        allowed_scopes=(RemoteScope.OBSERVE, RemoteScope.COMMAND),
    )
    device = control.pair_device(
        challenge_id=ticket.challenge_id,
        one_time_code=ticket.one_time_code,
        display_name="B2-6 device",
        signing_public_key=signing_public,
        exchange_public_key=exchange_public,
        scopes=(RemoteScope.OBSERVE, RemoteScope.COMMAND),
    )
    session = control.create_session(
        host_id=host.host_id,
        device_id=device.device_id,
        transport_mode=RemoteTransportMode.RELAY,
        protocol_version="phase5b.v1",
    )
    session_key = RemoteCrypto.derive_session_key(
        exchange_private,
        host.exchange_public_key,
        session_id=session.remote_session_id,
        host_id=host.host_id,
        device_id=device.device_id,
    )
    issued = datetime.now(timezone.utc)
    unsigned = EncryptedRemoteCommand(
        command_id="b26-memory-query",
        idempotency_key="b26-memory-query-key",
        host_id=host.host_id,
        device_id=device.device_id,
        remote_session_id=session.remote_session_id,
        protocol_version="phase5b.v1",
        issued_at=issued,
        expires_at=issued + timedelta(minutes=2),
        nonce="0" * 16,
        ciphertext="placeholder",
        signature="0" * 32,
    )
    body_marker = "Remote package source fact"
    encrypted = RemoteCrypto.encrypt_command_payload(
        unsigned,
        {
            "tool": "memory",
            "operation": "query",
            "target_id": project,
            "arguments": {"untrusted_marker": body_marker},
            "capabilities": [Capability.REMOTE_CONTROL_OBSERVE.value],
        },
        session_key,
        signing_private,
    )
    receipt = control.submit_command(encrypted)
    assert receipt.status.value == "completed"
    assert receipt.result_ref == f"remote-memory-state:{project}"
    assert len(seen) == 1 and seen[0].tool == "memory"

    relay_ciphertext, relay_nonce = RemoteCrypto.encrypt_relay_payload(
        encrypted.model_dump_json().encode(),
        session_key,
        route_ref=session.remote_session_id,
    )
    RelayService(control.repository).publish(
        RelayEnvelope(
            route_ref=session.remote_session_id,
            sender_ref=device.device_id,
            recipient_ref=host.host_id,
            protocol_version=session.protocol_version,
            ciphertext=relay_ciphertext,
            nonce=relay_nonce,
            expires_at=issued + timedelta(seconds=MAX_RELAY_TTL_SECONDS),
            created_at=issued,
        )
    )
    with manager.store._connect() as connection:
        row = connection.execute("SELECT * FROM relay_envelopes LIMIT 1").fetchone()
    assert row is not None
    assert body_marker not in row["ciphertext"]
    assert "body" not in set(row.keys())
