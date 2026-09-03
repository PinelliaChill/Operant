export type RuntimeCursor = number | bigint;

export interface RuntimeEvent {
  id: string;
  cursor: RuntimeCursor | null;
  eventType: string;
  resourceScope: string;
  payload: Record<string, unknown>;
}

export interface RuntimeEventState {
  cursors: Map<string, RuntimeCursor>;
  events: RuntimeEvent[];
  seen: Set<string>;
}

export type RuntimeRecovery =
  | { kind: 'replay'; cursor: RuntimeCursor }
  | { kind: 'query' };

export type RuntimeStreamCompletion = 'eof' | 'stale';

export const RUNTIME_EVENT_WINDOW = 500;
export const RUNTIME_CURSOR_SCOPE_WINDOW = 500;

export function emptyRuntimeEventState(): RuntimeEventState {
  return { cursors: new Map(), events: [], seen: new Set() };
}

function cursorAfter(left: RuntimeCursor, right: RuntimeCursor): boolean {
  return BigInt(left) > BigInt(right);
}

/** The server cursor is authoritative; duplicate replay frames never regress it. */
export function reduceRuntimeEvent(
  state: RuntimeEventState,
  event: RuntimeEvent,
): RuntimeEventState {
  const cursorKey = event.cursor === null ? 'none' : BigInt(event.cursor).toString();
  const key = `${event.resourceScope}:${cursorKey}:${event.id}`;
  if (state.seen.has(key)) return state;

  const events = [...state.events, event].slice(-RUNTIME_EVENT_WINDOW);
  const seen = new Set(events.map((entry) => {
    const entryCursor = entry.cursor === null ? 'none' : BigInt(entry.cursor).toString();
    return `${entry.resourceScope}:${entryCursor}:${entry.id}`;
  }));
  const cursors = new Map(state.cursors);
  const previousCursor = cursors.get(event.resourceScope) ?? 0;
  if (event.cursor !== null && cursorAfter(event.cursor, previousCursor)) {
    // Refresh insertion order so actively monitored scopes survive the bound.
    cursors.delete(event.resourceScope);
    cursors.set(event.resourceScope, event.cursor);
  }
  while (cursors.size > RUNTIME_CURSOR_SCOPE_WINDOW) {
    const oldestScope = cursors.keys().next().value;
    if (oldestScope === undefined) break;
    cursors.delete(oldestScope);
  }
  return { cursors, events, seen };
}

export function cursorForScope(state: RuntimeEventState, resourceScope: string): RuntimeCursor {
  return state.cursors.get(resourceScope) ?? 0;
}

export function eventsForScope(
  state: RuntimeEventState,
  resourceScope: string,
  limit = 12,
): RuntimeEvent[] {
  if (limit <= 0) return [];
  return state.events.filter((event) => event.resourceScope === resourceScope).slice(-limit);
}

/** Reconnect must replay the committed cursor before a Query corrects projections. */
export function reconnectRecovery(cursor: RuntimeCursor): RuntimeRecovery[] {
  return [{ kind: 'replay', cursor }, { kind: 'query' }];
}

/**
 * Awaiting iterator.next() is a race boundary. A scope can change while the
 * promise is pending, so both fulfilled and rejected results must be checked
 * against the owning epoch before they are allowed to affect the view.
 */
export async function consumeIteratorForEpoch<T>(
  iterator: AsyncIterator<T>,
  isCurrent: () => boolean,
  onValue: (value: T) => void,
): Promise<RuntimeStreamCompletion> {
  while (isCurrent()) {
    let next: IteratorResult<T>;
    try {
      next = await iterator.next();
    } catch (error) {
      if (!isCurrent()) return 'stale';
      throw error;
    }
    if (!isCurrent()) return 'stale';
    if (next.done) return 'eof';
    onValue(next.value);
  }
  return 'stale';
}

export function graphRunIsTerminal(status: string): boolean {
  return status === 'completed'
    || status === 'failed'
    || status === 'cancelled'
    || status === 'interrupted'
    || status === 'manual_reconcile_required';
}

export function containsUnknownOutcome(value: unknown, depth = 0): boolean {
  if (depth > 8) return false;
  if (typeof value === 'string') {
    const normalized = value.toLowerCase();
    return normalized.includes('manual_reconcile') || normalized.includes('outcome_unknown');
  }
  if (Array.isArray(value)) {
    return value.some((entry) => containsUnknownOutcome(entry, depth + 1));
  }
  if (typeof value === 'object' && value !== null) {
    return Object.values(value).some((entry) => containsUnknownOutcome(entry, depth + 1));
  }
  return false;
}

/** Commands with unknown side effects require Query/user reconciliation, never replay. */
export function canAutomaticallyReplayCommand(result: unknown): boolean {
  return !containsUnknownOutcome(result);
}

/** Keep one key for all retries of the same logical action. */
export class IdempotencyKeyRegistry {
  private readonly keys = new Map<string, string>();

  get(logicalAction: string): string {
    const existing = this.keys.get(logicalAction);
    if (existing) return existing;
    let actionHash = 2166136261;
    for (let index = 0; index < logicalAction.length; index += 1) {
      actionHash ^= logicalAction.charCodeAt(index);
      actionHash = Math.imul(actionHash, 16777619);
    }
    const suffix = globalThis.crypto?.randomUUID?.()
      ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const key = `gui-${(actionHash >>> 0).toString(36)}-${suffix}`;
    this.keys.set(logicalAction, key);
    return key;
  }

  release(logicalAction: string): void {
    this.keys.delete(logicalAction);
  }
}
