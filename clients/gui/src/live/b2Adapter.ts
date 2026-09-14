import type * as B2 from '../../../../sdk/typescript-client/b2.generated';

/**
 * The B2 client is generated from the additive B2 Schema.  Keep the adapter
 * deliberately small: it validates the public projection at the GUI
 * boundary, translates transport errors, and does not invent data for older
 * clients or for the Demo surface.
 */
export interface B2ClientLike {
  readonly protocolVersion: string;
  readonly schemaDigest: string;
  negotiateProtocol(force?: boolean): Promise<B2.ProtocolNegotiation>;
  createThread(
    request: B2.B2CreateThread,
    options?: B2.CreateThreadOptions,
  ): Promise<B2.ConversationThread>;
  listModels(): Promise<Array<B2.ModelProfile>>;
  createModel(request: B2.CreateModelProfileRequest, options?: B2.CreateModelOptions): Promise<B2.ModelProfile>;
  updateModel(
    profileId: string,
    request: B2.UpdateModelProfileRequest,
    options?: B2.UpdateModelOptions,
  ): Promise<B2.ModelProfile>;
  discoverModels(
    request: B2.DiscoverModelsRequest,
    options?: B2.DiscoverModelsOptions,
  ): Promise<B2.B2Discovery>;
  listRoles(options?: B2.ListRolesOptions): Promise<Array<B2.RolePreset>>;
  createRole(request: B2.CreateRoleRequest, options?: B2.CreateRoleOptions): Promise<B2.RolePreset>;
  updateRole(
    roleId: string,
    request: B2.UpdateRoleRequest,
    options?: B2.UpdateRoleOptions,
  ): Promise<B2.RolePreset>;
  cancelSession(sessionId: string, options?: B2.CancelSessionOptions): Promise<B2.B2Cancellation>;
  listAgents(options?: B2.ListAgentsOptions): Promise<B2.B2AgentPage>;
  listTasks(options?: B2.ListTasksOptions): Promise<B2.B2TaskPage>;
  getTask(sourceId: string, options?: B2.GetTaskOptions): Promise<B2.B2Task>;
  getSessionHistory(
    sessionId: string,
    options?: B2.GetSessionHistoryOptions,
  ): Promise<B2.B2SessionHistory>;
}

export interface B2UiError {
  code: string;
  message: string;
  retryable: boolean;
  recovery: string;
  detail?: unknown;
}

export class B2AdapterError extends Error {
  readonly detail: B2UiError;

