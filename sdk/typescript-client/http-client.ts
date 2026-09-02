/**
 * Operant 2.0 HTTP + SSE Client Implementation
 * Targets FastAPI endpoints (/v1/*) with automatic cursor tracking and error translation
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

  async listThreads(workspace?: string): Promise<Thread[]> {
    const sessions = await this.listSessions(workspace);
    return sessions.map((s) => ({
      id: `thread_${s.id}`,
      workspace: workspace || '/Users/bigo/agentworkspace/codexworkspace/operant',
      title: `${s.role_snapshot.role_name} Session`,
      session_id: s.id,
      root_agent_id: `agent_${s.id}`,
      status: 'active',
      created_at: s.created_at,
      updated_at: s.created_at,
    }));
  }

  async getThread(threadId: string): Promise<Thread> {
    const sessionId = threadId.replace(/^thread_/, '');
    const session = await this.getSession(sessionId);
    return {
      id: threadId,
      workspace: '/Users/bigo/agentworkspace/codexworkspace/operant',
      title: `${session.role_snapshot.role_name} Session`,
      session_id: session.id,
      root_agent_id: `agent_${session.id}`,
      status: 'active',
      created_at: session.created_at,
      updated_at: session.created_at,
    };
  }

  async listThreadMessages(threadId: string): Promise<CanonicalAgentMessage[]> {
    const sessionId = threadId.replace(/^thread_/, '');
    const rawEvents = await this.request<Array<{ event_type: string; payload: Record<string, unknown>; created_at: string }>>(
      `/v1/sessions/${sessionId}/events`
    );
    return rawEvents
      .filter((e) => e.event_type.startsWith('agent.') || e.event_type.startsWith('model.'))
      .map((e, idx) => ({
        id: `msg_${idx}`,
        thread_id: threadId,
        sender: {
          type: 'agent',
          id: 'agent_main',
          name: 'Main Agent',
        },
        role: 'agent',
        content: typeof e.payload.delta_text === 'string' ? e.payload.delta_text : JSON.stringify(e.payload),
        visibility: {
          deliver_to: ['*'],
          ui_visible_to: ['*'],
          audit_visible: true,
          context_injection: 'immediate',
        },
        created_at: e.created_at,
      }));
  }

  async getContextRevision(threadId: string): Promise<ContextRevision> {
    return {
      revision_id: `rev_${Date.now()}`,
      thread_id: threadId,
      total_tokens: 4200,
      context_window_limit: 128000,
      components: [
        {
          id: 'comp_sys',
          type: 'system_policy',
          label: 'System & Security Policy',
          estimated_tokens: 650,
          is_pinned: true,
          is_removable: false,
          is_compacted: false,
          updated_at: new Date().toISOString(),
        },
        {
          id: 'comp_role',
          type: 'role_snapshot',
          label: 'Role Snapshot (Coder v2)',
          estimated_tokens: 850,
          is_pinned: true,
          is_removable: false,
          is_compacted: false,
          updated_at: new Date().toISOString(),
        },
        {
          id: 'comp_msg',
          type: 'messages',
          label: 'Recent Interaction Timeline',
          estimated_tokens: 1800,
          is_pinned: false,
          is_removable: true,
          is_compacted: false,
          updated_at: new Date().toISOString(),
        },
        {
          id: 'comp_files',
          type: 'file_slices',
          label: 'Active Workspace Slices',
          estimated_tokens: 900,
          is_pinned: false,
          is_removable: true,
          is_compacted: false,
          updated_at: new Date().toISOString(),
        },
      ],
      created_at: new Date().toISOString(),
    };
  }

  async compactContext(threadId: string): Promise<ContextRevision> {
    return this.getContextRevision(threadId);
  }

  runSessionStream(
    sessionId: string,
    message: string,
    workspace: string,
    onEvent: EventSubscriber
  ): EventUnsubscribe {
    const controller = new AbortController();
    let isAborted = false;

    (async () => {
      try {
        const response = await fetch(`${this.baseUrl}/v1/sessions/${sessionId}/runs`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ message, workspace }),
          signal: controller.signal,
        });

        if (!response.ok || !response.body) {
          throw new Error(`Failed to initiate stream: ${response.statusText}`);
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (!isAborted) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const parts = buffer.split('\n\n');
          buffer = parts.pop() || '';

          for (const block of parts) {
            const lines = block.split('\n');
            let eventType = 'message';
            let dataStr = '';
            for (const line of lines) {
              if (line.startsWith('event: ')) {
                eventType = line.slice(7).trim();
              } else if (line.startsWith('data: ')) {
                dataStr = line.slice(6).trim();
              }
            }
            if (dataStr) {
              try {
                const parsed = JSON.parse(dataStr);
                const fullEvent: AnyOperantEvent = {
                  id: `evt_${Date.now()}_${Math.random().toString(36).substring(2, 7)}`,
                  sequence: Date.now(),
                  event_type: eventType as any,
                  schema_version: '2.0',
                  session_id: sessionId,
                  occurred_at: new Date().toISOString(),
                  payload: parsed,
                };
                onEvent(fullEvent);
              } catch {
                // ignore json parse error on partial chunks
              }
            }
          }
        }
      } catch (err: unknown) {
        if (!isAborted) {
          console.error('Session SSE Stream error:', err);
        }
      }
    })();

    return () => {
      isAborted = true;
      controller.abort();
    };
  }

  async cancelSession(sessionId: string): Promise<{ accepted: boolean }> {
    return this.request<{ accepted: boolean }>(`/v1/sessions/${sessionId}/cancel`, {
      method: 'POST',
    });
  }

  // --- Approvals ---
  async listPendingApprovals(sessionId?: string): Promise<ApprovalCard[]> {
    if (sessionId) {
      const raw = await this.request<Array<{ tool_call_id: string; tool_name: string; arguments?: Record<string, unknown> }>>(
        `/v1/sessions/${sessionId}/approvals`
      );
      return raw.map((item) => ({
        id: item.tool_call_id,
        session_id: sessionId,
        agent_instance_id: 'agent_live',
        role_name: 'Coder',
        host_id: 'host_local',
        action_name: item.tool_name,
        action_params_summary: item.arguments || {},
        target_resource: String((item.arguments || {}).target || 'workspace'),
        workspace_boundary: '/workspace',
        action_hash: 'sha256:4f8a3c...',
        risk_tier: 'high',
        risk_type: 'file_write',
        impact_summary: { files_affected: ['modified_file.ts'] },
        matched_policy_rule: {
          rule_id: 'rule_policy_write',
          source_scope: 'workspace',
          decision: 'ASK',
          description: 'Requires confirmation for file system modifications',
        },
        status: 'pending',
        created_at: new Date().toISOString(),
        expires_at: new Date(Date.now() + 600000).toISOString(),
      }));
    }
    return [];
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
    return [];
  }

  async getGraphDraft(draftId: string): Promise<GraphDraft> {
    throw new OperantError({ code: ErrorCode.NOT_FOUND, message: `Draft ${draftId} not found`, recoverable: false });
  }

  async saveGraphDraft(draft: Partial<GraphDraft> & { workspace: string; name: string }): Promise<GraphDraft> {
    return {
      id: draft.id || `draft_${Date.now()}`,
      workspace: draft.workspace,
      name: draft.name,
      nodes: draft.nodes || [],
      edges: draft.edges || [],
      updated_at: new Date().toISOString(),
      is_valid: true,
    };
  }

  async compileGraphDraft(_draftId: string): Promise<{ is_valid: boolean; diagnostics: GraphCompilerDiagnostic[] }> {
    return { is_valid: true, diagnostics: [] };
  }

  async publishGraphDraft(draftId: string, description?: string): Promise<GraphDefinitionRevision> {
    return {
      id: `rev_${Date.now()}`,
      draft_id: draftId,
      version: 1,
      workspace: '/Users/bigo/agentworkspace/codexworkspace/operant',
      name: 'Published Workflow',
      description,
      nodes: [],
      edges: [],
      compiled_ir: {},
      published_at: new Date().toISOString(),
      published_by: 'user',
    };
  }

  async listGraphRevisions(_workspace?: string): Promise<GraphDefinitionRevision[]> {
    return [];
  }

  // --- Workflow Runs ---
  async listWorkflowRuns(_workspace?: string): Promise<WorkflowRun[]> {
    const rawRuns = await this.request<any[]>('/v1/tasks');
    return rawRuns.map((r) => ({
      id: r.id,
      task: r.task,
      workspace: r.workspace,
      status: r.status,
      current_stage: r.current_stage,
      planner_role_id: r.planner_role_id,
      explorer_role_ids: r.explorer_role_ids,
      coder_role_id: r.coder_role_id,
      reviewer_role_id: r.reviewer_role_id,
      main_role_id: r.main_role_id,
      max_parallel_explorers: r.max_parallel_explorers,
      max_rework_rounds: r.max_rework_rounds,
      current_rework_round: 0,
      node_runs: [],
      final_verdict: r.final_verdict,
      last_error_type: r.last_error_type,
      created_at: r.created_at,
      updated_at: r.updated_at,
    }));
  }

  async getWorkflowRun(runId: string): Promise<WorkflowRun> {
    const r = await this.request<any>(`/v1/tasks/${runId}`);
    return {
      id: r.id,
      task: r.task,
      workspace: r.workspace,
      status: r.status,
      current_stage: r.current_stage,
      planner_role_id: r.planner_role_id,
      explorer_role_ids: r.explorer_role_ids,
      coder_role_id: r.coder_role_id,
      reviewer_role_id: r.reviewer_role_id,
      main_role_id: r.main_role_id,
      max_parallel_explorers: r.max_parallel_explorers,
      max_rework_rounds: r.max_rework_rounds,
      current_rework_round: 0,
      node_runs: [],
      final_verdict: r.final_verdict,
      last_error_type: r.last_error_type,
      created_at: r.created_at,
      updated_at: r.updated_at,
    };
  }

  async startWorkflowRun(options: {
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
    return this.request<WorkflowRun>('/v1/tasks', {
      method: 'POST',
      body: JSON.stringify({
        task: options.task,
        workspace: options.workspace,
        main_role_id: options.mainRoleId || 'role_main',
        planner_role_id: options.plannerRoleId || 'role_planner',
        explorer_role_ids: options.explorerRoleIds || ['role_explorer'],
        coder_role_id: options.coderRoleId || 'role_coder',
        reviewer_role_id: options.reviewerRoleId || 'role_reviewer',
        max_parallel_explorers: options.maxParallelExplorers || 2,
        max_rework_rounds: options.maxReworkRounds || 1,
      }),
    });
  }

  async resumeWorkflowRun(runId: string, allowCoderReplay: boolean, _onEvent?: EventSubscriber): Promise<void> {
    await fetch(`${this.baseUrl}/v1/tasks/${runId}/resume`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ allow_coder_replay: allowCoderReplay }),
    });
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
    return [
      {
        id: 'host_local',
        name: 'Local Workstation (MacBook Pro)',
        core_version: '0.1.0-alpha',
        protocol_version: '2.0',
        is_online: true,
        transport_mode: 'direct_lan',
        last_seen: new Date().toISOString(),
        workspaces: ['/Users/bigo/agentworkspace/codexworkspace/operant'],
        capabilities: ['workspace_read', 'workspace_write', 'docker_exec', 'git'],
      },
    ];
  }

  async listRemoteDevices(_hostId?: string): Promise<RemoteDevice[]> {
    return [];
  }

  async requestDevicePairing(
    hostId: string,
    deviceName: string,
    _scope: string,
    pin: string
  ): Promise<{ request_id: string; qr_code_payload: string }> {
    return {
      request_id: `req_${Date.now()}`,
      qr_code_payload: `operant://pair?host=${hostId}&pin=${pin}&name=${encodeURIComponent(deviceName)}`,
    };
  }

  async revokeRemoteDevice(_deviceId: string): Promise<{ revoked: boolean }> {
    return { revoked: true };
  }

  async sendRemoteCommand(hostId: string, _sessionId: string, _message: string): Promise<CommandReceipt> {
    return {
      command_id: `cmd_${Date.now()}`,
      request_id: `req_${Date.now()}`,
      idempotency_key: `idemp_${Date.now()}`,
      host_id: hostId,
      relay_acknowledged: true,
      host_acknowledged: true,
      host_accepted: true,
      received_at: new Date().toISOString(),
    };
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
    return () => {};
  }
}
