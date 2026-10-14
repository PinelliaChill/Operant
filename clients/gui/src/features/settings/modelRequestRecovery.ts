import type { ConnectionRequestResult } from '../../../../../sdk/typescript-client/onboarding.generated';

export interface PendingModelRequest {
  requestId: string;
  kind: 'api_key' | 'oauth';
}

export function modelRequestStorageKey(origin: string): string {
  return `operant.setup.pending-model:${encodeURIComponent(origin)}`;
}

/** Store one request identity, never submitted fields or authorization URLs. */
export function encodeModelRequest(request: PendingModelRequest): string {
  return JSON.stringify({ version: 1, requestId: request.requestId, kind: request.kind });
}

/** Invalid recovery data keeps the write barrier instead of enabling a retry. */
export function decodeModelRequest(raw: string | null): PendingModelRequest | 'invalid' | null {
  if (raw === null) return null;
  if (raw.length > 512) return 'invalid';
  try {
    const value: unknown = JSON.parse(raw);
    if (!value || typeof value !== 'object') return 'invalid';
    const item = value as Record<string, unknown>;
    if (Object.keys(item).length !== 3 || item.version !== 1 ||
      typeof item.requestId !== 'string' || !/^[a-zA-Z0-9_-]{1,200}$/.test(item.requestId) ||
      !['api_key', 'oauth'].includes(String(item.kind))) return 'invalid';
    return { requestId: item.requestId, kind: item.kind as PendingModelRequest['kind'] };
  } catch { return 'invalid'; }
}

export function modelRequestIsConfirmed(request: PendingModelRequest, result: ConnectionRequestResult): boolean {
  return request.kind === 'api_key' && result.request_id === request.requestId &&
    result.status === 'completed' && Boolean(result.connection?.connection_id) &&
    result.connection?.auth_method === 'api_key';
}

export function modelRequestResultCopy(request: PendingModelRequest, result: ConnectionRequestResult): string {
  if (result.request_id !== request.requestId) return '返回的请求编号不一致，请继续核对，暂时不要提交。';
  if (modelRequestIsConfirmed(request, result)) return '已找到原请求创建的连接，请查看下方连接状态。';
  if (request.kind === 'oauth') return '登录结果尚未确认。请核对已有连接和浏览器中的授权状态，暂时不要重新登录。';
  if (result.status === 'unconfirmed') return '连接已存在，但原请求结果尚未确认。请继续人工核对，暂时不要再次保存。';
  return '尚未找到原请求的连接。它可能已被移除，请继续人工核对，暂时不要再次保存。';
}
