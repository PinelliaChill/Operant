import type { ReferenceRequest } from '../../../../sdk/typescript-client/phase1e.generated';

export interface PendingConversationSend {
  threadId: string;
  workspaceId: string;
  workspaceRef: string;
  sourceConversationId: string | null;
  text: string;
  references: ReferenceRequest[];
  referencesRevision: number;
  draftRevision: number;
}

export function bindPendingConversationSend(input: PendingConversationSend): PendingConversationSend {
  return {
    ...input,
    text: input.text.trim(),
    references: input.references.map((reference) => ({ ...reference })),
  };
}

export function shouldQueuePendingSend(sendRequested: boolean, text: string, workspaceId: string): boolean {
  return sendRequested && Boolean(workspaceId) && Boolean(text.trim()) && !text.trim().startsWith('/');
}

export function pendingSendMatchesSelection(
  pending: PendingConversationSend,
  conversationId: string | null,
  selectedThreadId: string | null,
  selectedWorkspaceRef: string | null,
): boolean {
  return conversationId === pending.threadId
    && selectedThreadId === pending.threadId
    && Boolean(selectedWorkspaceRef)
    && (!pending.workspaceRef || pending.workspaceRef === selectedWorkspaceRef);
}

export function shouldCancelPendingOnRoute(
  pending: PendingConversationSend,
  pathname: string,
  targetVisited: boolean,
): boolean {
  const target = `/chat/${encodeURIComponent(pending.threadId)}`;
  if (pathname === target) return false;
  const source = pending.sourceConversationId ? `/chat/${encodeURIComponent(pending.sourceConversationId)}` : '/chat';
  return pathname !== source || targetVisited;
}

export function draftAfterAccepted(
  current: string,
  currentRevision: number,
  pending: Pick<PendingConversationSend, 'text' | 'draftRevision'>,
): string {
  return currentRevision === pending.draftRevision && current.trim() === pending.text ? '' : current;
}
