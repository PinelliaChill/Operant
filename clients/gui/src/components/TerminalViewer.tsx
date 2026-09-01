import React, { useState } from 'react';
import { Terminal, Copy, Check, Play, Shield } from 'lucide-react';
import { StatusBadge } from './StatusBadge';

interface TerminalViewerProps {
  command?: string;
  output?: string;
  exitCode?: number;
  runner?: 'host' | 'docker';
  executionTimeMs?: number;
}

export const TerminalViewer: React.FC<TerminalViewerProps> = ({
  command,
  output,
  exitCode = 0,
  runner = 'host',
  executionTimeMs,
}) => {
  const [copied, setCopied] = useState(false);

  const handleCopy = () => {
    navigator.clipboard.writeText(`${command ? `$ ${command}\n` : ''}${output || ''}`);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div
      style={{
        borderRadius: 'var(--radius-md)',
        border: '1px solid var(--border-subtle)',
        backgroundColor: 'var(--terminal-bg)',
        color: 'var(--terminal-text-strong)',
        overflow: 'hidden',
        display: 'flex',
        flexDirection: 'column',
        fontFamily: 'var(--font-mono)',
        fontSize: '12px',
        boxShadow: 'var(--shadow-md)',
      }}
    >
      <div
        style={{
          padding: '8px 12px',
          backgroundColor: 'var(--terminal-header)',
          borderBottom: '1px solid var(--terminal-border)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <Terminal size={14} color="var(--terminal-link)" />
          <span style={{ color: 'var(--terminal-text-strong)', fontWeight: 600 }}>命令执行</span>
          <span
            style={{
              fontSize: '11px',
              padding: '1px 6px',
              borderRadius: '4px',
              backgroundColor: runner === 'docker' ? '#1e3a8a' : '#14532d',
              color: runner === 'docker' ? '#93c5fd' : '#86efac',
              display: 'inline-flex',
              alignItems: 'center',
              gap: 3,
            }}
          >
            <Shield size={10} />
            {runner === 'docker' ? 'Docker 执行器' : '主机执行器'}
          </span>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          {executionTimeMs !== undefined && (
            <span style={{ color: 'var(--terminal-muted)', fontSize: '11px' }}>{executionTimeMs}ms</span>
          )}
          <StatusBadge
            status={exitCode === 0 ? 'succeeded' : 'failed'}
            label={`退出码 ${exitCode}`}
            size="sm"
          />
          <button
            onClick={handleCopy}
            className="btn btn-ghost btn-sm"
            style={{ color: 'var(--terminal-text)', padding: '2px 6px', fontSize: '11px' }}
            title="复制命令与输出"
            aria-label={copied ? '已复制命令与输出' : '复制命令与输出'}
          >
            {copied ? <Check size={12} color="var(--terminal-success)" /> : <Copy size={12} />}
          </button>
        </div>
      </div>

      {command && (
        <div
          style={{
            padding: '10px 14px',
            backgroundColor: 'var(--terminal-command-bg)',
            borderBottom: '1px solid var(--terminal-border)',
            color: 'var(--terminal-link)',
            display: 'flex',
            alignItems: 'center',
            gap: 8,
          }}
        >
          <Play size={12} color="var(--terminal-success)" />
          <span style={{ color: 'var(--terminal-muted)' }}>$</span>
          <span style={{ color: 'var(--terminal-text-strong)', fontWeight: 500 }}>{command}</span>
        </div>
      )}

      <div
        style={{
          padding: '12px 14px',
          maxHeight: '320px',
          overflowY: 'auto',
          lineHeight: 1.6,
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
          color: 'var(--terminal-text)',
        }}
      >
        {output || <span style={{ color: 'var(--terminal-faint)', fontStyle: 'italic' }}>未记录标准输出。</span>}
      </div>
    </div>
  );
};
