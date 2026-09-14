"""Generated offline interfaces. No runtime transport. DO NOT EDIT."""

# fmt: off
# ruff: noqa: E501, I001, F401
from __future__ import annotations
from typing import Any, Literal, Protocol
from typing_extensions import NotRequired, TypedDict
CONTRACT_VERSION = 'operant-memory-sdk.v1'
SCHEMA_DIGEST = 'eb71eefe97998fa17e3b9d1d538f030c3be569e14ce312d2e26259566fe0f894'

class AgentBinding(TypedDict):
    role_preset_id: str
    role_version: int
    definition_revision_id: str | None
    snapshot_id: str
    agent_instance_id: str
    session_id: str
    run_id: str | None
    model_profile_id: str
    provider_id: str
    provider_model_id: str
    capability_digest: str

class BindingChangeRequest(TypedDict):
    binding_id: str
    operation: Literal['enable', 'disable']
    expected_binding_epoch: int
    installation_id: str
    dataset_id: str
    config_id: str
    idempotency_key: str

class CandidateBatch(TypedDict):
    request_id: str
    candidates: list[CandidateReference]

class CandidateReference(TypedDict):
    ref: MemoryVersionRef
    score: float

class Certification(TypedDict):
    certification_id: str
    issuer_id: str
    plugin_id: str
    plugin_version: str
    package_digest: str
    dependencies_digest: str
    permissions_digest: str
    lifecycle_evidence_digest: str
    allowed_modes: list[Literal['trusted_in_process', 'isolated']]
    valid_from: str
    expires_at: str
    revocation_epoch: int
    state: Literal['valid', 'revoked', 'expired']
    signature_ref: str

class CleanupItem(TypedDict):
    resource_id: str
    outcome: Literal['pending', 'deleted', 'retained', 'blocked', 'external_unconfirmed']
    reason: Literal['exclusive', 'keep_selected', 'shared_consumer', 'retention_lock', 'active_run', 'directory_identity_changed', 'external_resource', 'retry_required']
    blocker_ids: list[str]

class CommitGuard(TypedDict):
    binding_id: str
    binding_epoch: int
    permission_epoch: int
    certification_id: str
    certification_epoch: int
    lease_id: str
    lease_fencing: int
    expected_head_revision: int
    idempotency_key: str
    request_digest: str

class ContextMemoryUse(TypedDict):
    context_revision_id: str
    manifest_id: str
    actual_versions: list[MemoryVersionRef]
    permission_epoch: int
    binding_epoch: int
    derived_compaction_ids: list[str]
    refresh: Literal['frozen', 'explicit_refresh', 'revocation_rebuild', 'blocked']

class ContractError(TypedDict):
    code: Literal['unsupported', 'unknown_scope', 'unknown_owner', 'permission_denied', 'source_deleted', 'revision_conflict', 'stale_epoch', 'lease_expired', 'deadline_exceeded', 'cancelled', 'idempotency_conflict', 'certification_invalid', 'isolation_unavailable', 'restart_required', 'cleanup_blocked', 'dataset_retained', 'protocol_mismatch', 'outcome_unknown', 'upgrade_required', 'budget_exceeded', 'package_unavailable']
    request_id: str
    retry: Literal['never', 'same_key_after_query', 'new_revision', 'after_restart']
    detail_ref: str | None

class Dataset(TypedDict):
    owner: DatasetOwner
    installation_id: str | None
    state: Literal['bound', 'retained', 'deleting', 'deleted', 'blocked']
    revision: int
    consumers: list[DatasetConsumer]
    retention_lock_ids: list[str]
    management_actions: list[Literal['inspect', 'export', 'delete', 'rebind']]

class DatasetConsumer(TypedDict):
    consumer_id: str
    installation_id: str
    grant_id: str
    purpose: Literal['primary_engine', 'shared_read', 'export', 'migration']

class DatasetOwner(TypedDict):
    kind: Literal['plugin_dataset']
    owner_namespace: str
    dataset_id: str
    principal_id: str

class DatasetTransfer(TypedDict):
    dataset_id: str
    destination_dataset_id: str
    expected_revision: int
    from_namespace: str
    to_namespace: str
    destination_installation_id: str
    authorization_grant_id: str
    mode: Literal['transfer', 'authorized_copy']
    idempotency_key: str

