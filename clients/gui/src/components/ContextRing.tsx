import React from 'react';
import { Layers, Sparkles } from 'lucide-react';
import { ContextRevision } from '@operant/sdk';
import { formatNumber } from '../lib/format';

interface ContextRingProps {
  revision?: ContextRevision;
  onClick?: () => void;
  size?: number;
  strokeWidth?: number;
  /** 演示模式下隐藏无法验证的缓存命中率指标 */
  hideCacheMetric?: boolean;
}

export const ContextRing: React.FC<ContextRingProps> = ({
  revision,
  onClick,
  size = 56,
  strokeWidth = 5,
  hideCacheMetric = false,
}) => {
  const total = revision?.total_tokens ?? 0;
  const limit = revision?.context_window_limit ?? 128000;
  const ratio = Math.min(1, total / Math.max(limit, 1));
  const percent = Math.round(ratio * 100);

  const radius = (size - strokeWidth) / 2;
  const circumference = 2 * Math.PI * radius;
  const offset = circumference - ratio * circumference;

  let strokeColor = 'var(--accent-action)';
  if (ratio > 0.85) strokeColor = 'var(--status-error-text)';
  else if (ratio > 0.65) strokeColor = 'var(--status-warn-text)';

  const formattedTokens = total > 1000 ? `${(total / 1000).toFixed(1)}k` : `${total}`;
  const formattedLimit = limit > 1000 ? `${Math.round(limit / 1000)}k` : `${limit}`;

  return (
    <button
      onClick={onClick}
      className="btn btn-ghost"
      title={`上下文窗口：${formatNumber(total)} / ${formatNumber(limit)} tokens（${percent}%）。点击查看分解。`}
      aria-label={`上下文窗口已使用 ${percent}%，点击查看分解`}
      style={{
        padding: '4px 8px',
        display: 'flex',
        alignItems: 'center',
        gap: 10,
        borderRadius: 'var(--radius-md)',
        border: '1px solid var(--border-subtle)',
        background: 'var(--bg-card)',
        cursor: 'pointer',
      }}
    >
      <div style={{ position: 'relative', width: size, height: size, flexShrink: 0 }}>
        <svg width={size} height={size} style={{ transform: 'rotate(-90deg)' }}>
          <circle
            cx={size / 2}
            cy={size / 2}
            r={radius}
            fill="transparent"
            stroke="var(--border-subtle)"
            strokeWidth={strokeWidth}
          />
          <circle
            cx={size / 2}
            cy={size / 2}
            r={radius}
            fill="transparent"
            stroke={strokeColor}
            strokeWidth={strokeWidth}
            strokeDasharray={circumference}
            strokeDashoffset={offset}
            strokeLinecap="round"
            style={{ transition: 'stroke-dashoffset 0.4s ease, stroke 0.3s ease' }}
          />
        </svg>
        <div
          style={{
            position: 'absolute',
            inset: 0,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            fontSize: '11px',
            fontWeight: 700,
            color: 'var(--text-primary)',
          }}
        >
          {percent}%
        </div>
      </div>

      <div className="context-ring-details" style={{ flexDirection: 'column', alignItems: 'flex-start', textAlign: 'left' }}>
        <div style={{ fontSize: '11px', color: 'var(--text-muted)', display: 'flex', alignItems: 'center', gap: 4 }}>
          <Layers size={12} />
          <span>上下文窗口</span>
        </div>
        <div style={{ fontSize: '12px', fontWeight: 600, color: 'var(--text-primary)' }}>
          {formattedTokens} / {formattedLimit}
        </div>
        {!hideCacheMetric && revision?.cache_hit_rate !== undefined && (
          <div style={{ fontSize: '11px', color: 'var(--accent-action)', display: 'flex', alignItems: 'center', gap: 2 }}>
            <Sparkles size={10} />
            <span>{(revision.cache_hit_rate * 100).toFixed(0)}% 已缓存</span>
          </div>
        )}
      </div>
    </button>
  );
};
