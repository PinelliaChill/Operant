import React from 'react';
import { LucideIcon, Inbox } from 'lucide-react';

interface EmptyStateProps {
  icon?: LucideIcon;
  title: string;
  description?: string;
  action?: React.ReactNode;
  /** 标题元素级别：默认 h4（区块内空态）；页面级空态（所在页无其他 h1）可升 h1 */
  titleAs?: 'h1' | 'h4';
}

export const EmptyState: React.FC<EmptyStateProps> = ({
  icon: Icon = Inbox,
  title,
  description,
  action,
  titleAs: TitleTag = 'h4',
}) => {
  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        padding: 'var(--space-6) var(--space-4)',
        textAlign: 'center',
        color: 'var(--text-secondary)',
      }}
    >
      <div
        style={{
          width: 48,
          height: 48,
          borderRadius: '50%',
          backgroundColor: 'var(--bg-subtle)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          color: 'var(--accent-action)',
          marginBottom: 'var(--space-4)',
        }}
      >
        <Icon size={24} />
      </div>
      <TitleTag
        style={{
          margin: 0,
          marginBottom: 'var(--space-2)',
          fontSize: TitleTag === 'h1' ? 'var(--font-size-title)' : 'var(--font-size-body)',
          fontWeight: 600,
          lineHeight: 'var(--line-height-heading)',
          color: 'var(--text-primary)',
        }}
      >
        {title}
      </TitleTag>
      {description && (
        <p
          style={{
            maxWidth: 360,
            margin: 0,
            marginBottom: action ? 'var(--space-4)' : 0,
            fontSize: 'var(--font-size-sm)',
            lineHeight: 'var(--line-height-body)',
            color: 'var(--text-secondary)',
          }}
        >
          {description}
        </p>
      )}
      {action && <div>{action}</div>}
    </div>
  );
};
