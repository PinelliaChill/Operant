import assert from 'node:assert/strict';
import test from 'node:test';
import {
  containsManualReconcile,
  emptyEventAccumulator,
  reduceEvent,
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
