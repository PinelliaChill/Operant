import assert from 'node:assert/strict';
import test from 'node:test';
import {
  mapGraphDraft,
  mapGraphRun,
  mapMailbox,
  mapNodeRuns,
  mapRuntimeFrame,
  mapTeamMessages,
  mapTeamRun,
} from '../src/live23/phase23Adapter.ts';

test('Phase 23 adapter accepts generated snake case projections without inventing ids', () => {
  assert.deepEqual(mapGraphDraft({
    draft_id: 'draft-1', name: 'Coding', workspace_ref: '/work', nodes: [], edges: [],
  }), {
    id: 'draft-1', name: 'Coding', workspaceRef: '/work', description: undefined, nodes: [], edges: [],
  });
  assert.throws(() => mapGraphDraft({ name: 'missing id' }), /missing draft_id\/id/);
  assert.equal(mapGraphRun({ run_id: 'run-1', definition_id: 'def-1', status: 'manual_reconcile_required' }).manualReconcileRequired, true);
});

test('Phase 23 adapter maps node and team projections', () => {
  assert.equal(mapNodeRuns({ node_runs: [{ node_run_id: 'nr-1', node_id: 'coder', status: 'running' }] })[0].nodeId, 'coder');
  assert.deepEqual(mapTeamRun({ team_run_id: 'tr-1', team_definition_id: 'td-1', status: 'running', roster: [] }), {
    id: 'tr-1', definitionId: 'td-1', status: 'running', roster: [],
  });
  assert.equal(mapTeamMessages({ messages: [{ message_id: 'm-1', sender_id: 'a', recipient_ids: ['b'], body: 'hi' }] })[0].body, 'hi');
  assert.equal(mapMailbox({ deliveries: [{ delivery_id: 'd-1', message_id: 'm-1', recipient_id: 'b', status: 'pending' }] })[0].status, 'pending');
});

test('Phase 23 event mapping retains lossless cursor and scope', () => {
  const event = mapRuntimeFrame({
    id: '9007199254740993',
    event: 'node.updated',
    resource_scope: 'graph_run:run-1',
    data: { id: 'event-1', payload: { node_id: 'coder' } },
  }, 'fallback');
  assert.equal(event.cursor, 9007199254740993n);
  assert.equal(event.resourceScope, 'graph_run:run-1');
});
