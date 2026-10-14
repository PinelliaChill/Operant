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
    title: '开始一段新对话',
    description: '直接输入任务，通用助手会开始处理。',
    action: 'create-thread' as const, label: '新建对话',
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

/** The service puts the tool name first and hides argument values in approval details. */
export function approvalActionLabel(detail: string, category: string): string {
  const tool = /^([a-z][a-z0-9_]*)(?=\s|;|$)/.exec(detail.trim())?.[1];
  const names: Record<string, string> = {
    run_command: '运行命令',
    file_write: '修改文件',
    write_file: '修改文件',
    apply_patch: '修改文件',
    read_file: '读取文件',
    search_files: '查找文件',
    ext_browser_observe: '查看网页',
    ext_browser_navigate: '打开网页',
    ext_browser_fill: '填写网页',
    ext_browser_click: '点击网页按钮',
    ext_browser_press_key: '在网页中按键',
    ext_browser_capture_viewport: '截取网页画面',
    ext_computer_observe: '读取窗口',
    ext_computer_click_button: '点击窗口按钮',
    ext_computer_type_text: '输入文本',
    ext_computer_press_key: '在窗口中按键',
    ext_computer_capture_window: '截取窗口画面',
    ext_computer_read_clipboard: '读取剪贴板',
    ext_computer_write_clipboard: '写入剪贴板',
  };
  return (tool && names[tool]) || (category === 'network_access' ? '访问网络' : '执行受保护操作');
}
