/** Confirmed management responses refresh Core projections, including late responses. */
export async function synchronizeProjectMutation(action: string, status: string, read: () => Promise<boolean>): Promise<boolean> {
  if (!['project_create', 'project_update', 'project_archive', 'project_detach'].includes(action) || ['failed', 'blocked'].includes(status)) return true;
  return read();
}

export function projectSelectionStorageKey(origin: string): string {
  return `operant.selected-project.v1:${new URL(origin).origin}`;
}
export function readProjectSelection(storage: { getItem: (key: string) => string | null }, origin: string): string | null {
  try { const value = storage.getItem(projectSelectionStorageKey(origin)); return value && value.length <= 300 ? value : null; }
  catch { return null; }
}

/** Resolve only the exact canonical selection; never retain a previously active workspace. */
export function selectionWorkspace(
  projects: readonly { id: string; workspaceRef: string }[],
  threads: readonly { id: string; workspaceRef: string | null }[],
  projectId: string | null,
  threadId: string | null,
): string {
  if (projectId) return projects.find((project) => project.id === projectId)?.workspaceRef || '';
  return threads.find((thread) => thread.id === threadId)?.workspaceRef || '';
}
