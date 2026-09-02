/**
 * Operant 2.0 TypeScript Client Interface
 * High-level contract for interacting with Operant Core (Live HTTP or High-Fidelity Mock)
 */

import {
  AnyOperantEvent,
  ApprovalCard,
  ApprovalDecision,
  CanonicalAgentMessage,
  CommandReceipt,
  ContextRevision,
  EvaluationResult,
  EvaluationRun,
  EvaluationSuite,
  EventCursor,
  GraphCompilerDiagnostic,
  GraphDefinitionRevision,
  GraphDraft,
  Memory,
  ModelProfile,
  RemoteDevice,
  RemoteHost,
  RolePreset,
  Session,
  Thread,
  WorkflowRun,
} from '../protocol';

export type EventUnsubscribe = () => void;
export type EventSubscriber = (event: AnyOperantEvent) => void;

export interface OperantClient {
  // --- Connection & Metadata ---
  readonly isMock: boolean;
  checkHealth(): Promise<{ status: string }>;

  // --- Model Profiles ---
  listModels(): Promise<ModelProfile[]>;
  getModel(id: string): Promise<ModelProfile>;
  createModel(profile: Omit<ModelProfile, 'id' | 'created_at'>): Promise<ModelProfile>;
  updateModel(id: string, updates: Partial<ModelProfile>): Promise<ModelProfile>;
  deactivateModel(id: string): Promise<ModelProfile>;
  discoverModels(baseUrl: string, secretRef?: string): Promise<{ model_ids: string[] }>;
  checkModelHealth(id: string): Promise<{ healthy: boolean; latency_ms?: number }>;

  // --- Role Presets ---
  listRoles(includeInactive?: boolean): Promise<RolePreset[]>;
  getRole(id: string, version?: number): Promise<RolePreset>;
  createRole(role: Omit<RolePreset, 'id' | 'created_at' | 'version'>): Promise<RolePreset>;
  updateRole(id: string, updates: Partial<RolePreset>): Promise<RolePreset>;
  copyRole(id: string, newName: string): Promise<RolePreset>;
  deactivateRole(id: string): Promise<RolePreset>;
  seedDefaultRoles(plannerModelId: string, coderModelId: string, reviewerModelId: string): Promise<RolePreset[]>;

  // --- Sessions & Threads ---
  listSessions(workspace?: string): Promise<Session[]>;
  getSession(id: string): Promise<Session>;
  createSession(options: {
    roleId?: string;
    newRole?: Partial<RolePreset>;
    modelProfileId?: string;
    effort?: string;
    budgetOverrides?: Record<string, unknown>;
  }): Promise<Session>;
  listThreads(workspace?: string): Promise<Thread[]>;
  getThread(threadId: string): Promise<Thread>;
  listThreadMessages(threadId: string): Promise<CanonicalAgentMessage[]>;
  getContextRevision(threadId: string): Promise<ContextRevision>;
  compactContext(threadId: string, reason?: string): Promise<ContextRevision>;
  runSessionStream(sessionId: string, message: string, workspace: string, onEvent: EventSubscriber): EventUnsubscribe;
  cancelSession(sessionId: string): Promise<{ accepted: boolean }>;

  // --- Approvals ---
  listPendingApprovals(sessionId?: string): Promise<ApprovalCard[]>;
  submitApproval(sessionId: string, approvalId: string, decision: ApprovalDecision): Promise<{ accepted: boolean }>;

  // --- Workflows & Graph ---
  listGraphDrafts(workspace?: string): Promise<GraphDraft[]>;
  getGraphDraft(draftId: string): Promise<GraphDraft>;
  saveGraphDraft(draft: Partial<GraphDraft> & { workspace: string; name: string }): Promise<GraphDraft>;
  compileGraphDraft(draftId: string): Promise<{ is_valid: boolean; diagnostics: GraphCompilerDiagnostic[] }>;
  publishGraphDraft(draftId: string, description?: string): Promise<GraphDefinitionRevision>;
  listGraphRevisions(workspace?: string): Promise<GraphDefinitionRevision[]>;

  // --- Workflow Runs ---
  listWorkflowRuns(workspace?: string): Promise<WorkflowRun[]>;
  getWorkflowRun(runId: string): Promise<WorkflowRun>;
  startWorkflowRun(options: {
    task: string;
    workspace: string;
    mainRoleId?: string;
    plannerRoleId?: string;
    explorerRoleIds?: string[];
    coderRoleId?: string;
    reviewerRoleId?: string;
    maxParallelExplorers?: number;
    maxReworkRounds?: number;
  }, onEvent?: EventSubscriber): Promise<WorkflowRun>;
  resumeWorkflowRun(runId: string, allowCoderReplay: boolean, onEvent?: EventSubscriber): Promise<void>;
  cancelWorkflowRun(runId: string): Promise<{ accepted: boolean }>;
  getWorkflowTrace(runId: string): Promise<Record<string, unknown>>;
  exportWorkflowTraceUrl(runId: string): string;

  // --- Remote Control ---
  listRemoteHosts(): Promise<RemoteHost[]>;
  listRemoteDevices(hostId?: string): Promise<RemoteDevice[]>;
  requestDevicePairing(hostId: string, deviceName: string, scope: string, pin: string): Promise<{ request_id: string; qr_code_payload: string }>;
  revokeRemoteDevice(deviceId: string): Promise<{ revoked: boolean }>;
  sendRemoteCommand(hostId: string, sessionId: string, message: string): Promise<CommandReceipt>;

  // --- Memory Governance ---
  searchMemories(sessionId: string, query: string, projectScope?: string, includeCandidates?: boolean): Promise<Memory[]>;
  createMemory(options: {
    sessionId: string;
    kind: 'working' | 'episodic' | 'project';
    content: string;
    projectScope?: string;
    roleScope?: string[];
    sourceTask?: string;
    confidence?: number;
    confirmed?: boolean;
  }): Promise<Memory>;
  confirmMemory(sessionId: string, memoryId: string, projectScope?: string): Promise<Memory>;
  deactivateMemory(sessionId: string, memoryId: string, projectScope?: string): Promise<Memory>;

  // --- Evaluation ---
  listEvaluationSuites(): Promise<EvaluationSuite[]>;
  listEvaluationRuns(suiteId?: string): Promise<EvaluationRun[]>;
  listEvaluationResults(runId: string): Promise<EvaluationResult[]>;

  // --- Global Event Subscription ---
  subscribeEvents(cursor?: EventCursor, subscriber?: EventSubscriber): EventUnsubscribe;
}