class EffectiveSetting(TypedDict):
    key: str
    value_ref: str
    source: Literal['global', 'project', 'agent', 'run']
    source_id: str
    revision: int
    effective_at: str
    applies_to: Literal['new_runs', 'next_request', 'immediate_revocation']

class Grant(TypedDict):
    grant_id: str
    subject_id: str
    grantor_id: str
    scope: WorkspaceScope | SessionScope | RunScope | PersonalScope
    purpose: Literal['recall', 'source_read', 'publish', 'share', 'transfer', 'delete']
    policy_revision: int
    permission_epoch: int
    expires_at: str

class HostAdmission(TypedDict):
    host_instance_id: str
    installation_id: str
    mode: Literal['trusted_in_process', 'isolated']
    certification_id: str | None
    isolation_evidence_ref: str | None
    eligibility: Literal['eligible', 'certification_invalid', 'isolation_unavailable', 'revoked']
    permission_epoch: int
    restart_required: bool

class HostReadRequest(TypedDict):
    context: RpcContext
    source: SourceRef
    max_bytes: int

class HostReadResult(TypedDict):
    source: SourceRef
    text: str
    truncated: bool

class IndexEvent(TypedDict):
    context: RpcContext
    event_id: str
    head: MemoryHead

class IndexReceipt(TypedDict):
    event_id: str
    index_generation: str
    applied_cursor: str
    outcome: Literal['applied', 'duplicate', 'stale', 'revoked']

class LifecycleReceipt(TypedDict):
    operation_id: str
    installation_id: str
    state: Literal['enabled', 'disabling', 'disabled', 'uninstalling', 'uninstalled', 'failed', 'restart_required', 'blocked']
    binding_epoch: int
    inventory_revision: int
    cleanup: list[CleanupItem]
    cursor: str
    ack: Literal['host_accepted', 'completed', 'failed', 'unknown']

class LifecycleRequest(TypedDict):
    context: RpcContext
    operation: Literal['negotiate', 'health', 'cancel', 'checkpoint', 'restore']
    checkpoint_ref: str | None

class LifecycleResult(TypedDict):
    request_id: str
    sdk_version: Literal['operant-memory-sdk.v1']
    state: Literal['ready', 'cancelled', 'checkpointed', 'restored', 'unsupported', 'failed']
    checkpoint_ref: str | None

class MaintenanceInput(TypedDict):
    context: RpcContext
    job_id: str
    action: Literal['deduplicate', 'conflict_check', 'expire', 'reindex']
    source_watermark: str
    budget_tokens: int

class MemoryConditions(TypedDict):
    commit_ref: str | None
    tree_digest: str | None
    file_fingerprints: dict[str, Any]
    environment_digest: str | None
    tool_versions: dict[str, Any]
    verified_at: str | None
    valid_from: str
    valid_until: str | None

class MemoryDisabled(TypedDict):
    state: Literal['disabled']
    binding_id: str
    binding_epoch: int
    cause: Literal['global_off', 'binding_off', 'not_installed', 'revoked', 'failed']

class MemoryEnabled(TypedDict):
    state: Literal['enabled']
    binding_id: str
    binding_epoch: int
    installation_id: str
    dataset_id: str
    config_digest: str
    package_digest: str
    certification_id: str
    index_generation: str
    global_enabled: Literal[True]

class MemoryHead(TypedDict):
    dataset_id: str
    record_id: str
    published_version: MemoryVersionRef | None
    revision: int
    publication_cursor: str
    state: Literal['unpublished', 'published', 'inactive', 'revoked', 'deleted']
    permission_epoch: int

class MemoryPack(TypedDict):
    manifest_id: str
    manifest_digest: str
    scope: WorkspaceScope | SessionScope | RunScope | PersonalScope
    run_id: str
    agent_instance_id: str
    binding: MemoryEnabled
    knowledge_cutoff: str
    policy_revision: int
    permission_epoch: int
    selected: list[MemoryVersionRef]
    total_token_budget: int
    token_count: int
    selection: Literal['automatic_and_explicit']

