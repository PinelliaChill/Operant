/**
 * Operant 2.0 Protocol Event Definitions
 * Standard Event Taxonomy with Sequence, Cursor and Structured Payloads
 */

import {
  ApprovalCard,
  ApprovalDecision,
  CapabilityLease,
  ContextRevision,
  NodeRun,
  WorkflowStage,
} from './models';

export interface EventCursor {
  sequence: number;
  event_id?: string;
  timestamp?: string;
}

export interface BaseProtocolEvent<TType extends string = string, TPayload = Record<string, unknown>> {
  id: string;
  sequence: number;
  event_type: TType;
  schema_version: string; // e.g. "2.0"
  session_id?: string;
  thread_id?: string;
  workflow_run_id?: string;
  agent_id?: string;
  occurred_at: string;
  payload: TPayload;
}

// ----------------------------------------------------------------------------
// Agent & Session Events
// ----------------------------------------------------------------------------

export type AgentStartedEvent = BaseProtocolEvent<
  'agent.started',
  {
    role_name: string;
    model_profile_name: string;
    effort?: string;
    workspace: string;
    initial_prompt?: string;
  }
>;

export type AgentCompletedEvent = BaseProtocolEvent<
  'agent.completed',
  {
    final_output?: string;
    total_turns: number;
    usage?: {
      input_tokens: number;
      output_tokens: number;
      cached_tokens?: number;
      cost_usd?: number;
    };
  }
>;

export type AgentFailedEvent = BaseProtocolEvent<
  'agent.failed',
  {
    error_type: string;
    message: string;
    stack_preview?: string;
    recoverable: boolean;
  }
>;

export type AgentCancelledEvent = BaseProtocolEvent<
  'agent.cancelled',
  {
    reason: string;
    cancelled_by: string;
  }
>;

export type AgentTimedOutEvent = BaseProtocolEvent<
  'agent.timed_out',
  {
    timeout_seconds: number;
    turns_completed: number;
  }
>;

export type AgentSteeredEvent = BaseProtocolEvent<
  'agent.steered',
  {
    steering_instruction: string;
    steered_by: string;
    injected_at_turn: number;
  }
>;

// ----------------------------------------------------------------------------
// Model Streaming Events (NO hidden thought chains!)
// ----------------------------------------------------------------------------

export type ModelDeltaEvent = BaseProtocolEvent<
  'model.delta',
  {
    delta_text: string;
    message_id: string;
    turn_index: number;
  }
>;

export type ModelCompletedEvent = BaseProtocolEvent<
  'model.completed',
  {
    message_id: string;
    finish_reason: string;
    turn_index: number;
    usage?: {
      prompt_tokens: number;
      completion_tokens: number;
      cached_tokens?: number;
    };
  }
>;

// ----------------------------------------------------------------------------
// Tool & Action Gateway Events
// ----------------------------------------------------------------------------

export type ToolStartedEvent = BaseProtocolEvent<
  'tool.started',
  {
    tool_call_id: string;
    tool_name: string;
    arguments: Record<string, unknown>;
  }
>;

export type ToolApprovalRequiredEvent = BaseProtocolEvent<
  'tool.approval_required',
  {
    tool_call_id: string;
    tool_name: string;
    arguments: Record<string, unknown>;
    approval_card: ApprovalCard;
  }
>;

export type ToolApprovalDecidedEvent = BaseProtocolEvent<
  'tool.approval_decided',
  {
    tool_call_id: string;
    approval_id: string;
    decision: ApprovalDecision;
    lease?: CapabilityLease;
  }
>;

export type ToolCompletedEvent = BaseProtocolEvent<
  'tool.completed',
  {
    tool_call_id: string;
    tool_name: string;
    output: string;
    diff?: string;
    exit_code?: number;
    execution_time_ms: number;
    runner_used: 'host' | 'docker';
  }
>;

export type ToolFailedEvent = BaseProtocolEvent<
  'tool.failed',
  {
    tool_call_id: string;
    tool_name: string;
    error_message: string;
    exit_code?: number;
  }
>;

// ----------------------------------------------------------------------------
// Test & Self-Correction Feedback Events
// ----------------------------------------------------------------------------

export type TestFailureFeedbackEvent = BaseProtocolEvent<
  'test.failure_feedback',
  {
    consecutive_failures: number;
    max_failures: number;
    failure_details: string;
    attempted_patch?: string;
    suggested_action?: string;
  }
>;

export type TestPassedFeedbackEvent = BaseProtocolEvent<
  'test.passed_feedback',
  {
    tests_run: number;
    passed_count: number;
    output_summary: string;
  }
>;

// ----------------------------------------------------------------------------
// Workflow & Graph Execution Events
// ----------------------------------------------------------------------------

export type WorkflowStageStartedEvent = BaseProtocolEvent<
  'workflow.stage_started',
  {
    stage: WorkflowStage;
    assigned_role_id?: string;
    assigned_role_name?: string;
  }
>;

export type WorkflowSubtaskResultEvent = BaseProtocolEvent<
  'workflow.subtask_result',
  {
    stage: WorkflowStage;
    role: string;
    subtask_id: string;
    summary: string;
    artifacts_created: string[];
  }
