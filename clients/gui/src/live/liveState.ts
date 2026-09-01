import type { AnyOperantEvent, EventCursor, Thread } from '@operant/sdk';

/**
 * Presentation-only values used by the live GUI.
 *
 * These are deliberately not wire models.  The wire models stay in the
 * generated SDK; this file only contains the small, normalized shape the
 * view needs after the adapter has received a projection.
 */
export interface LiveProjectProjection {
  id: string;
  name: string;
  workspaceRef: string;
  readable: boolean;
  writable: boolean;
  createdAt?: string;
  threadIds: string[];
  runIds: string[];
}

export interface LiveWorkspaceFile {
  path: string;
  name: string;
  kind: 'file' | 'directory' | 'unknown';
  size?: number;
  modifiedAt?: string;
}

export type LiveStreamStatus = 'idle' | 'connecting' | 'replaying' | 'connected' | 'error';

export interface LiveStreamState {
  status: LiveStreamStatus;
  cursor: EventCursor;
  events: AnyOperantEvent[];
  lastEvent?: AnyOperantEvent;
  error?: LiveError;
}

export interface LiveError {
  code: string;
  message: string;
  retryable: boolean;
  recovery?: string;
}

export interface LiveCommandState {
  status: 'idle' | 'sending' | 'awaiting_projection' | 'error';
  error?: LiveError;
}

export interface LiveActionState {
  status: 'idle' | 'sending' | 'awaiting_projection' | 'error';
  error?: LiveError;
}

/**
 * Resource-scoped event key.  Cursor is authoritative within a stream scope;
 * event ids are only a fallback for old SDK events that do not carry a cursor.
 */
export function eventKey(scope: string, event: AnyOperantEvent): string {
  if (Number.isSafeInteger(event.sequence)) {
    return `${scope}:cursor:${event.sequence}`;
  }
  return `${scope}:event:${event.id}`;
}

export function cursorFromEvent(event: AnyOperantEvent, previous: EventCursor): EventCursor {
  const nextSequence = Number.isSafeInteger(event.sequence)
    ? Math.max(previous.sequence ?? 0, event.sequence)
    : previous.sequence ?? 0;
  return {
    sequence: nextSequence,
    event_id: event.id || previous.event_id,
    timestamp: event.occurred_at || previous.timestamp,
  };
}

export interface EventAccumulator {
  cursor: EventCursor;
  events: AnyOperantEvent[];
  seen: ReadonlySet<string>;
}

/**
 * Reducer used by the GUI stream projection.  It never infers a run/approval
 * terminal state; it only accumulates committed events and their cursor.
 */
export function reduceEvent(
  current: EventAccumulator,
  scope: string,
  event: AnyOperantEvent
): EventAccumulator {
  const key = eventKey(scope, event);
  if (current.seen.has(key)) return current;

  const seen = new Set(current.seen);
  seen.add(key);
  return {
    cursor: cursorFromEvent(event, current.cursor),
    events: [...current.events, event],
    seen,
  };
}

export function emptyEventAccumulator(cursor: EventCursor = { sequence: 0 }): EventAccumulator {
  return { cursor, events: [], seen: new Set<string>() };
}

const TERMINAL_EVENT_TYPES = new Set([
  'agent.completed',
  'agent.failed',
  'agent.cancelled',
  'agent.timed_out',
  'workflow.completed',
]);

export function isTerminalEvent(event: AnyOperantEvent): boolean {
  return TERMINAL_EVENT_TYPES.has(event.event_type);
}

export function isApprovalProjectionEvent(event: AnyOperantEvent): boolean {
  return (
    event.event_type === 'approval.requested' ||
    event.event_type === 'approval.decided' ||
    event.event_type === 'approval.expired' ||
    event.event_type === 'approval.revoked' ||
    event.event_type === 'tool.approval_required' ||
    event.event_type === 'tool.approval_decided'
  );
}

export function isManualReconcileValue(value: unknown): boolean {
  return value === 'manual_reconcile_required' || value === 'outcome_unknown';
}

export function containsManualReconcile(value: unknown, seen = new Set<unknown>()): boolean {
  if (isManualReconcileValue(value)) return true;
  if (typeof value !== 'object' || value === null || seen.has(value)) return false;
  seen.add(value);
  if (Array.isArray(value)) return value.some((item) => containsManualReconcile(item, seen));
  return Object.values(value).some((item) => containsManualReconcile(item, seen));
}

export function threadNeedsManualReconcile(thread: Thread | undefined): boolean {
  if (!thread) return false;
  const metadata = thread.metadata;
  if (!metadata) return false;
  return containsManualReconcile(metadata);
}

export function safeText(value: unknown, fallback = ''): string {
  return typeof value === 'string' && value.trim() ? value : fallback;
}
