import assert from 'node:assert/strict';
import test from 'node:test';
import {
  base64url, canonicalJson, decryptPairingReply, derivePairingKey, encryptPairingPayload,
  fromBase64url, generatePairingKeys, nonce, pairingPaths, pairingRequestHash,
  signPairingEnvelope, verifyPairingReply,
} from '../src/lib/pairingCrypto.ts';

test('pairing keys are P-256 and private keys are not exportable', async () => {
  const keys = await generatePairingKeys();
  assert.equal(keys.signing.privateKey.extractable, false);
  assert.equal(keys.exchange.privateKey.extractable, false);
  assert.match(keys.deviceId, /^caller_[0-9a-f]{32}$/);
  assert.equal(fromBase64url(keys.signingPublic).length, 65);
  assert.equal(fromBase64url(keys.exchangePublic).length, 65);
  await assert.rejects(crypto.subtle.exportKey('pkcs8', keys.signing.privateKey));
});

test('WebCrypto matches the independent Python P-256/HKDF/AES-GCM pair vector', async () => {
  const corePublic = 'BGsX0fLhLEJH-Lzm5WOkQPJ3A32BLeszoPShOUXYmMKWT-NC4v4af5uO5-tKfA-eFivOM1drMV7Oy7ZAaDe_UfU';
  const devicePublic = 'BHzyexiNA09-ilI4AwS1GsPAiWnid_IbNaYLSPxHZpl4B3dVENuO0EApPZrGn3Qw27p9reY86YIpngS3nSJ4c9E';
  const raw = fromBase64url(devicePublic, 65);
  const scalar = new Uint8Array(32); scalar[31] = 2;
  const privateKey = await crypto.subtle.importKey('jwk', {
    kty: 'EC', crv: 'P-256', x: base64url(raw.slice(1, 33)), y: base64url(raw.slice(33)),
    d: base64url(scalar), ext: false, key_ops: ['deriveBits'],
  }, { name: 'ECDH', namedCurve: 'P-256' }, false, ['deriveBits']);
  const epoch = `core_${'1'.repeat(32)}`;
  const device = `caller_${'2'.repeat(32)}`;
  const key = await derivePairingKey(privateKey, corePublic, epoch, device, 'pair-request');
  const envelope = {
    protocol_version: 'caller-pairing.v1', pair_request_id: 'vector-1',
    ticket_id: `ticket_${'3'.repeat(32)}`, core_epoch_id: epoch, device_id: device,
    device_signing_public_key: devicePublic, device_exchange_public_key: devicePublic,
    scope: 'skill_source.manage', issued_at: 1700000000, expires_at: 1700000060,
    nonce: base64url(Uint8Array.from({ length: 12 }, (_, index) => index)),
  };
  const ciphertext = await encryptPairingPayload({ one_time_code: 'A'.repeat(43), display_name: 'Vector' },
    envelope, key, pairingPaths.pair);
  assert.equal(ciphertext, 'vla3a7eeZRMartdoWsWLEs8Phn3sXEijOIQTPPOVuRpncLgeaF00bIdO_SvQA58rylYi-rY4fER_fuEWR-FtuG2WxB0qvEDrIlL3ShPg3RirdTgIaWohTprBTvfwmJUbV9cdKT-O4A');
  assert.equal(await pairingRequestHash({ ...envelope, ciphertext }, pairingPaths.pair),
    'f663f198c48ec1da3f2648f5f565a94c64d1d9c95c05aea7b9c4348cd49ee912');
});

test('canonical signing, scoped HKDF and AES-GCM round-trip bind the exact request', async () => {
  const client = await generatePairingKeys();
  const core = await generatePairingKeys();
  const epochId = 'core_0123456789abcdef0123456789abcdef';
  const envelope = {
    protocol_version: 'caller-pairing.v1', core_epoch_id: epochId, device_id: client.deviceId,
    request_id: 'sample-request', operation: 'add', issued_at: 1_800_000_000,
    expires_at: 1_800_000_120, nonce: nonce(),
  };
  assert.equal(fromBase64url(envelope.nonce).length, 12);
  assert.equal(new TextDecoder().decode(canonicalJson({ z: 1, a: '中文' })), '{"a":"中文","z":1}');
  const clientKey = await derivePairingKey(client.exchange.privateKey, core.exchangePublic, epochId, client.deviceId, 'command-request');
  const coreKey = await derivePairingKey(core.exchange.privateKey, client.exchangePublic, epochId, client.deviceId, 'command-request');
  const ciphertext = await encryptPairingPayload({ path: '/tmp/技能' }, envelope, clientKey, pairingPaths.command);
  const signed = { ...envelope, ciphertext };
  const signature = await signPairingEnvelope(signed, client.signing.privateKey, pairingPaths.command);
  assert.equal(fromBase64url(signature).length, 64);
  assert.equal(await verifyPairingReply({ ...signed, signature }, client.signingPublic, pairingPaths.command), true);
  assert.equal(await verifyPairingReply({ ...signed, signature }, client.signingPublic, pairingPaths.pair), false);
  assert.deepEqual(await decryptPairingReply(signed, coreKey, pairingPaths.command), { path: '/tmp/技能' });
  assert.match(await pairingRequestHash({ ...signed, signature }, pairingPaths.command), /^[0-9a-f]{64}$/);
  await assert.rejects(decryptPairingReply({ ...signed, request_id: 'other' }, coreKey, pairingPaths.command));
  await assert.rejects(decryptPairingReply(signed, coreKey, pairingPaths.pair));
  const otherPurpose = await derivePairingKey(core.exchange.privateKey, client.exchangePublic, epochId, client.deviceId, 'command-reply');
  await assert.rejects(decryptPairingReply(signed, otherPurpose, pairingPaths.command));
  assert.equal(base64url(fromBase64url(signature)), signature);
  assert.throws(() => fromBase64url('AA='));
});
