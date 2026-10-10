import {
  base64url, canonicalJson, derivePairingKey, decryptPairingReply, encryptPairingPayload,
  fromBase64url, generatePairingKeys, nonce, pairingPaths, pairingPayloadHash, pairingRequestHash, signPairingEnvelope,
  verifyPairingReply, type PairingJsonObject, type PairingPath,
} from './pairingCrypto.ts';
import {
  clearPendingPairedWrite, reservePendingPairedWrite, savePairingIdentity,
  type PairingIdentity, type PendingPairedWrite,
} from './pairingIdentityStore.ts';

export interface CallerPairingTicket {
  protocol_version: 'caller-pairing.v1';
  ticket_id: string;
  core_epoch_id: string;
  base_url: string;
  core_exchange_public_key: string;
  core_signing_public_key: string;
  scope: 'skill_source.manage';
  expires_at: number;
  one_time_code: string;
}

export interface CallerEncryptedReply extends PairingJsonObject {
  protocol_version: 'caller-pairing.v1';
  core_epoch_id: string;
  device_id: string;
  request_id: string;
  operation: 'pair' | 'list' | 'add' | 'remove' | 'readback' | 'continue';
  request_hash: string;
  issued_at: number;
  expires_at: number;
  nonce: string;
  ciphertext: string;
  signature: string;
}

export interface CallerPairingClientLike {
  pairLocalCaller(request: PairingJsonObject): Promise<CallerEncryptedReply>;
  executeCallerCommand(request: PairingJsonObject): Promise<CallerEncryptedReply>;
  readCallerRequest(request: PairingJsonObject): Promise<CallerEncryptedReply>;
}

export interface PairedReceipt {
  request_id: string;
  device_id: string;
  operation: 'list' | 'add' | 'remove';
  payload_hash: string;
  state: 'in_progress' | 'awaiting_approval' | 'completed' | 'failed' | 'unconfirmed';
  http_status: number;
  result: Record<string, unknown> | null;
  approval_id: string | null;
  action_hash: string | null;
  error_code: string | null;
}

const idPattern = /^(?:core|caller|ticket)_[0-9a-f]{32}$/;
const publicKeyPattern = /^[A-Za-z0-9_-]{87}$/;
const hashPattern = /^[0-9a-f]{64}$/;
const scope = 'skill_source.manage';
const protocol = 'caller-pairing.v1';
const ticketPrefix = 'operant-caller-pairing-v1:';
const ticketFields = [
  'protocol_version', 'ticket_id', 'core_epoch_id', 'base_url',
  'core_exchange_public_key', 'core_signing_public_key', 'scope', 'expires_at', 'one_time_code',
];

function now(): number { return Math.floor(Date.now() / 1000); }

function requestId(): string { return crypto.randomUUID(); }

export function parsePairingTicket(input: string): CallerPairingTicket {
  const code = input.trim();
  if (!code.startsWith(ticketPrefix) || code.length > 2048) {
    throw new Error('配对码格式不正确，请从桌面版重新复制。');
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(fromBase64url(code.slice(ticketPrefix.length))));
  } catch { throw new Error('配对码格式不正确，请从桌面版重新复制。'); }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('配对信息格式不正确。');
  const ticket = parsed as Record<string, unknown>;
  if (Object.keys(ticket).length !== ticketFields.length || Object.keys(ticket).some((key) => !ticketFields.includes(key))) {
    throw new Error('配对码包含无法识别的信息，请重新从桌面版复制。');
  }
  let url: URL | null = null;
  try { if (typeof ticket.base_url === 'string') url = new URL(ticket.base_url); } catch { /* malformed ticket */ }
  if (ticket.protocol_version !== protocol || ticket.scope !== scope
    || typeof ticket.ticket_id !== 'string' || !idPattern.test(ticket.ticket_id) || !ticket.ticket_id.startsWith('ticket_')
    || typeof ticket.core_epoch_id !== 'string' || !idPattern.test(ticket.core_epoch_id) || !ticket.core_epoch_id.startsWith('core_')
    || !url || url.protocol !== 'http:' || !['127.0.0.1', '[::1]'].includes(url.hostname)
    || !/^\d+$/.test(url.port) || Number(url.port) <= 0
    || ticket.base_url !== url.origin || url.username || url.password || url.pathname !== '/' || url.search || url.hash
    || typeof ticket.core_exchange_public_key !== 'string' || !publicKeyPattern.test(ticket.core_exchange_public_key)
    || typeof ticket.core_signing_public_key !== 'string' || !publicKeyPattern.test(ticket.core_signing_public_key)
    || typeof ticket.expires_at !== 'number' || !Number.isSafeInteger(ticket.expires_at)
    || ticket.expires_at <= now() || ticket.expires_at > now() + 300
    || typeof ticket.one_time_code !== 'string' || !/^[A-Za-z0-9_-]{32,100}$/.test(ticket.one_time_code)) {
    throw new Error('配对信息无效或已过期，请在桌面版重新生成。');
  }
  return ticket as unknown as CallerPairingTicket;
}

