import type { McpLifecycle } from './phase45Adapter';
import type { LiveSecurityAuditFact } from './phase45Adapter';

export interface McpActionState {
  canStart: boolean;
  canStop: boolean;
  canDelete: boolean;
  canRefreshTools: boolean;
}

export type Phase45LoadPhase = 'idle' | 'loading' | 'ready' | 'error';
export type Phase45ConnectionStatus = 'connected' | 'reconnecting' | 'disconnected' | 'mock_active';

export function phase45SideEffectsDisabled(
  phase: Phase45LoadPhase,
  connectionStatus: Phase45ConnectionStatus,
  busy: boolean,
): boolean {
  return busy || phase !== 'ready' || connectionStatus !== 'connected';
}

export function phase45UnavailableMessage(
  phase: Phase45LoadPhase,
  connectionStatus: Phase45ConnectionStatus,
  diagnosticCode?: string,
): string | undefined {
  if (connectionStatus !== 'connected') {
    return 'Core 连接已断开；已保留上次只读 Projection，配置、审批、调用和 Receipt 操作均已禁用。';
  }
  if (phase === 'error') {
    const diagnostic = diagnosticCode ? `诊断码：${diagnosticCode}。` : '';
    return `Core 暂时不可用；已保留上次只读 Projection，所有 MCP 副作用入口均已禁用。${diagnostic}`;
  }
  return undefined;
}

/** Refresh failure never substitutes Demo data or clears the last safe projection. */
export function preservePhase45Projection<T>(current: T): T {
  return current;
}

export function mcpActionState(lifecycle: McpLifecycle, busy: boolean): McpActionState {
  if (busy) {
    return { canStart: false, canStop: false, canDelete: false, canRefreshTools: false };
  }
  return {
    canStart: lifecycle === 'stopped' || lifecycle === 'failed',
    canStop: lifecycle === 'running',
    canDelete: lifecycle === 'stopped',
    canRefreshTools: lifecycle === 'running',
  };
}

export function policyDecisionStatus(
  decision: 'allow' | 'ask' | 'deny',
): 'safe' | 'pending' | 'denied' {
  if (decision === 'allow') return 'safe';
  if (decision === 'deny') return 'denied';
  return 'pending';
}

export function boundedAuditFacts(
  facts: LiveSecurityAuditFact[],
  limit: number,
): LiveSecurityAuditFact[] {
  if (!Number.isSafeInteger(limit) || limit < 1) throw new TypeError('audit limit must be positive');
  return facts.slice(-limit);
}
