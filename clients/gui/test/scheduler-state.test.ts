import assert from 'node:assert/strict';
import test from 'node:test';
import {
  SchedulerIdempotencyKeys,
  beginSchedulerLoad,
  emptySchedulerSnapshot,
  failSchedulerLoad,
  finishSchedulerLoad,
  replaceSchedule,
} from '../src/live45/schedulerState.ts';
import type { LiveRunRequest, LiveSchedule } from '../src/live45/schedulerAdapter.ts';

const schedule: LiveSchedule = {
  id: 'schedule-1', version: 1, name: 'Nightly', triggerKind: 'cron',
  cronExpression: '0 3 * * *', timerAt: null, timezoneName: 'UTC',
  workflowId: 'workflow-1', workflowVersion: 1, status: 'enabled',
  dispatchIdempotency: 'idempotent', createdAt: null,
};
const request: LiveRunRequest = {
  id: 'request-1', scheduleId: 'schedule-1', scheduleVersion: 1, status: 'queued',
  occurrenceAt: '2026-09-03T00:00:00Z', availableAt: '2026-09-03T00:00:00Z',
  attemptCount: 0, maxAttempts: 3, workflowId: 'workflow-1', workflowVersion: 1,
  workflowRunId: null, replayOfRequestId: null, lastErrorCode: null, updatedAt: null,
};

test('load state preserves a stale projection on refresh failure and clears stale after success', () => {
  const loaded = finishSchedulerLoad([schedule], [request], []);
  assert.equal(beginSchedulerLoad(loaded).phase, 'loading');
  assert.deepEqual(failSchedulerLoad(loaded), { ...loaded, phase: 'error', stale: true });
  assert.equal(failSchedulerLoad(emptySchedulerSnapshot()).stale, false);
  assert.equal(finishSchedulerLoad([schedule], [], []).stale, false);
});

test('schedule status result replaces the same server id without duplicating it', () => {
  const paused = { ...schedule, version: 2, status: 'paused' as const };
  assert.deepEqual(replaceSchedule([schedule], paused), [paused]);
  assert.deepEqual(replaceSchedule([], paused), [paused]);
});

test('idempotency registry reuses a key for retries and releases it after known success', () => {
  let sequence = 0;
  const keys = new SchedulerIdempotencyKeys(() => `key-${++sequence}`);
  assert.equal(keys.acquire('trigger:schedule-1'), 'key-1');
  assert.equal(keys.acquire('trigger:schedule-1'), 'key-1');
  assert.equal(keys.has('trigger:schedule-1'), true);
  keys.release('trigger:schedule-1');
  assert.equal(keys.acquire('trigger:schedule-1'), 'key-2');
  assert.equal(keys.acquire('replay:request-1'), 'key-3');
});
