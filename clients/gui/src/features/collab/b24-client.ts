import type * as B24 from '../../../../../sdk/typescript-client/b2_4.generated';

/**
 * The B2-4 additive client is generated from the collaboration schema.  This
 * narrow facade keeps the view independent from the generated module while
 * that module evolves alongside the Core contract.
 */
export type CollaborationRole = B24.CollaborationRole;
export type CollaborationTeamMember = B24.TeamMember;
export type CollaborationTeam = B24.TeamDefinition;
export type CollaborationGraphRun = B24.CollaborationGraphRun;
export type CollaborationDirectory = Omit<B24.CollaborationDirectory, 'graph_runs' | 'graph_runs_has_more' | 'graph_runs_next_cursor'> & {
  graph_runs: CollaborationGraphRun[];
  graph_runs_has_more: boolean;
  graph_runs_next_cursor: string | null;
};
export type B24Command = B24.B24Command;
export type B24Result = B24.B24Result;
export type B24ClientLike = Pick<B24.B24Client, 'getCollaborationDirectory' | 'execute'>;

export function workflowKey(workflow: Pick<B24.WorkflowDefinition, 'workflow_id' | 'version'>): string {
  return `${workflow.workflow_id ?? ''}:v${workflow.version ?? ''}`;
}

export function teamKey(team: Pick<CollaborationTeam, 'team_id' | 'version'>): string {
  return `${team.team_id}:v${team.version}`;
}

export function workflowLabel(workflow: B24.WorkflowDefinition): string {
  return `${workflow.name} · v${workflow.version ?? '—'}`;
}

export function teamLabel(team: CollaborationTeam): string {
  const coordinator = team.members.find((member) => member.member_id === team.default_coordinator);
  const label = coordinator?.role || team.members[0]?.role || 'Team';
  return `${label} · ${team.members.length} 名成员 · v${team.version ?? '—'}`;
}

export function roleLabel(role: CollaborationRole): string {
  const model = role.model_id ? ` · ${role.model_id}` : '';
  return `${role.name}${model}`;
}

function object(value: unknown, label: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error(`${label} 返回格式无效。`);
  }
  return value as Record<string, unknown>;
}

function requiredString(value: Record<string, unknown>, key: string, label: string): string {
  const candidate = value[key];
  if (typeof candidate !== 'string' || !candidate.trim()) {
    throw new Error(`${label} 缺少 ${key}。`);
  }
  return candidate;
}

function requiredArray(value: Record<string, unknown>, key: string, label: string): unknown[] {
  if (!Array.isArray(value[key])) throw new Error(`${label} 缺少 ${key} 列表。`);
  return value[key] as unknown[];
}

function requiredBoolean(value: Record<string, unknown>, key: string, label: string): boolean {
  if (typeof value[key] !== 'boolean') throw new Error(`${label} 缺少 ${key}。`);
  return value[key] as boolean;
}

const GRAPH_RUN_STATUSES: readonly B24.GraphRunStatus[] = [
  'created', 'queued', 'running', 'waiting_input', 'waiting_approval',
  'interrupted', 'manual_reconcile_required', 'completed', 'failed', 'cancelled',
];

function graphRunSummary(raw: unknown): CollaborationGraphRun {
  const run = object(raw, 'Graph Run 摘要');
  const id = requiredString(run, 'id', 'Graph Run 摘要');
  const workflowId = requiredString(run, 'workflow_definition_id', `Graph Run ${id}`);
  const version = run.workflow_definition_version;
  if (!Number.isInteger(version) || Number(version) < 1) {
    throw new Error(`Graph Run ${id} 的 workflow_definition_version 无效。`);
  }
  const workspace = run.workspace_or_target;
  if (workspace !== null && typeof workspace !== 'string') {
    throw new Error(`Graph Run ${id} 的 workspace_or_target 无效。`);
  }
  const teamRunId = run.team_run_id;
  if (teamRunId !== null && typeof teamRunId !== 'string') {
    throw new Error(`Graph Run ${id} 的 team_run_id 无效。`);
  }
  if (typeof run.status !== 'string' || !GRAPH_RUN_STATUSES.includes(run.status as B24.GraphRunStatus)) {
    throw new Error(`Graph Run ${id} 的 status 无效。`);
  }
  const updatedAt = requiredString(run, 'updated_at', `Graph Run ${id}`);
  return {
    id,
    workflow_definition_id: workflowId,
    workflow_definition_version: Number(version),
    workspace_or_target: workspace as string | null,
    team_run_id: teamRunId as string | null,
    status: run.status as B24.GraphRunStatus,
    updated_at: updatedAt,
  };
}

