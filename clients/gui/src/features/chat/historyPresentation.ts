const TOOL_ACTIONS: Record<string, string> = {
  read_file: '读取文件',
  write_file: '修改文件',
  apply_patch: '修改文件',
  run_command: '运行命令',
  search_files: '搜索文件',
  git_diff: '查看代码变更',
};

export function toolActionLabel(toolName: string): string {
  return TOOL_ACTIONS[toolName] || '使用工具';
}

export function toolResultLabel(summary: string | null | undefined, outcome: 'completed' | 'failed' | 'outcome_unknown'): string {
  const tool = Object.keys(TOOL_ACTIONS).find((name) => summary?.includes(name));
  const action = tool ? TOOL_ACTIONS[tool] : '操作';
  return outcome === 'completed' ? `${action}已完成` : outcome === 'failed' ? `${action}失败` : `${action}结果待核对`;
}

export function systemEventLabel(eventType: string, summary: string | null | undefined): string {
  const event = eventType.toLowerCase();
  const detail = (summary || '').toLowerCase();
  if (detail.includes('/skill:') || event.includes('skill.')) {
    if (event.includes('fail') || event.includes('error') || detail.includes('fail') || detail.includes('error')) return '技能执行失败';
    if (event.includes('complet') || event.includes('succeed')) return '技能已完成';
    return '技能开始';
  }
  if (event === 'agent.started' || event === 'session.started') return '开始处理';
  if (event === 'agent.timed_out' || event === 'session.timed_out') return '任务超时';
  if (event === 'agent.completed' || event === 'session.completed') return '任务已完成';
  if (event === 'agent.failed' || event === 'session.run_failed') return '任务执行失败';
  if (event === 'tool.completed') return '工具已完成';
  if (event === 'tool.failed') return '工具执行失败';
  if (event === 'tool.started') return '开始使用工具';
  return '运行记录';
}
