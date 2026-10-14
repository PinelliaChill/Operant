import type { ConnectionRequestResult, OAuthAttempt } from '../../../../../sdk/typescript-client/onboarding.generated';

export type PendingModelRequest =
  | { requestId: string; kind: 'api_key' | 'oauth' | 'codex_oauth' }
  | { requestId: string; kind: 'codex_cancel'; attemptId: string };

export function codexLoginRequestHasEnded(request: PendingModelRequest | 'invalid' | null, attempt: OAuthAttempt | null, knownAttemptId: string | null): boolean {
  return request !== null && request !== 'invalid' && request.kind === 'codex_oauth'
    && Boolean(knownAttemptId) && attempt?.attempt_id === knownAttemptId
    && attempt.provider === 'codex' && attempt.status !== 'pending';
}

export function modelRequestAfterReconciliation(request: PendingModelRequest, result: ConnectionRequestResult): PendingModelRequest | null {
  if (!modelRequestIsConfirmed(request, result)) return request;
  return request.kind === 'codex_oauth' && result.oauth_attempt?.status === 'pending' ? request : null;
}

export function modelRequestStorageKey(origin: string): string {
  return `operant.setup.pending-model:${encodeURIComponent(origin)}`;
}

/** Store one request identity, never submitted fields or authorization URLs. */
export function encodeModelRequest(request: PendingModelRequest): string {
  return request.kind === 'codex_cancel'
    ? JSON.stringify({ version: 2, requestId: request.requestId, kind: request.kind, attemptId: request.attemptId })
    : JSON.stringify({ version: 1, requestId: request.requestId, kind: request.kind });
}

/** Invalid recovery data keeps the write barrier instead of enabling a retry. */
export function decodeModelRequest(raw: string | null): PendingModelRequest | 'invalid' | null {
  if (raw === null) return null;
  if (raw.length > 512) return 'invalid';
  try {
    const value: unknown = JSON.parse(raw);
    if (!value || typeof value !== 'object') return 'invalid';
    const item = value as Record<string, unknown>;
    if (typeof item.requestId !== 'string' || !/^[a-zA-Z0-9_-]{1,200}$/.test(item.requestId)) return 'invalid';
    if (item.kind === 'codex_cancel') {
      if (item.version !== 2 || Object.keys(item).length !== 4 ||
        typeof item.attemptId !== 'string' || !/^[a-zA-Z0-9_-]{1,200}$/.test(item.attemptId)) return 'invalid';
      return { requestId: item.requestId, kind: 'codex_cancel', attemptId: item.attemptId };
    }
    if (Object.keys(item).length !== 3 || item.version !== 1 ||
      !['api_key', 'oauth', 'codex_oauth'].includes(String(item.kind))) return 'invalid';
    return { requestId: item.requestId, kind: item.kind as 'api_key' | 'oauth' | 'codex_oauth' };
  } catch { return 'invalid'; }
}

export function modelRequestIsConfirmed(request: PendingModelRequest, result: ConnectionRequestResult): boolean {
  if (result.request_id !== request.requestId || result.status !== 'completed') return false;
  if (request.kind === 'api_key') return Boolean(result.connection?.connection_id) && result.connection?.auth_method === 'api_key';
  const attempt = result.oauth_attempt;
  if (request.kind === 'codex_cancel') return attempt?.provider === 'codex'
    && attempt.attempt_id === request.attemptId
    && ['connected', 'ready', 'cancelled', 'expired', 'error'].includes(attempt.status);
  return request.kind === 'codex_oauth'
    && result.connection?.provider === 'codex' && result.connection.auth_method === 'oauth'
    && Boolean(result.connection.connection_id)
    && attempt?.provider === 'codex' && Boolean(attempt.attempt_id)
    && attempt.connection_id === result.connection.connection_id
    && ['pending', 'connected', 'ready', 'cancelled', 'expired', 'error'].includes(attempt.status);
}

export function modelRequestResultCopy(request: PendingModelRequest, result: ConnectionRequestResult): string {
  if (result.request_id !== request.requestId) return '返回的请求编号不一致，请继续核对，暂时不要提交。';
  if (modelRequestIsConfirmed(request, result)) return request.kind === 'codex_oauth'
    ? result.oauth_attempt?.status === 'pending' ? '已找回原登录，正在等待浏览器授权；可在下方取消登录。' : '已找回原登录结果，请查看下方连接状态。'
    : request.kind === 'codex_cancel' ? '已核对原取消请求，请查看当前登录状态。'
    : '已找到原请求创建的连接，请查看下方连接状态。';
  if (request.kind === 'codex_cancel') return '取消登录的结果尚未确认。正在核对原请求，暂时不要重复取消或连接。';
  if (request.kind === 'codex_oauth') return '原 Codex 登录结果尚未确认。请稍后核对原请求，暂时不要重新登录。';
  if (request.kind === 'oauth') return '登录结果尚未确认。请核对已有连接和浏览器中的授权状态，暂时不要重新登录。';
  if (result.status === 'unconfirmed') return '连接已存在，但原请求结果尚未确认。请继续人工核对，暂时不要再次保存。';
  return '尚未找到原请求的连接。它可能已被移除，请继续人工核对，暂时不要再次保存。';
}
