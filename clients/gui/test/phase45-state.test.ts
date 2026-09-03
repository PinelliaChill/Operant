import assert from 'node:assert/strict';
import test from 'node:test';
import { mcpActionState, policyDecisionStatus } from '../src/live45/phase45State.ts';

test('MCP actions follow lifecycle and fail closed while busy', () => {
  assert.deepEqual(mcpActionState('stopped', false), {
    canStart: true,
    canStop: false,
    canDelete: true,
    canRefreshTools: false,
  });
  assert.deepEqual(mcpActionState('running', false), {
    canStart: false,
    canStop: true,
    canDelete: false,
    canRefreshTools: true,
  });
  assert.equal(Object.values(mcpActionState('failed', true)).every((value) => !value), true);
});

test('ASK remains pending rather than being presented as allowed', () => {
  assert.equal(policyDecisionStatus('allow'), 'safe');
  assert.equal(policyDecisionStatus('ask'), 'pending');
  assert.equal(policyDecisionStatus('deny'), 'denied');
});
