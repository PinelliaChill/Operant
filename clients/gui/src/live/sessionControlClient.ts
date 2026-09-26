import { currentBrowserOrigin } from '../lib/liveBaseUrl.ts';
import { Phase3Client } from '../../../../sdk/typescript-client/phase3.generated';

let phase3: Phase3Client | null = null;
function core(): Phase3Client {
  if (!phase3) phase3 = new Phase3Client(currentBrowserOrigin());
  return phase3;
}

export type ConfigField = 'system_prompt' | 'model_profile_id' | 'effort' | 'tool_policy' | 'budget' | 'temperature' | 'skill_ids' | 'mcp_server_ids' | 'approval_reviewer';
export type ConfigScope = 'global' | 'project' | 'workspace' | 'role';
export type ConfigValues = Partial<Record<ConfigField, unknown>>;
export interface ConfigSource { scope_type: ConfigScope | 'role_base' | 'run'; scope_id: string }
export interface EffectiveConfig {
  values: ConfigValues;
  sources: Partial<Record<ConfigField, ConfigSource>>;
  prompt_sources: ConfigSource[];
  revisions: Record<string, number>;
  scopes: Partial<Record<ConfigScope, ConfigOverride & { scope_type: ConfigScope; scope_id: string; updated_at?: string }>>;
  applies_to: 'new_sessions';
}
export interface ConfigOverride { patch: ConfigValues; revision: number }
export interface BtwRun {
  id: string;
  status: 'running' | 'completed' | 'failed' | 'promoted';
  response: string | null;
  error_code: string | null;
  promoted_turn_id: string | null;
  promoted_item_id: string | null;
}
export interface GoalRecord {
  id: string; owner_thread_id: string; objective: string;
  status: 'active' | 'blocked' | 'completed' | 'cancelled';
  token_budget: number | null; cost_budget: number | null; time_budget_seconds: number | null;
  completion_criteria: string[]; linked_thread_ids: string[]; linked_workflow_run_ids: string[];
  blocked_reason: string | null; result_ref: string | null; revision: number;
}
export interface PlanRecord {
  id: string; goal_id: string; scope: string;
  assumptions: string[]; constraints: string[]; open_questions: string[];
  proposed_changes: string[]; risk_items: string[]; approval_requirements: string[]; verification_plan: string[];
  source_mode: 'read_only'; status: 'draft' | 'in_review' | 'approved' | 'in_progress' | 'completed' | 'superseded'; revision: number;
}
export interface ChecklistRecord {
  id: string; plan_id: string; description: string; dependencies: string[];
  status: 'todo' | 'in_progress' | 'blocked' | 'done' | 'skipped';
  evidence_refs: string[]; blocker: string | null; revision: number;
}

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Core 返回的数据格式无效');
  return value as Record<string, unknown>;
}

async function request(path: string, init?: RequestInit): Promise<unknown> {
  const response = await fetch(`${currentBrowserOrigin()}${path}`, {
    ...init,
    headers: { Accept: 'application/json', ...(init?.body ? { 'Content-Type': 'application/json' } : {}), ...init?.headers },
  });
  const data: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = data && typeof data === 'object' && 'detail' in data ? (data as { detail: unknown }).detail : null;
    throw new Error(typeof detail === 'string' ? detail : `Core 请求失败（HTTP ${response.status}）`);
  }
  return data;
}

