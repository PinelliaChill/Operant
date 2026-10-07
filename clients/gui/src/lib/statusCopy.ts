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
};

export function knownModelConnectionErrorLabel(error: string | null | undefined): string | null {
  if (!error) return null;
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
