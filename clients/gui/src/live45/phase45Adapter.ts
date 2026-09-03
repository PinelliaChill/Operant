import type * as Phase45 from '../../../../sdk/typescript-client/phase45.generated.ts';

export interface Phase45ClientLike {
  negotiateProtocol(force?: boolean): Promise<unknown>;
  listSkills(options?: Phase45.ListSkillsOptions): Promise<Record<string, unknown>>;
  discoverSkills(request: Phase45.SkillDiscoverBody, options?: Phase45.DiscoverSkillsOptions): Promise<Record<string, unknown>>;
  listMcpServers(): Promise<Record<string, unknown>>;
  createMcpServer(request: Phase45.McpServerBody, options?: Phase45.CreateMcpServerOptions): Promise<Record<string, unknown>>;
  startMcpServer(id: string, options?: Phase45.StartMcpServerOptions): Promise<Record<string, unknown>>;
  stopMcpServer(id: string, options?: Phase45.StopMcpServerOptions): Promise<Record<string, unknown>>;
  deleteMcpServer(id: string, options?: Phase45.DeleteMcpServerOptions): Promise<Record<string, unknown>>;
  listMcpTools(id: string): Promise<Record<string, unknown>>;
  explainPolicy(request: Phase45.NormalizeActionBody, options?: Phase45.ExplainPolicyOptions): Promise<Record<string, unknown>>;
  listSecurityAudit(
    actionHash: string,
    options?: Phase45.ListSecurityAuditOptions,
  ): Promise<Record<string, unknown>>;
}

export interface Phase45UiError {
  code: string;
  message: string;
  retryable: boolean;
  recovery: string;
}

export interface LiveSkillCandidate {
  id: string;
  name: string;
  description: string;
  rootRef: string;
  relativeDirectory: string;
  manifestSha256: string;
  trustStatus: 'untrusted_candidate';
  discoveredAt: string;
  resources: Array<{ relativePath: string; kind: string; sizeBytes: number; sha256: string }>;
}

export interface LiveSkillIssue {
  rootIndex: number;
  relativeDirectory: string;
  code: string;
  message: string;
}

export type McpLifecycle = 'stopped' | 'starting' | 'running' | 'failed';

export interface LiveMcpServer {
  id: string;
  transport: 'stdio' | 'legacy_sse';
  endpointRef?: string;
  secretRef?: string;
  stdioArgv: string[];
  cwdRef?: string;
  environmentRefs: Record<string, string>;
  allowLoopbackHttp: boolean;
  lifecycle: McpLifecycle;
  createdAt: string;
  updatedAt: string;
}

export interface LiveMcpTool {
  name: string;
  description?: string;
  schemaSha256?: string;
}

export interface LivePolicyEvaluation {
  actionHash: string;
  decision: 'allow' | 'ask' | 'deny';
  reasonCode: string;
  riskLevel: string;
  hardDeny: boolean;
  policyVersion?: string;
  matchedRuleIds: string[];
  explanation?: unknown;
  remediation?: unknown;
}

export interface LiveSecurityAuditFact {
  eventId: string;
  cursor: number | bigint;
  principal: string;
  eventType: string;
  decision?: 'allow' | 'ask' | 'deny';
  ruleIds: string[];
  createdAt: string;
}

function record(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(`${label} 必须是对象。`);
  return value as Record<string, unknown>;
}

function text(value: unknown, label: string): string {
  if (typeof value !== 'string' || value.length === 0) throw new Error(`${label} 缺失。`);
  return value;
}

function optionalText(value: unknown): string | undefined {
  return typeof value === 'string' && value.length > 0 ? value : undefined;
}

function items(value: unknown, label: string): unknown[] {
  const source = record(value, label);
  if (!Array.isArray(source.items)) throw new Error(`${label}.items 缺失。`);
  return source.items;
}

export function mapSkillCandidate(value: unknown): LiveSkillCandidate {
  const source = record(value, 'Skill candidate');
  if (source.trust_status !== 'untrusted_candidate') throw new Error('Skill candidate 的信任状态无效。');
  const resources = Array.isArray(source.resources) ? source.resources.map((entry) => {
    const item = record(entry, 'Skill resource');
    return {
      relativePath: text(item.relative_path, 'Skill resource.relative_path'),
      kind: text(item.kind, 'Skill resource.kind'),
      sizeBytes: typeof item.size_bytes === 'number' ? item.size_bytes : 0,
      sha256: text(item.sha256, 'Skill resource.sha256'),
    };
  }) : [];
  return {
    id: text(source.candidate_id, 'Skill candidate.candidate_id'),
    name: text(source.name, 'Skill candidate.name'),
    description: text(source.description, 'Skill candidate.description'),
    rootRef: text(source.root_ref, 'Skill candidate.root_ref'),
    relativeDirectory: text(source.relative_directory, 'Skill candidate.relative_directory'),
    manifestSha256: text(source.manifest_sha256, 'Skill candidate.manifest_sha256'),
    trustStatus: 'untrusted_candidate',
    discoveredAt: text(source.discovered_at, 'Skill candidate.discovered_at'),
    resources,
  };
}

