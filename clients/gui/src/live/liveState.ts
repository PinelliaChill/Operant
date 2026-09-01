import type { Phase1E } from '@operant/sdk';

/**
 * Presentation state for the Phase 1E live surface.
 *
 * Values received from Core are generated `Phase1E` types. These small view
 * models only add labels/relationship indexes needed by React; they do not
 * define another wire protocol or infer relationships from a workspace path.
 */
export type Cursor = Phase1E.Cursor;
export type GeneratedProjectProjection = Phase1E.ProjectProjection;
export type GeneratedThreadProjection = Phase1E.ThreadProjection;
export type GeneratedSession = Phase1E.Session;
export type GeneratedApprovalProjection = Phase1E.ApprovalProjection;
export type GeneratedRunSessionStream = Phase1E.RunSessionStream;
export type GeneratedRunSessionRequest = Phase1E.RunSessionRequest;
export type GeneratedRunSessionOptions = Phase1E.RunSessionStreamOptions;
export type GeneratedCreateSessionRequest = Phase1E.CreateSessionRequest;
export type GeneratedCreateSessionOptions = Phase1E.CreateSessionOptions;
export type GeneratedCreateRole = Phase1E.CreateRole;
export type GeneratedApprovalDecisionResult = Phase1E.ApprovalDecisionResult;

export interface LiveProjectProjection {
  /** Exact generated `project_id`. */
  id: string;
  /** A safe display label derived from the generated workspace reference. */
  name: string;
  workspaceRef: string;
  readable: boolean;
  writable: boolean;
  createdAt: string;
  /** Exact IDs from `ProjectProjection.threads[]`. */
  threadIds: string[];
  /** Exact IDs from `ProjectProjection.workflow_runs[]`. */
  runIds: string[];
}

export interface LiveWorkspaceFile {
  path: string;
  name: string;
  kind: 'file' | 'directory';
  /** Keep int64 values as number|bigint. Never coerce a large value to number. */
  size: Cursor | null;
  modifiedAt: string | null;
}

export interface LiveThread {
  /** Exact generated `ThreadProjection.id`. */
  id: string;
  /** Phase 1E has no title field; the ID is the only honest label. */
  title: string;
  workspaceRef: string | null;
  /** UI aliases keep labels readable; both values are the generated projection. */
  workspace: string;
  status: Phase1E.ThreadStatus;
  createdAt: string;
  updatedAt: string;
  created_at: string;
  updated_at: string;
  archivedAt: string | null;
  /** Exact generated `ThreadProjection.legacy_refs`. */
  legacyRefs: Phase1E.ThreadLegacyRef[];
  /** Exact `source_id` for the session legacy ref, or null when absent. */
  sessionId: string | null;
  session_id: string | null;
  /** Exact `source_id` for the workflow_run legacy ref, or null when absent. */
  workflowRunId: string | null;
}

export interface LiveSession extends GeneratedSession {}

export interface LiveApproval {
  /** Generated approval_id, carried with its required session scope. */
  id: string;
  sessionId: string;
  session_id: string;
  toolCallId: string;
  category: string;
  detail: string;
  actionHash: string;
  status: Phase1E.ApprovalStatus;
  requestedAt: string;
  expiresAt: string;
  continuationAvailable: boolean;
}

/** No message Query is in the Schema; this shape is intentionally never populated in live mode. */
export interface LiveMessage {
  id: string;
  role: string;
  content: string;
  sender: { name: string };
}

/** One generated SSE frame after adding the UI's stable event key fields. */
export interface LiveEvent {
  id: string;
  sequence: Cursor | null;
  event_type: string;
  session_id?: string;
  thread_id?: string;
  occurred_at?: string;
  payload: Record<string, unknown>;
  /** Preserve a typed error carried in SSE data, including its detail. */
  error?: LiveError;
  detail?: unknown;
  resource_scope: string;
  stream_kind: string;
}

export type LiveStreamStatus = 'idle' | 'connecting' | 'replaying' | 'connected' | 'error';

export interface LiveStreamState {
  status: LiveStreamStatus;
  cursor: Cursor | null;
  events: LiveEvent[];
  lastEvent?: LiveEvent;
  error?: LiveError;
}

export interface LiveError {
  code: string;
  message: string;
  retryable: boolean;
  recovery?: string;
  /** Core may attach structured recovery evidence to an error. */
  detail?: unknown;
}

export interface LiveConnectionLoss {
  phase: 'connecting' | 'error';
  streamStatus: 'replaying';
  projectionStale: true;
  error: LiveError;
}

/** Resolve a newly-created Session only through an exact legacy session ref. */
export function threadForSession(
  threads: readonly LiveThread[],
  sessionId: string,
): LiveThread | undefined {
  return threads.find((thread) => thread.sessionId === sessionId);
}

export function connectionLossState(
  status: 'disconnected' | 'reconnecting',
): LiveConnectionLoss {
  return {
    phase: status === 'reconnecting' ? 'connecting' : 'error',
    streamStatus: 'replaying',
    projectionStale: true,
    error: {
      code: status === 'reconnecting' ? 'core_reconnecting' : 'core_disconnected',
      message: status === 'reconnecting'
        ? 'Core 连接正在重建，Projection 可能过期；命令与审批已禁用。'
        : 'Core 已断开，Projection 可能过期；命令与审批已禁用。',
      retryable: true,
      recovery: 'retry_later',
    },
  };
}

