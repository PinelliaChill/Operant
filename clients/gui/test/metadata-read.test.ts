import assert from 'node:assert/strict';
import test from 'node:test';
import type { ConversationMetadata } from '../../../sdk/typescript-client/onboarding.generated';
import { readConversationMetadata } from '../src/live/metadataRead.ts';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

const item = (title: string): ConversationMetadata => ({ thread_id: 'thread-1', title, title_source: 'auto', revision: 1 });

test('the first concurrent title read is superseded when the newer read succeeds', async () => {
  const first = deferred<{ items: ConversationMetadata[] }>();
  const second = deferred<{ items: ConversationMetadata[] }>();
  let requested = 0;
  let epoch = 1;
  const client = {
    listConversationMetadata: () => (++requested === 1 ? first.promise : second.promise),
    getConversationMetadata: async () => item('最新标题'),
  };
  const oldRead = readConversationMetadata(client, 'thread-1', () => epoch === 1);
  epoch = 2;
  const newRead = readConversationMetadata(client, 'thread-1', () => epoch === 2);
  second.resolve({ items: [] });
  assert.deepEqual(await newRead, { status: 'applied', items: [item('最新标题')] });
  first.resolve({ items: [item('旧标题')] });
  assert.deepEqual(await oldRead, { status: 'superseded' });
});

test('the first concurrent title read stays superseded when the newer read fails', async () => {
  const first = deferred<{ items: ConversationMetadata[] }>();
  const second = deferred<{ items: ConversationMetadata[] }>();
  let requested = 0;
  let epoch = 1;
  const client = {
    listConversationMetadata: () => (++requested === 1 ? first.promise : second.promise),
    getConversationMetadata: async () => item('标题'),
  };
  const oldRead = readConversationMetadata(client, 'thread-1', () => epoch === 1);
  epoch = 2;
  const newRead = readConversationMetadata(client, 'thread-1', () => epoch === 2);
  const error = new Error('列表读取失败');
  second.reject(error);
  assert.deepEqual(await newRead, { status: 'failed', scope: 'list', error });
  first.resolve({ items: [item('旧标题')] });
  assert.deepEqual(await oldRead, { status: 'superseded' });
});
