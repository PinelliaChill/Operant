import type {
  AnyOperantEvent,
  ApprovalCard,
  ApprovalDecision,
  CanonicalAgentMessage,
  EventCursor,
  EventSubscriber,
  EventUnsubscribe,
  OperantClient,
  Session,
  Thread,
} from '@operant/sdk';
import {
  LiveError,
  LiveProjectProjection,
  LiveWorkspaceFile,
  safeText,
} from './liveState';

type OptionalClientMethod = (...args: unknown[]) => unknown;

/**
 * Keep the generated SDK as the only wire-model owner.  The protocol line can
 * add Phase 1E methods to OperantClient without requiring this GUI adapter to
 * duplicate every generated response interface while the branches converge.
 */
function optionalClientMethod(client: OperantClient, name: string): OptionalClientMethod | undefined {
  const candidate = (client as unknown as Record<string, unknown>)[name];
  return typeof candidate === 'function' ? candidate as OptionalClientMethod : undefined;
}

async function callOptionalClientMethod(
  client: OperantClient,
  name: string,
  ...args: unknown[]
): Promise<unknown> {
  const method = optionalClientMethod(client, name);
  if (!method) return undefined;
  return method.apply(client, args);
}

export class LiveAdapterError extends Error {
  readonly detail: LiveError;

  constructor(detail: LiveError) {
    super(detail.message);
    this.name = 'LiveAdapterError';
    this.detail = detail;
    Object.setPrototypeOf(this, new.target.prototype);
  }
}

export function normalizeLiveError(error: unknown): LiveAdapterError {
  if (error instanceof LiveAdapterError) return error;

  if (typeof error === 'object' && error !== null) {
    const value = error as Record<string, unknown>;
    const nested = typeof value.error === 'object' && value.error !== null
      ? (value.error as Record<string, unknown>)
      : value;
    const code = safeText(nested.code, 'runtime_error');
    const message = safeText(nested.message, error instanceof Error ? error.message : 'Live request failed');
    const retryable = nested.retryable === true || value.recoverable === true;
    const recovery = safeText(nested.recovery, safeText(value.userGuidance));
    return new LiveAdapterError({ code, message, retryable, recovery: recovery || undefined });
  }

  return new LiveAdapterError({
    code: 'runtime_error',
    message: error instanceof Error ? error.message : 'Live request failed',
    retryable: true,
    recovery: '请检查 Core 连接后重试。',
  });
}

function asRecord(value: unknown): Record<string, unknown> | undefined {
  return typeof value === 'object' && value !== null ? (value as Record<string, unknown>) : undefined;
}

function asArray(value: unknown): unknown[] {
  if (Array.isArray(value)) return value;
  const record = asRecord(value);
  if (!record) return [];
  for (const key of ['items', 'projects', 'files', 'data']) {
    if (Array.isArray(record[key])) return record[key] as unknown[];
  }
  return [];
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
}

function normalizeProject(value: unknown): LiveProjectProjection | undefined {
  const record = asRecord(value);
  if (!record) return undefined;
  const workspaceRef = safeText(record.workspace_ref, safeText(record.workspaceRef));
  const id = safeText(record.project_id, safeText(record.projectId, safeText(record.id)));
  if (!workspaceRef || !id) return undefined;
  return {
    id,
    name: safeText(record.name, safeText(record.display_name, workspaceRef || id)),
    workspaceRef: workspaceRef || safeText(record.workspace, id),
    readable: record.readable !== false,
    writable: record.writable === true,
    createdAt: safeText(record.created_at, safeText(record.createdAt)) || undefined,
    threadIds: stringArray(record.thread_ids ?? record.threadIds),
    runIds: stringArray(record.run_ids ?? record.runIds),
  };
}

function normalizeFile(value: unknown): LiveWorkspaceFile | undefined {
  const record = asRecord(value);
  if (!record) return undefined;
  const path = safeText(record.path, safeText(record.relative_path, safeText(record.relativePath)));
  if (!path) return undefined;
  const name = safeText(record.name, path.split('/').filter(Boolean).pop() || path);
  const rawKind = safeText(record.type, safeText(record.kind));
  const kind: LiveWorkspaceFile['kind'] = rawKind === 'file' || rawKind === 'directory' ? rawKind : 'unknown';
  const size = typeof record.size === 'number' && Number.isFinite(record.size) ? record.size : undefined;
  return {
    path,
    name,
    kind,
    size,
    modifiedAt: safeText(record.modified_at, safeText(record.modifiedAt)) || undefined,
  };
}

export class LiveClientAdapter {
  private readonly client: OperantClient;

  constructor(client: OperantClient) {
    this.client = client;
  }

