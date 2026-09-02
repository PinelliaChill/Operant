/**
 * 统一日期/时间/数字格式化工具
 * 全项目禁止使用裸 toLocaleString / toLocaleTimeString / toLocaleDateString，
 * 一律改用此处的显式 zh-CN Intl 格式化器。
 */

const dateTimeFormatter = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
});

const timeFormatter = new Intl.DateTimeFormat('zh-CN', {
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
});

const dateFormatter = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
});

const numberFormatter = new Intl.NumberFormat('zh-CN');

function toDate(input: string | number | Date): Date {
  return input instanceof Date ? input : new Date(input);
}

/** 日期 + 时间，如 2026/08/27 14:30 */
export function formatDateTime(input: string | number | Date): string {
  return dateTimeFormatter.format(toDate(input));
}

/** 仅时间，如 14:30 */
export function formatTime(input: string | number | Date): string {
  return timeFormatter.format(toDate(input));
}

/** 仅日期，如 2026/08/27 */
export function formatDate(input: string | number | Date): string {
  return dateFormatter.format(toDate(input));
}

/** 千分位数字，如 128,000 */
export function formatNumber(value: number): string {
  return numberFormatter.format(value);
}

const WEEKDAY_LABELS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];

function startOfDayMs(d: Date): number {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
}

/**
 * 会话列表相对日期标签：今天 / 昨天 / 周X（一周内）/ 上周（两周内）/ 具体日期。
 * 用于会话侧栏"近期会话"右侧时间标签。
 */
export function formatRelativeDay(input: string | number | Date): string {
  const date = toDate(input);
  const diffDays = Math.round((startOfDayMs(new Date()) - startOfDayMs(date)) / 86_400_000);
  if (diffDays <= 0) return '今天';
  if (diffDays === 1) return '昨天';
  if (diffDays < 7) return WEEKDAY_LABELS[date.getDay()];
  if (diffDays < 14) return '上周';
  return formatDate(input);
}

/** 工作流阶段（WorkflowStage）→ 中文标签 */
export const WORKFLOW_STAGE_LABELS: Record<string, string> = {
  created: '已创建',
  planner: '规划',
  explorers: '探索',
  coder: '编码',
  reviewer: '评审',
  main: '主控',
  completed: '已完成',
};

/** 格式化工作流阶段为中文标签（未知值原样返回） */
export function formatStage(stage: string): string {
  return WORKFLOW_STAGE_LABELS[stage] || stage;
}

/** 推理强度（effort）→ 中文标签 */
export const EFFORT_LABELS: Record<string, string> = {
  low: '低',
  medium: '中',
  high: '高',
};

/** 格式化推理强度为中文标签（未知值原样返回） */
export function formatEffort(effort: string): string {
  return EFFORT_LABELS[effort] || effort;
}

/** 远程传输模式 → 中文标签（仅显示层映射，原始协议值不变） */
export const TRANSPORT_MODE_LABELS: Record<string, string> = {
  direct_lan: '局域网直连',
  direct_vpn: 'VPN 直连',
  self_hosted_relay: '自托管中继',
};

/** 格式化远程传输模式为中文标签（未知值原样返回） */
export function formatTransportMode(mode: string): string {
  return TRANSPORT_MODE_LABELS[mode] || mode;
}
