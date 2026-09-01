import assert from 'node:assert/strict';
import test from 'node:test';
import {
  LIVE_EVENT_WINDOW_SIZE,
  approvalActionKey,
  connectionLossState,
  containsManualReconcile,
  emptyEventAccumulator,
  eventNeedsManualReconcile,
  reduceEvent,
  threadForSession,
} from '../src/live/liveState.ts';

function event(sequence: number, id = `evt-${sequence}`) {
  return {
    id,
    sequence,
    event_type: 'model.delta',
    schema_version: 'phase1e.v1',
    thread_id: 'thread-live',
    occurred_at: `2026-09-01T00:00:${String(sequence).padStart(2, '0')}Z`,
    payload: { delta_text: `delta-${sequence}` },
  } as const;
}

test('reducer deduplicates by resource scope and committed cursor', () => {
  const first = reduceEvent(emptyEventAccumulator(), 'thread-live', event(7));
  const duplicate = reduceEvent(first, 'thread-live', event(7, 'different-id'));
  const otherScope = reduceEvent(first, 'thread-other', event(7));

  assert.equal(first.events.length, 1);
  assert.equal(first.cursor, 7);
  assert.equal(duplicate, first);
  assert.equal(otherScope.events.length, 2);
  assert.equal(otherScope.cursor, 7);
});

test('cursor reducer keeps an int64 cursor lossless', () => {
  const cursor = 9007199254740993n;
  const result = reduceEvent(emptyEventAccumulator(), 'thread-live', {
    ...event(1),
    id: 'large-cursor-event',
    sequence: cursor,
  });
  assert.equal(result.cursor, cursor);
  assert.equal(reduceEvent(result, 'thread-live', {
    ...event(1),
    id: 'replayed-event',
    sequence: cursor,
  }), result);
});

test('manual reconcile marker is found in nested projection payloads', () => {
  assert.equal(containsManualReconcile({ outcome: { state: 'outcome_unknown' } }), true);
  assert.equal(containsManualReconcile({ items: [{ status: 'manual_reconcile_required' }] }), true);
  assert.equal(containsManualReconcile({ status: 'completed' }), false);
});

test('manual reconcile SSE error/detail is sticky input and preserved', () => {
  const errorEvent = {
    ...event(2),
    error: {
      code: 'run_outcome_unknown',
      message: 'Core cannot determine the outcome',
      retryable: true,
      recovery: 'manual_reconcile',
      detail: { command_id: 'cmd-1' },
    },
    detail: { source: 'sse' },
  };
  assert.equal(eventNeedsManualReconcile(errorEvent), true);
  assert.deepEqual(errorEvent.error.detail, { command_id: 'cmd-1' });
  assert.equal(connectionLossState('disconnected').phase, 'error');
  assert.equal(connectionLossState('reconnecting').phase, 'connecting');
});

test('session projection and approval action keys retain exact scopes', () => {
  const projected = {
    id: 'thread-created',
    title: 'thread-created',
    workspaceRef: '/work/a',
    workspace: '/work/a',
    status: 'active',
    createdAt: 'x',
    updatedAt: 'y',
    created_at: 'x',
    updated_at: 'y',
    archivedAt: null,
    legacyRefs: [{ source_type: 'session', source_id: 'session-created' }],
    sessionId: 'session-created',
    session_id: 'session-created',
    workflowRunId: null,
  };
  assert.equal(threadForSession([projected], 'session-created'), projected);
  assert.equal(threadForSession([projected], 'session-missing'), undefined);
  assert.notEqual(approvalActionKey('session-a', 'approval-a', 'approve'), approvalActionKey('session-a', 'approval-a', 'reject'));
  let result = emptyEventAccumulator();
  for (let sequence = 1; sequence <= LIVE_EVENT_WINDOW_SIZE + 10; sequence += 1) {
    result = reduceEvent(result, 'thread-live', event(sequence));
  }
  assert.equal(result.events.length, LIVE_EVENT_WINDOW_SIZE);
  assert.equal(result.seen.size, LIVE_EVENT_WINDOW_SIZE);
});
