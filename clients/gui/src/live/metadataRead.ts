import type { ConversationMetadata } from '../../../../sdk/typescript-client/onboarding';

export type MetadataReadResult =
  | { status: 'applied'; items: ConversationMetadata[] }
  | { status: 'superseded' }
  | { status: 'failed'; scope: 'list' | 'selected'; error: unknown; items?: ConversationMetadata[] };

interface MetadataReader {
  listConversationMetadata(options: { limit: number }): Promise<{ items: ConversationMetadata[] }>;
  getConversationMetadata(threadId: string): Promise<ConversationMetadata>;
}

/** A newer title read owns the UI; an older completion is neither success nor failure. */
export async function readConversationMetadata(
  client: MetadataReader,
  selectedThreadId: string | null,
  isCurrent: () => boolean,
): Promise<MetadataReadResult> {
  try {
    const page = await client.listConversationMetadata({ limit: 500 });
    let selected: ConversationMetadata | null = null;
    let selectedError: unknown = null;
    if (selectedThreadId) {
      try { selected = await client.getConversationMetadata(selectedThreadId); }
      catch (error: unknown) { selectedError = error; }
    }
    if (!isCurrent()) return { status: 'superseded' };
    const items = selected ? [...page.items, selected] : page.items;
    if (selectedError) return { status: 'failed', scope: 'selected', error: selectedError, items };
    return { status: 'applied', items };
  } catch (error: unknown) {
    return isCurrent() ? { status: 'failed', scope: 'list', error } : { status: 'superseded' };
  }
}