  constructor(detail: B2UiError) {
    super(detail.message);
    this.name = 'B2AdapterError';
    this.detail = detail;
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function optionalText(value: unknown): string | undefined {
  return typeof value === 'string' && value.length > 0 ? value : undefined;
}

function requiredText(value: unknown, label: string): string {
  const result = optionalText(value);
  if (!result) {
    throw new B2AdapterError({
      code: 'invalid_b2_projection',
      message: `Core 返回的 ${label} 缺失。`,
      retryable: false,
      recovery: 'none',
      detail: value,
    });
  }
  return result;
}

function nullableText(value: unknown, label: string): string | null {
  if (value === null) return null;
  return requiredText(value, label);
}

function optionalFiniteNumber(value: unknown, label: string): number | undefined {
  if (value === undefined || value === null) return undefined;
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new B2AdapterError({
      code: 'invalid_b2_projection',
      message: `Core 返回的 ${label} 无效。`,
      retryable: false,
      recovery: 'none',
      detail: value,
    });
  }
  return value;
}

function requiredFiniteNumber(value: unknown, label: string): number {
  const result = optionalFiniteNumber(value, label);
  if (result === undefined) {
    throw new B2AdapterError({
      code: 'invalid_b2_projection',
      message: `Core 返回的 ${label} 缺失。`,
      retryable: false,
      recovery: 'none',
      detail: value,
    });
  }
  return result;
}

function effort(value: unknown, label: string): B2.Effort | undefined {
  if (value === undefined || value === null) return undefined;
  if (value === 'low' || value === 'medium' || value === 'high') return value;
  throw new B2AdapterError({ code: 'invalid_b2_projection', message: `Core 返回的 ${label} 无效。`, retryable: false, recovery: 'none', detail: value });
}

function efforts(value: unknown, label: string): B2.Effort[] | undefined {
  if (value === undefined || value === null) return undefined;
  if (!Array.isArray(value)) {
    throw new B2AdapterError({ code: 'invalid_b2_projection', message: `Core 返回的 ${label} 无效。`, retryable: false, recovery: 'none', detail: value });
  }
  return value.map((item) => {
    const mapped = effort(item, label);
    if (!mapped) throw new B2AdapterError({ code: 'invalid_b2_projection', message: `Core 返回的 ${label} 无效。`, retryable: false, recovery: 'none', detail: value });
    return mapped;
  });
}

function requiredRecord(value: unknown, label: string): Record<string, unknown> {
  if (!isRecord(value)) {
    throw new B2AdapterError({
      code: 'invalid_b2_projection',
      message: `Core 返回的 ${label} 不是对象。`,
      retryable: false,
      recovery: 'none',
      detail: value,
    });
  }
  return value;
}

function errorLike(value: unknown): B2UiError | undefined {
  if (!isRecord(value) || typeof value.code !== 'string' || typeof value.message !== 'string') {
    return undefined;
  }
  return {
    code: value.code,
    message: value.message,
    retryable: value.retryable === true,
    recovery: typeof value.recovery === 'string' ? value.recovery : 'none',
    detail: value.detail,
  };
}

/** Translate generated B2 errors without inferring a reason from HTTP text. */
export function normalizeB2Error(error: unknown): B2AdapterError {
  if (error instanceof B2AdapterError) return error;
  const detail = errorLike(error);
  if (detail) return new B2AdapterError(detail);
  return new B2AdapterError({
    code: 'transport_unavailable',
    message: error instanceof Error ? error.message : 'B2 Live request failed',
    retryable: true,
    recovery: 'retry_later',
    detail: error,
  });
}

function mapToolPolicy(value: unknown): B2.ToolPolicy | undefined {
  if (value === undefined || value === null) return undefined;
  const source = requiredRecord(value, 'RolePreset.tool_policy');
  const allowedTools = source.allowed_tools;
  const approvalRequired = source.approval_required;
  if (allowedTools !== undefined && (!Array.isArray(allowedTools) || allowedTools.some((item) => typeof item !== 'string'))) {
    throw new B2AdapterError({ code: 'invalid_b2_projection', message: 'Core 返回的 tool_policy.allowed_tools 无效。', retryable: false, recovery: 'none', detail: value });
  }
  if (approvalRequired !== undefined && (!Array.isArray(approvalRequired) || approvalRequired.some((item) => typeof item !== 'string'))) {
    throw new B2AdapterError({ code: 'invalid_b2_projection', message: 'Core 返回的 tool_policy.approval_required 无效。', retryable: false, recovery: 'none', detail: value });
  }
  return source as B2.ToolPolicy;
}

function mapBudget(value: unknown): B2.Budget | undefined {
  if (value === undefined || value === null) return undefined;
  return requiredRecord(value, 'RolePreset.budget') as B2.Budget;
}

function threadStatus(value: unknown, label: string): B2.ThreadStatus | undefined {
  if (value === undefined || value === null) return undefined;
  if (value === 'active' || value === 'completed' || value === 'cancelled' || value === 'archived') {
    return value;
  }
  throw new B2AdapterError({
    code: 'invalid_b2_projection',
    message: `Core 返回的 ${label} 无效。`,
    retryable: false,
    recovery: 'none',
    detail: value,
  });
}

/** Validate the returned identity without manufacturing a Phase 1E projection. */
export function mapConversationThread(value: B2.ConversationThread): B2.ConversationThread {
  const source = requiredRecord(value, 'ConversationThread');
  const legacyRefs = source.legacy_refs;
  if (legacyRefs !== undefined && (!Array.isArray(legacyRefs) || legacyRefs.some((ref) => {
    if (!isRecord(ref)) return true;
    return (ref.source_type !== 'session' && ref.source_type !== 'workflow_run')
      || !optionalText(ref.source_id);
  }))) {
    throw new B2AdapterError({
      code: 'invalid_b2_projection',
      message: 'Core 返回的 ConversationThread.legacy_refs 无效。',
      retryable: false,
      recovery: 'none',
      detail: legacyRefs,
    });
  }
  return {
    id: requiredText(source.id, 'ConversationThread.id'),
    cursor: source.cursor === null ? null : optionalFiniteNumber(source.cursor, 'ConversationThread.cursor'),
    parent_thread_id: source.parent_thread_id === null ? null : optionalText(source.parent_thread_id),
    workspace_ref: source.workspace_ref === null ? null : optionalText(source.workspace_ref),
    status: threadStatus(source.status, 'ConversationThread.status'),
    legacy_refs: legacyRefs as B2.ThreadLegacyRef[] | undefined,
    created_at: optionalText(source.created_at),
    updated_at: optionalText(source.updated_at),
    archived_at: source.archived_at === null ? null : optionalText(source.archived_at),
  };
}

export function mapModelProfile(value: B2.ModelProfile): B2.ModelProfile {
  const source = requiredRecord(value, 'ModelProfile');
  return {
    id: optionalText(source.id),
    name: requiredText(source.name, 'ModelProfile.name'),
    provider: optionalText(source.provider),
    model_id: requiredText(source.model_id, 'ModelProfile.model_id'),
    base_url: requiredText(source.base_url, 'ModelProfile.base_url'),
    secret_ref: requiredText(source.secret_ref, 'ModelProfile.secret_ref'),
    context_window: optionalFiniteNumber(source.context_window, 'ModelProfile.context_window'),
    default_token_budget: optionalFiniteNumber(source.default_token_budget, 'ModelProfile.default_token_budget'),
    input_usd_per_million_tokens: optionalFiniteNumber(source.input_usd_per_million_tokens, 'ModelProfile.input_usd_per_million_tokens'),
    output_usd_per_million_tokens: optionalFiniteNumber(source.output_usd_per_million_tokens, 'ModelProfile.output_usd_per_million_tokens'),
    supported_efforts: efforts(source.supported_efforts, 'ModelProfile.supported_efforts'),
    default_effort: effort(source.default_effort, 'ModelProfile.default_effort'),
    effort_parameter: source.effort_parameter === null ? null : optionalText(source.effort_parameter),
    effort_mapping: Array.isArray(source.effort_mapping)
      ? source.effort_mapping.map((item) => {
        const mapping = requiredRecord(item, 'ModelProfile.effort_mapping');
        const mappedEffort = effort(mapping.effort, 'ModelProfile.effort_mapping.effort');
        if (!mappedEffort) throw new B2AdapterError({ code: 'invalid_b2_projection', message: 'Core 返回的 ModelProfile.effort_mapping.effort 缺失。', retryable: false, recovery: 'none', detail: item });
        return { effort: mappedEffort, provider_value: requiredText(mapping.provider_value, 'ModelProfile.effort_mapping.provider_value') };
      })
      : undefined,
    enabled: typeof source.enabled === 'boolean' ? source.enabled : undefined,
    created_at: optionalText(source.created_at),
  };
}

export function mapRolePreset(value: B2.RolePreset): B2.RolePreset {
  const source = requiredRecord(value, 'RolePreset');
  const status = source.status === undefined || source.status === null
    ? undefined
    : source.status === 'active' || source.status === 'inactive'
      ? source.status
      : (() => { throw new B2AdapterError({ code: 'invalid_b2_projection', message: 'Core 返回的 RolePreset.status 无效。', retryable: false, recovery: 'none', detail: source.status }); })();
  const version = optionalFiniteNumber(source.version, 'RolePreset.version');
  return {
    id: optionalText(source.id),
    version,
    name: requiredText(source.name, 'RolePreset.name'),
    system_prompt: requiredText(source.system_prompt, 'RolePreset.system_prompt'),
    model_profile_id: requiredText(source.model_profile_id, 'RolePreset.model_profile_id'),
    effort: effort(source.effort, 'RolePreset.effort'),
    tool_policy: mapToolPolicy(source.tool_policy),
    budget: mapBudget(source.budget),
    memory_scope: optionalText(source.memory_scope),
    status,
    created_at: optionalText(source.created_at),
  };
}

export function mapTask(value: B2.B2Task): B2.B2Task {
  const source = requiredRecord(value, 'B2Task');
  const taskSource = requiredRecord(source.source, 'B2Task.source');
  const sourceType = taskSource.source_type;
  if (sourceType !== 'session' && sourceType !== 'workflow_run' && sourceType !== 'team_task') {
    throw new B2AdapterError({ code: 'invalid_b2_projection', message: 'B2Task.source.source_type 无效。', retryable: false, recovery: 'none', detail: source.source });
  }
  if (!Array.isArray(source.actions)) {
    throw new B2AdapterError({ code: 'invalid_b2_projection', message: 'B2Task.actions 缺失。', retryable: false, recovery: 'none', detail: source.actions });
  }
  const actions = source.actions.map((raw) => {
    const action = requiredRecord(raw, 'B2Task.action');
    if (!['inspect', 'cancel', 'resume', 'approve', 'archive'].includes(String(action.action))) {
      throw new B2AdapterError({ code: 'invalid_b2_projection', message: 'B2Task.action 无效。', retryable: false, recovery: 'none', detail: raw });
    }
    if (!['available', 'blocked', 'unsupported'].includes(String(action.availability))) {
      throw new B2AdapterError({ code: 'invalid_b2_projection', message: 'B2Task.action.availability 无效。', retryable: false, recovery: 'none', detail: raw });
    }
    const projectionRevision = optionalFiniteNumber(action.projection_revision, 'B2Task.action.projection_revision');
    if (projectionRevision === undefined) {
      throw new B2AdapterError({ code: 'invalid_b2_projection', message: 'B2Task.action.projection_revision 缺失。', retryable: false, recovery: 'none', detail: raw });
    }
    return {
      action: action.action as B2.TaskAction['action'],
      availability: action.availability as B2.TaskAction['availability'],
      reason_code: action.reason_code === null ? null : requiredText(action.reason_code, 'B2Task.action.reason_code'),
      projection_revision: projectionRevision,
    };
  });
  return {
    source: {
      source_type: sourceType,
      source_id: requiredText(taskSource.source_id, 'B2Task.source.source_id'),
    },
    thread_id: nullableText(source.thread_id, 'B2Task.thread_id'),
    project_id: nullableText(source.project_id, 'B2Task.project_id'),
    workspace_id: nullableText(source.workspace_id, 'B2Task.workspace_id'),
    source_status: requiredText(source.source_status, 'B2Task.source_status'),
    revision: requiredFiniteNumber(source.revision, 'B2Task.revision'),
    created_at: requiredText(source.created_at, 'B2Task.created_at'),
    title: requiredText(source.title, 'B2Task.title'),
    actions,
  };
}

export function mapTaskPage(value: B2.B2TaskPage): B2.B2TaskPage {
  const source = requiredRecord(value, 'B2TaskPage');
  if (!Array.isArray(source.items)) {
    throw new B2AdapterError({ code: 'invalid_b2_projection', message: 'Core 返回的 B2TaskPage.items 缺失。', retryable: false, recovery: 'none', detail: value });
  }
  return {
    items: source.items.map((item) => mapTask(item as B2.B2Task)),
    next_offset: source.next_offset === null ? null : requiredFiniteNumber(source.next_offset, 'B2TaskPage.next_offset'),
  };
}

export function mapSessionHistory(value: B2.B2SessionHistory): B2.B2SessionHistory {
  const source = requiredRecord(value, 'B2SessionHistory');
  const session = requiredRecord(source.session, 'B2SessionHistory.session') as unknown as B2.Session;
  requiredRecord((session as unknown as Record<string, unknown>).role_snapshot, 'Session.role_snapshot');
  const agents = source.agents;
  const items = source.items;
  if (!Array.isArray(agents) || !Array.isArray(items)) {
    throw new B2AdapterError({ code: 'invalid_b2_projection', message: 'Core 返回的 Session history 缺少 agents/items。', retryable: false, recovery: 'none', detail: value });
  }
  for (const agent of agents) {
    const entry = requiredRecord(agent, 'B2SessionHistory.agent');
    requiredText(entry.session_id, 'AgentInstance.session_id');
    requiredRecord(entry.role_snapshot, 'AgentInstance.role_snapshot');
  }
  for (const item of items) {
    const entry = requiredRecord(item, 'B2SessionHistory.item');
    requiredText(entry.thread_id, 'Item.thread_id');
    requiredText(entry.turn_id, 'Item.turn_id');
    requiredRecord(entry.payload, 'Item.payload');
  }
  return {
    session,
    thread_id: nullableText(source.thread_id, 'B2SessionHistory.thread_id'),
    agents: agents as B2.AgentInstance[],
    items: items as B2.Item[],
    next_cursor: source.next_cursor === null ? null : requiredFiniteNumber(source.next_cursor, 'B2SessionHistory.next_cursor'),
  };
}

export function mapDiscoveredModels(value: B2.B2Discovery): string[] {
  const source = requiredRecord(value, 'Model discovery');
  const models = source.model_ids;
  if (!Array.isArray(models) || models.some((model) => typeof model !== 'string' || model.length === 0)) {
    throw new B2AdapterError({ code: 'invalid_b2_projection', message: 'Core 返回的模型发现结果无效。', retryable: false, recovery: 'none', detail: models });
  }
  return [...models];
}

export class B2LiveAdapter {
  private readonly client: B2ClientLike;

