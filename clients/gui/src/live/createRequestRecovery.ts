import type { ConversationMetadata } from '../../../../sdk/typescript-client/onboarding.generated';

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
