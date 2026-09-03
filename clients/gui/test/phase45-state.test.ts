import assert from 'node:assert/strict';
import test from 'node:test';
import { boundedAuditFacts, mcpActionState, policyDecisionStatus } from '../src/live45/phase45State.ts';

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

test('security audit state keeps only the newest bounded facts', () => {
  const facts = Array.from({ length: 5 }, (_, index) => ({
    eventId: `event-${index}`,
    cursor: index + 1,
    principal: 'gui:user',
    eventType: 'policy.evaluated',
    ruleIds: [],
    createdAt: '2026-09-03T00:00:00Z',
  }));
  assert.deepEqual(boundedAuditFacts(facts, 2).map((fact) => fact.cursor), [4, 5]);
  assert.throws(() => boundedAuditFacts(facts, 0), /audit limit/);
});

test('ASK remains pending rather than being presented as allowed', () => {
  assert.equal(policyDecisionStatus('allow'), 'safe');
  assert.equal(policyDecisionStatus('ask'), 'pending');
  assert.equal(policyDecisionStatus('deny'), 'denied');
});
