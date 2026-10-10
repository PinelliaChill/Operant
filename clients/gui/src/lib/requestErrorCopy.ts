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
  agent_cancelled: '任务已取消。需要继续时，请重新开始任务。',
  agent_timed_out: '任务运行超时。请检查任务设置后重新开始。',
  budget_exhausted: '任务预算已用完。请调整预算后重新开始。',
  agent_no_progress: '任务连续没有进展，已停止。请调整任务后重新开始。',
  agent_max_turns: '任务已达到最大轮次。请调整任务后重新开始。',
};

const modelSetupCopy: Record<string, string> = {
  'Google desktop client ID and Cloud project ID are required': 'Gemini 登录还缺配置。请填写 Google Cloud 项目 ID，并在高级选项检查桌面 OAuth 客户端信息。',
  'Google desktop client secret is required': '缺少 Gemini 客户端密钥。请在高级选项填写后再连接。',
  'Google OAuth configuration is invalid': 'Gemini 登录配置无效。请检查项目 ID 和桌面 OAuth 客户端信息。',
  'Google Cloud project does not match this connection': '项目 ID 与当前连接不一致。请使用原项目；要更换项目，请先断开再连接。',
  'Google OAuth client does not match this connection': 'OAuth 客户端与当前连接不一致。请使用原客户端；要更换，请先断开再连接。',
};

const providerFailureCopy: Record<string, string> = {
  timeout: '模型请求超时，请检查网络连接后再试。',
  connect_timeout: '连接模型服务超时，请检查网络连接。',
  read_timeout: '等待模型响应超时，请稍后再试。',
  write_timeout: '发送模型请求超时，请检查网络连接。',
  pool_timeout: '模型连接等待超时，请稍后再试。',
  proxy_error: '代理连接失败，请检查代理设置。',
  connection_error: '无法连接模型服务，请检查网络连接。',
  protocol_error: '模型连接协议异常，请检查网络或代理设置。',
  network_error: '模型连接中断，请检查网络连接。',
  invalid_response: '模型响应格式异常，请稍后再试。',
};

function terminalFailureCopy(detail: unknown, code: string): string {
  if (detail && typeof detail === 'object') {
    const failure = (detail as { provider_failure?: unknown }).provider_failure;
    if (failure && typeof failure === 'object') {
      const { stage, category } = failure as { stage?: unknown; category?: unknown };
      if ((stage === 'inference_transport' || stage === 'inference_response')
        && typeof category === 'string' && Object.hasOwn(providerFailureCopy, category)) {
        return providerFailureCopy[category];
      }
    }
  }
  return code === 'session_run_failed'
    ? '任务启动失败。请检查模型连接和助手设置后重新开始。'
    : '任务运行失败。请检查模型连接或任务设置后重新开始。';
}

export function requestErrorCopy(error: unknown): string {
  if (!error || typeof error !== 'object') return '请求未完成。请刷新状态后重试。';
  const source = error as { code?: unknown; message?: unknown; recovery?: unknown; detail?: unknown };
  const code = typeof source.code === 'string' ? source.code : '';
  if (copy[code]) return copy[code];
  if (source.recovery === 'manual_reconcile' || source.recovery === 'retry_same_idempotency_key') return copy.outcome_unknown;
  if (code === 'agent_failed' || code === 'session_run_failed') return terminalFailureCopy(source.detail, code);
  const message = typeof source.message === 'string' ? source.message.trim() : '';
  const withoutHttp = message.replace(/^http_\d{3}:\s*/, '');
  if (code === 'http_400' && source.recovery === 'none' && modelSetupCopy[withoutHttp]) return modelSetupCopy[withoutHttp];
  if (/^[\u3400-\u9fff]/u.test(withoutHttp) && withoutHttp.length <= 300) return withoutHttp;
  if (code === 'http_401' || code === 'http_403') return copy.permission_denied;
  if (code === 'http_404') return copy.not_found;
  if (code === 'http_409') return copy.conflict;
  if (code === 'http_422' || code === 'validation_error') return '填写内容不符合要求。请检查所选目标和输入后重试。';
  if (code === 'http_502' || code === 'http_503' || code === 'http_504') return '本机服务暂时不可用。请稍后刷新状态，再决定是否重试。';
  return '请求未完成。请刷新并核对结果。';
}
