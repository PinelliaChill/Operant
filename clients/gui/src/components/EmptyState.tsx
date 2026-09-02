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
        padding: '48px 24px',
        textAlign: 'center',
        color: 'var(--text-muted)',
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
          marginBottom: 16,
        }}
      >
        <Icon size={24} />
      </div>
      <TitleTag style={{ fontSize: '15px', fontWeight: 600, color: 'var(--text-primary)', marginBottom: 6 }}>
        {title}
      </TitleTag>
      {description && (
        <p style={{ fontSize: '13px', maxWidth: 360, lineHeight: 1.5, marginBottom: action ? 16 : 0 }}>
          {description}
        </p>
      )}
      {action && <div>{action}</div>}
    </div>
  );
};
