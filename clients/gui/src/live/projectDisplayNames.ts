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

/** A workspace can outlive its managed project; lifecycle labels come from Core management. */
export function projectWorkspaceMetadata(projects: readonly NamedProject[]): { names: Record<string, string>; archivedWorkspaceIds: string[] } {
  const names = projectDisplayNames(projects);
  const active = new Set(projects.filter((project) => !project.archived).map((project) => project.workspace_id));
  const archivedWorkspaceIds = [...new Set(projects.filter((project) => project.archived && !active.has(project.workspace_id)).map((project) => project.workspace_id))];
  for (const workspace of archivedWorkspaceIds) {
    const archived = projects.filter((project) => project.workspace_id === workspace && project.archived);
    names[workspace] = `${archived.length === 1 && archived[0].name.trim() ? archived[0].name : '项目'}（已归档）`;
  }
  return { names, archivedWorkspaceIds };
}
export async function readProjectWorkspaceMetadata(adapter: {
  connect: () => Promise<unknown>;
  getManagement: () => Promise<{ projects: NamedProject[] }>;
}): Promise<ReturnType<typeof projectWorkspaceMetadata>> {
  await adapter.connect();
  return projectWorkspaceMetadata((await adapter.getManagement()).projects);
}