/** Validate the Core projection once at the boundary; never invent IDs. */
export function normalizeCollaborationDirectory(value: unknown): CollaborationDirectory {
  const projection = object(value, '协作目录');
  const workflows = requiredArray(projection, 'workflows', '协作目录').map((raw) => {
    const workflow = object(raw, 'Workflow Definition');
    const workflowId = requiredString(workflow, 'workflow_id', 'Workflow Definition');
    const version = workflow.version;
    if (!Number.isInteger(version) || Number(version) < 1) throw new Error(`Workflow ${workflowId} 版本无效。`);
    if (typeof workflow.name !== 'string' || !workflow.name.trim()) throw new Error(`Workflow ${workflowId} 缺少名称。`);
    if (!Array.isArray(workflow.nodes)) throw new Error(`Workflow ${workflowId} 缺少节点列表。`);
    return workflow as unknown as B24.WorkflowDefinition;
  });
  const roles = requiredArray(projection, 'roles', '协作目录').map((raw) => {
    const role = object(raw, 'RolePreset');
    const id = requiredString(role, 'id', 'RolePreset');
    requiredString(role, 'name', `RolePreset ${id}`);
    requiredString(role, 'model_profile_id', `RolePreset ${id}`);
    return role as unknown as CollaborationRole;
  });
  const teams = requiredArray(projection, 'teams', '协作目录').map((raw) => {
    const team = object(raw, 'Team Definition');
    const id = requiredString(team, 'team_id', 'Team Definition');
    const version = team.version;
    if (!Number.isInteger(version) || Number(version) < 1) throw new Error(`Team ${id} 版本无效。`);
    const members = requiredArray(team, 'members', `Team ${id}`).map((memberRaw) => {
      const member = object(memberRaw, 'Team Member');
      const memberId = requiredString(member, 'member_id', `Team ${id} member`);
      requiredString(member, 'agent_definition_id', `Team ${id} member ${memberId}`);
      requiredString(member, 'role', `Team ${id} member ${memberId}`);
      return member as unknown as CollaborationTeamMember;
    });
    if (members.length === 0) throw new Error(`Team ${id} 没有可用成员。`);
    requiredString(team, 'default_coordinator', `Team ${id}`);
    return { ...team, members } as unknown as CollaborationTeam;
  });
  const graphRuns = requiredArray(projection, 'graph_runs', '协作目录').map(graphRunSummary);
  const graphRunsHasMore = requiredBoolean(projection, 'graph_runs_has_more', '协作目录');
  const cursor = projection.graph_runs_next_cursor;
  if (cursor !== null && (typeof cursor !== 'string' || !cursor)) throw new Error('协作目录的翻页 Cursor 无效。');
  if (graphRunsHasMore !== (cursor !== null)) throw new Error('协作目录的翻页状态不一致。');
  return { workflows, teams, roles, graph_runs: graphRuns, graph_runs_has_more: graphRunsHasMore, graph_runs_next_cursor: cursor as string | null };
}

export function filterGraphRunsByWorkspace(runs: CollaborationGraphRun[], workspace: string): CollaborationGraphRun[] {
  const currentWorkspace = workspace.trim();
  if (!currentWorkspace) return [];
  return runs
    .filter((run) => run.workspace_or_target === currentWorkspace)
    .slice()
    .sort((left, right) => right.updated_at.localeCompare(left.updated_at));
}

export function graphRunSummaryFromProjection(run: {
  id: string;
  workflow_definition_id: string;
  workflow_definition_version: number;
  workspace_or_target?: string | null;
  team_run_id?: string | null;
  status: B24.GraphRunStatus;
  updated_at: string;
}): CollaborationGraphRun {
  return {
    id: run.id,
    workflow_definition_id: run.workflow_definition_id,
    workflow_definition_version: run.workflow_definition_version,
    workspace_or_target: run.workspace_or_target ?? null,
    team_run_id: run.team_run_id ?? null,
    status: run.status,
    updated_at: run.updated_at,
  };
}

export function commandResourceId(result: B24Result, label: string): string {
  if (result.status !== 'completed' || !result.resource_id.trim()) {
    throw new Error(`${label} 未返回可用资源。`);
  }
  return result.resource_id;
}



/** Preserve historical Roster rows, but target the newest Agent for each member. */
export function currentRoster<T extends { member_id: string; joined_at?: string | null }>(entries: T[]): T[] {
  const members = new Map<string, T>();
  for (const entry of entries) {
    const previous = members.get(entry.member_id);
    if (!previous || (entry.joined_at ?? '') > (previous.joined_at ?? '')) members.set(entry.member_id, entry);
  }
  return [...members.values()];
}

/** Keyset pages can overlap when a run is updated; retain one current summary per ID. */
export function mergeGraphRunPages(current: CollaborationGraphRun[], incoming: CollaborationGraphRun[]): CollaborationGraphRun[] {
  const byId = new Map(current.map((run) => [run.id, run]));
  incoming.forEach((run) => byId.set(run.id, run));
  return [...byId.values()].sort((left, right) => right.updated_at.localeCompare(left.updated_at) || right.id.localeCompare(left.id));
}