export function mapSkillList(value: unknown): LiveSkillCandidate[] {
  return items(value, 'Skill list').map(mapSkillCandidate);
}

export function mapSkillDiscovery(value: unknown): { candidates: LiveSkillCandidate[]; issues: LiveSkillIssue[] } {
  const source = record(value, 'Skill discovery');
  if (!Array.isArray(source.candidates) || !Array.isArray(source.issues)) throw new Error('Skill discovery 投影不完整。');
  return {
    candidates: source.candidates.map(mapSkillCandidate),
    issues: source.issues.map((entry) => {
      const issue = record(entry, 'Skill issue');
      return {
        rootIndex: typeof issue.root_index === 'number' ? issue.root_index : 0,
        relativeDirectory: text(issue.relative_directory, 'Skill issue.relative_directory'),
        code: text(issue.code, 'Skill issue.code'),
        message: text(issue.message, 'Skill issue.message'),
      };
    }),
  };
}

export function mapMcpServer(value: unknown): LiveMcpServer {
  const source = record(value, 'MCP server');
  const transport = source.transport;
  const lifecycle = source.lifecycle_status;
  if (transport !== 'stdio' && transport !== 'legacy_sse') throw new Error('MCP transport 无效。');
  if (!['stopped', 'starting', 'running', 'failed'].includes(String(lifecycle))) throw new Error('MCP lifecycle 无效。');
  return {
    id: text(source.server_id, 'MCP server.server_id'),
    transport,
    endpointRef: optionalText(source.endpoint_ref),
    secretRef: optionalText(source.secret_ref),
    stdioArgv: Array.isArray(source.stdio_argv) ? source.stdio_argv.filter((item): item is string => typeof item === 'string') : [],
    cwdRef: optionalText(source.cwd_ref),
    environmentRefs: source.environment_refs && typeof source.environment_refs === 'object' && !Array.isArray(source.environment_refs)
      ? Object.fromEntries(Object.entries(source.environment_refs).filter((entry): entry is [string, string] => typeof entry[1] === 'string'))
      : {},
    allowLoopbackHttp: source.allow_loopback_http === true,
    lifecycle: lifecycle as McpLifecycle,
    createdAt: text(source.created_at, 'MCP server.created_at'),
    updatedAt: text(source.updated_at, 'MCP server.updated_at'),
  };
}

export function mapMcpServerList(value: unknown): LiveMcpServer[] {
  return items(value, 'MCP server list').map(mapMcpServer);
}

export function mapMcpTools(value: unknown): LiveMcpTool[] {
  return items(value, 'MCP tool list').map((entry) => {
    const tool = record(entry, 'MCP tool');
    return { name: text(tool.name, 'MCP tool.name'), description: optionalText(tool.description), schemaSha256: optionalText(tool.schema_sha256) };
  });
}

export function mapPolicyEvaluation(value: unknown): LivePolicyEvaluation {
  const source = record(value, 'Policy response');
  const evaluation = record(source.evaluation, 'Policy evaluation');
  const decision = evaluation.decision;
  if (decision !== 'allow' && decision !== 'ask' && decision !== 'deny') throw new Error('Policy decision 无效。');
  return {
    actionHash: text(evaluation.action_hash, 'Policy evaluation.action_hash'),
    decision,
    reasonCode: text(evaluation.reason_code, 'Policy evaluation.reason_code'),
    riskLevel: text(evaluation.risk_level, 'Policy evaluation.risk_level'),
    hardDeny: evaluation.hard_deny === true,
    policyVersion: optionalText(evaluation.policy_version),
    matchedRuleIds: Array.isArray(evaluation.matched_rule_ids) ? evaluation.matched_rule_ids.filter((item): item is string => typeof item === 'string') : [],
    explanation: source.explanation,
    remediation: source.remediation,
  };
}