export function formatPairingTicket(ticket: CallerPairingTicket): string {
  return ticketPrefix + base64url(canonicalJson(ticket as unknown as PairingJsonObject));
}

async function checkedReply(
  response: CallerEncryptedReply,
  sent: PairingJsonObject,
  path: PairingPath,
  identity: PairingIdentity,
  purpose: 'pair-reply' | 'command-reply',
): Promise<PairingJsonObject> {
  if (response.protocol_version !== protocol || response.core_epoch_id !== identity.coreEpochId
    || response.device_id !== identity.deviceId || response.request_id !== (sent.pair_request_id ?? sent.request_id)
    || response.operation !== (path === pairingPaths.pair ? 'pair' : sent.operation)
    || response.request_hash !== await pairingRequestHash(sent, path)
    || !Number.isSafeInteger(response.issued_at) || !Number.isSafeInteger(response.expires_at)
    || response.expires_at < now() || response.issued_at > now() + 30) {
    throw new Error('配对响应与原请求不一致，请核对连接。');
  }
  if (!await verifyPairingReply(response, identity.coreSigningPublicKey, path)) {
    throw new Error('配对响应签名不匹配，请核对桌面版连接。');
  }
  const key = await derivePairingKey(identity.exchangePrivateKey, identity.coreExchangePublicKey,
    identity.coreEpochId, identity.deviceId, purpose);
  return decryptPairingReply(response, key, path);
}

function checkedReceipt(value: PairingJsonObject, identity: PairingIdentity): PairedReceipt {
  const raw = value.receipt;
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error('技能来源回执格式无效，请刷新核对。');
  const item = raw as PairingJsonObject;
  if (item.device_id !== identity.deviceId || typeof item.request_id !== 'string'
    || !['list', 'add', 'remove'].includes(String(item.operation))
    || typeof item.payload_hash !== 'string' || !hashPattern.test(item.payload_hash)
    || !['in_progress', 'awaiting_approval', 'completed', 'failed', 'unconfirmed'].includes(String(item.state))
    || !Number.isInteger(item.http_status) || Number(item.http_status) < 100 || Number(item.http_status) > 599
    || (item.result !== null && (typeof item.result !== 'object' || Array.isArray(item.result)))) {
    throw new Error('技能来源回执格式无效，请刷新核对。');
  }
  return item as unknown as PairedReceipt;
}

export interface PairingAttempt {
  requestId: string;
  requestHash: string;
  expiresAt: number;
  confirm: () => Promise<PairingIdentity>;
}