  constructor(client: B2ClientLike) {
    this.client = client;
  }

  async connect(): Promise<B2.ProtocolNegotiation> {
    try {
      const metadata = await this.client.negotiateProtocol(true);
      if (metadata.protocol_version !== this.client.protocolVersion
        || metadata.min_client_version !== this.client.protocolVersion
        || metadata.schema_digest !== this.client.schemaDigest) {
        throw new B2AdapterError({
          code: 'protocol_incompatible',
          message: 'Core B2 协议协商结果与生成 Client 不匹配，未建立配置/任务连接。',
          retryable: false,
          recovery: 'none',
          detail: metadata,
        });
      }
      return metadata;
    } catch (error: unknown) {
      throw normalizeB2Error(error);
    }
  }

  async createThread(request: B2.B2CreateThread, idempotencyKey?: string): Promise<B2.ConversationThread> {
    const workspaceId = request.workspace_id.trim();
    if (!workspaceId) {
      throw new B2AdapterError({
        code: 'workspace_required',
        message: '创建 Thread 必须绑定已选择的 Workspace。',
        retryable: false,
        recovery: 'none',
      });
    }
    try {
      return mapConversationThread(await this.client.createThread({ workspace_id: workspaceId }, { idempotencyKey }));
    } catch (error: unknown) {
      throw normalizeB2Error(error);
    }
  }