export interface LiveCommandState {
  status: 'idle' | 'sending' | 'awaiting_projection' | 'error';
  error?: LiveError;
  /** The stable key used for the current logical command, when present. */
  idempotencyKey?: string;
}

export interface LiveActionState {
  status: 'idle' | 'sending' | 'awaiting_projection' | 'error';
  error?: LiveError;
  idempotencyKey?: string;
}

export interface LiveCreateSessionInput {
  /** Exactly one of roleId and newRole must be supplied. */
  roleId?: string;
  newRole?: GeneratedCreateRole;
  modelProfileId?: string;
  effort?: string;
  budgetOverrides?: Record<string, unknown>;
}

/** Event key is scoped by resource, stream kind, and lossless cursor. */
export function eventKey(scope: string, event: LiveEvent): string {
  const resourceScope = event.resource_scope || scope;
  const streamKind = event.stream_kind || 'unknown';
  if (event.sequence !== null && event.sequence !== undefined) {
    return `${resourceScope}\0${streamKind}\0${BigInt(event.sequence).toString()}`;
  }
  return `${resourceScope}\0${streamKind}\0event:${event.id}`;
}

function maxCursor(previous: Cursor | null, next: Cursor | null): Cursor | null {
  if (next === null || next === undefined) return previous;
  if (previous === null || previous === undefined) return next;
  return BigInt(next) >= BigInt(previous) ? next : previous;
}

export function cursorFromEvent(event: LiveEvent, previous: Cursor | null): Cursor | null {
  return maxCursor(previous, event.sequence);
}

export interface EventAccumulator {
  cursor: Cursor | null;
  events: LiveEvent[];
  seen: ReadonlySet<string>;
}

/** Keep the visible timeline bounded while the current stream stays live. */
export const LIVE_EVENT_WINDOW_SIZE = 256;

/**
 * Reducer used by the GUI timeline. It only records committed frames and
 * never treats a frame or receipt as the authoritative run/approval state.
 */
export function reduceEvent(
  current: EventAccumulator,
  scope: string,
  event: LiveEvent,
): EventAccumulator {
  const key = eventKey(scope, event);
  if (current.seen.has(key)) return current;

  const seen = new Set(current.seen);
  seen.add(key);
  const events = [...current.events, event];
  if (events.length > LIVE_EVENT_WINDOW_SIZE) {
    const evicted = events.shift();
    if (evicted) seen.delete(eventKey(scope, evicted));
  }
  return {
    cursor: cursorFromEvent(event, current.cursor),
    events,
    seen,
  };
}

export function emptyEventAccumulator(cursor: Cursor | null = 0): EventAccumulator {
  return { cursor, events: [], seen: new Set<string>() };
}

const TERMINAL_EVENT_TYPES = new Set([
  'agent.completed',
  'agent.failed',
  'agent.cancelled',
  'agent.timed_out',
  'workflow.completed',
]);

export function isTerminalEvent(event: LiveEvent): boolean {
  return TERMINAL_EVENT_TYPES.has(event.event_type);
}

export function isApprovalProjectionEvent(event: LiveEvent): boolean {
  return event.event_type.includes('approval');
}

export function isManualReconcileValue(value: unknown): boolean {
  return value === 'manual_reconcile_required'
    || value === 'manual_reconcile'
    || value === 'outcome_unknown';
}

export function containsManualReconcile(value: unknown, seen = new Set<unknown>()): boolean {
  if (isManualReconcileValue(value)) return true;
  if (typeof value !== 'object' || value === null || seen.has(value)) return false;
  seen.add(value);
  if (Array.isArray(value)) return value.some((item) => containsManualReconcile(item, seen));
  return Object.values(value).some((item) => containsManualReconcile(item, seen));
}

export function eventNeedsManualReconcile(event: LiveEvent): boolean {
  return event.error?.recovery === 'manual_reconcile'
    || containsManualReconcile(event.error)
    || containsManualReconcile(event.detail)
    || containsManualReconcile(event.payload);
}

/** Include the action in an approval idempotency namespace. */
export function approvalActionKey(
  sessionId: string,
  approvalId: string,
  decision: 'approve' | 'reject',
): string {
  return `${sessionId}\0${approvalId}\0${decision}`;
}

export function threadNeedsManualReconcile(_thread: LiveThread | undefined): boolean {
  // ThreadProjection deliberately has no free-form metadata. Recovery is
  // therefore sticky in LiveContext and can only be raised by a typed error,
  // receipt, or committed SSE payload.
  return false;
}

export function formatCursor(cursor: Cursor | null | undefined): string {
  return cursor === null || cursor === undefined ? '—' : BigInt(cursor).toString();
}

export function safeText(value: string | null | undefined, fallback = ''): string {
  return typeof value === 'string' && value.trim() ? value : fallback;
}

/** Generate one stable key for one logical command. */
let fallbackKeyCounter = 0;
export function createIdempotencyKey(): string {
  const cryptoApi = globalThis.crypto;
  if (typeof cryptoApi?.randomUUID === 'function') return cryptoApi.randomUUID();
  if (typeof cryptoApi?.getRandomValues === 'function') {
    const bytes = cryptoApi.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }
  // The generated client itself will reject an unavailable crypto API. This
  // fallback is only useful in test doubles and remains unique per process.
  fallbackKeyCounter += 1;
  return `gui-test-${Date.now().toString(36)}-${fallbackKeyCounter.toString(36)}`;
}