export async function prepareSkillSourcePairing(
  client: CallerPairingClientLike,
  ticket: CallerPairingTicket,
  displayName: string,
  existing: PairingIdentity | null,
): Promise<PairingAttempt> {
  if (ticket.expires_at <= now()) throw new Error('配对信息已过期，请在桌面版重新生成。');
  if (!displayName.trim() || displayName.length > 100) throw new Error('请填写设备名称。');
  if (existing && existing.baseUrl !== ticket.base_url) throw new Error('桌面地址与已配设备不同，请核对票据。');
  if (existing && existing.coreEpochId === ticket.core_epoch_id
    && (existing.coreSigningPublicKey !== ticket.core_signing_public_key
      || existing.coreExchangePublicKey !== ticket.core_exchange_public_key)) {
    throw new Error('桌面身份信息发生变化，请重新从桌面版生成配对信息。');
  }
  const keys = existing ? null : await generatePairingKeys();
  const identity: PairingIdentity = {
    deviceId: existing?.deviceId ?? keys!.deviceId,
    baseUrl: ticket.base_url, coreEpochId: ticket.core_epoch_id,
    coreSigningPublicKey: ticket.core_signing_public_key,
    coreExchangePublicKey: ticket.core_exchange_public_key,
    signingPrivateKey: existing?.signingPrivateKey ?? keys!.signing.privateKey,
    exchangePrivateKey: existing?.exchangePrivateKey ?? keys!.exchange.privateKey,
    signingPublicKey: existing?.signingPublicKey ?? keys!.signingPublic,
    exchangePublicKey: existing?.exchangePublicKey ?? keys!.exchangePublic,
    expiresAt: 0, paired: false,
  };
  if (!existing) await savePairingIdentity(identity);
  const issuedAt = now();
  const envelope: PairingJsonObject = {
    protocol_version: protocol, pair_request_id: requestId(), ticket_id: ticket.ticket_id,
    core_epoch_id: ticket.core_epoch_id, device_id: identity.deviceId,
    device_signing_public_key: identity.signingPublicKey,
    device_exchange_public_key: identity.exchangePublicKey, scope,
    issued_at: issuedAt, expires_at: issuedAt + 60, nonce: nonce(),
  };
  const key = await derivePairingKey(identity.exchangePrivateKey, ticket.core_exchange_public_key,
    identity.coreEpochId, identity.deviceId, 'pair-request');
  envelope.ciphertext = await encryptPairingPayload({ one_time_code: ticket.one_time_code, display_name: displayName.trim() },
    envelope, key, pairingPaths.pair);
  envelope.signature = await signPairingEnvelope(envelope, identity.signingPrivateKey, pairingPaths.pair);
  return {
    requestId: String(envelope.pair_request_id),
    requestHash: await pairingRequestHash(envelope, pairingPaths.pair),
    expiresAt: issuedAt + 60,
    confirm: async () => {
      // The caller invokes this explicitly. If a response is lost, another click
      // repeats the exact signed envelope; no new key, nonce or ticket is made.
      const response = await client.pairLocalCaller(envelope);
      const plaintext = await checkedReply(response, envelope, pairingPaths.pair, identity, 'pair-reply');
      if (plaintext.device_id !== identity.deviceId || plaintext.core_epoch_id !== identity.coreEpochId
        || plaintext.scope !== scope || typeof plaintext.expires_at !== 'number'
        || !Number.isSafeInteger(plaintext.expires_at) || plaintext.expires_at <= now()) {
        throw new Error('配对确认内容不匹配，请核对桌面版连接。');
      }
      const paired: PairingIdentity = { ...identity, paired: true, expiresAt: plaintext.expires_at };
      await savePairingIdentity(paired);
      return paired;
    },
  };
}

export class PairedSkillSourceSession {
  readonly client: CallerPairingClientLike;
  readonly identity: PairingIdentity;

  constructor(client: CallerPairingClientLike, identity: PairingIdentity) {
    this.client = client;
    this.identity = identity;
  }

