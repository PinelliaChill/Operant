import type { McpLifecycle } from './phase45Adapter';
import type { LiveSecurityAuditFact } from './phase45Adapter';

export interface McpActionState {
  canStart: boolean;
  canStop: boolean;
  canDelete: boolean;
  canRefreshTools: boolean;
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
