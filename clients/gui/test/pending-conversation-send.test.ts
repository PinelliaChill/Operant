import assert from 'node:assert/strict';
import test from 'node:test';
import { bindPendingConversationSend, draftAfterAccepted, pendingSendMatchesSelection, shouldCancelPendingOnRoute, shouldQueuePendingSend } from '../src/live/pendingConversationSend.ts';

test('pending send captures clicked text, references and workspace', () => {
  const references = [{ ref_type: 'artifact' as const, target_id: 'artifact-old', include_mode: 'metadata' as const }];
  const pending = bindPendingConversationSend({ threadId: 'new-thread', workspaceId: 'workspace-a', workspaceRef: '/workspace/a', sourceConversationId: 'old-thread', text: ' original task ', references, referencesRevision: 4, draftRevision: 3 });
  references[0].target_id = 'artifact-new';
  assert.equal(pending.text, 'original task');
  assert.equal(pending.references[0].target_id, 'artifact-old');
  assert.equal(pending.workspaceId, 'workspace-a');
  assert.equal(pendingSendMatchesSelection(pending, 'new-thread', 'new-thread', '/workspace/a'), true);
  assert.equal(pendingSendMatchesSelection(pending, 'other-thread', 'other-thread', '/workspace/a'), false);
  assert.equal(pendingSendMatchesSelection(pending, 'new-thread', 'new-thread', '/workspace/b'), false);
  assert.equal(shouldCancelPendingOnRoute(pending, '/chat/old-thread', false), false);
  assert.equal(shouldCancelPendingOnRoute(pending, '/chat/other-thread', false), true);
  assert.equal(shouldCancelPendingOnRoute(pending, '/chat/old-thread', true), true);
});

test('new conversation does not queue a draft; accepted send clears only an unchanged draft', () => {
  assert.equal(shouldQueuePendingSend(false, 'task', 'workspace-a'), false);
  assert.equal(shouldQueuePendingSend(true, 'task', 'workspace-a'), true);
  assert.equal(shouldQueuePendingSend(true, '/review', 'workspace-a'), false);
  assert.equal(draftAfterAccepted('task', 2, { text: 'task', draftRevision: 2 }), '');
  assert.equal(draftAfterAccepted('edited task', 3, { text: 'task', draftRevision: 2 }), 'edited task');
  assert.equal(draftAfterAccepted('task', 3, { text: 'task', draftRevision: 2 }), 'task');
});
