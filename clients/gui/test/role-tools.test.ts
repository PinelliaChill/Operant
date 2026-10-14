import assert from 'node:assert/strict';
import test from 'node:test';
import { policyWithRoleTools, selectedRoleTools } from '../src/features/agents/roleTools.ts';

test('new role grants only explicitly selected workbench tools', () => {
  assert.deepEqual(policyWithRoleTools(undefined, []), {
    workspace_write: false,
    command_execution: false,
    allowed_tools: [],
  });
  assert.deepEqual(policyWithRoleTools(undefined, ['read_file', 'delegate_agent']).allowed_tools,
    ['read_file', 'delegate_agent']);
});

test('role edit preserves unrelated tools and every other policy field', () => {
  const existing = {
    allowed_tools: ['run_command', 'read_file', 'apply_patch', 'send_agent_message'],
    workspace_write: true,
    command_execution: true,
    approval_required: ['shell', 'network'],
    command_execution_policy: { runner: 'docker' as const, docker_image: 'safe-image' },
  };
  assert.deepEqual(selectedRoleTools(existing), ['read_file', 'send_agent_message']);
  const changed = policyWithRoleTools(existing, ['search_files', 'wait_for_agent']);
  assert.deepEqual(changed.allowed_tools, ['run_command', 'apply_patch', 'search_files', 'wait_for_agent']);
  assert.equal(changed.workspace_write, true);
  assert.equal(changed.command_execution, true);
  assert.deepEqual(changed.approval_required, existing.approval_required);
  assert.deepEqual(changed.command_execution_policy, existing.command_execution_policy);
  assert.deepEqual(existing.allowed_tools, ['run_command', 'read_file', 'apply_patch', 'send_agent_message']);
});
