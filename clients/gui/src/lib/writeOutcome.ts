/** A failed write may have committed before a proxy or transport lost its response. */
export function isWriteOutcomeUnknown(error: unknown): boolean {
  if (!error || typeof error !== 'object') return true;
  const value = error as { code?: unknown; recovery?: unknown };
  if (typeof value.code !== 'string' || typeof value.recovery !== 'string') return true;
  if (['manual_reconcile', 'retry_same_idempotency_key', 'retry_later', 'retry'].includes(value.recovery)) return true;
  if (/^http_5\d\d$/.test(value.code) || ['http_408', 'http_425', 'http_429'].includes(value.code)) return true;
  if (['transport_unavailable', 'invalid_error_envelope', 'command_in_progress', 'command_outcome_unknown', 'outcome_unknown'].includes(value.code)) return true;
  return false;
}
