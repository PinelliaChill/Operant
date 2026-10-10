import assert from 'node:assert/strict';
import test from 'node:test';
import { deepLinkLookupDecision } from '../src/live/deepLinkLookup.ts';

test('uncached deep links require one formal refresh before a not-found conclusion', () => {
  assert.equal(deepLinkLookupDecision('thread-new', false, true, null), 'refresh');
  assert.equal(deepLinkLookupDecision('thread-new', false, true, 'thread-new'), 'await_result');
  assert.equal(deepLinkLookupDecision('thread-new', true, true, 'thread-new'), 'found');
  assert.equal(deepLinkLookupDecision('thread-new', false, false, null), 'disconnected');
  assert.equal(deepLinkLookupDecision(null, false, true, null), 'empty');
});
