import type { Phase45Client } from '../../../../sdk/typescript-client/phase45.generated.ts';

export type ApprovalDecisionClient = Pick<Phase45Client, 'decidePhase45Approval' | 'getPhase45Approval'>;
export type ConfirmedApprovalStatus = 'approved' | 'denied' | 'consumed' | 'expired';

/** A failed POST is never repeated. Only the original approval can settle it. */
export async function readApprovalDecision(client: ApprovalDecisionClient, id: string): Promise<ConfirmedApprovalStatus | null> {
  try {
    const value = await client.getPhase45Approval(id);
    if (value.approval_id !== id) return null;
    return ['approved', 'denied', 'consumed', 'expired'].includes(String(value.status))
      ? value.status as ConfirmedApprovalStatus : null;
  } catch { return null; }
}

export async function submitApprovalDecision(client: ApprovalDecisionClient, id: string, approved: boolean): Promise<ConfirmedApprovalStatus | null> {
  try {
    await client.decidePhase45Approval(id, { approved, reason_code: approved ? 'human_approved' : 'human_denied' }, { idempotencyKey: crypto.randomUUID() });
  } catch { /* Preserve the unknown result, then read this exact approval. */ }
  return readApprovalDecision(client, id);
}
