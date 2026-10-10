import assert from 'node:assert/strict';
import test from 'node:test';
import { createPairedSkillSourceTransport, type PairedSourceOperations } from '../src/lib/pairedSkillSourceOnboardingTransport.ts';
import type { PairedReceipt } from '../src/lib/pairedSkillSourceTransport.ts';

const baseUrl = 'http://127.0.0.1:8000';
const sourcePath = '/v1/setup/skill-sources';

function receipt(operation: PairedReceipt['operation'], state: PairedReceipt['state'] = 'completed'): PairedReceipt {
  return {
    request_id: 'request-1', device_id: 'caller_0123456789abcdef0123456789abcdef', operation,
    payload_hash: 'a'.repeat(64), state, http_status: state === 'completed' ? 200 : 409,
    result: state === 'completed' ? { items: [] } : null,
    approval_id: state === 'awaiting_approval' ? 'approval-1' : null,
    action_hash: state === 'awaiting_approval' ? 'b'.repeat(64) : null, error_code: null,
  };
}

test('formal skill-source routes translate only to paired operations and preserve the original write key', async () => {
  const calls: unknown[] = [];
  const operations: PairedSourceOperations = {
    identity: { baseUrl },
    async list() { calls.push('list'); return receipt('list'); },
    async write(operation, args, id) { calls.push([operation, args, id]); return receipt(operation); },
  };
  let fallbackCount = 0;
  const transport = createPairedSkillSourceTransport(operations, async () => {
    fallbackCount += 1;
    return { status: 200, text: '{}' };
  });
  await transport({ method: 'GET', path: '/v1/protocol/onboarding', url: `${baseUrl}/v1/protocol/onboarding` });
  const listed = await transport({ method: 'GET', path: sourcePath, url: `${baseUrl}${sourcePath}` });
  assert.deepEqual(JSON.parse(await (listed.text as () => string)()), { items: [] });
  await transport({ method: 'POST', path: sourcePath, url: `${baseUrl}${sourcePath}`,
    headers: { 'Idempotency-Key': 'original-add-key' }, body: '{"path":"/tmp/skills"}' });
  await transport({ method: 'DELETE', path: `${sourcePath}/user-abcdef123456`, url: `${baseUrl}${sourcePath}/user-abcdef123456`,
    headers: { 'Idempotency-Key': 'original-remove-key' } });
  assert.deepEqual(calls, ['list', ['add', { path: '/tmp/skills' }, 'original-add-key'],
    ['remove', { root_ref: 'user-abcdef123456' }, 'original-remove-key']]);
  assert.equal(fallbackCount, 1);
});

test('paired source transport rejects unknown scope and retains approval as a real recovery state', async () => {
  let writes = 0;
  let fallback = 0;
  const transport = createPairedSkillSourceTransport({
    identity: { baseUrl },
    async list() { return receipt('list'); },
    async write() { writes += 1; return receipt('add', 'awaiting_approval'); },
  }, async () => { fallback += 1; return { status: 200, text: '{}' }; });
  const rejected = [
    { method: 'GET', path: '/v1/setup/state', url: `${baseUrl}/v1/setup/state` },
    { method: 'POST', path: sourcePath, url: `http://127.0.0.1:9000${sourcePath}`, body: '{"path":"/tmp/x"}' },
    { method: 'POST', path: sourcePath, url: `${baseUrl}${sourcePath}`, body: '{"path":"/tmp/x","scope":"command"}' },
    { method: 'POST', path: sourcePath, url: `${baseUrl}${sourcePath}`, body: '{"path":"/tmp/x"}' },
  ];
  for (const wire of rejected) await assert.rejects(transport(wire));
  assert.equal(writes, 0);
  assert.equal(fallback, 0);
  await assert.rejects(transport({ method: 'POST', path: sourcePath, url: `${baseUrl}${sourcePath}`,
    headers: { 'Idempotency-Key': 'original-key' }, body: '{"path":"/tmp/x"}' }),
  (error: unknown) => Boolean(error && typeof error === 'object' && (error as { code?: unknown }).code === 'approval_required'
    && (error as { recovery?: unknown }).recovery === 'manual_reconcile'));
  assert.equal(writes, 1);
});
