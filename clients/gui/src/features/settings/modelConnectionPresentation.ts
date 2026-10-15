export function groupModelConnections<T extends { provider: string }>(connections: T[]): { visible: T[]; legacy: T[] } {
  return {
    visible: connections.filter((connection) => connection.provider !== 'chatgpt'),
    legacy: connections.filter((connection) => connection.provider === 'chatgpt'),
  };
}

export function pendingOAuthUrl(attempt: { status: string; authorization_url?: string | null } | null | undefined): string | null {
  if (attempt?.status !== 'pending') return null;
  return attempt.authorization_url?.trim() || null;
}
