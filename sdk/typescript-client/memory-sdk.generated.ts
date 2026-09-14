// Generated offline interfaces. No runtime transport. DO NOT EDIT.
export const CONTRACT_VERSION = "operant-memory-sdk.v1" as const;
export const SCHEMA_DIGEST = "eb71eefe97998fa17e3b9d1d538f030c3be569e14ce312d2e26259566fe0f894" as const;

export interface AgentBinding {
  role_preset_id: string;
  role_version: number;
  definition_revision_id: string | null;
  snapshot_id: string;
  agent_instance_id: string;
  session_id: string;
  run_id: string | null;
  model_profile_id: string;
  provider_id: string;
  provider_model_id: string;
  capability_digest: string;
}

export interface BindingChangeRequest {
  binding_id: string;
  operation: "enable" | "disable";
  expected_binding_epoch: number;
  installation_id: string;
  dataset_id: string;
  config_id: string;
  idempotency_key: string;
}

export interface CandidateBatch {
  request_id: string;
  candidates: Array<CandidateReference>;
}

export interface CandidateReference {
  ref: MemoryVersionRef;
  score: number;
}

export interface Certification {
  certification_id: string;
  issuer_id: string;
  plugin_id: string;
  plugin_version: string;
  package_digest: string;
  dependencies_digest: string;
  permissions_digest: string;
  lifecycle_evidence_digest: string;
  allowed_modes: Array<"trusted_in_process" | "isolated">;
  valid_from: string;
  expires_at: string;
  revocation_epoch: number;
  state: "valid" | "revoked" | "expired";
  signature_ref: string;
}

export interface CleanupItem {
  resource_id: string;
  outcome: "pending" | "deleted" | "retained" | "blocked" | "external_unconfirmed";
  reason: "exclusive" | "keep_selected" | "shared_consumer" | "retention_lock" | "active_run" | "directory_identity_changed" | "external_resource" | "retry_required";
  blocker_ids: Array<string>;
}

export interface CommitGuard {
  binding_id: string;
  binding_epoch: number;
  permission_epoch: number;
  certification_id: string;
  certification_epoch: number;
  lease_id: string;
  lease_fencing: number;
  expected_head_revision: number;
  idempotency_key: string;
  request_digest: string;
}

export interface ContextMemoryUse {
  context_revision_id: string;
  manifest_id: string;
  actual_versions: Array<MemoryVersionRef>;
  permission_epoch: number;
  binding_epoch: number;
  derived_compaction_ids: Array<string>;
  refresh: "frozen" | "explicit_refresh" | "revocation_rebuild" | "blocked";
}

export interface ContractError {
  code: "unsupported" | "unknown_scope" | "unknown_owner" | "permission_denied" | "source_deleted" | "revision_conflict" | "stale_epoch" | "lease_expired" | "deadline_exceeded" | "cancelled" | "idempotency_conflict" | "certification_invalid" | "isolation_unavailable" | "restart_required" | "cleanup_blocked" | "dataset_retained" | "protocol_mismatch" | "outcome_unknown" | "upgrade_required" | "budget_exceeded" | "package_unavailable";
  request_id: string;
  retry: "never" | "same_key_after_query" | "new_revision" | "after_restart";
  detail_ref: string | null;
}

export interface Dataset {
  owner: DatasetOwner;
  installation_id: string | null;
  state: "bound" | "retained" | "deleting" | "deleted" | "blocked";
  revision: number;
  consumers: Array<DatasetConsumer>;
  retention_lock_ids: Array<string>;
  management_actions: Array<"inspect" | "export" | "delete" | "rebind">;
}

export interface DatasetConsumer {
  consumer_id: string;
  installation_id: string;
  grant_id: string;
  purpose: "primary_engine" | "shared_read" | "export" | "migration";
}

export interface DatasetOwner {
  kind: "plugin_dataset";
  owner_namespace: string;
  dataset_id: string;
  principal_id: string;
}

export interface DatasetTransfer {
  dataset_id: string;
  destination_dataset_id: string;
  expected_revision: number;
  from_namespace: string;
  to_namespace: string;
  destination_installation_id: string;
  authorization_grant_id: string;
  mode: "transfer" | "authorized_copy";
  idempotency_key: string;
}

