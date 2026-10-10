/** WebCrypto counterpart of src/operant/caller_pairing/crypto.py. */
const domain = new TextEncoder().encode('caller-pairing.v1\0');
const encoder = new TextEncoder();
const signingDomain = encoder.encode('caller-pairing.v1\0signature\0POST\0');
const aadDomain = encoder.encode('caller-pairing.v1\0aad\0POST\0');
const requestDomain = encoder.encode('caller-pairing.v1\0POST\0');

export const pairingPaths = {
  pair: '/v1/local-callers/pair',
  command: '/v1/local-callers/commands',
  readback: '/v1/local-callers/requests/readback',
} as const;

export type PairingPath = typeof pairingPaths[keyof typeof pairingPaths];
export type KeyPurpose = 'pair-request' | 'pair-reply' | 'command-request' | 'command-reply';
export type PairingJsonValue = null | boolean | number | string | PairingJsonValue[] | { [key: string]: PairingJsonValue };
export type PairingJsonObject = Record<string, PairingJsonValue>;
type JsonValue = PairingJsonValue;
type JsonObject = PairingJsonObject;

function bytes(...parts: Uint8Array[]): Uint8Array {
  const result = new Uint8Array(parts.reduce((total, part) => total + part.length, 0));
  let offset = 0;
  for (const part of parts) { result.set(part, offset); offset += part.length; }
  return result;
}

function byteSource(value: Uint8Array): Uint8Array<ArrayBuffer> {
  return Uint8Array.from(value);
}

function sortedJson(value: JsonValue): JsonValue {
  if (Array.isArray(value)) return value.map(sortedJson);
  if (value && typeof value === 'object') {
    const sorted: JsonObject = Object.create(null) as JsonObject;
    for (const key of Object.keys(value).sort()) sorted[key] = sortedJson(value[key]);
    return sorted;
  }
  if (typeof value === 'number' && !Number.isFinite(value)) throw new TypeError('配对数据包含无效数字。');
  return value;
}

export function canonicalJson(value: JsonObject): Uint8Array {
  return encoder.encode(JSON.stringify(sortedJson(value)));
}

function unsigned(envelope: JsonObject): JsonObject {
  const { signature: _signature, ...rest } = envelope;
  void _signature;
  return rest;
}

function metadata(envelope: JsonObject): JsonObject {
  const { ciphertext: _ciphertext, signature: _signature, ...rest } = envelope;
  void _ciphertext; void _signature;
  return rest;
}

export function base64url(value: Uint8Array): string {
  let binary = '';
  for (const byte of value) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, '');
}

export function fromBase64url(value: string, expectedBytes?: number): Uint8Array {
  if (!/^[A-Za-z0-9_-]+$/.test(value)) throw new Error('配对数据编码无效。');
  const raw = atob(value.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - value.length % 4) % 4));
  const result = Uint8Array.from(raw, (character) => character.charCodeAt(0));
  if (base64url(result) !== value || (expectedBytes !== undefined && result.length !== expectedBytes)) {
    throw new Error('配对数据编码无效。');
  }
  return result;
}

export function nonce(): string { return base64url(crypto.getRandomValues(new Uint8Array(12))); }

export async function generatePairingKeys(): Promise<{ signing: CryptoKeyPair; exchange: CryptoKeyPair; signingPublic: string; exchangePublic: string; deviceId: string }> {
  const signing = await crypto.subtle.generateKey({ name: 'ECDSA', namedCurve: 'P-256' }, false, ['sign', 'verify']);
  const exchange = await crypto.subtle.generateKey({ name: 'ECDH', namedCurve: 'P-256' }, false, ['deriveBits']);
  const signingRaw = new Uint8Array(await crypto.subtle.exportKey('raw', signing.publicKey));
  const exchangeRaw = new Uint8Array(await crypto.subtle.exportKey('raw', exchange.publicKey));
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', byteSource(signingRaw)));
  return {
    signing, exchange, signingPublic: base64url(signingRaw), exchangePublic: base64url(exchangeRaw),
    deviceId: `caller_${Array.from(digest.slice(0, 16), (byte) => byte.toString(16).padStart(2, '0')).join('')}`,
  };
}

