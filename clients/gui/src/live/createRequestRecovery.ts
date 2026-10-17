import type { ConversationInitialization, ConversationMetadata } from '../../../../sdk/typescript-client/onboarding.generated';

export function isConversationInitialization(value: unknown): value is ConversationInitialization {
  if (!value || typeof value !== 'object') return false;
  const result = value as Record<string, unknown>;
  return ['thread_id', 'session_id', 'workspace_id'].every((key) =>
    typeof result[key] === 'string' && result[key].length > 0)
    && typeof result.title === 'string';
}

export function recoveredCreateIsOpen(
  recoveredId: string | null,
  routeId: string | undefined,
  selectedThreadId: string | null,
  createOutcomeUnknown: boolean,
): boolean {
  return !createOutcomeUnknown && Boolean(recoveredId)
    && recoveredId === routeId && recoveredId === selectedThreadId;
}

export function createRequestStorageKey(coreOrigin: string): string {
  return `operant.setup.pending-conversation:${encodeURIComponent(coreOrigin)}`;
}

export function confirmedCreateMetadata(items: ConversationMetadata[]): ConversationMetadata | null {
  return items.length === 1 && items[0].thread_id ? items[0] : null;
}

export function mergeConversationMetadata(
  current: Record<string, ConversationMetadata>,
  incoming: ConversationMetadata[],
): Record<string, ConversationMetadata> {
  const merged = { ...current };
  for (const item of incoming) {
    if (!merged[item.thread_id] || item.revision >= merged[item.thread_id].revision) merged[item.thread_id] = item;
  }
  return merged;
}

export function renameIsConfirmed(metadata: ConversationMetadata, requestedTitle: string): boolean {
  return metadata.title === requestedTitle;
}

/** A read-only title warning must never hide an unresolved write result. */
export function visibleOnboardingError(createError: string, metadataWarning: string): string {
  return createError || metadataWarning;
}

export interface PendingCreateRequest { requestId: string; draftScope?: string }
/** Recovery tickets contain identity references only; draft text stays in the bounded draft store. */
export function encodeCreateRequest(request: PendingCreateRequest): string {
  return JSON.stringify({ version: 1, requestId: request.requestId, ...(request.draftScope ? { draftScope: request.draftScope } : {}) });
}
export function decodeCreateRequest(raw: string | null, origin: string): PendingCreateRequest | null {
  if (!raw) return null;
  if (!raw.startsWith('{')) return { requestId: raw }; // Existing installations stored the request ID alone.
  try {
    const value = JSON.parse(raw);
    if (value.version !== 1 || typeof value.requestId !== 'string' || !value.requestId || value.requestId.length > 300) return { requestId: raw };
    const scope = typeof value.draftScope === 'string' ? JSON.parse(value.draftScope) : null;
    const validScope = Array.isArray(scope) && scope.length === 3 && scope[0] === new URL(origin).origin && scope[1] === 'new' && typeof scope[2] === 'string';
    return { requestId: value.requestId, ...(validScope ? { draftScope: value.draftScope } : {}) };
  } catch { return { requestId: raw }; }
}
