import assert from 'node:assert/strict';
import test from 'node:test';
import { isWriteOutcomeUnknown } from '../src/lib/writeOutcome.ts';

test('a 503 after a committed write keeps the original request blocked for read-only reconciliation', () => {
  const requestKey = 'original-create-key';
  const lostResponse = { code: 'http_503', recovery: 'retry_later', message: 'upstream unavailable' };
  const pendingKey = isWriteOutcomeUnknown(lostResponse) ? requestKey : null;
  assert.equal(pendingKey, requestKey);
  assert.equal(isWriteOutcomeUnknown({ code: 'http_504', recovery: 'retry_later' }), true);
  assert.equal(isWriteOutcomeUnknown({ code: 'http_502', recovery: 'none' }), true);
  assert.equal(isWriteOutcomeUnknown({ code: 'http_408', recovery: 'none' }), true);
  assert.equal(isWriteOutcomeUnknown({ code: 'transport_unavailable', recovery: 'none' }), true);
});

test('confirmed validation and permission rejections may release the write key', () => {
  for (const code of ['http_400', 'http_401', 'http_403', 'http_409', 'http_422', 'validation_error', 'permission_denied']) {
    assert.equal(isWriteOutcomeUnknown({ code, recovery: 'none' }), false, code);
  }
  assert.equal(isWriteOutcomeUnknown(new Error('network failed')), true);
});
