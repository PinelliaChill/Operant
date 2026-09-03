import { currentBrowserOrigin } from '../lib/liveBaseUrl.ts';

export interface ScheduleCreateInput {
  name: string;
  trigger_kind: 'cron' | 'timer';
  cron_expression: string | null;
  timer_at: string | null;
  timezone_name: string;
  workflow_id: string;
  workflow_version: number;
}

export interface ScheduleStatusInput {
  status: 'enabled' | 'paused' | 'cancelled';
  expected_version: number;
}

class SchedulerHttpError extends Error {
  readonly code: string;
  readonly recovery: string;

  constructor(code: string, message: string, recovery = 'none') {
    super(message);
    this.name = 'SchedulerHttpError';
    this.code = code;
    this.recovery = recovery;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Page-local Phase 5A Query/Command adapter over the existing trusted same-origin boundary. */
export class SchedulerClient {
  private readonly baseUrl: string;

  constructor(baseUrl = currentBrowserOrigin()) {
    this.baseUrl = baseUrl.replace(/\/+$/, '');
  }

  private async request(path: string, init?: RequestInit): Promise<Record<string, unknown>> {
    const response = await fetch(`${this.baseUrl}${path}`, {
      ...init,
      headers: { Accept: 'application/json', ...(init?.body ? { 'Content-Type': 'application/json' } : {}), ...init?.headers },
    });
    let payload: unknown;
    try {
      payload = await response.json();
    } catch {
      throw new SchedulerHttpError('invalid_response', 'Core 返回了无法解析的响应。');
    }
    if (!response.ok) {
      if (isRecord(payload) && isRecord(payload.error)
        && typeof payload.error.code === 'string' && typeof payload.error.message === 'string') {
        throw new SchedulerHttpError(
          payload.error.code,
          payload.error.message,
          typeof payload.error.recovery === 'string' ? payload.error.recovery : 'none',
        );
      }
      const detail = isRecord(payload) && typeof payload.detail === 'string' ? payload.detail : `HTTP ${response.status}`;
      throw new SchedulerHttpError('scheduler_http_error', detail, response.status === 409 ? 'refresh_and_retry' : 'retry');
    }
    if (!isRecord(payload)) throw new SchedulerHttpError('invalid_response', 'Core 响应不是对象。');
    return payload;
  }

  listSchedules(): Promise<Record<string, unknown>> {
    return this.request('/v1/schedules');
  }

  listQueue(): Promise<Record<string, unknown>> {
    return this.request('/v1/scheduler/queue');
  }

  listDeadLetter(): Promise<Record<string, unknown>> {
    return this.request('/v1/scheduler/dead-letter');
  }

  createSchedule(input: ScheduleCreateInput): Promise<Record<string, unknown>> {
    return this.request('/v1/schedules', { method: 'POST', body: JSON.stringify(input) });
  }

  setScheduleStatus(scheduleId: string, input: ScheduleStatusInput): Promise<Record<string, unknown>> {
    return this.request(`/v1/schedules/${encodeURIComponent(scheduleId)}/status`, { method: 'POST', body: JSON.stringify(input) });
  }

  triggerSchedule(scheduleId: string, idempotencyKey: string): Promise<Record<string, unknown>> {
    return this.request(`/v1/schedules/${encodeURIComponent(scheduleId)}/trigger`, { method: 'POST', body: JSON.stringify({ idempotency_key: idempotencyKey }) });
  }

  replayDeadLetter(requestId: string, idempotencyKey: string): Promise<Record<string, unknown>> {
    return this.request(`/v1/scheduler/dead-letter/${encodeURIComponent(requestId)}/replay`, { method: 'POST', body: JSON.stringify({ idempotency_key: idempotencyKey }) });
  }
}
