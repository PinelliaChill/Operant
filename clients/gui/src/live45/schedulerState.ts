import type { LiveRunRequest, LiveSchedule } from './schedulerAdapter';

export type SchedulerLoadPhase = 'idle' | 'loading' | 'ready' | 'error';

export interface SchedulerSnapshot {
  phase: SchedulerLoadPhase;
  schedules: LiveSchedule[];
  queue: LiveRunRequest[];
  deadLetter: LiveRunRequest[];
  stale: boolean;
}

export function emptySchedulerSnapshot(): SchedulerSnapshot {
  return { phase: 'idle', schedules: [], queue: [], deadLetter: [], stale: false };
}

export function beginSchedulerLoad(snapshot: SchedulerSnapshot): SchedulerSnapshot {
  return { ...snapshot, phase: 'loading' };
}

export function finishSchedulerLoad(
  schedules: LiveSchedule[],
  queue: LiveRunRequest[],
  deadLetter: LiveRunRequest[],
): SchedulerSnapshot {
  return { phase: 'ready', schedules, queue, deadLetter, stale: false };
}

export function failSchedulerLoad(snapshot: SchedulerSnapshot): SchedulerSnapshot {
  const hasProjection = snapshot.schedules.length + snapshot.queue.length + snapshot.deadLetter.length > 0;
  return { ...snapshot, phase: 'error', stale: hasProjection };
}

export function replaceSchedule(
  schedules: readonly LiveSchedule[],
  next: LiveSchedule,
): LiveSchedule[] {
  const index = schedules.findIndex((schedule) => schedule.id === next.id);
  if (index < 0) return [next, ...schedules];
  return schedules.map((schedule) => schedule.id === next.id ? next : schedule);
}

/** Stable keys are retained across retryable failures and released after a known success. */
export class SchedulerIdempotencyKeys {
  private readonly keys = new Map<string, string>();
  private readonly createKey: () => string;

  constructor(createKey: () => string = () => globalThis.crypto.randomUUID()) {
    this.createKey = createKey;
  }

  acquire(action: string): string {
    const existing = this.keys.get(action);
    if (existing) return existing;
    const key = this.createKey();
    this.keys.set(action, key);
    return key;
  }

  release(action: string): void {
    this.keys.delete(action);
  }

  has(action: string): boolean {
    return this.keys.has(action);
  }
}
