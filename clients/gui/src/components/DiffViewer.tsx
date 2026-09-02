import React, { useState } from 'react';
import { Copy, Check, FileCode } from 'lucide-react';

interface DiffViewerProps {
  diffText: string;
  filePath?: string;
  title?: string;
}

export const DiffViewer: React.FC<DiffViewerProps> = ({
  diffText,
  filePath,
  title,
}) => {
  const [copied, setCopied] = useState(false);

  const handleCopy = () => {
    navigator.clipboard.writeText(diffText);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const lines = (diffText || '').split('\n');

  return (
    <div
      style={{
        borderRadius: 'var(--radius-md)',
        border: '1px solid var(--border-subtle)',
        backgroundColor: 'var(--bg-card)',
        overflow: 'hidden',
        display: 'flex',
        flexDirection: 'column',
        fontFamily: 'var(--font-mono)',
        fontSize: '12px',
      }}
    >
      <div
        style={{
          padding: '8px 12px',
          backgroundColor: 'var(--bg-surface)',
          borderBottom: '1px solid var(--border-subtle)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, color: 'var(--text-primary)', fontWeight: 600 }}>
          <FileCode size={14} color="var(--accent-action)" />
          <span>{filePath || title || '统一差异预览'}</span>
        </div>
        <button
          onClick={handleCopy}
          className="btn btn-ghost btn-sm"
          style={{ padding: '2px 6px', fontSize: '11px' }}
          title="复制原始差异"
          aria-label={copied ? '已复制差异' : '复制原始差异'}
        >
          {copied ? <Check size={12} color="var(--status-safe-text)" /> : <Copy size={12} />}
          <span>{copied ? '已复制' : '复制'}</span>
        </button>
      </div>

      <div
        style={{
          overflowX: 'auto',
          maxHeight: '420px',
          padding: '8px 0',
          backgroundColor: 'var(--bg-app)',
          lineHeight: 1.6,
        }}
      >
        {lines.map((line, index) => {
          let lineBg = 'transparent';
          let textColor = 'var(--text-secondary)';

          if (line.startsWith('+') && !line.startsWith('+++')) {
            lineBg = 'var(--diff-add-bg)';
            textColor = 'var(--diff-add-text)';
          } else if (line.startsWith('-') && !line.startsWith('---')) {
            lineBg = 'var(--diff-del-bg)';
            textColor = 'var(--diff-del-text)';
          } else if (line.startsWith('@@')) {
            lineBg = 'var(--bg-subtle)';
            textColor = 'var(--accent-action)';
          }

          return (
            <div
              key={index}
              style={{
                display: 'flex',
                backgroundColor: lineBg,
                color: textColor,
                padding: '0 12px',
                whiteSpace: 'pre',
              }}
            >
              <span
                style={{
                  width: '36px',
                  userSelect: 'none',
                  color: 'var(--text-muted)',
                  textAlign: 'right',
                  paddingRight: '12px',
                  opacity: 0.6,
                }}
              >
                {index + 1}
              </span>
              <span style={{ flex: 1 }}>{line}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
};
