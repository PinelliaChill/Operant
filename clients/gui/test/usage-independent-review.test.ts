import assert from 'node:assert/strict';
import test from 'node:test';
import { Phase1EError } from '../../../sdk/typescript-client/phase1e-transport.ts';
import { requestErrorCopy } from '../src/lib/requestErrorCopy.ts';
import { isWriteOutcomeUnknown } from '../src/lib/writeOutcome.ts';
import { ConversationDrafts, draftScope } from '../src/live/conversationDrafts.ts';
import { modelReadiness } from '../src/features/settings/modelReadiness.ts';
import { synchronizeProjectMutation } from '../src/live/projectSynchronization.ts';

function storage() {
  const values = new Map<string, string>();
  return { values, getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value); } };
}

test('draft identities, reopen, late acceptance, and edited first-send migration remain isolated', () => {
  const disk = storage();
  const drafts = new ConversationDrafts(() => disk);
  const origin = 'http://127.0.0.1:3000';
  const a = draftScope(origin, 'project-a', 'thread-a');
  const b = draftScope(origin, 'project-b', 'thread-b');
  const otherCore = draftScope('http://127.0.0.1:3037', 'project-a', 'thread-a');
  const fresh = draftScope(origin, 'project-a', null);
  const freshB = draftScope(origin, 'project-b', null);
  const clicked = drafts.write(a, ' original ');
  drafts.write(b, 'other thread');
  drafts.write(otherCore, 'other origin');
  drafts.write(freshB, 'other new project');
  drafts.write(a, 'edited');
  drafts.accept(clicked);
  assert.equal(drafts.read(a).text, 'edited');
  const sameTextNewRevision = drafts.write(a, clicked.text);
  drafts.accept(clicked);
  assert.equal(drafts.read(a).text, clicked.text);
  drafts.accept(sameTextNewRevision);
  assert.equal(drafts.read(a).text, '');

  const firstClick = drafts.write(fresh, 'first send');
  drafts.write(fresh, 'typed during create');
  const thread = draftScope(origin, 'project-a', 'new-thread');
  const moved = drafts.move(firstClick, thread);
  drafts.accept(moved);
  assert.equal(drafts.read(thread).text, 'typed during create');
  assert.equal(drafts.read(fresh).text, '');
  const reopened = new ConversationDrafts(() => disk);
  assert.equal(reopened.read(thread).text, 'typed during create');
  assert.equal(reopened.read(b).text, 'other thread');
  assert.equal(reopened.read(otherCore).text, 'other origin');
  assert.equal(reopened.read(freshB).text, 'other new project');
});

test('migration never overwrites an existing thread draft and acknowledgement clears only its source', () => {
  const drafts = new ConversationDrafts(() => storage());
  const source = draftScope('https://core.test', 'a', null);
  const target = draftScope('https://core.test', 'a', 'thread');
  const clicked = drafts.write(source, 'sent first message');
  drafts.write(target, 'saved thread work');
  const result = drafts.move(clicked, target);
  drafts.accept(result);
  assert.equal(drafts.read(source).text, '');
  assert.equal(drafts.read(target).text, 'saved thread work');
});

test('damaged and unavailable storage preserve usable memory with visible failure', () => {
  const disk = storage();
  disk.setItem('operant.conversation-drafts.v1', '{bad json');
  const damaged = new ConversationDrafts(() => disk);
  assert.match(damaged.warning, /损坏|读取/);
  const scope = draftScope('https://core.test', 'a', null);
  damaged.write(scope, 'recoverable edit');
  assert.equal(new ConversationDrafts(() => disk).read(scope).text, 'recoverable edit');
  const unavailable = new ConversationDrafts(() => { throw new Error('access denied'); });
  const submitted = unavailable.write(scope, 'in memory');
  assert.equal(unavailable.read(scope).text, 'in memory');
  assert.match(unavailable.warning, /保存失败/);
  unavailable.write(scope, 'new memory');
  unavailable.accept(submitted);
  assert.equal(unavailable.read(scope).text, 'new memory');
});

test('quota and bounded capacity retain earlier disk data while editing continues', () => {
  const disk = storage();
  let denied = false;
  const drafts = new ConversationDrafts(() => ({
    getItem: disk.getItem,
    setItem: (key, value) => { if (denied) throw new Error('QuotaExceededError'); disk.setItem(key, value); },
  }));
  const scope = draftScope('https://core.test', 'a', null);
  drafts.write(scope, 'persisted');
  denied = true;
  drafts.write(scope, 'memory only');
  assert.equal(drafts.read(scope).text, 'memory only');
  assert.equal(new ConversationDrafts(() => disk).read(scope).text, 'persisted');
  assert.match(drafts.warning, /保存失败/);
  denied = false;
  drafts.write(scope, 'x'.repeat(32_001));
  assert.equal(drafts.read(scope).text.length, 32_001);
  assert.match(drafts.warning, /容量/);
  drafts.write(scope, 'shortened');
  assert.equal(drafts.warning, '');
  assert.equal(new ConversationDrafts(() => disk).read(scope).text, 'shortened');
});

