import assert from 'node:assert/strict';
import test from 'node:test';
import { refreshThenOpenTeamReply } from '../src/features/collab/teamReplyNavigation.ts';

test('team reply opens only after a successful live projection refresh', async () => {
  const calls: string[] = [];
  const result = await refreshThenOpenTeamReply(async () => { calls.push('refresh'); return true; }, () => true, () => calls.push('open'));
  assert.equal(result, 'opened');
  assert.deepEqual(calls, ['refresh', 'open']);
});

test('failed refresh or changed run/route cannot open a stale reply', async () => {
  let opens = 0;
  assert.equal(await refreshThenOpenTeamReply(async () => false, () => true, () => { opens += 1; }), 'refresh_failed');
  assert.equal(await refreshThenOpenTeamReply(async () => true, () => false, () => { opens += 1; }), 'stale');
  assert.equal(opens, 0);
});