export interface EffectiveSetting {
  key: string;
  value_ref: string;
  source: "global" | "project" | "agent" | "run";
  source_id: string;
  revision: number;
  effective_at: string;
  applies_to: "new_runs" | "next_request" | "immediate_revocation";
}

export interface Grant {
  grant_id: string;
  subject_id: string;
  grantor_id: string;
  scope: WorkspaceScope | SessionScope | RunScope | PersonalScope;
  purpose: "recall" | "source_read" | "publish" | "share" | "transfer" | "delete";
  policy_revision: number;
  permission_epoch: number;
  expires_at: string;
}

export interface HostAdmission {
  host_instance_id: string;
  installation_id: string;
  mode: "trusted_in_process" | "isolated";
  certification_id: string | null;
  isolation_evidence_ref: string | null;
  eligibility: "eligible" | "certification_invalid" | "isolation_unavailable" | "revoked";
  permission_epoch: number;
  restart_required: boolean;
}

export interface HostReadRequest {
  context: RpcContext;
  source: SourceRef;
  max_bytes: number;
}

export interface HostReadResult {
  source: SourceRef;
  text: string;
  truncated: boolean;
}

export interface IndexEvent {
  context: RpcContext;
  event_id: string;
  head: MemoryHead;
}

export interface IndexReceipt {
  event_id: string;
  index_generation: string;
  applied_cursor: string;
  outcome: "applied" | "duplicate" | "stale" | "revoked";
}

export interface LifecycleReceipt {
  operation_id: string;
  installation_id: string;
  state: "enabled" | "disabling" | "disabled" | "uninstalling" | "uninstalled" | "failed" | "restart_required" | "blocked";
  binding_epoch: number;
  inventory_revision: number;
  cleanup: Array<CleanupItem>;
  cursor: string;
  ack: "host_accepted" | "completed" | "failed" | "unknown";
}

export interface LifecycleRequest {
  context: RpcContext;
  operation: "negotiate" | "health" | "cancel" | "checkpoint" | "restore";
  checkpoint_ref: string | null;
}

export interface LifecycleResult {
  request_id: string;
  sdk_version: "operant-memory-sdk.v1";
  state: "ready" | "cancelled" | "checkpointed" | "restored" | "unsupported" | "failed";
  checkpoint_ref: string | null;
}

export interface MaintenanceInput {
  context: RpcContext;
  job_id: string;
  action: "deduplicate" | "conflict_check" | "expire" | "reindex";
  source_watermark: string;
  budget_tokens: number;
}

export interface MemoryConditions {
  commit_ref: string | null;
  tree_digest: string | null;
  file_fingerprints: Record<string, unknown>;
  environment_digest: string | null;
  tool_versions: Record<string, unknown>;
  verified_at: string | null;
  valid_from: string;
  valid_until: string | null;
}

export interface MemoryDisabled {
  state: "disabled";
  binding_id: string;
  binding_epoch: number;
  cause: "global_off" | "binding_off" | "not_installed" | "revoked" | "failed";
}

export interface MemoryEnabled {
  state: "enabled";
  binding_id: string;
  binding_epoch: number;
  installation_id: string;
  dataset_id: string;
  config_digest: string;
  package_digest: string;
  certification_id: string;
  index_generation: string;
  global_enabled: true;
}

export interface MemoryHead {
  dataset_id: string;
  record_id: string;
  published_version: MemoryVersionRef | null;
  revision: number;
  publication_cursor: string;
  state: "unpublished" | "published" | "inactive" | "revoked" | "deleted";
  permission_epoch: number;
}

export interface MemoryPack {
  manifest_id: string;
  manifest_digest: string;
  scope: WorkspaceScope | SessionScope | RunScope | PersonalScope;
  run_id: string;
  agent_instance_id: string;
  binding: MemoryEnabled;
  knowledge_cutoff: string;
  policy_revision: number;
  permission_epoch: number;
  selected: Array<MemoryVersionRef>;
  total_token_budget: number;
  token_count: number;
  selection: "automatic_and_explicit";
}

