export function oauthStatusLabel(status: string): string {
  return ({ pending: '等待登录', connected: '账号已连接，正在准备模型', ready: '账号已就绪', cancelled: '登录已取消', expired: '登录已过期，请重新连接', error: '登录未完成，请查看原因' } as Record<string, string>)[status] || '登录状态待确认，请刷新';
}

const MODEL_CONNECTION_ERRORS: Record<string, string> = {
  chatgpt_plan_usage_disabled: '已登录，但未启用 ChatGPT 套餐调用。可重新授权套餐使用，或连接 API 密钥。',
  user_not_eligible: '此账号目前没有套餐调用资格。请确认账号套餐，或连接 API 密钥。',
  permission_denied: '当前服务未允许此账号或密钥访问。请检查授权和项目设置。',
  usage_unavailable: '登录状态已保留，暂时无法读取套餐用量。请稍后再查找模型。',
  authentication_required: '账号或密钥已失效，请重新连接。',
  usage_limit: '本期套餐用量已用完，请等待额度恢复或连接 API 密钥。',
  rate_limited: '请求过于频繁，请稍后再查找模型。',
  provider_unavailable: '模型服务暂时不可用，请稍后再查找模型。',
  network_error: '网络连接失败，请检查网络后再查找模型。',
  unsupported_capability: '本模型不支持当前请求参数。请调整模型或高级参数后重试。',
  revocation_unconfirmed: '断开连接的撤销尚未确认。请重试断开，不要重新登录。',
  'OAuth request failed': '登录处理发生异常，旧版本未记录具体原因。请检查应用诊断，暂时不要重复登录。',
  'OAuth token response is invalid': '登录服务返回的凭据格式无效。请检查应用的登录接口配置。',
  'OAuth token response is too large': '登录服务返回的数据超出安全限制。请检查应用的登录接口配置。',
  'ID token signing keys are unavailable': '暂时无法读取账号签名验证信息。请检查网络和登录服务状态。',
  'ID token validation failed': '账号凭据验证失败。请检查应用的登录配置，暂时不要重复登录。',
};

export function knownModelConnectionErrorLabel(error: string | null | undefined): string | null {
  if (!error) return null;
  const request = /^OAuth request failed \((callback_validation|token_exchange|identity_validation|credential_storage); (timeout|proxy_error|connection_error|protocol_error|network_error|http_error|credential_store_error|invalid_response|missing_data|invalid_data)\)$/.exec(error.trim());
  if (request) {
    const [, stage, category] = request;
    const step = ({ callback_validation: '检查登录回调', token_exchange: '换取登录凭据', identity_validation: '验证账号凭据', credential_storage: '保存登录凭据' } as Record<string, string>)[stage];
    if (category === 'timeout') return `${step}时请求超时。请检查网络和代理，暂时不要重复登录。`;
    if (category === 'proxy_error') return `${step}时代理连接失败。请检查代理配置，暂时不要重复登录。`;
    if (['connection_error', 'network_error', 'http_error'].includes(category)) return `${step}时网络连接失败。请检查网络和代理，暂时不要重复登录。`;
    if (category === 'protocol_error') return `${step}时网络响应不完整。请检查代理和服务状态，暂时不要重复登录。`;
    if (category === 'credential_store_error') return '本机凭据保存失败。请检查凭据文件权限，暂时不要重复登录。';
    if (category === 'invalid_response') return `${step}时服务返回了无效数据。请检查登录接口和代理响应，暂时不要重复登录。`;
    return `${step}时数据校验失败。请检查应用诊断，暂时不要重复登录。`;
  }
  const exchange = /^OAuth code exchange failed \(HTTP ([1-5]\d{2}); ([a-z0-9_]+)\)$/.exec(error.trim());
  if (exchange) {
    const [, status, code] = exchange;
    if (code === 'invalid_grant') return '授权码未被接受。请从 Operant 重新发起登录，不要刷新旧回调页面。';
    if (['invalid_client', 'unauthorized_client'].includes(code)) return '应用注册信息未被接受。请检查 Operant 的 OAuth 客户端配置。';
    if (['invalid_request', 'invalid_scope', 'unsupported_grant_type'].includes(code)) return '登录请求配置未被接受。请检查应用的登录配置。';
    if (['access_denied', '3p_delegated_access_policy_denied'].includes(code)) return '当前账号或工作区未允许这次授权。请检查工作区的外部应用权限。';
    if (code === 'subscription_sharing_user_not_eligible') return '当前账号或工作区没有 ChatGPT 套餐调用资格。请检查账号资格或连接 API 密钥。';
    if (['server_error', 'temporarily_unavailable'].includes(code) || status.startsWith('5')) return '登录服务暂时不可用。请稍后从 Operant 重新发起登录。';
    if (status === '429') return '登录请求过于频繁。请稍后从 Operant 重新发起登录。';
    if (status === '403') return '登录凭据换取被拒绝（HTTP 403）。请核对账号权限、应用配置和网络限制。';
    return `登录凭据换取失败（HTTP ${status}）。请查看失败原因并核对状态。`;
  }
  return MODEL_CONNECTION_ERRORS[error.trim()] ?? null;
}

