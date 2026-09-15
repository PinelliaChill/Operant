import assert from 'node:assert/strict';
import test from 'node:test';
import {
  commandResourceId,
  currentRoster,
  filterGraphRunsByWorkspace,
  normalizeCollaborationDirectory,
  mergeGraphRunPages,
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
    graph_runs: [],
    graph_runs_has_more: false, graph_runs_next_cursor: null,
  });

  assert.equal(directory.workflows[0]?.workflow_id, 'workflow-code-review');
  assert.equal(workflowKey(directory.workflows[0]!), 'workflow-code-review:v2');
  assert.equal(teamLabel(directory.teams[0]!), '评审员 · 1 名成员 · v1');
  assert.equal(roleLabel(directory.roles[0]!), '评审员 · gpt-test');
});

test('fresh Core directory discovers native Graph and its Team without a legacy run', () => {
  const run = {
    id: 'native-graph', workflow_definition_id: workflow.workflow_id,
    workflow_definition_version: 2, workspace_or_target: '/work/current',
    team_run_id: 'team-run-native', status: 'completed', updated_at: '2026-09-15T01:00:00Z',
  };
  const directory = normalizeCollaborationDirectory({
    workflows: [workflow], roles: [], teams: [], graph_runs_has_more: true, graph_runs_next_cursor: "page-2",
    graph_runs: [run, { ...run, id: 'other-graph', workspace_or_target: '/work/other' }],
  });
  assert.deepEqual(filterGraphRunsByWorkspace(directory.graph_runs, '/work/current').map((item) => [item.id, item.team_run_id]), [['native-graph', 'team-run-native']]);
  assert.equal(directory.graph_runs_has_more, true);
  assert.equal(directory.graph_runs.length, 2);
  assert.deepEqual(filterGraphRunsByWorkspace(directory.graph_runs, ''), []);
  assert.deepEqual(filterGraphRunsByWorkspace(directory.graph_runs, '/work/missing'), []);
});

test('run directory rejects old or malformed projections instead of hiding a discovery error', () => {
  const base = { workflows: [], roles: [], teams: [] };
  assert.throws(() => normalizeCollaborationDirectory(base), /graph_runs/);
  assert.throws(() => normalizeCollaborationDirectory({ ...base, graph_runs: [] }), /graph_runs_has_more/);
  assert.throws(() => normalizeCollaborationDirectory({
    ...base, graph_runs_has_more: false,
    graph_runs: [{ id: 'bad', workflow_definition_id: 'workflow', workflow_definition_version: 1,
      workspace_or_target: '/work/current', team_run_id: null, status: 'made-up', updated_at: '2026-09-15T01:00:00Z' }],
  }), /status/);
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

test('Graph pages merge without losing earlier runs or duplicating updated summaries', () => {
  const run = { id: 'older', workflow_definition_id: 'w', workflow_definition_version: 1, workspace_or_target: '/w', team_run_id: null, status: 'completed' as const, updated_at: '2026-09-14T00:00:00Z' };
  const newer = { ...run, id: 'newer', updated_at: '2026-09-15T00:00:00Z' };
  const pages = mergeGraphRunPages([newer], [run, { ...newer, team_run_id: 'team' }]);
  assert.deepEqual(pages.map((r) => r.id), ['newer', 'older']);
  assert.equal(pages[0].team_run_id, 'team');
  assert.throws(() => normalizeCollaborationDirectory({ workflows: [], roles: [], teams: [], graph_runs: [], graph_runs_has_more: true, graph_runs_next_cursor: null }), /翻页状态/);
});