  async listModels(): Promise<B2.ModelProfile[]> {
    try {
      return (await this.client.listModels()).map(mapModelProfile);
    } catch (error: unknown) {
      throw normalizeB2Error(error);
    }
  }

  async createModel(request: B2.CreateModelProfileRequest, idempotencyKey?: string): Promise<B2.ModelProfile> {
    try {
      return mapModelProfile(await this.client.createModel(request, { idempotencyKey }));
    } catch (error: unknown) {
      throw normalizeB2Error(error);
    }
  }

  async updateModel(
    profileId: string,
    request: B2.UpdateModelProfileRequest,
    idempotencyKey?: string,
  ): Promise<B2.ModelProfile> {
    if (!profileId.trim()) throw new B2AdapterError({ code: 'profile_required', message: '更新模型配置必须提供 Profile ID。', retryable: false, recovery: 'none' });
    try {
      return mapModelProfile(await this.client.updateModel(profileId, request, { idempotencyKey }));
    } catch (error: unknown) {
      throw normalizeB2Error(error);
    }
  }

  async discoverModels(
    request: B2.DiscoverModelsRequest,
    idempotencyKey?: string,
  ): Promise<string[]> {
    try {
      return mapDiscoveredModels(await this.client.discoverModels(request, { idempotencyKey }));
    } catch (error: unknown) {
      throw normalizeB2Error(error);
    }
  }

