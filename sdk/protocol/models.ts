/**
 * Operant 2.0 Protocol Domain Models
 * Authoritative Type Definitions for Web, Desktop, TUI and Core Clients
 */

export type Effort = 'low' | 'medium' | 'high';
export type RoleStatus = 'active' | 'inactive';
export type AgentStatus = 'created' | 'running' | 'completed' | 'failed' | 'cancelled' | 'timed_out';
export type CommandRunnerType = 'host' | 'docker';

export interface CommandExecutionPolicy {
  runner: CommandRunnerType;
  docker_image: string;
  cpu_limit: number;
  memory_limit_mb: number;
  pids_limit: number;
}

export interface ToolPolicy {
  allowed_tools: string[];
  workspace_write: boolean;
  command_execution: boolean;
  approval_required: string[];
  command_execution_policy?: CommandExecutionPolicy;
}

export interface Budget {
  max_turns: number;
  max_consecutive_test_failures: number;
  timeout_seconds: number;
  max_output_tokens?: number;
  max_cost_usd?: number;
}

export interface EffortMapping {
  effort: Effort;
  provider_value: string;
}

export interface ModelProfile {
  id: string;
  name: string;
  provider: string;
  model_id: string;
  base_url: string;
  secret_ref: string;
  context_window?: number;
  default_token_budget?: number;
  supported_efforts: Effort[];
  default_effort: Effort;
  effort_parameter?: string;
  effort_mapping: EffortMapping[];
  enabled: boolean;
  created_at: string;
}

export interface RolePreset {
  id: string;
  version: number;
  name: string;
  system_prompt: string;
  model_profile_id: string;
  effort: Effort;
  tool_policy: ToolPolicy;
  budget: Budget;
  memory_scope: string;
  status: RoleStatus;
  created_at: string;
}

export interface SnapshotOverrides {
  effort_overridden: boolean;
  model_profile_overridden: boolean;
  budget_fields: string[];
}

export interface RoleSnapshot {
  role_id: string;
  role_version: number;
  role_name: string;
  system_prompt: string;
  model_profile_id: string;
  model_profile_name: string;
  provider: string;
  model_id: string;
  base_url: string;
  secret_ref: string;
  effort: Effort;
  provider_effort_parameter?: string;
  provider_effort_value?: string;
  tool_policy: ToolPolicy;
  budget: Budget;
  memory_scope: string;
  captured_at: string;
  overrides: SnapshotOverrides;
}

export interface Session {
  id: string;
  role_snapshot: RoleSnapshot;
  created_at: string;
}

export interface AgentInstance {
  id: string;
  session_id: string;
  role_snapshot: RoleSnapshot;
  status: AgentStatus;
  created_at: string;
}

// ----------------------------------------------------------------------------
// Thread & Canonical Message Architecture (2.0)
// ----------------------------------------------------------------------------

export type ThreadStatus = 'active' | 'waiting_approval' | 'paused' | 'completed' | 'failed' | 'cancelled';
export type ThreadVisibility = 'public' | 'team_internal' | 'restricted';

export interface ParentChildLink {
  parent_thread_id: string;
  parent_agent_id?: string;
  spawn_reason: string;
  depth: number;
}

export interface Thread {
  id: string;
  workspace: string;
  title: string;
  session_id: string;
  root_agent_id: string;
  status: ThreadStatus;
  parent_link?: ParentChildLink;
  created_at: string;
  updated_at: string;
  metadata?: Record<string, unknown>;
}

export type MessageRole = 'user' | 'agent' | 'system' | 'tool';

export interface MessageVisibility {
  deliver_to: string[]; // Agent IDs
  ui_visible_to: string[]; // Role / User scopes
  audit_visible: boolean;
  context_injection: 'immediate' | 'on_demand' | 'artifact_ref_only';
}

export interface ToolCallItem {
  id: string;
  tool_name: string;
  arguments: Record<string, unknown>;
  requires_approval: boolean;
  approval_status?: 'pending' | 'approved' | 'rejected' | 'auto_allowed';
  result?: {
    success: boolean;
    output?: string;
    error?: string;
    exit_code?: number;
    diff?: string;
  };
}

