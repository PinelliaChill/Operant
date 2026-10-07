import assert from 'node:assert/strict';
import test from 'node:test';
import type { GraphRunStatus } from '../../../sdk/typescript-client/phase23.generated.ts';
import { graphRunControlAvailability } from '../src/features/collab/graphRunControls.ts';

test('Graph controls follow the Runtime resume and cancel state rules', () => {
  assert.deepEqual(graphRunControlAvailability('interrupted', true, false), { canResume: true, canCancel: true });
  for (const status of ['created', 'queued', 'running', 'waiting_input', 'waiting_approval'] as GraphRunStatus[]) {
    assert.deepEqual(graphRunControlAvailability(status, true, false), { canResume: false, canCancel: true });
  }
  for (const status of ['completed', 'failed', 'cancelled', 'manual_reconcile_required'] as GraphRunStatus[]) {
    assert.deepEqual(graphRunControlAvailability(status, true, false), { canResume: false, canCancel: false });
  }
  assert.deepEqual(graphRunControlAvailability('interrupted', false, false), { canResume: false, canCancel: false });
  assert.deepEqual(graphRunControlAvailability('interrupted', true, true), { canResume: false, canCancel: false });
  assert.deepEqual(graphRunControlAvailability(null, true, false), { canResume: false, canCancel: false });
});
