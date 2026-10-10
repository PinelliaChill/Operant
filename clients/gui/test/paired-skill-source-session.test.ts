import assert from 'node:assert/strict';
import test from 'node:test';
import {
  decryptPairingReply, derivePairingKey, encryptPairingPayload, generatePairingKeys,
  nonce, pairingPaths, pairingPayloadHash, pairingRequestHash, signPairingEnvelope,
  verifyPairingReply, type PairingJsonObject,
} from '../src/lib/pairingCrypto.ts';
import { PairedSkillSourceSession, formatPairingTicket, parsePairingTicket, prepareSkillSourcePairing, type CallerEncryptedReply } from '../src/lib/pairedSkillSourceTransport.ts';

test('ticket accepts only a short-lived numeric loopback Core and narrow scope', async () => {
  const core = await generatePairingKeys();
  const ticket = {
    protocol_version: 'caller-pairing.v1', ticket_id: 'ticket_0123456789abcdef0123456789abcdef',
    core_epoch_id: 'core_0123456789abcdef0123456789abcdef', base_url: 'http://127.0.0.1:8000',
    core_exchange_public_key: core.exchangePublic, core_signing_public_key: core.signingPublic,
    scope: 'skill_source.manage', expires_at: Math.floor(Date.now() / 1000) + 60,
    one_time_code: 'x'.repeat(43),
  };
  const code = formatPairingTicket(ticket);
  assert.match(code, /^operant-caller-pairing-v1:[A-Za-z0-9_-]+$/);
  assert.equal(parsePairingTicket(code).base_url, ticket.base_url);
  assert.throws(() => parsePairingTicket(JSON.stringify(ticket)));
  for (const changed of [
    { ...ticket, base_url: 'https://example.com' },
    { ...ticket, base_url: 'http://127.0.0.1:8000/' },
    { ...ticket, base_url: 'http://127.0.0.1:8000/other' },
    { ...ticket, scope: 'command' },
    { ...ticket, expires_at: 1 },
  ]) assert.throws(() => parsePairingTicket(formatPairingTicket(changed)));
});

test('an uncertain pair is retried only by an explicit call with the identical signed envelope', async () => {
  const deviceKeys = await generatePairingKeys();
  const coreKeys = await generatePairingKeys();
  const epoch = 'core_0123456789abcdef0123456789abcdef';
  const existing = {
    deviceId: deviceKeys.deviceId, baseUrl: 'http://127.0.0.1:8000', coreEpochId: epoch,
    coreSigningPublicKey: coreKeys.signingPublic, coreExchangePublicKey: coreKeys.exchangePublic,
    signingPrivateKey: deviceKeys.signing.privateKey, exchangePrivateKey: deviceKeys.exchange.privateKey,
    signingPublicKey: deviceKeys.signingPublic, exchangePublicKey: deviceKeys.exchangePublic,
    paired: false, expiresAt: 0,
  };
  const ticket = {
    protocol_version: 'caller-pairing.v1' as const, ticket_id: 'ticket_0123456789abcdef0123456789abcdef',
    core_epoch_id: epoch, base_url: existing.baseUrl, core_exchange_public_key: coreKeys.exchangePublic,
    core_signing_public_key: coreKeys.signingPublic, scope: 'skill_source.manage' as const,
    expires_at: Math.floor(Date.now() / 1000) + 90, one_time_code: 'C'.repeat(43),
  };
  const sent: PairingJsonObject[] = [];
  const fakeCore = {
    async pairLocalCaller(request: PairingJsonObject): Promise<CallerEncryptedReply> {
      sent.push(structuredClone(request));
      throw new Error('response lost after commit');
    },
    async executeCallerCommand(): Promise<CallerEncryptedReply> { throw new Error('not used'); },
    async readCallerRequest(): Promise<CallerEncryptedReply> { throw new Error('not used'); },
  };
  const attempt = await prepareSkillSourcePairing(fakeCore, ticket, 'Browser', existing);
  assert.match(attempt.requestHash, /^[0-9a-f]{64}$/);
  await assert.rejects(attempt.confirm());
  assert.equal(sent.length, 1);
  await assert.rejects(attempt.confirm());
  assert.deepEqual(sent[1], sent[0]);
  assert.equal(sent[0].pair_request_id, attempt.requestId);
  assert.equal(sent.length, 2);
});

test('a paired list command authenticates both directions and retains the formal receipt', async () => {
  const clientKeys = await generatePairingKeys();
  const coreKeys = await generatePairingKeys();
  const epoch = 'core_0123456789abcdef0123456789abcdef';
  const identity = {
    deviceId: clientKeys.deviceId, baseUrl: 'http://127.0.0.1:8000', coreEpochId: epoch,
    coreSigningPublicKey: coreKeys.signingPublic, coreExchangePublicKey: coreKeys.exchangePublic,
    signingPrivateKey: clientKeys.signing.privateKey, exchangePrivateKey: clientKeys.exchange.privateKey,
    signingPublicKey: clientKeys.signingPublic, exchangePublicKey: clientKeys.exchangePublic,
    paired: true, expiresAt: Math.floor(Date.now() / 1000) + 1000,
  };
  const listResult = { items: [{ root_ref: 'user-123456789abc', path: '/tmp/skills', label: 'Skills', exists: true, enabled: true }] };
  const fakeCore = {
    async pairLocalCaller(): Promise<CallerEncryptedReply> { throw new Error('not used'); },
    async readCallerRequest(): Promise<CallerEncryptedReply> { throw new Error('not used'); },
    async executeCallerCommand(request: PairingJsonObject): Promise<CallerEncryptedReply> {
      assert.equal(await verifyPairingReply(request, clientKeys.signingPublic, pairingPaths.command), true);
      const requestKey = await derivePairingKey(coreKeys.exchange.privateKey, clientKeys.exchangePublic,
        epoch, clientKeys.deviceId, 'command-request');
      assert.deepEqual(await decryptPairingReply(request, requestKey, pairingPaths.command), {});
      const issuedAt = Math.floor(Date.now() / 1000);
      const reply: PairingJsonObject = {
        protocol_version: 'caller-pairing.v1', core_epoch_id: epoch, device_id: clientKeys.deviceId,
        request_id: request.request_id, operation: 'list', request_hash: await pairingRequestHash(request, pairingPaths.command),
        issued_at: issuedAt, expires_at: issuedAt + 60, nonce: nonce(),
      };
      const replyKey = await derivePairingKey(coreKeys.exchange.privateKey, clientKeys.exchangePublic,
        epoch, clientKeys.deviceId, 'command-reply');
      reply.ciphertext = await encryptPairingPayload({ receipt: {
        request_id: request.request_id, device_id: clientKeys.deviceId, operation: 'list',
        payload_hash: await pairingPayloadHash('list', {}), state: 'completed', http_status: 200,
        result: listResult, approval_id: null, action_hash: null, error_code: null,
      } }, reply, replyKey, pairingPaths.command);
      reply.signature = await signPairingEnvelope(reply, coreKeys.signing.privateKey, pairingPaths.command);
      return reply as CallerEncryptedReply;
    },
  };
  const receipt = await new PairedSkillSourceSession(fakeCore, identity).list();
  assert.equal(receipt.state, 'completed');
  assert.deepEqual(receipt.result, listResult);
});
