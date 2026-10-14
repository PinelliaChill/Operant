import assert from 'node:assert/strict';
import test from 'node:test';
import type { WorkflowDefinition } from '../../../sdk/typescript-client/phase23.generated.ts';
import { graphNodeDisplayName } from '../src/features/collab/graphNodeName.ts';

const definition = (name: string, nodeId: string, metadata: Record<string, unknown>): WorkflowDefinition => ({
  workflow_id: 'workflow-1', version: 1, name, status: 'published', created_at: '2026-01-01T00:00:00Z',
  nodes: [{ node_id: nodeId, node_kind: 'agent', metadata }],
});

test('official basic team role metadata gives clear member names', () => {
  assert.equal(graphNodeDisplayName(definition('基础协作团队', 'planner', { role_id: 'role_planner', display_name: 'planner' }), 'planner'), '规划');
  assert.equal(graphNodeDisplayName(definition('基础协作团队', 'coder', { role_id: 'role_coder' }), 'coder'), '编程');
  assert.equal(graphNodeDisplayName(definition('基础协作团队', 'reviewer', { role_id: 'role_reviewer' }), 'reviewer'), '审查');
});

test('arbitrary Graph nodes use definition names or retain their exact identity', () => {
  assert.equal(graphNodeDisplayName(definition('其他流程', 'planner', { role_id: 'role_planner' }), 'planner'), 'planner');
  assert.equal(graphNodeDisplayName(definition('其他流程', 'check', { display_name: '核对结果' }), 'check'), '核对结果');
  assert.equal(graphNodeDisplayName(null, 'unknown-node'), 'unknown-node');
});
