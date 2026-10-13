import assert from 'node:assert/strict';
import test from 'node:test';
import { mapThreadProjection } from '../src/live/liveAdapter.ts';
import { metadataRefreshKey } from '../src/live/metadataRefreshKey.ts';

const thread = (id: string, updatedAt = '2026-10-07T00:00:00Z') => mapThreadProjection({
  id, cursor: null, parent_thread_id: null, workspace_ref: '/project', status: 'active',
  legacy_refs: [], created_at: '2026-10-07T00:00:00Z', updated_at: updatedAt, archived_at: null,
});
const event = (id: string, sequence: number, eventType: string) => ({
  id, sequence, event_type: eventType, schema_version: 'phase1e.v1', thread_id: 'thread-a',
  occurred_at: '2026-10-07T00:00:00Z', payload: {},
}) as never;

test('title refresh key changes when an external thread appears or selection changes', () => {
  const first = metadataRefreshKey([thread('thread-a')], 'thread-a', []);
  assert.notEqual(metadataRefreshKey([thread('thread-a'), thread('thread-b')], 'thread-a', []), first);
  assert.notEqual(metadataRefreshKey([thread('thread-a')], 'thread-b', []), first);
  assert.notEqual(metadataRefreshKey([thread('thread-a', '2026-10-07T01:00:00Z')], 'thread-a', []), first);
});

test('title refresh waits for a terminal run event rather than reading on every model delta', () => {
  const base = metadataRefreshKey([thread('thread-a')], 'thread-a', []);
  const streaming = metadataRefreshKey([thread('thread-a')], 'thread-a', [event('delta-1', 1, 'model.delta'), event('delta-2', 2, 'model.delta')]);
  assert.equal(streaming, base);
  assert.notEqual(metadataRefreshKey([thread('thread-a')], 'thread-a', [event('done', 3, 'agent.completed')]), base);
});
