export function projection(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(`${label} 格式无效。`);
  return value as Record<string, unknown>;
}

export function projectionItems(value: unknown, label: string): Record<string, unknown>[] {
  const page = projection(value, label);
  if (!Array.isArray(page.items)) throw new Error(`${label} 列表格式无效。`);
  return page.items.map((item) => projection(item, label));
}

export function requiredText(value: unknown, label: string): string {
  if (typeof value !== 'string' || !value) throw new Error(`${label} 缺失。`);
  return value;
}

export function optionalText(value: unknown): string | undefined {
  return typeof value === 'string' && value ? value : undefined;
}

export function stringList(value: unknown, label: string): string[] {
  if (!Array.isArray(value) || value.some((item) => typeof item !== 'string')) throw new Error(`${label} 格式无效。`);
  return value;
}

export function requestError(value: unknown): string {
  if (value instanceof Error) return value.message;
  return 'Core 请求失败。';
}

export function requestCode(value: unknown): string | undefined {
  if (!value || typeof value !== 'object') return undefined;
  const source = value as Record<string, unknown>;
  const detail = errorDetail(source);
  return optionalText(detail.code) ?? optionalText(source.code);
}

export function approvalId(value: unknown): string | undefined {
  if (!value || typeof value !== 'object') return undefined;
  const source = value as Record<string, unknown>;
  const detail = errorDetail(source);
  return optionalText(source.approval_id) ?? optionalText(source.approvalId) ?? optionalText(detail.approval_id);
}

function errorDetail(source: Record<string, unknown>): Record<string, unknown> {
  const outer = source.detail && typeof source.detail === 'object' && !Array.isArray(source.detail)
    ? source.detail as Record<string, unknown> : {};
  return outer.detail && typeof outer.detail === 'object' && !Array.isArray(outer.detail)
    ? outer.detail as Record<string, unknown> : outer;
}

export function idempotencyKey(): string {
  return crypto.randomUUID();
}

export const LOCAL_EXTENSION_CHANGE_EVENT = 'operant:local-extension-change';

export function outcomeNeedsReconciliation(value: unknown): boolean {
  const code = requestCode(value);
  return code === 'outcome_unknown' || code === 'command_outcome_unknown' || code === 'command_in_progress' || code === 'transport_unavailable' || code === 'invalid_error_envelope'
    || (Boolean(value) && typeof value === 'object' && ((value as Record<string, unknown>).recovery === 'manual_reconcile'
      || (value as Record<string, unknown>).outcomeUnknown === true));
}