export async function derivePairingKey(privateKey: CryptoKey, peerPublic: string, epochId: string, deviceId: string, purpose: KeyPurpose): Promise<CryptoKey> {
  const publicKey = await crypto.subtle.importKey('raw', byteSource(fromBase64url(peerPublic, 65)), { name: 'ECDH', namedCurve: 'P-256' }, false, []);
  const shared = await crypto.subtle.deriveBits({ name: 'ECDH', public: publicKey }, privateKey, 256);
  const salt = await crypto.subtle.digest('SHA-256', byteSource(bytes(domain, encoder.encode(epochId), new Uint8Array([0]), encoder.encode(deviceId))));
  const material = await crypto.subtle.importKey('raw', shared, 'HKDF', false, ['deriveKey']);
  return crypto.subtle.deriveKey(
    { name: 'HKDF', hash: 'SHA-256', salt, info: byteSource(bytes(domain, encoder.encode(purpose))) },
    material, { name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt'],
  );
}

export function pairingAad(envelope: JsonObject, path: PairingPath): Uint8Array {
  return bytes(aadDomain, encoder.encode(path), new Uint8Array([0]), canonicalJson(metadata(envelope)));
}

export async function encryptPairingPayload(payload: JsonObject, envelope: JsonObject, key: CryptoKey, path: PairingPath): Promise<string> {
  const iv = byteSource(fromBase64url(String(envelope.nonce), 12));
  const cipher = await crypto.subtle.encrypt({ name: 'AES-GCM', iv, additionalData: byteSource(pairingAad(envelope, path)), tagLength: 128 },
    key, byteSource(canonicalJson(payload)));
  return base64url(new Uint8Array(cipher));
}

export async function decryptPairingReply(envelope: JsonObject, key: CryptoKey, path: PairingPath): Promise<JsonObject> {
  const iv = byteSource(fromBase64url(String(envelope.nonce), 12));
  const cipher = byteSource(fromBase64url(String(envelope.ciphertext)));
  const plain = await crypto.subtle.decrypt({ name: 'AES-GCM', iv, additionalData: byteSource(pairingAad(envelope, path)), tagLength: 128 }, key, cipher);
  const parsed: unknown = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(plain));
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('配对响应格式无效。');
  return parsed as JsonObject;
}

function signedBytes(envelope: JsonObject, path: PairingPath): Uint8Array {
  return bytes(signingDomain, encoder.encode(path), new Uint8Array([0]), canonicalJson(unsigned(envelope)));
}

export async function signPairingEnvelope(envelope: JsonObject, privateKey: CryptoKey, path: PairingPath): Promise<string> {
  const signature = await crypto.subtle.sign({ name: 'ECDSA', hash: 'SHA-256' }, privateKey, byteSource(signedBytes(envelope, path)));
  return base64url(new Uint8Array(signature));
}

export async function verifyPairingReply(envelope: JsonObject, coreSigningPublic: string, path: PairingPath): Promise<boolean> {
  const publicKey = await crypto.subtle.importKey('raw', byteSource(fromBase64url(coreSigningPublic, 65)), { name: 'ECDSA', namedCurve: 'P-256' }, false, ['verify']);
  return crypto.subtle.verify({ name: 'ECDSA', hash: 'SHA-256' }, publicKey,
    byteSource(fromBase64url(String(envelope.signature), 64)), byteSource(signedBytes(envelope, path)));
}

export async function pairingRequestHash(envelope: JsonObject, path: PairingPath): Promise<string> {
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', byteSource(bytes(requestDomain, encoder.encode(path), new Uint8Array([0]), canonicalJson(unsigned(envelope))))));
  return Array.from(digest, (byte) => byte.toString(16).padStart(2, '0')).join('');
}

export async function pairingPayloadHash(operation: 'list' | 'add' | 'remove', args: JsonObject): Promise<string> {
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', byteSource(canonicalJson({ operation, args }))));
  return Array.from(digest, (byte) => byte.toString(16).padStart(2, '0')).join('');
}
