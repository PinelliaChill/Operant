import type * as Phase1E from '../../../../sdk/typescript-client/phase1e.generated';
import type {
  Cursor,
  LiveApproval,
  LiveCreateSessionInput,
  LiveError,
  LiveEvent,
  LiveProjectProjection,
  LiveSession,
  LiveThread,
  LiveWorkspaceFile,
} from './liveState.ts';

/** A capability which is not in the generated Phase 1E Schema. */
export class UnsupportedLiveCapabilityError extends Error {
  readonly code = 'capability_unavailable';
  readonly retryable = false;
  readonly recovery = 'none';

  constructor(capability: string) {
    super(`${capability} 不在 phase1e.v1 Schema 中，Live 不会调用旧 API 或伪造结果。`);
    this.name = 'UnsupportedLiveCapabilityError';
    Object.setPrototypeOf(this, new.target.prototype);
  }
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

/** Translate the generated transport error without guessing from HTTP text. */
export function normalizeLiveError(error: unknown): LiveAdapterError {
  if (error instanceof LiveAdapterError) return error;
  if (error instanceof UnsupportedLiveCapabilityError) {
    return new LiveAdapterError({
      code: error.code,
      message: error.message,
      retryable: error.retryable,
      recovery: error.recovery,
    });
  }
  if (isGeneratedPhase1EError(error)) {
    return new LiveAdapterError({
      code: error.code,
      message: error.message,
      retryable: error.retryable,
      recovery: error.recovery || undefined,
    });
  }
  return new LiveAdapterError({
    code: 'transport_unavailable',
    message: error instanceof Error ? error.message : 'Live request failed',
    retryable: true,
    recovery: 'retry_later',
  });
}

function isGeneratedPhase1EError(
  error: unknown,
): error is Error & { code: string; retryable: boolean; recovery: string } {
  if (!(error instanceof Error)) return false;
  const candidate = error as { code?: unknown; retryable?: unknown; recovery?: unknown };
  return typeof candidate.code === 'string'
    && typeof candidate.retryable === 'boolean'
    && typeof candidate.recovery === 'string';
}

function projectName(project: Phase1E.ProjectProjection): string {
  const parts = project.workspace_ref.split('/').filter(Boolean);
  return parts.at(-1) || project.project_id;
}

/** Map only generated fields; relationship IDs come from the nested projection. */
export function mapProjectProjection(project: Phase1E.ProjectProjection): LiveProjectProjection {
  return {
    id: project.project_id,
    name: projectName(project),
    workspaceRef: project.workspace_ref,
    readable: project.readable,
    writable: project.writable,
    createdAt: project.created_at,
    threadIds: project.threads.map((thread) => thread.id),
    runIds: project.workflow_runs.map((run) => run.id),
  };
}

function legacyId(
  refs: Phase1E.ThreadLegacyRef[],
  sourceType: Phase1E.ThreadLegacyRef['source_type'],
): string | null {
  return refs.find((ref) => ref.source_type === sourceType)?.source_id ?? null;
}

/** Preserve the missing-session case; it is not valid to infer a Session ID. */
export function mapThreadProjection(thread: Phase1E.ThreadProjection): LiveThread {
  return {
    id: thread.id,
    title: thread.id,
    workspaceRef: thread.workspace_ref,
    workspace: thread.workspace_ref || '',
    status: thread.status,
    createdAt: thread.created_at,
    updatedAt: thread.updated_at,
    created_at: thread.created_at,
    updated_at: thread.updated_at,
    archivedAt: thread.archived_at,
    legacyRefs: thread.legacy_refs,
    sessionId: legacyId(thread.legacy_refs, 'session'),
    session_id: legacyId(thread.legacy_refs, 'session'),
    workflowRunId: legacyId(thread.legacy_refs, 'workflow_run'),
  };
}

export function mapApprovalProjection(
  sessionId: string,
  approval: Phase1E.ApprovalProjection,
): LiveApproval {
  return {
    id: approval.approval_id,
    sessionId,
    session_id: sessionId,
    toolCallId: approval.tool_call_id,
    category: approval.category,
    detail: approval.detail,
    actionHash: approval.action_hash,
    status: approval.status,
    requestedAt: approval.requested_at,
    expiresAt: approval.expires_at,
    continuationAvailable: approval.continuation_available,
  };
}

export function mapWorkspaceFile(entry: Phase1E.WorkspaceFileEntry): LiveWorkspaceFile {
  return {
    path: entry.path,
    name: entry.name,
    kind: entry.type,
    size: entry.size_bytes,
    modifiedAt: entry.modified_at,
  };
}

function eventData(data: unknown): Phase1E.RuntimeEvent {
  if (typeof data === 'object' && data !== null && !Array.isArray(data)) {
    return data as Phase1E.RuntimeEvent;
  }
  return { payload: { value: data } };
}

function eventError(value: unknown): LiveError | undefined {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return undefined;
  const candidate = value as Record<string, unknown>;
  const detail = candidate.detail;
  const message = typeof candidate.message === 'string'
    ? candidate.message
    : typeof detail === 'string' ? detail : 'Core SSE 返回错误';
  return {
    ...candidate,
    code: typeof candidate.code === 'string' ? candidate.code : 'stream_error',
    message,
    retryable: typeof candidate.retryable === 'boolean' ? candidate.retryable : false,
    recovery: typeof candidate.recovery === 'string' ? candidate.recovery : undefined,
    detail,
  };
}

/** Convert generated RuntimeEvent data while retaining the generated Cursor. */
export function mapSseFrame(frame: Phase1E.SseFrame, sessionId: string): LiveEvent {
  const runtime = eventData(frame.data);
  const resourceScope = frame.resource_scope || `session:${sessionId}`;
  const streamKind = frame.stream_kind || 'session.run';
  return {
    id: runtime.id || `${resourceScope}:${frame.id === null ? 'no-cursor' : BigInt(frame.id).toString()}`,
    sequence: frame.id ?? runtime.cursor ?? null,
    event_type: runtime.event_type || frame.event,
    session_id: runtime.session_id || sessionId,
    thread_id: typeof runtime.thread_id === 'string' ? runtime.thread_id : undefined,
    occurred_at: runtime.created_at,
    payload: runtime.payload || {},
    error: eventError(runtime.error),
    detail: runtime.detail,
    resource_scope: resourceScope,
    stream_kind: streamKind,
  };
}

export type LiveRunStream = Phase1E.RunSessionStream;

/**
 * Adapter for the generated Phase 1E client only.
 *
 * There is deliberately no `OperantClient`/HttpClient union here. Missing
 * Schema operations fail explicitly so a live screen can show its boundary.
 */
export class LiveClientAdapter {
  private readonly sessions = new Map<string, LiveSession>();
  private readonly client: Phase1E.Phase1EClient;

