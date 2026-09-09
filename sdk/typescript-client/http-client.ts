/**
 * Operant 2.0 HTTP + SSE Client Implementation
 * Targets FastAPI endpoints (/v1/*) with automatic cursor tracking and error translation
 */

import {
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
  OperantError,
  ErrorCode,
  RemoteDevice,
  RemoteHost,
  RolePreset,
  Session,
  Thread,
  WorkflowRun,
} from '../protocol';
import { EventSubscriber, EventUnsubscribe, OperantClient } from './client';

export class HttpClient implements OperantClient {
  readonly isMock = false;
  private readonly baseUrl: string;

  constructor(baseUrl = 'http://127.0.0.1:8000') {
    this.baseUrl = baseUrl.replace(/\/+$/, '');
  }

  private unsupported(capability: string): never {
    throw new OperantError({
      code: ErrorCode.SCHEMA_INCOMPATIBLE,
      message: `HttpClient capability is not connected to a Core projection: ${capability}`,
      recoverable: false,
      user_guidance: 'Use the negotiated generated Client for supported Live capabilities.',
    });
  }

  private async request<T>(
    path: string,
    options: RequestInit = {}
  ): Promise<T> {
    const url = `${this.baseUrl}${path}`;
    const headers = new Headers(options.headers || {});
    if (!headers.has('Content-Type') && options.body && typeof options.body === 'string') {
      headers.set('Content-Type', 'application/json');
    }

    try {
      const response = await fetch(url, { ...options, headers });
      if (!response.ok) {
        let errorDetail = response.statusText;
        try {
          const json = await response.json();
          errorDetail = json.detail || json.message || JSON.stringify(json);
        } catch {
          // ignore non-json error responses
        }
        throw new OperantError({
          code: response.status === 404 ? ErrorCode.NOT_FOUND :
                response.status === 409 ? ErrorCode.CONFLICT :
                response.status === 403 ? ErrorCode.POLICY_DENIED :
                ErrorCode.RUNTIME_ERROR,
          message: `HTTP ${response.status}: ${errorDetail}`,
          recoverable: response.status >= 500,
        });
      }
      return (await response.json()) as T;
    } catch (err: unknown) {
      if (err instanceof OperantError) throw err;
      throw new OperantError({
        code: ErrorCode.RUNTIME_ERROR,
        message: err instanceof Error ? err.message : 'Network request failed',
        recoverable: true,
        user_guidance: 'Ensure Operant backend server is running on ' + this.baseUrl,
      });
    }
  }

  async checkHealth(): Promise<{ status: string }> {
    return this.request<{ status: string }>('/healthz');
  }

  // --- Model Profiles ---
  async listModels(): Promise<ModelProfile[]> {
    return this.request<ModelProfile[]>('/v1/models');
  }

  async getModel(id: string): Promise<ModelProfile> {
    return this.request<ModelProfile>(`/v1/models/${id}`);
  }

  async createModel(profile: Omit<ModelProfile, 'id' | 'created_at'>): Promise<ModelProfile> {
    return this.request<ModelProfile>('/v1/models', {
      method: 'POST',
      body: JSON.stringify(profile),
    });
  }

