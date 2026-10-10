import { requestErrorCopy } from './requestErrorCopy.ts';

const pairingErrors: Record<string, string> = {
  pairing_rejected: '配对未通过。请在桌面版生成新的配对码，并核对是否使用同一台电脑。',
  epoch_changed: '桌面 Core 已重新启动。请在桌面版确认重新配对，再核对原请求。',
  device_unavailable: '当前配对已失效。请在桌面版重新配对，再核对原请求。',
  receipt_unavailable: '暂时找不到原请求回执。请保留原请求，稍后再核对。',
  approval_not_granted: '桌面审批尚未通过。请先在桌面版完成审批，再继续原请求。',
  approval_binding_changed: '审批与原请求不一致。请重新核对，暂时不要再次提交。',
  request_identity_changed: '原请求标识与内容不一致。请保留原请求并核对。',
  nonce_replayed: '这次请求已使用。请按原请求刷新核对。',
};

/** Only whitelisted codes enter the ordinary page; raw response stays hidden. */
export function pairedErrorCopy(error: unknown): string {
  if (error && typeof error === 'object') {
    const source = error as { code?: unknown; detail?: unknown };
    const payload = source.detail;
    if (payload && typeof payload === 'object') {
      const detail = (payload as { detail?: unknown }).detail;
      if (detail && typeof detail === 'object') {
        const code = (detail as { code?: unknown }).code;
        if (typeof code === 'string' && pairingErrors[code]) return pairingErrors[code];
      }
    }
  }
  return requestErrorCopy(error);
}
