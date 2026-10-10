import { terminalRunOutcome, type LiveEvent, type LiveThread } from './liveState.ts';

/** A stable key for title reads. Model delta frames must not trigger a read. */
export function metadataRefreshKey(threads: readonly LiveThread[], selectedThreadId: string | null, events: readonly LiveEvent[]): string {
  const collection = threads.map((thread) => `${thread.id}:${thread.updatedAt}:${thread.title}`).join('|');
  const terminal = [...events].reverse().find((event) => terminalRunOutcome(event) !== null);
  return `${collection}\n${selectedThreadId ?? ''}\n${terminal ? `${terminal.id}:${terminal.sequence}` : ''}`;
}
