import assert from 'node:assert/strict';
import test from 'node:test';
import {
  mapRunRequest,
  mapRunRequests,
  mapSchedule,
  mapSchedules,
  schedulerError,
} from '../src/live45/schedulerAdapter.ts';

const schedule = {
  id: 'schedule-1',
  version: 2,
  name: 'Nightly',
  trigger_kind: 'cron',
  cron_expression: '0 3 * * *',
  timer_at: null,
  timezone_name: 'Asia/Shanghai',
  workflow_id: 'workflow-1',
  workflow_version: 4,
  dispatch_idempotency: 'idempotent',
  status: 'enabled',
  created_at: '2026-09-03T00:00:00Z',
};

const request = {
  id: 'request-1',
  schedule_id: 'schedule-1',
  schedule_version: 2,
  occurrence_at: '2026-09-03T01:00:00Z',
  available_at: '2026-09-03T01:00:05Z',
  workflow_id: 'workflow-1',
  workflow_version: 4,
  max_attempts: 3,
  attempt_count: 2,
  status: 'dead_letter',
  workflow_run_id: null,
  replay_of_request_id: null,
  last_error_code: 'provider.busy',
  updated_at: '2026-09-03T01:01:00Z',
};

test('scheduler adapter maps exact Phase 5A schedule fields', () => {
  assert.deepEqual(mapSchedules({ items: [schedule] }), [mapSchedule(schedule)]);
  assert.deepEqual(mapSchedule(schedule), {
    id: 'schedule-1', version: 2, name: 'Nightly', triggerKind: 'cron',
    cronExpression: '0 3 * * *', timerAt: null, timezoneName: 'Asia/Shanghai',
    workflowId: 'workflow-1', workflowVersion: 4, status: 'enabled',
    dispatchIdempotency: 'idempotent', createdAt: '2026-09-03T00:00:00Z',
  });
});

test('scheduler adapter maps queue and dead-letter facts without deriving state', () => {
  const mapped = mapRunRequest(request);
  assert.equal(mapped.status, 'dead_letter');
  assert.equal(mapped.attemptCount, 2);
  assert.equal(mapped.lastErrorCode, 'provider.busy');
  assert.deepEqual(mapRunRequests({ items: [request] }), [mapped]);
});

test('scheduler adapter rejects malformed pages and unknown statuses', () => {
  assert.throws(() => mapSchedules({ schedules: [] }), /items must be an array/);
  assert.throws(() => mapSchedule({ ...schedule, id: undefined }), /schedule.id is missing/);
  assert.throws(() => mapSchedule({ ...schedule, status: 'running' }), /unsupported schedule.status/);
  assert.throws(() => mapRunRequest({ ...request, status: 'failed' }), /unsupported run_request.status/);
});

test('scheduler error preserves generated recovery metadata and safely normalizes unknown failures', () => {
  const generated = Object.assign(new Error('version conflict'), {
    code: 'schedule_conflict', recovery: 'refresh_and_retry',
  });
  assert.deepEqual(schedulerError(generated), {
    code: 'schedule_conflict', message: 'version conflict', recovery: 'refresh_and_retry',
  });
  assert.deepEqual(schedulerError('offline'), {
    code: 'scheduler_request_failed',
    message: 'Core 调度请求失败，请检查本地服务后重试。',
    recovery: 'retry',
  });
});
