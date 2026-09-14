/**
 * Operant 2.0 - B2-4 协作与上下文视觉组件 (UI-COLLAB-03)
 * 职责：Antigravity（纯视觉组件与呈现层）
 * 稳定契约组件：B24Section, B24Status, B24Empty
 * 样式入口：../../styles/b2-collaboration.css
 */

import React, { useId, type ReactNode } from 'react';
import '../../styles/b2-collaboration.css';

export interface B24SectionProps {
  title: string;
  description?: string;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}

/**
 * 协作工作台统一卡片分区组件
 * 支持语义化标题、描述性副文本、头部操作栏及内容插槽
 */
export const B24Section: React.FC<B24SectionProps> = ({
  title,
  description,
  actions,
  children,
  className,
}) => {
  const headingId = useId();
  const sectionClasses = ['b24-card', 'b24-section', className].filter(Boolean).join(' ');

  return (
    <section className={sectionClasses} aria-labelledby={headingId}>
      <div className="b24-section-header">
        <div className="b24-section-title-group">
          <h2 id={headingId} className="b24-section-title">
            {title}
          </h2>
          {description && <p className="b24-section-description">{description}</p>}
        </div>
        {actions && <div className="b24-section-actions">{actions}</div>}
      </div>
      <div className="b24-section-body">{children}</div>
    </section>
  );
};

export type B24StatusTone = 'neutral' | 'success' | 'warning' | 'danger';

export interface B24StatusProps {
  label: string;
  tone?: B24StatusTone;
  className?: string;
}

/**
 * 状态胶囊徽标组件
 * 包含圆点指示符与语义标签，遵循 WCAG AA 颜色对比度
 */
export const B24Status: React.FC<B24StatusProps> = ({
  label,
  tone = 'neutral',
  className,
}) => {
  const statusClasses = ['b24-status', `b24-status-${tone}`, className].filter(Boolean).join(' ');

  return (
    <span className={statusClasses} data-tone={tone}>
      <span className="b24-status-dot" aria-hidden="true" />
      <span className="b24-status-label">{label}</span>
    </span>
  );
};

export interface B24EmptyProps {
  title: string;
  description?: string;
  className?: string;
}

/**
 * 空状态占位提示组件
 */
export const B24Empty: React.FC<B24EmptyProps> = ({
  title,
  description,
  className,
}) => {
  const emptyClasses = ['b24-empty', className].filter(Boolean).join(' ');

  return (
    <div className={emptyClasses} role="status">
      <div className="b24-empty-title">{title}</div>
      {description && <div className="b24-empty-description">{description}</div>}
    </div>
  );
};