  constructor(client: Phase1E.Phase1EClient) {
    this.client = client;
  }

  async connect(): Promise<Phase1E.ProtocolNegotiation> {
    try {
      // Negotiation is the live connection check. Phase1EClient validates the
      // exact protocol major/version and embedded Schema digest.
      const metadata = await this.client.negotiateProtocol(true);
      if (metadata.protocol_version !== this.client.protocolVersion
        || metadata.min_client_version !== this.client.protocolVersion
        || metadata.schema_digest !== this.client.schemaDigest) {
        throw new LiveAdapterError({
          code: 'protocol_incompatible',
          message: 'Core 协议协商结果与生成 Phase1EClient 不匹配，未建立实时连接。',
          retryable: false,
          recovery: 'none',
        });
      }
      return metadata;
    } catch (error: unknown) {
      throw normalizeLiveError(error);
    }
  }

  async listProjects(workspace?: string): Promise<LiveProjectProjection[]> {
    try {
      const projects = await this.client.listProjects();
      return projects
        .map(mapProjectProjection)
        .filter((project) => !workspace || project.workspaceRef === workspace);
    } catch (error: unknown) {
      throw normalizeLiveError(error);
    }
  }

  async listWorkspaceFiles(workspaceId: string, relativePath = ''): Promise<LiveWorkspaceFile[]> {
    try {
      const page = await this.client.listWorkspaceFiles(workspaceId, {
        path: relativePath || undefined,
      });
      return page.entries.map(mapWorkspaceFile);
    } catch (error: unknown) {
      throw normalizeLiveError(error);
    }
  }

  async listThreads(workspace?: string): Promise<LiveThread[]> {
    try {
      const threads = await this.client.listThreads({ workspaceRef: workspace });
      return threads.map(mapThreadProjection);
    } catch (error: unknown) {
      throw normalizeLiveError(error);
    }
  }

  async getThread(threadId: string): Promise<LiveThread> {
    const thread = (await this.listThreads()).find((item) => item.id === threadId);
    if (!thread) {
      throw new LiveAdapterError({
        code: 'thread_not_found',
        message: `Core 没有返回 Thread ${threadId} 的 Projection。`,
        retryable: false,
        recovery: 'none',
      });
    }
    return thread;
  }

