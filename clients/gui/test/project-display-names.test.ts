import assert from 'node:assert/strict';
import test from 'node:test';
import { projectDisplayNames, readProjectDisplayNames } from '../src/live/projectDisplayNames.ts';

test('only a unique active management project names the matching workspace', () => {
  const projects = [
    { project_id: 'managed-personal', workspace_id: 'workspace-personal', name: '个人工作区', archived: false },
    { project_id: 'managed-old', workspace_id: 'workspace-old', name: '旧项目', archived: true },
    { project_id: 'managed-a', workspace_id: 'workspace-shared', name: '甲项目', archived: false },
    { project_id: 'managed-b', workspace_id: 'workspace-shared', name: '乙项目', archived: false },
  ];
  assert.deepEqual(projectDisplayNames(projects), { 'workspace-personal': '个人工作区' });
});

test('name refresh negotiates and reads the formal management projection', async () => {
  const calls: string[] = [];
  const names = await readProjectDisplayNames({
    connect: async () => { calls.push('connect'); },
    getManagement: async () => { calls.push('read'); return { projects: [{ workspace_id: 'workspace-personal', name: '个人工作区', archived: false }] }; },
  });
  assert.deepEqual(calls, ['connect', 'read']);
  assert.deepEqual(names, { 'workspace-personal': '个人工作区' });
  await assert.rejects(readProjectDisplayNames({
    connect: async () => { throw new Error('protocol mismatch'); },
    getManagement: async () => { throw new Error('should not read'); },
  }), /protocol mismatch/);
});
