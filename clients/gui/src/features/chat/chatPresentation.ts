/** Presentation only: action handlers still enforce Core capability/connection guards. */
export function chatEmptyPresentation(input: {
  failed: boolean;
  deepLinkNotFound: boolean;
  hasProjects: boolean;
  hasProject: boolean;
}) {
  if (input.failed) return {
    title: '暂时无法连接本地服务',
    description: '请检查服务状态后重试。连接恢复前不会显示演示数据或提交任务。',
    action: 'reconnect' as const, label: '重新连接',
  };
  if (input.deepLinkNotFound) return {
    title: '没有找到这个会话',
    description: '当前服务未返回此会话。请从侧栏选择其他会话，或刷新后再试。',
    action: 'none' as const, label: '',
  };
  if (!input.hasProjects) return {
    title: '从一个项目开始',
    description: '先添加或选择可访问的项目，再创建会话开展工作。',
    action: 'projects' as const, label: '管理项目',
  };
  if (!input.hasProject) return {
    title: '选择你的工作项目',
    description: '在上方选择项目，或从侧栏继续已有会话。',
    action: 'select-project' as const, label: '选择项目',
  };
  return {
    title: '开始一段新会话',
    description: '会话保存在当前项目中。创建后选择角色，即可发送任务。',
    action: 'create-thread' as const, label: '新建会话',
  };
}

export function historyItemLabel(type: string) {
  const labels: Record<string, string> = {
    user_message: '你', agent_message: '助手', tool_call: '工具调用',
    tool_result_ref: '工具结果', artifact_ref: '工件', approval_link: '审批',
    steering: '运行指令', system_event: '运行记录',
  };
  return labels[type] ?? type;
}
