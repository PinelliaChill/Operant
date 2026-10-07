import assert from 'node:assert/strict';
import test from 'node:test';
import { chooseConversationControlSession, confirmedControlOpen, controlApprovalAction, controlOpenStorageKey, openedControlCanStartConversation, readPendingControlOpen } from '../src/features/extensions/conversationLocalControl.ts';

const computer = 'operant.macos.computer';
const browser = 'operant.chrome.browser';

test('conversation control reuses only a matching active conversation lease', () => {
  const session = { sessionId: 'conversation-control-1', pluginId: computer, computerBundleId: 'com.apple.TextEdit', allowedTargets: ['com.apple.TextEdit'], state: 'active' };
  assert.deepEqual(chooseConversationControlSession(computer, 'com.apple.TextEdit', ['com.apple.TextEdit'], [session], []), { kind: 'reuse', sessionId: session.sessionId });
  assert.equal(chooseConversationControlSession(computer, 'com.apple.Notes', ['com.apple.Notes'], [session], []).kind, 'blocked');
  assert.deepEqual(chooseConversationControlSession(browser, undefined, ['https://example.com'], [], []), { kind: 'open' });
  assert.equal(chooseConversationControlSession(browser, undefined, ['https://new.example'], [{ ...session, pluginId: browser, computerBundleId: undefined, allowedTargets: ['https://example.com'] }], []).kind, 'blocked');
});

test('manual or unsettled control never becomes a conversation lease', () => {
  const manual = { sessionId: 'local-control-1', pluginId: browser, state: 'active' };
  assert.equal(chooseConversationControlSession(browser, undefined, ['https://example.com'], [], [manual]).kind, 'blocked');
  assert.equal(chooseConversationControlSession(browser, undefined, ['https://example.com'], [{ ...manual, state: 'failed' }], []).kind, 'blocked');
});

test('unknown open retains only an origin-scoped request key and target', () => {
  assert.notEqual(controlOpenStorageKey('http://127.0.0.1:3018'), controlOpenStorageKey('http://127.0.0.1:3019'));
  assert.deepEqual(readPendingControlOpen(JSON.stringify({ key: 'same-key', pluginId: computer, computerBundleId: 'com.apple.TextEdit', expectedTargets: ['com.apple.TextEdit'], approvalId: 'approval-1', decisionSubmitted: true })), { key: 'same-key', pluginId: computer, computerBundleId: 'com.apple.TextEdit', expectedTargets: ['com.apple.TextEdit'], approvalId: 'approval-1', decisionSubmitted: true });
  assert.equal(readPendingControlOpen('{broken'), null);
});

test('read-only reconciliation accepts only the original active scope', () => {
  const pending = { key: 'original-key', pluginId: computer, computerBundleId: 'com.apple.TextEdit', expectedTargets: ['com.apple.TextEdit'] };
  const matched = { sessionId: 'new-session', pluginId: computer, computerBundleId: 'com.apple.TextEdit', allowedTargets: ['com.apple.TextEdit'], state: 'active' };
  assert.equal(confirmedControlOpen([], pending), null);
  assert.equal(confirmedControlOpen([{ ...matched, computerBundleId: 'com.apple.Notes' }], pending), null);
  assert.equal(confirmedControlOpen([{ ...matched, allowedTargets: ['com.apple.Notes'] }], pending), null);
  assert.equal(confirmedControlOpen([{ ...matched, state: 'closed' }], pending)?.state, 'closed');
  assert.equal(confirmedControlOpen([matched, matched], pending), null);
  assert.equal(confirmedControlOpen([matched], pending)?.sessionId, matched.sessionId);
  assert.equal(openedControlCanStartConversation(matched, pending), true);
  assert.equal(openedControlCanStartConversation({ ...matched, allowedTargets: ['com.apple.Notes'] }, pending), false);
  assert.equal(openedControlCanStartConversation({ ...matched, state: 'closed' }, pending), false);
});

test('an uncertain approval decision cannot be submitted again from the UI', () => {
  assert.equal(controlApprovalAction('pending', false), 'decide');
  assert.equal(controlApprovalAction('pending', true), 'read_only');
  assert.equal(controlApprovalAction('approved', true), 'continue');
  assert.equal(controlApprovalAction('consumed', true), 'read_only');
});
