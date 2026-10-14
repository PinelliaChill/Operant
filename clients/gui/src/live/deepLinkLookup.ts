export function deepLinkLookupDecision(
  targetId: string | null,
  projected: boolean,
  connected: boolean,
  attemptedId: string | null,
): 'empty' | 'found' | 'disconnected' | 'refresh' | 'await_result' {
  if (!targetId) return 'empty';
  if (projected) return 'found';
  if (!connected) return 'disconnected';
  return attemptedId === targetId ? 'await_result' : 'refresh';
}
