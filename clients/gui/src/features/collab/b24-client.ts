import type * as B24 from '../../../../../sdk/typescript-client/b2_4.generated';

/**
 * The B2-4 additive client is generated from the collaboration schema.  This
 * narrow facade keeps the view independent from the generated module while
 * that module evolves alongside the Core contract.
 */
export type CollaborationRole = B24.CollaborationRole;
export type CollaborationTeamMember = B24.TeamMember;
export type CollaborationTeam = B24.TeamDefinition;
export type CollaborationDirectory = B24.CollaborationDirectory;
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
  return { workflows, teams, roles };
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
