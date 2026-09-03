import assert from 'node:assert/strict';
import test from 'node:test';
import { SchedulerClient } from '../src/live45/schedulerClient.ts';

test('scheduler client uses same-origin Phase 5A paths and exact idempotency body', async () => {
  const originalFetch = globalThis.fetch;
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    calls.push({ url: String(input), init });
    return new Response(JSON.stringify({ id: 'request-1' }), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    });
  }) as typeof fetch;
  try {
    const client = new SchedulerClient('https://operant.local/');
    await client.triggerSchedule('schedule / 1', 'stable-key');
    assert.equal(calls[0]?.url, 'https://operant.local/v1/schedules/schedule%20%2F%201/trigger');
    assert.deepEqual(JSON.parse(String(calls[0]?.init?.body)), { idempotency_key: 'stable-key' });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('scheduler client preserves typed safe errors and normalizes legacy HTTP detail', async () => {
  const originalFetch = globalThis.fetch;
  try {
    globalThis.fetch = (async () => new Response(JSON.stringify({
      error: { code: 'schedule_conflict', message: 'stale version', recovery: 'refresh_and_retry' },
      detail: {},
    }), { status: 409, headers: { 'Content-Type': 'application/json' } })) as typeof fetch;
    await assert.rejects(
      new SchedulerClient('https://operant.local').listSchedules(),
      (error: unknown) => error instanceof Error
        && Object.assign(error as Error & { code?: string; recovery?: string }).code === 'schedule_conflict'
        && (error as Error & { recovery?: string }).recovery === 'refresh_and_retry',
    );

    globalThis.fetch = (async () => new Response(JSON.stringify({ detail: 'schedule not found' }), {
      status: 404, headers: { 'Content-Type': 'application/json' },
    })) as typeof fetch;
    await assert.rejects(
      new SchedulerClient('https://operant.local').listSchedules(),
      /schedule not found/,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});
