import type { WorkflowDefinition } from '../../../../../sdk/typescript-client/phase23.generated';

const BASIC_ROLE_NAMES: Record<string, string> = {
  role_planner: '规划',
  role_coder: '编程',
  role_reviewer: '审查',
};

export function graphNodeDisplayName(definition: WorkflowDefinition | null, nodeId: string): string {
  const spec = definition?.nodes.find((node) => node.node_id === nodeId);
  if (!spec) return nodeId;
  const roleId = spec.metadata?.role_id;
  if (definition?.name === '基础协作团队' && spec.node_kind === 'agent' && typeof roleId === 'string') {
    const basicName = BASIC_ROLE_NAMES[roleId];
    if (basicName) return basicName;
  }
  for (const key of ['display_name', 'label', 'title']) {
    const value = spec.metadata?.[key];
    if (typeof value === 'string' && value.trim()) return value.trim();
  }
  return spec.node_id;
}