  private async send(operation: 'list' | 'add' | 'remove' | 'readback' | 'continue', args: PairingJsonObject,
    id: string, path: PairingPath): Promise<PairedReceipt> {
    if (!this.identity.paired || this.identity.expiresAt <= now()) throw new Error('桌面配对已失效，请在桌面版确认重新连接。');
    const issuedAt = now();
    const envelope: PairingJsonObject = {
      protocol_version: protocol, core_epoch_id: this.identity.coreEpochId,
      device_id: this.identity.deviceId, request_id: id, operation,
      issued_at: issuedAt, expires_at: issuedAt + 60, nonce: nonce(),
    };
    const key = await derivePairingKey(this.identity.exchangePrivateKey, this.identity.coreExchangePublicKey,
      this.identity.coreEpochId, this.identity.deviceId, 'command-request');
    envelope.ciphertext = await encryptPairingPayload(args, envelope, key, path);
    envelope.signature = await signPairingEnvelope(envelope, this.identity.signingPrivateKey, path);
    const response = path === pairingPaths.readback
      ? await this.client.readCallerRequest(envelope) : await this.client.executeCallerCommand(envelope);
    const plaintext = await checkedReply(response, envelope, path, this.identity, 'command-reply');
    return checkedReceipt(plaintext, this.identity);
  }

  async list(): Promise<PairedReceipt> {
    const id = requestId();
    const receipt = await this.send('list', {}, id, pairingPaths.command);
    if (receipt.request_id !== id || receipt.operation !== 'list'
      || receipt.payload_hash !== await pairingPayloadHash('list', {})) throw new Error('技能来源回执与请求不一致。');
    return receipt;
  }

  async write(operation: 'add' | 'remove', args: PairingJsonObject, id: string): Promise<PairedReceipt> {
    const payloadHash = await pairingPayloadHash(operation, args);
    const pending: PendingPairedWrite = {
      deviceId: this.identity.deviceId, coreEpochId: this.identity.coreEpochId,
      requestId: id, operation, payloadHash,
    };
    // Persist the identifier before sending. A lost response never allows a new write key.
    await reservePendingPairedWrite(pending);
    const receipt = await this.send(operation, args, id, pairingPaths.command);
    await this.acceptReceipt(receipt, pending);
    return receipt;
  }

  async readback(pending: PendingPairedWrite): Promise<PairedReceipt> {
    if (pending.deviceId !== this.identity.deviceId) throw new Error('待核对请求属于其他设备。');
    const receipt = await this.send('readback', { original_request_id: pending.requestId }, requestId(), pairingPaths.readback);
    await this.acceptReceipt(receipt, pending);
    return receipt;
  }

  async continueApproved(receipt: PairedReceipt, pending: PendingPairedWrite): Promise<PairedReceipt> {
    if (pending.coreEpochId !== this.identity.coreEpochId) {
      throw new Error('桌面 Core 已重新启动。旧请求仅可核对，不能继续审批操作。');
    }
    if (receipt.state !== 'awaiting_approval' || receipt.request_id !== pending.requestId
      || receipt.payload_hash !== pending.payloadHash || !receipt.approval_id || !receipt.action_hash) {
      throw new Error('审批与原请求不一致，请先刷新核对。');
    }
    const continued = await this.send('continue', {
      original_request_id: pending.requestId, payload_hash: pending.payloadHash,
      approval_id: receipt.approval_id, action_hash: receipt.action_hash,
    }, requestId(), pairingPaths.command);
    await this.acceptReceipt(continued, pending);
    return continued;
  }

  private async acceptReceipt(receipt: PairedReceipt, pending: PendingPairedWrite): Promise<void> {
    if (receipt.request_id !== pending.requestId || receipt.device_id !== pending.deviceId
      || receipt.operation !== pending.operation || receipt.payload_hash !== pending.payloadHash) {
      throw new Error('回执与原修改不一致，请继续核对，暂勿重试。');
    }
    if (receipt.state === 'completed' || receipt.state === 'failed') {
      await clearPendingPairedWrite(pending.deviceId, pending.requestId);
    }
  }
}
