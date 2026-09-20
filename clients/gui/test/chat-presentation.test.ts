import test from 'node:test';
import assert from 'node:assert/strict';
import { chatEmptyPresentation, historyItemLabel } from '../src/features/chat/chatPresentation.ts';

const ready = { failed: false, deepLinkNotFound: false, hasProjects: true, hasProject: true };
test('a connection failure never offers a creation action even with a project', () => {
  assert.equal(chatEmptyPresentation({ ...ready, failed: true }).action, 'reconnect');
});
test('an unresolved deep link never masquerades as a new conversation', () => {
  assert.equal(chatEmptyPresentation({ ...ready, deepLinkNotFound: true }).action, 'none');
});
test('no accessible project leads to project management, not a fabricated workspace', () => {
  assert.equal(chatEmptyPresentation({ ...ready, hasProjects: false, hasProject: false }).action, 'projects');
});
test('explicit project selection precedes conversation creation', () => {
  assert.equal(chatEmptyPresentation({ ...ready, hasProject: false }).action, 'select-project');
  assert.equal(chatEmptyPresentation(ready).action, 'create-thread');
});
test('history presentation distinguishes tool activity from assistant text and preserves unknown types', () => {
  assert.notEqual(historyItemLabel('tool_call'), historyItemLabel('agent_message'));
  assert.equal(historyItemLabel('future_event'), 'future_event');
});
