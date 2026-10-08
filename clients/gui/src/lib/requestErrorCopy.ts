const copy: Record<string, string> = {
  transport_unavailable: '连接已中断。恢复连接后刷新并核对原请求，勿直接重复提交。',
  network_error: '连接失败。请检查网络与本机服务，再刷新状态。',
  authentication_required: '登录或密钥已失效。请重新连接后刷新状态。',
  permission_denied: '当前操作没有权限。请检查授权目标或项目设置。',
  approval_required: '此操作需要人工确认。请处理当前审批后继续。',
  command_outcome_unknown: '操作结果尚未确认。请按原请求刷新核对，勿再次提交。',
  outcome_unknown: '操作结果尚未确认。请按原请求刷新核对，勿再次提交。',
  manual_reconcile_required: '原请求需要核对。请查询原请求结果，暂时不要重复提交。',
  command_in_progress: '原请求仍在处理中。请稍后刷新，勿再次提交。',
  conflict: '状态已变化。请刷新并核对后重试。',
  not_found: '目标记录未找到。请刷新列表，确认它是否已关闭或移除。',
  observation_expired: '页面或窗口信息已过期，请重新查看后再操作。',
  observation_unavailable: '页面或窗口信息不可用，请重新查看后再操作。',
  invalid_error_envelope: '服务返回了无法识别的结果。请刷新并核对状态，勿重复写入。',
  request_validation_failed: '填写内容不符合要求。请检查所选目标和输入后重试。',
};

export function requestErrorCopy(error: unknown): string {
  if (!error || typeof error !== 'object') return '请求未完成。请刷新状态后重试。';
  const source = error as { code?: unknown; message?: unknown; recovery?: unknown };
  const code = typeof source.code === 'string' ? source.code : '';
  if (copy[code]) return copy[code];
  if (source.recovery === 'manual_reconcile' || source.recovery === 'retry_same_idempotency_key') return copy.outcome_unknown;
  const message = typeof source.message === 'string' ? source.message.trim() : '';
  const withoutHttp = message.replace(/^http_\d{3}:\s*/, '');
  if (/^[\u3400-\u9fff]/u.test(withoutHttp) && withoutHttp.length <= 300) return withoutHttp;
  if (code === 'http_401' || code === 'http_403') return copy.permission_denied;
  if (code === 'http_404') return copy.not_found;
  if (code === 'http_409') return copy.conflict;
  if (code === 'http_422' || code === 'validation_error') return '填写内容不符合要求。请检查所选目标和输入后重试。';
  if (code === 'http_502' || code === 'http_503' || code === 'http_504') return '本机服务暂时不可用。请稍后刷新状态，再决定是否重试。';
  return '请求未完成。请刷新并核对结果。';
}
