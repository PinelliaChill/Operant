import assert from 'node:assert/strict';
import test from 'node:test';
import {
  commandResourceId,
  currentRoster,
  normalizeCollaborationDirectory,
  roleLabel,
  teamLabel,
  workflowKey,
} from '../src/features/collab/b24-client.ts';

const workflow = {
  workflow_id: 'workflow-code-review',
  version: 2,
  name: '代码评审',
  description: '检查变更并给出可复现证据。',
  nodes: [],
  status: 'published' as const,
};

test('B2-4 directory validation preserves Core IDs without inventing entries', () => {
  const directory = normalizeCollaborationDirectory({
    workflows: [workflow],
    teams: [{
      team_id: 'team-review',
      version: 1,
      members: [{ member_id: 'reviewer', agent_definition_id: 'role-reviewer', role: '评审员' }],
      default_coordinator: 'reviewer',
    }],
    roles: [{ id: 'role-reviewer', name: '评审员', model_profile_id: 'profile-1', model_id: 'gpt-test', effort: 'medium' }],
  });

  assert.equal(directory.workflows[0]?.workflow_id, 'workflow-code-review');
  assert.equal(workflowKey(directory.workflows[0]!), 'workflow-code-review:v2');
  assert.equal(teamLabel(directory.teams[0]!), '评审员 · 1 名成员 · v1');
  assert.equal(roleLabel(directory.roles[0]!), '评审员 · gpt-test');
});

test('B2-4 directory validation rejects incomplete Core projections', () => {
  assert.throws(
    () => normalizeCollaborationDirectory({ workflows: [workflow], teams: [] }),
    /协作目录 缺少 roles 列表/,
  );
  assert.throws(
    () => normalizeCollaborationDirectory({
      workflows: [{ ...workflow, workflow_id: '' }],
      teams: [],
      roles: [],
    }),
    /Workflow Definition 缺少 workflow_id/,
  );
});

test('B2-4 command result requires a completed Core resource', () => {
  assert.equal(commandResourceId({ status: 'completed', resource_id: 'team-run-1', resource_type: 'team_run', directory: { workflows: [], teams: [], roles: [] } }, 'Team'), 'team-run-1');
  assert.throws(() => commandResourceId({ status: undefined, resource_id: 'team-run-1', resource_type: 'team_run', directory: { workflows: [], teams: [], roles: [] } }, 'Team'), /未返回可用资源/);
});


test('current roster selects retry Agent without deleting historical rows', () => {
  const old = {member_id: 'worker', joined_at: '2026-01-01T00:00:00Z', agent: 'old'};
  const next = {member_id: 'worker', joined_at: '2026-01-01T00:01:00Z', agent: 'next'};
  const other = {member_id: 'reviewer', joined_at: null, agent: 'reviewer'};
  const rows = [next, other, old];
  assert.deepEqual(currentRoster(rows), [next, other]);
  assert.equal(rows.length, 3);
});
