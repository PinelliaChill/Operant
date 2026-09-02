import React from 'react';
import {
  CheckCircle2,
  AlertTriangle,
  XCircle,
  Clock,
  Loader2,
  ShieldAlert,
  Radio,
  PauseCircle,
  FileEdit,
} from 'lucide-react';

export type BadgeStatus =
  | 'active'
  | 'running'
  | 'succeeded'
  | 'completed'
  | 'failed'
  | 'waiting'
  | 'waiting_approval'
  | 'pending'
  | 'interrupted'
  | 'cancelled'
  | 'denied'
  | 'safe'
  | 'warn'
  | 'critical'
  | 'high'
  | 'medium'
  | 'low'
  | 'connected'
  | 'disconnected'
  | 'mock_active'
  | 'inactive'
  | 'paused'
  | 'draft'
  | 'published'
  | 'granted'
  | 'revoked';

/** status → 中文标签映射（调用处传英文 status 也显示中文；label 属性可覆盖） */
export const STATUS_LABELS: Record<string, string> = {
  active: '活跃',
  running: '运行中',
  succeeded: '已完成',
  completed: '已完成',
  failed: '已失败',
  waiting: '待处理',
  waiting_approval: '等待审批',
  pending: '待处理',
  interrupted: '已中断',
  cancelled: '已取消',
  denied: '已拒绝',
  safe: '安全',
  warn: '警告',
  critical: '严重风险',
  high: '高风险',
  medium: '中风险',
  low: '低风险',
  connected: '已连接',
  disconnected: '未连接',
  mock_active: '演示数据',
  inactive: '已停用',
  paused: '已暂停',
  draft: '草稿',
  published: '已发布',
  granted: '已授权',
  revoked: '已撤销',
};

interface StatusBadgeProps {
  status: BadgeStatus | string;
  label?: string;
  size?: 'sm' | 'md';
  pulse?: boolean;
}

export const StatusBadge: React.FC<StatusBadgeProps> = ({
  status,
  label,
  size = 'md',
  pulse = false,
}) => {
  const normalized = status.toLowerCase();

  let bg = 'var(--bg-subtle)';
  let border = 'var(--border-subtle)';
  let text = 'var(--text-secondary)';
  let Icon = Clock;
  const displayLabel = label || STATUS_LABELS[normalized] || status;

  switch (normalized) {
    case 'active':
    case 'succeeded':
    case 'completed':
    case 'safe':
    case 'connected':
    case 'published':
    case 'granted':
      bg = 'var(--status-safe-bg)';
      border = 'var(--status-safe-border)';
      text = 'var(--status-safe-text)';
      Icon = CheckCircle2;
      break;

    case 'running':
      // running 使用 info 蓝系，不再使用品牌绿
      bg = 'var(--status-info-bg)';
      border = 'var(--status-info-border)';
      text = 'var(--status-info-text)';
      Icon = Loader2;
      break;

    case 'waiting':
    case 'waiting_approval':
    case 'pending':
    case 'warn':
    case 'interrupted':
    case 'medium':
      bg = 'var(--status-warn-bg)';
      border = 'var(--status-warn-border)';
      text = 'var(--status-warn-text)';
      Icon = AlertTriangle;
      break;

    case 'failed':
    case 'cancelled':
    case 'denied':
    case 'critical':
    case 'high':
    case 'disconnected':
    case 'revoked':
      bg = 'var(--status-error-bg)';
      border = 'var(--status-error-border)';
      text = 'var(--status-error-text)';
      Icon = normalized === 'denied' || normalized === 'revoked' ? ShieldAlert : XCircle;
      break;

    case 'low':
      bg = 'var(--status-info-bg)';
      border = 'var(--status-info-border)';
      text = 'var(--status-info-text)';
      Icon = AlertTriangle;
      break;

    case 'mock_active':
      bg = 'var(--status-info-bg)';
      border = 'var(--status-info-border)';
      text = 'var(--status-info-text)';
      Icon = Radio;
      break;

    case 'paused':
    case 'inactive':
      Icon = PauseCircle;
      break;

    case 'draft':
      Icon = FileEdit;
      break;
  }

  const isSpinning = normalized === 'running';

  return (
    <span
      className="badge"
      style={{
        backgroundColor: bg,
        borderColor: border,
        color: text,
        padding: size === 'sm' ? '2px 6px' : '3px 8px',
        fontSize: size === 'sm' ? '11px' : '12px',
      }}
      aria-label={`状态：${displayLabel}`}
    >
      <Icon
        size={size === 'sm' ? 12 : 14}
        className={isSpinning ? 'animate-spin' : undefined}
        style={{ flexShrink: 0 }}
      />
      {pulse && (
        <span
          style={{
            width: 6,
            height: 6,
            borderRadius: '50%',
            backgroundColor: text,
            display: 'inline-block',
          }}
        />
      )}
      <span>{displayLabel}</span>
    </span>
  );
};