export interface MemoryProposal {
  proposal_id: string;
  proposal_revision: number;
  owner: DatasetOwner;
  operation: "create" | "modify" | "merge" | "supersede" | "revoke";
  base_head: MemoryHead;
  proposed_version: MemoryVersionRef;
  source_refs: Array<SourceRef>;
  extractor_version: string;
  reason: string;
  state: "pending" | "accepted" | "rejected" | "conflict" | "cancelled";
}

export interface MemoryVersion {
  ref: MemoryVersionRef;
  owner: DatasetOwner;
  kind: "working" | "episodic" | "project";
  content_type: "fact" | "preference" | "episode" | "procedure";
  scope: WorkspaceScope | SessionScope | RunScope | PersonalScope;
  role_ids: Array<string>;
  agent_ids: Array<string>;
  content: string;
  sources: Array<SourceRef>;
  evidence: "user_asserted" | "observed" | "tested" | "inferred" | "legacy_unverified";
  sensitivity: "public" | "internal" | "sensitive";
  retention_policy_id: string;
  conditions: MemoryConditions;
  recorded_at: string;
}

export interface MemoryVersionRef {
  dataset_id: string;
  record_id: string;
  version: number;
  content_digest: string;
}

export interface ModelProxyRequest {
  context: RpcContext;
  model_profile_id: string;
  input_source_refs: Array<SourceRef>;
  instruction: string;
  max_output_tokens: number;
}

export interface ModelProxyResult {
  request_id: string;
  output: string;
  usage_tokens: number | null;
  usage_status: "reported" | "unknown";
}

export interface PersonalScope {
  kind: "personal";
  principal_id: string;
  opt_in_grant_id: string;
}

export interface PluginConfig {
  schema_version: "operant-memory-config.v1";
  config_id: string;
  revision: number;
  extraction_model_profile_id: string | null;
  rerank_model_profile_id: string | null;
  recall_token_budget: number;
  maintenance_enabled: boolean;
  scheduler_definition_id: string | null;
  secret_refs: Record<string, unknown>;
  effective_at: string;
}

export interface PluginManifest {
  plugin_id: string;
  plugin_version: string;
  sdk_version: "operant-memory-sdk.v1";
  host_api_versions: Array<"operant-memory-sdk.v1">;
  package_digest: string;
  dependencies_digest: string;
  permissions_digest: string;
  entrypoint: string;
  config_schema_ref: string;
  config_schema_digest: string;
  state_schema_version: string;
  capabilities: Array<"extract" | "recall" | "maintain" | "on_index_event">;
  memory_mb: number;
  max_rpc_bytes: number;
  max_concurrency: number;
  export_supported: boolean;
  import_supported: boolean;
  recoverable: boolean;
}

export interface PrivateIndexRequest {
  context: RpcContext;
  resource: PrivateIndexResource;
  operation: "read" | "replace" | "delete";
  expected_revision: number;
  content_digest: string | null;
  payload: string | null;
}

export interface PrivateIndexResource {
  resource_id: string;
  owner: DatasetOwner;
  installation_id: string;
  storage: "core_rows" | "managed_directory";
  category: "index";
  locator_ref: string;
  consumer_ids: Array<string>;
  retention_lock_ids: Array<string>;
  reconstructible: boolean;
}

export interface PrivateIndexResult {
  resource_id: string;
  revision: number;
  content_digest: string | null;
  payload: string | null;
}

export interface ProjectIdentity {
  project_id: string;
  display_name: string;
  revision: number;
  workspace_ids: Array<string>;
  sharing: "explicit_grants_only";
}

export interface ProjectionPage {
  projects: Array<ProjectIdentity>;
  workspaces: Array<WorkspaceIdentity>;
  tasks: Array<TaskProjection>;
  agents: Array<AgentBinding>;
  datasets: Array<Dataset>;
  hosts: Array<HostAdmission>;
  cursor: string;
  next_cursor: string | null;
}

export interface ProjectionQuery {
  scope: WorkspaceScope | SessionScope | RunScope | PersonalScope;
  kinds: Array<"project" | "workspace" | "task" | "agent" | "dataset" | "host">;
  cursor: string | null;
  limit: number;
}