export interface StructuredAgentSummary {
  goal: string;
  current_phase: string;
  action_summary: string[];
  evidence_refs: string[];
  artifact_refs: string[];
  conclusions: string[];
  uncertainties: string[];
  usage?: {
    input_tokens: number;
    output_tokens: number;
    cached_tokens?: number;
    cost_usd?: number;
  };
}

export interface CanonicalAgentMessage {
  id: string;
  thread_id: string;
  sender: {
    type: 'user' | 'agent' | 'system';
    id: string;
    name: string;
    role_name?: string;
  };
  role: MessageRole;
  content: string;
  structured_summary?: StructuredAgentSummary;
  tool_calls?: ToolCallItem[];
  visibility: MessageVisibility;
  artifact_refs?: string[];
  created_at: string;
}

export interface MailboxDelivery {
  id: string;
  message_id: string;
  recipient_agent_id: string;
  status: 'queued' | 'injected' | 'acknowledged';
  delivered_at?: string;
}

// ----------------------------------------------------------------------------
// Context & Compaction Revision (2.0)
// ----------------------------------------------------------------------------

export type ContextComponentType =
  | 'system_policy'
  | 'role_snapshot'
  | 'messages'
  | 'file_slices'
  | 'memory_items'
  | 'artifact_summaries'
  | 'tool_results'
  | 'compaction_summary'
  | 'reserved_output';

export interface ContextReference {
  id: string;
  source: string;
  path?: string;
  line_range?: [number, number];
  preview?: string;
}

export interface ContextComponent {
  id: string;
  type: ContextComponentType;
  label: string;
  estimated_tokens: number;
  is_pinned: boolean;
  is_removable: boolean;
  is_compacted: boolean;
  updated_at: string;
  references?: ContextReference[];
}

export type ContextCompactionReason =
  | 'token_budget_exceeded'
  | 'manual_user_compaction'
  | 'phase_transition'
  | 'long_running_session';

export interface ContextRevision {
  revision_id: string;
  thread_id: string;
  total_tokens: number;
  context_window_limit: number;
  components: ContextComponent[];
  compaction_summary?: {
    reason: ContextCompactionReason;
    original_token_count: number;
    compacted_token_count: number;
    created_at: string;
  };
  cache_hit_rate?: number; // Only when verified by provider
  created_at: string;
}

// ----------------------------------------------------------------------------
// Workflow Graph & IR Models (2.0)
// ----------------------------------------------------------------------------

export type GraphNodeType =
  | 'agent'
  | 'tool'
  | 'script'
  | 'condition'
  | 'join'
  | 'approval'
  | 'human_input'
  | 'subworkflow';

export type GraphEdgeType = 'data' | 'control' | 'condition_branch' | 'error_branch' | 'loop_back';

export interface GraphPort {
  id: string;
  name: string;
  type: 'string' | 'object' | 'artifact' | 'boolean' | 'any';
  direction: 'input' | 'output';
  required?: boolean;
}

export interface ScopeInheritance {
  model_profile: { value: string; source: 'global' | 'workspace' | 'workflow' | 'node_override' };
  budget: { value: Budget; source: 'global' | 'workspace' | 'workflow' | 'node_override' };
  tool_policy: { value: ToolPolicy; source: 'global' | 'workspace' | 'workflow' | 'node_override' };
}

export interface LoopConstraint {
  max_iterations: number;
  timeout_seconds: number;
  max_budget_usd?: number;
  exit_condition_expr: string;
  no_progress_threshold?: number;
}

export interface GraphNode {
  id: string;
  label: string;
  type: GraphNodeType;
  position: { x: number; y: number };
  role_id?: string;
  role_snapshot?: RoleSnapshot;
  tool_name?: string;
  script_content?: string;
  condition_expr?: string;
  scope_inheritance?: ScopeInheritance;
  loop_constraint?: LoopConstraint;
  inputs: GraphPort[];
  outputs: GraphPort[];
  metadata?: Record<string, unknown>;
}

