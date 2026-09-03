export type TriggerKind = 'cron' | 'timer';
export type ScheduleStatus = 'enabled' | 'paused' | 'cancelled';
export type DispatchIdempotency = 'idempotent' | 'non_idempotent';
export type RunRequestStatus = 'queued' | 'leased' | 'retry_wait' | 'succeeded' | 'cancelled' | 'dead_letter' | 'manual_reconcile_required';

export interface LiveSchedule {
  id: string;
  version: number;
  name: string;
  triggerKind: TriggerKind;
  cronExpression: string | null;
  timerAt: string | null;
  timezoneName: string;
  workflowId: string;
  workflowVersion: number;
  status: ScheduleStatus;
  dispatchIdempotency: DispatchIdempotency;
  createdAt: string | null;
}

export interface LiveRunRequest {
  id: string;
  scheduleId: string;
  scheduleVersion: number;
  status: RunRequestStatus;
  occurrenceAt: string;
  availableAt: string;
  attemptCount: number;
  maxAttempts: number;
  workflowId: string;
  workflowVersion: number;
  workflowRunId: string | null;
  replayOfRequestId: string | null;
  lastErrorCode: string | null;
  updatedAt: string | null;
}

function record(value: unknown, label: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new TypeError(`${label} must be an object`);
  }
  return value as Record<string, unknown>;
}

function string(value: unknown, field: string): string {
  if (typeof value !== 'string' || value.length === 0) throw new TypeError(`${field} is missing`);
  return value;
}

function optionalString(value: unknown, field: string): string | null {
  if (value === undefined || value === null) return null;
  return string(value, field);
}

function integer(value: unknown, field: string): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0) {
    throw new TypeError(`${field} must be a safe integer`);
  }
  return value;
}

function pageItems(value: unknown, label: string): Record<string, unknown>[] {
  const page = record(value, label);
  if (!Array.isArray(page.items)) throw new TypeError(`${label}.items must be an array`);
  return page.items.map((item, index) => record(item, `${label}.items[${index}]`));
}

const scheduleStatuses = new Set<ScheduleStatus>(['enabled', 'paused', 'cancelled']);
const triggerKinds = new Set<TriggerKind>(['cron', 'timer']);
const requestStatuses = new Set<RunRequestStatus>([
  'queued', 'leased', 'retry_wait', 'succeeded', 'cancelled', 'dead_letter',
  'manual_reconcile_required',
]);

export function mapSchedule(value: unknown): LiveSchedule {
  const item = record(value, 'schedule');
  const status = string(item.status, 'schedule.status') as ScheduleStatus;
  const triggerKind = string(item.trigger_kind, 'schedule.trigger_kind') as TriggerKind;
  if (!scheduleStatuses.has(status)) throw new TypeError(`unsupported schedule.status: ${status}`);
  if (!triggerKinds.has(triggerKind)) throw new TypeError(`unsupported schedule.trigger_kind: ${triggerKind}`);
  const dispatchIdempotency = string(
    item.dispatch_idempotency,
    'schedule.dispatch_idempotency',
  ) as DispatchIdempotency;
  if (dispatchIdempotency !== 'idempotent' && dispatchIdempotency !== 'non_idempotent') {
    throw new TypeError(`unsupported schedule.dispatch_idempotency: ${dispatchIdempotency}`);
  }
  return {
    id: string(item.id, 'schedule.id'),
    version: integer(item.version, 'schedule.version'),
    name: string(item.name, 'schedule.name'),
    triggerKind,
    cronExpression: optionalString(item.cron_expression, 'schedule.cron_expression'),
    timerAt: optionalString(item.timer_at, 'schedule.timer_at'),
    timezoneName: string(item.timezone_name, 'schedule.timezone_name'),
    workflowId: string(item.workflow_id, 'schedule.workflow_id'),
    workflowVersion: integer(item.workflow_version, 'schedule.workflow_version'),
    status,
    dispatchIdempotency,
    createdAt: optionalString(item.created_at, 'schedule.created_at'),
  };
}

export function mapSchedules(value: unknown): LiveSchedule[] {
  return pageItems(value, 'schedules').map(mapSchedule);
}

export function mapRunRequest(value: unknown): LiveRunRequest {
  const item = record(value, 'run request');
  const status = string(item.status, 'run_request.status') as RunRequestStatus;
  if (!requestStatuses.has(status)) throw new TypeError(`unsupported run_request.status: ${status}`);
  return {
    id: string(item.id, 'run_request.id'),
    scheduleId: string(item.schedule_id, 'run_request.schedule_id'),
    scheduleVersion: integer(item.schedule_version, 'run_request.schedule_version'),
    status,
    occurrenceAt: string(item.occurrence_at, 'run_request.occurrence_at'),
    availableAt: string(item.available_at, 'run_request.available_at'),
    attemptCount: integer(item.attempt_count, 'run_request.attempt_count'),
    maxAttempts: integer(item.max_attempts, 'run_request.max_attempts'),
    workflowId: string(item.workflow_id, 'run_request.workflow_id'),
    workflowVersion: integer(item.workflow_version, 'run_request.workflow_version'),
    workflowRunId: optionalString(item.workflow_run_id, 'run_request.workflow_run_id'),
    replayOfRequestId: optionalString(item.replay_of_request_id, 'run_request.replay_of_request_id'),
    lastErrorCode: optionalString(item.last_error_code, 'run_request.last_error_code'),
    updatedAt: optionalString(item.updated_at, 'run_request.updated_at'),
  };
}

export function mapRunRequests(value: unknown): LiveRunRequest[] {
  return pageItems(value, 'run requests').map(mapRunRequest);
}

export function schedulerError(error: unknown): { code: string; message: string; recovery: string } {
  if (error instanceof Error) {
    const candidate = error as Error & { code?: unknown; recovery?: unknown };
    return {
      code: typeof candidate.code === 'string' ? candidate.code : 'scheduler_request_failed',
      message: candidate.message,
      recovery: typeof candidate.recovery === 'string' ? candidate.recovery : 'retry',
    };
  }
  return {
    code: 'scheduler_request_failed',
    message: 'Core 调度请求失败，请检查本地服务后重试。',
    recovery: 'retry',
  };
}
