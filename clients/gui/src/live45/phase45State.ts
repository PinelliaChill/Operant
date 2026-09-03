import type { McpLifecycle } from './phase45Adapter';

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