export interface ProposalBatch {
  request_id: string;
  proposals: Array<MemoryProposal>;
  source_watermark: string;
}

export interface PublishRequest {
  proposal_id: string;
  proposal_revision: number;
  target: MemoryVersionRef;
  guard: CommitGuard;
}

export interface RecallRequest {
  context: RpcContext;
  query: string;
  explicit_refs: Array<MemoryVersionRef>;
  knowledge_cutoff: string;
  max_candidates: number;
  token_budget: number;
}

export interface Resource {
  resource_id: string;
  owner: DatasetOwner;
  installation_id: string;
  storage: "core_rows" | "managed_directory" | "shared_dependency" | "external";
  category: "package" | "config" | "state" | "index" | "cache" | "tmp" | "log" | "record" | "version" | "proposal" | "relation" | "observation" | "manifest" | "job" | "subscription";
  locator_ref: string;
  consumer_ids: Array<string>;
  retention_lock_ids: Array<string>;
  reconstructible: boolean;
}

export interface RpcContext {
  sdk_version: "operant-memory-sdk.v1";
  request_id: string;
  installation_id: string;
  dataset_id: string;
  scope: WorkspaceScope | SessionScope | RunScope | PersonalScope;
  deadline: string;
  cancel_token: string;
  idempotency_key: string;
  request_digest: string;
  binding_epoch: number;
  permission_epoch: number;
  lease_fencing: number;
}

export interface RunScope {
  kind: "run";
  project_id: string;
  workspace_id: string;
  run_id: string;
  writer_id: string | null;
}

export interface SessionScope {
  kind: "session";
  project_id: string;
  workspace_id: string;
  session_id: string;
}

export interface SourceBatch {
  context: RpcContext;
  sources: Array<SourceRef>;
  source_watermark: string;
}

export interface SourceRef {
  source_type: "item" | "artifact" | "memory_version" | "mailbox_message" | "workflow_event";
  source_id: string;
  revision: number;
  content_digest: string;
  scope: WorkspaceScope | SessionScope | RunScope | PersonalScope;
  permission_epoch: number;
  availability: "available" | "deleted" | "revoked" | "unavailable";
}

export interface TaskAction {
  action: "inspect" | "cancel" | "resume" | "approve" | "archive";
  availability: "available" | "blocked" | "unsupported";
  reason_code: string | null;
  projection_revision: number;
}

export interface TaskProjection {
  source: TaskSource;
  project_id: string;
  workspace_id: string;
  source_status: string;
  revision: number;
  actions: Array<TaskAction>;
}

export interface TaskSource {
  source_type: "session" | "workflow_run" | "team_task";
  source_id: string;
}

export interface Tombstone {
  dataset_id: string;
  record_id: string | null;
  state: "deleted" | "revoked" | "source_deleted";
  revision: number;
  cursor: string;
}

export interface UninstallRequest {
  installation_id: string;
  data_policy: "keep" | "delete";
  inventory_revision: number;
  expected_binding_epoch: number;
  idempotency_key: string;
  stop_run_ids: Array<string>;
}

export interface WorkspaceIdentity {
  workspace_id: string;
  project_id: string;
  workspace_ref: string;
  worktree_id: string | null;
  association_revision: number;
  association: "explicit_registration";
}

export interface WorkspaceScope {
  kind: "workspace";
  project_id: string;
  workspace_id: string;
}

export interface MemoryEngine {
  extract(request: SourceBatch): Promise<ProposalBatch | ContractError>;
  lifecycle(request: LifecycleRequest): Promise<LifecycleResult | ContractError>;
  maintain(request: MaintenanceInput): Promise<ProposalBatch | ContractError>;
  on_index_event(request: IndexEvent): Promise<IndexReceipt | ContractError>;
  recall(request: RecallRequest): Promise<CandidateBatch | ContractError>;
}

export interface MemoryHostApi {
  model(request: ModelProxyRequest): Promise<ModelProxyResult | ContractError>;
  private_index(request: PrivateIndexRequest): Promise<PrivateIndexResult | ContractError>;
  read_source(request: HostReadRequest): Promise<HostReadResult | ContractError>;
  search(request: RecallRequest): Promise<CandidateBatch | ContractError>;
}
