import assert from 'node:assert/strict';
import test from 'node:test';
import { newNode } from '../src/features/collab/canvas/live-graph-model.ts';
import { mergeSources } from '../src/features/collab/graph-merge-model.ts';

test('Merge source selection follows frozen writer keys and shared base revision', () => {
  const merge = { ...newNode('merge', 0), merge_policy: { strategy: 'three_way' as const, source_writer_keys: ['a', 'b'] } };
  const workspaces = [
    { writerWorkspaceId: 'wa', writerKey: 'a', isolationKind: 'worktree', isolationRef: '/tmp/a', ownershipPaths: ['a'], lease: null },
    { writerWorkspaceId: 'wb', writerKey: 'b', isolationKind: 'worktree', isolationRef: '/tmp/b', ownershipPaths: ['b'], lease: null },
  ];
  const artifacts = [
    { writerArtifactId: 'pa', writerWorkspaceId: 'wa', artifactKind: 'patch', baseRevision: 'abcdef0', changedPaths: ['a'], testEvidenceRefs: [] },
    { writerArtifactId: 'pb', writerWorkspaceId: 'wb', artifactKind: 'patch', baseRevision: 'abcdef0', changedPaths: ['b'], testEvidenceRefs: [] },
  ];
  assert.deepEqual(mergeSources(merge, ['pa', 'pb'], artifacts, workspaces), {
    artifactIds: ['pa', 'pb'], workspaceIds: ['wa', 'wb'], baseRevision: 'abcdef0',
  });
  assert.throws(() => mergeSources(merge, ['pa'], artifacts, workspaces), /Writer 集合/);
  assert.throws(() => mergeSources(merge, ['pa', 'pb'], [{ ...artifacts[0], baseRevision: 'old' }, artifacts[1]], workspaces), /基线版本/);
});
