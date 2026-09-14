"""MP-0 contract source. No Host, persistence migration or HTTP registration.

Identity and authorization are resolved by Core, never trusted from a plugin.
Schema validation checks representation; future repositories must enforce current
epochs, grants, ownership, CAS and source access inside their commit transaction.
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Literal

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, model_validator

APP_CONTRACT_VERSION = "operant-b2-contract.v1"
PLUGIN_SDK_VERSION = "operant-memory-sdk.v1"
Id = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
SecretRef = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,199}$")]
Revision = Annotated[int, Field(ge=0, le=2**53 - 1)]


def _bounded_cursor(value: str) -> str:
    if int(value) > 2**63 - 1:
        raise ValueError("cursor exceeds SQLite int64")
    return value


Cursor = Annotated[
    str,
    Field(
        pattern=r"^(0|[1-9][0-9]{0,18})$",
        json_schema_extra={"x-integer-maximum": str(2**63 - 1)},
    ),
    AfterValidator(_bounded_cursor),
]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WorkspaceIdentity(Contract):
    workspace_id: Id
    project_id: Id
    workspace_ref: Annotated[str, Field(pattern=r"^workspace:[0-9a-f]{64}$")]
    worktree_id: Id | None
    association_revision: Revision
    association: Literal["explicit_registration"]


class ProjectIdentity(Contract):
    project_id: Id
    display_name: Annotated[str, Field(min_length=1, max_length=200)]
    revision: Revision
    workspace_ids: tuple[Id, ...]
    sharing: Literal["explicit_grants_only"]


class WorkspaceScope(Contract):
    kind: Literal["workspace"]
    project_id: Id
    workspace_id: Id


class SessionScope(Contract):
    kind: Literal["session"]
    project_id: Id
    workspace_id: Id
    session_id: Id


class RunScope(Contract):
    kind: Literal["run"]
    project_id: Id
    workspace_id: Id
    run_id: Id
    writer_id: Id | None


class PersonalScope(Contract):
    kind: Literal["personal"]
    principal_id: Id
    opt_in_grant_id: Id


Scope = Annotated[
    WorkspaceScope | SessionScope | RunScope | PersonalScope, Field(discriminator="kind")
]


class Grant(Contract):
    grant_id: Id
    subject_id: Id
    grantor_id: Id
    scope: Scope
    purpose: Literal["recall", "source_read", "publish", "share", "transfer", "delete"]
    policy_revision: Revision
    permission_epoch: Revision
    expires_at: AwareDatetime


class TaskSource(Contract):
    source_type: Literal["session", "workflow_run", "team_task"]
    source_id: Id


class TaskAction(Contract):
    action: Literal["inspect", "cancel", "resume", "approve", "archive"]
    availability: Literal["available", "blocked", "unsupported"]
    reason_code: Id | None
    projection_revision: Revision


class TaskProjection(Contract):
    """Stable key is (source_type, source_id); status belongs to that source."""

    source: TaskSource
    project_id: Id
    workspace_id: Id
    source_status: Id
    revision: Revision
    actions: tuple[TaskAction, ...]


class AgentBinding(Contract):
    role_preset_id: Id
    role_version: Revision
    definition_revision_id: Id | None
    snapshot_id: Id
    agent_instance_id: Id
    session_id: Id
    run_id: Id | None
    model_profile_id: Id
    provider_id: Id
    provider_model_id: Annotated[str, Field(min_length=1, max_length=300)]
    capability_digest: Digest


class EffectiveSetting(Contract):
    key: Id
    value_ref: Id
    source: Literal["global", "project", "agent", "run"]
    source_id: Id
    revision: Revision
    effective_at: AwareDatetime
    applies_to: Literal["new_runs", "next_request", "immediate_revocation"]


class DatasetOwner(Contract):
    kind: Literal["plugin_dataset"]
    owner_namespace: Annotated[str, Field(pattern=r"^dataset:[A-Za-z0-9][A-Za-z0-9_.-]*$")]
    dataset_id: Id
    principal_id: Id

    @model_validator(mode="after")
    def namespace_matches_dataset(self) -> DatasetOwner:
        if self.owner_namespace != f"dataset:{self.dataset_id}":
            raise ValueError("unknown_owner: namespace must name the exact dataset")
        return self


class DatasetConsumer(Contract):
    consumer_id: Id
    installation_id: Id
    grant_id: Id
    purpose: Literal["primary_engine", "shared_read", "export", "migration"]


class Dataset(Contract):
    owner: DatasetOwner
    installation_id: Id | None
    state: Literal["bound", "retained", "deleting", "deleted", "blocked"]
    revision: Revision
    consumers: tuple[DatasetConsumer, ...]
    retention_lock_ids: tuple[Id, ...]
    management_actions: tuple[Literal["inspect", "export", "delete", "rebind"], ...]


class DatasetTransfer(Contract):
    dataset_id: Id
    destination_dataset_id: Id
    expected_revision: Revision
    from_namespace: Id
    to_namespace: Id
    destination_installation_id: Id
    authorization_grant_id: Id
    mode: Literal["transfer", "authorized_copy"]
    idempotency_key: Id

    @model_validator(mode="after")
    def check_transfer_identity(self) -> DatasetTransfer:
        if self.from_namespace != f"dataset:{self.dataset_id}":
            raise ValueError("unknown_owner: source namespace mismatch")
        if self.to_namespace != f"dataset:{self.destination_dataset_id}":
            raise ValueError("unknown_owner: destination namespace mismatch")
        same_dataset = self.dataset_id == self.destination_dataset_id
        if (self.mode == "transfer") != same_dataset:
            raise ValueError("transfer preserves dataset identity; authorized_copy needs a new id")
        return self


class MemoryDisabled(Contract):
    state: Literal["disabled"]
    binding_id: Id
    binding_epoch: Revision
    cause: Literal["global_off", "binding_off", "not_installed", "revoked", "failed"]


class MemoryEnabled(Contract):
    state: Literal["enabled"]
    binding_id: Id
    binding_epoch: Revision
    installation_id: Id
    dataset_id: Id
    config_digest: Digest
    package_digest: Digest
    certification_id: Id
    index_generation: Id
    global_enabled: Literal[True]


MemoryBinding = Annotated[MemoryDisabled | MemoryEnabled, Field(discriminator="state")]


class SourceRef(Contract):
    source_type: Literal["item", "artifact", "memory_version", "mailbox_message", "workflow_event"]
    source_id: Id
    revision: Revision
    content_digest: Digest
    scope: Scope
    permission_epoch: Revision
    availability: Literal["available", "deleted", "revoked", "unavailable"]


class MemoryVersionRef(Contract):
    dataset_id: Id
    record_id: Id
    version: Annotated[int, Field(ge=1, le=2**53 - 1)]
    content_digest: Digest


class MemoryConditions(Contract):
    commit_ref: Id | None
    tree_digest: Digest | None
    file_fingerprints: dict[Id, Digest]
    environment_digest: Digest | None
    tool_versions: dict[Id, Id]
    verified_at: AwareDatetime | None
    valid_from: AwareDatetime
    valid_until: AwareDatetime | None


class MemoryVersion(Contract):
    ref: MemoryVersionRef
    owner: DatasetOwner
    kind: Literal["working", "episodic", "project"]
    content_type: Literal["fact", "preference", "episode", "procedure"]
    scope: Scope
    role_ids: tuple[Id, ...]
    agent_ids: tuple[Id, ...]
    content: Annotated[str, Field(min_length=1, max_length=100_000)]
    sources: tuple[SourceRef, ...]
    evidence: Literal["user_asserted", "observed", "tested", "inferred", "legacy_unverified"]
    sensitivity: Literal["public", "internal", "sensitive"]
    retention_policy_id: Id
    conditions: MemoryConditions
    recorded_at: AwareDatetime

    @model_validator(mode="after")
    def check_boundaries(self) -> MemoryVersion:
        if self.ref.dataset_id != self.owner.dataset_id:
            raise ValueError("unknown_owner: version and owner dataset differ")
        if self.kind == "working" and not isinstance(self.scope, SessionScope):
            raise ValueError("unknown_scope: working memory requires session scope")
        if self.kind == "project" and not isinstance(self.scope, (WorkspaceScope, RunScope)):
            raise ValueError("unknown_scope: project memory requires workspace or run scope")
        if not self.content.strip():
            raise ValueError("memory content is blank")
        if not self.sources and self.evidence != "legacy_unverified":
            raise ValueError("verified provenance requires sources")
        return self


class MemoryHead(Contract):
    dataset_id: Id
    record_id: Id
    published_version: MemoryVersionRef | None
    revision: Revision
    publication_cursor: Cursor
    state: Literal["unpublished", "published", "inactive", "revoked", "deleted"]
    permission_epoch: Revision

    @model_validator(mode="after")
    def check_pointer(self) -> MemoryHead:
        ref = self.published_version
        if ref and (ref.dataset_id != self.dataset_id or ref.record_id != self.record_id):
            raise ValueError("published pointer must reference the same record and dataset")
        if self.state == "published" and ref is None:
            raise ValueError("published head requires an exact version")
        return self


class MemoryProposal(Contract):
    proposal_id: Id
    proposal_revision: Revision
    owner: DatasetOwner
    operation: Literal["create", "modify", "merge", "supersede", "revoke"]
    base_head: MemoryHead
    proposed_version: MemoryVersionRef
    source_refs: tuple[SourceRef, ...]
    extractor_version: Id
    reason: Annotated[str, Field(min_length=1, max_length=2000)]
    state: Literal["pending", "accepted", "rejected", "conflict", "cancelled"]

    @model_validator(mode="after")
    def check_proposal_target(self) -> MemoryProposal:
        if not (
            self.owner.dataset_id == self.base_head.dataset_id == self.proposed_version.dataset_id
            and self.base_head.record_id == self.proposed_version.record_id
        ):
            raise ValueError("proposal head and target must belong to the same owned record")
        return self


class CommitGuard(Contract):
    binding_id: Id
    binding_epoch: Revision
    permission_epoch: Revision
    certification_id: Id
    certification_epoch: Revision
    lease_id: Id
    lease_fencing: Revision
    expected_head_revision: Revision
    idempotency_key: Id
    request_digest: Digest


class PublishRequest(Contract):
    proposal_id: Id
    proposal_revision: Revision
    target: MemoryVersionRef
    guard: CommitGuard


class MemoryPack(Contract):
    manifest_id: Id
    manifest_digest: Digest
    scope: Scope
    run_id: Id
    agent_instance_id: Id
    binding: MemoryEnabled
    knowledge_cutoff: Cursor
    policy_revision: Revision
    permission_epoch: Revision
    selected: tuple[MemoryVersionRef, ...]
    total_token_budget: Annotated[int, Field(ge=0, le=1_000_000)]
    token_count: Annotated[int, Field(ge=0, le=1_000_000)]
    selection: Literal["automatic_and_explicit"]

    @model_validator(mode="after")
    def check_budget_and_dataset(self) -> MemoryPack:
        if self.token_count > self.total_token_budget:
            raise ValueError("budget_exceeded")
        if any(ref.dataset_id != self.binding.dataset_id for ref in self.selected):
            raise ValueError("permission_denied: cross-dataset requires authorized import")
        return self


class ContextMemoryUse(Contract):
    context_revision_id: Id
    manifest_id: Id
    actual_versions: tuple[MemoryVersionRef, ...]
    permission_epoch: Revision
    binding_epoch: Revision
    derived_compaction_ids: tuple[Id, ...]
    refresh: Literal["frozen", "explicit_refresh", "revocation_rebuild", "blocked"]


class Resource(Contract):
    resource_id: Id
    owner: DatasetOwner
    installation_id: Id
    storage: Literal["core_rows", "managed_directory", "shared_dependency", "external"]
    category: Literal[
        "package",
        "config",
        "state",
        "index",
        "cache",
        "tmp",
        "log",
        "record",
        "version",
        "proposal",
        "relation",
        "observation",
        "manifest",
        "job",
        "subscription",
    ]
    locator_ref: Id
    consumer_ids: tuple[Id, ...]
    retention_lock_ids: tuple[Id, ...]
    reconstructible: bool


class UninstallRequest(Contract):
    installation_id: Id
    data_policy: Literal["keep", "delete"]
    inventory_revision: Revision
    expected_binding_epoch: Revision
    idempotency_key: Id
    stop_run_ids: tuple[Id, ...]


class CleanupItem(Contract):
    resource_id: Id
    outcome: Literal["pending", "deleted", "retained", "blocked", "external_unconfirmed"]
    reason: Literal[
        "exclusive",
        "keep_selected",
        "shared_consumer",
        "retention_lock",
        "active_run",
        "directory_identity_changed",
        "external_resource",
        "retry_required",
    ]
    blocker_ids: tuple[Id, ...]


class LifecycleReceipt(Contract):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {"state": {"enum": ["enabled", "disabled", "uninstalled"]}}
                    },
                    "then": {"properties": {"ack": {"const": "completed"}}},
                },
                {
                    "if": {"properties": {"ack": {"const": "completed"}}},
                    "then": {
                        "properties": {
                            "state": {"enum": ["enabled", "disabled", "uninstalled"]},
                            "cleanup": {
                                "items": {
                                    "properties": {"outcome": {"enum": ["deleted", "retained"]}}
                                }
                            },
                        }
                    },
                },
            ]
        },
    )

    operation_id: Id
    installation_id: Id
    state: Literal[
        "enabled",
        "disabling",
        "disabled",
        "uninstalling",
        "uninstalled",
        "failed",
        "restart_required",
        "blocked",
    ]
    binding_epoch: Revision
    inventory_revision: Revision
    cleanup: tuple[CleanupItem, ...]
    cursor: Cursor
    ack: Literal["host_accepted", "completed", "failed", "unknown"]

    @model_validator(mode="after")
    def check_completion(self) -> LifecycleReceipt:
        terminal_success = self.state in {"enabled", "disabled", "uninstalled"}
        if terminal_success != (self.ack == "completed"):
            raise ValueError("terminal lifecycle success requires completed acknowledgement")
        if self.ack == "completed" and any(
            item.outcome in {"pending", "blocked", "external_unconfirmed"} for item in self.cleanup
        ):
            raise ValueError("cleanup_blocked: unfinished resources cannot report completion")
        return self


class Tombstone(Contract):
    dataset_id: Id
    record_id: Id | None
    state: Literal["deleted", "revoked", "source_deleted"]
    revision: Revision
    cursor: Cursor


class Certification(Contract):
    certification_id: Id
    issuer_id: Id
    plugin_id: Id
    plugin_version: Id
    package_digest: Digest
    dependencies_digest: Digest
    permissions_digest: Digest
    lifecycle_evidence_digest: Digest
    allowed_modes: tuple[Literal["trusted_in_process", "isolated"], ...]
    valid_from: AwareDatetime
    expires_at: AwareDatetime
    revocation_epoch: Revision
    state: Literal["valid", "revoked", "expired"]
    signature_ref: Id


class PluginManifest(Contract):
    plugin_id: Id
    plugin_version: Id
    sdk_version: Literal["operant-memory-sdk.v1"]
    host_api_versions: tuple[Literal["operant-memory-sdk.v1"], ...]
    package_digest: Digest
    dependencies_digest: Digest
    permissions_digest: Digest
    entrypoint: Id
    config_schema_ref: Id
    config_schema_digest: Digest
    state_schema_version: Id
    capabilities: tuple[Literal["extract", "recall", "maintain", "on_index_event"], ...]
    memory_mb: Annotated[int, Field(ge=1, le=65536)]
    max_rpc_bytes: Annotated[int, Field(ge=1, le=16_777_216)]
    max_concurrency: Annotated[int, Field(ge=1, le=64)]
    export_supported: bool
    import_supported: bool
    recoverable: bool


class HostAdmission(Contract):
    host_instance_id: Id
    installation_id: Id
    mode: Literal["trusted_in_process", "isolated"]
    certification_id: Id | None
    isolation_evidence_ref: Id | None
    eligibility: Literal["eligible", "certification_invalid", "isolation_unavailable", "revoked"]
    permission_epoch: Revision
    restart_required: bool

    @model_validator(mode="after")
    def check_eligible_evidence(self) -> HostAdmission:
        if self.eligibility == "eligible":
            if self.mode == "trusted_in_process" and not self.certification_id:
                raise ValueError("certification_invalid")
            if self.mode == "isolated" and not self.isolation_evidence_ref:
                raise ValueError("isolation_unavailable")
        return self


class RpcContext(Contract):
    sdk_version: Literal["operant-memory-sdk.v1"]
    request_id: Id
    installation_id: Id
    dataset_id: Id
    scope: Scope
    deadline: AwareDatetime
    cancel_token: Id
    idempotency_key: Id
    request_digest: Digest
    binding_epoch: Revision
    permission_epoch: Revision
    lease_fencing: Revision


class RecallRequest(Contract):
    context: RpcContext
    query: Annotated[str, Field(min_length=1, max_length=10_000)]
    explicit_refs: tuple[MemoryVersionRef, ...]
    knowledge_cutoff: Cursor
    max_candidates: Annotated[int, Field(ge=1, le=100)]
    token_budget: Annotated[int, Field(ge=0, le=1_000_000)]


class CandidateReference(Contract):
    ref: MemoryVersionRef
    score: Annotated[float, Field(ge=0, le=1)]


class CandidateBatch(Contract):
    request_id: Id
    candidates: Annotated[tuple[CandidateReference, ...], Field(max_length=100)]


class SourceBatch(Contract):
    context: RpcContext
    sources: Annotated[tuple[SourceRef, ...], Field(min_length=1, max_length=100)]
    source_watermark: Cursor


class ProposalBatch(Contract):
    request_id: Id
    proposals: Annotated[tuple[MemoryProposal, ...], Field(max_length=100)]
    source_watermark: Cursor


class MaintenanceInput(Contract):
    context: RpcContext
    job_id: Id
    action: Literal["deduplicate", "conflict_check", "expire", "reindex"]
    source_watermark: Cursor
    budget_tokens: Annotated[int, Field(ge=0, le=1_000_000)]


class IndexEvent(Contract):
    context: RpcContext
    event_id: Id
    head: MemoryHead


class IndexReceipt(Contract):
    event_id: Id
    index_generation: Id
    applied_cursor: Cursor
    outcome: Literal["applied", "duplicate", "stale", "revoked"]


class HostReadRequest(Contract):
    context: RpcContext
    source: SourceRef
    max_bytes: Annotated[int, Field(ge=1, le=100_000)]


class HostReadResult(Contract):
    source: SourceRef
    text: Annotated[str, Field(max_length=100_000)]
    truncated: bool


class ModelProxyRequest(Contract):
    context: RpcContext
    model_profile_id: Id
    input_source_refs: tuple[SourceRef, ...]
    instruction: Annotated[str, Field(min_length=1, max_length=10_000)]
    max_output_tokens: Annotated[int, Field(ge=1, le=16384)]


class ModelProxyResult(Contract):
    request_id: Id
    output: Annotated[str, Field(max_length=100_000)]
    usage_tokens: Annotated[int | None, Field(ge=0)]
    usage_status: Literal["reported", "unknown"]


class LifecycleRequest(Contract):
    context: RpcContext
    operation: Literal["negotiate", "health", "cancel", "checkpoint", "restore"]
    checkpoint_ref: Id | None


class LifecycleResult(Contract):
    request_id: Id
    sdk_version: Literal["operant-memory-sdk.v1"]
    state: Literal["ready", "cancelled", "checkpointed", "restored", "unsupported", "failed"]
    checkpoint_ref: Id | None


class PrivateIndexResource(Resource):
    category: Literal["index"]
    storage: Literal["core_rows", "managed_directory"]


class PrivateIndexRequest(Contract):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "allOf": [
                {
                    "if": {"properties": {"operation": {"const": "replace"}}},
                    "then": {
                        "properties": {
                            "payload": {"type": "string"},
                            "content_digest": {"type": "string"},
                        }
                    },
                    "else": {
                        "properties": {
                            "payload": {"type": "null"},
                            "content_digest": {"type": "null"},
                        }
                    },
                }
            ]
        },
    )
    context: RpcContext
    resource: PrivateIndexResource
    operation: Literal["read", "replace", "delete"]
    expected_revision: Revision
    content_digest: Digest | None
    payload: Annotated[str | None, Field(max_length=100_000)]

    @model_validator(mode="after")
    def check_payload_and_owner(self) -> PrivateIndexRequest:
        if (
            self.resource.owner.dataset_id != self.context.dataset_id
            or self.resource.installation_id != self.context.installation_id
        ):
            raise ValueError("unknown_owner: private index must belong to the calling binding")
        if self.operation == "replace":
            if self.payload is None or self.content_digest is None:
                raise ValueError("replace requires payload and content digest")
            if hashlib.sha256(self.payload.encode("utf-8")).hexdigest() != self.content_digest:
                raise ValueError("private index payload digest mismatch")
        elif self.payload is not None or self.content_digest is not None:
            raise ValueError("read/delete cannot carry a write payload or digest")
        return self


class PrivateIndexResult(Contract):
    resource_id: Id
    revision: Revision
    content_digest: Digest | None
    payload: Annotated[str | None, Field(max_length=100_000)]


class PluginConfig(Contract):
    schema_version: Literal["operant-memory-config.v1"]
    config_id: Id
    revision: Revision
    extraction_model_profile_id: Id | None
    rerank_model_profile_id: Id | None
    recall_token_budget: Annotated[int, Field(ge=0, le=1_000_000)]
    maintenance_enabled: bool
    scheduler_definition_id: Id | None
    secret_refs: dict[Id, SecretRef]
    effective_at: AwareDatetime


class ProjectionQuery(Contract):
    scope: Scope
    kinds: tuple[Literal["project", "workspace", "task", "agent", "dataset", "host"], ...]
    cursor: Cursor | None
    limit: Annotated[int, Field(ge=1, le=100)]


class ProjectionPage(Contract):
    projects: tuple[ProjectIdentity, ...]
    workspaces: tuple[WorkspaceIdentity, ...]
    tasks: tuple[TaskProjection, ...]
    agents: tuple[AgentBinding, ...]
    datasets: tuple[Dataset, ...]
    hosts: tuple[HostAdmission, ...]
    cursor: Cursor
    next_cursor: Cursor | None


class BindingChangeRequest(Contract):
    binding_id: Id
    operation: Literal["enable", "disable"]
    expected_binding_epoch: Revision
    installation_id: Id
    dataset_id: Id
    config_id: Id
    idempotency_key: Id


class ContractError(Contract):
    code: Literal[
        "unsupported",
        "unknown_scope",
        "unknown_owner",
        "permission_denied",
        "source_deleted",
        "revision_conflict",
        "stale_epoch",
        "lease_expired",
        "deadline_exceeded",
        "cancelled",
        "idempotency_conflict",
        "certification_invalid",
        "isolation_unavailable",
        "restart_required",
        "cleanup_blocked",
        "dataset_retained",
        "protocol_mismatch",
        "outcome_unknown",
        "upgrade_required",
        "budget_exceeded",
        "package_unavailable",
    ]
    request_id: Id
    retry: Literal["never", "same_key_after_query", "new_revision", "after_restart"]
    detail_ref: Id | None


# This registry generates separate offline Client interfaces, never HTTP routes.
APP_OPERATIONS = {
    "query_projections": (ProjectionQuery, ProjectionPage),
    "change_binding": (BindingChangeRequest, LifecycleReceipt),
    "publish": (PublishRequest, MemoryHead),
    "uninstall": (UninstallRequest, LifecycleReceipt),
    "transfer_dataset": (DatasetTransfer, Dataset),
}
ENGINE_OPERATIONS = {
    "extract": (SourceBatch, ProposalBatch),
    "recall": (RecallRequest, CandidateBatch),
    "maintain": (MaintenanceInput, ProposalBatch),
    "on_index_event": (IndexEvent, IndexReceipt),
    "lifecycle": (LifecycleRequest, LifecycleResult),
}
HOST_OPERATIONS = {
    "read_source": (HostReadRequest, HostReadResult),
    "search": (RecallRequest, CandidateBatch),
    "model": (ModelProxyRequest, ModelProxyResult),
    "private_index": (PrivateIndexRequest, PrivateIndexResult),
}
