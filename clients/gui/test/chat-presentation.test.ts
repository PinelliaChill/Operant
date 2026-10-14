import test from 'node:test';
import assert from 'node:assert/strict';
import { approvalActionLabel, chatEmptyPresentation, historyItemLabel } from '../src/features/chat/chatPresentation.ts';

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

test('approval titles identify only known tool actions, never hidden arguments or plugin IDs', () => {
  assert.equal(approvalActionLabel('ext_browser_observe category=security_policy; plugin=browser; argument values hidden', 'security_policy'), '查看网页');
  assert.equal(approvalActionLabel('ext_browser_click category=security_policy; plugin=browser; argument values hidden', 'security_policy'), '点击网页按钮');
  assert.equal(approvalActionLabel('ext_computer_type_text category=security_policy; plugin=computer; argument values hidden', 'security_policy'), '输入文本');
  assert.equal(approvalActionLabel('run_command category=network; executable=curl; argument_count=1', 'network'), '运行命令');
  assert.equal(approvalActionLabel('future_tool category=security_policy; plugin=ext_browser_click', 'security_policy'), '执行受保护操作');
  assert.equal(approvalActionLabel('ext_browser_clicker category=security_policy', 'security_policy'), '执行受保护操作');
});
