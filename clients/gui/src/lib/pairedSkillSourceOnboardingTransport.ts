import { Phase1EError, type Phase1ETransport } from '../../../../sdk/typescript-client/phase1e-transport.ts';
import { fetchCallerPairingTransport } from '../../../../sdk/typescript-client/caller-pairing-transport.ts';
import type { PairingJsonObject } from './pairingCrypto.ts';
import type { PairedReceipt } from './pairedSkillSourceTransport.ts';

export interface PairedSourceOperations {
  identity: { baseUrl: string };
  list(): Promise<PairedReceipt>;
  write(operation: 'add' | 'remove', args: PairingJsonObject, id: string): Promise<PairedReceipt>;
}

function receiptResult(receipt: PairedReceipt): Record<string, unknown> {
  if (receipt.state === 'completed' && receipt.result) return receipt.result;
  if (receipt.state === 'awaiting_approval') {
    throw new Phase1EError('approval_required', '请在桌面版批准这次技能来源修改，再回来核对。', false, 'manual_reconcile', receipt);
  }
  if (receipt.state === 'unconfirmed' || receipt.state === 'in_progress') {
    throw new Phase1EError('outcome_unknown', '修改结果尚未确认，请核对原请求。', false, 'manual_reconcile', receipt);
  }
  throw new Phase1EError(receipt.error_code ?? 'skill_source_rejected', '技能来源操作未完成，请核对目录与权限。', false, 'none', receipt);
}

/** The formal Onboarding SDK is reused for exactly three skill-source operations. */
export function createPairedSkillSourceTransport(session: PairedSourceOperations, fallback: Phase1ETransport = fetchCallerPairingTransport): Phase1ETransport {
  const sourcePath = '/v1/setup/skill-sources';
  const transport: Phase1ETransport = async (request) => {
    const url = new URL(request.url);
    if (url.origin !== session.identity.baseUrl || request.path !== url.pathname || url.search || url.hash) {
      throw new Phase1EError('pairing_scope_rejected', '技能来源请求地址与配对信息不一致。', false);
    }
    if (request.method === 'GET' && url.pathname === '/v1/protocol/onboarding') {
      return fallback(request);
    }
    let receipt: PairedReceipt;
    if (request.method === 'GET' && url.pathname === sourcePath) {
      receipt = await session.list();
    } else if (request.method === 'POST' && url.pathname === sourcePath) {
      let body: unknown;
      try { body = JSON.parse(request.body ?? ''); } catch { body = null; }
      if (!body || typeof body !== 'object' || Array.isArray(body)
        || Object.keys(body).length !== 1
        || typeof (body as Record<string, unknown>).path !== 'string') {
        throw new Phase1EError('request_validation_failed', '请选择有效的技能目录。', false);
      }
      const key = Object.entries(request.headers ?? {}).find(([name]) => name.toLowerCase() === 'idempotency-key')?.[1];
      if (!key) throw new Phase1EError('request_validation_failed', '缺少本次操作标识，请刷新后重试。', false);
      receipt = await session.write('add', { path: (body as { path: string }).path }, key);
    } else if (request.method === 'DELETE' && url.pathname.startsWith(`${sourcePath}/`)) {
      const encoded = url.pathname.slice(sourcePath.length + 1);
      if (!/^[A-Za-z0-9._~-]+$/.test(encoded)) throw new Phase1EError('request_validation_failed', '技能目录编号无效。', false);
      const key = Object.entries(request.headers ?? {}).find(([name]) => name.toLowerCase() === 'idempotency-key')?.[1];
      if (!key) throw new Phase1EError('request_validation_failed', '缺少本次操作标识，请刷新后重试。', false);
      receipt = await session.write('remove', { root_ref: encoded }, key);
    } else {
      throw new Phase1EError('pairing_scope_rejected', '当前连接只允许管理技能来源。', false);
    }
    const result = receiptResult(receipt);
    return { status: receipt.http_status, headers: { 'content-type': 'application/json', 'cache-control': 'no-store' }, text: () => JSON.stringify(result) };
  };
  return transport;
}
