import assert from 'node:assert/strict';
import test from 'node:test';
import { mapThreadProjection } from '../src/live/liveAdapter.ts';
import { visibleThreadTree } from '../src/features/chat/liveThreadTree.ts';

function thread(id: string, parent: string | null) {
  return mapThreadProjection({
    id,
    cursor: null,
    parent_thread_id: parent,
    workspace_ref: '/project',
    status: 'active',
    legacy_refs: [],
    created_at: '2026-09-25T00:00:00Z',
    updated_at: '2026-09-25T00:00:00Z',
    archived_at: null,
  });
}

test('live tree uses projected parents and hides descendants when folded', () => {
  const threads = [thread('child', 'parent'), thread('root', null), thread('parent', 'root')];
  assert.equal(threads[0].parentThreadId, 'parent');
  assert.deepEqual(visibleThreadTree(threads, '', []).map(({ thread: item, depth }) => [item.id, depth]), [
    ['root', 0], ['parent', 1], ['child', 2],
  ]);
  assert.deepEqual(visibleThreadTree(threads, '', ['parent']).map(({ thread: item }) => item.id), ['root', 'parent']);
  assert.deepEqual(visibleThreadTree(threads, 'child', ['parent']).map(({ thread: item }) => item.id), ['root', 'parent', 'child']);
});

test('orphan and cyclic Core threads remain navigable', () => {
  const threads = [thread('orphan', 'missing'), thread('a', 'b'), thread('b', 'a')];
  assert.deepEqual(visibleThreadTree(threads, '', []).map(({ thread: item }) => item.id), ['orphan', 'a', 'b']);
});
