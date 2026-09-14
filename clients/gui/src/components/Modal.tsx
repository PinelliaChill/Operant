import './b2-modal.css';
import React, { useId } from 'react';
import { createPortal } from 'react-dom';
import { X } from 'lucide-react';
import { useDialogA11y } from './useDialogA11y';

interface ModalProps {
  isOpen: boolean;
  onClose: () => void;
  title: React.ReactNode;
  children: React.ReactNode;
  footer?: React.ReactNode;
  maxWidth?: string | number;
  portal?: boolean;
}

export const Modal: React.FC<ModalProps> = ({
  isOpen,
  onClose,
  title,
  children,
  footer,
  maxWidth = 560,
  portal = false,
}) => {
  const panelRef = useDialogA11y<HTMLDivElement>(isOpen, onClose);
  const titleId = `modal-title-${useId().replace(/:/g, '')}`;

  if (!isOpen) return null;

  const content = (
    <div
      className={portal ? "b2-modal-overlay" : undefined}
      style={{
        position: 'fixed',
        inset: 0,
        backgroundColor: 'rgba(28, 25, 23, 0.45)',
        backdropFilter: 'blur(4px)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        zIndex: 100,
        padding: 16,
        animation: 'fadeIn var(--dur-2) var(--ease-standard)',
      }}
      onClick={onClose}
    >
      <div
        ref={panelRef}
        className={portal ? "card b2-modal-dialog" : "card"}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        style={{
          width: '100%',
          maxWidth,
          maxHeight: '90vh',
          display: 'flex',
          flexDirection: 'column',
          padding: 0,
          boxShadow: 'var(--shadow-lg)',
          overflow: 'hidden',
          backgroundColor: 'var(--bg-card)',
          border: '1px solid var(--border-subtle)',
          animation: 'scaleIn var(--dur-2) var(--ease-out)',
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <div
          className={portal ? "b2-modal-heading" : undefined}
          style={{
            padding: '14px 18px',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            borderBottom: '1px solid var(--border-subtle)',
            backgroundColor: 'var(--bg-surface)',
          }}
        >
          <h3 id={titleId} style={{ fontSize: '15px', fontWeight: 600, color: 'var(--text-primary)' }}>{title}</h3>
          <button
            onClick={onClose}
            className="btn btn-ghost btn-icon"
            aria-label="关闭对话框"
            style={{ padding: 4 }}
          >
            <X size={18} />
          </button>
        </div>

        <div className={portal ? "b2-modal-body" : undefined} style={{ padding: '18px', overflowY: 'auto', flex: 1 }}>{children}</div>

        {footer && (
          <div
            className={portal ? "b2-modal-footer" : undefined}
            style={{
              padding: '12px 18px',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'flex-end',
              gap: 8,
              borderTop: '1px solid var(--border-subtle)',
              backgroundColor: 'var(--bg-surface)',
            }}
          >
            {footer}
          </div>
        )}
      </div>
    </div>
  );
  return portal ? createPortal(content, document.body) : content;
};
