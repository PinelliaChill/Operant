import React, { useEffect, useState } from 'react';
import { formatPairingTicket, parsePairingTicket, type CallerPairingTicket } from '../../lib/pairedSkillSourceTransport';

/** Native confirmation happens inside create_caller_pairing_ticket. */
export const CallerPairingTicketPanel: React.FC = () => {
  const [ticket, setTicket] = useState<CallerPairingTicket | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!ticket) return;
    const delay = Math.max(0, ticket.expires_at * 1000 - Date.now());
    const timer = window.setTimeout(() => { setTicket(null); setCopied(false); }, delay);
    return () => window.clearTimeout(timer);
  }, [ticket]);

  const create = async () => {
    if (busy) return;
    setBusy(true); setTicket(null); setCopied(false); setError('');
    try {
      const { invoke } = await import('@tauri-apps/api/core');
      const next = await invoke<CallerPairingTicket>('create_caller_pairing_ticket');
      setTicket(parsePairingTicket(formatPairingTicket(next)));
    } catch (cause: unknown) {
      if (cause === '已取消配对。' || (cause instanceof Error && cause.message === '已取消配对。')) return;
      setError('无法生成配对码，请确认桌面 Core 已连接后重试。');
    } finally { setBusy(false); }
  };

  const copy = async () => {
    if (!ticket) return;
    try { await navigator.clipboard.writeText(formatPairingTicket(ticket)); setCopied(true); }
    catch { setError('复制失败，请手动复制下方配对信息。'); }
  };

  return <section className="skill-sources-card">
    <h2>连接其他客户端</h2>
    <p>在桌面版确认后，将短时配对码粘贴到这台电脑的浏览器或终端中。</p>
    <button type="button" className="btn btn-secondary" onClick={() => { void create(); }} disabled={busy}>
      {busy ? '正在生成…' : '连接其他客户端'}
    </button>
    {error && <p role="alert">{error}</p>}
    {ticket && <div className="skill-sources-ticket">
      <p>有效期至 {new Date(ticket.expires_at * 1000).toLocaleTimeString()}。信息只能用于一次配对。</p>
      <textarea readOnly className="textarea" aria-label="短时配对码" value={formatPairingTicket(ticket)} />
      <button type="button" className="btn btn-ghost btn-sm" onClick={() => { void copy(); }}>
        {copied ? '已复制' : '复制配对信息'}
      </button>
    </div>}
  </section>;
};