export interface GraphEdge {
  id: string;
  source_node_id: string;
  source_port_id: string;
  target_node_id: string;
  target_port_id: string;
  edge_type: GraphEdgeType;
  condition_label?: string;
}

export interface GraphCompilerDiagnostic {
  level: 'error' | 'warning' | 'info';
  message: string;
  node_id?: string;
  port_id?: string;
  edge_id?: string;
  rule_code: string;
}

export interface GraphDraft {
  id: string;
  workspace: string;
  name: string;
  description?: string;
  nodes: GraphNode[];
  edges: GraphEdge[];
  updated_at: string;
  diagnostics?: GraphCompilerDiagnostic[];
  is_valid: boolean;
}

export interface GraphDefinitionRevision {
  id: string;
  draft_id: string;
  version: number;
  workspace: string;
  name: string;
  description?: string;
  nodes: GraphNode[];
  edges: GraphEdge[];
  compiled_ir: Record<string, unknown>;
  published_at: string;
  published_by: string;
}

// ----------------------------------------------------------------------------
// Workflow Run & Node Execution
// ----------------------------------------------------------------------------

export type WorkflowRunStatus =
  | 'created'
  | 'running'
  | 'waiting_approval'
  | 'waiting_human_input'
  | 'interrupted'
  | 'manual_reconcile_required'
  | 'completed'
  | 'failed'
  | 'cancelled';

export type WorkflowStage =
  | 'created'
  | 'planner'
  | 'explorers'
  | 'coder'
  | 'reviewer'
  | 'main'
  | 'completed';

export type NodeRunStatus = 'pending' | 'running' | 'waiting' | 'succeeded' | 'failed' | 'skipped';

export interface NodeAttempt {
  attempt_number: number;
  status: NodeRunStatus;
  started_at: string;
  ended_at?: string;
  agent_instance_id?: string;
  input_refs?: string[];
  output_artifacts?: string[];
  error_message?: string;
  usage?: {
    input_tokens: number;
    output_tokens: number;
    cost_usd?: number;
  };
}

export interface NodeRun {
  id: string;
  workflow_run_id: string;
  node_id: string;
  label: string;
  type: GraphNodeType;
  status: NodeRunStatus;
  attempts: NodeAttempt[];
  current_attempt: number;
  created_at: string;
  updated_at: string;
}

export interface WorkflowRun {
  id: string;
  definition_revision_id?: string;
  task: string;
  workspace: string;
  status: WorkflowRunStatus;
  current_stage: WorkflowStage;
  planner_role_id?: string;
  explorer_role_ids?: string[];
  coder_role_id?: string;
  reviewer_role_id?: string;
  main_role_id?: string;
  max_parallel_explorers: number;
  max_rework_rounds: number;
  current_rework_round: number;
  node_runs: NodeRun[];
  final_verdict?: string;
  last_error_type?: string;
  resumed_from_id?: string;
  created_at: string;
  updated_at: string;
}

// ----------------------------------------------------------------------------
// Action Gateway, Approvals & Capability Leases (2.0)
// ----------------------------------------------------------------------------

export type RiskTier = 'low' | 'medium' | 'high' | 'critical';
export type RiskType = 'file_write' | 'network' | 'privileged_exec' | 'secret_access' | 'remote_host' | 'destructive';
export type PolicyDecision = 'ALLOW' | 'DENY' | 'ASK';
export type LeaseStatus = 'active' | 'consumed' | 'expired' | 'revoked';

export type ActionHash = string; // SHA-256 fingerprint

export interface LLMReviewAdvice {
  recommended_decision: 'approve' | 'reject' | 'modify';
  confidence: number;
  reasoning_summary: string;
  risk_factors: string[];
  suggested_modifications?: string;
}

export interface CapabilityLease {
  lease_id: string;
  action_hash: ActionHash;
  scope_description: string;
  granted_to_agent_id: string;
  valid_from: string;
  valid_until: string;
  status: LeaseStatus;
}