>;

export type WorkflowNodeStartedEvent = BaseProtocolEvent<
  'workflow.node_started',
  {
    node_id: string;
    node_label: string;
    node_type: string;
    attempt_number: number;
  }
>;

export type WorkflowNodeCompletedEvent = BaseProtocolEvent<
  'workflow.node_completed',
  {
    node_id: string;
    node_run: NodeRun;
  }
>;

export type WorkflowNodeFailedEvent = BaseProtocolEvent<
  'workflow.node_failed',
  {
    node_id: string;
    error: string;
    attempt_number: number;
    will_retry: boolean;
  }
>;

export type WorkflowCheckpointSavedEvent = BaseProtocolEvent<
  'workflow.checkpoint_saved',
  {
    checkpoint_id: string;
    stage: WorkflowStage;
    rework_round: number;
    snapshot_summary: string;
  }
>;

export type WorkflowResumeRejectedEvent = BaseProtocolEvent<
  'workflow.resume_rejected',
  {
    reason: string;
    coder_replay_blocked: boolean;
  }
>;

export type WorkflowCompletedEvent = BaseProtocolEvent<
  'workflow.completed',
  {
    final_verdict: string;
    total_duration_seconds: number;
    total_rework_rounds: number;
  }
>;

// ----------------------------------------------------------------------------
// Approval & Lease Lifecycle Events
// ----------------------------------------------------------------------------

export type ApprovalRequestedEvent = BaseProtocolEvent<
  'approval.requested',
  {
    approval_card: ApprovalCard;
  }
>;

export type ApprovalDecidedEvent = BaseProtocolEvent<
  'approval.decided',
  {
    approval_id: string;
    decision: ApprovalDecision;
  }
>;

export type ApprovalExpiredEvent = BaseProtocolEvent<
  'approval.expired',
  {
    approval_id: string;
    reason: 'timeout' | 'parameter_changed' | 'policy_updated';
  }
>;

export type ApprovalRevokedEvent = BaseProtocolEvent<
  'approval.revoked',
  {
    lease_id: string;
    revoked_by: string;
  }
>;

// ----------------------------------------------------------------------------
// Remote Control Events
// ----------------------------------------------------------------------------

export type RemoteDevicePairedEvent = BaseProtocolEvent<
  'remote.device_paired',
  {
    device_id: string;
    device_name: string;
    scope: string;
  }
>;

export type RemoteCommandReceiptEvent = BaseProtocolEvent<
  'remote.command_receipt',
  {
    command_id: string;
    request_id: string;
    host_accepted: boolean;
    error_message?: string;
  }
>;

export type RemoteRelayStateChangedEvent = BaseProtocolEvent<
  'remote.relay_state_changed',
  {
    host_id: string;
    relay_url: string;
    is_connected: boolean;
    latency_ms?: number;
  }
>;

// ----------------------------------------------------------------------------
// Context & Memory Events
// ----------------------------------------------------------------------------

export type ContextCompactedEvent = BaseProtocolEvent<
  'context.compacted',
  {
    revision: ContextRevision;
  }
>;

export type MemoryCreatedEvent = BaseProtocolEvent<
  'memory.created',
  {
    memory_id: string;
    kind: string;
    content: string;
    confirmed: boolean;
  }
>;

export type MemoryConfirmedEvent = BaseProtocolEvent<
  'memory.confirmed',
  {
    memory_id: string;
  }
>;

export type MemoryDeactivatedEvent = BaseProtocolEvent<
  'memory.deactivated',
  {
    memory_id: string;
  }
>;

// ----------------------------------------------------------------------------
// Union Type of All Operant Events
// ----------------------------------------------------------------------------

export type AnyOperantEvent =
  | AgentStartedEvent
  | AgentCompletedEvent
  | AgentFailedEvent
  | AgentCancelledEvent
  | AgentTimedOutEvent
  | AgentSteeredEvent
  | ModelDeltaEvent
  | ModelCompletedEvent
  | ToolStartedEvent
  | ToolApprovalRequiredEvent
  | ToolApprovalDecidedEvent
  | ToolCompletedEvent
  | ToolFailedEvent
  | TestFailureFeedbackEvent
  | TestPassedFeedbackEvent
  | WorkflowStageStartedEvent
  | WorkflowSubtaskResultEvent
  | WorkflowNodeStartedEvent
  | WorkflowNodeCompletedEvent
  | WorkflowNodeFailedEvent
  | WorkflowCheckpointSavedEvent
  | WorkflowResumeRejectedEvent
  | WorkflowCompletedEvent
  | ApprovalRequestedEvent
  | ApprovalDecidedEvent
  | ApprovalExpiredEvent
  | ApprovalRevokedEvent
  | RemoteDevicePairedEvent
  | RemoteCommandReceiptEvent
  | RemoteRelayStateChangedEvent
  | ContextCompactedEvent
  | MemoryCreatedEvent
  | MemoryConfirmedEvent
  | MemoryDeactivatedEvent;
