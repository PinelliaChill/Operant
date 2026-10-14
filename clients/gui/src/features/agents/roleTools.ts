import type * as B2 from '../../../../../sdk/typescript-client/b2.generated';

export const ROLE_TOOL_GROUPS = [
  {
    title: '只读工作区',
    tools: [
      { id: 'read_file', label: '读取文件' },
      { id: 'search_files', label: '搜索文件' },
      { id: 'git_diff', label: '查看 Git 差异' },
    ],
  },
  {
    title: '会话协作',
    tools: [
      { id: 'delegate_agent', label: '创建子 Agent' },
      { id: 'send_agent_message', label: '发送定向消息' },
      { id: 'wait_for_agent', label: '等待子 Agent 结果' },
    ],
  },
] as const;

export type SelectableRoleTool = (typeof ROLE_TOOL_GROUPS)[number]['tools'][number]['id'];

const selectable = ROLE_TOOL_GROUPS.flatMap((group) => group.tools.map((tool) => tool.id));
const selectableSet = new Set<string>(selectable);

export function selectedRoleTools(policy: B2.ToolPolicy | undefined): SelectableRoleTool[] {
  const allowed = new Set(policy?.allowed_tools ?? []);
  return selectable.filter((tool) => allowed.has(tool));
}

/** Change only the six visible tool grants; preserve all other policy fields. */
export function policyWithRoleTools(
  current: B2.ToolPolicy | undefined,
  selected: readonly SelectableRoleTool[],
): B2.ToolPolicy {
  const selectedSet = new Set(selected);
  const otherTools = (current?.allowed_tools ?? []).filter((tool) => !selectableSet.has(tool));
  return {
    ...(current ?? { workspace_write: false, command_execution: false }),
    allowed_tools: [...otherTools, ...selectable.filter((tool) => selectedSet.has(tool))],
  };
}
