import assert from 'node:assert/strict';
import test from 'node:test';
import { codexLoginRequestHasEnded, decodeModelRequest, encodeModelRequest, modelRequestAfterReconciliation, modelRequestIsConfirmed, modelRequestResultCopy, modelRequestStorageKey } from '../src/features/settings/modelRequestRecovery.ts';

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

test('only the original confirmed Codex receipt recovers a login attempt', () => {
  const oauth = { requestId: 'codex-request', kind: 'codex_oauth' as const };
  assert.deepEqual(decodeModelRequest(encodeModelRequest(oauth)), oauth);
  const attempt = { attempt_id: 'attempt-1', connection_id: 'codex-1', provider: 'codex' as const, status: 'pending' as const, authorization_url: null, expires_at: '2026-10-14T00:00:00Z' };
  const codexConnection = { ...connection, connection_id: 'codex-1', provider: 'codex' as const, auth_method: 'oauth' as const };
  const receipt = { request_id: oauth.requestId, status: 'completed' as const, connection: codexConnection, oauth_attempt: attempt };
  assert.equal(modelRequestIsConfirmed(oauth, receipt), true);
  assert.match(modelRequestResultCopy(oauth, receipt), /找回原登录.*取消登录/);
  assert.equal(modelRequestIsConfirmed({ ...oauth, kind: 'oauth' }, receipt), false);
  for (const mismatch of [
    { ...receipt, request_id: 'another-request' },
    { ...receipt, status: 'unconfirmed' as const },
    { ...receipt, oauth_attempt: null },
    { ...receipt, oauth_attempt: { ...attempt, connection_id: 'another-connection' } },
    { ...receipt, connection: { ...codexConnection, provider: 'chatgpt' as const } },
    { ...receipt, connection: { ...codexConnection, auth_method: 'api_key' as const } },
  ]) assert.equal(modelRequestIsConfirmed(oauth, mismatch), false);
  assert.match(modelRequestResultCopy(oauth, { ...receipt, status: 'unconfirmed' }), /暂时不要重新登录/);
});

test('confirmed pending Codex login survives repeated reloads until a terminal status', () => {
  const pending = { attempt_id: 'attempt-2', connection_id: 'codex-2', provider: 'codex' as const, status: 'pending' as const, authorization_url: null, expires_at: '2026-10-14T00:00:00Z' };
  const connectionForAttempt = { ...connection, connection_id: pending.connection_id, provider: 'codex' as const, auth_method: 'oauth' as const };
  let stored = { requestId: 'original-start-id', kind: 'codex_oauth' as const };
  const pendingReceipt = { request_id: stored.requestId, status: 'completed' as const, connection: connectionForAttempt, oauth_attempt: pending };
  for (let reload = 0; reload < 2; reload++) {
    const restored = decodeModelRequest(encodeModelRequest(stored));
    assert.deepEqual(restored, stored);
    assert.equal(modelRequestIsConfirmed(stored, pendingReceipt), true);
    stored = modelRequestAfterReconciliation(stored, pendingReceipt)!;
    assert.deepEqual(stored, { requestId: 'original-start-id', kind: 'codex_oauth' });
    assert.equal(codexLoginRequestHasEnded(stored, pending, pending.attempt_id), false);
  }
  for (const status of ['connected', 'ready', 'error', 'cancelled', 'expired'] as const) {
    const terminal = { ...pending, status };
    assert.equal(modelRequestAfterReconciliation(stored, { ...pendingReceipt, oauth_attempt: terminal }), null);
    assert.equal(codexLoginRequestHasEnded(stored, terminal, pending.attempt_id), true);
    assert.equal(codexLoginRequestHasEnded(stored, { ...terminal, attempt_id: 'foreign-attempt' }, pending.attempt_id), false);
  }
  assert.deepEqual(modelRequestAfterReconciliation(stored, { ...pendingReceipt, status: 'unconfirmed' }), stored);
  assert.equal(codexLoginRequestHasEnded({ requestId: 'legacy', kind: 'oauth' }, { ...pending, status: 'cancelled' }, pending.attempt_id), false);
  assert.equal(codexLoginRequestHasEnded(stored, null, pending.attempt_id), false);
});

test('a lost Codex cancel response needs its own matching terminal receipt', () => {
  const cancel = { requestId: 'cancel-request', kind: 'codex_cancel' as const, attemptId: 'attempt-3' };
  const encoded = encodeModelRequest({ ...cancel, authorization_url: 'https://example.test/secret' });
  assert.deepEqual(JSON.parse(encoded), { version: 2, ...cancel });
  assert.deepEqual(decodeModelRequest(encoded), cancel);
  for (const invalid of [
    JSON.stringify({ version: 2, requestId: cancel.requestId, kind: cancel.kind }),
    JSON.stringify({ version: 2, ...cancel, attemptId: '../other' }),
    JSON.stringify({ version: 2, ...cancel, authorization_url: 'https://example.test/secret' }),
  ]) assert.equal(decodeModelRequest(invalid), 'invalid');

  const attempt = { attempt_id: cancel.attemptId, connection_id: 'codex-3', provider: 'codex' as const, status: 'cancelled' as const, authorization_url: null, expires_at: '2026-10-14T00:00:00Z' };
  const receipt = { request_id: cancel.requestId, status: 'completed' as const, connection: null, oauth_attempt: attempt };
  assert.equal(modelRequestIsConfirmed(cancel, receipt), true);
  assert.equal(modelRequestAfterReconciliation(cancel, receipt), null);
  assert.match(modelRequestResultCopy(cancel, receipt), /已核对原取消请求/);
  for (const mismatch of [
    { ...receipt, request_id: 'start-request' },
    { ...receipt, status: 'unconfirmed' as const },
    { ...receipt, oauth_attempt: { ...attempt, attempt_id: 'another-attempt' } },
    { ...receipt, oauth_attempt: { ...attempt, provider: 'chatgpt' as const } },
    { ...receipt, oauth_attempt: { ...attempt, status: 'pending' as const } },
    { ...receipt, oauth_attempt: null },
  ]) {
    assert.equal(modelRequestIsConfirmed(cancel, mismatch), false);
    assert.deepEqual(modelRequestAfterReconciliation(cancel, mismatch), cancel);
  }
  const originalStart = { requestId: 'start-request', kind: 'codex_oauth' as const };
  assert.equal(modelRequestIsConfirmed(cancel, { ...receipt, request_id: originalStart.requestId }), false);
});
