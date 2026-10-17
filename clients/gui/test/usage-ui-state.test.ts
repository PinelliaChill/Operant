import { encodeCreateRequest, decodeCreateRequest } from '../src/live/createRequestRecovery.ts';
import assert from 'node:assert/strict';
import test from 'node:test';
import { projectWorkspaceMetadata } from '../src/live/projectDisplayNames.ts';
import { modelReadiness } from '../src/features/settings/modelReadiness.ts';
import { ConversationDrafts, draftScope, draftThreadFromPath, draftThreadForSelection, isNewConversationDraft, restoreConfirmedConversationDraft } from '../src/live/conversationDrafts.ts';
import { synchronizeProjectMutation, projectSelectionStorageKey, readProjectSelection } from '../src/live/projectSynchronization.ts';

const setup = { ready: true, default_model_profile_id: 'profile-a', models: [{ id: 'profile-a', name: 'Current model', model_id: 'actual-model' }] };
const connection = { connection_id: 'service-a', status: 'ready', profile_ids: ['profile-a'] };
test('model readiness respects the authoritative profile and existing CLI configuration', () => {
  assert.equal(modelReadiness(setup, [], false).ready, true);
  assert.equal(modelReadiness(setup, [], false).modelSelected, true);
  assert.equal(modelReadiness(setup, [connection], false).ready, true);
  assert.equal(modelReadiness(setup, [{ ...connection, status: 'error' }], false).ready, false);
  assert.equal(modelReadiness(setup, [{ ...connection, error: 'rate_limited' }], false).ready, false);
  assert.equal(modelReadiness(setup, [connection], true).ready, false);
  assert.equal(modelReadiness({ ...setup, ready: false }, [connection], false).ready, false);
  assert.equal(modelReadiness({ ...setup, models: [] }, [connection], false).ready, false);
  assert.equal(modelReadiness(null, [connection], false).ready, false);
});

test('all project mutations await a fresh projection and preserve read failure', async () => {
  for (const action of ['project_create', 'project_update', 'project_archive', 'project_detach']) {
    const calls: string[] = [];
    let finish!: (result: boolean) => void;
    const work = synchronizeProjectMutation(action, 'completed', async () => { calls.push('read'); return new Promise<boolean>((resolve) => { finish = resolve; }); });
    assert.deepEqual(calls, ['read']);
    finish(true);
    assert.equal(await work, true);
    assert.equal(await synchronizeProjectMutation(action, 'completed', async () => false), false);
  }
  let reads = 0;
  for (const [action, status] of [['project_create', 'blocked'], ['project_update', 'failed'], ['memory_save', 'completed']]) {
    await synchronizeProjectMutation(action, status, async () => { ++reads; return true; });
  }
  assert.equal(reads, 0);
});

test('a selected new-project draft restores after reopening while other origins remain isolated', () => {
  const values = new Map<string, string>();
  const storage = { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value); } };
  const origin = 'https://core.test';
  const store = new ConversationDrafts(() => storage);
  store.write(draftScope(origin, 'project-a', null), 'A');
  store.write(draftScope(origin, 'project-b', null), 'B');
  storage.setItem(projectSelectionStorageKey(origin), 'project-b');
  const restored = new ConversationDrafts(() => storage);
  assert.equal(restored.read(draftScope(origin, readProjectSelection(storage, origin) || '', null)).text, 'B');
  assert.equal(readProjectSelection(storage, 'https://other-core.test'), null);
  assert.equal(readProjectSelection({ getItem: () => { throw new Error('blocked'); } }, origin), null);
});

test('malformed links and persisted extra fields cannot change the addressed draft scope', () => {
  assert.equal(draftThreadFromPath('/chat/a%2Fb'), 'a/b');
  assert.equal(draftThreadFromPath('/chat/%E0%A4%A'), '%E0%A4%A');
  assert.equal(draftThreadFromPath('/settings'), null);
  const scope = draftScope('https://core.test', 'a', 'thread-a');
  const raw = JSON.stringify({ version: 1, records: { [scope]: { text: 'A', revision: 2, scope: 'other-thread', api_key: 'untrusted extra data' } } });
  const store = new ConversationDrafts(() => ({ getItem: () => raw, setItem: () => {} }));
  assert.deepEqual(store.read(scope), { scope, text: 'A', revision: 2 });
});

test('archived management lifecycle remains visible without revoking registered workspace reads', () => {
  assert.deepEqual(projectWorkspaceMetadata([
    { workspace_id: 'old', name: 'Archived project', archived: true },
    { workspace_id: 'active', name: 'Active project', archived: false },
    { workspace_id: 'reused', name: 'Old binding', archived: true },
    { workspace_id: 'reused', name: 'Current binding', archived: false },
  ]), { names: { old: 'Archived project（已归档）', active: 'Active project', reused: 'Current binding' }, archivedWorkspaceIds: ['old'] });
});

test('root chat retains the selected thread draft; only an empty welcome scope migrates', () => {
  const origin = 'https://core.test';
  assert.equal(draftThreadForSelection('/chat', 'existing'), 'existing');
  assert.equal(draftThreadForSelection('/chat/explicit', 'existing'), 'explicit');
  assert.equal(draftThreadForSelection('/chat', null), null);
  assert.equal(isNewConversationDraft({ scope: draftScope(origin, 'project', draftThreadForSelection('/chat', 'existing')), text: 'old work', revision: 1 }), false);
  assert.equal(isNewConversationDraft({ scope: draftScope(origin, 'project', draftThreadForSelection('/chat', null)), text: 'first send', revision: 1 }), true);
});

test('unknown create reopening restores only the referenced source draft after exact confirmation, without sending', () => {
  const values = new Map<string, string>();
  const storage = { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value); } };
  const origin = 'https://core.test';
  const source = draftScope(origin, 'project-a', null);
  const other = draftScope(origin, 'project-b', null);
  const target = draftScope(origin, 'project-a', 'confirmed-thread');
  const store = new ConversationDrafts(() => storage);
  store.write(source, 'original user draft');
  store.write(other, 'other project');
  const encoded = encodeCreateRequest({ requestId: 'original-request', draftScope: source });
  assert.equal(encoded.includes('original user draft'), false);
  const ticket = decodeCreateRequest(encoded, origin)!;
  const reopened = new ConversationDrafts(() => storage);
  assert.equal(restoreConfirmedConversationDraft(reopened, ticket.draftScope, target), '');
  assert.equal(reopened.read(target).text, 'original user draft');
  assert.equal(reopened.read(source).text, '');
  assert.equal(reopened.read(other).text, 'other project');
  assert.equal(new ConversationDrafts(() => storage).read(target).text, 'original user draft');
  assert.equal(decodeCreateRequest('legacy-request', origin)?.requestId, 'legacy-request');
  assert.equal(decodeCreateRequest(encoded, 'https://other.test')?.draftScope, undefined);
});
test('confirmed create recovery exposes a conflicting source without overwriting either draft', () => {
  const values = new Map<string, string>();
  const storage = { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value); } };
  const store = new ConversationDrafts(() => storage);
  const source = draftScope('https://core.test', 'project-a', null);
  const target = draftScope('https://core.test', 'project-a', 'confirmed-thread');
  store.write(source, 'source retained'); store.write(target, 'target retained');
  assert.equal(restoreConfirmedConversationDraft(store, source, target), 'source retained');
  assert.equal(store.read(source).text, 'source retained');
  assert.equal(store.read(target).text, 'target retained');
  store.write(target, '');
  assert.equal(restoreConfirmedConversationDraft(store, source, target), '');
  assert.equal(store.read(target).text, 'source retained');
});