test('stored extra fields cannot redirect a captured draft to another thread', () => {
  const disk = storage();
  const a = draftScope('https://core.test', 'a', 'thread-a');
  const b = draftScope('https://core.test', 'b', 'thread-b');
  disk.setItem('operant.conversation-drafts.v1', JSON.stringify({ version: 1, records: {
    [a]: { text: 'draft a', revision: 1, scope: b },
    [b]: { text: 'draft b', revision: 1 },
  } }));
  const drafts = new ConversationDrafts(() => disk);
  assert.equal(drafts.read(a).scope, a);
  const captured = drafts.read(a);
  drafts.accept(captured);
  assert.equal(drafts.read(b).text, 'draft b');
});

test('aggregate and scope limits keep new text in memory without evicting saved drafts', () => {
  for (const [count, text] of [[51, 'small'], [9, 'x'.repeat(32_000)]] as const) {
    const disk = storage();
    const drafts = new ConversationDrafts(() => disk);
    let last = '';
    for (let index = 0; index < count; index += 1) {
      last = draftScope('https://core.test', `project-${index}`, null);
      drafts.write(last, text);
    }
    assert.equal(drafts.read(last).text, text);
    assert.match(drafts.warning, /容量/);
    assert.equal(new ConversationDrafts(() => disk).read(draftScope('https://core.test', 'project-0', null)).text, text);
    drafts.write(last, '');
    if (count === 9) drafts.write(draftScope('https://core.test', 'project-7', null), '');
    assert.equal(drafts.warning, '');
  }
});

test('legacy desktop denial normalization cannot erase an unknown-write recovery marker', async () => {
  const legacy = { error: { code: 'local_caller_required', message: 'raw server message' } };
  const denied = await Phase1EError.fromResponse({ status: 403, json: JSON.stringify(legacy) });
  assert.equal(denied.code, 'local_caller_required');
  assert.match(requestErrorCopy(denied), /桌面/);
  assert.equal(isWriteOutcomeUnknown(denied), false);
  for (const [status, payload] of [
    [500, legacy],
    [403, { ...legacy, detail: { recovery: 'manual_reconcile' } }],
    [403, { error: { ...legacy.error, recovery: 'manual_reconcile' } }],
    [403, { error: { code: 'permission_denied', message: 'denied' } }],
  ] as const) {
    const invalid = await Phase1EError.fromResponse({ status, json: JSON.stringify(payload) });
    assert.equal(invalid.code, 'invalid_error_envelope');
    assert.equal(isWriteOutcomeUnknown(invalid), true);
  }
  const unknown = await Phase1EError.fromResponse({ status: 403, json: JSON.stringify({
    error: { code: 'local_caller_required', message: 'denied', retryable: false, recovery: 'manual_reconcile' }, detail: null,
  }) });
  assert.match(requestErrorCopy(unknown), /勿再次提交/);
  assert.equal(isWriteOutcomeUnknown(unknown), true);
});

test('confirmed project changes synchronize once; failed commands never claim a successful refresh', async () => {
  let reads = 0;
  for (const action of ['project_create', 'project_update', 'project_archive', 'project_detach']) {
    assert.equal(await synchronizeProjectMutation(action, 'succeeded', async () => { reads += 1; return true; }), true);
  }
  assert.equal(reads, 4);
  for (const status of ['failed', 'blocked']) {
    await synchronizeProjectMutation('project_create', status, async () => { reads += 1; return true; });
  }
  await synchronizeProjectMutation('memory_search', 'succeeded', async () => { reads += 1; return true; });
  assert.equal(reads, 4);
  assert.equal(await synchronizeProjectMutation('project_update', 'succeeded', async () => false), false);
});

test('CLI-configured default profiles remain selected without a connection wrapper', () => {
  const setup = { ready: true, default_model_profile_id: 'profile', models: [{ id: 'profile', name: 'CLI Model', model_id: 'exact-model' }] };
  const direct = modelReadiness(setup, [], false);
  assert.equal(direct.profile?.model_id, 'exact-model');
  assert.equal(direct.modelSelected, true);
  assert.equal(direct.ready, true);
  assert.equal(modelReadiness(setup, [], true).ready, false);
  const failed = modelReadiness(setup, [{ connection_id: 'wrapped', profile_ids: ['profile'], status: 'error', error: 'authentication_required' }], false);
  assert.equal(failed.modelSelected, true);
  assert.equal(failed.ready, false);
});