export function modelConnectionErrorLabel(error: string | null | undefined): string {
  return knownModelConnectionErrorLabel(error) ?? '模型连接暂不可用。请查看详情并核对状态。';
}

export function modelConnectionStatusLabel(status: string, error: string | null | undefined): string {
  if (error?.trim() === 'chatgpt_plan_usage_disabled') return '已登录，需授权套餐';
  if (error?.trim() === 'user_not_eligible') return '套餐不可用';
  if (error?.trim() === 'permission_denied') return '无访问权限';
  if (error?.trim() === 'usage_unavailable') return '用量暂不可查';
  if (error?.trim() === 'revocation_unconfirmed') return '撤销待确认';
  return ({ needs_auth: '需要登录', connected: '已连接', ready: '可用', error: '连接受阻' } as Record<string, string>)[status] ?? '状态待确认';
}

export function canReauthorizeModelConnection(status: string, error: string | null | undefined): boolean {
  return error?.trim() !== 'revocation_unconfirmed'
    && (status === 'needs_auth' || error?.trim() === 'chatgpt_plan_usage_disabled');
}

export function canContinueOAuthRegistration(provider: string, status: string, message?: string | null): boolean {
  return provider === 'chatgpt' && status === 'error'
    && message === 'OAuth code exchange failed (HTTP 400; invalid_grant)';
}

export function canDiscoverConnectionModels(status: string, error: string | null | undefined): boolean {
  return status !== 'needs_auth' && !['chatgpt_plan_usage_disabled', 'revocation_unconfirmed'].includes(error?.trim() ?? '');
}

export function offersApiKeyAlternative(error: string | null | undefined): boolean {
  return ['chatgpt_plan_usage_disabled', 'user_not_eligible'].includes(error?.trim() ?? '');
}

export function collaborationStatusLabel(status: string): string {
  return ({ created: '协作已创建', queued: '协作等待启动', running: '协作运行中', active: '协作进行中', waiting_input: '协作等待输入', waiting_approval: '协作等待审批', interrupted: '协作已中断，请查看运行进度', manual_reconcile_required: '协作需要人工核对', completed: '协作已完成', succeeded: '协作已完成', failed: '协作失败，请查看运行进度', cancelled: '协作已取消', blocked: '协作受阻，请查看运行进度' } as Record<string, string>)[status] || '已提交，状态待确认';
}

export function localStateLabel(status: string): string {
  return ({ enabled: '已启用', disabled: '未启用', installed: '已安装', uninstalled: '已卸载', active: '正在使用', human_control: '人工操作中', closed: '已关闭', pending: '等待处理', starting: '启动中', running: '运行中', stopping: '停止中', stopped: '已停止', succeeded: '已完成', failed: '失败，请查看详情', error: '出错，请查看详情' } as Record<string, string>)[status] || '状态待确认';
}