  /** No message Query exists in the phase1e.v1 Schema. */
  listThreadMessages(_threadId: string): Promise<never> {
    return Promise.reject(new UnsupportedLiveCapabilityError('Thread 消息 Query'));
  }

  /** Sessions are only retained when returned by generated createSession. */
  listSessions(): LiveSession[] {
    return [...this.sessions.values()];
  }

  async createSession(input: LiveCreateSessionInput | undefined, idempotencyKey?: string): Promise<LiveSession> {
    const hasRoleId = typeof input?.roleId === 'string' && input.roleId.trim().length > 0;
    const newRole = input?.newRole;
    const validatedNewRole = newRole
      && newRole.name.trim().length > 0
      && newRole.system_prompt.trim().length > 0
      && newRole.model_profile_id.trim().length > 0
      ? newRole
      : undefined;
    const hasNewRole = validatedNewRole !== undefined;
    if (Number(hasRoleId) + Number(hasNewRole) !== 1) {
      throw new LiveAdapterError({
        code: 'invalid_create_session_input',
        message: '创建 Session 必须明确提供 roleId 或完整 newRole，且两者只能提供一个。',
        retryable: false,
        recovery: 'none',
      });
    }
    try {
      const request: Phase1E.CreateSessionRequest = {
        role_id: hasRoleId ? input.roleId : undefined,
        new_role: validatedNewRole,
        model_profile_id: input?.modelProfileId,
        effort: input?.effort,
        budget_overrides: input?.budgetOverrides,
      };
      const session = await this.client.createSession(request, { idempotencyKey });
      this.sessions.set(session.id, session);
      return session;
    } catch (error: unknown) {
      throw normalizeLiveError(error);
    }
  }

  /** The generated method returns a Promise plus an AsyncIterable of frames. */
  runSessionStream(
    sessionId: string,
    request: Phase1E.RunSessionRequest,
    options: Phase1E.RunSessionStreamOptions,
  ): Promise<LiveRunStream> {
    if (!sessionId) {
      return Promise.reject(new LiveAdapterError({
        code: 'session_required',
        message: '运行 Session 必须提供服务端 Session ID。',
        retryable: false,
        recovery: 'none',
      }));
    }
    return this.client.runSessionStream(sessionId, request, options).catch((error: unknown) => {
      throw normalizeLiveError(error);
    });
  }

  /** No cancel operation is present in the generated Schema. */
  cancelSession(_sessionId: string): Promise<never> {
    return Promise.reject(new UnsupportedLiveCapabilityError('Session cancel Command'));
  }

  /** A session scope is mandatory; there is no global pending-approval Query. */
  async listPendingApprovals(sessionId: string): Promise<LiveApproval[]> {
    if (!sessionId) {
      throw new LiveAdapterError({
        code: 'session_required',
        message: 'Approval Query 必须提供服务端 Session ID。',
        retryable: false,
        recovery: 'none',
      });
    }
    try {
      const approvals = await this.client.listPendingApprovals(sessionId);
      return approvals.map((approval) => mapApprovalProjection(sessionId, approval));
    } catch (error: unknown) {
      throw normalizeLiveError(error);
    }
  }

  async submitApproval(
    approval: LiveApproval,
    approved: boolean,
    idempotencyKey: string,
  ): Promise<Phase1E.ApprovalDecisionResult> {
    if (!approval.sessionId || !approval.toolCallId) {
      throw new LiveAdapterError({
        code: 'approval_scope_required',
        message: 'Approval 缺少服务端 Session 或 tool call ID，不能提交。',
        retryable: false,
        recovery: 'none',
      });
    }
    try {
      return await this.client.submitApproval(
        approval.sessionId,
        approval.toolCallId,
        { approved },
        { idempotencyKey },
      );
    } catch (error: unknown) {
      throw normalizeLiveError(error);
    }
  }

  /** There is no generated global-event Query/stream in phase1e.v1. */
  subscribeEvents(): never {
    throw new UnsupportedLiveCapabilityError('全局事件 Query/订阅');
  }

}

export function eventPayload(event: LiveEvent): Record<string, unknown> {
  return event.payload;
}

export function cursorValue(cursor: Cursor | null | undefined): string {
  return cursor === null || cursor === undefined ? '' : BigInt(cursor).toString();
}
