import assert from 'node:assert/strict';
import test from 'node:test';
import { decodeModelRequest, encodeModelRequest, modelRequestIsConfirmed, modelRequestResultCopy, modelRequestStorageKey } from '../src/features/settings/modelRequestRecovery.ts';

const request = { requestId: 'original-key', kind: 'api_key' as const };
const connection = { connection_id: 'connection-1', name: 'API', provider: 'openai-compatible' as const, auth_method: 'api_key' as const, status: 'connected' as const, base_url: 'https://example.test', model_ids: [], model_names: {}, profile_ids: [] };

test('reload recovery stores only one bounded identity and no submitted secrets', () => {
  assert.notEqual(modelRequestStorageKey('http://127.0.0.1:3028'), modelRequestStorageKey('http://127.0.0.1:3018'));
  const input = { ...request, api_key: 'synthetic-secret', authorization_url: 'https://example.test/authorize' };
  const serialized = encodeModelRequest(input);
  assert.deepEqual(JSON.parse(serialized), { version: 1, ...request });
  assert.deepEqual(decodeModelRequest(serialized), request);
  assert.equal(decodeModelRequest(null), null);
  for (const invalid of ['bad-json', 'null', '{}', ' '.repeat(513), JSON.stringify({ version: 1, ...input }), JSON.stringify({ version: 1, ...request, requestId: '../other' })]) {
    assert.equal(decodeModelRequest(invalid), 'invalid');
  }
});

test('a lost response can be resolved only by a matching confirmed API creation', () => {
  const completed = { request_id: request.requestId, status: 'completed' as const, connection };
  assert.equal(modelRequestIsConfirmed(request, completed), true);
  assert.equal(modelRequestIsConfirmed(request, { ...completed, request_id: 'different-key' }), false);
  assert.equal(modelRequestIsConfirmed(request, { ...completed, connection: null }), false);
  for (const status of ['unconfirmed', 'unavailable'] as const) {
    const result = { ...completed, status };
    assert.equal(modelRequestIsConfirmed(request, result), false);
    assert.match(modelRequestResultCopy(request, result), /不要再次保存/);
  }
  assert.match(modelRequestResultCopy(request, { ...completed, request_id: 'different-key' }), /不一致/);
});

test('OAuth unavailable never proves authorization did not happen or permits replay', () => {
  const oauth = { ...request, kind: 'oauth' as const };
  for (const status of ['completed', 'unconfirmed', 'unavailable'] as const) {
    const result = { request_id: oauth.requestId, status, connection: { ...connection, auth_method: 'oauth' as const } };
    assert.equal(modelRequestIsConfirmed(oauth, result), false);
    assert.match(modelRequestResultCopy(oauth, result), /暂时不要重新登录/);
  }
});