class MemoryProposal(TypedDict):
    proposal_id: str
    proposal_revision: int
    owner: DatasetOwner
    operation: Literal['create', 'modify', 'merge', 'supersede', 'revoke']
    base_head: MemoryHead
    proposed_version: MemoryVersionRef
    source_refs: list[SourceRef]
    extractor_version: str
    reason: str
    state: Literal['pending', 'accepted', 'rejected', 'conflict', 'cancelled']

class MemoryVersion(TypedDict):
    ref: MemoryVersionRef
    owner: DatasetOwner
    kind: Literal['working', 'episodic', 'project']
    content_type: Literal['fact', 'preference', 'episode', 'procedure']
    scope: WorkspaceScope | SessionScope | RunScope | PersonalScope
    role_ids: list[str]
    agent_ids: list[str]
    content: str
    sources: list[SourceRef]
    evidence: Literal['user_asserted', 'observed', 'tested', 'inferred', 'legacy_unverified']
    sensitivity: Literal['public', 'internal', 'sensitive']
    retention_policy_id: str
    conditions: MemoryConditions
    recorded_at: str

class MemoryVersionRef(TypedDict):
    dataset_id: str
    record_id: str
    version: int
    content_digest: str

class ModelProxyRequest(TypedDict):
    context: RpcContext
    model_profile_id: str
    input_source_refs: list[SourceRef]
    instruction: str
    max_output_tokens: int

class ModelProxyResult(TypedDict):
    request_id: str
    output: str
    usage_tokens: int | None
    usage_status: Literal['reported', 'unknown']

class PersonalScope(TypedDict):
    kind: Literal['personal']
    principal_id: str
    opt_in_grant_id: str

class PluginConfig(TypedDict):
    schema_version: Literal['operant-memory-config.v1']
    config_id: str
    revision: int
    extraction_model_profile_id: str | None
    rerank_model_profile_id: str | None
    recall_token_budget: int
    maintenance_enabled: bool
    scheduler_definition_id: str | None
    secret_refs: dict[str, Any]
    effective_at: str

class PluginManifest(TypedDict):
    plugin_id: str
    plugin_version: str
    sdk_version: Literal['operant-memory-sdk.v1']
    host_api_versions: list[Literal['operant-memory-sdk.v1']]
    package_digest: str
    dependencies_digest: str
    permissions_digest: str
    entrypoint: str
    config_schema_ref: str
    config_schema_digest: str
    state_schema_version: str
    capabilities: list[Literal['extract', 'recall', 'maintain', 'on_index_event']]
    memory_mb: int
    max_rpc_bytes: int
    max_concurrency: int
    export_supported: bool
    import_supported: bool
    recoverable: bool

class PrivateIndexRequest(TypedDict):
    context: RpcContext
    resource: PrivateIndexResource
    operation: Literal['read', 'replace', 'delete']
    expected_revision: int
    content_digest: str | None
    payload: str | None

class PrivateIndexResource(TypedDict):
    resource_id: str
    owner: DatasetOwner
    installation_id: str
    storage: Literal['core_rows', 'managed_directory']
    category: Literal['index']
    locator_ref: str
    consumer_ids: list[str]
    retention_lock_ids: list[str]
    reconstructible: bool

class PrivateIndexResult(TypedDict):
    resource_id: str
    revision: int
    content_digest: str | None
    payload: str | None

class ProjectIdentity(TypedDict):
    project_id: str
    display_name: str
    revision: int
    workspace_ids: list[str]
    sharing: Literal['explicit_grants_only']

class ProjectionPage(TypedDict):
    projects: list[ProjectIdentity]
    workspaces: list[WorkspaceIdentity]
    tasks: list[TaskProjection]
    agents: list[AgentBinding]
    datasets: list[Dataset]
    hosts: list[HostAdmission]
    cursor: str
    next_cursor: str | None

class ProjectionQuery(TypedDict):
    scope: WorkspaceScope | SessionScope | RunScope | PersonalScope
    kinds: list[Literal['project', 'workspace', 'task', 'agent', 'dataset', 'host']]
    cursor: str | None
    limit: int

class ProposalBatch(TypedDict):
    request_id: str
    proposals: list[MemoryProposal]
    source_watermark: str