  async updateModel(id: string, updates: Partial<ModelProfile>): Promise<ModelProfile> {
    return this.request<ModelProfile>(`/v1/models/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(updates),
    });
  }

  async deactivateModel(id: string): Promise<ModelProfile> {
    return this.request<ModelProfile>(`/v1/models/${id}`, {
      method: 'DELETE',
    });
  }

  async discoverModels(baseUrl: string, secretRef = 'OPERANT_API_KEY'): Promise<{ model_ids: string[] }> {
    return this.request<{ model_ids: string[] }>('/v1/models/discover', {
      method: 'POST',
      body: JSON.stringify({ base_url: baseUrl, secret_ref: secretRef }),
    });
  }

  async checkModelHealth(id: string): Promise<{ healthy: boolean; latency_ms?: number }> {
    return this.request<{ healthy: boolean; latency_ms?: number }>(`/v1/models/${id}/health`, {
      method: 'POST',
    });
  }

  // --- Role Presets ---
  async listRoles(includeInactive = false): Promise<RolePreset[]> {
    return this.request<RolePreset[]>(`/v1/roles?include_inactive=${includeInactive}`);
  }

  async getRole(id: string, version?: number): Promise<RolePreset> {
    const query = version ? `?version=${version}` : '';
    return this.request<RolePreset>(`/v1/roles/${id}${query}`);
  }

  async createRole(role: Omit<RolePreset, 'id' | 'created_at' | 'version'>): Promise<RolePreset> {
    return this.request<RolePreset>('/v1/roles', {
      method: 'POST',
      body: JSON.stringify(role),
    });
  }

  async updateRole(id: string, updates: Partial<RolePreset>): Promise<RolePreset> {
    return this.request<RolePreset>(`/v1/roles/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(updates),
    });
  }

  async copyRole(id: string, newName: string): Promise<RolePreset> {
    return this.request<RolePreset>(`/v1/roles/${id}/copy`, {
      method: 'POST',
      body: JSON.stringify({ name: newName }),
    });
  }

  async deactivateRole(id: string): Promise<RolePreset> {
    return this.request<RolePreset>(`/v1/roles/${id}`, {
      method: 'DELETE',
    });
  }

  async seedDefaultRoles(
    plannerModelId: string,
    coderModelId: string,
    reviewerModelId: string
  ): Promise<RolePreset[]> {
    return this.request<RolePreset[]>('/v1/roles/seed-defaults', {
      method: 'POST',
      body: JSON.stringify({
        planner_model_profile_id: plannerModelId,
        coder_model_profile_id: coderModelId,
        reviewer_model_profile_id: reviewerModelId,
      }),
    });
  }

  // --- Sessions & Threads ---
  async listSessions(_workspace?: string): Promise<Session[]> {
    if (_workspace !== undefined) return this.unsupported('workspace-scoped listSessions');
    return this.request<Session[]>('/v1/sessions');
  }

  async getSession(id: string): Promise<Session> {
    return this.request<Session>(`/v1/sessions/${id}`);
  }

  async createSession(options: {
    roleId?: string;
    newRole?: Partial<RolePreset>;
    modelProfileId?: string;
    effort?: string;
    budgetOverrides?: Record<string, unknown>;
  }): Promise<Session> {
    return this.request<Session>('/v1/sessions', {
      method: 'POST',
      body: JSON.stringify({
        role_id: options.roleId,
        new_role: options.newRole,
        model_profile_id: options.modelProfileId,
        effort: options.effort,
        budget_overrides: options.budgetOverrides,
      }),
    });
  }

  async listThreads(_workspace?: string): Promise<Thread[]> {
    return this.unsupported('listThreads');
  }

  async getThread(_threadId: string): Promise<Thread> {
    return this.unsupported('getThread');
  }

  async listThreadMessages(_threadId: string): Promise<CanonicalAgentMessage[]> {
    return this.unsupported('listThreadMessages');
  }

  async getContextRevision(_threadId: string): Promise<ContextRevision> {
    return this.unsupported('getContextRevision');
  }

  async compactContext(_threadId: string): Promise<ContextRevision> {
    return this.unsupported('compactContext');
  }

  runSessionStream(
    _sessionId: string,
    _message: string,
    _workspace: string,
    _onEvent: EventSubscriber
  ): EventUnsubscribe {
    return this.unsupported('runSessionStream');
  }

  async cancelSession(sessionId: string): Promise<{ accepted: boolean }> {
    return this.request<{ accepted: boolean }>(`/v1/sessions/${sessionId}/cancel`, {
      method: 'POST',
    });
  }

  // --- Approvals ---
  async listPendingApprovals(_sessionId?: string): Promise<ApprovalCard[]> {
    return this.unsupported('listPendingApprovals');
  }

  async submitApproval(
    sessionId: string,
    approvalId: string,
    decision: ApprovalDecision
  ): Promise<{ accepted: boolean }> {
    return this.request<{ accepted: boolean }>(`/v1/sessions/${sessionId}/approvals/${approvalId}`, {
      method: 'POST',
      body: JSON.stringify({ approved: decision.decision.startsWith('approve') }),
    });
  }

  // --- Workflows & Graph ---
  async listGraphDrafts(_workspace?: string): Promise<GraphDraft[]> {
    return this.unsupported('listGraphDrafts');
  }

  async getGraphDraft(_draftId: string): Promise<GraphDraft> {
    return this.unsupported('getGraphDraft');
  }

  async saveGraphDraft(_draft: Partial<GraphDraft> & { workspace: string; name: string }): Promise<GraphDraft> {
    return this.unsupported('saveGraphDraft');
  }

  async compileGraphDraft(_draftId: string): Promise<{ is_valid: boolean; diagnostics: GraphCompilerDiagnostic[] }> {
    return this.unsupported('compileGraphDraft');
  }

  async publishGraphDraft(_draftId: string, _description?: string): Promise<GraphDefinitionRevision> {
    return this.unsupported('publishGraphDraft');
  }

  async listGraphRevisions(_workspace?: string): Promise<GraphDefinitionRevision[]> {
    return this.unsupported('listGraphRevisions');
  }

  // --- Workflow Runs ---
  async listWorkflowRuns(_workspace?: string): Promise<WorkflowRun[]> {
    return this.unsupported('listWorkflowRuns');
  }

  async getWorkflowRun(_runId: string): Promise<WorkflowRun> {
    return this.unsupported('getWorkflowRun');
  }

  async startWorkflowRun(_options: {
    task: string;
    workspace: string;
    mainRoleId?: string;
    plannerRoleId?: string;
    explorerRoleIds?: string[];
    coderRoleId?: string;
    reviewerRoleId?: string;
    maxParallelExplorers?: number;
    maxReworkRounds?: number;
  }, _onEvent?: EventSubscriber): Promise<WorkflowRun> {
    return this.unsupported('startWorkflowRun');
  }

  async resumeWorkflowRun(_runId: string, _allowCoderReplay: boolean, _onEvent?: EventSubscriber): Promise<void> {
    return this.unsupported('resumeWorkflowRun');
  }

  async cancelWorkflowRun(runId: string): Promise<{ accepted: boolean }> {
    return this.request<{ accepted: boolean }>(`/v1/tasks/${runId}/cancel`, {
      method: 'POST',
    });
  }

  async getWorkflowTrace(runId: string): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>(`/v1/tasks/${runId}/trace`);
  }

  exportWorkflowTraceUrl(runId: string): string {
    return `${this.baseUrl}/v1/tasks/${runId}/trace.jsonl`;
  }

  // --- Remote Control ---
  async listRemoteHosts(): Promise<RemoteHost[]> {
    return this.unsupported('listRemoteHosts');
  }

  async listRemoteDevices(_hostId?: string): Promise<RemoteDevice[]> {
    return this.unsupported('listRemoteDevices');
  }

  async requestDevicePairing(
    _hostId: string,
    _deviceName: string,
    _scope: string,
    _pin: string
  ): Promise<{ request_id: string; qr_code_payload: string }> {
    return this.unsupported('requestDevicePairing');
  }

  async revokeRemoteDevice(_deviceId: string): Promise<{ revoked: boolean }> {
    return this.unsupported('revokeRemoteDevice');
  }

  async sendRemoteCommand(_hostId: string, _sessionId: string, _message: string): Promise<CommandReceipt> {
    return this.unsupported('sendRemoteCommand');
  }

  // --- Memory Governance ---
  async searchMemories(
    sessionId: string,
    query: string,
    projectScope?: string,
    includeCandidates = false
  ): Promise<Memory[]> {
    const q = encodeURIComponent(query);
    const scopeParam = projectScope ? `&project_scope=${encodeURIComponent(projectScope)}` : '';
    return this.request<Memory[]>(
      `/v1/memories/search?session_id=${sessionId}&query=${q}${scopeParam}&include_candidates=${includeCandidates}`
    );
  }

  async createMemory(options: {
    sessionId: string;
    kind: 'working' | 'episodic' | 'project';
    content: string;
    projectScope?: string;
    roleScope?: string[];
    sourceTask?: string;
    confidence?: number;
    confirmed?: boolean;
  }): Promise<Memory> {
    return this.request<Memory>('/v1/memories', {
      method: 'POST',
      body: JSON.stringify({
        session_id: options.sessionId,
        kind: options.kind,
        content: options.content,
        project_scope: options.projectScope,
        role_scope: options.roleScope || [],
        source_task: options.sourceTask,
        confidence: options.confidence ?? 0.5,
        confirmed: options.confirmed ?? false,
      }),
    });
  }

  async confirmMemory(sessionId: string, memoryId: string, projectScope?: string): Promise<Memory> {
    const scopeParam = projectScope ? `?project_scope=${encodeURIComponent(projectScope)}` : '';
    return this.request<Memory>(`/v1/memories/${memoryId}/confirm${scopeParam}`, {
      method: 'POST',
      body: JSON.stringify({ session_id: sessionId }),
    });
  }

  async deactivateMemory(sessionId: string, memoryId: string, projectScope?: string): Promise<Memory> {
    const scopeParam = projectScope ? `?project_scope=${encodeURIComponent(projectScope)}` : '';
    return this.request<Memory>(`/v1/memories/${memoryId}${scopeParam}`, {
      method: 'DELETE',
      body: JSON.stringify({ session_id: sessionId }),
    });
  }

  // --- Evaluation ---
  async listEvaluationSuites(): Promise<EvaluationSuite[]> {
    return this.request<EvaluationSuite[]>('/v1/evaluations/suites');
  }

  async listEvaluationRuns(suiteId?: string): Promise<EvaluationRun[]> {
    const query = suiteId ? `?suite_id=${suiteId}` : '';
    return this.request<EvaluationRun[]>(`/v1/evaluations/runs${query}`);
  }

  async listEvaluationResults(runId: string): Promise<EvaluationResult[]> {
    return this.request<EvaluationResult[]>(`/v1/evaluations/runs/${runId}/results`);
  }

  // --- Global Event Subscription ---
  subscribeEvents(_cursor?: EventCursor, _subscriber?: EventSubscriber): EventUnsubscribe {
    return this.unsupported('subscribeEvents');
  }
}
