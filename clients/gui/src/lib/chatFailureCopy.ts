import { outcomeNeedsReconciliation, requestCode } from '../features/extensions/localProjection.ts';
import { requestErrorCopy } from './requestErrorCopy.ts';

export type ChatFailureContext = 'status' | 'history' | 'command_list' | 'artifact_list' | 'arguments' | 'command' | 'reference' | 'approval';

const knownCodes = new Set([
  'transport_unavailable', 'network_error', 'authentication_required', 'permission_denied',
  'approval_required', 'command_outcome_unknown', 'outcome_unknown',
  'manual_reconcile_required', 'command_in_progress', 'conflict', 'not_found',
  'invalid_error_envelope', 'request_validation_failed', 'validation_error',
  'observation_expired', 'observation_unavailable',
  'http_400', 'http_401', 'http_403', 'http_404', 'http_409', 'http_422',
  'http_502', 'http_503', 'http_504',
  'agent_failed', 'session_run_failed',
]);

const fallback: Record<ChatFailureContext, string> = {
  status: '连接或运行状态暂时无法确认。请刷新状态并核对当前对话。',
  history: '对话记录暂时无法读取。请刷新对话记录后核对。',
  command_list: '命令列表暂时无法读取。请检查连接后刷新，暂时不要提交命令。',
  artifact_list: '可引用内容暂时无法读取。请检查连接后重试读取。',
  arguments: '命令参数不符合要求。请检查 JSON 对象和必填项后重试。',
  command: '命令结果尚未确认。请刷新对话记录和审计，核对原请求后再决定下一步，暂时不要重复提交。',
  reference: '引用结果尚未确认。请刷新对话记录，核对原请求后再决定下一步，暂时不要重复提交。',
  approval: '审批结果尚未确认。请刷新审批状态并核对原请求，暂时不要重复提交。',
};

export function chatFailureCode(error: unknown): string | undefined {
  const code = requestCode(error);
  return code && knownCodes.has(code) ? code : undefined;
}

export function chatFailureCopy(error: unknown, context: ChatFailureContext): string {
  const recovery = error && typeof error === 'object' && 'recovery' in error
    ? (error as { recovery?: unknown }).recovery : undefined;
  if (outcomeNeedsReconciliation(error) || recovery === 'retry_same_idempotency_key') {
    return requestErrorCopy({ code: 'outcome_unknown' });
  }
  const code = chatFailureCode(error);
  if (!code) return fallback[context];
  const detail = error && typeof error === 'object' && 'detail' in error
    ? (error as { detail?: unknown }).detail : undefined;
  return requestErrorCopy({ code, ...(code === 'agent_failed' || code === 'session_run_failed' ? { detail } : {}) });
}
