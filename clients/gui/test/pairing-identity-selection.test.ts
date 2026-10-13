import assert from 'node:assert/strict';
import test from 'node:test';
import {
  hasPendingPairingIdentity, selectPairingIdentity,
  type PairingIdentity, type PendingPairedWrite,
} from '../src/lib/pairingIdentityStore.ts';

test('a saved selection survives reload and a pending request on any identity blocks a new identity for that Core', async () => {
  const first = { deviceId: 'caller_' + '1'.repeat(32), baseUrl: 'http://127.0.0.1:8000', paired: true, expiresAt: 0 } as PairingIdentity;
  const second = { deviceId: 'caller_' + '2'.repeat(32), baseUrl: first.baseUrl, paired: true, expiresAt: 9999999999 } as PairingIdentity;
  const otherCore = { deviceId: 'caller_' + '3'.repeat(32), baseUrl: 'http://127.0.0.1:8001', paired: true, expiresAt: 9999999999 } as PairingIdentity;
  const saved = [first, second, otherCore];
  assert.equal(selectPairingIdentity(saved, first.deviceId), first);
  assert.equal(selectPairingIdentity(saved, second.deviceId), second);
  assert.equal(selectPairingIdentity(saved, null), second);

  const seen: string[] = [];
  const read = async (deviceId: string): Promise<PendingPairedWrite | null> => {
    seen.push(deviceId);
    return deviceId === first.deviceId
      ? { deviceId, coreEpochId: 'core_' + '4'.repeat(32), requestId: 'original', operation: 'add', payloadHash: 'a'.repeat(64) }
      : null;
  };
  assert.equal(await hasPendingPairingIdentity(saved, first.baseUrl, read), true);
  assert.deepEqual(seen, [first.deviceId]);
  seen.length = 0;
  assert.equal(await hasPendingPairingIdentity(saved, otherCore.baseUrl, read), false);
  assert.deepEqual(seen, [otherCore.deviceId]);
});
