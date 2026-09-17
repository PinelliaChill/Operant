import test from 'node:test';
import assert from 'node:assert/strict';
import { buildExperienceView } from '../src/features/management/b26-view.ts';
import type { B26State } from '../../../sdk/typescript-client/b2_6.generated.ts';
import type { GovernanceState } from '../../../sdk/typescript-client/b2_5.generated.ts';
import type { ManagementState } from '../../../sdk/typescript-client/b2_3.generated.ts';

function fixture() {
  const ref = { dataset_id: 'dataset-a', record_id: 'record-a', version: 2, content_digest: 'a'.repeat(64) };
  const scope = { kind: 'workspace', project_id: 'project-a', workspace_id: 'workspace-a' };
  const state = {
    project_id: 'project-a', unresolved_command_ids: [], datasets: [],
    skills: { project_id: 'project-a', skills: [{ skill_id: 'skill-a', version: { skill_id: 'skill-a', version: 3, name: 'check', description: '检查', procedure_ref: ref, role_ids: [], agent_ids: [], artifact: { content_hash: 'b'.repeat(64) }, trust_status: 'validated' }, head: { published_version: 1, state: 'published', head_revision: 4, permission_epoch: 6 }, dependency_state: 'active', rollback_versions: [1], validation: { status: 'passed' } }] },
    sharing: { project_id: 'project-a', grants: [], worktrees: [], writer_evidence: [], transfers: [] },
    remote: { project_id: 'project-a', action: 'state', status: 'ready', message: '', packs: [], permission_epoch: 6 },
  } as unknown as B26State;
  const governance = { project_id: 'project-a', records: [{ currently_usable: true, version: { ref, scope, content: '精确步骤', content_type: 'procedure', owner: { principal_id: 'user' } } }] } as unknown as GovernanceState;
  const management = { projects: [{ project_id: 'project-a', name: 'A', workspace_id: 'workspace-a' }], installations: [] } as unknown as ManagementState;
  return { state, governance, management, ref };
}

test('publish is bound to the displayed draft and head; newer projection invalidates the old action id', () => {
  const f = fixture();
  const old = buildExperienceView(f.state, f.governance, f.management, 1);
  const action = old.sections[0].actions.find(a => a.id.includes('skill_publish:'))!;
  assert.deepEqual(old.commands.get(action.id)!({}), { action: 'skill_publish', project_id: 'project-a', skill_id: 'skill-a', skill_version: 3, expected_head_revision: 4, permission_epoch: 6 });
  const next = buildExperienceView(f.state, f.governance, f.management, 2);
  assert.equal(next.commands.has(action.id), false);
});

test('procedure source selection rejects an unshown or changed version', () => {
  const f = fixture(), view = buildExperienceView(f.state, f.governance, f.management, 1);
  const action = view.sections[0].actions.find(a => a.id.endsWith(':procedure_propose'))!;
  assert.throws(() => view.commands.get(action.id)!({ source: 'invisible-ref', content: 'test' }), /来源不在/);
  const selected = action.fields[0].options![0].value;
  const command = view.commands.get(action.id)!({ source: selected, content: '按顺序检查' });
  assert.equal(command.action, 'procedure_propose');
  if (command.action === 'procedure_propose') {
    assert.equal(command.sources![0].revision, f.ref.version);
    assert.equal(command.sources![0].content_digest, f.ref.content_digest);
    assert.equal(command.sources![0].permission_epoch, 6);
  }
});

test('source revocation removes it from all new source selectors', () => {
  const f = fixture();
  f.governance.records[0].currently_usable = false;
  const view = buildExperienceView(f.state, f.governance, f.management, 1);
  const create = view.sections[3].actions.find(a => a.id.endsWith(':remote_pack_create'))!;
  assert.equal(create.disabled, true);
  assert.equal(create.fields[0].options!.length, 0);
});

test('draft lifecycle actions are disabled before a command is prepared', () => {
  const f = fixture(), skill = f.state.skills.skills![0];
  skill.head.published_version = null;
  skill.head.state = 'draft';
  const view = buildExperienceView(f.state, f.governance, f.management, 1);
  for (const action of ['skill_disable:', 'skill_rollback:']) {
    const item = view.sections[0].actions.find(a => a.id.includes(action))!;
    assert.equal(item.disabled, true);
    assert.throws(() => view.commands.get(item.id)!({ version: '1' }));
  }
});

test('personal preference grants preserve Core scope and exact version', () => {
  const f = fixture();
  const personal = { ...f.governance.records[0].version,
    ref: { ...f.ref, record_id: 'preference-a' }, sources: [], content_type: 'preference' as const,
    scope: { kind: 'personal' as const, principal_id: 'user', opt_in_grant_id: 'optin-a' },
  };
  f.state.sharing.personal_preferences = [personal];
  const view = buildExperienceView(f.state, f.governance, f.management, 1);
  const action = view.sections[2].actions.find(a => a.id.endsWith(':grant_create'))!;
  const option = action.fields[0].options!.find(o => o.label.startsWith('个人偏好'))!;
  const result = view.commands.get(action.id)!({ source: option.value, target_project: 'project-a', subject: 'role-a', purpose: 'recall', hours: '1' });
  assert.equal(result.action, 'grant_create');
  if (result.action === 'grant_create') {
    assert.deepEqual(result.grant!.source_scope, personal.scope);
    assert.deepEqual(result.grant!.memory_refs, [personal.ref]);
  }
  const procedure = view.sections[0].actions.find(a => a.id.endsWith(':procedure_propose'))!;
  assert.equal(procedure.fields[0].options!.some(o => o.value === option.value), false);
});
