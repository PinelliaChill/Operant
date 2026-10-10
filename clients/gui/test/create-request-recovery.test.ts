import assert from 'node:assert/strict';
import test from 'node:test';
import { confirmedCreateMetadata, createRequestStorageKey, mergeConversationMetadata, renameIsConfirmed, visibleOnboardingError } from '../src/live/createRequestRecovery.ts';

const item = (threadId: string, title: string, revision = 1) => ({ thread_id: threadId, title, title_source: 'manual' as const, revision });

test('unknown create remains unresolved until the original request returns one conversation', () => {
  assert.notEqual(createRequestStorageKey('http://127.0.0.1:3018'), createRequestStorageKey('http://127.0.0.1:3019'));
  assert.equal(confirmedCreateMetadata([]), null);
  assert.equal(confirmedCreateMetadata([item('a', 'A'), item('b', 'B')]), null);
  assert.equal(confirmedCreateMetadata([item('a', 'A')])?.thread_id, 'a');
});

test('a short metadata page preserves prior titles and never replaces a newer revision', () => {
  const merged = mergeConversationMetadata({ old: item('old', '已保存标题', 3), child: item('child', '子对话', 2) }, [item('old', '旧标题', 1), item('new', '新对话', 1)]);
  assert.equal(merged.old.title, '已保存标题');
  assert.equal(merged.child.title, '子对话');
  assert.equal(merged.new.title, '新对话');
  assert.equal(renameIsConfirmed(item('old', '想要的标题'), '想要的标题'), true);
  assert.equal(renameIsConfirmed(item('old', '原标题'), '想要的标题'), false);
});

test('an interleaved title read failure cannot hide an unknown create or rename', () => {
  let writeError = '新建结果尚未确认，请按原请求核对。';
  let titleWarning = '标题读取失败，请刷新。';
  assert.equal(visibleOnboardingError(writeError, titleWarning), writeError);
  titleWarning = ''; // The next successful title read clears only its own warning.
  assert.equal(visibleOnboardingError(writeError, titleWarning), writeError);
  writeError = '改名结果尚未确认，请核对原请求。';
  titleWarning = '标题读取失败，请刷新。';
  assert.equal(visibleOnboardingError(writeError, titleWarning), writeError);
  writeError = '';
  assert.equal(visibleOnboardingError(writeError, titleWarning), titleWarning);
});