  async connect(): Promise<unknown> {
    try {
      await this.client.checkHealth();
      const protocol = await callOptionalClientMethod(this.client, 'negotiateProtocol');
      if (protocol === undefined) {
        throw new LiveAdapterError({
          code: 'protocol_negotiation_unavailable',
          message: '当前 TypeScript Client 未提供 Phase 1E 协议协商，未建立实时连接。',
          retryable: false,
          recovery: '请先合入由 Schema 生成的 Client，再重试。',
        });
      }
      const protocolRecord = asRecord(protocol);
      const version = safeText(protocolRecord?.protocol_version, safeText(protocolRecord?.protocolVersion));
      if (version !== 'phase1e.v1') {
        throw new LiveAdapterError({
          code: 'schema_incompatible',
          message: version ? `Core 协议版本 ${version} 不受本阶段 Client 支持。` : 'Core 未返回可识别的协议版本。',
          retryable: false,
          recovery: '请升级 Core 与生成 Client，使其使用 phase1e.v1。',
        });
      }
      const schemaDigest = safeText(protocolRecord?.schema_digest, safeText(protocolRecord?.schemaDigest));
      if (!schemaDigest) {
        throw new LiveAdapterError({
          code: 'schema_digest_missing',
          message: 'Core 协议协商响应缺少 Schema digest，未建立实时连接。',
          retryable: false,
          recovery: '请升级 Core 与生成 Client，使协商响应包含 schema_digest。',
        });
      }
      const clientRecord = this.client as unknown as Record<string, unknown>;
      const embeddedDigest = safeText(clientRecord.schema_digest, safeText(clientRecord.schemaDigest));
      if (embeddedDigest && embeddedDigest !== schemaDigest) {
        throw new LiveAdapterError({
          code: 'schema_digest_mismatch',
          message: 'Core Schema digest 与生成 Client 不一致，未建立实时连接。',
          retryable: false,
          recovery: '请重新生成并部署匹配当前 Core Schema 的 Client。',
        });
      }
      return protocol;
    } catch (error: unknown) {
      throw normalizeLiveError(error);
    }
  }

  async listProjects(workspace?: string): Promise<LiveProjectProjection[]> {
    try {
      const raw = await callOptionalClientMethod(this.client, 'listProjects', workspace);
      if (raw === undefined) {
        throw new LiveAdapterError({
          code: 'projects_unavailable',
          message: '当前 TypeScript Client 未提供 Phase 1E Workspace/Project 投影。',
          retryable: false,
          recovery: '请先合入由 Schema 生成的 Projects Client；本页不会从演示数据或旧 Session 猜测项目。',
        });
      }
      return asArray(raw)
        .map(normalizeProject)
        .filter((project): project is LiveProjectProjection => Boolean(project))
        .filter((project) => !workspace || project.workspaceRef === workspace);
    } catch (error: unknown) {
      throw normalizeLiveError(error);
    }
  }

  async listWorkspaceFiles(workspaceId: string, relativePath = ''): Promise<LiveWorkspaceFile[]> {
    try {
      const raw = await callOptionalClientMethod(this.client, 'listWorkspaceFiles', workspaceId, relativePath);
      if (raw === undefined) {
        throw new LiveAdapterError({
          code: 'workspace_files_unavailable',
          message: '当前 TypeScript Client 未提供安全只读文件浏览接口。',
          retryable: false,
          recovery: '请先合入生成的 Workspace Files Client；本页不会退回演示文件。',
        });
      }
      return asArray(raw)
        .map(normalizeFile)
        .filter((file): file is LiveWorkspaceFile => Boolean(file));
    } catch (error: unknown) {
      throw normalizeLiveError(error);
    }
  }

  listThreads(workspace?: string): Promise<Thread[]> {
    return this.client.listThreads(workspace);
  }

  getThread(threadId: string): Promise<Thread> {
    return this.client.getThread(threadId);
  }

  listThreadMessages(threadId: string): Promise<CanonicalAgentMessage[]> {
    return this.client.listThreadMessages(threadId);
  }

  listSessions(workspace?: string): Promise<Session[]> {
    return this.client.listSessions(workspace);
  }

  createSession(options: Parameters<OperantClient['createSession']>[0]): Promise<Session> {
    return this.client.createSession(options);
  }

  runSessionStream(
    sessionId: string,
    message: string,
    workspace: string,
    onEvent: EventSubscriber
  ): EventUnsubscribe {
    return this.client.runSessionStream(sessionId, message, workspace, onEvent);
  }

  cancelSession(sessionId: string): Promise<{ accepted: boolean }> {
    return this.client.cancelSession(sessionId);
  }

  listPendingApprovals(sessionId?: string): Promise<ApprovalCard[]> {
    return this.client.listPendingApprovals(sessionId);
  }

  submitApproval(
    sessionId: string,
    approvalId: string,
    decision: ApprovalDecision
  ): Promise<{ accepted: boolean }> {
    return this.client.submitApproval(sessionId, approvalId, decision);
  }

  subscribeEvents(cursor: EventCursor, subscriber: EventSubscriber): EventUnsubscribe {
    return this.client.subscribeEvents(cursor, subscriber);
  }

  /** Keep event typing visible at the adapter boundary for generated clients. */
  subscribeThreadEvents(
    threadId: string,
    cursor: EventCursor,
    subscriber: EventSubscriber
  ): EventUnsubscribe {
    const scopedMethod = optionalClientMethod(this.client, 'subscribeThreadEvents');
    if (scopedMethod) {
      return scopedMethod.apply(this.client, [threadId, cursor, subscriber]) as EventUnsubscribe;
    }
    return this.subscribeEvents(cursor, (event) => {
      if (event.thread_id && event.thread_id !== threadId) return;
      subscriber(event);
    });
  }

  /** No GUI caller should need to reach through this adapter to the transport. */
  get rawClient(): never {
    throw new Error('LiveClientAdapter.rawClient is intentionally unavailable');
  }
}

export function eventPayload(event: AnyOperantEvent): Record<string, unknown> {
  return asRecord(event.payload) ?? {};
}
