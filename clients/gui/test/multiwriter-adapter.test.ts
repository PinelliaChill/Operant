import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { loadMultiWriterProjection } from '../src/live23/multiwriterAdapter.ts';

test('live settings keeps the remote system route reachable', () => {
  const source = readFileSync(
    new URL('../src/features/settings/SettingsView.tsx', import.meta.url),
    'utf8',
  );
  assert.match(source, /searchParams\.get\('cat'\) === 'system'/);
  assert.match(source, /<RemoteView \/>/);
  assert.match(source, /clientMode === 'live' \? <LiveSettingsView \/>/);
});

test('maps writer state without requiring or exposing lease tokens', async () => {
  const responses: unknown[] = [
    [{
      writer_workspace_id: 'workspace-1', writer_key: 'writer-a',
      isolation_kind: 'worktree', isolation_ref: 'worktree:a',
      ownership_paths: ['src/a'],
      lease: { owner: 'coder-a', fencing: 2, expires_at: '2026-09-04T00:00:00Z' },
    }],
    [{
      writer_artifact_id: 'artifact-1', writer_workspace_id: 'workspace-1',
      artifact_kind: 'patch', changed_paths: ['src/a/main.py'],
      test_evidence_refs: ['pytest'],
    }],
    [{ conflict_id: 'conflict-1', status: 'open', paths: ['src/a/main.py'] }],
    [{ merge_run_id: 'merge-1', merge_node_id: 'merge', status: 'review_required' }],
  ];
  const calls: string[] = [];
  const next = async (runId: string) => {
    calls.push(runId);
    return responses.shift() as Array<Record<string, unknown>>;
  };
  const projection = await loadMultiWriterProjection({
    listWriterWorkspaces: next,
    listWriterArtifacts: next,
    listWriterConflicts: next,
    listMergeRuns: next,
  }, 'run/unsafe');
  assert.deepEqual(calls, ['run/unsafe', 'run/unsafe', 'run/unsafe', 'run/unsafe']);
  assert.equal(projection.workspaces[0]?.lease?.fencing, 2);
  assert.equal(projection.artifacts[0]?.testEvidenceRefs.length, 1);
  assert.equal(projection.conflicts[0]?.status, 'open');
  assert.equal(projection.merges[0]?.status, 'review_required');
});