export const sessionControlClient = {
  async effective(query: { project_id?: string; workspace_ref?: string; role_id: string }): Promise<EffectiveConfig> {
    const data = object(await core().getEffectiveConfig({ roleId: query.role_id, projectId: query.project_id, workspaceRef: query.workspace_ref }));
    if (data.applies_to !== 'new_sessions') throw new Error('Core 未返回明确的配置生效边界');
    return {
      values: object(data.values) as ConfigValues,
      sources: object(data.sources) as EffectiveConfig['sources'],
      prompt_sources: Array.isArray(data.prompt_sources) ? data.prompt_sources as ConfigSource[] : [],
      revisions: object(data.revisions) as EffectiveConfig['revisions'],
      scopes: object(data.scopes) as EffectiveConfig['scopes'],
      applies_to: 'new_sessions',
    };
  },
  async getOverride(scope: ConfigScope, id: string): Promise<ConfigOverride> {
    const data = object(await core().getConfigScope(scope, id));
    if (typeof data.revision !== 'number') throw new Error('Core 未返回配置修订号');
    return { patch: object(data.patch) as ConfigValues, revision: data.revision };
  },
  async saveOverride(scope: ConfigScope, id: string, patch: ConfigValues, revision: number): Promise<ConfigOverride> {
    const data = object(await core().putConfigScope(scope, id, { patch, expected_revision: revision }, { idempotencyKey: crypto.randomUUID() }));
    if (typeof data.revision !== 'number') throw new Error('Core 未返回保存后的配置修订号');
    return { patch: object(data.patch) as ConfigValues, revision: data.revision };
  },
  async resetOverride(scope: ConfigScope, id: string, revision: number): Promise<void> {
    await core().deleteConfigScope(scope, id, { expectedRevision: revision, idempotencyKey: crypto.randomUUID() });
  },
  async startBtw(input: { session_id: string; thread_id: string; workspace: string; prompt: string }, onStarted: (id: string) => void): Promise<BtwRun> {
    const response = await fetch(`${currentBrowserOrigin()}/v1/sidecars/btw`, {
      method: 'POST', headers: { Accept: 'text/event-stream', 'Content-Type': 'application/json' }, body: JSON.stringify(input),
    });
    if (!response.ok || !response.body) throw new Error(`BTW 启动失败（HTTP ${response.status}）`);
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let id = '';
    const consume = (frame: string) => {
      const dataLine = frame.split('\n').filter((line) => line.startsWith('data:')).map((line) => line.slice(5).trim()).join('\n');
      if (!dataLine) return;
      const event = object(JSON.parse(dataLine));
      if (event.event_type === 'btw.stream_error') {
        const detail = event.payload && typeof event.payload === 'object' ? event.payload as Record<string, unknown> : event;
        throw new Error(typeof detail.message === 'string' ? detail.message : 'BTW 模型调用失败');
      }
      if (typeof event.sidecar_run_id === 'string' && !id) { id = event.sidecar_run_id; onStarted(id); }
    };
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done }).replaceAll('\r\n', '\n');
      let boundary = buffer.indexOf('\n\n');
      while (boundary >= 0) { consume(buffer.slice(0, boundary)); buffer = buffer.slice(boundary + 2); boundary = buffer.indexOf('\n\n'); }
      if (done) break;
    }
    if (buffer.trim()) consume(buffer);
    if (!id) throw new Error('BTW 流结束，但 Core 未返回运行 ID');
    return this.getBtw(id);
  },
  async getBtw(id: string): Promise<BtwRun> {
    const data = object(await request(`/v1/sidecars/btw/${encodeURIComponent(id)}`));
    if (typeof data.id !== 'string' || !['running', 'completed', 'failed', 'promoted'].includes(String(data.status))) throw new Error('Core BTW Projection 格式无效');
    return data as unknown as BtwRun;
  },
  async promoteBtw(id: string): Promise<BtwRun> {
    await request(`/v1/sidecars/btw/${encodeURIComponent(id)}/promote`, {
      method: 'POST', headers: { 'Idempotency-Key': crypto.randomUUID() },
    });
    return this.getBtw(id);
  },
  async listGoals(threadId: string): Promise<GoalRecord[]> {
    const data = await core().listGoals({ ownerThreadId: threadId });
    if (!Array.isArray(data)) throw new Error('Core Goal 列表格式无效');
    return data as GoalRecord[];
  },
  async createGoal(input: { owner_thread_id: string; objective: string; completion_criteria?: string[]; token_budget?: number; cost_budget?: number; time_budget_seconds?: number }): Promise<GoalRecord> {
    return object(await core().createGoal(input, { idempotencyKey: crypto.randomUUID() })) as unknown as GoalRecord;
  },
  async updateGoal(id: string, revision: number, changes: Partial<GoalRecord>): Promise<GoalRecord> {
    return object(await core().updateGoal(id, { expected_revision: revision, changes }, { idempotencyKey: crypto.randomUUID() })) as unknown as GoalRecord;
  },
  async listPlans(goalId: string): Promise<PlanRecord[]> {
    const data = await core().listPlans(goalId);
    if (!Array.isArray(data)) throw new Error('Core Plan 列表格式无效');
    return data as PlanRecord[];
  },
  async createPlan(goalId: string, input: { scope: string }): Promise<PlanRecord> {
    return object(await core().createPlan(goalId, { goal_id: goalId, ...input }, { idempotencyKey: crypto.randomUUID() })) as unknown as PlanRecord;
  },
  async generatePlan(goalId: string, sourceSessionId: string): Promise<PlanRecord> {
    const result = object(await core().generatePlanDraft(
      goalId,
      { source_session_id: sourceSessionId, planner_role_id: 'role_planner' },
      { idempotencyKey: crypto.randomUUID() },
    ));
    return object(result.plan) as unknown as PlanRecord;
  },
  async updatePlan(id: string, revision: number, changes: Partial<PlanRecord>): Promise<PlanRecord> {
    return object(await core().updatePlan(id, { expected_revision: revision, changes }, { idempotencyKey: crypto.randomUUID() })) as unknown as PlanRecord;
  },
  async listChecklist(planId: string): Promise<ChecklistRecord[]> {
    const data = await core().listChecklistItems(planId);
    if (!Array.isArray(data)) throw new Error('Core Checklist 列表格式无效');
    return data as ChecklistRecord[];
  },
  async createChecklist(planId: string, description: string): Promise<ChecklistRecord> {
    return object(await core().createChecklistItem(planId, { plan_id: planId, description }, { idempotencyKey: crypto.randomUUID() })) as unknown as ChecklistRecord;
  },
  async updateChecklistStatus(id: string, revision: number, status: ChecklistRecord['status'], options: { evidence_refs?: string[]; blocker?: string } = {}): Promise<ChecklistRecord> {
    return object(await core().commandChecklistStatus(id, { expected_revision: revision, status, ...options }, { idempotencyKey: crypto.randomUUID() })) as unknown as ChecklistRecord;
  },
};