export interface ApprovalCard {
  id: string;
  session_id: string;
  thread_id?: string;
  workflow_run_id?: string;
  node_id?: string;
  agent_instance_id: string;
  role_name: string;
  host_id: string;
  device_id?: string;
  action_name: string;
  action_params_summary: Record<string, unknown>;
  target_resource: string;
  workspace_boundary: string;
  action_hash: ActionHash;
  risk_tier: RiskTier;
  risk_type: RiskType;
  impact_summary: {
    files_affected: string[];
    network_targets?: string[];
    secrets_accessed?: string[];
    host_impact?: string;
  };
  matched_policy_rule: {
    rule_id: string;
    source_scope: 'global' | 'workspace' | 'workflow';
    decision: PolicyDecision;
    description: string;
  };
  llm_review_advice?: LLMReviewAdvice;
  status: 'pending' | 'approved_once' | 'approved_for_run' | 'rejected' | 'expired';
  created_at: string;
  expires_at: string;
}

export interface ApprovalDecision {
  approval_id: string;
  decision: 'approve_once' | 'approve_for_run' | 'reject';
  custom_lease_duration_seconds?: number;
  rejection_reason?: string;
  decided_by: string;
  decided_at: string;
}

// ----------------------------------------------------------------------------
// Remote Control & Relay Infrastructure (2.0)
// ----------------------------------------------------------------------------

export type TransportMode = 'direct_lan' | 'direct_vpn' | 'self_hosted_relay';
export type DeviceScope = 'read_only' | 'interactive_steering' | 'full_control';

export interface RemoteHost {
  id: string;
  name: string;
  core_version: string;
  protocol_version: string;
  is_online: boolean;
  transport_mode: TransportMode;
  last_seen: string;
  workspaces: string[];
  capabilities: string[];
}

export interface RemoteDevice {
  device_id: string;
  device_name: string;
  device_type: 'mobile_pwa' | 'browser' | 'desktop_remote' | 'tui_remote';
  scope: DeviceScope;
  paired_at: string;
  last_active_at: string;
  is_revoked: boolean;
}

export interface RemoteSession {
  session_id: string;
  device_id: string;
  host_id: string;
  transport_mode: TransportMode;
  relay_url?: string;
  cursor: number;
  established_at: string;
}

export interface CommandReceipt {
  command_id: string;
  request_id: string;
  idempotency_key: string;
  host_id: string;
  relay_acknowledged: boolean;
  host_acknowledged: boolean;
  host_accepted: boolean;
  error_message?: string;
  received_at: string;
}

export interface RelayEnvelopeMetadata {
  envelope_id: string;
  sender_device_id: string;
  target_host_id: string;
  encrypted: boolean;
  timestamp: string;
}

// ----------------------------------------------------------------------------
// Artifacts & Memory
// ----------------------------------------------------------------------------

export type MemoryKind = 'working' | 'episodic' | 'project';
export type MemoryStatus = 'candidate' | 'active' | 'retired';

export interface Memory {
  id: string;
  session_id: string;
  kind: MemoryKind;
  content: string;
  project_scope?: string;
  role_scope: string[];
  source_task?: string;
  confidence: number;
  confirmed: boolean;
  status: MemoryStatus;
  created_at: string;
}

export interface Artifact {
  id: string;
  workspace: string;
  name: string;
  type: 'diff' | 'plan' | 'report' | 'test_summary' | 'code' | 'json';
  content?: string;
  size_bytes: number;
  created_by_agent_id: string;
  created_at: string;
  retention_policy?: string;
}

// ----------------------------------------------------------------------------
// Evaluation
// ----------------------------------------------------------------------------

export interface EvaluationSuite {
  id: string;
  name: string;
  description: string;
  created_at: string;
}

export interface EvaluationRun {
  id: string;
  suite_id: string;
  status: 'running' | 'completed' | 'failed';
  started_at: string;
  completed_at?: string;
}

export interface EvaluationResult {
  id: string;
  evaluation_run_id: string;
  test_name: string;
  passed: boolean;
  score?: number;
  diagnostics?: string;
}
