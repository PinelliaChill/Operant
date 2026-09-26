import assert from 'node:assert/strict';
import test from 'node:test';
import { graphDifference, hasOutputPort, newNode, nodePosition } from '../src/features/collab/canvas/live-graph-model.ts';

test('new live nodes use Phase23 ports and required kind fields', () => {
  const condition = newNode('condition', 0);
  assert.deepEqual(condition.output_ports?.map((port) => port.name), ['true', 'false']);
  assert.deepEqual(condition.input_ports?.map((port) => port.name), ['input']);
  const loop = newNode('loop', 1);
  assert.equal(loop.loop_policy?.max_iterations, 3);
  assert.equal(loop.loop_policy?.on_limit_node_id, loop.node_id);
  const timer = newNode('timer', 2);
  assert.equal(timer.metadata?.delay_seconds, 0);
});

test('definition difference keeps server baseline separate from local edits', () => {
  const first = newNode('agent', 0);
  const second = newNode('tool', 1);
  const edge = { edge_id: 'edge-1', source_node: first.node_id, source_port: 'output', target_node: second.node_id, target_port: 'input' };
  const baseline = { workflow_id: 'flow', version: 2, name: 'Flow', status: 'draft' as const, created_at: '2026-09-26T00:00:00Z', nodes: [first, second], edges: [edge] };
  const moved = { ...first, metadata: { ...first.metadata, canvas_position: { x: 400, y: 230 } } };
  assert.deepEqual(nodePosition(moved, 0), { x: 400, y: 230 });
  const difference = graphDifference(baseline, [moved], []);
  assert.deepEqual(difference.added, []);
  assert.deepEqual(difference.removed, [second.node_id]);
  assert.deepEqual(difference.changed, [first.node_id]);
  assert.deepEqual(difference.edgeRemoved, ['edge-1']);
  assert.deepEqual(difference.edgeAdded, []);
});

test('definition difference ignores JSON object key order', () => {
  const node = newNode('agent', 0);
  const baseline = { workflow_id: 'flow', version: 2, name: 'Flow', status: 'draft' as const, created_at: '2026-09-26T00:00:00Z', nodes: [node], edges: [] };
  const reordered = { ...node, metadata: { ...node.metadata } };
  const reverseKeys = (value: Record<string, unknown>): Record<string, unknown> =>
    Object.fromEntries(Object.entries(value).reverse());
  const sameNode = reverseKeys(reordered as Record<string, unknown>);
  sameNode.metadata = reverseKeys(reordered.metadata || {});
  assert.deepEqual(graphDifference(baseline, [sameNode as typeof node], []), {
    added: [], removed: [], changed: [], edgeAdded: [], edgeRemoved: [],
  });
});

test('removed connection source cannot create an edge', () => {
  const source = newNode('agent', 0);
  const target = newNode('tool', 1);
  const selected = { node: source.node_id, port: 'output' };
  assert.equal(hasOutputPort([source, target], selected), true);
  assert.equal(hasOutputPort([target], selected), false);
  assert.equal(hasOutputPort([{ ...source, output_ports: [] }, target], selected), false);
});
