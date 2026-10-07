export async function refreshThenOpenTeamReply(
  refresh: () => Promise<boolean>,
  isCurrent: () => boolean,
  open: () => void,
): Promise<'opened' | 'refresh_failed' | 'stale'> {
  let refreshed = false;
  try { refreshed = await refresh(); } catch { refreshed = false; }
  if (!isCurrent()) return 'stale';
  if (!refreshed) return 'refresh_failed';
  open();
  return 'opened';
}
