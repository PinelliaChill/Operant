import assert from 'node:assert/strict';
import test from 'node:test';
import type { Phase45 } from '@operant/sdk';
import {
  SchedulerClient,
  type GeneratedSchedulerClient,
} from '../src/live45/schedulerClient.ts';

function generatedDouble(overrides: Partial<GeneratedSchedulerClient> = {}): {
  client: GeneratedSchedulerClient;
  calls: Array<{ method: string; args: unknown[] }>;
} {
  const calls: Array<{ method: string; args: unknown[] }> = [];
  const result = (method: string, value: Record<string, unknown> = {}) => (...args: unknown[]) => {
    calls.push({ method, args });
    return Promise.resolve(value);
  };
  const client: GeneratedSchedulerClient = {
    negotiateProtocol: result('negotiateProtocol', {
      protocol_version: 'phase45.v1', schema_digest: 'digest',
      min_client_version: 'phase45.v1', capabilities: [],
    }) as GeneratedSchedulerClient['negotiateProtocol'],
    listSchedules: result('listSchedules') as GeneratedSchedulerClient['listSchedules'],
    listSchedulerQueue: result('listSchedulerQueue') as GeneratedSchedulerClient['listSchedulerQueue'],
    listDeadLetter: result('listDeadLetter') as GeneratedSchedulerClient['listDeadLetter'],
    createSchedule: result('createSchedule') as GeneratedSchedulerClient['createSchedule'],
    setScheduleStatus: result('setScheduleStatus') as GeneratedSchedulerClient['setScheduleStatus'],
    triggerSchedule: result('triggerSchedule') as GeneratedSchedulerClient['triggerSchedule'],
    replayDeadLetter: result('replayDeadLetter') as GeneratedSchedulerClient['replayDeadLetter'],
    ...overrides,
  };
  return { client, calls };
}

test('scheduler adapter delegates every operation to generated Phase45Client methods', async () => {
  const { client: generated, calls } = generatedDouble();
  const client = new SchedulerClient(generated);
  const schedule: Phase45.ScheduleDefinition = {
    name: 'Nightly', trigger_kind: 'cron', cron_expression: '0 3 * * *', timer_at: null,
    timezone_name: 'UTC', workflow_id: 'workflow-1', workflow_version: 2,
  };

  await client.negotiateProtocol(true);
  await client.listSchedules();
  await client.listQueue();
  await client.listDeadLetter();
  await client.createSchedule(schedule);
  await client.setScheduleStatus('schedule-1', { status: 'paused', expected_version: 2 });
  await client.triggerSchedule('schedule-1', 'trigger-key');
  await client.replayDeadLetter('request-1', 'replay-key');

  assert.deepEqual(calls.map((call) => call.method), [
    'negotiateProtocol', 'listSchedules', 'listSchedulerQueue', 'listDeadLetter',
    'createSchedule', 'setScheduleStatus', 'triggerSchedule', 'replayDeadLetter',
  ]);
  assert.deepEqual(calls[0]?.args, [true]);
  assert.deepEqual(calls[6]?.args, ['schedule-1', { idempotency_key: 'trigger-key' }]);
  assert.deepEqual(calls[7]?.args, ['request-1', { idempotency_key: 'replay-key' }]);
});

test('generated protocol negotiation failure propagates explicitly without fallback queries', async () => {
  const incompatible = Object.assign(new Error('Core Schema digest mismatch'), {
    code: 'protocol_incompatible', recovery: 'refresh_and_retry',
  });
  const { client: generated, calls } = generatedDouble({
    negotiateProtocol: async () => { throw incompatible; },
  });
  const client = new SchedulerClient(generated);

  await assert.rejects(client.negotiateProtocol(), (error: unknown) => error === incompatible);
  assert.deepEqual(calls, []);
});
