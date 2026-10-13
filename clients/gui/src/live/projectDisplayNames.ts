import type { B23ManagedProject } from './b23Adapter';

type NamedProject = Pick<B23ManagedProject, 'workspace_id' | 'name' | 'archived'>;

/** Phase 1E project IDs are workspace IDs; only an unambiguous managed name may replace the path label. */
export function projectDisplayNames(projects: readonly NamedProject[]): Record<string, string> {
  const names: Record<string, string> = {};
  const ambiguous = new Set<string>();
  for (const project of projects) {
    if (project.archived || !project.name.trim()) continue;
    if (names[project.workspace_id]) ambiguous.add(project.workspace_id);
    else names[project.workspace_id] = project.name;
  }
  for (const id of ambiguous) delete names[id];
  return names;
}

export async function readProjectDisplayNames(adapter: {
  connect: () => Promise<unknown>;
  getManagement: () => Promise<{ projects: NamedProject[] }>;
}): Promise<Record<string, string>> {
  await adapter.connect();
  return projectDisplayNames((await adapter.getManagement()).projects);
}