  async listRoles(includeInactive = false): Promise<B2.RolePreset[]> {
    try {
      return (await this.client.listRoles({ includeInactive })).map(mapRolePreset);
    } catch (error: unknown) {
      throw normalizeB2Error(error);
    }
  }

  async createRole(request: B2.CreateRoleRequest, idempotencyKey?: string): Promise<B2.RolePreset> {
    try {
      return mapRolePreset(await this.client.createRole(request, { idempotencyKey }));
    } catch (error: unknown) {
      throw normalizeB2Error(error);
    }
  }

  async updateRole(
    roleId: string,
    request: B2.UpdateRoleRequest,
    idempotencyKey?: string,
  ): Promise<B2.RolePreset> {
    if (!roleId.trim()) throw new B2AdapterError({ code: 'role_required', message: '更新角色预设必须提供 Role ID。', retryable: false, recovery: 'none' });
    try {
      return mapRolePreset(await this.client.updateRole(roleId, request, { idempotencyKey }));
    } catch (error: unknown) {
      throw normalizeB2Error(error);
    }
  }

  async cancelSession(sessionId: string, idempotencyKey?: string): Promise<B2.B2Cancellation> {
    if (!sessionId.trim()) throw new B2AdapterError({ code: 'session_required', message: '取消 Session 必须提供服务端 Session ID。', retryable: false, recovery: 'none' });
    try {
      const result = await this.client.cancelSession(sessionId, { idempotencyKey });
      if (typeof result.accepted !== 'boolean') {
        throw new B2AdapterError({ code: 'invalid_b2_projection', message: 'Core 返回的 Session cancel 结果无效。', retryable: false, recovery: 'none', detail: result });
      }
      return result;
    } catch (error: unknown) {
      throw normalizeB2Error(error);
    }
  }

