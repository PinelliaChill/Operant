import type { LiveThread } from '../../live/liveState';

export interface ThreadTreeRow {
  thread: LiveThread;
  depth: number;
  hasChildren: boolean;
}

/** Arrange Core Thread relationships for display without changing their status or ownership. */
export function visibleThreadTree(
  threads: LiveThread[],
  query: string,
  collapsedIds: readonly string[],
): ThreadTreeRow[] {
  const byId = new Map(threads.map((thread) => [thread.id, thread]));
  const children = new Map<string, LiveThread[]>();
  for (const thread of threads) {
    const parentId = thread.parentThreadId;
    if (parentId && parentId !== thread.id && byId.has(parentId)) {
      children.set(parentId, [...(children.get(parentId) ?? []), thread]);
    }
  }
  const visible = new Set<string>();
  if (query) {
    for (const thread of threads) {
      if (!thread.title.toLowerCase().includes(query) && !thread.id.toLowerCase().includes(query)) continue;
      let current: LiveThread | undefined = thread;
      const seen = new Set<string>();
      while (current && !seen.has(current.id)) {
        seen.add(current.id);
        visible.add(current.id);
        current = current.parentThreadId ? byId.get(current.parentThreadId) : undefined;
      }
    }
  } else {
    for (const thread of threads) visible.add(thread.id);
  }
  const rows: ThreadTreeRow[] = [];
  const visited = new Set<string>();
  const append = (thread: LiveThread, depth: number) => {
    if (visited.has(thread.id) || !visible.has(thread.id)) return;
    visited.add(thread.id);
    const descendants = (children.get(thread.id) ?? []).filter((child) => visible.has(child.id));
    rows.push({ thread, depth, hasChildren: descendants.length > 0 });
    if (query || !collapsedIds.includes(thread.id)) {
      for (const child of descendants) append(child, depth + 1);
    }
  };
  const roots = threads.filter((thread) => !thread.parentThreadId || !byId.has(thread.parentThreadId) || thread.parentThreadId === thread.id);
  for (const root of roots) append(root, 0);
  // Cycles cannot hide Core Threads; collapsed descendants still stay hidden.
  for (const thread of threads) {
    if (visited.has(thread.id)) continue;
    let current: LiveThread | undefined = thread;
    const ancestors = new Set<string>();
    while (current?.parentThreadId && byId.has(current.parentThreadId) && !ancestors.has(current.id)) {
      ancestors.add(current.id);
      current = byId.get(current.parentThreadId);
    }
    if (current && ancestors.has(current.id)) append(thread, 0);
  }
  return rows;
}
