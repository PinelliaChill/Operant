import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import {
  boundedAuditFacts,
  mcpActionState,
  phase45SideEffectsDisabled,
  phase45UnavailableMessage,
  policyDecisionStatus,
  preservePhase45Projection,
} from '../src/live45/phase45State.ts';

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

test('refresh failure retains stale roots and fail-closes MCP actions until recovery', () => {
  const roots = [{ rootRef: 'trusted-root' }];
  assert.equal(phase45SideEffectsDisabled('ready', 'connected', false), false);
  assert.equal(phase45SideEffectsDisabled('loading', 'connected', false), true);

  const retained = preservePhase45Projection(roots);
  assert.equal(retained, roots);
  assert.deepEqual(retained, [{ rootRef: 'trusted-root' }]);
  assert.equal(phase45SideEffectsDisabled('error', 'connected', false), true);
  assert.match(
    phase45UnavailableMessage('error', 'connected', 'invalid_error_envelope') ?? '',
    /Core 暂时不可用.*只读 Projection.*诊断码：invalid_error_envelope/,
  );

  assert.equal(phase45SideEffectsDisabled('ready', 'connected', false), false);
});

test('live Phase45 refresh failure has no Demo fallback', () => {
  const source = readFileSync(new URL('../src/live45/Phase45Context.tsx', import.meta.url), 'utf8');
  assert.match(source, /setWorkspaceRoots\(preservePhase45Projection\)/);
  assert.doesNotMatch(source, /DemoContext|fixtures|mockClient/);
});
