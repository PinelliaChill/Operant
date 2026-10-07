export interface ControlSessionChoice {
  sessionId: string;
  pluginId: string;
  state: string;
  computerBundleId?: string;
  allowedTargets?: string[];
}

export type ControlSessionDecision =
  | { kind: 'reuse'; sessionId: string }
  | { kind: 'open' }
  | { kind: 'blocked'; reason: string };

/** A manual console lease is never silently attached to a new conversation. */
export function chooseConversationControlSession(
  pluginId: string,
  computerBundleId: string | undefined,
  expectedTargets: readonly string[],
  conversationSessions: readonly ControlSessionChoice[],
  manualSessions: readonly ControlSessionChoice[],
): ControlSessionDecision {
  if (manualSessions.some((session) => session.pluginId === pluginId && session.state !== 'closed')) {
    return { kind: 'blocked', reason: '此能力正在手动操控台使用。请先结束该控制会话，再为新对话开启。' };
  }
  const current = conversationSessions.filter((session) => session.pluginId === pluginId && session.state !== 'closed');
  if (current.length > 1) return { kind: 'blocked', reason: '发现多个控制会话。请刷新并核对后再继续。' };
  const session = current[0];
  if (!session) return { kind: 'open' };
  if (session.state !== 'active') return { kind: 'blocked', reason: '此前的控制会话尚未结束。请先在本机操控台核对并结束。' };
  if (!sameControlTargets(session.allowedTargets, expectedTargets)) return { kind: 'blocked', reason: '已有控制会话的授权范围与当前选择不同。请刷新核对后再继续。' };
  if (pluginId === 'operant.macos.computer' && session.computerBundleId !== computerBundleId) {
    return { kind: 'blocked', reason: '此能力已连接另一应用。请先结束原控制会话，再选择新应用。' };
  }
  return { kind: 'reuse', sessionId: session.sessionId };
}

export function controlOpenStorageKey(coreOrigin: string): string {
  return `operant.setup.pending-local-control:${encodeURIComponent(coreOrigin)}`;
}

export interface PendingControlOpen {
  key: string;
  pluginId: string;
  computerBundleId?: string;
  expectedTargets: string[];
  approvalId?: string;
  decisionSubmitted?: boolean;
}

export function controlApprovalAction(
  status: string | null,
  decisionSubmitted: boolean,
): 'decide' | 'continue' | 'read_only' {
  if (status === 'pending' && !decisionSubmitted) return 'decide';
  if (status === 'approved') return 'continue';
  return 'read_only';
}

export function sameControlTargets(actual: readonly string[] | undefined, expected: readonly string[]): boolean {
  if (!actual || actual.length !== expected.length) return false;
  const sortedExpected = [...expected].sort();
  return [...actual].sort().every((target, index) => target === sortedExpected[index]);
}

export function confirmedControlOpen(
  items: readonly ControlSessionChoice[],
  pending: PendingControlOpen,
): ControlSessionChoice | null {
  if (items.length !== 1) return null;
  const session = items[0];
  return session.pluginId === pending.pluginId
    && session.computerBundleId === pending.computerBundleId
    && sameControlTargets(session.allowedTargets, pending.expectedTargets)
    ? session : null;
}

export function openedControlCanStartConversation(
  session: ControlSessionChoice,
  request: PendingControlOpen,
): boolean {
  return session.state === 'active' && confirmedControlOpen([session], request) !== null;
}

export function readPendingControlOpen(raw: string | null): PendingControlOpen | null {
  if (!raw) return null;
  try {
    const value: unknown = JSON.parse(raw);
    if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
    const record = value as Record<string, unknown>;
    if (typeof record.key !== 'string' || !record.key || typeof record.pluginId !== 'string' || !record.pluginId) return null;
    if (record.computerBundleId !== undefined && typeof record.computerBundleId !== 'string') return null;
    if (!Array.isArray(record.expectedTargets) || record.expectedTargets.some((target) => typeof target !== 'string')) return null;
    if (record.approvalId !== undefined && typeof record.approvalId !== 'string') return null;
    if (record.decisionSubmitted !== undefined && typeof record.decisionSubmitted !== 'boolean') return null;
    return { key: record.key, pluginId: record.pluginId, expectedTargets: record.expectedTargets as string[], ...(record.computerBundleId ? { computerBundleId: record.computerBundleId } : {}), ...(record.approvalId ? { approvalId: record.approvalId } : {}), ...(record.decisionSubmitted ? { decisionSubmitted: true } : {}) };
  } catch { return null; }
}