export function mapSecurityAudit(value: unknown): LiveSecurityAuditFact[] {
  return items(value, 'Security audit').map((entry) => {
    const event = record(entry, 'Security audit event');
    const rawCursor = event.cursor;
    const cursor = typeof rawCursor === 'bigint'
      ? rawCursor
      : typeof rawCursor === 'number' && Number.isSafeInteger(rawCursor) && rawCursor >= 1
        ? rawCursor
        : typeof rawCursor === 'string' && /^[1-9][0-9]*$/.test(rawCursor)
          ? BigInt(rawCursor)
          : undefined;
    if (cursor === undefined) throw new Error('Security audit event.cursor 无效。');
    const decision = event.decision;
    if (decision !== null && decision !== undefined && decision !== 'allow' && decision !== 'ask' && decision !== 'deny') {
      throw new Error('Security audit event.decision 无效。');
    }
    return {
      eventId: text(event.event_id, 'Security audit event.event_id'),
      cursor,
      principal: text(event.principal, 'Security audit event.principal'),
      eventType: text(event.event_type, 'Security audit event.event_type'),
      decision: decision ?? undefined,
      ruleIds: Array.isArray(event.rule_ids) ? event.rule_ids.filter((item): item is string => typeof item === 'string') : [],
      createdAt: text(event.created_at, 'Security audit event.created_at'),
    };
  });
}

export function normalizePhase45Error(error: unknown): Phase45UiError {
  if (error && typeof error === 'object') {
    const source = error as Record<string, unknown>;
    if (typeof source.code === 'string' && typeof source.message === 'string') {
      return {
        code: source.code,
        message: source.message,
        retryable: source.retryable === true,
        recovery: typeof source.recovery === 'string' ? source.recovery : 'none',
      };
    }
  }
  return { code: 'invalid_phase45_projection', message: error instanceof Error ? error.message : 'Phase 4/5 返回了无法识别的结果。', retryable: false, recovery: 'none' };
}

export class Phase45LiveAdapter {
  private readonly client: Phase45ClientLike;
  private readonly mutationKeys = new Map<string, string>();

  constructor(client: Phase45ClientLike) {
    this.client = client;
  }
  connect() { return this.client.negotiateProtocol(true); }
  async listSkills() { return mapSkillList(await this.client.listSkills({ limit: 200 })); }
  async discoverSkills(rootRefs: string[] = []) {
    const action = `discover:${JSON.stringify(rootRefs)}`;
    return mapSkillDiscovery(await this.mutate(action, (idempotencyKey) => (
      this.client.discoverSkills({ root_refs: rootRefs }, { idempotencyKey })
    )));
  }
  async listMcpServers() { return mapMcpServerList(await this.client.listMcpServers()); }
  async createMcpServer(request: Phase45.McpServerBody) {
    return mapMcpServer(await this.mutate(
      `mcp:create:${JSON.stringify(request)}`,
      (idempotencyKey) => this.client.createMcpServer(request, { idempotencyKey }),
    ));
  }
  async startMcpServer(id: string) {
    return mapMcpServer(await this.mutate(
      `mcp:start:${id}`,
      (idempotencyKey) => this.client.startMcpServer(id, { idempotencyKey }),
    ));
  }
  async stopMcpServer(id: string) {
    return mapMcpServer(await this.mutate(
      `mcp:stop:${id}`,
      (idempotencyKey) => this.client.stopMcpServer(id, { idempotencyKey }),
    ));
  }
  async deleteMcpServer(id: string) {
    await this.mutate(
      `mcp:delete:${id}`,
      (idempotencyKey) => this.client.deleteMcpServer(id, { idempotencyKey }),
    );
  }
  async listMcpTools(id: string) { return mapMcpTools(await this.client.listMcpTools(id)); }
  async explainPolicy(request: Phase45.NormalizeActionBody) {
    return mapPolicyEvaluation(await this.mutate(
      `policy:explain:${request.idempotency_key}`,
      (idempotencyKey) => this.client.explainPolicy(request, { idempotencyKey }),
      request.idempotency_key,
    ));
  }
  async listSecurityAudit(actionHash: string, afterCursor = 0, limit = 100) {
    return mapSecurityAudit(await this.client.listSecurityAudit(actionHash, { afterCursor, limit }));
  }

  private async mutate<T>(
    action: string,
    operation: (idempotencyKey: string) => Promise<T>,
    preferredKey?: string,
  ): Promise<T> {
    let key = this.mutationKeys.get(action);
    if (!key) {
      key = preferredKey ?? globalThis.crypto.randomUUID();
      this.mutationKeys.set(action, key);
    }
    try {
      const result = await operation(key);
      this.mutationKeys.delete(action);
      return result;
    } catch (error: unknown) {
      const recovery = error && typeof error === 'object'
        ? (error as { recovery?: unknown }).recovery
        : undefined;
      if (recovery === 'use_new_idempotency_key') this.mutationKeys.delete(action);
      throw error;
    }
  }
}
