/**
 * Operant 2.0 Protocol Command Definitions
 * Idempotent Commands with Request IDs, Idempotency Keys, and Structured Payloads
 */

import {
  ApprovalDecision,
  Budget,
  DeviceScope,
  Effort,
  GraphEdge,
  GraphNode,
  MemoryKind,
  ModelProfile,
  RolePreset,
} from './models';

export interface BaseCommand<TPayload = Record<string, unknown>> {
  request_id: string;
  idempotency_key: string;
  timestamp: string;
  sender_id?: string;
  payload: TPayload;
}

// ----------------------------------------------------------------------------
// Session & Thread Commands
// ----------------------------------------------------------------------------

export type CreateSessionCommand = BaseCommand<{
  role_id?: string;
  new_role?: Partial<RolePreset>;
  model_profile_id?: string;
  effort?: Effort;
  budget_overrides?: Partial<Budget>;
  workspace?: string;
}>;

export type RunSessionCommand = BaseCommand<{
  session_id: string;
  message: string;
  workspace: string;
}>;

export type SteerSessionCommand = BaseCommand<{
  session_id: string;
  instruction: string;
}>;

export type CancelSessionCommand = BaseCommand<{
  session_id: string;
  reason?: string;
}>;

export type CompactContextCommand = BaseCommand<{
  session_id: string;
  thread_id: string;
  keep_pinned_only?: boolean;
}>;

// ----------------------------------------------------------------------------
// Action Gateway & Approval Commands
// ----------------------------------------------------------------------------

export type SubmitApprovalCommand = BaseCommand<{
  session_id?: string;
  approval_id: string;
  decision: ApprovalDecision;
}>;

export type RevokeCapabilityLeaseCommand = BaseCommand<{
  lease_id: string;
  reason: string;
}>;

// ----------------------------------------------------------------------------
// Workflow Graph & Execution Commands
// ----------------------------------------------------------------------------

export type SaveGraphDraftCommand = BaseCommand<{
  draft_id?: string;
  workspace: string;
  name: string;
  description?: string;
  nodes: GraphNode[];
  edges: GraphEdge[];
}>;

export type CompileGraphDraftCommand = BaseCommand<{
  draft_id: string;
}>;

export type PublishGraphDraftCommand = BaseCommand<{
  draft_id: string;
  description?: string;
}>;

export type CreateWorkflowRunCommand = BaseCommand<{
  task: string;
  workspace: string;
  definition_revision_id?: string;
  main_role_id?: string;
  planner_role_id?: string;
  explorer_role_ids?: string[];
  coder_role_id?: string;
  reviewer_role_id?: string;
  max_parallel_explorers?: number;
  max_rework_rounds?: number;
}>;

export type ResumeWorkflowRunCommand = BaseCommand<{
  workflow_run_id: string;
  allow_coder_replay?: boolean;
}>;

export type CancelWorkflowRunCommand = BaseCommand<{
  workflow_run_id: string;
  reason?: string;
}>;

// ----------------------------------------------------------------------------
// Remote Control Commands
// ----------------------------------------------------------------------------

export type RequestDevicePairingCommand = BaseCommand<{
  host_id: string;
  device_name: string;
  device_type: 'mobile_pwa' | 'browser' | 'desktop_remote' | 'tui_remote';
  requested_scope: DeviceScope;
  pairing_pin: string;
}>;

export type ConfirmDevicePairingCommand = BaseCommand<{
  pairing_request_id: string;
  granted_scope: DeviceScope;
}>;

export type RevokeRemoteDeviceCommand = BaseCommand<{
  device_id: string;
}>;

export type QueueRemoteCommand = BaseCommand<{
  host_id: string;
  target_session_id: string;
  message: string;
}>;

export type SteerRemoteCommand = BaseCommand<{
  host_id: string;
  target_session_id: string;
  instruction: string;
}>;

// ----------------------------------------------------------------------------
// Memory Commands
// ----------------------------------------------------------------------------

export type CreateMemoryCommand = BaseCommand<{
  session_id: string;
  kind: MemoryKind;
  content: string;
  project_scope?: string;
  role_scope?: string[];
  source_task?: string;
  confidence?: number;
  confirmed?: boolean;
}>;

export type ConfirmMemoryCommand = BaseCommand<{
  memory_id: string;
  session_id: string;
  project_scope?: string;
}>;

export type DeactivateMemoryCommand = BaseCommand<{
  memory_id: string;
  session_id: string;
  project_scope?: string;
}>;

// ----------------------------------------------------------------------------
// Settings & Profile Commands
// ----------------------------------------------------------------------------

export type SaveModelProfileCommand = BaseCommand<{
  profile: Partial<ModelProfile> & { name: string; model_id: string; base_url: string };
}>;

export type DiscoverModelsCommand = BaseCommand<{
  base_url: string;
  secret_ref?: string;
}>;

export type SaveRolePresetCommand = BaseCommand<{
  role: Partial<RolePreset> & { name: string; system_prompt: string; model_profile_id: string };
}>;