class PublishRequest(TypedDict):
    proposal_id: str
    proposal_revision: int
    target: MemoryVersionRef
    guard: CommitGuard

class RecallRequest(TypedDict):
    context: RpcContext
    query: str
    explicit_refs: list[MemoryVersionRef]
    knowledge_cutoff: str
    max_candidates: int
    token_budget: int

class Resource(TypedDict):
    resource_id: str
    owner: DatasetOwner
    installation_id: str
    storage: Literal['core_rows', 'managed_directory', 'shared_dependency', 'external']
    category: Literal['package', 'config', 'state', 'index', 'cache', 'tmp', 'log', 'record', 'version', 'proposal', 'relation', 'observation', 'manifest', 'job', 'subscription']
    locator_ref: str
    consumer_ids: list[str]
    retention_lock_ids: list[str]
    reconstructible: bool

class RpcContext(TypedDict):
    sdk_version: Literal['operant-memory-sdk.v1']
    request_id: str
    installation_id: str
    dataset_id: str
    scope: WorkspaceScope | SessionScope | RunScope | PersonalScope
    deadline: str
    cancel_token: str
    idempotency_key: str
    request_digest: str
    binding_epoch: int
    permission_epoch: int
    lease_fencing: int

class RunScope(TypedDict):
    kind: Literal['run']
    project_id: str
    workspace_id: str
    run_id: str
    writer_id: str | None

class SessionScope(TypedDict):
    kind: Literal['session']
    project_id: str
    workspace_id: str
    session_id: str

class SourceBatch(TypedDict):
    context: RpcContext
    sources: list[SourceRef]
    source_watermark: str

class SourceRef(TypedDict):
    source_type: Literal['item', 'artifact', 'memory_version', 'mailbox_message', 'workflow_event']
    source_id: str
    revision: int
    content_digest: str
    scope: WorkspaceScope | SessionScope | RunScope | PersonalScope
    permission_epoch: int
    availability: Literal['available', 'deleted', 'revoked', 'unavailable']

class TaskAction(TypedDict):
    action: Literal['inspect', 'cancel', 'resume', 'approve', 'archive']
    availability: Literal['available', 'blocked', 'unsupported']
    reason_code: str | None
    projection_revision: int

class TaskProjection(TypedDict):
    source: TaskSource
    project_id: str
    workspace_id: str
    source_status: str
    revision: int
    actions: list[TaskAction]

class TaskSource(TypedDict):
    source_type: Literal['session', 'workflow_run', 'team_task']
    source_id: str

class Tombstone(TypedDict):
    dataset_id: str
    record_id: str | None
    state: Literal['deleted', 'revoked', 'source_deleted']
    revision: int
    cursor: str

class UninstallRequest(TypedDict):
    installation_id: str
    data_policy: Literal['keep', 'delete']
    inventory_revision: int
    expected_binding_epoch: int
    idempotency_key: str
    stop_run_ids: list[str]

class WorkspaceIdentity(TypedDict):
    workspace_id: str
    project_id: str
    workspace_ref: str
    worktree_id: str | None
    association_revision: int
    association: Literal['explicit_registration']

class WorkspaceScope(TypedDict):
    kind: Literal['workspace']
    project_id: str
    workspace_id: str

class MemoryEngine(Protocol):
    async def extract(self, request: SourceBatch) -> ProposalBatch | ContractError: ...
    async def lifecycle(self, request: LifecycleRequest) -> LifecycleResult | ContractError: ...
    async def maintain(self, request: MaintenanceInput) -> ProposalBatch | ContractError: ...
    async def on_index_event(self, request: IndexEvent) -> IndexReceipt | ContractError: ...
    async def recall(self, request: RecallRequest) -> CandidateBatch | ContractError: ...

class MemoryHostApi(Protocol):
    async def model(self, request: ModelProxyRequest) -> ModelProxyResult | ContractError: ...
    async def private_index(self, request: PrivateIndexRequest) -> PrivateIndexResult | ContractError: ...
    async def read_source(self, request: HostReadRequest) -> HostReadResult | ContractError: ...
    async def search(self, request: RecallRequest) -> CandidateBatch | ContractError: ...
