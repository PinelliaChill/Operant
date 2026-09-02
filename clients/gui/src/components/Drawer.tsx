import React, { useEffect, useId } from 'react';
import { X } from 'lucide-react';
import { useDialogA11y } from './useDialogA11y';

interface DrawerProps {
  isOpen: boolean;
  onClose: () => void;
  title: React.ReactNode;
  children: React.ReactNode;
  side?: 'right' | 'left';
  width?: string | number;
  /** 覆盖内容区样式（如侧栏抽屉需要无内边距 + 自带滚动区时） */
  bodyStyle?: React.CSSProperties;
}

export const Drawer: React.FC<DrawerProps> = ({
  isOpen,
  onClose,
  title,
  children,
  side = 'right',
  width = 460,
  bodyStyle,
}) => {
  const panelRef = useDialogA11y<HTMLDivElement>(isOpen, onClose);
  const titleId = `drawer-title-${useId().replace(/:/g, '')}`;

  // 打开时禁止 body 滚动
  useEffect(() => {
    if (!isOpen) return;
    document.body.classList.add('no-scroll');
    return () => {
      document.body.classList.remove('no-scroll');
    };
  }, [isOpen]);

  if (!isOpen) return null;

  return (
    <div
      style={{
        position: 'fixed',
        inset: 0,
        backgroundColor: 'rgba(28, 25, 23, 0.45)',
        backdropFilter: 'blur(2px)',
        zIndex: 90,
        display: 'flex',
        justifyContent: side === 'right' ? 'flex-end' : 'flex-start',
        animation: 'fadeIn var(--dur-2) var(--ease-standard)',
      }}
      onClick={onClose}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        style={{
          width,
          maxWidth: '90vw',
          height: '100%',
          backgroundColor: 'var(--bg-card)',
          borderLeft: side === 'right' ? '1px solid var(--border-subtle)' : undefined,
          borderRight: side === 'left' ? '1px solid var(--border-subtle)' : undefined,
          boxShadow: 'var(--shadow-lg)',
          display: 'flex',
          flexDirection: 'column',
          overflow: 'hidden',
          animation: `${side === 'right' ? 'slideInRight' : 'slideInLeft'} var(--dur-2) var(--ease-out)`,
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <div
          style={{
            height: 52,
            padding: '0 16px',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            borderBottom: '1px solid var(--border-subtle)',
            backgroundColor: 'var(--bg-surface)',
          }}
        >
          <h2 id={titleId} style={{ margin: 0, fontSize: '14px', fontWeight: 600, color: 'var(--text-primary)' }}>{title}</h2>
          <button onClick={onClose} className="btn btn-ghost btn-icon" aria-label="关闭抽屉" style={{ padding: 4 }}>
            <X size={18} />
          </button>
        </div>

        <div style={{ flex: 1, overflowY: 'auto', padding: 16, ...bodyStyle }}>{children}</div>
      </div>
    </div>
  );
};
