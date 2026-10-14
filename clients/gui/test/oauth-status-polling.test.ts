import assert from 'node:assert/strict';
import test from 'node:test';
import { sameOAuthAttempt, startOAuthStatusPolling } from '../src/features/settings/oauthStatusPolling.ts';

test('a foreign OAuth status cannot replace the current attempt', () => {
  const current = { attempt_id: 'attempt-1', provider: 'codex', connection_id: 'connection-1' };
  assert.equal(sameOAuthAttempt(current, current), true);
  assert.equal(sameOAuthAttempt(current, { ...current, attempt_id: 'attempt-2' }), false);
  assert.equal(sameOAuthAttempt(current, { ...current, provider: 'chatgpt' }), false);
  assert.equal(sameOAuthAttempt(current, { ...current, connection_id: 'connection-2' }), false);
});

test('Codex status polling retries a failed read without starting another login', async () => {
  const scheduled = new Map<number, () => void>();
  let id = 0;
  const timer = {
    set: (callback: () => void, delayMs: number) => { assert.equal(delayMs, 2500); scheduled.set(++id, callback); return id; },
    clear: (timerId: number) => { scheduled.delete(timerId); },
  };
  let reads = 0;
  const statuses: string[] = [];
  const errors: unknown[] = [];
  const stop = startOAuthStatusPolling({
    read: async () => { reads++; if (reads === 1) throw new Error('temporary disconnect'); return 'pending'; },
    onStatus: (status) => statuses.push(status),
    onError: (error) => errors.push(error),
    retryOnError: true,
    timer,
  });
  const runNext = async () => {
    const [timerId, callback] = [...scheduled][0];
    scheduled.delete(timerId);
    callback();
    await new Promise<void>((resolve) => setImmediate(resolve));
  };
  await runNext();
  assert.equal(reads, 1);
  assert.equal(errors.length, 1);
  assert.equal(scheduled.size, 1);
  await runNext();
  assert.equal(reads, 2);
  assert.deepEqual(statuses, ['pending']);
  stop();
  assert.equal(scheduled.size, 0);
});