  async listTasks(offset = 0, limit = 100): Promise<B2.B2TaskPage> {
    try {
      return mapTaskPage(await this.client.listTasks({ offset, limit }));
    } catch (error: unknown) {
      throw normalizeB2Error(error);
    }
  }

  async listAgents(offset = 0): Promise<B2.B2AgentPage> {
    try {
      const page = await this.client.listAgents({ offset, limit: 100 });
      if (!Array.isArray(page.items)) throw new Error('Core Agent page is invalid');
      for (const agent of page.items) {
        requiredText(agent.id, 'AgentInstance.id');
        requiredText(agent.session_id, 'AgentInstance.session_id');
        requiredRecord(agent.role_snapshot, 'AgentInstance.role_snapshot');
        if (!['created', 'running', 'completed', 'failed', 'cancelled', 'timed_out'].includes(agent.status || '')) {
          throw new Error('Core Agent status is invalid');
        }
      }
      if (page.next_offset !== null && (!Number.isInteger(page.next_offset) || page.next_offset <= offset)) {
        throw new Error('Core Agent cursor did not advance');
      }
      return page;
    } catch (error: unknown) { throw normalizeB2Error(error); }
  }

  async getTask(sourceId: string, sourceType?: B2.GetTaskOptions['sourceType']): Promise<B2.B2Task> {
    if (!sourceId.trim()) throw new B2AdapterError({ code: 'task_required', message: '读取任务详情必须提供来源 ID。', retryable: false, recovery: 'none' });
    try {
      return mapTask(await this.client.getTask(sourceId, { sourceType }));
    } catch (error: unknown) {
      throw normalizeB2Error(error);
    }
  }

  async getSessionHistory(sessionId: string, afterCursor?: number | null, limit = 100): Promise<B2.B2SessionHistory> {
    if (!sessionId.trim()) throw new B2AdapterError({ code: 'session_required', message: '读取 Session history 必须提供服务端 Session ID。', retryable: false, recovery: 'none' });
    try {
      return mapSessionHistory(await this.client.getSessionHistory(sessionId, { afterCursor, limit }));
    } catch (error: unknown) {
      throw normalizeB2Error(error);
    }
  }
}
